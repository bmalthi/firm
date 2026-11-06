"""The guest's one way out: an HTTP proxy on the host that reaches allowed hosts by name.

``network_main`` runs it on a thread beside the packet filter, which lets the guest open
TCP to the usernet gateway at the proxy's port and nothing else. Lima's usernet NATs the
gateway address to the host's ``127.0.0.1`` (the same rewrite Lima applies to proxy
settings it propagates), so a guest connection to ``gateway:port`` arrives here, on host
loopback, where no other machine can reach it.

``CONNECT host:443`` is tunnelled without TLS termination and ``http://host/...`` is
forwarded to port 80; nothing else is served. The proxy resolves names itself, so the
guest needs no DNS and cannot front one name with another's address, and it refuses
literal addresses and names that resolve only to private, loopback, link-local or host
addresses. Every request is logged and summarized by role and host.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

from .models import EGRESS_MODES, host_entries, hostname, identifier
from .packet_policy import host_ipv4

LOGGER = logging.getLogger(__name__)

# Shipped with the sandbox and always allowed; a firm adds names under [network]
# allow_hosts and the owner grants more at run time. Sorted within each purpose.
BASELINE_HOSTS = tuple(sorted({
    # Model providers: Claude Code and Codex, whose web search runs provider-side.
    "*.anthropic.com", "*.claude.com", "*.openai.com", "chatgpt.com", "*.chatgpt.com",
    # GitHub: git over HTTPS, the API, release downloads and raw content.
    "github.com", "*.github.com", "*.githubusercontent.com",
    # Language registries and uv's installer bootstrap plus its redirect download host.
    "astral.sh", "files.pythonhosted.org", "pypi.org", "registry.npmjs.org",
    "releases.astral.sh",
    # Container images: Docker Hub (and get.docker.com, the image's Docker install) and
    # GitHub's registry.
    "*.docker.com", "*.docker.io", "ghcr.io",
    # The Linux image: Ubuntu's apt mirrors (ports.ubuntu.com on arm64) and Node.js.
    "*.debian.org", "*.ubuntu.com", "nodejs.org",
    # The Mac: Homebrew's formula API (bottles come from ghcr.io), and Apple's developer
    # downloads that mac-image-apply's simulator runtimes need.
    "*.apple.com", "*.cdn-apple.com", "formulae.brew.sh",
}))

# The Linux image's own boot provisioning (apt, Docker, uv, Node, npm). Not a role ID,
# so it can never be one role's name.
PROVISION = "_provision"
UNKNOWN = "unknown"
PORTS = {"https": 443, "http": 80}
HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 30
CONNECT_TIMEOUT = 15
# A relay with no bytes either way for this long is closed (record reason ``idle``).
IDLE_TIMEOUT = 300
# How often the proxy re-checks hosts.json without a request to prompt it.
HOSTS_CHECK = 1.0
MAX_CONNECTIONS = 512
LOG_LIMIT = 10 * 1024 * 1024
SUMMARY_KEYS = 512
# Connection-scoped headers a proxy consumes; the origin gets its own Host and Connection.
CONSUMED = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "host"}


def read_hosts(path: Path) -> tuple[str, ...]:
    """The owner's granted hosts in ``hosts.json``; none until the first grant."""
    try:
        value = json.loads(Path(path).read_text())["hosts"]
    except FileNotFoundError:
        return ()
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid granted hosts: {path}") from exc
    return host_entries(value)


def matches(host: str, entry: str) -> bool:
    """``*.suffix`` matches any depth of subdomain, never the bare suffix."""
    return host.endswith(entry[1:]) if entry.startswith("*.") else host == entry


def permitted(host: str, entries) -> bool:
    return any(matches(host, entry) for entry in entries)


def token(key: bytes, identity: str) -> str:
    """The proxy password of ``identity``: HMAC-SHA256 under the VM's proxy key."""
    return hmac.new(key, identity.encode(), hashlib.sha256).hexdigest()


def public_address(address: str, local_addresses=host_ipv4) -> bool:
    """A global IPv4 unicast address that is none of this host's own."""
    try:
        target = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return target.is_global and not target.is_multicast and target.packed not in local_addresses()


async def resolve(host: str, port: int) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(row[4][0] for row in rows))


def literal(host: str) -> bool:
    """Any spelling of an address rather than a name, including ``127.1``."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host.rsplit(".", 1)[-1].isdigit()


TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
TLS_RECORD = 16384 + 256  # the largest TLS record a peer may send, with its expansion
HELLO_LIMIT = 64 * 1024


def parse_head(head: bytes):
    """Request line and headers; a bare CR or LF anywhere is refused, never reinterpreted."""
    lines = head.decode("latin-1").split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):
        raise ValueError("Bare CR or LF in the request head")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ValueError("Malformed request line")
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not TOKEN.fullmatch(name):
            raise ValueError("Malformed header")
        headers.append((name, value.strip()))
    return parts[0], parts[1], parts[2], headers


def body_length(headers) -> int:
    """The one request body a plain-HTTP forward carries: ``Content-Length`` bytes, else none.

    Any ``Transfer-Encoding``, or more than one ``Content-Length``, could frame the body
    differently here and at the origin, so it is refused rather than interpreted.
    """
    if any(name.lower() == "transfer-encoding" for name, _ in headers):
        raise ValueError("Transfer-Encoding is not forwarded; send Content-Length")
    lengths = [value for name, value in headers if name.lower() == "content-length"]
    if len(lengths) > 1:
        raise ValueError("More than one Content-Length")
    if lengths and not re.fullmatch(r"[0-9]{1,15}", lengths[0]):
        raise ValueError("Invalid Content-Length")
    return int(lengths[0]) if lengths else 0


class NotTLS(ValueError):
    """The client's first bytes in a tunnel are not a TLS ClientHello."""


async def client_hello(reader) -> tuple[bytes, str | None]:
    """The records that carry the client's ClientHello, and the host name it asks for.

    A ClientHello may span records; at most ``HELLO_LIMIT`` bytes of it are read.
    """
    raw, handshake = b"", b""
    while True:
        header = await reader.readexactly(5)
        length = int.from_bytes(header[3:5], "big")
        if header[0] != 22 or header[1] != 3 or not 0 < length <= TLS_RECORD:
            raise NotTLS("Not a TLS handshake record")
        body = await reader.readexactly(length)
        raw, handshake = raw + header + body, handshake + body
        if len(handshake) >= 4:
            if handshake[0] != 1:
                raise NotTLS("The first handshake message is not a ClientHello")
            needed = 4 + int.from_bytes(handshake[1:4], "big")
            if needed > HELLO_LIMIT:
                raise NotTLS("ClientHello too large")
            if len(handshake) >= needed:
                return raw, server_name(handshake[4:needed])


class _Fields:
    """Strict reading of TLS structures: every length must fit inside what encloses it."""

    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def take(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise NotTLS("A ClientHello length overruns its structure")
        chunk, self.position = self.data[self.position:end], end
        return chunk

    def number(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def vector(self, size: int) -> _Fields:
        """A field prefixed by its length in ``size`` bytes."""
        return _Fields(self.take(self.number(size)))

    def done(self) -> bool:
        return self.position == len(self.data)

    def end(self, what: str) -> None:
        if not self.done():
            raise NotTLS(f"{what} does not match its declared length")


def server_name(hello: bytes) -> str | None:
    """The ``host_name`` in a ClientHello's server_name extension, lowercase, or ``None``.

    Any length that overruns its enclosing structure, or leaves bytes over inside it,
    raises ``NotTLS``: the proxy and the origin must never parse different names.
    """
    fields = _Fields(hello)
    fields.take(2 + 32)  # client_version, random
    fields.vector(1)  # session_id
    fields.vector(2)  # cipher_suites
    fields.vector(1)  # compression_methods
    if fields.done():
        return None  # no extensions
    extensions = fields.vector(2)
    fields.end("The ClientHello")
    found = None
    while not extensions.done():
        kind, data = extensions.number(2), extensions.vector(2)
        if kind != 0:
            continue
        if found is not None:
            raise NotTLS("More than one server_name extension")
        listing = data.vector(2)
        data.end("The server_name extension")
        if listing.done():
            raise NotTLS("An empty server_name list")
        while not listing.done():
            name_type, name = listing.number(1), listing.vector(2)
            if name_type != 0 or found is not None:
                raise NotTLS("server_name holds something other than one host name")
            try:
                found = name.take(len(name.data)).decode("ascii").lower().rstrip(".")
            except UnicodeDecodeError as exc:
                raise NotTLS("A server name that is not ASCII") from exc
            if not found:
                raise NotTLS("An empty server name")
    return found


def parse_target(method: str, target: str):
    """``(scheme, host, port, path)`` of a CONNECT authority or an absolute ``http://`` URL."""
    if method == "CONNECT":
        parts = urlsplit("//" + target)
        if parts.path or parts.query or parts.fragment or parts.username is not None:
            raise ValueError("CONNECT takes host:port")
        scheme, path = "https", None
    else:
        parts = urlsplit(target)
        if parts.scheme.lower() != "http" or not parts.netloc:
            raise ValueError("Send CONNECT for HTTPS, or an absolute http:// URL")
        if parts.username is not None:
            raise ValueError("Credentials in a request URL are not forwarded")
        scheme = "http"
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid port") from None
    if port is None:
        if method == "CONNECT":
            raise ValueError("CONNECT takes host:port")
        port = PORTS["http"]
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValueError("Missing host")
    return scheme, host, port, path


async def respond(writer, status: int, message: str, headers=()):
    body = (message + "\n").encode()
    lines = [
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers),
    ]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


async def pipe(reader, writer, counts, key, activity) -> bool:
    """Copy until end of stream; True for a clean end, False for a broken connection.

    ``activity[0]`` is the loop time of the last bytes either way, for the idle watch.
    """
    loop = asyncio.get_running_loop()
    try:
        while chunk := await reader.read(65536):
            activity[0] = loop.time()
            counts[key] += len(chunk)
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof() and not writer.is_closing():
            writer.write_eof()
        return True
    except OSError:
        return False


async def idle_watch(activity, idle) -> None:
    """Return once no bytes have moved either way for ``idle`` seconds."""
    loop = asyncio.get_running_loop()
    while (remaining := activity[0] + idle - loop.time()) > 0:
        await asyncio.sleep(remaining)


class RequestLog:
    """One JSON line per request; the previous file is kept, so both stay under ``limit``."""

    def __init__(self, path: Path, limit=LOG_LIMIT):
        self.path, self.limit = Path(path), limit
        self.previous = self.path.with_name(self.path.name + ".1")

    def write(self, record: dict) -> None:
        line = (json.dumps(record) + "\n").encode()
        try:
            if self.path.exists() and self.path.stat().st_size + len(line) > self.limit // 2:
                self.path.replace(self.previous)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(line)
        except OSError:
            pass  # Observation failure must not break or refuse a request.


class Summary:
    """Requests, denials and bytes by (role, host), bounded like the filter's traffic table."""

    def __init__(self, capacity=SUMMARY_KEYS):
        self.capacity = capacity
        self.rows = {}
        self.overflow = 0
        self.started = time.time()
        self.lock = threading.Lock()

    def request(self, role: str, host: str, allowed: bool, listed: bool) -> None:
        """One request; ``listed`` says whether enforce mode would admit its host, which
        observe mode's ``allowed`` does not."""
        with self.lock:
            row = self.rows.get((role, host))
            if row is None:
                if len(self.rows) >= self.capacity:
                    self.overflow += 1
                    return
                row = self.rows[(role, host)] = {
                    "role": role, "host": host, "listed": listed, "requests": 0,
                    "denied": 0, "unlisted": 0, "bytes_up": 0, "bytes_down": 0,
                    "last_seen": 0.0,
                }
            row["requests"] += 1
            row["denied"] += not allowed
            row["unlisted"] += not listed
            row["listed"] = listed
            row["last_seen"] = time.time()

    def transfer(self, role: str, host: str, up: int, down: int) -> None:
        with self.lock:
            row = self.rows.get((role, host))
            if row is not None:
                row["bytes_up"] += up
                row["bytes_down"] += down
                row["last_seen"] = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "observed_at": time.time(),
                "since": self.started,
                # Requests from (role, host) pairs past the capacity, counted but not shown.
                "overflow": self.overflow,
                "entries": [dict(row) for row in self.rows.values()],
            }


class EgressProxy:
    """The proxy of one VM. With ``key`` every request names its role with credentials
    (the Linux VM); with ``role`` every request is that role's (the Mac).

    ``mode`` ``observe`` allows every host and still logs and summarizes; port, address
    and credential checks hold in both modes. ``provisioning`` answers, per request,
    whether the ``PROVISION`` credential still holds. ``resolver``, ``acceptable`` and
    ``opener`` are the network seams tests replace.
    """

    def __init__(
        self, *, port, log: RequestLog, mode="enforce", allow_hosts=(), key=None, role=None,
        provisioning=lambda: False, hosts_path=None, address="127.0.0.1", resolver=resolve,
        acceptable=public_address, opener=asyncio.open_connection, idle=IDLE_TIMEOUT,
    ):
        if mode not in EGRESS_MODES:
            raise ValueError(f"Unknown egress mode: {mode}")
        if (key is None) == (role is None):
            raise ValueError("The proxy needs a key for role credentials, or one fixed role")
        self.address, self.port, self.mode = address, port, mode
        self.key, self.role, self.log, self.provisioning = key, role, log, provisioning
        self.configured = BASELINE_HOSTS + tuple(allow_hosts)
        self.granted = ()
        self.resolver, self.acceptable, self.opener = resolver, acceptable, opener
        self.idle = idle
        self.summary = Summary()
        self.connections = 0
        self.error = None
        self._loop = self._thread = None
        # Every open relay's task and its record, so a revocation can end it.
        self._relays = {}
        # hosts.json on disk is the authority for the granted hosts.
        self.hosts_path = None if hosts_path is None else Path(hosts_path)
        self._hosts_seen = object()  # the file's (mtime, size, inode) last read
        self.hosts_error = None
        self._hosts_lock = threading.Lock()

    def refresh(self, *, force=False) -> None:
        """Read ``hosts.json`` again if it changed since the last read (one stat).

        A file that cannot be read or validated keeps the previous list; ``hosts_error``
        says why, logged once until the file is good again. The file's version is
        remembered only once it has been read, so the next check retries a failed read.
        Requests and a once-a-second timer both come through here.
        """
        if self.hosts_path is None:
            return
        with self._hosts_lock:
            try:
                info = self.hosts_path.stat()
                seen = (info.st_mtime_ns, info.st_size, info.st_ino)
            except FileNotFoundError:
                seen = None
            except OSError as exc:
                seen = exc  # Never equal to a remembered version: retried next time.
            if seen == self._hosts_seen and not force:
                return
            try:
                hosts = read_hosts(self.hosts_path)
            except (OSError, ValueError) as exc:
                if str(exc) != self.hosts_error:
                    LOGGER.warning("Keeping the previous granted hosts: %s", exc)
                self.hosts_error = str(exc)
                return
            self._hosts_seen = seen
            self.hosts_error = None
            if hosts != self.granted:
                self.set_granted(hosts)

    def set_granted(self, hosts) -> None:
        """Replace the granted hosts; open relays to a host no longer allowed are closed."""
        self.granted = tuple(hosts)
        self._close_relays("revoked", lambda host: not self.allowed(host))

    def close_relays(self) -> None:
        """End every open relay: egress was cut off."""
        self._close_relays("egress_off", lambda host: True)

    def _close_relays(self, reason, condition):
        def close():
            for task, record in list(self._relays.items()):
                if condition(record["host"]):
                    record["reason"] = reason
                    task.cancel()

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(close)
            except RuntimeError:
                pass  # The loop closed meanwhile, and its relays with it.

    def listed(self, host: str) -> bool:
        """Whether the host is on the firm's list, as enforce mode decides it."""
        return permitted(host, self.configured + self.granted)

    def allowed(self, host: str) -> bool:
        return self.mode == "observe" or self.listed(host)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.error is None)

    def start(self) -> None:
        bound = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                server = loop.run_until_complete(asyncio.start_server(
                    self._serve, self.address, self.port, limit=HEAD_LIMIT,
                ))
            except BaseException as exc:
                self.error = exc
                loop.close()
                bound.set()
                return
            self.port = server.sockets[0].getsockname()[1]
            self._loop = loop
            # Idle tunnels make no requests: a timer keeps the grants they hold current.
            loop.create_task(self._watch_hosts())
            bound.set()
            try:
                loop.run_forever()
            except BaseException as exc:
                self.error = exc
            finally:
                server.close()
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.close()

        self._thread = threading.Thread(target=run, name="egress-proxy", daemon=True)
        self._thread.start()
        bound.wait()
        if self.error is not None:
            raise RuntimeError(
                f"Egress proxy cannot listen on {self.address}:{self.port}: {self.error}"
            ) from self.error

    async def _watch_hosts(self) -> None:
        """Re-check ``hosts.json`` every ``HOSTS_CHECK`` seconds, as each request does."""
        while True:
            await asyncio.sleep(HOSTS_CHECK)
            try:
                self.refresh()
            except Exception:  # A failed check must not end the checks that follow.
                LOGGER.exception("Granted hosts check failed")

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return  # The loop already closed.
        thread.join(timeout=5)

    # ---- one connection --------------------------------------------------------------

    async def _serve(self, reader, writer):
        try:
            if self.connections >= MAX_CONNECTIONS:
                await respond(writer, 503, "The egress proxy is at its connection limit")
                return
            self.connections += 1
            try:
                await self._exchange(reader, writer)
            finally:
                self.connections -= 1
        except OSError:
            pass  # The client went away; its record, if any, was already written.
        finally:
            writer.close()

    def _identity(self, headers):
        """The role named by valid credentials, or ``None``."""
        if self.role is not None:
            return self.role
        values = [value for name, value in headers if name.lower() == "proxy-authorization"]
        if len(values) != 1:
            return None
        scheme, _, encoded = values[0].partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, separator, secret = (
                base64.b64decode(encoded.strip(), validate=True).decode().partition(":")
            )
        except (binascii.Error, UnicodeDecodeError):
            return None
        if not separator:
            return None
        if user == PROVISION:
            # The image's provisioning credential travels in Lima's cidata, which a role
            # may read: it holds only until the new VM's guest controller is first ready.
            if not self.provisioning():
                return None
        else:
            try:
                identifier(user)
            except ValueError:
                return None
        return user if hmac.compare_digest(secret, token(self.key, user)) else None

    def _finish(self, record):
        self.summary.transfer(record["role"], record["host"], record["bytes_up"],
                              record["bytes_down"])
        self.log.write(record)

    def _refuse(self, record, reason):
        record["reason"] = reason
        self.summary.request(record["role"], record["host"], record["allowed"], record["listed"])
        self.log.write(record)

    async def _deny(self, writer, record, status, reason, message, headers=()):
        self._refuse(record, reason)
        await respond(writer, status, message, headers)

    async def _exchange(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            return  # No request: nothing to attribute or log.
        try:
            method, target, version, headers = parse_head(head)
            scheme, host, port, path = parse_target(method, target)
            length = 0 if method == "CONNECT" else body_length(headers)
        except ValueError as exc:
            await respond(writer, 400, str(exc))
            return
        self.refresh()
        record = {
            "time": datetime.now(UTC).isoformat(), "role": UNKNOWN, "host": host,
            "port": port, "allowed": False, "listed": self.listed(host), "bytes_up": 0,
            "bytes_down": 0, "reason": None,
        }
        role = self._identity(headers)
        if role is None:
            return await self._deny(
                writer, record, 407, "credentials", "Proxy credentials are missing or wrong",
                [("Proxy-Authenticate", 'Basic realm="firm"')],
            )
        record["role"] = role
        if port != PORTS[scheme]:
            return await self._deny(
                writer, record, 403, "port", f"Only {scheme} on port {PORTS[scheme]} is allowed"
            )
        if literal(host):
            return await self._deny(
                writer, record, 403, "literal_address", "Use a host name, not an address"
            )
        try:
            hostname(host)
        except ValueError:
            return await self._deny(writer, record, 400, "bad_host", f"Invalid host: {host}")
        if not self.allowed(host):
            return await self._deny(
                writer, record, 403, "not_allowed", f"Host not allowed by the firm: {host}"
            )
        record["allowed"] = True
        try:
            addresses = await asyncio.wait_for(self.resolver(host, port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError):
            return await self._deny(writer, record, 502, "unresolved", f"Cannot resolve {host}")
        public = [address for address in addresses if self.acceptable(address)]
        if not public:
            record["allowed"] = False
            return await self._deny(
                writer, record, 403, "private_address",
                f"{host} resolves only to private or host addresses",
            )
        for address in public:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self.opener(address, port), CONNECT_TIMEOUT
                )
                break
            except (OSError, TimeoutError):
                continue
        else:
            return await self._deny(writer, record, 502, "unreachable", f"Cannot reach {host}")
        # Registered before anything is answered: a revocation reaches a tunnel even while
        # it waits for its ClientHello.
        task, counted, logged = asyncio.current_task(), False, False
        self._relays[task] = record
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                # The tunnel carries TLS to the name it was opened for and to no other
                # name served from the same address: the ClientHello's SNI must be it.
                try:
                    first, name = await asyncio.wait_for(client_hello(reader), CONNECT_TIMEOUT)
                except (NotTLS, asyncio.IncompleteReadError, TimeoutError):
                    reason = "not_tls"
                else:
                    reason = None if name == host else "sni_mismatch"
                    # The grant may have gone while the client was sending its hello.
                    if reason is None and not self.allowed(host):
                        reason = "revoked"
                if reason is not None:
                    record["allowed"], logged = False, True
                    return self._refuse(record, reason)
            self.summary.request(role, host, True, record["listed"])
            counted = True
            if method == "CONNECT":
                record["bytes_up"] += len(first)
                upstream_writer.write(first)
                await upstream_writer.drain()
                await self._relay(reader, writer, upstream_reader, upstream_writer, record)
            else:
                await self._forward(
                    reader, writer, upstream_reader, upstream_writer, record,
                    f"{method} {path} {version}", headers, length,
                )
        except asyncio.CancelledError:
            # A revocation or cutoff ends the relay; its record says which. Before any
            # byte was forwarded, it is a refusal.
            if record["reason"] is None:
                record["reason"] = "closed"
            if not counted:
                record["allowed"], counted = False, True
                self.summary.request(role, host, False, record["listed"])
            raise
        finally:
            self._relays.pop(task, None)
            upstream_writer.close()
            if not logged:
                if not counted:
                    self.summary.request(role, host, record["allowed"], record["listed"])
                self._finish(record)

    async def _forward(self, reader, writer, upstream_reader, upstream_writer, record,
                       request_line, headers, length):
        """Exactly one request: its head, then ``length`` body bytes; then only the answer.

        The answer is relayed while the body uploads, so ``100 Continue`` and an early
        refusal reach the client. Nothing the client sends after the body reaches the
        origin, so a pipelined second request, to whatever host it names, is never
        forwarded.
        """
        host, port = record["host"], record["port"]
        authority = host if port == PORTS["http"] else f"{host}:{port}"
        lines = [
            request_line, f"Host: {authority}",
            *(f"{name}: {value}" for name, value in headers if name.lower() not in CONSUMED),
            "Connection: close",
        ]
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        activity = [asyncio.get_running_loop().time()]
        down = asyncio.create_task(pipe(upstream_reader, writer, record, "bytes_down", activity))
        up = asyncio.create_task(
            self._upload(reader, upstream_writer, record, head, length, activity)
        )
        await self._until_done(down, up, activity, record)

    async def _upload(self, reader, upstream_writer, record, head, length, activity) -> bool:
        """The request head and exactly ``length`` body bytes; True once all were sent.

        Every read from the client and every wait for the origin to take bytes is bounded
        by the idle time, so neither side can hold the upload open forever.
        """
        loop = asyncio.get_running_loop()
        try:
            record["bytes_up"] += len(head)
            upstream_writer.write(head)
            await asyncio.wait_for(upstream_writer.drain(), self.idle)
            remaining = length
            while remaining:
                chunk = await asyncio.wait_for(reader.read(min(65536, remaining)), self.idle)
                if not chunk:
                    return False  # The client went away mid-body: no complete request.
                activity[0] = loop.time()
                remaining -= len(chunk)
                record["bytes_up"] += len(chunk)
                upstream_writer.write(chunk)
                await asyncio.wait_for(upstream_writer.drain(), self.idle)
                activity[0] = loop.time()
            return True
        except TimeoutError:
            record["reason"] = "idle"
            return False
        except OSError:
            return False

    async def _relay(self, reader, writer, upstream_reader, upstream_writer, record):
        activity = [asyncio.get_running_loop().time()]
        up = asyncio.create_task(pipe(reader, upstream_writer, record, "bytes_up", activity))
        down = asyncio.create_task(pipe(upstream_reader, writer, record, "bytes_down", activity))
        await self._until_done(down, up, activity, record)

    async def _until_done(self, down, up, activity, record):
        """Wait for the answer to end, a broken client, or ``idle`` seconds of silence.

        A client that half-closed cleanly still waits for its answer.
        """
        watch = asyncio.create_task(idle_watch(activity, self.idle))
        tasks = {task for task in (up, down, watch) if task is not None}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if up is not None and up.done() and not down.done() and up.result():
                await asyncio.wait({down, watch}, return_when=asyncio.FIRST_COMPLETED)
            if watch.done() and not down.done():
                record["reason"] = "idle"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
