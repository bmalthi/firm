"""Experimental AGY subscription adapter for native NDJSON execution."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from firm_sandbox import RunSpec

from . import agy_launch, credential_mirror
from .provider_contract import (
    SUPPORTED_EFFORTS,
    ProviderError,
    ProviderResult,
    ProviderSetupError,
    ProviderUsage,
    category_for_code,
    safe_code,
    summary_error,
    token_count,
    tool_name,
)
from .provider_support import (
    activation_url,
    canonical_uuid,
    render_invocation,
    standing,
    summary_instructions,
    text_setting,
)
from .summaries import SummaryOutput, summary_role_id

NATIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def native_model_id(model, effort):
    """Firm stores model and effort separately; only this boundary joins them."""
    if not isinstance(effort, str) or effort not in SUPPORTED_EFFORTS["agy"]:
        raise ProviderSetupError(
            category="config",
            code="invalid_effort",
            message="AGY supports low, medium, high and the Firm xhigh mapping",
        )
    if not isinstance(model, str) or model not in {"gemini-3.8-flash", "gemini-4-argon"}:
        raise ProviderSetupError(
            category="config",
            code="invalid_model",
            message="AGY requires a supported base Gemini model without effort",
        )
    return "gemini-4-argon-high" if effort == "xhigh" else f"{model}-{effort}"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite JSON")


def _events(logs):
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    lines = raw.split("\n")
    events = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=_unique, parse_constant=_nonfinite)
        except (ValueError, RecursionError):
            # Polling may end halfway through a line. It cannot complete a turn.
            if index == len(lines) - 1 and not raw.endswith("\n"):
                return events, not any(item.get("event") == "result" for item in events)
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
    return events, True


def _stream(logs):
    events, valid = _events(logs)
    session = terminal = launch = expected = None
    for event in events:
        if event.get("type") == "firm.agy_expected_session":
            candidate = event.get("conversation_id")
            if not isinstance(candidate, str) or not NATIVE_ID.fullmatch(candidate):
                valid = False
            elif expected is not None and expected != candidate:
                valid = False
            expected = candidate
            continue
        if event.get("type") == "firm.launch_failed":
            if event.get("kind") in {"activation_gate", "launcher_setup"}:
                launch = event
            continue
        if event.get("type") == "error":
            launch = event
            continue
        kind = event.get("event")
        payload = event.get(kind) if isinstance(kind, str) else None
        if kind not in {"init", "step_update", "result"}:
            continue
        if not isinstance(payload, dict):
            valid = False
            continue
        if terminal is not None:
            valid = False
        identifier = (
            event.get("conversation_id") if kind == "init" else payload.get("conversation_id")
        )
        startup_error = (
            kind == "result"
            and session is None
            and identifier == ""
            and payload.get("status") == "ERROR"
        )
        if not startup_error:
            if not isinstance(identifier, str) or not NATIVE_ID.fullmatch(identifier):
                valid = False
            elif kind == "init":
                if session is not None:
                    valid = False
                else:
                    session = identifier
            elif session != identifier:
                valid = False
        if kind == "result":
            terminal = payload
    if expected is not None and session is not None and expected != session:
        launch = {"kind": "launcher_setup", "code": "resume_id_mismatch"}
        session = None
    return events, session, terminal, launch, valid


def _error(kind, exit_code, code=None):
    return ProviderError(
        kind=kind,
        category=("transient" if code in {
            "print_timeout", "token_headroom", "event_too_large", "output_unavailable",
        } else category_for_code(code)),
        message="AGY did not complete a valid successful turn",
        exit_code=exit_code,
        code=code,
    )


def parse_result(logs, exit_code):
    if type(exit_code) is not int:
        raise TypeError("exit_code must be an integer")
    events, session, terminal, launch, valid = _stream(logs)
    if launch:
        if launch.get("type") == "error":
            detail = launch.get("error")
            code = safe_code(detail.get("code")) if isinstance(detail, dict) else None
            return ProviderResult(session, False, None, _error("turn_failed", exit_code, code))
        return ProviderResult(
            session, False, None, _error(launch["kind"], exit_code, safe_code(launch.get("code")))
        )
    if not valid:
        return ProviderResult(session, False, None, _error("invalid_output", exit_code))
    # Native MCP ERROR cannot distinguish ordinary isError from transport failure.
    # Like Codex/Claude, individual tool failures do not fail a successful turn.
    if terminal is not None and terminal.get("finished_reason") == "print_timeout":
        return ProviderResult(
            session, False, None, _error("turn_failed", exit_code, "print_timeout"),
            interrupted=True
        )
    if terminal is not None and terminal.get("status") != "SUCCESS":
        # Even resumed expiry-poisoned turns consume the ordinary failure budget;
        # only print_timeout is a clean yield. Never infer auth from native prose.
        return ProviderResult(session, False, None, _error("result_error", exit_code))
    if exit_code != 0:
        return ProviderResult(session, False, None, _error("process_exit", exit_code))
    if session is None or terminal is None or not isinstance(terminal.get("response"), str):
        return ProviderResult(session, False, None, _error("invalid_output", exit_code))
    return ProviderResult(session, True, terminal["response"], None, parse_usage(logs))


def parse_usage(logs):
    events, _, terminal, _, valid = _stream(logs)
    if not valid:
        return None
    model = next(
        (event["init"].get("model") for event in events if event.get("event") == "init"), None
    )
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model):
        model = None
    usage = terminal.get("usage") if terminal else None
    if not isinstance(usage, dict):
        if model is None:
            return None
        usage = {}
    # Native totals include earlier activations. Thinking is a subset of output,
    # not an extra output charge. No new persisted baseline is needed.
    return ProviderUsage(
        session_input_tokens=token_count(usage, "input_tokens"),
        session_output_tokens=token_count(usage, "output_tokens"),
        session_cached_input_tokens=token_count(usage, "cache_read_tokens"),
        session_reasoning_output_tokens=token_count(usage, "thinking_tokens"),
        resolved_model=model,
        source="agy-stream",
    )


def _tool_calls(logs, *, failed=False):
    events, _, _, _, valid = _stream(logs)
    if not valid:
        return {}
    steps, failures = {}, set()
    for event in events:
        if event.get("event") != "step_update":
            continue
        payload = event["step_update"]
        index = payload.get("step_index")
        if payload.get("step_type") != "tool" or type(index) is not int or index < 0:
            continue
        name = tool_name(payload.get("tool_name"))
        if name.startswith("mcp_firm_"):
            name = "mcp__firm__" + tool_name(name.removeprefix("mcp_firm_"))
        elif name == "call_mcp_tool":
            name = "other"  # Only eager Firm tools have verified identity.
        if payload.get("state") == "ERROR":
            failures.add(index)
        steps[index] = name if index not in steps or steps[index] == name else "other"
    return dict(Counter(name for index, name in steps.items() if not failed or index in failures))


def parse_tool_calls(logs):
    return _tool_calls(logs)


def parse_failed_tool_calls(logs):
    return _tool_calls(logs, failed=True)


def parse_summary(logs, exit_code):
    result, usage = parse_result(logs, exit_code), parse_usage(logs)
    counters = dict(usage.__dict__) if usage else {}
    # Summary/chooser sessions are fresh, just like isolated task sessions.
    for name in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
        counters[name] = counters.get("session_" + name)
    return SummaryOutput(
        result.final_text,
        output_tokens=counters.get("output_tokens"),
        usage=counters or None,
        error=summary_error(result, exit_code),
    )


def parse_subscription(logs):
    return None


class AgyProvider:
    name = "agy"

    def __init__(self, *, model, effort, auth_file, executable=None):
        native_model_id(model, effort)
        self.model, self.effort = model, effort
        if not isinstance(auth_file, Path):
            raise TypeError("auth_file must be a Path")
        self.auth_file = auth_file
        if executable is not None:
            executable = text_setting(executable, "executable")
            if not executable.startswith("/"):
                raise ProviderSetupError(
                    category="config",
                    code="invalid_executable",
                    message="executable must be an absolute sandbox path",
                )
        self.executable = executable

    def validate_credentials(self):
        from .credential_store import CredentialStore

        CredentialStore(self.name, self.auth_file)._agy_snapshot(False, margin=None)

    def _spec(
        self,
        *,
        role_id,
        run_id,
        session_id,
        mode,
        layout,
        model,
        effort,
        instructions,
        prompt,
        start_url,
        token,
        mcp_url=None,
        native_id=None,
        mcp_tools=(),
        mcp_timeout=600,
    ):
        if not isinstance(token, str) or not token:
            raise ProviderSetupError(
                category="config",
                code="invalid_invocation",
                message="AGY admission token is required",
            )
        if not isinstance(start_url, str) or not start_url:
            raise ProviderSetupError(
                category="config",
                code="invalid_invocation",
                message="AGY admission URL is required",
            )
        canonical_uuid(session_id, "runtime session ID")
        if native_id is not None and (
            not isinstance(native_id, str) or not NATIVE_ID.fullmatch(native_id)
        ):
            raise ProviderSetupError(
                category="config", code="invalid_session", message="invalid AGY conversation ID"
            )
        command = [
            self.executable or f"{layout.tools}/agy",
            "--print=",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--disable-slash-commands",
            "--agent",
            f"firm-{mode}",
            "--model",
            native_model_id(model, effort),
        ]
        role = mode == "role"
        token_name = "activation-token" if role else "summary-token"
        return RunSpec(
            role_id=role_id,
            run_id=run_id,
            argv=(layout.python, layout.credential("agy-launcher")),
            env={
                "FIRM_AGY_ARGV": json.dumps(command),
                "FIRM_AGY_MODE": mode,
                "FIRM_AGY_NATIVE_ID": native_id or "",
                "FIRM_AGY_MCP_URL": mcp_url or "",
                "FIRM_AGY_MCP_TOOLS": json.dumps(list(mcp_tools)),
                "FIRM_AGY_MCP_TIMEOUT": str(mcp_timeout),
                "FIRM_RUNTIME_SESSION_ID": session_id,
                "FIRM_STATE_DIR": layout.state,
                "FIRM_PRIVATE_HOME": layout.private,
                ("FIRM_ACTIVATION_START_URL" if role else "FIRM_SUMMARY_START_URL"): start_url,
                "FIRM_CREDENTIALS_URL": start_url.replace(
                    "/activation/start" if role else "/summary/start", "/credentials"
                ),
                **({} if role else {"FIRM_SUMMARY_RUN": "1"}),
                **({"FIRM_TASK_RUN": "1"} if mode == "task" else {}),
            },
            secrets={
                token_name: token.encode(),
                "agy-auth": self._access_snapshot(),
                "agy-launcher": Path(agy_launch.__file__).read_bytes(),
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "agy-instructions": instructions.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN" if role else "FIRM_SUMMARY_TOKEN": token_name},
            stdin=(json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode(),
        )

    def _access_snapshot(self):
        from .credential_store import CredentialStore

        # Like other providers, stage locally; the first mirror fetch obtains headroom.
        snapshot = CredentialStore(self.name, self.auth_file)._agy_snapshot(
            False, margin=agy_launch.STARTUP_MARGIN + 1
        )
        return json.dumps(snapshot["data"]).encode()

    def build_spec(
        self,
        invocation,
        role_id,
        run_id,
        mcp_url,
        provider_session_id=None,
        *,
        layout,
        mcp_tools=None,
        mcp_timeout=600,
    ):
        if mcp_tools is None:
            from .mcp_server import TOOL_DESCRIPTIONS

            mcp_tools = list(TOOL_DESCRIPTIONS)
        return self._spec(
            role_id=role_id,
            run_id=run_id,
            session_id=invocation.session_id,
            mode="role",
            layout=layout,
            model=self.model,
            effort=self.effort,
            instructions=standing(invocation.context),
            prompt=render_invocation(invocation, provider_session_id),
            start_url=activation_url(mcp_url),
            token=invocation.token,
            mcp_url=mcp_url,
            native_id=provider_session_id,
            mcp_tools=mcp_tools,
            mcp_timeout=mcp_timeout,
        )

    def _one_shot(self, request, layout, mode):
        from .task_runner import WORKER_INSTRUCTIONS

        if request.provider not in (None, self.name):
            raise ProviderSetupError(
                category="config", code="provider_mismatch", message="AGY summary provider mismatch"
            )
        return self._spec(
            role_id=summary_role_id(request.run_id),
            run_id=request.run_id,
            session_id=str(uuid5(NAMESPACE_URL, request.run_id)),
            mode=mode,
            layout=layout,
            model=request.model or self.model,
            effort=request.effort or self.effort,
            instructions=request.instructions
            or (WORKER_INSTRUCTIONS if mode == "task" else summary_instructions()),
            prompt=request.prompt,
            start_url=request.start_url,
            token=request.start_token,
        )

    def build_summary_spec(self, request, layout):
        return self._one_shot(request, layout, "tool_free")

    def build_task_spec(self, request, layout):
        return self._one_shot(request, layout, "task")

    def parse_result(self, logs, exit_code):
        result = parse_result(logs, exit_code)
        return (
            replace(
                result, error=replace(result.error, model=native_model_id(self.model, self.effort))
            )
            if result.error
            else result
        )

    parse_usage = staticmethod(parse_usage)
    parse_tool_calls = staticmethod(parse_tool_calls)
    parse_failed_tool_calls = staticmethod(parse_failed_tool_calls)
    parse_summary = staticmethod(parse_summary)
    parse_subscription = staticmethod(parse_subscription)
