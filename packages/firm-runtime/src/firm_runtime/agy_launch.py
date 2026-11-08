"""Standalone guest AGY preparation, admission gate and supervised native process."""

from __future__ import annotations

import json
import math
import os
import re
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import UUID

# Declared tools are not a security boundary: strict scoped permissions also apply.
NATIVE_TOOLS = [
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
]
NATIVE_INVENTORY = [
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
MODE_TOOLS = {"role": NATIVE_TOOLS, "task": NATIVE_TOOLS, "tool_free": []}
STARTUP_MARGIN = 300
MAX_EVENT_BYTES = 1024 * 1024
NATIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _settings(mode):
    denied = ["read_url(*)", "execute_url(*)"]
    if mode != "role":
        denied.append("mcp(*)")
    if mode == "tool_free":
        denied += [f"{name}(*)" for name in sorted(set(NATIVE_INVENTORY) | {
            "command", "read_file", "write_file",
        })]
    # Native strict approvals deny task reads; roles/tasks use VM/UID isolation,
    # like Codex/Claude, with the policy verified against AGY 1.5.3.
    return {
        "useG1Credits": False,
        "toolPermission": "strict" if mode == "tool_free" else "always-proceed",
        "agentMode": "accept-edits",
        "permissions": {"deny": denied},
    }


def _absolute(name):
    value = os.environ.get(name, "")
    if not value.startswith("/"):
        raise ValueError("expected an absolute layout path")
    return Path(value)


def _directory(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        return
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("AGY state must be a real directory")
    path.chmod(0o700)


def _write(path, content):
    """Replace only ordinary Firm settings, never follow a role-created link."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("invalid AGY settings path")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".firm-agy-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _mode_tools(mode):
    if mode not in MODE_TOOLS:
        raise ValueError("invalid AGY mode")
    return MODE_TOOLS[mode]


def _mcp_settings(mode):
    if mode != "role":
        return None
    names = json.loads(os.environ["FIRM_AGY_MCP_TOOLS"])
    if (
        not isinstance(names, list)
        or not names
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            for name in names
        )
    ):
        raise ValueError("invalid Firm tool catalog")
    timeout = float(os.environ["FIRM_AGY_MCP_TIMEOUT"])
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid Firm tool timeout")
    return {
        "mcpServers": {
            "firm": {
                "serverUrl": os.environ["FIRM_AGY_MCP_URL"],
                "headers": {"Authorization": "Bearer " + os.environ["FIRM_ACTIVATION_TOKEN"]},
                # AGY 1.5.3 silently ignores the whole server when this is a JSON float.
                "timeoutSeconds": math.ceil(timeout),
                "tools": {name: {"eager": True} for name in names},
            }
        }
    }


def _resume_arguments(native_id, home):
    if not native_id:
        return []
    if not NATIVE_ID.fullmatch(native_id):
        raise ValueError("invalid AGY conversation ID")
    directory = home / ".gemini/antigravity-cli/conversations"
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ValueError("AGY conversation directory must not be a link")
    if not stat.S_ISREG((directory / f"{native_id}.db").lstat().st_mode):
        raise ValueError("AGY resume requires an existing regular conversation file")
    return ["--conversation", native_id]


def _home():
    session = os.environ["FIRM_RUNTIME_SESSION_ID"]
    if str(UUID(session)) != session:
        raise ValueError("runtime session must be a canonical UUID")
    root = _absolute("FIRM_PRIVATE_HOME") / ".firm"
    for directory in (root, root / "agy-sessions", root / "agy-sessions" / session):
        _directory(directory)
    return root / "agy-sessions" / session


def _link_ephemeral(path, source):
    # Credentials live in the sandbox's ephemeral credential directory, not in
    # retained native conversation state. Replace stale links from interrupted runs.
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ValueError("invalid AGY credential path")
    path.symlink_to(source)


def _prepare_home(mode):
    os.umask(0o077)
    tools = _mode_tools(mode)
    home = _home()
    for path in (
        home / ".gemini",
        home / ".gemini/antigravity-cli",
        home / ".gemini/config",
        home / ".gemini/config/agents",
    ):
        _directory(path)
    _cleanup(home)  # Remove stale credential links before any settings work.
    credentials = _absolute("FIRM_CREDENTIALS_DIR")
    auth = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    _link_ephemeral(auth, credentials / "agy-auth")
    _write(home / ".gemini/antigravity-cli/settings.json", json.dumps(_settings(mode)))
    _write(
        home / ".gemini/config/config.json", json.dumps({"userSettings": {"useAiCredits": False}})
    )
    mcp = _mcp_settings(mode)
    mcp_path = home / ".gemini/config/mcp_config.json"
    if mcp is not None:
        _write(credentials / "agy-mcp", json.dumps(mcp))
        _link_ephemeral(mcp_path, credentials / "agy-mcp")
    else:
        mcp_path.unlink(missing_ok=True)
    instructions = (credentials / "agy-instructions").read_text()
    _write(
        home / f".gemini/config/agents/firm-{mode}.md",
        f"---\nname: firm-{mode}\ndescription: Firm-owned {mode} agent.\n"
        f"tools: {json.dumps(tools)}\ninheritCustomizations: false\n"
        "mainAgent: true\nsubagent: false\n---\n" + instructions,
    )
    return home, auth


def _print_timeout(auth):
    value = json.loads(auth.read_bytes())
    expiry = datetime.fromisoformat(value["token"]["expiry"].replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        raise ValueError("AGY token expiry requires timezone")
    # Native reads credentials once: yield before expiry poisons the conversation.
    # The dispatcher separately owns the activity-extended inactivity deadline.
    timeout = math.floor(expiry.timestamp() - time.time() - STARTUP_MARGIN - 1)
    if timeout < 1:
        raise ValueError("AGY token has insufficient startup headroom")
    return ["--print-timeout", f"{timeout}s"]


def _event(kind, code=None):
    event = {"type": "firm.launch_failed", "kind": kind}
    if code is not None:
        event["code"] = code
    print(json.dumps(event), flush=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _begin_activation(url, token):
    if not url or not token:
        _event("activation_gate")
        return False
    request = urllib.request.Request(
        url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
            if response.status == 200:
                return True
            _event("activation_gate", f"http_{response.status}")
    except urllib.error.HTTPError as exc:
        _event("activation_gate", f"http_{exc.code}")
    except (OSError, urllib.error.URLError):
        _event("activation_gate", "connection")
    return False


def _environment(home):
    # Preserve guest networking and ordinary identity, not role admission secrets,
    # API keys or provider endpoint overrides. The token file selects subscription.
    result = {
        key: os.environ[key]
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
        )
        if key in os.environ
    }
    result.update(HOME=str(home), AGY_CLI_DISABLE_AUTO_UPDATE="true")
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        path = home / key.lower()
        _directory(path)
        result[key] = str(path)
    return result


def _relay(stream, expected, failed):
    """Forward bounded native lines, terminating a silently fresh resume immediately."""
    try:
        while line := stream.readline(MAX_EVENT_BYTES + 1):
            if len(line) > MAX_EVENT_BYTES:
                _event("launcher_setup", "event_too_large")
                failed.set()
                return
            if expected:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    event = None
                if (
                    isinstance(event, dict)
                    and event.get("event") == "init"
                    and event.get("conversation_id") != expected
                ):
                    _event("launcher_setup", "resume_id_mismatch")
                    failed.set()
                    return
            sys.stdout.write(line.decode(errors="replace"))
            sys.stdout.flush()
    except OSError:
        _event("launcher_setup", "output_unavailable")
        failed.set()


def _run(command, home):
    expected = os.environ.get("FIRM_AGY_NATIVE_ID", "")
    if expected:
        print(
            json.dumps({"type": "firm.agy_expected_session", "conversation_id": expected}),
            flush=True,
        )
    process = subprocess.Popen(
        command, env=_environment(home), start_new_session=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
    failed = threading.Event()
    relay = threading.Thread(target=_relay, args=(process.stdout, expected, failed), daemon=True)
    stop_deadline = None

    def forward(signum, _frame):
        nonlocal stop_deadline
        stop_deadline = time.monotonic() + 5
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    relay.start()
    try:
        while True:
            if failed.is_set():
                forward(signal.SIGKILL, None)
            try:
                code = process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    forward(signal.SIGKILL, None)
    finally:
        forward(signal.SIGKILL, None)
        process.wait()
        relay.join(timeout=5)
        if not relay.is_alive():
            process.stdout.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 71 if failed.is_set() or relay.is_alive() else code


def _cleanup(home):
    # Refuse symlinked parent directories even on a failed preparation path.
    for relative in (
        ".gemini/antigravity-cli/antigravity-oauth-token",
        ".gemini/config/mcp_config.json",
    ):
        path = home / relative
        try:
            if all(
                stat.S_ISDIR(parent.lstat().st_mode) for parent in (home / ".gemini", path.parent)
            ):
                path.unlink(missing_ok=True)
        except OSError:
            pass


def main():
    home = None
    try:
        command = json.loads(os.environ["FIRM_AGY_ARGV"])
        if (
            not isinstance(command, list)
            or len(command) < 2
            or command[1] != "--print="
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
        ):
            raise ValueError("invalid AGY argv")
        mode = os.environ["FIRM_AGY_MODE"]
        home = _home()
        home, auth = _prepare_home(mode)
        try:
            command += _resume_arguments(os.environ.get("FIRM_AGY_NATIVE_ID", ""), home)
        except (OSError, ValueError):
            _event("launcher_setup", "resume_state_missing")
            return 71
        role = mode == "role"
        token = os.environ["FIRM_ACTIVATION_TOKEN" if role else "FIRM_SUMMARY_TOKEN"]
        url = os.environ["FIRM_ACTIVATION_START_URL" if role else "FIRM_SUMMARY_START_URL"]
        if not _begin_activation(url, token):
            return 70
        with ExitStack() as stack:
            if os.environ.get("FIRM_CREDENTIALS_URL"):
                module = runpy.run_path(
                    str(_absolute("FIRM_CREDENTIALS_DIR") / "credential-mirror")
                )
                try:
                    stack.enter_context(
                        module["CredentialMirror"](_absolute("FIRM_CREDENTIALS_DIR") / "agy-auth")
                    )
                except module["MirrorUnavailable"]:
                    return 72
            # Shared mirror polling is harmless; native reads this token only once.
            # Bound generation by the token actually staged at native startup.
            try:
                command += _print_timeout(auth)
            except (KeyError, ValueError, OSError):
                _event("launcher_setup", "token_headroom")
                return 72
            return _run(command, home)
    except (KeyError, OSError, ValueError, TypeError, urllib.error.URLError):
        _event("launcher_setup", "settings_invalid")
        return 71
    finally:
        if home is not None:
            _cleanup(home)


if __name__ == "__main__":
    sys.exit(main())
