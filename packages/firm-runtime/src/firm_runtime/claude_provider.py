"""A concrete Claude Code adapter for one isolated sandbox invocation."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from firm_sandbox import Layout, RunSpec

from . import claude_launch, credential_mirror
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

MAX_TOKEN_BYTES = 4096


def _auth_error(code: str, message: str) -> ProviderSetupError:
    return ProviderSetupError(category="auth", code=code, message=message)


def _read_auth(path: Path) -> tuple[str, bytes]:
    raw = read_private_credential(path, "Claude")
    if b"\0" in raw:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication contains a NUL byte"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication is not valid UTF-8"
        ) from None
    if text.lstrip().startswith(("{", "[")):
        try:
            credentials = json.loads(text)
        except json.JSONDecodeError:
            raise _auth_error(
                "credential_invalid", "configured Claude authentication is not valid JSON"
            ) from None
        if not isinstance(credentials, dict) or not credentials:
            raise _auth_error(
                "credential_invalid",
                "configured Claude authentication must contain a JSON object",
            )
        return "credentials-json", raw
    if len(raw) > MAX_TOKEN_BYTES:
        raise _auth_error(
            "credential_too_large", "configured Claude OAuth token exceeds 4096 bytes"
        )
    if text.endswith("\r\n"):
        body = text[:-2]
    elif text.endswith(("\n", "\r")):
        body = text[:-1]
    else:
        body = text
    if "\n" in body or "\r" in body:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication must contain one line"
        )
    token = body.strip()
    if not token:
        raise _auth_error("credential_invalid", "configured Claude authentication is empty")
    return "oauth-token", token.encode()


class ClaudeProvider:
    """Build sandbox runs for an explicit Claude model, effort, and account token."""

    name = "claude"

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
        if effort not in SUPPORTED_EFFORTS["claude"]:
            raise ProviderSetupError(
                category="config",
                code="invalid_effort",
                message=("effort must be one of " + ", ".join(sorted(SUPPORTED_EFFORTS["claude"]))),
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
        return self.executable or f"{layout.tools}/claude"

    def validate_credentials(self):
        kind, raw = _read_auth(self.auth_file)
        if kind == "credentials-json":
            account = json.loads(raw).get("claudeAiOauth")
            if not (
                isinstance(account, dict)
                and isinstance(account.get("accessToken"), str)
                and account["accessToken"].strip()
            ):
                raise _auth_error(
                    "credential_missing_token",
                    "configured Claude authentication has no OAuth token",
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
                    provider_session_id, "Claude provider session ID"
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

        mcp_config = {
            "mcpServers": {
                "firm": {
                    "type": "http",
                    "url": mcp_url,
                    "headers": {"Authorization": "Bearer ${FIRM_ACTIVATION_TOKEN}"},
                }
            }
        }
        settings = {
            "crossSessionInbound": "refuse",
            "autoMemoryEnabled": False,
            # Roles' commits are attributed through git's identity; Claude Code's own
            # co-author trailer links an unrelated GitHub account.
            "attribution": {"commit": "", "pr": ""},
            "claudeMdExcludes": ["**"],
        }
        command = [
            self._executable(layout),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--setting-sources",
            "",
            "--settings",
            json.dumps(settings, separators=(",", ":"), sort_keys=True),
            "--permission-mode",
            "bypassPermissions",
            "--permission-prompts",
            "none",
            "--dangerously-skip-permissions",
            "--disallowedTools",
            "SendMessage",
            "ListAgents",
            "Task",
            "Agent",
            "--mcp-config",
            json.dumps(mcp_config, separators=(",", ":"), sort_keys=True),
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--system-prompt-file",
            layout.credential("role-instructions"),
            "--system-prompt-snapshot",
            "off",
        ]
        if provider_session_id is None:
            command += ["--session-id", runtime_session_id]
        else:
            command += ["--resume", provider_session_id]

        auth_kind, auth = _read_auth(self.auth_file)
        secret_env = {"FIRM_ACTIVATION_TOKEN": "activation-token"}
        if auth_kind == "oauth-token":
            secret_env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-auth"
        return RunSpec(
            role_id=role_id,
            run_id=run_id,
            argv=(layout.python, layout.credential("claude-launcher")),
            env={
                "FIRM_ACTIVATION_START_URL": start_url,
                "FIRM_CREDENTIALS_URL": start_url.replace("/activation/start", "/credentials"),
                "FIRM_CLAUDE_ARGV": json.dumps(command, ensure_ascii=True),
                "FIRM_CLAUDE_AUTH_KIND": auth_kind,
                "FIRM_RUNTIME_SESSION_ID": runtime_session_id,
                "FIRM_STATE_DIR": layout.state,
                "FIRM_PRIVATE_HOME": layout.private,
                # Telemetry and update checks stay off the firm's allowlist.
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            secrets={
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "activation-token": invocation.token.encode(),
                "role-instructions": instructions.encode(),
                "claude-auth": access_credentials("claude", auth),
                "claude-launcher": Path(claude_launch.__file__).read_bytes(),
            },
            secret_env=secret_env,
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
    def parse_tool_calls(logs: bytes | str) -> dict[str, int]:
        return parse_tool_calls(logs)

    @staticmethod
    def parse_subscription(logs: bytes | str) -> dict | None:
        return parse_subscription(logs)

    def build_summary_spec(self, request: SummaryLaunch, layout: Layout) -> RunSpec:
        """Build a foreground Claude summary with the CLI tool set empty."""
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
        if effort not in SUPPORTED_EFFORTS["claude"]:
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
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--setting-sources",
            "",
            *(
                [
                    "--permission-mode", "bypassPermissions",
                    "--permission-prompts", "none",
                    "--dangerously-skip-permissions",
                    "--tools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,NotebookEdit",
                    "--disallowedTools", "SendMessage", "ListAgents", "Task", "Agent", "Skill",
                ]
                if task else ["--tools", ""]
            ),
            "--settings",
            json.dumps({
                "autoMemoryEnabled": False,
                "claudeMdExcludes": ["**"],
                "crossSessionInbound": "refuse",
                "attribution": {"commit": "", "pr": ""},
            }),
            "--system-prompt-file",
            layout.credential("summary-instructions"),
            "--system-prompt-snapshot",
            "off",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--strict-mcp-config",
            *(["--mcp-config", '{"mcpServers":{}}'] if task else []),
        ]
        auth_kind, auth = _read_auth(self.auth_file)
        secret_env = {}
        if auth_kind == "oauth-token":
            secret_env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-auth"
        return RunSpec(
            role_id=summary_role_id(request.run_id),
            run_id=request.run_id,
            argv=(layout.python, layout.credential("claude-launcher")),
            env={
                "FIRM_CLAUDE_ARGV": json.dumps(command, ensure_ascii=True),
                "FIRM_CLAUDE_AUTH_KIND": auth_kind,
                "FIRM_RUNTIME_SESSION_ID": str(uuid5(NAMESPACE_URL, request.run_id)),
                "FIRM_STATE_DIR": layout.state,
                "FIRM_PRIVATE_HOME": layout.private,
                "FIRM_SUMMARY_RUN": "1",
                **({"FIRM_TASK_RUN": "1"} if task else {}),
                **({} if task else {
                    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(request.max_output_tokens)
                }),
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "FIRM_SUMMARY_START_URL": request.start_url,
                "FIRM_CREDENTIALS_URL": request.start_url.replace("/summary/start", "/credentials"),
            },
            secrets={
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "claude-auth": access_credentials("claude", auth),
                "claude-launcher": Path(claude_launch.__file__).read_bytes(),
                "summary-token": request.start_token.encode(),
                "summary-instructions": (request.instructions or summary_instructions()).encode(),
            },
            secret_env={**secret_env, "FIRM_SUMMARY_TOKEN": "summary-token"},
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


def _event_code(event: dict) -> str | None:
    candidates = [event.get("code"), event.get("error_code")]
    detail = event.get("error")
    if isinstance(detail, str):
        candidates.append(detail)
    if isinstance(detail, dict):
        candidates.extend((detail.get("code"), detail.get("type")))
    for value in candidates:
        if code := safe_code(value):
            return code
    return None


def _terminal_quota_code(event: dict | None) -> str | None:
    """Recognize the CLI's own terminal limit notice, never ordinary role prose."""
    if event is None or event.get("type") != "result" or event.get("is_error") is not True:
        return None
    text = event.get("result")
    if isinstance(text, str) and re.fullmatch(
        r"You've hit your session limit(?: · resets [^\r\n]{1,160})?", text
    ):
        return "usage_limit_reached"
    return None


def _terminal_reset(event: dict | None) -> float | None:
    """Accept only an explicit absolute ISO timestamp in the CLI reset text."""
    if _terminal_quota_code(event) is None:
        return None
    text = event.get("result")
    match = re.search(
        r"resets[^\r\n]{0,80}?"
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))",
        text,
        re.IGNORECASE,
    )
    return reset_at({"reset_at": match.group(1)}) if match else None


def _error(
    kind: ErrorKind,
    exit_code: int,
    code: str | None = None,
    *evidence: dict,
) -> ProviderError:
    messages = {
        "activation_gate": "The activation gate refused provider launch",
        "launcher_setup": "The sandbox provider launcher could not initialize",
        "turn_failed": "Claude reported that the turn failed",
        "result_error": "Claude reported an unsuccessful result",
        "process_exit": "Claude exited before completing the turn",
        "invalid_output": "Claude did not produce a valid completed turn",
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


def _claude_usage(mapping: object) -> tuple[int | None, int | None, int | None, int | None]:
    if not isinstance(mapping, dict):
        return None, None, None, None
    plain = token_count(mapping, "input_tokens")
    created = token_count(mapping, "cache_creation_input_tokens")
    read = token_count(mapping, "cache_read_input_tokens")
    inputs = (
        None
        if plain is None and created is None and read is None
        else sum(value or 0 for value in (plain, created, read))
    )
    reasoning = token_count(mapping, "reasoning_output_tokens")
    details = mapping.get("output_tokens_details")
    thinking = token_count(details, "thinking_tokens")
    if thinking is not None:
        reasoning = thinking
    return (
        inputs,
        read,
        token_count(mapping, "output_tokens"),
        reasoning,
    )


def _claude_context(
    usage: tuple[int | None, int | None, int | None, int | None],
    *,
    provisional: bool = False,
) -> int | None:
    """Return post-response occupancy; a provisional event gives its final input side."""
    inputs, _, output, _ = usage
    if provisional:
        return inputs
    return inputs + output if inputs is not None and output is not None else None


def parse_tool_calls(logs: bytes | str) -> dict[str, int]:
    """Count the turn's ``tool_use`` blocks by name, never their inputs or results.

    Claude streams one assistant event per content block and repeats a request's usage
    across them, so blocks are deduplicated by their own identifier where one is given.
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
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            identifier = block.get("id")
            if isinstance(identifier, str):
                if identifier in seen:
                    continue
                seen.add(identifier)
            tally[tool_name(block.get("name"))] += 1
    return dict(tally)


def parse_usage(logs: bytes | str) -> ProviderUsage | None:
    """Separate terminal accounting from per-request context occupancy."""
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    requests: dict[str, tuple[int | None, int | None, int | None, int | None]] = {}
    terminal = None
    terminal_iterations: list[tuple[int | None, int | None, int | None, int | None]] = []
    current = high_water = window = None
    compactions = 0
    current_model = None
    assistant_after_compaction = False
    for index, line in enumerate(raw.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model.strip():
                    current_model = model
                usage = _claude_usage(message.get("usage"))
                if any(value is not None for value in usage):
                    request_id = message.get("id")
                    key = request_id if isinstance(request_id, str) else f"line:{index}"
                    requests[key] = usage
                    assistant_after_compaction = True
                    # Claude streams every assistant event with a null stop_reason. Its
                    # input side is final for that request; only output is provisional,
                    # so the live value is a lower bound until terminal iterations.
                    provisional = message.get("stop_reason") is None and "stop_reason" in message
                    current = _claude_context(usage, provisional=provisional)
                    if current is not None:
                        high_water = max(high_water or 0, current)
        elif event.get("type") == "result":
            terminal = _claude_usage(event.get("usage"))
            usage = event.get("usage")
            if isinstance(usage, dict):
                iterations = usage.get("iterations")
                if isinstance(iterations, list):
                    terminal_iterations = [
                        parsed
                        for item in iterations
                        if (parsed := _claude_usage(item))
                        and any(value is not None for value in parsed)
                    ]
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                current_model = model
        if event.get("type") == "system" and event.get("subtype") == "compact_boundary":
            compactions += 1
            current = None
            assistant_after_compaction = False
            metadata = event.get("compact_metadata") or event.get("compactMetadata")
            if isinstance(metadata, dict):
                post = token_count(metadata, "post_tokens")
                if post is None:
                    post = token_count(metadata, "postTokens")
                pre = token_count(metadata, "pre_tokens")
                if pre is None:
                    pre = token_count(metadata, "preTokens")
                if pre is not None:
                    high_water = max(high_water or 0, pre)
                current = post
        native_window = token_count(event, "model_context_window")
        if native_window is None:
            native_window = token_count(event, "context_window")
        if native_window is not None and native_window > 0:
            window = native_window
        model_usage = event.get("modelUsage")
        if isinstance(model_usage, dict):
            candidates = (
                [(current_model, model_usage.get(current_model))]
                if current_model is not None
                else list(model_usage.items())
                if len(model_usage) == 1
                else []
            )
            for _, details in candidates:
                if isinstance(details, dict):
                    candidate_window = details.get("contextWindow")
                    if candidate_window is None:
                        candidate_window = details.get("context_window")
                    if type(candidate_window) is int and candidate_window > 0:
                        window = candidate_window
                        break
    values = list(requests.values())
    if terminal_iterations:
        for usage in terminal_iterations:
            context = _claude_context(usage)
            if context is not None:
                high_water = max(high_water or 0, context)
        if assistant_after_compaction:
            current = _claude_context(terminal_iterations[-1])
    terminal_has_values = terminal is not None and any(value is not None for value in terminal)
    if (
        not values and not terminal_has_values and not terminal_iterations
        and compactions == 0 and current_model is None
    ):
        return None
    request_totals = tuple(
        sum(value[position] or 0 for value in values)
        if any(value[position] is not None for value in values)
        else None
        for position in range(4)
    )
    totals_source = (
        tuple(
            terminal[position] if terminal[position] is not None else request_totals[position]
            for position in range(4)
        )
        if terminal_has_values
        else request_totals
    )
    return ProviderUsage(
        current_context_tokens=current,
        high_water_context_tokens=high_water,
        provider_context_window=window,
        input_tokens=totals_source[0],
        cached_input_tokens=totals_source[1],
        output_tokens=totals_source[2],
        reasoning_output_tokens=totals_source[3],
        native_compactions=compactions,
        source="claude_stream",
        resolved_model=current_model,
    )


def parse_subscription(logs: bytes | str) -> dict | None:
    """Return the latest structured subscription allowance event, if any."""
    raw = logs.decode(errors="replace") if isinstance(logs, bytes) else logs
    if not isinstance(raw, str):
        raise TypeError("logs must be bytes or str")
    latest = None
    for index, line in enumerate(raw.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            event_id = event.get("uuid")
            latest = {
                "event_id": event_id if isinstance(event_id, str) else f"line:{index}",
                "windows": [],
                "status": None,
                "warning": None,
            }
            continue
        unified = info.get("unifiedWindows")
        windows = []
        for key, value in unified.items() if isinstance(unified, dict) else ():
            if not re.fullmatch(r"[a-z0-9_]{1,40}", key) or not isinstance(value, dict):
                continue
            used, reset = value.get("utilization"), value.get("resetsAt")
            used, reset = _finite_number(used), _valid_reset_timestamp(reset)
            if used is None or not 0 <= used <= 1 or reset is None:
                continue
            # Preserve precision for admission; display rounding belongs in the console.
            windows.append(
                {"window": key, "used_percent": used * 100, "resets_at": reset}
            )
        warning = _warning_window(info)
        event_id = event.get("uuid")
        latest = {
            "event_id": event_id if isinstance(event_id, str) else f"line:{index}",
            "windows": windows,
            "status": info.get("status"),
            "warning": warning,
        }
    return latest


_RATE_LIMIT_TYPES = {
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_overage_included",
    "overage",
}


def _finite_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _valid_reset_timestamp(value: object) -> float | None:
    timestamp = _finite_number(value)
    return timestamp if timestamp is not None and 0 < timestamp <= 253402300799 else None


def _warning_window(info: dict) -> dict | None:
    """Identify only the bucket the provider itself marked as warning."""
    status = info.get("status")
    rate_type = info.get("rateLimitType")
    warning_status = status
    if (
        status == "allowed_warning"
        and isinstance(rate_type, str)
        and rate_type in _RATE_LIMIT_TYPES
    ):
        reset_key = "overageResetsAt" if rate_type == "overage" else "resetsAt"
        reset = info.get(reset_key)
        name = rate_type
    elif info.get("overageStatus") == "allowed_warning":
        reset = info.get("overageResetsAt")
        name = "overage"
        warning_status = "allowed_warning"
    else:
        return None
    reset = _valid_reset_timestamp(reset)
    if reset is None:
        return None
    return {"window": name, "resets_at": reset, "status": warning_status}


def _refused_reset(info: dict) -> float | None:
    """Use only the reset attached to the subscription bucket actually refused."""
    status = info.get("status")
    rate_type = info.get("rateLimitType")
    resets = []
    if status == "rejected":
        if rate_type == "overage":
            direct = info.get("overageResetsAt")
        else:
            direct = info.get("resetsAt")
            if direct is None and isinstance(rate_type, str) and rate_type in _RATE_LIMIT_TYPES:
                windows = info.get("unifiedWindows")
                bucket = windows.get(rate_type) if isinstance(windows, dict) else None
                direct = bucket.get("resetsAt") if isinstance(bucket, dict) else None
        direct = _valid_reset_timestamp(direct)
        if direct is None:
            return None
        resets.append(direct)
    if info.get("overageStatus") == "rejected":
        direct = info.get("overageResetsAt")
        direct = _valid_reset_timestamp(direct)
        if direct is None:
            return None
        resets.append(direct)
    return max(resets) if resets else None


def parse_result(logs: bytes | str, exit_code: int) -> ProviderResult:
    """Extract the exact session, terminal text, and bounded structured errors."""
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
    terminal = None
    native_failure = None
    launcher_failure = None
    refused_resets = []
    refused_reset_unknown = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "firm.launch_failed":
            kind = event.get("kind")
            if kind in {"activation_gate", "launcher_setup"}:
                launcher_failure = (kind, _event_code(event))
            continue
        candidate = event.get("session_id")
        if candidate is not None:
            try:
                candidate = canonical_uuid(candidate, "Claude provider session ID")
            except ValueError:
                invalid_session = True
            else:
                if session_id is not None and session_id != candidate:
                    invalid_session = True
                session_id = candidate
        if event.get("type") == "result":
            terminal = event
        elif event.get("type") in {"error", "turn.failed"} or (
            event.get("type") == "assistant" and _event_code(event) is not None
        ):
            native_failure = event
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and (
                info.get("status") == "rejected" or info.get("overageStatus") == "rejected"
            ):
                reset = _refused_reset(info)
                if reset is None:
                    refused_reset_unknown = True
                else:
                    refused_resets.append(reset)
            else:
                refused_resets.clear()
                refused_reset_unknown = False

    if launcher_failure is not None:
        kind, code = launcher_failure
        return ProviderResult(session_id, False, None, _error(kind, exit_code, code))
    terminal_success = terminal is not None and (
        terminal.get("subtype") == "success" and terminal.get("is_error") is False
    )
    terminal_failed = terminal is not None and not terminal_success
    if terminal_failed or (terminal is None and native_failure is not None):
        failure = (
            terminal
            if terminal is not None
            and (
                _event_code(terminal) is not None
                or (
                    _terminal_quota_code(terminal) is not None
                    and (native_failure is None or _event_code(native_failure) is None)
                )
            )
            else native_failure or terminal
        )
        assert failure is not None
        detail = failure.get("error")
        evidence = [failure]
        if isinstance(detail, dict):
            evidence.insert(0, detail)
        failure_code = _event_code(failure) or _terminal_quota_code(failure)
        terminal_session_limit = _terminal_quota_code(terminal) == "usage_limit_reached"
        native_code = _event_code(native_failure) if native_failure is not None else None
        subscription_refusal = failure_code == "usage_limit_reached" or (
            terminal_session_limit and category_for_code(native_code) == "quota"
        )
        refused_reset = (
            max(refused_resets)
            if subscription_refusal and refused_resets and not refused_reset_unknown
            else None
        )
        return ProviderResult(
            None if invalid_session else session_id,
            False,
            None,
            _error(
                "result_error" if terminal is not None else "turn_failed",
                exit_code,
                failure_code,
                *evidence,
                *(
                    [{"reset_at": refused_reset}]
                    if refused_reset is not None
                    else (
                        [{"reset_at": _terminal_reset(terminal)}]
                        if subscription_refusal and terminal
                        else []
                    )
                ),
            ),
        )
    if invalid_session or session_id is None:
        return ProviderResult(
            None, False, None, _error("invalid_output", exit_code, "invalid_session")
        )
    if terminal is None:
        kind = "process_exit" if exit_code else "invalid_output"
        return ProviderResult(session_id, False, None, _error(kind, exit_code))
    if exit_code != 0:
        return ProviderResult(session_id, False, None, _error("process_exit", exit_code))
    final_text = terminal.get("result")
    if final_text is not None and not isinstance(final_text, str):
        return ProviderResult(
            session_id, False, None, _error("invalid_output", exit_code, "invalid_result")
        )
    return ProviderResult(session_id, True, final_text, None, parse_usage(raw))
