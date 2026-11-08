import json
import threading
from dataclasses import replace

import pytest
from runtime_test_support import summary_snapshot

from firm_runtime.codex_provider import CodexProvider
from firm_runtime.store import Store
from firm_runtime.summaries import (
    MAX_CONTEXT_BYTES,
    MAX_SUMMARY_CONCURRENCY,
    MAX_SUMMARY_PROMPT_BYTES,
    MAX_SUMMARY_TEXT_BYTES,
    SandboxSummaryRunner,
    SummaryInputTooLarge,
    SummaryLaunch,
    SummaryOutput,
    SummaryService,
    SummaryTimedOut,
    select_context,
)


def row(message_id, sequence, participants, body="body"):
    return {
        "message_id": message_id,
        "sequence": sequence,
        "participants_json": json.dumps(participants),
        "body": body,
    }


def envelope(value):
    return {
        "message_id": value["message_id"],
        "participants": json.loads(value["participants_json"]),
        "body": value["body"],
    }


def test_context_selects_newest_first_then_renders_chronologically():
    people = [{"employee_id": "a"}, {"employee_id": "b"}]
    rows = [
        row("m4", 4, people, "latest"),
        row("m1", 1, people),
        row("m2", 2, people, "second"),
    ]
    snapshot = select_context(rows, "c1", envelope=envelope)
    assert snapshot.conversation_id == "c1"
    assert snapshot.source_message_ids == ("m1", "m2", "m4")
    assert [item["message_id"] for item in snapshot.messages] == ["m1", "m2", "m4"]
    assert snapshot.matching_count == 3


def test_context_keeps_newest_oversized_envelope_as_bounded_excerpt():
    people = [{"employee_id": "a"}, {"employee_id": "b"}]
    rows = [row("m1", 1, people, "x" * (MAX_CONTEXT_BYTES * 2))]
    snapshot = select_context(rows, "c1", envelope=envelope)
    assert snapshot.source_message_ids == ("m1",)
    assert snapshot.input_bytes <= MAX_CONTEXT_BYTES
    assert {item["reason"] for item in snapshot.omissions} == {"source_envelope_truncated"}


def test_context_omits_oversized_older_envelope_without_truncating_it():
    people = [{"employee_id": "a"}]
    rows = [
        row("new", 2, people, "newest"),
        row("old", 1, people, "x" * MAX_CONTEXT_BYTES),
    ]
    snapshot = select_context(rows, "c1", envelope=envelope)
    assert snapshot.source_message_ids == ("new",)
    assert snapshot.omissions[0]["message_id"] == "old"
    assert snapshot.omissions[0]["reason"] == "source_byte_limit"


def test_context_presents_times_as_iso_utc_and_measures_the_rendered_text():
    people = [{"employee_id": "a"}]

    def timed(value):
        return {**envelope(value), "created_at": value["created_at"]}

    rows = [
        {**row("m2", 2, people, "x" * (MAX_CONTEXT_BYTES * 2)), "created_at": 1789604700.9},
        {**row("m1", 1, people, "first"), "created_at": 1789604683.100898},
    ]
    snapshot = select_context(rows, "c1", envelope=timed)
    assert [item["created_at"] for item in snapshot.messages] == ["2026-09-17T00:25:00Z"]
    assert snapshot.omissions[0]["reason"] == "source_envelope_truncated"
    prompt = snapshot.prompt()
    assert '"created_at":"2026-09-17T00:25:00Z"' in prompt
    assert "1789604700" not in prompt
    sources = prompt.split("\n", 1)[1].rstrip("\n")
    assert len(sources.encode()) == snapshot.input_bytes <= MAX_CONTEXT_BYTES

    first = select_context(rows[1:], "c1", envelope=timed)
    assert '"created_at":"2026-09-17T00:24:43Z"' in first.prompt()
    assert rows[1]["created_at"] == 1789604683.100898


class BlockingProvider:
    def __init__(self):
        self.started = threading.Event()
        self._lock = threading.Lock()
        self._count = 0
        self.release = threading.Event()

    def summarize(self, prompt, *, timeout):
        with self._lock:
            self._count += 1
            if self._count == MAX_SUMMARY_CONCURRENCY:
                self.started.set()
        self.started.wait(timeout=2)
        self.release.wait(timeout=2)
        return "derived"


class FunctionRunner:
    """Adapt a test function to the production SummaryRunner contract."""

    def __init__(self, provider):
        self.provider = provider

    def start(self, request):
        return request

    def wait(self, handle, timeout):
        value = self.provider.summarize(handle.prompt, timeout=timeout)
        return value if isinstance(value, SummaryOutput) else SummaryOutput(value)

    def stop(self, handle):
        return True

    def active_run_ids(self):
        return set()


def test_summary_service_returns_busy_without_queueing_beyond_its_concurrency():
    provider = BlockingProvider()
    service = SummaryService(FunctionRunner(provider))
    outputs = []
    threads = [
        threading.Thread(target=lambda: outputs.append(service.summarize(summary_snapshot())))
        for _ in range(MAX_SUMMARY_CONCURRENCY)
    ]
    for thread in threads:
        thread.start()
    assert provider.started.wait(timeout=2)
    busy = service.summarize(summary_snapshot())
    assert busy["status"] == "unavailable"
    assert busy["reason"] == "busy"
    provider.release.set()
    for thread in threads:
        thread.join(timeout=2)
    assert [item["status"] for item in outputs] == ["summarized"] * MAX_SUMMARY_CONCURRENCY


def test_summary_service_normalizes_timeout_without_retry():
    class TimeoutProvider:
        def summarize(self, prompt, *, timeout):
            raise SummaryTimedOut

    result = SummaryService(FunctionRunner(TimeoutProvider())).summarize(summary_snapshot())
    assert result["status"] == "unavailable"
    assert result["reason"] == "timeout"


def test_summary_service_persists_usage_and_latency(tmp_path):
    class Provider:
        def summarize(self, prompt, *, timeout):
            return SummaryOutput("derived", output_tokens=7, usage={"output_tokens": 7})

    store = Store.scratch(tmp_path / "firm.sqlite3")
    result = SummaryService(FunctionRunner(Provider()), store=store).summarize(
        summary_snapshot()
    )
    assert result["output_tokens"] == 7
    with store.read() as db:
        run = db.execute(
            "SELECT state,input_bytes,output_tokens,latency_ms FROM summary_runs"
        ).fetchone()
    assert tuple(run) == ("summarized", result["coverage"]["input_bytes"], 7, 0)


@pytest.mark.parametrize("kind", ["summary", "staffing"])
@pytest.mark.parametrize("output_tokens", [None, 1_000_000])
def test_isolated_turn_keeps_answer_regardless_of_reported_tokens(kind, output_tokens):
    class Provider:
        def summarize(self, prompt, *, timeout):
            return SummaryOutput(
                "é" * (MAX_SUMMARY_TEXT_BYTES // 2),
                output_tokens=output_tokens,
                usage={"output_tokens": output_tokens},
            )

    result = SummaryService(FunctionRunner(Provider())).run_isolated("derive", kind=kind)
    assert result["status"] == "completed"
    assert result["text"] == "é" * (MAX_SUMMARY_TEXT_BYTES // 2)
    assert "truncated" not in result
    if output_tokens is not None:
        assert result["output_tokens"] == output_tokens
        assert result["usage"] == {"output_tokens": output_tokens}
    else:
        assert "output_tokens" not in result
        assert "usage" not in result


@pytest.mark.parametrize("output_tokens", [None, 1_000_000])
def test_summary_cuts_overlong_text_at_utf8_boundary_with_marker(output_tokens):
    original = "é界🙂" * 2000

    class Provider:
        def summarize(self, prompt, *, timeout):
            return SummaryOutput(
                original, output_tokens=output_tokens, usage={"output_tokens": output_tokens}
            )

    result = SummaryService(FunctionRunner(Provider())).run_isolated("derive")
    assert result["status"] == "completed"
    assert result["truncated"] is True
    kept, marker = result["text"].rsplit("\n", 1)
    assert marker == "[THIS SUMMARY WAS CUT OFF at 16 KiB; the rest was not delivered.]"
    assert kept and original.startswith(kept)
    assert MAX_SUMMARY_TEXT_BYTES - 4 < len(result["text"].encode("utf-8")) <= (
        MAX_SUMMARY_TEXT_BYTES
    )
    if output_tokens is not None:
        assert result["output_tokens"] == output_tokens
        assert result["usage"] == {"output_tokens": output_tokens}


@pytest.mark.parametrize("kind", ["staffing", "other"])
@pytest.mark.parametrize("output_tokens", [None, 1_000_000])
def test_isolated_turn_rejects_answer_over_text_byte_cap(kind, output_tokens):
    class Provider:
        def summarize(self, prompt, *, timeout):
            return SummaryOutput(
                "é" * (MAX_SUMMARY_TEXT_BYTES // 2) + "x",
                output_tokens=output_tokens,
                usage={"output_tokens": output_tokens},
            )

    result = SummaryService(FunctionRunner(Provider())).run_isolated("derive", kind=kind)
    assert result["status"] == "unavailable"
    assert result["reason"] == "output_limit"
    if output_tokens is not None:
        assert result["usage"] == {"output_tokens": output_tokens}


class Capacity:
    def __init__(self):
        self.ordinary_active = False

    def reserve_summary(self, db, run_id):
        if (
            self.ordinary_active
            or db.execute(
                "SELECT 1 FROM summary_runs WHERE account_id='summary-a' AND state='running'"
            ).fetchone()
        ):
            return None
        return {"account_id": "summary-a", "profile": "summary", "provider": "codex"}


class Runner:
    def __init__(self, *, timeout=False, confirm_stop=True):
        self.timeout = timeout
        self.confirm_stop = confirm_stop
        self.requests = []
        self.stopped = []

    def start(self, request: SummaryLaunch):
        self.requests.append(request)
        return request.run_id

    def wait(self, handle, timeout):
        if self.timeout:
            raise SummaryTimedOut
        return SummaryOutput("derived", output_tokens=4, usage={"output_tokens": 4})

    def stop(self, handle):
        self.stopped.append(handle)
        return self.confirm_stop

    def active_run_ids(self):
        return set()


def test_isolated_runner_receives_no_tools_or_workspace_authority(tmp_path):
    runner = Runner()
    store = Store.scratch(tmp_path / "firm.sqlite3")
    capacity = Capacity()
    result = SummaryService(runner, store=store, capacity=capacity).summarize(
        summary_snapshot()
    )
    assert result["status"] == "summarized"
    request = runner.requests[0]
    assert request.max_input_bytes == MAX_SUMMARY_PROMPT_BYTES
    assert request.tools_enabled is False
    assert request.workspace_grants == ()
    assert request.mcp_url is None
    with store.read() as db:
        row = db.execute("SELECT state,account_id,profile,provider FROM summary_runs").fetchone()
    assert tuple(row) == ("summarized", "summary-a", "summary", "codex")


def test_summary_capacity_busy_includes_ordinary_active_slot(tmp_path):
    runner = Runner()
    store = Store.scratch(tmp_path / "firm.sqlite3")
    capacity = Capacity()
    capacity.ordinary_active = True
    result = SummaryService(runner, store=store, capacity=capacity).summarize(
        summary_snapshot()
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "busy"
    assert runner.requests == []


def test_timeout_releases_only_after_runner_confirms_exit(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    runner = Runner(timeout=True, confirm_stop=True)
    capacity = Capacity()
    service = SummaryService(runner, store=store, capacity=capacity)
    result = service.summarize(summary_snapshot())
    assert result["reason"] == "timeout"
    assert runner.stopped == [runner.requests[0].run_id]
    with store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "unavailable"


def test_unconfirmed_timeout_keeps_durable_slot_until_reconciliation(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    runner = Runner(timeout=True, confirm_stop=False)
    capacity = Capacity()
    service = SummaryService(runner, store=store, capacity=capacity)
    result = service.summarize(summary_snapshot())
    assert result["reason"] == "timeout_unconfirmed"
    with store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"
    assert service.reconcile(set()) == 1
    with store.read() as db:
        row = db.execute("SELECT state,reason FROM summary_runs").fetchone()
    assert tuple(row) == ("unavailable", "host_restart")


def test_start_sandbox_error_keeps_reservation_until_inventory_reconciliation(tmp_path):
    class StartFails(Runner):
        def start(self, request):
            raise RuntimeError("sandbox connection lost after create")

    store = Store.scratch(tmp_path / "firm.sqlite3")
    result = SummaryService(StartFails(), store=store, capacity=Capacity()).summarize(
        summary_snapshot()
    )
    assert result["reason"] == "provider_exit_unconfirmed"
    with store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "running"


@pytest.mark.parametrize("max_input_bytes", [MAX_SUMMARY_PROMPT_BYTES, 256 * 1024])
def test_oversized_input_is_rejected_before_reservation_even_when_slots_are_busy(
    tmp_path, max_input_bytes
):
    runner = Runner()
    store = Store.scratch(tmp_path / "firm.sqlite3")
    service = SummaryService(runner, store=store)
    reservations = []

    def reserve(db, run_id):
        reservations.append(run_id)
        return {}

    # UTF-8 bytes, not character counts or caller-supplied usage, set the boundary.
    prompt = "é" * (max_input_bytes // 2)
    for _ in range(3):
        result = service.run_isolated(
            prompt + "x", reserve=reserve, input_bytes=1, max_input_bytes=max_input_bytes
        )
        assert result == {"status": "unavailable", "reason": "input_limit"}
    assert not runner.requests and not reservations and not service.inflight()
    with store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM summary_runs").fetchone()[0] == 0

    assert all(service._slots.acquire(blocking=False) for _ in range(MAX_SUMMARY_CONCURRENCY))
    try:
        assert service.run_isolated(prompt + "x", max_input_bytes=max_input_bytes)["reason"] == (
            "input_limit"
        )
        assert service.run_isolated(prompt, max_input_bytes=max_input_bytes)["reason"] == "busy"
    finally:
        for _ in range(MAX_SUMMARY_CONCURRENCY):
            service._slots.release()

    assert (
        service.run_isolated(prompt, reserve=reserve, max_input_bytes=max_input_bytes)["status"]
        == "completed"
    )
    assert len(runner.requests) == len(reservations) == 1
    assert runner.requests[0].max_input_bytes == max_input_bytes
    with store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "summarized"


def test_explicit_prelaunch_input_failure_releases_reservation(tmp_path):
    class RejectingRunner(Runner):
        def start(self, request):
            raise SummaryInputTooLarge("rejected before launching")

    store = Store.scratch(tmp_path / "firm.sqlite3")
    service = SummaryService(RejectingRunner(), store=store)
    assert service.run_isolated("bounded")["reason"] == "input_limit"
    with store.read() as db:
        assert db.execute("SELECT state,reason FROM summary_runs").fetchone()[:] == (
            "unavailable",
            "input_limit",
        )
    assert service.inflight() == set()


@pytest.mark.parametrize("confirm_stop", [False, True])
def test_input_exception_after_launch_requires_confirmed_stop(tmp_path, confirm_stop):
    class LateFailure(Runner):
        def wait(self, handle, timeout):
            raise SummaryInputTooLarge("adapter failed after launching")

    store = Store.scratch(tmp_path / "firm.sqlite3")
    runner = LateFailure(confirm_stop=confirm_stop)
    service = SummaryService(runner, store=store)
    result = service.run_isolated("bounded")
    assert result["reason"] == ("provider_error" if confirm_stop else "provider_exit_unconfirmed")
    assert runner.stopped == [runner.requests[0].run_id]
    with store.read() as db:
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == (
            "unavailable" if confirm_stop else "running"
        )


@pytest.mark.parametrize("kind", ["summary", "staffing"])
def test_generated_isolated_run_ids_build_real_provider_specs(tmp_path, kind):
    from firm_sandbox import Layout, Run

    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text('{"tokens":{"access_token":"host-secret"}}')
    auth_file.chmod(0o600)
    adapter = CodexProvider(model="gpt-5.6-luna", effort="medium", auth_file=auth_file)

    class Sandbox:
        spec = None

        def start(self, spec):
            self.spec = spec
            return Run(spec.role_id, spec.run_id, "a" * 64)

        def follow(self, _run):
            yield b"derived"

        def status(self, _run):
            return {"running": False, "exit_code": 0}

        def retire(self, _role_id):
            pass

    sandbox = Sandbox()
    runner = SandboxSummaryRunner(
        sandbox,
        lambda request: adapter.build_summary_spec(request, Layout()),
        lambda _request, logs, _exit_code: SummaryOutput(logs.decode()),
        start_url="http://firm/summary/start",
    )

    result = SummaryService(runner).run_isolated("bounded", kind=kind)

    assert result == {"status": "completed", "text": "derived"}
    assert sandbox.spec.run_id.startswith("run-")
    assert len(sandbox.spec.run_id) == 36


def test_durable_summary_cap_spans_service_instances(tmp_path):
    class PermissiveCapacity(Capacity):
        def reserve_summary(self, db, run_id, *, caller_session_id=None):
            return {"account_id": "summary-a", "profile": "summary", "provider": "codex"}

    store = Store.scratch(tmp_path / "firm.sqlite3")
    for _ in range(MAX_SUMMARY_CONCURRENCY):
        stuck = SummaryService(
            Runner(timeout=True, confirm_stop=False), store=store, capacity=PermissiveCapacity()
        )
        assert stuck.summarize(summary_snapshot())["reason"] == "timeout_unconfirmed"
    beyond = SummaryService(Runner(), store=store, capacity=PermissiveCapacity()).summarize(
        summary_snapshot()
    )
    assert beyond["reason"] == "busy"


@pytest.mark.parametrize("max_input_bytes", [MAX_SUMMARY_PROMPT_BYTES, 256 * 1024])
def test_sandbox_summary_runner_enforces_input_boundary_and_no_authority_spec(max_input_bytes):
    from firm_sandbox import Layout, Organization, Run, RunSpec

    class Sandbox:
        def __init__(self):
            self.spec = None
            self.running = True

        def layout(self, role_id):
            return Layout()

        def start(self, spec):
            self.spec = spec
            return Run(spec.role_id, spec.run_id, "a" * 64)

        def status(self, handle):
            return {"running": False, "exit_code": 0}

        def follow(self, handle):
            yield b"summary"

        def stop(self, handle, timeout=0):
            self.running = False
            return {"running": False}

        def retire(self, role_id):
            pass

    sandbox = Sandbox()
    runner = SandboxSummaryRunner(
        sandbox,
        lambda request: RunSpec(
            role_id="summary",
            run_id=request.run_id,
            argv=("provider",),
            stdin=request.prompt.encode(),
        ),
        lambda request, logs, exit_code: SummaryOutput(logs.decode(), output_tokens=1),
    )
    request = SummaryLaunch(
        "run-id", "é" * (max_input_bytes // 2), 30, max_input_bytes=max_input_bytes
    )
    assert runner.wait(runner.start(request), 30).text == "summary"
    assert sandbox.spec.stdin.decode() == request.prompt
    with pytest.raises(SummaryInputTooLarge, match="UTF-8 bytes"):
        runner.start(replace(request, run_id="oversized", prompt=request.prompt + "x"))
    assert "oversized" not in runner._handles

    rejecting = SandboxSummaryRunner(
        sandbox,
        lambda request: RunSpec(
            role_id="summary",
            run_id=request.run_id,
            argv=("provider",),
            organization=Organization("founder"),
        ),
        lambda request, logs, exit_code: SummaryOutput("unused"),
    )
    with pytest.raises(ValueError, match="organization authority"):
        rejecting.wait(rejecting.start(request), 2)


@pytest.mark.parametrize("unresolved", [False, True])
def test_reconcile_uses_the_dispatchers_own_inventory_status_without_asking_again(unresolved):
    """The dispatcher already fetched every run's status this tick; reconcile must
    settle a still-active summary run from that same reading rather than asking
    the guest for it a second time.
    """
    from firm_sandbox import Run

    from firm_runtime import summaries as summaries_module

    class Sandbox:
        def __init__(self):
            self.status_calls = 0

        def status(self, run):
            self.status_calls += 1
            return {"running": True}

    sandbox = Sandbox()
    runner = SandboxSummaryRunner(sandbox, lambda request: None, lambda *args: None)
    run = Run("s-" + "a" * 32, "run-" + "a" * 32, "g" * 64)
    handle = summaries_module._SummaryHandle(SummaryLaunch(run.run_id, "fixture", 30))
    handle.launch_pending = False
    runner._handles[run.run_id] = handle
    maintenance = []
    runner._maintain = lambda action, run: maintenance.append((action, run))
    records = [{"run_id": run.run_id, "deadline": 1_000_000}]
    status = {"running": not unresolved, "unresolved": unresolved}
    active = runner.reconcile({run.run_id: (run, status)}, records, now=0)
    assert active == {run.run_id}
    assert sandbox.status_calls == 0
    assert maintenance == ([("stop", run)] if unresolved else [])
    assert not handle.exit_confirmed


def test_completed_summary_backlog_retires_once_without_a_burst_or_delaying_stops():
    from firm_sandbox import Run

    from firm_runtime.summaries import summary_role_id

    retiring, release, stopped = threading.Event(), threading.Event(), threading.Event()
    retired = []

    class Sandbox:
        def retire(self, role_id):
            retired.append(role_id)
            retiring.set()
            assert release.wait(5)

        def stop(self, run):
            stopped.set()
            return {"running": True}

    runner = SandboxSummaryRunner(Sandbox(), lambda request: None, lambda *args: None)
    runs = [
        Run(summary_role_id(f"run-{i:032x}"), f"run-{i:032x}", "g" * 64)
        for i in range(31)
    ]
    inventory = {run.run_id: (run, {"running": False}) for run in runs}
    records = [{"run_id": run.run_id, "deadline": 0} for run in runs]

    def join_maintenance():
        with runner._lock:
            threads = tuple(runner._maintenance.values())
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()

    try:
        runner.reconcile(inventory, records, now=1)
        assert retiring.wait(5)
        runner.reconcile(inventory, records, now=2)
        assert retired == [runs[0].role_id]
        # Cancellation must still run while retirement is blocked in the guest.
        runner._maintain("stop", Run("s-live", "run-live", "g" * 64))
        assert stopped.wait(5)
    finally:
        release.set()
        join_maintenance()

    # A restart backlog drains on subsequent ticks, then idle ticks do no cleanup.
    for now in range(3, 40):
        assert runner.reconcile(inventory, records, now=now) == set()
        join_maintenance()
    assert sorted(retired) == sorted(run.role_id for run in runs)


def test_failed_summary_retirement_retries_until_success():
    from firm_sandbox import Run

    run = Run("s-" + "a" * 32, "run-" + "a" * 32, "g" * 64)
    attempts = []

    class Sandbox:
        def retire(self, role_id):
            attempts.append(role_id)
            if len(attempts) == 1:
                raise RuntimeError("file server retirement failed")

    runner = SandboxSummaryRunner(Sandbox(), lambda request: None, lambda *args: None)
    for now in range(3):
        runner.reconcile(
            {run.run_id: (run, {"running": False})},
            [{"run_id": run.run_id, "deadline": 0}],
            now=now,
        )
        with runner._lock:
            threads = tuple(runner._maintenance.values())
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert attempts == [run.role_id, run.role_id]


@pytest.mark.parametrize("kind", ["staffing", "summary"])
def test_agy_thinking_keeps_answer_and_raw_usage(kind):
    from test_agy_provider import encode, stream

    from firm_runtime.agy_provider import parse_summary

    class Provider:
        def summarize(self, prompt, *, timeout):
            return parse_summary(
                encode(stream(usage={"output_tokens": 2294, "thinking_tokens": 2241})), 0
            )

    result = SummaryService(FunctionRunner(Provider())).run_isolated(
        "Synthetic choice", kind=kind
    )
    assert result["status"] == "completed"
    assert result["text"] == "FIRM_OK"
    assert result["output_tokens"] == 2294
    assert result["usage"]["output_tokens"] == 2294
    assert result["usage"]["reasoning_output_tokens"] == 2241
