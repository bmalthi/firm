"""Focused no-VM checks for the Mac station's VM: clone policy, boot, guest controller install."""

from __future__ import annotations

import json
import runpy
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from firm_sandbox import Config, Sandbox, guest_control, mac_vm, mac_vm_guest, sandbox
from firm_sandbox.compatibility import image_identity
from firm_sandbox.guest_control import save
from firm_sandbox.models import VMSettings


def settings(root, tmp_path):
    return VMSettings(root, firm_locks=tmp_path / "locks", firm_id="firm", cpus=4, memory_gib=12)


def marked_image(**fields):
    return image_identity() | fields


class MockRPC:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, operation, **payload):
        self.calls.append((operation, payload))
        if self.result is not None:
            return self.result
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
            return {
                "screen_lock": "screenLock is off",
                "screen_saver_idle": 0,
                "relock_at_login": False,
                "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0},
            }
        return {}


class FakeLinux:
    def __init__(self, settings):
        root = self.root = settings.root
        self.calls = []
        self.running = True
        self.settings = settings
        self.ssh_config = root / "ssh.config"
        self.paths = SimpleNamespace(verify_runtime=lambda: None)
        self.socket_root = root
        self.commands = object()
        self.network = SimpleNamespace(ready=lambda: True)

    def _state(self):
        return {"generation": "linux-generation", "ready": True}

    def _running(self):
        return self.running

    def up(self, *, checkpoint=lambda: None):
        checkpoint()
        self.calls.append("up")
        return {}

    def down(self):
        self.calls.append("down")
        self.running = False

    def force_down(self):
        self.calls.append("force-down")
        self.running = False

    def remove(self):
        self.calls.append("remove")

    def rpc(self, operation, **payload):
        self.calls.append((operation, payload))
        return []


@pytest.fixture
def firm(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_vm.shutil, "which", lambda _: "/opt/homebrew/bin/limactl")
    monkeypatch.setattr(sandbox, "LinuxVM", FakeLinux)
    root = Path("/tmp").resolve() / f"fv-{uuid4().hex[:10]}"
    root.mkdir()
    root.chmod(0o700)
    image = tmp_path / "image"
    image.mkdir()
    metadata = marked_image(enrollment={"stations": 1})
    save(image / "image.json", metadata)
    box = Sandbox(
        Config(
            root,
            mac_image=str(tmp_path / "image"),
            mac_cpus=6,
            mac_memory_gib=12,
        )
    )
    vm = box.mac
    vm.root.mkdir(mode=0o700, parents=True)
    vm.record.write_text(
        json.dumps(
            {
                "firm_id": "firm",
                "image": str(tmp_path / "image"),
                "created_with_release": metadata["created_with_release"],
                "compatibility_generation": metadata["compatibility_generation"],
                "image_id": metadata["image_id"],
                "generation": "generation-new",
                "cpus": 6,
                "memory_gib": 12,
                "ready": True,
                "stations": 1,
                "enrolled": True,
            }
        )
    )
    vm.record.chmod(0o600)
    vm._rpc = MockRPC()
    box._file_server = SimpleNamespace(
        config=box.config,
        start=lambda: None,
        stop=lambda: None,
        require_ready=lambda: None,
        discard=lambda: None,
        retire_role=lambda role: None,
    )
    yield box
    shutil.rmtree(root)


def test_constructor_rejects_owner_root_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(mac_vm.shutil, "which", lambda _: "/opt/homebrew/bin/limactl")
    with pytest.raises(ValueError, match="must not be a symlink"):
        mac_vm.MacVM(settings(link, tmp_path))


def test_mac_vm_is_the_mac_instance_under_its_own_root_sharing_the_firm_lifecycle_lock(firm):
    vm = firm.mac
    # All account records share the same Lima instance and machine directory.
    assert mac_vm.INSTANCE == "mac" and vm.instance == vm.root / "lima" / "mac"
    assert vm.root == firm.config.durable / "mac" and vm.stations == (1,)
    assert firm.root == firm.linux.root == firm.config.durable / "linux"
    assert vm.firm_locks == firm.firm_locks == firm.config.locks


@pytest.mark.parametrize("removed", [False, True])
def test_replace_clears_lease_quarantine_only_after_removing_old_guest(firm, monkeypatch, removed):
    from firm_sandbox.mac_station import DIRTY_MARKER

    marker = firm.mac_station(1).root / DIRTY_MARKER
    marker.parent.mkdir(parents=True, mode=0o700)
    marker.write_text("unverified launch")
    for name in ("down", "up"):
        monkeypatch.setattr(firm, name, lambda *_args: None)
    monkeypatch.setattr(firm.mac, "_image_base", lambda *_args: None)
    monkeypatch.setattr(firm.mac, "down", lambda: None)
    monkeypatch.setattr(firm.mac, "_running", lambda: False)
    monkeypatch.setattr(firm.mac.paths, "prepare", lambda: None)
    monkeypatch.setattr(firm.mac, "_lima", lambda *_args, **_kwargs: None)
    firm.mac.paths.instance.mkdir(parents=True, exist_ok=True)
    if removed:
        monkeypatch.setattr(
            firm.mac, "_lima", lambda *_args, **_kwargs: firm.mac.paths.instance.rmdir()
        )
        monkeypatch.setattr(firm.mac.paths, "remove_instance", lambda: None)
        firm.replace()
        assert not marker.exists()
        assert firm.linux.calls == ["remove"]
    else:
        with pytest.raises(RuntimeError, match="not removed"):
            firm.replace()
        assert marker.exists()
        assert firm.linux.calls == []


def test_replace_refuses_active_mac_lease(firm):
    import os

    from firm_sandbox.mac_station import station_lock

    descriptor = station_lock(firm.firm_locks, 1)
    try:
        with pytest.raises(OSError, match="already leased"):
            firm.replace()
    finally:
        os.close(descriptor)


def test_mac_replace_refuses_active_mac_lease_and_a_running_firm(firm):
    import os

    from firm_sandbox.mac_station import station_lock

    # FakeLinux reports running from construction, so mac_replace refuses at once.
    with pytest.raises(RuntimeError, match="Stop the firm"):
        firm.mac_replace()
    # Once the firm is stopped, a held station lock is its own, separate refusal.
    firm.linux.running = False
    descriptor = station_lock(firm.firm_locks, 1)
    try:
        with pytest.raises(OSError, match="already leased"):
            firm.mac_replace()
    finally:
        os.close(descriptor)


def test_replace_uses_configured_image_across_sandbox_reopen(firm, tmp_path, monkeypatch):
    image = tmp_path / "replacement-image"
    image.mkdir()
    metadata = marked_image(enrollment={"stations": 1})
    guest_control.save(image / "image.json", metadata)
    config_path = firm.config.root / "sandbox.toml"
    config_path.write_text(f'[mac]\nimage = "{image}"\ncpus = 6\nmemory_gib = 12\n')
    original = firm.mac._state()
    configured = Sandbox(Config.load(config_path))
    configured._file_server = firm._file_server
    cloned = []

    def ready(box):
        vm = box.mac
        monkeypatch.setattr(vm, "_image_base", lambda selected: None)
        monkeypatch.setattr(vm, "_validate_config", lambda: None)
        monkeypatch.setattr(vm, "_running", lambda: True)
        monkeypatch.setattr(vm.network, "ready", lambda: True)
        monkeypatch.setattr(vm, "_install_controller", lambda: None)
        monkeypatch.setattr(vm, "_ensure_boot_script", lambda: None)
        monkeypatch.setattr(vm, "_rpc", MockRPC())
        monkeypatch.setattr(vm.paths, "prepare", lambda: None)
        monkeypatch.setattr(vm.paths, "synchronize", lambda: None)

    ready(configured)
    monkeypatch.setattr(configured, "down", lambda: None)
    monkeypatch.setattr(configured.mac, "down", lambda: None)
    monkeypatch.setattr(configured.mac, "_lima", lambda *args: None)
    monkeypatch.setattr(configured.mac.paths, "remove_instance", lambda: None)
    # The old guest is stopped for removal; the fresh clone boots.
    monkeypatch.setattr(configured.mac, "_running", lambda: bool(cloned))

    def clone(selected):
        cloned.append(selected)
        guest_control.save(
            configured.mac.record,
            {
                **original,
                "image": str(selected),
                "created_with_release": metadata["created_with_release"],
                "compatibility_generation": metadata["compatibility_generation"],
                "image_id": metadata["image_id"],
                "generation": "replacement-generation",
            },
        )

    monkeypatch.setattr(configured.mac, "_clone", clone)
    configured.replace()
    assert cloned == [image]
    assert configured.mac.image == image
    assert configured.mac_status()["configured"] is True
    assert configured.mac_status()["enrolled"] is True

    reopened = Sandbox(Config.load(config_path))
    reopened._file_server = firm._file_server
    ready(reopened)
    reopened.up()
    assert reopened.mac._state()["image"] == str(image)
    assert reopened.mac._state()["generation"] != original["generation"]


def test_up_rejects_record_bound_to_different_firm(firm):
    record = firm.mac._state()
    record["firm_id"] = "another-firm"
    guest_control.save(firm.mac.record, record)
    with pytest.raises(ValueError, match="another firm identity"):
        firm.up()
    assert firm.mac._rpc.calls == []
    assert firm.linux.calls == ["up", "force-down"]


def test_lifecycle_allows_exclusive_to_shared_but_rejects_shared_to_exclusive(tmp_path):
    holder = SimpleNamespace(firm_locks=tmp_path / "locks")

    @guest_control.lifecycle()
    def shared(_holder):
        return "shared"

    @guest_control.lifecycle(exclusive=True)
    def exclusive(_holder):
        return shared(holder)

    assert exclusive(holder) == "shared"

    @guest_control.lifecycle(exclusive=True)
    def another_exclusive(_holder):
        return "exclusive"

    @guest_control.lifecycle()
    def outer_shared(_holder):
        return another_exclusive(holder)

    with pytest.raises(RuntimeError, match="upgrade"):
        outer_shared(holder)


def test_clone_rejects_running_source_image_before_copy(tmp_path, monkeypatch):
    image = tmp_path / "image"
    base = image / "lima" / "base"
    base.mkdir(parents=True)
    image.chmod(0o700)
    (image / "lima").chmod(0o700)
    base.chmod(0o700)
    save(image / "image.json", marked_image(control_user="firmvm"))
    root = Path("/tmp").resolve() / f"fv-{uuid4().hex[:10]}"
    monkeypatch.setattr(mac_vm.shutil, "which", lambda _: "/opt/homebrew/bin/limactl")
    box = mac_vm.MacVM(settings(root, tmp_path), image=image)

    def running_source(_commands, argv, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=b'{"name":"base","status":"Running"}\n', stderr=b""
        )

    monkeypatch.setattr(mac_vm.Commands, "run", running_source)
    with pytest.raises(ValueError, match="stopped"):
        box._clone(image)
    assert not box.instance.exists()
    shutil.rmtree(root, ignore_errors=True)


def test_clone_refuses_mismatched_image_before_creating_guest_state(tmp_path, monkeypatch):
    image = tmp_path / "image"
    image.mkdir(mode=0o700)
    metadata = marked_image(control_user="firmvm")
    metadata["compatibility_generation"] += 1
    save(image / "image.json", metadata)
    root = tmp_path / "firm"
    monkeypatch.setattr(mac_vm.shutil, "which", lambda _: "/opt/homebrew/bin/limactl")
    box = mac_vm.MacVM(settings(root, tmp_path), image=image)
    with pytest.raises(ValueError, match="requires generation"):
        box._clone(image)
    assert not box.instance.exists()
    monkeypatch.setattr(box.paths, "prepare", lambda: pytest.fail("projected Lima files"))
    with pytest.raises(ValueError, match="requires generation"):
        box.up()
    assert not box.instance.exists()


def test_down_uses_vz_stop_when_filter_is_unavailable(firm, monkeypatch):
    running = iter([True, False])
    firm.mac._running = lambda: next(running)
    firm.mac.instance.mkdir(parents=True)
    monkeypatch.setattr(firm.mac, "_validate_config", lambda: None)
    calls = []
    firm.mac._lima = lambda *args, **kwargs: calls.append(args)
    firm.mac.network = SimpleNamespace(
        paths={"pid": firm.mac.root / "filter.pid"},
        ready=lambda: False,
        stop=lambda: calls.append(("filter-stop",)),
    )
    assert firm.down() == {"running": False}
    assert calls == [("stop", "--force", "--tty=false", "mac"), ("filter-stop",)]
    assert firm.linux.calls == [("runs", {}), "down"]


@pytest.mark.parametrize("operation", ["down", "force_down"])
def test_broken_vm_can_be_force_stopped_without_losing_disk(firm, monkeypatch, operation):
    firm.mac.instance.mkdir(parents=True, exist_ok=True)
    disk = firm.mac.instance / "diffdisk"
    disk.write_bytes(b"retained guest data")
    state = "Broken"
    calls = []

    def lima(*args, **kwargs):
        nonlocal state
        calls.append(args)
        if args[0] == "stop":
            state = "Stopped"
        return SimpleNamespace(stdout=json.dumps({"name": "mac", "status": state}).encode())

    monkeypatch.setattr(firm.mac, "_validate_config", lambda: None)
    monkeypatch.setattr(firm.mac, "_lima", lima)
    monkeypatch.setattr(firm.mac.network, "stop", lambda: None)
    assert getattr(firm, operation)() == {"running": False}
    assert ("stop", "--force", "--tty=false", "mac") in calls
    assert disk.read_bytes() == b"retained guest data"


@pytest.mark.parametrize(
    ("status", "expected"), [("running", True), ("Running", True), ("STOPPED", False)]
)
def test_running_check_normalizes_lima_status_case(firm, monkeypatch, status, expected):
    firm.mac.instance.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        firm.mac,
        "_lima",
        lambda *a, **k: SimpleNamespace(
            stdout=json.dumps({"name": "mac", "status": status}).encode()
        ),
    )
    assert firm.mac._running() is expected


def test_status_reports_broken_instance_without_guest_rpc(firm, monkeypatch):
    def broken():
        raise mac_vm.VMStateError("Broken")

    monkeypatch.setattr(firm.mac, "_running", broken)
    result = firm.infrastructure_status()
    assert result["state"] == "recovery_required" and result["running"] is None
    assert "Broken" in result["errors"]["mac"] and firm.mac._rpc.calls == []


@pytest.mark.parametrize("live", [False, True])
def test_replace_persistent_broken_vm_only_after_host_shutdown(firm, monkeypatch, live):
    from firm_sandbox import lima_paths as paths_module

    firm.mac.instance.mkdir(parents=True, exist_ok=True)
    disk = firm.mac.instance / "diffdisk"
    disk.write_bytes(b"retained guest data")
    calls = []

    def lima(*args, **kwargs):
        calls.append(args)
        if args[0] == "delete":
            shutil.rmtree(firm.mac.paths.instance)
        return SimpleNamespace(stdout=b'{"name":"mac","status":"Broken"}')

    for name in ("_validate_config", "_image_base"):
        monkeypatch.setattr(firm.mac, name, lambda *args: None)
    monkeypatch.setattr(firm.mac, "_lima", lima)
    monkeypatch.setattr(firm, "up", lambda: {"replaced": True})
    monkeypatch.setattr(firm.mac.network, "stop", lambda: None)
    processes = f"12345 limactl hostagent {firm.mac.paths.instance}" if live else "12345 /bin/other"
    monkeypatch.setattr(
        paths_module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=processes.encode(), returncode=0),
    )
    if live:
        with pytest.raises(RuntimeError, match="host process is still running"):
            firm.replace()
        assert disk.read_bytes() == b"retained guest data"
        assert not any(call[0] == "delete" for call in calls)
    else:
        assert firm.replace() == {"replaced": True}
        assert not disk.exists()
        # The firm's shutdown stops the VM, and removal confirms it stopped again.
        assert [call[0] for call in calls if call[0] != "list"] == ["stop", "stop", "delete"]


def test_installed_sudoers_matches_actual_rpc_command(firm, monkeypatch):
    scripts, commands = [], []

    def lima(*args, **kwargs):
        if args[0] == "copy" and args[-1].endswith("/install-controller.sh"):
            scripts.append(Path(args[1]).read_text())

    monkeypatch.setattr(firm.mac, "_lima", lima)
    monkeypatch.setattr(firm.mac, "_shell", lambda *args, **kwargs: None)
    firm.mac._install_controller()
    popen = guest_control.subprocess.Popen

    def request(argv, **kwargs):
        commands.append(argv)
        return popen([sys.executable, "-c", 'print(\'{"ok":true,"result":{}}\')'], **kwargs)

    monkeypatch.setattr(guest_control.subprocess, "Popen", request)
    monkeypatch.setattr(firm.mac.network, "boundary", lambda **_: guest_control.Boundary.ENFORCED)
    mac_vm.MacVM._rpc(firm.mac, "status")
    argv = commands[0]
    sudo = argv.index("/usr/bin/sudo")
    assert argv[sudo + 1] == "-n" and "mac" in argv[:sudo] and "sudo" not in argv
    command = " ".join(argv[sudo + 2 :])
    assert f"firmvm ALL=(root) NOPASSWD: {command}'" in scripts[0]
    assert "trap '/bin/rm -f /Users/firmvm/install-controller.sh' EXIT" in scripts[0]
    assert "/bin/rm /Users/firmvm/firm-controller.py" in scripts[0]
    # Root runs it: no program is found through PATH (heredoc bodies aside).
    body, ending = [], None
    for line in scripts[0].splitlines():
        if ending:
            ending = None if line.strip() == ending else ending
            continue
        ending = "PY" if line.endswith("<<'PY'") else None
        body.append(line.split(" ", 1)[0])
    assert all(
        word.startswith(("/", "{")) or "=" in word or word in ("set", "trap", "printf", "umask")
        for word in body
        if word
    ), body


@pytest.mark.parametrize("fails", [False, True])
def test_vm_start_marker_identifies_filter_process_and_clears_after_start(firm, monkeypatch, fails):
    import os

    from firm_sandbox import host_network

    network_pid = os.getpid() + 10000
    monkeypatch.setattr(
        host_network,
        "ask",
        lambda *_a: {
            "policy": firm.mac.network.policy,
            "pid": network_pid,
            "attached": True,
            "failed": False,
        },
    )
    for name in ("prepare", "synchronize", "mark_running"):
        monkeypatch.setattr(firm.mac.paths, name, lambda: None)
    monkeypatch.setattr(firm.mac, "_running", lambda: False)
    monkeypatch.setattr(firm.mac, "_validate_config", lambda: None)
    monkeypatch.setattr(firm.mac, "_install_controller", lambda: None)
    monkeypatch.setattr(firm.mac, "_ensure_boot_script", lambda: None)
    monkeypatch.setattr(firm.mac, "info", lambda: {"ready": True})
    firm.mac.network.paths["config"].parent.mkdir(parents=True, mode=0o700)
    started = []

    def lima(*args, **_kwargs):
        assert int(firm.mac.network.paths["pid"].read_text()) == network_pid
        started.append(args[0])
        if args[0] == "start" and fails:
            raise RuntimeError("startup failed")
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(firm.mac, "_lima", lima)
    if fails:
        with pytest.raises(RuntimeError, match="startup failed"):
            firm.up()
        assert started == ["start", "stop"]
    else:
        result = firm.up()
        assert result["mac"]["stations"][0]["ready"] is True
        assert started == ["start"]
    assert not firm.mac.network.paths["pid"].exists()


def test_mac_vm_owns_the_linux_vm_lifecycle_and_leaves_runs_to_the_sandbox():
    from firm_sandbox.linux_vm import LinuxVM

    for name in ("up", "down", "force_down", "remove"):
        assert callable(getattr(mac_vm.MacVM, name)) and callable(getattr(LinuxVM, name))
    for name in (
        "replace",
        "start",
        "stop",
        "status",
        "wait",
        "runs",
        "logs",
        "provision",
        "write_file",
        "import_tree",
    ):
        assert not hasattr(mac_vm.MacVM, name)


# ---- the Mac VM's own lifecycle ------------------------------------------------------


@pytest.fixture
def booting(firm, monkeypatch):
    """A stopped, recorded Mac VM whose boot steps are recorded instead of run."""
    vm, calls = firm.mac, []
    for name in ("prepare", "synchronize", "mark_running"):
        monkeypatch.setattr(vm.paths, name, lambda: None)
    monkeypatch.setattr(vm.paths, "preserve_logs", lambda: calls.append("preserve-logs"))
    monkeypatch.setattr(vm, "_running", lambda: False)
    monkeypatch.setattr(vm, "_validate_config", lambda: None)
    monkeypatch.setattr(vm, "_install_controller", lambda: calls.append("install-controller"))
    monkeypatch.setattr(vm, "_ensure_boot_script", lambda: calls.append("ensure-boot-script"))
    monkeypatch.setattr(vm, "info", lambda: {"ready": True})
    monkeypatch.setattr(vm.network, "ready", lambda: True)
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    monkeypatch.setattr(vm.network, "start", lambda: calls.append("filter-start"))
    monkeypatch.setattr(vm.network, "publish_lima_pid", lambda: calls.append("publish"))
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append(args[0]))
    return vm, calls


def test_up_boots_behind_the_filter_then_initializes_the_guest(booting):
    vm, calls = booting
    save(vm.record, {**vm._state(), "ready": False})  # a fresh clone
    checks = []
    assert vm.up(checkpoint=lambda: checks.append(list(calls))) == {"ready": True}
    assert calls == [
        "filter-stop",
        "filter-start",
        "publish",
        "start",
        "install-controller",
        "ensure-boot-script",
    ]
    assert checks == [
        [],
        ["filter-stop", "filter-start", "publish"],
        ["filter-stop", "filter-start", "publish", "start"],
    ]
    # MacVM owns machine initialization; Sandbox owns the recorded account sessions.
    # Then any password change an earlier call left half done is finished, and a fresh
    # clone is given its own control password, both through the controller's sudo rule.
    assert vm._rpc.calls == [
        ("initialize", {"firm_id": "firm", "proxy": proxy_setting(vm)}),
        ("settle-credentials", {"timeout": 400}),
        ("rotate-control-password", {"timeout": 400}),
    ]
    assert vm._state()["ready"] is True
    # A VM that was ready before keeps its password across boots.
    vm._rpc.calls.clear()
    vm.up()
    assert [op for op, _ in vm._rpc.calls] == ["initialize", "settle-credentials"]
    # The host network's reconstructible files sit in the VM's own directory.
    assert json.loads(vm.network.paths["config"].read_text())["scratch"] == str(
        vm.settings.root / "scratch"
    )


def test_a_resized_mac_keeps_its_disk_and_records_the_new_definition(booting):
    vm, _ = booting
    vm.instance.mkdir(mode=0o700, parents=True)
    (vm.instance / "lima.yaml").write_text("vmType: vz\ncpus: 2\nmemory: 4GiB\ndisk: 120GiB\n")
    record = json.loads(vm.record.read_text())
    save(vm.record, {**record, "cpus": 2, "memory_gib": 4})

    assert vm.up() == {"ready": True}

    definition = yaml.safe_load((vm.instance / "lima.yaml").read_text())
    # Only the two resource keys move; the disk the station lives on is not touched.
    assert (definition["cpus"], definition["memory"]) == (6, "12GiB")
    assert (definition["disk"], definition["vmType"]) == ("120GiB", "vz")
    state = json.loads(vm.record.read_text())
    assert (state["cpus"], state["memory_gib"]) == (6, 12)


def test_up_ensures_boot_script_even_when_already_running(booting, monkeypatch):
    vm, calls = booting
    monkeypatch.setattr(vm, "_running", lambda: True)
    vm.up()
    assert calls == ["install-controller", "ensure-boot-script"]


@pytest.mark.parametrize("installed", ["current", "old", "missing", "owner", "group", "mode"])
def test_ensure_boot_script_checks_guest_digest_and_metadata(firm, monkeypatch, installed):
    vm, calls = firm.mac, []
    content = vm._boot_script_source()
    digest = mac_vm.hashlib.sha256(content).hexdigest()
    metadata = {
        "owner": "firmvm:wheel:755",
        "group": "root:staff:755",
        "mode": "root:wheel:700",
    }.get(installed, "root:wheel:755")

    def shell(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=1 if installed == "missing" else 0,
            stdout=f"{metadata}\n{'old' if installed == 'old' else digest}  path\n".encode(),
        )

    monkeypatch.setattr(vm, "_shell", shell)
    vm._ensure_boot_script()
    assert "/usr/bin/stat" in calls[0][0][-1] and "/usr/bin/shasum" in calls[0][0][-1]
    assert len(calls) == (1 if installed == "current" else 2)
    if installed != "current":
        argv, kwargs = calls[1]
        # firmvm's only passwordless sudo is the controller: root via its password file.
        assert argv[:2] == ("/bin/zsh", "-c")
        assert argv[2].startswith("/usr/bin/sudo -k -S")
        assert argv[2].endswith("< /Users/firmvm/password")
        script = shlex.split(argv[2])[-3]
        assert content.decode() in script
        assert '/usr/sbin/chown root:wheel "$temporary"' in script
        assert '/bin/chmod 755 "$temporary"' in script
        assert f'/bin/mv -f "$temporary" {mac_vm.BOOT_SCRIPT}' in script
        assert script.endswith("/bin/sync\n") and "password" not in script


@pytest.mark.parametrize("available_after", [0, 3, 60, None])
def test_waiting_init_script_waits_for_executable_and_has_a_bound(tmp_path, available_after):
    agent = tmp_path / "agent"
    agent.write_text('#!/bin/sh\nprintf "%s" "$*"\n')
    agent.chmod(0o755 if available_after == 0 else 0o644)
    # Run the actual shell loop against a harmless local agent and a virtual sleep.
    prefix = f"""slept=0
sleep() {{
  slept=$((slept + 1))
  [ "$slept" -ne {available_after or -1} ] || /bin/chmod 755 {agent}
}}
"""
    script = (
        mac_vm.MacVM._boot_script_source()
        .decode()
        .replace(mac_vm.BOOT_AGENT, str(agent))
        .replace("/bin/sleep", "sleep")
    )
    result = subprocess.run(
        ["/bin/sh", "-s"], input=prefix + script, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == (1 if available_after is None else 0)
    if available_after is None:
        assert "slept=60" in result.stderr and "slept=61" not in result.stderr
        assert "is not executable" in result.stderr
    else:
        assert result.stdout.endswith("fake-cloud-init")


@pytest.mark.parametrize("failure", [None, "command", "timeout"])
def test_boot_watcher_gives_ready_guest_grace_and_attempts_only_once(firm, monkeypatch, failure):
    vm, clock, calls = firm.mac, [0], []

    class Finished:
        def wait(self, seconds):
            clock[0] += seconds
            return clock[0] > 40

        def is_set(self):
            return False

    def shell(*args, **kwargs):
        calls.append((clock[0], args, kwargs))
        if "fake-cloud-init" in args[-1]:
            if failure == "command":
                raise mac_vm.CommandError("failed")
            if failure == "timeout":
                raise subprocess.TimeoutExpired("fake ssh", 30)
        # SSH initially unavailable, then the mount point exists without an agent.
        return SimpleNamespace(returncode=255 if clock[0] == 2 else int(clock[0] == 4))

    monkeypatch.setattr(mac_vm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vm, "_shell", shell)
    vm._watch_boot(Finished())
    recoveries = [(at, args, kw) for at, args, kw in calls if "fake-cloud-init" in args[-1]]
    assert len(recoveries) == 1
    at, args, kwargs = recoveries[0]
    assert at == 6 + mac_vm.BOOT_GRACE
    assert args == (
        "/bin/zsh",
        "-c",
        f"[ -x {mac_vm.BOOT_AGENT} ] && [ ! -e {mac_vm.BOOT_DONE} ] && "
        + mac_vm.mac_sudo_command(mac_vm.BOOT_AGENT, "fake-cloud-init"),
    )
    assert kwargs["timeout"] == 30


@pytest.mark.parametrize("condition", ["ssh-timeout", "not-ready", "normal-boot", "finished"])
def test_boot_watcher_does_not_rescue_without_sustained_readiness(firm, monkeypatch, condition):
    clock, calls = [0], []

    class Finished:
        def wait(self, seconds):
            clock[0] += seconds
            return clock[0] > 30

        def is_set(self):
            return condition == "finished"

    def shell(*args, **kwargs):
        calls.append(args)
        assert "fake-cloud-init" not in args[-1]
        if condition == "ssh-timeout":
            raise subprocess.TimeoutExpired("fake ssh", 5)
        # Normal init completes before the grace period expires.
        return SimpleNamespace(returncode=int(condition == "not-ready" or clock[0] >= 10))

    monkeypatch.setattr(mac_vm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(firm.mac, "_shell", shell)
    firm.mac._watch_boot(Finished())
    assert calls


def test_lost_race_is_recovered_while_lima_start_is_waiting(booting, monkeypatch):
    vm, calls = booting
    recovered = threading.Event()
    monkeypatch.setattr(mac_vm, "BOOT_POLL", 0.001)
    monkeypatch.setattr(mac_vm, "BOOT_GRACE", 0.002)
    attempts = []

    def shell(*args, **kwargs):
        if "fake-cloud-init" in args[-1]:
            attempts.append(args)
            recovered.set()
        return SimpleNamespace(returncode=0)

    def lima(*args, **kwargs):
        assert args == ("start", "--tty=false", "mac") and kwargs == {"timeout": 180}
        assert recovered.wait(2), "Lima cannot complete until the watcher recovers init"
        calls.append("start")

    monkeypatch.setattr(vm, "_shell", shell)
    monkeypatch.setattr(vm, "_lima", lima)
    vm.up()
    assert len(attempts) == 1 and calls[-1] == "ensure-boot-script"
    assert not any(thread.name == "mac-boot" for thread in threading.enumerate())


@pytest.mark.parametrize("fails", [False, True])
def test_start_always_joins_watcher_before_returning(firm, monkeypatch, fails):
    watching, joined = threading.Event(), threading.Event()

    def watch(finished):
        watching.set()
        assert finished.wait(2)
        joined.set()

    def lima(*args, **kwargs):
        assert watching.wait(2)
        if fails:
            raise subprocess.TimeoutExpired("fake lima", 180)

    monkeypatch.setattr(firm.mac, "_watch_boot", watch)
    monkeypatch.setattr(firm.mac, "_lima", lima)
    if fails:
        with pytest.raises(subprocess.TimeoutExpired):
            firm.mac._start()
    else:
        firm.mac._start()
    assert joined.is_set()


def test_a_running_mac_refuses_to_be_resized(booting, monkeypatch):
    vm, _ = booting
    vm.instance.mkdir(mode=0o700, parents=True)
    (vm.instance / "lima.yaml").write_text("cpus: 2\nmemory: 4GiB\n")
    save(vm.record, {**json.loads(vm.record.read_text()), "cpus": 2, "memory_gib": 4})
    monkeypatch.setattr(vm, "_running", lambda: True)
    with pytest.raises(mac_vm.VMStateError):
        vm.up()
    assert yaml.safe_load((vm.instance / "lima.yaml").read_text())["cpus"] == 2


def _compliant_lima_config(**overrides):
    cfg = {
        "os": "Darwin",
        "vmType": "vz",
        "networks": [{"lima": "user-v2"}],
        "mounts": [],
        "additionalDisks": [],
        "plain": True,
        "propagateProxyEnv": False,
        "ssh": {
            "loadDotSSHPubKeys": False,
            "forwardAgent": False,
            "forwardX11": False,
            "forwardX11Trusted": False,
        },
        "cpus": 6,
        "memory": "12GiB",
    }
    cfg.update(overrides)
    return cfg


def test_validate_config_accepts_an_edited_but_still_compliant_lima_yaml(firm):
    # Unlike the retired digest check, an edit that keeps the isolation contract
    # (here, a resize) is not a reason to refuse the VM.
    vm = firm.mac
    vm.instance.mkdir(mode=0o700, parents=True)
    cfg = _compliant_lima_config(cpus=8, memory="16GiB")
    (vm.instance / "lima.yaml").write_text(yaml.safe_dump(cfg))
    vm._validate_config()


def test_validate_config_refuses_a_noncompliant_lima_yaml(firm):
    vm = firm.mac
    vm.instance.mkdir(mode=0o700, parents=True)
    cfg = _compliant_lima_config(mounts=[{"location": "/tmp"}])
    (vm.instance / "lima.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="isolated network/storage contract"):
        vm._validate_config()


def test_validate_config_refuses_ssh_forwarding_or_owner_key_loading(firm):
    vm = firm.mac
    vm.instance.mkdir(mode=0o700, parents=True)
    cfg = _compliant_lima_config(
        ssh={
            "loadDotSSHPubKeys": False,
            "forwardAgent": True,
            "forwardX11": False,
            "forwardX11Trusted": False,
        }
    )
    (vm.instance / "lima.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="SSH forwarding or owner-key loading"):
        vm._validate_config()


def test_vm_boot_initializes_the_machine_without_managing_station_sessions(booting):
    vm, _ = booting
    # Sandbox owns station session and awake coordination under the account locks.
    assert vm.stations == (1,) and vm.enrolled()
    assert vm.up() == {"ready": True}
    # Then any password change an earlier call left half done is finished, before
    # anything asks for a recorded password.
    assert vm._rpc.calls == [
        ("initialize", {"firm_id": "firm", "proxy": proxy_setting(vm)}),
        ("settle-credentials", {"timeout": 400}),
    ]


def test_the_mac_proxy_needs_no_credentials_and_its_settings_are_not_recorded(booting):
    vm, _ = booting
    vm.up()
    # Gateway-addressed TCP reaches host loopback at the same port: the filter's exception.
    assert proxy_setting(vm) == {"host": "192.168.5.2", "port": vm.network.proxy_port}
    assert vm.network.proxy_role == "mac"
    config = json.loads(vm.network.paths["config"].read_text())
    assert (config["proxy_role"], config["proxy_port"]) == ("mac", vm.network.proxy_port)
    # Nothing about egress is cloned into the record, so no setting forces a re-clone.
    assert "allow_tcp" not in vm._state()
    with pytest.raises(RuntimeError, match="no credentials"):
        vm.network.proxy_url("analyst")


def proxy_setting(vm):
    return {"host": vm.network.gateway, "port": vm.network.proxy_port}


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (1, []),
        (2, ["filter-stop", "filter-start", "publish", "stop", "preserve-logs", "filter-stop"]),
        (
            3,
            [
                "filter-stop",
                "filter-start",
                "publish",
                "start",
                "stop",
                "preserve-logs",
                "filter-stop",
            ],
        ),
    ],
)
def test_a_failed_checkpoint_abandons_the_boot_and_powers_off_what_started(
    booting, phase, expected
):
    # Logs are archived on the way down whenever the boot is abandoned.
    vm, calls = booting
    checks = []

    def checkpoint():
        checks.append(len(checks) + 1)
        if len(checks) == phase:
            raise RuntimeError("Firm was force-stopped")

    with pytest.raises(RuntimeError, match="force-stopped"):
        vm.up(checkpoint=checkpoint)
    assert calls == expected
    assert vm._rpc.calls == [] and not vm.network.paths["pid"].exists()


def test_force_down_powers_off_before_cleanup_without_lifecycle_lock(firm, monkeypatch):
    def forbidden_lock(*args, **kwargs):
        raise AssertionError("emergency shutdown must not take the ordinary lifecycle lock")

    vm, calls = firm.mac, []
    vm.instance.mkdir(parents=True)
    monkeypatch.setattr(guest_control, "lifecycle_locked", forbidden_lock)
    monkeypatch.setattr(vm, "_validate_config", lambda: None)
    monkeypatch.setattr(vm, "_running", lambda: False)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    assert vm.force_down() == {"running": False}
    assert calls == [("stop", "--force", "--tty=false", "mac"), "filter-stop"]


@pytest.mark.parametrize("operation", ["force_down", "down"])
def test_no_stop_is_sent_into_a_runtime_the_paths_refuse(firm, monkeypatch, operation):
    """test_lima_paths covers what is refused; a refusal must reach Lima as nothing at all."""
    vm, calls = firm.mac, []
    vm.instance.mkdir(parents=True)

    def refuse():
        raise RuntimeError("Short VM runtime is owned by another firm")

    monkeypatch.setattr(vm.paths, "verify_runtime", refuse)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    with pytest.raises(RuntimeError, match="owned by another firm"):
        getattr(vm, operation)()
    assert calls == []


@pytest.mark.parametrize("operation", ["force_down", "down"])
def test_a_filter_that_will_not_stop_does_not_cost_the_stopped_vms_logs(
    firm, monkeypatch, operation
):
    vm, calls = firm.mac, []
    vm.instance.mkdir(parents=True)
    running = iter([True, False]) if operation == "down" else iter([False])
    monkeypatch.setattr(vm, "_validate_config", lambda: None)
    monkeypatch.setattr(vm, "_running", lambda: next(running))
    monkeypatch.setattr(vm.network, "ready", lambda: True)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append("lima-stop"))
    monkeypatch.setattr(vm.paths, "preserve_logs", lambda: calls.append("logs"))
    monkeypatch.setattr(vm.paths, "mark_stopped", lambda: calls.append("mark-stopped"))

    def stuck():
        calls.append("filter-stop")
        raise RuntimeError("network filter did not exit")

    monkeypatch.setattr(vm.network, "stop", stuck)
    with pytest.raises(RuntimeError, match="did not exit"):
        getattr(vm, operation)()
    # The running record stays, because the filter did; the logs were kept before that.
    assert calls == ["lima-stop", "logs", "filter-stop"]


def test_down_falls_back_to_force_stop_when_the_graceful_stop_fails(firm, monkeypatch):
    vm, calls = firm.mac, []
    running = iter([True, False])
    monkeypatch.setattr(vm, "_running", lambda: next(running))
    monkeypatch.setattr(vm.network, "ready", lambda: True)
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))

    def lima(*args, **kwargs):
        calls.append(args)
        if "--force" not in args:
            raise RuntimeError("guest did not answer")

    monkeypatch.setattr(vm, "_lima", lima)
    assert vm.down() == {"running": False}
    assert calls == [
        ("stop", "--tty=false", "mac"),
        ("stop", "--force", "--tty=false", "mac"),
        "filter-stop",
    ]


@pytest.mark.parametrize("removed", [True, False])
def test_remove_stops_then_discards_the_disk_and_record(firm, monkeypatch, removed):
    vm, calls = firm.mac, []
    vm.paths.instance.mkdir(parents=True)
    monkeypatch.setattr(vm, "down", lambda: calls.append("down"))
    monkeypatch.setattr(vm, "_running", lambda: False)
    monkeypatch.setattr(vm.paths, "prepare", lambda: None)

    def lima(*args, **kwargs):
        calls.append(args)
        if removed:
            vm.paths.instance.rmdir()

    monkeypatch.setattr(vm, "_lima", lima)
    if removed:
        assert vm.remove() == {"removed": True}
        assert not vm.record.exists()
    else:
        with pytest.raises(RuntimeError, match="not removed"):
            vm.remove()
        assert vm.record.exists()
    assert calls == ["down", ("delete", "--force", "--tty=false", "mac")]


def test_remove_of_a_never_cloned_mac_vm_succeeds_without_lima(firm, monkeypatch):
    vm = firm.mac
    vm.record.unlink()

    def lima(*args, **kwargs):
        raise AssertionError(f"no Lima instance to act on: {args}")

    monkeypatch.setattr(vm, "_lima", lima)
    assert vm.remove() == {"removed": True}
    assert not vm.instance.exists() and not vm.record.exists()


def test_a_removal_interrupted_after_the_instance_went_is_finished_by_repeating_it(
    firm, monkeypatch
):
    """The record is unlinked last, so it can outlive the lima.yaml its check would read."""
    vm = firm.mac
    assert vm.record.exists() and not vm.instance.exists()

    def lima(*args, **kwargs):
        raise AssertionError(f"no Lima instance to act on: {args}")

    monkeypatch.setattr(vm, "_lima", lima)
    assert vm.remove() == {"removed": True}
    assert not vm.record.exists()


def test_replace_of_a_firm_whose_mac_was_never_cloned_starts_it_fresh(firm, monkeypatch):
    firm.mac.image.mkdir(exist_ok=True)
    save(firm.mac.image / "image.json", marked_image(enrollment={"stations": 1}))
    firm.mac.record.unlink()
    monkeypatch.setattr(firm.mac, "_image_base", lambda image: None)
    monkeypatch.setattr(firm.mac, "_lima", lambda *args, **kwargs: pytest.fail(str(args)))
    monkeypatch.setattr(firm, "up", lambda: {"replaced": True})
    assert firm.replace() == {"replaced": True}
    assert "remove" in firm.linux.calls and not firm.mac.record.exists()


def test_default_stop_timeout_exceeds_the_guest_settlement():
    from firm_sandbox import linux_vm_guest

    assert sandbox.STOP_TIMEOUT > linux_vm_guest.SETTLE_TIMEOUT


def test_first_controller_install_changes_the_control_password_through_the_controller(
    firm, monkeypatch
):
    scripts = []

    def lima(*args, **kwargs):
        if args[0] == "copy" and args[-1].endswith("/install-controller.sh"):
            scripts.append(Path(args[1]).read_text())

    monkeypatch.setattr(firm.mac, "_lima", lima)
    monkeypatch.setattr(firm.mac, "_shell", lambda *args, **kwargs: None)
    firm.mac._install_controller()
    state = firm.mac._state()
    state.update(ready=False, controller_digest=None)
    save(firm.mac.record, state)
    firm.mac._install_controller()
    # The install script is the last thing that needs the password file as it is; it
    # never changes the password itself, so an interrupted rotation cannot lock the
    # reinstall out. The rotation is up's, through the controller's passwordless sudo.
    for script in scripts:
        assert "rotate_control_password" not in script and "-passwd" not in script
    assert callable(runpy.run_path(mac_vm_guest.__file__)["rotate_control_password"])


def test_station_inventory_comes_from_image_then_the_clone_record(firm, tmp_path):
    vm = firm.mac
    vm.image.mkdir(exist_ok=True)
    save(vm.image / "image.json", marked_image(enrollment={"stations": 3}))
    # Once cloned, editing the configured image does not change account inventory.
    assert vm.stations == (1,)
    vm.record.unlink()
    assert vm.stations == (1, 2, 3)
    save(vm.image / "image.json", marked_image(control_user="firmvm"))
    assert vm.stations == () and not vm.enrolled()


@pytest.mark.parametrize("count", [True, "2", 0, 9])
def test_image_station_inventory_rejects_invalid_counts(firm, count):
    vm = firm.mac
    vm.record.unlink()
    vm.image.mkdir(exist_ok=True)
    save(vm.image / "image.json", marked_image(enrollment={"stations": count}))
    with pytest.raises(ValueError, match="between 1 and 8"):
        _ = vm.stations


def test_mac_guest_busy_retry_keeps_the_original_deadline(firm, monkeypatch):
    vm = firm.mac
    del vm._rpc  # exercise MacVM's transport instead of the fixture's reply stub
    clock = [0.0]
    calls = []
    monkeypatch.setattr(mac_vm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mac_vm.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def call(self, operation, *, timeout, **payload):
        calls.append((operation, timeout, payload))
        clock[0] += 10
        if len(calls) < 3:
            raise RuntimeError("The Mac is busy with another operation; try again")
        return {"ready": True}

    monkeypatch.setattr(guest_control.GuestControl, "_rpc", call)
    assert vm._rpc("station-session-ensure", station=2, timeout=75) == {"ready": True}
    assert [timeout for _, timeout, _ in calls] == [75, 64.75, 54.5]
    assert all(payload == {"station": 2} for _, _, payload in calls)


def test_busy_retry_stops_at_the_call_deadline(firm, monkeypatch):
    vm = firm.mac
    del vm._rpc
    clock = [0.0]
    calls = []
    monkeypatch.setattr(mac_vm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mac_vm.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def busy(self, operation, *, timeout, **payload):
        calls.append(timeout)
        clock[0] += timeout
        raise RuntimeError("The Mac is busy with another operation; try again")

    monkeypatch.setattr(guest_control.GuestControl, "_rpc", busy)
    with pytest.raises(TimeoutError, match="deadline"):
        vm._rpc("station-status", station=1, timeout=12)
    assert calls == [12]


def test_non_busy_guest_error_is_not_retried(firm, monkeypatch):
    vm = firm.mac
    del vm._rpc
    calls = []

    def failure(self, operation, **payload):
        calls.append(operation)
        raise RuntimeError("wrong account")

    monkeypatch.setattr(guest_control.GuestControl, "_rpc", failure)
    with pytest.raises(RuntimeError, match="wrong account"):
        vm._rpc("station-status", station=1)
    assert calls == ["station-status"]


@pytest.mark.parametrize("count", [0, 3, 8])
def test_clone_records_the_image_account_inventory_once(firm, monkeypatch, count):
    vm = firm.mac
    vm.record.unlink()
    image = vm.image
    base = image / "lima" / "base"
    base.mkdir(parents=True, mode=0o700)
    metadata = marked_image(control_user="firmvm")
    if count:
        metadata["enrollment"] = {"stations": count}
    save(image / "image.json", metadata)
    for name in ("disk", "disk.patched", "vz-aux", "vz-hwmodel", "lima-version"):
        (base / name).write_bytes(b"image asset")
        (base / name).chmod(0o600)
    keys = image / "lima" / "_config"
    keys.mkdir(mode=0o700)
    for name in ("user", "user.pub"):
        (keys / name).write_text("image key")
        (keys / name).chmod(0o600)
    (base / "lima.yaml").write_text(
        yaml.safe_dump(
            {
                "os": "Darwin",
                "arch": "aarch64",
                "vmType": "vz",
                "images": [],
                "disk": "64GiB",
                "user": {"name": "firmvm"},
            }
        )
    )
    monkeypatch.setattr(vm, "_image_base", lambda selected: base)
    monkeypatch.setattr(
        vm.commands, "run", lambda argv, **kwargs: shutil.copyfile(argv[-2], argv[-1])
    )
    vm._clone(image)
    assert vm._state()["stations"] == count
    assert vm._state()["enrolled"] is bool(count)
    assert vm.stations == tuple(range(1, count + 1))
    assert vm.instance == vm.root / "lima/mac"
    save(image / "image.json", marked_image(enrollment={"stations": 1}))
    assert len(vm.stations) == count
