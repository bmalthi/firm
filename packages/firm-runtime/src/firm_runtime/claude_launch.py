"""Guest-side setup and activation gate for the Claude sandbox adapter."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import sys
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
def _claude_home() -> Path:
    return _absolute("FIRM_STATE_DIR") / "claude"


def _auth_source() -> Path:
    return _absolute("FIRM_CREDENTIALS_DIR") / "claude-auth"


def _session_root() -> Path:
    return _absolute("FIRM_PRIVATE_HOME") / ".firm" / "claude-sessions"


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
    sessions = _session_root()
    _directory(sessions.parent)
    _directory(sessions)
    session_root = sessions / _uuid(runtime_session_id)
    _directory(session_root)
    projects = session_root / "projects"
    _directory(projects)

    claude_home = _claude_home()
    claude_home.mkdir(mode=0o700)
    (claude_home / "projects").symlink_to(projects, target_is_directory=True)


def _prepare_auth(kind: str) -> None:
    if kind == "oauth-token":
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise ValueError("missing Claude OAuth token")
        return
    if kind == "credentials-json":
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        credentials = _claude_home() / ".credentials.json"
        if os.environ.get("FIRM_TASK_RUN") == "1":
            credentials.symlink_to(_auth_source())
        else:
            shutil.copyfile(_auth_source(), credentials)
            credentials.chmod(0o600)
        return
    raise ValueError("invalid Claude authentication kind")


def _safe_code(value: object) -> str | None:
    if isinstance(value, int) and 100 <= value <= 599:
        return f"http_{value}"
    return None


def _event(kind: str, code: str | None = None) -> None:
    event = {"type": "firm.launch_failed", "kind": kind}
    if code is not None:
        event["code"] = code
    print(json.dumps(event), flush=True)


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
        command = json.loads(os.environ["FIRM_CLAUDE_ARGV"])
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
        ):
            raise ValueError("invalid Claude command")
        _prepare_home(os.environ["FIRM_RUNTIME_SESSION_ID"])
        _prepare_auth(os.environ["FIRM_CLAUDE_AUTH_KIND"])
        if os.environ.get("FIRM_SUMMARY_RUN") == "1":
            token = os.environ.get("FIRM_SUMMARY_TOKEN", "")
            if not _begin_summary(os.environ.get("FIRM_SUMMARY_START_URL", ""), token):
                return 70
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
                os.environ.pop(name, None)
            claude_home = _claude_home()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
            # The guest denies the nested namespaces Claude's optional subprocess
            # environment sandbox requires, for workers as well as roles.
            os.environ["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "0"
            if os.environ.get("FIRM_CREDENTIALS_URL"):
                mirror = runpy.run_path(str(_auth_source().parent / "credential-mirror"))
                auth = _auth_source() if os.environ.get("FIRM_TASK_RUN") == "1" else (
                    claude_home / ".credentials.json"
                )
                return mirror["run_claude"](command, auth)
            os.execvpe(command[0], command, os.environ)
            return 0
        token = os.environ["FIRM_ACTIVATION_TOKEN"]
        if not token:
            raise ValueError("missing activation token")
        if not _begin_activation(os.environ["FIRM_ACTIVATION_START_URL"], token):
            return 70

        # The selected account token is the only provider credential source.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            os.environ.pop(name, None)
        claude_home = _claude_home()
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        # The role runs as an ordinary account inside the firm's Linux VM, whose
        # policy denies the nested namespaces Claude's optional PID sandbox needs.
        os.environ["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "0"
        if os.environ.get("FIRM_CREDENTIALS_URL"):
            mirror = runpy.run_path(str(_auth_source().parent / "credential-mirror"))
            return mirror["run_claude"](command, claude_home / ".credentials.json")
        os.execvpe(command[0], command, os.environ)
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        _event("launcher_setup")
        return 71
    return 71


if __name__ == "__main__":
    sys.exit(main())
