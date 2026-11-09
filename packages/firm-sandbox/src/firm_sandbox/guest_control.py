"""What both guest VMs share on the host: owner-only records, lifecycle locks and the
one-request control channel to a root-owned guest controller."""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import stat
import subprocess
import tempfile
import time
from functools import wraps
from pathlib import Path

from .host_network import Boundary
from .locking import lifecycle_locked

MAX_FILE = 8 * 1024 * 1024
MAX_REPLY = 24 * 1024 * 1024
# The in-flight re-check exists for one event: the filter detaching part-way through a
# call. That is rare and not time-critical to the second, so five seconds bounds the guest's
# exposure well enough while costing the single-threaded control socket almost nothing.
BOUNDARY_INTERVAL = 5.0
# A probe that cannot be answered is a transport fact, not a verdict: the control socket is
# single-threaded and several roles share it. Tolerate silence this long — long enough to
# outlast a busy accept queue, short enough that a filter which really is gone stops the
# call well inside the guest's own timeout — then fail closed.
BOUNDARY_GRACE = 15.0
logger = logging.getLogger(__name__)


class ControlConnectionError(RuntimeError):
    """A guest request may have committed before its response was lost."""


def lifecycle(*, exclusive=False, timeout=None):
    """Coordinate the full operation, including nested public API calls.

    The holder names the firm's lock directory as ``firm_locks``; the Linux VM, the
    Mac VM, its station and the sandbox all take the one firm-wide lock there. With
    ``timeout`` the call is refused (``LockBusy``) instead of waiting longer for it.
    """

    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            private_directory(self.firm_locks)
            bound = {} if timeout is None else {"timeout": timeout}
            with lifecycle_locked(self.firm_locks, exclusive=exclusive, **bound):
                return method(self, *args, **kwargs)

        return call

    return decorate


def private_directory(path: Path):
    if path.is_symlink():
        raise ValueError("VM state directories must not be symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"VM state must be an owner-only directory: {path}")


def read_record(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(f"Unsafe VM record: {path}")
    if info.st_size > 1024 * 1024:
        raise ValueError("VM record too large")
    return json.loads(path.read_text())


def save(path: Path, value):
    temporary = path.with_suffix(".new")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def control_request(vm, controller_argv, operation, *, timeout=120, **payload):
    """One JSON request on the guest controller's stdin, one ``ok``/``error`` reply on stdout.

    ``vm`` supplies the packet filter (``network``) and the command environment
    (``commands.env``); ``controller_argv`` launches the guest controller, independently of
    any role command in ``payload["argv"]``.
    """
    # Nothing is in flight yet, so one retry is the whole cost of surviving a contended
    # control socket; past that the call never starts, which is the cheap, safe failure.
    boundary = vm.network.boundary()
    if boundary is Boundary.UNREACHABLE:
        time.sleep(0.25)
        boundary = vm.network.boundary()
    if boundary is not Boundary.ENFORCED:
        raise RuntimeError("VM network boundary is unavailable; stop the VM")
    message = json.dumps({"operation": operation, **payload}).encode()
    if len(message) > 2 * MAX_FILE:
        raise ValueError("Guest request exceeds its size limit")
    with tempfile.TemporaryFile() as input_file:
        input_file.write(message)
        input_file.seek(0)
        process = subprocess.Popen(
            controller_argv,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=vm.commands.env,
            start_new_session=True,
        )
        output = bytearray()
        diagnostics = bytearray()
        streams = [process.stdout, process.stderr]
        deadline = time.monotonic() + timeout
        next_check = time.monotonic() + BOUNDARY_INTERVAL
        silent_since = None
        try:
            while streams:
                now = time.monotonic()
                if now >= next_check:
                    # Aborting here destroys work the guest has already done, so only a
                    # boundary that answers, and answers no, ends the call at once. Silence
                    # is tolerated until BOUNDARY_GRACE has passed, then fails closed too.
                    boundary = vm.network.boundary()
                    if boundary is Boundary.UNENFORCED:
                        raise ControlConnectionError("VM network boundary is unavailable")
                    if boundary is Boundary.UNREACHABLE:
                        if silent_since is None:
                            silent_since = now
                        if now - silent_since >= BOUNDARY_GRACE:
                            raise ControlConnectionError(
                                "VM network boundary stopped answering; treating it as lost"
                            )
                    else:
                        silent_since = None
                    next_check = now + BOUNDARY_INTERVAL
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Guest controller reply deadline exceeded")
                wait = max(0.0, min(next_check - time.monotonic(), remaining))
                for stream in select.select(streams, [], [], wait)[0]:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        streams.remove(stream)
                    elif stream is process.stderr:
                        diagnostics.extend(chunk)
                        del diagnostics[:-16384]
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_REPLY:
                            raise ValueError("Guest response exceeded its size limit")
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("Guest controller reply deadline exceeded") from exc
            if code:
                detail = diagnostics.decode(errors="replace").strip()
                raise ControlConnectionError(
                    "Guest control connection failed" + (f": {detail}" if detail else "")
                )
        finally:
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    try:
        reply = json.loads(output)
    except ValueError as exc:
        # What the guest wrote beside its reply is the only account of why there was none.
        detail = diagnostics.decode(errors="replace").strip()[-600:]
        raise ControlConnectionError(
            "Invalid guest controller reply" + (f": {detail}" if detail else "")
        ) from exc
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Guest operation failed"))
    if diagnostics:
        logger.warning(
            "Guest controller diagnostics: %s", diagnostics.decode(errors="replace").strip()
        )
    return reply["result"]


class GuestControl:
    """The trusted guest controller call both VM drivers share.

    A driver names its Lima instance and the guest command that runs its guest
    controller; everything else about reaching the guest controller is identical.
    """

    guest_instance: str
    guest_controller_command: tuple[str, ...]

    def _rpc(self, operation, *, timeout=120, **payload):
        # Like the driver's own Lima commands, this one acts on whatever the runtime holds.
        self.paths.verify_runtime()
        # An absolute sudo, on macOS and Ubuntu alike: the login shell Lima runs the
        # command in never chooses which sudo receives the controller call.
        argv = [
            self.lima, "shell", "--tty=false", "--workdir=/", self.guest_instance,
            "/usr/bin/sudo", "-n",
            *self.guest_controller_command,
        ]
        return control_request(self, argv, operation, timeout=timeout, **payload)

    @lifecycle()
    def rpc(self, operation, **payload):
        """Trusted host calls into the guest controller, under the firm lifecycle lock."""
        return self._rpc(operation, **payload)

    @lifecycle()
    def storage(self, role_id=None):
        """Live attribution within a shared 60-second guest budget."""
        payload = {"role_id": role_id} if role_id is not None else {}
        return self._rpc("storage", timeout=90, **payload)

    @lifecycle()
    def dispose(self, role_id):
        """Explicitly discard a stopped role's scratch and scoped Docker state."""
        return self._rpc("dispose", role_id=role_id)

    @lifecycle()
    def info(self):
        """Read the VM identity and its guest's state; lifecycle state belongs to Sandbox."""
        state = self._state()
        return {**state, "guest": self._rpc("info") if self._running() and state["ready"] else None}
