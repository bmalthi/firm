"""Host filesystem checks and operations shared by the sandbox and runtime."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import stat
from pathlib import Path

# Everything Firm keeps in /private/tmp sits under this one directory: the short Lima
# runtimes and the Mac image build workspaces, both there only because Darwin's socket
# paths must be short. /private/tmp is world-writable and _owned_directory below checks
# only the leaf it is given, so each user validates this directory before creating
# anything inside it.
TEMP_PARENT = Path("/private/tmp") / f"firm-{os.getuid()}"
TEXT_FILE_LIMIT = 1024 * 1024


def read_text_file(path: Path, *, limit: int = TEXT_FILE_LIMIT) -> str:
    """Read a bounded UTF-8 regular file without blocking on special files."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path} must be a regular file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} must be valid UTF-8") from None


def _owned_directory(path: Path, *, private: bool = False) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise RuntimeError(f"Unsafe host state directory: {path}")


def _read_json(path: Path, *, limit: int = 4096) -> dict:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_size > limit
    ):
        raise RuntimeError(f"Unsafe host state record: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # An owner-owned record of mode 000 passes the checks above and cannot be read.
        raise RuntimeError(f"Invalid host state record: {path}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid host state record: {path}")
    return value


def _pid(path: Path) -> int | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32:
            return None
        value = int(path.read_text().strip())
        return value if value > 1 else None
    except (FileNotFoundError, UnicodeDecodeError, ValueError):
        return None


def _link(link: Path, target: Path) -> None:
    try:
        info = link.lstat()
    except FileNotFoundError:
        link.symlink_to(target, target_is_directory=True)
        return
    if stat.S_ISLNK(info.st_mode) and os.readlink(link) == str(target):
        return
    raise RuntimeError(f"{link} must be an exact link to this firm's durable VM state")


def _move_durably(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        source_fd = os.open(source, os.O_RDONLY)
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            copy = ctypes.CDLL(None, use_errno=True).fcopyfile
            if copy(source_fd, destination_fd, None, (1 << 3) | (1 << 27)):
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
            os.fsync(destination_fd)
        finally:
            os.close(source_fd)
            os.close(destination_fd)
        shutil.copystat(source, destination, follow_symlinks=False)
        source.unlink()
    else:
        shutil.copytree(source, destination, symlinks=True)
        shutil.rmtree(source)


def _transient_instance_entry(name: str) -> bool:
    return name in {
        "mnt", "sock", "ssh.config", "logs", "logs.previous", "logs.staging",
    } or name.endswith((".log", ".pid", ".sock"))


def remove_directory(parent: Path, name: str) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("This platform lacks symlink-safe directory deletion")
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        shutil.rmtree(name, dir_fd=fd)
    finally:
        os.close(fd)
