"""Filesystem ownership checks for firm state."""

import os
import stat
from pathlib import Path


def validate_firm_directories(directory: Path) -> bool:
    """Require the firm root and any existing durable parent to be real owner dirs.

    Return whether ``durable/`` exists. This runs before inspecting database contents so
    a durable symlink cannot redirect writes to another tree.
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
