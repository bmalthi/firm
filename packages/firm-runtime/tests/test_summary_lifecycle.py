import json
import threading
import time
from types import SimpleNamespace

import pytest
from firm_sandbox import Run, RunSpec, Sandbox
from runtime_test_support import FakeProvider, FakeSandbox, summary_snapshot
from starlette.testclient import TestClient

from firm_runtime import summaries
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_runtime.errors import FirmError
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.summaries import SandboxSummaryRunner, SummaryLaunch, SummaryOutput


class SummarySandbox(FakeSandbox):
    def __init__(self, root):
        super().__init__(root)
        self.release_output = threading.Event()
        self.release_output.set()
        self.retired = threading.Event()
        self.gate = None
        self.provider_starts = 0
        self.output_chunks = [b"derived fixture"]
        self.read_chunks = 0

    def start(self, spec):
        run = super().start(spec)
        try:
            self.gate(spec.secrets["summary-token"].decode())
        except FirmError:
            self.invocations[run.run_id][1] = False
            self.invocations[run.run_id][3] = 70
        else:
            self.provider_starts += 1
        return run

    def follow(self, run):
        assert self.release_output.wait(3)
        for chunk in self.output_chunks:
            self.read_chunks += 1
            yield chunk
        if self.invocations[run.run_id][1]:
            self.invocations[run.run_id][1] = False
            self.invocations[run.run_id][3] = 0

    def stop(self, run, timeout=0):
        if run.run_id not in self.invocations:
            raise RuntimeError("invocation was already removed")
        result = super().stop(run, timeout)
        self.release_output.set()
        return result

    def retire(self, role_id):
        for key, state in tuple(self.invocations.items()):
            if state[0].role_id == role_id:
                assert not state[1]
                del self.invocations[key]
        self.retired.set()


@pytest.fixture
def setup(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "runtime" / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Test work",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "codex",
            "profiles": {"codex": {}},
        },
    )
    box = SummarySandbox(tmp_path / "sandbox")
    box.gate = runtime.summary_service.begin_execution

    def build(request):
        return RunSpec(
            "s-" + request.run_id[-32:],
            ("provider",),
            run_id=request.run_id,
            secrets={"summary-token": request.start_token.encode()},
        )

    runner = SandboxSummaryRunner(
        box,
        build,
        lambda request, logs, code: SummaryOutput(logs.decode(), 4),
        start_allowed=lambda: runtime.status()["state"] == "running",
    )

    class Providers(dict):
        def summary_runner(self, sandbox, *, mcp_url):
            return runner

    runtime.summary_service.capacity = None
    with SandboxDispatcher(
        runtime, box, Providers(codex=FakeProvider()), mcp_url="http://127.0.0.1:8000/mcp"
    ) as host:
        host.start()
        try:
            yield runtime, box, runner, host
        finally:
            box.release_start.set()
            box.release_output.set()
            runner.close()


def test_real_sandbox_facade_supports_summary_stream_and_cleanup(tmp_path):
    box = object.__new__(Sandbox)
    box.config = SimpleNamespace(locks=tmp_path / "locks", egress_mode="enforce", allow_hosts=())
    # Keep the facade's launch bookkeeping identical to Sandbox.__init__;
    # summary start/stop uses this condition while reconciling VM launches.
    box._launch_condition = threading.Condition()
    box._pending_launches = 0
    box._force_stopped = threading.Event()
    box._host = SimpleNamespace(
        ready=lambda: True, host_network=SimpleNamespace(ready=lambda: True)
    )
    retired = threading.Event()
    run = Run("s-123", "summary-123", "a" * 64)
    # Use a real generator, matching the backend's streaming transport.
    def follow(value):
        yield b"derived"

    # The facade delegates these operations to the guest controller. Keep the
    # test transport in-process while retaining the public VM lifecycle shape.
    box.start = lambda spec: run
    box.follow = follow
    box.status = lambda value: {"running": False, "exit_code": 0}
    box.stop = lambda value, timeout=0: {"running": False, "exit_code": 0}
    box.retire = lambda role_id: retired.set()
    runner = SandboxSummaryRunner(
        box,
        lambda request: RunSpec(run.role_id, ("provider",), run_id=run.run_id),
        lambda request, logs, code: SummaryOutput(logs.decode(), 1),
    )
    result = runner.wait(runner.start(SummaryLaunch(run.run_id, "fixture", 30)), 2)
    assert result.text == "derived"
    assert retired.wait(2)


def test_summary_inventory_is_recognized_and_private_resources_retired(setup):
    runtime, box, runner, host = setup
    box.release_output.clear()
    results = []
    call = threading.Thread(
        target=lambda: results.append(runtime.summary_service.summarize(summary_snapshot()))
    )
    call.start()
    assert box.start_created.wait(2)
    host.reconcile()
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
    box.release_output.set()
    call.join(2)
    assert not call.is_alive()
    assert results[0]["status"] == "summarized"
    assert box.retired.wait(2)
    host.reconcile()
    assert runner._handles == {}


def test_unregistered_summary_prefix_does_not_bypass_inventory_ownership(setup):
    runtime, box, runner, host = setup
    run = Run("s-unregistered", "summary-unregistered", "f" * 64)
    box.invocations[run.run_id] = [run, True, b"", None]
    with pytest.raises(FirmError, match="not registered"):
        host.reconcile()
    host.stop(force=True)
    host.tick()
    assert not box.live


def test_failed_launch_handle_requires_inventory_after_launch_settles(setup):
    runtime, box, runner, host = setup
    run_id, _ = runtime.summary_service._reserve(summary_snapshot(), None)
    handle = summaries._SummaryHandle(SummaryLaunch(run_id, "fixture", 30))
    runner._handles[run_id] = handle
    handle.launch_pending = False
    handle.result.set_exception(RuntimeError("sandbox call failed before creation"))
    records = [{"run_id": run_id}]
    runner.reconcile({}, records, now=0, protected={run_id})
    assert run_id in runner._handles
    assert not handle.exit_confirmed
    runner.reconcile({}, records, now=0)
    assert runner._handles == {}


def test_new_failed_launch_cannot_use_inventory_from_before_its_reservation(setup, monkeypatch):
    runtime, box, runner, host = setup
    original_inventory = host._inventory
    created = []

    def create_after_inventory():
        inventory = original_inventory()
        run_id, _ = runtime.summary_service._reserve(summary_snapshot(), None)
        handle = summaries._SummaryHandle(SummaryLaunch(run_id, "fixture", 30))
        handle.launch_pending = False
        handle.result.set_exception(RuntimeError("sandbox call failed after creation"))
        runner._handles[run_id] = handle
        run = Run("s-" + run_id[-32:], run_id, "f" * 64)
        handle.run = run
        box.invocations[run_id] = [run, True, b"", None]
        created.append(handle)
        return inventory

    monkeypatch.setattr(host, "_inventory", create_after_inventory)
    host.reconcile()
    handle = created[0]
    assert not handle.exit_confirmed
    assert handle.request.run_id in runner._handles
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
    monkeypatch.setattr(host, "_inventory", original_inventory)
    host.stop(force=True)
    host.tick()


def test_one_late_summary_is_powered_off_while_another_launch_remains_pending(setup, monkeypatch):
    runtime, box, runner, host = setup
    handles = []
    for _ in range(2):
        run_id, _ = runtime.summary_service._reserve(summary_snapshot(), None)
        handle = summaries._SummaryHandle(SummaryLaunch(run_id, "fixture", 30))
        runner._handles[run_id] = handle
        handles.append(handle)
    first, second = handles
    original_status = box.infrastructure_status
    observed = False

    def late_create_after_first_power_off_observation():
        nonlocal observed
        status = original_status()
        if not observed:
            observed = True
            first.launch_pending = False
            first.result.set_exception(summaries.SummaryTimedOut())
            box.live = True  # A late sandbox call creates after the observed off state.
        return status

    monkeypatch.setattr(box, "infrastructure_status", late_create_after_first_power_off_observation)
    try:
        host.stop(force=True)
        host.tick()
        assert box.force_downs == 2
        assert not box.live
        assert runtime.status()["state"] == "stopping"
        with runtime.store.read() as db:
            states = dict(db.execute("SELECT run_id,state FROM summary_runs"))
        assert states[first.request.run_id] == "unavailable"
        assert states[second.request.run_id] == "running"
        assert not second.exit_confirmed
    finally:
        second.launch_pending = False
        second.result.set_exception(summaries.SummaryTimedOut())
        host.tick()


def test_pending_start_timeout_and_dead_network_hard_stop_retain_ownership(setup, monkeypatch):
    runtime, box, runner, host = setup
    monkeypatch.setattr(summaries, "SUMMARY_TIMEOUT_SECONDS", 0.05)
    box.block_start = True
    box.resurrect_on_release = True
    began = time.monotonic()
    result = runtime.summary_service.summarize(summary_snapshot())
    assert time.monotonic() - began < 0.5
    assert result["reason"] == "timeout_unconfirmed"
    assert runner.pending_launches()
    host.reconcile()
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
    box.unavailable = True
    host.stop(force=True)
    host.tick()
    assert not box.live
    assert runtime.status()["state"] == "stopping"
    box.release_start.set()
    for handle in tuple(runner._handles.values()):
        with pytest.raises(summaries.SummaryTimedOut):
            handle.result.result(timeout=2)
    assert box.provider_starts == 0  # The late launch cannot pass the closed gate.
    host.tick()
    assert not box.live
    assert runtime.status()["state"] == "stopped"
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "unavailable"


def test_stream_limit_stops_collection_before_unbounded_allocation(setup):
    runtime, box, runner, host = setup
    box.output_chunks = [b"x" * 8192] * 100
    result = runtime.summary_service.summarize(summary_snapshot())
    assert result["status"] == "unavailable"
    assert box.read_chunks <= summaries.MAX_SUMMARY_LOG_BYTES // 8192 + 1
    assert box.retired.wait(2)
    host.reconcile()
    with runtime.store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "unavailable"


@pytest.mark.parametrize("closure", ["drain", "hard", "expired", "replay"])
def test_summary_gate_is_one_use_and_refuses_closed_or_expired_launches(setup, closure):
    runtime, box, runner, host = setup
    run_id, reservation = runtime.summary_service._reserve(summary_snapshot(), None)
    token = reservation["start_token"]
    with pytest.raises(FirmError):
        runtime.authenticate(token)
    if closure in {"drain", "hard"}:
        host.stop(force=closure == "hard")
    elif closure == "expired":
        with runtime.store.transaction() as db:
            db.execute("UPDATE summary_runs SET deadline=0 WHERE run_id=?", (run_id,))
    else:
        assert runtime.summary_service.begin_execution(token)["run_id"] == run_id
    with pytest.raises(FirmError):
        runtime.summary_service.begin_execution(token)


def test_http_summary_gate_accepts_only_the_separate_one_use_token(setup):
    runtime, box, runner, host = setup
    run_id, reservation = runtime.summary_service._reserve(summary_snapshot(), None)
    app = build_http_app(
        runtime, allowed_hosts=["testserver"], allowed_origins=["http://testserver"]
    )
    headers = {"Authorization": f"Bearer {reservation['start_token']}"}
    with TestClient(app) as client:
        assert client.post("/summary/start").status_code == 401
        assert client.post("/activation/start", headers=headers).status_code == 401
        response = client.post("/summary/start", headers=headers)
        assert response.status_code == 200
        assert response.json()["run_id"] == run_id
        assert client.post("/summary/start", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "error,reason",
    [
        ("process_exit", "process_exit"),
        ("result_error", "result_error"),
        ("provider_error", "provider_error"),
        ("Unbounded provider prose\nwith a newline", "provider_error"),
        ("x" * 65, "provider_error"),
    ],
)
def test_provider_failure_records_its_short_reason_and_keeps_known_usage(setup, error, reason):
    runtime, box, runner, host = setup
    runner.parse_output = lambda *args: SummaryOutput(
        "",
        9,
        {
            "input_tokens": 41,
            "cached_input_tokens": 12,
            "output_tokens": 9,
            "reasoning_output_tokens": 3,
            "resolved_model": "reported-model",
        },
        error=error,
    )
    result = runtime.summary_service.summarize(summary_snapshot())
    assert result["reason"] == reason
    with runtime.store.read() as db:
        row = db.execute(
            "SELECT input_tokens,cached_input_tokens,output_token_usage,reasoning_output_tokens,"
            "state,reason,resolved_model FROM summary_runs"
        ).fetchone()
    assert tuple(row) == (41, 12, 9, 3, "unavailable", reason, "reported-model")


@pytest.mark.parametrize("provider", [CodexProvider, ClaudeProvider])
def test_a_cli_that_refuses_to_start_is_a_process_exit(tmp_path, provider):
    adapter = provider(model="fixture", effort="medium", auth_file=tmp_path / "unused-auth")
    logs = b"error: bubblewrap is required for subprocess env scrubbing and isolation\n"
    assert adapter.parse_summary(logs, 1).error == "process_exit"
    assert adapter.parse_summary(b"", 0).error == "invalid_output"


def test_a_completed_claude_turn_without_text_is_left_to_the_empty_summary_check(tmp_path):
    adapter = ClaudeProvider(model="fixture", effort="medium", auth_file=tmp_path / "unused")
    session = "0b6b7c52-2f0e-4c1a-9d8e-3a5b0f1e2d3c"
    logs = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "session_id": session,
        }
    )
    output = adapter.parse_summary(logs, 0)
    assert (output.text, output.error) == ("", None)


@pytest.mark.parametrize(
    "provider,event,reason",
    [
        (
            CodexProvider,
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 7}},
            "process_exit",
        ),
        (
            ClaudeProvider,
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "usage": {"input_tokens": 20, "output_tokens": 7},
            },
            "result_error",
        ),
    ],
)
def test_real_provider_parsers_preserve_usage_on_failed_exit(tmp_path, provider, event, reason):
    adapter = provider(model="fixture", effort="medium", auth_file=tmp_path / "unused-auth")
    output = adapter.parse_summary(json.dumps(event), 1)
    assert output.error == reason
    assert output.usage["input_tokens"] == 20
    assert output.usage["output_tokens"] == 7


def test_unexpected_off_observation_does_not_release_a_launch_that_settles_after_it(
    setup, monkeypatch
):
    runtime, box, runner, host = setup
    run_id, _ = runtime.summary_service._reserve(summary_snapshot(), None)
    handle = summaries._SummaryHandle(SummaryLaunch(run_id, "fixture", 30))
    runner._handles[run_id] = handle
    host.stop()
    original_status = box.infrastructure_status

    def stale_off_observation():
        handle.launch_pending = False
        handle.result.set_exception(summaries.SummaryTimedOut())
        box.live = True
        return {"running": False}

    monkeypatch.setattr(box, "infrastructure_status", stale_off_observation)
    try:
        with pytest.raises(FirmError, match="ownership is unresolved"):
            host.tick()
        assert not handle.exit_confirmed
        with runtime.store.read() as db:
            assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
    finally:
        monkeypatch.setattr(box, "infrastructure_status", original_status)
        host.stop(force=True)
        host.tick()
