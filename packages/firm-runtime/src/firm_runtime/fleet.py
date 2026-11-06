"""Firm fleet operations across a firms directory: status, list, export and removal."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from firm_sandbox import Config, Sandbox, host_paths
from firm_sandbox.locking import lifecycle_locked

from . import export as exports
from .errors import FirmError
from .runtime import Runtime


def sandbox_status(sandbox_config: Path) -> tuple[str, str | None]:
    """Never raises: an unreadable or absent sandbox configuration reports "unknown"."""
    try:
        return Sandbox(Config.load(sandbox_config)).state(), None
    except Exception as exc:  # noqa: BLE001 - status/list/gc must never fail on this
        return "unknown", str(exc)


@contextmanager
def runtime_owner_lock(directory: Path):
    """Exclusively hold the execution host lock so no host can start while held."""
    lock_path = directory / "locks" / "runtime-owner.lock"
    lock_path.parent.mkdir(mode=0o700, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def export(directory: Path, runtime: Runtime, *, destination=None, homes=False) -> dict:
    """Export a stopped or running firm; a stopped one boots only its Linux VM for the drive."""
    firm_id = runtime.status()["firm_id"]
    with runtime_owner_lock(directory) as acquired:
        # Without an execution host, holding its lock keeps one from starting while the
        # Linux VM runs for the copy; with one, its running Linux VM serves the copy.
        return exports.write(
            directory, firm_id, destination=destination, homes=homes, boot=acquired
        )


def _require_settled(runtime: Runtime, action: str) -> None:
    """Under the execution host lock: the firm is stopped with no admitted or running work."""
    status = runtime.status()
    if status["state"] != "stopped":
        raise FirmError(f"stop the firm before {action}")
    if status["active"]:
        raise FirmError(f"cannot {action} while activations are admitted or running")


def delete(directory: Path, confirm: str, runtime: Runtime) -> dict:
    """Remove a stopped, idle firm while holding both the execution-host and lifecycle locks."""
    firm_id = runtime.status()["firm_id"]
    if confirm != firm_id:
        raise FirmError("--confirm does not match this firm's id")
    with runtime_owner_lock(directory) as acquired:
        if not acquired:
            raise FirmError("an execution host owns this firm; refusing to delete")
        _require_settled(runtime, "deleting it")
        config = Config.load(directory / "sandbox.toml")
        with lifecycle_locked(config.locks, exclusive=True):
            state, error = sandbox_status(directory / "sandbox.toml")
            if state not in {"stopped", "discarded"}:
                detail = f": {error}" if error else ""
                raise FirmError(
                    "the firm's VMs must be stopped before deletion; "
                    f"sandbox state is {state}{detail}"
                )
            host_paths.remove_directory(directory.parent, directory.name)
    return {"directory": str(directory), "firm_id": firm_id, "status": "deleted"}


def discard_vms(directory: Path, confirm: str, runtime: Runtime, *, homes=True) -> dict:
    """Export a stopped, idle firm and, once the export verifies, discard its VMs for good.

    Role homes are exported by default, since they go with the disks. The firm directory
    stays: its runtime database, host records and ``export/``. A stopped firm is always
    exported afresh, so an earlier export never stands in for this one. A repeated call
    after an interrupted discard verifies the existing export and finishes, unless it
    lacks the homes this call asks for: the discard has already begun, so nothing can be
    exported again.
    """
    firm_id = runtime.status()["firm_id"]
    if confirm != firm_id:
        raise FirmError("--confirm does not match this firm's id")
    with runtime_owner_lock(directory) as acquired:
        if not acquired:
            raise FirmError("an execution host owns this firm; refusing to discard its VMs")
        _require_settled(runtime, "discarding its VMs")
        state, error = sandbox_status(directory / "sandbox.toml")
        if state == "discarded":
            location = directory / exports.EXPORT
            manifest = exports.verify(location, expected_firm_id=firm_id)
            if homes and not manifest["homes"]:
                raise FirmError(
                    "the VMs are already being discarded and the kept export has no role "
                    "homes; finish with --no-homes"
                )
            exported = exports.summary(location, manifest)
        elif state == "stopped":
            exported = exports.write(directory, firm_id, homes=homes, boot=True)
        else:
            detail = f": {error}" if error else ""
            raise FirmError(
                "the firm's VMs must be stopped before discarding them; "
                f"sandbox state is {state}{detail}"
            )
        try:
            Sandbox(Config.load(directory / "sandbox.toml")).discard()
        except RuntimeError as exc:
            raise FirmError(str(exc)) from exc
    return {
        "directory": str(directory),
        "firm_id": firm_id,
        "status": "discarded",
        "export": exported,
    }


def discover_firms(firms_directory: Path) -> list[Path]:
    if not firms_directory.is_dir():
        raise FirmError(f"{firms_directory} is not a directory")
    return sorted(
        (
            child
            for child in firms_directory.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / "durable" / "firm.json").is_file()
        ),
        key=lambda path: path.name,
    )


def firm_id(database: Path) -> str | None:
    if not database.is_file():
        return None
    try:
        db = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT firm_id FROM firm WHERE singleton=1").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def durable_size(durable: Path) -> int:
    """Sum file sizes under durable/, without following symlinks."""
    total = 0
    stack = [durable]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def newest_event_time(durable: Path) -> datetime | None:
    events_path = durable / "events.jsonl"
    if not events_path.is_file():
        return None
    newest = None
    for line in events_path.read_text().splitlines():
        if not line:
            continue
        try:
            when = datetime.fromisoformat(json.loads(line)["time"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if newest is None or when > newest:
            newest = when
    return newest


def list_firms(firms_directory: Path) -> list[dict]:
    entries = []
    for directory in discover_firms(firms_directory):
        durable = directory / "durable"
        state, _ = sandbox_status(directory / "sandbox.toml")
        newest = newest_event_time(durable)
        entries.append(
            {
                "directory": directory.name,
                "firm_id": firm_id(durable / "firm.sqlite3"),
                "sandbox_state": state,
                "durable_bytes": durable_size(durable),
                "newest_event_at": newest.isoformat() if newest is not None else None,
            }
        )
    return entries
