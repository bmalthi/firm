"""Task service, real launch builders/parsers and the one-use credential gate; no VM."""

import json
import threading

import pytest
from firm_sandbox import Layout, Sandbox
from starlette.testclient import TestClient
from test_task_runner import child_setup as child_setup
from test_tasks import setup as setup
from test_tasks import task_rows

from firm_runtime.mcp_http import build_http_app
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.providers import Providers
from firm_runtime.task_runner import WORKER_INSTRUCTIONS, reconcile_tasks


@pytest.fixture(params=["codex", "claude"])
def worker(child_setup, request, tmp_path):
    runtime, caller, _, pool, now, box = child_setup
    provider = request.param
    auth = tmp_path / "worker-auth"
    auth.write_text(
        '{"tokens":{"access_token":"access-only"}}' if provider == "codex" else "oauth-test"
    )
    auth.chmod(0o600)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["accounts"]["a"].update(provider=provider, auth_file=str(auth))
        config["profiles"]["primary"].update(provider=provider)
        config["task"][provider] = {"model": "worker-model", "effort": "high"}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
        db.execute("UPDATE execution_bindings SET provider=?", (provider,))
    box.layout = Layout.for_role
    box.child_layout = lambda role_id, run_id: Sandbox.child_layout(box, role_id, run_id)
    runner = Providers(runtime).task_runner(box, mcp_url="http://localhost:8421/mcp")
    runtime.task_service.runner = runner
    try:
        yield runtime, caller, runner, pool, now, box, provider
    finally:
        runner.close()


def logs(provider, *, partial=False):
    if provider == "codex":
        events = [
            {"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"},
            {"type": "firm.telemetry", "provider": "codex", "model": "reported-worker",
             "usage": {"total_token_usage": {
                 "input_tokens": 23, "cached_input_tokens": 5, "output_tokens": 7,
             }}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 23, "cached_input_tokens": 5, "output_tokens": 7,
            }},
        ]
        if partial:
            events = events[:2]
    else:
        events = [{
            "type": "result", "subtype": "success", "is_error": False, "result": "Done",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "model": "reported-worker", "usage": {
                "input_tokens": 18, "cache_read_input_tokens": 5, "output_tokens": 7,
            },
        }]
        if partial:
            events = [{"type": "assistant", "message": {
                "id": "partial", "model": "reported-worker",
                "usage": {"input_tokens": 18, "cache_read_input_tokens": 5, "output_tokens": 7},
            }}]
    return "\n".join(json.dumps(event) for event in events).encode()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_real_task_builders_and_parsers_retain_usage(worker, outcome):
    runtime, caller, _, pool, _, box, provider = worker
    box.output = logs(provider, partial=outcome != "success")
    cancelled = threading.Event()
    result = pool.submit(
        runtime.task_service.call, caller.token, "Inspect only this instruction", "worker",
        cancelled=cancelled,
    )
    assert box.started.wait(2)
    run_id = task_rows(runtime)[0]["run_id"]
    parent, payload = box.calls[0]
    env = payload["env"]
    command = json.loads(env[f"FIRM_{provider.upper()}_ARGV"])
    assert command[command.index("--model") + 1] == "worker-model"
    if provider == "codex":
        assert 'model_reasoning_effort="high"' in command
        assert "features.multi_agent=false" in command and "mcp_servers={}" in command
    else:
        assert command[command.index("--effort") + 1] == "high"
        assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert "Agent" in command and "Task" in command
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    assert env["FIRM_PRIVATE_HOME"] == f"/var/lib/firm/task-homes/{run_id}/home"
    assert env["FIRM_STATE_DIR"] == f"/var/lib/firm/task-homes/{run_id}/state"
    assert payload["argv"][1].startswith(f"/run/firm/{parent.role_id}/tasks/{run_id}/credentials/")
    assert payload["stdin"] == b"Inspect only this instruction"
    assert payload["secrets"]["summary-instructions"] == WORKER_INSTRUCTIONS.encode()
    assert payload["secrets"]["summary-token"] != caller.token.encode()
    assert caller.token not in repr(payload)
    assert "FIRM_ACTIVATION_TOKEN" not in payload["secret_env"]
    inputs = runtime.store.path.parent / "inputs" / "tasks" / run_id
    assert (inputs / "task.txt").read_bytes() == payload["stdin"]
    assert (inputs / "instructions.txt").read_bytes() == WORKER_INSTRUCTIONS.encode()
    provenance = json.loads((inputs / "provenance.json").read_text())
    assert provenance["caller_activation_id"] == caller.activation_id
    assert provenance["provider"] == provider
    assert provenance["offered_role_tools"] == []
    assert provenance["tool_policy"] == {
        "provider_native_tools": "enabled", "firm_tools": "disabled", "delegation": "disabled",
    }
    assert "Prepared" in provenance["launch_state"]
    assert inputs.stat().st_mode & 0o777 == 0o700
    for path in inputs.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert payload["secrets"]["summary-token"] not in path.read_bytes()
        assert caller.token.encode() not in path.read_bytes()
        assert b"access-only" not in path.read_bytes() and b"oauth-test" not in path.read_bytes()
    if outcome == "cancelled":
        cancelled.set()
    else:
        box.complete(run_id, code=0 if outcome == "success" else 1)
    answer = result.result(3)
    assert answer == (
        {"status": "completed", "answer": "Done"} if outcome == "success" else
        {"status": "unavailable", "reason": "cancelled" if outcome == "cancelled"
         else "provider_failure"}
    )
    stored = task_rows(runtime)[0]
    assert stored["input_tokens"] == 23
    assert stored["cached_input_tokens"] == 5
    assert stored["output_token_usage"] == 7
    assert stored["resolved_model"] == "reported-worker"


def test_task_builder_configuration_failure_holds_bound_account(worker):
    runtime, caller, _, _, _, box, provider = worker
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["task"][provider]["effort"] = "invalid-effort"
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    assert runtime.task_service.call(caller.token, "Inspect", "bad-setup") == {
        "status": "unavailable", "reason": "configuration",
    }
    assert not box.calls
    with runtime.store.read() as db:
        hold = runtime.accounts._incident(db, "account", "a")
    assert hold["kind"] == "config" and hold["resolved_at"] is None


def test_task_workers_do_not_receive_the_roles_gemini_settings(worker, tmp_path):
    runtime, caller, _, pool, _, box, provider = worker
    key = tmp_path / "gemini.key"
    key.write_text("test-gemini-secret")
    key.chmod(0o600)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["google_cloud"] = {"project": "firm-project", "api_key_file": str(key)}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    box.output = logs(provider)
    result = pool.submit(runtime.task_service.call, caller.token, "Inspect local files", "scope")
    assert box.started.wait(2)
    _, payload = box.calls[0]
    for field in ("env", "secret_env"):
        assert "GEMINI_API_KEY" not in payload[field]
        assert "GOOGLE_CLOUD_PROJECT" not in payload[field]
    assert "google-cloud-key" not in payload["secrets"]
    assert b"test-gemini-secret" not in payload["secrets"].values()
    box.complete(task_rows(runtime)[0]["run_id"], code=0)
    assert result.result(3) == {"status": "completed", "answer": "Done"}


@pytest.mark.parametrize("category", ["auth", "config", "quota"])
@pytest.mark.parametrize("reason", ["exited", "timeout", "cancelled", "log_overflow",
                                   "interrupted", "launch_failed"])
def test_task_provider_failure_retains_usage_but_holds_only_on_exit(worker, category, reason):
    runtime, caller, _, pool, now, box, provider = worker
    event = (
        {"type": "firm.launch_failed", "kind": "launcher_setup"} if category == "config" else
        {"type": "error", "code": "unauthorized" if category == "auth" else "rate_limit_error",
         "retry_after_seconds": 30, "reset_at": 5000 if category == "quota" else None}
    )
    box.output = logs(provider, partial=True) + b"\n" + json.dumps(event).encode()
    result = pool.submit(runtime.task_service.call, caller.token, "Inspect", "provider-error")
    assert box.started.wait(2)
    box.complete(task_rows(runtime)[0]["run_id"], code=1, reason=reason)
    expected = reason if reason in {"timeout", "cancelled", "log_overflow"} else "provider_failure"
    assert result.result(3) == {"status": "unavailable", "reason": expected}
    with runtime.store.read() as db:
        hold = runtime.accounts._incident(db, "account", "a")
    if reason == "exited":
        assert hold["kind"] == category
        assert hold["until_at"] == (max(now[0] + 30, 5000) if category == "quota" else None)
    else:
        assert hold is None
    assert task_rows(runtime)[0]["input_tokens"] == 23


@pytest.mark.parametrize("category", ["auth", "transient"])
def test_task_setup_error_holds_only_repairable_account_failures(worker, monkeypatch, category):
    runtime, caller, runner, _, _, box, _ = worker

    def unavailable(*_):
        raise ProviderSetupError(category=category, code="credential_unavailable", message="failed")

    monkeypatch.setattr(runner, "build_spec", unavailable)
    assert runtime.task_service.call(caller.token, "Inspect", "setup-error") == {
        "status": "unavailable", "reason": "provider_failure",
    }
    assert not box.calls
    with runtime.store.read() as db:
        hold = runtime.accounts._incident(db, "account", "a")
    assert (hold["kind"] if hold else None) == ("auth" if category == "auth" else None)


@pytest.mark.parametrize("parent_state", ["live", "ended", "stopping", "expired"])
def test_task_one_use_launch_and_credential_gate(worker, parent_state):
    runtime, caller, _, _, now, _, _ = worker
    _, _, request = runtime.task_service._reserve(caller.token, "Inspect", "gate")
    if parent_state != "live":
        with runtime.store.transaction() as db:
            if parent_state == "ended":
                db.execute("UPDATE activations SET state='completed' WHERE activation_id=?",
                           (caller.activation_id,))
            elif parent_state == "stopping":
                db.execute("UPDATE sandbox_runs SET stop_cause='cancelled'")
            else:
                db.execute("UPDATE sandbox_runs SET deadline=?", (now[0],))
    app = build_http_app(runtime, allowed_hosts=["localhost:8421"],
                         allowed_origins=["http://localhost:8421"])
    headers = {"Authorization": f"Bearer {request.start_token}"}
    with TestClient(app, base_url="http://localhost:8421") as client:
        assert client.get("/credentials", headers=headers).status_code == 401
        started = client.post("/summary/start", headers=headers)
        assert started.status_code == (200 if parent_state == "live" else 401)
        assert client.post("/summary/start", headers=headers).status_code == 401
        credentials = client.get("/credentials", headers=headers)
        assert credentials.status_code == (200 if parent_state == "live" else 401)
        # A worker launch token never becomes a role tool token.
        assert client.post("/activation/start", headers=headers).status_code == 401
        if parent_state == "live":
            with runtime.store.transaction() as db:
                db.execute("UPDATE sandbox_runs SET stop_cause='cancelled'")
            assert client.get("/credentials", headers=headers).status_code == 401


@pytest.mark.parametrize("stop_reason", ["cancelled", "exited"])
def test_restart_recovery_retains_usage_without_holds(worker, monkeypatch, stop_reason):
    runtime, caller, runner, _, _, box, provider = worker
    _, _, request = runtime.task_service._reserve(caller.token, "Inspect", "restart")
    # The restarted host has a durable reservation and guest journal, but no local handle.
    assert not runner._handles
    box.rows[request.run_id] = {"running": True}
    box.output = logs(provider, partial=True) + b'\n{"type":"error","code":"rate_limit_error"}'
    stop = box.stop_child

    def stopped(run_id):
        return {**stop(run_id), "reason": stop_reason, "exit_code": 1}

    monkeypatch.setattr(box, "stop_child", stopped)
    box.confirmed = False
    assert reconcile_tasks(runtime, box) == set()
    assert task_rows(runtime)[0]["state"] == "running"
    box.confirmed = True
    assert reconcile_tasks(runtime, box) == {request.run_id}
    stored = task_rows(runtime)[0]
    assert stored["state"] == "unavailable"
    assert stored["input_tokens"] == 23
    assert stored["cached_input_tokens"] == 5
    assert stored["output_token_usage"] == 7
    assert stored["resolved_model"] == "reported-worker"
    with runtime.store.read() as db:
        assert runtime.accounts._incident(db, "account", "a") is None
    assert runtime.task_service.call(caller.token, "Inspect", "restart") == {
        "status": "unavailable", "reason": "cancelled",
    }
    assert not box.calls  # Recovery/replay never relaunches the worker.


@pytest.mark.parametrize("confirmed", [True, False])
def test_stop_retains_usage_without_holds_even_if_receipt_says_exited(
    worker, monkeypatch, confirmed,
):
    runtime, caller, runner, _, _, box, provider = worker
    _, _, request = runtime.task_service._reserve(caller.token, "Inspect", "stop")
    handle = runner.start(request)
    assert box.started.wait(2)
    box.output = logs(provider, partial=True) + b'\n{"type":"error","code":"unauthorized"}'
    monkeypatch.setattr(box, "stop_child", lambda _: {
        "confirmed": confirmed, "reason": "exited", "exit_code": 1,
    })
    assert runner.stop(handle) is confirmed
    assert task_rows(runtime)[0]["input_tokens"] == 23
    with runtime.store.read() as db:
        assert runtime.accounts._incident(db, "account", "a") is None


@pytest.mark.parametrize("parent_state", ["stopping", "expired"])
def test_completed_task_delivery_revalidates_parent_work_period(worker, monkeypatch, parent_state):
    runtime, caller, runner, pool, now, box, provider = worker
    original_stop = runner.stop

    def stop_before_delivery(handle):
        confirmed = original_stop(handle)
        assert confirmed
        assert handle.future.result() == "Done"
        # Worker completion is confirmed, but the parent's stop/expiry races delivery.
        with runtime.store.transaction() as db:
            if parent_state == "stopping":
                db.execute("UPDATE sandbox_runs SET stop_cause='cancelled'")
            else:
                db.execute("UPDATE sandbox_runs SET deadline=?", (now[0],))
        return confirmed

    monkeypatch.setattr(runner, "stop", stop_before_delivery)
    box.output = logs(provider)
    result = pool.submit(runtime.task_service.call, caller.token, "Inspect", "delivery-race")
    assert box.started.wait(2)
    box.complete(task_rows(runtime)[0]["run_id"])
    assert result.result(3) == {"status": "unavailable", "reason": "cancelled"}
    runtime.authenticate(caller.token)  # Activation state alone still permits role authentication.
    stored = task_rows(runtime)[0]
    assert stored["state"] == "unavailable" and stored["reason"] == "cancelled"
    assert stored["input_tokens"] == 23
    assert stored["output_token_usage"] == 7
    assert stored["resolved_model"] == "reported-worker"
