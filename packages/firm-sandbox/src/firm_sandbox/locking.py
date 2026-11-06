"""Host locks coordinate processes; they are not guest security boundaries."""

import fcntl
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

LIFECYCLE = "lifecycle"
_held = ContextVar("lifecycle_locks", default=())


class LockBusy(RuntimeError):
    """The lock stayed held by another operation for longer than the caller would wait."""


@contextmanager
def locked(directory: Path, name: str, *, shared: bool = False, timeout: float | None = None):
    """Hold a host lock; with ``timeout``, raise ``LockBusy`` rather than wait longer."""
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / name).open("a") as handle:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if timeout is None:
            fcntl.flock(handle, mode)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockBusy(
                            "The firm is busy with a lifecycle operation; try again"
                        ) from None
                    time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def lifecycle_locked(locks: Path, *, exclusive: bool = False, timeout: float | None = None):
    """Hold the firm-wide lifecycle lock in ``locks``, the firm's lock directory.

    Every lifecycle operation of the firm, on either VM or on the whole firm, takes
    this one lock. A call nested in an operation that already holds it proceeds
    without locking again; a shared holder cannot upgrade to exclusive. With
    ``timeout`` the wait is bounded and ``LockBusy`` raised past it.
    """
    path = Path(locks).absolute() / LIFECYCLE
    held = _held.get()
    for other, writer in held:
        if other == path:
            if exclusive and not writer:
                raise RuntimeError("Cannot upgrade a lifecycle lock")
            yield
            return
    bound = {} if timeout is None else {"timeout": timeout}
    with locked(path.parent, LIFECYCLE, shared=not exclusive, **bound):
        token = _held.set((*held, (path, exclusive)))
        try:
            yield
        finally:
            _held.reset(token)
