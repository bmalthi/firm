import json

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.store import ORDINARY_PRIORITY, Store


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0])
    runtime = Runtime(store)
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {"max_active": 1, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    runtime.start()
    first = runtime.admit()
    runtime.mark_started(first.activation_id)
    return runtime, first, now


def test_reminder_survives_restart_and_stop_releases_once_as_ordinary_mail(system):
    runtime, first, now = system
    tools = runtime.tools(first.token)
    reminder = tools.set_reminder(1100, "Check the customer response", "schedule")
    assert tools.set_reminder(1100, "Check the customer response", "schedule") == reminder
    runtime.finish(first.activation_id)
    assert runtime.admit() is None
    runtime.stop()
    now[0] = 1200
    runtime = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
    assert runtime.admit() is None
    assert len(runtime.reminders.list(first.employee_id)["reminders"]) == 1
    runtime.start()
    due = runtime.admit()
    assert due.employee_id == first.employee_id
    with runtime.store.read() as db:
        mail = db.execute(
            "SELECT * FROM messages WHERE subject='Scheduled self-reminder'"
        ).fetchall()
        assert len(mail) == 1
        assert "Check the customer response" in mail[0]["body"]
        # The due time a role reads is a date, never raw epoch seconds.
        assert "(due at 1970-01-01T00:18:20Z)" in mail[0]["body"]
        snapshot = db.execute(
            "SELECT wake_snapshot FROM activations WHERE activation_id=?", (due.activation_id,)
        ).fetchone()[0]
        assert all(wake["priority"] == ORDINARY_PRIORITY for wake in json.loads(snapshot))
    runtime.mark_started(due.activation_id)
    runtime.finish(due.activation_id)
    assert runtime.admit() is None
    assert runtime.reminders.list(first.employee_id)["reminders"] == []
    # An identical retry remains idempotent even after its due time passed.
    assert (
        runtime.reminders.set(first.employee_id, 1100, "Check the customer response", "schedule")
        == reminder
    )


def test_due_reminder_during_activation_preserves_single_execution(system):
    runtime, first, now = system
    runtime.tools(first.token).set_reminder(1100, "Follow up", "schedule")
    now[0] = 1100
    assert runtime.admit() is None
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0] == 1
    runtime.finish(first.activation_id)
    assert runtime.admit().employee_id == first.employee_id


def test_cancel_is_owned_idempotent_and_does_not_create_work(system):
    runtime, first, now = system
    tools = runtime.tools(first.token)
    reminder = tools.set_reminder(1100, "Follow up", "schedule")
    child = tools.hire("child", "Help", "hire")["employee_id"]
    with pytest.raises(FirmError, match="unknown reminder"):
        runtime.reminders.cancel(child, reminder["reminder_id"], "steal")
    result = tools.cancel_reminder(reminder["reminder_id"], "cancel")
    assert result["state"] == "cancelled"
    assert tools.cancel_reminder(reminder["reminder_id"], "cancel") == result
    runtime.finish(first.activation_id)
    now[0] = 1200
    runtime.admit()
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE subject='Scheduled self-reminder'"
            ).fetchone()[0]
            == 0
        )


def test_retired_identity_never_delivers_to_replacement(system):
    runtime, first, now = system
    child = runtime.tools(first.token).hire("child", "Help", "hire")["employee_id"]
    runtime.reminders.set(child, 1100, "Old employee work", "schedule")
    runtime.lifecycle.fire(first.employee_id, child, reason="Replace", request_id="fire")
    replacement = runtime.tools(first.token).hire("child", "New work", "new")
    now[0] = 1200
    runtime.finish(first.activation_id)
    assert runtime.admit().employee_id == replacement["employee_id"]
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM reminders").fetchone()[0] == "cancelled"
        assert (
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE subject='Scheduled self-reminder'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("due", [float("nan"), float("inf"), True, "tomorrow", 999])
def test_invalid_due_time(system, due):
    runtime, first, _ = system
    with pytest.raises(FirmError):
        runtime.tools(first.token).set_reminder(due, "Follow up", "schedule")


def test_expired_activation_cannot_schedule(system):
    runtime, first, _ = system
    runtime.finish(first.activation_id)
    with pytest.raises(FirmError, match="expired"):
        runtime.tools(first.token).set_reminder(1100, "Follow up", "schedule")


def test_note_length_is_bounded_at_4096_characters(system):
    runtime, first, _ = system
    tools = runtime.tools(first.token)
    tools.set_reminder(1100, "x" * 4096, "max-note")
    with pytest.raises(FirmError, match="4096 characters"):
        tools.set_reminder(1200, "x" * 4097, "too-long")


def test_at_most_100_pending_reminders_per_employee(system):
    runtime, first, _ = system
    tools = runtime.tools(first.token)
    for i in range(100):
        tools.set_reminder(1100 + i, f"note {i}", f"schedule-{i}")
    with pytest.raises(FirmError, match="at most 100 pending"):
        tools.set_reminder(1300, "one too many", "schedule-100")


def test_reminder_references_are_short_unique_and_keep_retry_identity(system):
    runtime, first, _ = system
    tools = runtime.tools(first.token)
    one = tools.set_reminder(1100, "Original ${note} {request_id}\n", "one")
    assert one["reminder_id"] == "r1"
    tools.cancel_reminder(one["reminder_id"], "cancel-one")
    two = tools.set_reminder(1200, "Second", "two")
    child = tools.hire("child", "Help", "hire")["employee_id"]
    three = runtime.reminders.set(child, 1300, "Third", "three")
    assert len({one["reminder_id"], two["reminder_id"], three["reminder_id"]}) == 3
    assert tools.set_reminder(1100, "Original ${note} {request_id}\n", "one") == one
    listed = tools.list_reminders()["reminders"]
    assert [r["reminder_id"] for r in listed] == [two["reminder_id"]]
