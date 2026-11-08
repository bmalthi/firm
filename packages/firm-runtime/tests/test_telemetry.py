import json

import pytest
from runtime_test_support import provider_usage as usage
from runtime_test_support import start_activation as start
from runtime_test_support import telemetry_runtime as make_runtime

from firm_runtime.errors import FirmError
from firm_runtime.messaging import MAX_PEEK_BYTES, MAX_RESULT_BYTES
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store, encoded
from firm_runtime.telemetry import context_policy


@pytest.mark.parametrize(
    ("provider", "model", "window"),
    [
        ("claude", "opus", 500_000),
        ("claude", "claude-fable-5-1", 500_000),
        ("claude", "sonnet", 500_000),
        ("agy", "gemini-3.8-flash", None),
        ("codex", "gpt-5.6-sol", 256_000),
        ("codex", "gpt-6-astra", 256_000),
    ],
)
def test_provider_soft_window_defaults(provider, model, window):
    assert context_policy({"provider": provider, "model": model}) == (window, 0.6, 10_000)
    assert context_policy({"provider": provider, "context_window": 400_000})[0] == 400_000


def test_provider_usage_rejects_invalid_structured_counters():
    with pytest.raises(ValueError, match="current_context_tokens"):
        ProviderUsage(current_context_tokens=-1)
    with pytest.raises(ValueError, match="usage source"):
        ProviderUsage(source="provider transcript")
    with pytest.raises(ValueError, match="provider_context_window"):
        ProviderUsage(provider_context_window=0)
    for invalid in ("", " ", 4, {}):
        with pytest.raises(ValueError, match="resolved_model"):
            ProviderUsage(resolved_model=invalid)


def test_model_only_updates_are_persisted_and_idempotent(tmp_path):
    runtime = make_runtime(tmp_path)
    invocation = start(runtime)
    for model in ("claude-opus-4-1", "claude-opus-4-6"):
        reading = ProviderUsage(resolved_model=model, input_tokens=10, source="test")
        assert runtime.record_usage(invocation.activation_id, reading)
        assert not runtime.record_usage(invocation.activation_id, reading)
    # A later partial reading cannot erase an identity the provider already reported.
    runtime.record_usage(invocation.activation_id, ProviderUsage(input_tokens=20, source="test"))
    with runtime.store.read() as db:
        row = db.execute("SELECT * FROM activation_usage").fetchone()
        assert row["resolved_model"] == "claude-opus-4-6"
        assert row["input_tokens"] == 20
        session = db.execute("SELECT cumulative_input_tokens FROM context_sessions").fetchone()
        assert session[0] == 20


def test_partial_usage_preserves_unknown_totals_and_compactions(tmp_path):
    runtime = make_runtime(tmp_path)
    invocation = start(runtime)
    runtime.record_usage(
        invocation.activation_id,
        ProviderUsage(current_context_tokens=123, source="partial_provider"),
    )
    with runtime.store.read() as db:
        session = db.execute("SELECT * FROM context_sessions").fetchone()
        activation = db.execute("SELECT * FROM activation_usage").fetchone()
    assert session["cumulative_input_tokens"] is None
    assert session["cumulative_cached_input_tokens"] is None
    assert session["cumulative_output_tokens"] is None
    assert session["cumulative_reasoning_output_tokens"] is None
    assert session["native_compactions"] is None
    assert activation["native_compactions"] is None


def test_historical_usage_backfills_finished_activation_without_touching_resumed_session(tmp_path):
    now = [1000.0]
    runtime = make_runtime(tmp_path, clock=lambda: now[0])
    first = start(runtime)
    runtime.record_usage(first.activation_id, usage(100, inputs=10))
    runtime.finish(first.activation_id)
    runtime.messaging.send(OWNER, ["founder"], "More work", request_id="more")
    now[0] = 2000.0
    second = start(runtime)
    assert second.session_id == first.session_id
    runtime.record_usage(second.activation_id, usage(500, inputs=50))
    with runtime.store.read() as db:
        before = dict(db.execute("SELECT * FROM context_sessions").fetchone())
        event_count = db.execute("SELECT COUNT(*) FROM analysis_events").fetchone()[0]
    old_usage = ProviderUsage(
        current_context_tokens=200,
        high_water_context_tokens=200,
        input_tokens=20,
        session_input_tokens=20,
        source="recovered_journal",
        resolved_model="reported-model",
    )
    assert not runtime.record_usage(first.activation_id, old_usage)
    assert runtime.record_usage(first.activation_id, old_usage, historical=True)
    assert not runtime.record_usage(first.activation_id, old_usage, historical=True)
    with runtime.store.read() as db:
        assert dict(db.execute("SELECT * FROM context_sessions").fetchone()) == before
        assert db.execute("SELECT COUNT(*) FROM analysis_events").fetchone()[0] == event_count
        history = dict(
            db.execute(
                "SELECT * FROM activation_usage WHERE activation_id=?",
                (first.activation_id,),
            ).fetchone()
        )
    assert history["current_context_tokens"] == 200
    assert history["input_tokens"] == 20
    assert history["observed_at"] == 1000.0
    assert history["source"] == "recovered_journal"
    assert history["resolved_model"] == "reported-model"


def test_role_guidance_and_claim_gate_use_current_context_and_packaged_checklist(tmp_path):
    runtime = make_runtime(tmp_path)
    message = runtime.messaging.send(
        OWNER, ["founder"], "A new assignment body", request_id="assignment"
    )
    invocation = start(runtime)

    assert runtime.record_usage(invocation.activation_id, usage(6_500))
    advisory = runtime.tools(invocation.token).peek()["context_pressure"]
    assert advisory["level"] == "advisory"
    assert advisory["window_source"] == "configured_soft_window"
    assert advisory["change_since_previous_guidance"] is None

    assert runtime.record_usage(invocation.activation_id, usage(9_100, inputs=200))
    blocked = runtime.tools(invocation.token).claim("claim", [message["message_id"]])
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "context_handoff_required"
    pressure = blocked["context_pressure"]
    assert pressure["level"] == "handoff_required"
    assert "unfinished work" in pressure["handoff_checklist"]
    assert 'handoff(notes, next="continue"|"wait")' in pressure["handoff_checklist"]
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT disposition FROM message_deliveries WHERE message_id=?",
                (message["message_id"],),
            ).fetchone()[0]
            == "pending"
        )

    # Native compaction lowers current occupancy without erasing the session high-water.
    runtime.record_usage(
        invocation.activation_id, usage(2_000, high=9_100, compactions=1, inputs=250)
    )
    claimed = runtime.tools(invocation.token).claim("after-compact", [message["message_id"]])
    assert claimed["messages"][0]["body"] == "A new assignment body"
    assert claimed["context_pressure"]["level"] == "normal"
    diagnostics = runtime.usage_diagnostics()
    assert diagnostics["sessions"][0]["high_water_context_tokens"] == 9_100
    assert diagnostics["sessions"][0]["native_compactions"] == 1


def test_required_handoff_and_drain_controls_fit_reserved_result_space(tmp_path):
    runtime = make_runtime(tmp_path)
    invocation = start(runtime)
    runtime.record_usage(invocation.activation_id, usage(9_500))
    runtime.stop()
    result = runtime.tools(invocation.token).peek()
    controls = {
        "context_pressure": result["context_pressure"],
        "operations_control": result["operations_control"],
    }
    size = len(encoded(controls).encode())
    assert size <= 64 * 1024 - MAX_RESULT_BYTES
    assert size <= 16 * 1024 - MAX_PEEK_BYTES


def test_provider_native_window_is_used_only_when_structured_and_config_is_absent(tmp_path):
    runtime = make_runtime(tmp_path, context_window=None)
    invocation = start(runtime)
    runtime.record_usage(invocation.activation_id, usage(13_000, native_window=20_000))
    pressure = runtime.tools(invocation.token).peek()["context_pressure"]
    assert pressure["window_source"] == "provider_reported_native_window"
    assert pressure["level"] == "advisory"

    runtime2 = make_runtime(tmp_path / "unknown", context_window=None)
    invocation2 = start(runtime2)
    runtime2.record_usage(
        invocation2.activation_id,
        ProviderUsage(input_tokens=10, source="terminal_only"),
    )
    assert "context_pressure" not in runtime2.tools(invocation2.token).peek()


@pytest.mark.parametrize("observation", [None, usage(9_500)])
def test_usage_time_peek_and_completion_do_not_send_mail_or_wake_managers(tmp_path, observation):
    now = [1000.0]
    runtime = make_runtime(tmp_path, clock=lambda: now[0])
    founder = start(runtime)
    child = runtime.tools(founder.token).hire(
        "analyst", "Own analyst tasks.", "hire-analyst", temporary_periods=1
    )["employee_id"]
    runtime.finish(founder.activation_id)
    analyst = start(runtime)
    assert analyst.employee_id == child
    with runtime.store.read() as db:
        before_messages = list(db.execute("SELECT * FROM messages"))
        before_wakes = list(db.execute("SELECT * FROM scheduling_opportunities"))
    for elapsed in (0, 900, 1800):
        now[0] += elapsed
        runtime.record_usage(analyst.activation_id, observation)
        runtime.messaging.peek(founder.employee_id)
        runtime.tools(analyst.token).peek()
    runtime.finish(analyst.activation_id)
    with runtime.store.read() as db:
        assert list(db.execute("SELECT * FROM messages")) == before_messages
        assert list(db.execute("SELECT * FROM scheduling_opportunities")) == before_wakes
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='workload_notice_state'"
            ).fetchone()
            is None
        )
    assert runtime.admit() is None
    with pytest.raises(FirmError, match="only the owner"):
        runtime.usage_diagnostics("founder")


def test_retained_session_policy_correction_keeps_execution_identity(tmp_path):
    config = {
        "max_active": 1,
        "staffing": False,
        "founder_profile": "primary",
        "accounts": {
            "a": {
                "provider": "codex",
                "auth_file": "/missing/a",
                "max_active": 1,
            }
        },
        "profiles": {
            "primary": {
                "provider": "codex",
                "model": "test",
                "effort": "low",
                "accounts": ["a"],
                "context_window": 10_000,
                "handoff_reserve": 1_000,
            }
        },
    }
    runtime = Runtime(Store.scratch(tmp_path / "retained.sqlite3"))
    runtime.lifecycle.initialize("test", "Build.", config)
    runtime.start()
    invocation = runtime.admit()
    with runtime.store.read() as db:
        before = dict(runtime.accounts.binding(db, invocation.session_id))
    revised = json.loads(json.dumps(config))
    revised["profiles"]["primary"].update(
        context_window=20_000, context_advisory_fraction=0.7, handoff_reserve=2_000
    )
    result = runtime.owner.configure(OWNER, revised)
    with runtime.store.read() as db:
        after = dict(runtime.accounts.binding(db, invocation.session_id))
        policy = db.execute(
            "SELECT * FROM context_sessions WHERE session_id=?", (invocation.session_id,)
        ).fetchone()
    assert result["retained_context_policies_updated"] == 1
    assert (after["provider"], after["account_id"]) == (before["provider"], before["account_id"])
    assert json.loads(after["settings_json"])["model"] == "test"
    saved_policy = (
        policy["soft_context_window"],
        policy["advisory_fraction"],
        policy["handoff_reserve"],
    )
    assert saved_policy == (
        20_000,
        0.7,
        2_000,
    )


@pytest.mark.parametrize("projected", [False, True])
def test_codex_compaction_invalidates_persisted_occupancy_until_remeasured(tmp_path, projected):
    from firm_runtime.codex_provider import parse_usage

    runtime = make_runtime(tmp_path)
    actor = start(runtime)
    sent = runtime.messaging.send(OWNER, [actor.employee_id], "Work", request_id="work")
    info = {
        "last_token_usage": {"total_tokens": 9500},
        "total_token_usage": {"input_tokens": 12000},
        "model_context_window": 10000,
    }

    def measurement(info):
        return (
            {"type": "firm.telemetry", "provider": "codex", "usage": info}
            if projected
            else {"type": "event_msg", "payload": {"type": "token_count", "info": info}}
        )

    events = [measurement(info)]

    def record():
        observed = parse_usage("\n".join(json.dumps(e) for e in events))
        runtime.record_usage(actor.activation_id, observed)
        return observed

    record()
    tools = runtime.tools(actor.token)
    assert tools.claim("blocked", [sent["message_id"]])["status"] == "blocked"
    events.append(
        {"type": "firm.telemetry", "provider": "codex", "native_compaction": True}
        if projected
        else {"type": "event_msg", "payload": {"type": "context_compacted"}}
    )
    observed = record()
    assert observed.current_context_tokens is None
    assert observed.high_water_context_tokens == 9500
    assert observed.session_input_tokens == 12000
    assert observed.provider_context_window == 10000
    assert observed.native_compactions == 1
    assert "context_pressure" not in tools.peek()
    assert tools.peek()["context"]["self"]["context"]["state"] == "unknown"
    assert tools.claim("after-compaction", [sent["message_id"]])["messages"]
    with runtime.store.read() as db:
        assert (
            db.execute("SELECT current_context_tokens FROM context_sessions").fetchone()[0] is None
        )
        transition = json.loads(
            db.execute(
                "SELECT payload_json FROM analysis_events WHERE kind='context.pressure_changed' "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert transition["value"] is None
        assert transition["previous"] == "handoff_required"
    events.append(measurement({"last_token_usage": {"total_tokens": 1000}}))
    assert record().current_context_tokens == 1000
    assert tools.peek()["context_pressure"]["level"] == "normal"
