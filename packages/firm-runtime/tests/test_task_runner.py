"""Child launch-spec transport; no VM or provider process is started."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from firm_sandbox import RunSpec
from test_tasks import setup as setup
from test_tasks import submit, task_rows

from firm_runtime.task_runner import GuestTaskRunner, reconcile_tasks
from firm_runtime.tasks import TaskExecutionError
from firm_runtime.workspaces import sandbox_role_id, sandbox_run_id


class ChildSandbox:
    def __init__(self):
        self.started = threading.Event()
        self.release_start = threading.Event()
        self.release_start.set()
        self.rows = {}
        self.calls = []
        self.confirmed = True
        self.lost_reply = False
        self.output = b"answer"

    def start_child(self, parent, **request):
        self.calls.append((parent, request))
        self.started.set()
        assert self.release_start.wait(3)
        # Model the controller's immutable ID and stop tombstone.
        self.rows.setdefault(request["run_id"], {"running": True})
        if self.lost_reply:
            raise OSError("lost launch reply")

    def child_status(self, run_id):
        return self.rows[run_id]

    def child_logs(self, run_id, *, offset, limit):
        return self.output[offset:offset + limit]

    def stop_child(self, run_id):
        if self.confirmed:
            self.rows[run_id] = {"running": False, "confirmed": True,
                                 "reason": "cancelled", "exit_code": 137}
        return {"confirmed": self.confirmed}

    def complete(self, run_id, *, code=0, reason="exited"):
        self.rows[run_id] = {"running": False, "confirmed": True,
                             "exit_code": code, "reason": reason}


@pytest.fixture
def child_setup(setup):
    runtime, caller, _, pool, now = setup
    parent_id = sandbox_run_id(caller.activation_id)
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO sandbox_runs(activation_id,role_id,run_id,provider,deadline,"
            "vm_generation) VALUES(?,?,?,?,?,?)",
            (caller.activation_id, sandbox_role_id(caller.employee_id), parent_id,
             "primary", now[0] + 50, "generation"),
        )
    box = ChildSandbox()
    runner = GuestTaskRunner(
        runtime, box, build_spec=lambda request, parent: RunSpec(
            parent.role_id, ("/bin/sh", "-c", "printf answer"),
        ),
    )
    runtime.task_service.runner = runner
    try:
        yield runtime, caller, runner, pool, now, box
    finally:
        box.release_start.set()
        box.confirmed = True
        runner.close()


def reserve(child_setup):
    runtime, caller, *_ = child_setup
    _, _, request = runtime.task_service._reserve(caller.token, "inspect", "request")
    return request


def test_start_wait_stop_binds_live_parent_and_deadline(child_setup):
    runtime, caller, runner, _, now, box = child_setup
    request = reserve(child_setup)
    handle = runner.start(request)
    assert box.started.wait(1)
    parent, payload = box.calls[0]
    assert parent.run_id == sandbox_run_id(caller.activation_id)
    assert payload == {"run_id": request.run_id, "argv": ("/bin/sh", "-c", "printf answer"),
                       "seconds_remaining": 50, "env": {}, "secrets": {}, "secret_env": {},
                       "stdin": b""}
    with pytest.raises(TimeoutError):
        runner.wait(handle, 0.01)
    box.complete(request.run_id)
    assert runner.wait(handle, 2) == "answer"
    assert runner.stop(handle)


def test_service_completion_is_delivered_after_status_poll(child_setup):
    runtime, caller, runner, pool, now, box = child_setup
    result = submit(child_setup[:5], "complete")
    assert box.started.wait(1)
    box.complete(task_rows(runtime)[0]["run_id"])
    assert result.result(2) == {"status": "completed", "answer": "answer"}
    assert task_rows(runtime)[0]["state"] == "summarized"


def test_lost_launch_reply_still_has_owned_handle_and_cleanup(child_setup):
    runtime, _, runner, _, _, box = child_setup
    box.lost_reply = True
    request = reserve(child_setup)
    handle = runner.start(request)
    with pytest.raises(OSError, match="lost launch reply"):
        runner.wait(handle, 2)
    assert runner.stop(handle)
    assert box.rows[request.run_id]["confirmed"]


def test_unconfirmed_stop_retains_reservation_until_reconcile_confirms(child_setup):
    runtime, _, runner, _, _, box = child_setup
    box.confirmed = False
    result = submit(child_setup[:5], "unconfirmed")
    assert box.started.wait(1)
    run_id = task_rows(runtime)[0]["run_id"]
    box.complete(run_id)
    box.rows[run_id]["confirmed"] = False
    assert result.result(2)["reason"] == "provider_failure_unconfirmed"
    runner.reconcile()
    assert task_rows(runtime)[0]["state"] == "running"
    box.confirmed = True
    runner.reconcile()
    assert task_rows(runtime)[0]["state"] == "unavailable"


def test_restart_stops_journaled_run_before_releasing_pending_request(child_setup):
    runtime, caller, runner, _, _, box = child_setup
    request = reserve(child_setup)
    runner.start(request)
    assert box.started.wait(1)
    # A fresh host has no handles. Failed proof must not release capacity.
    box.confirmed = False
    reconcile_tasks(runtime, box)
    assert task_rows(runtime)[0]["state"] == "running"
    box.confirmed = True
    reconcile_tasks(runtime, box)
    assert task_rows(runtime)[0]["state"] == "unavailable"
    assert runtime.task_service.call(caller.token, "inspect", "request") == {
        "status": "unavailable", "reason": "cancelled",
    }


def test_reconciliation_protects_current_launch_and_fences_delayed_rpc(child_setup):
    runtime, _, runner, _, _, box = child_setup
    box.release_start.clear()
    request = reserve(child_setup)
    handle = runner.start(request)
    assert box.started.wait(1)
    runtime.task_service._inflight.add(request.run_id)
    runner.reconcile()
    assert task_rows(runtime)[0]["state"] == "running"
    runtime.task_service._inflight.remove(request.run_id)
    assert runner.stop(handle)
    runner.reconcile()
    assert task_rows(runtime)[0]["state"] == "unavailable"
    box.release_start.set()
    with pytest.raises(RuntimeError, match="stopped"):
        runner.wait(handle, 2)
    assert box.rows[request.run_id]["running"] is False


def test_recovered_reservation_cannot_launch(child_setup):
    runtime, _, runner, _, _, box = child_setup
    request = reserve(child_setup)
    reconcile_tasks(runtime, box)
    with pytest.raises(Exception, match="not live"):
        runner.start(request)
    assert not box.calls


@pytest.mark.parametrize("reason", ["timeout", "cancelled", "log_overflow"])
def test_guest_refusal_reason_is_retained(child_setup, reason):
    _, _, runner, _, _, box = child_setup
    request = reserve(child_setup)
    handle = runner.start(request)
    assert box.started.wait(1)
    box.complete(request.run_id, code=137, reason=reason)
    with pytest.raises(TaskExecutionError) as caught:
        runner.wait(handle, 2)
    assert caught.value.reason == reason
    assert runner.stop(handle)


def test_service_waits_in_one_second_slices_and_wakes_on_future(setup, monkeypatch):
    runtime, caller, fake, _, _ = setup
    durations = []
    wait = fake.wait

    def observed(handle, timeout):
        durations.append(timeout)
        return wait(handle, timeout)

    monkeypatch.setattr(fake, "wait", observed)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(runtime.task_service.call, caller.token, "inspect", "slow")
        _, future = fake.started.get(timeout=1)
        future.set_result("done")
        assert result.result(0.5)["answer"] == "done"
    assert durations == [1.0]


def test_reconcile_cannot_finalize_call_between_cleanup_and_answer_commit(child_setup, monkeypatch):
    runtime, _, runner, _, _, box = child_setup
    stopped, release = threading.Event(), threading.Event()
    stop = runner.stop

    def delayed(handle):
        confirmed = stop(handle)
        stopped.set()
        assert release.wait(2)
        return confirmed

    monkeypatch.setattr(runner, "stop", delayed)
    result = submit(child_setup[:5], "commit-race")
    assert box.started.wait(1)
    box.complete(task_rows(runtime)[0]["run_id"])
    try:
        assert stopped.wait(2)
        runner.reconcile()
        assert task_rows(runtime)[0]["state"] == "running"
    finally:
        release.set()
    assert result.result(2) == {"status": "completed", "answer": "answer"}


def test_confirmed_recovery_wins_over_late_unconfirmed_result(setup):
    runtime, _, runner, _, _ = setup
    result = submit(setup, "recover-race")
    request, future = runner.started.get(timeout=1)
    # Models the hard-stop path's independent VM exit proof.
    runtime.task_service.recover_confirmed([request.run_id])
    runner.confirmed = False
    future.set_result("late answer")
    assert result.result(2) == {"status": "unavailable", "reason": "cancelled"}
    assert task_rows(runtime)[0]["state"] == "unavailable"


def test_child_specs_can_run_in_parallel(child_setup):
    runtime, _, _, _, _, box = child_setup
    first = submit(child_setup[:5], "first")
    assert box.started.wait(1)
    box.started.clear()
    second = submit(child_setup[:5], "second")
    assert box.started.wait(1)
    assert len(box.calls) == 2
    assert not first.done() and not second.done()
    for row in task_rows(runtime):
        box.complete(row["run_id"])
    assert first.result(2)["answer"] == second.result(2)["answer"] == "answer"


def test_confirmed_completion_does_not_issue_another_stop(child_setup, monkeypatch):
    _, _, runner, _, _, box = child_setup
    request = reserve(child_setup)
    handle = runner.start(request)
    assert box.started.wait(1)
    box.complete(request.run_id)
    assert runner.wait(handle, 2) == "answer"

    def forbidden(_):
        pytest.fail("completed, confirmed child must not issue stop RPC")

    monkeypatch.setattr(box, "stop_child", forbidden)
    assert runner.stop(handle)
