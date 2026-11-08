import json
from uuid import uuid4

import pytest
from firm_sandbox import Layout

from firm_runtime import claude_launch
from firm_runtime.claude_provider import (
    ClaudeProvider,
    parse_result,
    parse_subscription,
    parse_tool_calls,
    parse_usage,
)
from firm_runtime.prompt_resources import prompt
from firm_runtime.provider_contract import (
    MAX_RETRY_AFTER_SECONDS,
    ProviderSetupError,
    reset_at,
    retry_after_seconds,
)
from firm_runtime.runtime import Invocation
from firm_runtime.summaries import SummaryLaunch


def invocation(*, context=None, token="activation-secret", session_id=None):
    if context is not None:
        context = {
            "mandate": {"body": "Build the firm.", "author_id": "owner", "revision": 0},
            **context,
        }
    return Invocation(
        activation_id=str(uuid4()),
        employee_id="founder",
        token=token,
        session_id=session_id or str(uuid4()),
        context=context
        or {
            "employee_id": "founder",
            "address": "founder",
            "manager_id": "owner",
            "mandate_revision": 0,
            "mandate": {"revision": 0, "author_id": "owner", "body": "Build the firm."},
            "birth": True,
            "fresh_session": True,
            "workspace_references": {
                "private": "/home/agent", "scratch": "/home/agent/scratch", "workspace": "/work"
            },
            "inbox": {"messages": [], "count": 0},
        },
    )


def provider(tmp_path, *, auth=b"oauth-test-secret"):
    auth_file = tmp_path / "claude-token"
    auth_file.write_bytes(auth)
    auth_file.chmod(0o600)
    return ClaudeProvider(model="claude-opus-4-1", effort="high", auth_file=auth_file)


def test_builds_fresh_spec_with_explicit_settings_and_secret_token(tmp_path):
    call = invocation()
    spec = provider(tmp_path).build_spec(
        call,
        "r-example",
        "run-example",
        "http://firm.local:8421/mcp",
        layout=Layout(),
    )

    assert spec.role_id == "r-example"
    assert spec.run_id == "run-example"
    assert spec.argv == (Layout().python, Layout().credential("claude-launcher"))
    assert spec.secret_env == {
        "FIRM_ACTIVATION_TOKEN": "activation-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-auth",
    }
    assert spec.secrets["claude-auth"] == b"oauth-test-secret"
    assert spec.env["FIRM_ACTIVATION_START_URL"] == ("http://firm.local:8421/activation/start")
    assert spec.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    command = json.loads(spec.env["FIRM_CLAUDE_ARGV"])
    assert command[:2] == [f"{Layout().tools}/claude", "-p"]
    assert command[command.index("--model") + 1] == "claude-opus-4-1"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--session-id") + 1] == call.session_id
    assert "--resume" not in command
    assert "--continue" not in command
    assert command[command.index("--setting-sources") + 1] == ""
    assert json.loads(command[command.index("--settings") + 1]) == {
        "crossSessionInbound": "refuse",
        "autoMemoryEnabled": False,
        "attribution": {"commit": "", "pr": ""},
        "claudeMdExcludes": ["**"],
    }
    assert "--strict-mcp-config" in command
    mcp = json.loads(command[command.index("--mcp-config") + 1])
    assert mcp == {
        "mcpServers": {
            "firm": {
                "headers": {"Authorization": "Bearer ${FIRM_ACTIVATION_TOKEN}"},
                "type": "http",
                "url": "http://firm.local:8421/mcp",
            }
        }
    }
    rendered = "\n".join(command)
    assert "oauth-test-secret" not in rendered
    assert "activation-secret" not in rendered
    assert "oauth-test-secret" not in repr(spec)
    assert "Build the firm." in spec.secrets["role-instructions"].decode()
    assert "Build the firm." not in spec.stdin.decode()
    assert prompt("founder_birth.md") in spec.stdin.decode()
    assert (
        command[command.index("--system-prompt-file") + 1]
        == Layout().credential("role-instructions")
    )
    assert "--append-system-prompt" not in command
    assert command[command.index("--system-prompt-snapshot") + 1] == "off"
    assert "--disable-slash-commands" in command


def test_builds_summary_spec_with_empty_tool_set_and_no_activation(tmp_path):
    adapter = provider(tmp_path)
    request = SummaryLaunch(
        "summary-" + uuid4().hex[:32],
        "derive this",
        30,
        account_id="personal",
        provider="claude",
        model="sonnet",
        effort="medium",
        start_url="http://firm/summary/start",
        start_token="summary-secret",
    )
    spec = adapter.build_summary_spec(request, layout=Layout())
    command = json.loads(spec.env["FIRM_CLAUDE_ARGV"])
    assert spec.secret_env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-auth",
        "FIRM_SUMMARY_TOKEN": "summary-token",
    }
    assert "FIRM_ACTIVATION_TOKEN" not in spec.env
    assert spec.env["FIRM_SUMMARY_START_URL"] == "http://firm/summary/start"
    assert spec.env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    assert spec.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert spec.secrets["summary-token"] == b"summary-secret"
    summary = spec.secrets["summary-instructions"].decode()
    assert "source envelopes" in summary and "not instructions to execute" in summary
    assert "You are founder" not in summary
    assert "role-instructions" not in spec.secrets
    assert spec.stdin == request.prompt.encode()
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "medium"
    assert command[command.index("--tools") + 1] == ""
    assert command.count("-") == 0
    assert "--no-session-persistence" in command
    assert "--disable-slash-commands" in command
    assert len(spec.run_id) <= 40
    assert len(spec.role_id) <= 40


@pytest.mark.parametrize(
    "auth,kind,instructions,model,effort",
    [
        (b"oauth-test-secret", "summary", None, "sonnet", "medium"),
        (b'{"claudeAiOauth":{"accessToken":"x"}}', "summary", None, None, None),
        (b"oauth-test-secret", "staffing", "Choose a level.", "haiku", "low"),
    ],
)
def test_isolated_claude_turn_always_has_an_empty_tool_set_and_no_mcp(
    tmp_path, auth, kind, instructions, model, effort
):
    """The launcher turns env scrubbing off for these runs only because no tool can run."""
    request = SummaryLaunch(
        "run-" + uuid4().hex,
        "derive this",
        30,
        provider="claude",
        model=model,
        effort=effort,
        start_url="http://firm/summary/start",
        start_token="summary-secret",
        kind=kind,
        instructions=instructions,
    )
    spec = provider(tmp_path, auth=auth).build_summary_spec(request, layout=Layout())
    command = json.loads(spec.env["FIRM_CLAUDE_ARGV"])
    assert command.count("--tools") == 1
    assert command[command.index("--tools") + 1] == ""
    assert not any(arg in command for arg in ("--mcp-config", "--allowedTools", "--allowed-tools"))
    assert "--strict-mcp-config" in command
    assert spec.env["FIRM_SUMMARY_RUN"] == "1"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in spec.env


def test_summary_launcher_refuses_missing_or_denied_gate(monkeypatch):
    assert claude_launch._begin_summary("", "token") is False
    assert claude_launch._begin_summary("http://firm/summary/start", "") is False
    monkeypatch.setattr(claude_launch, "_prepare_home", lambda _session: None)
    monkeypatch.setattr(claude_launch, "_prepare_auth", lambda _kind: None)
    monkeypatch.setattr(claude_launch, "_begin_summary", lambda _url, _token: False)
    launched = []
    monkeypatch.setattr(claude_launch.os, "execvpe", lambda *_args: launched.append(True))
    monkeypatch.setenv("FIRM_CLAUDE_ARGV", json.dumps(["claude", "-p"]))
    monkeypatch.setenv("FIRM_CLAUDE_AUTH_KIND", "oauth-token")
    monkeypatch.setenv("FIRM_RUNTIME_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("FIRM_SUMMARY_RUN", "1")
    assert claude_launch.main() == 70
    assert launched == []


@pytest.mark.parametrize("summary", [False, True])
def test_launcher_selects_isolation_after_authenticated_gate(monkeypatch, summary):
    class ExecReached(Exception):
        pass

    calls = []
    monkeypatch.setattr(claude_launch, "_prepare_home", lambda _session: None)
    monkeypatch.setattr(claude_launch, "_prepare_auth", lambda _kind: None)
    monkeypatch.setattr(
        claude_launch, "_begin_activation", lambda url, token: calls.append((url, token)) or True
    )

    def execute(_file, _argv, environment):
        assert calls == [("http://firm/start", "gate-secret")]
        # Neither run may require bubblewrap, which the Linux VM does not provide.
        assert environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "0"
        assert environment["CLAUDE_CONFIG_DIR"] == "/run/firm/claude"
        assert "ANTHROPIC_API_KEY" not in environment
        assert "ANTHROPIC_AUTH_TOKEN" not in environment
        assert "ANTHROPIC_BASE_URL" not in environment
        raise ExecReached

    monkeypatch.setattr(claude_launch.os, "execvpe", execute)
    monkeypatch.setenv("FIRM_CLAUDE_ARGV", json.dumps(["claude", "-p"]))
    monkeypatch.setenv("FIRM_STATE_DIR", "/run/firm")
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", "/run/firm/credentials")
    monkeypatch.setenv("FIRM_PRIVATE_HOME", "/home/agent")
    monkeypatch.setenv("FIRM_CLAUDE_AUTH_KIND", "credentials-json")
    monkeypatch.setenv("FIRM_RUNTIME_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("FIRM_SUMMARY_RUN", "1" if summary else "0")
    monkeypatch.setenv("FIRM_SUMMARY_TOKEN", "gate-secret")
    monkeypatch.setenv("FIRM_SUMMARY_START_URL", "http://firm/start")
    monkeypatch.setenv("FIRM_ACTIVATION_TOKEN", "gate-secret")
    monkeypatch.setenv("FIRM_ACTIVATION_START_URL", "http://firm/start")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(name, "unselected-fixture-value")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "unselected-fixture-directory")
    monkeypatch.setenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "inherited-fixture-value")
    with pytest.raises(ExecReached):
        claude_launch.main()


def test_builds_exact_resume_and_rejects_noncanonical_native_id(tmp_path):
    call = invocation(
        context={
            "employee_id": "founder",
            "address": "founder",
            "manager_id": "owner",
            "birth": False,
            "fresh_session": False,
            "workspace_references": {
                "private": "/home/agent", "scratch": "/home/agent/scratch", "workspace": "/work"
            },
            "inbox": {"messages": []},
        }
    )
    native_id = str(uuid4())
    spec = provider(tmp_path).build_spec(
        call,
        "r-example",
        "run-example",
        "https://bridge.example/mcp",
        native_id,
        layout=Layout(),
    )
    command = json.loads(spec.env["FIRM_CLAUDE_ARGV"])
    assert command[command.index("--resume") + 1] == native_id
    assert "--session-id" not in command
    assert "--continue" not in command

    with pytest.raises(ProviderSetupError, match="must be a UUID") as caught:
        provider(tmp_path).build_spec(
            call,
            "r-example",
            "run-example",
            "https://bridge.example/mcp",
            "../../token",
            layout=Layout(),
        )
    assert caught.value.category == "config"
    assert caught.value.code == "invalid_invocation"


def test_auth_file_is_private_explicit_and_read_at_build(tmp_path):
    auth_file = tmp_path / "not-created-yet"
    adapter = ClaudeProvider(model="claude-sonnet-4-5", effort="medium", auth_file=auth_file)
    with pytest.raises(ProviderSetupError) as missing:
        adapter.build_spec(
            invocation(), "r-example", "run-example", "http://firm/mcp", layout=Layout()
        )
    assert (missing.value.category, missing.value.code) == ("auth", "credential_unreadable")

    auth_file.write_text("first\nsecond")
    auth_file.chmod(0o600)
    with pytest.raises(ProviderSetupError) as multiline:
        adapter.build_spec(
            invocation(), "r-example", "run-example", "http://firm/mcp", layout=Layout()
        )
    assert (multiline.value.category, multiline.value.code) == (
        "auth",
        "credential_invalid",
    )

    auth_file.write_text("secret")
    auth_file.chmod(0o644)
    with pytest.raises(ProviderSetupError) as public:
        adapter.build_spec(
            invocation(), "r-example", "run-example", "http://firm/mcp", layout=Layout()
        )
    assert (public.value.category, public.value.code) == (
        "auth",
        "credential_permissions",
    )


def test_exported_credentials_json_is_copied_to_tmpfs_not_put_in_environment(tmp_path):
    adapter = provider(tmp_path, auth=b'{"claudeAiOauth":{"accessToken":"secret"}}')
    spec = adapter.build_spec(
        invocation(), "r-example", "run-example", "http://firm.local/mcp", layout=Layout()
    )
    assert spec.env["FIRM_CLAUDE_AUTH_KIND"] == "credentials-json"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spec.secret_env
    assert spec.secrets["claude-auth"].startswith(b"{")

    token_spec = provider(tmp_path).build_spec(
        invocation(), "r-example", "run-example", "http://firm.local/mcp", layout=Layout()
    )
    assert token_spec.env["FIRM_CLAUDE_AUTH_KIND"] == "oauth-token"
    assert token_spec.secret_env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-auth"


def test_launcher_keeps_only_projects_tree_in_persistent_home(tmp_path, monkeypatch):
    runtime_session_id = str(uuid4())
    claude_home = tmp_path / "run" / "firm" / "claude"
    claude_home.parent.mkdir(parents=True)
    role_home = tmp_path / "home" / "agent"
    role_home.mkdir(parents=True)
    monkeypatch.setenv("FIRM_STATE_DIR", str(claude_home.parent))
    monkeypatch.setenv("FIRM_PRIVATE_HOME", str(role_home))

    claude_launch._prepare_home(runtime_session_id)

    assert (claude_home / "projects").is_symlink()
    persistent = role_home / ".firm" / "claude-sessions" / runtime_session_id
    assert (claude_home / "projects").resolve() == (persistent / "projects").resolve()
    assert list(persistent.iterdir()) == [persistent / "projects"]
    assert not (persistent / ".credentials.json").exists()


@pytest.mark.parametrize("task", [False, True])
def test_launcher_keeps_exported_credentials_on_tmpfs(tmp_path, monkeypatch, task):
    claude_home = tmp_path / "run" / "firm" / "claude"
    claude_home.mkdir(parents=True)
    source = tmp_path / "run" / "firm" / "credentials" / "claude-auth"
    source.parent.mkdir()
    source.write_bytes(b'{"claudeAiOauth":{"accessToken":"secret"}}')
    monkeypatch.setenv("FIRM_STATE_DIR", str(claude_home.parent))
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", str(source.parent))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")
    monkeypatch.setenv("FIRM_TASK_RUN", "1" if task else "0")

    claude_launch._prepare_auth("credentials-json")

    copied = claude_home / ".credentials.json"
    assert copied.read_bytes() == source.read_bytes()
    assert copied.is_symlink() is task
    if task:
        assert copied.resolve() == source.resolve()
    assert copied.stat().st_mode & 0o077 == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in claude_launch.os.environ


def test_launcher_posts_empty_authenticated_gate_request(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def open_request(request, timeout):
        seen.update(
            url=request.full_url,
            data=request.data,
            authorization=request.get_header("Authorization"),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(claude_launch.urllib.request, "urlopen", open_request)
    assert claude_launch._begin_activation(
        "http://firm.local/activation/start", "activation-secret"
    )
    assert seen == {
        "url": "http://firm.local/activation/start",
        "data": b"",
        "authorization": "Bearer activation-secret",
        "timeout": 10,
    }


def test_parse_result_extracts_exact_session_and_terminal_text():
    native_id = str(uuid4())
    result = parse_result(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "session_id": native_id}),
                json.dumps({"type": "assistant", "message": {"content": "untrusted"}}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": native_id,
                        "result": "Done",
                    }
                ),
            ]
        ),
        0,
    )
    assert result.success
    assert result.provider_session_id == native_id
    assert result.final_text == "Done"
    assert result.error is None


@pytest.mark.parametrize("reset", [None, "2026-09-10T19:00:00Z"])
def test_cli_terminal_session_limit_pauses_quota_without_guessing_reset(reset):
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "errors": None,
        "session_id": str(uuid4()),
        "result": "You've hit your session limit · resets 7pm (UTC)",
    }
    if reset:
        event["reset_at"] = reset
    result = parse_result(json.dumps(event), 0)
    assert not result.success
    assert result.final_text is None
    assert result.error.category == "quota"
    assert result.error.code == "usage_limit_reached"
    assert result.error.retry_after_seconds is None
    # A clock time without a date is not a reliable reset timestamp.
    assert result.error.reset_at == (1789066800.0 if reset else None)
    assert "7pm" not in repr(result)


def test_terminal_reset_text_fallback_requires_an_absolute_timestamp():
    result = parse_result(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "You've hit your session limit · resets 2026-09-10T19:00:00Z",
            }
        ),
        1,
    )
    assert result.error.reset_at == 1789066800


def test_session_limit_words_in_normal_assistant_output_do_not_pause_account():
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": str(uuid4()),
        "result": "You've hit your session limit · resets 7pm (UTC)",
    }
    result = parse_result(json.dumps(event), 0)
    assert result.success and result.error is None
    event["is_error"] = True
    event["result"] = "The task discusses: You've hit your session limit · resets 7pm (UTC)"
    assert parse_result(json.dumps(event), 1).error.category == "unknown"


def test_explicit_native_error_takes_precedence_over_terminal_limit_text():
    result = parse_result(
        "\n".join(
            [
                json.dumps({"type": "error", "error": {"type": "authentication_error"}}),
                json.dumps(
                    {
                        "type": "result",
                        "is_error": True,
                        "result": "You've hit your session limit · resets 7pm (UTC)",
                    }
                ),
            ]
        ),
        1,
    )
    assert result.error.category == "auth"
    assert result.error.code == "authentication_error"


def test_parse_result_keeps_only_structured_failure_evidence():
    native_id = str(uuid4())
    sensitive = "prompt and bearer activation-secret"
    result = parse_result(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "session_id": native_id,
                "result": sensitive,
                "error": {
                    "type": "rate_limit_error",
                    "message": sensitive,
                    "retry_after_seconds": 90,
                    "reset_at": "2026-09-10T00:00:00Z",
                },
            }
        ),
        1,
    )
    assert not result.success
    assert result.final_text is None
    assert result.error.kind == "result_error"
    assert result.error.category == "quota"
    assert result.error.code == "rate_limit_error"
    assert result.error.retry_after_seconds == 90
    assert result.error.reset_at == 1788998400.0
    assert sensitive not in repr(result)

    unknown = parse_result(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "session_id": native_id,
                "result": "authentication failed in arbitrary prose",
            }
        ),
        1,
    )
    assert unknown.error.category == "unknown"
    assert unknown.error.code is None

    preceding = parse_result(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "error",
                        "session_id": native_id,
                        "error": {"type": "authentication_error", "message": sensitive},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "is_error": True,
                        "session_id": native_id,
                        "result": sensitive,
                    }
                ),
            ]
        ),
        1,
    )
    assert preceding.error.category == "auth"
    assert preceding.error.code == "authentication_error"
    assert sensitive not in repr(preceding)

    no_session = parse_result(
        json.dumps(
            {
                "type": "error",
                "error": {"type": "authentication_error", "message": sensitive},
            }
        ),
        1,
    )
    assert no_session.provider_session_id is None
    assert no_session.error.kind == "turn_failed"
    assert no_session.error.category == "auth"
    assert sensitive not in repr(no_session)


def test_parse_result_preserves_activation_gate_kind_and_validates_session():
    gate = parse_result(
        json.dumps({"type": "firm.launch_failed", "kind": "activation_gate", "code": "http_401"}),
        70,
    )
    assert gate.error.kind == "activation_gate"
    assert gate.error.category == "unknown"

    malformed = parse_result(
        "\n".join(
            [
                json.dumps({"type": "system", "session_id": "../../token"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "Done",
                    }
                ),
            ]
        ),
        0,
    )
    assert malformed.provider_session_id is None
    assert malformed.error.kind == "invalid_output"


@pytest.mark.parametrize("invalid", [None, "", "  ", 42, {}, []])
def test_usage_keeps_last_reported_model_even_without_counters(invalid):
    events = [
        {"type": "system", "model": "not-execution-evidence"},
        {"type": "assistant", "message": {"model": "claude-opus-4-1"}},
        {"type": "assistant", "message": {"model": invalid}},
    ]
    logs = "\n".join(json.dumps(e) for e in events)
    assert parse_usage(logs).resolved_model == "claude-opus-4-1"
    logs += '\n' + json.dumps({"type": "result", "model": "claude-opus-4-6"})
    assert parse_usage(logs.encode()).resolved_model == "claude-opus-4-6"
    logs += '\n' + json.dumps({"type": "result", "model": invalid})
    assert parse_usage(logs).resolved_model == "claude-opus-4-6"
    assert parse_usage(logs).input_tokens is None
    unknown = parse_usage(json.dumps({"type": "result", "usage": {"input_tokens": 10}}))
    assert unknown.resolved_model is None


def test_rejected_model_is_a_config_failure_naming_the_configured_model(tmp_path):
    """A renamed/retired/typo'd model is Anthropic's 404 not_found_error.

    It must not be treated as transient (retrying a bad model wastes tokens on a
    guaranteed repeat failure), and the adapter instance - the only thing that
    knows which model it was configured with - must attach that model to the
    failure so it doesn't surface as a generic provider error.
    """
    adapter = provider(tmp_path)
    logs = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "session_id": str(uuid4()),
            "result": "model: claude-opus-4-1 not found",
            "error": {"type": "not_found_error", "message": "model: claude-opus-4-1 not found"},
        }
    )
    result = adapter.parse_result(logs, 1)
    assert not result.success
    assert result.error.category == "config"
    assert result.error.code == "not_found_error"
    assert result.error.model == "claude-opus-4-1"

    # The bare module-level parser has no adapter instance and cannot know the
    # model; only the instance method (what the dispatcher actually calls) does.
    assert parse_result(logs, 1).error.model is None


def test_structured_retry_boundaries_reject_negative_and_nonpositive_timestamps():
    assert retry_after_seconds({"retry_after_ms": -1}) is None
    assert retry_after_seconds({"retry_after_ms": MAX_RETRY_AFTER_SECONDS * 1000}) == (
        MAX_RETRY_AFTER_SECONDS
    )
    assert retry_after_seconds({"retry_after_ms": MAX_RETRY_AFTER_SECONDS * 1000 + 1}) is None
    assert reset_at({"reset_at": -1}) is None
    assert reset_at({"reset_at": 0}) is None
    assert reset_at({"reset_at": "1970-01-01T00:00:00Z"}) is None
    assert reset_at({"resetsAt": 180}) == 180


def test_tool_calls_are_counted_by_name_without_their_arguments():
    logs = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "id": "one",
                    "content": [
                        {"type": "text", "text": "Checking the build."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "rm -rf /secret"},
                        },
                    ],
                },
            },
            # Claude repeats a request's events; the same block must not count twice.
            {
                "type": "assistant",
                "message": {
                    "id": "one",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "rm -rf /secret"},
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "two",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "mcp__firm__send",
                            "input": {"body": "confidential draft"},
                        },
                        {"type": "tool_use", "id": "toolu_3", "name": "Bash", "input": {}},
                        {"type": "tool_use", "id": "toolu_4", "name": ["not a name"]},
                    ],
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "result", "usage": {"output_tokens": 3}},
        )
    )
    tally = parse_tool_calls(logs)
    assert tally == {"Bash": 2, "mcp__firm__send": 1, "other": 1}
    assert parse_tool_calls(logs.encode()) == tally
    assert parse_tool_calls("not json\n") == {}
    with pytest.raises(TypeError):
        parse_tool_calls(None)


def test_stream_usage_is_live_request_context_and_activation_total_with_compaction():
    logs = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "assistant",
                "message": {
                    "id": "one",
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 1_000,
                        "cache_read_input_tokens": 8_000,
                        "output_tokens": 50,
                    },
                },
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"pre_tokens": 9_150, "post_tokens": 900},
            },
            {
                "type": "assistant",
                "message": {
                    "id": "two",
                    "usage": {
                        "input_tokens": 200,
                        "cache_read_input_tokens": 2_000,
                        "output_tokens": 75,
                    },
                },
            },
        )
    )
    observed = parse_usage(logs)
    assert observed.current_context_tokens == 2_275
    assert observed.high_water_context_tokens == 9_150
    assert observed.input_tokens == 11_300
    assert observed.cached_input_tokens == 10_000
    assert observed.output_tokens == 125
    assert observed.native_compactions == 1
    assert observed.session_input_tokens is None


def test_terminal_cumulative_usage_does_not_claim_current_context():
    observed = parse_usage(
        json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 120_000,
                    "cache_read_input_tokens": 80_000,
                    "output_tokens": 4_000,
                },
            }
        )
    )
    assert observed.current_context_tokens is None
    assert observed.high_water_context_tokens is None
    assert observed.input_tokens == 200_000
    assert observed.output_tokens == 4_000


def test_terminal_usage_wins_for_accounting_and_iterations_supply_context():
    logs = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "id": "message",
                    "stop_reason": None,
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 6_002,
                        "cache_read_input_tokens": 3_289,
                        "output_tokens": 1,
                        "output_tokens_details": {"thinking_tokens": 3},
                    },
                },
            },
            {
                "type": "result",
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 6_002,
                    "cache_read_input_tokens": 3_289,
                    "output_tokens": 155,
                    "output_tokens_details": {"thinking_tokens": 9},
                    "iterations": [
                        {
                            "input_tokens": 2,
                            "cache_creation_input_tokens": 6_002,
                            "cache_read_input_tokens": 3_289,
                            "output_tokens": 155,
                        }
                    ],
                },
                "modelUsage": {
                    "claude-sonnet-5": {"contextWindow": 1_000_000},
                },
            },
        )
    )

    observed = parse_usage(logs)

    assert observed.current_context_tokens == 9_448
    assert observed.high_water_context_tokens == 9_448
    assert observed.provider_context_window == 1_000_000
    assert observed.input_tokens == 9_293
    assert observed.cached_input_tokens == 3_289
    assert observed.output_tokens == 155
    assert observed.reasoning_output_tokens == 9


def test_provisional_assistant_usage_reports_input_side_as_current_context():
    observed = parse_usage(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "provisional",
                    "stop_reason": None,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "output_tokens": 1,
                    },
                },
            }
        )
    )

    assert observed.current_context_tokens == 60
    assert observed.high_water_context_tokens == 60
    assert observed.input_tokens == 60
    assert observed.output_tokens == 1


def test_terminal_iterations_do_not_cross_a_compaction_boundary():
    logs = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"pre_tokens": 12, "post_tokens": 4},
            },
            {
                "type": "result",
                "usage": {
                    "output_tokens": 3,
                    "iterations": [{"input_tokens": 30, "output_tokens": 5}],
                },
            },
        )
    )

    observed = parse_usage(logs)

    assert observed.current_context_tokens == 4
    assert observed.high_water_context_tokens == 35


def test_terminal_usage_fills_only_missing_request_totals():
    logs = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "output_tokens": 2,
                        }
                    },
                }
            ),
            json.dumps({"type": "result", "usage": {"output_tokens": 3}}),
        )
    )

    observed = parse_usage(logs)

    assert observed.input_tokens == 30
    assert observed.cached_input_tokens == 20
    assert observed.output_tokens == 3


def test_compaction_without_post_count_clears_current_until_next_request():
    logs = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "before",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"pre_tokens": 200},
                }
            ),
        )
    )
    observed = parse_usage(logs)
    assert observed.current_context_tokens is None
    assert observed.high_water_context_tokens == 200
    assert observed.native_compactions == 1


def test_compaction_without_metadata_clears_current_until_next_request():
    logs = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "before",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                }
            ),
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
        )
    )
    observed = parse_usage(logs)
    assert observed.current_context_tokens is None
    assert observed.high_water_context_tokens == 110
    assert observed.native_compactions == 1


@pytest.mark.parametrize("terminal", [True, False])
def test_claude_assistant_auth_failure_is_classified_without_retaining_text(terminal):
    events = [
        {
            "type": "assistant",
            "error": "authentication_failed",
            "message": {"content": [{"type": "text", "text": "private credential detail"}]},
        }
    ]
    if terminal:
        events.append({"type": "result", "is_error": True, "result": "private detail"})
    result = parse_result("\n".join(json.dumps(e) for e in events), 1)
    assert not result.success
    assert result.error.category == "auth"
    assert result.error.code == "authentication_failed"
    assert "private" not in repr(result)


def test_authentication_words_in_assistant_content_do_not_classify_failure():
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "authentication_failed"}]},
        },
        {"type": "result", "is_error": True},
    ]
    assert parse_result("\n".join(json.dumps(e) for e in events), 1).error.category == "unknown"


def test_latest_streamed_rate_limit_windows_become_percentages():
    def event(uuid, five_hour, seven_day):
        return json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed",
                    "resetsAt": 1789621200,
                    "rateLimitType": "five_hour",
                    "unifiedWindows": {
                        "five_hour": {"utilization": five_hour, "resetsAt": 1789621200},
                        "seven_day": {"utilization": seven_day, "resetsAt": 1789858800},
                    },
                },
                "uuid": uuid,
                "session_id": "2acbddf8-813f-4d90-899c-2c719d7e18d3",
            }
        )

    logs = "\n".join((event("old", 0.01, 0.5), event("new", 0.04, 0.54), "not json"))
    assert parse_subscription(logs.encode()) == {
        "event_id": "new",
        "windows": [
            {"window": "five_hour", "used_percent": 4.0, "resets_at": 1789621200},
            {"window": "seven_day", "used_percent": 54.0, "resets_at": 1789858800},
        ],
        "status": "allowed",
        "warning": None,
    }
    malformed = parse_subscription(event("bad", 4, True))
    assert malformed is not None and malformed["windows"] == []
    assert parse_subscription(json.dumps({"type": "result"})) is None


def test_malformed_rate_limit_type_and_status_are_ignored_safely():
    result = parse_subscription(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": [],
                    "rateLimitType": [],
                    "resetsAt": 2000,
                    "unifiedWindows": {},
                },
            }
        )
    )
    assert result is not None and result["windows"] == [] and result["warning"] is None


def test_newer_empty_subscription_event_supersedes_older_windows():
    old = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.8, "resetsAt": 2000}
                },
            },
        }
    )
    new = json.dumps(
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    )
    assert parse_subscription("\n".join([old, new]))["windows"] == []


def test_subscription_percentage_is_not_rounded_up_before_admission():
    reading = parse_subscription(json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {"five_hour": {"utilization": 0.7996, "resetsAt": 5000}},
        },
    }))
    assert reading["windows"][0]["used_percent"] == pytest.approx(79.96)
    assert reading["windows"][0]["used_percent"] < 80


def test_rejected_subscription_window_supplies_only_its_structured_reset():
    def event(info):
        return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})

    rejected = event(
        {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": 1800,
            "unifiedWindows": {
                "five_hour": {"utilization": 1, "resetsAt": 1800},
                "seven_day": {"utilization": 0.6, "resetsAt": 3000},
            },
        }
    )
    result = parse_result(
        "\n".join(
            [
                rejected,
                json.dumps(
                    {
                        "type": "result",
                        "is_error": True,
                        "result": "You've hit your session limit",
                    }
                ),
            ]
        ),
        1,
    )
    assert result.error.category == "quota"
    assert result.error.reset_at == 1800


def test_allowed_subscription_window_cannot_supply_refusal_reset():
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed",
                        "rateLimitType": "seven_day",
                        "resetsAt": 3000,
                        "unifiedWindows": {
                            "five_hour": {"utilization": 1, "resetsAt": 1800}
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "You've hit your session limit",
                }
            ),
        ]
    )
    assert parse_result(logs, 1).error.reset_at is None


@pytest.mark.parametrize("base_reset,overage_reset", [(1800, 2700), (None, 2700), (1800, None)])
def test_all_rejected_buckets_need_resets_before_automatic_recovery(base_reset, overage_reset):
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": base_reset,
                        "overageStatus": "rejected",
                        "overageResetsAt": overage_reset,
                    },
                }
            ),
            json.dumps(
                {"type": "result", "is_error": True, "result": "You've hit your session limit"}
            ),
        ]
    )
    expected = 2700 if base_reset is not None and overage_reset is not None else None
    assert parse_result(logs, 1).error.reset_at == expected


@pytest.mark.parametrize(
    "latest_info", [None, {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 3000}]
)
def test_only_latest_subscription_status_can_time_a_terminal_subscription_refusal(latest_info):
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": 1800,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": latest_info,
                }
            ),
            json.dumps(
                {"type": "result", "is_error": True, "result": "You've hit your session limit"}
            ),
        ]
    )
    assert parse_result(logs, 1).error.reset_at is None


def test_multiple_still_rejected_subscription_buckets_use_the_latest_reset():
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": 1800,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "seven_day",
                        "resetsAt": 3000,
                    },
                }
            ),
            json.dumps(
                {"type": "result", "is_error": True, "result": "You've hit your session limit"}
            ),
        ]
    )
    assert parse_result(logs, 1).error.reset_at == 3000


def test_cli_subscription_limit_text_supplies_context_for_native_quota_error():
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": 1800,
                    },
                }
            ),
            json.dumps(
                {"type": "assistant", "error": "rate_limit", "message": {"content": []}}
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "You've hit your session limit · resets 7pm (UTC)",
                }
            ),
        ]
    )
    result = parse_result(logs, 1)
    assert result.error.category == "quota"
    assert result.error.reset_at == 1800


def test_subscription_reset_does_not_time_an_api_rate_limit_error():
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": 1800,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "API limit"},
                }
            ),
        ]
    )
    assert parse_result(logs, 1).error.reset_at is None


def test_allowed_warning_identifies_window_from_provider_status():
    result = parse_subscription(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "rateLimitType": "seven_day_opus",
                    "resetsAt": 5000,
                    "unifiedWindows": {
                        "seven_day_opus": {"utilization": 0.8, "resetsAt": 5000}
                    },
                },
            }
        )
    )
    assert result["warning"] == {
        "window": "seven_day_opus",
        "resets_at": 5000,
        "status": "allowed_warning",
    }
