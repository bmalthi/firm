"""Catalog, org-chart delivery, owner overrides and release reconciliation."""

import json
import shutil
from pathlib import Path

import pytest

from firm_runtime import guidance
from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store, encoded


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test", "Build things.",
        {"max_active": 4, "staffing": False, "founder_profile": "main", "profiles": {"main": {}}},
    )
    return runtime, now


def role(runtime, employee_id="founder"):
    with runtime.store.read() as db:
        return runtime.store.role(db, employee_id)


def selection(runtime, employee_id="founder"):
    with runtime.store.read() as db:
        return guidance.effective(
            db, runtime.store.role(db, employee_id), runtime.accounts.config(db)
        )


def hire(runtime, manager="founder", slug="worker", **kwargs):
    return runtime.lifecycle.hire(manager, slug, "Do work.", request_id="hire-" + slug, **kwargs)[
        "employee_id"
    ]


def test_packaged_catalog_and_all_bodies():
    assert len(guidance.catalog()) == 20
    assert len(guidance.chooser_entries()) == 18
    assert all(guidance.body(e["id"]).startswith("### ") for e in guidance.catalog())


@pytest.mark.parametrize("defect", [
    "missing", "encoding", "orphan", "duplicate", "dimension", "pattern", "empty", "heading",
    "org_chart", "title", "applies", "keys", "not_list",
])
def test_catalog_refuses_packaging_errors(tmp_path, monkeypatch, defect):
    source = Path(__file__).parents[1] / "src/firm_runtime/prompts/guidance"
    target = tmp_path / "prompts/guidance"
    shutil.copytree(source, target)
    entries = json.loads((target / "catalog.json").read_text())
    if defect == "encoding":
        (target / "legal.md").write_bytes(b"\xff")
    elif defect == "missing":
        (target / "legal.md").unlink()
    elif defect == "orphan":
        (target / "orphan.md").write_text("### Orphan\n")
    elif defect == "duplicate":
        entries.append(entries[0])
    elif defect == "dimension":
        entries[0]["dimension"] = "other"
    elif defect == "pattern":
        entries[0]["id"] = "../escape"
    elif defect == "empty":
        (target / "legal.md").write_text("\n")
    elif defect == "heading":
        (target / "legal.md").write_text("## Wrong heading\n")
    elif defect == "org_chart":
        entries.pop(0)
        (target / "firm-leader.md").unlink()
    elif defect in {"title", "applies"}:
        entries[0][defect] = ""
    elif defect == "keys":
        entries[0]["extra"] = True
    elif defect == "not_list":
        entries = {}
    (target / "catalog.json").write_text(json.dumps(entries))
    monkeypatch.setattr(guidance, "files", lambda _: tmp_path)
    guidance.catalog.cache_clear()
    try:
        with pytest.raises(FirmError, match="guidance"):
            guidance.catalog()
    finally:
        guidance.catalog.cache_clear()


def test_org_chart_keeps_paused_and_exhausted_reports_then_restores_chooser_scope(system):
    runtime, _ = system
    assert selection(runtime) == {"scope": "firm-leader", "functions": []}
    lead = hire(runtime, slug="lead")
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["staffing"] = {"classes": {"medium": "main"}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
        db.execute("UPDATE roles SET scope_guidance='specialist-adviser',guidance_state='chosen' "
                   "WHERE employee_id=?", (lead,))
    assert selection(runtime, lead)["scope"] == "specialist-adviser"
    child = hire(runtime, lead, "child", temporary_periods=1)
    assert selection(runtime, lead)["scope"] == "team-leader"
    runtime.owner.stop_role(OWNER, child, "pause")
    assert selection(runtime, lead)["scope"] == "team-leader"
    with runtime.store.transaction() as db:
        db.execute("UPDATE appointments SET remaining_periods=0 WHERE employee_id=?", (child,))
    assert selection(runtime, lead)["scope"] == "team-leader"
    runtime.lifecycle.fire(lead, child, reason="Done", request_id="fire")
    assert selection(runtime, lead)["scope"] == "specialist-adviser"


def test_staffing_switch_changes_effective_selection_without_writes(system):
    runtime, _ = system
    employee = hire(runtime)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET scope_guidance='cross-team-lead',functional_guidance='[\"legal\"]',"
            "guidance_state='chosen' WHERE employee_id=?",
            (employee,),
        )
        config = runtime.accounts.config(db)
        config["staffing"] = {"classes": {}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
    original = role(runtime, employee)
    assert selection(runtime)["scope"] == "firm-leader"
    assert selection(runtime, employee) == {"scope": "cross-team-lead", "functions": ["legal"]}
    with runtime.store.transaction() as db:
        config["staffing"] = False
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
    assert selection(runtime, employee) == {"scope": None, "functions": []}
    assert role(runtime, employee) == original
    with runtime.store.transaction() as db:
        config["staffing"] = {"classes": {}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
    assert selection(runtime, employee) == {"scope": "cross-team-lead", "functions": ["legal"]}
    runtime.owner.set_guidance(OWNER, employee, "specialist-adviser", ["sales"], "set")
    with runtime.store.transaction() as db:
        config["staffing"] = False
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
    assert selection(runtime, employee) == {"scope": "specialist-adviser", "functions": ["sales"]}


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("remaining", [[], ["sales"]])
def test_removed_guides_reconcile_once_keep_owner_or_request_current_revision(
    system, owner, remaining
):
    runtime, _ = system
    employee = hire(runtime)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["staffing"] = {"classes": {}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
        db.execute(
            "UPDATE roles SET guidance_state=?,scope_guidance='removed-scope',"
            "functional_guidance=?,guidance_attempts=2 WHERE employee_id=?",
            ("owner" if owner else "chosen", encoded(["removed-function", *remaining]), employee),
        )
    runtime.start()
    result = role(runtime, employee)
    assert result["scope_guidance"] is None
    assert json.loads(result["functional_guidance"]) == remaining
    assert result["guidance_state"] == ("owner" if owner else "pending")
    if not owner:
        assert result["guidance_revision"] == 0
        assert result["guidance_attempts"] == 0
    with runtime.store.read() as db:
        before = list(db.iterdump())
        (notice,) = db.execute(
            "SELECT body FROM messages WHERE subject=?", ("Role guidance no longer available",)
        ).fetchall()
        assert "removed-scope, removed-function" in notice[0]
    runtime.start()
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_removed_guides_with_staffing_off_drop_ids_without_a_notice(system):
    runtime, _ = system
    employee = hire(runtime)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET guidance_state='chosen',scope_guidance='removed-scope',"
            "functional_guidance=? WHERE employee_id=?",
            (encoded(["removed-function", "sales"]), employee),
        )
    runtime.start()
    result = role(runtime, employee)
    assert result["scope_guidance"] is None
    assert json.loads(result["functional_guidance"]) == ["sales"]
    assert result["guidance_state"] == "chosen"
    with runtime.store.read() as db:
        assert not db.execute(
            "SELECT 1 FROM messages WHERE subject=?", ("Role guidance no longer available",)
        ).fetchall()


def test_owner_controls_validate_dimensions_authority_and_replay(system):
    runtime, _ = system
    employee = hire(runtime)
    for scope, functions in [
        ("legal", []),
        ("unknown", []),
        (None, ["team-leader"]),
        (None, ["../legal"]),
        (None, [False]),
    ]:
        with pytest.raises(FirmError):
            runtime.owner.set_guidance(OWNER, employee, scope, functions, "invalid")
    for operation in [
        lambda: runtime.owner.set_guidance(employee, employee, None, [], "set"),
        lambda: runtime.owner.automatic_guidance(employee, employee, "auto"),
    ]:
        with pytest.raises(FirmError, match="only the owner"):
            operation()
    expected = {"scope": "firm-leader", "functions": ["legal", "sales"]}
    assert (
        runtime.owner.set_guidance(OWNER, employee, "firm-leader", ["sales", "legal"], "set")
        == expected
    )
    assert (
        runtime.owner.set_guidance(OWNER, employee, "firm-leader", ["sales", "legal"], "set")
        == expected
    )
    runtime.lifecycle.revise_mandate("founder", employee, 0, "Changed", request_id="rev")
    assert role(runtime, employee)["guidance_state"] == "owner"
    assert selection(runtime, employee) == expected
    assert runtime.owner.automatic_guidance(OWNER, employee, "auto") == {
        "scope": None,
        "functions": [],
    }
    assert role(runtime, employee)["guidance_state"] is None
    assert role(runtime, employee)["guidance_revision"] is None
    assert runtime.owner.set_guidance(OWNER, employee, None, [], "empty") == {
        "scope": None,
        "functions": [],
    }


def test_setup_and_host_start_refuse_invalid_catalog(system, tmp_path, monkeypatch):
    from firm_runtime import sandbox_dispatcher, setup_check

    def broken():
        raise FirmError("guidance legal: missing body")

    monkeypatch.setattr(guidance, "catalog", broken)
    report = setup_check.check_setup(tmp_path, check_ports=False)
    assert (
        next(c for c in report["checks"] if c["check"] == "guidance_catalog")["status"] == "failure"
    )
    # Validation precedes even the host-open and provider preflight checks.
    with pytest.raises(FirmError, match="guidance legal"):
        sandbox_dispatcher.SandboxDispatcher.start(None)


def test_guidance_resource_directory_contains_only_catalog_bodies():
    directory = Path(__file__).parents[1] / "src/firm_runtime/prompts"
    assert (
        (directory / "guidance-introduction.md").read_text().startswith("## Guidance for your role")
    )
    assert {f.name for f in (directory / "guidance").iterdir()} == {
        "catalog.json", *(entry["id"] + ".md" for entry in guidance.catalog())
    }


def test_automatic_clears_owner_selection_while_staffing_is_off(system):
    runtime, _ = system
    employee = hire(runtime)
    runtime.owner.set_guidance(OWNER, employee, "specialist-adviser", ["legal"], "set")
    assert runtime.owner.automatic_guidance(OWNER, employee, "auto") == {
        "scope": None, "functions": [],
    }
    current = role(runtime, employee)
    assert current["scope_guidance"] is None
    assert json.loads(current["functional_guidance"]) == []
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["staffing"] = {"classes": {}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
    assert selection(runtime, employee) == {"scope": None, "functions": []}


@pytest.mark.parametrize("scope", ["firm-leader", "team-leader", "legal", "removed-scope"])
def test_automatic_delivery_accepts_only_chooser_scopes_from_storage(system, scope):
    runtime, _ = system
    employee = hire(runtime)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["staffing"] = {"classes": {}}
        db.execute("UPDATE firm SET config_json=?", (encoded(config),))
        db.execute("UPDATE roles SET scope_guidance=?,guidance_state='chosen' WHERE employee_id=?",
                   (scope, employee))
    assert selection(runtime, employee) == {"scope": None, "functions": []}
