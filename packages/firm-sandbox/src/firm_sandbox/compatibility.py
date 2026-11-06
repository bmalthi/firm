"""Release compatibility shared by guest images and the runtime package."""

import json
import os
import re
import stat
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

COMPATIBILITY_GENERATION = 1
FIRM_RECORD = "firm-release.json"
_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?\Z")


def release_identifier() -> str:
    try:
        return version("firm-sandbox")
    except PackageNotFoundError as exc:
        raise RuntimeError("firm-sandbox distribution metadata is unavailable") from exc


def image_identity() -> dict:
    """Compatibility marker and immutable identity for a newly built image artifact."""
    return {
        "created_with_release": release_identifier(),
        "compatibility_generation": COMPATIBILITY_GENERATION,
        "image_id": uuid4().hex,
    }


def validate_image_manifest(manifest: dict) -> dict:
    """Reject unmarked, malformed, or generation-mismatched images before cloning."""
    release = manifest.get("created_with_release") if isinstance(manifest, dict) else None
    actual = manifest.get("compatibility_generation") if isinstance(manifest, dict) else None
    image_id = manifest.get("image_id") if isinstance(manifest, dict) else None
    valid = (
        isinstance(release, str)
        and bool(release.strip())
        and type(actual) is int
        and actual > 0
        and isinstance(image_id, str)
        and len(image_id) == 32
    )
    if not valid:
        raise ValueError(
            f"Mac image has no valid compatibility marker (recorded release: "
            f"{release or 'missing'}; actual generation: {actual!r}; expected generation: "
            f"{COMPATIBILITY_GENERATION}). Refusing to clone it. Rebuild and seal a fresh "
            "image with this Firm release."
        )
    if actual != COMPATIBILITY_GENERATION:
        raise ValueError(
            f"Mac image was created with release {release} and compatibility generation "
            f"{actual}; this runtime requires generation {COMPATIBILITY_GENERATION}. "
            "Refusing to clone it. Rebuild and seal a fresh image."
        )
    return manifest


def validate_retained_vm(record: dict) -> dict:
    """Check persisted guest provenance before changing, booting, or installing into it."""
    if not isinstance(record, dict):
        raise ValueError("Retained VM compatibility record is malformed; refusing changes")
    release = record.get("created_with_release")
    actual = record.get("compatibility_generation")
    image_id = record.get("image_id")
    if (
        not isinstance(release, str)
        or not release.strip()
        or type(actual) is not int
        or actual <= 0
        or not isinstance(image_id, str)
        or not image_id.strip()
    ):
        raise ValueError(
            f"Retained VM has no valid compatibility/image record (recorded release: "
            f"{release or 'missing'}; actual generation: {actual!r}; expected generation: "
            f"{COMPATIBILITY_GENERATION}); refusing changes. Keep using its original Firm "
            "runtime, or export and create a fresh firm and image."
        )
    if actual != COMPATIBILITY_GENERATION:
        raise ValueError(
            f"Retained VM was created with release {release} and compatibility generation "
            f"{actual}; this runtime requires generation {COMPATIBILITY_GENERATION}. "
            "Refusing guest changes. Export with the original runtime, then create a fresh "
            "firm and image."
        )
    return record


def validate_firm_directories(directory: Path) -> bool:
    """Require the firm root and any existing durable parent to be real owner dirs.

    Return whether ``durable/`` exists. This runs before inspecting database contents so
    a durable symlink cannot redirect marker creation or validation to another tree.
    """
    directory = Path(directory)
    durable = directory / "durable"

    def require_owner_directory(path: Path, label: str, *, missing_ok: bool) -> bool:
        try:
            details = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return False
            raise ValueError(f"{label} must be a real, owner-controlled directory") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o022
        ):
            raise ValueError(f"{label} must be a real, owner-controlled directory")
        return True

    require_owner_directory(directory, "Firm directory", missing_ok=False)
    return require_owner_directory(durable, "Firm durable directory", missing_ok=True)


def initialize_firm_record(directory: Path, runtime_release: str) -> dict:
    """Create a new firm marker without replacing an existing or malformed marker."""
    directory = Path(directory)
    validate_firm_directories(directory)
    path = directory / FIRM_RECORD
    if not isinstance(runtime_release, str) or not _RELEASE.fullmatch(runtime_release):
        raise ValueError("Firm runtime release identifier is malformed")
    database = directory / "durable" / "firm.sqlite3"
    if os.path.lexists(database):
        raise ValueError(
            "Firm database already exists; this release does not stamp or migrate existing firms"
        )
    if path.is_symlink() or path.exists():
        record = validate_firm_record(directory)
        return record
    durable = directory / "durable"
    if durable.exists() and any(durable.iterdir()):
        raise ValueError(
            "Unmarked durable state already exists; this release does not stamp or migrate "
            "existing firms. Use its original runtime to stop and export it."
        )
    record = {
        "record_version": 1,
        "created_with_release": runtime_release,
        "compatibility_generation": COMPATIBILITY_GENERATION,
    }
    data = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def validate_firm_record(directory: Path, *, require_existing: bool = True) -> dict | None:
    """Read-only firm compatibility check shared by runtime and sandbox CLI."""
    directory = Path(directory)
    validate_firm_directories(directory)
    path = directory / FIRM_RECORD
    try:
        info = path.lstat()
    except FileNotFoundError:
        if require_existing:
            raise ValueError(
                f"Firm has no release compatibility record (recorded release: missing; "
                f"actual generation: missing; expected generation: "
                f"{COMPATIBILITY_GENERATION}). Refusing database or guest changes. This "
                "release does not migrate unmarked firms; stop and export with the original "
                "runtime, then create a fresh firm and rebuild its image."
            ) from None
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_size > 4096
    ):
        raise ValueError(
            f"Firm release compatibility record is malformed (expected generation: "
            f"{COMPATIBILITY_GENERATION}; actual: unsafe record); refusing changes. "
            "Export with the original runtime, then create a fresh firm and rebuild its image."
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        release = record["created_with_release"]
        actual = record["compatibility_generation"]
        if (
            type(record) is not dict
            or type(record.get("record_version")) is not int
            or record["record_version"] != 1
            or not isinstance(release, str)
            or not _RELEASE.fullmatch(release)
            or type(actual) is not int
            or actual <= 0
        ):
            raise ValueError("invalid fields")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ValueError(
            f"Firm release compatibility record is malformed (expected generation: "
            f"{COMPATIBILITY_GENERATION}; actual: malformed; recorded release: unavailable). "
            "Refusing database or guest changes. "
            "Stop and export with the original runtime, then create a fresh firm and rebuild "
            "its image."
        ) from None
    if actual != COMPATIBILITY_GENERATION:
        relation = "older" if actual < COMPATIBILITY_GENERATION else "newer"
        raise ValueError(
            f"Firm was created with release {release} and compatibility generation {actual}; "
            f"this runtime requires generation {COMPATIBILITY_GENERATION} (record is "
            f"{relation}). Refusing database or guest changes. Stop and export with the "
            "original runtime, then create a fresh firm and rebuild its image."
        )
    return record
