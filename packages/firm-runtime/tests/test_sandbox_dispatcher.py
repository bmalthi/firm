import json
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from firm_sandbox import Layout, Run, RunSpec
from runtime_test_support import FakeProvider, FakeSandbox

from firm_runtime.errors import FirmError
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import OWNER, Store
from firm_runtime.workspaces import grants_for, sandbox_role_id, sandbox_run_id


@pytest.fixture
def system(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "runtime" / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Do useful work",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "codex",
            "profiles": {"codex": {}},
        },
    )
    box = FakeSandbox(tmp_path / "sandbox")
    provider = FakeProvider()
    return runtime, box, provider


def host(system):
    runtime, box, provider = system
    return SandboxDispatcher(runtime, box, {"codex": provider}, mcp_url="http://127.0.0.1:9000/mcp")


def launch(dispatcher, runtime):
    (invocation,) = dispatcher.tick()
    for future in dispatcher._launches.values():
        future.result(timeout=2)
    # Simulate the guest launcher's authenticated handshake, not an owner-side start guess.
    runtime.begin_execution(invocation.token)
    dispatcher.tick()
    return invocation


def test_opening_creates_the_firm_lock_directory_the_sandbox_can_lock(system):
    from firm_sandbox.locking import lifecycle_locked

    runtime, box, _ = system
    with host(system):
        assert box.config.locks.stat().st_mode & 0o777 == 0o700
        with lifecycle_locked(box.config.locks, exclusive=True):
            pass


@pytest.mark.parametrize("force", [False, True])
def test_unknown_infrastructure_state_never_settles_execution_as_stopped(system, force):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        box.infrastructure_status = lambda: {"running": None, "state": "recovery_required"}
        if force:
            dispatcher.stop(force=True)
        with pytest.raises(FirmError, match="recovery|shutdown is not confirmed"):
            dispatcher.tick()
        with runtime.store.read() as db:
            assert (
                db.execute(
                    "SELECT state FROM activations WHERE activation_id=?",
                    (invocation.activation_id,),
                ).fetchone()[0]
                == "running"
            )
        assert runtime.status()["state"] != "stopped"


def test_a_healthy_probe_clears_the_unknown_state_a_failed_one_recorded(system):
    runtime, box, _ = system

    def recorded():
        with runtime.store.read() as db:
            return db.execute("SELECT infrastructure_state FROM execution_host").fetchone()[0]

    with host(system) as dispatcher:
        dispatcher.start()
        healthy = box.infrastructure_status
        box.infrastructure_status = lambda: {"running": None, "state": "recovery_required"}
        with pytest.raises(FirmError, match="recovery"):
            dispatcher.tick()
        assert recorded() == "unknown"
        box.infrastructure_status = healthy
        dispatcher.tick()
        assert recorded() == "running"


def test_launch_handoff_shutdown_and_real_session_resume(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        first = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        dispatcher.tick()
        runtime.messaging.send(OWNER, ["founder"], "New work", request_id="mail")
        second = launch(dispatcher, runtime)
        assert provider.resumed == [None, "native-session"]
        assert second.session_id == first.session_id
        runtime.tools(second.token).handoff("Next task", "continue", "handoff")
        dispatcher.stop()
        second_run = list(box.invocations)[-1]
        box.complete(second_run)
        dispatcher.tick()
        assert runtime.status()["state"] == "stopped"
        assert not box.live
        assert dispatcher.tick() == []
        dispatcher.start()
        third = launch(dispatcher, runtime)
        assert third.session_id != second.session_id
        assert provider.resumed[-1] is None
        dispatcher.stop(force=True)
        dispatcher.tick()
        assert not box.live


def usage_row(runtime):
    with runtime.store.read() as db:
        return dict(
            db.execute("SELECT memory_peak_bytes,cpu_usec,oom_kills FROM sandbox_runs").fetchone()
        )


def test_a_completed_run_stores_what_its_slice_used(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.usage = {"memory_peak_bytes": 734003200, "cpu_usec": 9500000, "oom_kills": 0}
        box.complete(run_id)
        dispatcher.tick()
    assert usage_row(runtime) == box.usage


def test_reclaim_is_stored_with_the_terminal_run(system):
    runtime, box, _ = system
    box.reclaim = {"categories": {"scratch": {"count": 4, "bytes": 500, "skipped": {}}}}
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        dispatcher.tick()
    with runtime.store.read() as db:
        row = db.execute("SELECT * FROM sandbox_run_settlements").fetchone()
        assert row["run_id"] == run_id
        assert json.loads(row["reclaim_json"]) == box.reclaim


def test_unresolved_reclaim_keeps_admission_until_cleanup_retries_successfully(system, monkeypatch):
    runtime, box, _ = system
    box.reclaim = {"categories": {"scratch": {"completed": False, "skipped": {"deadline": 1}}}}
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        original_status = box._status
        monkeypatch.setattr(
            box, "_status", lambda state: {**original_status(state), "unresolved": True}
        )
        monkeypatch.setattr(box, "stop", lambda run: box.status(run))
        with pytest.raises(FirmError, match="cleanup"):
            dispatcher.reconcile()
        assert runtime.status()["active"][0]["activation_id"] == invocation.activation_id
        with runtime.store.read() as db:
            assert json.loads(db.execute("SELECT reclaim_json FROM sandbox_run_settlements")
                              .fetchone()[0]) == box.reclaim
        monkeypatch.setattr(box, "_status", original_status)
        dispatcher.reconcile()
        assert runtime.status()["active"] == []


@pytest.mark.parametrize("condition", [
    "eligible", "recent_host", "recent_guest", "active_host", "active_guest",
    "unresolved", "wrong_generation", "wrong_role", "unknown", "lost_log", "short_log",
    "remove_failed", "boot_interrupted", "record_failed",
    "tool_parse_failed", "tool_write_failed", "usage_parse_failed", "usage_write_failed",
])
def test_journal_pruning_requires_recorded_finished_execution(system, monkeypatch, condition):
    from firm_runtime.sandbox_dispatcher import RUN_JOURNAL_RETENTION

    runtime, box, provider = system
    removed = []
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        dispatcher.tick()
        provider.parse_tool_calls = lambda logs: {"Bash": 3}
        provider.parse_usage = lambda logs: ProviderUsage(input_tokens=120, source="journal")
        old = runtime.store.clock() - RUN_JOURNAL_RETENTION - 1
        box.ended[run_id] = old
        box.usage = {"memory_peak_bytes": 500, "cpu_usec": 200, "oom_kills": 0}
        with runtime.store.transaction() as db:
            if condition != "recent_host":
                db.execute("UPDATE activations SET finished_at=? WHERE activation_id=?",
                           (old, invocation.activation_id))
            if condition == "active_host":
                db.execute("UPDATE activations SET state='running' WHERE activation_id=?",
                           (invocation.activation_id,))
            if condition in {"wrong_generation", "wrong_role"}:
                column = "vm_generation" if condition == "wrong_generation" else "role_id"
                db.execute(f"UPDATE sandbox_runs SET {column}='other'")
        inventory = box.inventory()
        run, info = inventory[run_id]
        if condition == "recent_guest":
            info["ended"] = runtime.store.clock()
        if condition == "active_guest":
            info["running"] = True
        if condition == "unresolved":
            info["unresolved"] = True
        if condition == "boot_interrupted":
            info.pop("ended")
            info["interrupted"] = True
        if condition == "unknown":
            inventory = {"unknown": (Run(run.role_id, "unknown", run.vm_generation), info)}
        if condition == "lost_log":
            monkeypatch.setattr(box, "logs", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
        if condition == "short_log":
            monkeypatch.setattr(box, "logs", lambda *a, **kw: b"short")
        if condition == "record_failed":
            def fail_record(*args):
                raise sqlite3.OperationalError("host database unavailable")
            monkeypatch.setattr(dispatcher, "_record_settlement", fail_record)
        if condition in {"tool_parse_failed", "usage_parse_failed"}:
            def fail_parse(logs):
                raise ValueError("unexpected provider event")
            attribute = "parse_tool_calls" if condition == "tool_parse_failed" else "parse_usage"
            monkeypatch.setattr(provider, attribute, fail_parse)
        if condition in {"tool_write_failed", "usage_write_failed"}:
            def fail_write(*args, **kwargs):
                raise sqlite3.OperationalError("host database unavailable")
            attribute = ("record_tool_calls" if condition == "tool_write_failed"
                         else "record_usage")
            monkeypatch.setattr(runtime, attribute, fail_write)

        def remove(run):
            # Nothing guest-side disappears until the host transaction commits.
            assert usage_row(runtime) == box.usage
            with runtime.store.read() as db:
                receipt = db.execute("SELECT * FROM sandbox_run_settlements WHERE run_id=?",
                                     (run.run_id,)).fetchone()
                assert json.loads(receipt["usage_json"]) == box.usage
                assert db.execute("SELECT calls FROM activation_tool_calls").fetchone()[0] == 3
                assert db.execute("SELECT input_tokens FROM activation_usage").fetchone()[0] == 120
            removed.append(run.run_id)
            if condition == "remove_failed":
                raise OSError("disconnected")
            del box.invocations[run.run_id]

        monkeypatch.setattr(box, "remove", remove, raising=False)
        dispatcher._next_journal_sweep = 0
        dispatcher._prune_run_journals(inventory)
        assert removed == (
            [run_id] if condition in {"eligible", "remove_failed", "boot_interrupted"} else []
        )
        dispatcher._prune_run_journals(inventory)
        assert len(removed) <= 1  # Sweep is hourly, not on every scheduler tick.
        assert invocation.activation_id not in dispatcher._logs


@pytest.mark.parametrize("cause", ["host_restart", "owner_stop", "timeout"])
def test_journal_backfills_logless_completion_without_changing_live_state(
    system, monkeypatch, cause,
):
    from firm_runtime.sandbox_dispatcher import RUN_JOURNAL_RETENTION

    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        row = {**dispatcher._records(active_only=True)[0], "stop_cause": cause}
        run, info = box.inventory()[run_id]
        with monkeypatch.context() as patch:
            def unavailable(*args, **kwargs):
                raise OSError("log unavailable at completion")
            patch.setattr(box, "logs", unavailable)
            dispatcher._finish_run(row, run, info)
        with runtime.store.read() as db:
            assert runtime._activation(db, invocation.activation_id)["finished_at"] is not None
            assert db.execute("SELECT COUNT(*) FROM activation_usage").fetchone()[0] == 0
        provider.parse_tool_calls = lambda logs: {"Bash": 7}
        provider.parse_usage = lambda logs: ProviderUsage(input_tokens=321, source="journal")
        provider.parse_subscription = lambda logs: pytest.fail("historical subscription replay")
        old = runtime.store.clock() - RUN_JOURNAL_RETENTION - 1
        box.ended[run_id] = old
        with runtime.store.transaction() as db:
            db.execute("UPDATE activations SET finished_at=? WHERE activation_id=?",
                       (old, invocation.activation_id))
            db.execute("UPDATE context_sessions SET current_context_tokens=999,observed_at=?",
                       (runtime.store.clock(),))
        with runtime.store.read() as db:
            before = [dict(r) for r in db.execute("SELECT * FROM context_sessions")]
            sessions = [dict(r) for r in db.execute("SELECT * FROM provider_sessions")]
        removed = []
        monkeypatch.setattr(box, "remove", removed.append, raising=False)
        dispatcher._next_journal_sweep = 0
        dispatcher._prune_run_journals(box.inventory())
        assert removed == [run]
        with runtime.store.read() as db:
            observed = db.execute("SELECT * FROM activation_usage").fetchone()
            assert observed["input_tokens"] == 321 and observed["observed_at"] == old
            assert db.execute("SELECT calls FROM activation_tool_calls").fetchone()[0] == 7
            assert [dict(r) for r in db.execute("SELECT * FROM context_sessions")] == before
            assert [dict(r) for r in db.execute("SELECT * FROM provider_sessions")] == sessions


def test_host_restart_collects_usage_without_replaying_subscription(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    run_id = next(iter(box.invocations))
    box.invocations[run_id][2] = b"old usage and allowance events"
    provider.parse_usage = lambda logs: ProviderUsage(input_tokens=123, source="journal")
    provider.parse_subscription = lambda logs: pytest.fail("old allowance refreshed after restart")
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)) as dispatcher:
        assert dispatcher._subscription_events == {}
        dispatcher.reconcile()
        with reopened.store.read() as db:
            assert reopened._activation(db, invocation.activation_id)["state"] == "interrupted"
            assert db.execute("SELECT input_tokens FROM activation_usage").fetchone()[0] == 123


def test_journal_sweep_includes_recorded_summaries_but_protects_inflight(system, monkeypatch):
    from firm_runtime.sandbox_dispatcher import RUN_JOURNAL_RETENTION
    from firm_runtime.summaries import summary_role_id

    runtime, box, _ = system
    old = runtime.store.clock() - RUN_JOURNAL_RETENTION - 1
    run = Run(summary_role_id("summary-one"), "summary-one", "generation")
    with runtime.store.transaction() as db:
        db.execute("INSERT INTO summary_runs(run_id,started_at,finished_at,state,input_bytes) "
                   "VALUES(?,?,?,'summarized',10)", (run.run_id, old - 20, old))
    inventory = {run.run_id: (run, {"running": False, "ended": old, "usage": {"cpu_usec": 10}})}
    removed = []
    monkeypatch.setattr(box, "remove", removed.append, raising=False)
    with host(system) as dispatcher:
        monkeypatch.setattr(runtime.summary_service, "inflight", lambda: {run.run_id})
        dispatcher._prune_run_journals(inventory)
        assert removed == []
        monkeypatch.setattr(runtime.summary_service, "inflight", set)
        dispatcher._next_journal_sweep = 0
        dispatcher._prune_run_journals(inventory)
        assert removed == [run]
        with runtime.store.read() as db:
            assert db.execute("SELECT usage_json FROM sandbox_run_settlements").fetchone()[0] == (
                '{"cpu_usec": 10}'
            )


def test_a_stopped_run_stores_the_usage_the_stop_read(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        box.usage = {"memory_peak_bytes": 1048576, "cpu_usec": 42, "oom_kills": 1}
        runtime.owner.stop_role(OWNER, invocation.employee_id, "stop-role")
        dispatcher.tick()
    assert usage_row(runtime) == box.usage


def test_a_run_without_a_reading_stores_nothing(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        box.complete(next(iter(box.invocations)))
        dispatcher.tick()
    assert usage_row(runtime) == {"memory_peak_bytes": None, "cpu_usec": None, "oom_kills": None}


def test_running_provider_logs_update_context_before_completion(system):
    runtime, box, provider = system
    provider.parse_usage = lambda logs: (
        ProviderUsage(
            current_context_tokens=7_000,
            high_water_context_tokens=7_000,
            provider_context_window=10_000,
            input_tokens=100,
            output_tokens=20,
            native_compactions=0,
            source="fake_live_stream",
            resolved_model="reported-model",
        )
        if logs
        else None
    )
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.invocations[run_id][2] = b'{"type":"structured-usage"}\n'
        dispatcher.tick()
        assert box.status(box.invocations[run_id][0])["running"]
        observed = runtime.usage_diagnostics()["activations"]
        assert observed[0]["activation_id"] == invocation.activation_id
        assert observed[0]["current_context_tokens"] == 7_000
        assert observed[0]["source"] == "fake_live_stream"
        assert observed[0]["resolved_model"] == "reported-model"
        dispatcher.stop(force=True)
        dispatcher.tick()


def tool_calls(runtime):
    with runtime.store.read() as db:
        return {
            row["activation_id"]: (row["tool"], row["calls"])
            for row in db.execute("SELECT * FROM activation_tool_calls")
        }


def test_terminal_collection_tallies_the_tools_the_work_period_called(system):
    runtime, box, provider = system
    seen = []
    provider.parse_tool_calls = lambda logs: seen.append(logs) or {"Bash": 4, "Edit": 1}
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        # A running period is not collected yet, so nothing is tallied from a partial log.
        dispatcher.tick()
        assert tool_calls(runtime) == {}
        box.complete(next(iter(box.invocations)))
        dispatcher.tick()
    with runtime.store.read() as db:
        assert dict(db.execute("SELECT tool,calls FROM activation_tool_calls")) == {
            "Bash": 4,
            "Edit": 1,
        }
        assert {r[0] for r in db.execute("SELECT activation_id FROM activation_tool_calls")} == {
            invocation.activation_id
        }
    assert seen


def test_a_changed_provider_log_shape_leaves_the_period_untallied(system):
    runtime, box, provider = system

    def broken(logs):
        raise ValueError("unexpected event shape")

    provider.parse_tool_calls = broken
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        box.complete(next(iter(box.invocations)))
        dispatcher.tick()
    assert tool_calls(runtime) == {}
    with runtime.store.read() as db:
        assert runtime._activation(db, invocation.activation_id)["state"] == "completed"


def test_a_failed_tally_write_leaves_the_period_untallied_and_the_tick_whole(system, monkeypatch):
    runtime, box, provider = system
    provider.parse_tool_calls = lambda logs: {"Bash": 1}

    def refuse(activation_id, tally):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "record_tool_calls", refuse)
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        box.complete(next(iter(box.invocations)))
        dispatcher.tick()
    assert tool_calls(runtime) == {}
    with runtime.store.read() as db:
        assert runtime._activation(db, invocation.activation_id)["state"] == "completed"


def test_streamed_allowance_is_recorded_once_per_event_for_the_bound_account(system, monkeypatch):
    runtime, box, provider = system
    provider.parse_subscription = lambda logs: (
        {"event_id": logs.decode(), "windows": [{"window": "five_hour"}]} if logs else None
    )
    recorded = []
    bound = {"account_id": "acct", "profile": "codex", "provider": None, "settings_json": "{}"}
    monkeypatch.setattr(runtime.accounts, "binding", lambda db, session: bound)
    monkeypatch.setattr(
        runtime.accounts,
        "record_subscription",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        # Naming a session on the first event stops the no-session-yet path from
        # re-parsing later ticks, so appending the second event (a run's log only
        # ever grows) does not hand it an invalid, concatenated JSON document.
        box.invocations[run_id][2] = b'{"event": 1, "session": "native-session"}'
        dispatcher.tick()
        dispatcher.tick()
        box.invocations[run_id][2] += b'{"event": 2}'
        dispatcher.tick()
        assert [record[0] for record in recorded] == [
            ("acct", [{"window": "five_hour"}]),
            ("acct", [{"window": "five_hour"}]),
        ]
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_default_timeout_stops_silent_role_after_30_minutes(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        now[0] += 1799
        dispatcher.tick()
        assert not box.stops
        now[0] += 1
        dispatcher.tick()
        assert len(box.stops) == 1
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["state"] == "failed"
        assert result["failure"] == "execution inactivity timeout expired"


def test_a_run_that_completes_between_inventory_and_polling_finishes_completed(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        row = dispatcher._records(active_only=True)[0]
        # The snapshot this poll works from is taken while the run is still going.
        run, stale_info = box.inventory()[run_id]
        now[0] = row["deadline"]
        box.complete(run_id)  # It writes its last output and exits right after that.
        box.status_calls = 0
        dispatcher._poll_run(row, run, stale_info)
        assert box.status_calls == 1  # One extra call, only on this rare path.
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["state"] == "completed"
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_output_arriving_between_inventory_and_polling_refreshes_the_deadline(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        row = dispatcher._records(active_only=True)[0]
        run, stale_info = box.inventory()[run_id]
        now[0] = row["deadline"]
        # New output was written after the snapshot but before this poll reads it.
        box.invocations[run_id][2] = b'{"progress": "still working"}'
        box.status_calls = 0
        dispatcher._poll_run(row, run, stale_info)
        assert box.status_calls == 1
        with runtime.store.read() as db:
            activation = runtime._activation(db, invocation.activation_id)
            refreshed_deadline = db.execute(
                "SELECT deadline FROM sandbox_runs WHERE activation_id=?",
                (invocation.activation_id,),
            ).fetchone()[0]
        assert activation["state"] == "running"
        assert refreshed_deadline > row["deadline"]
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_a_genuinely_silent_run_past_its_deadline_still_times_out(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        row = dispatcher._records(active_only=True)[0]
        run, stale_info = box.inventory()[run_id]
        now[0] = row["deadline"]
        dispatcher._poll_run(row, run, stale_info)
        assert len(box.stops) == 1
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["state"] == "failed"
        assert result["failure"] == "execution inactivity timeout expired"


def test_a_deadline_the_clock_crosses_mid_poll_is_not_claimed_without_a_refresh(system):
    """One clock reading decides the poll, so the claim never runs ahead of the refresh."""
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        row = dispatcher._records(active_only=True)[0]
        run, stale_info = box.inventory()[run_id]
        # Before the deadline when the poll starts, past it on every later reading.
        readings = iter([row["deadline"] - 1])
        runtime.store.clock = lambda: next(readings, row["deadline"] + 60)
        box.complete(run_id)  # It finished after the snapshot; only a refresh would see that.
        box.status_calls = 0
        dispatcher._poll_run(row, run, stale_info)
        assert box.status_calls == 0 and not box.stops
        with runtime.store.read() as db:
            assert runtime._activation(db, invocation.activation_id)["state"] == "running"
        # The next tick sees the passed deadline, refreshes, and settles it by its own exit.
        dispatcher.tick()
        with runtime.store.read() as db:
            assert runtime._activation(db, invocation.activation_id)["state"] == "completed"
        assert not box.stops
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_a_run_that_completes_between_the_refresh_and_the_stop_is_not_a_timeout(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        row = dispatcher._records(active_only=True)[0]
        run, stale_info = box.inventory()[run_id]
        now[0] = row["deadline"]

        def stop(run, timeout=0):
            # The guest finds the run already exited by itself and reports that exit.
            box.stops.append(run)
            box.complete(run_id)
            return box.status(run)

        box.stop = stop
        dispatcher._poll_run(row, run, stale_info)
        assert len(box.stops) == 1
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["state"] == "completed"
        dispatcher.stop(force=True)
        dispatcher.tick()


@pytest.mark.parametrize("activity", ["output", "peek", "context"])
def test_role_activity_allows_work_beyond_total_timeout(system, monkeypatch, activity):
    runtime, box, provider = system
    if activity == "output":
        # A run's log only ever grows, so each step below appends rather than
        # replaces it; by the last step that is no longer one valid JSON
        # document, and only its size matters here, not its content.
        provider.parse_result = lambda logs, exit_code: SimpleNamespace(
            provider_session_id=None, success=False, error=SimpleNamespace(kind="process_exit")
        )
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    monkeypatch.setattr(runtime.messaging, "context", lambda *args, **kwargs: {})
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        for step in range(4):
            now[0] += 1700
            if activity == "output":
                box.invocations[run_id][2] += json.dumps({"step": step}).encode()
            elif activity == "peek":
                runtime.tools(invocation.token).peek()
            else:
                runtime.tools(invocation.token).context("c1")
            dispatcher.tick()
            assert not box.stops
        # Over 113 minutes of work, then 30 minutes of silence.
        now[0] += 1800
        dispatcher.tick()
        assert len(box.stops) == 1


def test_activity_digest_catches_same_length_different_content(system):
    """record_activity hashes content rather than length; exercised directly,

    since a tick reads a log only from its remembered offset, so two
    same-length readings it could observe are never anything but identical.
    """
    from firm_runtime.execution_activity import record_activity

    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)

        def deadline():
            with runtime.store.read() as db:
                return db.execute(
                    "SELECT deadline FROM sandbox_runs WHERE activation_id=?",
                    (invocation.activation_id,),
                ).fetchone()[0]

        first = deadline()
        now[0] += 100
        record_activity(runtime.store, invocation.activation_id, output=b"aaaa")
        second = deadline()
        assert second > first
        now[0] += 100
        # Same length, different content: a length-only check would miss this.
        record_activity(runtime.store, invocation.activation_id, output=b"bbbb")
        assert deadline() > second
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_polling_incoming_mail_and_invalid_tokens_do_not_refresh_activity(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.invocations[run_id][2] = b'{"session":"native-session"}'
        dispatcher.tick()
        for step in range(1, 4):
            now[0] += 600
            runtime.status()
            runtime.authenticate(invocation.token)  # Authorization inspection is not a tool call.
            runtime.messaging.send(OWNER, ["founder"], "Update", request_id=f"mail-{step}")
            with pytest.raises(FirmError, match="invalid"):
                runtime.tools("invalid-token").peek()
            dispatcher.tick()
        assert len(box.stops) == 1


def test_host_restart_preserves_activity_history_without_resuming_role(system):
    runtime, box, provider = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with SandboxDispatcher(
        runtime,
        box,
        {"codex": provider},
        mcp_url="http://127.0.0.1:9000/mcp",
        execution_timeout=60,
    ) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        now[0] += 50
        box.invocations[run_id][2] = b'{"session":"native-session"}'
        dispatcher.tick()
    with runtime.store.read() as db:
        before = dict(db.execute("SELECT * FROM execution_activity").fetchone())
        deadline = db.execute("SELECT deadline FROM sandbox_runs").fetchone()["deadline"]
    recovered = Runtime(Store.scratch(runtime.store.path, clock=runtime.store.clock))
    with host((recovered, box, provider)) as dispatcher:
        dispatcher.reconcile()
        assert len(box.stops) == 1
        with recovered.store.read() as db:
            assert dict(db.execute("SELECT * FROM execution_activity").fetchone()) == before
            row = db.execute("SELECT deadline,stop_cause FROM sandbox_runs").fetchone()
            assert row["deadline"] == deadline
            assert row["stop_cause"] == "host_restart"


def test_recent_activity_does_not_prevent_owner_hard_stop(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        runtime.tools(invocation.token).peek()
        dispatcher.stop(force=True)
        dispatcher.tick()
        assert not box.live
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["state"] == "interrupted"
        assert result["failure_category"] == "owner_stop"


def test_activity_refreshes_only_the_calling_role(system):
    runtime, box, _ = system
    now = [runtime.store.clock()]
    runtime.store.clock = lambda: now[0]
    with host(system) as dispatcher:
        dispatcher.start()
        founder = launch(dispatcher, runtime)
        runtime.tools(founder.token).hire("analyst", "Do useful work", "hire")
        analyst = launch(dispatcher, runtime)
        now[0] += 1790
        runtime.tools(analyst.token).peek()
        now[0] += 10
        dispatcher.tick()
        with runtime.store.read() as db:
            assert runtime._activation(db, founder.activation_id)["state"] == "failed"
            assert runtime._activation(db, analyst.activation_id)["state"] == "running"
        assert len(box.stops) == 1


def test_stop_rejects_guest_start_during_inflight_launch(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        (reserved,) = dispatcher.tick()
        dispatcher.stop(force=True)
        with pytest.raises(FirmError, match="closed"):
            runtime.begin_execution(reserved.token)
        for future in dispatcher._launches.values():
            future.result(timeout=2)
        dispatcher.tick()
        assert runtime.status()["state"] == "stopped"
        assert box.force_downs
        with runtime.store.read() as db:
            assert runtime.store.role(db, "founder")["birth_delivered"] == 0
            assert runtime.store.role(db, "founder")["attempt_count"] == 0


def test_start_marks_vm_starting_before_slow_sandbox_up_and_stop_wins(system):
    runtime, box, _ = system
    box.block_up = True
    errors = []
    try:
        with host(system) as dispatcher:
            thread = threading.Thread(target=lambda: _record_error(errors, dispatcher.start))
            thread.start()
            assert box.up_started.wait(timeout=2)
            with runtime.store.read() as db:
                state = db.execute("SELECT infrastructure_state FROM execution_host").fetchone()[0]
                assert state == "starting"
            runtime.stop()
            assert runtime.status()["state"] == "draining"
            box.release_up.set()
            thread.join(timeout=2)
            assert len(errors) == 1
            assert isinstance(errors[0], FirmError)
            assert runtime.status()["state"] != "running"
            dispatcher.tick()
            assert runtime.status()["state"] == "stopped"
    finally:
        box.release_up.set()


def _record_error(errors, function):
    try:
        function()
    except BaseException as exc:
        errors.append(exc)


def test_already_exited_gate_refusal_during_drain_is_owner_stop(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        (reserved,) = dispatcher.tick()
        for future in dispatcher._launches.values():
            future.result(timeout=2)
        dispatcher.stop()
        box.gate_refused(next(iter(box.invocations)))
        dispatcher.tick()

        assert runtime.status()["state"] == "stopped"
        with runtime.store.read() as db:
            activation = runtime._activation(db, reserved.activation_id)
            assert activation["failure_category"] == "owner_stop"
            assert runtime.store.role(db, "founder")["attempt_count"] == 0


def test_non_gate_prestart_exit_during_drain_remains_a_failure(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        (reserved,) = dispatcher.tick()
        for future in dispatcher._launches.values():
            future.result(timeout=2)
        dispatcher.stop()
        box.complete(next(iter(box.invocations)), success=False, session=None)
        dispatcher.tick()

        with runtime.store.read() as db:
            activation = runtime._activation(db, reserved.activation_id)
            assert activation["failure_category"] == "unknown"
            assert runtime.store.role(db, "founder")["attempt_count"] == 1


def test_hard_stop_stops_inventoried_launch_before_future_resolves(system):
    runtime, box, _ = system
    box.block_start = True
    box.resurrect_on_release = True
    try:
        with host(system) as dispatcher:
            dispatcher.start()
            (reserved,) = dispatcher.tick()
            assert box.start_created.wait(timeout=2)
            assert not dispatcher._launches[reserved.activation_id].done()

            dispatcher.stop(force=True)
            dispatcher.tick()

            assert box.force_downs
            assert len(runtime.status()["active"]) == 1
            assert runtime.status()["state"] == "stopping"
            assert not box.live  # VM is off, but the unresolved launch retains its reservation.
            with pytest.raises(FirmError, match="pending launch"):
                dispatcher.start()

            box.release_start.set()
            dispatcher._launches[reserved.activation_id].result(timeout=2)
            dispatcher.tick()
            assert runtime.status()["state"] == "stopped"
            assert not box.live
            assert box.force_downs >= 3
    finally:
        box.release_start.set()


def test_provider_setup_failure_closes_admission_without_spending_attempt(system):
    runtime, box, provider = system
    provider.setup_error = "credential contents must never reach the owner error"
    with host(system) as dispatcher:
        dispatcher.start()
        with pytest.raises(FirmError, match="provider setup failed") as failure:
            dispatcher.tick()
        assert "credential contents" not in str(failure.value)
        assert runtime.status()["state"] == "draining"
        assert runtime.status()["active"] == []
        with runtime.store.read() as db:
            assert runtime.store.role(db, "founder")["attempt_count"] == 0

        assert dispatcher.tick() == []
        assert runtime.status()["state"] == "stopped"
        assert not box.live


def test_setup_failure_names_firm_errors_and_only_the_kind_of_others(system):
    # A Firm error is written for the owner, so it is repeated; anything else may
    # hold credential material and only its type is named.
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()

        def fail(*args, **kwargs):
            raise FirmError("workspace provisioning failed: drive is unreachable")

        provider.build_spec = fail
        with pytest.raises(FirmError, match="drive is unreachable") as failure:
            dispatcher.tick()
        assert "provider setup failed (workspace provisioning failed" in str(failure.value)


def test_missing_execution_profile_uses_safe_setup_failure_path(system):
    runtime, box, _ = system
    dispatcher = SandboxDispatcher(runtime, box, {}, mcp_url="http://127.0.0.1:9000/mcp")
    with dispatcher:
        dispatcher.start()
        with pytest.raises(FirmError, match="provider setup failed"):
            dispatcher.tick()
        assert runtime.status()["state"] == "draining"
        assert runtime.status()["active"] == []
        dispatcher.tick()
        assert runtime.status()["state"] == "stopped"


def test_hard_stop_with_dead_network_waits_for_host_confirmation(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        box.unavailable = True
        box.refuse_down = True
        dispatcher.stop(force=True)
        with pytest.raises(FirmError, match="VM shutdown"):
            dispatcher.tick()
        assert runtime.status()["state"] == "stopping"
        assert len(runtime.status()["active"]) == 1
        box.refuse_down = False
        dispatcher.tick()
        assert runtime.status()["state"] == "stopped"
        assert not box.live
        assert box.force_downs == 2


def test_dead_network_during_drain_never_forces_vm_down(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        box.unavailable = True
        dispatcher.stop()
        with pytest.raises(OSError, match="engine unavailable"):
            dispatcher.tick()
        assert runtime.status()["state"] == "draining"
        assert box.live
        assert box.force_downs == 0
        box.unavailable = False
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_graceful_shutdown_requires_mac_cleanup_but_force_stop_does_not(system, monkeypatch):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()

        def unclean():
            assert box.live
            raise FirmError("Mac release is not confirmed")

        monkeypatch.setattr(runtime.mac, "drain", unclean)
        dispatcher.stop()
        with pytest.raises(FirmError, match="Mac release"):
            dispatcher.tick()
        assert box.live and box.force_downs == 0
        dispatcher.stop(force=True)
        dispatcher.tick()
        assert not box.live and box.force_downs == 1


def test_force_stop_does_not_query_an_unreachable_mac_station(system, monkeypatch):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        dispatcher.stop(force=True)

        def unreachable():
            raise AssertionError("Mac lease housekeeping must not run before forced VM shutdown")

        monkeypatch.setattr(runtime.mac, "refresh", unreachable)
        dispatcher.tick()
        assert not box.live and box.force_downs == 1


def test_mac_lease_reaches_the_sandbox_by_role_id_and_the_relay_by_lease_id(system):
    """Captures land in the role's workspace through the sandbox's file server."""
    runtime, box, _ = system
    acquired, connected = [], []

    class Connection:
        def usable(self):
            return True

        def request(self, op, **arguments):
            if op == "capture_window":
                return {"status": "ok", "png_base64": "iVBORw0K", **arguments}
            if op == "list_apps":
                return {"status": "ok", "apps": [{"pid": 7, "name": "TextEdit"}]}
            return {"status": "ok", "ready": True}

        def close(self):
            pass

    def mac_acquire(role_id, activation_id):
        acquired.append((role_id, activation_id))
        return {"lease_id": "lease-1", "station": 2, "ssh_host": "127.0.0.1",
                "ssh_port": 2223, "user": "firmstation2",
                "drive": "/Users/firmstation2/drive",
                "project_dir": "/Users/firmstation2/drive/org/founder/work",
                "scratch": "/Users/firmstation2/scratch"}

    def mac_connect(lease_id):
        connected.append(lease_id)
        return Connection()

    box.mac_acquire, box.mac_connect = mac_acquire, mac_connect
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        granted = runtime.mac.acquire(invocation.token)
        assert granted["status"] == "granted" and granted["station"] == 2
        assert runtime.mac.call(invocation.token, "status")["ready"] is True
        assert acquired == [(sandbox_role_id(invocation.employee_id), invocation.activation_id)]
        assert sandbox_role_id(invocation.employee_id) != invocation.employee_id
        assert connected == ["lease-1"]
        captured = runtime.mac.call(invocation.token, "capture_window", pid=7, window_id=3)
        workspace = box.file_server.workspace_path(sandbox_role_id(invocation.employee_id))
        assert captured["drive_path"].startswith(workspace + "/captures/")
        assert captured["drive_path"].endswith("-textedit.png")
        assert captured["mac_path"] == "/Users/firmstation2/drive/" + captured["drive_path"]
        assert (box.file_server.tree / captured["drive_path"]).read_bytes() == b"\x89PNG\r\n"
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_host_restart_interrupts_owned_execution_before_new_admission(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        first = launch(dispatcher, runtime)
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)) as dispatcher:
        dispatcher.start()
        assert len(box.invocations) == 1
        assert len(box.stops) == 1
        with reopened.store.read() as db:
            row = reopened._activation(db, first.activation_id)
            assert row["state"] == "interrupted"
            assert row["failure_category"] == "host_restart"


def test_grants_that_cannot_be_applied_at_restart_power_off_surviving_runs(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
    reopened = Runtime(Store.scratch(runtime.store.path))

    def refuse(hosts):
        raise RuntimeError("Allowed hosts change incomplete: linux: not confirmed")

    box.set_allowed_hosts = refuse
    assert any(state[1] for state in box.invocations.values())  # survived the old host
    with host((reopened, box, provider)) as dispatcher:
        with pytest.raises(FirmError, match="could not be applied.*powered off"):
            dispatcher.start()
    assert box.live is False and box.force_downs == 1
    assert not any(state[1] for state in box.invocations.values())
    with reopened.store.read() as db:
        assert db.execute("SELECT infrastructure_state FROM execution_host").fetchone()[0] == (
            "unknown"
        )


def test_unavailable_engine_does_not_release_slots_or_kill_other_execution(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        box.unavailable = True
        with pytest.raises(OSError, match="unavailable"):
            dispatcher.tick()
        assert len(runtime.status()["active"]) == 1
        assert not box.stops
        box.unavailable = False
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_lost_start_response_is_reconciled_by_immutable_run_id(system):
    runtime, box, _ = system
    box.start_error_after_create = True
    with host(system) as dispatcher:
        dispatcher.start()
        (invocation,) = dispatcher.tick()
        for future in dispatcher._launches.values():
            with pytest.raises(OSError):
                future.result(timeout=2)
        runtime.begin_execution(invocation.token)
        dispatcher.tick()
        assert len(runtime.status()["active"]) == 1
        assert len(box.invocations) == 1
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_grants_separate_private_workspace_and_name_no_other_line(system):
    runtime, _, _ = system
    manager = runtime.lifecycle.hire("founder", "engineering", "Build", "codex", request_id="eng")
    peer = runtime.lifecycle.hire("founder", "finance", "Bill", "codex", request_id="fin")
    analyst = runtime.lifecycle.hire(
        manager["employee_id"], "api", "Build API", "codex", request_id="api"
    )
    grants = grants_for(runtime.store, analyst["employee_id"], Layout())
    assert grants.organization.address == "founder/engineering/api"
    assert grants.references["workspace"] == (
        "/home/firmrole/drive/org/founder/engineering/api/work"
    )
    assert peer["address"] not in str(grants.references)
    assert grants.references["private"] == Layout().private


@pytest.mark.parametrize("configured", [False, True])
def test_launch_adds_github_helper_and_identity_only_with_github(system, monkeypatch, configured):
    from firm_runtime import github

    runtime, box, _ = system
    if configured:
        monkeypatch.setattr(
            github,
            "connect",
            lambda settings: {
                "app_id": 1,
                "organization": "firmery",
                "installation_id": 42,
                "bot_login": "firmery-roles[bot]",
                "bot_user_id": 777,
            },
        )
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
        runtime.owner.configure(
            OWNER,
            {**config, "github": {"organization": "firmery"}},
        )
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    spec = box.specs[sandbox_run_id(invocation.activation_id)]
    assert ("github-credential" in spec.secrets) is configured
    assert invocation.context["capabilities"]["github"] is configured
    if configured:
        assert spec.env["FIRM_GITHUB_URL"] == "http://127.0.0.1:9000/github/token"
        assert spec.env["GIT_AUTHOR_NAME"] == spec.env["GIT_COMMITTER_NAME"] == "founder"
        assert spec.env["GIT_COMMITTER_EMAIL"] == "777+firmery-roles[bot]@users.noreply.github.com"
    else:
        assert not {"FIRM_GITHUB_URL", "GIT_AUTHOR_NAME"} & set(spec.env)


@pytest.mark.parametrize("configured", [False, True])
def test_launch_adds_the_image_key_only_with_google_cloud(system, tmp_path, configured):
    runtime, box, _ = system
    key = tmp_path / "demo-game.key"
    key.write_text("AIzaSyExampleKeyForTestsOnly0123456789\n")
    key.chmod(0o600)
    if configured:
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
        runtime.owner.configure(
            OWNER,
            {**config, "google_cloud": {"project": "demo-game", "api_key_file": str(key)}},
        )
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    spec = box.specs[sandbox_run_id(invocation.activation_id)]
    assert invocation.context["capabilities"]["google_cloud"] == (
        {"project": "demo-game"} if configured else None
    )
    assert str(key) not in json.dumps(invocation.context)
    assert "AIzaSyExampleKeyForTestsOnly" not in json.dumps(invocation.context)
    if configured:
        assert spec.secrets["google-cloud-key"] == b"AIzaSyExampleKeyForTestsOnly0123456789"
        assert spec.secret_env["GEMINI_API_KEY"] == "google-cloud-key"
        assert spec.env["GOOGLE_CLOUD_PROJECT"] == "demo-game"
    else:
        assert "google-cloud-key" not in spec.secrets
        assert not {"GEMINI_API_KEY"} & set(spec.secret_env)
        assert "GOOGLE_CLOUD_PROJECT" not in spec.env


@pytest.mark.parametrize("mac_source", ["none", "image", "durable_vm"])
def test_launch_describes_the_mac_available_to_the_sandbox(system, mac_source):
    from dataclasses import replace

    runtime, box, _ = system
    configured = mac_source != "none"
    box.config = replace(
        box.config, mac_image=str(box.config.root / "image") if mac_source == "image" else None
    )
    box.mac = object() if configured else None
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    assert invocation.context["capabilities"]["mac"] is configured


def test_single_host_covers_database_and_sandbox(system, tmp_path):
    runtime, box, provider = system
    with host(system), pytest.raises(FirmError, match="another"):
        with host(system):
            pass


def test_rejected_second_host_preserves_live_workspace_storage(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        founder = launch(dispatcher, runtime)
        with pytest.raises(FirmError, match="another"):
            with host(system):
                pass
        assert runtime.discovery.files is box.file_server
        child = runtime.tools(founder.token).hire("new-report", "Do work", "hire")
        assert child["employee_id"] == "e1"
        assert (box.file_server.tree / box.file_server.workspace_path("r-e1")).is_dir()
    assert runtime.lifecycle.provision_role is None
    assert runtime.discovery.files is None


def test_role_stop_terminates_inflight_run_before_launch_call_resolves(system):
    runtime, box, _ = system
    box.block_start = True
    try:
        with host(system) as dispatcher:
            dispatcher.start()
            (reserved,) = dispatcher.tick()
            assert box.start_created.wait(timeout=2)
            runtime.owner.stop_role(OWNER, reserved.employee_id, "stop-role")
            dispatcher.tick()
            assert box.stops
            assert len(runtime.status()["active"]) == 1
            assert runtime.status()["state"] == "running"
            with pytest.raises(FirmError, match="confirm.*stopped"):
                runtime.owner.route(OWNER, reserved.employee_id, "codex", "premature-route")
            with pytest.raises(FirmError):
                runtime.begin_execution(reserved.token)
            box.release_start.set()
            dispatcher._launches[reserved.activation_id].result(timeout=2)
            dispatcher.tick()
            assert runtime.status()["active"] == []
            with runtime.store.read() as db:
                assert runtime.store.role(db, reserved.employee_id)["attempt_count"] == 0
                assert runtime.store.role(db, reserved.employee_id)["employment"] == "paused"
    finally:
        box.release_start.set()


def test_stop_during_provider_setup_cancels_unlaunched_reservation(system):
    runtime, box, provider = system
    build = provider.build_spec

    def stopped(*args, **kwargs):
        spec = build(*args, **kwargs)
        runtime.owner.stop_role(OWNER, "founder", "stop-during-setup")
        return spec

    provider.build_spec = stopped
    with host(system) as dispatcher:
        dispatcher.start()
        assert dispatcher.tick() == []
        assert box.invocations == {}
        assert runtime.status()["active"] == []
        with runtime.store.read() as db:
            assert runtime.store.role(db, "founder")["attempt_count"] == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_log_timeout_keeps_reservation_and_allows_other_roles_to_reconcile(
    system, monkeypatch, terminal
):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        founder = launch(dispatcher, runtime)
        runtime.tools(founder.token).hire("analyst", "Work", "hire")
        analyst = launch(dispatcher, runtime)
        first_run, second_run = list(box.invocations)
        # Give the founder's log something new to fetch, so the read below is
        # genuinely attempted (and fails) instead of being skipped as unchanged.
        box.invocations[first_run][2] = b"partial output\n"
        box.complete(second_run)
        if terminal:
            box.complete(first_run)
        original = box.logs

        def logs(run, *, offset=0, until=None):
            if run.run_id == first_run:
                raise subprocess.TimeoutExpired("guest logs", 10)
            return original(run, offset=offset, until=until)

        monkeypatch.setattr(box, "logs", logs)
        with runtime.store.read() as db:
            deadline = db.execute(
                "SELECT deadline FROM sandbox_runs WHERE activation_id=?", (founder.activation_id,)
            ).fetchone()[0]
        dispatcher.tick()
        with runtime.store.read() as db:
            assert runtime._activation(db, founder.activation_id)["state"] == "running"
            assert runtime._activation(db, analyst.activation_id)["state"] == "completed"
            assert (
                db.execute(
                    "SELECT deadline FROM sandbox_runs WHERE activation_id=?",
                    (founder.activation_id,),
                ).fetchone()[0]
                == deadline
            )
        # Missing logs do not reset inactivity; confirmed stop can still settle it.
        if not terminal:
            runtime.store.clock = lambda: deadline + 1
            dispatcher.tick()
            with runtime.store.read() as db:
                assert (
                    runtime._activation(db, founder.activation_id)["failure"]
                    == "execution inactivity timeout expired"
                )
        else:
            monkeypatch.setattr(box, "logs", original)
            dispatcher.tick()
            with runtime.store.read() as db:
                assert runtime._activation(db, founder.activation_id)["state"] == "completed"


def test_idle_running_roles_cost_no_extra_calls_per_tick(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        founder = launch(dispatcher, runtime)
        runtime.tools(founder.token).hire("analyst", "Work", "hire")
        launch(dispatcher, runtime)
        box.status_calls = box.inventory_calls = 0
        box.log_calls.clear()
        dispatcher.tick()
        # One inventory call carries both roles' status; neither one is idle-polled
        # again, and neither has produced anything new for a log call to fetch.
        assert box.inventory_calls == 1
        assert box.status_calls == 0
        assert box.log_calls == []
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_a_role_with_new_output_is_read_from_its_remembered_offset(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        # A session established up front keeps later, non-JSON growth from being
        # eagerly re-parsed as a session probe.
        first_chunk = b'{"session": "native-session"}'
        box.invocations[run_id][2] = first_chunk
        box.log_calls.clear()
        dispatcher.tick()
        assert box.log_calls == [(run_id, 0)]
        box.invocations[run_id][2] += b"more output, not itself valid json"
        box.log_calls.clear()
        dispatcher.tick()
        assert box.log_calls == [(run_id, len(first_chunk))]
        box.log_calls.clear()
        dispatcher.tick()
        assert box.log_calls == []
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_accumulated_log_across_ticks_equals_the_full_log(system):
    runtime, box, provider = system
    seen = []

    def parse_result(logs, exit_code):
        seen.append(logs)
        return SimpleNamespace(provider_session_id=None, success=exit_code == 0, error=None)

    provider.parse_result = parse_result
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.invocations[run_id][2] = b"chunk one "
        dispatcher.tick()
        box.invocations[run_id][2] += b"chunk two "
        dispatcher.tick()
        full_log = box.invocations[run_id][2] + b"chunk three"
        box.invocations[run_id][1] = False
        box.invocations[run_id][2] = full_log
        box.invocations[run_id][3] = 0
        dispatcher.tick()
    assert seen[-1] == full_log
    with runtime.store.read() as db:
        assert runtime._activation(db, invocation.activation_id)["state"] == "completed"


def test_a_fresh_dispatcher_reads_an_established_log_from_zero_once(system):
    runtime, box, _ = system
    dispatcher = host(system)
    with dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.invocations[run_id][2] = b'{"partial": true}'
        dispatcher.tick()
        assert invocation.activation_id in dispatcher._logs
    # The process closed without a clean stop; the offset it had buffered must
    # not survive into the next time this same dispatcher opens.
    assert dispatcher._logs == {}
    box.log_calls.clear()
    with dispatcher:
        row = dispatcher._records(active_only=True)[0]
        run, info = box.inventory()[run_id]
        logs = dispatcher._read_logs(row, run, info)
        assert logs == box.invocations[run_id][2]
        assert box.log_calls == [(run_id, 0)]
        dispatcher.stop(force=True)
        dispatcher.tick()


def test_log_buffer_is_dropped_on_host_restart_without_closing(system):
    """The same cleanup the fresh-dispatcher case relies on also runs on its own:

    a row already carrying ``host_restart`` (as a real reopen would leave it)
    is settled, and its buffer forgotten, without the dispatcher ever closing.
    """
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.invocations[run_id][2] = b'{"partial": true}'
        dispatcher.tick()
        assert invocation.activation_id in dispatcher._logs
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE sandbox_runs SET stop_cause='host_restart' WHERE activation_id=?",
                (invocation.activation_id,),
            )
        dispatcher.reconcile()
        assert invocation.activation_id not in dispatcher._logs
        with runtime.store.read() as db:
            result = runtime._activation(db, invocation.activation_id)
        assert result["failure_category"] == "host_restart"


def test_log_buffer_is_dropped_once_the_run_finishes(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        assert invocation.activation_id in dispatcher._logs
        box.complete(run_id)
        dispatcher.tick()
        assert invocation.activation_id not in dispatcher._logs


def test_log_buffer_is_dropped_when_a_run_goes_missing_from_inventory(system):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        assert invocation.activation_id in dispatcher._logs
        del box.invocations[run_id]
        dispatcher.reconcile()
        assert invocation.activation_id not in dispatcher._logs
    with runtime.store.read() as db:
        assert runtime._activation(db, invocation.activation_id)["state"] == "interrupted"


def test_malformed_codex_message_finishes_as_safe_failure(system, monkeypatch):
    from firm_runtime.codex_provider import parse_result

    runtime, box, provider = system
    monkeypatch.setattr(provider, "parse_result", parse_result)
    with host(system) as dispatcher:
        dispatcher.start()
        actor = launch(dispatcher, runtime)
        run_id = next(iter(box.invocations))
        box.complete(run_id)
        box.invocations[run_id][2] = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": str(uuid4())},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": {"private": "secret"}},
                },
                {"type": "turn.completed"},
            ]
        ).encode()
        dispatcher.tick()
        with runtime.store.read() as db:
            result = runtime._activation(db, actor.activation_id)
        assert result["state"] == "failed"
        assert result["failure"] == "provider: invalid_output"


def test_host_restart_stops_owned_execution_without_reading_tokens(system, monkeypatch):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    run = next(iter(box.invocations.values()))[0]
    monkeypatch.setattr(
        box, "logs", lambda _run, offset=0: (_ for _ in ()).throw(OSError("unavailable"))
    )
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)) as dispatcher:
        dispatcher.start()
        assert box.stops == [run]
        assert not box.invocations[run.run_id][1]
        with reopened.store.read() as db:
            row = reopened._activation(db, invocation.activation_id)
            assert row["state"] == "interrupted"
            assert row["failure_category"] == "host_restart"
        assert len(box.invocations) == 1


def test_restart_stop_keeps_reservation_until_cleanup_is_confirmed(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)) as dispatcher:
        box.refuse_stop = True
        with pytest.raises(FirmError, match="has not confirmed cleanup"):
            dispatcher.reconcile()
        assert len(reopened.status()["active"]) == 1
        with reopened.store.read() as db:
            row = db.execute(
                "SELECT stop_cause FROM sandbox_runs WHERE activation_id=?",
                (invocation.activation_id,),
            ).fetchone()
            assert row["stop_cause"] == "host_restart"
        box.refuse_stop = False
        dispatcher.reconcile()
        with reopened.store.read() as db:
            assert reopened._activation(db, invocation.activation_id)["state"] == "interrupted"


@pytest.mark.parametrize("cause", ["owner_stop", "timeout"])
def test_host_restart_preserves_existing_stop_cause(system, cause):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        invocation = launch(dispatcher, runtime)
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE sandbox_runs SET stop_cause=? WHERE activation_id=?",
                (cause, invocation.activation_id),
            )
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)):
        with reopened.store.read() as db:
            row = db.execute(
                "SELECT stop_cause FROM sandbox_runs WHERE activation_id=?",
                (invocation.activation_id,),
            ).fetchone()
            assert row["stop_cause"] == cause


def test_restart_refuses_unknown_execution_before_stopping_owned_runs(system):
    runtime, box, provider = system
    with host(system) as dispatcher:
        dispatcher.start()
        launch(dispatcher, runtime)
    unknown = box.start(RunSpec("unregistered", ("analyst",), run_id="unknown-run"))
    reopened = Runtime(Store.scratch(runtime.store.path))
    with host((reopened, box, provider)) as dispatcher:
        with pytest.raises(FirmError, match="not registered"):
            dispatcher.reconcile()
        assert not box.stops
        assert box.invocations[unknown.run_id][1]
        assert len(reopened.status()["active"]) == 1


def test_dispatcher_restart_recovers_tasks_only_after_confirmed_stop(system, monkeypatch):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        caller = launch(dispatcher, runtime)
        with runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,kind,"
                "caller_activation_id) VALUES('task-orphan',?,'running',1,'task',?)",
                (runtime.store.clock(), caller.activation_id),
            )
    confirmations = [False, True]
    stopped = []

    def stop_child(run_id):
        stopped.append(run_id)
        return {"confirmed": confirmations.pop(0)}

    monkeypatch.setattr(box, "stop_child", stop_child, raising=False)
    with host(system) as restarted:
        restarted.reconcile()
        with runtime.store.read() as db:
            assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
        restarted.reconcile()
        with runtime.store.read() as db:
            assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "unavailable"
    assert stopped == ["task-orphan", "task-orphan"]


def test_dispatcher_hard_stop_recovers_tasks_only_after_vm_proof(system, monkeypatch):
    runtime, box, _ = system
    with host(system) as dispatcher:
        dispatcher.start()
        caller = launch(dispatcher, runtime)
        with runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,kind,"
                "caller_activation_id) VALUES('task-orphan',?,'running',1,'task',?)",
                (runtime.store.clock(), caller.activation_id),
            )
        observed = box.infrastructure_status
        monkeypatch.setattr(box, "infrastructure_status", lambda: {"running": None})
        dispatcher.stop(force=True)
        with pytest.raises(FirmError, match="shutdown is not confirmed"):
            dispatcher.tick()
        with runtime.store.read() as db:
            assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
        monkeypatch.setattr(box, "infrastructure_status", observed)
        dispatcher.tick()
        with runtime.store.read() as db:
            assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "unavailable"


class TaskProviders(dict):
    def __init__(self, runtime, provider):
        super().__init__(codex=provider)
        self.runtime = runtime

    def task_runner(self, sandbox, *, mcp_url):
        from firm_runtime.task_runner import GuestTaskRunner

        return GuestTaskRunner(
            self.runtime, sandbox, build_spec=lambda request, parent: RunSpec(
                parent.role_id, ("sh", "-c", "printf answer"),
            ),
        )


def test_dispatcher_opens_provider_task_runner_and_closes_with_host(system):
    from firm_runtime.task_runner import GuestTaskRunner

    runtime, box, provider = system
    assert runtime.task_service.runner is None
    with SandboxDispatcher(runtime, box, TaskProviders(runtime, provider),
                           mcp_url="http://unused") as host:
        assert isinstance(runtime.task_service.runner, GuestTaskRunner)
        assert host._task_runner is runtime.task_service.runner
    assert runtime.task_service.runner is None


def test_task_launch_gate_closes_before_emergency_poweroff_without_guest_wait(system, monkeypatch):
    runtime, box, provider = system
    with SandboxDispatcher(runtime, box, TaskProviders(runtime, provider),
                           mcp_url="http://unused") as host:
        host.start()
        runner = host._task_runner
        close = runner.close
        down = box.down

        def poweroff(**options):
            assert runner.closed
            return down(**options)

        def join_after_poweroff():
            assert box.infrastructure_status()["running"] is False
            close()

        monkeypatch.setattr(box, "down", poweroff)
        monkeypatch.setattr(runner, "close", join_after_poweroff)
        host.stop(force=True)
        host.tick()
        host.start()
        assert host._task_runner is not runner
        assert not host._task_runner.closed
