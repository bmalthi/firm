"""Small, resumable helpers for the interactive first ``firm start`` flow."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .accounts import credentials_path
from .credential_store import CredentialStore
from .errors import FirmError
from .provider_contract import ProviderSetupError


@dataclass(frozen=True)
class SetupChoice:
    provider: str
    task_kind: str
    task: str


def default_data_directory() -> Path:
    """Keep runs under the directory from which the owner starts Firm."""
    return Path.cwd() / "firms"


def firm_slug(task: str) -> str:
    """Derive a readable, filesystem-safe firm ID suffix from the task text."""
    words = re.findall(r"[a-z0-9]+", task.lower())
    return "-".join(words[:5])[:40].strip("-") or "new-firm"


def choose_directory(root: Path, task: str, *, today: date | None = None) -> Path:
    root = Path(root).expanduser().absolute()
    day = (today or date.today()).isoformat()
    base = root / f"{day}_{firm_slug(task)}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base.name}_{suffix}"
        suffix += 1
    return candidate


def list_firms(root: Path) -> list[dict]:
    """List direct child firms without constructing a Store or opening their DBs."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    firms = []
    for child in sorted(root.iterdir()):
        try:
            info = child.lstat()
        except OSError:
            continue
        if not child.is_dir() or child.is_symlink() or info.st_uid != os.getuid():
            continue
        from .compatibility import validate_firm
        from .setup_check import database_initialization_state

        database = child / "durable" / "firm.sqlite3"
        database_state = database_initialization_state(database)
        release_record = child / "firm-release.json"
        release_present = release_record.exists() or release_record.is_symlink()
        release_error = None
        if database_state in {"initialized", "partial"} or release_present:
            try:
                validate_firm(child)
            except (OSError, ValueError) as exc:
                release_error = str(exc)
        if release_error is not None:
            state = "needs attention"
            reason = release_error
        elif database_state == "initialized":
            state, reason = "initialized", None
        elif database_state in {"missing", "partial"}:
            has_setup_files = any(
                (child / name).is_file() for name in ("mandate.md", "firm.toml", "sandbox.toml")
            )
            if has_setup_files or not any(child.iterdir()) or database_state == "partial":
                state, reason = "incomplete", None
            else:
                continue
        else:
            state, reason = "needs attention", "The database is not a safe, resumable Firm state."
        firms.append({"name": child.name, "path": child, "state": state, "reason": reason})
    return firms


def _write_new_file(path: Path, content: str) -> None:
    """Create one owner-only UTF-8 file atomically; never replace user data."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        # link() gives create-if-absent semantics; replace() would overwrite a file
        # created by another process after the wizard's existence check.
        os.link(temporary, path)
    except FileExistsError:
        raise FirmError(f"refusing to replace existing {path.name}: {path}") from None
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def generated_files(directory: Path, provider: str, mandate: str) -> dict[Path, str]:
    """Return the ordinary files consumed by existing init/check/start paths."""
    if provider not in {"codex", "claude", "agy"}:
        raise FirmError("choose Codex, Claude or experimental AGY")
    if not isinstance(mandate, str) or not mandate.strip():
        raise FirmError("the first task must not be empty")
    firm_id = Path(directory).name.lower()
    account = f"{provider}-personal"
    if provider == "codex":
        model, effort = "gpt-6-luna", "low"
    elif provider == "agy":
        model, effort = "gemini-3.8-flash", "medium"
    else:
        model, effort = "sonnet", "medium"
    firm_toml = (
        "# One provider and one role for the first firm.\n"
        "max_active = 1\n"
        "staffing = false\n"
        f'founder_profile = "{provider}"\n\n'
        f"[profiles.{provider}]\n"
        f'provider = "{provider}"\n'
        f'model = "{model}"\n'
        f'effort = "{effort}"\n'
        f'accounts = ["{account}"]\n\n'
        f"[accounts.{account}]\n"
        f'provider = "{provider}"\n'
        "max_active = 1\n"
    )
    sandbox_toml = f'[firm]\nid = "{firm_id}"\ncpus = 4\nmemory_gib = 8\ndisk_gib = 60\n'
    return {
        Path(directory) / "mandate.md": mandate.rstrip() + "\n",
        Path(directory) / "firm.toml": firm_toml,
        Path(directory) / "sandbox.toml": sandbox_toml,
    }


def create_firm_files(directory: Path, provider: str, mandate: str) -> None:
    """Create the three source files atomically and never overwrite any of them."""
    directory = Path(directory).expanduser().absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = directory.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise FirmError("firm directory must be owner-controlled")
    os.chmod(directory, 0o700)
    for path, content in generated_files(directory, provider, mandate).items():
        _write_new_file(path, content)


def create_missing_files(directory: Path, provider: str, mandate: str) -> None:
    """Complete only absent setup files; preserve every file already present."""
    directory = Path(directory).expanduser().absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = directory.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise FirmError("firm directory must be owner-controlled")
    os.chmod(directory, 0o700)
    for path, content in generated_files(directory, provider, mandate).items():
        if not path.exists():
            _write_new_file(path, content)


def _require_private_directory(path: Path) -> None:
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise FirmError(
            f"setup state directory is not a private owner-controlled directory: {path}"
        )


def _private_credential_exists(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise FirmError("provider credential state could not be inspected safely") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise FirmError("provider credential state is not a private regular file")
    return True


def _provider_command(provider: str) -> tuple[list[str], str]:
    if provider == "agy":
        from .agy_auth import native_command

        executable = native_command()
        return [executable], executable
    if provider not in {"codex", "claude"}:
        raise FirmError("unsupported login provider")
    executable = provider
    found = shutil.which(executable)
    if found:
        return [found], found
    npx = shutil.which("npx")
    if not npx:
        raise FirmError(
            f"Node.js/npm is needed to run the {provider.title()} login flow. "
            "Install Node.js from its official distribution, then resume firm start."
        )
    package = "@openai/codex" if provider == "codex" else "@anthropic-ai/claude-code"
    return [npx, "--yes", "--package", package, executable], npx


def _stop_child(process: subprocess.Popen) -> None:
    """Best-effort bounded stop and reap for an interrupted provider child."""
    with contextlib.suppress(BaseException):
        if process.poll() is None:
            process.terminate()

    def kill_and_reap() -> None:
        with contextlib.suppress(BaseException):
            process.kill()
        with contextlib.suppress(BaseException):
            process.wait(timeout=1)

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        kill_and_reap()
    except BaseException:
        # Keep the read/cancellation exception that caused cleanup to run.
        kill_and_reap()


def _run_claude_setup_token(
    command: list[str], *, environment: dict, cwd: Path
) -> tuple[int, str | None]:
    """Stream sign-in help while capturing and redacting the emitted bearer token."""
    token_pattern = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_-]+")
    process = subprocess.Popen(
        command,
        env=environment,
        cwd=cwd,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    tokens = []
    try:
        if process.stdout is not None:
            for raw_line in iter(process.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace")
                found = token_pattern.findall(line)
                tokens.extend(found)
                safe_line = token_pattern.sub("[credential hidden]", line)
                if safe_line:
                    print(safe_line, end="", file=sys.stderr, flush=True)
        returncode = process.wait()
    except BaseException:
        _stop_child(process)
        if process.stdout is not None:
            with contextlib.suppress(BaseException):
                process.stdout.close()
        raise
    else:
        if process.stdout is not None:
            process.stdout.close()
    return returncode, tokens[0] if len(tokens) == 1 else None


def login_provider(
    provider: str,
    account: str,
    *,
    state_directory: Path | None = None,
    target_credentials_path: Path | None = None,
    force_login: bool = False,
) -> Path:
    """Run an isolated provider sign-in and save its credential.

    Temporary provider homes are intentionally retained if validation or the final
    owner-private save fails. This lets the owner resume without losing a completed
    login, while a successful save removes the temporary copy.
    """
    if provider == "agy":
        return _login_agy(account, target_credentials_path=target_credentials_path,
                          force_login=force_login)
    if provider not in {"codex", "claude"}:
        raise FirmError("choose Codex or Claude")
    if state_directory is None:
        temp_root = Path(tempfile.mkdtemp(prefix=f"firm-{provider}-login-"))
    else:
        temp_root = Path(state_directory).expanduser().absolute()
        temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(temp_root)
    os.chmod(temp_root, 0o700)
    home = temp_root / "provider-home"
    home.mkdir(mode=0o700, exist_ok=True)
    _require_private_directory(home)
    environment = dict(os.environ)
    if provider == "codex":
        environment["CODEX_HOME"] = str(home)
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("OPENAI_BASE_URL", None)
        environment.pop("CODEX_API_KEY", None)
        credential = home / "auth.json"
        config = home / "config.toml"
        if not config.exists():
            descriptor = os.open(
                config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write('cli_auth_credentials_store = "file"\n')
                output.flush()
                os.fsync(output.fileno())
        else:
            details = config.lstat()
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or config.read_text(encoding="utf-8") != 'cli_auth_credentials_store = "file"\n'
            ):
                raise FirmError(
                    f"isolated Codex login settings changed at {config}; remove that setup-login "
                    "directory and rerun firm start."
                )
    else:
        environment["CLAUDE_CONFIG_DIR"] = str(home)
        environment["DISABLE_AUTOUPDATER"] = "1"
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_PROFILE",
            "ANTHROPIC_FEDERATION_RULE_ID",
            "ANTHROPIC_ORGANIZATION_ID",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            environment.pop(name, None)
        credential = temp_root / "claude-setup-token"

    target = (
        credentials_path(account)
        if target_credentials_path is None
        else Path(target_credentials_path).expanduser()
    )
    if _private_credential_exists(credential):
        if force_login:
            credential.unlink()
        else:
            try:
                raw = credential.read_bytes()
                CredentialStore(provider, target).save(raw)
            except (OSError, ProviderSetupError):
                pass
            else:
                shutil.rmtree(temp_root)
                with contextlib.suppress(OSError):
                    temp_root.parent.rmdir()
                return target
    command, _ = _provider_command(provider)
    login_command = command + (["login"] if provider == "codex" else ["setup-token"])

    print(
        f"An isolated {provider.title()} sign-in is opening. Complete it in your browser, "
        "then return here.",
        file=sys.stderr,
        flush=True,
    )
    try:
        if provider == "claude":
            returncode, setup_token = _run_claude_setup_token(
                login_command, environment=environment, cwd=temp_root
            )
        else:
            completed = subprocess.run(login_command, env=environment, cwd=temp_root, check=False)
            returncode, setup_token = completed.returncode, None
    except OSError as exc:
        raise FirmError(f"could not start the {provider.title()} login flow: {exc}") from None
    if returncode != 0:
        raise FirmError(
            f"{provider.title()} login exited with status {returncode}; "
            f"its temporary state is kept at {temp_root}. Rerun firm start to resume."
        )
    if (
        provider == "claude"
        and setup_token is not None
        and not _private_credential_exists(credential)
    ):
        descriptor = os.open(
            credential,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(setup_token.encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
    if not _private_credential_exists(credential):
        raise FirmError(
            f"{provider.title()} completed without credentials in its isolated login state. "
            f"Temporary state is kept at {temp_root}; rerun login or consult the provider's "
            "official sign-in help."
        )

    try:
        raw = credential.read_bytes()
        CredentialStore(provider, target).save(raw)
    except (OSError, ProviderSetupError) as exc:
        raise FirmError(
            f"{provider.title()} credentials could not be saved ({getattr(exc, 'code', 'io')}); "
            f"temporary login state is kept at {temp_root}. Rerun firm start to resume."
        ) from None
    if state_directory is not None:
        shutil.rmtree(temp_root)
        with contextlib.suppress(OSError):
            temp_root.parent.rmdir()
    else:
        shutil.rmtree(temp_root)
    return target


def _login_agy(account, *, target_credentials_path=None, force_login=False):
    """Retain the host-owned native profile for subsequent native renewal."""
    from .agy_auth import environment, host_argv, prepare_profile
    from .provider_support import read_private_credential

    target = (credentials_path(account, "agy") if target_credentials_path is None
              else Path(target_credentials_path).expanduser())
    home = prepare_profile(target)
    store = CredentialStore("agy", target)
    command, _ = _provider_command("agy")
    print(
        "Experimental AGY uses your subscription login in a dedicated host profile. "
        "Credit overages are disabled. Quota and context telemetry are unavailable; "
        "Workspace instructions can be read and implicit native tools are not fully isolated.",
        file=sys.stderr,
    )
    if not force_login and _private_credential_exists(target):
        from .credential_store import guest_credentials

        guest_credentials("agy", read_private_credential(target, "AGY"))
        return target
    # Native login state may be needed by models; retain the whole dedicated profile.
    # Like other providers, interactive login never holds the renewal lock.
    process = None
    try:
        process = subprocess.Popen(
            host_argv(
                ["acp", "--login", "--include-default-customizations=false"],
                executable=command[0],
            ),
            env=environment(home), cwd=home, umask=0o077,
        )
        returncode = process.wait()
    except OSError:
        raise FirmError(f"could not start AGY login; profile retained at {home}") from None
    except BaseException:
        if process is not None:
            _stop_child(process)
        raise
    finally:
        prepare_profile(target)
    if returncode != 0:
        raise FirmError(
            f"AGY login exited with status {returncode}; profile retained at {home}. "
            "Rerun firm login to retry sign-in in this profile."
        )
    try:
        # Validate and atomically save the token, preserving all other native state.
        store.save(read_private_credential(target, "AGY"))
    except (OSError, ProviderSetupError) as exc:
        raise FirmError(
            f"AGY credentials could not be saved ({getattr(exc, 'code', 'io')}); "
            f"profile retained at {home}. Rerun firm login to retry sign-in."
        ) from None
    return target
