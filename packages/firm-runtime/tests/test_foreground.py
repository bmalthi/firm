import os
import signal
import socket
import time as wall_time
from pathlib import Path
from types import SimpleNamespace

import pytest
from firm_sandbox import Layout

import firm_runtime.sandbox_dispatcher as sandbox_dispatcher
from firm_runtime import foreground
from firm_runtime.errors import FirmError
from firm_runtime.foreground import (
    HostControl,
    configured_providers,
    console_announcement,
    drive_announcement,
)
from firm_runtime.hosting import LocalServer
from firm_runtime.notify import OwnerNotifier
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store


def test_stop_cancels_a_pending_resume_request():
    stopped = []
    control = HostControl(lambda force=False: stopped.append(force) or {"status": "stopped"})
    control.request_start()
    control.stop()
    assert control.consume(timeout=0) == (False, False)
    assert stopped == [False]


def _runtime(tmp_path, auth_file="/missing/codex-auth.json"):
    runtime = Runtime(Store.scratch(tmp_path / "durable" / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "codex",
            "accounts": {
                "personal": {"provider": "codex", "auth_file": auth_file, "max_active": 1}
            },
            "profiles": {
                "codex": {
                    "provider": "codex",
                    "model": "gpt-6-astra",
                    "effort": "high",
                    "accounts": ["personal"],
                }
            },
        },
    )
    return runtime


def test_missing_absolute_auth_file_does_not_prevent_host_reopening(tmp_path):
    runtime = _runtime(tmp_path)

    providers = configured_providers(runtime)
    runtime.start()
    invocation = runtime.admit()
    assert providers.for_session(invocation.session_id).auth_file == Path(
        "/missing/codex-auth.json"
    )


def test_relative_auth_file_is_still_rejected(tmp_path):
    with pytest.raises(FirmError, match="absolute auth_file"):
        _runtime(tmp_path, "relative-auth.json")


def _sandbox_config(tmp_path):
    root = tmp_path
    return SimpleNamespace(
        root=root,
        durable=root / "durable",
        identity_file=root / "durable" / "firm.json",
        locks=root / "locks",
        firm_id="test",
        egress_mode="enforce",
        allow_hosts=(),
    )


class Box:
    """The sandbox as the foreground host uses it, with no VM."""

    latest = None

    def __init__(self, config):
        self.config = config
        self.live = False
        self.ups = self.downs = 0
        self.file_server = object()
        config.root.mkdir(parents=True, exist_ok=True)
        config.root.chmod(0o700)
        Box.latest = self

    def identity(self):
        return "0123456789abcdef"

    def host_address(self):
        return Layout().host

    def configure_runtime(self, port):
        assert port == 8765

    def data_status(self):
        return {
            "storage": "linux-vm",
            "url": "smb://firmowner@127.0.0.1:52817/firm",
            "user": "firmowner",
            "password": "drive-admin-secret",
            "path": "/srv/firm",
        }

    def mac_acquire(self, role_id, activation_id):
        raise AssertionError("the Mac lease is not part of these host tests")

    def mac_release(self, lease_id):
        return {"status": "ok", "lease_id": lease_id, "gone": True}

    def mac_status(self):
        return {"configured": False, "enrolled": False, "stations": []}

    def mac_connect(self, lease_id):
        raise AssertionError("the Mac lease is not part of these host tests")

    def up(self):
        self.live = True
        self.ups += 1

    def set_allowed_hosts(self, hosts):
        self.allowed_hosts = hosts

    def runs(self):
        return []

    def inventory(self):
        return {}

    def infrastructure_status(self):
        return {"running": self.live}

    def down(self, *, expected_runs=(), force=False):
        assert expected_runs == ()
        self.live = False
        self.downs += 1


class Server:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _serve(monkeypatch, runtime, config, sandbox=Box, *, wait_for_resume=False):
    monkeypatch.setattr(foreground.Config, "load", lambda path: config)
    monkeypatch.setattr(foreground, "Sandbox", sandbox)
    monkeypatch.setattr(foreground, "LocalServer", Server)
    silent = OwnerNotifier(runner=lambda argv: None)
    monkeypatch.setattr(foreground, "OwnerNotifier", lambda: silent)
    monkeypatch.setattr(foreground, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(sandbox_dispatcher, "ingest_public_profiles", lambda *args, **kwargs: None)
    foreground.serve(
        runtime,
        sandbox_config=config.root / "sandbox.toml",
        wait_for_resume=wait_for_resume,
    )


def test_the_drive_announcement_carries_the_connection_details():
    access = {
        "url": "smb://firmowner@127.0.0.1:52817/firm",
        "user": "firmowner",
        "password": "drive-admin-secret",
    }
    announcement = drive_announcement(access)
    for value in access.values():
        assert value in announcement


def test_the_console_announcement_leads_with_the_bookmarkable_address():
    announcement = console_announcement(8766, "console-key-value")
    assert announcement.splitlines()[0] == "Console: http://127.0.0.1:8766/"
    assert "http://127.0.0.1:8766/unlock/console-key-value" in announcement


def test_duplicate_signals_during_unregistered_transaction_drain_before_admission(
    tmp_path, monkeypatch, capsys
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)
    handlers = {}

    def install(sig, handler):
        previous = handlers.get(sig)
        handlers[sig] = handler
        return previous

    class Signalling(Box):
        def up(self):
            # Reproduce the gap after BEGIN obtains its lock but before the
            # transaction context is registered. A handler must not open a writer.
            with runtime.store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                db.rollback()
            super().up()

    monkeypatch.setattr(foreground.signal, "signal", install)
    _serve(monkeypatch, runtime, config, Signalling)
    printed = capsys.readouterr().out
    assert "Drive: smb://firmowner@127.0.0.1:52817/firm\n" in printed
    assert "  Log in as firmowner with password drive-admin-secret\n" in printed
    assert runtime.status()["state"] == "stopped"
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 0
        state = db.execute("SELECT infrastructure_state FROM execution_host").fetchone()[0]
        assert state == "stopped"


def test_a_stop_from_another_terminal_closes_the_host_after_its_start(
    tmp_path, monkeypatch, capsys
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)

    class Stopped(Box):
        def infrastructure_status(self):
            if self.live and self.ups == 1 and self.downs == 0:
                # `firm stop <directory>` in another terminal, mid-tick.
                runtime.stop()
            return super().infrastructure_status()

    _serve(monkeypatch, runtime, config, Stopped)
    sandbox = Box.latest
    assert (sandbox.ups, sandbox.downs) == (1, 1)
    assert runtime.status()["state"] == "stopped"
    assert "The firm is stopped; closing the host." in capsys.readouterr().out


def test_waiting_host_resumes_and_stops_again_without_releasing_its_lifecycle(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)
    monkeypatch.setattr(foreground.Config, "load", lambda path: config)
    monkeypatch.setattr(foreground, "Sandbox", Box)
    monkeypatch.setattr(foreground, "LocalServer", Server)
    monkeypatch.setattr(
        foreground, "OwnerNotifier", lambda: OwnerNotifier(runner=lambda argv: None)
    )
    monkeypatch.setattr(foreground, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(sandbox_dispatcher, "ingest_public_profiles", lambda *args, **kwargs: None)
    captured = {}

    def console_app(runtime, *, allowed_host, sandbox, control):
        captured["control"] = control
        return SimpleNamespace(state=SimpleNamespace(console_key="test-key"))

    monkeypatch.setattr(foreground, "build_console_app", console_app)
    original_tick = foreground.SandboxDispatcher.tick
    calls = []

    def stop_each_run(dispatcher):
        calls.append(True)
        runtime.stop()
        return original_tick(dispatcher)

    monkeypatch.setattr(foreground.SandboxDispatcher, "tick", stop_each_run)
    consume_calls = []

    def resume_then_close(control, timeout=1.0):
        consume_calls.append(True)
        return len(consume_calls) == 1, len(consume_calls) == 2

    monkeypatch.setattr(foreground.HostControl, "consume", resume_then_close)
    foreground.serve(runtime, sandbox_config=config.root / "sandbox.toml", wait_for_resume=True)
    assert len(calls) == 2
    assert (Box.latest.ups, Box.latest.downs) == (2, 2)
    assert runtime.status()["state"] == "stopped"
    assert not (runtime.store.path.parent / foreground.HOST_RECORD).exists()


def test_failed_start_is_reported_to_the_caller(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)

    class Unavailable(Box):
        def up(self):
            raise RuntimeError("Linux VM failed to start")

    with pytest.raises(RuntimeError, match="Linux VM failed"):
        _serve(monkeypatch, runtime, _sandbox_config(tmp_path), Unavailable)
    assert runtime.status()["state"] == "stopped"
    with runtime.store.read() as db:
        events = [
            row[0]
            for row in db.execute(
                "SELECT kind FROM analysis_events WHERE kind IN "
                "('analysis.started','analysis.stopped') ORDER BY sequence"
            )
        ]
    assert events == ["analysis.started", "analysis.stopped"]


def test_detached_failed_start_keeps_console_until_shutdown_is_confirmed_then_allows_retry(
    tmp_path, monkeypatch, capsys
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)
    active_servers = 0

    class TrackedServer(Server):
        def __enter__(self):
            nonlocal active_servers
            active_servers += 1
            return self

        def __exit__(self, *args):
            nonlocal active_servers
            active_servers -= 1

    monkeypatch.setattr(foreground.Config, "load", lambda path: config)
    monkeypatch.setattr(foreground, "Sandbox", Box)
    monkeypatch.setattr(foreground, "LocalServer", TrackedServer)
    monkeypatch.setattr(
        foreground, "OwnerNotifier", lambda: OwnerNotifier(runner=lambda argv: None)
    )
    monkeypatch.setattr(foreground, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(sandbox_dispatcher, "ingest_public_profiles", lambda *args, **kwargs: None)
    monkeypatch.setattr(foreground.AnalysisObserver, "resources", lambda self: None)

    controls = []
    original_control = foreground.HostControl

    def capture_control(stop):
        control = original_control(stop)
        controls.append(control)
        return control

    monkeypatch.setattr(foreground, "HostControl", capture_control)
    original_start = sandbox_dispatcher.SandboxDispatcher.start
    starts = []

    def fail_then_start(dispatcher):
        starts.append(True)
        if len(starts) == 1:
            dispatcher.sandbox.up()
            dispatcher._infrastructure_state("unknown")
            raise RuntimeError("Linux VM provisioning failed")
        return original_start(dispatcher)

    monkeypatch.setattr(sandbox_dispatcher.SandboxDispatcher, "start", fail_then_start)
    original_stop = sandbox_dispatcher.SandboxDispatcher.stop
    stop_failures = []

    def fail_stop_once(dispatcher, force=False):
        if force and not stop_failures:
            stop_failures.append(True)
            raise RuntimeError("stop request temporarily failed")
        return original_stop(dispatcher, force=force)

    monkeypatch.setattr(sandbox_dispatcher.SandboxDispatcher, "stop", fail_stop_once)
    original_tick = sandbox_dispatcher.SandboxDispatcher.tick
    shutdown_failures = []

    def stop_after_retry(dispatcher):
        if runtime.status()["state"] == "stopping" and len(shutdown_failures) < 2:
            shutdown_failures.append(True)
            raise RuntimeError("VM shutdown probe failed")
        if runtime.status()["state"] == "running":
            runtime.stop()
        return original_tick(dispatcher)

    monkeypatch.setattr(sandbox_dispatcher.SandboxDispatcher, "tick", stop_after_retry)
    resumes = []

    def resume_only_after_shutdown(control, timeout=1.0):
        status = runtime.status()
        with runtime.store.read() as db:
            host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
        assert active_servers == 2
        assert status["state"] == "stopped"
        assert host["infrastructure_state"] == "stopped"
        resumes.append(True)
        if len(resumes) == 1:
            message = control.startup_error()
            assert "Linux VM provisioning failed" in message
            assert "VM shutdown is confirmed" in message
            assert len(message) < 256
            return True, False
        return False, True

    monkeypatch.setattr(original_control, "consume", resume_only_after_shutdown)
    foreground.serve(
        runtime,
        sandbox_config=config.root / "sandbox.toml",
        wait_for_resume=True,
    )

    assert len(starts) == 2
    assert len(resumes) == 2
    assert len(stop_failures) == 1
    assert len(shutdown_failures) == 2
    assert (Box.latest.ups, Box.latest.downs) == (2, 2)
    assert active_servers == 0
    assert "Firm startup failed: Linux VM provisioning failed" in capsys.readouterr().out


def test_http_startup_failure_closes_the_analysis_observation(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)

    def unavailable(self):
        raise FirmError("port is in use")

    monkeypatch.setattr(Server, "__enter__", unavailable)
    with pytest.raises(FirmError, match="port is in use"):
        _serve(monkeypatch, runtime, _sandbox_config(tmp_path))
    assert Box.latest.ups == 0
    with runtime.store.read() as db:
        events = [
            row[0]
            for row in db.execute(
                "SELECT kind FROM analysis_events WHERE kind IN "
                "('analysis.started','analysis.stopped') ORDER BY sequence"
            )
        ]
    assert events == ["analysis.started", "analysis.stopped"]


def test_first_tick_failure_after_successful_start_keeps_the_host_open(
    tmp_path, monkeypatch, capsys
):
    runtime = _runtime(tmp_path)
    tick = foreground.SandboxDispatcher.tick
    calls = []

    def interrupted(dispatcher):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary inventory failure")
        runtime.stop()
        return tick(dispatcher)

    monkeypatch.setattr(foreground.SandboxDispatcher, "tick", interrupted)
    _serve(monkeypatch, runtime, _sandbox_config(tmp_path))
    assert len(calls) >= 2
    assert (Box.latest.ups, Box.latest.downs) == (1, 1)
    assert runtime.status()["state"] == "stopped"
    assert "temporary inventory failure" in capsys.readouterr().out


def test_a_started_firm_reapplies_the_hosts_the_owner_granted(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.hosts.allow(OWNER, "pypi.org")
    tick = foreground.SandboxDispatcher.tick

    def once(dispatcher):
        runtime.stop()
        return tick(dispatcher)

    monkeypatch.setattr(foreground.SandboxDispatcher, "tick", once)
    _serve(monkeypatch, runtime, _sandbox_config(tmp_path))
    assert Box.latest.allowed_hosts == ["pypi.org"]


def test_a_firm_whose_grants_cannot_be_applied_does_not_start(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.hosts.allow(OWNER, "pypi.org")

    class Refusing(Box):
        def set_allowed_hosts(self, hosts):
            raise RuntimeError("Allowed hosts change incomplete: linux: not confirmed")

    with pytest.raises(FirmError, match="granted hosts could not be applied.*not confirmed"):
        _serve(monkeypatch, runtime, _sandbox_config(tmp_path), Refusing)
    assert runtime.status()["state"] != "running"
    with runtime.store.read() as db:
        assert db.execute("SELECT infrastructure_state FROM execution_host").fetchone()[0] == (
            "unknown"
        )


@pytest.mark.parametrize("wait_for_resume", [False, True])
def test_signal_before_start_leaves_the_firm_stopped_without_booting(
    tmp_path, monkeypatch, wait_for_resume
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)

    def install(sig, handler):
        if sig == signal.SIGTERM and callable(handler):
            handler(sig, None)
        return None

    monkeypatch.setattr(foreground.signal, "signal", install)
    _serve(monkeypatch, runtime, config, wait_for_resume=wait_for_resume)
    assert runtime.status()["state"] == "stopped"
    assert Box.latest.ups == 0
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 0


def test_signal_after_start_closes_a_waiting_host_once_firm_is_stopped(
    tmp_path, monkeypatch, capsys
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)
    handlers = {}

    def install(sig, handler):
        handlers[sig] = handler
        return None

    monkeypatch.setattr(foreground.signal, "signal", install)
    tick = foreground.SandboxDispatcher.tick
    calls = []

    def signal_once(dispatcher):
        calls.append(True)
        if len(calls) == 1:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return tick(dispatcher)

    monkeypatch.setattr(foreground.SandboxDispatcher, "tick", signal_once)
    _serve(monkeypatch, runtime, config, wait_for_resume=True)
    assert runtime.status()["state"] == "stopped"
    assert (Box.latest.ups, Box.latest.downs) == (1, 1)
    assert "closing the host after the signal" in capsys.readouterr().out


@pytest.mark.parametrize("mismatch", ["directory", "id"])
def test_wrong_firm_configuration_is_refused_before_sandbox_creation(
    tmp_path, monkeypatch, mismatch
):
    runtime = _runtime(tmp_path)
    config = _sandbox_config(tmp_path)
    if mismatch == "directory":
        config.root = tmp_path / "another-firm"
    else:
        config.firm_id = "another-firm"
    monkeypatch.setattr(foreground.Config, "load", lambda _: config)

    def unexpected_sandbox(_):
        pytest.fail("wrong firm configuration reached the sandbox")

    monkeypatch.setattr(foreground, "Sandbox", unexpected_sandbox)
    with pytest.raises(FirmError, match="firm"):
        foreground.serve(runtime, sandbox_config=tmp_path / "sandbox.toml")


def test_a_taken_port_names_the_port_and_the_likely_cause():
    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        with pytest.raises(FirmError) as error:
            with LocalServer(lambda port: None, port=port):
                pass
    finally:
        taken.close()
    assert f"cannot listen on 127.0.0.1:{port}" in str(error.value)
    assert "another firm's host is probably running" in str(error.value)
    assert "--mcp-port" in str(error.value)


def test_detached_launcher_returns_reconnects_and_keeps_child_output_alive(tmp_path, monkeypatch):
    directory = tmp_path / "detached-firm"
    directory.mkdir()
    from firm_runtime.compatibility import initialize_firm

    initialize_firm(directory)
    durable = directory / "durable"
    durable.mkdir(mode=0o700)
    key_file = durable / "console-key"
    key_file.write_text("A" * 43, encoding="ascii")
    key_file.chmod(0o600)
    fake_child = tmp_path / "fake-child"
    fake_child.write_text(
        "#!/bin/sh\n"
        'directory="$4"\n'
        'cat > "$directory/durable/owner-host.json" <<EOF\n'
        '{"pid":$$,"mcp_port":8765,"console_port":8766,"firm_id":"test",'
        '"console_scope":"fake-scope"}\n'
        "EOF\n"
        'chmod 600 "$directory/durable/owner-host.json"\n'
        "sleep 0.3\n"
        "echo later stdout\n"
        'echo child-owned bounded log >> "$FIRM_OWNER_HOST_LOG"\n'
        "sleep 4\n",
        encoding="utf-8",
    )
    fake_child.chmod(0o700)
    monkeypatch.setattr(foreground.sys, "executable", str(fake_child))

    def alive(record, _durable):
        try:
            os.kill(record["pid"], 0)
            return True
        except OSError:
            return False

    monkeypatch.setattr(foreground, "_console_alive", alive)
    first = foreground.launch_local_host(
        directory,
        sandbox_config=directory / "sandbox.toml",
        open_browser=False,
    )
    assert first["reused"] is False
    assert first["unlock_url"].endswith("/unlock/" + "A" * 43)
    wall_time.sleep(0.7)
    assert os.kill(first["pid"], 0) is None
    assert "child-owned bounded log" in (durable / foreground.HOST_LOG).read_text()
    second = foreground.launch_local_host(
        directory,
        sandbox_config=directory / "sandbox.toml",
        open_browser=False,
    )
    assert second["reused"] is True and second["pid"] == first["pid"]
    os.killpg(first["pid"], signal.SIGTERM)


def test_console_reconnect_probe_rejects_a_stale_or_different_http_service(tmp_path, monkeypatch):
    import json
    import urllib.error

    from firm_runtime.console import console_scope

    durable = tmp_path / "durable"
    durable.mkdir()
    key_file = durable / "console-key"
    key_file.write_text("B" * 43, encoding="ascii")
    key_file.chmod(0o600)
    record = {
        "pid": os.getpid(),
        "mcp_port": 8765,
        "console_port": 8766,
        "firm_id": "expected-firm",
        "console_scope": console_scope(durable / "firm.sqlite3", "expected-firm"),
    }
    seen = {}

    def stale(request, timeout):
        seen["cookie"] = request.headers["Cookie"]
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(foreground.urllib.request, "urlopen", stale)
    assert foreground._console_alive(record, durable) is False
    assert seen["cookie"] == f"firm_owner_console_127.0.0.1_8766={'B' * 43}"

    class Response:
        status = 200

        def __init__(self, firm_id, scope):
            self.firm_id, self.scope = firm_id, scope

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, _limit):
            return json.dumps(
                {"principal": "owner", "firm_id": self.firm_id, "console_scope": self.scope}
            ).encode()

    monkeypatch.setattr(
        foreground.urllib.request,
        "urlopen",
        lambda request, timeout: Response("other-firm", "wrong-scope"),
    )
    assert foreground._console_alive(record, durable) is False
    monkeypatch.setattr(
        foreground.urllib.request,
        "urlopen",
        lambda request, timeout: Response("expected-firm", record["console_scope"]),
    )
    assert foreground._console_alive(record, durable) is True


def test_cloned_firm_record_cannot_reconnect_to_the_source_console(tmp_path, monkeypatch):
    from firm_runtime.console import console_scope

    source = tmp_path / "source-firm"
    clone = tmp_path / "cloned-firm"
    (source / "durable").mkdir(parents=True)
    (clone / "durable").mkdir(parents=True)
    key = "C" * 43
    for durable in (source / "durable", clone / "durable"):
        key_file = durable / "console-key"
        key_file.write_text(key, encoding="ascii")
        key_file.chmod(0o600)
    record = {
        "pid": os.getpid(),
        "mcp_port": 8765,
        "console_port": 8766,
        "firm_id": "same-firm-id",
        "console_scope": console_scope(source / "durable" / "firm.sqlite3", "same-firm-id"),
    }
    contacted = []
    monkeypatch.setattr(
        foreground.urllib.request,
        "urlopen",
        lambda request, timeout: contacted.append(request) or None,
    )
    assert foreground._console_alive(record, clone / "durable") is False
    assert contacted == []


def test_bounded_host_log_rotates_owner_only_and_refuses_symlinks(tmp_path):
    path = tmp_path / "owner-host.log"
    path.parent.chmod(0o700)
    stream = foreground.BoundedHostLog(path)
    stream.write("x" * foreground.HOST_LOG_MAX)
    stream.write("y")
    stream.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == "y"
    assert path.with_name(path.name + ".1").stat().st_size == foreground.HOST_LOG_MAX

    path.unlink()
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    stream = foreground.BoundedHostLog(path)
    with pytest.raises(OSError):
        stream.write("blocked")
    assert target.read_text() == "untouched"
