"""A firm's export: what is worth keeping from a run once its VMs are gone.

An export directory, ``<firm>/export/`` unless the owner names another, holds:

- ``firm.sqlite3``: a consistent backup of the runtime database;
- ``events.jsonl``, ``inputs/`` and ``mac/stations/<station>/leases/``: the firm's host records;
- ``drive/``: the shared trees from the Linux VM;
- ``homes/<role>/``: with ``homes``, each role's private ``work/`` and provider sessions;
- ``manifest.json``: the firm ID, export time, totals, and every file's
  size and SHA-256, so the export can be verified later.

An export is written to a staging sibling, verified against its manifest, and swapped
into place in one rename, replacing any previous export whole.
"""

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from firm_sandbox import Config, Sandbox
from firm_sandbox.compatibility import FIRM_RECORD, validate_firm_record
from firm_sandbox.locking import locked

from .errors import FirmError

EXPORT = "export"
MANIFEST = "manifest.json"
DATABASE = "firm.sqlite3"
HOST_RECORDS = ("inputs", "mac/stations/*/leases")  # under the firm's durable directory
RENAME_SWAP = 0x2  # renamex_np: exchange two existing paths atomically


def write(directory: Path, firm_id: str, *, destination=None, homes=False, boot=False) -> dict:
    """Export the firm at ``directory``; ``boot`` lets a stopped Linux VM boot for the drive.

    The caller holds the execution host lock when it passes ``boot``, so no host
    starts the firm while its Linux VM runs for the copy.
    """
    config = Config.load(directory / "sandbox.toml")
    if config.root != directory.resolve():
        raise FirmError("sandbox configuration belongs to a different firm directory")
    release_record = validate_firm_record(config.root)
    destination = Path(destination or directory / EXPORT).absolute()
    if destination.is_symlink() or (
        os.path.lexists(destination) and not (destination / MANIFEST).is_file()
    ):
        raise FirmError(f"{destination} exists and is not a firm export; refusing to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Each attempt owns only its own staging directory. A fixed sibling name could
    # delete unrelated owner data or an export another process is still writing.
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.new-", dir=destination.parent))
    try:
        try:
            refused = Sandbox(config).export(staging, homes=homes, boot=boot)
        except RuntimeError as exc:
            raise FirmError(str(exc)) from exc
        _copy_host_records(config, staging)
        entries = _entries(staging)
        totals = {}
        for path, entry in entries.items():
            section = totals.setdefault(path.split("/")[0], {"files": 0, "bytes": 0})
            section["files"] += 1
            section["bytes"] += entry.get("bytes", 0)
        manifest = {
            "firm_id": firm_id,
            "created_with_release": release_record["created_with_release"],
            "compatibility_generation": release_record["compatibility_generation"],
            "exported_at": datetime.now(UTC).isoformat(),
            "homes": homes,
            "drive_missing": not os.path.lexists(staging / "drive"),
            "refused": refused,
            "files": sum(section["files"] for section in totals.values()),
            "bytes": sum(section["bytes"] for section in totals.values()),
            "totals": totals,
            "entries": entries,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        verify(staging, expected_firm_id=firm_id)
        _swap(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return summary(destination, manifest)


def summary(directory: Path, manifest: dict) -> dict:
    """What the owner sees of an export: its manifest without the per-file entries."""
    return {"directory": str(directory)} | {
        key: value for key, value in manifest.items() if key != "entries"
    }


def verify(directory: Path, *, expected_firm_id: str | None = None) -> dict:
    """Check an export against its manifest and its database's integrity; return the manifest."""
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
        recorded = manifest["entries"]
        if (
            not isinstance(recorded, dict)
            or not all(isinstance(path, str) for path in recorded)
            or not isinstance(manifest["firm_id"], str)
            or type(manifest["homes"]) is not bool
            # Exports from before this field always held the drive.
            or type(manifest.setdefault("drive_missing", False)) is not bool
            or not isinstance(manifest.get("created_with_release"), str)
            or type(manifest.get("compatibility_generation")) is not int
            or manifest["compatibility_generation"] <= 0
        ):
            raise ValueError("invalid manifest fields")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FirmError(f"{directory} has no readable export manifest") from exc
    actual = _entries(directory)
    if manifest["drive_missing"] != (not os.path.lexists(directory / "drive")):
        raise FirmError("export drive presence does not match its manifest")
    for path in sorted(set(recorded) | set(actual)):
        if recorded.get(path) != actual.get(path):
            raise FirmError(f"export does not match its manifest at {path}")
    try:
        db = sqlite3.connect((directory / DATABASE).absolute().as_uri() + "?mode=ro", uri=True)
        try:
            check = db.execute("PRAGMA quick_check").fetchone()[0]
            firm = db.execute("SELECT firm_id FROM firm WHERE singleton=1").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise FirmError(f"exported database is unreadable: {exc}") from exc
    if check != "ok":
        raise FirmError(f"exported database failed its integrity check: {check}")
    if firm is None or firm[0] != manifest["firm_id"]:
        raise FirmError("exported database does not match the manifest's firm id")
    try:
        release_record = json.loads((directory / FIRM_RECORD).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FirmError("export does not contain a readable firm release record") from exc
    if (
        not isinstance(release_record, dict)
        or release_record.get("created_with_release") != manifest["created_with_release"]
        or release_record.get("compatibility_generation") != manifest["compatibility_generation"]
    ):
        raise FirmError("export release identity does not match its manifest")
    if expected_firm_id is not None and firm[0] != expected_firm_id:
        raise FirmError("export belongs to a different firm")
    return manifest


def _copy_host_records(config: Config, staging: Path) -> None:
    durable = config.durable
    shutil.copy2(config.root / FIRM_RECORD, staging / FIRM_RECORD)
    # The backup API copies one consistent snapshot, even while the firm runs.
    source = sqlite3.connect((durable / DATABASE).as_uri() + "?mode=ro", uri=True, timeout=10)
    target = sqlite3.connect(staging / DATABASE)
    try:
        source.backup(target)
        # One self-contained file that read-only analysis opens without a WAL.
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()
    (staging / DATABASE).chmod(0o600)
    with locked(config.locks, "events", shared=True):
        if (durable / "events.jsonl").is_file():
            shutil.copy2(durable / "events.jsonl", staging / "events.jsonl")
    for pattern in HOST_RECORDS:
        for source in sorted(durable.glob(pattern)):
            if source.is_dir():
                shutil.copytree(source, staging / source.relative_to(durable), symlinks=True)


def _entries(root: Path) -> dict:
    """Every file and link under ``root`` but the manifest, by POSIX relative path."""
    entries = {}
    for current, directories, names in os.walk(root):
        for name in directories + names:
            path = Path(current, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[relative] = {"link": os.readlink(path)}
            elif relative != MANIFEST and path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                entries[relative] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    return entries


def _swap(staging: Path, destination: Path) -> None:
    """Put the verified export in place in one rename; a previous export is then removed."""
    if os.path.lexists(destination):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renamex_np(os.fsencode(staging), os.fsencode(destination), RENAME_SWAP):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        shutil.rmtree(staging)
    else:
        os.rename(staging, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
