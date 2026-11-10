import base64
import json
import os
import shutil
import socket
import stat
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from firm_sandbox.egress_proxy import token
from firm_sandbox.host_network import (
    Boundary,
    HostNetwork,
    create_proxy_key,
    locations,
    policy_id,
    proxy_port,
    read_egress,
    read_hosts,
    serve,
    write_egress,
    write_hosts,
)
from firm_sandbox.models import VMSettings


class FakeCommands:
    def __init__(self, env=None):
        self.env = dict(env or {})


def test_the_network_and_vm_settings_refuse_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getuid", lambda: 0)
    with pytest.raises(ValueError, match="ordinary host user"):
        serve(tmp_path / "network.json")
    with pytest.raises(ValueError, match="ordinary host user"):
        VMSettings(tmp_path, tmp_path / "locks", "firm", 1, 1)


def test_locations_are_bounded_and_instance_local(tmp_path):
    paths = locations(tmp_path, socket_root=tmp_path / "runtime")
    assert paths["config"] == paths["scratch"] / "network.json"
    assert paths["scratch"] == tmp_path / "scratch"
    # Removing reconstructible scratch state must not re-enable egress, drop the owner's
    # grants or change every role's proxy credential.
    assert paths["egress"] == tmp_path / "egress.json"
    assert paths["hosts"] == tmp_path / "hosts.json"
    assert paths["key"] == tmp_path / "proxy.key"
    for name in ("egress", "hosts", "key"):
        assert not paths[name].is_relative_to(paths["scratch"])
    for name in ("requests", "summary", "traffic"):
        assert paths[name].parent == paths["scratch"]
    assert paths["pid"].parent == tmp_path / "runtime" / "lima" / "_networks" / "user-v2"
    assert all(path.is_relative_to(tmp_path) for path in paths.values())


def test_policy_id_covers_the_proxy_exception_and_the_mode():
    reference = policy_id("192.168.5.2", 23456, "enforce")
    assert reference == policy_id("192.168.5.2", 23456, "enforce")
    assert reference != policy_id("192.168.6.2", 23456, "enforce")
    assert reference != policy_id("192.168.5.2", 23457, "enforce")
    assert reference != policy_id("192.168.5.2", 23456, "observe")


def test_a_grant_or_a_configured_host_never_unsettles_the_boundary(tmp_path):
    plain = HostNetwork(settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime")
    configured = HostNetwork(
        settings(tmp_path, allow_hosts=("example.com",)), FakeCommands(),
        socket_root=tmp_path / "runtime",
    )
    write_hosts(tmp_path / "hosts.json", ["granted.example"])
    assert plain.policy == configured.policy
    observing = HostNetwork(
        settings(tmp_path, egress_mode="observe"), FakeCommands(),
        socket_root=tmp_path / "runtime",
    )
    assert observing.policy != plain.policy


def test_each_vm_directory_has_its_own_stable_proxy_port(tmp_path):
    linux, mac = proxy_port(tmp_path / "linux"), proxy_port(tmp_path / "mac")
    assert linux == proxy_port(tmp_path / "linux") and linux != mac
    assert all(20000 <= port < 40000 for port in (linux, mac))


def test_egress_state_is_bounded_and_validated(tmp_path):
    path = tmp_path / "egress.json"
    assert read_egress(path) is True
    write_egress(path, False)
    assert read_egress(path) is False
    assert json.loads(path.read_text()) == {"egress": False}
    path.write_text(json.dumps({"egress": "yes"}))
    with pytest.raises(ValueError, match="Invalid persisted"):
        read_egress(path)
    with pytest.raises(ValueError, match="boolean"):
        write_egress(path, 1)


def test_granted_hosts_are_validated_sorted_and_private(tmp_path):
    path = tmp_path / "hosts.json"
    assert read_hosts(path) == ()
    assert write_hosts(path, ["b.example", "*.a.example", "b.example"]) == (
        "*.a.example", "b.example",
    )
    assert read_hosts(path) == ("*.a.example", "b.example")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for bad in (["1.2.3.4"], ["Example.com"], ["*.com"], ["com"], "example.com"):
        with pytest.raises(ValueError):
            write_hosts(path, bad)
    path.write_text(json.dumps({"hosts": ["localhost"]}))
    with pytest.raises(ValueError):
        read_hosts(path)


def test_proxy_key_is_created_once_owner_only_and_never_changes(tmp_path):
    path = tmp_path / "vm" / "proxy.key"
    key = create_proxy_key(path)
    assert len(key) == 32 and create_proxy_key(path) == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == ["proxy.key"]
    path.write_bytes(b"short")
    with pytest.raises(ValueError, match="Invalid proxy key"):
        create_proxy_key(path)


def settings(tmp_path, egress_mode="enforce", allow_hosts=()):
    return VMSettings(
        tmp_path, firm_locks=tmp_path / "locks", firm_id="firm", cpus=8, memory_gib=24,
        egress_mode=egress_mode, allow_hosts=allow_hosts,
    )


def test_proxy_url_names_the_role_and_its_token_but_never_the_key(tmp_path):
    network = HostNetwork(
        settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime",
        subnet="192.168.94.0/24",
    )
    network.create_key()
    url = network.proxy_url("analyst")
    key = (tmp_path / "proxy.key").read_bytes()
    assert url == (
        f"http://analyst:{token(key, 'analyst')}@192.168.94.2:{network.proxy_port}"
    )
    assert key.hex() not in url and base64.b64encode(key).decode() not in url
    for invalid in ("Analyst", "_internal", "role:x", "", "unknown@x"):
        with pytest.raises(ValueError):
            network.proxy_url(invalid)
    mac = HostNetwork(
        settings(tmp_path / "mac"), FakeCommands(), socket_root=tmp_path / "runtime",
        proxy_role="mac",
    )
    with pytest.raises(RuntimeError, match="no credentials"):
        mac.proxy_url("analyst")
    mac.create_key()
    assert not (tmp_path / "mac" / "proxy.key").exists()


def test_a_run_never_mints_a_key_the_running_proxy_would_reject(tmp_path, monkeypatch):
    network = HostNetwork(settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime")
    with pytest.raises(RuntimeError, match="proxy key missing; stop and start the VM"):
        network.proxy_url("analyst")
    assert not network.paths["key"].exists()
    # Only a filter's start makes it, before launching the process that reads it.
    launched = []

    def popen(*args, **kwargs):
        launched.append(network.paths["key"].exists())
        raise OSError("stop here")

    answer(monkeypatch, ConnectionRefusedError("no filter"))
    monkeypatch.setattr("firm_sandbox.host_network.subprocess.Popen", popen)
    network.paths["scratch"].mkdir()
    with pytest.raises(OSError, match="stop here"):
        network.start()
    assert launched == [True]
    assert network.proxy_url("analyst").startswith("http://analyst:")
    # A key lost while the proxy runs is not replaced behind its back.
    network.paths["key"].unlink()
    with pytest.raises(RuntimeError, match="proxy key missing"):
        network.proxy_url("analyst")
    assert not network.paths["key"].exists()


def test_network_configuration_carries_mode_exception_and_configured_hosts(tmp_path):
    network = HostNetwork(
        settings(tmp_path, "observe", ("example.com",)), FakeCommands(),
        socket_root=tmp_path / "runtime", subnet="192.168.94.0/24", proxy_role="mac",
    )
    network.write_configuration()
    assert json.loads(network.paths["config"].read_text()) == {
        "scratch": str(tmp_path / "scratch"),
        "socket_root": str(tmp_path / "runtime"),
        "subnet": "192.168.94.0/24",
        "proxy_port": network.proxy_port,
        "proxy_role": "mac",
        "mode": "observe",
        "allow_hosts": ["example.com"],
    }
    assert network.policy == policy_id("192.168.94.2", network.proxy_port, "observe")


def test_network_rejects_non_private_or_non_slash24_subnet(tmp_path):
    with pytest.raises(ValueError, match="private IPv4 /24"):
        HostNetwork(
            settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime",
            subnet="8.8.8.0/24",
        )


def answer(monkeypatch, reply):
    def ask(path, command, timeout=1.0):
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("firm_sandbox.host_network.ask", ask)


def test_network_ready_requires_matching_policy_and_attachment(tmp_path, monkeypatch):
    vm = settings(tmp_path)
    network = HostNetwork(vm, FakeCommands(), socket_root=tmp_path / "runtime")
    answer(monkeypatch, {"policy": network.policy, "attached": True})
    assert network.ready()
    assert network.ready(attached=False)
    assert network.boundary() is Boundary.ENFORCED
    answer(monkeypatch, {"policy": "wrong", "attached": True})
    assert not network.ready()
    assert network.boundary() is Boundary.UNENFORCED


def test_boundary_separates_an_unanswerable_probe_from_a_negative_answer(tmp_path, monkeypatch):
    vm = settings(tmp_path)
    network = HostNetwork(vm, FakeCommands(), socket_root=tmp_path / "runtime")
    answer(monkeypatch, ConnectionRefusedError("busy accept queue"))
    # A refused connection is a transport fact, not a detached filter...
    assert network.boundary() is Boundary.UNREACHABLE
    # ...but every caller that only asks ready() still reads it conservatively.
    assert not network.ready()
    answer(monkeypatch, {"policy": network.policy, "attached": False})
    assert network.boundary() is Boundary.UNENFORCED
    assert network.boundary(attached=False) is Boundary.ENFORCED


def test_hosts_reload_is_confirmed_only_by_the_list_the_proxy_now_holds(tmp_path, monkeypatch):
    network = HostNetwork(settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime")
    write_hosts(network.paths["hosts"], ["granted.example"])
    sent = []

    def ask(path, command, timeout=1.0):
        sent.append((command, timeout))
        return {"policy": network.policy, "granted_hosts": ["granted.example"]}

    monkeypatch.setattr("firm_sandbox.host_network.ask", ask)
    # No filter runs: nothing to ask, and its proxy reads the file when it starts.
    assert network.reload_hosts() is None and sent == []
    network.paths["control"].parent.mkdir(parents=True)
    network.paths["control"].touch()
    assert network.reload_hosts(timeout=0.5)["granted_hosts"] == ["granted.example"]
    assert sent == [(b"hosts", 0.5)]
    answer(monkeypatch, {"policy": network.policy, "granted_hosts": [],
                         "hosts_error": "Invalid granted hosts"})
    with pytest.raises(RuntimeError, match="not confirmed"):
        network.reload_hosts()
    answer(monkeypatch, ConnectionRefusedError("filter is down"))
    with pytest.raises(RuntimeError, match="not confirmed"):
        network.reload_hosts()


def test_a_control_reply_is_bounded_by_its_whole_timeout(tmp_path):
    """A filter that accepts and never answers costs the caller its timeout, no more."""
    import time

    from firm_sandbox.host_network import ask

    root = Path("/tmp").resolve() / f"hn-{uuid4().hex[:8]}"
    root.mkdir(mode=0o700)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
            silent.bind(str(root / "net.sock"))
            silent.listen()
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                ask(root / "net.sock", b"hosts", 0.3)
            assert time.monotonic() - started < 1
    finally:
        shutil.rmtree(root)


def test_egress_control_and_summary_read_the_live_filter_then_its_saved_record(
    tmp_path, monkeypatch
):
    network = HostNetwork(settings(tmp_path), FakeCommands(), socket_root=tmp_path / "runtime")
    answer(monkeypatch, {"egress": False})
    assert network.set_egress(False)["egress"] is False
    with pytest.raises(RuntimeError, match="not confirmed"):
        network.set_egress(True)
    summary = {"entries": [{"role": "analyst", "host": "pypi.org", "requests": 1}], "overflow": 0}
    answer(monkeypatch, {"egress_summary": summary})
    assert network.egress_summary() == {**summary, "live": True}
    answer(monkeypatch, ConnectionRefusedError("filter is down"))
    with pytest.raises(RuntimeError, match="unavailable"):
        network.egress_summary()
    network.paths["summary"].parent.mkdir()
    network.paths["summary"].write_text(json.dumps(summary))
    assert network.egress_summary() == {**summary, "live": False}
    assert network.prepare_egress(False) == {"egress": False, "live": False}
    assert network.saved_egress() is False


# ---- the network process, with a stand-in for Lima's usernet ---------------------------


@pytest.fixture
def live(tmp_path):
    """A HostNetwork whose filter process really runs; Lima's usernet only binds its socket.

    Unix socket paths are short-limited, so the VM directory lives directly under /tmp.
    """
    root = Path("/tmp").resolve() / f"hn-{uuid4().hex[:8]}"
    root.mkdir(mode=0o700)
    limactl = tmp_path / "limactl"
    # limactl usernet --endpoint EP --listen UPSTREAM ...: bind UPSTREAM and wait.
    limactl.write_text(
        f"#!/bin/sh\nexec {sys.executable} -c 'import socket, sys, time; "
        "s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(600)' "
        '"$5"\n'
    )
    limactl.chmod(0o700)
    network = HostNetwork(
        settings(root), FakeCommands({**os.environ, "FIRM_REAL_LIMACTL": str(limactl)}),
        socket_root=root / "rt", subnet="192.168.94.0/24",
    )
    network.paths["scratch"].mkdir(mode=0o700)
    network.write_configuration()
    yield network
    network.stop()
    shutil.rmtree(root)


def proxied(network, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", network.proxy_port), timeout=5) as connection:
        connection.sendall(request + b"\r\n")
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
        return answer


def test_the_network_process_runs_the_proxy_and_answers_its_control_commands(live):
    network = live
    write_hosts(network.paths["hosts"], ["granted.example"])
    network.start()
    assert network.ready(attached=False)
    assert stat.S_IMODE(network.paths["key"].stat().st_mode) == 0o600
    key = network.paths["key"].read_bytes()
    authorization = base64.b64encode(f"analyst:{token(key, 'analyst')}".encode())
    denied = proxied(network, b"CONNECT unlisted.example:443 HTTP/1.1\r\n"
                     b"Proxy-Authorization: Basic " + authorization + b"\r\n")
    assert denied.startswith(b"HTTP/1.1 403")
    assert proxied(network, b"CONNECT pypi.org:443 HTTP/1.1\r\n").startswith(b"HTTP/1.1 407")

    write_hosts(network.paths["hosts"], ["granted.example", "unlisted.example"])
    assert network.reload_hosts()["granted_hosts"] == ["granted.example", "unlisted.example"]
    summary = network.egress_summary()
    assert summary["live"] is True
    rows = {(row["role"], row["host"]): row for row in summary["entries"]}
    assert rows["analyst", "unlisted.example"]["denied"] == 1
    assert rows["unknown", "pypi.org"]["denied"] == 1

    assert network.set_egress(False)["egress"] is False and network.saved_egress() is False
    traffic = network.traffic()
    assert traffic["live"] is True and traffic["egress"] is False
    assert traffic["policy"] == network.policy and traffic["mode"] == "enforce"

    network.stop()
    assert not network.paths["control"].exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", network.proxy_port), timeout=1).close()
    assert network.egress_summary()["live"] is False
    lines = network.paths["requests"].read_text().splitlines()
    assert [json.loads(line)["reason"] for line in lines] == ["not_allowed", "credentials"]


def test_a_proxy_that_cannot_listen_stops_the_filter_with_it(live):
    network = live
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", network.proxy_port))
        taken.listen()
        with pytest.raises(RuntimeError, match="did not start"):
            network.start()
    assert not network.paths["control"].exists()
    assert "Egress proxy cannot listen" in network.paths["log"].read_text()
