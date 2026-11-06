"""A concrete Codex CLI adapter for one isolated sandbox invocation."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from firm_sandbox import Layout, RunSpec

from . import codex_launch, credential_mirror
from .provider_contract import (
    SUPPORTED_EFFORTS,
    ErrorKind,
    ProviderError,
    ProviderResult,
    ProviderSetupError,
    ProviderUsage,
    category_for_code,
    reset_at,
    retry_after_seconds,
    safe_code,
    summary_error,
    token_count,
    tool_name,
)
from .provider_support import (
    access_credentials,
    activation_url,
    canonical_uuid,
    read_private_credential,
    render_invocation,
    standing,
    summary_instructions,
    text_setting,
)
from .runtime import Invocation
from .summaries import SummaryLaunch, SummaryOutput, summary_role_id

# Completed items that are work the role asked for, rather than its own prose or reasoning.
TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


def _read_auth(path: Path) -> bytes:
    data = read_private_credential(path, "Codex")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderSetupError(
            category="auth",
            code="credential_invalid",
            message="configured Codex authentication is not valid JSON",
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderSetupError(
            category="auth",
            code="credential_invalid",
            message="configured Codex authentication must contain a JSON object",
        )
    return data


class CodexProvider:
    """Build sandbox runs for the configured Codex model and credential file."""

    name = "codex"

    def __init__(
        self,
        *,
        model: str,
        effort: str,
        auth_file: Path,
        executable: str | None = None,
    ) -> None:
        try:
            self.model = text_setting(model, "model")
        except ValueError as exc:
            raise ProviderSetupError(
                category="config", code="invalid_model", message=str(exc)
            ) from None
        if effort not in SUPPORTED_EFFORTS["codex"]:
            raise ProviderSetupError(
                category="config",
                code="invalid_effort",
                message=("effort must be one of " + ", ".join(sorted(SUPPORTED_EFFORTS["codex"]))),
            )
        self.effort = effort
        if not isinstance(auth_file, Path):
            raise TypeError("auth_file must be a Path")
        self.auth_file = auth_file
        if executable is None:
            self.executable = None
            return
        try:
            self.executable = text_setting(executable, "executable")
        except ValueError as exc:
            raise ProviderSetupError(
                category="config", code="invalid_executable", message=str(exc)
            ) from None
        if not self.executable.startswith("/"):
            raise ProviderSetupError(
                category="config",
                code="invalid_executable",
                message="executable must be an absolute sandbox path",
            )

    def _executable(self, layout: Layout) -> str:
        return self.executable or f"{layout.tools}/codex"

    def validate_credentials(self):
        parsed = json.loads(_read_auth(self.auth_file))
        tokens = parsed.get("tokens")
        if not (
            isinstance(parsed.get("OPENAI_API_KEY"), str) and parsed["OPENAI_API_KEY"].strip()
        ) and not (
            isinstance(tokens, dict)
            and isinstance(tokens.get("access_token"), str)
            and tokens["access_token"].strip()
        ):
            raise ProviderSetupError(
                category="auth",
                code="credential_missing_token",
                message="configured Codex authentication has no account token or API key",
            )

    def build_spec(
        self,
        invocation: Invocation,
        role_id: str,
        run_id: str,
        mcp_url: str,
        provider_session_id: str | None = None,
        *,
        layout: Layout,
    ) -> RunSpec:
        try:
            runtime_session_id = canonical_uuid(invocation.session_id, "runtime session ID")
            if provider_session_id is not None:
                provider_session_id = canonical_uuid(
                    provider_session_id, "Codex provider session ID"
                )
            if not isinstance(invocation.token, str) or not invocation.token:
                raise ValueError("invocation token must be a non-empty string")
            start_url = activation_url(mcp_url)
            instructions = standing(invocation.context)
            prompt = render_invocation(invocation, provider_session_id)
        except ValueError as exc:
            raise ProviderSetupError(
                category="config", code="invalid_invocation", message=str(exc)
            ) from None

        config = [
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="danger-full-access"',
            "-c",
            "tools.web_search=true",
            "-c",
            "include_collaboration_mode_instructions=false",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "skills.include_instructions=false",
            "-c",
            "project_root_markers=[]",
            "-c",
            "features.multi_agent=false",
            "-c",
            "agents.enabled=false",
            "-c",
            f"model_reasoning_effort={json.dumps(self.effort)}",
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            f"mcp_servers.firm.url={json.dumps(mcp_url)}",
            "-c",
            'mcp_servers.firm.bearer_token_env_var="FIRM_ACTIVATION_TOKEN"',
            "-c",
            "mcp_servers.firm.required=true",
            "-c",
            f"model_instructions_file={json.dumps(layout.credential('role-instructions'))}",
        ]
        common = [
            self._executable(layout),
            "--model",
            self.model,
            *config,
            "exec",
        ]
        options = [
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if provider_session_id is None:
            command = [*common, *options, "-"]
        else:
            command = [*common, "resume", *options, provider_session_id, "-"]

        launcher = Path(codex_launch.__file__).read_bytes()
        return RunSpec(
            role_id=role_id,
            run_id=run_id,
            argv=(layout.python, layout.credential("codex-launcher")),
            env={
                "FIRM_ACTIVATION_START_URL": start_url,
                "FIRM_CREDENTIALS_URL": start_url.replace("/activation/start", "/credentials"),
                "FIRM_CODEX_ARGV": json.dumps(command, ensure_ascii=True),
                "FIRM_RUNTIME_SESSION_ID": runtime_session_id,
                "FIRM_STATE_DIR": layout.state,
                "FIRM_PRIVATE_HOME": layout.private,
            },
            secrets={
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "activation-token": invocation.token.encode(),
                "codex-auth": access_credentials("codex", _read_auth(self.auth_file)),
                "codex-launcher": launcher,
                "role-instructions": instructions.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
            stdin=prompt.encode(),
        )

    def parse_result(self, logs: bytes | str, exit_code: int) -> ProviderResult:
        result = parse_result(logs, exit_code)
        if result.error is None:
            return result
        # Only this instance knows which configured model launched the run; a
        # rejected-model failure is otherwise indistinguishable from any other.
        return replace(result, error=replace(result.error, model=self.model))

    def parse_usage(self, logs: bytes | str) -> ProviderUsage | None:
        return parse_usage(logs)

    @staticmethod
    def parse_subscription(logs: bytes | str) -> dict | None:
        return parse_subscription(logs)

    @staticmethod
    def parse_tool_calls(logs: bytes | str) -> dict[str, int]:
        return parse_tool_calls(logs)

    def build_summary_spec(self, request: SummaryLaunch, layout: Layout) -> RunSpec:
        """Build a foreground summary run with no role authority or tools."""
        return self._build_one_shot_spec(request, layout, task=False)

    def build_task_spec(self, request: SummaryLaunch, layout: Layout) -> RunSpec:
        """Build a fresh worker with ordinary tools and no firm authority."""
        return self._build_one_shot_spec(request, layout, task=True)

    def _build_one_shot_spec(
        self, request: SummaryLaunch, layout: Layout, *, task: bool
    ) -> RunSpec:
        if request.provider not in (None, self.name):
            raise ProviderSetupError(
                category="config", code="provider_mismatch", message="summary provider mismatch"
            )
        model = request.model or self.model
        effort = request.effort or self.effort
        if effort not in SUPPORTED_EFFORTS["codex"]:
            raise ProviderSetupError(
                category="config", code="invalid_effort", message="invalid summary effort"
            )
        if not isinstance(request.start_url, str) or not request.start_url:
            raise ProviderSetupError(
                category="config",
                code="summary_gate_missing",
                message="summary gate URL is required",
            )
        if not isinstance(request.start_token, str) or not request.start_token:
            raise ProviderSetupError(
                category="config",
                code="summary_gate_missing",
                message="summary gate token is required",
            )
        command = [
            self._executable(layout),
            "--model",
            model,
            "-c",
            'approval_policy="never"',
            "-c",
            f"model_instructions_file={json.dumps(layout.credential('summary-instructions'))}",
            "-c",
            "include_collaboration_mode_instructions=false",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "skills.include_instructions=false",
            "-c",
            f"features.shell_tool={str(task).lower()}",
            "-c",
            f"features.view_image={str(task).lower()}",
            "-c",
            'web_search="live"' if task else 'web_search="disabled"',
            "-c",
            "project_root_markers=[]",
            "-c",
            "features.multi_agent=false",
            "-c",
            "agents.enabled=false",
            "-c",
            "mcp_servers={}",
            "-c",
            "features.memories=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.skill_search=false",
            "-c",
            "features.skip_host_skill_discovery=true",
            "-c",
            "features.hooks=false",
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            f"model_reasoning_effort={json.dumps(effort)}",
            "exec",
            *([] if task else ["--ephemeral"]),
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--skip-git-repo-check",
            "--json",
            *(["--dangerously-bypass-approvals-and-sandbox"] if task else []),
            "-",
        ]
        return RunSpec(
            role_id=summary_role_id(request.run_id),
            run_id=request.run_id,
            argv=(layout.python, layout.credential("codex-launcher")),
            env={
                "FIRM_CODEX_ARGV": json.dumps(command, ensure_ascii=True),
                "FIRM_RUNTIME_SESSION_ID": str(uuid5(NAMESPACE_URL, request.run_id)),
                "FIRM_STATE_DIR": layout.state,
                "FIRM_PRIVATE_HOME": layout.private,
                "FIRM_SUMMARY_RUN": "1",
                **({"FIRM_TASK_RUN": "1"} if task else {}),
                "FIRM_SUMMARY_START_URL": request.start_url,
                "FIRM_CREDENTIALS_URL": request.start_url.replace("/summary/start", "/credentials"),
            },
            secrets={
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "codex-auth": access_credentials("codex", _read_auth(self.auth_file)),
                "codex-launcher": Path(codex_launch.__file__).read_bytes(),
                "summary-token": request.start_token.encode(),
                "summary-instructions": (request.instructions or summary_instructions()).encode(),
            },
            secret_env={"FIRM_SUMMARY_TOKEN": "summary-token"},
            stdin=request.prompt.encode(),
        )

    @staticmethod
    def parse_summary(logs: bytes | str, exit_code: int) -> SummaryOutput:
        parsed = parse_result(logs, exit_code)
        usage = parse_usage(logs)
        return SummaryOutput(
            parsed.final_text,
            output_tokens=usage.output_tokens if usage else None,
            usage=(usage.__dict__ if usage else None),
            error=summary_error(parsed, exit_code),
        )


def _error(
    kind: ErrorKind,
    exit_code: int,
    code: str | None = None,
    *evidence: dict,
) -> ProviderError:
    messages = {
        "activation_gate": "The activation gate refused provider launch",
        "launcher_setup": "The sandbox provider launcher could not initialize",
        "turn_failed": "Codex reported that the turn failed",
        "process_exit": "Codex exited before completing the turn",
        "invalid_output": "Codex did not produce a valid completed turn",
    }
    category = "config" if kind == "launcher_setup" else category_for_code(code)
    return ProviderError(
        kind=kind,
        category=category,
        message=messages[kind],
        exit_code=exit_code,
        code=code,
        retry_after_seconds=retry_after_seconds(*evidence),
        reset_at=reset_at(*evidence),
    )


def _event_code(event: dict) -> str | None:
    candidates = [event.get("code"), event.get("error_code")]
    detail = event.get("error")
    if isinstance(detail, dict):
        candidates.extend((detail.get("code"), detail.get("type")))
    elif isinstance(detail, str):
        candidates.append(detail)
    for value in candidates:
        if code := safe_code(value):
            return code
    return None


def _usage_payload(event: dict) -> dict | None:
    if event.get("type") == "firm.telemetry" and event.get("provider") == "codex":
        value = event.get("usage")
        return value if isinstance(value, dict) else None
    if event.get("type") == "event_msg":
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            value = payload.get("info")
            return value if isinstance(value, dict) else None
    return None


def parse_tool_calls(logs: bytes | str) -> dict[str, int]:
    """Count completed tool items by name, never their arguments or results.

    Codex names its work by item type rather than by tool, so a shell call counts as
    ``command_execution``; only MCP calls carry the ``mcp__server__tool`` name a Claude
    period would show. Items are deduplicated by their own identifier where one is given.
    """
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    tally: Counter[str] = Counter()
    seen: set[str] = set()
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in TOOL_ITEMS:
            continue
        identifier = item.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                continue
            seen.add(identifier)
        if item.get("type") == "mcp_tool_call":
            server, tool = tool_name(item.get("server")), tool_name(item.get("tool"))
            tally[f"mcp__{server}__{tool}"] += 1
        else:
            tally[tool_name(item.get("type"))] += 1
    return dict(tally)


def parse_usage(logs: bytes | str) -> ProviderUsage | None:
    """Separate current-context facts from activation and session cumulative usage."""
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    current = window = None
    high_water = None
    session = {
        key: None
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    }
    activation = dict(session)
    compactions = 0
    rollout_observed = False
    observed = False
    resolved_model = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "firm.telemetry" and event.get("provider") == "codex":
            rollout_observed = True
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                resolved_model = model
                observed = True
            if event.get("native_compaction") is True:
                compactions += 1
                current = None
                observed = True
        elif event.get("type") == "turn_context":
            payload = event.get("payload")
            model = payload.get("model") if isinstance(payload, dict) else None
            if isinstance(model, str) and model.strip():
                resolved_model = model
                rollout_observed = observed = True
        elif event.get("type") == "event_msg":
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") in {
                "token_count",
                "context_compacted",
            }:
                rollout_observed = True
                if payload.get("type") == "context_compacted":
                    compactions += 1
                    current = None
                    observed = True
        info = _usage_payload(event)
        if info is not None:
            last = info.get("last_token_usage")
            total = info.get("total_token_usage")
            if isinstance(last, dict):
                value = token_count(last, "total_tokens")
                if value is not None:
                    current = value
                    high_water = max(high_water or 0, value)
                    observed = True
            native_window = token_count(info, "model_context_window")
            if native_window is not None and native_window > 0:
                window = native_window
            if isinstance(total, dict):
                for key in session:
                    value = token_count(total, key)
                    if value is not None:
                        session[key] = max(session[key] or 0, value)
                        observed = True
        if event.get("type") == "turn.completed":
            turn = event.get("usage")
            if isinstance(turn, dict):
                for key in activation:
                    value = token_count(turn, key)
                    if value is not None:
                        activation[key] = max(activation[key] or 0, value)
                        observed = True
    if not observed:
        return None
    return ProviderUsage(
        current_context_tokens=current,
        high_water_context_tokens=high_water,
        provider_context_window=window,
        input_tokens=activation["input_tokens"],
        cached_input_tokens=activation["cached_input_tokens"],
        output_tokens=activation["output_tokens"],
        reasoning_output_tokens=activation["reasoning_output_tokens"],
        session_input_tokens=session["input_tokens"],
        session_cached_input_tokens=session["cached_input_tokens"],
        session_output_tokens=session["output_tokens"],
        session_reasoning_output_tokens=session["reasoning_output_tokens"],
        native_compactions=compactions if rollout_observed else None,
        source="codex_rollout" if rollout_observed else "codex_turn",
        resolved_model=resolved_model,
    )


def parse_subscription(logs: bytes | str) -> dict | None:
    """Read Codex's optional rollout rate_limits attached to token_count events."""
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    latest = None
    for index, line in enumerate(raw.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "firm.telemetry":
            continue
        if event.get("provider") != "codex":
            continue
        limits = event.get("rate_limits")
        if not isinstance(limits, dict):
            if isinstance(event.get("usage"), dict):
                latest = {"event_id": f"line:{index}", "windows": []}
            continue
        limit_id = limits.get("limit_id")
        if isinstance(limit_id, str) and limit_id:
            bucket = "".join(
                char.lower() if char.isascii() and char.isalnum() else "_"
                for char in limit_id
            ).strip("_")[:24]
            if not bucket:
                bucket = "unknown"
        else:
            bucket = "unknown"
        windows = []
        for slot in ("primary", "secondary"):
            value = limits.get(slot)
            if not isinstance(value, dict):
                continue
            used, reset = value.get("used_percent"), value.get("resets_at")
            if type(used) not in (int, float) or type(reset) not in (int, float):
                continue
            try:
                used, reset = float(used), float(reset)
            except OverflowError:
                continue
            if not math.isfinite(used) or used < 0 or not math.isfinite(reset) or reset <= 0:
                continue
            windows.append(
                {
                    "window": f"{bucket}_{slot}",
                    "limit_id": limit_id if isinstance(limit_id, str) else None,
                    "used_percent": used,
                    "resets_at": reset,
                }
            )
        latest = {"event_id": f"line:{index}", "windows": windows}
    return latest


def parse_result(logs: bytes | str, exit_code: int) -> ProviderResult:
    """Extract safe terminal evidence without copying arbitrary provider text."""
    if isinstance(logs, bytes):
        raw = logs.decode(errors="replace")
    elif isinstance(logs, str):
        raw = logs
    else:
        raise TypeError("logs must be bytes or str")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise TypeError("exit_code must be an integer")

    session_id = None
    invalid_session = False
    final_text = None
    invalid_message = False
    completed = False
    turn_failed = False
    failed_event = None
    native_failure = None
    launcher_failure = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "firm.launch_failed":
            kind = event.get("kind")
            if kind in {"activation_gate", "launcher_setup"}:
                launcher_failure = (kind, _event_code(event))
        elif event_type == "thread.started":
            candidate = event.get("thread_id") or event.get("session_id")
            try:
                candidate = canonical_uuid(candidate, "Codex provider session ID")
            except ValueError:
                invalid_session = True
            else:
                if session_id is not None and session_id != candidate:
                    invalid_session = True
                session_id = candidate
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_text = text
                else:
                    invalid_message = True
        elif event_type == "turn.completed":
            completed = True
        elif event_type == "turn.failed":
            turn_failed = True
            failed_event = event
        elif event_type == "error":
            native_failure = event

    if launcher_failure is not None:
        kind, code = launcher_failure
        return ProviderResult(session_id, False, None, _error(kind, exit_code, code))
    if turn_failed or (native_failure is not None and not completed):
        if failed_event is None or _event_code(failed_event) is None:
            failed_event = native_failure or failed_event
        assert failed_event is not None
        detail = failed_event.get("error")
        evidence = [failed_event]
        if isinstance(detail, dict):
            evidence.insert(0, detail)
        return ProviderResult(
            None if invalid_session else session_id,
            False,
            None,
            _error(
                "turn_failed",
                exit_code,
                _event_code(failed_event),
                *evidence,
            ),
        )
    if invalid_session or session_id is None:
        return ProviderResult(
            None, False, None, _error("invalid_output", exit_code, "invalid_session")
        )
    if exit_code != 0:
        return ProviderResult(session_id, False, None, _error("process_exit", exit_code))
    if not completed or invalid_message:
        return ProviderResult(session_id, False, None, _error("invalid_output", exit_code))
    return ProviderResult(session_id, True, final_text, None, parse_usage(raw))
