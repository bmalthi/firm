"""Host-owned packet boundary on Lima 2.2's private user-v2 BESS transport.

The VM receives the filter socket. Only the filter receives the upstream socket.
Losing this process closes the VM's link, rather than exposing an unfiltered route.
The same process runs the VM's egress proxy, the one destination the filter lets the
guest reach; if either fails, both stop.
"""

from __future__ import annotations

import array
import enum
import errno
import hashlib
import ipaddress
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .egress_proxy import EgressProxy, RequestLog, read_hosts, token
from .models import EGRESS_MODES, host_entries, identifier
from .network_observation import Traffic
from .packet_policy import MAX_FRAME, PacketPolicy


def hosts_file(root: Path) -> Path:
    """The owner's granted hosts for the VM whose directory is ``root``."""
    return root / "hosts.json"


def locations(root: Path, socket_root: Path) -> dict[str, Path]:
    """The filter's files for the VM whose directory is ``root`` (``VMSettings.root``)."""
    scratch = root / "scratch"
    network = socket_root / "lima" / "_networks" / "user-v2"
    return {
        "pid": network / "usernet_user-v2.pid",
        "fd": network / "user-v2_fd.sock",
        "ep": network / "user-v2_ep.sock",
        "upstream": socket_root / "net-up.sock",
        "control": socket_root / "net.sock",
        "scratch": scratch,
        "config": scratch / "network.json",
        "log": scratch / "network.log",
        # The owner's cutoff decision and grants must survive scratch removal; keep them
        # beside the VM record, with the proxy key that never enters the guest.
        "egress": root / "egress.json",
        "hosts": hosts_file(root),
        "key": root / "proxy.key",
        "traffic": scratch / "traffic.json",
        "requests": scratch / "egress.jsonl",
        "summary": scratch / "egress-summary.json",
    }


def proxy_port(root: Path) -> int:
    """The VM's egress proxy port on host loopback, stable per VM directory.

    Usernet forwards ``gateway:port`` to the host's ``127.0.0.1:port``, so every VM on
    the host needs its own port, as every Linux VM has its own subnet. A port another
    process holds fails this filter's start instead of sharing a listener.
    """
    return 20000 + int(hashlib.sha256(str(root).encode()).hexdigest()[:8], 16) % 20000


def policy_id(gateway: str, port: int, mode: str) -> str:
    """The filter's one fixed exception and the proxy's mode; a grant never changes it."""
    return hashlib.sha256(json.dumps([gateway, port, mode]).encode()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_egress(path: Path, default=True) -> bool:
    try:
        value = json.loads(path.read_text())["egress"]
    except FileNotFoundError:
        value = default
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid persisted egress policy: {path}") from exc
    if type(value) is not bool:
        raise ValueError(f"Invalid persisted egress policy: {path}")
    return value


def write_egress(path: Path, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ValueError("Egress control requires a boolean")
    _write_json(path, {"egress": enabled})


def write_hosts(path: Path, hosts) -> tuple[str, ...]:
    hosts = host_entries(hosts)
    _write_json(path, {"hosts": list(hosts)})
    return hosts


def read_proxy_key(path: Path) -> bytes:
    """The VM's proxy key as the running proxy holds it; never made here, so a key lost
    while the proxy runs cannot be replaced by one it would reject."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        raise RuntimeError(f"proxy key missing; stop and start the VM ({path})") from None
    if len(key) != 32:
        raise ValueError(f"Invalid proxy key: {path}")
    return key


def create_proxy_key(path: Path) -> bytes:
    """The VM's 32-byte proxy key, created once with mode 0600 and never sent to the guest.

    Only a filter's start makes it, before the proxy that will check it reads it.
    """
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".proxy-key-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)  # Create-if-absent: a concurrent creator's key wins.
            except FileExistsError:
                pass
        finally:
            Path(name).unlink(missing_ok=True)
    return read_proxy_key(path)


class Boundary(enum.Enum):
    """What a boundary probe established, keeping "no" apart from "I could not ask".

    The filter holds the only upstream socket, so an unreachable control socket
    never means an unfiltered guest: it means either a busy single-threaded
    ``serve()`` loop or a filter whose loss has already severed the guest's link.
    """

    ENFORCED = "enforced"
    UNENFORCED = "unenforced"
    UNREACHABLE = "unreachable"


def ask(path: Path, command: bytes, timeout: float = 1.0) -> dict:
    """One command on the control socket, answered within ``timeout`` seconds in all.

    Raises ``OSError`` (``TimeoutError`` past the bound) or ``ValueError`` instead of
    answering.
    """
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("The filter's control socket did not answer in time")
        return left

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining())
        connection.connect(str(path))
        connection.sendall(command)
        output = bytearray()
        while True:
            connection.settimeout(remaining())
            chunk = connection.recv(65536)
            if not chunk:
                break
            output.extend(chunk)
            # Bounded above the largest reply: the egress summary's 512 (role, host) rows.
            if len(output) > 512 * 1024:
                raise ValueError("Control reply exceeded its size limit")
        return json.loads(output)


def query(path: Path, command: bytes, timeout: float | None = None) -> dict | None:
    """``ask`` for callers that have no use for the difference between silence and a refusal."""
    try:
        return ask(path, command) if timeout is None else ask(path, command, timeout)
    except (OSError, ValueError):
        return None


class HostNetwork:
    """One VM's filter and proxy. ``proxy_role`` names the one role every proxied request
    belongs to (the Mac); without it each request names its role with credentials."""

    def __init__(
        self, settings, commands, *, socket_root: Path, subnet="192.168.5.0/24",
        verify=lambda: None, proxy_role=None,
    ):
        self.settings, self.commands = settings, commands
        network = ipaddress.ip_network(subnet, strict=True)
        if network.version != 4 or network.prefixlen != 24 or not network.is_private:
            raise ValueError("Host usernet subnet must be a private IPv4 /24")
        self.subnet = str(network)
        self.socket_root = socket_root
        self.paths = locations(settings.root, socket_root)
        self.gateway = str(network.network_address + 2)
        self.proxy_port = proxy_port(settings.root)
        self.proxy_role = None if proxy_role is None else identifier(proxy_role)
        self.policy = policy_id(self.gateway, self.proxy_port, settings.egress_mode)
        # The control socket lives in socket_root, the VM's runtime. ``verify`` raises
        # RuntimeError for a runtime that may be another firm's, whose filter would
        # otherwise answer here, before anything is sent to it or started in it.
        self._verify = verify

    def write_configuration(self) -> None:
        """``network.json``, what the next filter process enforces."""
        _write_json(self.paths["config"], {
            "scratch": str(self.paths["scratch"]),
            "socket_root": str(self.socket_root),
            "subnet": self.subnet,
            "proxy_port": self.proxy_port,
            "proxy_role": self.proxy_role,
            "mode": self.settings.egress_mode,
            "allow_hosts": list(self.settings.allow_hosts),
        })

    def create_key(self) -> None:
        """Make the proxy key if there is none. Only while no filter runs: ``start`` calls
        it before launching one."""
        if self.proxy_role is None:
            create_proxy_key(self.paths["key"])

    def proxy_url(self, identity: str) -> str:
        """``http://<identity>:<token>@<gateway>:<port>``, the Linux guest's proxy setting.

        The token is the only secret in it, and it names only ``identity``: the key it is
        derived from stays on the host.
        """
        if self.proxy_role is not None:
            raise RuntimeError("This VM's proxy takes no credentials")
        identifier(identity)
        secret = token(read_proxy_key(self.paths["key"]), identity)
        return f"http://{identity}:{secret}@{self.gateway}:{self.proxy_port}"

    def _ask(self, command: bytes) -> dict:
        self._verify()
        return ask(self.paths["control"], command)

    def _query(self, command: bytes) -> dict | None:
        self._verify()
        return query(self.paths["control"], command)

    def boundary(self, *, attached: bool = True) -> Boundary:
        """Whether the configured filter is enforcing, is not, or could not be asked."""
        try:
            record = self._ask(b"status")
        except (OSError, ValueError, RuntimeError):
            # A refused runtime's filter is not this VM's to ask.
            return Boundary.UNREACHABLE
        enforcing = bool(
            record
            and record.get("policy") == self.policy
            and (not attached or record.get("attached"))
        )
        return Boundary.ENFORCED if enforcing else Boundary.UNENFORCED

    def ready(self, *, attached: bool = True) -> bool:
        """The conservative reading: anything short of a confirmed filter is not ready."""
        return self.boundary(attached=attached) is Boundary.ENFORCED

    def publish_lima_pid(self) -> int:
        """Identify the real network process in Lima's temporary usernet marker.

        Lima may signal this PID during failed startup or shutdown. Publishing
        the runtime/launcher PID here would terminate the runtime itself.
        """
        target = self.paths["pid"]
        # Never leave an earlier runtime PID available to Lima's failure
        # cleanup if this network-status lookup fails.
        target.unlink(missing_ok=True)
        status = self._query(b"status")
        pid = status.get("pid") if status else None
        if (not status or status.get("policy") != self.policy
                or status.get("failed") or type(pid) is not int or pid <= 1
                or pid == os.getpid()):
            raise RuntimeError("Cannot identify the live host network process for Lima")
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".usernet-pid-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(pid) + "\n")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return pid

    def traffic(self) -> dict:
        result = self._query(b"traffic")
        if result is None:
            try:
                result = json.loads(self.paths["traffic"].read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeError("Network observation is unavailable") from exc
            return {**result, "live": False}
        return {**result, "live": True}

    def egress_summary(self) -> dict:
        """The proxy's requests by role and host; the last persisted one when it is down."""
        result = self._query(b"egress-summary")
        summary = result.get("egress_summary") if result else None
        if not isinstance(summary, dict):
            try:
                summary = json.loads(self.paths["summary"].read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeError("Egress summary is unavailable") from exc
            return {**summary, "live": False}
        return {**summary, "live": True}

    def reload_hosts(self, timeout: float = 1.0) -> dict | None:
        """Have the running proxy read ``hosts.json`` again, and confirm what it holds.

        ``None`` when no filter runs: its proxy reads the file when it starts. A filter
        that does not confirm within ``timeout`` seconds raises, like one that refuses.
        """
        self._verify()
        if not self.paths["control"].exists():
            return None
        expected = list(read_hosts(self.paths["hosts"]))
        result = query(self.paths["control"], b"hosts", timeout)
        if result is None or result.get("granted_hosts") != expected:
            raise RuntimeError("Allowed hosts change was not confirmed")
        return result

    def set_egress(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Egress control requires a boolean")
        result = self._query(b"egress-on" if enabled else b"egress-off")
        if result is None or result.get("egress") is not enabled:
            raise RuntimeError("Network egress change was not confirmed")
        return result

    def saved_egress(self) -> bool:
        return read_egress(self.paths["egress"])

    def prepare_egress(self, enabled: bool) -> dict:
        """Set the next boot's policy. Caller must hold the firm lifecycle lock.

        This does not claim to change a running filter; use set_egress and require its
        acknowledgement for a live VM.
        """
        write_egress(self.paths["egress"], enabled)
        return {"egress": enabled, "live": False}

    def start(self) -> None:
        if self.ready(attached=False):
            return
        if self._query(b"status"):
            raise RuntimeError("Stop the VM before changing its host network policy")
        self.create_key()
        self.paths["pid"].parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with self.paths["log"].open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "firm_sandbox.network_main",
                    str(self.paths["config"]),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=self.commands.env,
                start_new_session=True,
            )
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if self.ready(attached=False):
                return
            time.sleep(0.05)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise RuntimeError(f"Host network boundary did not start; inspect {self.paths['log']}")

    def stop(self) -> None:
        if self._query(b"stop") is None:
            return
        for _ in range(100):
            if not self.paths["control"].exists():
                return
            time.sleep(0.05)
        raise RuntimeError("Host network boundary did not stop")


def receive_fd(connection: socket.socket) -> socket.socket:
    connection.settimeout(2)
    _, controls, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(4 * 4))
    descriptors = []
    for level, kind, data in controls:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(data[: len(data) - len(data) % values.itemsize])
            descriptors.extend(values)
    if len(descriptors) != 1 or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        for descriptor in descriptors:
            os.close(descriptor)
        raise ValueError("Invalid BESS descriptor handshake")
    result = socket.socket(fileno=descriptors[0])
    if result.family != socket.AF_UNIX or result.type != socket.SOCK_DGRAM:
        result.close()
        raise ValueError("BESS endpoint must be a Unix datagram socket")
    result.setblocking(False)
    return result


def forward(
    guest: socket.socket,
    upstream: socket.socket,
    retained: socket.socket,
    policy: PacketPolicy,
    stop,
    traffic=None,
) -> None:
    # Keep the sent socket alive for the connection lifetime, as Lima's VZ driver
    # does; Darwin can otherwise sever a datagram pair during descriptor transfer.
    with guest, upstream, retained:
        while not stop.is_set():
            readable, _, _ = select.select([guest, upstream], [], [], 0.25)
            for source in readable:
                try:
                    frame, _, flags, _ = source.recvmsg(MAX_FRAME)
                except ConnectionResetError:
                    return  # Normal VM shutdown also closes this datagram link.
                if flags & socket.MSG_TRUNC:
                    continue
                if not frame:
                    return
                allowed = policy.outbound(frame) if source is guest else policy.inbound(frame)
                if source is guest and traffic is not None:
                    traffic.observe(frame, allowed)
                if allowed:
                    target = upstream if source is guest else guest
                    try:
                        target.send(frame)
                    except BlockingIOError:
                        pass  # Packet loss under backpressure is safer than unbounded queues.
                    except OSError as exc:
                        if exc.errno == errno.ENOBUFS:
                            continue
                        direction = "upstream" if source is guest else "guest"
                        raise RuntimeError(
                            f"Network send to {direction} failed ({len(frame)} bytes)"
                        ) from exc


def listener(path: Path) -> socket.socket:
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    path.chmod(0o600)
    # The single-threaded serve() loop accepts one connection per turn while every
    # in-flight guest call re-checks the boundary periodically. A short backlog makes
    # Darwin refuse those probes outright, so the queue must absorb the concurrency
    # rather than turning it into refusals the callers have to reason about.
    sock.listen(socket.SOMAXCONN)
    sock.settimeout(0.25)
    return sock


def serve(configuration: Path) -> None:
    if os.getuid() == 0:
        raise ValueError("Run the sandbox as an ordinary host user")
    settings = json.loads(configuration.read_text())
    subnet = settings["subnet"]
    network_range = ipaddress.ip_network(subnet, strict=True)
    if network_range.version != 4 or network_range.prefixlen != 24 or not network_range.is_private:
        raise ValueError("Invalid host usernet subnet")
    gateway = str(network_range.network_address + 2)
    port, mode, proxy_role = settings["proxy_port"], settings["mode"], settings["proxy_role"]
    if type(port) is not int or not 1 <= port <= 65535 or mode not in EGRESS_MODES:
        raise ValueError("Invalid egress proxy settings")
    if proxy_role is not None:
        identifier(proxy_role)
    policy_name = policy_id(gateway, port, mode)
    paths = locations(Path(settings["scratch"]).parent, Path(settings["socket_root"]))
    proxy = EgressProxy(
        port=port, mode=mode, log=RequestLog(paths["requests"]),
        allow_hosts=host_entries(settings["allow_hosts"]), role=proxy_role,
        key=None if proxy_role is not None else read_proxy_key(paths["key"]),
        hosts_path=paths["hosts"],
    )
    proxy.refresh()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    egress = read_egress(paths["egress"])
    backend = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "firm_sandbox.usernet_child",
            "--endpoint",
            str(paths["ep"]),
            "--listen",
            str(paths["upstream"]),
            "--subnet",
            subnet,
            "--mtu",
            "1500",
        ],
        stdin=subprocess.PIPE,
    )
    network = control = None
    worker = None
    policy = None
    traffic = Traffic(gateway=gateway)
    next_snapshot = time.monotonic() + 5

    def persist(path, snapshot):
        temporary = path.with_suffix(".new")
        try:
            temporary.write_text(json.dumps(snapshot))
            temporary.chmod(0o600)
            temporary.replace(path)
        except OSError:
            pass  # Observation failure must not break enforcement or shutdown.

    def persist_observations():
        persist(paths["traffic"], {**traffic.snapshot(), "egress": egress})
        persist(paths["summary"], proxy.summary.snapshot())

    try:
        deadline = time.monotonic() + 8
        while not paths["upstream"].exists():
            if backend.poll() is not None or time.monotonic() > deadline or stop.is_set():
                raise RuntimeError("Upstream usernet failed to start")
            time.sleep(0.05)
        # Bound before the guest can attach: a filter that reports ready has its proxy.
        proxy.start()
        network, control = listener(paths["fd"]), listener(paths["control"])
        while not stop.is_set() and backend.poll() is None:
            if not proxy.alive:
                raise RuntimeError("Egress proxy stopped")
            if time.monotonic() >= next_snapshot:
                persist_observations()
                next_snapshot = time.monotonic() + 5
            readable, _, _ = select.select([network, control], [], [], 0.25)
            for ready in readable:
                with ready.accept()[0] as connection:
                    if ready is control:
                        connection.settimeout(1)
                        command = connection.recv(16)
                        observation = {}
                        if command in {b"egress-on", b"egress-off"}:
                            enabled = command == b"egress-on"
                            write_egress(paths["egress"], enabled)
                            egress = enabled
                            if policy is not None:
                                policy.egress = egress
                            if not egress:
                                # The filter now drops their frames; end them outright too.
                                proxy.close_relays()
                        elif command == b"hosts":
                            # A nudge: the proxy re-reads the file on every change anyway.
                            proxy.refresh(force=True)
                            if proxy.hosts_error:
                                observation["hosts_error"] = proxy.hosts_error
                            observation["granted_hosts"] = list(proxy.granted)
                        elif command == b"egress-summary":
                            observation["egress_summary"] = proxy.summary.snapshot()
                            persist_observations()
                        elif command == b"traffic":
                            observation = traffic.snapshot()
                            persist_observations()
                        connection.sendall(
                            json.dumps(
                                {
                                    "policy": policy_name,
                                    "attached": bool(
                                        worker and worker.is_alive() and policy.address
                                    ),
                                    "failed": bool(worker and not worker.is_alive()),
                                    "pid": os.getpid(),
                                    "egress": egress,
                                    "mode": mode,
                                    **observation,
                                }
                            ).encode()
                        )
                        if command == b"stop":
                            stop.set()
                    else:
                        if worker:
                            raise RuntimeError("Refusing a second VM NIC")
                        guest = receive_fd(connection)
                        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                        for endpoint in (client, server):
                            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024**2)
                            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024**2)
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as transport:
                            transport.settimeout(2)
                            transport.connect(str(paths["upstream"]))
                            transport.sendmsg(
                                [b"\0"],
                                [
                                    (
                                        socket.SOL_SOCKET,
                                        socket.SCM_RIGHTS,
                                        array.array("i", [server.fileno()]),
                                    )
                                ],
                            )
                        client.setblocking(False)
                        policy = PacketPolicy(port, egress=egress, subnet=subnet)
                        worker = threading.Thread(
                            target=forward,
                            args=(guest, client, server, policy, stop, traffic),
                            daemon=True,
                        )
                        worker.start()
    finally:
        stop.set()
        for sock in (network, control):
            if sock:
                sock.close()
        if worker:
            worker.join(timeout=1)
        proxy.stop()
        persist_observations()
        backend.stdin.close()
        try:
            backend.wait(timeout=3)
        except subprocess.TimeoutExpired:
            backend.kill()
            backend.wait()
        for key in ("fd", "ep", "upstream", "control"):
            paths[key].unlink(missing_ok=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
