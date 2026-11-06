import pytest
from firm_sandbox import Layout

from firm_runtime.errors import FirmError
from firm_runtime.provider_support import render_invocation
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store
from firm_runtime.workspaces import sandbox_role_id


@pytest.fixture
def system(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {"max_active": 2, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    return runtime, founder, runtime.tools(founder.token)


def hire_and_run(runtime, tools, periods=1):
    hired = tools.hire("analyst", "Own work", "hire", temporary_periods=periods)
    invocation = runtime.admit()
    assert invocation.employee_id == hired["employee_id"]
    runtime.mark_started(invocation.activation_id)
    return hired, invocation


def test_last_period_finishes_and_renewal_retains_identity_mail_and_context(system):
    runtime, founder, tools = system
    hired, analyst = hire_and_run(runtime, tools)
    assert "work period" in render_invocation(analyst, None)
    assert hired["appointment"]["kind"] == "temporary"
    layout = Layout.for_role(sandbox_role_id(hired["employee_id"]))
    assert hired["access_on_activation"]["workspace"] == layout.workspace(hired["address"])
    assert hired["access_on_activation"]["shared"] == layout.shared
    runtime.begin_execution(analyst.token)  # duplicate handshake cannot spend twice
    runtime.finish(analyst.activation_id)
    tools.send([analyst.employee_id], "More work waiting for renewal", "more")
    assert runtime.admit() is None
    with runtime.store.read() as db:
        assert runtime.appointments.terms(db, analyst.employee_id)["used_periods"] == 1
    renewed = tools.renew_temporary(analyst.employee_id, 2, "renew")
    assert renewed["remaining_periods"] == 2
    assert tools.renew_temporary(analyst.employee_id, 2, "renew") == renewed
    next_work = runtime.admit()
    assert next_work.employee_id == analyst.employee_id
    assert next_work.session_id == analyst.session_id
    assert next_work.context["birth"] is False
    assert next_work.context["temporary_appointment"]["remaining_periods"] == 2


def test_period_budget_is_not_spent_by_admission_or_setup_failure(system):
    runtime, _, tools = system
    hired = tools.hire("analyst", "Own work", "hire", temporary_periods=1)
    analyst = runtime.admit()
    with runtime.store.read() as db:
        assert runtime.appointments.terms(db, hired["employee_id"])["remaining_periods"] == 1
    runtime.finish(analyst.activation_id, "interrupted", category="owner_stop")
    with runtime.store.read() as db:
        assert runtime.appointments.terms(db, hired["employee_id"])["remaining_periods"] == 1


def test_promotion_preserves_identity_and_does_not_clear_unrelated_hold(system):
    runtime, _, tools = system
    _, analyst = hire_and_run(runtime, tools)
    runtime.finish(analyst.activation_id)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET hold_reason='refusal' WHERE employee_id=?", (analyst.employee_id,)
        )
    promoted = tools.promote(analyst.employee_id, "promote")
    assert promoted["kind"] == "durable"
    assert tools.promote(analyst.employee_id, "promote") == promoted
    assert runtime.admit() is None
    with runtime.store.read() as db:
        role = runtime.store.role(db, analyst.employee_id)
        assert role["hold_reason"] == "refusal"
        assert role["session_id"] == analyst.session_id


def test_only_direct_manager_can_renew_and_retired_roles_stay_retired(system):
    runtime, founder, tools = system
    _, analyst = hire_and_run(runtime, tools)
    with pytest.raises(FirmError, match="direct manager"):
        runtime.appointments.renew(analyst.employee_id, analyst.employee_id, 1, "self")
    tools.fire(analyst.employee_id, "Retire", "fire")
    with pytest.raises(FirmError, match="retired"):
        tools.renew_temporary(analyst.employee_id, 1, "renew")
    with pytest.raises(FirmError, match="retired"):
        tools.promote(analyst.employee_id, "promote")


def test_budget_and_stopped_state_survive_restart(system):
    runtime, _, tools = system
    _, analyst = hire_and_run(runtime, tools)
    runtime.finish(analyst.activation_id)
    runtime.stop(force=True)
    reopened = Runtime(Store.scratch(runtime.store.path))
    assert reopened.status()["temporary_appointments"][0]["remaining_periods"] == 0
    assert reopened.admit() is None


@pytest.mark.parametrize("operation", ["renew", "promote"])
@pytest.mark.parametrize("change", ["retire", "manager"])
def test_receipt_replay_survives_target_state_changes(system, operation, change):
    runtime, _, tools = system
    _, analyst = hire_and_run(runtime, tools)
    runtime.finish(analyst.activation_id)

    def mutate(request, employee=analyst.employee_id):
        if operation == "renew":
            return tools.renew_temporary(employee, 2, request)
        return tools.promote(employee, request)

    result = mutate("change")
    if change == "retire":
        tools.fire(analyst.employee_id, "Retire", "fire")
    else:
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE roles SET manager_id=? WHERE employee_id=?", (OWNER, analyst.employee_id)
            )
    with runtime.store.read() as db:
        events = db.execute("SELECT COUNT(*) FROM analysis_events").fetchone()[0]
        wakes = db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0]
    assert mutate("change") == result
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_events").fetchone()[0] == events
        assert db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0] == wakes
    with pytest.raises(FirmError, match="retired|direct manager"):
        mutate("new")
    with pytest.raises(FirmError, match="request"):
        mutate("change", "founder")


@pytest.mark.parametrize("periods", [0, -1, True, 1.5, 1001])
def test_invalid_explicit_budgets(system, periods):
    _, _, tools = system
    with pytest.raises(FirmError, match="explicit integer"):
        tools.hire("analyst", "Own work", "hire", temporary_periods=periods)
