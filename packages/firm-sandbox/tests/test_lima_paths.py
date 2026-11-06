import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firm_sandbox import lima_paths as paths_module


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("evidence", ["stopped", "pid", "command", "unknown", "starting"])
def test_broken_shutdown_requires_independent_process_evidence(
    projection, monkeypatch, projected, evidence
):
    paths = (
        projection
        if projected
        else paths_module.lima_paths(Path("/tmp").resolve() / "vm-short", instance="linux")
    )
    monkeypatch.setattr(paths, "has_live_processes", lambda: False)
    monkeypatch.setattr(paths_module, "_pid", lambda path: 12345 if evidence == "pid" else None)
    output = b"12345 /usr/bin/other\n"
    if evidence == "command":
        output = f"12345 limactl hostagent {paths.instance}\n".encode()
    elif evidence == "unknown":
        output = b""
    monkeypatch.setattr(
        paths_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output)
    )

    def running():
        raise paths_module.VMStateError("Starting" if evidence == "starting" else "Broken")

    if evidence == "stopped":
        paths.verify_stopped(running)
    else:
        with pytest.raises(RuntimeError):
            paths.verify_stopped(running)


@pytest.fixture
def projection(tmp_path, monkeypatch):
    # RUNTIME_PARENT is a "lima" child of one shared parent; shared stands in for
    # that parent (/private/tmp/firm-<uid>) without ever touching the real one.
    shared = Path("/tmp").resolve() / ("vp-" + uuid4().hex[:12])
    monkeypatch.setattr(paths_module, "RUNTIME_PARENT", shared / "lima")
    root = tmp_path / ("long-development-firm-" * 4) / "durable" / "linux"
    root.mkdir(mode=0o700, parents=True)
    paths = paths_module.lima_paths(root, instance="linux")
    paths.durable_instance.mkdir(mode=0o700, parents=True)
    (paths.durable_instance / "disk").write_text("durable disk fixture")
    (paths.durable_instance / "lima.yaml").write_text("os: Darwin\n")
    paths.durable_home.chmod(0o700)
    yield paths
    if shared.is_symlink():
        shared.unlink()
    elif shared.exists():
        shutil.rmtree(shared)


def test_real_short_home_projects_files_and_preserves_durable_ownership(projection):
    paths = projection
    original_inode = (paths.durable_instance / "disk").stat().st_ino
    paths.prepare()
    assert paths.projected
    assert not paths.home.is_symlink() and paths.home.resolve() == paths.home
    assert not paths.instance.is_symlink()
    assert len(os.fsencode(paths.ssh_config.parent / "ssh.sock.1234567890123456")) < 104
    assert (paths.instance / "disk").is_symlink()
    assert (paths.instance / "disk").resolve() == paths.durable_instance / "disk"
    assert (paths.instance / "disk").stat().st_ino == original_inode
    assert (paths.home / "_config").resolve() == paths.durable_home / "_config"


def test_stopped_runtime_projection_is_reconstructible_without_moving_disk(projection):
    paths = projection
    paths.prepare()
    inode = (paths.durable_instance / "disk").stat().st_ino
    shutil.rmtree(paths.runtime)
    paths.prepare()
    assert (paths.instance / "disk").read_text() == "durable disk fixture"
    assert (paths.durable_instance / "disk").stat().st_ino == inode


def test_lima_atomic_replacement_is_synchronized_back_into_firm(projection):
    paths = projection
    paths.prepare()
    config = paths.instance / "lima.yaml"
    config.unlink()
    config.write_text("new Lima metadata")
    (paths.instance / "ssh.config").write_text("transient SSH config")
    paths.synchronize()
    assert config.is_symlink()
    assert (paths.durable_instance / "lima.yaml").read_text() == "new Lima metadata"
    assert not (paths.instance / "ssh.config").is_symlink()
    assert not (paths.durable_instance / "ssh.config").exists()


def test_runtime_alias_collision_and_symlink_substitution_fail_closed(projection, tmp_path):
    paths = projection
    paths.prepare()
    (paths.runtime / "owner.json").write_text(
        json.dumps({"root": "/another/firm", "instance": "linux"})
    )
    with pytest.raises(RuntimeError, match="another firm"):
        paths.prepare()
    (paths.runtime / "owner.json").write_text(
        json.dumps({"root": str(paths.root), "instance": "linux"})
    )
    alias = paths.instance / "disk"
    alias.unlink()
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    alias.symlink_to(outside)
    with pytest.raises(RuntimeError, match="Unexpected link"):
        paths.prepare()
    assert outside.read_text() == "preserve"


def test_durable_symlink_cannot_be_projected_as_a_vm_asset(projection, tmp_path):
    paths = projection
    disk = paths.durable_instance / "disk"
    disk.unlink()
    outside = tmp_path / "owner-file"
    outside.write_text("private owner data")
    disk.symlink_to(outside)
    with pytest.raises(RuntimeError, match="Unsafe persistent"):
        paths.prepare()
    assert outside.read_text() == "private owner data"


def test_missing_runtime_cannot_rebind_while_recorded_host_process_is_live(projection, monkeypatch):
    paths = projection
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})
    monkeypatch.setattr(
        paths_module.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout=f"limactl hostagent {paths.runtime}/lima/linux".encode()
        ),
    )
    with pytest.raises(RuntimeError, match="still running"):
        paths.prepare()
    assert not paths.runtime.exists()


def test_absent_running_record_falls_back_to_the_computed_runtime(projection):
    paths = projection
    assert not paths.running_record.exists()
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert reloaded.runtime == paths_module.RUNTIME_PARENT / paths.runtime.name


def test_vanished_recorded_runtime_falls_back_to_the_computed_runtime(projection):
    paths = projection
    paths.prepare()
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})
    shutil.rmtree(paths.runtime)
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert reloaded.runtime == paths.runtime
    assert not reloaded.runtime.exists()


def test_running_record_under_the_new_parent_is_reused(projection):
    paths = projection
    paths.prepare()
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert reloaded.runtime == paths.runtime
    assert reloaded.instance == paths.instance


def test_running_record_naming_a_directory_outside_the_runtime_parent_is_refused_by_prepare(
    projection, tmp_path
):
    # Construction must never raise for a bad record: it falls back to the
    # computed path and only prepare() refuses, with the same reason.
    paths = projection
    outside = tmp_path / "outside-the-runtime-parent" / paths.runtime.name
    outside.mkdir(mode=0o700, parents=True)
    paths._record(paths.running_record, {"runtime": str(outside), "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert reloaded.runtime != outside
    with pytest.raises(RuntimeError, match="untrusted runtime"):
        reloaded.prepare()


def test_running_record_with_the_wrong_digest_name_is_refused_by_prepare(projection):
    paths = projection
    wrong = paths_module.RUNTIME_PARENT / "not-this-firms-digest"
    wrong.mkdir(mode=0o700, parents=True)
    paths._record(paths.running_record, {"runtime": str(wrong), "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert reloaded.runtime != wrong
    with pytest.raises(RuntimeError, match="untrusted runtime"):
        reloaded.prepare()


def test_running_record_with_a_mismatched_owner_is_refused_by_prepare(projection):
    paths = projection
    paths.prepare()
    (paths.runtime / "owner.json").write_text(
        json.dumps({"root": "/another/firm", "instance": "linux"})
    )
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    with pytest.raises(RuntimeError, match="owned by another firm"):
        reloaded.prepare()
    # A stop builds nothing, so it verifies the runtime alone, and is refused the same way.
    with pytest.raises(RuntimeError, match="owned by another firm"):
        reloaded.verify_runtime()


def test_a_stop_refuses_a_computed_runtime_that_is_another_firms_without_any_record(projection):
    """No running record vouches for the computed path either; its own binding must."""
    paths = projection
    paths.prepare()
    (paths.runtime / "owner.json").write_text(
        json.dumps({"root": "/another/firm", "instance": "linux"})
    )
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    assert not reloaded.running_record.exists()
    with pytest.raises(RuntimeError, match="owned by another firm"):
        reloaded.verify_runtime()


def test_a_stop_refuses_a_runtime_that_is_a_link_to_another_directory(projection, tmp_path):
    paths = projection
    elsewhere = tmp_path / "another-firms-runtime"
    elsewhere.mkdir(mode=0o700)
    paths_module.RUNTIME_PARENT.mkdir(mode=0o700, parents=True)
    paths.runtime.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeError, match="runtime is unsafe"):
        paths.verify_runtime()


def test_a_runtime_that_cannot_be_read_is_refused_not_an_os_error(projection):
    """Callers that report rather than act catch RuntimeError; a PermissionError got past."""
    paths = projection
    paths.prepare()
    paths.runtime.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="runtime is unsafe|cannot be checked"):
            paths.verify_runtime()
    finally:
        paths.runtime.chmod(0o700)


def test_the_ssh_port_is_not_read_from_a_refused_runtime(projection):
    paths = projection
    paths.prepare()
    paths.ssh_config.write_text("Host lima-linux\n  Port 60022\n")
    assert paths.ssh_port() == 60022
    (paths.runtime / "owner.json").write_text(
        json.dumps({"root": "/another/firm", "instance": "linux"})
    )
    with pytest.raises(RuntimeError, match="owned by another firm"):
        paths.ssh_port()


def test_a_stop_accepts_its_own_runtime_and_one_that_is_not_there(projection):
    paths = projection
    paths.verify_runtime()  # nothing there: nothing to stop, nothing to refuse
    paths.prepare()
    paths.verify_runtime()


def test_an_unreadable_running_record_does_not_block_construction(projection):
    """An owner-owned record of mode 000 passes the metadata checks and cannot be read."""
    paths = projection
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})
    paths.running_record.chmod(0o000)
    try:
        reloaded = paths_module.lima_paths(paths.root, instance="linux")
        with pytest.raises(RuntimeError, match="Invalid host state record"):
            reloaded.verify_runtime()
    finally:
        paths.running_record.chmod(0o600)


def test_running_record_with_a_non_string_runtime_field_is_refused_by_prepare(projection):
    paths = projection
    paths._record(paths.running_record, {"runtime": 12345, "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    with pytest.raises(RuntimeError, match="Invalid VM host process record"):
        reloaded.prepare()


def test_construction_error_does_not_block_has_live_processes_or_verify_stopped(
    projection, tmp_path, monkeypatch
):
    """_recorded_runtime's rejection must not stop the independent shutdown checks."""
    paths = projection
    outside = tmp_path / "outside-the-runtime-parent" / paths.runtime.name
    outside.mkdir(mode=0o700, parents=True)
    paths._record(paths.running_record, {"runtime": str(outside), "pids": [99999]})
    reloaded = paths_module.lima_paths(paths.root, instance="linux")

    monkeypatch.setattr(
        paths_module.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b""),
    )
    # has_live_processes reads the record's own pids/runtime fields directly; the
    # shape/parent rejection above never touches it.
    assert reloaded.has_live_processes() is False

    monkeypatch.setattr(reloaded, "has_live_processes", lambda: False)
    monkeypatch.setattr(
        paths_module.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(stdout=b"12345 /usr/bin/other\n"),
    )

    def running():
        raise paths_module.VMStateError("Broken")

    reloaded.verify_stopped(running)  # still gets its attempt; does not raise


def test_bad_record_on_one_vm_does_not_block_a_sibling_vms_construction(projection):
    """One VM's corrupt record must not stop a sibling VM's shutdown attempt from reaching it."""
    linux_paths = projection
    linux_paths._record(linux_paths.running_record, {"runtime": 12345, "pids": [99999]})

    reloaded_linux = paths_module.lima_paths(linux_paths.root, instance="linux")
    mac_paths = paths_module.lima_paths(linux_paths.root, instance="mac")
    mac_paths.prepare()
    assert mac_paths.runtime.exists()

    with pytest.raises(RuntimeError, match="Invalid VM host process record"):
        reloaded_linux.prepare()


@pytest.mark.parametrize("hostile", ["symlink", "permissions"])
def test_a_recorded_runtime_under_a_hostile_parent_is_refused(projection, tmp_path, hostile):
    """Construction absorbs the unsafe parent behind the record; prepare() and a stop's
    verify_runtime() both surface it."""
    paths = projection
    parent = paths_module.RUNTIME_PARENT
    parent.parent.mkdir(mode=0o700, parents=True)
    if hostile == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        parent.symlink_to(elsewhere, target_is_directory=True)
    else:
        parent.mkdir()
        parent.chmod(0o777)  # chmod, not mkdir's mode: the umask cannot narrow it
    paths.runtime.mkdir(mode=0o700)
    paths._record(paths.runtime / "owner.json", paths.owner_binding)
    paths._record(paths.running_record, {"runtime": str(paths.runtime), "pids": [99999]})

    reloaded = paths_module.lima_paths(paths.root, instance="linux")
    with pytest.raises(RuntimeError, match="runtime parent is unsafe"):
        reloaded.verify_runtime()
    with pytest.raises(RuntimeError, match="runtime parent is unsafe"):
        reloaded.prepare()


def test_runtime_parent_requires_exclusive_owner_access(projection):
    paths_module.RUNTIME_PARENT.parent.mkdir(mode=0o700, parents=True)
    paths_module.RUNTIME_PARENT.mkdir()
    paths_module.RUNTIME_PARENT.chmod(0o777)
    with pytest.raises(RuntimeError, match="Unsafe host state"):
        projection.prepare()


def test_shared_parent_requires_exclusive_owner_access(projection):
    paths_module.RUNTIME_PARENT.parent.mkdir(parents=True)
    paths_module.RUNTIME_PARENT.parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="Unsafe host state"):
        projection.prepare()


def test_shared_parent_must_be_a_real_directory_not_a_symlink(projection, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    paths_module.RUNTIME_PARENT.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Unsafe host state"):
        projection.prepare()
    assert elsewhere.is_dir()


def test_durable_instance_removal_requires_runtime_instance_removed(projection):
    paths = projection
    paths.prepare()
    with pytest.raises(RuntimeError, match="runtime VM instance"):
        paths.remove_instance()
    shutil.rmtree(paths.instance)
    paths.remove_instance()
    assert not paths.durable_instance.exists()


def test_short_roots_keep_existing_layout_without_creating_projection():
    paths = paths_module.lima_paths(Path("/tmp").resolve() / "vm-short", instance="linux")
    assert not paths.projected
    assert paths.home == Path("/tmp").resolve() / "vm-short/lima"
    assert paths.ssh_config == paths.durable_instance / "ssh.config"


def test_mac_instance_has_distinct_binding_and_stopped_adoption(projection):
    paths = paths_module.lima_paths(projection.root, instance="mac")
    assert paths.runtime != projection.runtime
    assert projection.running_record.name == "linux-host-running.json"
    assert paths.running_record.name == "mac-host-running.json"
    assert projection.owner_binding == {"root": str(paths.root), "instance": "linux"}
    paths.prepare()
    paths.instance.mkdir(mode=0o700)
    disk = paths.instance / "disk"
    disk.write_text("mac disk with retained station state")
    inode = disk.stat().st_ino
    (paths.instance / "lima.yaml").write_text("os: Darwin\n")
    paths.adopt_stopped_instance()
    assert disk.is_symlink()
    assert paths.durable_instance.name == "mac"
    assert (paths.durable_instance / "disk").stat().st_ino == inode
    assert json.loads((paths.runtime / "owner.json").read_text()) == {
        "root": str(paths.root),
        "instance": "mac",
    }
    shutil.rmtree(paths.runtime)
    paths.prepare()
    assert disk.read_text() == "mac disk with retained station state"
    with pytest.raises(RuntimeError, match="already exists"):
        paths.adopt_stopped_instance()
    shutil.rmtree(paths.instance)
    paths.remove_instance()
    assert not paths.durable_instance.exists()
    assert projection.durable_instance.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "pid", "permissions"])
def test_adoption_rejects_untrusted_or_active_creation(projection, unsafe):
    paths = paths_module.lima_paths(projection.root, instance="mac")
    paths.prepare()
    paths.instance.mkdir(mode=0o700)
    if unsafe == "symlink":
        (paths.instance / "disk").symlink_to(projection.durable_instance / "disk")
    elif unsafe == "pid":
        (paths.instance / "ha.pid").write_text("12345")
    else:
        (paths.instance / "disk").touch(mode=0o666)
        (paths.instance / "disk").chmod(0o666)
    with pytest.raises(RuntimeError, match="Unsafe persistent|process or socket"):
        paths.adopt_stopped_instance()
    assert not paths.durable_instance.exists()
    assert paths.instance.exists()


def test_instance_name_cannot_escape_runtime_root():
    with pytest.raises(ValueError, match="Unknown VM instance"):
        paths_module.lima_paths("/private/tmp/a", instance="../escape")


def test_ssh_port_comes_from_the_generated_ssh_config(tmp_path, monkeypatch):
    paths = paths_module.lima_paths(Path("/tmp").resolve() / "vm-short", instance="mac")
    monkeypatch.setattr(paths, "ssh_config", tmp_path / "ssh.config")
    paths.ssh_config.write_text(
        'Host lima-mac\n  IdentityFile "/x/user"\n  Hostname 127.0.0.1\n  Port 60022\n'
    )
    assert paths.ssh_port() == 60022
    paths.ssh_config.write_text("Host lima-mac\n")
    with pytest.raises(RuntimeError, match="No SSH port"):
        paths.ssh_port()


# ---- logs kept across a stop --------------------------------------------------------


def test_logs_are_kept_on_stop(projection):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("boot ok\n")
    (paths.instance / "serial.log").write_text("console output\n")

    paths.mark_stopped()

    logs = paths.durable_instance / "logs"
    assert (logs / "ha.stderr.log").read_text() == "boot ok\n"
    assert (logs / "serial.log").read_text() == "console output\n"
    assert not (paths.durable_instance / "logs.previous").exists()
    # A copy, not a move: the runtime keeps its own logs until it is cleared.
    assert (paths.instance / "ha.stderr.log").read_text() == "boot ok\n"


def test_second_stop_with_new_logs_rotates_the_previous_copy_once(projection):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("first boot\n")
    paths.mark_stopped()

    (paths.instance / "ha.stderr.log").write_text("second boot, and much longer this time\n")
    paths.mark_stopped()

    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == (
        "second boot, and much longer this time\n"
    )
    assert (paths.durable_instance / "logs.previous" / "ha.stderr.log").read_text() == (
        "first boot\n"
    )


def test_repeat_stop_with_unchanged_logs_rotates_nothing(projection):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("steady state\n")
    paths.mark_stopped()

    paths.mark_stopped()

    assert not (paths.durable_instance / "logs.previous").exists()
    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == "steady state\n"


def test_only_two_log_generations_are_kept(projection):
    paths = projection
    paths.prepare()
    for content in ("generation one\n", "generation two\n", "generation three\n"):
        (paths.instance / "ha.stderr.log").write_text(content)
        paths.mark_stopped()

    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == "generation three\n"
    assert (paths.durable_instance / "logs.previous" / "ha.stderr.log").read_text() == (
        "generation two\n"
    )


def test_logs_are_not_projected_back_into_the_runtime(projection):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("boot ok\n")
    paths.mark_stopped()

    paths.synchronize()

    assert not (paths.instance / "logs").exists()
    assert not (paths.instance / "logs").is_symlink()


def test_a_failing_log_copy_does_not_raise(projection, monkeypatch, caplog):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("boot ok\n")

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(paths_module.shutil, "copy2", fail)
    with caplog.at_level("WARNING"):
        paths.mark_stopped()
    assert "Could not preserve" in caplog.text


def test_two_consecutive_failing_copies_do_not_cost_the_last_good_archive(
    projection, monkeypatch, caplog
):
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("first boot\n")
    paths.mark_stopped()  # the last good archive

    real_copy2 = paths_module.shutil.copy2
    remaining_failures = [2]

    def flaky_copy2(*args, **kwargs):
        if remaining_failures[0] > 0:
            remaining_failures[0] -= 1
            raise OSError("disk full")
        return real_copy2(*args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "copy2", flaky_copy2)

    with caplog.at_level("WARNING"):
        (paths.instance / "ha.stderr.log").write_text("second boot, failing copy one\n")
        paths.mark_stopped()
        staging = paths.durable_instance / "logs.staging"
        assert staging.exists() and not any(staging.iterdir())

        (paths.instance / "ha.stderr.log").write_text(
            "third boot, failing copy two, longer still\n"
        )
        paths.mark_stopped()  # the stale staging directory above is cleared and retried
        assert staging.exists() and not any(staging.iterdir())

    assert caplog.text.count("Could not preserve") == 2
    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == "first boot\n"
    assert not (paths.durable_instance / "logs.previous").exists()

    (paths.instance / "ha.stderr.log").write_text("fourth boot, the copy that succeeds\n")
    paths.mark_stopped()

    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == (
        "fourth boot, the copy that succeeds\n"
    )
    assert (paths.durable_instance / "logs.previous" / "ha.stderr.log").read_text() == (
        "first boot\n"
    )
    assert not staging.exists()


def test_two_consecutive_failing_publications_do_not_cost_the_last_published_archive(
    projection, monkeypatch, caplog
):
    """The rotation moved logs aside and the publish failed: logs.previous is all there is."""
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("first boot\n")
    paths.mark_stopped()  # the last published archive

    real_rename = Path.rename
    remaining_failures = [2]

    def flaky_rename(self, target):
        if self.name == "logs.staging" and remaining_failures[0] > 0:
            remaining_failures[0] -= 1
            raise OSError("no space left on device")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    previous = paths.durable_instance / "logs.previous"
    with caplog.at_level("WARNING"):
        for content in ("second boot, longer\n", "third boot, longer again\n"):
            (paths.instance / "ha.stderr.log").write_text(content)
            paths.mark_stopped()
            assert (previous / "ha.stderr.log").read_text() == "first boot\n"
            assert not (paths.durable_instance / "logs").exists()
    assert caplog.text.count("Could not preserve") == 2

    (paths.instance / "ha.stderr.log").write_text("fourth boot, the publish that succeeds\n")
    paths.mark_stopped()
    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == (
        "fourth boot, the publish that succeeds\n"
    )
    assert (previous / "ha.stderr.log").read_text() == "first boot\n"


def test_verify_stopped_archives_logs_on_the_accepted_broken_path(projection, monkeypatch):
    """The Broken instance verify_stopped accepts is exactly the failure logs matter for."""
    paths = projection
    paths.prepare()
    (paths.instance / "ha.stderr.log").write_text("broken vm\n")
    monkeypatch.setattr(paths, "has_live_processes", lambda: False)
    monkeypatch.setattr(
        paths_module.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"12345 /usr/bin/other\n"),
    )

    def running():
        raise paths_module.VMStateError("Broken")

    paths.verify_stopped(running)

    assert (paths.durable_instance / "logs" / "ha.stderr.log").read_text() == "broken vm\n"


def test_non_projected_firm_needs_no_log_preservation():
    """A short root's durable instance is its instance; there is nothing to copy."""
    root = Path("/tmp").resolve() / ("vm-short-logs-" + uuid4().hex[:12])
    paths = paths_module.lima_paths(root, instance="linux")
    assert not paths.projected
    try:
        paths.durable_instance.mkdir(mode=0o700, parents=True)
        (paths.durable_instance / "ha.stderr.log").write_text("boot ok\n")
        paths.mark_stopped()
        assert not (paths.durable_instance / "logs").exists()
    finally:
        shutil.rmtree(root)
