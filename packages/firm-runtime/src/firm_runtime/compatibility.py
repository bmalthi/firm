"""Runtime release identity and public firm compatibility guard."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from firm_sandbox.compatibility import (
    COMPATIBILITY_GENERATION,
    FIRM_RECORD,
    initialize_firm_record,
    validate_firm_record,
)

__all__ = [
    "COMPATIBILITY_GENERATION",
    "FIRM_RECORD",
    "initialize_firm",
    "release_identifier",
    "validate_firm",
]


def release_identifier() -> str:
    try:
        return version("firm-runtime")
    except PackageNotFoundError as exc:
        raise RuntimeError("firm-runtime distribution metadata is unavailable") from exc


def initialize_firm(directory: Path) -> dict:
    """Create the owner record on a new firm during explicit initialization."""
    return initialize_firm_record(directory, release_identifier())


def validate_firm(directory: Path, *, require_existing: bool = True) -> dict | None:
    """Validate release identity using read-only I/O, before opening Store or Sandbox."""
    return validate_firm_record(directory, require_existing=require_existing)
