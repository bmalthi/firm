"""Offline integrated AGY acceptance routing, projection, and resume evidence."""

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_sandbox import Config, RunSpec

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"


def load(name):
    path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"acceptance_{name}", ACCEPTANCE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = path


provider = load("provider")
summary = load("summary")
task = load("task")
common = sys.modules["provider_checks.acceptance_common"]


def access_document():
    # Synthetic only: never load the authenticated profile in offline tests.
    return {"auth_method": "oauth-personal", "token": {
        "access_token": "synthetic-access-do-not-record", "token_type": "Bearer",
        "expiry": "2099-01-01T00:00:00Z", "refresh_token": "", "expires_in": 3600,
    }}


def launch_spec(**kwargs):
    return RunSpec("role", ("guest-python", "launcher"), run_id="run-one", env={
        "FIRM_AGY_MODE": "task", "FIRM_AGY_ARGV": json.dumps([
            "/usr/local/bin/agy", "--print=", "--model", "gemini-3.8-flash-high",
        ]),
    }, secrets={"agy-auth": json.dumps(access_document()).encode()}, **kwargs)


def native_stream(*, model="gemini-3.8-flash-high", native_id="synthetic-native-id",
                  tools=(), reason=None):
    events = [{"event": "init", "conversation_id": native_id,
               "init": {"model": model, "tools": list(tools)}}]
    for index, name in enumerate(tools):
        events.append({"event": "step_update", "step_update": {
            "conversation_id": native_id, "step_index": index, "step_type": "tool",
            "state": "DONE", "tool_name": name, "arguments": "synthetic-secret-arguments",
        }})
    events.append({"event": "result", "result": {
        "conversation_id": native_id, "status": "SUCCESS", "finished_reason": reason,
        "response": "synthetic-secret-response", "usage": {"input_tokens": 8, "output_tokens": 2},
    }})
    return b"".join((json.dumps(event) + "\n").encode() for event in events)


@pytest.mark.parametrize("module", [provider, summary, task])
def test_cli_selects_agy_explicitly(module):
    args = ["--root", "/tmp/synthetic-firm", "--evidence", "/tmp/synthetic-evidence",
            "--provider-config", "/tmp/synthetic.toml", "--profile", "agy", "--provider", "agy"]
    assert module.argument_parser().parse_args(args).provider == "agy"
    if module is provider:
        assert module.argument_parser().parse_args([*args, "--resume"]).resume
    with pytest.raises(SystemExit):
        module.argument_parser().parse_args([*args[:-1], "unknown"])


def config():
    return {"max_active": 2, "staffing": False, "founder_profile": "agy",
            "profiles": {"agy": {"provider": "agy", "model": "gemini-3.8-flash",
                                 "effort": "medium", "accounts": ["dedicated"]}},
            "accounts": {"dedicated": {"provider": "agy", "max_active": 2,
                                       "auth_file": common.AGY_AUTH_FILE}}}


def test_selected_profile_is_copy_free_and_rejects_normal_home_or_billing():
    source = config()
    profile, accounts = common.selected_profile(source, "agy", "agy")
    assert profile["provider"] == "agy"
    assert accounts["dedicated"]["auth_file"] == common.AGY_AUTH_FILE
    with pytest.raises(ValueError, match="select --provider"):
        common.selected_profile(source, "agy", "claude")
    source["accounts"]["dedicated"]["auth_file"] = "~/.gemini/antigravity-oauth-token"
    with pytest.raises(ValueError, match="dedicated profile"):
        common.selected_profile(source, "agy")
    source = config()
    source["google_cloud"] = {"project": "synthetic"}
    with pytest.raises(ValueError, match="google_cloud"):
        common.selected_profile(source, "agy")
    source["profiles"]["agy"]["provider"] = "unknown"
    with pytest.raises(ValueError, match="Unsupported"):
        common.selected_profile(source, "agy")


@pytest.mark.parametrize("change", ["none", "refresh", "email", "nested_email", "unknown",
                                    "missing_access", "bad_type", "bad_lifetime"])
def test_projection_evidence_checks_staged_bytes_without_reprojecting(change):
    document = access_document()
    if change == "refresh":
        document["token"]["refresh_token"] = "synthetic-refresh-do-not-record"
    elif change == "email":
        document["email"] = "synthetic-owner@example.invalid"
    elif change == "nested_email":
        document["token"]["email"] = "synthetic-owner@example.invalid"
    elif change == "unknown":
        document["extra"] = "synthetic-private-field"
    elif change == "missing_access":
        document["token"].pop("access_token")
    elif change == "bad_type":
        document["token"]["access_token"] = 1
    elif change == "bad_lifetime":
        document["token"]["expires_in"] = True
    evidence = common.agy_projection_evidence(json.dumps(document))
    assert evidence["passed"] is (change == "none")
    assert evidence["kind"] == "agy-access"
    assert "synthetic-" not in json.dumps(evidence)


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"token":{},"token":{}}', b"\xff"])
def test_projection_malformed_input_has_only_safe_failure(raw):
    assert common.agy_projection_evidence(raw) == {
        "kind": "agy-access", "access_only": False, "no_refresh_token": False,
        "no_email": False, "passed": False,
    }


def test_launch_receipt_checks_actual_model_and_projection(tmp_path):
    calls = []
    box = SimpleNamespace(start=lambda spec: calls.append(spec))
    launches = common.capture_agy_launches(box, tmp_path)
    spec = launch_spec()
    box.start(spec)
    evidence = launches[spec.run_id]
    assert evidence["passed"] and evidence["native_model"] == "gemini-3.8-flash-high"
    assert evidence["route"] == "native-subscription"
    assert evidence["quota"] == evidence["context"] == "unavailable"
    assert "synthetic-access" not in (tmp_path / "run-one.launch.json").read_text()
    spec.secrets["agy-auth"] = b'{"refresh_token":"synthetic-private-field"}'
    with pytest.raises(ValueError, match="access projection"):
        box.start(spec)
    assert len(calls) == 1


@pytest.mark.parametrize("model", ["gemini-4-argon-high", "gemini-3.8-flash-medium",
                                  "gemini-4-argon-medium"])
def test_native_model_and_resume_evidence_omit_payloads(model):
    logs = native_stream(model=model, tools=("mcp_firm_peek",))
    evidence = common.agy_output_evidence(
        logs, expected_model=model, expected_id="synthetic-native-id")
    assert evidence["success"] and evidence["model_matches"] and evidence["conversation_matches"]
    assert evidence["firm_tool_seen"] and evidence["tool_steps"] == 1
    assert evidence["native_cumulative_usage"]["input_tokens"] == 8
    assert "synthetic-" not in json.dumps(evidence)
    assert common.native_id_from_output(logs) == "synthetic-native-id"


@pytest.mark.parametrize("failure", ["wrong_id", "wrong_model", "timeout", "missing_result",
                                     "nonzero_exit", "unknown_model"])
def test_output_cannot_claim_wrong_route_resume_or_terminal(failure):
    logs = native_stream(
        reason="print_timeout" if failure == "timeout" else None,
        model="synthetic-secret-model" if failure == "unknown_model" else "gemini-3.8-flash-high")
    if failure == "missing_result":
        logs = logs.splitlines(keepends=True)[0]
    evidence = common.agy_output_evidence(
        logs, expected_model="gemini-4-argon-high" if failure == "wrong_model"
        else "gemini-3.8-flash-high", expected_id="different" if failure == "wrong_id" else None,
        exit_code=1 if failure == "nonzero_exit" else 0)
    assert not (evidence["success"] and evidence["model_matches"])
    assert "synthetic-" not in json.dumps(evidence)


def test_resume_wakes_existing_session_without_fresh_route(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "vm-provider-live", provider.MANDATE, config())["founder"]
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET session_id=? WHERE employee_id=?",
                   ("synthetic-session", founder["employee_id"]))
        db.execute("INSERT INTO provider_sessions VALUES(?,?,?)",
                   ("synthetic-session", "agy", "synthetic-native-id"))
    assert provider.wake_retained_session(runtime, founder, "agy") == (
        "synthetic-session", "synthetic-native-id")
    with runtime.store.read() as db:
        assert runtime.store.role(db, founder["employee_id"])["session_id"] == "synthetic-session"
    with pytest.raises(ValueError, match="original profile"):
        provider.wake_retained_session(runtime, founder, "different")


def test_resume_requires_retained_native_state(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "vm-provider-live", provider.MANDATE, config())["founder"]
    with pytest.raises(ValueError, match="retained native session"):
        provider.wake_retained_session(runtime, founder, "agy")


def test_agy_completed_turn_without_handoff_retains_resumable_session(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "vm-provider-live", provider.AGY_MANDATE, config())["founder"]
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    with runtime.store.transaction() as db:
        db.execute("INSERT INTO provider_sessions VALUES(?,?,?)",
                   (invocation.session_id, "agy", "synthetic-native-id"))
    runtime.finish(invocation.activation_id, "completed")
    assert provider.wake_retained_session(runtime, founder, "agy") == (
        invocation.session_id, "synthetic-native-id")
    resumed = runtime.admit()
    assert resumed.session_id == invocation.session_id


def test_server_peek_receipt_requires_valid_activation_and_omits_contents(tmp_path):
    from firm_runtime.errors import FirmError

    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("vm-provider-live", provider.AGY_MANDATE, config())
    receipts = provider.capture_server_peek(runtime, tmp_path)
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    with pytest.raises(FirmError):
        runtime.tools("invalid-synthetic-token").peek()
    assert not receipts and not (tmp_path / "server-tools.json").exists()
    runtime.tools(invocation.token).peek()
    assert receipts == [{"tool": "peek", "activation_validated": True}]
    assert json.loads((tmp_path / "server-tools.json").read_text()) == receipts


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_non_agy_resume_preserves_existing_fresh_route(name):
    calls = []
    runtime = SimpleNamespace(owner=SimpleNamespace(route=lambda *args: calls.append(args)))
    assert provider.prepare_resume(runtime, {"employee_id": "founder"}, "profile", name) == (
        None, None)
    assert len(calls) == 1 and calls[0][:3] == ("owner", "founder", "profile")


def test_agy_resume_uses_retained_session_path(monkeypatch):
    calls = []
    monkeypatch.setattr(provider, "wake_retained_session",
                        lambda *args: calls.append(args) or ("session", "native"))
    runtime, founder = object(), {"employee_id": "founder"}
    assert provider.prepare_resume(runtime, founder, "profile", "agy") == ("session", "native")
    assert calls == [(runtime, founder, "profile")]


@pytest.mark.parametrize("marker_id", ["synthetic-native-id", "wrong-native-id"])
def test_resume_evidence_validates_launcher_marker(marker_id):
    marker = json.dumps({"type": "firm.agy_expected_session", "conversation_id": marker_id})
    evidence = common.agy_output_evidence(
        marker.encode() + b"\n" + native_stream(), expected_id="synthetic-native-id",
        expected_model="gemini-3.8-flash-high")
    assert evidence["success"] is (marker_id == "synthetic-native-id")
    assert "synthetic-" not in json.dumps(evidence)


def test_readme_configuration_recipe_matches_runtime_schema(tmp_path):
    from firm_runtime.lifecycle import validate_config

    recipe = (ACCEPTANCE / "README.md").read_text().split(
        "## Experimental AGY integrated acceptance", 1)[1]
    firm, sandbox = re.findall(r"```toml\n(.*?)```", recipe, re.S)[:2]
    configured = validate_config(tomllib.loads(firm))
    profile, accounts = common.selected_profile(configured, "agy", "agy")
    assert profile["effort"] == "medium"
    assert accounts["agy-dedicated"]["max_active"] == 2
    assert configured["summary"]["agy"]["effort"] == "medium"
    assert configured["task"]["agy"]["effort"] == "high"
    path = tmp_path / "sandbox.toml"
    path.write_text(sandbox)
    assert Config.load(path).mac_image is None


def test_provider_capture_preserves_incremental_log_api(tmp_path):
    logs = native_stream()
    run = SimpleNamespace(run_id="run-one")
    calls = []

    def read(run, *, offset=0, until=None):
        calls.append((offset, until))
        return logs[offset:until]

    box = SimpleNamespace(logs=read)
    outputs = provider.capture_provider_logs(
        box, tmp_path, "agy", {run.run_id: {"native_model": "gemini-3.8-flash-high"}})
    midpoint = len(logs) // 2
    assert box.logs(run, offset=0, until=midpoint) == logs[:midpoint]
    assert not outputs[run.run_id][0]["success"]
    assert box.logs(run, offset=midpoint, until=len(logs)) == logs[midpoint:]
    assert outputs[run.run_id][0]["success"]
    assert calls == [(0, midpoint), (midpoint, len(logs))]
    saved = (tmp_path / "run-one.output.json").read_text()
    assert "synthetic-" not in saved and not list(tmp_path.glob("*.log"))


def test_recall_nonce_is_only_seeded_in_fresh_role_input():
    calls = []
    box = SimpleNamespace(start=lambda spec: calls.append(spec))
    provider.seed_recall_nonce(box, "synthetic-recall-nonce")
    original = RunSpec("role", ("agy",), env={"FIRM_AGY_MODE": "role"},
                       stdin=b'{"event":"user","message":{"content":"original"}}')
    box.start(original)
    assert "synthetic-recall-nonce" in json.loads(calls[0].stdin)["message"]["content"]
    assert "synthetic-recall-nonce" not in original.stdin.decode()
    assert "synthetic-recall-nonce" not in provider.AGY_MANDATE + provider.AGY_RECALL
    check = RunSpec("role", ("/bin/cat", "provider-validation.txt"))
    box.start(check)
    assert calls[1] is check


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("spacing", ["", " ", "\n"])
def test_resume_recall_requires_exact_previous_nonce_without_saving_response(
        tmp_path, matches, spacing):
    nonce = "synthetic-recall-nonce"
    response = "RECALLED:" + spacing + (nonce if matches else "wrong")
    logs = native_stream().replace(
        b"synthetic-secret-response", json.dumps(response)[1:-1].encode())
    box = SimpleNamespace(logs=lambda run, **kwargs: logs)
    run = SimpleNamespace(run_id="run-one")
    outputs = provider.capture_provider_logs(
        box, tmp_path, "agy", {run.run_id: {"native_model": "gemini-3.8-flash-high"}},
        "synthetic-native-id", nonce)
    box.logs(run)
    assert outputs[run.run_id][0]["recall_matches"] is matches
    saved = (tmp_path / "run-one.output.json").read_text()
    assert nonce not in saved and response not in saved
