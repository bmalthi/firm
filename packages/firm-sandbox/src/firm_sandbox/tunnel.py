"""Owner-controlled SSH port forwards between the host and either guest.

The host owns the SSH key and control socket. Guests get a loopback address, not
authority to create tunnels or connect to arbitrary owner services. A tunnel is
bound to one VM generation; a replaced VM cannot inherit a stale forward.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
from pathlib import Path

from .guest_control import private_directory, read_record, save
from .locking import locked


class Tunnel:
    def __init__(
        self, *, name, host, forward, ssh_config, socket_root, root, locks, generation, commands,
        verify,
    ):
        """``root`` holds the tunnel's record; ``locks`` is the firm's lock directory.

        ``ssh_config`` and ``socket_root`` are in the VM's runtime; ``verify`` refuses a
        runtime that may be another firm's before any ssh command goes through it.
        """
        self.name, self.host, self.forward = name, host, tuple(forward)
        self.ssh_config, self.commands, self.generation = ssh_config, commands, generation
        self._verify = verify
        # OpenSSH adds a 17-byte temporary suffix before publishing its socket.
        # Keep lease IDs in the durable record, within Darwin's 104-byte limit here.
        socket_name = name if len(os.fsencode(name)) <= 16 else hashlib.sha256(
            name.encode()
        ).hexdigest()[:16]
        self.socket = Path(socket_root) / f"{socket_name}.sock"
        self.root = Path(root)
        self.locks = Path(locks)
        self.record = self.root / f"{name}.json"

    def _argv(self):
        # Every ssh command this tunnel runs is built here.
        self._verify()
        return [
            "/usr/bin/ssh",
            "-F",
            str(self.ssh_config),
            "-o",
            f"ControlPath={self.socket}",
            "-o",
            "ServerAliveInterval=5",
            # Lima's SSH forward can stall for twenty seconds under concurrent load
            # while the guest remains alive. Allow thirty seconds before dropping
            # every lease's drive; this still detects a dead guest within half a minute.
            "-o",
            "ServerAliveCountMax=6",
        ]

    def _control(self, operation):
        return (
            self.commands.run(
                [*self._argv(), "-O", operation, self.host], check=False, timeout=10
            ).returncode
            == 0
        )

    def _identity(self):
        return {"generation": self.generation(), "forward": list(self.forward)}

    def _matches(self):
        return self.record.exists() and read_record(self.record) == self._identity()

    def _remove_dead_socket(self):
        try:
            before = self.socket.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            raise RuntimeError(f"Unsafe {self.name} control socket")
        if not self.record.exists():
            raise RuntimeError(f"Unrecognized {self.name} control socket")
        read_record(self.record)  # Verify owner-controlled metadata before recovery.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(self.socket))
            except ConnectionRefusedError:
                pass
            except FileNotFoundError:
                return
            else:
                raise RuntimeError(f"{self.name} control socket has a live unrecognized listener")
        after = self.socket.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError(f"{self.name} control socket changed during recovery")
        self.socket.unlink()

    def start(self):
        private_directory(self.root)
        private_directory(self.locks)
        with locked(self.locks, self.name):
            if self._control("check"):
                if not self._matches():
                    raise RuntimeError(f"An existing {self.name} tunnel has a different binding")
                return
            self._remove_dead_socket()
            save(self.record, self._identity())
            self.commands.run(
                [
                    *self._argv(),
                    "-o",
                    "ControlMaster=yes",
                    "-o",
                    "ControlPersist=yes",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-fN",
                    *self.forward,
                    self.host,
                ],
                timeout=30,
            )
            self.require_ready()

    def require_ready(self):
        if not self._matches() or not self._control("check"):
            raise RuntimeError(f"{self.name} connection is unavailable; reconnect before use")

    def close(self):
        private_directory(self.locks)
        with locked(self.locks, self.name):
            if self._control("check"):
                if not self._matches():
                    raise RuntimeError(f"Refusing to close a differently bound {self.name} tunnel")
                if not self._control("exit"):
                    raise RuntimeError(f"{self.name} tunnel shutdown was not confirmed")
            else:
                self._remove_dead_socket()


def runtime_tunnel(vm, port: int) -> Tunnel:
    """Forward only the runtime's localhost service into the firm's Linux guest."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid runtime port")
    return Tunnel(
        name="runtime-mcp",
        host="lima-linux",
        forward=("-R", f"127.0.0.1:{port}:127.0.0.1:{port}"),
        ssh_config=vm.ssh_config,
        socket_root=vm.socket_root,
        root=vm.root,
        locks=vm.firm_locks,
        generation=lambda: vm._state()["generation"],
        commands=vm.commands,
        verify=vm.paths.verify_runtime,
    )
