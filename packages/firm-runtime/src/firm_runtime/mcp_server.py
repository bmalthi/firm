"""MCP role tools shared by the trusted stdio and authenticated HTTP transports."""

import argparse
import inspect
import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .errors import ConfigurationError, FirmError
from .prompt_resources import render
from .role_output import render_tool_result
from .runtime import Runtime
from .store import Store

MANDATE_GUIDANCE = render("tools", "mandate_guidance")

TOOL_DESCRIPTIONS = {
    "task": render("tools", "task"),
    "send": render("tools", "send"),
    "reply": render("tools", "reply"),
    "peek": render("tools", "peek"),
    "claim": render("tools", "claim"),
    "dismiss": render("tools", "dismiss"),
    "clear": render("tools", "clear"),
    "messages": render("tools", "messages"),
    "context": render("tools", "context"),
    "set_reminder": render("tools", "set_reminder"),
    "list_reminders": render("tools", "list_reminders"),
    "cancel_reminder": render("tools", "cancel_reminder"),
    "hire": render("tools", "hire", mandate_guidance=MANDATE_GUIDANCE),
    "renew_temporary": render("tools", "renew_temporary"),
    "promote": render("tools", "promote"),
    "get_role": render("tools", "get_role"),
    "find_roles": render("tools", "find_roles"),
    "org_tree": render("tools", "org_tree"),
    "revise_mandate": render("tools", "revise_mandate", mandate_guidance=MANDATE_GUIDANCE),
    "fire": render("tools", "fire"),
    "retry": render("tools", "retry"),
    "handoff": render("tools", "handoff"),
    "create_repository": render("tools", "create_repository"),
    "request_access": render("tools", "request_access"),
    **{
        name: render("tools", name)
        for name in (
            "mac_acquire",
            "mac_release",
            "mac_status",
            "desktop_status",
            "desktop_apps",
            "desktop_windows",
            "desktop_inspect",
            "desktop_launch",
            "desktop_action",
            "desktop_focus",
            "desktop_type",
            "desktop_press_key",
            "desktop_capture",
            "desktop_click",
            "desktop_drag",
            "desktop_scroll",
            "desktop_tap_sequence",
            "desktop_close_apps",
        )
    },
}


def _rendered(method, name):
    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def async_call(*args, **kwargs):
            return render_tool_result(name, await method(*args, **kwargs))

        return async_call

    @wraps(method)
    def call(*args, **kwargs):
        try:
            result = method(*args, **kwargs)
        except ConfigurationError:
            raise FirmError(render("tools", "configuration_error")) from None
        if name == "desktop_capture":
            result = dict(result)
            png = result.pop("png_base64", None)
            content = [TextContent(type="text", text=render_tool_result(name, result))]
            if png is not None:
                content.append(ImageContent(type="image", data=png, mimeType="image/png"))
            return content
        return render_tool_result(name, result)

    return call


def register_tools(server: FastMCP, runtime: Runtime, token: str | Callable[[], str]) -> None:
    """Register role-only tools, resolving dynamic HTTP credentials at call time."""

    signature_facade = runtime.tools("")
    readonly = {
        "peek",
        "messages",
        "context",
        "get_role",
        "find_roles",
        "org_tree",
        "list_reminders",
        "mac_status",
        "desktop_status",
        "desktop_apps",
        "desktop_windows",
        "desktop_inspect",
    }
    for name, description in TOOL_DESCRIPTIONS.items():
        signature_method = getattr(signature_facade, name)

        if callable(token):

            @wraps(signature_method)
            def invoke(*args, _name=name, **kwargs):
                return getattr(runtime.tools(token()), _name)(*args, **kwargs)

            method = invoke
            if inspect.iscoroutinefunction(signature_method):

                @wraps(signature_method)
                async def async_invoke(*args, _name=name, **kwargs):
                    return await getattr(runtime.tools(token()), _name)(*args, **kwargs)

                method = async_invoke
        else:
            method = getattr(runtime.tools(token), name)

        server.tool(
            name=name,
            description=description,
            annotations=ToolAnnotations(
                readOnlyHint=name in readonly,
                destructiveHint=name
                in {
                    "task",
                    "fire",
                    "mac_release",
                    "desktop_action",
                    "desktop_type",
                    "desktop_press_key",
                    "desktop_close_apps",
                    "desktop_click",
                    "desktop_drag",
                    "desktop_scroll",
                    "desktop_tap_sequence",
                },
                idempotentHint=name
                not in {
                    "desktop_launch",
                    "desktop_action",
                    "desktop_type",
                    "desktop_press_key",
                    "desktop_capture",
                    "desktop_click",
                    "desktop_drag",
                    "desktop_scroll",
                    "desktop_tap_sequence",
                },
                openWorldHint=(name.startswith("desktop_") and name != "desktop_status")
                or name
                in {
                    "task",
                    "mac_acquire",
                    "mac_release",
                    "create_repository",
                    "request_access",
                },
            ),
            structured_output=False,
        )(_rendered(method, name))
        # FastMCP otherwise silently drops unknown arguments. Fail closed so a
        # guessed actor/resource selector is never mistaken for an accepted choice.
        registered = server._tool_manager.get_tool(name)
        registered.fn_metadata.arg_model.model_config["extra"] = "forbid"
        registered.fn_metadata.arg_model.model_rebuild(force=True)
        registered.parameters = registered.fn_metadata.arg_model.model_json_schema()


def build_server(runtime: Runtime, token: str) -> FastMCP:
    server = FastMCP("firm-role-tools")
    register_tools(server, runtime, token)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    args = parser.parse_args()
    token = os.environ.get("FIRM_ACTIVATION_TOKEN")
    if not token:
        parser.error("FIRM_ACTIVATION_TOKEN is required")
    from .compatibility import validate_firm

    try:
        validate_firm(Path(args.database).resolve().parent.parent)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    runtime = Runtime(Store(args.database))
    runtime.authenticate(token)
    build_server(runtime, token).run(transport="stdio")
