"""Host-brokered guest shims and runner for firm-managed `claude`, `codex`, and `agy` CLIs."""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import math
import os
import re
import runpy
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROVIDERS = ("claude", "codex", "agy")
NATIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_LOG_BYTES = 512 * 1024
HEARTBEAT_INTERVAL = 25.0
STARTUP_MARGIN = 300
DEFAULT_CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,NotebookEdit"

AGY_NATIVE_TOOLS = [
    "view_file",
    "list_dir",
    "grep_search",
    "find_by_name",
    "run_command",
    "command_status",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "search_web",
    "generate_image",
]
AGY_NATIVE_INVENTORY = [
    "ask_custom_permission",
    "ask_permission",
    "ask_question",
    "browser_click_element",
    "browser_drag_pixel_to_pixel",
    "browser_get_dom",
    "browser_get_network_request",
    "browser_input",
    "browser_list_network_requests",
    "browser_mouse_down",
    "browser_mouse_up",
    "browser_move_mouse",
    "browser_press_key",
    "browser_refresh_page",
    "browser_resize_window",
    "browser_scroll",
    "browser_scroll_dom",
    "browser_select_option",
    "browser_subagent",
    "call_mcp_tool",
    "capture_browser_console_logs",
    "capture_browser_screenshot",
    "click_browser_pixel",
    "command_status",
    "define_subagent",
    "delete_knowledge",
    "execute_browser_javascript",
    "find_by_name",
    "finish",
    "generate_image",
    "grep_search",
    "invoke_subagent",
    "list_browser_pages",
    "list_dir",
    "list_permissions",
    "list_plugin_accounts",
    "list_resources",
    "manage_inbox",
    "manage_subagents",
    "manage_task",
    "multi_replace_file_content",
    "notebook_edit",
    "notebook_execution",
    "open_browser_url",
    "read_browser_page",
    "read_resource",
    "read_url_content",
    "replace_file_content",
    "run_command",
    "run_workflow",
    "schedule",
    "search_marketplace",
    "search_web",
    "sed_file",
    "send_command_input",
    "send_message",
    "view_file",
    "wait",
    "wait_5_seconds",
    "write_to_file",
]

CODEX_VALUE_FLAGS = {
    "-c",
    "--config",
    "-C",
    "--cd",
    "-i",
    "--image",
    "-m",
    "--model",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
    "-a",
    "--ask-for-approval",
    "-o",
    "--output-last-message",
    "--output-schema",
    "--effort",
    "--add-dir",
    "--color",
}
CLAUDE_VALUE_FLAGS = {
    "-m",
    "--model",
    "--effort",
    "--tools",
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
    "--system-prompt",
    "--system-prompt-file",
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--resume",
    "-r",
    "--session-id",
    "--output-format",
    "--input-format",
    "--json-schema",
    "--max-turns",
    "--max-budget-usd",
    "--fallback-model",
    "--permission-mode",
    "--setting-sources",
    "--settings",
    "--mcp-config",
    "--add-dir",
    "--agent",
}
AGY_VALUE_FLAGS = {
    "-m",
    "--model",
    "--effort",
    "--tools",
    "--system-prompt",
    "--system-prompt-file",
    "--resume",
    "-r",
    "--conversation",
    "--agent",
    "--input-format",
    "--output-format",
    "--print-timeout",
}

_TRANSIENT_REFUSALS = {"firm_stopping", "busy", "capacity", "hold", "provider_failure"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _directory(path: Path) -> Path:
    """Ensure a real, owner-only 0700 directory without following symlinks."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        return path
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"{path} must be a real directory")
    if details.st_uid != os.getuid():
        raise ValueError(f"{path} must belong to the current user")
    path.chmod(0o700)
    return path


def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write an ordinary file without following an existing symlink."""
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError(f"{path} must be a regular file")
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".firm-llm-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def install_shims(
    state_dir: str | Path,
    credentials_dir: str | Path,
    private_home: str | Path,
    llm_url: str,
) -> Path:
    """Install guest CLI shims and config under state_dir and prepend bin to PATH."""
    state = Path(state_dir)
    creds = Path(credentials_dir)
    private = Path(private_home)
    for label, path in (
        ("state_dir", state),
        ("credentials_dir", creds),
        ("private_home", private),
    ):
        if not path.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        _directory(path)

    clean_url = llm_url.rstrip("/")
    if not clean_url.endswith("/llm"):
        raise ValueError("llm_url must end in /llm")
    credentials_url = clean_url[: -len("/llm")] + "/credentials"

    config_path = state / "llm-config.json"
    config = {
        "llm_url": clean_url,
        "credentials_url": credentials_url,
        "credentials_dir": str(creds),
        "state_dir": str(state),
        "private_home": str(private),
    }
    _write_atomic(config_path, json.dumps(config, indent=2).encode("utf-8"), 0o600)

    bin_dir = _directory(state / "bin")
    runner = creds / "llm-cli"
    for provider in PROVIDERS:
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(runner))} "
            f"--config {shlex.quote(str(config_path))} {provider} \"$@\"\n"
        ).encode()
        _write_atomic(bin_dir / provider, script, 0o700)

    current_path = os.environ.get("PATH", "")
    parts = [part for part in current_path.split(os.pathsep) if part] if current_path else []
    bin_str = str(bin_dir)
    parts = [bin_str, *[part for part in parts if part != bin_str]]
    os.environ["PATH"] = os.pathsep.join(parts)
    return bin_dir


def _load_config(path_str: str) -> dict:
    config_path = Path(path_str)
    if not config_path.is_absolute():
        raise ValueError("config path must be absolute")
    details = config_path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("llm-config.json must be a regular file")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for key in ("llm_url", "credentials_url", "credentials_dir", "state_dir", "private_home"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"missing {key} in llm-config.json")
    return data


def _find_real_binary(provider: str, bin_dir: Path) -> str:
    resolved_bin = bin_dir.resolve()
    parts = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).resolve() == resolved_bin:
                continue
        except OSError:
            pass
        parts.append(entry)
    found = shutil.which(provider, path=os.pathsep.join(parts))
    if found:
        return found
    fallback = Path("/usr/local/bin") / provider
    if fallback.exists() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise FileNotFoundError(f"real {provider} binary not found")


_PROVIDER_SCRUB_VARS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    }
)


def _scrub_base_env(child_env: dict[str, str], *, child_marker: bool = True) -> dict[str, str]:
    result = {
        k: v
        for k, v in child_env.items()
        if not k.startswith("FIRM_") and k not in _PROVIDER_SCRUB_VARS
    }
    if child_marker:
        result["FIRM_LLM_CHILD"] = "1"
    return result


def _claude_has_positional(forwarded_head: list[str], tail: list[str]) -> bool:
    if tail:
        return True
    i = 0
    while i < len(forwarded_head):
        tok = forwarded_head[i]
        if tok in CLAUDE_VALUE_FLAGS and "=" not in tok:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return True
    return False


def _stdin_fd() -> int | None:
    try:
        return sys.stdin.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return None


def _claude_use_stdin(has_positional: bool) -> bool:
    if sys.stdin.isatty():
        return False
    if not has_positional:
        return True
    fd = _stdin_fd()
    if fd is None:
        return False
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    if stat.S_ISCHR(st.st_mode):
        return False
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
        return bool(select.select([fd], [], [], 0.0)[0])
    return stat.S_ISREG(st.st_mode)


def _read_agy_stdin(has_positional: bool) -> bytes:
    if sys.stdin.isatty():
        return b""
    if not has_positional:
        return sys.stdin.buffer.read()
    fd = _stdin_fd()
    if fd is None:
        return b""
    try:
        st = os.fstat(fd)
    except OSError:
        return b""
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
        if select.select([fd], [], [], 0.0)[0]:
            return sys.stdin.buffer.read()
        return b""
    if stat.S_ISREG(st.st_mode):
        return sys.stdin.buffer.read()
    return b""


def _split_double_dash(args: list[str]) -> tuple[list[str], list[str], bool]:
    if "--" in args:
        idx = args.index("--")
        return args[:idx], args[idx + 1 :], True
    return list(args), [], False


def _is_help_or_version(provider: str, head: list[str]) -> bool:
    for arg in head:
        if arg in {"-h", "--help", "-V", "--version"}:
            return True
    if provider == "codex" and head[:1] == ["help"]:
        return True
    return False


def _validate_and_parse(provider: str, args: list[str]) -> dict | int:
    head, tail, has_dd = _split_double_dash(args)
    if _is_help_or_version(provider, head):
        return {"help_or_version": True}

    value_flags = {
        "claude": CLAUDE_VALUE_FLAGS,
        "codex": CODEX_VALUE_FLAGS,
        "agy": AGY_VALUE_FLAGS,
    }[provider]

    effort = None
    system_prompt = None
    tools_value = None
    tools_set = False
    no_tools = False
    has_print = False
    resumed = False
    resume_id = None
    forwarded_head: list[str] = []

    i = 0
    while i < len(head):
        arg = head[i]
        # Reject --model / -m
        if arg in {"-m", "--model"} or arg.startswith("--model="):
            print(
                "firm: model selection is managed by the firm; "
                "omit --model (use --effort low|medium|high if needed)",
                file=sys.stderr,
            )
            return 2

        # Reject MCP override flags
        if arg in {"--mcp-config", "--strict-mcp-config"} or arg.startswith("--mcp-config="):
            print(
                "firm: MCP configuration is managed by the firm "
                "and cannot be overridden in CLI calls",
                file=sys.stderr,
            )
            return 2
        if provider == "codex":
            cfg_val = None
            if arg in {"-c", "--config"} and i + 1 < len(head):
                cfg_val = head[i + 1]
            elif arg.startswith(("-c=", "--config=")):
                cfg_val = arg.split("=", 1)[1]
            if cfg_val is not None:
                key_part = cfg_val.split("=", 1)[0].strip()
                if key_part == "mcp_servers" or key_part.startswith("mcp_servers."):
                    print(
                        "firm: MCP configuration is managed by the firm "
                        "and cannot be overridden in CLI calls",
                        file=sys.stderr,
                    )
                    return 2
                if key_part in {"model", "model_instructions_file", "model_reasoning_effort"}:
                    if key_part == "model":
                        print(
                            "firm: model selection is managed by the firm; "
                            "omit --model (use --effort low|medium|high if needed)",
                            file=sys.stderr,
                        )
                        return 2

        # Extract --effort
        if arg == "--effort":
            if i + 1 >= len(head):
                print("firm: missing value for --effort", file=sys.stderr)
                return 2
            effort = head[i + 1]
            i += 2
            continue
        if arg.startswith("--effort="):
            effort = arg.split("=", 1)[1]
            i += 1
            continue

        # Handle --no-tools for codex
        if provider == "codex" and arg == "--no-tools":
            no_tools = True
            i += 1
            continue

        # Extract --system-prompt / --system-prompt-file for claude / agy
        if provider in {"claude", "agy"}:
            if arg == "--system-prompt":
                if i + 1 >= len(head):
                    print("firm: missing value for --system-prompt", file=sys.stderr)
                    return 2
                system_prompt = head[i + 1]
                i += 2
                continue
            if arg.startswith("--system-prompt="):
                system_prompt = arg.split("=", 1)[1]
                i += 1
                continue
            if arg == "--system-prompt-file":
                if i + 1 >= len(head):
                    print("firm: missing value for --system-prompt-file", file=sys.stderr)
                    return 2
                system_prompt = Path(head[i + 1]).read_text(encoding="utf-8")
                i += 2
                continue
            if arg.startswith("--system-prompt-file="):
                system_prompt = Path(arg.split("=", 1)[1]).read_text(encoding="utf-8")
                i += 1
                continue

        # Print/non-interactive detection & stripping conflicting stream options
        if provider in {"claude", "agy"}:
            if arg in {"-p", "--print"}:
                has_print = True
                if provider == "claude":
                    i += 1
                    continue
                i += 1
                continue
            if provider == "agy" and arg.startswith("--print="):
                has_print = True
                val = arg.split("=", 1)[1]
                if val:
                    forwarded_head.append(val)
                i += 1
                continue
            if arg in {"--tools"} and i + 1 < len(head):
                tools_value = head[i + 1]
                tools_set = True
                if provider == "claude":
                    forwarded_head.extend([arg, head[i + 1]])
                i += 2
                continue
            if arg.startswith("--tools="):
                tools_value = arg.split("=", 1)[1]
                tools_set = True
                if provider == "claude":
                    forwarded_head.append(arg)
                i += 1
                continue
            if arg in {"--resume", "-r", "--continue", "-c"}:
                resumed = True
                if arg in {"--resume", "-r"}:
                    if i + 1 >= len(head):
                        print(f"firm: missing value for {arg}", file=sys.stderr)
                        return 2
                    resume_id = head[i + 1]
                    if provider == "claude":
                        forwarded_head.extend([arg, head[i + 1]])
                    i += 2
                    continue
                if provider == "claude":
                    forwarded_head.append(arg)
                i += 1
                continue
            if arg.startswith(("--resume=", "-r=")):
                resumed = True
                resume_id = arg.split("=", 1)[1]
                if provider == "claude":
                    forwarded_head.append(arg)
                i += 1
                continue
            if provider == "agy" and arg in {"--conversation"}:
                if i + 1 >= len(head):
                    print("firm: missing value for --conversation", file=sys.stderr)
                    return 2
                resumed = True
                resume_id = head[i + 1]
                i += 2
                continue
            if provider == "agy" and arg.startswith("--conversation="):
                resumed = True
                resume_id = arg.split("=", 1)[1]
                i += 1
                continue
            if provider == "claude" and arg == "--output-format":
                i += 2 if i + 1 < len(head) else 1
                continue
            if provider == "claude" and arg.startswith("--output-format="):
                i += 1
                continue
            if provider == "claude" and arg == "--verbose":
                i += 1
                continue

        # Copy normal flag + value or positional
        forwarded_head.append(arg)
        if arg in value_flags and i + 1 < len(head):
            forwarded_head.append(head[i + 1])
            i += 2
            continue
        i += 1

    if provider in {"claude", "agy"} and not has_print:
        print(
            f"firm: interactive mode is unavailable; pass -p (for example: {provider} -p "
            f'"instruction", or {provider} -p --resume SESSION_ID "follow-up")',
            file=sys.stderr,
        )
        return 2

    if provider == "codex":
        pre_exec: list[str] = []
        post_exec: list[str] = []
        codex_positionals: list[str] = []
        exec_found = False
        seen_positional = False
        j = 0
        while j < len(forwarded_head):
            tok = forwarded_head[j]
            if not exec_found:
                if tok in CODEX_VALUE_FLAGS and j + 1 < len(forwarded_head):
                    pre_exec.extend([tok, forwarded_head[j + 1]])
                    j += 2
                    continue
                if tok.startswith("-"):
                    pre_exec.append(tok)
                    j += 1
                    continue
                if tok != "exec":
                    print(
                        'firm: interactive mode is unavailable; use codex exec "instruction" '
                        '(or codex exec resume SESSION_ID "follow-up")',
                        file=sys.stderr,
                    )
                    return 2
                exec_found = True
                j += 1
                continue
            if tok in {"-C", "--cd"} and j + 1 < len(forwarded_head):
                pre_exec.extend([tok, forwarded_head[j + 1]])
                j += 2
                continue
            if tok.startswith(("-C=", "--cd=")):
                pre_exec.append(tok)
                j += 1
                continue
            if tok == "resume" and not seen_positional:
                resumed = True
                seen_positional = True
                j += 1
                continue
            if tok in CODEX_VALUE_FLAGS and j + 1 < len(forwarded_head):
                post_exec.extend([tok, forwarded_head[j + 1]])
                j += 2
                continue
            if tok in {"--json", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox"}:
                j += 1
                continue
            if not tok.startswith("-") or tok == "-":
                seen_positional = True
                codex_positionals.append(tok)
            post_exec.append(tok)
            j += 1
        if not exec_found:
            print(
                'firm: interactive mode is unavailable; use codex exec "instruction" '
                '(or codex exec resume SESSION_ID "follow-up")',
                file=sys.stderr,
            )
            return 2
        if has_dd:
            post_exec.append("--")
            post_exec.extend(tail)
            codex_positionals.extend(tail)
        wants_stdin = ("-" in codex_positionals) or (
            len(codex_positionals) <= (1 if resumed else 0)
        )
        return {
            "help_or_version": False,
            "effort": effort,
            "resumed": resumed,
            "wants_stdin": wants_stdin,
            "tools_enabled": not no_tools,
            "pre_exec": pre_exec,
            "post_exec": post_exec,
        }

    if provider == "agy":
        positionals = list(forwarded_head)
        if tail:
            positionals.extend(tail)
        if resume_id is not None and not NATIVE_ID.fullmatch(resume_id):
            print(f"firm: unknown agy session {resume_id}", file=sys.stderr)
            return 2
        return {
            "help_or_version": False,
            "effort": effort,
            "resumed": resumed,
            "resume_id": resume_id,
            "tools_enabled": not (tools_set and tools_value == ""),
            "tools_value": tools_value if tools_set else None,
            "system_prompt": system_prompt,
            "positionals": positionals,
        }

    # claude
    has_positional = _claude_has_positional(forwarded_head, tail)
    forwarded = list(forwarded_head)
    if has_dd:
        forwarded.append("--")
        forwarded.extend(tail)
    return {
        "help_or_version": False,
        "effort": effort,
        "resumed": resumed,
        "has_positional": has_positional,
        "tools_set": tools_set,
        "system_prompt": system_prompt,
        "forwarded": forwarded,
    }


def _http_json(
    url: str, token: str, payload: dict | None = None, *, timeout: float = 30.0
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            body = json.loads(response.read(1024 * 1024 + 1))
            return response.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(65536))
        except Exception:
            body = {}
        return exc.code, body if isinstance(body, dict) else {}
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return 503, {}


def _format_refusal(provider: str, response: dict, requested_effort: str | None) -> str:
    reason = response.get("reason")
    if reason == "llm_disabled":
        return "firm: LLM CLI tools are disabled in this firm's configuration"
    if reason == "provider_not_configured":
        msg = f"firm: {provider} is not configured for this firm"
        configured = response.get("configured") or []
        if configured:
            msg += f"; configured: {', '.join(configured)}"
        return msg
    if reason == "firm_stopping":
        return (
            "firm: the firm is stopping; "
            "save handoff notes if requested and finish this work period"
        )
    if reason == "busy":
        max_concurrent = response.get("max_concurrent", 2)
        return (
            f"firm: at most {max_concurrent} LLM CLI calls can run concurrently "
            "in your role; wait for one to finish"
        )
    if reason == "capacity":
        return (
            f"firm: no free account capacity is currently available for {provider}; "
            "wait or continue directly"
        )
    if reason == "hold":
        return (
            f"firm: the {provider} account is temporarily paused by Operations; "
            "continue directly or try another configured CLI"
        )
    if reason == "configuration":
        return f"firm: {provider} CLI settings failed setup; Operations has notified the owner"
    if reason == "invalid_effort":
        supported = response.get("supported") or []
        return (
            f"firm: unsupported effort {requested_effort} for {provider}; "
            f"supported: {', '.join(supported)}"
        )
    if reason == "provider_failure":
        return f"firm: {provider} credentials are currently unavailable"
    return f"firm: {provider} CLI call is currently unavailable ({reason or 'unknown'})"


def _rate_limits_event(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    limit_id = value.get("limit_id")
    if (
        isinstance(limit_id, str)
        and 0 < len(limit_id) <= 64
        and all((c.isascii() and c.isalnum()) or c in "._-" for c in limit_id)
    ):
        result["limit_id"] = limit_id
    for name in ("primary", "secondary"):
        source = value.get(name)
        if not isinstance(source, dict):
            continue
        used, reset = source.get("used_percent"), source.get("resets_at")
        if type(used) not in (int, float) or type(reset) not in (int, float):
            continue
        try:
            used_f, reset_f = float(used), float(reset)
        except OverflowError:
            continue
        if (
            not math.isfinite(used_f)
            or used_f < 0
            or not math.isfinite(reset_f)
            or not 0 < reset_f <= 253402300799
        ):
            continue
        result[name] = {"used_percent": used_f, "resets_at": reset_f}
    return result if "primary" in result or "secondary" in result else None


def _codex_usage_event(record: object) -> dict | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "turn_context":
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return {"type": "firm.telemetry", "provider": "codex", "model": model}
        return None
    if record.get("type") != "event_msg":
        return None
    if payload.get("type") == "context_compacted":
        return {"type": "firm.telemetry", "provider": "codex", "native_compaction": True}
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        info = {}

    def counters(value: object) -> dict:
        if not isinstance(value, dict):
            return {}
        return {
            key: value[key]
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
            if type(value.get(key)) is int and value[key] >= 0
        }

    usage = {
        "last_token_usage": counters(info.get("last_token_usage")),
        "total_token_usage": counters(info.get("total_token_usage")),
    }
    rate_limits = _rate_limits_event(payload.get("rate_limits"))
    window = info.get("model_context_window")
    if type(window) is int and window > 0:
        usage["model_context_window"] = window
    if not usage["last_token_usage"] and not usage["total_token_usage"] and not rate_limits:
        return None
    event: dict[str, object] = {"type": "firm.telemetry", "provider": "codex", "usage": usage}
    if rate_limits:
        event["rate_limits"] = rate_limits
    return event


def _rollouts(root: Path) -> list[Path]:
    return [p for p in root.glob("**/*.jsonl") if p.is_file() and not p.is_symlink()]


def _scan_rollouts(root: Path, offsets: dict[Path, int], emit) -> None:
    for path in _rollouts(root):
        offset = offsets.get(path, 0)
        try:
            size = path.stat().st_size
            if size < offset:
                offset = 0
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
                end = handle.tell()
        except OSError:
            continue
        complete, separator, remainder = data.rpartition(b"\n")
        if not separator:
            offsets[path] = offset
            continue
        offsets[path] = end - len(remainder)
        for line in complete.splitlines():
            try:
                event = _codex_usage_event(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event is not None:
                emit(json.dumps(event, separators=(",", ":")))


class _TelemetryBuffer:
    """Keep compact telemetry lines within MAX_LOG_BYTES, preserving session/terminal events."""

    def __init__(self, limit: int = MAX_LOG_BYTES):
        self.limit = limit
        self._lock = threading.Lock()
        self._lines: list[bytes] = []
        self._bytes = 0

    def add(self, line: str | bytes) -> None:
        raw = line.encode("utf-8", errors="replace") if isinstance(line, str) else line
        if not raw.endswith(b"\n"):
            raw = raw + b"\n"
        with self._lock:
            if len(raw) > self.limit:
                return
            self._lines.append(raw)
            self._bytes += len(raw)
            # Evict oldest middle lines once past limit, keeping line 0 (session init) intact.
            while self._bytes > self.limit and len(self._lines) > 2:
                removed = self._lines.pop(1)
                self._bytes -= len(removed)

    def render(self) -> str:
        with self._lock:
            return b"".join(self._lines).decode("utf-8", errors="replace")


def _project_event(
    provider: str, event: dict, telemetry: _TelemetryBuffer, state: dict
) -> None:
    if provider == "claude":
        etype = event.get("type")
        if etype == "firm.launch_failed":
            telemetry.add(json.dumps(event, separators=(",", ":")))
            return
        projected: dict[str, object] = {}
        for key in ("type", "subtype", "is_error", "session_id", "uuid", "model_context_window",
                    "context_window"):
            if key in event:
                projected[key] = event[key]
        if etype == "assistant":
            msg = event.get("message")
            if isinstance(msg, dict):
                projected["message"] = {
                    k: msg[k]
                    for k in ("id", "model", "usage", "stop_reason")
                    if k in msg
                }
            for key in ("error", "code", "error_code"):
                if key in event:
                    projected[key] = event[key]
            telemetry.add(json.dumps(projected, separators=(",", ":")))
        elif etype == "result":
            res_text = event.get("result")
            if event.get("is_error") is False and event.get("subtype") == "success":
                if isinstance(res_text, str):
                    state["answer"] = res_text
                elif "structured_output" in event:
                    state["answer"] = json.dumps(event["structured_output"])
                projected["result"] = ""
            else:
                if isinstance(res_text, str):
                    projected["result"] = res_text[:512]
                elif res_text is not None:
                    projected["result"] = res_text
            for key in ("usage", "model", "modelUsage", "error", "code", "error_code"):
                if key in event:
                    projected[key] = event[key]
            telemetry.add(json.dumps(projected, separators=(",", ":")))
        elif etype in {"rate_limit_event", "error", "turn.failed", "system"}:
            telemetry.add(json.dumps(event, separators=(",", ":")))
        elif "session_id" in event:
            telemetry.add(json.dumps(projected, separators=(",", ":")))
        return

    if provider == "codex":
        etype = event.get("type")
        if etype in {"firm.launch_failed", "firm.telemetry", "thread.started", "turn.completed",
                     "turn.failed", "error"}:
            telemetry.add(json.dumps(event, separators=(",", ":")))
        elif etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    state["answer"] = text
                    telemetry.add(
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": ""},
                            },
                            separators=(",", ":"),
                        )
                    )
                else:
                    telemetry.add(json.dumps(event, separators=(",", ":")))
        return

    # agy
    if event.get("type") in {"firm.launch_failed", "error"}:
        telemetry.add(json.dumps(event, separators=(",", ":")))
        return
    kind = event.get("event")
    payload = event.get(kind) if isinstance(kind, str) else None
    if not isinstance(payload, dict):
        return
    if kind == "init":
        telemetry.add(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": event.get("conversation_id"),
                    "init": {"model": payload.get("model")},
                },
                separators=(",", ":"),
            )
        )
    elif kind == "step_update" and payload.get("step_type") == "agent_response":
        telemetry.add(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        k: payload[k]
                        for k in ("conversation_id", "step_index", "step_type", "usage")
                        if k in payload
                    },
                },
                separators=(",", ":"),
            )
        )
    elif kind == "result":
        if payload.get("status") == "SUCCESS" and isinstance(payload.get("response"), str):
            state["answer"] = payload["response"]
        projected_payload = {
            k: payload[k]
            for k in ("conversation_id", "status", "finished_reason", "usage")
            if k in payload
        }
        if "response" in payload:
            projected_payload["response"] = (
                "" if isinstance(payload["response"], str) else payload["response"]
            )
        if "error" in payload and isinstance(payload["error"], str):
            projected_payload["error"] = payload["error"][:512]
        elif "error" in payload:
            projected_payload["error"] = payload["error"]
        telemetry.add(
            json.dumps({"event": "result", "result": projected_payload}, separators=(",", ":"))
        )


def _stage_credentials(provider: str, creds: dict, run_dir: Path) -> Path | None:
    kind = creds.get("kind")
    data = creds.get("data")
    if provider == "claude":
        if kind == "oauth-token":
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = str(data)
            return run_dir / ".credentials.json"
        if kind == "credentials-json":
            auth = run_dir / ".credentials.json"
            _write_atomic(auth, json.dumps(data).encode("utf-8"), 0o600)
            return auth
        raise ValueError("unsupported claude credential kind")
    if provider == "codex":
        if kind != "codex-json":
            raise ValueError("unsupported codex credential kind")
        auth = run_dir / "auth.json"
        _write_atomic(auth, json.dumps(data).encode("utf-8"), 0o600)
        return auth
    # agy
    if kind != "agy-access":
        raise ValueError("unsupported agy credential kind")
    auth = run_dir / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    _write_atomic(auth, json.dumps(data).encode("utf-8"), 0o600)
    return auth


def _agy_settings(tool_free: bool) -> dict:
    denied = ["read_url(*)", "execute_url(*)", "mcp(*)"]
    if tool_free:
        denied += [
            f"{name}(*)"
            for name in sorted(set(AGY_NATIVE_INVENTORY) | {"command", "read_file", "write_file"})
        ]
    return {
        "useG1Credits": False,
        "toolPermission": "strict" if tool_free else "always-proceed",
        "agentMode": "accept-edits",
        "permissions": {"deny": denied},
    }


_AGY_ALIASES = {
    "hydrogen": "gemini-4-hydrogen",
    "neon": "gemini-4-neon",
    "carbon": "gemini-4-carbon",
    "argon": "gemini-4-argon",
    "low": "gemini-4-hydrogen",
    "medium": "gemini-4-neon",
    "high": "gemini-4-neon",
}


def _agy_native_model(model: str, effort: str) -> str:
    base = _AGY_ALIASES.get(model, model)
    return "gemini-4-carbon-high" if effort == "xhigh" else f"{base}-{effort}"


def _agy_print_timeout(auth_path: Path, max_timeout: int) -> int:
    value = json.loads(auth_path.read_bytes())
    expiry = datetime.fromisoformat(value["token"]["expiry"].replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        raise ValueError("AGY token expiry requires timezone")
    headroom = math.floor(expiry.timestamp() - time.time() - STARTUP_MARGIN - 1)
    if headroom < 1:
        raise ValueError("AGY token has insufficient startup headroom")
    return min(max_timeout, headroom)


def _agy_agent_tools(tools_enabled: bool, tools_value: str | None) -> list[str]:
    if not tools_enabled:
        return []
    if tools_value is not None:
        requested = [item.strip() for item in tools_value.split(",") if item.strip()]
        valid = [name for name in requested if name in AGY_NATIVE_TOOLS]
        if valid:
            return valid
    return list(AGY_NATIVE_TOOLS)


def _load_pdeathsig():
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL("libc.so.6")
        prctl = libc.prctl
    except (OSError, AttributeError):
        return None

    def apply():
        prctl(1, signal.SIGKILL, 0, 0, 0)

    return apply


def _run_managed(
    provider: str,
    parsed: dict,
    grant: dict,
    config: dict,
    activation_token: str,
    real_exe: str,
    stdin_bytes: bytes,
) -> int:
    run_id = grant["run_id"]
    run_token = grant["run_token"]
    model = grant["model"]
    effort = grant["effort"]
    timeout_seconds = grant["timeout_seconds"]
    creds = grant["credentials"]

    state_dir = Path(config["state_dir"])
    creds_dir = Path(config["credentials_dir"])
    private_home = Path(config["private_home"])
    llm_url = config["llm_url"]
    credentials_url = config["credentials_url"]

    llm_root = _directory(state_dir / "llm")
    run_root = _directory(llm_root / run_id)
    run_dir = _directory(run_root / provider)

    cli_root = _directory(private_home / ".firm")
    cli_base = _directory(cli_root / "cli")
    provider_persist = _directory(cli_base / provider)
    persist_sub = {"claude": "projects", "codex": "sessions", "agy": "conversations"}[provider]
    persist_dir = _directory(provider_persist / persist_sub)

    worker_instructions = (creds_dir / "llm-instructions").read_text(encoding="utf-8")
    caller_prompt = parsed.get("system_prompt")
    full_instructions = (
        f"{worker_instructions.rstrip()}\n\n{caller_prompt}"
        if caller_prompt
        else worker_instructions
    )

    telemetry = _TelemetryBuffer()
    state: dict[str, object] = {"answer": None}
    reason: str | None = None
    exit_code = 1
    resumed = bool(parsed["resumed"])
    prior_claude_oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")

    try:
        if provider == "agy":
            _directory(run_dir / ".gemini")
            _directory(run_dir / ".gemini" / "antigravity-cli")
            _directory(run_dir / ".gemini" / "config")
            _directory(run_dir / ".gemini" / "config" / "agents")

        auth_path = _stage_credentials(provider, creds, run_dir)
        if provider == "claude":
            (run_dir / "projects").symlink_to(persist_dir, target_is_directory=True)
        elif provider == "codex":
            images_dir = _directory(provider_persist / "generated_images")
            (run_dir / "sessions").symlink_to(persist_dir, target_is_directory=True)
            (run_dir / "generated_images").symlink_to(images_dir, target_is_directory=True)
        else:
            brain_dir = _directory(provider_persist / "brain")
            (run_dir / ".gemini" / "antigravity-cli" / "conversations").symlink_to(
                persist_dir, target_is_directory=True
            )
            (run_dir / ".gemini" / "antigravity-cli" / "brain").symlink_to(
                brain_dir, target_is_directory=True
            )

        mirror_mod = runpy.run_path(str(creds_dir / "credential-mirror"))
        try:
            mirror = mirror_mod["CredentialMirror"](
                auth_path, url=credentials_url, token=run_token
            )
            with contextlib.redirect_stdout(io.StringIO()):
                mirror.__enter__()
        except mirror_mod["MirrorUnavailable"]:
            reason = "provider_failure"
            print(
                _format_refusal(provider, {"reason": "provider_failure"}, effort),
                file=sys.stderr,
            )
            return 75

        try:
            child_env = _scrub_base_env(dict(os.environ))
            child_stdin: bytes | None = None
            popen_stdin: int | object = subprocess.DEVNULL

            if provider == "claude":
                if _claude_use_stdin(bool(parsed["has_positional"])):
                    popen_stdin = sys.stdin
                else:
                    popen_stdin = subprocess.DEVNULL
                if creds.get("kind") != "oauth-token":
                    child_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                elif "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:
                    child_env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
                child_env["CLAUDE_CONFIG_DIR"] = str(run_dir)
                child_env["CLAUDE_CODE_MAX_RETRIES"] = "0"
                child_env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
                child_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
                child_env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "0"
                instructions_file = run_dir / "instructions.md"
                _write_atomic(instructions_file, full_instructions.encode("utf-8"), 0o600)
                settings_json = json.dumps(
                    {
                        "autoMemoryEnabled": False,
                        "claudeMdExcludes": ["**"],
                        "crossSessionInbound": "refuse",
                        "attribution": {"commit": "", "pr": ""},
                    }
                )
                command = [
                    real_exe,
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
                    "--permission-mode",
                    "bypassPermissions",
                    "--permission-prompts",
                    "none",
                    "--dangerously-skip-permissions",
                    *(
                        []
                        if parsed["tools_set"]
                        else ["--tools", DEFAULT_CLAUDE_TOOLS]
                    ),
                    "--disallowedTools",
                    "SendMessage",
                    "ListAgents",
                    "Task",
                    "Agent",
                    "Skill",
                    "--settings",
                    settings_json,
                    "--system-prompt-file",
                    str(instructions_file),
                    "--system-prompt-snapshot",
                    "off",
                    "--disable-slash-commands",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    *parsed["forwarded"],
                ]
            elif provider == "codex":
                if parsed["wants_stdin"] and not sys.stdin.isatty():
                    popen_stdin = sys.stdin
                else:
                    popen_stdin = subprocess.DEVNULL
                child_env["CODEX_HOME"] = str(run_dir)
                instructions_file = run_dir / "instructions.md"
                _write_atomic(instructions_file, full_instructions.encode("utf-8"), 0o600)
                tools_enabled = bool(parsed["tools_enabled"])
                firm_cfg = [
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    'sandbox_mode="danger-full-access"',
                    "-c",
                    f"model_instructions_file={json.dumps(str(instructions_file))}",
                    "-c",
                    "include_collaboration_mode_instructions=false",
                    "-c",
                    "project_doc_max_bytes=0",
                    "-c",
                    "skills.include_instructions=false",
                    "-c",
                    f"features.shell_tool={str(tools_enabled).lower()}",
                    "-c",
                    f"features.view_image={str(tools_enabled).lower()}",
                    "-c",
                    f"features.image_generation={str(tools_enabled).lower()}",
                    "-c",
                    'web_search="live"' if tools_enabled else 'web_search="disabled"',
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
                ]
                exec_opts = [
                    "--json",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    *(
                        ["--dangerously-bypass-approvals-and-sandbox"]
                        if tools_enabled
                        else []
                    ),
                ]
                command = [
                    real_exe,
                    "--model",
                    model,
                    *parsed["pre_exec"],
                    *firm_cfg,
                    "exec",
                    *(["resume"] if resumed else []),
                    *exec_opts,
                    *parsed["post_exec"],
                ]
            else:
                # agy
                try:
                    print_timeout = _agy_print_timeout(auth_path, timeout_seconds)
                except (KeyError, ValueError, OSError):
                    reason = "provider_failure"
                    print(
                        _format_refusal(provider, {"reason": "provider_failure"}, effort),
                        file=sys.stderr,
                    )
                    return 75
                tool_free = not parsed["tools_enabled"]
                agent_name = "firm-tool_free" if tool_free else "firm-llm"
                agent_tools = _agy_agent_tools(not tool_free, parsed["tools_value"])
                _write_atomic(
                    run_dir / ".gemini" / "antigravity-cli" / "settings.json",
                    json.dumps(_agy_settings(tool_free)).encode("utf-8"),
                    0o600,
                )
                _write_atomic(
                    run_dir / ".gemini" / "config" / "config.json",
                    json.dumps({"userSettings": {"useAiCredits": False}}).encode("utf-8"),
                    0o600,
                )
                _write_atomic(
                    run_dir / ".gemini" / "config" / "agents" / f"{agent_name}.md",
                    (
                        f"---\nname: {agent_name}\n"
                        "description: Firm-owned LLM CLI agent.\n"
                        f"tools: {json.dumps(agent_tools)}\ninheritCustomizations: false\n"
                        "mainAgent: true\nsubagent: false\n---\n" + full_instructions
                    ).encode("utf-8"),
                    0o600,
                )
                agy_env = {
                    key: child_env[key]
                    for key in (
                        "PATH",
                        "USER",
                        "LOGNAME",
                        "LANG",
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "NO_PROXY",
                        "http_proxy",
                        "https_proxy",
                        "no_proxy",
                        "SSL_CERT_FILE",
                        "SSL_CERT_DIR",
                        "PLAYWRIGHT_BROWSERS_PATH",
                        "PLAYWRIGHT_SKIP_BROWSER_GC",
                    )
                    if key in child_env
                }
                agy_env.update(
                    HOME=str(run_dir),
                    AGY_CLI_DISABLE_AUTO_UPDATE="true",
                    FIRM_LLM_CHILD="1",
                )
                for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR"):
                    sub = _directory(run_dir / var.lower())
                    agy_env[var] = str(sub)
                child_env = agy_env

                prompt_parts = list(parsed["positionals"])
                stdin_text = (
                    stdin_bytes.decode("utf-8", errors="replace").strip() if stdin_bytes else ""
                )
                if not prompt_parts and stdin_text:
                    prompt_text = stdin_text
                elif prompt_parts and stdin_text:
                    prompt_text = "\n\n".join(prompt_parts) + "\n\n" + stdin_text
                else:
                    prompt_text = "\n\n".join(prompt_parts)
                if not prompt_text:
                    reason = "launcher_setup"
                    print("firm: missing instruction for agy", file=sys.stderr)
                    return 2
                child_stdin = (
                    json.dumps({"event": "user", "message": {"content": prompt_text}}) + "\n"
                ).encode("utf-8")
                popen_stdin = subprocess.PIPE
                if parsed["resume_id"]:
                    telemetry.add(
                        json.dumps(
                            {
                                "type": "firm.agy_expected_session",
                                "conversation_id": parsed["resume_id"],
                            },
                            separators=(",", ":"),
                        )
                    )
                command = [
                    real_exe,
                    "--print=",
                    "--input-format=stream-json",
                    "--output-format=stream-json",
                    "--disable-slash-commands",
                    "--agent",
                    agent_name,
                    "--model",
                    _agy_native_model(model, effort),
                    *(["--conversation", parsed["resume_id"]] if parsed["resume_id"] else []),
                    "--print-timeout",
                    f"{print_timeout}s",
                ]

            rollout_offsets = (
                {p: p.stat().st_size for p in _rollouts(persist_dir)}
                if provider == "codex"
                else {}
            )

            preexec = _load_pdeathsig()
            process = subprocess.Popen(
                command,
                env=child_env,
                stdin=popen_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL if provider == "agy" else subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
            )

            progress_lock = threading.Lock()
            progress_flag = {"dirty": False}
            stop_event = threading.Event()
            stop_reason: dict[str, str | None] = {"value": None}
            deadline_mono = time.monotonic() + timeout_seconds

            def mark_progress() -> None:
                with progress_lock:
                    progress_flag["dirty"] = True

            def terminate_child(why: str) -> None:
                with progress_lock:
                    if stop_reason["value"] is None:
                        stop_reason["value"] = why
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    return
                kill_at = time.monotonic() + 5.0
                while process.poll() is None and time.monotonic() < kill_at:
                    time.sleep(0.1)
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            def heartbeat_loop() -> None:
                next_beat = time.monotonic() + HEARTBEAT_INTERVAL
                while not stop_event.wait(0.25):
                    now_m = time.monotonic()
                    if now_m >= deadline_mono:
                        terminate_child("timeout")
                        return
                    if now_m >= next_beat:
                        with progress_lock:
                            had_progress = progress_flag["dirty"]
                            progress_flag["dirty"] = False
                        status, hb_body = _http_json(
                            llm_url + "/heartbeat",
                            run_token,
                            {"progress": had_progress},
                            timeout=10.0,
                        )
                        if status == 200 and hb_body.get("status") == "stop":
                            terminate_child(hb_body.get("reason") or "cancelled")
                            return
                        if provider == "codex":
                            _scan_rollouts(persist_dir, rollout_offsets, telemetry.add)
                        next_beat = time.monotonic() + HEARTBEAT_INTERVAL

            stderr_chunks: list[bytes] = []

            def read_stderr() -> None:
                if process.stderr is None:
                    return
                try:
                    while chunk := process.stderr.read(4096):
                        if sum(len(c) for c in stderr_chunks) < 64 * 1024:
                            stderr_chunks.append(chunk)
                except OSError:
                    pass

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
            hb_thread.start()

            def forward_signal(signum, _frame):
                terminate_child("cancelled")

            previous = {
                sig: signal.signal(sig, forward_signal)
                for sig in (signal.SIGTERM, signal.SIGINT)
            }
            try:
                if child_stdin is not None and process.stdin is not None:
                    try:
                        process.stdin.write(child_stdin)
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                assert process.stdout is not None
                for raw_line in iter(process.stdout.readline, b""):
                    mark_progress()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        _project_event(provider, event, telemetry, state)
                exit_code = process.wait()
            finally:
                stop_event.set()
                hb_thread.join(timeout=6.0)
                stderr_thread.join(timeout=2.0)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    process.wait()

            if provider == "codex":
                _scan_rollouts(persist_dir, rollout_offsets, telemetry.add)

            if stop_reason["value"] is not None:
                reason = stop_reason["value"]
        finally:
            mirror.__exit__()
    finally:
        status_code, finish_body = _http_json(
            llm_url + "/finish",
            run_token,
            {
                "exit_code": exit_code,
                "logs": telemetry.render(),
                "reason": reason,
                "resumed": resumed,
            },
            timeout=30.0,
        )
        if prior_claude_oauth is None:
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        else:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = prior_claude_oauth
        shutil.rmtree(run_root, ignore_errors=True)

    answer = state.get("answer")
    finish_status = finish_body.get("status")
    session_id = finish_body.get("session_id")
    controls_notice = finish_body.get("controls_notice") or ""

    if finish_status == "completed" and isinstance(answer, str):
        if answer:
            sys.stdout.write(answer if answer.endswith("\n") else answer + "\n")
            sys.stdout.flush()
    else:
        if finish_body.get("reason") == "configuration":
            print(_format_refusal(provider, {"reason": "configuration"}, effort), file=sys.stderr)
        elif finish_body.get("reason") == "provider_failure":
            print(
                _format_refusal(provider, {"reason": "provider_failure"}, effort), file=sys.stderr
            )
        elif finish_body.get("reason") == "timeout" or reason == "timeout":
            print(f"firm: {provider} CLI call timed out after {timeout_seconds}s", file=sys.stderr)
        elif finish_body.get("reason") == "cancelled" or reason == "cancelled":
            print(f"firm: {provider} CLI call was cancelled", file=sys.stderr)
        elif status_code != 200:
            print(f"firm: failed to finalize {provider} CLI call", file=sys.stderr)

    if session_id:
        print(f"firm: session {session_id}", file=sys.stderr)
    if controls_notice:
        print(controls_notice, file=sys.stderr)

    if finish_status == "completed":
        return 0
    if finish_body.get("reason") in _TRANSIENT_REFUSALS or reason in {"timeout", "cancelled"}:
        return 75
    return exit_code if exit_code else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--config":
        print("usage: llm-cli --config PATH PROVIDER [ARGS...]", file=sys.stderr)
        return 2
    config_file, provider, rest = args[1], args[2], args[3:]
    if provider not in PROVIDERS:
        print(f"firm: unsupported LLM provider {provider}", file=sys.stderr)
        return 2

    if os.environ.get("FIRM_LLM_CHILD") == "1":
        print(
            "firm: nested LLM CLI calls are disabled; "
            "return your findings to the calling role session",
            file=sys.stderr,
        )
        return 2

    try:
        config = _load_config(config_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"firm: invalid LLM CLI configuration: {exc}", file=sys.stderr)
        return 2

    bin_dir = Path(config["state_dir"]) / "bin"
    parsed = _validate_and_parse(provider, rest)
    if isinstance(parsed, int):
        return parsed

    try:
        real_exe = _find_real_binary(provider, bin_dir)
    except FileNotFoundError:
        print(f"firm: {provider} CLI binary is unavailable", file=sys.stderr)
        return 2

    if parsed.get("help_or_version"):
        clean_env = _scrub_base_env(dict(os.environ), child_marker=False)
        proc = subprocess.run([real_exe, *rest], env=clean_env, check=False)
        return proc.returncode

    if provider == "agy" and parsed.get("resume_id"):
        db_path = (
            Path(config["private_home"])
            / ".firm"
            / "cli"
            / "agy"
            / "conversations"
            / f"{parsed['resume_id']}.db"
        )
        try:
            details = db_path.lstat()
        except FileNotFoundError:
            details = None
        if details is None or not stat.S_ISREG(details.st_mode):
            print(f"firm: unknown agy session {parsed['resume_id']}", file=sys.stderr)
            return 2

    if provider == "agy":
        stdin_bytes = _read_agy_stdin(bool(parsed["positionals"]))
        if not parsed["positionals"] and not stdin_bytes.strip():
            print("firm: missing instruction for agy", file=sys.stderr)
            return 2
    else:
        stdin_bytes = b""
    token_path = Path(config["credentials_dir"]) / "activation-token"
    try:
        token_details = token_path.lstat()
        if not stat.S_ISREG(token_details.st_mode):
            raise ValueError("invalid activation token file")
        activation_token = token_path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        print("firm: execution credentials are unavailable", file=sys.stderr)
        return 75

    status, response = _http_json(
        config["llm_url"] + "/start",
        activation_token,
        {
            "provider": provider,
            "effort": parsed["effort"],
            "resumed": bool(parsed["resumed"]),
        },
        timeout=30.0,
    )
    if status == 401:
        print("firm: LLM CLI tools exist only during your work period", file=sys.stderr)
        return 75
    if status != 200 or response.get("status") != "granted":
        reason = response.get("reason")
        print(_format_refusal(provider, response, parsed["effort"]), file=sys.stderr)
        return 75 if reason in _TRANSIENT_REFUSALS or status >= 500 else 2

    return _run_managed(provider, parsed, response, config, activation_token, real_exe, stdin_bytes)


if __name__ == "__main__":
    sys.exit(main())
