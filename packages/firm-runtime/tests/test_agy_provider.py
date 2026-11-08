import json
from pathlib import Path
from uuid import uuid4

import pytest
from firm_sandbox import Layout
from test_agy_credentials import account
from test_codex_provider import invocation

from firm_runtime import agy_launch
from firm_runtime.agy_provider import (
    AgyProvider,
    native_model_id,
    parse_failed_tool_calls,
    parse_result,
    parse_summary,
    parse_tool_calls,
    parse_usage,
)
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.summaries import SummaryLaunch

FIXTURES = Path(__file__).resolve().parents[3] / "tools/tests/fixtures/agy"


def stream(*, status="SUCCESS", usage=None):
    return [
        {"event": "init", "conversation_id": "synthetic-id", "init": {}},
        {
            "event": "result",
            "result": {
                "conversation_id": "synthetic-id",
                "status": status,
                "response": "FIRM_OK",
                "usage": usage,
            },
        },
    ]


def encode(events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_offline_protocol_fixtures(fixture):
    value = json.loads(fixture.read_bytes())
    events = value["events"]
    expected_id = value.get("parser_options", {}).get("expected_conversation_id")
    if expected_id:
        events = [{"type": "firm.agy_expected_session", "conversation_id": expected_id}, *events]
    result = parse_result(encode(events), value["exit_code"])
    options = value.get("parser_options", {})
    if options.get("expected_turns", 1) != 1:
        # Firm sends exactly one user event; multiple turn results are invalid
        # for this adapter even when the multi-input A0 harness expects them.
        assert not result.success
    elif options.get("expected_conversation_id"):
        assert not result.success
        assert result.error.code == "resume_id_mismatch"
    else:
        assert result.success == value["expected"]["success"]


@pytest.mark.parametrize(
    "status", ["ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING", "unknown", None]
)
def test_only_terminal_success_completes(status):
    assert not parse_result(encode(stream(status=status)), 0).success


def test_partial_stream_keeps_native_id_without_success():
    logs = encode(stream()[:1]) + '{"event":"step_up'
    parsed = parse_result(logs, 0)
    assert parsed.provider_session_id == "synthetic-id"
    assert not parsed.success
    assert not parse_result(encode(stream()) + '{"event":', 0).success
    assert not parse_result(encode(stream()), 1).success


def test_changed_id_and_duplicate_terminal_are_invalid():
    events = stream()
    events[-1]["result"]["conversation_id"] = "different-id"
    assert parse_result(encode(events), 0).error.kind == "invalid_output"
    events = stream()
    assert not parse_result(encode([*events, events[-1]]), 0).success


def test_native_error_prose_is_not_classified_or_leaked():
    events = stream(status="ERROR")
    events[-1]["result"]["error"] = "authentication_failed synthetic-private-path"
    result = parse_result(encode(events), 1)
    assert result.error.category == "unknown"
    assert result.error.code is None
    assert "synthetic-private" not in str(result)


def test_session_totals_never_become_activation_totals():
    first = parse_usage(encode(stream(usage={"input_tokens": 100, "output_tokens": 20})))
    second = parse_usage(encode(stream(usage={"input_tokens": 160, "output_tokens": 30})))
    assert first.session_input_tokens == 100 and second.session_input_tokens == 160
    assert second.input_tokens is None and second.output_tokens is None
    assert second.current_context_tokens is None and second.provider_context_window is None
    assert second.native_compactions is None and second.resolved_model is None
    assert parse_usage(encode(stream())) is None


@pytest.mark.parametrize("bad", [-1, True, "8", None, 1.5])
def test_invalid_usage_stays_unknown(bad):
    usage = parse_usage(encode(stream(usage={"input_tokens": bad, "output_tokens": 8})))
    assert usage.session_input_tokens is None
    assert usage.session_output_tokens == 8


def test_tool_steps_deduplicate_and_do_not_retain_arguments():
    events = stream()
    step = {
        "event": "step_update",
        "step_update": {
            "conversation_id": "synthetic-id",
            "step_type": "tool",
            "step_index": 1,
            "state": "ACTIVE",
            "tool_name": "read_file",
            "arguments": "synthetic-private",
        },
    }
    events[1:1] = [step, step, {**step, "step_update": {**step["step_update"], "state": "DONE"}}]
    assert parse_tool_calls(encode(events)) == {"read_file": 1}
    step["step_update"]["tool_name"] = "call_mcp_tool"
    assert "synthetic-private" not in str(parse_tool_calls(encode(events)))


def test_summary_uses_fresh_session_totals():
    result = parse_summary(encode(stream(usage={"output_tokens": 8})), 0)
    assert result.text == "FIRM_OK" and result.error is None
    assert result.output_tokens == 8
    assert result.usage["output_tokens"] == 8


@pytest.mark.parametrize("mode", ["role", "task", "tool_free"])
def test_fake_launch_spec_has_one_native_turn_and_mode_authority(tmp_path, mode):
    path = account(tmp_path, expiry=4102444800)
    provider = AgyProvider(model="gemini-3.8-flash", effort="medium", auth_file=path)
    if mode == "role":
        spec = provider.build_spec(
            invocation(), "r-example", "run-example", "http://firm.local:8421/mcp", layout=Layout()
        )
        assert spec.secret_env == {"FIRM_ACTIVATION_TOKEN": "activation-token"}
    else:
        request = SummaryLaunch(
            run_id="s-" + uuid4().hex,
            prompt="Synthetic prompt",
            provider="agy",
            timeout=30,
            start_url="http://firm.local:8421/summary/start",
            start_token="synthetic-summary",
            instructions="Synthetic policy",
            tools_enabled=mode == "task",
        )
        build = provider.build_task_spec if mode == "task" else provider.build_summary_spec
        spec = build(request, Layout())
        assert spec.secret_env == {"FIRM_SUMMARY_TOKEN": "summary-token"}
        assert "activation-token" not in spec.secrets
        assert spec.env["FIRM_AGY_MCP_URL"] == ""
    command = json.loads(spec.env["FIRM_AGY_ARGV"])
    assert command[:2] == [f"{Layout().tools}/agy", "--print="]
    assert "gemini-3.8-flash-medium" in command
    assert f"firm-{mode}" in command
    assert spec.env["FIRM_AGY_MODE"] == mode
    assert len(spec.stdin.splitlines()) == 1
    assert json.loads(spec.stdin)["event"] == "user"
    assert "content" in json.loads(spec.stdin)["message"]
    assert b"synthetic-host-refresh" not in spec.secrets["agy-auth"]
    assert b"removed" not in spec.secrets["agy-auth"]
    assert spec.argv == (Layout().python, Layout().credential("agy-launcher"))
    provider.validate_credentials()
    assert provider.parse_result("", 1).error.model == "gemini-3.8-flash-medium"


def test_native_model_id_maps_xhigh_and_rejects_encoded_model():
    assert native_model_id("gemini-4-argon", "high") == "gemini-4-argon-high"
    assert native_model_id("gemini-3.8-flash", "xhigh") == "gemini-4-argon-high"
    for model, effort in [("gemini-4-argon", "max"), ("gemini-3.8-flash-high", "high")]:
        with pytest.raises(ProviderSetupError):
            native_model_id(model, effort)


def test_rejected_gate_cannot_start_native_child(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRM_AGY_ARGV", '["/fake/agy", "--print="]')
    monkeypatch.setenv("FIRM_AGY_MODE", "tool_free")
    monkeypatch.setenv("FIRM_SUMMARY_TOKEN", "synthetic")
    monkeypatch.setenv("FIRM_SUMMARY_START_URL", "http://gate/summary/start")
    monkeypatch.setattr(agy_launch, "_home", lambda: tmp_path)
    monkeypatch.setattr(agy_launch, "_print_timeout", lambda _: [])
    monkeypatch.setattr(agy_launch, "_prepare_home", lambda _: (tmp_path, tmp_path / "auth"))
    monkeypatch.setattr(agy_launch, "_begin_activation", lambda *_: False)
    monkeypatch.setattr(agy_launch, "_run", lambda *_: pytest.fail("inference"))
    assert agy_launch.main() == 70


def test_resume_requires_regular_existing_native_state(tmp_path):
    native = tmp_path / ".gemini/antigravity-cli/conversations"
    native.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        agy_launch._resume_arguments("synthetic-id", tmp_path)
    database = native / "synthetic-id.db"
    database.write_bytes(b"synthetic-state")
    assert agy_launch._resume_arguments("synthetic-id", tmp_path) == [
        "--conversation",
        "synthetic-id",
    ]
    database.unlink()
    database.symlink_to(tmp_path / "other")
    with pytest.raises(ValueError):
        agy_launch._resume_arguments("synthetic-id", tmp_path)


def test_guest_environment_drops_admission_and_cloud_credentials(monkeypatch, tmp_path):
    for key in (
        "FIRM_ACTIVATION_TOKEN",
        "FIRM_SUMMARY_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "NODE_OPTIONS",
        "AGY_ADC_AUTH",
    ):
        monkeypatch.setenv(key, "synthetic-secret")
    env = agy_launch._environment(tmp_path)
    assert "synthetic-secret" not in str(env)
    assert env["HOME"] == str(tmp_path)


def test_preparation_writes_private_native_home_and_named_agent(tmp_path, monkeypatch):
    import os

    state, credentials = tmp_path / "state", tmp_path / "credentials"
    state.mkdir()
    credentials.mkdir()
    (credentials / "agy-auth").write_text('{"synthetic":"access-only"}')
    (credentials / "agy-auth").chmod(0o600)
    (credentials / "agy-instructions").write_text("Synthetic instructions.")
    monkeypatch.setenv("FIRM_RUNTIME_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("FIRM_STATE_DIR", str(state))
    monkeypatch.setenv("FIRM_PRIVATE_HOME", str(tmp_path))
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", str(credentials))
    monkeypatch.setitem(agy_launch.MODE_TOOLS, "tool_free", [])
    previous = os.umask(0o077)
    try:
        home, auth = agy_launch._prepare_home("tool_free")
    finally:
        os.umask(previous)
    assert auth == home / ".gemini/antigravity-cli/antigravity-oauth-token"
    assert auth.stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700
    agent = (home / ".gemini/config/agents/firm-tool_free.md").read_text()
    assert "inheritCustomizations: false" in agent and "subagent: false" in agent
    assert "tools: []" in agent and agent.endswith("Synthetic instructions.")
    settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
    assert settings["useG1Credits"] is False
    assert "manage_task(*)" in settings["permissions"]["deny"]
    previous = os.umask(0o077)
    try:
        assert agy_launch._prepare_home("tool_free")[0] == home
    finally:
        os.umask(previous)


def test_supervision_forwards_signal_kills_stubborn_child_and_reaps(monkeypatch, tmp_path):
    import signal
    import subprocess

    handlers, signals, waits = {}, [], []
    now = [0.0]

    import io

    class Process:
        pid = 12345
        stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                now[0] = 6.0
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return -signal.SIGKILL

    def register(sig, handler):
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(agy_launch.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(agy_launch.signal, "signal", register)
    monkeypatch.setattr(agy_launch.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(agy_launch.time, "monotonic", lambda: now[0])
    assert agy_launch._run(["/synthetic/agy", "--print="], tmp_path) == -signal.SIGKILL
    assert signals[0] == (12345, signal.SIGTERM)
    assert (12345, signal.SIGKILL) in signals
    assert waits[-1] is None
    assert handlers[signal.SIGTERM] == signal.SIG_DFL


def test_native_identity_and_all_cumulative_counters_are_preserved():
    events = stream(
        usage={
            "input_tokens": 6680,
            "output_tokens": 147,
            "thinking_tokens": 122,
            "cache_read_tokens": 30,
            "total_tokens": 6827,
        }
    )
    events[0]["init"]["model"] = "gemini-4-argon-high"
    usage = parse_usage(encode(events))
    assert usage.resolved_model == "gemini-4-argon-high"
    assert usage.session_input_tokens == 6680
    assert usage.session_output_tokens == 147
    assert usage.session_reasoning_output_tokens == 122
    assert usage.session_cached_input_tokens == 30
    assert usage.input_tokens is None and usage.output_tokens is None
    assert parse_usage(encode(events[:1])).resolved_model == "gemini-4-argon-high"


@pytest.mark.parametrize("firm", [False, True])
def test_tool_errors_do_not_fail_activation(firm):
    events = stream()
    name = "mcp_firm_peek" if firm else "manage_task"
    step = {
        "event": "step_update",
        "step_update": {
            "conversation_id": "synthetic-id",
            "step_type": "tool",
            "tool_name": name,
            "step_index": 2,
            "state": "ERROR",
        },
    }
    events[1:1] = [step, step]
    logs = encode(events)
    expected_name = "mcp__firm__peek" if firm else name
    assert parse_tool_calls(logs) == {expected_name: 1}
    assert parse_failed_tool_calls(logs) == {expected_name: 1}
    result = parse_result(logs, 0)
    assert result.success


@pytest.mark.parametrize("mode", ["role", "task", "tool_free"])
def test_mode_permissions_include_explicit_native_denials(mode):
    settings = agy_launch._settings(mode)
    denied = settings["permissions"]["deny"]
    assert "read_url(*)" in denied and "execute_url(*)" in denied
    if mode == "tool_free":
        assert settings["toolPermission"] == "strict"
        assert agy_launch._mode_tools(mode) == []
        for namespace in (
            *agy_launch.NATIVE_INVENTORY, "command", "read_file", "write_file", "mcp",
        ):
            assert f"{namespace}(*)" in denied
    else:
        assert settings["toolPermission"] == "always-proceed"
        assert settings["agentMode"] == "accept-edits"
        assert "run_command" in agy_launch._mode_tools(mode)
        assert "search_web" in agy_launch._mode_tools(mode)
        assert "allow" not in settings["permissions"]
        assert "search_web(*)" not in denied
        assert "read_file(*)" not in denied
    assert ("mcp(*)" in denied) is (mode != "role")


@pytest.mark.parametrize(
    "remaining,limit,expected",
    [
        (300, 1800, None),
        (301, 1800, None),
        (302, 1800, 1),
        (1200, 1800, 899),
        (3600, 1800, 3299),
        (3600, 30, 3299),
    ],
)
def test_print_timeout_ignores_idle_limit_and_respects_token_margin(
    tmp_path, monkeypatch, remaining, limit, expected
):
    from datetime import UTC, datetime

    path = tmp_path / "auth"
    path.write_text(
        json.dumps({"token": {"expiry": datetime.fromtimestamp(1000 + remaining, UTC).isoformat()}})
    )
    monkeypatch.setattr(agy_launch.time, "time", lambda: 1000)
    monkeypatch.setenv("FIRM_AGY_TIMEOUT", str(limit))
    if expected is None:
        with pytest.raises(ValueError, match="headroom"):
            agy_launch._print_timeout(path)
    else:
        assert agy_launch._print_timeout(path) == ["--print-timeout", f"{expected}s"]


@pytest.mark.parametrize("mode", ["role", "task", "tool_free"])
def test_fake_native_launch_persists_session_and_removes_secrets(tmp_path, mode):
    import os
    import subprocess
    import sys

    from test_agy_credentials import fake_agy

    native = fake_agy(
        tmp_path,
        """import json, os, sys
from pathlib import Path
home = Path(os.environ['HOME'])
mode = sys.argv[sys.argv.index('--agent') + 1].removeprefix('firm-')
assert '--effort' not in sys.argv
assert sys.argv[1] == '--print='
assert '--print-timeout' in sys.argv
assert 'FIRM_ACTIVATION_TOKEN' not in os.environ
settings = json.loads((home / '.gemini/antigravity-cli/settings.json').read_text())
assert settings['toolPermission'] == ('strict' if mode == 'tool_free' else 'always-proceed')
if mode == 'tool_free':
    assert 'manage_task(*)' in settings['permissions']['deny']
print('synthetic non-JSON native diagnostic', file=sys.stderr)
mcp = home / '.gemini/config/mcp_config.json'
if mode == 'role':
    server = json.loads(mcp.read_text())['mcpServers']['firm']
    assert server['headers']['Authorization'] == 'Bearer synthetic-admission'
    assert server['tools'] == {'peek': {'eager': True}, 'send': {'eager': True}}
    assert server['timeoutSeconds'] == 600
else:
    assert not mcp.exists()
assert json.loads(sys.stdin.read())['event'] == 'user'
directory = home / '.gemini/antigravity-cli/conversations'
directory.mkdir(exist_ok=True)
identifier = 'synthetic-session'
if '--conversation' in sys.argv:
    identifier = sys.argv[sys.argv.index('--conversation') + 1]
    assert (directory / (identifier + '.db')).is_file()
    if (home / 'mismatch').exists():
        identifier = 'wrong-session'
(directory / (identifier + '.db')).write_bytes(b'synthetic-native-state')
print(json.dumps({'event': 'init', 'conversation_id': identifier,
                  'init': {'model': sys.argv[sys.argv.index('--model') + 1]}}), flush=True)
print(json.dumps({'event': 'result', 'result': {'conversation_id': identifier,
    'status': 'SUCCESS', 'response': 'FIRM_OK'}}), flush=True)
""",
    )
    credential = account(tmp_path, expiry=4102444800)
    adapter = AgyProvider(
        model="gemini-3.8-flash", effort="xhigh", auth_file=credential, executable=str(native)
    )
    private, state, staged = tmp_path / "private", tmp_path / "state", tmp_path / "staged"
    for path in (private, state, staged):
        path.mkdir(mode=0o700)
    # Use the real spec builder and guest launcher; only admission is a local fake.
    spec = adapter._spec(
        role_id="r-example",
        run_id="run-example",
        session_id=str(uuid4()),
        mode=mode,
        layout=Layout(),
        model=adapter.model,
        effort=adapter.effort,
        instructions="Synthetic policy",
        prompt="Synthetic request",
        start_url="http://synthetic/start",
        token="synthetic-admission",
        mcp_url="http://synthetic/mcp",
        mcp_tools=["peek", "send"],
    )
    for name, data in spec.secrets.items():
        target = staged / name
        target.write_bytes(data)
        target.chmod(0o600)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        **spec.env,
        "FIRM_PRIVATE_HOME": str(private),
        "FIRM_STATE_DIR": str(state),
        "FIRM_CREDENTIALS_DIR": str(staged),
    }
    env.pop("FIRM_CREDENTIALS_URL")
    for key, name in spec.secret_env.items():
        env[key] = spec.secrets[name].decode()
    launcher = [
        sys.executable,
        "-c",
        "from firm_runtime import agy_launch; "
        "agy_launch._begin_activation = lambda *_: True; "
        "raise SystemExit(agy_launch.main())",
    ]
    first = subprocess.run(launcher, input=spec.stdin, env=env, capture_output=True, timeout=10)
    assert first.returncode == 0, first.stderr.decode()
    assert parse_result(first.stdout, first.returncode).success
    assert parse_usage(first.stdout).resolved_model == "gemini-4-argon-high"
    home = private / ".firm/agy-sessions" / spec.env["FIRM_RUNTIME_SESSION_ID"]
    assert (home / ".gemini/antigravity-cli/conversations/synthetic-session.db").is_file()
    for relative in (
        ".gemini/antigravity-cli/antigravity-oauth-token",
        ".gemini/config/mcp_config.json",
    ):
        assert not os.path.lexists(home / relative)
    env["FIRM_AGY_NATIVE_ID"] = "synthetic-session"
    resumed = subprocess.run(launcher, input=spec.stdin, env=env, capture_output=True, timeout=10)
    assert parse_result(resumed.stdout, resumed.returncode).success
    (home / "mismatch").touch()
    mismatch = subprocess.run(launcher, input=spec.stdin, env=env, capture_output=True, timeout=10)
    assert mismatch.returncode != 0
    assert parse_result(mismatch.stdout, mismatch.returncode).error.code == "resume_id_mismatch"


@pytest.mark.parametrize("code,category", [
    ("service_unavailable", "transient"), ("authentication_failed", "auth"),
])
def test_shared_mirror_errors_keep_existing_provider_categories(code, category):
    logs = encode([{"type": "error", "error": {"code": code}}])
    result = parse_result(logs, 72)
    assert result.error.code == code and result.error.category == category


@pytest.mark.parametrize("code", [
    "resume_state_missing", "resume_id_mismatch", "token_headroom", "settings_invalid",
    "event_too_large", "output_unavailable",
])
def test_local_launch_errors_never_hold_shared_dependencies(code):
    result = parse_result(encode([{"type": "firm.launch_failed", "kind": "launcher_setup",
                                   "code": code}]), 71)
    assert result.error.code == code
    assert result.error.category not in {"auth", "config", "quota"}


def test_diagnostics_unicode_and_invalid_terminal_retain_known_session():
    events = stream()
    events[-1]["result"]["response"] = "one\u2028two\u2029three\u0085four"
    logs = "native diagnostic\n" + "\n".join(
        json.dumps(e, ensure_ascii=False) for e in events
    ) + "\n"
    assert parse_result(logs, 0).success
    failed = parse_result(logs + '{"event":', 0)
    assert not failed.success and failed.provider_session_id == "synthetic-id"


def test_token_deadline_yields_an_interruption_with_retained_conversation():
    events = stream()
    events[-1]["result"]["finished_reason"] = "print_timeout"
    result = parse_result(encode(events), 0)
    assert not result.success and result.interrupted
    assert result.provider_session_id == "synthetic-id"
    assert result.error.category == "transient"


def test_missing_resume_emits_coded_failure_before_admission(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIRM_AGY_ARGV", '["/fake/agy", "--print="]')
    monkeypatch.setenv("FIRM_AGY_MODE", "role")
    monkeypatch.setenv("FIRM_AGY_NATIVE_ID", "missing-session")
    monkeypatch.setattr(agy_launch, "_home", lambda: tmp_path)
    monkeypatch.setattr(agy_launch, "_prepare_home", lambda _: (tmp_path, tmp_path / "auth"))
    monkeypatch.setattr(agy_launch, "_begin_activation", lambda *_: pytest.fail("admission"))
    assert agy_launch.main() == 71
    result = parse_result(capsys.readouterr().out, 71)
    assert result.error.code == "resume_state_missing"
    assert result.error.category == "unknown"


@pytest.mark.parametrize("code", ["service_unavailable", "authentication_failed"])
def test_launcher_keeps_shared_mirror_error_without_setup_override(
    tmp_path, monkeypatch, capsys, code
):
    class Unavailable(Exception):
        pass

    class Mirror:
        def __init__(self, *_):
            pass

        def __enter__(self):
            print(json.dumps({"type": "error", "error": {"code": code}}))
            raise Unavailable

        def __exit__(self, *_):
            pass

    for key, value in {
        "FIRM_AGY_ARGV": '["/fake/agy", "--print="]',
        "FIRM_AGY_MODE": "task", "FIRM_SUMMARY_TOKEN": "synthetic",
        "FIRM_SUMMARY_START_URL": "http://synthetic/start",
        "FIRM_CREDENTIALS_URL": "http://synthetic/credentials",
        "FIRM_CREDENTIALS_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(agy_launch, "_home", lambda: tmp_path)
    monkeypatch.setattr(agy_launch, "_prepare_home", lambda _: (tmp_path, tmp_path / "auth"))
    monkeypatch.setattr(agy_launch, "_begin_activation", lambda *_: True)
    monkeypatch.setattr(agy_launch.runpy, "run_path", lambda _: {
        "CredentialMirror": Mirror, "MirrorUnavailable": Unavailable,
    })
    monkeypatch.setattr(agy_launch, "_run", lambda *_: pytest.fail("inference"))
    assert agy_launch.main() == 72
    logs = capsys.readouterr().out
    assert "firm.launch_failed" not in logs
    assert parse_result(logs, 72).error.code == code


def test_stale_auth_link_is_removed_before_settings_preparation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini/antigravity-cli").mkdir(parents=True)
    (home / ".gemini/config/agents").mkdir(parents=True)
    auth = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    auth.symlink_to(tmp_path / "old-run-auth")
    monkeypatch.setattr(agy_launch, "_home", lambda: home)
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", str(tmp_path))
    def link(path, _source):
        assert path == auth
        assert not path.is_symlink()
        raise ValueError("stop before staging")
    monkeypatch.setattr(agy_launch, "_link_ephemeral", link)
    with pytest.raises(ValueError, match="before staging"):
        agy_launch._prepare_home("tool_free")


def test_summary_keeps_answer_and_raw_output_with_thinking_usage():
    output = parse_summary(
        encode(stream(usage={"output_tokens": 2294, "thinking_tokens": 2241})), 0
    )
    assert output.text == "FIRM_OK"
    assert output.output_tokens == 2294
    assert output.usage["output_tokens"] == 2294
    assert output.usage["reasoning_output_tokens"] == 2241


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_summary_raw_output_rule_is_shared_with_other_providers(monkeypatch, provider):
    from firm_runtime import claude_provider, codex_provider
    from firm_runtime.provider_contract import ProviderResult, ProviderUsage

    module = codex_provider if provider == "codex" else claude_provider
    adapter = module.CodexProvider if provider == "codex" else module.ClaudeProvider
    monkeypatch.setattr(
        module, "parse_result", lambda *_: ProviderResult("synthetic", True, "ok", None)
    )
    monkeypatch.setattr(module, "parse_usage", lambda *_: ProviderUsage(
        output_tokens=2294, reasoning_output_tokens=2241,
    ))
    output = adapter.parse_summary("", 0)
    assert output.text == "ok"
    assert output.output_tokens == 2294
    assert output.usage["output_tokens"] == 2294
    assert output.usage["reasoning_output_tokens"] == 2241


@pytest.mark.parametrize(("configured", "written"), [("600", 600), ("600.0", 600), ("2.5", 3)])
def test_role_mcp_timeout_is_a_json_integer(monkeypatch, configured, written):
    # AGY 1.5.3 silently drops the Firm MCP server when timeoutSeconds is a float.
    monkeypatch.setenv("FIRM_AGY_MCP_TOOLS", json.dumps(["org_tree"]))
    monkeypatch.setenv("FIRM_AGY_MCP_TIMEOUT", configured)
    monkeypatch.setenv("FIRM_AGY_MCP_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("FIRM_ACTIVATION_TOKEN", "synthetic")
    server = agy_launch._mcp_settings("role")["mcpServers"]["firm"]
    assert server["timeoutSeconds"] == written
    assert type(server["timeoutSeconds"]) is int
    assert f'"timeoutSeconds": {written}' in json.dumps(server)
