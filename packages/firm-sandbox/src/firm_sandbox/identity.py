"""The firm's runtime ID in its identity file, validated with owner-only host record checks."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path

# runtime_id identifies this firm's sandbox installation and is the only key.
REQUIRED_KEYS = {"runtime_id"}


def _read_json(path: Path, *, limit: int = 256 * 1024) -> dict:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_size > limit
    ):
        raise RuntimeError(f"Unsafe firm identity file: {path}")
    try:
        value = json.loads(path.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"Invalid firm identity file: {path}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid firm identity file: {path}")
    return value


def _validate(value: dict, path: Path) -> str:
    runtime_id = value.get("runtime_id")
    if (
        set(value) != REQUIRED_KEYS
        or not isinstance(runtime_id, str)
        or not re.fullmatch(r"[0-9a-f]{16}", runtime_id)
    ):
        raise RuntimeError(f"Invalid firm identity file: {path}")
    return runtime_id


def load_runtime_id(config) -> str:
    """Read and validate the runtime ID. Raises FileNotFoundError if none exists yet."""
    return _validate(_read_json(config.identity_file), config.identity_file)


def ensure_runtime_id(config) -> str:
    """Create the identity file with a fresh runtime ID if none exists yet."""
    config.durable.mkdir(mode=0o700, exist_ok=True)
    runtime_id = secrets.token_hex(8)
    payload = json.dumps({"runtime_id": runtime_id}, sort_keys=True, separators=(",", ":"))
    try:
        descriptor = os.open(config.identity_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_runtime_id(config)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(payload)
    return runtime_id
