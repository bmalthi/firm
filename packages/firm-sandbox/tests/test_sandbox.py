"""Runtime facade contracts without booting a VM or touching native accounts."""

import base64
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import Config, Layout, Organization, RunSpec, Sandbox, sandbox
from firm_sandbox.compatibility import initialize_firm_record, release_identifier


class FakeNetwork:
    """A VM's filter and proxy: a confirmed boundary, a role's proxy URL, and what was asked."""

    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.fail = None
        self.live = True  # whether a filter runs to answer

    def ready(self, **_):
        return True

    def proxy_url(self, role_id):
        return f"http://{role_id}:token-{role_id}@192.168.5.2:23456"

    def reload_hosts(self, timeout):
        if not self.live:
            return None
        self.calls.append("hosts")
        self.timeouts.append(timeout)
        if self.fail:
            raise RuntimeError(self.fail)
        return {}


class FakeLinux:
    """The Linux VM boots with the firm; the guest controller answers run requests."""

    def __init__(self, settings):
        root = self.root = settings.root
        self.firm_locks = settings.firm_locks
        self.settings = settings
        self.commands = object()
        self.ssh_config = root / "lima" / "linux" / "ssh.config"
        self.paths = SimpleNamespace(verify_runtime=lambda: None)
        self.socket_root = root
        self.network = FakeNetwork()
        self.calls = []
        self.running = False
        self.flushed = True
        self.rows = []
        self.logs = {}
        self.generation = "a" * 32

    def _state(self):
        return {"generation": self.generation, "ready": True}

    def _running(self):
        return self.running

    def _check_run(self, run):
        if run.vm_generation != self.generation:
            raise ValueError("Run belongs to a discarded VM generation")

    def up(self):
        self.calls.append("up")
        self.running = True
        return {"running": True}

    def down(self):
        self.calls.append("down")
        self.running = False

    def force_down(self):
        self.calls.append("force-down")
        self.running = False

    def flush(self, *, timeout=15):
        self.calls.append("flush")
        return self.flushed

    def remove(self):
        self.calls.append("remove")

    def _rpc(self, operation, **payload):
        self.calls.append((operation, payload))
        if operation == "runs":
            return self.rows
        if operation == "start":
            if not any(row["run_id"] == payload["run_id"] for row in self.rows):
                self.rows.append(
                    dict(role_id=payload["role_id"], run_id=payload["run_id"], running=True)
                )
            return {}
        if operation in {"status", "stop"}:
            row = next(row for row in self.rows if row["run_id"] == payload["run_id"])
            if operation == "stop":
                row.update(running=False, exit_code=137)
            return row
        if operation == "logs":
            data = self.logs.get(payload["run_id"], b"")
            chunk = data[payload["offset"] : payload["offset"] + payload["limit"]]
            return {"data": base64.b64encode(chunk).decode()}
        if operation == "write-credential":
            return {"path": f"/run/firm/{payload['role_id']}/credentials/{payload['name']}"}
        return {}

    def storage(self, role_id=None):
        from firm_sandbox.guest_control import GuestControl

        return GuestControl.storage(self, role_id)

    def dispose(self, role_id):
        from firm_sandbox.guest_control import GuestControl

        return GuestControl.dispose(self, role_id)

    def rpc(self, operation, **payload):
        return self._rpc(operation, **payload)


class FakeMac:
    def __init__(self, settings, image):
        root = self.root = settings.root
        self.image = image
        self.stations = (1,)
        self.firm_locks = settings.firm_locks
        self.settings = settings
        self.instance = root / "lima" / "mac"
        self.commands = object()
        self.ssh_config = root / "lima" / "mac" / "ssh.config"
        self.paths = SimpleNamespace(verify_runtime=lambda: None)
        self.socket_root = root
        self.network = FakeNetwork()
        self.calls = []
        self.running = False
        self.controller_intact = True

    def _state(self):
        return {
            "generation": "m" * 32,
            "ready": True,
            "stations": len(self.stations),
            "enrolled": self.enrolled(),
        }

    def enrolled(self):
        image = self.image / "image.json" if self.image is not None else None
        return bool(image and image.is_file() and "enrollment" in json.loads(image.read_text()))

    def _running(self):
        return self.running

    def controller_installed(self):
        return self.controller_intact

    def boot_script_installed(self):
        return True

    def up(self, *, checkpoint=lambda: None):
        checkpoint()
        self.running = True

    def down(self):
        self.running = False

    def force_down(self):
        self.running = False

    def remove(self):
        self.calls.append("remove")

    def ssh_port(self):
        return 60022

    def rpc(self, operation, **payload):
        return self._rpc(operation, **payload)

    def _rpc(self, operation, **payload):
        self.calls.append((operation, payload))
        if operation == "station-session-ensure":
            station = payload["station"]
            return {
                "station": station,
                "user": f"firmstation{station}",
                "uid": 501 + station,
                "ready": True,
                "created": False,
                "helper": {"ready": True},
            }
        if operation == "station-keep-awake":
            # What the guest reads back from a desktop that will neither lock nor sleep.
            return {"screen_lock": "screenLock is off", "screen_saver_idle": 0,
                    "relock_at_login": False,
                    "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0}}
        return {}


class FakeFiles:
    def __init__(self):
        self.calls = []
        self.ready = True

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def reconnect(self):
        self.calls.append("reconnect")

    def require_ready(self):
        if not self.ready:
            raise RuntimeError("files-mac connection is unavailable")

    def credentials(self, role_id):
        from firm_sandbox.file_server import Credentials

        return Credentials("firmfile-" + role_id, "secret-" + role_id, 4450)

    def address(self, role_id):
        return "founder/" + role_id

    def retire_role(self, role_id):
        self.calls.append(("retire", role_id))

    def discard(self):
        self.calls.append("discard")

    def owner_access(self):
        return {"url": "smb://firmowner@127.0.0.1:4450/firm"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    initialize_firm_record(tmp_path, release_identifier())
    monkeypatch.setattr(sandbox, "LinuxVM", FakeLinux)
    monkeypatch.setattr(sandbox, "MacVM", FakeMac)
    box = Sandbox(Config(tmp_path, mac_image="/private/base"))
    box._file_server = FakeFiles()
    return box


@pytest.fixture
def two_stations(tmp_path, monkeypatch):
    """A firm whose one Mac holds two station accounts."""
    initialize_firm_record(tmp_path, release_identifier())
    monkeypatch.setattr(sandbox, "LinuxVM", FakeLinux)
    monkeypatch.setattr(sandbox, "MacVM", FakeMac)
    box = Sandbox(Config(tmp_path, mac_image="/private/base"))
    box.mac.stations = (1, 2)
    box._file_server = FakeFiles()
    return box


def test_facade_uses_the_linux_layout_without_creating_host_data(box):
    assert box.linux.root == box.config.durable / "linux"
    assert box.mac.root == box.config.durable / "mac"
    assert box.root == box.linux.root
    assert box.layout("analyst") == Layout.for_role("analyst")
    assert box.layout("analyst").private.startswith("/home/firmrole-")
    assert box.layout("analyst").python == "/usr/bin/python3"
    assert box.layout("analyst").tools == "/usr/local/bin"
    assert box.layout("analyst").credentials == "/run/firm/analyst/credentials"
    assert box.host_address() == "127.0.0.1"
    assert not (box.config.root / "data").exists()
    assert (box.linux.settings.cpus, box.mac.settings.cpus) == (8, 8)
    assert box.linux.firm_locks == box.mac.firm_locks == box.config.locks


# ---- a firm without [mac] ------------------------------------------------------------


def test_config_without_mac_has_no_station_image(tmp_path):
    path = tmp_path / "sandbox.toml"
    path.write_text('[firm]\nid = "demo"\n')
    assert Config.load(path).mac_image is None
    path.write_text('[firm]\nid = "demo"\n[mac]\ncpus = 4\n')
    with pytest.raises(ValueError, match="\\[mac\\] must name the station image"):
        Config.load(path)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "60"])
def test_linux_disk_size_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(ValueError, match="VM resource"):
        Config(tmp_path, disk_gib=value)


def test_linux_disk_size_is_configured_and_passed_to_the_vm(tmp_path):
    path = tmp_path / "sandbox.toml"
    path.write_text("[firm]\ndisk_gib = 120\n")
    box = Sandbox(Config.load(path))
    assert box.config.disk_gib == box.linux.settings.disk_gib == 120
    path.write_text("[firm]\n")
    assert Config.load(path).disk_gib == 60


def test_config_reads_the_egress_mode_and_allowed_hosts(tmp_path):
    path = tmp_path / "sandbox.toml"
    path.write_text('[network]\negress = "observe"\nallow_hosts = ["b.example", "*.a.example"]\n')
    config = Config.load(path)
    assert (config.egress_mode, config.allow_hosts) == ("observe", ("*.a.example", "b.example"))
    box = Sandbox(config)
    assert box.linux.settings.allow_hosts == config.allow_hosts
    assert box.linux.settings.egress_mode == "observe"
    path.write_text("")
    assert (Config.load(path).egress_mode, Config.load(path).allow_hosts) == ("enforce", ())


@pytest.mark.parametrize(("setting", "message"), [
    ('allow_hosts = ["example.com:443"]', "Allowed hosts must be"),
    ('allow_hosts = ["1.2.3.4"]', "Allowed hosts must be"),
    ('allow_hosts = ["*.com"]', "Allowed hosts must be"),
    ('allow_hosts = ["Example.com"]', "Allowed hosts must be"),
    ('allow_hosts = "example.com"', "must be a list"),
    ('egress = "open"', "enforce, observe"),
    ('allow_tcp = ["1.2.3.4:443"]', "Unknown network setting: allow_tcp"),
])
def test_config_explains_malformed_network_settings(tmp_path, setting, message):
    path = tmp_path / "sandbox.toml"
    path.write_text(f"[network]\n{setting}\n")
    with pytest.raises(ValueError, match=message):
        Config.load(path)


def test_config_uses_shared_mac_resources_and_rejects_station_count(tmp_path):
    path = tmp_path / "sandbox.toml"
    path.write_text('[mac]\nimage = "/private/base"\n')
    config = Config.load(path)
    assert (config.mac_cpus, config.mac_memory_gib) == (8, 24)
    assert config.memory_gib == 16
    path.write_text('[mac]\nimage = "/private/base"\nstations = 1\n')
    with pytest.raises(ValueError, match="Unknown mac setting: stations"):
        Config.load(path)


@pytest.fixture
def linux_only(tmp_path, monkeypatch):
    """A firm whose sandbox.toml has no [mac]: one VM and no station."""
    initialize_firm_record(tmp_path, release_identifier())
    monkeypatch.setattr(sandbox, "LinuxVM", FakeLinux)
    box = Sandbox(Config(tmp_path))
    box.linux.network.traffic = lambda: {"live": True, "egress": True}
    box.linux.network.egress_summary = lambda: {"live": True, "entries": [], "overflow": 0}
    return box


def test_linux_only_firm_boots_one_vm_and_forwards_the_share_to_the_host_only(
    linux_only, monkeypatch
):
    box = linux_only
    assert box.mac is None
    assert [tunnel.name for tunnel in box.file_server.forwards(4450)] == ["files-owner"]
    FakeTunnel.log = []
    monkeypatch.setattr("firm_sandbox.tunnel.Tunnel", FakeTunnel)
    box._file_server = FakeFiles()
    box.up()
    assert box.linux.calls == ["up"] and box._file_server.calls == ["start"]
    assert FakeTunnel.log == []
    status = box.infrastructure_status()
    assert (status["state"], status["running"], status["errors"]) == ("running", True, {})
    assert list(box.network_status()["vms"]) == ["linux"]
    assert box.data_status()["storage"] == "linux-vm"
    box.down()
    assert box.infrastructure_status()["state"] == "stopped"
    assert [call for call in box.linux.calls if isinstance(call, str)] == ["up", "down"]
    assert box._file_server.calls == ["start", "stop"]
    box.linux.running = True
    assert box.force_down() == {"running": False}
    assert box.linux.calls[-1] == "force-down" and not box.linux.running
    kinds = [event["kind"] for event in box.events()]
    assert kinds == ["firm.started", "firm.stopped", "firm.stopped"]


def test_linux_only_firm_has_no_mac_to_lease(linux_only, monkeypatch):
    box = linux_only
    box._file_server = FakeFiles()
    box.up()
    FakeTunnel.log = []
    monkeypatch.setattr("firm_sandbox.tunnel.Tunnel", FakeTunnel)
    assert box.mac_status() == {"configured": False, "enrolled": False, "stations": []}
    assert box.mac_acquire("r-e1", "activation-1") == {
        "status": "unavailable", "reason": "no_mac",
    }
    assert FakeTunnel.log == []
    assert not any(operation == "write-credential" for operation, _ in
                   (call for call in box.linux.calls if isinstance(call, tuple)))
    for call in (lambda: box.mac_release("0" * 32), lambda: box.mac_connect("0" * 32)):
        with pytest.raises(RuntimeError, match="add \\[mac\\] to sandbox.toml"):
            call()
    assert [event["kind"] for event in box.events()] == ["firm.started"]


def test_linux_only_network_control_and_replace_touch_only_the_linux_vm(
    linux_only, monkeypatch
):
    box = linux_only
    box._file_server = FakeFiles()
    box.linux.network.prepare_egress = lambda enabled: {"egress": enabled, "live": False}
    assert box.set_network_enabled(False) == {"linux": {"egress": False, "live": False}}
    assert box.set_allowed_hosts(["pkg.example"]) == {"hosts": ["pkg.example"]}
    assert box.granted_hosts() == ["pkg.example"]
    assert not (box.config.durable / "mac").exists()
    calls = []
    monkeypatch.setattr(box, "down", lambda: calls.append("down"))
    monkeypatch.setattr(box, "up", lambda: calls.append("up") or {"replaced": True})
    assert box.replace() == {"replaced": True}
    assert calls == ["down", "up"]
    assert box.linux.calls == ["remove"] and box._file_server.calls == ["discard"]


def test_resources_sample_running_vms_and_leave_out_the_rest(box, monkeypatch):
    monkeypatch.setattr(sandbox, "host_sample", lambda: {"boot": "host", "cpus": 10})
    monkeypatch.setattr(sandbox, "vm_process", lambda vm: {"host_footprint_bytes": 1 << 30})
    assert box.resources() == {
        "host": {"boot": "host", "cpus": 10}, "linux": None, "mac": None,
    }
    box.linux.running = True
    box.mac.running = True

    def unreachable(operation, **payload):
        raise RuntimeError("VM network boundary is unavailable")

    box.mac._rpc = unreachable
    box.mac.rpc = unreachable
    box.linux.rpc = lambda operation, **payload: {"operation": operation, **payload}
    observed = box.resources()
    assert observed["mac"] is None
    assert observed["linux"] == {
        "operation": "sample", "timeout": 10, "host_footprint_bytes": 1 << 30
    }


def test_up_boots_both_vms_before_the_file_server(box):
    events = []
    box.mac.up = lambda **_: events.append("mac") or setattr(box.mac, "running", True)
    box._file_server.start = lambda: events.append("files")
    box.up()
    assert box.linux.calls == ["up"] and events == ["mac", "files"]
    assert box.infrastructure_status()["state"] == "running"
    box.down()
    assert box._file_server.calls == ["stop"]
    assert [call for call in box.linux.calls if isinstance(call, str)] == ["up", "down"]
    assert not box.mac.running
    assert box.infrastructure_status()["state"] == "stopped"


def test_file_forwards_start_only_after_both_vm_generations_exist(box, monkeypatch):
    from firm_sandbox.tunnel import Tunnel

    box._file_server = None
    seen = []
    mac_state = box.mac._state

    def existing_mac_state():
        if not box.mac.running:
            raise FileNotFoundError(box.mac.root / "vm.json")
        return mac_state()

    def start_files():
        for tunnel in box.file_server.forwards(4450):
            assert isinstance(tunnel, Tunnel)
            seen.append((tunnel.name, tunnel._identity()["generation"]))

    monkeypatch.setattr(box.mac, "_state", existing_mac_state)
    monkeypatch.setattr(box.file_server, "start", start_files)
    box.up()
    assert seen == [("files-owner", "a" * 32), ("files-mac", "m" * 32)]


def test_linux_failure_during_up_powers_off_partial_boot(box):
    def broken():
        box.linux.running = True
        raise RuntimeError("guest initialization failed")

    box.linux.up = broken
    with pytest.raises(RuntimeError, match="guest initialization"):
        box.up()
    assert box.linux.calls == ["force-down"]
    assert not box.linux.running and not box.mac.running
    assert box._file_server.calls == ["stop"]
    assert box._pending_boots == 0


def test_mac_failure_during_up_powers_everything_off(box):
    def broken(**_):
        box.mac.running = True
        raise RuntimeError("station image is missing")

    box.mac.up = broken
    with pytest.raises(RuntimeError, match="station image"):
        box.up()
    assert box.linux.calls == ["up", "force-down"]
    assert box._file_server.calls == ["stop"]
    assert not box.linux.running and not box.mac.running


def test_file_server_failure_during_up_stops_both_vms_and_reports_cleanup_errors(box):
    def broken():
        assert box.linux.running and box.mac.running
        raise RuntimeError("files-mac connection failed")

    def failed_close():
        raise RuntimeError("forward cleanup failed")

    box._file_server.start = broken
    box._file_server.stop = failed_close
    with pytest.raises(RuntimeError, match="files-mac") as failure:
        box.up()
    assert not box.linux.running and not box.mac.running
    assert failure.value.__notes__ == ["Startup cleanup failed: forward cleanup failed"]
    assert box._pending_boots == 0


def test_runtime_tunnel_binds_to_the_linux_vm_and_its_failure_powers_off(box, monkeypatch):
    box.configure_runtime(8765)
    seen = []

    def broken(vm, port):
        seen.append((vm, port))
        raise RuntimeError("port is bound elsewhere")

    monkeypatch.setattr("firm_sandbox.tunnel.runtime_tunnel", broken)
    with pytest.raises(RuntimeError, match="bound elsewhere"):
        box.up()
    assert seen == [(box.linux, 8765)]
    assert box.linux.calls == ["up", "force-down"]
    assert box._file_server.calls == ["start", "stop"]


@pytest.mark.parametrize("flushed", [True, False])
def test_down_with_unavailable_network_flushes_then_powers_off_both_vms(box, monkeypatch, flushed):
    # Losing the boundary forfeits a graceful guest shutdown. The roles' writes
    # are committed first so a stop cannot truncate their work, and the event
    # says whether that succeeded.
    calls = []
    box.linux.running = True
    box.linux.flushed = flushed
    monkeypatch.setattr(box.linux.network, "ready", lambda: False)
    monkeypatch.setattr(box.linux, "force_down", lambda: calls.append("linux"))
    monkeypatch.setattr(box.mac, "force_down", lambda: calls.append("mac"))
    assert box.down() == {"running": False}
    assert calls == ["mac", "linux"]
    assert box.linux.calls == ["flush"]
    stopped = [json.loads(line) for line in
               (box.config.durable / "events.jsonl").read_text().splitlines()]
    assert stopped[-1]["kind"] == "firm.stopped"
    assert stopped[-1]["forced"] is True and stopped[-1]["flushed"] is flushed


def test_down_cannot_verify_expected_runs_without_network(box, monkeypatch):
    box.linux.running = True
    monkeypatch.setattr(box.linux.network, "ready", lambda: False)
    with pytest.raises(RuntimeError, match="Cannot verify active execution"):
        box.down(expected_runs=set())
    assert box.linux.running
    assert box.linux.calls == []


def test_disconnected_down_cancels_queued_launch_without_waiting_under_lock(box, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    checking_network, launch_queued = Event(), Event()
    box.linux.running = True
    original_start = box._start

    def disconnected():
        checking_network.set()
        assert launch_queued.wait(2)
        return False

    def queued_start(spec):
        launch_queued.set()
        return original_start(spec)

    monkeypatch.setattr(box.linux.network, "ready", disconnected)
    monkeypatch.setattr(box, "_start", queued_start)
    with ThreadPoolExecutor(max_workers=2) as pool:
        shutdown = pool.submit(box.down)
        assert checking_network.wait(2)
        launch = pool.submit(box.start, RunSpec("analyst", ("true",)))
        assert shutdown.result(timeout=2) == {"running": False}
        with pytest.raises(RuntimeError, match="force-stopped"):
            launch.result(timeout=2)
    assert box._pending_launches == 0
    assert box.linux.calls == ["flush", "force-down"]


def test_cutoff_covers_the_mac_even_when_linux_acknowledgement_fails(box):
    class Network:
        def __init__(self, fail=False):
            self.fail, self.calls = fail, []

        def set_egress(self, enabled):
            self.calls.append(("live", enabled))
            if self.fail:
                raise RuntimeError("no acknowledgement")
            return {"egress": enabled}

        def prepare_egress(self, enabled):
            self.calls.append(("saved", enabled))
            return {"egress": enabled, "live": False}

    box.linux.network = Network(fail=True)
    box.linux.running = True
    box.mac.network = Network()
    box.mac.running = True
    with pytest.raises(RuntimeError, match="linux: no acknowledgement"):
        box.set_network_enabled(False)
    assert box.mac.network.calls == [("live", False)]
    box.linux.running = box.mac.running = False
    assert box.set_network_enabled(False) == {
        "linux": {"egress": False, "live": False},
        "mac": {"egress": False, "live": False},
    }


def test_allowed_hosts_reach_every_vm_file_and_every_running_proxy(box):
    box.linux.running = box.mac.running = True
    assert box.set_allowed_hosts(["b.example", "*.a.example", "b.example"]) == {
        "hosts": ["*.a.example", "b.example"],
    }
    for vm in (box.linux, box.mac):
        assert json.loads((vm.root / "hosts.json").read_text()) == {
            "hosts": ["*.a.example", "b.example"],
        }
        assert vm.network.calls == ["hosts"]
    (event,) = [event for event in box.events() if event["kind"] == "network.hosts_changed"]
    assert (event["hosts"], event["failures"], event["applied"]) == (
        ["*.a.example", "b.example"], [], True,
    )
    # A VM without a running filter is only written; its proxy reads the file when it starts.
    box.mac.network.live = False
    box.set_allowed_hosts([])
    assert box.mac.network.calls == ["hosts"] and box.linux.network.calls == ["hosts", "hosts"]
    assert json.loads((box.mac.root / "hosts.json").read_text()) == {"hosts": []}
    # Every confirmation is asked within what is left of the call's bound.
    assert all(0 < timeout <= sandbox.HOSTS_TIMEOUT for timeout in box.linux.network.timeouts)


def test_allowed_hosts_take_the_shared_lifecycle_lock_and_never_wait_long_for_it(box):
    import fcntl
    import time

    from firm_sandbox.locking import LIFECYCLE, LockBusy

    box.config.locks.mkdir(mode=0o700, exist_ok=True)
    with (box.config.locks / LIFECYCLE).open("a") as other:
        # Beside a run or an observation, which hold the lock shared, it goes ahead.
        fcntl.flock(other, fcntl.LOCK_SH)
        assert box.set_allowed_hosts(["pkg.example"]) == {"hosts": ["pkg.example"]}
        fcntl.flock(other, fcntl.LOCK_UN)
        # Behind up, down, replace or discard, which hold it exclusively, it is refused
        # within its short wait and changes nothing.
        fcntl.flock(other, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(LockBusy, match="busy with a lifecycle operation"):
            box.set_allowed_hosts(["other.example"])
        assert time.monotonic() - started < sandbox.HOSTS_LOCK_WAIT + 1
        fcntl.flock(other, fcntl.LOCK_UN)
    assert box.granted_hosts() == ["pkg.example"]
    assert box.events()[-1]["hosts"] == ["pkg.example"]


def test_a_filter_that_does_not_answer_fails_the_call_within_its_bound(box, tmp_path):
    """The Mac's filter accepts the command and never replies; the call still ends soon."""
    import socket
    import time
    from uuid import uuid4

    from firm_sandbox.host_network import HostNetwork
    from firm_sandbox.models import VMSettings

    runtime = Path("/tmp").resolve() / f"sb-{uuid4().hex[:8]}"
    runtime.mkdir(mode=0o700)
    try:
        network = HostNetwork(
            VMSettings(box.mac.root, firm_locks=box.config.locks, firm_id="firm", cpus=1,
                       memory_gib=1),
            SimpleNamespace(env={}), socket_root=runtime, proxy_role="mac",
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
            silent.bind(str(network.paths["control"]))
            silent.listen()
            box.mac.network = network
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="mac: Allowed hosts change was not confirmed"):
                box.set_allowed_hosts(["new.example"])
            elapsed = time.monotonic() - started
    finally:
        shutil.rmtree(runtime)
    assert elapsed < sandbox.HOSTS_TIMEOUT + sandbox.HOSTS_RESTORE_TIMEOUT * 2 + 1
    # The restore put both VMs back: there was no list before.
    assert not (box.linux.root / "hosts.json").exists()
    assert not (box.mac.root / "hosts.json").exists()
    assert box.linux.network.calls == ["hosts", "hosts"]
    assert box.events()[-1]["applied"] is False


def test_a_vm_that_cannot_take_the_hosts_puts_every_vm_back_on_the_previous_list(box):
    box.linux.running = box.mac.running = True
    box.set_allowed_hosts(["old.example"])
    for vm in (box.linux, box.mac):
        vm.network.calls.clear()
    # Linux takes the new list, then the Mac's proxy does not confirm it.
    box.mac.network.fail = "Allowed hosts change was not confirmed"
    # The Mac's restore reload fails too; the files are what count, and they are back.
    with pytest.raises(RuntimeError, match="mac: Allowed hosts change was not confirmed; "
                                           "the previous hosts files were restored"):
        box.set_allowed_hosts(["new.example"])
    for vm in (box.linux, box.mac):
        assert json.loads((vm.root / "hosts.json").read_text()) == {"hosts": ["old.example"]}
        assert oct((vm.root / "hosts.json").stat().st_mode & 0o777) == "0o600"
    # Each proxy was asked again after the restore (the Mac's still fails, best effort).
    assert box.linux.network.calls == ["hosts", "hosts"]
    assert box.mac.network.calls == ["hosts", "hosts"]
    event = box.events()[-1]
    assert event["kind"] == "network.hosts_changed"
    assert (event["hosts"], event["applied"]) == (["new.example"], False)
    assert event["failures"] == ["mac: Allowed hosts change was not confirmed"]
    assert box.granted_hosts() == ["old.example"]


def test_a_restore_that_cannot_write_its_file_is_named_in_the_failure(box, monkeypatch):
    box.set_allowed_hosts(["old.example"])
    box.mac.network.fail = "Allowed hosts change was not confirmed"
    restore = sandbox.Sandbox._restore_hosts
    monkeypatch.setattr(
        sandbox.Sandbox, "_restore_hosts",
        staticmethod(lambda vm, content: vm is not box.mac and restore(vm, content)),
    )
    with pytest.raises(RuntimeError, match="could not be put back on mac"):
        box.set_allowed_hosts(["new.example"])


def test_an_event_log_that_cannot_be_written_never_undoes_an_applied_change(box):
    events = box.config.durable / "events.jsonl"
    events.unlink(missing_ok=True)
    events.mkdir()  # Any append now fails.
    assert box.set_allowed_hosts(["pkg.example"]) == {"hosts": ["pkg.example"]}
    for vm in (box.linux, box.mac):
        assert json.loads((vm.root / "hosts.json").read_text()) == {"hosts": ["pkg.example"]}
    box.mac.network.fail = "Allowed hosts change was not confirmed"
    with pytest.raises(RuntimeError, match="Allowed hosts change was not confirmed"):
        box.set_allowed_hosts(["other.example"])
    assert box.granted_hosts() == ["pkg.example"]


def test_a_first_grant_that_fails_leaves_no_hosts_file_behind(box):
    box.linux.running = True
    box.linux.network.fail = "Allowed hosts change was not confirmed"
    with pytest.raises(RuntimeError, match="linux: Allowed hosts change was not confirmed"):
        box.set_allowed_hosts(["pkg.example"])
    assert not (box.linux.root / "hosts.json").exists()
    assert not (box.mac.root / "hosts.json").exists()
    assert box.granted_hosts() == []


@pytest.mark.parametrize("hosts", [["1.2.3.4"], ["Pkg.example"], ["*.com"], "pkg.example"])
def test_invalid_allowed_hosts_change_nothing(box, hosts):
    with pytest.raises(ValueError):
        box.set_allowed_hosts(hosts)
    assert not (box.linux.root / "hosts.json").exists() and box.events() == []


def test_network_status_reports_the_firm_and_each_vm(box):
    box.linux.root.mkdir(parents=True)
    (box.linux.root / "hosts.json").write_text(json.dumps({"hosts": ["pkg.example"]}))
    linux, mac = box.linux.network, box.mac.network
    entry = {"role": "analyst", "host": "pypi.org", "requests": 2, "denied": 1,
             "bytes_up": 10, "bytes_down": 20, "last_seen": 1.0}
    linux.traffic = lambda: {"live": True, "egress": True, "destinations": []}
    linux.egress_summary = lambda: {"live": True, "entries": [entry], "overflow": 3}
    mac.traffic = lambda: {"live": True, "egress": True, "destinations": []}
    mac.egress_summary = lambda: {"live": True, "entries": [{**entry, "role": "mac"}],
                                  "overflow": 1}
    status = box.network_status()
    assert set(status) == {"enabled", "mode", "allowed_hosts", "egress", "vms"}
    assert (status["enabled"], status["mode"]) == (True, "enforce")
    assert status["egress"] == {
        "live": True, "overflow": 4,
        "entries": [{**entry, "vm": "linux"}, {**entry, "role": "mac", "vm": "mac"}],
    }
    sources = {row["host"]: row["source"] for row in status["allowed_hosts"]}
    assert sources["*.anthropic.com"] == "baseline" and sources["pkg.example"] == "granted"
    assert status["vms"]["linux"]["traffic"]["destinations"] == []

    # A stopped Mac answers from its saved switch; its tallies are unavailable.
    def unavailable():
        raise RuntimeError("Network observation is unavailable")

    linux.traffic = lambda: {"live": True, "egress": False, "destinations": []}
    mac.traffic = mac.egress_summary = unavailable
    mac.saved_egress = lambda: True
    status = box.network_status()
    assert status["enabled"] is False
    assert (status["vms"]["linux"]["enabled"], status["vms"]["mac"]["enabled"]) == (False, True)
    assert status["vms"]["mac"]["egress"] == status["vms"]["mac"]["traffic"] == {
        "live": False, "available": False,
    }
    assert status["egress"]["live"] is False
    assert status["egress"]["entries"] == [{**entry, "vm": "linux"}]


def test_configured_hosts_are_reported_once_by_their_first_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "LinuxVM", FakeLinux)
    box = Sandbox(Config(tmp_path, allow_hosts=("github.com", "docs.example")))
    box._file_server = FakeFiles()
    box.set_allowed_hosts(["docs.example", "granted.example"])
    rows = box.allowed_hosts()
    assert not any(row["host"] == "storage.googleapis.com" for row in rows)
    assert [row for row in rows if row["host"] in {"github.com", "docs.example",
                                                   "granted.example"}] == [
        {"host": "github.com", "source": "baseline"},
        {"host": "docs.example", "source": "config"},
        {"host": "granted.example", "source": "granted"},
    ]
    assert len(rows) == len({row["host"] for row in rows})


def test_organizational_launch_carries_the_drive_and_a_plain_run_does_not(box):
    spec = RunSpec("founder", ("true",), run_id="run-a", organization=Organization("founder"))
    box.start(spec)
    prepared = [value for operation, value in box.linux.calls if operation == "start"]
    assert prepared[0]["drive"] == {
        "user": "firmfile-founder", "password": "secret-founder", "port": 4450, "share": "firm",
    }
    assert prepared[0]["organization"] == "founder"
    assert set(prepared[0]["secrets"]) == {"egress-proxy"}
    box._file_server.ready = False
    with pytest.raises(RuntimeError, match="files-mac"):
        box.start(RunSpec("other", ("true",), run_id="run-b", organization=Organization("other")))
    box.start(RunSpec("summary", ("true",), run_id="run-c"))
    assert box.linux.calls[-1][1]["drive"] is None


def test_runtime_grants_and_secrets_use_the_control_channel(box):
    spec = RunSpec(
        "analyst", ("true",), run_id="run-runtime", organization=Organization("team/analyst"),
        secrets={"token": b"private-value"}, secret_env={"TOKEN": "token"},
    )
    run = box.start(spec)
    operation, payload = box.linux.calls[-1]
    assert operation == "start"
    assert payload["organization"] == "team/analyst"
    url = base64.b64encode(box.linux.network.proxy_url("analyst").encode()).decode()
    assert payload["secrets"] == {"token": "cHJpdmF0ZS12YWx1ZQ==", "egress-proxy": url}
    assert payload["secret_env"] == {
        "TOKEN": "token", "HTTPS_PROXY": "egress-proxy", "HTTP_PROXY": "egress-proxy",
        "https_proxy": "egress-proxy", "http_proxy": "egress-proxy",
    }
    assert payload["argv"] == ["true"]
    assert payload["env"] == {"NO_PROXY": "localhost,127.0.0.1,::1",
                              "no_proxy": "localhost,127.0.0.1,::1"}
    assert run.role_id == "analyst" and run.run_id == "run-runtime"


def test_a_run_carries_its_roles_proxy_credential_but_never_the_key_or_the_url_in_events(
    box, tmp_path
):
    from firm_sandbox.host_network import HostNetwork
    from firm_sandbox.models import VMSettings

    root = tmp_path / "vm"
    network = HostNetwork(
        VMSettings(root, firm_locks=tmp_path / "locks", firm_id="firm", cpus=1, memory_gib=1),
        SimpleNamespace(env={}), socket_root=tmp_path / "runtime",
    )
    box.linux.network = network
    with pytest.raises(RuntimeError, match="proxy key missing"):
        box.start(RunSpec("analyst", ("true",), run_id="run-none"))
    network.create_key()
    box.start(RunSpec("analyst", ("true",), run_id="run-a",
                      env={"HTTPS_PROXY": "http://elsewhere.example:1"}))
    _, payload = box.linux.calls[-1]
    url = base64.b64decode(payload["secrets"]["egress-proxy"]).decode()
    assert url == network.proxy_url("analyst") and url.startswith("http://analyst:")
    # The run's own proxy setting gives way: the guest applies secret_env last.
    assert payload["secret_env"]["HTTPS_PROXY"] == "egress-proxy"
    key = (root / "proxy.key").read_bytes()
    sent = json.dumps(payload)
    for encoding in (key.hex(), base64.b64encode(key).decode()):
        assert encoding not in sent
    token = url.split(":")[2].split("@")[0]
    assert token not in json.dumps(box.events())
    with pytest.raises(ValueError, match="reserved"):
        box.start(RunSpec("analyst", ("true",), run_id="run-b",
                          secrets={"egress-proxy": b"mine"}))

@pytest.mark.parametrize("clean_up", [False, True])
def test_lost_start_reply_retries_the_same_run_identity(box, clean_up):
    from firm_sandbox.guest_control import ControlConnectionError

    calls = []
    original = box.linux._rpc

    def rpc(operation, **payload):
        calls.append((operation, payload))
        if len(calls) == 1:
            raise ControlConnectionError("reply lost after launch")
        return original(operation, **payload)

    box.linux._rpc = rpc
    run = box.start(RunSpec("analyst", ("true",), clean_up=clean_up))
    # The same request, but each send carries the host's clock as of that send.
    times = [payload.pop("host_time") for _, payload in calls]
    assert times[0] <= times[1] <= time.time()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1]["run_id"] == run.run_id
    assert calls[0][1]["clean_up"] is clean_up

    def lost(operation, **payload):
        raise TimeoutError("connection lost")

    box.linux._rpc = lost
    with pytest.raises(RuntimeError, match="run-known.*ps/stop or down"):
        box.start(RunSpec("analyst", ("true",), run_id="run-known"))


def test_retirement_revokes_the_guest_account_and_the_file_server_login(box):
    box.retire("analyst")
    assert ("retire", {"role_id": "analyst"}) in box.linux.calls
    assert box._file_server.calls == [("retire", "analyst")]
    box.start(RunSpec("active", ("true",), run_id="run-a"))
    with pytest.raises(ValueError, match="Stop and resolve"):
        box.retire("active")
    assert not any(call == ("retire", {"role_id": "active"}) for call in box.linux.calls)


def test_data_status_names_the_owner_mount(box):
    status = box.data_status()
    assert status["storage"] == "linux-vm"
    assert status["url"] == "smb://firmowner@127.0.0.1:4450/firm"
    layout = box.layout("analyst")
    assert layout.organization == layout.drive + "/org"
    assert layout.workspace("a/b") == layout.drive + "/org/a/b/work"


def test_emergency_shutdown_attempts_linux_even_if_the_mac_stop_fails(box):
    calls = []

    def fail():
        calls.append("mac")
        raise RuntimeError("macOS did not stop")

    box.mac.force_down = fail
    box.linux.force_down = lambda: calls.append("linux")
    box._close_runtime_tunnel = lambda: calls.append("runtime-tunnel")
    with pytest.raises(RuntimeError, match="macOS did not stop"):
        box.down(force=True)
    assert calls == ["mac", "linux", "runtime-tunnel"]


def test_shutdown_refuses_foreign_active_run_but_independent_stop_works(box):
    box.up()
    first = box.start(RunSpec("first", ("true",), run_id="run-a"))
    second = box.start(RunSpec("second", ("true",), run_id="run-b"))
    with pytest.raises(RuntimeError, match="unrecognized"):
        box.down(expected_runs=(first,))
    assert box.infrastructure_status()["running"]
    box.stop(first)
    assert box.status(second)["running"]
    box.down(expected_runs=(first, second))
    assert not box.infrastructure_status()["running"]
    assert all(not row["running"] for row in box.linux.rows)


def test_vm_generation_and_run_ids_are_preserved_in_inventory(box):
    box.up()
    run = box.start(RunSpec("analyst", ("true",), run_id="run-fixed"))
    assert box.runs() == [run]
    assert box.current("analyst") == run
    assert box.identity() == Sandbox(box.config).identity()
    from firm_sandbox.models import Run

    with pytest.raises(ValueError, match="discarded VM generation"):
        box.status(Run("analyst", "run-old", "generation-old"))


def test_inventory_carries_each_runs_status_from_one_runs_call(box):
    box.up()
    first = box.start(RunSpec("analyst", ("true",), run_id="run-a"))
    second = box.start(RunSpec("writer", ("true",), run_id="run-b"))
    box.linux.calls.clear()
    inventory = box.inventory()
    assert [operation for operation, _ in box.linux.calls] == ["runs"]
    assert set(inventory) == {"run-a", "run-b"}
    run, info = inventory["run-a"]
    assert run == first and info["running"] is True
    assert info["observation"] == "guest-controller"
    assert inventory["run-b"][0] == second


def test_logs_reads_the_whole_log_without_an_offset(box):
    run = box.start(RunSpec("analyst", ("true",), run_id="run-a"))
    box.linux.logs[run.run_id] = b"line one\nline two\n"
    assert box.logs(run) == b"line one\nline two\n"


def test_logs_with_an_offset_reads_only_the_remaining_tail(box):
    run = box.start(RunSpec("analyst", ("true",), run_id="run-a"))
    box.linux.logs[run.run_id] = b"0123456789"
    box.linux.calls.clear()
    assert box.logs(run, offset=4) == b"456789"
    reads = [payload for operation, payload in box.linux.calls if operation == "logs"]
    assert reads[0]["offset"] == 4
    assert len(reads) == 2  # the tail, then the empty page that says it ended


def test_logs_bounded_by_a_known_size_never_asks_for_the_empty_page(box):
    run = box.start(RunSpec("analyst", ("true",), run_id="run-a"))
    box.linux.logs[run.run_id] = b"0123456789"
    box.linux.calls.clear()
    assert box.logs(run, offset=4, until=10) == b"456789"
    assert [op for op, _ in box.linux.calls if op == "logs"] == ["logs"]


def test_logs_excludes_growth_past_a_known_size(box):
    # The log grows past ``until`` after the caller's stat but before this read;
    # the bound must still hold even though the guest now has more to offer.
    run = box.start(RunSpec("analyst", ("true",), run_id="run-a"))
    box.linux.logs[run.run_id] = b"0123456789" + b"grown after the stat was taken"
    box.linux.calls.clear()
    assert box.logs(run, offset=4, until=10) == b"456789"
    assert [op for op, _ in box.linux.calls if op == "logs"] == ["logs"]


def test_current_selects_latest_completed_run_by_timestamp(box):
    box.linux.rows = [
        {"role_id": "analyst", "run_id": "run-old", "running": False, "ended": 10},
        {"role_id": "analyst", "run_id": "run-new", "running": False, "ended": 20},
    ]
    assert box.current("analyst").run_id == "run-new"


def test_private_layout_rejects_role_path_traversal(box):
    with pytest.raises(ValueError):
        box.layout("../other")


def test_stop_retains_guest_data(box):
    disk = box.config.durable / "project-sentinel"
    disk.write_text("keep")
    box.up()
    box.down()
    assert box.state() == "stopped"
    assert disk.read_text() == "keep"


def test_state_reports_partial_when_only_one_vm_is_up(box):
    box.linux.running = True
    assert box.state() == "partial"
    assert box.infrastructure_status()["running"] is None


def test_state_reports_unobservable_vm_without_claiming_stopped(box):
    def broken():
        raise RuntimeError("VM needs recovery: Broken")

    box.linux._running = broken
    result = box.infrastructure_status()
    assert result["state"] == "recovery_required" and result["running"] is None
    assert "Broken" in result["errors"]["linux"]
    box.mac.running = True
    assert box.infrastructure_status()["running"] is None


def test_stop_forwards_requested_timeout_and_refuses_unresolved_cleanup(box):
    run = box.start(RunSpec("analyst", ("true",), run_id="run-deadline"))
    box.stop(run, timeout=0.5)
    assert ("stop", {"run_id": run.run_id, "timeout": 0.5}) in box.linux.calls
    box.linux.rows[0].update(running=False, unresolved=True, exit_code=125)
    with pytest.raises(RuntimeError, match="cleanup remains unresolved"):
        box.stop(run)


def test_emergency_stop_powers_off_before_tunnel_cleanup(box):
    events = []
    box.mac.force_down = lambda: events.append("power-off")
    box.linux.force_down = lambda: events.append("linux-off")
    box._runtime_tunnel = type(
        "Tunnel", (), {"close": lambda self: events.append("tunnel-close")}
    )()
    box.down(force=True)
    assert events == ["power-off", "linux-off", "tunnel-close"]
    assert box._runtime_tunnel is None


def test_stopped_vm_still_cleans_runtime_tunnel(box):
    closed = []
    box._runtime_tunnel = type("Tunnel", (), {"close": lambda self: closed.append(True)})()
    box.down()
    assert closed == [True]


def test_lifecycle_and_transport_acquire_one_shared_lock(box, monkeypatch):
    from contextlib import contextmanager

    from firm_sandbox import guest_control, locking

    acquired = []

    @contextmanager
    def lock(directory, name, **options):
        acquired.append((directory, name, options))
        yield

    monkeypatch.setattr(locking, "locked", lock)
    box.linux.rpc = guest_control.lifecycle()(box.linux.rpc.__func__).__get__(box.linux)
    box.runs()
    assert acquired == [(box.config.locks, "lifecycle", {"shared": True})]


def lifecycle_lock_held(config):
    """Whether another holder has the firm's lifecycle lock file, probed without waiting."""
    import fcntl

    with (config.locks / "lifecycle").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def test_sandbox_and_station_share_one_firm_lifecycle_lock(box):
    held = []
    station = box.mac_station(1)
    station.probe = lambda: (
        held.append(("station", lifecycle_lock_held(box.config)))
        or SimpleNamespace(request=lambda op: {}, close=lambda: None)
    )
    linux_up = box.linux.up

    def up():
        held.append(("sandbox", lifecycle_lock_held(box.config)))
        station.helper_status()
        return linux_up()

    box.linux.up = up
    box.up()
    station.helper_status()
    assert not lifecycle_lock_held(box.config)
    assert held == [("sandbox", True), ("station", True), ("station", True)]
    assert box.firm_locks == box.mac.firm_locks == station.firm_locks == box.config.locks


def test_up_checks_admission_between_mac_boot_phases(box):
    phases = []

    def boot(*, checkpoint):
        checkpoint()
        phases.append("started")
        # An emergency stop that lands while Lima boots the Mac.
        box._force_stopped.set()
        checkpoint()
        phases.append("initialized")
        box.mac.running = True

    box.mac.up = boot
    with pytest.raises(RuntimeError, match="force-stopped"):
        box.up()
    assert phases == ["started"]
    assert box._file_server.calls == ["stop"]
    assert not box.mac.running and box.linux.calls == ["up", "force-down"]


def test_up_is_rejected_until_emergency_shutdown_finishes(box):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    stopping, release = Event(), Event()

    def power_off():
        stopping.set()
        assert release.wait(2)
        box.mac.running = False

    box.mac.force_down = power_off
    with ThreadPoolExecutor(max_workers=1) as pool:
        shutdown = pool.submit(box.force_down)
        try:
            assert stopping.wait(2)
            with pytest.raises(RuntimeError, match="Emergency shutdown"):
                box.up()
            assert box._force_stopped.is_set()
        finally:
            release.set()
        assert shutdown.result(2) == {"running": False}
    assert not box.mac.running
    box.up()
    assert box.mac.running and box.linux.running


def test_emergency_shutdown_drains_boot_and_stops_a_late_start(box):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    booting, release, first_stop = Event(), Event(), Event()
    events = []

    def boot():
        booting.set()
        assert release.wait(2)
        events.append("boot")
        return {}

    def power_off():
        events.append("stop")
        first_stop.set()

    box.linux.up = boot
    box.mac.force_down = power_off
    with ThreadPoolExecutor(max_workers=2) as pool:
        startup = pool.submit(box.up)
        assert booting.wait(2)
        shutdown = pool.submit(box.force_down)
        try:
            assert first_stop.wait(2)
            assert not shutdown.done()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="force-stopped"):
            startup.result(2)
        assert shutdown.result(2) == {"running": False}
    assert events == ["stop", "boot", "stop", "stop"]
    assert box._pending_boots == 0
    assert box._force_stopped.is_set()


def test_replace_discards_both_guests_and_the_file_server_ledger(box, monkeypatch):
    from firm_sandbox.mac_station import DIRTY_MARKER

    box.mac_station(1).root.mkdir(parents=True, mode=0o700)
    (box.mac_station(1).root / DIRTY_MARKER).write_text("unverified")
    calls = []
    monkeypatch.setattr(box, "down", lambda: calls.append("down"))
    monkeypatch.setattr(box, "up", lambda: calls.append("up") or {"replaced": True})
    box.mac._image_base = lambda image: calls.append(("image", image))
    box.mac.remove = lambda: calls.append(
        ("remove", (box.mac_station(1).root / DIRTY_MARKER).exists())
    )
    assert box.replace() == {"replaced": True}
    assert calls == [("image", Path("/private/base")), "down", ("remove", True), "up"]
    assert box.linux.calls == ["remove"] and box._file_server.calls == ["discard"]
    assert not (box.mac_station(1).root / DIRTY_MARKER).exists()
    assert box.mac.image == Path("/private/base")


def test_mac_replace_refuses_a_running_firm(box):
    box.up()
    with pytest.raises(RuntimeError, match="Stop the firm"):
        box.mac_replace()


def test_mac_replace_refuses_under_a_held_station_lock(box):
    from firm_sandbox.mac_station import station_lock

    held = station_lock(box.firm_locks, 1)
    try:
        with pytest.raises(OSError, match="already leased"):
            box.mac_replace()
    finally:
        os.close(held)


def test_mac_replace_leaves_the_linux_record_and_disk_untouched(box):
    box.mac._image_base = lambda image: None
    assert box.mac_replace() == {"replaced": True}
    assert box.linux.calls == []
    assert box._file_server.calls == []


def test_mac_replace_discards_lease_records_and_markers(box, monkeypatch):
    from firm_sandbox.mac_station import DIRTY_MARKER

    monkeypatch.setattr("firm_sandbox.tunnel.Tunnel", FakeTunnel)
    monkeypatch.setattr(FakeTunnel, "log", [])
    box.mac_station(1).root.mkdir(parents=True, mode=0o700)
    (box.mac_station(1).root / DIRTY_MARKER).write_text("unverified")
    (box.mac_station(1).root / "leases").mkdir()
    lease_id = "a" * 32
    (box.mac_station(1).root / "leases" / f"{lease_id}.json").write_text(
        json.dumps({"lease_id": lease_id, "role_id": "analyst"})
    )
    # The tunnel's own record, which a crash leaves behind with the lease's.
    forward = ["-R", "127.0.0.1:2222:127.0.0.1:60022"]
    box.linux.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tunnel_record = box.linux.root / f"mac-lease-{lease_id}.json"
    tunnel_record.write_text(json.dumps({"generation": "a" * 32, "forward": forward}))
    tunnel_record.chmod(0o600)
    box.mac._image_base = lambda image: None
    # The Mac's own ssh.config went with an earlier, interrupted attempt.
    box.mac.ssh_port = lambda: (_ for _ in ()).throw(FileNotFoundError("ssh.config"))
    assert box.mac_replace() == {"replaced": True}
    assert not (box.mac_station(1).root / DIRTY_MARKER).exists()
    assert not (box.mac_station(1).root / "leases" / f"{lease_id}.json").exists()
    # A lease left over from a crash is closed by the binding its tunnel recorded.
    assert ("close", "mac-lease-" + lease_id) in FakeTunnel.log
    assert not tunnel_record.exists()
    # Every step repeats, so an interrupted call is finished by calling it again.
    assert box.mac_replace() == {"replaced": True}


def test_mac_replace_validates_the_configured_image_before_removing_the_mac(box):
    calls = []
    box.mac._image_base = lambda image: calls.append(("image", image))
    box.mac._clone = lambda image: calls.append(("clone", image))
    box.mac.remove = lambda: calls.append("remove")
    assert box.mac_replace() == {"replaced": True}
    # No clone here: the next up() clones the configured image, as a first up() does.
    assert calls == [("image", Path("/private/base")), "remove"]
    assert box.mac.image == Path("/private/base")
    assert box.mac.running is False


def test_mac_replace_keeps_the_mac_when_the_configured_image_is_refused(box):
    calls = []

    def refuse(image):
        raise ValueError("Base image must be stopped before cloning")

    box.mac._image_base = refuse
    box.mac.remove = lambda: calls.append("remove")
    with pytest.raises(ValueError, match="must be stopped"):
        box.mac_replace()
    assert calls == []


def test_mac_restart_cycles_the_machine_and_refuses_either_lease(two_stations, monkeypatch):
    from firm_sandbox.mac_station import station_lock

    box, calls = two_stations, []
    monkeypatch.setattr(box.mac, "down", lambda: calls.append("down"))
    monkeypatch.setattr(box.mac, "up", lambda: calls.append("up") or {"ready": True})
    assert box.mac_restart()["ready"] is True
    assert calls == ["down", "up"]
    assert box.linux.calls == [] and box._file_server.calls == ["reconnect"]
    for number in box.mac.stations:
        held = station_lock(box.firm_locks, number)
        try:
            with pytest.raises(OSError, match="already leased"):
                box.mac_restart()
        finally:
            os.close(held)
    assert calls == ["down", "up"]


@pytest.mark.parametrize(("running", "booted"), [(False, True), (True, False)])
def test_export_boots_only_a_stopped_linux_vm_and_stops_it_again(box, running, booted):
    box.linux.running = running
    box.linux.export = lambda destination, *, root, homes: box.linux.calls.append(
        ("export", destination, root, homes, box.linux.running)
    ) or ["drive/escape"]
    assert box.export(Path("/export"), homes=True, boot=True) == ["drive/escape"]
    export = ("export", Path("/export"), "/srv/firm", True, True)
    assert box.linux.calls == (["up", export, "down"] if booted else [export])
    assert box.linux.running is running and box.mac.running is False
    assert box._file_server.calls == []


def test_export_without_boot_uses_only_the_running_linux_vm(box):
    box.linux.export = lambda destination, *, root, homes: box.linux.calls.append("export") or []
    with pytest.raises(RuntimeError, match="may not boot it"):
        box.export(Path("/export"))
    box.linux.running = True
    assert box.export(Path("/export")) == []
    assert box.linux.calls == ["export"]


def test_discard_removes_both_guests_for_good_and_keeps_host_records(box, monkeypatch):
    from firm_sandbox.mac_station import DIRTY_MARKER

    box.mac_station(1).root.mkdir(parents=True, mode=0o700)
    (box.mac_station(1).root / DIRTY_MARKER).write_text("unverified")
    (box.mac_station(1).root / "leases").mkdir()
    (box.mac_station(1).root / "leases" / "lease.json").write_text("{}")
    box.mac.remove = lambda: box.mac.calls.append(
        ("remove", (box.config.durable / sandbox.DISCARDED).exists())
    )
    box.linux.running = True
    with pytest.raises(RuntimeError, match="Stop the firm's VMs"):
        box.discard()
    box.linux.running = False
    assert box.state() == "stopped"
    assert box.discard() == {"discarded": True}
    assert box.mac.calls == [("remove", True)]
    assert box.linux.calls == ["remove"] and box._file_server.calls == ["discard"]
    assert (box.mac_station(1).root / "leases" / "lease.json").exists()
    assert box.state() == "discarded"
    assert box.discard() == {"discarded": True}  # finishes an interrupted discard
    for operation in (box.up, box.replace, lambda: box.export(Path("/export"), boot=True)):
        with pytest.raises(RuntimeError, match="were discarded"):
            operation()
    assert box.linux.calls == ["remove", "remove"]


# ---- the Mac lease facade ---------------------------------------------------------------

GIB = 1 << 30
STATION_WORK_REPORT = {
    "ok": True,
    "processes": {"ok": True, "verified": True, "extra": [], "tolerated": []},
    "services": {"ok": True, "verified": True, "extra": []},
    "launch_agents": {"ok": True, "verified": True, "extra": []},
    "crontab": {"ok": True, "verified": True, "empty": True},
}
STATION_REPLIES = {
    "station-account": {"present": True},
    "station-keep-awake": {"screen_lock": "screenLock is off", "screen_saver_idle": 0,
                           "relock_at_login": False,
                           "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0}},
    "station-baseline": {"processes": [], "services": [], "launch_agents": [], "crontab": ""},
    "station-reclaim": {
        "refused": False, "completed": True, "timed_out": False, "categories": {},
        "kept": {"home": [], "entries": {}, "outside_categories": []},
        "scratch": "/Users/firmstation1/scratch",
    },
    "station-mount": {"mounted": True, "point": "/Users/firmstation1/drive",
                      "scratch": "/Users/firmstation1/scratch"},
    "station-key-install": {"installed": True},
    "station-key-remove": {"present": False},
    "station-kill-sessions": {"remaining": []},
    "station-sweep": STATION_WORK_REPORT,
    "station-verify": {**STATION_WORK_REPORT, "key_present": False, "sessions": [],
                       "drive_mounted": False, "status_errors": {}},
    "station-unmount": {"mounted": False},
    "station-command-log": {"data": ""},
    "sample": {"disk_used_bytes": 40 * GIB, "disk_total_bytes": 100 * GIB},
}


class FakeRelay:
    def request(self, op, **arguments):
        return {"ready": True, "clean": True, "apps": [], "status": "ok",
                "environment": "vm-session"}

    def close(self):
        pass


class FakeTunnel:
    """Records lifecycle; the socket file exists while the tunnel is open."""

    log = []

    def __init__(self, **options):
        self.options = options
        self.socket = Path(options["socket_root"]) / f"{options['name']}.sock"
        self.record = Path(options["root"]) / f"{options['name']}.json"

    def start(self):
        FakeTunnel.log.append(("start", self.options["name"]))
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.touch()
        self.record.write_text("{}")

    def close(self):
        FakeTunnel.log.append(("close", self.options["name"]))
        self.socket.unlink(missing_ok=True)


def leasable(box, monkeypatch):
    """A booted firm whose every station answers its guest RPCs and relays cleanly."""
    box.up()
    FakeTunnel.log = []
    monkeypatch.setattr("firm_sandbox.tunnel.Tunnel", FakeTunnel)
    monkeypatch.setattr(
        "firm_sandbox.mac_station.generate_key_pair",
        lambda: (b"PRIVATE KEY MATERIAL", "ssh-ed25519 AAAAC3 firm-mac-lease"),
    )
    vm = box.mac
    vm.calls.clear()
    vm.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def rpc(operation, **payload):
        vm.calls.append((operation, payload))
        number = payload.get("station", 1)
        if operation == "station-reclaim":
            vm.last_reclaim = number
            return {
                **STATION_REPLIES["station-reclaim"],
                "scratch": f"/Users/firmstation{number}/scratch",
            }
        if operation == "station-session-ensure":
            return {
                "station": number,
                "user": f"firmstation{number}",
                "uid": 501 + number,
                "ready": True,
                "created": False,
                "helper": {"ready": True},
            }
        if operation == "station-mount":
            return {"mounted": True, "point": f"/Users/firmstation{number}/drive",
                    "scratch": f"/Users/firmstation{number}/scratch"}
        return STATION_REPLIES.get(operation, {})

    monkeypatch.setattr(vm, "_rpc", rpc)
    for number in vm.stations:
        box.mac_station(number).root.mkdir(parents=True, exist_ok=True, mode=0o700)
        monkeypatch.setattr(
            box.mac_station(number), "_relay", lambda binding, **options: FakeRelay()
        )
    return box


@pytest.fixture
def leased(box, monkeypatch):
    return leasable(box, monkeypatch)


@pytest.fixture
def leased_pair(two_stations, monkeypatch):
    return leasable(two_stations, monkeypatch)


def test_mac_acquire_opens_the_lease_tunnel_and_hands_the_key_to_the_role(leased):
    box = leased
    lease = box.mac_acquire("r-e1", "activation-1")
    assert set(lease) == {
        "lease_id", "station", "ssh_host", "ssh_port", "user", "drive", "project_dir",
        "scratch", "key_path", "disk", "disk_reclaimed", "disk_refusals",
    }
    assert lease["disk_refusals"] == []
    assert lease["disk"] == {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
    assert lease["disk_reclaimed"] == 0
    assert lease["scratch"] == "/Users/firmstation1/scratch"
    assert "PRIVATE" not in json.dumps(lease)
    assert lease["station"] == 1
    assert lease["ssh_host"] == "127.0.0.1" and lease["ssh_port"] == 2222
    assert lease["project_dir"] == "/Users/firmstation1/drive/org/founder/r-e1/work"
    assert lease["key_path"] == "/run/firm/r-e1/credentials/mac-" + lease["lease_id"]
    name = "mac-lease-" + lease["lease_id"]
    assert FakeTunnel.log == [("start", name)]
    tunnel = FakeTunnel.log and box._lease_tunnel(lease["lease_id"], 1)
    assert tunnel.options["host"] == "lima-linux"
    assert tunnel.options["forward"] == ("-R", "127.0.0.1:2222:127.0.0.1:60022")
    assert tunnel.options["root"] == box.linux.root
    assert tunnel.options["generation"]() == box.linux.generation
    written = next(payload for operation, payload in box.linux.calls
                   if operation == "write-credential")
    assert written == {
        "role_id": "r-e1",
        "name": "mac-" + lease["lease_id"],
        "data": base64.b64encode(b"PRIVATE KEY MATERIAL").decode(),
    }
    mounted = next(
        payload for operation, payload in box.mac.calls if operation == "station-mount"
    )
    assert mounted["drive"] == {"user": "firmfile-r-e1", "password": "secret-r-e1",
                                "port": 4450, "share": "firm"}
    status = box.mac_status()
    assert status["configured"] is True and len(status["stations"]) == 1
    assert status["stations"][0]["held"] is True
    assert status["stations"][0]["holder"] == "r-e1"
    # The firm's only station is taken, so the next caller waits for it.
    with pytest.raises(OSError, match="already leased"):
        box.mac_acquire("r-e2", "activation-2")
    result = box.mac_release(lease["lease_id"])
    assert result["clean"] is True
    assert result["disk"] == {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
    assert result["disk_reclaimed"] == 0
    assert result["checks"]["access_revoked"]["tunnel_closed"] is True
    assert FakeTunnel.log[-1] == ("close", name)
    assert not (box.linux.root / f"{name}.json").exists()
    assert box.mac_release(lease["lease_id"]) == {
        "status": "ok", "lease_id": lease["lease_id"], "gone": True,
    }
    assert box.mac_status()["stations"][0]["held"] is False
    events = box.events()
    assert [event["kind"] for event in events[-2:]] == ["mac.acquired", "mac.released"]
    assert [event["station"] for event in events[-2:]] == [1, 1]


def test_a_tunnel_that_survives_release_fails_access_revoked(leased, monkeypatch):
    box = leased
    lease = box.mac_acquire("r-e1", "activation-1")
    monkeypatch.setattr(FakeTunnel, "close", lambda self: None)
    result = box.mac_release(lease["lease_id"])
    assert result["clean"] is False
    assert result["checks"]["access_revoked"] == {
        "ok": False, "key_present": False, "sessions": [], "tunnel_closed": False,
    }
    assert box.mac_status()["stations"][0]["dirty"] is True


def test_mac_recover_cli_revokes_the_stale_lease_tunnel(leased, monkeypatch, capsys):
    import sys

    from firm_sandbox import cli

    box = leased
    lease = box.mac_acquire("r-e1", "activation-1")
    close = FakeTunnel.close
    monkeypatch.setattr(FakeTunnel, "close", lambda self: None)
    assert box.mac_release(lease["lease_id"])["clean"] is False
    monkeypatch.setattr(FakeTunnel, "close", close)
    monkeypatch.setitem(STATION_REPLIES, "station-verify", {
        **STATION_REPLIES["station-sweep"], "key_present": False, "sessions": [],
        "drive_mounted": False,
    })
    monkeypatch.setattr(cli.Config, "load", lambda path: box.config)
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm-sandbox",
            "--config",
            str(box.config.root / "sandbox.toml"),
            "mac-recover",
            "--station",
            "1",
            "--confirm-cleanup",
        ],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["recovered"] is True
    assert result["checks"]["access_revoked"]["tunnel_closed"] is True
    assert FakeTunnel.log[-1] == ("close", "mac-lease-" + lease["lease_id"])
    assert box.mac_status()["stations"][0]["dirty"] is False


def test_mac_acquire_releases_the_lease_when_the_key_cannot_reach_the_role(leased, monkeypatch):
    box = leased
    original = box.linux._rpc

    def rpc(operation, **payload):
        if operation == "write-credential":
            raise RuntimeError("no active run")
        return original(operation, **payload)

    monkeypatch.setattr(box.linux, "_rpc", rpc)
    with pytest.raises(RuntimeError, match="no active run"):
        box.mac_acquire("r-e1", "activation-1")
    assert [kind for kind, _ in FakeTunnel.log] == ["start", "close"]
    assert box.mac_status() == {
        "configured": True,
        "enrolled": False,
        "stations": [{
            "configured": True, "station": 1, "enrolled": False, "held": False,
            "lease_id": None, "holder": None, "since": None, "dirty": False,
        }],
    }


def test_mac_status_on_a_stopped_mac_reports_stopped_desktops_without_probing(
    leased, monkeypatch, capsys
):
    import sys

    from firm_sandbox import cli

    box = leased
    box.mac.running = False
    monkeypatch.setattr(box.mac_station(1), "probe", lambda: pytest.fail("relayed to a stopped VM"))
    monkeypatch.setattr(box.mac, "_rpc", lambda *args, **kwargs: pytest.fail("stopped guest RPC"))
    monkeypatch.setattr(cli.Config, "load", lambda path: box.config)
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(
        sys,
        "argv",
        ["firm-sandbox", "--config", str(box.config.root / "sandbox.toml"), "mac-status"],
    )
    cli.main()
    row = json.loads(capsys.readouterr().out)["stations"][0]
    assert row["helper"] == {"status": "stopped"} and row["awake"] == {"status": "stopped"}
    assert row["station"] == 1


def test_mac_acquire_on_a_mac_without_the_station_account_refuses_before_any_lease(
    leased, monkeypatch
):
    from firm_sandbox.mac_station import StationNotEnrolled

    box = leased
    monkeypatch.setitem(STATION_REPLIES, "station-account", {"present": False})
    with pytest.raises(StationNotEnrolled):
        box.mac_acquire("r-e1", "activation-1")
    assert [operation for operation, _ in box.mac.calls] == ["station-account"]
    assert FakeTunnel.log == []
    assert not any(isinstance(call, tuple) and call[0] == "write-credential"
                   for call in box.linux.calls)
    assert box.mac_status()["stations"][0]["dirty"] is False


def test_mac_connect_requires_the_held_lease(leased):
    with pytest.raises(OSError, match="not held"):
        leased.mac_connect("0" * 32)


# ---- two stations ------------------------------------------------------------------------


def test_a_second_acquire_lands_on_the_next_station_and_a_third_waits(leased_pair):
    box = leased_pair
    first = box.mac_acquire("r-e1", "activation-1")
    second = box.mac_acquire("r-e2", "activation-2")
    assert (first["station"], second["station"]) == (1, 2)
    assert (first["ssh_port"], second["ssh_port"]) == (2222, 2223)
    assert box._lease_tunnel(second["lease_id"], 2).options["forward"] == (
        "-R", "127.0.0.1:2223:127.0.0.1:60022",
    )
    assert [row["holder"] for row in box.mac_status()["stations"]] == ["r-e1", "r-e2"]
    with pytest.raises(OSError, match="already leased"):
        box.mac_acquire("r-e3", "activation-3")
    # Each release reaches its own station, and frees it for the next caller.
    assert box.mac_release(second["lease_id"])["clean"] is True
    assert box.mac_acquire("r-e3", "activation-3")["station"] == 2


def test_an_acquire_for_one_station_tries_that_station_alone(leased_pair):
    """The owner's checks address a station by number; a busy one is not passed over."""
    from firm_sandbox.mac_station import StationBusy

    box = leased_pair
    second = box.mac_acquire("r-e2", "activation-2", station=2)
    assert second["station"] == 2
    assert not any(payload.get("station") == 1 for _, payload in box.mac.calls)
    with pytest.raises(StationBusy):
        box.mac_acquire("r-e3", "activation-3", station=2)
    assert box.mac_acquire("r-e1", "activation-1", station=1)["station"] == 1
    with pytest.raises(ValueError, match="not station 3"):
        box.mac_acquire("r-e4", "activation-4", station=3)


def test_a_station_needing_recovery_does_not_hide_the_other(leased_pair):
    from firm_sandbox.mac_station import DIRTY_MARKER, StationBusy, StationNeedsRecovery

    box = leased_pair
    (box.mac_station(1).root / DIRTY_MARKER).write_text("unverified")
    lease = box.mac_acquire("r-e1", "activation-1")
    assert lease["station"] == 2
    assert not any(payload.get("station") == 1 for _, payload in box.mac.calls)
    # A busy station may free itself, so that is what the caller hears about first.
    with pytest.raises(StationBusy):
        box.mac_acquire("r-e2", "activation-2")
    # With nothing leased and both stations dirty, the owner has to look at them.
    box.mac_release(lease["lease_id"])
    (box.mac_station(2).root / DIRTY_MARKER).write_text("unverified")
    with pytest.raises(StationNeedsRecovery):
        box.mac_acquire("r-e2", "activation-2")


def low_disk(monkeypatch, station):
    """Have shared free space remain low after this account's reclaim attempt."""
    original = station._station_rpc
    vm = station.vm
    sample = vm._rpc

    def rpc(operation, **payload):
        if operation == "sample" and getattr(vm, "last_reclaim", None) == station.station:
            return {"disk_used_bytes": 99 * GIB, "disk_total_bytes": 100 * GIB}
        return sample(operation, **payload)

    def reclaim(operation, **payload):
        if operation == "station-reclaim":
            vm.last_reclaim = station.station
        return original(operation, **payload)

    monkeypatch.setattr(vm, "_rpc", rpc)
    monkeypatch.setattr(station, "_station_rpc", reclaim)


def test_a_full_station_moves_to_the_next_and_refuses_with_evidence(leased_pair, monkeypatch):
    from firm_sandbox.mac_station import StationDiskFull

    box = leased_pair
    low_disk(monkeypatch, box.mac_station(1))
    lease = box.mac_acquire("r-e1", "activation-1")
    assert lease["station"] == 2
    # Station 1's refusal is not lost to the grant that followed it.
    assert [refusal["station"] for refusal in lease["disk_refusals"]] == [1]
    assert lease["disk_refusals"][0]["disk"] == {"free_bytes": 1 * GIB, "total_bytes": 100 * GIB}
    assert box.mac_station(1).status()["dirty"] is False
    assert box.mac_release(lease["lease_id"])["clean"] is True
    # Now both stations are full.
    low_disk(monkeypatch, box.mac_station(2))
    with pytest.raises(StationDiskFull) as excinfo:
        box.mac_acquire("r-e2", "activation-2")
    assert excinfo.value.station == 2
    assert excinfo.value.disk == {"free_bytes": 1 * GIB, "total_bytes": 100 * GIB}
    assert excinfo.value.disk_reclaimed == 0
    # Both, not only the last one tried.
    assert [refusal["station"] for refusal in excinfo.value.disk_refusals] == [1, 2]
    refused = [event for event in box._events.read() if event["kind"] == "mac.refused"]
    assert [event["station"] for event in refused] == [1, 1, 2]


def test_busy_is_preferred_over_a_full_station_which_may_lease_elsewhere(
    leased_pair, monkeypatch
):
    from firm_sandbox.mac_station import StationBusy

    box = leased_pair
    low_disk(monkeypatch, box.mac_station(1))
    box.mac_acquire("r-e1", "activation-1")  # lands on station 2; station 1 stays full
    with pytest.raises(StationBusy) as excinfo:
        box.mac_acquire("r-e2", "activation-2")
    # Busy outranks it as the answer; the full station's evidence still travels.
    assert [refusal["station"] for refusal in excinfo.value.disk_refusals] == [1]


def test_a_full_stations_refusal_survives_a_later_station_losing_its_connection(
    leased_pair, monkeypatch
):
    """Not only the three refusals: whatever ends the acquire carries what was found."""
    box = leased_pair
    low_disk(monkeypatch, box.mac_station(1))
    original = box.mac._rpc

    def lost(operation, **payload):
        if operation == "station-baseline" and payload["station"] == 2:
            raise OSError("guest control connection failed")
        return original(operation, **payload)

    monkeypatch.setattr(box.mac, "_rpc", lost)
    with pytest.raises(OSError, match="connection failed") as excinfo:
        box.mac_acquire("r-e1", "activation-1")
    assert [refusal["station"] for refusal in excinfo.value.disk_refusals] == [1]


def test_needing_recovery_is_preferred_over_a_full_station(leased_pair, monkeypatch):
    from firm_sandbox.mac_station import DIRTY_MARKER, StationNeedsRecovery

    box = leased_pair
    low_disk(monkeypatch, box.mac_station(1))
    (box.mac_station(2).root / DIRTY_MARKER).write_text("unverified")
    with pytest.raises(StationNeedsRecovery) as excinfo:
        box.mac_acquire("r-e1", "activation-1")
    assert [refusal["station"] for refusal in excinfo.value.disk_refusals] == [1]


def test_release_after_a_restart_routes_by_the_station_holding_the_record(
    leased_pair, monkeypatch
):
    box = leased_pair
    box.mac_acquire("r-e1", "activation-1")
    lease = box.mac_acquire("r-e2", "activation-2")
    assert lease["station"] == 2
    # The runtime died holding the lease: its lock is gone with the process, and the
    # station's record and dirty marker are what is left.
    station = box.mac_station(2)
    os.close(station._held["lock"])
    station._held = None
    box.mac_release(box.mac_station(1)._held["lease_id"])
    # A fresh Sandbox over the same firm remembers no lease; the records answer.
    reopened = Sandbox(box.config)
    reopened.mac.stations = box.mac.stations
    reopened._file_server = box._file_server
    leasable(reopened, monkeypatch)
    assert reopened._lease_station(lease["lease_id"]) == 2
    assert reopened.mac_release(lease["lease_id"])["clean"] is True
    assert reopened.mac_release(lease["lease_id"])["gone"] is True


def test_replace_refuses_while_either_station_is_leased(leased_pair, monkeypatch):
    box = leased_pair
    box.mac._image_base = lambda image: None
    box.mac.remove = lambda: box.mac.calls.append("remove")
    first = box.mac_acquire("r-e1", "activation-1")
    second = box.mac_acquire("r-e2", "activation-2")
    monkeypatch.setattr(box, "down", lambda: None)
    monkeypatch.setattr(box, "up", lambda: {"replaced": True})
    with pytest.raises(OSError, match="already leased"):
        box.replace()
    # Replacement takes every station's lock, so one held lease is enough to refuse.
    assert box.mac_release(first["lease_id"])["clean"] is True
    with pytest.raises(OSError, match="already leased"):
        box.replace()
    assert "remove" not in box.linux.calls
    assert "remove" not in box.mac.calls
    assert box.mac_release(second["lease_id"])["clean"] is True
    assert box.replace() == {"replaced": True}
    assert "remove" in box.mac.calls
    assert box.linux.calls[-1] == "remove"


def test_up_reports_unready_account_and_restates_awake_for_every_account(two_stations):
    box = two_stations
    original = box.mac._rpc

    def rpc(operation, **payload):
        if operation == "station-session-ensure" and payload["station"] == 1:
            box.mac.calls.append((operation, payload))
            return {"station": 1, "ready": False, "failure": "login_failed"}
        return original(operation, **payload)

    box.mac._rpc = rpc
    result = box.up()
    assert [row["ready"] for row in result["mac"]["stations"]] == [False, True]
    assert box.linux.running and box.mac.running
    assert ("keep-awake", {}) in box.mac.calls
    assert {payload["station"] for operation, payload in box.mac.calls
            if operation == "station-keep-awake"} == {1, 2}


def test_acquire_passes_an_unready_session_without_creating_a_lease(leased_pair, monkeypatch):
    box = leased_pair
    original = box.mac._rpc

    def rpc(operation, **payload):
        if operation == "station-session-ensure" and payload["station"] == 1:
            return {"station": 1, "ready": False, "failure": "helper_not_ready"}
        return original(operation, **payload)

    monkeypatch.setattr(box.mac, "_rpc", rpc)
    lease = box.mac_acquire("r-e1", "activation-1")
    assert lease["station"] == 2 and lease["user"] == "firmstation2"
    assert lease["drive"] == "/Users/firmstation2/drive"
    assert lease["scratch"] == "/Users/firmstation2/scratch"
    assert lease["project_dir"] == "/Users/firmstation2/drive/org/founder/r-e1/work"
    assert not box.mac_station(1)._records()
    assert box.mac_station(1).status()["dirty"] is False


@pytest.mark.parametrize("operation", ["down", "mac_restart"])
def test_machine_operations_refuse_before_touching_either_vm_under_any_lease(
    leased_pair, operation
):
    box = leased_pair
    first = box.mac_acquire("r-e1", "activation-1")
    second = box.mac_acquire("r-e2", "activation-2")
    box.mac_release(first["lease_id"])
    before = (list(box.linux.calls), list(box.mac.calls))
    with pytest.raises(OSError, match="already leased"):
        getattr(box, operation)()
    assert (box.linux.calls, box.mac.calls) == before
    assert box.mac_status()["stations"][1]["lease_id"] == second["lease_id"]


def test_up_on_a_running_firm_restates_only_the_stations_it_can_lock(leased_pair):
    box = leased_pair
    lease = box.mac_acquire("r-e1", "activation-1")
    box.mac.calls.clear()
    result = box.up()["mac"]
    assert [row["station"] for row in result["stations"]] == [2]
    assert result["stations_skipped"] == [{"station": 1, "skipped": "leased"}]
    # Nothing was asked of the leased station; the machine's own awake policy still is.
    assert {payload["station"] for operation, payload in box.mac.calls
            if "station" in payload} == {2}
    assert ("keep-awake", {}) in box.mac.calls
    assert box.mac_status()["stations"][0]["lease_id"] == lease["lease_id"]


def test_up_keeps_the_whole_machine_lock_when_a_boot_or_an_install_would_write(
    leased_pair, monkeypatch
):
    box = leased_pair
    box.mac_acquire("r-e1", "activation-1")
    before = (list(box.linux.calls), list(box.mac.calls))
    # A controller install writes the whole Mac, whether or not it is already booted.
    box.mac.controller_intact = False
    with pytest.raises(OSError, match="already leased"):
        box.up()
    box.mac.controller_intact = True
    with monkeypatch.context() as patch:
        patch.setattr(box.mac, "boot_script_installed", lambda: False)
        with pytest.raises(OSError, match="already leased"):
            box.up()
    box.mac.running = False
    with pytest.raises(OSError, match="already leased"):
        box.up()
    box.mac.running = True
    box.linux.running = False
    with pytest.raises(OSError, match="already leased"):
        box.up()
    assert (box.linux.calls, box.mac.calls) == before


def test_one_file_forward_and_one_machine_for_all_accounts(two_stations):
    box = two_stations
    box._file_server = None
    assert [forward.name for forward in box.file_server.forwards(4450)] == [
        "files-owner", "files-mac",
    ]
    assert box._vms() == {"linux": box.linux, "mac": box.mac}
    assert box.mac_station(1).vm is box.mac_station(2).vm is box.mac
    for number in box.mac.stations:
        assert box.mac_station(number).root == box.config.durable / "mac" / "stations" / str(number)


def test_unprepared_mac_refuses_acquire_without_a_station_or_lease(box):
    from firm_sandbox.mac_station import StationNotEnrolled

    box.mac.stations = ()
    with pytest.raises(StationNotEnrolled, match="mac-prepare"):
        box.mac_acquire("r-e1", "activation-1")
    assert box.mac.calls == []
    assert box._mac_stations == {}


def test_child_rpc_facade_carries_worker_launch_settings(box, monkeypatch):
    from firm_sandbox import Run

    box.up()
    parent = Run("analyst", "parent", box.linux.generation)
    calls = []

    def rpc(operation, **payload):
        calls.append((operation, payload))
        return {"data": base64.b64encode(b"answer").decode(), "confirmed": True}

    monkeypatch.setattr(box.linux, "rpc", rpc)
    box.start_child(parent, run_id="task-one", argv=("sh", "-c", "printf answer"),
                    seconds_remaining=123, env={"WORKER": "1"}, secrets={"auth": b"token"},
                    secret_env={"PROVIDER_AUTH": "auth"}, stdin=b"instruction")
    box.child_status("task-one")
    assert box.child_logs("task-one", offset=2, limit=4) == b"answer"
    assert box.stop_child("task-one")["confirmed"]
    url = base64.b64encode(box.linux.network.proxy_url("analyst").encode()).decode()
    assert calls[0][1].pop("host_time") <= time.time()
    assert calls == [
        ("start-child", {"parent_run_id": "parent", "run_id": "task-one",
                         "argv": ["sh", "-c", "printf answer"], "seconds_remaining": 123,
                         "env": {"WORKER": "1", "NO_PROXY": "localhost,127.0.0.1,::1",
                                 "no_proxy": "localhost,127.0.0.1,::1"},
                         "secrets": {"auth": "dG9rZW4=", "egress-proxy": url},
                         "secret_env": {"PROVIDER_AUTH": "auth", "HTTPS_PROXY": "egress-proxy",
                                        "HTTP_PROXY": "egress-proxy",
                                        "https_proxy": "egress-proxy",
                                        "http_proxy": "egress-proxy"},
                         "stdin": "aW5zdHJ1Y3Rpb24="}),
        ("child-status", {"run_id": "task-one"}),
        ("child-logs", {"run_id": "task-one", "offset": 2, "limit": 4}),
        ("stop-child", {"run_id": "task-one", "timeout": 60}),
    ]
    with pytest.raises(ValueError, match="discarded VM"):
        box.start_child(Run("analyst", "parent", "old"), run_id="late", argv=("true",),
                        seconds_remaining=123)


def test_child_layout_uses_disk_state_and_tmpfs_credentials(box):
    layout = box.child_layout("analyst", "task-one")
    parent = box.layout("analyst")
    assert layout.private == "/var/lib/firm/task-homes/task-one/home"
    assert layout.state == "/var/lib/firm/task-homes/task-one/state"
    assert layout.credentials == "/run/firm/analyst/tasks/task-one/credentials"
    assert (layout.private_work, layout.drive) == (parent.private_work, parent.drive)


def test_disposal_calls_guest_control_without_retiring_files(box):
    assert box.dispose("analyst") == {}
    assert ("dispose", {"role_id": "analyst"}) in box.linux.calls
    assert box._file_server.calls == []
    box.start(RunSpec("active", ("true",), run_id="run-a"))
    with pytest.raises(ValueError, match="Stop and resolve"):
        box.dispose("active")
    box.linux.rows[0].update(running=False, unresolved=True)
    with pytest.raises(ValueError, match="Stop and resolve"):
        box.dispose("active")
    assert not any(call == ("dispose", {"role_id": "active"}) for call in box.linux.calls)


@pytest.mark.parametrize("role", [None, "analyst"])
def test_storage_rpc_and_sandbox_shape_without_run_checks(box, role):
    # Even a live run does not block read-only attribution or require inventory.
    box.linux.rows.append({"role_id": "analyst", "run_id": "run-a", "running": True})
    box.linux.calls.clear()
    assert box.storage(role) == {}
    payload = {"timeout": 90, **({"role_id": role} if role is not None else {})}
    assert box.linux.calls == [("storage", payload)]
