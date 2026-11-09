import json
import sqlite3
import sys
import time

import pytest
from starlette.testclient import TestClient

from firm_runtime import analysis_history, cli
from firm_runtime.analysis import (
    SECTIONS,
    AnalysisNotFound,
    AnalysisQueryError,
    AnalysisReader,
    disk_state,
)
from firm_runtime.analysis_history import RESOURCE_INTERVAL
from firm_runtime.console import build_console_app
from firm_runtime.errors import FirmError
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.runtime import Runtime
from firm_runtime.store import OPERATIONS, OWNER, Store
from firm_runtime.summaries import ContextSnapshot, SummaryOutput


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "durable" / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "analysis-test",
        "Build things",
        {
            "max_active": 4,
            "staffing": False,
            "founder_profile": "main",
            "accounts": {"a": {"provider": "codex", "max_active": 4, "auth_file": "/secret/auth"}},
            "profiles": {
                "main": {
                    "provider": "codex",
                    "model": "captured-model",
                    "effort": "low",
                    "accounts": ["a"],
                    "context_window": 10000,
                    "handoff_reserve": 5000,
                }
            },
        },
    )
    return runtime, now, AnalysisReader(runtime.store.path, clock=lambda: now[0])


def start(runtime):
    runtime.start()
    invocation = runtime.admit()
    assert invocation
    runtime.mark_started(invocation.activation_id)
    return invocation


def read(reader, section=None, **kwargs):
    return reader.read(section, from_at=0, until=10000, **kwargs)


def dump(runtime):
    with runtime.store.read() as db:
        return list(db.iterdump())


def test_usage_and_timeline_label_sessions_with_current_public_names(system):
    runtime, _, reader = system
    invocation = start(runtime)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=? WHERE employee_id='founder'",
            ("# Profile\n\n**Public name:** Wren Calloway\nPrivate profile detail",),
        )
    before = dump(runtime)
    for section in ("usage", "timeline"):
        data = read(reader, section)
        assert data["role_labels"] == {
            "founder": {"address": "founder", "identity": "Wren Calloway"}
        }
        assert "Private profile detail" not in json.dumps(data)
        if section == "usage":
            session = next(row for row in data["rows"] if row["type"] == "session")
            assert session["employee_id"] == "founder"
            assert session["session_id"] == invocation.session_id
    assert dump(runtime) == before


def test_read_only_export_missing_storage_and_secret_allowlist(system, monkeypatch, capsys):
    runtime, now, reader = system
    invocation = start(runtime)
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO execution_incidents "
            "VALUES('expired','account','a','quota',900,800,NULL,NULL)"
        )
        analysis_history.record(db, 1001, "role.held", "founder", secret="must-not-export")
    before = dump(runtime)

    def forbidden(*args, **kwargs):
        raise AssertionError("read attempted runtime/provider operation")

    monkeypatch.setattr(runtime.accounts, "select", forbidden)
    monkeypatch.setattr(runtime.accounts, "_incident", forbidden)
    monkeypatch.setattr(cli, "Runtime", forbidden)
    data = read(reader)
    output = json.dumps(data)
    for secret in (
        "/secret/auth",
        "must-not-export",
        invocation.token,
        "account_json",
        "context_json",
    ):
        assert secret not in output
    assert dump(runtime) == before
    monkeypatch.setattr(
        sys, "argv", ["firm", "analyze", str(runtime.store.path.parent.parent), "--json"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["firm_id"] == "analysis-test"
    assert dump(runtime) == before
    missing = runtime.store.path.parent / "missing.sqlite3"
    with pytest.raises(FirmError, match="Cannot read"):
        AnalysisReader(missing).read()
    assert not missing.exists()


@pytest.mark.parametrize("table", ["analysis_events", "summary_runs", "resource_samples"])
def test_analysis_rejects_missing_required_tables_without_repair(system, table):
    runtime, _, reader = system
    with runtime.store.transaction() as db:
        db.execute(f"DROP TABLE {table}")
    before = dump(runtime)
    with pytest.raises(FirmError, match=f"no such table: {table}"):
        read(reader)
    assert dump(runtime) == before


def test_resource_machine_names_are_not_constrained_by_the_database(system):
    runtime, _, _ = system
    with runtime.store.transaction() as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='resource_samples'"
        ).fetchone()[0]
        assert "CHECK" not in sql
        analysis_history.record_resources(db, 1000, "future-mac", {"boot": "one"})


def test_empty_analysis_history_reports_unknown_coverage(system):
    runtime, _, reader = system
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM analysis_events")
    coverage = read(reader)["coverage"]
    assert coverage["journal_recording_from"] is None
    assert coverage["last_host_observation"] is None
    assert coverage["recorded_gaps"] == 0


def test_usage_resumed_session_partial_counters_and_identity(system):
    runtime, now, reader = system
    first = start(runtime)
    usage = ProviderUsage(
        input_tokens=100,
        output_tokens=30,
        cached_input_tokens=20,
        session_input_tokens=700,
        source="test",
    )
    runtime.record_usage(first.activation_id, usage)
    runtime.record_usage(first.activation_id, usage)
    now[0] = 1010
    runtime.finish(first.activation_id)
    runtime.messaging.send(OWNER, ["founder"], "More", request_id="more")
    now[0] = 1100
    second = runtime.admit()
    runtime.mark_started(second.activation_id)
    assert second.session_id == first.session_id
    runtime.record_usage(second.activation_id, ProviderUsage(input_tokens=50, source="test"))
    with runtime.store.transaction() as db:
        config = json.loads(db.execute("SELECT config_json FROM firm").fetchone()[0])
        config["profiles"]["main"]["model"] = "new-model"
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    data = read(reader, "usage")
    assert data["totals"]["input_tokens"]["value"] == 150
    assert data["totals"]["output_tokens"] == {
        "value": 30,
        "eligible": 2,
        "measured": 1,
        "complete": False,
    }
    model = next(r for r in data["rows"] if r["type"] == "model")
    assert model["label"] == ["codex", "captured-model"]
    assert model["resolved_models"] == [None]
    assert model["missing_resolved_model_identity"] == 2
    session = next(r for r in data["rows"] if r["type"] == "session")
    assert session["cumulative_input_tokens"] == 750
    later = reader.read("usage", from_at=1050, until=1200)
    assert later["sessions_used"] == 1 and later["sessions_first_admitted"] == 0
    assert later["totals"]["input_tokens"]["value"] == 50


def test_usage_resolved_models_follow_execution_evidence_and_window(system):
    from firm_runtime.claude_provider import parse_usage

    runtime, now, reader = system
    # Use a captured Claude alias, with real parser output flowing into storage.
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["accounts"]["a"]["provider"] = "claude"
        config["profiles"]["main"].update(provider="claude", model="opus")
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    first = start(runtime)
    current = first
    for index, model in enumerate(("claude-opus-4-1", "claude-opus-4-6", None)):
        if index:
            runtime.finish(current.activation_id)
            runtime.messaging.send(OWNER, ["founder"], "More", request_id=f"more-{index}")
            now[0] += 100
            current = runtime.admit()
            runtime.mark_started(current.activation_id)
            assert current.session_id == first.session_id
        event = {"type": "result", "model": model, "usage": {"input_tokens": 10}}
        runtime.record_usage(current.activation_id, parse_usage(json.dumps(event)))
    # An execution without any usage evidence is also unknown.
    runtime.finish(current.activation_id)
    runtime.messaging.send(OWNER, ["founder"], "More", request_id="unmeasured")
    now[0] += 100
    current = runtime.admit()
    runtime.mark_started(current.activation_id)
    before = dump(runtime)
    report = read(reader, "usage")
    assert report["missing_model_identity"] == 0
    assert report["missing_resolved_model_identity"] == 2
    for row in report["rows"]:
        if row["type"] in {"role", "model", "session", "workload"}:
            assert row["resolved_models"] == ["claude-opus-4-1", "claude-opus-4-6", None]
            assert row["missing_resolved_model_identity"] == 2
    model = next(r for r in report["rows"] if r["type"] == "model")
    assert model["label"] == ["claude", "opus"]
    session = next(r for r in report["rows"] if r["type"] == "session")
    assert session["model"] == "opus" and session["execution_profile"] == "main"
    assert session["employee_id"] == "founder"
    later = reader.read("usage", from_at=1050, until=1400, row_type="session", limit=1)
    assert later["rows"][0]["resolved_models"] == ["claude-opus-4-6", None]
    assert later["missing_resolved_model_identity"] == 2
    assert later["role_labels"]["founder"]["address"] == "founder"
    assert any("last non-empty" in limit for limit in later["coverage"]["limits"])
    assert dump(runtime) == before


@pytest.mark.parametrize("table", ["activation_usage", "summary_runs"])
def test_old_schema_is_not_upgraded_or_silently_treated_as_unknown(system, table):
    runtime, _, reader = system
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes) "
            "VALUES('old-summary',1000,'summarized',0)"
        )
        db.execute(f"ALTER TABLE {table} DROP COLUMN resolved_model")
    before = dump(runtime)
    Runtime(Store.scratch(runtime.store.path))
    assert dump(runtime) == before
    with pytest.raises(FirmError, match=r"no such column: (t\.)?resolved_model"):
        read(reader, "usage")
    assert dump(runtime) == before


def test_first_start_is_counted_even_when_reservation_precedes_window(system):
    runtime, now, reader = system
    runtime.start()
    invocation = runtime.admit()
    now[0] = 1010
    runtime.mark_started(invocation.activation_id)
    data = reader.read("usage", from_at=1005, until=1020)
    assert data["sessions_first_admitted"] == 0
    assert data["sessions_first_started"] == 1
    assert data["sessions_used"] == 0  # Usage still selects admissions, not starts.


@pytest.mark.parametrize(
    "failure",
    ["execution inactivity timeout expired", "provider: process_exit", "provider: result_error"],
)
def test_timeline_recorded_failure_reason_is_read_only(system, failure):
    runtime, now, reader = system
    invocation = start(runtime)
    now[0] = 1010
    runtime.finish(invocation.activation_id, "failed", failure=failure)
    before = dump(runtime)

    data = read(reader, "timeline", row_type="activation")

    assert data["rows"][0]["failure_category"] == "unknown"
    assert data["rows"][0]["failure"] == failure
    assert dump(runtime) == before


def test_timeline_overlap_handoff_failure_and_clock_anomaly(system):
    runtime, now, reader = system
    first = start(runtime)
    assert reader.read("timeline", from_at=9000, until=10000)["rows"] == []
    child = runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")[
        "employee_id"
    ]
    now[0] = 1010
    second = runtime.admit()
    runtime.mark_started(second.activation_id)
    runtime.tools(first.token).handoff("secret notes", "wait", "handoff")
    now[0] = 1020
    runtime.finish(first.activation_id)
    now[0] = 1030
    runtime.finish(second.activation_id, "interrupted", category="host_restart")
    data = reader.read("timeline", from_at=1005, until=1040)
    assert data["peak_observed_concurrency"] == 2
    assert any(r.get("kind") == "activation.handoff_saved" for r in data["rows"])
    first_row = next(r for r in data["rows"] if r["id"] == "activation:" + first.activation_id)
    assert first_row["clipped_from"] == 1005 and first_row["started_at"] == 1000
    assert "secret notes" not in json.dumps(data)
    second_row = next(
        r for r in data["rows"] if r.get("employee_id") == child and r["type"] == "activation"
    )
    assert second_row["time_quality"] == "observed"
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE activations SET finished_at=999 WHERE activation_id=?", (first.activation_id,)
        )
    data = read(reader, "timeline")
    first_row = next(r for r in data["rows"] if r["id"] == "activation:" + first.activation_id)
    assert first_row["duration_seconds"] is None


def test_deliveries_claim_latency_old_backlog_and_bounded_detail(system):
    runtime, now, reader = system
    first = start(runtime)
    children = [
        runtime.lifecycle.hire("founder", f"analyst{i}", "Work", "main", request_id=f"hire{i}")[
            "employee_id"
        ]
        for i in range(2)
    ]
    old = runtime.messaging.send(OWNER, [children[0]], "old", request_id="old")
    now[0] = 1100
    message = runtime.messaging.send(
        OWNER, ["founder", *children], "<script>literal</script>", request_id="multi"
    )
    now[0] = 1120
    runtime.tools(first.token).claim("claim", [message["message_id"]])
    data = reader.read("conversations", from_at=1050, until=1200)
    assert data["message_count"] == 1 and data["delivery_count"] == 3
    assert data["claim_delay"]["median_seconds"] == 20
    assert data["claim_delay"]["samples"] == 1
    assert data["pending_deliveries_from_before_window"] == 1
    assert any(r.get("message_id") == old["message_id"] for r in data["rows"])
    waiting = {r["employee_id"]: r["pending_mail"] for r in read(reader, "readiness")["rows"]}
    assert waiting["founder"] is None
    assert waiting[children[0]] == {"deliveries": 2, "oldest_age_seconds": 120}
    assert waiting[children[1]] == {"deliveries": 1, "oldest_age_seconds": 20}
    conversation = next(r for r in data["rows"] if r["type"] == "conversation")
    detail = read(
        reader, "conversations", conversation_id=conversation["conversation_id"], limit=1
    )
    assert detail["rows"][0]["body"] == "<script>literal</script>"


def test_claims_split_by_whether_the_work_period_had_already_started(system):
    runtime, now, reader = system
    now[0] = 900
    waiting = runtime.messaging.send(OWNER, ["founder"], "before", request_id="before")
    now[0] = 1000
    invocation = start(runtime)
    now[0] = 1100
    during = runtime.messaging.send(OWNER, ["founder"], "during", request_id="during")
    now[0] = 1150
    runtime.tools(invocation.token).claim("claim", [waiting["message_id"], during["message_id"]])
    split = read(reader, "conversations")["claims_by_arrival"]
    assert split["while_running"]["samples"] == 1
    assert split["while_running"]["median_seconds"] == 50
    assert split["before_work_period"]["samples"] == 1
    assert split["before_work_period"]["median_seconds"] == 250
    assert split["unattributed"]["samples"] == 0


def test_waiting_mail_notice_is_recorded_and_timed_against_the_claim(system):
    runtime, now, reader = system
    invocation = start(runtime)
    tools = runtime.tools(invocation.token)
    now[0] = 1100
    message = runtime.messaging.send(OWNER, ["founder"], "during", request_id="during")
    now[0] = 1120
    announced = tools.list_reminders()
    assert announced["mail_waiting"]["senders"][0][1] == 1
    now[0] = 1160
    tools.claim("claim", [message["message_id"]])
    split = read(reader, "conversations")["claims_by_arrival"]
    assert split["after_notice"]["samples"] == 1
    assert split["after_notice"]["median_seconds"] == 40
    assert split["claimed_without_notice"] == 0
    assert split["while_running"]["median_seconds"] == 60
    assert any(r.get("kind") == "mail.presented" for r in read(reader, "timeline")["rows"])


def test_owner_inbox_lists_pending_mail_with_bodies_and_author_labels(system):
    runtime, now, reader = system
    start(runtime)
    analyst = runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")[
        "employee_id"
    ]
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=? WHERE employee_id=?",
            ("# Profile\n\n**Public name:** Wren Calloway\n", analyst),
        )
    waiting = runtime.messaging.send(
        analyst, ["owner"], "Need a decision on the drive", subject="Decision", request_id="ask"
    )
    claimed = runtime.messaging.send(analyst, ["owner"], "Already read", request_id="read")
    dismissed = runtime.messaging.send(analyst, ["owner"], "Not interesting", request_id="skip")
    runtime.messaging.claim(OWNER, [claimed["message_id"]], request_id="claim")
    runtime.messaging.dismiss(OWNER, [dismissed["message_id"]], request_id="dismiss")
    now[0] = 1300
    data = read(reader, "inbox")
    assert [row["message_id"] for row in data["rows"]] == [waiting["message_id"]]
    row = data["rows"][0]
    assert row["type"] == "pending_mail"
    assert row["subject"] == "Decision"
    assert row["body"] == "Need a decision on the drive"
    assert row["author_id"] == analyst
    assert row["pending_age_seconds"] == 300
    assert data["pending_count"] == 1
    assert data["pending_age"]["median_seconds"] == 300
    assert data["role_labels"][analyst] == {
        "address": "founder/analyst",
        "identity": "Wren Calloway",
    }
    # Waiting mail is current, so a window that starts after it was sent still reports it.
    later = reader.read("inbox", from_at=1250, until=1400)
    assert [r["message_id"] for r in later["rows"]] == [waiting["message_id"]]


def test_owner_inbox_pages_like_other_sections(system):
    runtime, _, reader = system
    start(runtime)
    sent = [
        runtime.messaging.send("founder", ["owner"], f"Item {index}", request_id=f"mail{index}")[
            "message_id"
        ]
        for index in range(3)
    ]
    first = read(reader, "inbox", limit=2)
    assert [row["message_id"] for row in first["rows"]] == sent[:2]
    assert first["total_rows"] == 3 and first["has_more"]
    second = read(reader, "inbox", limit=2, cursor=first["next_cursor"])
    assert [row["message_id"] for row in second["rows"]] == sent[2:]
    assert not second["has_more"] and second["next_cursor"] is None


def test_reminders_and_expired_incidents_are_observed_without_release(system):
    runtime, now, reader = system
    first = start(runtime)
    runtime.tools(first.token).set_reminder(1100, "Later", "reminder")
    runtime.finish(first.activation_id)
    assert read(reader, "readiness")["rows"][0]["primary_reason"] == "scheduled_wake"
    now[0] = 1200
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO execution_incidents "
            "VALUES('expired','account','a','quota',1100,1000,NULL,NULL)"
        )
    before = dump(runtime)
    row = read(reader, "readiness")["rows"][0]
    assert row["primary_reason"] == "reminder_due"
    assert "account_quota_hold" not in row["reasons"]
    events = read(reader, "timeline")["rows"]
    expired = next(r for r in events if r["id"] == "incident:expired:cleared")
    assert expired["observed_at"] == 1100
    assert expired["evidence"]["resolved_by"] == "expired"
    assert dump(runtime) == before


def test_compact_readiness_retains_labels_and_blocker_references(system):
    runtime, now, reader = system
    first = start(runtime)
    with runtime.store.transaction() as db:
        request = runtime.requests.ask_owner(
            db, {"employee_id": "founder", "activation_id": first.activation_id},
            "thing", "widget", "Build", "needs_owner",
        )
    runtime.finish(first.activation_id)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET hold_reason='refusal',retry_at=1100,public_profile=?",
            ("# Profile\n**Public name:** Evidence reviewer",),
        )
    before = dump(runtime)
    full = read(reader, "readiness")["rows"][0]
    with runtime.store.read() as db:
        compact = AnalysisReader.console_readiness(db, now[0])
    assert compact["sampled_at"] == now[0]
    (row,) = compact["rows"]
    assert row.pop("blockers") == [
        {"hold_reason": "refusal"}, {"retry_at": 1100}, {"request_ids": [request["request_id"]]},
    ]
    assert row == {key: full[key] for key in row}
    assert row["identity"] == "Evidence reviewer"
    assert row["next_check_at"] == 1100
    assert dump(runtime) == before


def test_recording_idempotence_rollback_promotion_and_failures(system, caplog):
    runtime, now, reader = system
    first = start(runtime)
    analyst = runtime.lifecycle.hire(
        "founder", "temp", "Work", "main", temporary_periods=2, request_id="temp"
    )["employee_id"]
    activation = runtime.admit()
    runtime.mark_started(activation.activation_id)
    runtime.appointments.promote("founder", analyst, "promote")
    runtime.appointments.promote("founder", analyst, "promote")
    runtime.tools(first.token).handoff("Notes", "wait", "handoff")
    runtime.tools(first.token).handoff("Notes", "wait", "handoff")
    kinds = [r["kind"] for r in read(reader, "timeline")["rows"] if r["type"] == "event"]
    assert kinds.count("appointment.started") == 1
    assert kinds.count("appointment.promoted") == 1
    assert kinds.count("activation.handoff_saved") == 1
    usage = read(reader, "usage")
    allocation = next(r["appointment"] for r in usage["rows"] if r.get("label") == analyst)
    assert allocation["kind"] == "durable"
    assert allocation["used_periods"] == 1
    assert allocation["periods_in_window"] == 1
    before = dump(runtime)
    with pytest.raises(RuntimeError):
        with runtime.store.transaction() as db:
            analysis_history.record(db, 1100, "role.held", "founder")
            raise RuntimeError("rollback")
    assert dump(runtime) == before
    with runtime.store.transaction() as db:
        db.execute(
            "CREATE TRIGGER break_analysis BEFORE INSERT ON analysis_events BEGIN "
            "SELECT RAISE(ABORT,'analysis unavailable'); END"
        )
    runtime.stop()
    runtime.finish(first.activation_id)
    assert "recording failed" in caplog.text.lower()
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT state FROM activations WHERE activation_id=?", (first.activation_id,)
            ).fetchone()[0]
            == "completed"
        )
    assert read(reader)["coverage"]["recording_failure_pending"]
    with runtime.store.transaction() as db:
        db.execute("DROP TRIGGER break_analysis")
        analysis_history.record(db, now[0], "analysis.started", epoch="after-failure")
    assert read(reader)["coverage"]["recorded_gaps"] == 1


class FakeMachines:
    """The sandbox as the observer sees it: one reading of every machine, or a failure."""

    def __init__(self, samples):
        self.samples = samples
        self.calls = 0

    def resources(self):
        self.calls += 1
        if isinstance(self.samples, Exception):
            raise self.samples
        return self.samples


STOPPED = {"host": {"boot": "host-1", "cpus": 10}, "linux": None, "mac": None}
RUNNING = {
    "host": {"boot": "host-1", "cpus": 10, "memory_available_bytes": 20 << 30},
    "linux": {"boot": "linux-1", "cpus": 8, "memory_available_bytes": 9 << 30,
              "host_footprint_bytes": 22 << 30},
    "mac": {"boot": "mac-boot-1", "cpus": 8, "memory_available_bytes": 6 << 30},
}


def recorded_samples(runtime):
    with runtime.store.read() as db:
        return [dict(r) for r in db.execute("SELECT * FROM resource_samples ORDER BY sequence")]


def test_resource_samples_are_recorded_once_a_minute_while_a_vm_runs(system, caplog):
    runtime, now, reader = system
    machines = FakeMachines(STOPPED)
    observer = analysis_history.AnalysisObserver(runtime.store, machines)
    observer.resources()
    assert recorded_samples(runtime) == []  # a stopped firm's host counters say nothing
    now[0] += 61
    machines.samples = RUNNING
    observer.resources()
    now[0] += 30
    observer.resources()
    assert machines.calls == 2
    assert [(r["machine"], r["observed_at"]) for r in recorded_samples(runtime)] == [
        ("host", 1061.0),
        ("linux", 1061.0),
        ("mac", 1061.0),
    ]
    linux = recorded_samples(runtime)[1]
    assert linux["cpus"] == 8 and linux["host_footprint_bytes"] == 22 << 30
    assert linux["compressed_bytes"] is None
    now[0] += 61
    machines.samples = RuntimeError("guest control connection failed")
    observer.resources()
    assert "Resource sample failed" in caplog.text
    assert len(recorded_samples(runtime)) == 3


def test_observer_deduplicates_and_marks_new_epoch(system):
    runtime, now, reader = system
    observer = analysis_history.AnalysisObserver(runtime.store, FakeMachines(STOPPED))
    observer.start()
    observer.sample()
    before = dump(runtime)
    now[0] += 5
    observer.sample()
    assert dump(runtime) == before
    runtime.start()
    observer.sample()
    observer.stop()
    next_host = analysis_history.AnalysisObserver(runtime.store, FakeMachines(STOPPED))
    next_host.start()
    next_host.sample()
    events = [r for r in read(reader, "timeline")["rows"] if r.get("kind") == "readiness.observed"]
    assert len(events) == 3


GIB = 1 << 30


def add_samples(runtime, machine, samples):
    with runtime.store.transaction() as db:
        for observed_at, sample in samples:
            analysis_history.record_resources(db, observed_at, machine, sample)


def add_run(runtime, activation_id, **usage):
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO sandbox_runs(activation_id,role_id,run_id,provider,deadline,"
            "memory_peak_bytes,cpu_usec,oom_kills) VALUES(?,?,?,'codex',0,?,?,?)",
            (
                activation_id,
                "r-" + activation_id,
                "run-" + activation_id,
                usage.get("memory_peak_bytes"),
                usage.get("cpu_usec"),
                usage.get("oom_kills"),
            ),
        )


def linux_samples():
    """Three minutes of cumulative counters, the last one after a reboot."""
    return [
        (1000, {"boot": "b1", "cpus": 8, "memory_total_bytes": 24 * GIB,
                "memory_available_bytes": 8 * GIB, "memory_stall_usec": 0, "cpu_usec": 0,
                "oom_kills": 0, "disk_used_bytes": 30 * GIB, "disk_total_bytes": 60 * GIB}),
        (1060, {"boot": "b1", "cpus": 8, "memory_total_bytes": 24 * GIB,
                "memory_available_bytes": 2 * GIB, "memory_stall_usec": 2_000_000,
                "cpu_usec": 240_000_000, "oom_kills": 1, "disk_used_bytes": 42 * GIB,
                "disk_total_bytes": 60 * GIB}),
        (1120, {"boot": "b2", "cpus": 8, "memory_total_bytes": 24 * GIB,
                "memory_available_bytes": 20 * GIB, "memory_stall_usec": 0, "cpu_usec": 0,
                "oom_kills": 0, "disk_used_bytes": 41 * GIB, "disk_total_bytes": 60 * GIB}),
    ]


@pytest.mark.parametrize(
    "used,total,expected",
    [
        (50 * GIB, 60 * GIB, False),  # below the threshold
        (51 * GIB, 60 * GIB, True),  # exactly 85%: the threshold itself counts as low
        (55 * GIB, 60 * GIB, True),  # above
    ],
)
def test_disk_state_low_disk_threshold(used, total, expected):
    state = disk_state(
        {"disk_used_bytes": used, "disk_total_bytes": total, "observed_at": 1000}, 1000
    )
    assert state["low_disk"] is expected
    assert state["free_bytes"] == total - used
    assert state["stale"] is False and state["unknown"] is False


@pytest.mark.parametrize(
    "free,expected",
    [(230 * 1000**3, False), (100 * 1000**3, False), (100 * 1000**3 - 1, True)],
)
def test_host_disk_is_low_only_below_a_fixed_free_space(free, expected):
    total = 2000 * 1000**3  # 89% used at 230 GB free: the fraction alone would say low
    state = disk_state({"machine": "host", "disk_used_bytes": total - free,
                        "disk_total_bytes": total, "observed_at": 1000}, 1000)
    assert state["low_disk"] is expected


def test_disk_state_stale_reading_never_asserts_low_disk():
    sample = {"disk_used_bytes": 59 * GIB, "disk_total_bytes": 60 * GIB, "observed_at": 1000}
    fresh = disk_state(sample, 1000 + 3 * RESOURCE_INTERVAL)
    stale = disk_state(sample, 1000 + 3 * RESOURCE_INTERVAL + 1)
    assert fresh["stale"] is False and fresh["low_disk"] is True
    assert stale["stale"] is True and stale["low_disk"] is False
    assert stale["free_bytes"] == GIB  # staleness hides low_disk, not the figures themselves


@pytest.mark.parametrize(
    "sample",
    [
        {"disk_used_bytes": None, "disk_total_bytes": 60 * GIB, "observed_at": 1000},
        {"disk_used_bytes": 50 * GIB, "disk_total_bytes": None, "observed_at": 1000},
        {"disk_used_bytes": 50 * GIB, "disk_total_bytes": 0, "observed_at": 1000},
    ],
)
def test_disk_state_missing_or_zero_fields_are_unknown_not_low(sample):
    state = disk_state(sample, 1000)
    assert state["unknown"] is True
    assert state["low_disk"] is False
    assert state["free_bytes"] is None


def test_disk_states_reports_each_machines_own_latest_sample_regardless_of_window(system):
    runtime, now, reader = system
    add_samples(runtime, "linux", linux_samples())
    add_samples(
        runtime, "host", [(500, {"disk_used_bytes": 10 * GIB, "disk_total_bytes": 100 * GIB})]
    )
    now[0] = 1200
    with reader.snapshot() as (db, sampled_at):
        states = reader.disk_states(db, sampled_at)
    assert set(states) == {"linux", "host"}  # host included; the Mac was never sampled
    # The latest sample by sequence (1120), not the window's largest or its first.
    assert (states["linux"]["used_bytes"], states["linux"]["total_bytes"]) == (41 * GIB, 60 * GIB)
    assert states["host"]["used_bytes"] == 10 * GIB and states["host"]["low_disk"] is False


@pytest.mark.parametrize("retired_at", [100, 1000], ids=["stale", "fresh"])
def test_current_disk_excludes_retired_machines_but_preserves_history(system, retired_at):
    runtime, now, reader = system
    current = {"host", "linux", "mac"}
    retired = {"mac-1", "mac-2", "other-machine"}
    for machine in current | retired:
        add_samples(runtime, machine, [
            (retired_at if machine in retired else 1000,
             {"disk_used_bytes": 59 * GIB, "disk_total_bytes": 60 * GIB}),
        ])
    now[0] = 1010
    before = dump(runtime)

    report = read(reader)

    assert set(report["resources"]["disk"]) == current
    assert set(report["overview"]["low_disk"]) == {"linux", "mac"}
    assert not any("Disk readings for" in limit for limit in report["coverage"]["limits"])
    history = report["resources"]["machines"]
    assert set(history) == current | retired
    for machine in retired:
        assert history[machine]["disk_used_last_bytes"] == 59 * GIB
        assert history[machine]["low_disk_last"] is True
    assert dump(runtime) == before


def test_resources_summarize_each_machine_without_counting_across_a_reboot(system):
    runtime, now, reader = system
    add_samples(runtime, "linux", linux_samples())
    now[0] = 1200
    machine = read(reader, "resources")["machines"]["linux"]
    assert machine["samples"] == 3 and machine["boots"] == 2
    assert machine["largest_gap_seconds"] == 60
    # The counters reset at the reboot, so only the first pair is measured time.
    assert machine["memory_stall_seconds"] == 2.0
    assert machine["cpu_utilization_mean"] == machine["cpu_utilization_p95"] == 0.5
    assert machine["oom_kills"] == 1
    assert machine["memory_available_low_bytes"] == 2 * GIB
    assert machine["memory_used_p95_bytes"] == 22 * GIB
    assert (machine["disk_used_max_bytes"], machine["disk_total_bytes"]) == (42 * GIB, 60 * GIB)
    assert machine["cpu_stall_seconds"] is None
    assert machine["host_footprint_max_bytes"] is None
    # The window's last sample (41 GiB, at 1120), not the window's peak (42 GiB): named
    # separately so a historical window is never mistaken for the machine's current state.
    assert machine["disk_used_last_bytes"] == 41 * GIB
    assert machine["disk_free_last_bytes"] == 19 * GIB
    assert machine["low_disk_last"] is False
    narrow = reader.read("resources", from_at=1000, until=1100)["machines"]["linux"]
    assert narrow["samples"] == 2 and narrow["boots"] == 1
    # Narrowed to end before the last sample: the window's own last differs from the full one.
    assert narrow["disk_used_last_bytes"] == 42 * GIB


def test_missing_and_reset_only_resource_counters_remain_unknown(system):
    runtime, now, reader = system
    add_samples(
        runtime,
        "linux",
        [
            (1000, {"boot": "b1", "cpus": 4, "oom_kills": 5}),
            (1060, {"boot": "b1", "cpus": 4, "oom_kills": 2}),
            (1120, {"boot": "b2", "cpus": 4, "oom_kills": 0}),
        ],
    )
    now[0] = 1200

    machine = read(reader, "resources")["machines"]["linux"]

    assert machine["oom_kills"] is None
    assert machine["memory_stall_seconds"] is None
    assert machine["cpu_stall_seconds"] is None
    assert machine["swapouts"] is None


def test_a_window_that_ended_low_still_reads_low_long_afterwards(system):
    runtime, now, reader = system
    add_samples(
        runtime, "mac", [(1000, {"disk_used_bytes": 95 * GIB, "disk_total_bytes": 97 * GIB})]
    )
    now[0] = 1000 + 7 * 24 * 3600  # the firm stopped a week ago
    report = read(reader, "resources")
    # The window answers for the moment of its last sample; only the current state goes stale.
    assert report["machines"]["mac"]["low_disk_last"] is True
    assert report["disk"]["mac"]["stale"] is True
    assert report["disk"]["mac"]["low_disk"] is False


def test_resources_report_each_run_its_role_and_a_concurrent_peak_bound(system):
    runtime, now, reader = system
    founder = start(runtime)
    runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")
    analyst = runtime.admit()
    runtime.mark_started(analyst.activation_id)
    now[0] = 1100
    runtime.finish(founder.activation_id)
    add_run(runtime, founder.activation_id, memory_peak_bytes=700 * 1048576, cpu_usec=50_000_000)
    add_run(runtime, analyst.activation_id, memory_peak_bytes=300 * 1048576, oom_kills=2)
    now[0] = 1200
    data = read(reader, "resources")
    second = analyst.employee_id
    runs = {row["employee_id"]: row for row in data["rows"] if row["type"] == "run"}
    assert runs["founder"]["duration_seconds"] == 100
    assert runs["founder"]["cpu_seconds"] == 50.0
    assert runs["founder"]["mean_vcpus"] == 0.5
    # The second role is still running: measured through sampled_at, not a finish time.
    assert runs[second]["finished_at"] is None
    assert runs[second]["duration_seconds"] == 200
    roles = {row["label"]: row for row in data["rows"] if row["type"] == "role"}
    assert roles["founder"]["memory_peak_max_bytes"] == 700 * 1048576
    assert (roles[second]["runs"], roles[second]["oom_kills"]) == (1, 2)
    assert data["run_memory_peak_concurrent_bound_bytes"] == 1000 * 1048576
    assert data["max_active"] == 4
    only = read(reader, "resources", employee_id=second)
    assert {row["employee_id"] for row in only["rows"]} == {second}
    page = read(reader, "resources", row_type="run", limit=1)
    assert page["total_rows"] == 2 and page["has_more"]
    rest = read(reader, "resources", row_type="run", limit=1, cursor=page["next_cursor"])
    assert rest["rows"][0]["id"] != page["rows"][0]["id"] and rest["next_cursor"] is None


def test_resources_include_isolated_settlements_without_inventing_an_employee(system):
    runtime, now, reader = system
    usage = {"cpu_usec": 50}
    with runtime.store.transaction() as db:
        db.execute("INSERT INTO summary_runs(run_id,started_at,finished_at,state,input_bytes) "
                   "VALUES('summary-one',990,999,'summarized',10)")
        db.execute("INSERT INTO sandbox_run_settlements "
                   "(run_id,role_id,vm_generation,recorded_at,usage_json) VALUES(?,?,?,?,?)",
                   ("summary-one", "s-summary-one", "generation", now[0], json.dumps(usage)))
    data = read(reader, "resources", row_type="summary_run")
    assert data["rows"][0]["usage"] == usage
    assert "reclaim" not in data["rows"][0]
    assert data["role_labels"] == {}
    assert not read(reader, "resources", row_type="summary_run", employee_id="founder")["rows"]


def add_mac_lease(runtime, activation_id, lease_id, station, acquired_at, released_at=None):
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO mac_leases(lease_id,station,activation_id,employee_id,acquired_at,"
            "released_at,outcome) VALUES(?,?,?,'founder',?,?,?)",
            (lease_id, station, activation_id, acquired_at, released_at,
             "released" if released_at is not None else None),
        )


def add_mac_lease_disk(
    runtime, lease_id, station, phase, at, free_bytes, total_bytes, reclaimed_bytes=None
):
    with runtime.store.transaction() as db:
        analysis_history.record(
            db, at, "mac.disk", "founder", lease_id=lease_id, station=station, phase=phase,
            free_bytes=free_bytes, total_bytes=total_bytes, reclaimed_bytes=reclaimed_bytes,
        )


def add_mac_disk_refusal(runtime, station, at, free_bytes, total_bytes, reclaimed_bytes,
                         employee_id="founder", activation_id=None):
    with runtime.store.transaction() as db:
        analysis_history.record(
            db, at, "mac.disk", employee_id, activation_id, station=station, phase="refused",
            free_bytes=free_bytes, total_bytes=total_bytes, reclaimed_bytes=reclaimed_bytes,
        )


def test_mac_section_carries_each_leases_disk_reading_and_unknown_without_rows(system):
    runtime, now, reader = system
    founder = start(runtime)
    measured, bare = "a" * 32, "b" * 32
    add_mac_lease(runtime, founder.activation_id, measured, 1, 1000, 1100)
    add_mac_lease_disk(runtime, measured, 1, "acquire", 1000, 60 * GIB, 100 * GIB, 2 * GIB)
    # A release that reclaimed less than hoped is still clean; this is where that shows.
    reclaim = {
        "completed": False, "timed_out": True, "refused": None,
        "categories": [{"category": "xcode", "count": 9, "bytes": 5 * GIB, "skipped": 2,
                        "skipped_bytes": 9, "skipped_reasons": ["errno:1"]}],
    }
    # A step that failed without timing out says why; one that completed or timed out
    # has nothing to say.
    simulators = [{"step": "shutdown", "completed": True, "timed_out": False},
                  {"step": "default", "completed": False, "timed_out": True},
                  {"step": "testing", "completed": False, "timed_out": False,
                   "returncode": 148, "said": "Unable to delete devices: Invalid device state"}]
    with runtime.store.transaction() as db:
        analysis_history.record(
            db, 1100, "mac.disk", "founder", lease_id=measured, station=1, phase="release",
            free_bytes=30 * GIB, total_bytes=100 * GIB, reclaimed_bytes=5 * GIB,
            reclaim=reclaim, simulators=simulators,
        )
    add_mac_lease(runtime, founder.activation_id, bare, 1, 1000, 1100)
    now[0] = 1200
    data = read(reader, "mac")
    rows = {row["lease_id"]: row for row in data["rows"] if row["type"] == "lease"}
    unknown = {"free_bytes": None, "total_bytes": None, "reclaimed_bytes": None,
               "reclaim": None, "simulators": None}
    assert rows[measured]["disk"] == {
        "acquired": {**unknown, "free_bytes": 60 * GIB, "total_bytes": 100 * GIB,
                     "reclaimed_bytes": 2 * GIB},
        # The journal's allow-list carried every field of the evidence through.
        "released": {"free_bytes": 30 * GIB, "total_bytes": 100 * GIB,
                     "reclaimed_bytes": 5 * GIB, "reclaim": reclaim, "simulators": simulators},
    }
    # A lease recorded before this figure existed has no `mac.disk` events at all.
    assert rows[bare]["disk"] == {"acquired": unknown, "released": unknown}
    assert "machine-wide free space" in data["semantics"]
    assert "reclaimed_bytes" in data["semantics"] and "`reclaim`" in data["semantics"]
    assert "`returncode`" in data["semantics"] and "`said`" in data["semantics"]


def test_mac_section_reports_a_disk_full_refusal_without_a_lease(system):
    runtime, now, reader = system
    start(runtime)
    add_mac_disk_refusal(runtime, 1, 1000, 1 * GIB, 100 * GIB, 0)
    now[0] = 1200
    data = read(reader, "mac")
    refused = [row for row in data["rows"] if row["type"] == "disk_full"]
    assert len(refused) == 1
    row = refused[0]
    assert row["employee_id"] == "founder" and row["station"] == 1 and row["lease_id"] is None
    assert row["free_bytes"] == 1 * GIB and row["total_bytes"] == 100 * GIB
    assert row["reclaimed_bytes"] == 0
    assert data["by_role"]["founder"]["disk_full"] == 1
    only = read(reader, "mac", row_type="disk_full")
    assert [row["type"] for row in only["rows"]] == ["disk_full"]
    assert only["rows"][0]["id"] == row["id"]


def test_cli_analyze_reads_the_resources_section(system, monkeypatch, capsys):
    runtime, now, reader = system
    add_samples(runtime, "linux", linux_samples())
    now[0] = 1200
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm", "analyze", str(runtime.store.path.parent.parent),
            "--section", "resources",
            "--from", "1970-01-01T00:00:00+00:00", "--until", "1970-01-02T00:00:00+00:00",
        ],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["machines"]["linux"]["samples"] == 3
    assert "page cache" in result["semantics"]


def test_cli_analyze_narrows_a_section_to_one_row_type(system, monkeypatch, capsys):
    runtime, now, reader = system
    start(runtime)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm", "analyze", str(runtime.store.path.parent.parent),
            "--section", "timeline", "--row-type", "activation",
            "--from", "1970-01-01T00:00:00+00:00", "--until", "1970-01-02T00:00:00+00:00",
        ],
    )
    cli.main()
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert rows and {row["type"] for row in rows} == {"activation"}


def test_console_breakdowns_filter_before_paging_without_hiding_api_events(system):
    runtime, now, reader = system
    start(runtime)
    now[0] += 10
    runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")
    analyst = runtime.admit()
    before = dump(runtime)
    first = read(reader, "timeline", row_type="activation", limit=1)
    assert first["total_rows"] == 2
    assert first["rows"][0]["type"] == "activation"
    assert first["rows"][0]["activation_id"] == analyst.activation_id
    second = read(reader, "timeline", row_type="activation", limit=1, cursor=first["next_cursor"])
    assert second["rows"][0]["type"] == "activation"
    assert second["rows"][0]["id"] != first["rows"][0]["id"]
    assert second["next_cursor"] is None
    assert any(row["type"] == "event" for row in read(reader, "timeline")["rows"])
    roles = read(reader, "usage", row_type="role", limit=1)
    assert roles["total_rows"] == 2
    assert roles["rows"][0]["type"] == "role"
    with pytest.raises(AnalysisQueryError):
        read(reader, "usage", row_type="session", cursor=roles["next_cursor"])
    with pytest.raises(AnalysisQueryError):
        read(reader, "timeline", cursor=first["next_cursor"])
    with pytest.raises(AnalysisQueryError):
        read(reader, "readiness", row_type="activation")
    assert dump(runtime) == before


def test_paging_pins_selection_and_totals_across_concurrent_writes(system):
    runtime, now, reader = system
    start(runtime)
    for i in range(5):
        runtime.lifecycle.hire("founder", f"analyst{i}", "Work", "main", request_id=f"hire{i}")
    first = read(reader, "timeline", limit=2)
    ids = [r["id"] for r in first["rows"]]
    runtime.lifecycle.hire("founder", "late", "Work", "main", request_id="late")
    next_cursor = first["next_cursor"]
    while next_cursor:
        page = read(reader, "timeline", limit=2, cursor=next_cursor)
        assert page["total_rows"] == first["total_rows"]
        ids += [r["id"] for r in page["rows"]]
        next_cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == first["total_rows"]
    assert read(reader, "timeline", limit=500)["total_rows"] > len(ids)
    with pytest.raises(AnalysisQueryError):
        reader.read("timeline", from_at=1, until=10000, cursor=first["next_cursor"])
    with pytest.raises(AnalysisNotFound):
        read(reader, employee_id="does-not-exist")


@pytest.mark.parametrize(
    "query",
    [
        {"from_at": float("nan")},
        {"until": float("inf")},
        {"from_at": 2000, "until": 1000},
        {"limit": 0},
        {"limit": 501},
        {"cursor": "garbage"},
    ],
)
def test_invalid_queries(system, query):
    with pytest.raises(AnalysisQueryError):
        system[2].read("timeline", **query)


def test_analysis_routes_auth_errors_and_no_mutations(system):
    runtime, now, reader = system
    host = "127.0.0.1:8765"
    app = build_console_app(runtime, allowed_host=host)
    paths = [
        f"/api/analysis/{s}"
        for s in ("overview", "timeline", "usage", "conversations", "readiness", "resources")
    ]
    paths += ["/api/analysis/roles/founder/readiness", "/api/analysis/conversations/missing"]
    with TestClient(app, base_url="http://" + host) as client:
        assert all(client.get(path).status_code == 401 for path in paths)
        client.get(f"/unlock/{app.state.console_key}")
        before = dump(runtime)
        for path in paths[:-1]:
            assert client.get(path).status_code == 200
            assert (
                client.post(
                    path,
                    headers={
                        "Origin": f"http://{host}",
                        "Sec-Fetch-Site": "same-origin",
                        "X-Firm-Console": "same-origin",
                    },
                ).status_code
                == 405
            )
        assert client.get(paths[-1]).status_code == 404
        assert client.get("/api/analysis/timeline?limit=999").status_code == 400
        assert client.get("/api/analysis/nope").status_code == 404
        assert dump(runtime) == before


def test_console_reads_the_tool_breakdown_and_the_conversation_facts(system):
    runtime, now, reader = system
    invocation = start(runtime)
    tally(runtime, invocation.activation_id, {"Bash": 5, "mcp__firm__peek": 2})
    host = "127.0.0.1:8765"
    app = build_console_app(runtime, allowed_host=host)
    with TestClient(app, base_url="http://" + host) as client:
        client.get(f"/unlock/{app.state.console_key}")
        before = dump(runtime)
        usage = client.get("/api/analysis/usage?from=0&row_type=tool").json()
        assert {r["label"]: r["calls"] for r in usage["rows"]} == {"Bash": 5, "mcp__firm__peek": 2}
        assert {r["group"] for r in usage["rows"]} == {"guest", "firm"}
        assert usage["tool_calls"]["by_group"] == {"guest": 5, "firm": 2}
        timeline = client.get("/api/analysis/timeline?from=0&row_type=activation").json()
        assert timeline["rows"][0]["tool_calls"] == {"Bash": 5, "mcp__firm__peek": 2}
        assert timeline["rows"][0]["wake_reasons"]
        conversations = client.get("/api/analysis/conversations?from=0").json()
        assert conversations["undeliverable_reasons"] == {}
        assert "unresolved_reply_confirmations" not in conversations
        assert client.get("/api/analysis/mac?row_type=tool").status_code == 400
        assert dump(runtime) == before


def test_snapshot_keeps_one_view_during_writes(system):
    runtime, now, reader = system
    with reader.snapshot() as (db, sampled):
        before = db.execute("SELECT state FROM firm").fetchone()[0]
        runtime.start()
        assert db.execute("SELECT state FROM firm").fetchone()[0] == before == "stopped"
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE firm SET state='running'")


def test_summary_usage_captures_caller_configured_and_reported_model(system):
    runtime, now, reader = system
    invocation = start(runtime)

    class Runner:
        def start(self, request):
            return request.run_id

        def wait(self, handle, timeout):
            return SummaryOutput(
                "Short text", output_tokens=3,
                usage={"input_tokens": 200, "output_tokens": 40, "resolved_model": "reported-luna"},
            )

        def stop(self, handle):
            return True

        def active_run_ids(self):
            return set()

    runtime.summary_service.provider = Runner()
    snapshot = ContextSnapshot("c1", (), ("m1",), (), 100, 1)
    result = runtime.summary_service.summarize(snapshot, caller_session_id=invocation.session_id)
    assert result["status"] == "summarized"
    report = read(reader, "usage", employee_id="founder")
    assert report["summary_totals"]["output_tokens"]["value"] == 40
    model = next(
        r for r in report["rows"] if r["type"] == "model" and r["label"][1] != "captured-model"
    )
    assert model["label"] == ["codex", "gpt-6-luna"]
    assert model["resolved_models"] == ["reported-luna"]
    assert model["missing_resolved_model_identity"] == 0
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM analysis_events WHERE kind='summary.bound'")
    report = read(reader, "usage")
    assert report["missing_model_identity"] == 1
    assert report["resolved_models"] == ["reported-luna", None]
    assert report["missing_resolved_model_identity"] == 1
    assert report["summary_totals"]["output_tokens"]["value"] == 40


def test_task_usage_is_separate_attributed_and_keeps_unknowns(system):
    runtime, _, reader = system
    invocation = start(runtime)
    runtime.record_usage(invocation.activation_id, ProviderUsage(input_tokens=100, source="test"))
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,input_tokens) "
            "VALUES('summary',1001,'summarized',0,20)"
        )
        for run_id, at, state, reason, tokens, model in (
            ("worker-ok", 1002, "summarized", None, 30, "reported-worker"),
            ("worker-failed", 1003, "unavailable", "timeout", None, None),
        ):
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,finished_at,state,reason,input_bytes,"
                "input_tokens,resolved_model,kind,caller_activation_id,provider,account_id) "
                "VALUES(?,?,1010,?,?,0,?,?,'task',?,'codex','a')",
                (run_id, at, state, reason, tokens, model, invocation.activation_id),
            )
            analysis_history.summary_bound(
                db, at, run_id, invocation.session_id,
                {"account_id": "a", "provider": "codex", "model": "worker-model"},
                activation_id=invocation.activation_id,
            )
    before = dump(runtime)
    report = read(reader, "usage")
    assert report["totals"]["input_tokens"] == {
        "value": 150, "measured": 3, "eligible": 4, "complete": False,
    }
    assert report["role_totals"]["input_tokens"]["value"] == 100
    assert report["summary_totals"]["input_tokens"]["value"] == 20
    assert report["task_totals"]["input_tokens"] == {
        "value": 30, "measured": 1, "eligible": 2, "complete": False,
    }
    tasks = read(reader, "usage", row_type="task", employee_id="founder")
    assert tasks["total_rows"] == 2
    assert tasks["role_labels"]["founder"]["address"] == "founder"
    assert tasks["coverage"]["limits"]
    assert all(row["activation_id"] == invocation.activation_id for row in tasks["rows"])
    succeeded = next(row for row in tasks["rows"] if row["run_id"] == "worker-ok")
    assert succeeded["model"] == "worker-model"
    assert succeeded["resolved_model"] == "reported-worker"
    assert dump(runtime) == before
    # Missing best-effort binding history loses configured model, not durable caller identity.
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM analysis_events WHERE kind='summary.bound'")
    tasks = reader.read("usage", from_at=1001, until=1020, row_type="task", employee_id="founder")
    assert tasks["total_rows"] == 2  # Caller admission is outside this window.
    assert tasks["task_totals"]["input_tokens"]["value"] == 30
    assert tasks["role_totals"]["input_tokens"]["eligible"] == 0
    assert all(row["model"] is None for row in tasks["rows"])
    assert tasks["role_labels"]["founder"]["address"] == "founder"


def test_task_timeline_outcomes_overlap_and_unconfirmed_cleanup(system):
    runtime, now, reader = system
    invocation = start(runtime)
    now[0] = 1030
    with runtime.store.transaction() as db:
        for run_id, finished, state, reason in (
            ("complete", 1020, "summarized", None),
            ("timeout", 1020, "unavailable", "timeout"),
            ("unconfirmed", None, "running", "cancelled_unconfirmed"),
        ):
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,finished_at,state,reason,input_bytes,"
                "kind,caller_activation_id) VALUES(?,1002,?,?,?,0,'task',?)",
                (run_id, finished, state, reason, invocation.activation_id),
            )
    before = dump(runtime)
    report = reader.read(
        "timeline", from_at=1010, until=1025, row_type="task", employee_id="founder"
    )
    rows = {row["run_id"]: row for row in report["rows"]}
    assert report["activation_count"] == 1 and report["task_count"] == 3
    assert rows["complete"]["outcome"] == "completed"
    assert rows["timeout"]["reason"] == "timeout"
    assert rows["timeout"]["outcome"] == "unavailable"
    assert rows["unconfirmed"]["reason"] == "cancelled_unconfirmed"
    assert rows["unconfirmed"]["open"] is True
    assert rows["unconfirmed"]["outcome"] == "unavailable"
    assert rows["unconfirmed"]["state"] == "running"
    assert all(row["caller_activation_id"] == invocation.activation_id for row in rows.values())
    assert all(row["employee_id"] == "founder" for row in rows.values())
    assert report["role_labels"]["founder"]["address"] == "founder"
    assert report["coverage"]["limits"]
    assert "instruction" not in json.dumps(report)
    assert "answer" not in json.dumps(report)
    page = read(reader, "timeline", row_type="task", limit=1)
    rest = read(reader, "timeline", row_type="task", limit=2, cursor=page["next_cursor"])
    assert {r["id"] for r in page["rows"]}.isdisjoint({r["id"] for r in rest["rows"]})
    assert len(rest["rows"]) == 2
    assert "include child tasks" in read(reader, "resources")["semantics"]
    assert dump(runtime) == before


def test_pressure_changes_distinguish_policy_and_usage_without_double_compactions(system):
    runtime, now, reader = system
    invocation = start(runtime)
    for current, compactions in ((1000, 0), (5500, 1), (5500, 1), (1000, 1)):
        now[0] += 1
        runtime.record_usage(
            invocation.activation_id,
            ProviderUsage(
                current_context_tokens=current,
                high_water_context_tokens=current,
                native_compactions=compactions,
                source="test",
            ),
        )
    with runtime.store.read() as db:
        config = json.loads(db.execute("SELECT config_json FROM firm").fetchone()[0])
    config["profiles"]["main"]["context_window"] = 6000
    runtime.owner.configure(OWNER, config)
    events = [r for r in read(reader, "timeline")["rows"] if r["type"] == "event"]
    pressure = [r["evidence"] for r in events if r["kind"] == "context.pressure_changed"]
    assert [p["value"] for p in pressure] == [
        "normal",
        "handoff_required",
        "normal",
        "handoff_required",
    ]
    assert pressure[-1]["cause"] == "policy"
    assert len([r for r in events if r["kind"] == "context.compaction_observed"]) == 1
    session = next(r for r in read(reader, "usage")["rows"] if r["type"] == "session")
    assert session["native_compactions"] == 1
    assert session["observed_threshold_entries"] == 1


def test_readiness_fallback_summary_occupancy_and_low_quota(system):
    runtime, now, reader = system
    runtime.start()
    with runtime.store.transaction() as db:
        config = json.loads(db.execute("SELECT config_json FROM firm").fetchone()[0])
        config["accounts"]["b"] = {"provider": "claude", "max_active": 1, "auth_dir": "/secret/b"}
        config["profiles"]["fallback"] = {
            "provider": "claude",
            "model": "test",
            "effort": "low",
            "accounts": ["b"],
        }
        config["profiles"]["main"]["alternates"] = ["fallback"]
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
        db.execute("INSERT INTO account_observations VALUES('a',0.01,1000,100,'test')")
    report = read(reader, "readiness")["rows"][0]
    assert report["activity"] == "ready"
    assert report["account_options"][0]["blockers"] == []  # low quota only ranks it last
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id) "
            "VALUES('occupied',1000,'running',0,'b')"
        )
    report = read(reader, "readiness")["rows"][0]
    assert report["activity"] == "ready"  # an isolated run holds no slot on b
    assert "account_capacity" not in report["reasons"]

    invocation = runtime.admit()  # the founder falls back onto b and does occupy it
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    with runtime.store.read() as db:
        assert runtime.accounts.binding(db, invocation.session_id)["account_id"] == "b"
    child = runtime.tools(invocation.token).hire("child", "Own work", "hire-child")["employee_id"]
    report = next(r for r in read(reader, "readiness")["rows"] if r["employee_id"] == child)
    assert report["activity"] == "ready"  # b is full, so the low "a" takes the work
    options = {o["account_id"]: o["blockers"] for o in report["account_options"]}
    assert options == {"a": [], "b": ["account_capacity"]}
    runtime.finish(invocation.activation_id)  # b's slot comes back
    with runtime.store.transaction() as db:
        db.execute("UPDATE summary_runs SET state='summarized'")
    runtime.accounts.pause(OWNER, "profile", "main", "config")
    report = next(r for r in read(reader, "readiness")["rows"] if r["employee_id"] == child)
    assert report["activity"] == "ready"
    assert report["account_options"][0]["blockers"] == ["profile_config_hold"]
    assert runtime.admit() is not None


def test_overview_names_low_machines_without_blocking_any_role(system):
    runtime, now, reader = system
    before = read(reader, "readiness")["rows"][0]
    with runtime.store.transaction() as db:
        analysis_history.record_resources(
            db, 1000, "mac", {"disk_used_bytes": 52 * GIB, "disk_total_bytes": 60 * GIB}
        )
        # The host is the owner's own Mac, not a firm resource: excluded here even though
        # its own reading is past the threshold.
        analysis_history.record_resources(
            db, 1000, "host", {"disk_used_bytes": 59 * GIB, "disk_total_bytes": 60 * GIB}
        )
    now[0] = 1010
    overview = read(reader, "overview")
    assert set(overview["low_disk"]) == {"mac"}
    assert overview["low_disk"]["mac"]["free_bytes"] == 8 * GIB
    # A low disk is a fact about the firm. It must not become a reason on a role, where
    # anything but an idle reason classifies the role as blocked.
    after = overview["rows"][0]
    assert (after["activity"], after["reasons"]) == (before["activity"], before["reasons"])


def test_coverage_notes_a_stale_or_unknown_disk_reading(system):
    runtime, now, reader = system
    with runtime.store.transaction() as db:
        analysis_history.record_resources(
            db, 100, "mac", {"disk_used_bytes": 10 * GIB, "disk_total_bytes": 60 * GIB}
        )
    now[0] = 100 + 3 * RESOURCE_INTERVAL + 1  # older than three sampling intervals
    limits = read(reader)["coverage"]["limits"]
    assert any("mac" in limit and "stale or unknown" in limit for limit in limits)


def test_representative_resource_sample_volume(system):
    runtime, now, reader = system
    minutes = 30 * 24 * 60
    with runtime.store.transaction() as db:
        db.executemany(
            "INSERT INTO resource_samples(observed_at,machine,boot,cpus,memory_total_bytes,"
            "memory_available_bytes,memory_stall_usec,cpu_usec,oom_kills,disk_used_bytes) "
            "VALUES(?,?,'b1',8,?,?,?,?,0,?)",
            [
                (1000 + minute * 60, machine, 24 * GIB, 8 * GIB, minute, minute * 1000, 30 * GIB)
                for minute in range(minutes)
                for machine in ("host", "linux", "mac")
            ],
        )
    now[0] = 1000 + minutes * 60
    started = time.perf_counter()
    data = reader.read("resources", from_at=0, until=now[0], limit=100)
    elapsed = time.perf_counter() - started
    assert [m["samples"] for m in data["machines"].values()] == [minutes] * 3
    assert elapsed < 10
    print(f"Resources: {minutes * 3:,} samples in {elapsed:.3f}s")


def test_representative_analysis_volume(system):
    runtime, now, reader = system
    with runtime.store.transaction() as db:
        db.executemany(
            "INSERT INTO roles(employee_id,address,manager_id,identity,execution_profile,"
            "recovery_episode,created_at) VALUES(?,?,'founder','analyst','main','1',1000)",
            [(f"scale{i}", f"scale{i}") for i in range(5)],
        )
        db.executemany(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,"
            "wake_snapshot,context_json,created_at,started_at,finished_at) "
            "VALUES(?,? ,?,'completed','[]','{}',?,?,?)",
            [
                (f"a{i}", f"scale{i % 5}", f"s{i // 50}-{i % 5}", 1000 + i, 1001 + i, 1005 + i)
                for i in range(10000)
            ],
        )
        db.executemany(
            "INSERT INTO context_sessions(session_id,employee_id,execution_profile,"
            "advisory_fraction,handoff_reserve) VALUES(?,?,'main',0.6,5000)",
            [(f"s{i}-{j}", f"scale{j}") for i in range(200) for j in range(5)],
        )
        db.executemany(
            "INSERT INTO conversations VALUES(?,?,?,'[]',NULL,1000)",
            [(f"x{i}", i + 1, f'["x{i}"]') for i in range(2000)],
        )
        db.executemany(
            "INSERT INTO messages(message_id,sequence,conversation_id,author_id,author_address,"
            "author_identity,recipients_json,participants_json,subject,body,created_at) "
            "VALUES(?,? ,?,'owner','owner','Owner','[]','[]','Work','Body',?)",
            [(f"m{i}", i + 1, f"x{i // 5}", 1000 + i) for i in range(10000)],
        )
        db.executemany(
            "INSERT INTO message_deliveries(message_id,employee_id) VALUES(?,?)",
            [(f"m{i}", f"scale{j}") for i in range(10000) for j in range(5)],
        )
    now[0] = 12000
    started = time.perf_counter()
    result = reader.read(from_at=0, until=12000, limit=100)
    elapsed = time.perf_counter() - started
    assert result["timeline"]["activation_count"] == 10000
    assert result["conversations"]["delivery_count"] == 50000
    assert len(result["conversations"]["rows"]) == 100
    with reader.snapshot() as (db, _):
        plans = [
            tuple(r)
            for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT message_id FROM messages "
                "WHERE conversation_id='x1' ORDER BY sequence LIMIT 100"
            )
        ]
    assert any("messages_by_conversation" in r[3] for r in plans)
    print(f"Analysis: 10,000 activations / 50,000 deliveries, bundle {elapsed:.3f}s; {plans}")


def tally(runtime, activation_id, counts):
    with runtime.store.transaction() as db:
        db.executemany(
            "INSERT INTO activation_tool_calls VALUES(?,?,?)",
            [(activation_id, tool, calls) for tool, calls in counts.items()],
        )


def test_work_period_shows_why_it_woke_what_it_called_and_what_it_changed(system):
    runtime, now, reader = system
    invocation = start(runtime)
    tally(runtime, invocation.activation_id, {"Bash": 12, "mcp__firm__send": 2})
    runtime.tools(invocation.token).handoff("secret notes", "wait", "handoff")
    data = read(reader, "timeline", row_type="activation")
    row = next(r for r in data["rows"] if r["activation_id"] == invocation.activation_id)
    assert "wake_snapshot" not in row
    assert [w["reason"] for w in row["wake_reasons"]] == ["birth"]
    assert all({"priority", "ready_at", "source_id"} <= set(w) for w in row["wake_reasons"])
    assert row["tool_calls"] == {"Bash": 12, "mcp__firm__send": 2}
    assert row["operations"] == {"handoff": 1}
    assert data["work_period_contents"]["tallied_work_periods"] == 1
    assert data["work_period_contents"]["eligible_work_periods"] == 1


def test_untallied_work_period_reads_as_unmeasured_not_as_calling_nothing(system):
    runtime, now, reader = system
    invocation = start(runtime)
    data = read(reader, "timeline", row_type="activation")
    row = next(r for r in data["rows"] if r["activation_id"] == invocation.activation_id)
    assert row["tool_calls"] == {} and row["operations"] == {}
    assert data["work_period_contents"]["tallied_work_periods"] == 0
    assert data["work_period_contents"]["eligible_work_periods"] == 1
    assert any("not measured" in limit for limit in data["coverage"]["limits"])


def test_usage_groups_tool_calls_by_tool_and_by_mcp_server(system):
    runtime, now, reader = system
    first = start(runtime)
    runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")
    second = runtime.admit()
    tally(runtime, first.activation_id, {"Bash": 12, "mcp__firm__send": 2})
    tally(runtime, second.activation_id, {"Bash": 3, "mcp__firm__peek": 9})
    data = read(reader, "usage", row_type="tool")
    assert {r["label"]: r["calls"] for r in data["rows"]} == {
        "Bash": 15,
        "mcp__firm__send": 2,
        "mcp__firm__peek": 9,
    }
    assert next(r for r in data["rows"] if r["label"] == "Bash")["executions"] == 2
    assert next(r for r in data["rows"] if r["label"] == "Bash")["group"] == "guest"
    assert data["tool_calls"]["by_group"] == {"guest": 15, "firm": 11}
    assert data["tool_calls"]["tallied_executions"] == 2
    assert data["tool_calls"]["eligible_executions"] == 2
    with pytest.raises(AnalysisQueryError):
        read(reader, "timeline", row_type="tool")


def test_tallies_page_under_the_work_period_cutoff(system):
    runtime, now, reader = system
    first = start(runtime)
    tally(runtime, first.activation_id, {"Bash": 1})
    runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")
    now[0] = 1010
    second = runtime.admit()
    tally(runtime, second.activation_id, {"Read": 2})
    page = read(reader, "timeline", row_type="activation", limit=1)
    assert page["next_cursor"] and page["total_rows"] == 2
    # A work period admitted after the page was taken is outside its pinned cutoff.
    runtime.lifecycle.hire("founder", "later", "Work", "main", request_id="hire2")
    now[0] = 1020
    late = runtime.admit()
    tally(runtime, late.activation_id, {"Edit": 4})
    remaining = read(
        reader, "timeline", row_type="activation", limit=1, cursor=page["next_cursor"]
    )
    shown = [r["activation_id"] for r in page["rows"] + remaining["rows"]]
    assert shown == [second.activation_id, first.activation_id]
    assert late.activation_id not in shown and remaining["next_cursor"] is None
    assert "Edit" not in json.dumps(page) + json.dumps(remaining)


def test_conversations_report_undeliverable_mail_without_bodies(system):
    runtime, now, reader = system
    start(runtime)
    analyst = runtime.lifecycle.hire("founder", "analyst", "Work", "main", request_id="hire")[
        "employee_id"
    ]
    other = runtime.lifecycle.hire("founder", "other", "Work", "main", request_id="hire2")[
        "employee_id"
    ]
    opening = runtime.messaging.send(
        OWNER, ["founder", analyst], "Plan", subject="Plan", request_id="opening"
    )
    runtime.messaging.reply(
        analyst, opening["message_id"], "Adding a reader.", add=[other], request_id="widen"
    )
    runtime.messaging.reply(
        "founder", opening["message_id"], "confidential draft", request_id="draft"
    )
    stranded = runtime.messaging.send(OWNER, [other], "For you", request_id="stranded")
    runtime.lifecycle.fire("founder", other, reason="done", request_id="fire")
    now[0] = 1200
    data = read(reader, "conversations")
    # Both the widened reply and the mail addressed to the retired role are stranded.
    assert data["undeliverable_reasons"] == {"recipient retired": 2}
    assert data["dispositions"]["undeliverable"] == 2
    assert "confidential draft" not in json.dumps(data)
    assert stranded["message_id"] in json.dumps(data)
    # The introduction started a second conversation; the founder's draft stayed in the first.
    rows = {r["conversation_id"]: r for r in data["rows"] if r["type"] == "conversation"}
    assert rows[opening["conversation_id"]]["messages"] == 2
    assert sorted(rows[opening["conversation_id"]]["participants"]) == [analyst, "founder", OWNER]
    widened = next(
        r for r in rows.values() if other in r["participants"] and OWNER in r["participants"]
    )
    assert widened["messages"] == 1
    assert widened["dispositions"] == {"undeliverable": 1, "pending": 2}


def test_new_evidence_never_carries_bodies_notes_or_tool_arguments(system):
    runtime, now, reader = system
    invocation = start(runtime)
    analyst = runtime.lifecycle.hire(
        "founder", "analyst", "Keep this mandate", "main", request_id="hire"
    )["employee_id"]
    runtime.messaging.send(
        OWNER, ["founder", analyst], "message body", subject="Plan", request_id="note"
    )
    runtime.tools(invocation.token).handoff("secret notes", "wait", "handoff")
    tally(runtime, invocation.activation_id, {"Bash": 3, "mcp__firm__send": 1})
    with runtime.store.read() as db:
        ledger = db.execute(
            "SELECT payload_json FROM requests WHERE operation='handoff'"
        ).fetchone()
    assert "secret notes" in ledger["payload_json"]
    whole = json.dumps(read(reader))
    for secret in ("secret notes", "message body", "payload_json", "result_json", "rm -rf"):
        assert secret not in whole
    for section in SECTIONS:
        page = json.dumps(read(reader, section))
        assert "secret notes" not in page and "message body" not in page
    timeline = read(reader, "timeline", row_type="activation")["rows"][0]
    assert set(timeline["tool_calls"]) == {"Bash", "mcp__firm__send"}
    # The owner's own send carries no work period, so only the role's operations are here.
    assert timeline["operations"] == {"handoff": 1, "hire": 1}



def disk_mails(runtime):
    with runtime.store.read() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE subject LIKE 'Linux disk %' ORDER BY sequence")]


def record_linux_disk(runtime, now, used, *, config=None):
    with runtime.store.transaction() as db:
        analysis_history.record_resources(
            db, now, "linux",
            {"disk_used_bytes": used * 1024**3 if used is not None else None,
             "disk_total_bytes": 100 * 1024**3},
            notice=runtime.messaging.notice, sandbox_toml=config,
        )


def test_linux_owner_disk_mail_hysteresis_and_reentry(system):
    runtime, now, _ = system
    for used, subjects in [
        (84, []), (85, ["low"]), (90, ["low"]), (83, ["low"]), (80, ["low"]),
        (84, ["low"]), (85, ["low"]), (79, ["low", "recovered"]),
        (81, ["low", "recovered"]), (85, ["low", "recovered", "low"]),
    ]:
        record_linux_disk(runtime, now[0], used, config="/firm/sandbox.toml")
        assert [m["subject"] for m in disk_mails(runtime)] == [
            "Linux disk " + subject for subject in subjects]
        now[0] += 600
    low, recovery, _ = disk_mails(runtime)
    assert [r["employee_id"] for r in json.loads(low["recipients_json"])] == ["owner"]
    assert low["author_id"] == OPERATIONS
    assert low["body"] == (
        "The firm's Linux disk is 85% used (15.0 GiB free). Roles have been told and asked "
        "to delete what they no longer need; give them a work period or two before acting "
        "yourself. To see each role's usage with its sandbox role ID (r- followed by the "
        "employee ID), run `firm-sandbox --config /firm/sandbox.toml storage`. To empty a "
        "role's scratch and remove its containers, dangling images and build cache, "
        "run `firm-sandbox --config /firm/sandbox.toml dispose ROLE` with that ID while the "
        "role is not in a work period; the role is not told, so anything it kept in scratch "
        "is gone."
    )
    assert recovery["body"] == (
        "The firm's Linux disk is back to 79% used (21.0 GiB free). No action is needed.")


def test_linux_owner_disk_mail_ignores_unknown_and_other_machines(system):
    runtime, now, _ = system
    record_linux_disk(runtime, now[0], None)
    with runtime.store.transaction() as db:
        analysis_history.record_resources(
            db, now[0], "mac", {"disk_used_bytes": 95, "disk_total_bytes": 100},
            notice=runtime.messaging.notice)
    assert disk_mails(runtime) == []
    record_linux_disk(runtime, now[0], 90)
    assert disk_mails(runtime)[0]["body"].endswith(" PATH is the firm's sandbox.toml.")
    record_linux_disk(runtime, now[0], None)
    assert len(disk_mails(runtime)) == 1
    # Persisted alert state survives a gap in sampling.
    now[0] += 181
    record_linux_disk(runtime, now[0], 90)
    assert [m["subject"] for m in disk_mails(runtime)] == ["Linux disk low"]


def test_linux_owner_disk_notice_and_sample_commit_together(system):
    runtime, now, _ = system
    with pytest.raises(RuntimeError, match="rollback"):
        with runtime.store.transaction() as db:
            analysis_history.record_resources(
                db, now[0], "linux", {"disk_used_bytes": 90, "disk_total_bytes": 100},
                notice=runtime.messaging.notice)
            assert db.execute("SELECT COUNT(*) FROM resource_samples").fetchone()[0] == 1
            assert db.execute(
                "SELECT COUNT(*) FROM messages WHERE subject='Linux disk low'"
            ).fetchone()[0] == 1
            raise RuntimeError("rollback")
    assert disk_mails(runtime) == []
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM resource_samples").fetchone()[0] == 0
    record_linux_disk(runtime, now[0], 90)
    assert len(disk_mails(runtime)) == 1



def test_observer_disk_mail_uses_config_and_survives_observer_restart(system):
    runtime, now, _ = system
    sample = {"linux": {"disk_used_bytes": 90 * 1024**3, "disk_total_bytes": 100 * 1024**3}}
    machines = FakeMachines(sample)
    # Even with admissions stopped, a running VM supplies resource samples.
    assert runtime.status()["state"] == "stopped"
    for _ in range(2):
        observer = analysis_history.AnalysisObserver(
            runtime.store, machines, messaging=runtime.messaging,
            sandbox_toml="/firm/sandbox.toml")
        observer.resources()
        now[0] += 60
    assert len(disk_mails(runtime)) == 1
    assert "--config /firm/sandbox.toml storage" in disk_mails(runtime)[0]["body"]
    machines.samples = STOPPED
    observer.resources()
    assert len(recorded_samples(runtime)) == 2



def test_disk_alert_survives_hours_in_band_and_restart_then_recovers_and_reenters(system):
    runtime, now, _ = system
    record_linux_disk(runtime, now[0], 86, config="/firm path/sandbox.toml")
    assert "--config '/firm path/sandbox.toml' storage" in disk_mails(runtime)[0]["body"]
    for index in range(180):
        now[0] += 60
        record_linux_disk(runtime, now[0], 80 + index % 5)
    assert [m["subject"] for m in disk_mails(runtime)] == ["Linux disk low"]
    now[0] += 86400
    sample = {"linux": {"disk_used_bytes": 90 * 1024**3, "disk_total_bytes": 100 * 1024**3}}
    analysis_history.AnalysisObserver(
        runtime.store, FakeMachines(sample), messaging=runtime.messaging).resources()
    assert len(disk_mails(runtime)) == 1
    now[0] += 600
    record_linux_disk(runtime, now[0], 79.6)
    assert disk_mails(runtime)[-1]["body"] == (
        "The firm's Linux disk is back to 79% used (20.4 GiB free). No action is needed.")
    now[0] += 600
    record_linux_disk(runtime, now[0], 90)
    record_linux_disk(runtime, now[0], None)
    assert [m["subject"] for m in disk_mails(runtime)] == [
        "Linux disk low", "Linux disk recovered", "Linux disk low"]
    with runtime.store.read() as db:
        assert analysis_history._linux_disk_alerted(db)
        index = db.execute("PRAGMA index_info(resource_samples_by_machine)").fetchall()
        assert [column["name"] for column in index] == ["machine", "observed_at"]
