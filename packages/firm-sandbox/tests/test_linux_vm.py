"""The Linux VM driver without Lima: image policy, boot recovery, guest controller install."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from firm_sandbox import guest_control, linux_base, linux_vm
from firm_sandbox import lima_paths as paths_module
from firm_sandbox.compatibility import COMPATIBILITY_GENERATION
from firm_sandbox.models import VMSettings


def settings(root, tmp_path, firm_id="firm", cpus=8, memory_gib=24, disk_gib=60):
    return VMSettings(
        root,
        firm_locks=tmp_path / "locks",
        firm_id=firm_id,
        cpus=cpus,
        memory_gib=memory_gib,
        disk_gib=disk_gib,
    )


@pytest.fixture
def image(tmp_path):
    target = tmp_path / "linux.yaml"
    target.write_bytes(linux_vm.IMAGE.read_bytes())
    target.chmod(0o600)
    return target


@pytest.fixture
def vm(tmp_path, monkeypatch):
    base = tmp_path / "base-key.img"
    base.write_bytes(b"base disk")
    monkeypatch.setattr(linux_base, "base_image", lambda *a: base)
    return linux_vm.LinuxVM(settings(tmp_path / "firm", tmp_path), limactl="/usr/local/bin/limactl")


def test_image_policy_requires_linux_vz_usernet_without_mounts(image, tmp_path):
    assert linux_vm._validate_image(image)["networks"] == [{"lima": "user-v2"}]
    config = yaml.safe_load(image.read_text())
    config["mounts"] = [{"location": str(tmp_path)}]
    image.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="isolated VM policy"):
        linux_vm._validate_image(image)


def test_retained_vm_generation_refuses_before_resize(vm, monkeypatch):
    state = {
        "firm_id": vm.settings.firm_id,
        "subnet": vm.subnet,
        "cpus": 1,
        "memory_gib": 1,
        "created_with_release": "0.0.9",
        "compatibility_generation": COMPATIBILITY_GENERATION + 1,
        "image_id": hashlib.sha256(linux_vm.IMAGE.read_bytes()).hexdigest(),
    }
    vm._state = lambda: state
    monkeypatch.setattr(linux_vm, "resize_instance", lambda *args: pytest.fail("resized"))
    with pytest.raises(ValueError, match="0.0.9.*requires generation"):
        vm._validate_existing()


def test_compatible_release_keeps_original_image_provenance(vm):
    state = {
        "firm_id": vm.settings.firm_id, "subnet": vm.subnet,
        "cpus": vm.settings.cpus, "memory_gib": vm.settings.memory_gib,
        "created_with_release": "0.0.9",
        "compatibility_generation": COMPATIBILITY_GENERATION,
        "image_id": "0" * 64,
    }
    vm._state = lambda: state
    vm._validate_existing()
    assert state["created_with_release"] == "0.0.9" and state["image_id"] == "0" * 64


def test_moved_vm_keeps_recorded_network(vm, tmp_path):
    vm.root.mkdir(parents=True)
    vm.record.write_text(json.dumps({
        "firm_id": vm.settings.firm_id,
        "subnet": "192.168.68.0/24",
        "cpus": vm.settings.cpus,
        "memory_gib": vm.settings.memory_gib,
        "created_with_release": "0.1.0",
        "compatibility_generation": COMPATIBILITY_GENERATION,
        "image_id": "0" * 64,
    }))
    destination = tmp_path / "moved-firm"
    vm.root.rename(destination)
    moved = linux_vm.LinuxVM(settings(destination, tmp_path), limactl=vm.lima)
    moved._validate_existing()
    assert moved.subnet == moved.network.subnet == "192.168.68.0/24"


def test_incompatible_vm_refuses_before_projecting_lima_files(vm, monkeypatch):
    vm.record.parent.mkdir(parents=True, exist_ok=True)
    guest_control.save(vm.record, {
        "created_with_release": "0.0.9",
        "compatibility_generation": COMPATIBILITY_GENERATION + 1,
        "image_id": "0" * 64,
    })
    monkeypatch.setattr(vm.paths, "prepare", lambda: pytest.fail("projected Lima files"))
    with pytest.raises(ValueError, match="requires generation"):
        vm._prepare()


def test_firm_recipe_configures_identity_and_marks_this_boot_ready(image):
    config = yaml.safe_load(image.read_text())
    assert config["dns"] == ["127.0.0.1"] and config["hostResolver"] == {"enabled": False}
    script = config["provision"][0]["script"]
    assert "modprobe -a ip_tables iptable_nat nf_tables" in script
    assert "net getlocalsid" in script
    assert "make-ssl-cert generate-default-snakeoil" in script
    assert script.index("rm -f /run/firm-provisioned") < script.index("for command in")
    assert script.index("for command in") < script.index("mv /run/firm-provisioned.new")


def test_agy_wrapper_disables_updates_even_if_caller_enables_them(tmp_path):
    script = yaml.safe_load(linux_base.RECIPE.read_text())["provision"][0]["script"]
    wrapper = script.split("cat > /usr/local/bin/agy <<'AGY'\n", 1)[1].split("\nAGY\n", 1)[0]
    binary = tmp_path / "binary"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$AGY_CLI_DISABLE_AUTO_UPDATE" "$@"\n')
    binary.chmod(0o755)
    command = tmp_path / "agy"
    command.write_text(wrapper.replace('/usr/local/lib/agy/agy', str(binary)))
    command.chmod(0o755)
    result = subprocess.run([str(command), "argument with spaces"], check=True,
                            capture_output=True, text=True,
                            env={"AGY_CLI_DISABLE_AUTO_UPDATE": "false"})
    assert result.stdout.splitlines() == ["true", "argument with spaces"]


@pytest.mark.parametrize("valid_checksum", [False, True])
def test_agy_install_requires_verified_payload(
    tmp_path, valid_checksum
):
    script = yaml.safe_load(linux_base.RECIPE.read_text())["provision"][0]["script"]
    block = script[script.index('agy_stage=$(mktemp -d)'):script.index(
        "# Enforce the vendor's opt-out")]
    # Run the real install/check block as an ordinary user in a disposable prefix.
    block = block.replace('/usr/local/lib/agy', '"$INSTALL_ROOT"')
    block = block.replace("-o root -g root ", "")
    if sys.platform == "darwin":
        block = block.replace("sha512sum --check --status", "shasum -a 512 --check --status")
    binary = tmp_path / "antigravity"
    binary.write_text('#!/bin/sh\ntouch "$PROVIDER_STARTED"\nexit 99\n')
    binary.chmod(0o755)
    archive = tmp_path / "agy.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        target.add(binary, arcname="antigravity")
    installed = tmp_path / "installed"
    downloads = tmp_path / "downloads"
    started = tmp_path / "provider-started"
    checksum = hashlib.sha512(archive.read_bytes()).hexdigest() if valid_checksum else "0" * 128
    env = {
        **os.environ, "INSTALL_ROOT": str(installed), "FIXTURE": str(archive),
        "DOWNLOADS": str(downloads), "PROVIDER_STARTED": str(started),
        "AGY_URL": "https://unused.invalid/pinned.tar.gz",
        "AGY_SHA512": checksum,
    }
    command = (
        'set -eu\nCURL=fetch\n'
        'fetch() { echo download >> "$DOWNLOADS"; cp "$FIXTURE" "${@: -1}"; }\n' + block
    )

    def run():
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True)

    assert (run().returncode == 0) is valid_checksum
    assert not started.exists()
    assert (installed / "agy").exists() is valid_checksum
    assert downloads.read_text().splitlines() == ["download"]



def test_force_down_powers_off_before_cleanup_without_lifecycle_lock(vm, monkeypatch):
    vm.paths.prepare()
    vm.paths.instance.mkdir(mode=0o700)
    calls = []

    def forbidden_lock(*args, **kwargs):
        raise AssertionError("emergency shutdown must not take the ordinary lifecycle lock")

    monkeypatch.setattr(guest_control, "lifecycle_locked", forbidden_lock)
    monkeypatch.setattr(vm, "_running", lambda: False)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append(("network-stop", {})))
    result = vm.force_down()
    assert calls[0][0][:4] == ("stop", "--force", "--tty=false", "linux")
    assert calls[1][0] == "network-stop"
    assert result["running"] is False


@pytest.mark.parametrize(
    "operation",
    [
        lambda vm: vm.force_down(),
        lambda vm: vm.down(),
        lambda vm: vm._rpc("runs"),
        lambda vm: vm._shell("/bin/true"),
        lambda vm: vm.network.stop(),
        lambda vm: vm.network.start(),
        lambda vm: vm.network.set_egress(False),
        lambda vm: vm.network.reload_hosts(),
    ],
    ids=["force_down", "down", "rpc", "shell", "network-stop", "network-start", "egress", "hosts"],
)
def test_nothing_is_sent_into_a_runtime_that_is_another_firms(vm, monkeypatch, operation):
    """Lima, the guest controller and the filter's control socket all act on whatever the
    runtime holds, a fixed name under /private/tmp; its binding says whose VM that is."""
    from firm_sandbox import host_network

    vm.paths.prepare()
    vm.paths.instance.mkdir(mode=0o700)
    (vm.paths.runtime / "owner.json").write_text(
        json.dumps({"root": "/another/firm", "instance": "linux"})
    )
    sent = []
    monkeypatch.setattr(vm.commands, "run", lambda *args, **kwargs: sent.append(("lima", args)))
    monkeypatch.setattr(
        host_network, "ask", lambda path, command: sent.append(("filter", command)) or {}
    )
    monkeypatch.setattr(
        guest_control.subprocess, "Popen", lambda *args, **kwargs: sent.append(("rpc", args))
    )
    monkeypatch.setattr(
        host_network.subprocess, "Popen", lambda *args, **kwargs: sent.append(("filter", args))
    )
    with pytest.raises(RuntimeError) as refused:
        operation(vm)
    reason = refused.value.__cause__ or refused.value
    assert "owned by another firm" in str(reason)
    assert sent == []
    # Reporting still answers, conservatively, instead of raising.
    assert vm.network.ready() is False and sent == []


@pytest.mark.parametrize("operation", ["up", "down", "remove"])
def test_lifecycle_takes_the_firm_lifecycle_lock_exclusively(vm, monkeypatch, operation):
    import contextlib

    taken = []

    @contextlib.contextmanager
    def lifecycle_locked(locks, *, exclusive=False):
        taken.append((locks, exclusive))
        yield

    class Reached(Exception):
        pass

    def body(**_):
        taken.append("body")
        raise Reached

    monkeypatch.setattr(guest_control, "lifecycle_locked", lifecycle_locked)
    monkeypatch.setattr(vm, "_prepare", body)
    monkeypatch.setattr(vm, "_running", body)
    monkeypatch.setattr(vm.network, "stop", lambda: None)
    with pytest.raises(Reached):
        getattr(vm, operation)()
    assert taken[0] == (vm.settings.firm_locks, True) and taken[-1] == "body"
    assert not (vm.root / "locks").exists()


def test_down_recovers_a_broken_lima_vm_with_force_stop(vm, monkeypatch):
    calls = []
    states = iter([linux_vm.VMStateError("Broken"), False, False])

    def running():
        state = next(states)
        if isinstance(state, BaseException):
            raise state
        return state

    monkeypatch.setattr(vm, "_running", running)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(vm.network, "stop", lambda: None)
    assert vm.down()["running"] is False
    assert calls == [
        (("stop", "--force", "--tty=false", "linux"), {"timeout": 120, "check": False}),
    ]


def test_status_reports_broken_lima_as_recovery_required(vm, monkeypatch):
    monkeypatch.setattr(
        vm, "_running", lambda: (_ for _ in ()).throw(linux_vm.VMStateError("Broken"))
    )
    result = vm.status()
    assert result["running"] is None and result["state"] == "recovery_required"
    assert "Broken" in result["error"]


@pytest.mark.parametrize(
    ("status", "expected"), [("running", True), ("Running", True), ("STOPPED", False)]
)
def test_running_check_normalizes_lima_status_case(vm, monkeypatch, status, expected):
    vm.paths.instance.mkdir(parents=True)
    monkeypatch.setattr(
        vm,
        "_lima",
        lambda *a, **k: SimpleNamespace(
            stdout=json.dumps({"name": "linux", "status": status}).encode()
        ),
    )
    assert vm._running() is expected


@pytest.mark.parametrize("stop_fails", [False, True])
def test_pid_publication_failure_stops_vm_and_filter_even_when_lima_stop_fails(
    vm, monkeypatch, tmp_path, stop_fails
):
    calls = []
    monkeypatch.setattr(vm, "_prepare", lambda **_: None)
    monkeypatch.setattr(vm, "_state", lambda: {})
    monkeypatch.setattr(vm, "_running", lambda: False)
    network = tmp_path / "network"
    monkeypatch.setattr(
        vm.network,
        "paths",
        {
            "scratch": network,
            "config": network / "config.json",
            "pid": network / "usernet.pid",
        },
    )
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    monkeypatch.setattr(vm.network, "start", lambda: calls.append("filter-start"))
    monkeypatch.setattr(vm.paths, "preserve_logs", lambda: calls.append("preserve-logs"))

    def publish():
        calls.append("publish")
        vm.network.paths["pid"].write_text("12345")
        raise RuntimeError("PID publication failed")

    def lima(*args, **kwargs):
        assert args == ("stop", "--force", "--tty=false", "linux")
        assert kwargs == {"check": False}
        calls.append("vm-stop")
        if stop_fails:
            raise RuntimeError("VM stop failed")

    monkeypatch.setattr(vm.network, "publish_lima_pid", publish)
    monkeypatch.setattr(vm, "_lima", lima)
    with pytest.raises(RuntimeError, match="VM stop failed" if stop_fails else "PID publication"):
        vm.up()
    # Logs are archived whether or not the recovery stop itself succeeded.
    assert calls == [
        "filter-stop",
        "filter-start",
        "publish",
        "vm-stop",
        "preserve-logs",
        "filter-stop",
    ]
    assert not vm.network.paths["pid"].exists()


def test_first_boot_requires_current_provisioning_marker_before_controller(
    vm, monkeypatch
):
    vm.root.mkdir(parents=True)
    state = {"ready": False, "controller_digest": "old"}
    guest_running = False
    calls = []
    provision_attempts = 0

    monkeypatch.setattr(vm, "_prepare", lambda **_: None)
    monkeypatch.setattr(vm, "_state", lambda: state)
    monkeypatch.setattr(guest_control, "save", lambda _path, value: state.update(value))
    monkeypatch.setattr(vm, "status", lambda: {"running": True})
    monkeypatch.setattr(vm.paths, "mark_running", lambda: None)
    monkeypatch.setattr(vm.paths, "synchronize", lambda: None)
    monkeypatch.setattr(vm.paths, "preserve_logs", lambda: calls.append("preserve-logs"))
    monkeypatch.setattr(vm.network, "write_configuration", lambda: None)
    monkeypatch.setattr(vm.network, "ready", lambda **_: True)
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    monkeypatch.setattr(vm.network, "start", lambda: calls.append("filter-start"))
    monkeypatch.setattr(vm.network, "publish_lima_pid", lambda: None)

    def lima(*args, **kwargs):
        nonlocal guest_running
        calls.append(args[0])
        if args[0] == "start":
            guest_running = True
        elif args[0] == "stop":
            guest_running = False
        return SimpleNamespace(stdout=b"")

    def running():
        return guest_running

    def verify(*, require_provision_marker):
        nonlocal provision_attempts
        assert require_provision_marker is True
        calls.append("verify-provisioning")
        provision_attempts += 1
        if provision_attempts == 1:
            raise RuntimeError("Linux guest provisioning did not complete")

    monkeypatch.setattr(vm, "_lima", lima)
    monkeypatch.setattr(vm, "_running", running)
    monkeypatch.setattr(vm, "_verify_guest_ready", verify)
    monkeypatch.setattr(
        vm, "_install_controller", lambda: calls.append("install-controller")
    )
    monkeypatch.setattr(vm, "_rpc", lambda operation, **_: calls.append(operation))

    with pytest.raises(RuntimeError, match="provisioning did not complete"):
        vm.up()
    assert state["ready"] is False
    assert "install-controller" not in calls and "initialize" not in calls
    assert not guest_running

    # A later start retries the offline configuration stage.
    calls.clear()
    assert vm.up() == {"running": True}
    assert calls.index("verify-provisioning") < calls.index("install-controller")
    assert calls.index("install-controller") < calls.index("initialize")
    assert state["ready"] is True


def test_ready_compatible_vm_uses_dependency_check_without_new_recipe_marker(vm, monkeypatch):
    calls = []
    monkeypatch.setattr(
        vm,
        "_shell",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0),
    )
    vm._verify_guest_ready(require_provision_marker=False)
    assert len(calls) == 1
    assert linux_vm.PROVISION_MARKER not in calls[0][0]
    for dependency in linux_vm.GUEST_DEPENDENCIES:
        assert f"command -v {dependency}" in calls[0][0][2]


def first_boot(vm, monkeypatch, *, ready=False, start_fails=None, verify_fails=None,
               logs=b"E: Failed to fetch gcc-15\n"):
    """``vm.up()`` with Lima, the filter and the guest faked; returns the calls in order."""
    vm.root.mkdir(parents=True)
    state = {"ready": ready, "controller_digest": "old"}
    calls, guest = [], {"running": False}
    monkeypatch.setattr(vm, "_prepare", lambda **_: None)
    monkeypatch.setattr(vm, "_state", lambda: state)
    monkeypatch.setattr(guest_control, "save", lambda _path, value: state.update(value))
    for name in ("mark_running", "synchronize", "preserve_logs"):
        monkeypatch.setattr(vm.paths, name, lambda: None)
    for name in ("write_configuration", "stop", "start", "publish_lima_pid"):
        monkeypatch.setattr(vm.network, name, lambda: None)
    monkeypatch.setattr(vm.network, "ready", lambda **_: True)
    monkeypatch.setattr(vm, "_running", lambda: guest["running"])

    def lima(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "start":
            if start_fails:
                raise start_fails
            guest["running"] = True
        elif args[0] == "stop":
            guest["running"] = False
        return SimpleNamespace(stdout=b"")

    def verify(*, require_provision_marker):
        calls.append("verify")
        if verify_fails:
            raise RuntimeError(verify_fails)

    def shell(*args, **kwargs):
        calls.append(("shell", args))
        if isinstance(logs, BaseException):
            raise logs
        return SimpleNamespace(returncode=0, stdout=logs)

    monkeypatch.setattr(vm, "_lima", lima)
    monkeypatch.setattr(vm, "_verify_guest_ready", verify)
    monkeypatch.setattr(vm, "_shell", shell)
    return calls


@pytest.mark.parametrize("failure", ["unprovisioned", "lima-start-timeout"])
def test_a_failed_first_boot_copies_guest_logs_before_power_off_and_names_them(
    vm, monkeypatch, failure
):
    if failure == "unprovisioned":
        calls = first_boot(vm, monkeypatch,
                           verify_fails="Linux guest provisioning did not complete")
        cause = "provisioning did not complete"
    else:
        # Lima's own wait can end while a stalled download still runs in the guest.
        calls = first_boot(vm, monkeypatch, start_fails=subprocess.TimeoutExpired(
            ["limactl", "start"], 1200))
        cause = "timed out after 1200 seconds"
    with pytest.raises(RuntimeError) as failed:
        vm.up()
    message = str(failed.value)
    assert cause in message and str(vm.provision_log) in message
    assert message.endswith("; retry firm start")
    copy = calls.index(("shell", (
        "/usr/bin/sudo", "-n", "/usr/bin/tail", "-c", linux_vm.PROVISION_LOG_LIMIT,
        *linux_vm.PROVISION_LOGS,
    )))
    assert copy < calls.index("stop")  # copied while the guest still runs
    assert vm.provision_log.read_bytes() == b"E: Failed to fetch gcc-15\n"
    assert vm.provision_log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("logs", [subprocess.TimeoutExpired(["ssh"], 30), b""])
def test_unreachable_guest_logs_still_fail_the_first_boot_clearly(vm, monkeypatch, logs):
    first_boot(vm, monkeypatch, verify_fails="Linux guest provisioning did not complete",
               logs=logs)
    with pytest.raises(RuntimeError, match="did not complete; its guest logs could not be "
                                           "copied to the host; retry firm start"):
        vm.up()
    assert not vm.provision_log.exists()


def test_an_interrupted_log_copy_still_powers_the_guest_off(vm, monkeypatch):
    calls = first_boot(vm, monkeypatch, verify_fails="Linux guest provisioning did not complete",
                       logs=KeyboardInterrupt())
    stopped = []
    monkeypatch.setattr(vm.network, "stop", lambda: stopped.append("filter-stop"))
    with pytest.raises(KeyboardInterrupt):
        vm.up()
    # The guest is powered off and the filter stopped (once before the boot, once after).
    assert calls[-1] == "stop" and stopped == ["filter-stop", "filter-stop"]


def test_a_provisioned_vm_that_fails_to_start_copies_no_first_boot_logs(vm, monkeypatch):
    calls = first_boot(vm, monkeypatch, ready=True,
                       verify_fails="Linux VM is missing required runtime")
    with pytest.raises(RuntimeError, match="^Linux VM is missing required runtime$"):
        vm.up()
    assert not any(isinstance(call, tuple) for call in calls)
    assert not vm.provision_log.exists()


@pytest.mark.parametrize("stops", [True, False])
def test_up_recovers_unfiltered_vm_only_after_confirmed_shutdown(vm, monkeypatch, stops):
    calls = []
    running = iter([True, not stops, True, True])
    monkeypatch.setattr(vm, "_prepare", lambda **_: None)
    monkeypatch.setattr(
        vm, "_state", lambda: {"ready": True, "controller_digest": "installed"}
    )
    monkeypatch.setattr(vm, "_running", lambda: next(running))
    monkeypatch.setattr(guest_control, "save", lambda path, state: None)
    monkeypatch.setattr(vm, "status", lambda: {"running": True})
    monkeypatch.setattr(vm, "_install_controller", lambda: calls.append("install-controller"))
    monkeypatch.setattr(vm, "_verify_guest_ready", lambda **_: calls.append("verify-dependencies"))
    monkeypatch.setattr(vm, "_rpc", lambda operation, **payload: calls.append(operation))
    monkeypatch.setattr(vm.paths, "mark_running", lambda: None)
    monkeypatch.setattr(vm.paths, "synchronize", lambda: None)
    monkeypatch.setattr(vm.paths, "preserve_logs", lambda: calls.append("preserve-logs"))
    monkeypatch.setattr(vm.network, "ready", lambda **_: "filter-start" in calls)
    monkeypatch.setattr(vm.network, "stop", lambda: calls.append("filter-stop"))
    monkeypatch.setattr(vm.network, "start", lambda: calls.append("filter-start"))
    monkeypatch.setattr(vm.network, "publish_lima_pid", lambda: None)
    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: calls.append(args[0]))
    vm.root.mkdir(parents=True)
    if stops:
        # The recovery stop's logs are archived before the VM restarts.
        assert vm.up() == {"running": True}
        assert calls == [
            "stop",
            "preserve-logs",
            "filter-stop",
            "filter-start",
            "start",
            "verify-dependencies",
            "install-controller",
            "initialize",
        ]
    else:
        with pytest.raises(RuntimeError, match="shutdown was not confirmed"):
            vm.up()
        assert calls == ["stop"]


@pytest.mark.parametrize("disk_gib", [60, 120])
def test_create_applies_resources_and_records_the_generation(vm, monkeypatch, tmp_path, disk_gib):
    vm.settings = replace(vm.settings, disk_gib=disk_gib)
    calls = []

    def lima(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            vm.paths.instance.mkdir(mode=0o700, parents=True)
            (vm.paths.instance / "disk").write_text("retained Linux data")
            (vm.paths.instance / "lima.yaml").write_text(f"os: Linux\ndisk: {disk_gib}GiB\n")
        if args[0] == "list":
            return SimpleNamespace(stdout=b'{"name":"linux","status":"Stopped"}\n')
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(vm, "_lima", lima)
    vm._prepare()
    create = next(call for call in calls if call[0] == "create")
    assert "--name=linux" in create and "--cpus=8" in create and "--memory=24" in create
    assert f"--disk={disk_gib}" in create
    (setting,) = [arg for arg in create if arg.startswith("--set=")]
    assert json.loads(setting.split(" = ", 1)[1]) == [
        {"location": str(tmp_path / "base-key.img"), "arch": "aarch64"}
    ]
    assert create[-1] == str(linux_vm.IMAGE)
    record = json.loads(vm.record.read_text())
    assert record["base_image"]["key"] == "base-key"
    assert record["base_image"]["built"].endswith("+00:00")
    assert record["generation"] and record["firm_id"] == "firm" and record["ready"] is False
    assert record["subnet"] == vm.subnet and vm.subnet.startswith("192.168.")
    networks = yaml.safe_load((vm.paths.home / "_config" / "networks.yaml").read_text())
    assert networks["networks"]["user-v2"]["gateway"] == vm.subnet.replace("0/24", "1")
    vm._prepare()
    assert sum(call[0] == "create" for call in calls) == 1

    # Editing the configured resources resizes the stopped guest; its disk, and so
    # every role's home, stays where it is.
    bigger = linux_vm.LinuxVM(
        settings(vm.root, tmp_path, cpus=12, memory_gib=32, disk_gib=disk_gib * 2), limactl=vm.lima
    )
    monkeypatch.setattr(bigger, "_lima", lima)
    bigger._prepare()
    assert sum(call[0] == "create" for call in calls) == 1
    assert (vm.paths.instance / "disk").read_text() == "retained Linux data"
    definition = yaml.safe_load((bigger.instance / "lima.yaml").read_text())
    assert (definition["cpus"], definition["memory"], definition["os"]) == (12, "32GiB", "Linux")
    assert definition["disk"] == f"{disk_gib}GiB"
    record = json.loads(bigger.record.read_text())
    assert (record["cpus"], record["memory_gib"]) == (12, 32)

    other = linux_vm.LinuxVM(settings(vm.root, tmp_path, firm_id="other"), limactl=vm.lima)
    monkeypatch.setattr(other, "_lima", lima)
    with pytest.raises(ValueError, match="another firm"):
        other._prepare()


def test_installed_sudoers_matches_the_rpc_command(vm, monkeypatch):
    scripts, commands = [], []
    vm.root.mkdir(parents=True)
    guest_control.save(vm.record, {"ready": False})

    installed = [
        hashlib.sha256(
            Path(linux_vm.__file__).with_name("linux_vm_guest.py").read_bytes()
        ).hexdigest()
    ]

    def shell(*args, **kwargs):
        if args[0] == "/usr/bin/mktemp":
            assert args[1:] == ("-d", "/tmp/firm-controller.XXXXXXXXXX")
            return SimpleNamespace(stdout=b"/tmp/firm-controller.A1b2C3d4E5\n")
        if args[0] == "/bin/rm":
            assert args[1:] == ("-rf", "--", "/tmp/firm-controller.A1b2C3d4E5")
            return SimpleNamespace(stdout=b"")
        if args[0] == "/usr/bin/sha256sum":
            return SimpleNamespace(
                returncode=0, stdout=f"{installed[0]}  {linux_vm.CONTROLLER}\n".encode()
            )
        scripts.append(args[-1])
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(vm, "_lima", lambda *args, **kwargs: None)
    monkeypatch.setattr(vm, "_shell", shell)
    vm._install_controller()
    popen = guest_control.subprocess.Popen

    def request(argv, **kwargs):
        commands.append(argv)
        return popen([sys.executable, "-c", 'print(\'{"ok":true,"result":{}}\')'], **kwargs)

    monkeypatch.setattr(guest_control.subprocess, "Popen", request)
    monkeypatch.setattr(vm.network, "boundary", lambda **_: guest_control.Boundary.ENFORCED)
    vm._rpc("status")
    argv = commands[0]
    sudo = argv.index("/usr/bin/sudo")
    assert argv[sudo + 1] == "-n" and "sudo" not in argv
    command = " ".join(argv[sudo + 2 :])
    assert f'NOPASSWD: {command}" \\' in scripts[0]
    assert "${SUDO_USER:?}" in scripts[0] and "visudo -cf /etc/sudoers.d/firm" in scripts[0]
    assert "rm -f /tmp/firm-controller.A1b2C3d4E5/controller.py" in scripts[0]
    # The guest's sudo-rs refuses !syslog and !pam_session, so nothing quiets it in
    # sudoers; the rsyslog rule below narrows to the exact successful controller call.
    assert "Defaults" not in scripts[0]
    assert (
        f"$msg == ' {linux_vm.CONTROL_USER_PLACEHOLDER} :  PWD=/ ; USER=root ; COMMAND={command}'"
    ) in scripts[0]
    # RainerScript rejects a backslash escape such as `\.`; one bad rule stops auth.log.
    assert "\\" not in linux_vm.QUIET_CONTROLLER_SUDO and "re_match" not in scripts[0]
    # The rule is validated before rsyslog restarts, and removed if rsyslog refuses it.
    validate = scripts[0].index("rsyslogd -N1")
    assert validate < scripts[0].index("systemctl restart rsyslog")
    assert "rm -f /etc/rsyslog.d/10-firm.conf" in scripts[0]
    # The heredoc delimiter is quoted, so $msg and $programname reach the guest file
    # unexpanded; only the placeholder is substituted, in a separate command.
    assert "<<'RSYSLOG'" in scripts[0]
    assert "$msg" in scripts[0] and "$programname" in scripts[0]
    assert (
        f'sed -i "s/{linux_vm.CONTROL_USER_PLACEHOLDER}/${{SUDO_USER:?}}/" '
        "/etc/rsyslog.d/10-firm.conf"
    ) in scripts[0]
    assert "/etc/rsyslog.d/10-firm.conf" in scripts[0]
    assert "SystemMaxUse=500M" in scripts[0]
    assert "systemctl restart rsyslog systemd-journald" in scripts[0]
    assert json.loads(vm.record.read_text())["controller_digest"]
    assert scripts[0].rstrip().endswith("\nsync")  # on disk before the host records it
    scripts.clear()
    vm._install_controller()
    assert scripts == []
    # A boot that failed after the install powered the VM off before the file reached
    # its disk: an empty controller is installed again although the digest is recorded,
    # and without asking it about runs it could not answer.
    installed[0] = hashlib.sha256(b"").hexdigest()
    guest_control.save(vm.record, {**json.loads(vm.record.read_text()), "ready": True})
    monkeypatch.setattr(vm, "_rpc", lambda *a, **k: pytest.fail("asked an empty controller"))
    vm._install_controller()
    assert len(scripts) == 1


def test_quiet_controller_sudo_equals_only_the_exact_successful_call():
    """The rule compares the whole message, so only that one line can equal it."""
    match = re.search(r"\$msg == '(.*?)'", linux_vm.QUIET_CONTROLLER_SUDO)
    assert match
    dropped = match.group(1).replace(linux_vm.CONTROL_USER_PLACEHOLDER, "bmalthi")
    command = " ".join(linux_vm.CONTROLLER_COMMAND)
    # What sudo-rs logged on a live guest for a successful controller call.
    assert dropped == f" bmalthi :  PWD=/ ; USER=root ; COMMAND={command}"
    for kept in (
        f" bmalthi : command not allowed ; PWD=/ ; USER=root ; COMMAND={command}",
        dropped + " --extra",
        f" bmalthi :  PWD=/ ; USER=root ; COMMAND=/usr/bin/cat {linux_vm.CONTROLLER}",
        f" other :  PWD=/ ; USER=root ; COMMAND={command}",
    ):
        assert kept != dropped


def test_controller_update_waits_for_stopped_roles(vm, monkeypatch):
    vm.root.mkdir(parents=True)
    guest_control.save(vm.record, {"ready": True, "controller_digest": "stale"})
    monkeypatch.setattr(vm, "_rpc", lambda operation, **payload: [{"running": True}])
    with pytest.raises(RuntimeError, match="Stop running roles"):
        vm._install_controller()


def test_controller_copy_uses_snapshot_and_refuses_corruption(vm, monkeypatch):
    vm.root.mkdir(parents=True)
    guest_control.save(vm.record, {"ready": False})
    copied = []

    def copy(*args, **kwargs):
        assert args[0] == "copy"
        copied.append(Path(args[1]).read_bytes())
        assert args[2] == "linux:/tmp/firm-controller.A1b2C3d4E5/controller.py"
        assert Path(args[1]) != Path(linux_vm.__file__).with_name("linux_vm_guest.py")

    def shell(*args, **kwargs):
        import hashlib

        if args[0] == "/usr/bin/mktemp":
            return SimpleNamespace(stdout=b"/tmp/firm-controller.A1b2C3d4E5\n")
        if args[0] == "/bin/rm":
            assert args[-1] == "/tmp/firm-controller.A1b2C3d4E5"
            return SimpleNamespace(stdout=b"")
        script = args[-1]
        assert hashlib.sha256(copied[0]).hexdigest() in script
        assert script.index("sha256sum --check") < script.index("install -m 755")
        raise RuntimeError("checksum mismatch")

    monkeypatch.setattr(vm, "_lima", copy)
    monkeypatch.setattr(vm, "_shell", shell)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        vm._install_controller()
    assert copied[0] == Path(linux_vm.__file__).with_name("linux_vm_guest.py").read_bytes()
    assert "controller_digest" not in json.loads(vm.record.read_text())


def test_run_from_old_generation_is_rejected(vm):
    vm.root.mkdir(parents=True)
    guest_control.save(vm.record, {"generation": "new"})
    with pytest.raises(ValueError, match="discarded VM generation"):
        vm._check_run(SimpleNamespace(vm_generation="old"))


# ---- the control channel -------------------------------------------------------------


@pytest.fixture
def ready(vm, monkeypatch):
    monkeypatch.setattr(vm.network, "boundary", lambda **_: guest_control.Boundary.ENFORCED)
    monkeypatch.setattr(vm.network, "ready", lambda **_: True)
    return vm


def test_start_rpc_keeps_role_argv_in_payload(ready, monkeypatch):
    popen = guest_control.subprocess.Popen
    commands = []

    def request(argv, **kwargs):
        commands.append(argv)
        return popen(
            [
                sys.executable,
                "-c",
                "import json, sys; print(json.dumps({'ok': True, 'result': json.load(sys.stdin)}))",
            ],
            **kwargs,
        )

    monkeypatch.setattr(guest_control.subprocess, "Popen", request)
    payload = {"role_id": "r-founder", "run_id": "run-one", "argv": ["/usr/bin/id"]}
    assert ready._rpc("start", **payload) == {"operation": "start", **payload}
    assert commands == [
        [
            ready.lima,
            "shell",
            "--tty=false",
            "--workdir=/",
            linux_vm.INSTANCE,
            "/usr/bin/sudo",
            "-n",
            *linux_vm.CONTROLLER_COMMAND,
        ]
    ]


def test_rpc_drains_and_bounds_control_diagnostics(ready, monkeypatch):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x'*100000+'control failed'); sys.exit(1)",
            ],
            **kwargs,
        ),
    )
    with pytest.raises(guest_control.ControlConnectionError, match="control failed") as error:
        ready._rpc("status")
    assert len(str(error.value)) < 17000


def test_rpc_honors_short_deadline(ready, monkeypatch):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        ),
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        ready._rpc("stop", timeout=0.1, run_id="run-one")
    assert time.monotonic() - started < 2


def test_rpc_normalizes_timeout_after_streams_close(ready, monkeypatch):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"],
            **kwargs,
        ),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        ready._rpc("stop", timeout=0.2, run_id="run-one")


def test_successful_rpc_surfaces_guest_diagnostics(ready, monkeypatch, caplog):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [
                sys.executable,
                "-c",
                'import sys; print("cgroup still draining", file=sys.stderr); '
                'print(\'{"ok":true,"result":{}}\')',
            ],
            **kwargs,
        ),
    )
    assert ready._rpc("runs") == {}
    assert "cgroup still draining" in caplog.text


def stubbed_process(closed):
    """A guest controller whose pipes never produce anything, so only the boundary ends the call."""
    streams = []
    for name in ("stdout", "stderr"):
        read, write = os.pipe()

        def close(name=name, read=read, write=write):
            os.close(read)
            os.close(write)
            closed.append(name)

        streams.append(SimpleNamespace(fileno=lambda read=read: read, close=close))
    return SimpleNamespace(
        pid=12345,
        stdout=streams[0],
        stderr=streams[1],
        poll=lambda: None,
        wait=lambda: closed.append("wait"),
    )


def boundary_answers(vm, monkeypatch, answers):
    """Answer each in-flight probe in turn, keeping the last answer once they run out."""
    replies = list(answers)
    asked = []

    def boundary(**_):
        asked.append(len(asked))
        return replies[min(len(asked) - 1, len(replies) - 1)]

    monkeypatch.setattr(vm.network, "boundary", boundary)
    return asked


def test_rpc_aborts_at_once_when_the_boundary_answers_that_it_is_not_enforcing(vm, monkeypatch):
    """An answered "no" is a verdict: the guest may be running unfiltered, so stop now."""
    monkeypatch.setattr(guest_control, "BOUNDARY_INTERVAL", 0)
    boundary_answers(
        vm,
        monkeypatch,
        [guest_control.Boundary.ENFORCED, guest_control.Boundary.UNENFORCED],
    )
    closed = []
    monkeypatch.setattr(
        guest_control.subprocess, "Popen", lambda *args, **kwargs: stubbed_process(closed)
    )
    monkeypatch.setattr(guest_control.os, "killpg", lambda pid, sig: closed.append((pid, sig)))
    with pytest.raises(guest_control.ControlConnectionError, match="boundary is unavailable"):
        vm._rpc("status", run_id="run-one")
    assert closed == ["stdout", "stderr", (12345, guest_control.signal.SIGKILL), "wait"]


def test_rpc_survives_a_probe_that_cannot_be_answered(vm, monkeypatch):
    """A refused probe is a transport event; the call in flight must not pay for it."""
    monkeypatch.setattr(guest_control, "BOUNDARY_INTERVAL", 0.05)
    asked = boundary_answers(
        vm,
        monkeypatch,
        [
            guest_control.Boundary.ENFORCED,
            guest_control.Boundary.UNREACHABLE,
            guest_control.Boundary.UNREACHABLE,
            guest_control.Boundary.ENFORCED,
        ],
    )
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [
                sys.executable,
                "-c",
                'import time; time.sleep(0.4); print(\'{"ok":true,"result":{}}\')',
            ],
            **kwargs,
        ),
    )
    assert vm._rpc("status") == {}
    assert len(asked) > 3  # The tolerated silence did not stop the call or the probing.


def test_rpc_fails_closed_once_the_boundary_has_been_silent_too_long(vm, monkeypatch):
    """Tolerance is bounded: past the grace, silence is treated as a lost boundary."""
    monkeypatch.setattr(guest_control, "BOUNDARY_INTERVAL", 0.02)
    monkeypatch.setattr(guest_control, "BOUNDARY_GRACE", 0.2)
    boundary_answers(
        vm,
        monkeypatch,
        [guest_control.Boundary.ENFORCED, guest_control.Boundary.UNREACHABLE],
    )
    closed = []
    monkeypatch.setattr(
        guest_control.subprocess, "Popen", lambda *args, **kwargs: stubbed_process(closed)
    )
    monkeypatch.setattr(guest_control.os, "killpg", lambda pid, sig: closed.append((pid, sig)))
    started = time.monotonic()
    with pytest.raises(guest_control.ControlConnectionError, match="stopped answering"):
        vm._rpc("status", run_id="run-one")
    assert 0.2 <= time.monotonic() - started < 5
    assert closed == ["stdout", "stderr", (12345, guest_control.signal.SIGKILL), "wait"]


def test_rpc_retries_an_unanswerable_precondition_once_before_refusing_to_start(vm, monkeypatch):
    """Nothing is in flight yet, so a second ask is cheap and a second silence is final."""
    popen = guest_control.subprocess.Popen
    launched = []
    monkeypatch.setattr(
        guest_control.subprocess, "Popen", lambda *args, **kwargs: launched.append(args)
    )
    boundary_answers(vm, monkeypatch, [guest_control.Boundary.UNREACHABLE])
    with pytest.raises(RuntimeError, match="boundary is unavailable; stop the VM"):
        vm._rpc("status", run_id="run-one")
    assert not launched

    asked = boundary_answers(
        vm,
        monkeypatch,
        [guest_control.Boundary.UNREACHABLE, guest_control.Boundary.ENFORCED],
    )
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [sys.executable, "-c", 'print(\'{"ok":true,"result":{}}\')'], **kwargs
        ),
    )
    assert vm._rpc("status") == {}
    assert len(asked) >= 2


def test_guest_error_reply_is_raised(ready, monkeypatch):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        guest_control.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [sys.executable, "-c", 'print(\'{"ok":false,"error":"Retired role IDs"}\')'], **kwargs
        ),
    )
    with pytest.raises(RuntimeError, match="Retired role IDs"):
        ready._rpc("provision", role_id="analyst")


GUEST_EXPORT = """
import io, json, sys, tarfile
request = json.load(sys.stdin)
assert request == {"operation": "export", "root": "/srv/firm", "homes": True}, request
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    data = b"work"
    member = tarfile.TarInfo("drive/org/notes.md")
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))
    for name, target in (("drive/escape", "/etc/passwd"), ("drive/org/up", "../../..")):
        link = tarfile.TarInfo(name)
        link.type, link.linkname = tarfile.SYMTYPE, target
        archive.addfile(link)
"""


def test_export_unpacks_the_guest_stream_and_refuses_escaping_members(ready, monkeypatch, tmp_path):
    popen = guest_control.subprocess.Popen
    commands = []

    def guest(argv, **kwargs):
        commands.append(argv)
        return popen([sys.executable, "-c", GUEST_EXPORT], **kwargs)

    monkeypatch.setattr(linux_vm.subprocess, "Popen", guest)
    destination = tmp_path / "export"
    destination.mkdir()
    refused = ready.export(destination, root="/srv/firm", homes=True)
    assert refused == ["drive/escape", "drive/org/up"]
    assert (destination / "drive" / "org" / "notes.md").read_text() == "work"
    assert not (destination / "drive" / "escape").exists()
    assert commands == [
        [
            ready.lima,
            "shell",
            "--tty=false",
            "--workdir=/",
            linux_vm.INSTANCE,
            "/usr/bin/sudo",
            "-n",
            *linux_vm.CONTROLLER_COMMAND,
        ]
    ]


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.stderr.write('No space left on device'); sys.exit(1)",
        "import sys; sys.stdout.buffer.write(b'x' * 700); sys.stderr.write('killed'); sys.exit(1)",
    ],
)
def test_export_fails_with_guest_diagnostics_when_the_guest_fails(
    ready, monkeypatch, tmp_path, script
):
    popen = guest_control.subprocess.Popen
    monkeypatch.setattr(
        linux_vm.subprocess,
        "Popen",
        lambda argv, **kwargs: popen([sys.executable, "-c", script], **kwargs),
    )
    with pytest.raises(RuntimeError, match="Guest export failed: (No space|killed)"):
        ready.export(tmp_path, root="/srv/firm", homes=False)


def test_export_requires_the_network_boundary(vm, monkeypatch, tmp_path):
    monkeypatch.setattr(vm.network, "ready", lambda: False)
    with pytest.raises(RuntimeError, match="boundary is unavailable"):
        vm.export(tmp_path, root="/srv/firm", homes=False)


# ---- long host paths -------------------------------------------------------------------


@pytest.fixture
def projected(tmp_path, monkeypatch):
    # RUNTIME_PARENT is a "lima" child of one shared parent; cache stands in for that
    # parent (/private/tmp/firm-<uid>) without ever touching the real one.
    cache = Path("/tmp").resolve() / ("lp-" + uuid4().hex[:12])
    monkeypatch.setattr(paths_module, "RUNTIME_PARENT", cache / "lima")
    root = tmp_path / ("retained-development-firm-" * 4) / "durable" / "linux"
    value = linux_vm.LinuxVM(settings(root, tmp_path), limactl="/usr/local/bin/limactl")
    yield value
    if cache.exists():
        shutil.rmtree(cache)


def test_long_paths_share_the_short_lima_and_filter_runtime(projected):
    paths = projected.paths
    assert paths.projected
    assert projected.commands.env["LIMA_HOME"] == str(paths.home)
    assert projected.ssh_config == paths.instance / "ssh.config"
    assert projected.socket_root == paths.runtime
    assert projected.instance == projected.root / "lima/linux"
    assert len(os.fsencode(paths.instance / "ssh.sock.1234567890123456")) < 104
    assert len(os.fsencode(paths.home / "_networks/user-v2/user-v2_ep.sock")) < 104
    assert str(paths.runtime) in str(projected.network.paths["ep"])
    assert "proxy" not in " ".join(projected.commands.env).lower()


def test_create_publishes_disk_then_resume_reconstructs_runtime(projected, monkeypatch, tmp_path):
    base = tmp_path / "base.img"
    base.write_bytes(b"base")
    monkeypatch.setattr(linux_base, "base_image", lambda *a: base)
    calls = []

    def lima(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            projected.paths.instance.mkdir(mode=0o700)
            (projected.paths.instance / "disk").write_text("retained Linux data")
            (projected.paths.instance / "lima.yaml").write_text("os: Linux\n")
        if args[0] == "list":
            return SimpleNamespace(stdout=b'{"name":"linux","status":"Stopped"}\n')
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(projected, "_lima", lima)
    projected._prepare()
    disk = projected.instance / "disk"
    inode = disk.stat().st_ino
    assert (projected.paths.instance / "disk").is_symlink()
    shutil.rmtree(projected.paths.runtime)
    assert projected.status()["running"] is False
    projected._prepare()
    assert disk.stat().st_ino == inode
    assert (projected.paths.instance / "disk").read_text() == "retained Linux data"
    assert sum(call[0] == "create" for call in calls) == 1


def test_missing_projection_with_live_process_fails_closed(projected, monkeypatch):
    monkeypatch.setattr(projected.paths, "has_live_processes", lambda: True)
    with pytest.raises(RuntimeError, match="host process is alive"):
        projected.status()


@pytest.mark.parametrize("live", [False, True])
def test_remove_persistent_broken_vm_requires_host_shutdown(projected, monkeypatch, live):
    projected.paths.prepare()
    projected.paths.instance.mkdir(mode=0o700)
    (projected.paths.instance / "disk").write_bytes(b"retained Linux data")
    projected.paths.adopt_stopped_instance()
    calls = []

    def lima(*args, **kwargs):
        calls.append(args)
        if args[0] == "delete":
            shutil.rmtree(projected.paths.instance)
        return SimpleNamespace(stdout=b'{"name":"linux","status":"Broken"}')

    monkeypatch.setattr(projected, "_lima", lima)
    monkeypatch.setattr(projected.network, "stop", lambda: None)
    command = f"12345 limactl hostagent {projected.paths.instance}" if live else "12345 /bin/other"
    monkeypatch.setattr(
        paths_module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=command.encode(), returncode=0),
    )
    if live:
        with pytest.raises(RuntimeError, match="host process is still running"):
            projected.remove()
        assert (projected.instance / "disk").read_bytes() == b"retained Linux data"
        assert not any(call[0] == "delete" for call in calls)
    else:
        assert projected.remove() == {"removed": True}
        assert not projected.instance.exists()
        assert [call[0] for call in calls if call[0] != "list"] == ["stop", "delete"]


@pytest.mark.parametrize("returned", [b"/tmp/other\n", b"/tmp/firm-controller.abc;id\n"])
def test_controller_refuses_unexpected_staging_paths(vm, monkeypatch, returned):
    vm.root.mkdir(parents=True)
    guest_control.save(vm.record, {"ready": False})
    calls = []
    monkeypatch.setattr(vm, "_lima", lambda *a, **k: pytest.fail("copied to unsafe staging"))

    def shell(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=returned)

    monkeypatch.setattr(vm, "_shell", shell)
    with pytest.raises(RuntimeError, match="Invalid controller staging"):
        vm._install_controller()
    assert len(calls) == 1 and calls[0][0] == "/usr/bin/mktemp"
