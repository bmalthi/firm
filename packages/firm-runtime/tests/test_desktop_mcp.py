"""The desktop tools keep host authority private and return native MCP images."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from firm_runtime.errors import FirmError
from firm_runtime.mcp_server import build_server
from firm_runtime.tools import RoleTools

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AwMCAO+aT1cAAAAASUVORK5CYII="
)


class Lease:
    def __init__(self):
        self.live_token = "live-activation"
        self.calls = []

    def call(self, token, op, **arguments):
        if token != self.live_token:
            raise FirmError("invalid or expired activation token")
        self.calls.append((token, op, arguments))
        result = {
            "status": "ok",
            "operations_control": {"instruction": "Save your work before finishing."},
        }
        if op == "capture_window":
            result.update(
                png_base64=PNG, width=1, height=1,
                drive_path="org/founder/work/captures/20260916T142305Z-textedit.png",
            )
        return result


def server_for(desktop):
    runtime = SimpleNamespace(mac=desktop)
    runtime.tools = lambda token: RoleTools(runtime, token)
    return build_server(runtime, "live-activation")


def test_desktop_mcp_authority_schema_and_effect_annotations():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        tools = {tool.name: tool for tool in await server.list_tools()}
        names = {name for name in tools if name.startswith("desktop_")}
        assert names == {
            "desktop_status", "desktop_apps", "desktop_windows", "desktop_inspect",
            "desktop_launch", "desktop_action", "desktop_focus", "desktop_type",
            "desktop_press_key",
            "desktop_capture", "desktop_click", "desktop_drag", "desktop_scroll",
            "desktop_tap_sequence", "desktop_close_apps",
        }
        for name in names:
            schema = tools[name].inputSchema
            assert schema["additionalProperties"] is False
            assert not {
                "uid", "account", "owner", "token", "activation_id", "socket", "employee_id",
            } & schema.get("properties", {}).keys()
        assert "maxLength" not in tools["desktop_type"].inputSchema["properties"]["text"]
        launch_schema = tools["desktop_launch"].inputSchema
        assert launch_schema["properties"]["launch_mode"]["enum"] == [
            "launch_services", "direct",
        ]
        assert launch_schema["properties"]["launch_mode"]["default"] == "launch_services"
        assert launch_schema["required"] == ["path"]
        inspect_schema = tools["desktop_inspect"].inputSchema
        handle_schema = inspect_schema["properties"]["handle"]
        assert {item.get("type") for item in handle_schema["anyOf"]} == {"string", "null"}
        assert "handle" not in inspect_schema["required"]
        assert tools["desktop_status"].annotations.readOnlyHint is True
        # Every capture also saves a new file on the drive.
        assert tools["desktop_capture"].annotations.readOnlyHint is False
        assert tools["desktop_capture"].annotations.destructiveHint is False
        assert tools["desktop_capture"].annotations.idempotentHint is False
        for name in (
            "desktop_launch", "desktop_action", "desktop_type", "desktop_click", "desktop_drag",
            "desktop_scroll", "desktop_press_key", "desktop_tap_sequence",
        ):
            assert tools[name].annotations.readOnlyHint is False
            assert tools[name].annotations.idempotentHint is False
            assert tools[name].annotations.openWorldHint is True
        assert tools["desktop_close_apps"].annotations.destructiveHint is True

        with pytest.raises(Exception, match="Extra inputs"):
            await server.call_tool("desktop_apps", {"uid": 501})
        assert desktop.calls == []
        await server.call_tool("desktop_status", {})
        assert desktop.calls == [("live-activation", "status", {})]
        await server.call_tool("desktop_launch", {"path": "/Applications/One.app"})
        await server.call_tool("desktop_launch", {
            "path": "/Applications/DriveProbe.app", "launch_mode": "direct",
        })
        assert [call[1:] for call in desktop.calls[-2:]] == [
            ("launch_app", {"path": "/Applications/One.app",
                             "launch_mode": "launch_services"}),
            ("launch_app", {"path": "/Applications/DriveProbe.app",
                             "launch_mode": "direct"}),
        ]
        with pytest.raises(ToolError):
            await server.call_tool("desktop_launch", {
                "path": "/Applications/One.app", "launch_mode": "guess",
            })
        assert len(desktop.calls) == 3

        desktop.live_token = None
        with pytest.raises(Exception, match="expired activation"):
            await server.call_tool("desktop_apps", {})
        assert len(desktop.calls) == 3

    asyncio.run(check())


def test_desktop_shortcuts_validate_codes_and_modifiers_before_forwarding():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        definitions = {tool.name: tool for tool in await server.list_tools()}
        properties = definitions["desktop_press_key"].inputSchema["properties"]
        assert properties["key_code"]["minimum"] == 0
        assert properties["key_code"]["maximum"] == 127
        assert properties["modifiers"]["maxItems"] == 4
        for arguments in (
            {"key_code": True}, {"key_code": -1}, {"key_code": 128},
            {"key_code": "1"}, {"key_code": 1, "modifiers": ["capslock"]},
            {"key_code": 1, "modifiers": ["shift"] * 5},
        ):
            with pytest.raises(ToolError):
                await server.call_tool("desktop_press_key", {"pid": 123, **arguments})
        assert desktop.calls == []
        await server.call_tool("desktop_press_key", {"pid": 123, "key_code": 36})
        await server.call_tool("desktop_press_key", {
            "pid": 123, "key_code": 1, "modifiers": ["command"],
        })
        assert desktop.calls == [
            ("live-activation", "press_key", {"pid": 123, "key_code": 36, "modifiers": []}),
            ("live-activation", "press_key", {
                "pid": 123, "key_code": 1, "modifiers": ["command"],
            }),
        ]

    asyncio.run(check())


def test_desktop_capture_returns_image_saved_path_and_controls_without_base64_text():
    async def check():
        desktop = Lease()
        result = await server_for(desktop).call_tool(
            "desktop_capture", {"pid": 123, "window_id": 42}
        )
        assert len(result) == 2
        text, image = result
        assert isinstance(text, TextContent)
        assert "Save your work before finishing." in text.text
        assert PNG not in text.text
        assert "png_base64" not in text.text
        assert "org/founder/work/captures/20260916T142305Z-textedit.png" in text.text
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"
        assert image.data == PNG
        assert desktop.calls == [
            ("live-activation", "capture_window", {"pid": 123, "window_id": 42})
        ]

    asyncio.run(check())


def test_desktop_accessible_action_forwards_exact_handle_and_optional_focus():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        await server.call_tool("desktop_inspect", {"pid": 123})
        await server.call_tool("desktop_inspect", {
            "pid": 123, "handle": "opaque-outline", "depth": 5, "max_elements": 80,
        })
        await server.call_tool(
            "desktop_action", {"pid": 123, "handle": "opaque-control", "action": "AXPress"}
        )
        await server.call_tool("desktop_type", {"pid": 123, "text": "hello"})
        assert desktop.calls == [
            ("live-activation", "inspect_ax", {"pid": 123, "depth": 4, "max_elements": 200}),
            ("live-activation", "inspect_ax", {
                "pid": 123, "depth": 5, "max_elements": 80, "handle": "opaque-outline",
            }),
            ("live-activation", "action", {
                "pid": 123, "handle": "opaque-control", "action": "AXPress",
            }),
            ("live-activation", "type_text", {"pid": 123, "text": "hello", "handle": None}),
        ]

    asyncio.run(check())


def test_desktop_pointer_tools_require_exact_window_and_supported_button():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        definitions = {tool.name: tool for tool in await server.list_tools()}
        button = definitions["desktop_click"].inputSchema["properties"]["button"]
        assert button["enum"] == ["left", "right"]
        assert button["default"] == "left"
        with pytest.raises(Exception, match="left.*right"):
            await server.call_tool(
                "desktop_click", {"pid": 123, "window_id": 42, "x": 10, "y": 20,
                                  "button": "middle"},
            )
        with pytest.raises(Exception, match="window_id"):
            await server.call_tool("desktop_click", {"pid": 123, "x": 10, "y": 20})
        assert desktop.calls == []
        await server.call_tool(
            "desktop_click", {"pid": 123, "window_id": 42, "x": 10.5, "y": 20.5}
        )
        await server.call_tool(
            "desktop_drag", {"pid": 123, "window_id": 42, "from_x": 10, "from_y": 20,
                             "to_x": 50, "to_y": 60},
        )
        await server.call_tool(
            "desktop_scroll", {"pid": 123, "window_id": 42, "x": 10, "y": 20, "delta_y": -40}
        )
        assert desktop.calls == [
            ("live-activation", "click", {
                "pid": 123, "window_id": 42, "x": 10.5, "y": 20.5, "button": "left",
                "modifiers": [], "hold_ms": 0,
            }),
            ("live-activation", "drag", {
                "pid": 123, "window_id": 42, "from_x": 10.0, "from_y": 20.0,
                "to_x": 50.0, "to_y": 60.0,
                "modifiers": [], "hold_ms": 0, "duration_ms": None,
            }),
            ("live-activation", "scroll", {
                "pid": 123, "window_id": 42, "x": 10.0, "y": 20.0,
                "delta_x": 0, "delta_y": -40, "modifiers": [],
            }),
        ]

    asyncio.run(check())


def test_pointer_modifiers_holds_and_pacing_are_bounded_and_always_sent():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        definitions = {tool.name: tool for tool in await server.list_tools()}
        click = definitions["desktop_click"].inputSchema["properties"]
        assert click["hold_ms"]["maximum"] == 10000 and click["hold_ms"]["minimum"] == 0
        assert click["modifiers"]["maxItems"] == 4
        assert definitions["desktop_scroll"].inputSchema["properties"]["modifiers"]
        for name, arguments in (
            ("desktop_click", {"hold_ms": 10001}),
            ("desktop_click", {"hold_ms": -1}),
            ("desktop_click", {"modifiers": ["capslock"]}),
            ("desktop_drag", {"duration_ms": 49}),
            ("desktop_drag", {"duration_ms": 10001}),
        ):
            window = {"pid": 123, "window_id": 42}
            points = {"x": 1, "y": 2} if name == "desktop_click" else {
                "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4,
            }
            with pytest.raises(ToolError):
                await server.call_tool(name, {**window, **points, **arguments})
        assert desktop.calls == []
        await server.call_tool("desktop_click", {
            "pid": 123, "window_id": 42, "x": 1, "y": 2,
            "modifiers": ["option"], "hold_ms": 800,
        })
        await server.call_tool("desktop_drag", {
            "pid": 123, "window_id": 42, "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4,
            "modifiers": ["shift"], "hold_ms": 500, "duration_ms": 1200,
        })
        assert [call[2]["modifiers"] for call in desktop.calls] == [["option"], ["shift"]]
        assert desktop.calls[0][2]["hold_ms"] == 800
        assert desktop.calls[1][2]["duration_ms"] == 1200

    asyncio.run(check())


def test_tap_sequence_is_bounded_and_forwarded_as_plain_taps():
    async def check():
        desktop = Lease()
        server = server_for(desktop)
        definitions = {tool.name: tool for tool in await server.list_tools()}
        taps = definitions["desktop_tap_sequence"].inputSchema["properties"]["taps"]
        assert taps["minItems"] == 1 and taps["maxItems"] == 64
        for value in (
            [],
            [{"x": 1, "y": 2, "after_ms": 0}] * 65,
            [{"x": 1, "y": 2, "after_ms": 5001}],
            [{"x": 1, "y": 2, "after_ms": -1}],
            [{"x": 1, "y": 2}],
            [{"x": 1, "y": 2, "after_ms": 0, "button": "left"}],
        ):
            with pytest.raises(ToolError):
                await server.call_tool(
                    "desktop_tap_sequence", {"pid": 123, "window_id": 42, "taps": value}
                )
        assert desktop.calls == []
        await server.call_tool("desktop_tap_sequence", {
            "pid": 123, "window_id": 42,
            "taps": [{"x": 1, "y": 2, "after_ms": 0}, {"x": 3, "y": 4, "after_ms": 250}],
        })
        assert desktop.calls == [
            ("live-activation", "tap_sequence", {
                "pid": 123, "window_id": 42,
                "taps": [{"x": 1.0, "y": 2.0, "after_ms": 0}, {"x": 3.0, "y": 4.0,
                                                               "after_ms": 250}],
            }),
        ]

    asyncio.run(check())
