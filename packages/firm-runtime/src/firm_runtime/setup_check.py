"""Read-only checks for files and local dependencies needed to start a firm."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tomllib
from pathlib import Path

from firm_sandbox import Config, linux_base
from firm_sandbox.compatibility import validate_firm_directories
from firm_sandbox.host_paths import read_text_file

from .credential_store import setup_token_warning
from .lifecycle import validate_config
from .provider_contract import ProviderSetupError
from .providers import provider_adapter

MIN_LIMA_VERSION = (2, 2, 0)
HOST_MEMORY_RESERVE_GIB = 4
_TABLE_DECLARATION = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE,
)


def _result(name: str, status: str, message: str, *, action: str | None = None) -> dict:
    result = {"check": name, "status": status, "message": message}
    if action is not None:
        result["action"] = action
    return result


def _check_directory(path: Path) -> dict:
    """Apply init's leaf ownership and permissions policy without changing the path."""
    try:
        details = path.lstat()
    except OSError:
        details = None
    if (
        details is None
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        return _result(
            "directory",
            "failure",
            "The firm directory is missing or is not owner-controlled.",
            action=(
                "Use a real directory owned by the current user and remove group/other "
                "write permissions."
            ),
        )
    return _result("directory", "good", "The firm directory is owner-controlled.")


def _read_database(path: Path) -> tuple[dict | None, dict]:
    """Read the current singleton through SQLite's read-only connection mode."""
    try:
        # Store uses WAL, so immutable mode could miss a concurrent committed update.
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2) as database:
            database.execute("PRAGMA query_only=ON")
            row = database.execute(
                "SELECT firm_id,config_json,initial_mandate FROM firm WHERE singleton=1"
            ).fetchone()
        if row is None:
            return None, _result(
                "database",
                "failure",
                "The database has no initialized firm.",
                action="Restore the firm database or use a new directory for initialization.",
            )
        raw_config = json.loads(row[1])
        if (
            not isinstance(row[0], str)
            or not row[0]
            or len(row[0]) > 64
            or not isinstance(row[2], str)
            or not row[2].strip()
            or not isinstance(raw_config, dict)
        ):
            raise ValueError
        return {
            "firm_id": row[0],
            "config": raw_config,
            "initial_mandate": row[2],
        }, _result("database", "good", "The initialized firm database is readable.")
    except (OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return None, _result(
            "database",
            "failure",
            "The initialized firm database is not readable in the expected format.",
            action="Restore the database from a matching Firm export or revision.",
        )


def _runtime_table_names() -> set[str]:
    """Read the schema declarations without constructing a Store or opening writes."""
    from . import (
        accounts,
        analysis_history,
        appointments,
        github,
        hosts,
        levels,
        mac_lease,
        messaging,
        reminders,
        requests,
        scheduling,
        store,
        summaries,
        telemetry,
    )

    modules = (
        accounts,
        analysis_history,
        appointments,
        github,
        hosts,
        levels,
        mac_lease,
        messaging,
        reminders,
        requests,
        scheduling,
        store,
        summaries,
        telemetry,
    )
    return {name for module in modules for name in _TABLE_DECLARATION.findall(module.SCHEMA)}


def database_initialization_state(path: Path) -> str:
    """Read-only classification for first-init recovery and initialized firms.

    A partial state is safe to resume only when no firm or user data was committed.
    Store creates one discovery cursor before lifecycle.initialize writes the firm.
    """
    try:
        validate_firm_directories(Path(path).parent.parent)
    except (OSError, ValueError):
        return "invalid"
    try:
        details = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "invalid"
    if not stat.S_ISREG(details.st_mode):
        return "invalid"
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2) as database:
            database.execute("PRAGMA query_only=ON")
            tables = [
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            quick_check = database.execute("PRAGMA quick_check").fetchone()
            expected_tables = _runtime_table_names()
            if quick_check != ("ok",) or not set(tables) <= expected_tables:
                return "invalid"
            if "firm" in tables:
                initialized = database.execute("SELECT COUNT(*) FROM firm").fetchone()[0]
                if initialized:
                    if not expected_tables <= set(tables):
                        return "invalid"
                    saved, _check = _read_database(path)
                    if saved is None:
                        return "invalid"
                    founder = database.execute(
                        "SELECT 1 FROM roles WHERE employee_id='founder'"
                    ).fetchone()
                    mandate = database.execute(
                        "SELECT 1 FROM mandates WHERE employee_id='founder' AND revision=0"
                    ).fetchone()
                    return (
                        "initialized" if founder is not None and mandate is not None else "invalid"
                    )
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                count = database.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                if count == 0:
                    continue
                if table == "runtime_secrets":
                    try:
                        keys = [
                            row[0] for row in database.execute("SELECT name FROM runtime_secrets")
                        ]
                    except sqlite3.Error:
                        return "invalid"
                    if keys == ["discovery_cursor"]:
                        continue
                if table == "employee_sequence":
                    try:
                        sequence = database.execute(
                            "SELECT singleton,next_number FROM employee_sequence"
                        ).fetchall()
                    except sqlite3.Error:
                        return "invalid"
                    if sequence == [(1, 1)]:
                        continue
                return "invalid"
        return "partial"
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
        return "invalid"


def _check_ports(ports: tuple[tuple[str, int], ...]) -> list[dict]:
    results = []
    reservations = []
    try:
        for name, port in ports:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                results.append(
                    _result(
                        name,
                        "failure",
                        f"Port {port!r} is outside the valid range 1-65535.",
                        action=f"Choose a valid --{name.replace('_', '-')} value.",
                    )
                )
                continue
            reservation = None
            try:
                reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                reservation.bind(("127.0.0.1", port))
                reservation.listen(1)
            except OSError:
                if reservation is not None:
                    reservation.close()
                results.append(
                    _result(
                        name,
                        "failure",
                        f"Loopback port {port} is unavailable.",
                        action=(
                            "Stop the process using this port or choose a different "
                            f"--{name.replace('_', '-')} value."
                        ),
                    )
                )
            else:
                reservations.append(reservation)
                results.append(_result(name, "good", f"Loopback port {port} is available to bind."))
    finally:
        for reservation in reservations:
            reservation.close()
    return results


def _check_host_platform(*, system=None, machine=None, mac_version=None) -> dict:
    """Check the supported host and Lima VZ baseline without changing the host."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    mac_version = platform.mac_ver()[0] if mac_version is None else mac_version
    if system != "Darwin":
        return _result(
            "host_platform",
            "failure",
            f"Firm VM hosting requires macOS on Apple Silicon; this host reports "
            f"{system or 'unknown'}.",
            action="Run Firm on an Apple Silicon Mac.",
        )
    if machine.lower() not in {"arm64", "aarch64"}:
        return _result(
            "host_platform",
            "failure",
            f"Firm VM hosting requires Apple Silicon; this host reports {machine or 'unknown'}.",
            action="Use an Apple Silicon Mac and a native arm64 Python installation.",
        )
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", mac_version)
    if match is None:
        return _result(
            "host_platform",
            "failure",
            "The macOS version could not be identified.",
            action="Update macOS and rerun firm check.",
        )
    version = tuple(int(part or 0) for part in match.groups())
    if version < (13, 0, 0):
        return _result(
            "host_platform",
            "failure",
            f"macOS {mac_version} is older than the Lima Virtualization.framework baseline (13.0).",
            action="Update to macOS 13 or newer and rerun firm check.",
        )
    return _result(
        "host_platform",
        "good",
        f"Supported Apple Silicon host: macOS {mac_version}, {machine}.",
    )


def _check_lima() -> dict:
    limactl = shutil.which("limactl")
    if limactl is None or not os.access(limactl, os.X_OK):
        return _result(
            "lima",
            "failure",
            "The limactl command is unavailable.",
            action="Install Lima with `brew install lima` and rerun firm check.",
        )
    try:
        completed = subprocess.run(
            [limactl, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    output = "" if completed is None else (completed.stdout or "") + (completed.stderr or "")
    match = re.search(r"\bversion\s+(\d+)\.(\d+)\.(\d+)", output)
    if completed is None or completed.returncode != 0 or match is None:
        return _result(
            "lima",
            "failure",
            "The Lima version could not be read from `limactl --version`.",
            action=(
                "Install a supported Lima release with `brew upgrade lima` and rerun firm check."
            ),
        )
    version = tuple(int(part) for part in match.groups())
    if version < MIN_LIMA_VERSION:
        minimum = ".".join(map(str, MIN_LIMA_VERSION))
        return _result(
            "lima",
            "failure",
            f"Lima {'.'.join(map(str, version))} is older than Firm's tested baseline {minimum}.",
            action="Upgrade Lima with `brew upgrade lima` and rerun firm check.",
        )
    return _result(
        "lima", "good", f"Lima {'.'.join(map(str, version))} meets Firm's tested baseline."
    )


def _check_linux_base(directory: Path) -> dict:
    # Only a firm without a Linux VM yet is created from the base image.
    if (directory / "durable" / "linux" / "vm.json").exists():
        return _result("linux_base", "skipped", "This firm's Linux VM already exists.")
    age = linux_base.image_age_days()
    if age is None:
        return _result(
            "linux_base",
            "good",
            "No Linux base image yet; creating this firm's VM builds one (about 5 minutes).",
        )
    if age > linux_base.MAX_AGE_DAYS:
        return _result(
            "linux_base",
            "warning",
            f"The Linux base image is {age:.0f} days old; this firm's VM will start from it.",
            action=(
                f"To start from current updates instead, delete {linux_base.cache_directory()} "
                "before starting the firm; it is rebuilt then (about 5 minutes)."
            ),
        )
    return _result("linux_base", "good", f"The Linux base image is {age:.0f} days old.")


def _host_memory_gib() -> float | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        if page_size > 0 and pages > 0:
            return page_size * pages / (1024**3)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _check_vm_resources(directory: Path, memory_gib: int, disk_gib: int) -> list[dict]:
    results = []
    total_memory = _host_memory_gib()
    if total_memory is None:
        results.append(
            _result(
                "host_memory",
                "warning",
                "Physical memory could not be measured.",
                action=("Confirm the Mac has at least the VM allocation plus 4 GiB for macOS."),
            )
        )
    else:
        required = memory_gib + HOST_MEMORY_RESERVE_GIB
        if total_memory < required:
            results.append(
                _result(
                    "host_memory",
                    "failure",
                    f"The VM requests {memory_gib} GiB; this host has "
                    f"{total_memory:.1f} GiB RAM and needs at least {required} GiB "
                    "for the VM plus macOS.",
                    action=(
                        "Use a Mac with more memory or choose a smaller VM allocation "
                        "in sandbox.toml."
                    ),
                )
            )
        else:
            results.append(
                _result(
                    "host_memory",
                    "good",
                    f"{total_memory:.1f} GiB RAM covers the {memory_gib} GiB VM "
                    "allocation plus 4 GiB for macOS.",
                )
            )
    try:
        free = shutil.disk_usage(directory).free / (1024**3)
    except OSError:
        results.append(
            _result(
                "host_disk",
                "failure",
                "Free disk space could not be measured.",
                action="Choose an accessible firm data directory on a disk with enough free space.",
            )
        )
    else:
        if free < disk_gib:
            results.append(
                _result(
                    "host_disk",
                    "failure",
                    f"The Linux VM has a {disk_gib} GiB virtual disk limit; this "
                    f"volume has {free:.1f} GiB free.",
                    action=(
                        f"Free at least {disk_gib} GiB on this volume or choose a "
                        "smaller VM disk allocation."
                    ),
                )
            )
        else:
            results.append(
                _result(
                    "host_disk",
                    "good",
                    f"{free:.1f} GiB is free for the Linux VM (configured virtual "
                    f"disk limit: {disk_gib} GiB).",
                )
            )
    return results


def _check_credentials(config: dict | None) -> list[dict]:
    if config is None:
        return [
            _result(
                "credentials",
                "skipped",
                "Provider credentials cannot be checked until configuration is valid.",
            )
        ]
    accounts = config.get("accounts")
    if accounts is None:
        return [
            _result(
                "credentials",
                "skipped",
                "No provider accounts are configured; controlled execution needs none.",
            )
        ]

    profiles_by_account: dict[str, dict] = {}
    for profile in config["profiles"].values():
        if profile.get("provider") is None:
            continue
        for account_id in profile["accounts"]:
            profiles_by_account.setdefault(account_id, profile)

    results = []
    for account_id, account in accounts.items():
        profile = profiles_by_account.get(account_id)
        name = f"credential:{account_id}"
        if profile is None:
            results.append(
                _result(
                    name,
                    "skipped",
                    "The account is not used by any execution profile.",
                )
            )
            continue
        try:
            provider_adapter(profile, account).validate_credentials()
        except ProviderSetupError as exc:
            results.append(
                {
                    **_result(
                        name,
                        "failure",
                        f"Local {account['provider']} credentials failed validation ({exc.code}).",
                        action=(
                            f"Repair the configured auth_file for account {account_id} and rerun "
                            "firm check."
                        ),
                    ),
                    "code": exc.code,
                }
            )
        else:
            warning = setup_token_warning(account_id, account)
            results.append(
                _result(
                    name,
                    "warning",
                    "Local claude credentials are readable but long-lived.",
                    action=warning,
                )
                if warning
                else _result(
                    name,
                    "good",
                    f"Local {account['provider']} credentials are readable and well formed.",
                )
            )
    return results


def check_setup(
    directory: Path,
    *,
    mcp_port: int = 8765,
    console_port: int = 8766,
    check_ports: bool = True,
) -> dict:
    """Return a safe local setup report without constructing Store, Runtime, or Sandbox."""
    directory = directory.absolute()
    checks = [_check_directory(directory)]

    database_path = directory / "durable" / "firm.sqlite3"
    database_state = database_initialization_state(database_path)
    initialized = database_state == "initialized"
    saved = None
    if initialized:
        saved, database_check = _read_database(database_path)
        checks.append(database_check)
    elif database_state == "missing":
        checks.append(
            _result(
                "database",
                "skipped",
                "No firm database exists yet; checking files for initialization.",
            )
        )
    elif database_state == "partial":
        checks.append(
            _result(
                "database",
                "skipped",
                "Firm initialization was interrupted before its first durable record; `firm init` "
                "can resume it.",
            )
        )
    else:
        checks.append(
            _result(
                "database",
                "failure",
                "durable/firm.sqlite3 or its durable parent is not a safe owner-controlled path.",
                action=("Use a real owner-controlled durable directory and regular firm database."),
            )
        )

    mandate = None
    mandate_path = directory / "mandate.md"
    try:
        mandate = read_text_file(mandate_path)
        if not mandate.strip():
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        checks.append(
            _result(
                "mandate",
                "failure",
                "mandate.md must be a readable, non-empty UTF-8 regular file up to 1 MiB.",
                action="Write a bounded regular mandate.md file in the firm directory.",
            )
        )
    else:
        checks.append(_result("mandate", "good", "mandate.md is readable and non-empty."))

    local_config = None
    config_path = directory / "firm.toml"
    try:
        local_config = validate_config(tomllib.loads(read_text_file(config_path)))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
        checks.append(
            _result(
                "firm_config",
                "failure",
                "firm.toml must be readable, valid UTF-8 TOML in a regular file up to 1 MiB.",
                action="Write a bounded regular firm.toml using documented configuration fields.",
            )
        )
    else:
        checks.append(_result("firm_config", "good", "firm.toml is valid."))

    active_config = local_config
    expected_firm_id = directory.resolve().name
    if saved is not None:
        expected_firm_id = saved["firm_id"]
        try:
            active_config = validate_config(saved["config"])
        except (ValueError, TypeError):
            active_config = None
            checks.append(
                _result(
                    "saved_configuration",
                    "failure",
                    "The database configuration is invalid for this Firm revision.",
                    action="Use the code revision that created this firm or restore it.",
                )
            )
        else:
            checks.append(
                _result(
                    "saved_configuration",
                    "good",
                    "The configuration used by firm start is valid.",
                )
            )
        if local_config is None:
            checks.append(
                _result(
                    "firm_config_consistency",
                    "skipped",
                    "The local firm.toml cannot be compared with initialized settings.",
                )
            )
        elif local_config != saved["config"]:
            checks.append(
                _result(
                    "firm_config_consistency",
                    "failure",
                    "firm.toml has unapplied changes; firm start will use saved settings.",
                    action="Apply intended edits with firm configure, or restore firm.toml.",
                )
            )
        else:
            checks.append(
                _result(
                    "firm_config_consistency",
                    "good",
                    "firm.toml matches the initialized settings.",
                )
            )
        if mandate is None:
            checks.append(
                _result(
                    "mandate_consistency",
                    "skipped",
                    "The local mandate cannot be compared with the initialized mandate.",
                )
            )
        elif mandate != saved["initial_mandate"]:
            checks.append(
                _result(
                    "mandate_consistency",
                    "failure",
                    "mandate.md differs from the mandate used to initialize the firm.",
                    action="Restore the initial mandate.md or use a new directory and firm ID.",
                )
            )
        else:
            checks.append(
                _result(
                    "mandate_consistency",
                    "good",
                    "mandate.md matches the initialized mandate.",
                )
            )

    sandbox_path = directory / "sandbox.toml"
    sandbox_settings = None
    try:
        sandbox_config = Config.load(sandbox_path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
        checks.append(
            _result(
                "sandbox_config",
                "failure",
                "sandbox.toml must be readable, valid UTF-8 TOML in a regular file up to 1 MiB.",
                action=(
                    "Write a bounded regular sandbox.toml using documented configuration fields."
                ),
            )
        )
    else:
        if sandbox_config.root != directory.resolve():
            checks.append(
                _result(
                    "sandbox_config",
                    "failure",
                    "sandbox.toml belongs to a different firm directory.",
                    action="Keep sandbox.toml inside the firm directory.",
                )
            )
        elif sandbox_config.firm_id != expected_firm_id:
            checks.append(
                _result(
                    "sandbox_config",
                    "failure",
                    "sandbox.toml firm.id does not match the expected firm ID.",
                    action=f"Set sandbox.toml [firm].id to {expected_firm_id!r}.",
                )
            )
        else:
            sandbox_settings = sandbox_config
            checks.append(
                _result(
                    "sandbox_config",
                    "good",
                    "sandbox.toml is valid and identifies this firm.",
                )
            )

    checks.append(_check_host_platform())
    checks.append(_check_lima())
    checks.append(_check_linux_base(directory))
    if sandbox_settings is not None:
        checks.extend(
            _check_vm_resources(directory, sandbox_settings.memory_gib, sandbox_settings.disk_gib)
        )
    else:
        checks.append(
            _result(
                "host_memory",
                "skipped",
                "VM resource needs cannot be checked until sandbox.toml is valid.",
            )
        )
        checks.append(
            _result(
                "host_disk",
                "skipped",
                "VM resource needs cannot be checked until sandbox.toml is valid.",
            )
        )

    checks.extend(_check_credentials(active_config))
    if check_ports:
        checks.extend(_check_ports((("mcp_port", mcp_port), ("console_port", console_port))))
    failures = sum(check["status"] == "failure" for check in checks)
    return {
        "directory": str(directory),
        "initialized": initialized and saved is not None,
        "ok": failures == 0,
        "failure_count": failures,
        "checks": checks,
        "credential_scope": "local format and readability only; remote validity is not tested",
    }
