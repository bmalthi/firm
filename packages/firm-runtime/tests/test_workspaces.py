
import pytest
from firm_sandbox import Config, Layout
from firm_sandbox.file_server import FileServer, local_runner

from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.workspaces import (
    grants_for,
    provision_runtime_role,
    sandbox_config,
    sandbox_role_id,
    workspace_path,
)


def file_server(root):
    """The packaged file server against a local tree; its guest steps run in simulation."""
    tree = root / "tree"
    tree.mkdir(exist_ok=True)
    files = FileServer(Config(root), local_runner(tree))
    files.start()
    files.tree = tree
    return files


def work(files, employee_id):
    return files.tree / workspace_path(files, employee_id)


@pytest.fixture
def firm(tmp_path):
    tmp_path.chmod(0o700)
    files = file_server(tmp_path)
    runtime = Runtime(Store.scratch(tmp_path / ".state/runtime.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {
            "max_active": 8,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    runtime.discovery.files = files
    provision_runtime_role(runtime.store, tmp_path, "founder", files=files)
    runtime.lifecycle.provision_role = lambda employee_id: provision_runtime_role(
        runtime.store, tmp_path, employee_id, files=files
    )
    return runtime, tmp_path


def hire(runtime, parent, slug):
    return runtime.lifecycle.hire(parent, slug, "Work", request_id=slug)["employee_id"]


def test_hire_returns_only_after_friendly_workspace_is_usable(firm):
    runtime, root = firm
    eng = hire(runtime, "founder", "engineering")
    api = hire(runtime, eng, "api")
    hire(runtime, "founder", "finance")
    grants = grants_for(runtime.store, api, Layout())
    assert grants.organization.address == "founder/engineering/api"
    assert grants.references == {
        "private": Layout().private,
        "scratch": Layout().scratch,
        "workspace": "/home/firmrole/drive/org/founder/engineering/api/work",
        "drive": Layout().drive,
        "organization": Layout().organization,
        "shared": Layout().shared,
    }
    assert workspace_path(runtime.discovery.files, api) == (
        "org/founder/engineering/api/work"
    )
    assert work(runtime.discovery.files, api).is_dir()
    assert runtime.discovery.files.credentials(sandbox_role_id(api)).user.startswith("firmfile-")


def test_rehire_waits_for_retired_descendant_but_not_active_ancestor(firm):
    runtime, root = firm
    manager = hire(runtime, "founder", "engineering")
    analyst = hire(runtime, manager, "api")
    files = runtime.discovery.files
    (work(files, analyst) / "result.md").write_text("completed work")
    runtime.start()
    invocations = []
    while invocation := runtime.admit():
        runtime.mark_started(invocation.activation_id)
        invocations.append(invocation)
    runtime.lifecycle.fire(manager, analyst, reason="Finished", request_id="retire-analyst")
    runtime.lifecycle.fire("founder", manager, reason="Finished", request_id="retire-manager")
    for invocation in invocations:
        if invocation.employee_id == manager:
            runtime.finish(invocation.activation_id, "completed")
    with pytest.raises(FirmError, match="retired subtree roles"):
        runtime.lifecycle.hire("founder", "engineering", "New work", request_id="replace")
    analyst_invocation = next(item for item in invocations if item.employee_id == analyst)
    runtime.finish(analyst_invocation.activation_id, "completed")
    replacement = runtime.lifecycle.hire("founder", "engineering", "New work", request_id="replace")
    assert work(files, replacement["employee_id"]).is_dir()
    archived = workspace_path(files, analyst)
    assert archived.startswith("org/.retired/")
    assert (files.tree / archived / "result.md").read_text() == "completed work"
    with runtime.store.read() as db:
        assert db.execute(
            "SELECT 1 FROM activations WHERE employee_id='founder' AND state='running'"
        ).fetchone()


def test_hierarchy_authority_cannot_drift_from_manager_relationship(firm):
    runtime, _ = firm
    eng = hire(runtime, "founder", "engineering")
    analyst = hire(runtime, eng, "api")
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET manager_id='founder' WHERE employee_id=?", (analyst,))
    with pytest.raises(FirmError, match="workspace hierarchy"):
        grants_for(runtime.store, analyst, Layout())


def test_retry_after_hiring_transaction_rollback_archives_orphaned_workspace(firm):
    runtime, root = firm
    files = runtime.discovery.files
    with pytest.raises(RuntimeError, match="rollback"):
        with runtime.store.transaction():
            original = hire(runtime, "founder", "analyst")
            (work(files, original) / "orphaned.md").write_text("preserved")
            raise RuntimeError("rollback")
    retry = hire(runtime, "founder", "analyst")
    assert retry != original
    assert work(files, retry).is_dir()
    assert not (work(files, retry) / "orphaned.md").exists()
    archived = workspace_path(files, original)
    assert archived.startswith("org/.retired/")
    assert (files.tree / archived / "orphaned.md").read_text() == "preserved"


def test_different_hire_after_rollback_gets_its_own_workspace(firm):
    runtime, root = firm
    with pytest.raises(RuntimeError, match="rollback"):
        with runtime.store.transaction():
            original = hire(runtime, "founder", "first")
            raise RuntimeError("rollback")
    different = hire(runtime, "founder", "second")
    assert different != original
    files = runtime.discovery.files
    assert workspace_path(files, different) == "org/founder/second/work"
    assert workspace_path(files, original) == "org/founder/first/work"


def test_storage_names_keep_short_employee_ids_readable():
    assert sandbox_role_id("founder") == "r-founder"
    assert sandbox_role_id("e123") == "r-e123"
    assert sandbox_role_id("unsafe/identity").startswith("r-")
    assert "/" not in sandbox_role_id("unsafe/identity")


def test_workspace_ownership_cannot_be_adopted_by_a_different_runtime_database(firm, tmp_path):
    runtime, root = firm
    employee = hire(runtime, "founder", "analyst")
    original = work(runtime.discovery.files, employee)
    (original / "retained.md").write_text("belongs to original firm")
    other = Runtime(Store.scratch(tmp_path / "different/runtime.sqlite3"))
    other.lifecycle.initialize(
        "other",
        "Other work",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    with pytest.raises(FirmError, match="different runtime database"):
        provision_runtime_role(other.store, root, "founder", files=runtime.discovery.files)
    assert (original / "retained.md").read_text() == "belongs to original firm"


def test_sandbox_config_loads_the_bound_firm_or_refuses_as_a_firm_error(tmp_path):
    with pytest.raises(FirmError, match="unreadable sandbox configuration"):
        sandbox_config(tmp_path)
    (tmp_path / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    assert sandbox_config(tmp_path).firm_id == "demo"
    (tmp_path / "sandbox.toml").write_text("[firm]\ncpus = 0\n")
    with pytest.raises(FirmError, match="unreadable sandbox configuration"):
        sandbox_config(tmp_path)
    with pytest.raises(FirmError, match="absolute"):
        sandbox_config("relative/firm")
