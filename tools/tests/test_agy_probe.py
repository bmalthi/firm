"""Offline AGY protocol and bounded process probes; no AGY credentials or inference."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"
for name in ("agy_protocol", "agy_probe"):
    spec = importlib.util.spec_from_file_location(name, ACCEPTANCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
protocol = sys.modules["agy_protocol"]
probe = sys.modules["agy_probe"]


@pytest.fixture
def events():
    return [
        {"event": "init", "conversation_id": "synthetic-conversation", "init": {}},
        {"event": "result", "result": {
            "conversation_id": "synthetic-conversation", "status": "SUCCESS", "response": "ok",
        }},
    ]


def analyze(events, exit_code=0):
    return protocol.analyze(b"\n".join(json.dumps(event).encode() for event in events), exit_code)


def test_replay_uses_shared_parser_without_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("replay must not start a process")
    monkeypatch.setattr(probe.subprocess, "Popen", forbidden)
    result = probe.replay(Path(__file__).parent / "fixtures" / "agy")
    assert result["passed"] and not result["provider_ready"]


@pytest.mark.parametrize("mode", ["role", "task"])
def test_captured_subscription_search_without_page_fetch(mode):
    fixture = json.loads((Path(__file__).parent / "fixtures/agy" /
                          f"captured_search_{mode}.json").read_text())
    result = analyze(fixture["events"], fixture["exit_code"])
    assert result["success"]
    assert result["tool_steps"]["search_web"] > 0
    assert "read_url_content" not in result["tool_steps"]
    assert not result["failed_tool_steps"]


@pytest.mark.parametrize("status", sorted(protocol.STATUSES))
@pytest.mark.parametrize("code", [0, 1, 2, -9])
def test_only_success_and_zero_exit_complete_work(events, status, code):
    events[-1]["result"]["status"] = status
    result = analyze(events, code)
    assert result["protocol_complete"]
    assert result["success"] is (status == "SUCCESS" and code == 0)


@pytest.mark.parametrize("raw,code", [
    (b'{"event":"result","event":"init"}', "duplicate_json_key"),
    (b'{"value":NaN}', "nonfinite_json"),
    (b'{"value":Infinity}', "nonfinite_json"),
    (b'\xff', "invalid_json"),
    (b'[]', "event_not_object"),
    (b'{}', "unsupported_event"),
    (b'{' * 2000, "invalid_json"),
    (b'a' * (protocol.MAX_BYTES + 1), "output_limit"),
])
def test_malformed_protocol_is_bounded_static_failure(raw, code):
    result = protocol.analyze(raw, 0)
    assert not result["success"] and result["error"] == code


@pytest.mark.parametrize("value", [-1, 0.5, True, "123", None])
def test_invalid_usage_not_accepted(events, value):
    events[-1]["result"]["usage"] = {"input_tokens": value}
    assert analyze(events)["error"] == "invalid_usage"


def test_usage_not_promoted_to_activation_counters(events):
    events[-1]["result"]["usage"] = {"input_tokens": 160, "output_tokens": 30}
    result = analyze(events)
    assert result["native_cumulative_usage"]["input_tokens"] == 160
    assert result["native_cumulative_usage"]["cache_read_tokens"] is None
    assert result["activation_usage"] is None


def test_no_result_changed_id_and_trailing_events_fail(events):
    assert analyze(events[:1])["error"] == "missing_result"
    events[-1]["result"]["conversation_id"] = "another-conversation"
    assert analyze(events)["error"] == "conversation_changed"
    events[-1]["result"]["conversation_id"] = "synthetic-conversation"
    assert analyze([*events, events[-1]])["error"] == "event_after_result"


def test_tool_updates_deduplicated_and_payloads_omitted(events):
    event = {"event": "step_update", "step_update": {
        "conversation_id": "synthetic-conversation", "step_index": 1,
        "step_type": "tool", "state": "DONE", "tool_name": "run_command",
        "tool_info": {"parameters": "secret-command", "output": "private-output"},
    }}
    result = analyze([events[0], event, event, events[1]])
    assert result["tool_steps"] == {"run_command": 1}
    assert not any(value in json.dumps(result) for value in (
        "secret-command", "private-output", "synthetic-conversation",
    ))


def test_environment_excludes_credentials_and_provider_settings(tmp_path, monkeypatch):
    for name in ("GEMINI_API_KEY", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS",
                 "DBUS_SESSION_BUS_ADDRESS", "FIRM_ROLE_TOKEN"):
        monkeypatch.setenv(name, "secret")
    env = probe.environment(tmp_path)
    assert set(env) == {"HOME", "PATH", "LANG", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                        "XDG_CACHE_HOME", "TMPDIR", "AGY_CLI_DISABLE_AUTO_UPDATE",
                        "USER", "LOGNAME"}
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "secret" not in str(env)


def test_inspection_reads_help_from_stderr(tmp_path):
    executable = tmp_path / "fake-agy"
    executable.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 1.5.3; "
                          "else echo '" + " ".join(probe.FLAGS) + "' >&2; fi\n")
    executable.chmod(0o700)
    result = probe.inspect_cli(executable, tmp_path / "inspection")
    assert result["inspection_complete"] and not result["provider_ready"]


def test_streams_stay_separate_and_nonzero_exit_preserved(tmp_path):
    stdout, stderr, code = probe.run(
        [sys.executable, "-c", "import sys;print('out');print('err',file=sys.stderr);sys.exit(2)"],
        tmp_path, probe.environment(tmp_path),
    )
    assert (stdout, stderr, code) == (b"out\n", b"err\n", 2)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_rejected_before_spawn(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(protocol.InvalidProtocol, match="invalid_timeout"):
        probe.run([], tmp_path, {}, timeout=timeout)


def test_timeout_and_output_limit_are_bounded(tmp_path):
    for script, error in [
        ("import time; time.sleep(5)", "process_timeout"),
        (f"print('x'*{protocol.MAX_BYTES + 1})", "output_limit"),
    ]:
        with pytest.raises(protocol.InvalidProtocol, match=error):
            probe.run([sys.executable, "-c", script], tmp_path, {}, timeout=0.2)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("close_pipes", [False, True])
def test_descendants_killed_even_after_successful_parent(tmp_path, close_pipes):
    marker = tmp_path / "delayed-marker"
    script = (
        "import os,signal,time; from pathlib import Path\n"
        "if os.fork()==0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        + (" os.close(1); os.close(2)\n" if close_pipes else "")
        + f" time.sleep(1); Path({str(marker)!r}).touch(); os._exit(0)\n"
        "print('success')\n"
    )
    try:
        result = probe.run([sys.executable, "-c", script], tmp_path, {}, timeout=0.2)
    except protocol.InvalidProtocol as exc:
        # Host sandboxes can deny querying a reparented descendant. Never report
        # success when that prevents cleanup confirmation.
        assert str(exc) in ({"cleanup_unconfirmed"} if close_pipes else {
            "process_timeout", "cleanup_unconfirmed",
        })
    else:
        assert close_pipes and result[2] == 0
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.parametrize("hook_runs", [False, True])
def test_exercise_uses_explicit_subscription_environment_and_never_claims_readiness(
    events, tmp_path, monkeypatch, hook_runs,
):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    events[-1]["result"]["response"] = "FIRM_OK"
    events[0]["init"]["tools"] = []

    def run(argv, workspace, env, *, timeout, stdin):
        assert "--print=" in argv and probe.PROMPT not in argv
        assert timeout == 25
        assert json.loads(stdin) == {"event": "user", "message": {"content": probe.PROMPT}}
        assert env["HOME"] == str(workspace.parent / "home")
        assert "GEMINI_API_KEY" not in env and "AGY_ADC_AUTH" not in env
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["useG1Credits"] is False
        assert settings["toolPermission"] == "strict"
        assert "--dangerously-skip-permissions" not in argv
        hook = json.loads((workspace / ".agents/hooks.json").read_text())
        hook_command = hook["firm-probe"]["SessionStart"][0]["command"]
        assert str(workspace / "firm-hook-executed") in hook_command
        if hook_runs:
            (workspace / "firm-hook-executed").touch()
        return b"\n".join(json.dumps(event).encode() for event in events), b"private diagnostic", 0

    monkeypatch.setattr(probe, "run", run)
    result = probe.exercise(executable, tmp_path / "fresh", "workspace-hook")
    assert result["case_passed"] is not hook_runs
    assert not result["provider_ready"]
    assert result["discovery_isolation"] == ("failed" if hook_runs else "unverified")
    assert "private diagnostic" not in json.dumps(result)
    assert json.loads((tmp_path / "fresh" / "decision.json").read_text()) == result


def test_exercise_failed_start_does_not_establish_hook_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    fixture = json.loads((Path(__file__).parent / "fixtures/agy/startup_error.json").read_text())
    stdout = b"\n".join(json.dumps(event).encode() for event in fixture["events"])
    monkeypatch.setattr(probe, "run", lambda *a, **k: (stdout, b"private", 1))
    result = probe.exercise(executable, tmp_path / "fresh", "workspace-hook")
    assert not result["case_passed"]
    assert result["discovery_isolation"] == "unverified"
    assert result["analysis"]["startup_failure"]


def test_exercise_requires_pinned_runtime_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.4")
    monkeypatch.setattr(probe, "run", lambda *a, **k: pytest.fail("unexpected inference"))
    executable = tmp_path / "agy"
    executable.touch()
    with pytest.raises(protocol.InvalidProtocol, match="unsupported_cli_version"):
        probe.exercise(executable, tmp_path / "fresh", "isolated-home")


def test_result_only_success_never_completes(events):
    events[-1]["result"]["conversation_id"] = ""
    result = analyze(events[-1:])
    assert not result["success"] and result["error"] == "invalid_conversation_id"


def test_error_prose_does_not_classify_auth_failure(events):
    events[-1]["result"].update(status="ERROR", error="authentication required")
    result = analyze(events)
    assert result["error"] == "unsuccessful_status"
    assert "authentication" not in json.dumps(result)


def test_unconfirmed_cleanup_overrides_success(tmp_path, monkeypatch):
    def denied(*args):
        raise PermissionError
    monkeypatch.setattr(probe.os, "killpg", denied)
    with pytest.raises(protocol.InvalidProtocol, match="cleanup_unconfirmed"):
        probe.run([sys.executable, "-c", "print('success')"], tmp_path, {}, timeout=1)


def test_stdout_and_stderr_share_one_output_budget(tmp_path):
    size = protocol.MAX_BYTES // 2 + 1
    script = f"import sys; sys.stdout.write('x'*{size});sys.stderr.write('y'*{size})"
    with pytest.raises(protocol.InvalidProtocol, match="output_limit"):
        probe.run([sys.executable, "-c", script], tmp_path, {}, timeout=2)


def test_prepare_login_uses_native_login_in_private_profile(tmp_path):
    executable = tmp_path / "fake agy"
    executable.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 1.5.3; "
                          "else printf '%s\\n' \"$@\"; fi\n")
    executable.chmod(0o700)
    profile = tmp_path / "profile with ' quotes"
    result = probe.prepare_login(executable, profile)
    assert not result["login_completed"] and not result["credentials_exported"]
    assert not result["provider_ready"]
    assert probe.profile_home(profile) == profile / "home"
    completed = probe.subprocess.run([str(profile / "login.sh")], capture_output=True, check=True)
    assert completed.stdout.splitlines() == [
        b"acp", b"--login", b"--include-default-customizations=false",
    ]
    assert json.loads((profile / "home/.gemini/antigravity-cli/settings.json").read_text())[
        "useG1Credits"
    ] is False
    assert json.loads((profile / "home/.gemini/config/config.json").read_text()) == {
        "userSettings": {"useAiCredits": False},
    }
    assert (profile / "login.sh").stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        probe.prepare_login(executable, profile)


@pytest.mark.parametrize("part", ["home", "workspace", "home/.gemini", "home/.gemini/config",
                                 "home/.gemini/config/agents", "home/.gemini/antigravity-cli"])
def test_authenticated_profile_rejects_linked_directories(tmp_path, monkeypatch, part):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    profile = tmp_path / "profile"
    probe.prepare_login(executable, profile)
    path = profile / part
    moved = path.with_name(path.name + "-moved")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(protocol.InvalidProtocol, match="invalid_profile_dir"):
        probe.profile_home(profile)


def test_authenticated_profile_is_reused_without_reading_credentials(tmp_path, events, monkeypatch):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    profile = tmp_path / "profile"
    probe.prepare_login(executable, profile)
    credential = profile / "home/.gemini/antigravity-cli/opaque-credential"
    credential.write_bytes(b"do-not-read-or-copy")
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != credential
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    events[0]["init"]["tools"] = []
    events[-1]["result"]["response"] = "FIRM_OK"

    def run(argv, workspace, env, **kwargs):
        assert env["HOME"] == str(profile / "home")
        assert argv[argv.index("--agent") + 1] == "firm-probe"
        return b"\n".join(json.dumps(event).encode() for event in events), b"", 0

    monkeypatch.setattr(probe, "run", run)
    result = probe.exercise(executable, tmp_path / "exercise", "isolated-home", profile=profile)
    assert result["case_passed"] and result["dedicated_profile_reused"]
    assert "do-not-read-or-copy" not in json.dumps(result)
    assert credential.stat().st_size == len(b"do-not-read-or-copy")


@pytest.mark.parametrize("inventory", [None, ["run_command"]])
def test_tool_free_probe_requires_an_empty_advertised_inventory(
    tmp_path, events, monkeypatch, inventory,
):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    events[0]["init"]["tools"] = inventory
    events[-1]["result"]["response"] = "FIRM_OK"
    stdout = b"\n".join(json.dumps(event).encode() for event in events)
    monkeypatch.setattr(probe, "run", lambda *a, **k: (stdout, b"", 0))
    result = probe.exercise(executable, tmp_path / "exercise", "isolated-home")
    assert not result["case_passed"] and not result["provider_ready"]


def native_credential():
    return {"auth_method": "consumer", "id_token_email": "private@example.invalid", "token": {
        "access_token": "synthetic-access", "refresh_token": "synthetic-renewal",
        "token_type": "Bearer", "expiry": "2030-01-01T00:00:00Z", "expires_in": 3600,
        "unknown": "never-project",
    }}


def test_access_projection_drops_renewal_identity_and_unknown_fields():
    document = native_credential()
    result = probe.access_projection(json.dumps(document).encode())
    assert result == {"auth_method": "consumer", "token": {
        "access_token": "synthetic-access", "refresh_token": "", "token_type": "Bearer",
        "expiry": "2030-01-01T00:00:00Z", "expires_in": 3600,
    }}
    assert document["token"]["refresh_token"] == "synthetic-renewal"
    expired = probe.access_projection(json.dumps(document).encode(), expired=True)
    assert expired["token"]["expiry"] == "2000-01-01T00:00:00Z"
    assert expired["token"]["expires_in"] == 0


@pytest.mark.parametrize("key,value", [
    ("access_token", ""), ("access_token", None), ("token_type", "Other"),
    ("expiry", "not-a-date"), ("expiry", "2030-01-01T00:00:00"),
    ("expires_in", True), ("expires_in", -1),
])
def test_access_projection_rejects_invalid_native_fields(key, value):
    document = native_credential()
    document["token"][key] = value
    with pytest.raises(protocol.InvalidProtocol, match="invalid_native"):
        probe.access_projection(json.dumps(document).encode())


@pytest.mark.parametrize("failure", [False, True])
def test_access_projection_is_private_and_removed_even_after_failure(tmp_path, monkeypatch, events,
                                                                  failure):
    monkeypatch.setattr(probe, "command", lambda *a: b"1.5.3")
    executable = tmp_path / "agy"
    executable.touch()
    profile = tmp_path / "profile"
    probe.prepare_login(executable, profile)
    source = profile / "home/.gemini/antigravity-cli/antigravity-oauth-token"
    original = json.dumps(native_credential())
    source.write_text(original)
    source.chmod(0o600)
    work = tmp_path / "exercise"
    staged = work / "home/.gemini/antigravity-cli/antigravity-oauth-token"

    def run(argv, workspace, env, **kwargs):
        assert env["HOME"] == str(work / "home")
        assert staged.stat().st_mode & 0o777 == 0o600
        assert json.loads(staged.read_text())["token"]["refresh_token"] == ""
        if failure:
            raise protocol.InvalidProtocol("process_timeout")
        return b"\n".join(json.dumps(event).encode() for event in events), b"", 0

    monkeypatch.setattr(probe, "run", run)
    if failure:
        with pytest.raises(protocol.InvalidProtocol, match="process_timeout"):
            probe.exercise(executable, work, "isolated-home", profile=profile,
                           credential_mode="access-only")
    else:
        result = probe.exercise(executable, work, "isolated-home", profile=profile,
                                credential_mode="access-only")
        assert not result["refresh_material_projected"]
        assert "synthetic-access" not in json.dumps(result)
    assert not staged.exists()
    assert source.read_text() == original


def test_captured_timeout_success_is_not_completion(events):
    events[-1]['result']['finished_reason'] = 'print_timeout'
    result = analyze(events)
    assert result['protocol_complete'] and result['status'] == 'SUCCESS'
    assert not result['success'] and result['error'] == 'print_timeout'


def test_resume_requires_exact_native_id(events):
    stream = b'\n'.join(json.dumps(event).encode() for event in events)
    result = protocol.analyze(stream, 0, expected_conversation_id='requested-other-id')
    assert not result['success'] and result['error'] == 'resume_id_mismatch'
    assert 'requested-other-id' not in json.dumps(result)


def test_failed_tool_is_structured_even_when_native_result_succeeds(events):
    update = {'event': 'step_update', 'step_update': {
        'conversation_id': 'synthetic-conversation', 'step_index': 2,
        'step_type': 'tool', 'tool_name': 'mcp_firm_firm_echo', 'state': 'ERROR',
    }}
    result = analyze([events[0], update, events[1]])
    assert result['success']
    assert result['failed_tool_steps'] == {'mcp_firm_firm_echo': 1}
    assert result['tool_steps'] == {'mcp_firm_firm_echo': 1}


def test_multiple_turns_require_explicit_count_and_keep_latest_cumulative_usage(events):
    events[-1]['result']['usage'] = {'input_tokens': 10, 'output_tokens': 5}
    second = json.loads(json.dumps(events[-1]))
    second['result']['usage'] = {'input_tokens': 30, 'output_tokens': 12}
    step = {'event': 'step_update', 'step_update': {
        'conversation_id': 'synthetic-conversation', 'step_index': 2,
        'step_type': 'user_input', 'state': 'DONE',
    }}
    stream = b'\n'.join(json.dumps(e).encode() for e in [*events, step, second])
    assert protocol.analyze(stream, 0)['error'] == 'event_after_result'
    result = protocol.analyze(stream, 0, expected_turns=2)
    assert result['success'] and result['native_cumulative_usage']['input_tokens'] == 30
    assert result['activation_usage'] is None
    assert protocol.analyze(stream, 0, expected_turns=3)['error'] == 'missing_turn_result'


def test_capture_omits_identity_arguments_and_diagnostics(events):
    events[0]['init'].update(model='gemini-4-argon-high', cwd='/private/path',
                             agent='private-agent')
    events[-1]['result'].update(response='PRIVATE_RESPONSE', error='PRIVATE_ERROR')
    events.insert(1, {'event': 'step_update', 'step_update': {
        'conversation_id': 'synthetic-conversation', 'step_index': 1,
        'step_type': 'tool', 'state': 'ERROR', 'tool_name': 'call_mcp_tool',
        'tool_info': {'parameters': 'PRIVATE_TOKEN', 'output': 'PRIVATE_OUTPUT'},
    }})
    captured = probe.capture_events(b'\n'.join(json.dumps(e).encode() for e in events))
    text = json.dumps(captured)
    assert all(secret not in text for secret in (
        'PRIVATE_', 'synthetic-conversation', '/private/path', 'private-agent'))
    assert captured[0]['init']['model'] == 'gemini-4-argon-high'
    assert captured[1]['step_update']['state'] == 'ERROR'


@pytest.mark.parametrize('tool_name', ['firm_echo', 'org_tree'])
def test_echo_server_rejects_bad_authorization_and_records_real_call(tool_name):
    import urllib.error
    import urllib.request

    with probe.echo_server(tool_names=[tool_name]) as (url, token, receipts, _server):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': tool_name, 'arguments': {}}}).encode()
        wrong = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer wrong'})
        with pytest.raises(urllib.error.HTTPError, match='401'):
            urllib.request.urlopen(wrong, timeout=2)
        request = urllib.request.Request(url, data=body,
                                         headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)['result']['content'][0]['text'] == 'FIRM_ECHO_OK'
        assert receipts == [{'authorized': False}, {
            'authorized': True, 'method': 'tools/call', 'tool': tool_name,
        }]
        assert token not in json.dumps(receipts)


def test_gate_profile_is_restricted_before_reading_credentials(tmp_path, monkeypatch):
    executable = tmp_path / 'agy'
    executable.touch()
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: pytest.fail('unexpected credential read'))
    with pytest.raises(protocol.InvalidProtocol, match='dedicated_profile_required'):
        probe.live_gates(executable, tmp_path / 'work', tmp_path / 'normal-home', 'models')


def test_run_observer_can_cancel_and_preserves_partial_stream(tmp_path):
    import signal

    seen = []

    def stop(process):
        if seen:
            os.killpg(process.pid, signal.SIGTERM)

    stdout, _stderr, code = probe.run(
        [sys.executable, '-c', 'import time; print("partial", flush=True); time.sleep(5)'],
        tmp_path, probe.environment(tmp_path), timeout=2, poll=stop, observe=seen.append,
    )
    assert stdout == b'partial\n' and code == -signal.SIGTERM and seen


@pytest.mark.parametrize('failure', [False, True])
def test_live_gate_projection_cleanup_and_sanitized_capture(tmp_path, monkeypatch, events, failure):
    import tempfile

    executable = tmp_path / 'agy'
    executable.touch()
    monkeypatch.setattr(probe, 'profile_home', lambda _p: tmp_path)
    monkeypatch.setattr(probe, 'command', lambda *a: b'1.5.3')
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(native_credential()).encode())
    events[-1]['result']['response'] = 'PRIVATE_RESPONSE'

    def run(argv, workspace, env, **kwargs):
        credential = Path(env['HOME']) / '.gemini/antigravity-cli/antigravity-oauth-token'
        value = json.loads(credential.read_text())
        assert value['token']['refresh_token'] == ''
        assert value['token']['access_token'] == 'synthetic-access'
        assert credential.stat().st_mode & 0o777 == 0o600
        assert json.loads(argv[argv.index('--settings') + 1])['useG1Credits'] is False
        assert kwargs['timeout'] == 42
        if failure:
            raise protocol.InvalidProtocol('process_timeout')
        return b'\n'.join(json.dumps(e).encode() for e in events), b'PRIVATE_ERROR', 0

    monkeypatch.setattr(probe, 'run', run)
    with tempfile.TemporaryDirectory(prefix='firm-agy-unit-', dir='/tmp') as directory:
        work = Path(directory)
        work.rmdir()  # The gate requires a new path, and the context still owns cleanup.
        result = probe.live_gates(executable, work, Path('/tmp/firm-agy-dedicated-profile'),
                                  'models', only='gemini-3.8-flash-medium')
        assert not (work / 'home/.gemini/antigravity-cli/antigravity-oauth-token').exists()
        assert len(result['rows']) == 1
        assert all(value not in json.dumps(result) for value in (
            'synthetic-access', 'synthetic-renewal', 'private@example.invalid',
            'PRIVATE_ERROR', 'PRIVATE_RESPONSE'))
        if failure:
            assert result['rows'][0]['error'] == 'process_timeout'


def test_token_remaining_uses_actual_expiry_not_expires_in():
    from datetime import UTC, datetime

    value = probe.access_projection(json.dumps(native_credential()).encode())
    value['token']['expires_in'] = 1
    assert probe.token_remaining(value, now=datetime(2029, 12, 31, 23, 54, tzinfo=UTC)) == 360


@pytest.mark.parametrize('failed_rename', [False, True])
def test_atomic_expiry_replacement_stays_private_and_drops_refresh(
    tmp_path, monkeypatch, failed_rename,
):
    path = tmp_path / 'antigravity-oauth-token'
    path.write_text('previous')
    original_replace = probe.os.replace

    def replace(source, destination):
        assert source.parent == destination.parent == tmp_path
        value = json.loads(source.read_text())
        assert value['token']['refresh_token'] == '' and 'id_token_email' not in value
        assert source.stat().st_mode & 0o777 == 0o600
        assert destination.read_text() == 'previous'
        if failed_rename:
            raise OSError('synthetic failure')
        original_replace(source, destination)

    monkeypatch.setattr(probe.os, 'replace', replace)
    if failed_rename:
        with pytest.raises(OSError):
            probe.replace_projection(path, native_credential())
        assert path.read_text() == 'previous'
    else:
        probe.replace_projection(path, native_credential())
        assert json.loads(path.read_text())['token']['access_token'] == 'synthetic-access'
    assert list(tmp_path.iterdir()) == [path]


def test_native_renewal_uses_models_and_reports_metadata_only(tmp_path, monkeypatch):
    source = tmp_path / 'home'
    source.mkdir()
    first = native_credential()
    second = native_credential()
    second['token']['access_token'] = 'PRIVATE_RENEWED'
    values = iter([first, second])
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(next(values)).encode())

    def run(argv, workspace, env, **kwargs):
        assert argv == ['/synthetic/agy', 'models']
        assert workspace == tmp_path / 'workspace' and env['HOME'] == str(source)
        assert kwargs['timeout'] == 30
        return b'PRIVATE_MODELS', b'PRIVATE_DIAGNOSTIC', 0

    monkeypatch.setattr(probe, 'run', run)
    projected, evidence = probe.native_renewal(Path('/synthetic/agy'), source)
    assert projected['token']['refresh_token'] == ''
    assert evidence['token_changed'] and evidence['exit_code'] == 0
    assert 'PRIVATE_' not in json.dumps(evidence)
    assert 'synthetic-access' not in json.dumps(evidence)


@pytest.mark.parametrize('replacement', [False, True])
@pytest.mark.parametrize('timeout', [False, True])
@pytest.mark.parametrize('changed_during_models', [False, True])
def test_expiry_turn_replaces_only_selected_copy_and_cleans_up(
    tmp_path, monkeypatch, events, replacement, timeout, changed_during_models,
):
    from datetime import UTC, datetime, timedelta

    value = probe.access_projection(json.dumps(native_credential()).encode())
    value['token']['expiry'] = (datetime.now(UTC) + timedelta(seconds=390)).isoformat()
    fresh = json.loads(json.dumps(value))
    fresh['token']['access_token'] = 'synthetic-fresh-access'
    clock = [1000.0]
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])
    renewals = []

    def renew(*args):
        renewals.append(True)
        return fresh, {'exit_code': 0, 'token_changed': changed_during_models}

    monkeypatch.setattr(probe, 'native_renewal', renew)
    events[-1]['result']['response'] = 'FIRM_DONE'

    def run(argv, workspace, env, **kwargs):
        assert argv[argv.index('--print-timeout') + 1] == '0'
        assert kwargs['timeout'] == 900
        credential = Path(env['HOME']) / '.gemini/antigravity-cli/antigravity-oauth-token'
        assert json.loads(credential.read_text()) == value
        clock[0] += 121
        kwargs['poll'](None)
        assert json.loads(credential.read_text()) == (fresh if replacement else value)
        kwargs['observe'](json.dumps(events[0]).encode() + b'\n')
        if timeout:
            raise protocol.InvalidProtocol('process_timeout')
        return b'\n'.join(json.dumps(e).encode() for e in events), b'PRIVATE_ERROR', 0

    monkeypatch.setattr(probe, 'run', run)
    result = probe.expiry_turn(Path('/synthetic/agy'), tmp_path / 'turn', tmp_path / 'source',
                               value, replacement=replacement)
    assert result['projection_removed']
    assert result['replacement_published'] is replacement
    assert bool(renewals) is replacement
    assert 'PRIVATE_ERROR' not in json.dumps(result)
    assert 'synthetic-access' not in json.dumps(result)
    assert not list((tmp_path / 'turn/home/.gemini/antigravity-cli').glob('.firm-agy-token-*'))
    if timeout:
        assert result['outer_error'] == 'process_timeout' and result['exit_code'] is None
        assert result['analysis']['error'] == 'missing_result'
    else:
        assert result['response_is_firm_done'] and result['analysis']['success']


def test_expiry_gate_waits_for_real_window_and_checks_native_renewal(tmp_path, monkeypatch):
    value = probe.access_projection(json.dumps(native_credential()).encode())
    fresh = json.loads(json.dumps(value))
    fresh['token']['access_token'] = 'synthetic-renewed'
    clock = {'elapsed': 0.0, 'remaining': 1201.0}
    monkeypatch.setattr(probe, 'read_private_credential', lambda *a: json.dumps(value).encode())
    monkeypatch.setattr(probe, 'token_remaining',
                        lambda projection: 3599 if projection == fresh else clock['remaining'])
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock['elapsed'])

    def sleep(seconds):
        assert seconds <= 5
        clock['remaining'] -= seconds
        clock['elapsed'] += seconds

    monkeypatch.setattr(probe.time, 'sleep', sleep)
    checks = []

    def renewal(*args):
        checks.append(clock['remaining'])
        return fresh, {'exit_code': 0, 'token_changed': True}

    monkeypatch.setattr(probe, 'native_renewal', renewal)

    def turn(executable, directory, source, original, *, replacement):
        assert 330 <= clock['remaining'] <= 390 and original == value
        return {'label': 'replacement' if replacement else 'no-replacement'}

    monkeypatch.setattr(probe, 'expiry_turn', turn)
    result = probe.expiry_gate(Path('/synthetic/agy'), tmp_path, tmp_path / 'source')
    assert checks == [1200.0, 600.0, 390.0]
    assert len(result['rows']) == 2 and result['phase'] == 'complete'
    assert value['token']['expiry'] == '2030-01-01T00:00:00Z'


@pytest.mark.parametrize('mismatch', [False, True])
def test_expiry_resume_requires_same_conversation_and_removes_fresh_projection(
    tmp_path, monkeypatch, events, mismatch,
):
    native_id = '00000000-0000-4000-8000-000000000001'
    home = tmp_path / 'home'
    workspace = tmp_path / 'workspace'
    home.mkdir()
    workspace.mkdir()
    probe.configure_home(home)
    conversations = home / '.gemini/antigravity-cli/conversations'
    conversations.mkdir()
    (conversations / (native_id + '.db')).touch()
    (workspace / 'step-6.txt').touch()
    events[0]['conversation_id'] = 'another-conversation' if mismatch else native_id
    events[-1]['result'].update(conversation_id=events[0]['conversation_id'], response='6')
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(native_credential()).encode())

    def run(argv, cwd, env, **kwargs):
        assert argv[argv.index('--conversation') + 1] == native_id and cwd == workspace
        credential = Path(env['HOME']) / '.gemini/antigravity-cli/antigravity-oauth-token'
        assert json.loads(credential.read_text())['token']['refresh_token'] == ''
        return b'\n'.join(json.dumps(e).encode() for e in events), b'', 0

    monkeypatch.setattr(probe, 'run', run)
    result = probe.resume_expiry_turn(Path('/synthetic/agy'), tmp_path, tmp_path / 'source')
    assert result['projection_removed'] and result['completed_step_recalled']
    assert result['analysis']['success'] is not mismatch
    assert native_id not in json.dumps(result)


@pytest.mark.parametrize('name', [
    'captured_expiry_no_replacement', 'captured_expiry_replacement',
    'captured_expiry_resume_stale_error',
])
def test_real_expiry_capture_exit_zero_does_not_mean_success(name):
    fixture = json.loads((Path(__file__).parent / 'fixtures/agy' / (name + '.json')).read_text())
    assert fixture['exit_code'] == 0
    result = analyze(fixture['events'], fixture['exit_code'])
    assert result['protocol_complete'] and not result['success']
    assert result['status'] == 'ERROR' and result['finished_reason'] == 'error'
    assert result['error'] == 'unsuccessful_status'
    assert result['native_cumulative_usage']['input_tokens'] > 0


def test_expiry_gate_bounds_persistent_error_confirmation_to_one_extra_resume(
    tmp_path, monkeypatch,
):
    value = probe.access_projection(json.dumps(native_credential()).encode())
    monkeypatch.setattr(probe, 'read_private_credential', lambda *a: json.dumps(value).encode())
    monkeypatch.setattr(probe, 'token_remaining', lambda *a: 390)
    monkeypatch.setattr(probe, 'native_renewal', lambda *a: (value, {'exit_code': 0}))

    def turn(executable, directory, source, original, **kwargs):
        return {'label': directory.name, 'analysis': {'status': 'ERROR'},
                'diagnostic_markers': ['expired']}

    monkeypatch.setattr(probe, 'expiry_turn', turn)
    calls = []

    def resume(executable, directory, source):
        calls.append(directory.name)
        return {'label': directory.name + '-resume', 'analysis': {'status': 'ERROR'},
                'completed_step_recalled': True}

    monkeypatch.setattr(probe, 'resume_expiry_turn', resume)
    result = probe.expiry_gate(Path('/synthetic/agy'), tmp_path, tmp_path / 'source')
    assert calls == ['no-replacement', 'replacement', 'no-replacement']
    assert result['resume_confirmation']['label'] == 'no-replacement-resume-confirmation'


@pytest.mark.parametrize('failure', [False, True])
def test_forced_native_renewal_changes_only_expiry_and_restores_billing(
    tmp_path, monkeypatch, failure,
):
    home = tmp_path / 'home'
    home.mkdir()
    probe.configure_home(home)
    path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
    before = native_credential()
    before['host_extra'] = {'preserve': True}
    probe.private_json(path, before)

    def native(executable, source):
        stale = json.loads(path.read_text())
        expected = json.loads(json.dumps(before))
        expected['token']['expiry'] = '2000-01-01T00:00:00Z'
        assert stale == expected and source == home
        assert path.stat().st_mode & 0o777 == 0o600
        if failure:
            return {}, {'exit_code': 1}
        after = json.loads(json.dumps(before))
        after['token']['access_token'] = 'synthetic-fresh-access'
        probe.replace_private_json(path, after)
        settings = home / '.gemini/antigravity-cli/settings.json'
        config = home / '.gemini/config/config.json'
        probe.private_json(settings, {'unrelated': True})
        probe.private_json(config, {'userSettings': {'useAiCredits': True, 'unrelated': True}})
        return probe.access_projection(json.dumps(after).encode()), {'exit_code': 0}

    monkeypatch.setattr(probe, 'native_renewal', native)
    result = probe.forced_native_renewal(Path('/synthetic/agy'), home)
    assert result['billing_keys_restored_false']
    assert not result['expires_in_changed_before_native']
    assert result['top_level_fields_preserved'] and result['token_fields_preserved']
    assert result['non_token_fields_unchanged'] and not result['refresh_token_rotated']
    assert json.loads((home / '.gemini/antigravity-cli/settings.json').read_text())[
        'useG1Credits'] is False
    assert json.loads((home / '.gemini/config/config.json').read_text())[
        'userSettings']['useAiCredits'] is False
    assert all(value not in json.dumps(result) for value in (
        'synthetic-access', 'synthetic-renewal', 'private@example.invalid'))
    if failure:
        assert json.loads(path.read_text()) == before
        assert result['cached_expiry_restored_after_failure']
    else:
        assert result['access_token_changed']
        assert json.loads((home / '.gemini/antigravity-cli/settings.json').read_text())['unrelated']


def test_run_after_exit_observes_native_descendants_before_forced_cleanup(tmp_path):
    marker = tmp_path / 'delayed'
    child = f'import time;from pathlib import Path;time.sleep(.2);Path({str(marker)!r}).touch()'
    script = ('import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(child)
              + '],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)')
    observed = []

    def after_exit(process):
        assert process.returncode == 0 and not marker.exists()
        time.sleep(.4)
        observed.append(marker.exists())

    _out, _err, code = probe.run([sys.executable, '-c', script], tmp_path, {}, timeout=2,
                                after_exit=after_exit)
    assert code == 0 and observed == [True]


@pytest.mark.parametrize('delayed', [False, True])
def test_yield_probe_observes_native_exit_before_forced_cleanup(
    tmp_path, monkeypatch, events, delayed,
):
    from types import SimpleNamespace

    home = tmp_path / 'home'
    workspace = tmp_path / 'workspace'
    home.mkdir()
    workspace.mkdir()
    probe.configure_home(home)
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(native_credential()).encode())
    clock = [0.0]
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        if delayed and clock[0] >= 100:
            (workspace / 'cycle-1-step-3.txt').touch()

    monkeypatch.setattr(probe.time, 'sleep', sleep)
    events[-1]['result']['finished_reason'] = 'print_timeout'

    def run(argv, cwd, env, **kwargs):
        assert argv[argv.index('--print-timeout') + 1] == '90s'
        assert kwargs['timeout'] == 135
        (workspace / 'cycle-1-step-2.txt').touch()
        clock[0] = 90
        process = SimpleNamespace(poll=lambda: 0)
        kwargs['poll'](process)
        kwargs['after_exit'](process)
        assert clock[0] == 135
        return b'\n'.join(json.dumps(e).encode() for e in events), b'', 0

    monkeypatch.setattr(probe, 'run', run)
    row, native_id = probe.yield_probe_turn(Path('/synthetic/agy'), tmp_path, tmp_path / 'source',
                                           nonce='synthetic-nonce', cycle=1)
    assert native_id == 'synthetic-conversation'
    assert row['analysis']['error'] == 'print_timeout'
    assert row['native_exit_elapsed_seconds'] == 90
    assert row['markers_at_native_exit'] == [2]
    assert row['delayed_marker_after_exit'] is delayed
    assert row['observation_before_forced_group_cleanup'] and row['projection_removed']
    assert 'synthetic-nonce' not in json.dumps(row)


def test_yield_resume_checks_recall_and_normal_native_success(tmp_path, monkeypatch, events):
    home = tmp_path / 'home'
    workspace = tmp_path / 'workspace'
    home.mkdir()
    workspace.mkdir()
    probe.configure_home(home)
    (workspace / 'cycle-1-step-2.txt').touch()
    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(native_credential()).encode())
    events[-1]['result']['response'] = 'synthetic-nonce STEP=2'

    def run(argv, cwd, env, **kwargs):
        assert argv[argv.index('--conversation') + 1] == 'synthetic-conversation'
        assert kwargs['after_exit'] is None
        return b'\n'.join(json.dumps(e).encode() for e in events), b'', 0

    monkeypatch.setattr(probe, 'run', run)
    row, _ = probe.yield_probe_turn(Path('/synthetic/agy'), tmp_path, tmp_path / 'source',
                                    nonce='synthetic-nonce', cycle=1,
                                    conversation='synthetic-conversation', resume=True)
    assert row['analysis']['success'] and row['analysis']['finished_reason'] is None
    assert row['nonce_recalled'] and row['step_recalled'] and row['projection_removed']


def test_renewal_yield_gate_spaces_three_renewals_and_resumes_same_id(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    probe.configure_home(home)
    clock = [0.0]
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(probe.time, 'sleep',
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(probe, 'forced_native_renewal', lambda *a: {'native': {'exit_code': 0}})
    calls = []

    def turn(executable, directory, source, **kwargs):
        calls.append((kwargs['cycle'], kwargs.get('resume', False), kwargs['conversation']))
        clock[0] += 10
        return {'label': 'synthetic'}, 'synthetic-conversation'

    monkeypatch.setattr(probe, 'yield_probe_turn', turn)
    poisoned_calls = []

    def poisoned(*args):
        poisoned_calls.append(True)
        return {'label': 'poisoned'}

    monkeypatch.setattr(probe, 'resume_expiry_turn', poisoned)
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda path: True if str(path).startswith(
        '/tmp/firm-agy-a2-expiry-fixed-1107/no-replacement/') else original_is_dir(path))
    result = probe.renewal_yield_gate(Path('/synthetic/agy'), tmp_path, tmp_path / 'source')
    assert [row['elapsed_seconds'] for row in result['renewals']] == [0, 180, 360]
    assert calls == [(1, False, None), (1, True, 'synthetic-conversation'),
                     (2, False, 'synthetic-conversation'), (2, True, 'synthetic-conversation')]
    assert poisoned_calls == [True] and result['phase'] == 'complete'


@pytest.mark.parametrize('cycle', [1, 2])
def test_captured_print_yield_is_not_completion_and_fresh_resume_is_normal(cycle):
    fixtures = Path(__file__).parent / 'fixtures/agy'
    yielded = json.loads((fixtures / f'captured_print_cycle_{cycle}_yield.json').read_text())
    resumed = json.loads((fixtures / f'captured_print_cycle_{cycle}_resume.json').read_text())
    result = analyze(yielded['events'], yielded['exit_code'])
    assert result['protocol_complete'] and not result['success']
    assert result['status'] == 'SUCCESS' and result['finished_reason'] == 'print_timeout'
    assert yielded['markers_at_native_exit'] == [1, 2]
    assert yielded['markers_45s_after_native_exit'] == [1, 2, 3]
    result = analyze(resumed['events'], resumed['exit_code'])
    assert result['success'] and result['finished_reason'] is None
    assert resumed['nonce_recalled']


def test_captured_mcp_inventory_and_success_do_not_prove_tool_availability():
    fixtures = Path(__file__).parent / 'fixtures/agy'
    for label, expected in [('float_ignored', {}),
                            ('integer_authenticated', {'mcp_firm_org_tree': 1})]:
        captured = json.loads((fixtures / f'captured_mcp_{label}.json').read_text())
        result = analyze(captured['events'], captured['exit_code'])
        inventory = captured['events'][0]['init']['tools']
        assert result['success'] and result['tool_steps'] == expected
        assert len(inventory) == 60 and not any(t.startswith('mcp_firm_') for t in inventory)
        assert captured['mcp_symlink'] and captured['catalog_size'] == 42
        calls = [r for r in captured['receipts'] if r.get('method') == 'tools/call']
        assert calls == ([{'authorized': True, 'method': 'tools/call', 'tool': 'org_tree'}]
                         if expected else [])


@pytest.mark.parametrize('label,fail', [('float-timeout', False), ('integer-timeout', False),
                                       ('integer-timeout', True)])
def test_launcher_mcp_reproduction_preserves_encoding_and_cleans_secrets(
        tmp_path, monkeypatch, events, label, fail):
    from contextlib import contextmanager

    monkeypatch.setattr(probe, 'read_private_credential',
                        lambda *a: json.dumps(native_credential()).encode())
    monkeypatch.setattr(probe, 'token_remaining', lambda value: 1800)

    @contextmanager
    def server(**kwargs):
        assert 'org_tree' in kwargs['tool_names']
        yield 'http://127.0.0.1:1234/mcp', 'PRIVATE_MCP_SECRET', [], None

    @contextmanager
    def proxy():
        yield 'http://127.0.0.1:1235', []

    monkeypatch.setattr(probe, 'echo_server', server)
    monkeypatch.setattr(probe, 'rejecting_proxy', proxy)
    parent = dict(os.environ)
    homes = []

    def run(argv, cwd, env, **kwargs):
        home = Path(env['HOME'])
        homes.append(home)
        path = home / '.gemini/config/mcp_config.json'
        assert path.is_symlink()
        entry = json.loads(path.read_bytes())['mcpServers']['firm']
        assert type(entry['timeoutSeconds']) is (float if label == 'float-timeout' else int)
        assert entry['headers']['Authorization'] == 'Bearer PRIVATE_MCP_SECRET'
        assert 'FIRM_ACTIVATION_TOKEN' not in env
        assert dict(os.environ) == parent
        if fail:
            raise protocol.InvalidProtocol('process_timeout')
        return b'\n'.join(json.dumps(e).encode() for e in events), b'', 0

    monkeypatch.setattr(probe, 'run', run)
    if fail:
        with pytest.raises(protocol.InvalidProtocol, match='process_timeout'):
            probe.launcher_mcp_gate(Path('/synthetic/agy'), tmp_path, tmp_path, only=label)
    else:
        result = probe.launcher_mcp_gate(Path('/synthetic/agy'), tmp_path, tmp_path, only=label)
        assert 'PRIVATE_MCP_SECRET' not in json.dumps(result)
    assert homes and dict(os.environ) == parent
    assert not list(tmp_path.rglob('agy-auth')) and not list(tmp_path.rglob('agy-mcp'))
    assert not (homes[0] / '.gemini/config/mcp_config.json').is_symlink()
    assert not (homes[0] / '.gemini/antigravity-cli/antigravity-oauth-token').is_symlink()
