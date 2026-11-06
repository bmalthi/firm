"""Guest-side setup and activation gate for the Codex sandbox adapter."""

from __future__ import annotations

import json
import math
import os
import runpy
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID


def _absolute(name: str) -> Path:
    value = os.environ.get(name, "")
    if not value.startswith("/"):
        raise ValueError(f"{name} must be an absolute path")
    return Path(value)


# The sandbox entrypoint supplies FIRM_CREDENTIALS_DIR; the runtime supplies the
# state and private-home paths from the sandbox layout. Nothing here is fixed.
def _codex_home() -> Path:
    return _absolute("FIRM_STATE_DIR") / "codex"


def _auth_source() -> Path:
    return _absolute("FIRM_CREDENTIALS_DIR") / "codex-auth"


def _session_root() -> Path:
    return _absolute("FIRM_PRIVATE_HOME") / ".firm" / "codex-sessions"


def _uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        raise ValueError("runtime session must be a UUID") from None
    if str(parsed) != value:
        raise ValueError("runtime session must be a canonical UUID")
    return value


def _directory(path: Path) -> None:
    """Create one private directory without following a role-created symlink."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        return
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("provider session path must be a directory")
    path.chmod(0o700)


def _prepare_home(runtime_session_id: str) -> None:
    os.umask(0o077)
    session_roots = _session_root()
    _directory(session_roots.parent)
    _directory(session_roots)
    session_root = session_roots / _uuid(runtime_session_id)
    _directory(session_root)
    sessions = session_root / "sessions"
    _directory(sessions)

    codex_home = _codex_home()
    codex_home.mkdir(mode=0o700)
    auth = codex_home / "auth.json"
    if os.environ.get("FIRM_TASK_RUN") == "1":
        auth.symlink_to(_auth_source())
    else:
        shutil.copyfile(_auth_source(), auth)
        auth.chmod(0o600)
    (codex_home / "sessions").symlink_to(sessions, target_is_directory=True)


def _safe_code(value: object) -> str | None:
    if isinstance(value, int) and 100 <= value <= 599:
        return f"http_{value}"
    return None


def _event(kind: str, code: str | None = None) -> None:
    event = {"type": "firm.launch_failed", "kind": kind}
    if code is not None:
        event["code"] = code
    print(json.dumps(event), flush=True)


def _usage_event(record: object) -> dict | None:
    """Project Codex usage and model identity; never emit transcript content."""
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
    event = {"type": "firm.telemetry", "provider": "codex", "usage": usage}
    if rate_limits:
        event["rate_limits"] = rate_limits
    return event


def _rate_limits_event(value: object) -> dict | None:
    """Copy only Codex's numeric rollout allowance windows into bounded telemetry."""
    if not isinstance(value, dict):
        return None
    result = {}
    limit_id = value.get("limit_id")
    if isinstance(limit_id, str) and 0 < len(limit_id) <= 64 and all(
        (char.isascii() and char.isalnum()) or char in "._-" for char in limit_id
    ):
        result["limit_id"] = limit_id
    for name in ("primary", "secondary"):
        source = value.get(name)
        if not isinstance(source, dict):
            continue
        used = source.get("used_percent")
        reset = source.get("resets_at")
        if (
            type(used) not in (int, float)
            or type(reset) not in (int, float)
        ):
            continue
        try:
            used, reset = float(used), float(reset)
        except OverflowError:
            continue
        if (
            not math.isfinite(used)
            or used < 0
            or not math.isfinite(reset)
            or not 0 < reset <= 253402300799
        ):
            continue
        projected = {"used_percent": used, "resets_at": reset}
        result[name] = projected
    return result if "primary" in result or "secondary" in result else None


def _rollouts(root: Path) -> list[Path]:
    return [path for path in root.glob("**/*.jsonl") if path.is_file() and not path.is_symlink()]


def _scan_rollouts(root: Path, offsets: dict[Path, int]) -> None:
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
                event = _usage_event(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event is not None:
                print(json.dumps(event, separators=(",", ":")), flush=True)


def _run(command: list[str], sessions: Path) -> int:
    if os.environ.get("FIRM_CREDENTIALS_URL"):
        mirror = runpy.run_path(str(_auth_source().parent / "credential-mirror"))
        try:
            auth = _auth_source() if os.environ.get("FIRM_TASK_RUN") == "1" else (
                sessions.parent / "auth.json"
            )
            with mirror["CredentialMirror"](auth):
                return _run_process(command, sessions)
        except mirror["MirrorUnavailable"]:
            return 72
    return _run_process(command, sessions)


def _run_process(command: list[str], sessions: Path) -> int:
    offsets = {path: path.stat().st_size for path in _rollouts(sessions)}
    environment = dict(os.environ)
    # Codex 0.153.4 login and OpenAI provider overrides. The
    # copied auth.json and explicit launch settings own authentication/routing.
    for key in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
    ):
        environment.pop(key, None)
    process = subprocess.Popen(command, env=environment)

    def forward(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    previous = {
        signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while process.poll() is None:
            _scan_rollouts(sessions, offsets)
            time.sleep(0.25)
        _scan_rollouts(sessions, offsets)
        return process.returncode
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _begin_activation(url: str, token: str) -> bool:
    request = urllib.request.Request(
        url,
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status == 200:
                return True
            _event("activation_gate", _safe_code(response.status))
    except urllib.error.HTTPError as exc:
        _event("activation_gate", _safe_code(exc.code))
    except (OSError, TimeoutError, urllib.error.URLError):
        _event("activation_gate", "connection")
    return False


def _begin_summary(url: str, token: str) -> bool:
    """Enter the one-use summary gate without granting role/MCP authority."""
    if not url or not token:
        return False
    return _begin_activation(url, token)


def main() -> int:
    try:
        command = json.loads(os.environ["FIRM_CODEX_ARGV"])
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
        ):
            raise ValueError("invalid Codex command")
        _prepare_home(os.environ["FIRM_RUNTIME_SESSION_ID"])
        if os.environ.get("FIRM_SUMMARY_RUN") == "1":
            token = os.environ.get("FIRM_SUMMARY_TOKEN", "")
            if not _begin_summary(os.environ.get("FIRM_SUMMARY_START_URL", ""), token):
                return 70
            codex_home = _codex_home()
            os.environ["CODEX_HOME"] = str(codex_home)
            return _run(command, codex_home / "sessions")
        token = os.environ["FIRM_ACTIVATION_TOKEN"]
        if not token:
            raise ValueError("missing activation token")
        if not _begin_activation(os.environ["FIRM_ACTIVATION_START_URL"], token):
            return 70
        codex_home = _codex_home()
        os.environ["CODEX_HOME"] = str(codex_home)
        return _run(command, codex_home / "sessions")
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        _event("launcher_setup")
        return 71
    return 71


if __name__ == "__main__":
    sys.exit(main())
