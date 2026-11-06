from firm_runtime.lifecycle import FOUNDER_ID
from firm_runtime.orientation import (
    MAX_NEIGHBORS,
    MAX_ORIENTATION_BYTES,
    build_orientation,
)
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store, encoded

CONFIG = {
    "max_active": 2,
    "staffing": False,
    "founder_profile": "default",
    "profiles": {"default": {}},
}


def _hire(runtime, manager, slug, request_id=None):
    return runtime.lifecycle.hire(
        manager,
        slug,
        f"Own {slug} work.",
        "default",
        request_id=request_id or f"hire-{slug}",
    )


def test_birth_orientation_has_manager_reports_and_top_level_with_honest_references(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build useful things.", CONFIG)
    manager = _hire(runtime, FOUNDER_ID, "delivery")
    sibling = _hire(runtime, FOUNDER_ID, "research")
    analyst = _hire(runtime, manager["employee_id"], "api")
    report = _hire(runtime, analyst["employee_id"], "testing")

    with runtime.store.read() as db:
        orientation = build_orientation(
            runtime.store,
            db,
            analyst["employee_id"],
            include_top_level=True,
        )

    assert orientation["manager"]["employee_id"] == manager["employee_id"]
    assert [role["employee_id"] for role in orientation["direct_reports"]] == [
        report["employee_id"]
    ]
    assert [role["employee_id"] for role in orientation["top_level_roles"]] == [
        sibling["employee_id"]
    ]
    assert orientation["omitted"] == {"direct_reports": 0, "top_level_roles": 0}
    rendered = encoded(orientation)
    assert len(rendered.encode()) <= MAX_ORIENTATION_BYTES
    assert "org_tree" in rendered
    assert orientation["direct_reports"][0]["inspect"].startswith("get_role(role=")
    assert orientation["references"]["organization_browse"].startswith("org_tree(")


def test_orientation_combines_report_and_top_level_limits_with_omission_counts(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build useful things.", CONFIG)
    manager = _hire(runtime, FOUNDER_ID, "manager")
    actor = _hire(runtime, manager["employee_id"], "actor")
    for index in range(24):
        _hire(
            runtime,
            actor["employee_id"],
            f"report-{index:02}",
            request_id=f"report-{index:02}",
        )
    for index in range(6):
        _hire(
            runtime,
            FOUNDER_ID,
            f"top-{index:02}",
            request_id=f"top-{index:02}",
        )

    with runtime.store.read() as db:
        orientation = build_orientation(
            runtime.store,
            db,
            actor["employee_id"],
            include_top_level=True,
        )

    shown = len(orientation["direct_reports"]) + len(orientation["top_level_roles"])
    assert shown == MAX_NEIGHBORS
    assert orientation["omitted"] == {"direct_reports": 4, "top_level_roles": 6}
    assert len(encoded(orientation).encode()) <= MAX_ORIENTATION_BYTES


def test_retained_orientation_only_repeats_changed_facts_after_successful_begin(tmp_path):
    now = [1_000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build useful things.", CONFIG)
    manager = _hire(runtime, FOUNDER_ID, "manager")
    actor = _hire(runtime, manager["employee_id"], "actor")
    _hire(runtime, actor["employee_id"], "report")
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM execution_requests")
        runtime.store.request_execution(db, actor["employee_id"], "test-birth")

    runtime.start()
    birth = runtime.admit()
    assert birth.employee_id == actor["employee_id"]
    assert birth.context["orientation"]["top_level_included"] is True
    runtime.mark_started(birth.activation_id)
    runtime.finish(birth.activation_id)

    runtime.messaging.send(
        OWNER,
        [actor["employee_id"]],
        "Check unchanged orientation",
        request_id="unchanged",
    )
    unchanged = runtime.admit()
    assert "orientation" not in unchanged.context
    runtime.mark_started(unchanged.activation_id)
    runtime.finish(unchanged.activation_id)

    with runtime.store.transaction() as db:
        before = db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0]
        db.execute(
            "UPDATE roles SET public_profile=? WHERE employee_id=?",
            ("Delivery Director\nCurrent public profile.", manager["employee_id"]),
        )
        assert db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0] == before
    runtime.messaging.send(
        OWNER,
        [actor["employee_id"]],
        "Check changed orientation",
        request_id="changed",
    )
    changed_prelaunch = runtime.admit()
    assert changed_prelaunch.context["orientation"]["manager"]["identity"] == "Delivery Director"
    runtime.mark_launch_attempted(changed_prelaunch.activation_id)
    runtime.finish(changed_prelaunch.activation_id, "failed")
    now[0] += 60
    retried = runtime.admit()
    assert retried.context["orientation"] == changed_prelaunch.context["orientation"]
    runtime.mark_started(retried.activation_id)
    runtime.finish(retried.activation_id)

    runtime.messaging.send(
        OWNER,
        [actor["employee_id"]],
        "Check settled orientation",
        request_id="unchanged-again",
    )
    after_success = runtime.admit()
    assert "orientation" not in after_success.context


def test_nonbirth_fresh_session_restores_bounded_top_level_neighborhood(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build useful things.", CONFIG)
    actor = _hire(runtime, FOUNDER_ID, "actor")
    sibling = _hire(runtime, FOUNDER_ID, "sibling")
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET birth_delivered=1 WHERE employee_id=?",
            (actor["employee_id"],),
        )
        db.execute("DELETE FROM execution_requests")
        runtime.store.request_execution(db, actor["employee_id"], "fresh-work")
    runtime.start()

    invocation = runtime.admit()
    assert invocation.context["fresh_session"] is True
    assert invocation.context["birth"] is False
    assert invocation.context["orientation"]["top_level_included"] is True
    assert [r["employee_id"] for r in invocation.context["orientation"]["top_level_roles"]] == [
        sibling["employee_id"]
    ]
