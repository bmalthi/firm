import shutil
from dataclasses import replace

import pytest
from runtime_test_support import FakeSandbox

from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store


def dispatcher(runtime, sandbox):
    return SandboxDispatcher(runtime, sandbox, {}, mcp_url="http://127.0.0.1:1/mcp")


def bound_firm(tmp_path):
    root = tmp_path / "original" / "2026-09-09_move-test"
    runtime = Runtime(Store.scratch(root / ".state" / "runtime.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Keep useful work",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    sandbox = FakeSandbox(root)
    with dispatcher(runtime, sandbox) as host:
        host._infrastructure_state("stopped")
    return runtime, sandbox


def relocate(runtime, sandbox, destination, *, copy=False):
    if copy:
        shutil.copytree(sandbox.config.root, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        sandbox.config.root.rename(destination)
    reopened = Runtime(Store.scratch(destination / ".state" / "runtime.sqlite3"))
    moved = FakeSandbox(destination)
    moved.runtime_id = sandbox.runtime_id
    return reopened, moved


def binding(runtime):
    with runtime.store.read() as db:
        return dict(db.execute("SELECT * FROM execution_host").fetchone())


@pytest.mark.parametrize("copy_then_remove", [False, True])
def test_stopped_firm_database_rebinds_after_whole_firm_relocation(tmp_path, copy_then_remove):
    runtime, sandbox = bound_firm(tmp_path)
    original = binding(runtime)
    moved_runtime, moved = relocate(
        runtime, sandbox, tmp_path / "relocated" / sandbox.config.root.name, copy=copy_then_remove
    )
    if copy_then_remove:
        shutil.rmtree(sandbox.config.root)
    with dispatcher(moved_runtime, moved):
        current = binding(moved_runtime)
        assert current == {**original, "sandbox_root": str(moved.config.root)}
        assert moved_runtime.status()["state"] == "stopped"
    # The new binding remains usable on subsequent host openings.
    with dispatcher(moved_runtime, moved):
        assert binding(moved_runtime) == current


def test_copied_database_or_firm_cannot_bind_while_original_remains(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    copied_runtime, copied = relocate(runtime, sandbox, tmp_path / "copy", copy=True)
    with pytest.raises(FirmError, match="previous sandbox root exists"):
        with dispatcher(copied_runtime, copied):
            pass
    assert binding(copied_runtime)["sandbox_root"] == str(sandbox.config.root)


@pytest.mark.parametrize("change", ["identity", "firm_id"])
def test_identity_and_firm_id_cannot_change_even_when_root_is_absent(tmp_path, change):
    runtime, sandbox = bound_firm(tmp_path)
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")
    if change == "identity":
        moved.runtime_id = "0" * 16
    else:
        moved.config = replace(moved.config, firm_id="different")
    with pytest.raises(FirmError, match="different sandbox identity"):
        with dispatcher(moved_runtime, moved):
            pass
    assert binding(moved_runtime)["sandbox_root"] == str(sandbox.config.root)


def test_replacing_identity_in_place_is_rejected(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    sandbox.runtime_id = "0" * 16
    with pytest.raises(FirmError, match="different sandbox identity"):
        with dispatcher(runtime, sandbox):
            pass


@pytest.mark.parametrize("observed", [True, None, "unknown"])
def test_relocation_requires_confirmed_host_shutdown(tmp_path, observed):
    runtime, sandbox = bound_firm(tmp_path)
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")
    moved.infrastructure_status = lambda: {"running": observed}
    with pytest.raises(FirmError, match="confirmed stopped"):
        with dispatcher(moved_runtime, moved):
            pass
    assert binding(moved_runtime)["sandbox_root"] == str(sandbox.config.root)


def test_relocation_refuses_unresolved_admission_despite_host_stopped(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    with runtime.store.transaction() as db:
        db.execute("UPDATE execution_host SET infrastructure_state='running'")
    runtime.start()
    runtime.admit()
    # Even a stale stopped firm row cannot erase its admitted execution.
    with runtime.store.transaction() as db:
        db.execute("UPDATE firm SET state='stopped'")
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")
    with pytest.raises(FirmError, match="settle its executions"):
        with dispatcher(moved_runtime, moved):
            pass


def test_dangling_original_path_is_not_treated_as_removed(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")
    sandbox.config.root.symlink_to(tmp_path / "absent")
    with pytest.raises(FirmError, match="previous sandbox root exists"):
        with dispatcher(moved_runtime, moved):
            pass


def test_relocation_requires_runtime_stop_even_when_no_role_is_active(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    with runtime.store.transaction() as db:
        db.execute("UPDATE firm SET state='running'")
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")
    with pytest.raises(FirmError, match="stop the firm"):
        with dispatcher(moved_runtime, moved):
            pass


def test_failed_host_state_probe_does_not_rebind_or_keep_ownership_lock(tmp_path):
    runtime, sandbox = bound_firm(tmp_path)
    moved_runtime, moved = relocate(runtime, sandbox, tmp_path / "moved")

    def unavailable():
        raise RuntimeError("host process state unavailable")

    moved.infrastructure_status = unavailable
    with pytest.raises(RuntimeError, match="host process state unavailable"):
        with dispatcher(moved_runtime, moved):
            pass
    assert binding(moved_runtime)["sandbox_root"] == str(sandbox.config.root)
    moved.infrastructure_status = lambda: {"running": False}
    with dispatcher(moved_runtime, moved):
        assert binding(moved_runtime)["sandbox_root"] == str(moved.config.root)
