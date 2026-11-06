import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.role_output import person, render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.simulation import ControlledExecutor, SimulationDispatcher
from firm_runtime.store import OWNER, Store


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
        sent = tools.send([employee], "Please prepare the invoice.", "invoice")
        (analyst,) = dispatcher.tick()
        analyst_tools = runtime.tools(analyst.token)
        analyst_tools.claim("open", [sent["message_id"]])
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
            analyst_tools.send([founder], "stale", "stale")
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


@pytest.mark.parametrize("disposition", [None, "peek", "claim", "dismiss"])
def test_admission_race_and_arrival_not_lost(system, disposition):
    runtime, founder, _ = system
    runtime.start()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: runtime.admit(), range(2)))
    (first,) = [result for result in results if result]
    runtime.mark_started(first.activation_id)
    sent = runtime.messaging.send(OWNER, [founder], "New work during execution", request_id="new")
    if disposition == "peek":
        runtime.messaging.peek(founder)
    elif disposition:
        getattr(runtime.messaging, disposition)(founder, [sent["message_id"]], request_id="open")
    runtime.finish(first.activation_id)
    if disposition in {"claim", "dismiss"}:
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
    runtime.messaging.send(OWNER, [founder], "Can wait", request_id="mail")
    runtime.start()
    first = run(runtime)
    runtime.finish(first.activation_id)
    assert runtime.admit() is None
    assert count(runtime, "scheduling_opportunities") == 0


def test_hard_stop_revokes_token_preserves_claim_and_recovery(system):
    runtime, founder, _ = system
    mail = runtime.messaging.send(OWNER, [founder], "Do a thing", request_id="mail")
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        runtime.start()
        (first,) = dispatcher.tick()
        runtime.tools(first.token).claim("claim", [mail["message_id"]])
        assert dispatcher.stop(force=True)["state"] == "stopped"
        assert dispatcher.executor.running == {}
        with pytest.raises(FirmError):
            runtime.tools(first.token).peek()
        runtime.start()
        (second,) = dispatcher.tick()
        assert second.session_id == first.session_id
        assert second.context["recovery"]["cause"] == "owner_stop"
        assert [c["message_id"] for c in second.context["recovery"]["recent_claims"]] == [
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
        runtime.messaging.send(OWNER, [founder], "Additional work", request_id=f"mail-{attempt}")
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
    runtime.messaging.send(OWNER, [founder], "Owner note", request_id="owner-mail")
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
        runtime.messaging.send(founder, [founder], "Rolled back", request_id="rollback")
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


def test_recovery_claims_latest_five_across_activations_without_bodies(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    ids = []
    for index in range(7):
        mail = runtime.messaging.send(
            OWNER,
            [founder],
            f"Secret body {index}",
            request_id=f"send-{index}",
            subject=f"Message subject number {index}",
        )
        ids.append(mail["message_id"])
        runtime.tools(first.token).claim(f"claim-{index}", [ids[-1]])
    runtime.finish(first.activation_id)
    runtime.messaging.send(OWNER, [founder], "New opportunity", request_id="next")
    second = run(runtime)
    runtime.finish(second.activation_id, "interrupted", category="owner_stop")
    third = run(runtime)
    recovery = third.context["recovery"]
    assert [c["message_id"] for c in recovery["recent_claims"]] == list(reversed(ids[-5:]))
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
    runtime.messaging.send(founder, [employees[0]], "Old ordinary mail", request_id="ordinary-mail")
    now[0] += 1
    runtime.messaging.send(OWNER, [employees[1]], "Owner work", request_id="owner")
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
    runtime.messaging.send(founder, [employees[0]], "Earlier opportunity", request_id="earlier")
    now[0] += 1
    runtime.messaging.send(founder, [employees[2]], "Later opportunity", request_id="later")
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
    assert second.context["inbox"]["pending_count"] == 0


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
    sent = runtime.messaging.send(OWNER, [founder], "New work", request_id="new")
    runtime.tools(first.token).claim("claim", [sent["message_id"]])
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
                second.context["recovery"]["recent_claims"][0]["message_id"] == sent["message_id"]
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
    runtime.messaging.send(child, [founder], "Older ordinary work", request_id="older")
    now[0] += 1
    runtime.messaging.send(OWNER, [child], "Owner work", request_id="child-owner")
    with runtime.store.transaction() as db:
        urgent = runtime.messaging.notice(
            db, [founder], "Control work", subject="Control work for founder", priority=2
        )
    assert runtime.status()["waiting"] == 2
    runtime.messaging.claim(founder, [urgent["message_id"]], request_id="claim-control")
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


def test_mail_arriving_during_work_is_shown_once_and_leaves_it_pending(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    tools = runtime.tools(first.token)
    assert "mail_waiting" not in tools.get_role(role=founder)
    runtime.messaging.send(OWNER, [founder], "Quick question", request_id="q1")
    shown = tools.get_role(role=founder)
    assert labelled(shown) == [("Owner", 1)]
    rendered = render_tool_result("get_role", shown)
    assert rendered.endswith("\n\nNew mail: 1 from Owner.")
    assert "mail_waiting" not in tools.get_role(role=founder)
    assert pending(runtime, founder) == 1


def test_peek_is_the_observation_and_renders_no_notice(system):
    runtime, founder, _ = system
    runtime.start()
    first = run(runtime)
    tools = runtime.tools(first.token)
    runtime.messaging.send(OWNER, [founder], "Quick question", request_id="q1")
    peeked = tools.peek()
    assert "mail_waiting" not in peeked
    assert "New mail" not in render_tool_result("peek", peeked)
    assert "mail_waiting" not in tools.get_role(role=founder)
    assert pending(runtime, founder) == 1


def test_paginated_peek_preserves_late_mail_through_runtime_clear(system):
    runtime, founder, _ = system
    original_owner = runtime.messaging.send(
        OWNER, [founder], "Original owner request", request_id="original-owner"
    )
    with runtime.store.transaction() as db:
        operations = runtime.messaging.notice(
            db, [founder], "Original operations notice", subject="Operations"
        )
    runtime.start()
    invocation = run(runtime)
    tools = runtime.tools(invocation.token)

    first = tools.peek(limit=1)
    assert first["seen_sequence"] is None
    assert first["next_cursor"]
    assert first["conversations"][0]["conversation_id"] == operations["conversation_id"]
    assert "mail_waiting" not in first
    tools.claim("claim-operations", [operations["message_id"]])
    with runtime.store.read() as db:
        watermark = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM message_sequence"
        ).fetchone()[0]

    late = runtime.messaging.send(
        OWNER, [founder], "Arrived after the peek snapshot", request_id="late-owner"
    )
    continued = tools.peek(cursor=first["next_cursor"], limit=1)
    assert continued["seen_sequence"] == watermark
    assert labelled(continued) == [("Owner", 1)]
    assert continued["conversations"][0]["conversation_id"] == original_owner["conversation_id"]

    cleared = tools.clear("clear-snapshot")
    assert cleared["count"] == 1
    assert cleared["conversations"][0]["message_ids"] == [original_owner["message_id"]]
    assert pending(runtime, founder) == 1
    with runtime.store.read() as db:
        state = db.execute(
            "SELECT disposition FROM message_deliveries WHERE employee_id=? AND message_id=?",
            (founder, late["message_id"]),
        ).fetchone()[0]
    assert state == "pending"


def test_wake_byte_pagination_needs_complete_traversal_before_clear(system, monkeypatch):
    import firm_runtime.messaging as messaging_module

    runtime, founder, _ = system
    workers = [
        runtime.lifecycle.hire(
            founder, f"inbox-{index}", "Prepare a status report.", "default",
            request_id=f"inbox-hire-{index}",
        )["employee_id"]
        for index in range(5)
    ]
    subject = "📦" * 80
    original = [
        runtime.messaging.send(
            OWNER, [founder], "Owner request", subject=subject, request_id="wake-owner"
        )
    ]
    with runtime.store.transaction() as db:
        original.append(
            runtime.messaging.notice(
                db, [founder], "Operations request", subject=subject
            )
        )
    for index, worker in enumerate(workers):
        original.append(
            runtime.messaging.send(
                worker, [founder], "Status update", subject=subject,
                request_id=f"wake-worker-{index}",
            )
        )
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM execution_requests WHERE employee_id!=?", (founder,))

    # Force the wake overview through the same response byte bound used by tool peeks.
    monkeypatch.setattr(messaging_module, "MAX_PEEK_BYTES", 1_800)
    runtime.start()
    invocation = run(runtime)
    assert invocation.employee_id == founder
    with runtime.store.read() as db:
        wake_coverage = db.execute(
            "SELECT inbox_seen_sequence FROM activations WHERE activation_id=?",
            (invocation.activation_id,),
        ).fetchone()[0]
    assert wake_coverage is None
    assert invocation.context["inbox"]["next_cursor"]

    tools = runtime.tools(invocation.token)
    too_soon = tools.clear("clear-before-traversal")
    assert too_soon == {"status": "nothing_cleared", "reason": "no_peek"}
    assert pending(runtime, founder) == len(original)

    first = tools.peek()
    assert first["next_cursor"]
    assert first["conversations"][0]["conversation_id"] == original[1]["conversation_id"]
    tools.claim("claim-before-continuation", [original[1]["message_id"]])
    with runtime.store.read() as db:
        watermark = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM message_sequence"
        ).fetchone()[0]
    late = runtime.messaging.send(
        OWNER, [founder], "Arrived after the wake snapshot", request_id="wake-late-owner"
    )
    with runtime.store.read() as db:
        late_sequence = db.execute(
            "SELECT sequence FROM messages WHERE message_id=?", (late["message_id"],)
        ).fetchone()[0]
        presented_sequence = db.execute(
            "SELECT mail_presented_sequence FROM activations WHERE activation_id=?",
            (invocation.activation_id,),
        ).fetchone()[0]
    assert late_sequence > watermark >= presented_sequence

    seen = []
    notices = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = tools.peek(cursor=cursor)
        seen.extend(row["conversation_id"] for row in page["conversations"])
        if page.get("mail_waiting"):
            notices.extend(labelled(page))
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["seen_sequence"] is not None
    assert notices == [("Owner", 1)]
    assert set(seen) | {original[1]["conversation_id"]} == {
        item["conversation_id"] for item in original
    }

    cleared = tools.clear("clear-after-wake-traversal")
    cleared_ids = {
        message_id for row in cleared["conversations"] for message_id in row["message_ids"]
    }
    assert cleared["count"] == len(original) - 1
    assert original[1]["message_id"] not in cleared_ids
    assert late["message_id"] not in cleared_ids
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
    runtime.messaging.send(founder, [child], "Kickoff", request_id="kickoff")
    activation = run(runtime)
    assert activation.employee_id == child
    runtime.messaging.send(peers[0], [child], "Peer note", request_id="peer-early")
    runtime.messaging.send(peers[1], [child], "Peer note", request_id="peer-late")
    runtime.messaging.send(founder, [child], "Manager note", request_id="manager")
    with runtime.store.transaction() as db:
        runtime.messaging.notice(db, [child], "Operations note", subject="Operations")
    runtime.messaging.send(OWNER, [child], "Owner note", request_id="owner")
    runtime.messaging.send(OWNER, [child], "Owner again", request_id="owner-again")
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
