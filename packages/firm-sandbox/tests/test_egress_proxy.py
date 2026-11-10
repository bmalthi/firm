"""The egress proxy on loopback: allowlist, credentials, framing, SNI, revocation, records.

Names resolve through a fake resolver and every upstream connection lands on a loopback
server: an echo server for tunnels, or an origin that answers with what it received for
plain HTTP. Nothing leaves this machine.
"""

import asyncio
import base64
import json
import socket
import socketserver
import threading
import time

import pytest

from firm_sandbox.egress_proxy import (
    BASELINE_HOSTS,
    EgressProxy,
    RequestLog,
    Summary,
    matches,
    permitted,
    public_address,
    token,
)

KEY = bytes(range(32))
PUBLIC = "93.184.216.34"
NAMES = {
    "api.anthropic.com": [PUBLIC],
    "pypi.org": [PUBLIC],
    "storage.googleapis.com": [PUBLIC],
    "a.b.example.com": [PUBLIC],
    "example.com": [PUBLIC],
    "unlisted.example": [PUBLIC],
    "private.example": ["10.0.0.5", "127.0.0.1"],
    "mixed.example": ["10.0.0.5", PUBLIC],
}


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(65536):
            self.request.sendall(data)


class Origin(socketserver.BaseRequestHandler):
    """An HTTP origin that answers with every byte it received before going quiet."""

    received = []

    def handle(self):
        self.request.settimeout(0.3)
        data = b""
        try:
            while chunk := self.request.recv(65536):
                data += chunk
        except TimeoutError:
            pass
        Origin.received.append(data)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(data) + data)


class Stall(socketserver.BaseRequestHandler):
    """An origin that takes the request and never answers."""

    def handle(self):
        while self.request.recv(65536):
            pass


class Continue(socketserver.BaseRequestHandler):
    """An origin that says 100 Continue after the head, then answers with the body."""

    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.request.recv(65536)
        head, body = data.split(b"\r\n\r\n", 1)
        length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length")))
        self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        while len(body) < length:
            body += self.request.recv(65536)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)


class Deaf(socketserver.BaseRequestHandler):
    """An origin that accepts the connection and never reads from it."""

    def handle(self):
        time.sleep(3)


def serve(handler):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


@pytest.fixture
def servers():
    Origin.received = []
    running = {"echo": serve(Echo), "origin": serve(Origin), "stall": serve(Stall),
               "continue": serve(Continue), "deaf": serve(Deaf)}
    yield {name: server.server_address[1] for name, server in running.items()}
    for server in running.values():
        server.shutdown()
        server.server_close()


@pytest.fixture
def start(tmp_path, servers):
    dialed, proxies = [], []

    async def resolver(host, port):
        if host not in NAMES:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return NAMES[host]

    def launch(upstream=None, **options):
        async def opener(address, port):
            dialed.append((address, port))
            # Tunnels reach the echo server and plain HTTP the origin, unless named.
            name = upstream or ("echo" if port == 443 else "origin")
            return await asyncio.open_connection("127.0.0.1", servers[name])

        proxy = EgressProxy(**{
            "port": 0, "log": RequestLog(tmp_path / "egress.jsonl"), "key": KEY,
            "resolver": resolver, "opener": opener,
            "acceptable": lambda address: public_address(address, local_addresses=set),
            **options,
        })
        proxy.start()
        proxy.dialed = dialed
        proxies.append(proxy)
        return proxy

    yield launch
    for proxy in proxies:
        proxy.stop()


def credentials(role, secret=None):
    value = f"{role}:{secret or token(KEY, role)}".encode()
    return b"Proxy-Authorization: Basic " + base64.b64encode(value) + b"\r\n"


def size(data, width=2):
    return len(data).to_bytes(width, "big") + data


def hello(name=None, *, records=1, extensions=None):
    """A minimal TLS ClientHello, an unrelated extension before its server_name; or one
    carrying exactly the ``extensions`` bytes given."""
    if extensions is None:
        extensions = b"\x00\x0b\x00\x02\x01\x00"
        if name is not None:
            names = size(b"\x00" + size(name.encode()))
            extensions += b"\x00\x00" + size(names)
    body = (b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
            + size(extensions))
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    step = -(-len(handshake) // records)
    return b"".join(
        b"\x16\x03\x01" + size(part)
        for part in (handshake[index:index + step] for index in range(0, len(handshake), step))
    )


ENTRY = b"\x00" + size(b"api.anthropic.com")  # one host_name entry of a server_name list
MALFORMED = {
    # An extension that declares more bytes than the extensions block holds.
    "extension_overrun": hello(extensions=b"\x00\x00\xff\xff" + size(ENTRY)),
    # A server_name list longer, or shorter, than the extension's contents.
    "list_longer": hello(extensions=b"\x00\x00" + size((len(ENTRY) + 3).to_bytes(2, "big")
                                                       + ENTRY)),
    "list_shorter": hello(extensions=b"\x00\x00" + size((len(ENTRY) - 3).to_bytes(2, "big")
                                                        + ENTRY)),
    # A host name whose declared length runs past the list that holds it.
    "name_truncated": hello(extensions=b"\x00\x00" + size(size(
        b"\x00" + (20).to_bytes(2, "big") + b"api.a"
    ))),
    # Two names in the one list, or two server_name extensions.
    "two_names": hello(extensions=b"\x00\x00" + size(size(ENTRY + ENTRY))),
    "two_extensions": hello(extensions=(b"\x00\x00" + size(size(ENTRY))) * 2),
}


def read_head(connection):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(1)
        if not chunk:
            break
        data += chunk
    return data


def read_all(connection):
    data = b""
    try:
        while chunk := connection.recv(65536):
            data += chunk
    except ConnectionResetError:
        pass
    return data


def ask(proxy, head, *, body=b""):
    """One request; the whole answer once the proxy closes."""
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(head + b"\r\n" + body)
        connection.shutdown(socket.SHUT_WR)
        return read_all(connection)


def tunnel(proxy, host, role="analyst"):
    """CONNECT ``host`` with the role's credentials; the answer and the client's records."""
    return ask(proxy, f"CONNECT {host}:443 HTTP/1.1\r\n".encode() + credentials(role),
               body=hello(host))


def records(tmp_path, count=1):
    path = tmp_path / "egress.jsonl"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lines = path.read_text().splitlines() if path.exists() else []
        if len(lines) >= count:
            return [json.loads(line) for line in lines]
        time.sleep(0.01)
    raise AssertionError("the proxy wrote no log record")


def connect(proxy, target, *extra):
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    connection.sendall(f"CONNECT {target} HTTP/1.1\r\n".encode() + b"".join(extra) + b"\r\n")
    return connection


# ---- allowlist and credentials ----------------------------------------------------------


@pytest.mark.parametrize(("host", "entry", "expected"), [
    ("api.github.com", "*.github.com", True),
    ("a.b.github.com", "*.github.com", True),
    ("github.com", "*.github.com", False),
    ("evilgithub.com", "*.github.com", False),
    ("github.com.evil.example", "*.github.com", False),
    ("github.com", "github.com", True),
    ("api.github.com", "github.com", False),
])
def test_suffix_entries_match_any_subdomain_but_never_the_bare_suffix(host, entry, expected):
    assert matches(host, entry) is expected


def test_baseline_is_sorted_valid_and_carries_the_provider_and_registry_hosts():
    from firm_sandbox.models import host_entries

    assert BASELINE_HOSTS == host_entries(BASELINE_HOSTS)
    for host in ("api.anthropic.com", "api.openai.com", "github.com", "registry.npmjs.org",
                 "pypi.org", "files.pythonhosted.org", "registry-1.docker.io", "ghcr.io",
                 "ports.ubuntu.com", "objects.githubusercontent.com"):
        assert permitted(host, BASELINE_HOSTS), host


def test_uv_installer_redirect_host_is_allowed_without_wildcarding_astral():
    assert permitted("astral.sh", BASELINE_HOSTS)
    assert permitted("releases.astral.sh", BASELINE_HOSTS)
    assert not permitted("download.releases.astral.sh", BASELINE_HOSTS)
    assert not permitted("releases.astral.sh.attacker.example", BASELINE_HOSTS)


def test_agy_hosts_are_exact_and_do_not_include_host_renewal_or_optional_services():
    for host in ("www.googleapis.com", "daily-cloudcode-pa.googleapis.com"):
        assert permitted(host, BASELINE_HOSTS)
        assert not permitted("subdomain." + host, BASELINE_HOSTS)
        assert not permitted(host + ".attacker.example", BASELINE_HOSTS)
    for host in ("oauth2.googleapis.com", "accounts.google.com", "play.googleapis.com",
                 "aicode.googleapis.com",
                 "lh3.googleusercontent.com", "generativelanguage.googleapis.com",
                 "antigravity-cli-auto-updater-974169037036.us-central1.run.app"):
        assert not permitted(host, BASELINE_HOSTS)


def test_host_outside_baseline_obeys_owner_grants_and_observe_mode(start):
    host = "storage.googleapis.com"
    proxy = start()
    proxy.set_granted((host,))
    assert tunnel(proxy, host, "analyst").startswith(b"HTTP/1.1 200")
    proxy.set_granted(())
    assert tunnel(proxy, host, "analyst").startswith(b"HTTP/1.1 403")
    observed = start(mode="observe")
    assert not observed.listed(host)
    assert tunnel(observed, host, "analyst").startswith(b"HTTP/1.1 200")


def test_connect_to_an_allowed_name_tunnels_and_is_logged_and_summarized(start, tmp_path):
    proxy = start()
    first = hello("api.anthropic.com")
    with connect(proxy, "api.anthropic.com:443", credentials("analyst")) as connection:
        assert read_head(connection) == b"HTTP/1.1 200 Connection established\r\n\r\n"
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        assert received == first
        connection.sendall(b"application data")
        assert connection.recv(64) == b"application data"
        connection.shutdown(socket.SHUT_WR)
        assert read_all(connection) == b""
    (record,) = records(tmp_path)
    assert set(record) == {"time", "role", "host", "port", "allowed", "listed", "bytes_up",
                           "bytes_down", "reason"}
    total = len(first) + 16
    assert {key: record[key] for key in record if key != "time"} == {
        "role": "analyst", "host": "api.anthropic.com", "port": 443, "allowed": True,
        "listed": True, "bytes_up": total, "bytes_down": total, "reason": None,
    }
    assert proxy.dialed == [(PUBLIC, 443)]
    (row,) = proxy.summary.snapshot()["entries"]
    assert {key: row[key] for key in row if key != "last_seen"} == {
        "role": "analyst", "host": "api.anthropic.com", "listed": True, "requests": 1,
        "denied": 0, "unlisted": 0, "bytes_up": total, "bytes_down": total,
    }


def test_an_unlisted_name_is_refused_with_one_line_and_never_dialed(start, tmp_path):
    proxy = start()
    answer = ask(proxy, b"CONNECT unlisted.example:443 HTTP/1.1\r\n" + credentials("analyst"))
    head, body = answer.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 403 Forbidden")
    assert body == b"Host not allowed by the firm: unlisted.example\n"
    (record,) = records(tmp_path)
    assert (record["role"], record["allowed"], record["listed"], record["reason"]) == (
        "analyst", False, False, "not_allowed"
    )
    assert proxy.dialed == []
    assert proxy.summary.snapshot()["entries"][0]["denied"] == 1


def test_a_grant_or_a_configured_suffix_takes_effect_without_a_restart(start):
    proxy = start(allow_hosts=("*.example.com",))
    assert tunnel(proxy, "a.b.example.com").startswith(b"HTTP/1.1 200")
    assert b" 403 " in tunnel(proxy, "example.com")
    assert b" 403 " in tunnel(proxy, "unlisted.example")
    proxy.set_granted(["unlisted.example"])
    assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize("authorization", [
    b"",
    credentials("analyst", "0" * 64),
    credentials("analyst", token(KEY, "other")),
    credentials("Analyst"),
    credentials("_internal"),
    b"Proxy-Authorization: Bearer abc\r\n",
    b"Proxy-Authorization: Basic not-base64!\r\n",
    credentials("analyst") + credentials("analyst"),
])
def test_missing_or_wrong_credentials_get_407_and_are_logged_as_unknown(
    start, tmp_path, authorization
):
    proxy = start()
    answer = ask(proxy, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n" + authorization)
    assert answer.startswith(b"HTTP/1.1 407 Proxy Authentication Required")
    assert b'Proxy-Authenticate: Basic realm="firm"' in answer
    (record,) = records(tmp_path)
    assert (record["role"], record["allowed"], record["reason"]) == (
        "unknown", False, "credentials"
    )
    assert proxy.dialed == []


# ---- ports, addresses, framing ----------------------------------------------------------


@pytest.mark.parametrize(("request_line", "reason"), [
    (b"CONNECT api.anthropic.com:22 HTTP/1.1", "port"),
    (b"CONNECT api.anthropic.com:80 HTTP/1.1", "port"),
    (b"GET http://api.anthropic.com:8080/ HTTP/1.1", "port"),
    (b"GET http://api.anthropic.com:443/ HTTP/1.1", "port"),
    (b"CONNECT 93.184.216.34:443 HTTP/1.1", "literal_address"),
    (b"CONNECT 127.1:443 HTTP/1.1", "literal_address"),
    (b"CONNECT [::1]:443 HTTP/1.1", "literal_address"),
    (b"GET http://2130706433/ HTTP/1.1", "literal_address"),
    (b"CONNECT localhost:443 HTTP/1.1", "bad_host"),
    (b"CONNECT private.example:443 HTTP/1.1", "private_address"),
])
def test_ports_literals_and_private_names_are_refused_in_either_mode(
    start, tmp_path, request_line, reason
):
    for mode in ("enforce", "observe"):
        (tmp_path / "egress.jsonl").unlink(missing_ok=True)
        proxy = start(mode=mode, allow_hosts=("private.example",))
        answer = ask(proxy, request_line + b"\r\n" + credentials("analyst"))
        assert answer.split(b"\r\n", 1)[0].split(b" ")[1] in (b"403", b"400")
        assert records(tmp_path)[-1]["reason"] == reason
        assert proxy.dialed == []


def test_only_the_public_addresses_of_a_name_are_dialed(start):
    proxy = start(allow_hosts=("mixed.example",))
    assert tunnel(proxy, "mixed.example").startswith(b"HTTP/1.1 200")
    assert proxy.dialed == [(PUBLIC, 443)]


@pytest.mark.parametrize("head", [
    b"GET /relative HTTP/1.1\r\n",
    b"GET https://pypi.org/ HTTP/1.1\r\n",
    b"CONNECT pypi.org HTTP/1.1\r\n",
    b"NONSENSE\r\n",
    b"GET http://user:pw@pypi.org/ HTTP/1.1\r\n",
    # Framing the origin could read differently from the proxy.
    b"POST http://pypi.org/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n",
    b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: 0\r\nTransfer-Encoding: identity\r\n",
    b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n",
    b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: -1\r\n",
    b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: \xb2\r\n",
    # A bare LF or CR is never read as a line break.
    b"GET http://pypi.org/ HTTP/1.1\r\nAccept: */*\nHost: evil.example\r\n",
    b"GET http://pypi.org/ HTTP/1.1\r\nAccept: */*\rHost: evil.example\r\n",
    b"GET http://pypi.org/ HTTP/1.1\r\nBad Name: x\r\n",
])
def test_malformed_requests_are_refused_without_a_record_or_a_dial(start, tmp_path, head):
    proxy = start()
    assert ask(proxy, head + credentials("analyst")).startswith(b"HTTP/1.1 400")
    assert not (tmp_path / "egress.jsonl").exists()
    assert proxy.dialed == [] and Origin.received == []


def test_plain_http_reaches_the_url_host_without_proxy_headers(start, tmp_path):
    proxy = start()
    answer = ask(
        proxy,
        b"GET http://pypi.org/simple/?q=1 HTTP/1.1\r\nHost: fronted.example\r\n"
        b"Proxy-Connection: keep-alive\r\nAccept: */*\r\n" + credentials("analyst"),
    )
    forwarded = (
        b"GET /simple/?q=1 HTTP/1.1\r\nHost: pypi.org\r\nAccept: */*\r\n"
        b"Connection: close\r\n\r\n"
    )
    assert Origin.received == [forwarded]
    assert answer.endswith(b"\r\n\r\n" + forwarded)
    assert proxy.dialed == [(PUBLIC, 80)]
    (record,) = records(tmp_path)
    assert record["bytes_up"] == len(forwarded) and record["bytes_down"] == len(answer)


def test_exactly_one_request_and_its_body_reach_the_origin(start):
    proxy = start()
    pipelined = b"GET http://unlisted.example/steal HTTP/1.1\r\nHost: unlisted.example\r\n\r\n"
    answer = ask(
        proxy,
        b"POST http://pypi.org/upload HTTP/1.1\r\nContent-Length: 5\r\n" + credentials("analyst"),
        body=b"hello" + pipelined,
    )
    (received,) = Origin.received
    assert received == (b"POST /upload HTTP/1.1\r\nHost: pypi.org\r\nContent-Length: 5\r\n"
                        b"Connection: close\r\n\r\nhello")
    assert b"unlisted.example" not in received and answer.startswith(b"HTTP/1.1 200 OK")


# ---- the TLS destination ----------------------------------------------------------------


@pytest.mark.parametrize("first", [hello("API.Anthropic.com"), hello("api.anthropic.com.")])
def test_the_server_name_matches_without_regard_to_case(start, first):
    proxy = start()
    head = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n" + credentials("analyst")
    assert ask(proxy, head, body=first) == b"HTTP/1.1 200 Connection established\r\n\r\n" + first


def test_a_client_hello_spread_over_records_is_read_whole(start):
    proxy = start()
    first = hello("api.anthropic.com", records=3)
    head = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n" + credentials("analyst")
    assert ask(proxy, head, body=first).endswith(first)


@pytest.mark.parametrize(("first", "reason"), [
    (hello("unlisted.example"), "sni_mismatch"),
    (hello("evil.api.anthropic.com"), "sni_mismatch"),
    (hello(None), "sni_mismatch"),
    (b"GET / HTTP/1.1\r\nHost: unlisted.example\r\n\r\n", "not_tls"),
    (b"\x16\x03\x01\x00\x05\x02\x00\x00\x01\x00", "not_tls"),
    (hello("api.anthropic.com")[:20], "not_tls"),
    *((malformed, "not_tls") for malformed in MALFORMED.values()),
], ids=lambda value: next((name for name, data in MALFORMED.items() if data == value), None))
def test_a_tunnel_whose_client_hello_names_another_host_forwards_nothing(
    start, tmp_path, first, reason
):
    proxy = start()
    head = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n" + credentials("analyst")
    # The echo upstream would return anything forwarded; nothing comes back.
    assert ask(proxy, head, body=first) == b"HTTP/1.1 200 Connection established\r\n\r\n"
    (record,) = records(tmp_path)
    assert (record["allowed"], record["reason"], record["bytes_up"]) == (False, reason, 0)
    assert proxy.summary.snapshot()["entries"][0]["denied"] == 1


@pytest.mark.parametrize("name", list(MALFORMED))
def test_the_client_hello_parser_refuses_every_length_that_does_not_fit(name):
    from firm_sandbox.egress_proxy import NotTLS, server_name

    handshake = MALFORMED[name][5:]
    with pytest.raises(NotTLS):
        server_name(handshake[4:])
    assert server_name(hello("api.anthropic.com")[9:]) == "api.anthropic.com"


# ---- revocation ---------------------------------------------------------------------------


@pytest.mark.parametrize("cut", ["revoked", "egress_off"])
def test_an_open_relay_closes_when_its_host_is_revoked_or_egress_is_cut(start, tmp_path, cut):
    proxy = start()
    proxy.set_granted(["unlisted.example", "pypi.org"])
    first = hello("unlisted.example")
    with connect(proxy, "unlisted.example:443", credentials("analyst")) as connection:
        read_head(connection)
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        if cut == "revoked":
            proxy.set_granted(["pypi.org"])
        else:
            proxy.close_relays()
        assert read_all(connection) == b""
    (record,) = records(tmp_path)
    assert (record["allowed"], record["reason"]) == (True, cut)


def test_a_tunnel_revoked_before_its_client_hello_forwards_nothing(start, tmp_path):
    proxy = start()
    proxy.set_granted(["unlisted.example"])
    with connect(proxy, "unlisted.example:443", credentials("analyst")) as connection:
        assert read_head(connection) == b"HTTP/1.1 200 Connection established\r\n\r\n"
        proxy.set_granted([])
        time.sleep(0.1)
        try:
            connection.sendall(hello("unlisted.example"))
        except OSError:
            pass  # Already closed: the point of the test.
        assert read_all(connection) == b""
    (record,) = records(tmp_path)
    assert (record["allowed"], record["reason"], record["bytes_up"]) == (False, "revoked", 0)
    assert proxy.summary.snapshot()["entries"][0]["denied"] == 1


def test_a_grant_gone_while_the_client_hello_arrives_is_refused_before_forwarding(
    start, tmp_path
):
    proxy = start()
    proxy.set_granted(["unlisted.example"])
    # Allowed when the CONNECT is decided, gone by the time its hello has been read: the
    # change lands between the two checks, where no revocation could reach the relay.
    answers = iter([True, False])
    proxy.allowed = lambda host, identity=None: next(answers)
    answer = tunnel(proxy, "unlisted.example")
    assert answer == b"HTTP/1.1 200 Connection established\r\n\r\n"
    (record,) = records(tmp_path)
    assert (record["allowed"], record["reason"]) == (False, "revoked")


# ---- hosts.json is the authority -------------------------------------------------------------


def test_the_hosts_file_takes_effect_on_the_next_request_without_a_command(start, tmp_path):
    path = tmp_path / "hosts.json"
    proxy = start(hosts_path=path)
    assert b" 403 " in tunnel(proxy, "unlisted.example")
    path.write_text(json.dumps({"hosts": ["unlisted.example"]}))
    assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")
    # An open tunnel to it closes once the file stops granting it and a request notices.
    first = hello("unlisted.example")
    with connect(proxy, "unlisted.example:443", credentials("analyst")) as connection:
        read_head(connection)
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        path.write_text(json.dumps({"hosts": []}))
        assert b" 403 " in tunnel(proxy, "unlisted.example")
        assert read_all(connection) == b""
    path.unlink()
    assert proxy.granted == () and b" 403 " in tunnel(proxy, "unlisted.example")


def test_an_idle_tunnel_loses_a_revoked_grant_without_any_new_request(start, tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": ["unlisted.example"]}))
    proxy = start(hosts_path=path)
    first = hello("unlisted.example")
    with connect(proxy, "unlisted.example:443", credentials("analyst")) as connection:
        read_head(connection)
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        path.write_text(json.dumps({"hosts": []}))
        started = time.monotonic()
        assert read_all(connection) == b""
        assert time.monotonic() - started < 2.5
    (record,) = records(tmp_path)
    assert record["reason"] == "revoked"


def test_a_failed_read_is_retried_even_when_the_file_did_not_change(tmp_path):
    """The file's version is remembered only once read: a transient failure is retried."""
    path = tmp_path / "hosts.json"
    proxy = EgressProxy(port=0, log=RequestLog(tmp_path / "egress.jsonl"), key=KEY,
                        hosts_path=path)
    path.write_text(json.dumps({"hosts": ["unlisted.example"]}))
    path.chmod(0)
    try:
        proxy.refresh()
        assert proxy.granted == () and proxy.hosts_error
    finally:
        path.chmod(0o600)  # Same mtime, size and inode: only the read changes.
    proxy.refresh()
    assert proxy.granted == ("unlisted.example",) and proxy.hosts_error is None
    # A directory in the file's place is a read error too, and a good file then wins.
    path.unlink()
    path.mkdir()
    proxy.refresh()
    assert proxy.granted == ("unlisted.example",) and proxy.hosts_error
    path.rmdir()
    path.write_text(json.dumps({"hosts": ["pypi.org"]}))
    proxy.refresh()
    assert proxy.granted == ("pypi.org",)


def test_a_corrupt_hosts_file_keeps_the_previous_list_and_is_logged_once(
    start, tmp_path, caplog
):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": ["unlisted.example"]}))
    proxy = start(hosts_path=path)
    assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")
    for content in ("{not json", "[also not an object"):
        path.write_text(content)
        assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")
        assert tunnel(proxy, "pypi.org").startswith(b"HTTP/1.1 200")
    assert proxy.hosts_error and proxy.granted == ("unlisted.example",)
    assert sum("Keeping the previous granted hosts" in line for line in caplog.messages) == 1
    # A list the sandbox would never write (an address) is refused the same way.
    path.write_text(json.dumps({"hosts": ["1.2.3.4"]}))
    assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")
    assert "1.2.3.4" in proxy.hosts_error
    path.write_text(json.dumps({"hosts": []}))
    assert b" 403 " in tunnel(proxy, "unlisted.example")
    assert proxy.hosts_error is None


def test_revoking_one_host_leaves_a_relay_to_another_open(start):
    proxy = start()
    proxy.set_granted(["unlisted.example"])
    first = hello("api.anthropic.com")
    with connect(proxy, "api.anthropic.com:443", credentials("analyst")) as connection:
        read_head(connection)
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        proxy.set_granted([])
        time.sleep(0.1)
        connection.sendall(b"still open")
        assert connection.recv(64) == b"still open"


# ---- idle relays ----------------------------------------------------------------------------


def test_a_silent_tunnel_closes_after_the_idle_time(start, tmp_path):
    proxy = start(idle=0.3)
    first = hello("api.anthropic.com")
    with connect(proxy, "api.anthropic.com:443", credentials("analyst")) as connection:
        read_head(connection)
        connection.sendall(first)
        received = b""
        while len(received) < len(first):
            received += connection.recv(65536)
        # Traffic either way keeps it open past the idle time...
        for _ in range(3):
            time.sleep(0.15)
            connection.sendall(b"ping")
            assert connection.recv(64) == b"ping"
        # ...and silence closes it.
        started = time.monotonic()
        assert read_all(connection) == b""
        assert time.monotonic() - started < 2
    (record,) = records(tmp_path)
    assert (record["allowed"], record["reason"]) == (True, "idle")


def test_an_origin_that_never_answers_is_closed_after_the_idle_time(start, tmp_path):
    proxy = start(idle=0.3, upstream="stall")
    started = time.monotonic()
    assert ask(proxy, b"GET http://pypi.org/ HTTP/1.1\r\n" + credentials("analyst")) == b""
    assert time.monotonic() - started < 2
    (record,) = records(tmp_path)
    assert record["reason"] == "idle" and record["bytes_down"] == 0


def test_a_body_that_stops_arriving_is_closed_after_the_idle_time(start, tmp_path):
    # The origin waits for the rest of the body and says nothing meanwhile.
    proxy = start(idle=0.3, upstream="stall")
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: 10\r\n"
                           + credentials("analyst") + b"\r\nabc")
        assert read_all(connection) == b""
    (record,) = records(tmp_path)
    assert record["reason"] == "idle"


def test_100_continue_reaches_the_client_before_it_sends_its_body(start, tmp_path):
    proxy = start(upstream="continue")
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(b"POST http://pypi.org/upload HTTP/1.1\r\nContent-Length: 5\r\n"
                           b"Expect: 100-continue\r\n" + credentials("analyst") + b"\r\n")
        # The client holds its body until the origin says to go on.
        assert read_head(connection) == b"HTTP/1.1 100 Continue\r\n\r\n"
        connection.sendall(b"hello")
        answer = read_all(connection)
    assert answer == b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
    (record,) = records(tmp_path)
    assert record["reason"] is None


def test_an_origin_that_stops_reading_the_upload_is_closed_after_the_idle_time(
    start, tmp_path
):
    proxy = start(idle=0.3, upstream="deaf")
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        body = 64 * 1024 * 1024
        connection.sendall(b"POST http://pypi.org/ HTTP/1.1\r\nContent-Length: %d\r\n" % body
                           + credentials("analyst") + b"\r\n")
        try:
            # Fills every buffer between here and the deaf origin, then is cut off.
            connection.sendall(bytes(body))
        except OSError:
            pass
        read_all(connection)
    assert time.monotonic() - started < 4
    (record,) = records(tmp_path)
    assert record["reason"] == "idle" and record["bytes_up"] < body


# ---- observe mode, the Mac, lifecycle -----------------------------------------------------


def test_observe_mode_allows_every_name_and_says_which_enforce_would_refuse(start, tmp_path):
    proxy = start(mode="observe")
    assert tunnel(proxy, "unlisted.example").startswith(b"HTTP/1.1 200")
    assert tunnel(proxy, "pypi.org").startswith(b"HTTP/1.1 200")
    unlisted, listed = records(tmp_path, 2)
    assert (unlisted["host"], unlisted["allowed"], unlisted["listed"]) == (
        "unlisted.example", True, False,
    )
    assert (listed["host"], listed["allowed"], listed["listed"]) == ("pypi.org", True, True)
    rows = {row["host"]: row for row in proxy.summary.snapshot()["entries"]}
    assert (rows["unlisted.example"]["listed"], rows["unlisted.example"]["unlisted"]) == (False, 1)
    assert (rows["pypi.org"]["listed"], rows["pypi.org"]["unlisted"]) == (True, 0)
    assert rows["unlisted.example"]["denied"] == 0


def test_the_mac_proxy_attributes_everything_to_the_mac_without_credentials(start, tmp_path):
    proxy = start(key=None, role="mac")
    ask(proxy, b"GET http://pypi.org/ HTTP/1.1\r\n" + credentials("analyst"))
    assert b"Proxy-Authorization" not in Origin.received[0]
    assert ask(proxy, b"CONNECT unlisted.example:443 HTTP/1.1\r\n").startswith(b"HTTP/1.1 403")
    assert [(record["role"], record["allowed"]) for record in records(tmp_path, 2)] == [
        ("mac", True), ("mac", False),
    ]


def test_the_proxy_needs_exactly_one_way_to_name_roles(tmp_path):
    log = RequestLog(tmp_path / "egress.jsonl")
    for options in ({}, {"key": KEY, "role": "mac"}):
        with pytest.raises(ValueError, match="key for role credentials"):
            EgressProxy(port=0, log=log, **options)
    with pytest.raises(ValueError, match="mode"):
        EgressProxy(port=0, log=log, key=KEY, mode="open")


def test_a_taken_port_fails_the_start(start):
    proxy = start()
    with pytest.raises(RuntimeError, match="cannot listen"):
        start(port=proxy.port)


def test_stop_ends_the_listener(start):
    proxy = start()
    assert proxy.alive
    proxy.stop()
    assert not proxy.alive
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=1).close()


def test_summary_is_bounded_with_an_overflow_count():
    summary = Summary(capacity=2)
    summary.request("a", "one.example", True, True)
    summary.request("a", "two.example", False, False)
    summary.request("b", "one.example", True, True)
    summary.request("a", "one.example", True, True)
    summary.transfer("a", "one.example", 5, 7)
    summary.transfer("b", "one.example", 5, 7)
    snapshot = summary.snapshot()
    assert snapshot["overflow"] == 1
    rows = {(row["role"], row["host"]): row for row in snapshot["entries"]}
    assert set(rows) == {("a", "one.example"), ("a", "two.example")}
    assert (rows["a", "one.example"]["requests"], rows["a", "one.example"]["bytes_down"]) == (2, 7)
    assert (rows["a", "two.example"]["denied"], rows["a", "two.example"]["unlisted"]) == (1, 1)


def test_request_log_keeps_one_previous_file_within_its_limit(tmp_path):
    log = RequestLog(tmp_path / "egress.jsonl", limit=400)
    for index in range(20):
        log.write({"index": index, "padding": "x" * 20})
    current = (tmp_path / "egress.jsonl").read_text().splitlines()
    previous = (tmp_path / "egress.jsonl.1").read_text().splitlines()
    assert json.loads(current[-1])["index"] == 19
    assert json.loads(previous[-1])["index"] == json.loads(current[0])["index"] - 1
    total = sum(path.stat().st_size for path in tmp_path.iterdir())
    assert total <= 400
    assert oct((tmp_path / "egress.jsonl").stat().st_mode & 0o777) == "0o600"


def test_public_address_refuses_private_and_host_addresses():
    assert public_address(PUBLIC, local_addresses=set)
    assert not public_address(PUBLIC, local_addresses=lambda: {socket.inet_aton(PUBLIC)})
    for address in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "224.0.0.1",
                    "0.0.0.0", "::1", "not-an-address"):
        assert not public_address(address, local_addresses=set)
