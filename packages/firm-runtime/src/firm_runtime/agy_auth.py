"""Native subscription credentials and private, host-owned AGY profiles."""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .provider_contract import ProviderSetupError
from .provider_support import read_private_credential

TOKEN_PATH = Path(".gemini/antigravity-cli/antigravity-oauth-token")
# Native tokens are read once and last about an hour; start with nearly a full token.
RENEWAL_MARGIN = 50 * 60
RENEWAL_TIMEOUT = 30
KEYCHAIN_SANDBOX = '(version 1)(allow default)(deny process-exec (literal "/usr/bin/security"))'


def failure(code, *, transient=False):
    return ProviderSetupError(
        category="transient" if transient else "auth",
        code=code,
        message="AGY subscription credentials require native renewal or isolated login",
    )


def access_projection(document):
    """Allowlist the native token envelope; renewal authority stays on the host."""
    if not isinstance(document, dict) or not isinstance(document.get("token"), dict):
        raise failure("credential_invalid")
    token, method = document["token"], document.get("auth_method")
    if not isinstance(method, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", method):
        raise failure("credential_invalid")
    access, expiry = token.get("access_token"), token.get("expiry")
    if (
        not isinstance(access, str)
        or not access.strip()
        or token.get("token_type") != "Bearer"
        or not isinstance(expiry, str)
    ):
        raise failure("credential_invalid")
    try:
        timestamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
        expires_at = timestamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise failure("credential_invalid_expiry") from None
    projected = {
        "access_token": access,
        "token_type": "Bearer",
        "expiry": expiry,
        "refresh_token": "",
    }
    if "expires_in" in token:
        lifetime = token["expires_in"]
        if type(lifetime) is not int or lifetime < 0:
            raise failure("credential_invalid_expiry")
        projected["expires_in"] = lifetime
    return {
        "kind": "agy-access",
        "data": {"auth_method": method, "token": projected},
        "expires_at": expires_at,
    }


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
    ):
        raise failure("credential_profile_invalid")
    path.chmod(0o700)


def profile_home(auth_file):
    """Require the dedicated native layout, never the user's ordinary HOME."""
    path = Path(auth_file).expanduser()
    if len(path.parents) < 3:
        raise failure("credential_profile_invalid")
    home = path.parents[2]
    if not path.is_absolute() or path != home / TOKEN_PATH or home.name != "home":
        raise failure("credential_profile_invalid")
    if home.resolve() == Path.home().resolve():
        raise failure("credential_profile_invalid")
    # Inspect ancestors without following links; no ordinary AGY state is read.
    for directory in (home.parent, home, home / ".gemini", path.parent):
        try:
            details = directory.lstat()
        except OSError:
            raise failure("credential_profile_unreadable") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            raise failure("credential_profile_invalid")
        directory.chmod(0o700)
    return home


def environment(home):
    """Equivalent to env -i: no inherited keys, cloud routing, or keyring env."""
    result = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }
    result["USER"] = result["LOGNAME"] = pwd.getpwuid(os.getuid()).pw_name
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        path = home / key.lower()
        private_directory(path)
        result[key] = str(path)
    return result


def prepare_profile(auth_file):
    path = Path(auth_file).expanduser()
    if len(path.parents) < 3:
        raise failure("credential_profile_invalid")
    home = path.parents[2]
    if not path.is_absolute() or path != home / TOKEN_PATH or home.name != "home":
        raise failure("credential_profile_invalid")
    if home.resolve() == Path.home().resolve():
        raise failure("credential_profile_invalid")
    # The credential area's ancestors may be ordinary owner directories. Every
    # directory inside the dedicated profile is private and cannot be a symlink.
    home.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (home.parent, home, home / ".gemini", path.parent, home / ".gemini/config"):
        private_directory(directory)
    profile_home(path)
    for relative, value in (
        (".gemini/antigravity-cli/settings.json", {"useG1Credits": False}),
        (".gemini/config/config.json", {"userSettings": {"useAiCredits": False}}),
    ):
        target = home / relative
        document = {}
        if target.exists() or target.is_symlink():
            document = json.loads(read_private_credential(target, "AGY settings"))
            if not isinstance(document, dict):
                raise failure("credential_profile_settings_changed")
        # Native models rewrites this file; own only billing keys, preserving native settings.
        if "userSettings" in value:
            settings = document.setdefault("userSettings", {})
            if not isinstance(settings, dict):
                raise failure("credential_profile_settings_changed")
            settings.update(value["userSettings"])
        else:
            document.update(value)
        descriptor, temporary = tempfile.mkstemp(prefix=".firm-settings-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(document, output)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return home


def native_command():
    executable = shutil.which("agy")
    if not executable:
        raise failure("credential_native_cli_missing")
    return executable


def host_argv(arguments, *, executable=None):
    """Keep native file fallback without allowing macOS Keychain access or UI."""
    command = [executable or native_command(), *arguments]
    if sys.platform != "darwin":
        return command
    sandbox = "/usr/bin/sandbox-exec"
    if not os.access(sandbox, os.X_OK):
        raise ProviderSetupError(
            category="auth",
            code="credential_sandbox_missing",
            message="AGY host login and renewal require /usr/bin/sandbox-exec on macOS",
        )
    return [sandbox, "-p", KEYCHAIN_SANDBOX, *command]


def read_native(auth_file):
    """Native writes may be incomplete; classify decoding as retryable, never prose."""
    profile_home(auth_file)
    raw = read_private_credential(Path(auth_file), "AGY")
    try:
        document = json.loads(raw)
        access_projection(document)
    except (ValueError, TypeError, RecursionError):
        raise failure("credential_read_unavailable", transient=True) from None
    return raw, document


def _wait_native(process):
    # Keep the leader unreaped until helpers are killed: its PID cannot be reused.
    try:
        deadline = time.monotonic() + RENEWAL_TIMEOUT
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("agy models", RENEWAL_TIMEOUT)
            time.sleep(0.05)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin returns EPERM for a group containing only zombies. Keep the
            # leader unreaped while checking; never ignore a live helper's denial.
            if sys.platform != "darwin":
                raise
            states = subprocess.check_output(
                ["/bin/ps", "-g", str(process.pid), "-o", "stat="],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).split()
            if not states or any(not state.startswith("Z") for state in states):
                raise
        process.wait()
    return process.returncode


def renew(auth_file, *, executable=None):
    """Native non-inference renewal. Caller holds the account credential lock."""
    command = host_argv(["models"], executable=executable)
    home = prepare_profile(auth_file)
    try:
        process = subprocess.Popen(
            command,
            env=environment(home), cwd=home,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, umask=0o077,
        )
        if _wait_native(process) != 0:
            raise failure("credential_refresh_unavailable", transient=True)
        return read_native(auth_file)[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        raise failure("credential_refresh_unavailable", transient=True) from None
    finally:
        prepare_profile(auth_file)


FORCED_EXPIRY = "2000-01-01T00:00:00Z"


def _expiry_backup(auth_file):
    return Path(auth_file).with_name(".firm-agy-expiry-backup")


def restore_expiry(auth_file, *, replace):
    """Recover an interrupted metadata edit before another locked credential read."""
    backup = _expiry_backup(auth_file)
    if not backup.exists() and not backup.is_symlink():
        return
    try:
        saved = json.loads(read_private_credential(backup, "AGY expiry backup"))
        raw, document = read_native(auth_file)
        token = document["token"]
        if token["expiry"] == FORCED_EXPIRY and token["access_token"] == saved["access_token"]:
            restored = {**document, "token": {**token, "expiry": saved["expiry"]}}
            access_projection(restored)
            if not replace(raw, restored):
                raise failure("credential_refresh_conflict", transient=True)
        backup.unlink()
    except (OSError, ValueError, KeyError, TypeError):
        raise failure("credential_restore_unavailable", transient=True) from None


def force_renew(auth_file, *, replace):
    """Expire only native cache metadata, under the caller's credential lock."""
    restore_expiry(auth_file, replace=replace)
    raw, original = read_native(auth_file)
    prior = access_projection(original)
    backup = _expiry_backup(auth_file)
    descriptor, temporary = tempfile.mkstemp(prefix=".agy-expiry-", dir=backup.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({key: original["token"][key] for key in ("access_token", "expiry")}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, backup)
    finally:
        Path(temporary).unlink(missing_ok=True)
    cached = {**original, "token": {**original["token"], "expiry": FORCED_EXPIRY}}
    # Native models otherwise renews only inside 300s. expires_in is unchanged.
    try:
        if not replace(raw, cached):
            raise failure("credential_refresh_conflict", transient=True)
        renewed = renew(auth_file)
        if access_projection(renewed)["expires_at"] <= prior["expires_at"]:
            raise failure("credential_refresh_invalid", transient=True)
        return renewed
    except ProviderSetupError:
        raise
    except (OSError, ValueError):
        raise failure("credential_refresh_unavailable", transient=True) from None
    finally:
        # The backup also survives SIGKILL and is restored on the next locked pass.
        restore_expiry(auth_file, replace=replace)
