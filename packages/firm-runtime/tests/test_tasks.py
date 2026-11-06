import asyncio
import copy
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from test_accounts import configuration, start

from firm_runtime.accounts import TASK_DEFAULTS
from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import validate_config
from firm_runtime.mcp_server import build_server, register_tools
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store
from firm_runtime.tasks import TaskService


class FakeTaskRunner:
    def __init__(self):
        self.started = queue.Queue()
        self.handles = []
        self.confirmed = True
        self.stopped = []

    def start(self, request):
        handle = Future()
        self.handles.append(handle)
        self.started.put((request, handle))
        return handle

    def wait(self, handle, timeout):
        return handle.result(timeout)

    def stop(self, handle):
        self.stopped.append(handle)
        return self.confirmed


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    runner = FakeTaskRunner()
    runtime = Runtime(
        Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]), task_runner=runner
    )
    config = configuration()
    config["accounts"] = {"a": {**config["accounts"]["a"], "max_active": 2}}
    config["profiles"] = {"primary": {**config["profiles"]["primary"], "accounts": ["a"]}}
    config["profiles"]["primary"].pop("alternates")
    runtime.lifecycle.initialize("test", "Test tasks", config)
    runtime.start()
    caller = start(runtime)
    with ThreadPoolExecutor(6) as pool:
        try:
            yield runtime, caller, runner, pool, now
        finally:
            now[0] += 1000  # let any blocked fake calls stop even after a failed assertion


def submit(setup, request_id, instruction="Inspect the file"):
    runtime, caller, _, pool, _ = setup
    return pool.submit(runtime.task_service.call, caller.token, instruction, request_id)


def occupancy(runtime):
    with runtime.store.read() as db:
        return runtime.accounts._usage(db, "a")


def task_rows(runtime):
    with runtime.store.read() as db:
        return [dict(r) for r in db.execute("SELECT * FROM summary_runs WHERE kind='task'")]


def test_task_defaults_and_partial_overrides_apply_on_init_and_configure(tmp_path):
    config = configuration()
    original = copy.deepcopy(config)
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    result = runtime.lifecycle.initialize("test", "Test", config)
    assert result["firm"]["config"]["task"] == TASK_DEFAULTS
    assert config == original
    config["task"] = {"max_concurrent": 3, "timeout_seconds": 20, "codex": {"model": "custom"}}
    config["summary"] = {"codex": {"model": "summary-model", "effort": "low"}}
    runtime.owner.configure(OWNER, config)
    with runtime.store.read() as db:
        stored = runtime.accounts.config(db)
    assert stored["task"] == {
        **TASK_DEFAULTS,
        "max_concurrent": 3,
        "timeout_seconds": 20,
        "codex": {"model": "custom", "effort": "medium"},
    }
    assert stored["summary"] == config["summary"]
    runtime.owner.configure(OWNER, original)
    with runtime.store.read() as db:
        assert runtime.accounts.config(db)["task"] == TASK_DEFAULTS


@pytest.mark.parametrize(
    "task",
    [
        None,
        [],
        {"provider": "codex"},
        {"other": {}},
        {"codex": None},
        {"claude": {"account": "a"}},
        {"codex": {"model": ""}},
        {"claude": {"model": "  "}},
        {"codex": {"model": 42}},
        {"claude": {"effort": []}},
        {"codex": {"effort": "invalid"}},
        *[
            {key: value}
            for key in (
                "max_concurrent",
                "timeout_seconds",
                "max_instruction_bytes",
                "max_answer_bytes",
            )
            for value in (True, 0, -1, 1.5, "2")
        ],
    ],
)
def test_invalid_task_configuration(task):
    with pytest.raises(FirmError, match="task"):
        validate_config({**configuration(), "task": task})


def test_first_borrows_second_uses_slot_third_hits_cap_and_borrow_transfers(setup):
    runtime, caller, runner, _, _ = setup
    first = submit(setup, "first")
    launch1, handle1 = runner.started.get(timeout=2)
    assert occupancy(runtime) == 1
    second = submit(setup, "second")
    launch2, handle2 = runner.started.get(timeout=2)
    assert occupancy(runtime) == 2
    assert submit(setup, "third").result(2) == {"status": "unavailable", "reason": "busy"}
    assert launch1.caller_activation_id == launch2.caller_activation_id == caller.activation_id
    assert (launch1.account_id, launch1.provider, launch1.model, launch1.effort) == (
        "a",
        "codex",
        "gpt-6.1-sol",
        "medium",
    )
    handle1.set_result("first answer")
    assert first.result(2)["answer"] == "first answer"
    assert occupancy(runtime) == 1
    handle2.set_result("second answer")
    assert second.result(2)["answer"] == "second answer"
    assert occupancy(runtime) == 1
    assert all(row["state"] == "summarized" for row in task_rows(runtime))
    with runtime.store.read() as db:
        events = db.execute("SELECT activation_id FROM analysis_events WHERE kind='summary.bound'")
        assert [r[0] for r in events] == [caller.activation_id] * 2


def test_max_active_one_allows_first_refuses_second_and_persists_refusal(setup):
    runtime, _, runner, _, _ = setup
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["accounts"]["a"]["max_active"] = 1
    runtime.owner.configure(OWNER, config)
    first = submit(setup, "first")
    _, handle = runner.started.get(timeout=2)
    refused = submit(setup, "second").result(2)
    assert refused == {"status": "unavailable", "reason": "capacity"}
    handle.set_result("answer")
    first.result(2)
    assert submit(setup, "second").result(2) == refused
    assert len(runner.handles) == 1


def test_task_and_role_admission_compete_atomically(setup):
    runtime, caller, runner, pool, _ = setup
    runtime.tools(caller.token).hire("child", "Do work", "hire")
    first = submit(setup, "first")
    _, handle1 = runner.started.get(timeout=2)
    gate = threading.Barrier(2)

    def task():
        gate.wait()
        return runtime.task_service.call(caller.token, "Second", "second")

    def role():
        gate.wait()
        return runtime.admit()

    second = pool.submit(task)
    child = pool.submit(role).result(2)
    if child is None:
        _, handle2 = runner.started.get(timeout=2)
        assert occupancy(runtime) == 2
        assert runtime.admit() is None
        handle2.set_result("second")
        assert second.result(2)["status"] == "completed"
        assert runtime.admit() is not None
    else:
        assert second.result(2)["reason"] == "capacity"
        assert len(runner.handles) == 1
    handle1.set_result("first")
    first.result(2)


@pytest.mark.parametrize("block", ["profile", "account"])
def test_holds_apply_to_borrowed_slot_and_refusals_replay(setup, block):
    runtime, _, runner, _, now = setup
    # A low reading is not a hold: the caller's account is the task's only option.
    runtime.accounts.observe(OWNER, "a", 0, observed_at=now[0], valid_for=100, source="test")
    incident = runtime.accounts.pause(
        OWNER, block, "a" if block == "account" else "primary", "config"
    )["incident_id"]
    result = submit(setup, "held").result(2)
    assert result["reason"] == "hold"
    runtime.accounts.clear(OWNER, incident)
    assert submit(setup, "held").result(2) == result
    assert runner.handles == []
    fresh = submit(setup, "fresh")
    _, handle = runner.started.get(timeout=2)
    handle.set_result("available")
    assert fresh.result(2)["answer"] == "available"


def test_new_quota_threshold_holds_tasks_until_the_streamed_reset(setup):
    runtime, _, runner, _, now = setup
    reset = now[0] + 30
    runtime.accounts.record_subscription(
        "a", [{"window": "five_hour", "used_percent": 90, "resets_at": reset}]
    )
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["accounts"]["a"]["stop_admitting_at"] = 0.9
    runtime.owner.configure(OWNER, config)

    assert submit(setup, "held").result(2) == {"status": "unavailable", "reason": "hold"}
    assert runner.handles == []
    now[0] = reset + 1
    fresh = submit(setup, "after-reset")
    _, handle = runner.started.get(timeout=2)
    handle.set_result("available")
    assert fresh.result(2)["answer"] == "available"


def test_identical_calls_join_across_services_conflicts_fail_and_completion_replays(setup):
    runtime, caller, runner, pool, _ = setup
    first = submit(setup, "same")
    _, handle = runner.started.get(timeout=2)
    second_service = TaskService(runtime, runner)
    duplicate = pool.submit(second_service.call, caller.token, "Inspect the file", "same")
    with pytest.raises(FirmError, match="different operation or payload"):
        submit(setup, "same", "Different").result(2)
    assert len(task_rows(runtime)) == 1
    handle.set_result("durable answer")
    assert (
        first.result(2)
        == duplicate.result(2)
        == {"status": "completed", "answer": "durable answer"}
    )
    runtime.task_service = TaskService(runtime, None)
    assert submit(setup, "same").result(2) == first.result(2)
    assert len(runner.handles) == 1


def test_config_captured_and_account_removal_rejected_while_task_holds_it(setup):
    runtime, _, runner, _, _ = setup
    pending = submit(setup, "before")
    launch, handle = runner.started.get(timeout=2)
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    removed = copy.deepcopy(config)
    removed["accounts"] = {"b": removed["accounts"]["a"]}
    removed["profiles"]["primary"]["accounts"] = ["b"]
    with pytest.raises(FirmError, match="accounts held by tasks"):
        runtime.owner.configure(OWNER, removed)
    config["task"]["codex"]["model"] = "new-model"
    runtime.owner.configure(OWNER, config)
    assert launch.model == "gpt-6.1-sol"
    handle.set_result("old settings")
    pending.result(2)
    fresh = submit(setup, "after")
    launch, handle = runner.started.get(timeout=2)
    assert launch.model == "new-model"
    handle.set_result("new settings")
    fresh.result(2)


@pytest.mark.parametrize("confirmed", [True, False])
def test_timeout_kills_only_task_and_retains_unconfirmed_capacity(setup, confirmed):
    runtime, caller, runner, _, now = setup
    runner.confirmed = confirmed
    pending = submit(setup, "timeout")
    launch, _ = runner.started.get(timeout=2)
    now[0] = launch.deadline
    result = pending.result(2)
    assert result["reason"] == ("timeout" if confirmed else "timeout_unconfirmed")
    assert runtime.authenticate(caller.token)
    assert occupancy(runtime) == (1 if confirmed else 2)
    assert task_rows(runtime)[0]["state"] == ("unavailable" if confirmed else "running")
    runtime.summary_service.reconcile(set(), host_stopped=True)
    assert task_rows(runtime)[0]["state"] == ("unavailable" if confirmed else "running")
    runtime.task_service.recover_confirmed([launch.run_id])
    assert occupancy(runtime) == 1
    assert submit(setup, "timeout").result(2) == result


def test_pending_recovery_is_durable_and_never_relaunches(setup):
    runtime, caller, runner, _, _ = setup
    _, pending, launch = runtime.task_service._reserve(caller.token, "Reserved", "pending")
    assert pending["status"] == "pending"
    duplicate = submit(setup, "pending", "Reserved")
    runtime.task_service.recover_confirmed([launch.run_id])
    assert duplicate.result(2) == {"status": "unavailable", "reason": "cancelled"}
    assert runner.handles == []


def test_parent_end_cancels_task_and_old_activation_still_counts_toward_cap(setup):
    runtime, caller, runner, _, _ = setup
    runner.confirmed = False
    pending = submit(setup, "old")
    _, _ = runner.started.get(timeout=2)
    runtime.finish(caller.activation_id)
    assert pending.result(2)["reason"] == "cancelled_unconfirmed"
    assert occupancy(runtime) == 1
    runtime.messaging.send(OWNER, ["founder"], "Continue", request_id="wake")
    fresh = start(runtime)
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["task"]["max_concurrent"] = 1
    runtime.owner.configure(OWNER, config)
    assert runtime.task_service.call(fresh.token, "New", "new")["reason"] == "busy"


@pytest.mark.parametrize(
    "instruction,answer,reason",
    [
        ("", "", "invalid_instruction"),
        ("é" * 8193, "", "instruction_too_large"),
        ("Small", "é" * 8193, "answer_too_large"),
        ("Small", "", "invalid_answer"),
    ],
)
def test_utf8_size_limits_and_invalid_answers_persist(setup, instruction, answer, reason):
    runtime, _, runner, _, _ = setup
    pending = submit(setup, "size", instruction)
    if reason in {"answer_too_large", "invalid_answer"}:
        _, handle = runner.started.get(timeout=2)
        handle.set_result(answer)
    assert pending.result(2)["reason"] == reason
    assert submit(setup, "size", instruction).result(2) == pending.result(2)


@pytest.mark.parametrize("dynamic_token", [False, True])
def test_mcp_calls_overlap_and_return_text_with_only_task_arguments(setup, dynamic_token):
    runtime, caller, runner, _, _ = setup

    async def check():
        server = build_server(runtime, caller.token)
        if dynamic_token:
            from mcp.server.fastmcp import FastMCP

            server = FastMCP("dynamic")
            register_tools(server, runtime, lambda: caller.token)
        definition = next(t for t in await server.list_tools() if t.name == "task")
        assert set(definition.inputSchema["properties"]) == {"instruction", "request_id"}
        assert definition.inputSchema["additionalProperties"] is False
        calls = [
            asyncio.create_task(
                server.call_tool("task", {"instruction": "Work", "request_id": str(i)})
            )
            for i in range(2)
        ]
        handles = [await asyncio.to_thread(runner.started.get, True, 2) for _ in calls]
        assert occupancy(runtime) == 2
        for _, handle in handles:
            handle.set_result("one answer")
        results = await asyncio.gather(*calls)
        assert all(result[0].text == "one answer" for result in results)

    asyncio.run(asyncio.wait_for(check(), 5))


def test_mcp_cancellation_stops_task_without_ending_caller(setup):
    runtime, caller, runner, _, _ = setup

    async def check():
        pending = asyncio.create_task(runtime.tools(caller.token).task("Work", "cancel"))
        await asyncio.to_thread(runner.started.get, True, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert runtime.authenticate(caller.token)
        assert runner.stopped
        replay = await runtime.tools(caller.token).task("Work", "cancel")
        assert replay == {"status": "unavailable", "reason": "cancelled"}

    asyncio.run(check())


def test_task_completion_and_replay_deliver_current_parent_controls(setup):
    runtime, caller, runner, _, _ = setup

    async def check():
        tools = runtime.tools(caller.token)
        pending = asyncio.create_task(tools.task("Work", "controls"))
        _, handle = await asyncio.to_thread(runner.started.get, True, 2)
        runtime.messaging.send(OWNER, [caller.employee_id], "New work", request_id="during-task")
        runtime.record_usage(
            caller.activation_id,
            ProviderUsage(current_context_tokens=255_000, provider_context_window=256_000),
        )
        runtime.stop()
        handle.set_result("Worker answer stays separate.")
        result = await pending
        assert result["answer"] == "Worker answer stays separate."
        assert result["mail_waiting"]["senders"][0][1] == 1
        assert result["operations_control"]["kind"] == "drain"
        assert result["context_pressure"]["level"] == "handoff_required"
        rendered = render_tool_result("task", result)
        assert rendered.startswith(result["answer"] + "\n\nOperations:")

        runtime.messaging.send(OWNER, [caller.employee_id], "Later work", request_id="after-task")
        replay = await tools.task("Work", "controls")
        assert replay["answer"] == result["answer"]
        assert replay["mail_waiting"]["senders"][0][1] == 1
        assert replay["operations_control"]["kind"] == "drain"
        assert len(runner.handles) == 1
        # Durable answers contain worker output only; controls are assembled at delivery time.
        assert runtime.task_service.call(caller.token, "Work", "controls") == {
            "status": "completed", "answer": result["answer"],
        }

    asyncio.run(asyncio.wait_for(check(), 5))


def test_task_refusal_still_delivers_parent_mail(setup):
    runtime, caller, runner, _, _ = setup
    runtime.messaging.send(OWNER, [caller.employee_id], "New work", request_id="before-refusal")
    result = asyncio.run(runtime.tools(caller.token).task("", "empty"))
    assert result["status"] == "unavailable" and result["reason"] == "invalid_instruction"
    assert result["mail_waiting"]["senders"][0][1] == 1
    assert not runner.handles


def test_task_completion_cannot_deliver_controls_after_parent_ends(setup):
    runtime, caller, runner, _, _ = setup

    async def check():
        pending = asyncio.create_task(runtime.tools(caller.token).task("Work", "ended"))
        _, handle = await asyncio.to_thread(runner.started.get, True, 2)
        runtime.finish(caller.activation_id)
        handle.set_result("Too late")
        with pytest.raises(FirmError, match="expired"):
            await pending
        assert runner.stopped

    asyncio.run(asyncio.wait_for(check(), 5))


def test_actual_fallback_provider_and_account_determine_task_settings(tmp_path):
    runner = FakeTaskRunner()
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"), task_runner=runner)
    runtime.lifecycle.initialize("test", "Test", configuration())
    runtime.start()
    runtime.accounts.pause(OWNER, "profile", "primary", "config")
    caller = start(runtime)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(runtime.task_service.call, caller.token, "Work", "fallback")
        launch, handle = runner.started.get(timeout=2)
        handle.set_result("answer")
        assert result.result(2)["status"] == "completed"
    assert (launch.provider, launch.account_id, launch.model, launch.effort) == (
        "claude",
        "c",
        "opus",
        "medium",
    )


def test_without_injected_runner_refuses_and_replays(setup):
    runtime, _, runner, _, _ = setup
    runtime.task_service.runner = None
    assert submit(setup, "absent").result(2)["reason"] == "runner_unavailable"
    runtime.task_service.runner = runner
    assert submit(setup, "absent").result(2)["reason"] == "runner_unavailable"
    assert not task_rows(runtime)


def test_simultaneous_identical_requests_reserve_once_and_replay_in_later_period(setup):
    runtime, caller, runner, pool, _ = setup
    barrier = threading.Barrier(2)

    def call():
        barrier.wait()
        return runtime.task_service.call(caller.token, "Same", "duplicate")

    calls = [pool.submit(call) for _ in range(2)]
    _, handle = runner.started.get(timeout=2)
    handle.set_result("one durable answer")
    assert calls[0].result(2) == calls[1].result(2)
    assert len(runner.handles) == 1
    runtime.finish(caller.activation_id)
    runtime.messaging.send(OWNER, ["founder"], "Continue", request_id="wake-replay")
    later = start(runtime)
    assert runtime.task_service.call(later.token, "Same", "duplicate") == calls[0].result()
    assert len(runner.handles) == 1


def test_provider_replacement_refused_until_task_exit_confirmed(setup):
    runtime, _, runner, _, _ = setup
    pending = submit(setup, "provider")
    _, handle = runner.started.get(timeout=2)
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["accounts"]["a"]["provider"] = "claude"
    config["profiles"]["primary"]["provider"] = "claude"
    with pytest.raises(FirmError, match="accounts held by tasks"):
        runtime.owner.configure(OWNER, config)
    handle.set_result("done")
    pending.result(2)
    runtime.owner.configure(OWNER, config)


def test_refused_third_request_stays_refused_after_capacity_frees(setup):
    _, _, runner, _, _ = setup
    first = submit(setup, "one")
    _, handle1 = runner.started.get(timeout=2)
    second = submit(setup, "two")
    _, handle2 = runner.started.get(timeout=2)
    refusal = submit(setup, "three").result(2)
    assert refusal["reason"] == "busy"
    handle1.set_result("one")
    handle2.set_result("two")
    first.result(2)
    second.result(2)
    assert submit(setup, "three").result(2) == refusal
    assert len(runner.handles) == 2
