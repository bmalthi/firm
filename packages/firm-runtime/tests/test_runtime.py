import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.role_output import person, render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.simulation import ControlledExecutor, SimulationDispatcher
from firm_runtime.store import OWNER, Store, encoded


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    result = runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    founder = result["founder"]["employee_id"]
    return runtime, founder, now


def run(runtime):
    invocation = runtime.admit()
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    return invocation


def count(runtime, table):
    with runtime.store.read() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_admission_uses_one_time_for_candidates_and_wake_snapshot(system, monkeypatch):
    runtime, _, now = system
    runtime.start()
    with runtime.store.transaction() as db:
        db.execute("UPDATE execution_requests SET ready_at=1001")
        db.execute("UPDATE message_deliveries SET scheduling_ready_at=NULL")
    original = runtime.admission_candidates

    def cross_ready_time(db, **kwargs):
        now[0] = 1002
        return original(db, **kwargs)

    monkeypatch.setattr(runtime, "admission_candidates", cross_ready_time)
    assert runtime.admit() is None
    invocation = runtime.admit()
    assert invocation is not None
    with runtime.store.read() as db:
        row = db.execute("SELECT created_at,wake_snapshot FROM activations").fetchone()
        assert row["created_at"] == 1002
        assert json.loads(row["wake_snapshot"])


def test_wake_snapshot_shows_last_authors_and_newest_unread(system):
    runtime, founder, _ = system
    runtime.owner.send_mail(OWNER, [founder], "Everyday mail", request_id="everyday")
    newest = runtime.owner.send_mail(
        OWNER, [founder], "Named body", subject="Topic", request_id="named",
    )
    runtime.start()
    invocation = run(runtime)
    snapshot = invocation.context["inbox"]
    assert snapshot["newest_message_id"] == newest["message_id"]
    text = render_tool_result("peek", snapshot)
    assert f"Inbox: 2 threads · 2 unread messages · newest {newest['message_id']}." in text
    assert "Last message: Owner: Everyday mail" in text
    assert text.endswith("Last message: Owner")


def test_handoff_notes_can_exceed_context_reserve_and_survive_fresh_start(system):
    runtime, _, _ = system
    runtime.start()
    invocation = run(runtime)
    notes = "Evidence and next steps.\n" * 3000
    runtime.handoff(invocation.token, notes, "continue", "large-handoff")
    runtime.finish(invocation.activation_id)
    following = runtime.admit()
    assert following.context["handoff_notes"] == notes


def test_full_hire_mail_handoff_drain_restart(system):
    runtime, founder, _ = system
    executor = ControlledExecutor()
    with SimulationDispatcher(runtime, executor) as dispatcher:
        assert dispatcher.tick() == []
        runtime.start()
        (first,) = dispatcher.tick()
        tools = runtime.tools(first.token)
        child = tools.hire("finance", "Own invoices and receivables.", "hire")
        employee = child["employee_id"]
        sent = tools.send([employee], [], "Please prepare the invoice.")
        (analyst,) = dispatcher.tick()
        analyst_tools = runtime.tools(analyst.token)
        analyst_tools.read(sent["message_id"])
        analyst_tools.handoff("Invoice draft saved; next review it.", "continue", "handoff")
        assert analyst.activation_id in executor.running
        assert dispatcher.stop()["state"] == "draining"
        assert dispatcher.tick() == []
        tools.handoff("Finance has the invoice task.", "wait", "founder-handoff")
        executor.complete(first.activation_id)
        executor.complete(analyst.activation_id)
        assert dispatcher.tick() == []
        assert runtime.status()["state"] == "stopped"
        with pytest.raises(FirmError, match="expired"):
            analyst_tools.send([founder], [], "stale")
        runtime.start()
        resumed_runs = dispatcher.tick()
        resumed = next(item for item in resumed_runs if item.employee_id == employee)
        assert resumed.employee_id == employee
        assert resumed.session_id != analyst.session_id
        assert resumed.context["handoff_notes"].startswith("Invoice draft")
        assert not resumed.context["birth"]
        assert resumed.context["mandate"]["body"] == "Own invoices and receivables."
        executor.complete(resumed.activation_id)
        assert dispatcher.tick() == []


@pytest.mark.parametrize("disposition", [None, "peek", "read", "dismiss"])
def test_admission_race_and_arrival_not_lost(system, disposition):
    runtime, founder, _ = system
    runtime.start()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: runtime.admit(), range(2)))
    (first,) = [result for result in results if result]
    runtime.mark_started(first.activation_id)
    sent = runtime.owner.send_mail(OWNER, [founder], "New work during execution", request_id="new")
    if disposition == "peek":
        runtime.messaging.peek(founder)
    elif disposition:
        getattr(runtime.messaging, disposition)(founder, sent["message_id"])
    runtime.finish(first.activation_id)
    if disposition in {"read", "dismiss"}:
        assert runtime.admit() is None
        return
    second = run(runtime)
    assert second.session_id == first.session_id
    assert second.context["mandate"] == first.context["mandate"]
    assert second.context["mandate_changed"] is False
    runtime.finish(second.activation_id)
    assert runtime.admit() is None


def test_pending_backlog_does_not_repeat_wake(system):
    runtime, founder, _ = system
    runtime.owner.send_mail(OWNER, [founder], "Can wait", request_id="mail")
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    assert runtime.admit() is None
    assert count(runtime, "scheduling_opportunities") == 0


def test_hard_stop_revokes_token_preserves_claim_and_recovery(system):
    runtime, founder, _ = system
    mail = runtime.owner.send_mail(OWNER, [founder], "Do a thing", request_id="mail")
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        runtime.start()
        (first,) = dispatcher.tick()
        runtime.tools(first.token).read(mail["message_id"])
        assert dispatcher.stop(force=True)["state"] == "stopped"
        assert dispatcher.executor.running == {}
        with pytest.raises(FirmError):
            runtime.tools(first.token).peek()
        runtime.start()
        (second,) = dispatcher.tick()
        assert second.session_id == first.session_id
        assert second.context["recovery"]["cause"] == "owner_stop"
        assert [c["message_id"] for c in second.context["recovery"]["recent_reads"]] == [
            mail["message_id"]
        ]
        with runtime.store.read() as db:
            assert runtime.store.role(db, founder)["attempt_count"] == 1
        dispatcher.executor.complete(second.activation_id)
        dispatcher.tick()


def test_retry_delay_and_budget_survive_restart_and_mail(system):
    runtime, founder, now = system
    runtime.start()
    for attempt, delay in enumerate((60, 300, 1800), start=1):
        invocation = run(runtime)
        runtime.finish(invocation.activation_id, "failed", failure="temporary")
        with runtime.store.read() as db:
            assert runtime.store.role(db, founder)["attempt_count"] == attempt
        runtime = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
        runtime.owner.send_mail(OWNER, [founder], "Additional work", request_id=f"mail-{attempt}")
        now[0] += delay - 1
        assert runtime.admit() is None
        now[0] += 1
    invocation = run(runtime)
    runtime.finish(invocation.activation_id, "failed")
    assert runtime.admit() is None
    runtime.stop()
    runtime.start()
    assert runtime.admit() is None
    (held,) = runtime.status()["held"]
    assert held["hold_reason"] == "attempts_exhausted"
    result = runtime.retry(OWNER, founder, held["recovery_episode"], "retry")
    assert runtime.retry(OWNER, founder, held["recovery_episode"], "retry") == result
    with pytest.raises(FirmError, match="stale"):
        runtime.retry(OWNER, founder, held["recovery_episode"], "another-retry")
    assert run(runtime)


def test_retry_delays_are_configurable_and_persisted(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
            "retry_delays": [2, 4, 6],
        },
    )
    runtime.start()
    invocation = run(runtime)
    runtime.finish(invocation.activation_id, "failed", failure="temporary")
    now[0] += 1
    assert runtime.admit() is None
    now[0] += 1
    assert runtime.admit() is not None


def test_launch_failure_keeps_birth_and_handoff_inputs(system):
    runtime, founder, now = system
    runtime.start()
    first = runtime.admit()
    runtime.mark_launch_attempted(first.activation_id)
    runtime.finish(first.activation_id, "failed")
    now[0] += 60
    second = run(runtime)
    assert second.context["birth"] and second.context["fresh_session"]
    assert "recovery" not in second.context
    runtime.handoff(second.token, "Saved work", "continue", "handoff")
    runtime.finish(second.activation_id)
    third = runtime.admit()
    runtime.mark_launch_attempted(third.activation_id)
    runtime.finish(third.activation_id, "failed")
    now[0] += 60
    fourth = run(runtime)
    assert fourth.context["handoff_notes"] == "Saved work"


def test_refusal_is_immediate_role_hold_and_retry_requires_manager(system):
    runtime, founder, _ = system
    child = runtime.lifecycle.hire(founder, "analyst", "Do work", "default", request_id="hire")
    runtime.start()
    first = run(runtime)
    second = run(runtime)
    analyst = first if first.employee_id == child["employee_id"] else second
    runtime.finish(analyst.activation_id, "failed", category="refusal")
    with runtime.store.read() as db:
        role = runtime.store.role(db, analyst.employee_id)
    assert role["hold_reason"] == "refusal"
    with pytest.raises(FirmError, match="manager"):
        runtime.retry(analyst.employee_id, analyst.employee_id, role["recovery_episode"], "self")
    runtime.retry(founder, analyst.employee_id, role["recovery_episode"], "manager-retry")


def test_completed_execution_rejects_failure_classification_without_side_effects(system):
    runtime, founder, _ = system
    runtime.start()
    invocation = run(runtime)

    with pytest.raises(FirmError, match="completed.*failure category"):
        runtime.finish(invocation.activation_id, "completed", category="auth")

    with runtime.store.read() as db:
        activation = runtime._activation(db, invocation.activation_id)
        assert activation["state"] == "running"
        assert db.execute("SELECT COUNT(*) FROM execution_incidents").fetchone()[0] == 0
        assert runtime.store.role(db, founder)["hold_reason"] is None


def test_context_revisions_and_no_stale_handoff_after_crash(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    runtime.handoff(first.token, "Old notes", "continue", "handoff")
    runtime.finish(first.activation_id)
    second = run(runtime)
    assert second.context["handoff_notes"] == "Old notes"
    runtime.lifecycle.revise_mandate(OWNER, founder, 0, "New owner mandate", request_id="revision")
    runtime.finish(second.activation_id, "failed")
    now[0] += 60
    third = run(runtime)
    assert "handoff_notes" not in third.context
    assert third.context["mandate"]["body"] == "New owner mandate"
    assert "recovery" in third.context


def test_failed_prelaunch_does_not_consume_pending_mandate_revision(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    runtime.lifecycle.revise_mandate(OWNER, founder, 0, "New owner mandate", request_id="revision")
    admitted = runtime.admit()
    assert admitted.context["mandate_changed"] is True
    assert admitted.context["mandate"]["body"] == "New owner mandate"
    # Execution never launched: finish before mark_started, as a real dispatcher would
    # after a setup failure.
    runtime.finish(admitted.activation_id, "interrupted")
    with runtime.store.read() as db:
        role = runtime.store.role(db, founder)
    assert role["session_mandate_revision"] == 0
    later = run(runtime)
    assert later.context["mandate_changed"] is True
    assert later.context["mandate"]["body"] == "New owner mandate"


def test_multiple_wake_reasons_combine_into_one_activation(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "Owner note", request_id="owner-mail")
    with runtime.store.transaction() as db:
        runtime.store.request_execution(db, founder, "interrupted_work")
    invocation = runtime.admit()
    assert invocation is not None
    with runtime.store.read() as db:
        reasons = {
            row["reason"]
            for row in json.loads(
                db.execute(
                    "SELECT wake_snapshot FROM activations WHERE activation_id=?",
                    (invocation.activation_id,),
                ).fetchone()[0]
            )
        }
    assert reasons == {"message", "interrupted_work"}
    # The combined reasons produced one activation; no second one is left outstanding.
    assert runtime.admit() is None


def test_auth_and_mutation_rollback_share_transaction(system):
    runtime, founder, _ = system
    runtime.start()
    invocation = run(runtime)
    before = count(runtime, "messages")
    with pytest.raises(RuntimeError), runtime.authorized(invocation.token):
        runtime.messaging.send(founder, [founder], [], "Rolled back", request_id="rollback")
        raise RuntimeError("after nested service write")
    assert count(runtime, "messages") == before
    assert count(runtime, "requests") == 0


def test_only_one_dispatcher(system):
    runtime, _, _ = system
    with (
        SimulationDispatcher(runtime, ControlledExecutor()),
        pytest.raises(FirmError, match="another"),
    ):
        with SimulationDispatcher(runtime, ControlledExecutor()):
            pass


def test_recovery_reads_latest_five_across_activations_without_bodies(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    ids = []
    for index in range(7):
        mail = runtime.owner.send_mail(
            OWNER, [founder], f"Secret body {index}", request_id=f"send-{index}",
            subject=f"Message subject number {index}",
        )
        ids.append(mail["message_id"])
        runtime.tools(first.token).read(ids[-1])
    runtime.finish(first.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "New opportunity", request_id="next")
    second = run(runtime)
    runtime.finish(second.activation_id, "interrupted", category="owner_stop")
    third = run(runtime)
    recovery = third.context["recovery"]
    assert [c["message_id"] for c in recovery["recent_reads"]] == list(reversed(ids[-5:]))
    assert "Secret body" not in json.dumps(recovery)


def test_host_restart_reconciles_reservation_and_preserves_delay(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    reopened = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
    with SimulationDispatcher(reopened, ControlledExecutor()) as dispatcher:
        assert reopened.status()["active"] == []
        assert dispatcher.tick() == []
        with pytest.raises(FirmError, match="expired"):
            reopened.authenticate(first.token)
        now[0] += 60
        (recovered,) = dispatcher.tick()
        assert recovered.employee_id == founder
        assert recovered.context["recovery"]["cause"] == "host_restart"
        assert recovered.context["recovery"]["time_is_detection"]
        assert recovered.session_id == first.session_id


def test_wake_priority_is_outstanding_opportunity_not_old_mail(system):
    runtime, founder, now = system
    employees = [
        runtime.lifecycle.hire(founder, slug, "Do work", "default", request_id=slug)["employee_id"]
        for slug in ("ordinary", "owner", "control")
    ]
    runtime.start()
    for _ in range(4):
        born = run(runtime)
        runtime.finish(born.activation_id)
    runtime.messaging.send(
        founder, [employees[0]], [], "Old ordinary mail", request_id="ordinary-mail",
    )
    now[0] += 1
    runtime.owner.send_mail(OWNER, [employees[1]], "Owner work", request_id="owner")
    now[0] += 1
    with runtime.store.transaction() as db:
        runtime.messaging.notice(
            db,
            [employees[2]],
            "Manager action required",
            subject="Control work for manager",
            priority=2,
        )
    ordered = []
    for _ in range(3):
        invocation = run(runtime)
        ordered.append(invocation.employee_id)
        runtime.finish(invocation.activation_id)
    assert ordered == list(reversed(employees))
    assert runtime.admit() is None
    runtime.messaging.send(founder, [employees[0]], [], "Earlier opportunity", request_id="earlier")
    now[0] += 1
    runtime.messaging.send(founder, [employees[2]], [], "Later opportunity", request_id="later")
    assert run(runtime).employee_id == employees[0]


@pytest.mark.parametrize("same_time", [False, True])
def test_wake_age_is_scoped_to_highest_ready_priority(system, same_time):
    runtime, founder, now = system
    child = runtime.lifecycle.hire(founder, "analyst", "Work", "default", request_id="hire")[
        "employee_id"
    ]
    runtime.start()
    for _ in range(2):
        born = run(runtime)
        runtime.finish(born.activation_id)
    now[0] = 1040
    a, b = sorted([founder, child], reverse=True)
    with runtime.store.transaction() as db:
        for recipient, priority, ready_at in [
            (a, 0, 1001),
            (a, 1, 1020 if same_time else 1030),
            (b, 1, 1020),
            (a, 2, 1050),
        ]:
            runtime.messaging.notice(
                db,
                [recipient],
                "Work",
                subject="Scheduling priority test",
                priority=priority,
                ready_at=ready_at,
            )
    first = run(runtime)
    assert first.employee_id == b
    runtime.finish(first.activation_id)
    second = run(runtime)
    assert second.employee_id == a
    runtime.finish(second.activation_id)
    assert runtime.admit() is None
    now[0] = 1050
    assert run(runtime).employee_id == a


def test_longer_provider_delay_wins_and_interrupted_retiree_has_no_wake(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id, "failed", retry_after=now[0] + 600)
    now[0] += 60
    assert runtime.admit() is None
    now[0] += 540
    resumed = run(runtime)
    analyst = runtime.tools(resumed.token).hire("analyst", "Do work", "hire")
    child = run(runtime)
    runtime.tools(resumed.token).fire(analyst["employee_id"], "No longer needed", "fire")
    runtime.finish(child.activation_id, "interrupted", category="owner_stop")
    with runtime.store.read() as db:
        assert not db.execute(
            "SELECT 1 FROM scheduling_opportunities WHERE employee_id=?", (analyst["employee_id"],)
        ).fetchone()


def test_restart_before_launch_does_not_spend_attempt_budget(system):
    runtime, founder, now = system
    runtime.start()
    reserved = runtime.admit()
    with runtime.store.read() as db:
        assert runtime.store.role(db, founder)["attempt_count"] == 0
    reopened = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
    with SimulationDispatcher(reopened, ControlledExecutor()) as dispatcher:
        (launched,) = dispatcher.tick()
        assert launched.activation_id != reserved.activation_id
        assert launched.context["birth"]
        assert "recovery" not in launched.context
        with reopened.store.read() as db:
            assert reopened.store.role(db, founder)["attempt_count"] == 1


def test_drain_is_scoped_to_activation_and_shown_on_next_tool_response(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    before = count(runtime, "messages")
    runtime.stop()
    runtime.stop()
    (active,) = runtime.status()["active"]
    assert active["drain_requested_at"] is not None
    assert active["drain_presented_at"] is None
    assert active["next_intent"] is None
    response = runtime.tools(first.token).get_role(role=founder)
    assert response["operations_control"]["activation_id"] == first.activation_id
    (active,) = runtime.status()["active"]
    assert active["drain_presented_at"] is not None
    assert active["next_intent"] is None
    assert count(runtime, "messages") == before
    runtime.tools(first.token).handoff("Continue later", "continue", "handoff")
    runtime.finish(first.activation_id)
    runtime.start()
    second = run(runtime)
    assert "operations_control" not in runtime.tools(second.token).peek()
    assert second.context["inbox"]["unread_count"] == 0


def test_host_crash_during_drain_is_not_an_owner_hard_stop(system):
    runtime, founder, now = system
    runtime.start()
    run(runtime)
    runtime.stop()
    reopened = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
    with SimulationDispatcher(reopened, ControlledExecutor()) as dispatcher:
        assert reopened.status()["state"] == "stopped"
        with reopened.store.read() as db:
            role = reopened.store.role(db, founder)
        assert role["attempt_count"] == 1
        assert json.loads(role["recovery_json"])["cause"] == "host_restart"
        reopened.start()
        assert dispatcher.tick() == []
        now[0] += 60
        (resumed,) = dispatcher.tick()
        assert resumed.employee_id == founder


@pytest.mark.parametrize("outcome", ["completed", "interrupted", "continue"])
def test_claimed_arrival_does_not_replace_recovery_or_continuation(system, outcome):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    sent = runtime.owner.send_mail(OWNER, [founder], "New work", request_id="new")
    runtime.tools(first.token).read(sent["message_id"])
    assert count(runtime, "scheduling_opportunities") == 0
    if outcome == "continue":
        runtime.tools(first.token).handoff("Continue saved work", "continue", "handoff")
    runtime.finish(
        first.activation_id,
        "interrupted" if outcome == "interrupted" else "completed",
        category="owner_stop" if outcome == "interrupted" else "unknown",
    )
    if outcome == "completed":
        assert runtime.admit() is None
    else:
        second = run(runtime)
        assert (second.session_id == first.session_id) == (outcome == "interrupted")
        if outcome == "interrupted":
            assert (
                second.context["recovery"]["recent_reads"][0]["message_id"] == sent["message_id"]
            )
        else:
            assert second.context["handoff_notes"] == "Continue saved work"


def test_message_priority_is_projected_and_drops_after_claim(system):
    runtime, founder, now = system
    child = runtime.lifecycle.hire(founder, "analyst", "Work", "default", request_id="hire")[
        "employee_id"
    ]
    runtime.start()
    for _ in range(2):
        born = run(runtime)
        runtime.finish(born.activation_id)
    runtime.messaging.send(child, [founder], [], "Older ordinary work", request_id="older")
    now[0] += 1
    runtime.owner.send_mail(OWNER, [child], "Owner work", request_id="child-owner")
    with runtime.store.transaction() as db:
        urgent = runtime.messaging.notice(
            db, [founder], "Control work", subject="Control work for founder", priority=2
        )
    assert runtime.status()["waiting"] == 2
    runtime.messaging.read(founder, urgent["message_id"])
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_requests").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT MAX(priority) FROM scheduling_opportunities WHERE employee_id=?", (founder,)
            ).fetchone()[0]
            == 0
        )
    first = run(runtime)
    assert first.employee_id == child
    assert runtime.status()["waiting"] == 1
    runtime.finish(first.activation_id)
    second = run(runtime)
    assert second.employee_id == founder
    runtime.finish(second.activation_id)
    assert runtime.admit() is None
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM message_deliveries "
                "WHERE employee_id=? AND disposition='pending'",
                (founder,),
            ).fetchone()[0]
            == 1
        )


def pending(runtime, employee_id):
    with runtime.store.read() as db:
        return db.execute(
            "SELECT COUNT(*) FROM message_deliveries WHERE employee_id=? AND disposition='pending'",
            (employee_id,),
        ).fetchone()[0]


def drain_births(runtime):
    while True:
        invocation = runtime.admit()
        if invocation is None:
            return
        runtime.mark_started(invocation.activation_id)
        runtime.finish(invocation.activation_id)


def labelled(result):
    return [(person(sender), count) for sender, count in result["mail_waiting"]["senders"]]


@pytest.mark.parametrize("wake", [True, False])
def test_mail_arriving_during_work_is_shown_once_and_leaves_it_pending(system, wake):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    tools = runtime.tools(first.token)
    assert "mail_waiting" not in tools.get_role(role=founder)
    runtime.owner.send_mail(
        OWNER, [founder], "Quick question", action_recipients=None if wake else [],
        request_id="q1",
    )
    shown = tools.get_role(role=founder)
    assert labelled(shown) == [("Owner", 1)]
    rendered = render_tool_result("get_role", shown)
    assert rendered.endswith("\n\nNew mail: 1 from Owner.")
    assert "mail_waiting" not in tools.get_role(role=founder)
    assert pending(runtime, founder) == 1


@pytest.mark.parametrize("held", [False, True])
def test_quiet_mail_receives_one_idle_review_after_hold_clears(system, held):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    if held:
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE roles SET hold_reason='attempts_exhausted' WHERE employee_id=?", (founder,)
            )
    quiet = runtime.owner.send_mail(
        OWNER, [founder], "Reference for later", action_recipients=[], request_id="later",
    )
    if held:
        assert runtime.admit() is None
        with runtime.store.transaction() as db:
            db.execute("UPDATE roles SET hold_reason=NULL WHERE employee_id=?", (founder,))
    second = run(runtime)
    assert second.context["idle_inbox_review"]
    assert second.context["inbox"]["threads"][0]["message_id"] == quiet["message_id"]
    runtime.finish(second.activation_id)
    assert runtime.admit() is None
    assert pending(runtime, founder) == 1
    runtime.owner.send_mail(OWNER, [founder], "New assignment", request_id="assignment")
    third = run(runtime)
    claimed = runtime.tools(third.token).read(quiet["message_id"])
    assert claimed["messages"][0]["body"] == "Reference for later"


def test_peek_is_the_observation_and_renders_no_notice(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    tools = runtime.tools(first.token)
    runtime.owner.send_mail(OWNER, [founder], "Quick question", request_id="q1")
    peeked = tools.peek()
    assert "mail_waiting" not in peeked
    assert "New mail" not in render_tool_result("peek", peeked)
    assert "mail_waiting" not in tools.get_role(role=founder)
    assert pending(runtime, founder) == 1




def test_mail_notice_orders_owner_operations_manager_then_oldest(system):
    runtime, founder, _ = system
    child = runtime.lifecycle.hire(founder, "analyst", "Work", "default", request_id="hire")[
        "employee_id"
    ]
    peers = [
        runtime.lifecycle.hire(founder, slug, "Work", "default", request_id=slug)["employee_id"]
        for slug in ("early", "late")
    ]
    runtime.start()
    drain_births(runtime)
    runtime.messaging.send(founder, [child], [], "Kickoff", request_id="kickoff")
    activation = run(runtime)
    assert activation.employee_id == child
    runtime.messaging.send(peers[0], [child], [], "Peer note", request_id="peer-early")
    runtime.messaging.send(peers[1], [child], [], "Peer note", request_id="peer-late")
    runtime.messaging.send(founder, [child], [], "Manager note", request_id="manager")
    with runtime.store.transaction() as db:
        runtime.messaging.notice(db, [child], "Operations note", subject="Operations")
    runtime.owner.send_mail(OWNER, [child], "Owner note", request_id="owner")
    runtime.owner.send_mail(OWNER, [child], "Owner again", request_id="owner-again")
    before = pending(runtime, child)
    shown = runtime.tools(activation.token).get_role(role=child)
    # The kickoff mail came with the wake update, so it is not a new arrival.
    assert labelled(shown) == [
        ("Owner", 2),
        ("Operations", 1),
        (person({"employee_id": founder, "address": "founder"}), 1),
        (person({"employee_id": peers[0], "address": "founder/early"}), 1),
        (person({"employee_id": peers[1], "address": "founder/late"}), 1),
    ]
    assert render_tool_result("get_role", shown).endswith(
        "\n\nNew mail: 2 from Owner, 1 from Operations, 1 from founder, "
        f"1 from founder/early ({peers[0]}), 1 from founder/late ({peers[1]})."
    )
    assert pending(runtime, child) == before


def test_handoff_cleanup_intent_replays_only_the_applied_request(system):
    runtime, _, _ = system
    runtime.start()
    actor = run(runtime)
    tools = runtime.tools(actor.token)
    saved = tools.handoff("Done", "continue", "first", clean_up=True)
    assert tools.handoff("Done", "continue", "first", clean_up=True) == saved
    with pytest.raises(FirmError, match="request_id"):
        tools.handoff("Done", "continue", "first", clean_up=False)
    with pytest.raises(FirmError, match="already has a handoff"):
        tools.handoff("Other", "wait", "second", clean_up=False)
    with runtime.store.read() as db:
        rows = db.execute("SELECT * FROM requests WHERE operation='handoff'").fetchall()
        assert len(rows) == 1 and rows[0]["activation_id"] == actor.activation_id
        assert json.loads(rows[0]["payload_json"]) == {
            "notes": "Done", "next": "continue", "clean_up": True}
        activation = runtime._activation(db, actor.activation_id)
        assert activation["handoff_notes"] == "Done" and activation["next_intent"] == "continue"
    # Non-sandbox completion ignores the flag and performs normal handoff.
    runtime.finish(actor.activation_id)
    assert run(runtime).context["handoff_notes"] == "Done"


@pytest.mark.parametrize("value", [None, 1, "true", []])
def test_handoff_cleanup_requires_a_boolean(system, value):
    runtime, _, _ = system
    runtime.start()
    actor = run(runtime)
    with pytest.raises(FirmError, match="clean_up must be a boolean"):
        runtime.tools(actor.token).handoff("Done", "wait", "bad", clean_up=value)


def test_handoff_default_cleanup_is_saved_and_cannot_be_replaced(system):
    runtime, _, _ = system
    runtime.start()
    actor = run(runtime)
    tools = runtime.tools(actor.token)
    saved = tools.handoff("Keep work", "wait", "first")
    assert tools.handoff("Keep work", "wait", "first", clean_up=False) == saved
    with pytest.raises(FirmError, match="already has a handoff"):
        tools.handoff("Delete work", "wait", "second", clean_up=True)
    with runtime.store.read() as db:
        request = db.execute(
            "SELECT payload_json FROM requests WHERE operation='handoff'"
        ).fetchone()
        assert json.loads(request[0])["clean_up"] is False


def guidance_role(runtime, employee="founder"):
    with runtime.store.read() as db:
        return runtime.store.role(db, employee)


def request_guidance_wake(runtime, employee="founder"):
    with runtime.store.transaction() as db:
        runtime.store.request_execution(db, employee, "owner_fresh_route")


def test_guidance_failed_prelaunch_and_admitted_selection_delivery(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    old = guidance_role(runtime)["session_guidance"]
    runtime.owner.set_guidance(OWNER, founder, None, ["legal"], "guides")
    request_guidance_wake(runtime)
    failed = runtime.admit()
    assert failed.context["guidance_changed"] is True
    runtime.finish(failed.activation_id, "interrupted")
    assert guidance_role(runtime)["session_guidance"] == old
    request_guidance_wake(runtime)
    admitted = runtime.admit()
    assert admitted.context["guidance_changed"] is True
    runtime.owner.set_guidance(OWNER, founder, None, ["sales"], "different")
    runtime.mark_started(admitted.activation_id)
    assert guidance_role(runtime)["session_guidance"] == admitted.context["guidance"]
    assert json.loads(admitted.context["guidance"])["functions"] == ["legal"]
    runtime.finish(admitted.activation_id)
    request_guidance_wake(runtime)
    assert runtime.admit().context["guidance_changed"] is True


def test_org_chart_changes_notify_retained_manager_without_reselection(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    lead = runtime.lifecycle.hire(founder, "lead", "Lead work", request_id="lead")["employee_id"]
    runtime.finish(first.activation_id)
    initial = run(runtime)
    assert initial.employee_id == lead
    assert json.loads(initial.context["guidance"])["scope"] is None
    session = initial.session_id
    child = runtime.lifecycle.hire(lead, "child", "Work", request_id="child")["employee_id"]
    runtime.owner.stop_role(OWNER, child, "pause")
    runtime.finish(initial.activation_id)
    request_guidance_wake(runtime, lead)
    manager = run(runtime)
    assert manager.session_id == session
    assert manager.context["guidance_changed"] is True
    assert json.loads(manager.context["guidance"])["scope"] == "team-leader"
    runtime.finish(manager.activation_id)
    runtime.lifecycle.fire(lead, child, reason="Done", request_id="fire")
    request_guidance_wake(runtime, lead)
    contributor = run(runtime)
    assert contributor.session_id == session
    assert contributor.context["guidance_changed"] is True
    assert json.loads(contributor.context["guidance"])["scope"] is None


@pytest.mark.parametrize("selector", [False, True])
@pytest.mark.parametrize("born", [False, True])
@pytest.mark.parametrize("staffing", [False, True])
def test_guidance_initial_wait_is_only_for_unborn_roles_with_staffing_and_selector(
    system, selector, born, staffing
):
    from firm_runtime import guidance

    runtime, founder, _ = system
    if staffing:
        with runtime.store.transaction() as db:
            config = runtime.accounts.config(db)
            config["staffing"] = {"classes": {"medium": "default"}}
            db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
            guidance.register(db, founder)
            db.execute("UPDATE roles SET birth_delivered=?", (born,))
    runtime.levels.chooser = (lambda _: {}) if selector else None
    runtime.start()
    admitted = runtime.admit()
    waits = staffing and selector and not born
    assert (admitted is None) == waits
    if waits:
        with runtime.store.transaction() as db:
            db.execute("UPDATE roles SET guidance_state='defaulted'")
        assert runtime.admit() is not None


@pytest.mark.parametrize(
    "event", ["mail", "handoff", "recovery", "renewal", "promotion", "route", "stop-resume"]
)
def test_ordinary_events_reuse_guidance_without_an_assessment(tmp_path, event):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build", {
        "max_active": 4, "profiles": {"main": {"provider": "codex", "model": "test"}},
    })
    calls = []
    runtime.levels.chooser = lambda p: calls.append(p) or {
        "model_class": "low", "effort": "low", "scope_guidance": None,
        "functional_guidance": ["legal"],
    }
    runtime.start()
    runtime.levels.tick(background=False)
    founder = run(runtime)
    employee = runtime.lifecycle.hire("founder", "worker", "Work", request_id="hire",
                                      temporary_periods=2)["employee_id"]
    runtime.levels.tick(background=False)
    current = run(runtime)
    assert current.employee_id == employee
    before = guidance_role(runtime, employee)
    if event == "mail":
        runtime.owner.send_mail(OWNER, [employee], "Work", request_id="mail")
        runtime.finish(current.activation_id)
    elif event == "handoff":
        runtime.handoff(current.token, "Progress", "continue", "handoff")
        runtime.finish(current.activation_id)
    elif event == "recovery":
        runtime.finish(current.activation_id, "interrupted")
    elif event == "renewal":
        runtime.appointments.renew("founder", employee, 2, "renew")
    elif event == "promotion":
        runtime.appointments.promote("founder", employee, "promote")
    elif event == "route":
        runtime.finish(current.activation_id)
        runtime.owner.route(OWNER, employee, before["execution_profile"], "route")
    else:
        runtime.owner.stop_role(OWNER, employee, "stop")
        runtime.finish(current.activation_id, "interrupted")
        runtime.owner.resume_role(OWNER, employee, "resume")
    assert runtime.levels.tick(background=False) == []
    assert len(calls) == 2
    after = guidance_role(runtime, employee)
    assert {k: v for k, v in before.items() if k.startswith("guidance_") or k in (
        "scope_guidance", "functional_guidance"
    )} == {k: v for k, v in after.items() if k.startswith("guidance_") or k in (
        "scope_guidance", "functional_guidance"
    )}
    assert founder.employee_id == "founder"


def test_mandate_revision_with_unchanged_guidance_has_no_guidance_notice(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    runtime.lifecycle.revise_mandate(OWNER, founder, 0, "New work", request_id="rev")
    next_run = runtime.admit()
    assert next_run.context["mandate_changed"] is True
    assert next_run.context["guidance_changed"] is False


def test_hire_birth_waits_for_guidance_after_its_level_succeeds(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build", {
        "max_active": 4, "profiles": {"main": {"provider": "codex", "model": "test"}},
    })
    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder")
    employee = runtime.lifecycle.hire("founder", "worker", "Work", request_id="hire")["employee_id"]
    runtime.levels.chooser = lambda _: {"model_class": "low", "effort": "low"}
    runtime.start()
    runtime.levels.tick(background=False)
    first = run(runtime)
    assert first.employee_id == "founder"
    assert runtime.admit() is None
    with runtime.store.transaction() as db:
        assert runtime.levels.record(db, employee)["state"] == "chosen"
        db.execute("UPDATE roles SET guidance_state='defaulted' WHERE employee_id=?", (employee,))
    assert runtime.admit().employee_id == employee


def snapshot_ids(runtime, result):
    with runtime.store.read() as db:
        return runtime.messaging.offered_ids(db, "founder", result)


def frontier(runtime, employee):
    with runtime.store.read() as db:
        return runtime.store.role(db, employee)["last_offered_message_sequence"]


def test_idle_review_waits_for_global_idle_and_does_not_repeat_pending_mail(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    quiet = runtime.owner.send_mail(
        OWNER, [founder], "Useful FYI", action_recipients=[], request_id="quiet",
    )
    assert runtime.admit() is None
    assert "mail_waiting" in runtime.tools(first.token).get_role(role=founder)
    assert frontier(runtime, founder) == 0
    runtime.finish(first.activation_id)
    review = run(runtime)
    assert review.context["idle_inbox_review"]
    assert snapshot_ids(runtime, review.context["inbox"]) == [quiet["message_id"]]
    assert frontier(runtime, founder) > 0
    assert "mail_waiting" not in runtime.tools(review.token).get_role(role=founder)
    runtime.owner.send_mail(
        OWNER, [founder], "During review", action_recipients=[], request_id="during-review",
    )
    assert labelled(runtime.tools(review.token).get_role(role=founder)) == [("Owner", 1)]
    runtime.tools(review.token).peek()
    runtime.finish(review.activation_id)
    assert runtime.admit() is None
    runtime.owner.send_mail(OWNER, [founder], "New FYI", action_recipients=[], request_id="new")
    following = run(runtime)
    assert following.context["idle_inbox_review"]
    assert snapshot_ids(runtime, following.context["inbox"]) != [quiet["message_id"]]


def test_idle_review_prefix_exhausts_unchanged_backlog_and_future_hole(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    sent = [
        runtime.owner.send_mail(
            OWNER, [founder], f"FYI {i}", action_recipients=[], request_id=f"quiet-{i}",
        )["message_id"]
        for i in range(13)
    ]
    offers = []
    while review := runtime.admit():
        runtime.mark_started(review.activation_id)
        offers.extend(snapshot_ids(runtime, review.context["inbox"]))
        runtime.finish(review.activation_id)
    assert offers == sent
    assert pending(runtime, founder) == 13
    with runtime.store.transaction() as db:
        future = runtime.messaging.notice(
            db, [founder], "Future", subject="Future", ready_at=now[0] + 20
        )
    later = runtime.owner.send_mail(
        OWNER, [founder], "Behind future", action_recipients=[], request_id="behind",
    )
    assert runtime.admit() is None
    now[0] += 20
    normal = run(runtime)
    assert not normal.context.get("idle_inbox_review")
    # A bounded ordinary page leaves a hole, then prefix reviews reach both newer messages.
    runtime.finish(normal.activation_id)
    offered = snapshot_ids(runtime, normal.context["inbox"])
    while review := runtime.admit():
        runtime.mark_started(review.activation_id)
        offered.extend(snapshot_ids(runtime, review.context["inbox"]))
        runtime.finish(review.activation_id)
    assert future["message_id"] in offered and later["message_id"] in offered



def test_idle_frontier_rollback_and_prestart_restore(system, monkeypatch):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "FYI", action_recipients=[], request_id="quiet")
    original = runtime.messaging.advance_offered

    def fail_after_offer(*args):
        original(*args)
        raise RuntimeError("rollback")

    monkeypatch.setattr(runtime.messaging, "advance_offered", fail_after_offer)
    with pytest.raises(RuntimeError, match="rollback"):
        runtime.admit()
    assert frontier(runtime, founder) == 0
    monkeypatch.setattr(runtime.messaging, "advance_offered", original)
    admitted = runtime.admit()
    assert frontier(runtime, founder) > 0
    runtime.finish(admitted.activation_id, "interrupted", category="owner_stop")
    assert frontier(runtime, founder) == 0
    # Existing recovery may be ordinary work; either way its committed offer is restored
    # only until mark_started confirms the work period began.
    retry = run(runtime)
    offered = frontier(runtime, founder)
    assert offered > 0
    runtime.finish(retry.activation_id, "interrupted", category="owner_stop")
    assert frontier(runtime, founder) == offered


def test_two_idle_admits_serialize_and_normal_work_can_run_during_review(system):
    runtime, founder, _ = system
    peer = runtime.lifecycle.hire(founder, "peer", "Own peer work.", "default", request_id="peer")[
        "employee_id"
    ]
    runtime.start()
    while invocation := runtime.admit():
        runtime.mark_started(invocation.activation_id)
        runtime.finish(invocation.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "FYI", action_recipients=[], request_id="quiet")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.admit(), range(2)))
    (review,) = [item for item in results if item is not None]
    assert review.context["idle_inbox_review"]
    runtime.owner.send_mail(OWNER, [peer], "Act now", request_id="ordinary")
    normal = runtime.admit()
    assert normal.employee_id == peer
    assert not normal.context.get("idle_inbox_review")


def test_explicit_future_read_removes_due_wake(system):
    runtime, founder, now = system
    runtime.start()
    first = run(runtime)
    with runtime.store.transaction() as db:
        future = runtime.messaging.notice(
            db, [founder], "Future", subject="Future", ready_at=now[0] + 20
        )
    runtime.tools(first.token).read(future["message_id"])
    runtime.finish(first.activation_id)
    assert runtime.admit() is None
    now[0] += 20
    assert runtime.admit() is None


@pytest.mark.parametrize("budget", [1000, 1])
def test_idle_first_header_budget_progress_or_atomic_refusal(system, monkeypatch, budget):
    from firm_runtime import messaging

    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET identity=? WHERE employee_id=?", ("界" * 250, founder))
    runtime.messaging.send(founder, [], [founder], "界" * 80, request_id="unicode")
    monkeypatch.setattr(messaging, "MAX_PEEK_BYTES", budget)
    if budget == 1:
        with pytest.raises(FirmError, match="exceeds"):
            runtime.admit()
        assert frontier(runtime, founder) == 0
    else:
        review = runtime.admit()
        assert review is not None
        assert snapshot_ids(runtime, review.context["inbox"])
        assert len(json.dumps(review.context["inbox"], ensure_ascii=False).encode()) <= budget
        assert frontier(runtime, founder) > 0


def test_sequence_allocation_and_offers_share_serialized_write_transaction(system):
    from threading import Event

    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    started, committed = Event(), Event()
    other = Runtime(Store.scratch(runtime.store.path, clock=runtime.store.clock))

    def competing_lookup():
        started.set()
        result = other.messaging.peek(founder, activation_id=first.activation_id)
        committed.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.store.transaction() as db:
            pending_mail = runtime.messaging.notice(db, [founder], "Uncommitted", subject="Serial",
                                                   wake=False)
            future = pool.submit(competing_lookup)
            assert started.wait(2)
            assert not committed.wait(0.05)
            assert runtime.store.role(db, founder)["last_offered_message_sequence"] == 0
        result = future.result(timeout=5)
    assert pending_mail["message_id"] in snapshot_ids(runtime, result)
    assert frontier(runtime, founder) > 0


def test_idle_review_normal_gate_reuse_and_blocked_work_does_not_starve_peer(system):
    runtime, founder, now = system
    peer = runtime.lifecycle.hire(founder, "peer", "Own peer work.", "default",
                                  request_id="peer")["employee_id"]
    runtime.start()
    while invocation := runtime.admit():
        runtime.mark_started(invocation.activation_id)
        runtime.finish(invocation.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "Ordinary but held", request_id="ordinary")
    runtime.owner.send_mail(OWNER, [peer], "Review later", action_recipients=[], request_id="quiet")
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET hold_reason='attempts_exhausted' WHERE employee_id=?",
                   (founder,))
        db.execute("UPDATE roles SET retry_at=? WHERE employee_id=?", (now[0] + 20, peer))
    assert runtime.admit() is None
    now[0] += 20
    review = run(runtime)
    assert review.employee_id == peer and review.context["idle_inbox_review"]


def test_failed_idle_account_candidate_can_create_normal_work_for_another_role(system, monkeypatch):
    runtime, founder, _ = system
    peer = runtime.lifecycle.hire(founder, "peer", "Own peer work.", "default",
                                  request_id="peer")["employee_id"]
    runtime.start()
    while invocation := runtime.admit():
        runtime.mark_started(invocation.activation_id)
        runtime.finish(invocation.activation_id)
    runtime.owner.send_mail(OWNER, [peer], "FYI", action_recipients=[], request_id="quiet")
    monkeypatch.setattr(runtime.accounts, "enabled", lambda db: True)
    selected = object()

    def select_account(db, role):
        if role["employee_id"] == peer:
            runtime.messaging.notice(db, [founder], "Repair peer", subject="Repair")
            return None
        return selected

    monkeypatch.setattr(runtime.accounts, "select", select_account)
    monkeypatch.setattr(runtime.accounts, "bind", lambda *args: None)
    admitted = runtime.admit()
    assert admitted.employee_id == founder
    assert not admitted.context.get("idle_inbox_review")


def test_chained_account_notices_take_normal_priority_before_idle_commit(system, monkeypatch):
    runtime, founder, _ = system
    manager = runtime.lifecycle.hire(founder, "manager", "Manage.", "default",
                                     request_id="manager")["employee_id"]
    leaf = runtime.lifecycle.hire(manager, "leaf", "Work.", "default",
                                  request_id="leaf")["employee_id"]
    quiet = runtime.lifecycle.hire(founder, "quiet", "Work.", "default",
                                   request_id="quiet")["employee_id"]
    runtime.start()
    while invocation := runtime.admit():
        runtime.mark_started(invocation.activation_id)
        runtime.finish(invocation.activation_id)
    for employee in (leaf, quiet):
        runtime.owner.send_mail(OWNER, [employee], "FYI", action_recipients=[], request_id=employee)
    monkeypatch.setattr(runtime.accounts, "enabled", lambda db: True)
    notified = set()

    def select(db, role):
        employee = role["employee_id"]
        if employee in {leaf, manager}:
            if employee not in notified:
                notified.add(employee)
                runtime.messaging.notice(db, [role["manager_id"]], "Repair", subject="Repair")
            return None
        return object()

    monkeypatch.setattr(runtime.accounts, "select", select)
    monkeypatch.setattr(runtime.accounts, "bind", lambda *args: None)
    invocation = runtime.admit()
    assert invocation.employee_id == founder
    assert not invocation.context.get("idle_inbox_review")


@pytest.mark.parametrize("operation", ["send", "reply", "introduce"])
def test_role_mail_retry_key_is_exact_and_scoped_to_work_period(system, operation, monkeypatch):
    runtime, founder, _ = system
    peer = runtime.lifecycle.hire(founder, "peer", "Help", "default",
                                  request_id="peer")["employee_id"]
    source = runtime.owner.send_mail(OWNER, [founder], "Source", subject="Topic",
                                     request_id="source")
    runtime.start()
    first = run(runtime)
    if first.employee_id != founder:
        runtime.finish(first.activation_id)
        first = run(runtime)
    assert first.employee_id == founder
    summaries = []

    def summarize(snapshot, session_id):
        summaries.append(snapshot.source_message_ids)
        return {"status": "summarized", "summary": "Derived context."}

    monkeypatch.setattr(runtime.messaging, "_summarize", summarize)

    def call(tools, body):
        if operation == "send":
            return tools.send([], [OWNER], body, subject="Topic")
        if operation == "reply":
            return tools.reply(source["message_id"], [], body)
        return tools.introduce(source["message_id"], [peer], [], body, summarize=True)

    tools = runtime.tools(first.token)
    sent = call(tools, "Body")
    replay = call(tools, "Body")
    assert replay["message_id"] == sent["message_id"]
    assert replay["replayed"]
    assert render_tool_result(operation, replay).startswith(
        f"Already sent as {sent['message_id']} in this work period; not sent again."
    )
    changed = call(tools, "Changed body")
    assert changed["message_id"] != sent["message_id"]
    if operation == "introduce":
        assert len(summaries) == 2
    runtime.finish(first.activation_id)
    runtime.owner.send_mail(OWNER, [founder], "Continue", request_id="continue")
    following = run(runtime)
    assert following.employee_id == founder
    again = call(runtime.tools(following.token), "Body")
    assert again["message_id"] not in {sent["message_id"], changed["message_id"]}
    if operation == "introduce":
        assert len(summaries) == 3


def test_peek_offered_frontier_stops_at_an_omitted_older_thread(system, monkeypatch):
    from firm_runtime import messaging

    runtime, founder, _ = system
    peer = runtime.lifecycle.hire(founder, "peer", "Help", "default",
                                  request_id="peer")["employee_id"]
    runtime.start()
    drain_births(runtime)
    older = runtime.messaging.send(peer, [], [founder], "Older", subject="Ordinary",
                                   request_id="older")
    newer = runtime.owner.send_mail(OWNER, [founder], "Newer", subject="Owner",
                                    action_recipients=[], request_id="newer")
    with runtime.store.read() as db:
        rows = runtime.messaging._pending_rows(db, founder)
        full = runtime.messaging._overview(rows, founder)
    monkeypatch.setattr(messaging, "MAX_PEEK_BYTES", len(json.dumps(full).encode()) + 200)
    result = runtime.messaging.peek(founder)
    assert [t["message_id"] for t in result["threads"]] == [newer["message_id"]]
    assert frontier(runtime, founder) == 0
    runtime.messaging.dismiss(founder, older["message_id"])
    runtime.messaging.peek(founder)
    assert frontier(runtime, founder) > 0


@pytest.mark.parametrize("operation", ["read", "introduce"])
def test_summary_cannot_write_after_its_work_period_ends(system, monkeypatch, operation):
    runtime, founder, _ = system
    source = runtime.owner.send_mail(OWNER, [founder], "Source", request_id="source")
    runtime.start()
    first = run(runtime)
    peer = runtime.lifecycle.hire(founder, "peer", "Help", "default",
                                  request_id="peer")["employee_id"]
    before = count(runtime, "messages")

    def summarize(snapshot, session_id):
        runtime.finish(first.activation_id)
        return {"status": "summarized", "summary": "Derived summary."}

    monkeypatch.setattr(runtime.messaging, "_summarize", summarize)
    tools = runtime.tools(first.token)
    with pytest.raises(FirmError, match="does not authorize"):
        if operation == "introduce":
            tools.introduce(source["message_id"], [peer], [], "Join", summarize=True)
        else:
            tools.read(source["message_id"], summarize=True)
    assert count(runtime, "messages") == before
    with runtime.store.read() as db:
        assert db.execute("SELECT disposition FROM message_deliveries WHERE message_id=?",
                          (source["message_id"],)).fetchone()[0] == "pending"


def test_small_idle_snapshot_advances_past_lower_priority_oldest_thread(system, monkeypatch):
    from firm_runtime import messaging

    runtime, founder, _ = system
    peer = runtime.lifecycle.hire(founder, "peer", "Help", "default",
                                  request_id="peer")["employee_id"]
    runtime.start()
    drain_births(runtime)
    older = runtime.messaging.send(peer, [], [founder], "Old", subject="Ordinary",
                                   request_id="old")
    newer = runtime.owner.send_mail(OWNER, [founder], "New", subject="Owner",
                                    action_recipients=[], request_id="new")
    with runtime.store.read() as db:
        rows = runtime.messaging.idle_rows(db, founder, runtime.store.clock())
        one = runtime.messaging._overview(rows[:1], founder, newly_offered=True)
    monkeypatch.setattr(messaging, "MAX_PEEK_BYTES", len(encoded(one).encode()) + 10)
    first = run(runtime)
    assert snapshot_ids(runtime, first.context["inbox"]) == [older["message_id"]]
    assert frontier(runtime, founder) > 0
    runtime.finish(first.activation_id)
    second = run(runtime)
    assert snapshot_ids(runtime, second.context["inbox"]) == [newer["message_id"]]
    runtime.finish(second.activation_id)
    assert runtime.admit() is None
