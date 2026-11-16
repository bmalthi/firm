"""Allowance contracts through the real parsing and persistence consumers."""

import json
from types import SimpleNamespace

import pytest

from firm_runtime import claude_provider, codex_provider
from firm_runtime.providers import Providers, record_task_output
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store


def allowance(provider, *, empty=False):
    if provider == "claude":
        event = {
            "type": "rate_limit_event",
            "uuid": "empty" if empty else "valid",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {}
                if empty
                else {
                    "five_hour": {"utilization": 0.7996, "resetsAt": 2000},
                },
            },
        }
    else:
        event = {
            "type": "firm.telemetry",
            "provider": "codex",
            "usage": {},
            "rate_limits": {}
            if empty
            else {
                "limit_id": "codex_other",
                "primary": {"used_percent": 132, "resets_at": 2000},
            },
        }
    return json.dumps(event).encode()


def completed(provider):
    session = "11111111-1111-4111-8111-111111111111"
    if provider == "claude":
        events = [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done",
                "session_id": session,
            }
        ]
    else:
        events = [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "turn.completed"},
        ]
    return "\n".join(json.dumps(event) for event in events).encode()


@pytest.fixture(params=["claude", "codex"])
def observation_system(request, tmp_path):
    provider = request.param
    now = [1000]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test",
        "Work",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "p",
            "profiles": {
                "p": {"provider": provider, "model": "test", "effort": "high", "accounts": ["a"]}
            },
            "accounts": {"a": {"provider": provider, "auth_file": "/unused", "max_active": 1}},
        },
    )
    return runtime, now, provider


@pytest.mark.parametrize("path", ["role", "summary", "staffing", "task"])
def test_observations_keep_exact_window_shapes_and_empty_replacement(observation_system, path):
    runtime, now, provider = observation_system
    parser = claude_provider if provider == "claude" else codex_provider
    if path == "role":
        runtime.start()
        invocation = runtime.admit()
        dispatcher = SandboxDispatcher.__new__(SandboxDispatcher)
        dispatcher.runtime = runtime
        dispatcher._subscription_events = {}
        dispatcher._provider = lambda row: parser
        row = {"activation_id": invocation.activation_id, "session_id": invocation.session_id}
        def consume(logs):
            return dispatcher._observe_usage(row, logs)
    elif path in ("summary", "staffing"):
        runner = Providers(runtime).summary_runner(None, mcp_url="http://localhost:8421/mcp")
        request = SimpleNamespace(provider=provider, account_id="a", kind=path)
        def consume(logs):
            return runner.parse_output(request, logs, 0)
    else:
        def consume(logs):
            return record_task_output(
                    runtime,
                    "unused",
                    provider,
                    "a",
                    logs,
                    0,
                    apply_holds=True,
                )
    logs = allowance(provider) + b"\n" + completed(provider)
    consume(logs)
    expected = (
        [{"window": "five_hour", "used_percent": 79.96, "resets_at": 2000.0}]
        if provider == "claude"
        else [
            {
                "window": "codex_other_primary",
                "limit_id": "codex_other",
                "used_percent": 132.0,
                "resets_at": 2000.0,
            }
        ]
    )
    with runtime.store.read() as db:
        value = runtime.accounts.subscription_reading(db, "a")
        assert value == {"observed_at": 1000, "windows": expected}
        stored = db.execute("SELECT windows_json FROM subscription_readings").fetchone()[0]
        assert json.loads(stored) == expected
    now[0] += 1
    # Irrelevant output and a replayed role event do not refresh the reading.
    consume(completed(provider))
    with runtime.store.read() as db:
        assert runtime.accounts.subscription_reading(db, "a") == value
    consume(logs + b"\n" + allowance(provider, empty=True))
    with runtime.store.read() as db:
        assert runtime.accounts.subscription_reading(db, "a") == {
            "observed_at": 1001,
            "windows": [],
        }
        assert db.execute("SELECT windows_json FROM subscription_readings").fetchone()[0] == "[]"
        assert db.execute("SELECT COUNT(*) FROM account_observations").fetchone()[0] == 0


@pytest.mark.parametrize("event", [False, True])
def test_recovered_task_never_creates_subscription_evidence(observation_system, event):
    runtime, _, provider = observation_system
    logs = (allowance(provider) + b"\n" if event else b"") + completed(provider)
    record_task_output(runtime, "unused", provider, "a", logs, 0, apply_holds=False)
    with runtime.store.read() as db:
        assert runtime.accounts.subscription_reading(db, "a") is None
        assert db.execute("SELECT COUNT(*) FROM execution_incidents").fetchone()[0] == 0
