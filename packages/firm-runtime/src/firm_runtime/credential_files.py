"""Private credential reads and bounded, atomic file coordination."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .provider_contract import ProviderSetupError

MAX_AUTH_BYTES = 1024 * 1024


def failure(code, *, transient=False):
    return ProviderSetupError(
        category="transient" if transient else "auth",
        code=code,
        message="Account credential renewal is temporarily unavailable"
        if transient
        else "Account credentials require renewal or login",
    )


def read_private_credential(path: Path, provider: str) -> bytes:
    """Read an owner-private authentication file without following links or blocking.

    It must be a regular file owned by this user, readable by no one else and at most
    1 MiB. ``provider`` names the file in the owner-facing error; parsing the contents
    belongs to the provider.
    """
    subject = f"configured {provider} authentication"

    def refused(code: str, message: str) -> ProviderSetupError:
        return ProviderSetupError(category="auth", code=code, message=f"{subject} {message}")

    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        raise refused("credential_unreadable", "file is not readable") from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise refused("credential_invalid", "must be a regular file")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise refused("credential_ownership", "has the wrong owner")
        if details.st_mode & 0o077:
            raise refused("credential_permissions", "is not private")
        if details.st_size > MAX_AUTH_BYTES:
            raise refused("credential_too_large", "exceeds 1 MiB")
        data = b""
        while len(data) <= MAX_AUTH_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_AUTH_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
    except OSError:
        raise refused("credential_unreadable", "file is not readable") from None
    finally:
        os.close(descriptor)
    if len(data) > MAX_AUTH_BYTES:
        raise refused("credential_too_large", "exceeds 1 MiB")
    return data


class CredentialFile:
    """Storage mechanics with a provider-validated read for conflict comparisons."""

    def __init__(self, path, reader):
        self.path = Path(path).expanduser()
        self.reader = reader

    def read(self):
        return self.reader(self.path)

    @contextmanager
    def locked(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.path) + ".lock", flags, 0o600)
        except OSError:
            raise failure("credential_lock_unavailable", transient=True) from None
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
            ):
                raise failure("credential_lock_invalid")
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise failure("credential_lock_timeout", transient=True) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(descriptor)

    def replace(self, expected, document):
        """Check the current source immediately before atomic replacement.

        Firm participants share the lock. Uncooperative external writers cannot
        be made atomic with this compare/read/replace protocol; changed sources
        detected here are reread, never overwritten with the stale candidate.
        """
        raw = json.dumps(document, separators=(",", ":")).encode()
        if len(raw) > MAX_AUTH_BYTES:
            raise failure("credential_refresh_invalid", transient=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".firm-auth-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if self.read() != expected:
                return False
            os.replace(temporary, self.path)
            return True
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def save(self, raw):
        """Save owner-supplied bytes under the same lock used for renewal."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.locked():
            descriptor, temporary = tempfile.mkstemp(prefix=".firm-auth-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
