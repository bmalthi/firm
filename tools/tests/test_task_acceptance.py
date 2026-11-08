"""Offline tests for acceptance evidence filtering and process identity checks."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from firm_runtime.agy_provider import AgyProvider
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_sandbox import RunSpec

spec = importlib.util.spec_from_file_location(
    "task_acceptance", Path(__file__).resolve().parents[1] / "acceptance" / "task.py",
)
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


@pytest.mark.parametrize("adapter,event", [
    (CodexProvider, {"type": "item.completed", "item": {
        "id": "one", "type": "command_execution", "command": "secret-command",
        "aggregated_output": "secret-output",
    }}),
    (ClaudeProvider, {"type": "assistant", "message": {"content": [{
        "id": "one", "type": "tool_use", "name": "Bash", "input": {"secret": "credential"},
    }]}}),
])
def test_output_evidence_counts_tools_without_copying_payloads(adapter, event):
    logs = (json.dumps(event) + '\nnot-json secret-output\n').encode()
    evidence = task.output_evidence(logs, adapter, "ROLE-sentinel")
    assert evidence["tool_calls"] == 1
    assert evidence["json_events"] == 1
    assert evidence["bytes"] == len(logs)
    assert not evidence["firm_tools_seen"] and not evidence["role_marker_seen"]
    assert not any(secret in json.dumps(evidence) for secret in (
        "secret-command", "secret-output", "credential",
    ))


@pytest.mark.parametrize("adapter,event", [
    (CodexProvider, {"type": "item.completed", "item": {
        "id": "one", "type": "mcp_tool_call", "server": "firm", "tool": "peek",
    }}),
    (ClaudeProvider, {"type": "system", "subtype": "init", "tools": ["mcp__firm__peek"]}),
])
def test_output_evidence_detects_firm_tools_and_role_marker(adapter, event):
    logs = (json.dumps(event) + '\nROLE-sentinel\n').encode()
    evidence = task.output_evidence(logs, adapter, "ROLE-sentinel")
    assert evidence["firm_tools_seen"] and evidence["role_marker_seen"]


def test_survivors_distinguishes_pid_reuse_and_requires_disappearance():
    original = {"pid": 42, "start": "100", "state": "S"}
    reused = {"pid": 42, "start": "200", "state": "S"}
    zombie = {**original, "state": "Z"}
    assert task.survivors([original], [None, reused]) == []
    assert task.survivors([original], [zombie]) == [zombie]


@pytest.mark.parametrize("name", ["codex/auth.json", "claude/.credentials.json"])
@pytest.mark.parametrize("state", ["symlink", "copied", "outside_run", "missing", "dangling"])
def test_auth_probe_checks_metadata_without_reading_credentials(tmp_path, monkeypatch, name, state):
    namespace = {"__name__": "probe_test"}
    exec(task.AUTH_PROBE, namespace)
    run = tmp_path / "run"
    run.mkdir()
    credential = run / "credential"
    credential.write_text("secret-never-in-evidence")
    path = tmp_path / "home" / name
    path.parent.mkdir(parents=True)
    if state == "symlink":
        path.symlink_to(credential)
    elif state == "copied":
        path.write_text("secret-never-in-evidence")
    elif state == "outside_run":
        outside = tmp_path / "outside"
        outside.write_text("secret-never-in-evidence")
        path.symlink_to(outside)
    elif state == "dangling":
        path.symlink_to(run / "missing")

    def forbidden(*args, **kwargs):
        pytest.fail("auth inspection must not read credential contents")

    monkeypatch.setattr(Path, "open", forbidden)
    evidence = namespace["auth_evidence"](path, True, run)
    assert evidence["passed"] is (state == "symlink")
    assert evidence["path"] == str(path)
    assert all(type(v) is bool for k, v in evidence.items() if k != "path")
    assert "secret-never-in-evidence" not in json.dumps(evidence)


def test_claude_token_auth_requires_no_home_credential_file(tmp_path):
    namespace = {"__name__": "probe_test"}
    exec(task.AUTH_PROBE, namespace)
    path = tmp_path / ".credentials.json"
    assert namespace["auth_evidence"](path, False)["passed"]
    path.write_text("unexpected credential")
    assert not namespace["auth_evidence"](path, False)["passed"]


def test_auth_wrapper_inspects_after_provider_exit_and_preserves_exit_code(
    tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "codex/auth.json"
    path.parent.mkdir()
    monkeypatch.setenv("FIRM_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["probe", "codex", "provider", "arg"])

    def provider(argv, **kwargs):
        assert argv == ["provider", "arg"]
        path.write_text("credential replaced by provider")
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", provider)
    with pytest.raises(SystemExit) as exited:
        exec(task.AUTH_PROBE, {"__name__": "__main__"})
    assert exited.value.code == 7
    receipt = json.loads(capsys.readouterr().out)["task_auth_files"]
    assert not receipt["passed"] and not receipt["absent"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_acceptance_wraps_real_task_spec_and_filters_auth_evidence(monkeypatch, provider):
    spec = RunSpec("role", ("python", "launcher"), env={"key": "value"}, stdin=b"instruction")
    monkeypatch.setattr(task.Providers, "__init__", lambda *args: None)
    monkeypatch.setattr(task.Providers, "task_runner", lambda *args, **kwargs: SimpleNamespace(
        build_spec=lambda request, parent: spec,
    ))
    box = SimpleNamespace(child_layout=lambda *args: SimpleNamespace(python="guest-python"))
    runner = task.TaskProviders(None, None).task_runner(box, mcp_url="unused")
    wrapped = runner.build_spec(SimpleNamespace(provider=provider, run_id="one"),
                                SimpleNamespace(role_id="role"))
    assert wrapped.argv == ("guest-python", "-c", task.AUTH_PROBE, provider, *spec.argv)
    assert wrapped.env == spec.env and wrapped.stdin == spec.stdin
    name = "auth.json" if provider == "codex" else ".credentials.json"
    path = f"/var/lib/firm/task-homes/one/state/{provider}/{name}"
    receipt = {"path": path, "requires_symlink": True, "symlink_into_run": True,
               "absent": False, "passed": True}
    logs = json.dumps({"task_auth_files": {**receipt, "contents": "secret"}}).encode()
    assert task.auth_file_evidence(logs, provider, "one") == receipt
    assert task.auth_file_evidence(b"no receipt", provider, "one") == {
        "path": path, "passed": False,
    }


@pytest.mark.parametrize("state", ["clean", "refresh", "email", "copied", "symlink",
                                   "missing", "public", "outside_run"])
def test_agy_guest_checks_ephemeral_projection_and_native_file_cleanup(tmp_path, state):
    namespace = {"__name__": "probe_test"}
    exec(task.AUTH_PROBE, namespace)
    run = tmp_path / "run"
    run.mkdir()
    source = run / "agy-auth" if state != "outside_run" else tmp_path / "outside"
    document = {"auth_method": "oauth-personal", "token": {
        "access_token": "synthetic-access-never-in-evidence", "token_type": "Bearer",
        "expiry": "2099-01-01T00:00:00Z", "refresh_token": "",
    }}
    if state == "refresh":
        document["token"]["refresh_token"] = "synthetic-refresh-never-in-evidence"
    if state == "email":
        document["email"] = "synthetic-owner@example.invalid"
    if state != "missing":
        source.write_text(json.dumps(document))
        source.chmod(0o644 if state == "public" else 0o600)
    path = tmp_path / "native-auth"
    if state == "copied":
        path.write_text(json.dumps(document))
    elif state == "symlink":
        path.symlink_to(source)
    evidence = namespace["agy_auth_evidence"](path, source, run)
    assert evidence["passed"] is (state == "clean")
    assert "synthetic-" not in json.dumps(evidence)
    assert evidence["kind"] == "agy-access"


def test_agy_auth_receipt_filters_values_and_recomputes_pass():
    path = task.auth_file_evidence(b"", "agy", "one")["path"]
    assert "/home/.firm/agy-sessions/" in path
    assert path.endswith("/.gemini/antigravity-cli/antigravity-oauth-token")
    receipt = {"path": path, "kind": "agy-access", "private_file": True,
               "projection_into_run": True, "absent": True, "access_only": True,
               "no_refresh_token": True, "no_email": True, "passed": True}
    logs = json.dumps({"task_auth_files": {**receipt, "extra": "synthetic-private"}}).encode()
    assert task.auth_file_evidence(logs, "agy", "one") == receipt
    receipt["no_refresh_token"] = False
    logs = json.dumps({"task_auth_files": receipt}).encode()
    assert not task.auth_file_evidence(logs, "agy", "one")["passed"]
    assert not task.auth_file_evidence(logs, "agy", "different")["passed"]
    with pytest.raises(KeyError):
        task.auth_file_evidence(b"", "unknown", "one")


def test_agy_advertised_firm_tools_are_seen_inside_init():
    logs = json.dumps({"event": "init", "conversation_id": "synthetic-id",
                       "init": {"tools": ["mcp_firm_peek"]}}).encode()
    assert task.output_evidence(logs, AgyProvider, "ROLE-marker")["firm_tools_seen"]


def test_agy_task_wrapper_separates_receipt_from_native_parser(monkeypatch):
    projection = {"auth_method": "oauth-personal", "token": {
        "access_token": "synthetic-access", "token_type": "Bearer",
        "expiry": "2099-01-01T00:00:00Z", "refresh_token": "",
    }}
    spec = RunSpec("role", ("python", "launcher"), env={
        "FIRM_AGY_MODE": "task", "FIRM_AGY_ARGV": json.dumps([
            "agy", "--model", "gemini-3.8-flash-high"]),
    }, secrets={"agy-auth": json.dumps(projection).encode()}, stdin=b"synthetic instruction")
    parsed = []
    monkeypatch.setattr(task.Providers, "__init__", lambda *args: None)
    monkeypatch.setattr(task.Providers, "task_runner", lambda *args, **kwargs: SimpleNamespace(
        build_spec=lambda request, parent: spec,
        parse_output=lambda request, logs, *args, **kwargs: parsed.append(logs),
    ))
    box = SimpleNamespace(child_layout=lambda *args: SimpleNamespace(python="guest-python"))
    providers = task.TaskProviders(None, None)
    runner = providers.task_runner(box, mcp_url="unused")
    request = SimpleNamespace(provider="agy", run_id="one")
    wrapped = runner.build_spec(request, SimpleNamespace(role_id="role"))
    assert wrapped.argv == ("guest-python", "-c", task.AUTH_PROBE, "agy", *spec.argv)
    assert wrapped.stdin == spec.stdin and wrapped.secrets == spec.secrets
    assert providers.launches["one"]["credential_projection"]["passed"]
    native = b'{"event":"result"}\n'
    logs = native + b'{"task_auth_files":{"passed":true}}\n'
    runner.parse_output(request, logs, 0, apply_holds=True)
    assert parsed == [native]
    # Do not hide a malformed native tail or an event after the wrapper receipt.
    assert task.provider_output(logs + b"unexpected\n") == logs + b"unexpected\n"
