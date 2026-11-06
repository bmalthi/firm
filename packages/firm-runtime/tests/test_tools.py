import pytest

from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store


def test_retirement_revokes_all_live_role_tools_and_preserves_old_identity(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "test",
        "Build",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )["founder"]["employee_id"]
    analyst = runtime.lifecycle.hire(founder, "analyst", "Do work", "default", request_id="hire")
    runtime.start()
    invocations = [runtime.admit(), runtime.admit()]
    for invocation in invocations:
        runtime.mark_started(invocation.activation_id)
    invocation = next(i for i in invocations if i.employee_id == analyst["employee_id"])
    facade = runtime.tools(invocation.token)
    assert facade.get_role(role=founder)["role"]["employee_id"] == founder
    context_message = runtime.messaging.send(
        analyst["employee_id"], [founder], "Context lookup", request_id="context-mail"
    )
    assert facade.context(context_message["conversation_id"])["status"] == "unavailable"
    runtime.lifecycle.fire(founder, analyst["employee_id"], reason="Reorganize", request_id="fire")
    with pytest.raises(FirmError, match="expired"):
        facade.peek()
    with pytest.raises(FirmError, match="expired"):
        facade.send([founder], "forged after retirement", "stale")
    replacement = runtime.lifecycle.hire(founder, "analyst", "Rework", "default", request_id="new")
    assert replacement["employee_id"] != analyst["employee_id"]
    with pytest.raises(FirmError, match="expired"):
        facade.handoff("Old analyst notes", "continue", "old-handoff")


def test_recovery_tokens_cannot_cross_employees_or_replay_a_completed_recovery(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {"max_active": 3, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    first = runtime.lifecycle.hire("founder", "first", "First", request_id="first")
    second = runtime.lifecycle.hire("founder", "second", "Second", request_id="second")
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET hold_reason='attempts_exhausted' WHERE employee_id!='founder'")
        first_token = runtime.store.role(db, first["employee_id"])["recovery_episode"]
        second_token = runtime.store.role(db, second["employee_id"])["recovery_episode"]
    assert first_token != second_token
    with pytest.raises(FirmError, match="stale recovery"):
        runtime.retry("founder", second["employee_id"], first_token, "cross-employee")
    with pytest.raises(FirmError, match="current direct manager"):
        runtime.retry(first["employee_id"], second["employee_id"], second_token, "unauthorized")
    retried = runtime.retry("founder", first["employee_id"], first_token, "retry-first")
    assert retried["recovery_episode"] != first_token
    assert runtime.retry("founder", first["employee_id"], first_token, "retry-first") == retried
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET hold_reason='attempts_exhausted' WHERE employee_id=?",
            (first["employee_id"],),
        )
        assert runtime.store.role(db, second["employee_id"])["recovery_episode"] == second_token
    with pytest.raises(FirmError, match="stale recovery"):
        runtime.retry("founder", first["employee_id"], first_token, "old-recovery-new-request")
