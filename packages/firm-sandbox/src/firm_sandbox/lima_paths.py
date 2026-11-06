"""Real short Lima runtime directories projecting firm-owned persistent files.

Lima 2.2 resolves LIMA_HOME symlinks (pkg/limatype/dirnames/dirnames.go).
A directory alias therefore cannot solve Darwin's 104-byte socket-path limit.
Keep the runtime directories real and link their individual durable entries,
using the existing host_paths ownership and sparse-file publication helpers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

from .commands import VMStateError
from .host_paths import (
    TEMP_PARENT,
    _link,
    _move_durably,
    _owned_directory,
    _pid,
    _read_json,
    _transient_instance_entry,
    remove_directory,
)

logger = logging.getLogger(__name__)

RUNTIME_PARENT = TEMP_PARENT / "lima"


class LimaPaths:
    def __init__(self, root, *, instance):
        if instance not in ("linux", "mac"):
            raise ValueError("Unknown VM instance")
        self.name = instance
        self.root = Path(root).resolve()
        self.durable_home = self.root / "lima"
        self.durable_instance = self.durable_home / self.name
        longest_socket = max(
            len(os.fsencode(self.durable_instance / "ssh.sock.1234567890123456")),
            len(os.fsencode(self.root / "lima/_networks/user-v2/user-v2_ep.sock")),
            len(os.fsencode(self.root / "0123456789abcdef.sock.1234567890123456")),
        )
        self.projected = longest_socket >= 104
        binding = os.fsencode(self.root) + b"\0" + self.name.encode()
        digest = hashlib.sha256(binding).hexdigest()[:20]
        self.running_record = self.root / f"{self.name}-host-running.json"
        self.runtime = RUNTIME_PARENT / digest if self.projected else self.root
        self._runtime_error = None
        if self.projected:
            # A running VM keeps its runtime fixed at the directory it booted from:
            # this is recomputed on every invocation, so moving RUNTIME_PARENT would
            # otherwise strand it. Construction must never raise for a bad record —
            # one VM's corrupt state must not block another VM's emergency shutdown —
            # so an untrustworthy record is kept here and refused by verify_runtime().
            try:
                recorded = self._recorded_runtime(digest)
            except (OSError, RuntimeError) as exc:
                self._runtime_error = str(exc)
            else:
                if recorded is not None:
                    self.runtime = recorded
        self.home = self.runtime / "lima"
        self.instance = self.home / self.name
        self.ssh_config = self.instance / "ssh.config"

    @property
    def owner_binding(self):
        return {"root": str(self.root), "instance": self.name}

    def _recorded_runtime(self, digest):
        """The still-present runtime directory this instance's running record names.

        Absent the record or its directory, the caller falls back to the freshly
        computed path. The path's shape — the right digest under RUNTIME_PARENT — is
        checked before it is ever touched, then the directory itself; anything else
        raises.
        """
        if not self.running_record.exists():
            return None
        runtime = _read_json(self.running_record).get("runtime")
        if not isinstance(runtime, str):
            raise RuntimeError("Invalid VM host process record")
        path = Path(runtime)
        if path.name != digest or path.parent != RUNTIME_PARENT:
            raise RuntimeError(f"VM host running record names an untrusted runtime: {path}")
        if not path.exists():
            return None
        self._verify_runtime_directory(path)
        return path

    def _verify_runtime_directory(self, path):
        """``path`` and its parent are ours alone, and ``path`` is not another firm's."""
        for directory, noun in ((path.parent, "runtime parent"), (path, "runtime")):
            self._validate_runtime_directory(directory, noun)
        owner = path / "owner.json"
        if owner.exists() or owner.is_symlink():
            if _read_json(owner) != self.owner_binding:
                raise RuntimeError(f"Short VM runtime is owned by another firm: {path}")
        elif any(path.iterdir()):
            raise RuntimeError(f"Unowned files occupy the short VM runtime: {path}")

    @staticmethod
    def _validate_runtime_directory(path, noun):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError(f"Short VM {noun} is unsafe: {path}")

    def verify_runtime(self):
        """Refuse a runtime that may not be this VM's before a Lima command is sent into it.

        prepare() starts here, and so does everything that acts through the runtime:
        the drivers' Lima commands, the guest controller call, the filter's control
        socket, the SSH tunnels and the station's helper relay. None of them builds
        anything, and none may reach whatever VM a rejected or foreign directory holds.
        """
        if not self.projected:
            return
        if self._runtime_error is not None:
            raise RuntimeError(self._runtime_error)
        try:
            if self.runtime.exists() or self.runtime.is_symlink():
                self._verify_runtime_directory(self.runtime)
        except OSError as exc:
            # A directory that cannot be read cannot be vouched for either.
            raise RuntimeError(f"Short VM runtime cannot be checked: {self.runtime}") from exc

    def ssh_port(self):
        """The host-side port of the instance's sshd, from Lima's generated ssh.config."""
        self.verify_runtime()
        for line in self.ssh_config.read_text().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "Port" and fields[1].isdigit():
                return int(fields[1])
        raise RuntimeError(f"No SSH port recorded in {self.ssh_config}")

    def _record(self, path, value):
        temporary = path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def has_live_processes(self):
        if not self.running_record.exists():
            return False
        record = _read_json(self.running_record)
        pids, runtime = record.get("pids"), record.get("runtime")
        if (
            not isinstance(pids, list)
            or len(pids) > 4
            or any(type(pid) is not int or pid <= 1 for pid in pids)
            or not isinstance(runtime, str)
        ):
            raise RuntimeError("Invalid VM host process record")
        for pid in pids:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0 and os.fsencode(runtime) in result.stdout:
                return True
            if result.returncode not in (0, 1):
                raise RuntimeError("Unable to verify VM host process identity")
        return False

    def verify_stopped(self, running):
        """Accept Broken metadata only with independent host shutdown evidence."""
        try:
            if running():
                raise RuntimeError("VM shutdown was not confirmed")
            return
        except VMStateError as exc:
            if exc.status != "Broken":
                raise
        if self.has_live_processes():
            raise RuntimeError("VM host process is still running")
        # A failed launch may never publish our running record. Check Lima's
        # PID files and the host process table as well, including short roots.
        result = subprocess.run(
            ["/bin/ps", "-ax", "-o", "pid=,command="],
            capture_output=True, check=True, timeout=10,
        )
        if not result.stdout.strip():
            raise RuntimeError("Unable to verify VM host process inventory")
        pids = {
            pid for name in ("ha.pid", "vz.pid")
            if (pid := _pid(self.instance / name)) is not None
        }
        paths = {os.fsencode(self.instance), os.fsencode(self.durable_instance)}
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                raise RuntimeError("Unable to verify VM host process inventory")
            if int(fields[0]) in pids or any(path in fields[1] for path in paths):
                raise RuntimeError("VM host process is still running")
        # Accepted without a caller's mark_stopped(): a Broken VM is exactly the
        # failure whose logs matter most, so archive them here too.
        self.preserve_logs()

    def prepare(self):
        if not self.projected:
            return
        self.verify_runtime()
        _owned_directory(self.root, private=True)
        _owned_directory(self.durable_home, private=True)
        # /private/tmp is world-writable and RUNTIME_PARENT a fixed name beneath it, so
        # the shared parent is validated before anything is created inside it.
        _owned_directory(RUNTIME_PARENT.parent, private=True)
        _owned_directory(RUNTIME_PARENT, private=True)
        if not self.runtime.exists() and self.has_live_processes():
            raise RuntimeError(
                "Short VM runtime is missing while its host process is still running"
            )
        _owned_directory(self.runtime, private=True)
        # verify_runtime() refused an existing runtime that was another firm's or unowned.
        owner = self.runtime / "owner.json"
        if not owner.exists():
            self._record(owner, self.owner_binding)
        _owned_directory(self.home, private=True)
        for name in ("_config", "_disks"):
            target = self.durable_home / name
            _owned_directory(target, private=True)
            _link(self.home / name, target)
        self.synchronize()

    def adopt_stopped_instance(self):
        """Publish a newly created stopped Lima instance into durable state."""
        if not self.projected:
            return
        if self.durable_instance.exists() or self.durable_instance.is_symlink():
            raise RuntimeError("Durable VM instance already exists")
        if self.has_live_processes():
            raise RuntimeError("Cannot adopt a running VM instance")
        if not self.instance.exists():
            raise RuntimeError("New VM instance is missing")
        _owned_directory(self.instance, private=True)
        for entry in self.instance.iterdir():
            self._validate_persistent(entry)
            if entry.name.endswith((".pid", ".sock")):
                raise RuntimeError("Cannot adopt VM instance with process or socket evidence")
        _move_durably(self.instance, self.durable_instance)
        self.synchronize()

    def synchronize(self):
        if not self.projected or not self.durable_instance.exists():
            return
        _owned_directory(self.durable_instance, private=True)
        _owned_directory(self.instance, private=True)
        for entry in tuple(self.instance.iterdir()):
            target = self.durable_instance / entry.name
            if entry.is_symlink():
                if os.readlink(entry) != str(target):
                    raise RuntimeError("Unexpected link in the short VM runtime")
                continue
            if _transient_instance_entry(entry.name):
                continue
            self._validate_persistent(entry)
            if target.exists() or target.is_symlink():
                self._validate_persistent(target)
            # Lima replaces some regular metadata files atomically. Bring that
            # new file back into the firm, then restore its runtime projection.
            _move_durably(entry, target)
        for entry in self.durable_instance.iterdir():
            if _transient_instance_entry(entry.name):
                continue
            self._validate_persistent(entry)
            _link(self.instance / entry.name, entry)

    @staticmethod
    def _validate_persistent(path):
        info = path.lstat()
        if (
            not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise RuntimeError("Unsafe persistent VM state entry")

    def mark_running(self):
        if not self.projected:
            return
        pids = sorted(
            {
                pid
                for name in ("ha.pid", "vz.pid")
                if (pid := _pid(self.instance / name)) is not None
            }
        )
        if not pids:
            raise RuntimeError("VM started without host process evidence")
        self._record(self.running_record, {"runtime": str(self.runtime), "pids": pids})
        if not self.has_live_processes():
            raise RuntimeError("VM host process identity could not be verified")

    def mark_stopped(self):
        if self.projected:
            if self.instance.exists():
                # First, and it never raises: a synchronize that refuses an entry must
                # not also cost the evidence of why the VM stopped.
                self.preserve_logs()
                self.synchronize()
            self.running_record.unlink(missing_ok=True)

    def preserve_logs(self):
        """Copy the runtime instance's *.log files into durable state after a confirmed stop.

        /private/tmp can be cleared by a reboot, and _transient_instance_entry keeps
        *.log out of the durable projection, so this copy is a stopped VM's only
        surviving record of why it died. Called by every confirmed-stop path, not only
        mark_stopped: a failed boot's or a wrongly attached VM's recovery stop, and the
        Broken instance verify_stopped accepts, matter most, so they archive here too.
        Idempotent: an unchanged copy is left in place, and only a genuinely new set of
        logs displaces the previous one. Copied into a staging directory first and
        published — the old copy rotated to ``logs.previous`` — only once every file
        has copied, so a failed copy never costs the last successful archive. Never
        raised: nothing here may block a stop or an emergency shutdown.
        """
        if not self.durable_instance.exists():
            return
        try:
            logs = [
                entry
                for entry in self.instance.iterdir()
                if entry.name.endswith(".log") and entry.is_file() and not entry.is_symlink()
            ]
            if not logs:
                return
            destination = self.durable_instance / "logs"
            if destination.exists() and self._logs_match(destination, logs):
                return
            staging = self.durable_instance / "logs.staging"
            if staging.exists():
                remove_directory(self.durable_instance, "logs.staging")
            _owned_directory(staging, private=True)
            for entry in logs:
                shutil.copy2(entry, staging / entry.name)
            # Rotate only when there is a published copy to rotate. After a publish that
            # failed halfway, logs.previous is the only archive left and must survive.
            if destination.exists():
                if (self.durable_instance / "logs.previous").exists():
                    remove_directory(self.durable_instance, "logs.previous")
                destination.rename(self.durable_instance / "logs.previous")
            staging.rename(destination)
        except (OSError, RuntimeError) as exc:
            logger.warning("Could not preserve %s VM logs (%s)", self.name, type(exc).__name__)

    @staticmethod
    def _logs_match(destination, logs):
        existing = {entry.name: entry for entry in destination.iterdir()}
        return {entry.name for entry in logs} == set(existing) and all(
            existing[entry.name].stat().st_size == entry.stat().st_size
            and existing[entry.name].stat().st_mtime == entry.stat().st_mtime
            for entry in logs
        )

    def remove_instance(self):
        if not self.projected:
            return
        if self.has_live_processes():
            raise RuntimeError("Refusing to remove live durable VM state")
        if self.instance.exists() or self.instance.is_symlink():
            raise RuntimeError("Stop and remove the runtime VM instance first")
        if self.durable_instance.exists():
            _owned_directory(self.durable_instance, private=True)
            remove_directory(self.durable_home, self.name)


def lima_paths(root, *, instance):
    """Pure path computation shared by the Linux VM, the Mac VM, the SSH tunnels and the station."""
    return LimaPaths(root, instance=instance)
