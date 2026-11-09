import json

from runtime_test_support import provider_usage as usage
from runtime_test_support import start_activation as start
from runtime_test_support import telemetry_runtime as make_runtime

from firm_runtime.messaging import MAX_PEEK_BYTES
from firm_runtime.store import OWNER, encoded


def test_self_context_unknown_measured_aged_and_previous_period(tmp_path):
    now = [1000.0]
    runtime = make_runtime(tmp_path, clock=lambda: now[0])
    actor = start(runtime)

    def peek():
        return runtime.messaging.peek(actor.employee_id)["context"]["self"]["context"]

    assert peek()["state"] == "unknown"
    assert peek()["observed_tokens"] is None
    runtime.record_usage(actor.activation_id, usage(6_500))
    assert peek()["state"] == "measured"
    assert peek()["level"] == "advisory"
    now[0] += 301
    assert peek()["state"] == "stale"
    assert peek()["stale_reason"] == "measurement_age"
    runtime.finish(actor.activation_id)
    assert peek()["stale_reason"] == "no_current_work_period_measurement"
    runtime.messaging.send(OWNER, [actor.employee_id], "Continue", request_id="continue")
    next_actor = start(runtime)
    assert peek()["state"] == "stale"
    runtime.record_usage(next_actor.activation_id, usage(1_000))
    assert peek()["state"] == "measured"
    assert peek()["level"] == "normal"


def test_temporary_admitted_running_final_and_finished_are_distinct(tmp_path):
    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    tools = runtime.tools(founder.token)
    child = tools.hire("analyst", "Own work", "hire", temporary_periods=1)
    analyst = runtime.admit()

    saved = analyst.context["inbox"]["context"]["self"]
    assert saved["activity"] == "admitted"
    assert saved["temporary_periods"] == {
        "unspent_periods": 1,
        "periods_after_current": 0,
        "final_work_period_running": False,
    }
    with runtime.store.read() as db:
        stored = db.execute(
            "SELECT context_json FROM activations WHERE activation_id=?", (analyst.activation_id,)
        ).fetchone()[0]
        assert json.loads(stored) == analyst.context

    def facts():
        return tools.peek()["context"]["direct_reports"][0]

    assert facts()["employee_id"] == child["employee_id"]
    assert facts()["activity"] == "admitted"
    assert facts()["temporary_periods"] == {
        "unspent_periods": 1,
        "periods_after_current": 0,
        "final_work_period_running": False,
    }
    runtime.mark_started(analyst.activation_id)
    assert facts()["activity"] == "running"
    assert facts()["temporary_periods"] == {
        "unspent_periods": 0,
        "periods_after_current": 0,
        "final_work_period_running": True,
    }
    runtime.finish(analyst.activation_id)
    assert facts()["activity"] == "idle"
    assert facts()["temporary_periods"]["final_work_period_running"] is False
    tools.renew_temporary(analyst.employee_id, 5, "renew")
    assert tools.peek()["context"]["direct_reports"] == []
    assert tools.peek()["context"]["other_direct_reports"] == 1


def test_only_relevant_direct_reports_are_visible(tmp_path):
    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    tools = runtime.tools(founder.token)
    manager = tools.hire("manager", "Manage", "manager")
    manager_run = start(runtime)
    tools.hire("peer", "Own work", "peer")
    manager_tools = runtime.tools(manager_run.token)
    grandchild = manager_tools.hire("analyst", "Own work", "analyst", temporary_periods=1)
    own = manager_tools.peek()["context"]
    assert own["self"]["employee_id"] == manager["employee_id"]
    assert [row["employee_id"] for row in own["direct_reports"]] == [grandchild["employee_id"]]
    parent = tools.peek()["context"]
    assert [row["employee_id"] for row in parent["direct_reports"]] == [manager["employee_id"]]
    assert parent["other_direct_reports"] == 1
    runtime.record_usage(manager_run.activation_id, usage(9_500))
    runtime.finish(manager_run.activation_id)
    observed = tools.peek()["context"]["direct_reports"][0]["context"]
    assert observed["state"] == "stale"
    assert observed["level"] == "handoff_required"
    assert "context" not in runtime.messaging.peek(OWNER)


def test_context_and_mail_share_budget_with_explicit_omissions(tmp_path, monkeypatch):
    # Exercise the smaller budget reserved for restored controls as well as the current one.
    import firm_runtime.messaging as messaging

    monkeypatch.setattr(messaging, "MAX_PEEK_BYTES", min(MAX_PEEK_BYTES, 10 * 1024))
    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    tools = runtime.tools(founder.token)
    reports = []
    for i in range(30):
        reports.append(tools.hire(f"analyst-{i}", "Own work", f"hire-{i}", temporary_periods=1))
        runtime.messaging.send(
            OWNER, [founder.employee_id], "Body", subject="X" * 80, request_id=f"mail-{i}"
        )
    context = tools.peek()["context"]
    assert context["omitted_relevant_reports"] > 0
    assert len(context["direct_reports"]) + context["omitted_relevant_reports"] == 30
    assert len(encoded(context).encode()) <= messaging.MAX_PEEK_BYTES // 3
    # Thirty reports each write in, so the founder has thirty-one conversations to page through.
    for report in reports:
        runtime.messaging.send(
            report["employee_id"], [founder.employee_id], "Body", subject="😀" * 80, request_id="in"
        )
    first = runtime.messaging.peek(founder.employee_id)
    # The owner's conversation comes first and carries all thirty of its messages.
    assert first["conversations"][0]["participants"][0]["employee_id"] == OWNER
    assert first["conversations"][0]["pending_count"] == 30
    seen, cursor, pages = [], None, 0
    while True:
        result = runtime.messaging.peek(founder.employee_id, cursor=cursor)
        assert len(encoded(result).encode()) <= messaging.MAX_PEEK_BYTES
        assert result["context"] == context
        seen.extend(row["conversation_id"] for row in result["conversations"])
        pages += 1
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert pages > 1
    assert len(seen) == len(set(seen)) == 31


def test_fresh_session_does_not_relabel_previous_pressure_or_window(tmp_path):
    runtime = make_runtime(tmp_path, context_window=None)
    actor = start(runtime)
    runtime.record_usage(actor.activation_id, usage(11_000, native_window=12_000))
    runtime.tools(actor.token).handoff("Keep working", "continue", "handoff")
    runtime.finish(actor.activation_id)
    fresh = start(runtime)
    assert fresh.session_id != actor.session_id
    context = runtime.tools(fresh.token).peek()["context"]["self"]["context"]
    assert context["state"] == "stale"
    assert context["stale_reason"] == "no_current_work_period_measurement"
    assert context["observed_tokens"] == 11_000
    assert context["window_tokens"] == 12_000
    assert context["window_source"] == "provider_window"
    runtime.record_usage(fresh.activation_id, usage(500, native_window=24_000))
    context = runtime.tools(fresh.token).peek()["context"]["self"]["context"]
    assert context["state"] == "measured"
    assert context["window_tokens"] == 24_000
    assert context["window_source"] == "provider_window"


GIB = 1 << 30


LOW_DISK_NOTICE = (
    "The Mac you hold is low on disk: about {free:.1f} GiB free. "
    "Delete what you no longer need in your scratch folder."
)


def hold_mac_lease(runtime, actor, station):
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO mac_leases"
            "(lease_id,station,activation_id,employee_id,acquired_at) VALUES(?,?,?,?,?)",
            (f"{station:032x}", station, actor.activation_id, actor.employee_id,
             runtime.store.clock()),
        )


def sample_mac_disk(runtime, free_bytes, total_bytes, *, age=0):
    """The Mac's own resource sample, as the once-a-minute observer records it."""
    from firm_runtime import analysis_history

    with runtime.store.transaction() as db:
        analysis_history.record_resources(
            db, runtime.store.clock() - age, "mac",
            {"disk_used_bytes": total_bytes - free_bytes, "disk_total_bytes": total_bytes},
        )


def test_a_holder_learns_on_peek_that_its_mac_has_filled_since_it_acquired(tmp_path):
    runtime = make_runtime(tmp_path)
    actor = start(runtime)
    hold_mac_lease(runtime, actor, 1)
    # Roomy when the lease began: acquire refuses a full Mac, so it always is.
    sample_mac_disk(runtime, 60 * GIB, 100 * GIB)
    assert runtime.messaging.peek(actor.employee_id)["context"]["notices"] == []
    sample_mac_disk(runtime, 5 * GIB, 100 * GIB)
    notices = runtime.messaging.peek(actor.employee_id)["context"]["notices"]
    assert notices == [LOW_DISK_NOTICE.format(free=5.0)]


def test_shared_mac_notice_requires_a_lease_and_a_fresh_reading(tmp_path):
    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    tools = runtime.tools(founder.token)
    tools.hire("analyst", "Own work", "hire")
    other = start(runtime)
    sample_mac_disk(runtime, 1 * GIB, 100 * GIB)
    # Only holders receive a notice; both account holders share the same disk reading.
    assert runtime.messaging.peek(other.employee_id)["context"]["notices"] == []
    hold_mac_lease(runtime, other, 2)
    assert runtime.messaging.peek(founder.employee_id)["context"]["notices"] == []
    assert runtime.messaging.peek(other.employee_id)["context"]["notices"] == [
        LOW_DISK_NOTICE.format(free=1.0)
    ]
    # An old reading asserts nothing: the Mac may have been cleaned since.
    hold_mac_lease(runtime, founder, 1)
    assert runtime.messaging.peek(founder.employee_id)["context"]["notices"] == [
        LOW_DISK_NOTICE.format(free=1.0)
    ]
    sample_mac_disk(runtime, 1 * GIB, 100 * GIB, age=3600)
    assert runtime.messaging.peek(founder.employee_id)["context"]["notices"] == []


def test_unicode_identity_keeps_self_and_reports_within_context_budget(tmp_path):
    from firm_runtime.inbox_context import overview

    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    tools = runtime.tools(founder.token)
    for i in range(10):
        tools.hire(f"analyst-{i}", "Own work", f"hire-{i}", temporary_periods=1)
    # Nested valid addresses may be long; storage allows names beyond rendered previews.
    with runtime.store.transaction() as db:
        for i, row in enumerate(db.execute("SELECT employee_id FROM roles").fetchall()):
            db.execute(
                "UPDATE roles SET address=? WHERE employee_id=?",
                ("\U0001f600" * 200 + str(i), row["employee_id"]),
            )
        result = overview(db, founder.employee_id, 1000.0, byte_limit=10 * 1024 // 3)
    assert result["self"]["address_truncated"] is True
    assert result["omitted_relevant_reports"] > 0
    assert len(encoded(result).encode()) <= 10 * 1024 // 3


def test_linux_disk_notice_reaches_every_role_and_ignores_stale_unknown_samples(tmp_path):
    from firm_runtime import analysis_history

    runtime = make_runtime(tmp_path)
    founder = start(runtime)
    runtime.tools(founder.token).hire("analyst", "Own work", "hire")
    other = start(runtime)
    for free, total, age, expected in (
        (None, None, 0, []),
        (16 * GIB, 100 * GIB, 0, []),
        (15 * GIB, 100 * GIB, 0, [
            "The Linux disk you share with your colleagues is low: about 15.0 GiB free. "
            "Delete scratch files and Docker containers, images and volumes you no longer need."
        ]),
        (1 * GIB, 100 * GIB, 3600, []),
        (None, 100 * GIB, 0, []),
    ):
        with runtime.store.transaction() as db:
            analysis_history.record_resources(db, runtime.store.clock() - age, "linux", {
                "disk_used_bytes": total - free if free is not None else None,
                "disk_total_bytes": total,
            })
        for actor in (founder, other):
            assert runtime.messaging.peek(actor.employee_id)["context"]["notices"] == expected



def test_housekeeping_counts_finished_periods_for_only_this_employee(tmp_path):
    from firm_runtime.inbox_context import HOUSEKEEPING_EVERY

    runtime = make_runtime(tmp_path)
    actor = start(runtime)
    expected = (
        "Housekeeping: see what your scratch directory and Docker hold (du -sh on scratch, "
        "docker system df) and remove checkouts, containers, images and volumes from finished "
        "work. Keep what current work still needs."
    )
    for finished in range(2 * HOUSEKEEPING_EVERY + 1):
        notices = runtime.messaging.peek(actor.employee_id)["context"]["notices"]
        assert notices == ([expected] if (finished + 1) % HOUSEKEEPING_EVERY == 0 else [])
        with runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO activations(activation_id,employee_id,session_id,state,"
                "wake_snapshot,context_json,created_at,finished_at) VALUES(?,?,?,?,'{}','{}',0,1)",
                (f"old-{finished}", actor.employee_id, actor.session_id,
                 ("completed", "failed", "interrupted")[finished % 3]),
            )
    assert "context" not in runtime.messaging.peek(OWNER)



def test_housekeeping_does_not_count_other_roles_or_unfinished_activations(tmp_path):
    from firm_runtime.inbox_context import HOUSEKEEPING_EVERY

    runtime = make_runtime(tmp_path)
    actor = start(runtime)
    child = runtime.tools(actor.token).hire("analyst", "Work", "hire")
    with runtime.store.transaction() as db:
        for index in range(HOUSEKEEPING_EVERY - 1):
            for employee, finished in [(actor.employee_id, None), (child["employee_id"], 1)]:
                db.execute(
                    "INSERT INTO activations(activation_id,employee_id,session_id,state,"
                    "wake_snapshot,context_json,created_at,finished_at) "
                    "VALUES(?,?,?,'interrupted','{}','{}',0,?)",
                    (f"old-{employee}-{index}", employee, actor.session_id, finished),
                )
    assert runtime.messaging.peek(actor.employee_id)["context"]["notices"] == []
    assert runtime.messaging.peek(child["employee_id"])["context"]["notices"][0].startswith(
        "Housekeeping:")
