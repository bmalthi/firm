import json
import os
from uuid import uuid4

import pytest
from firm_sandbox import Layout

from firm_runtime import codex_launch
from firm_runtime.codex_provider import (
    CodexProvider,
    parse_result,
    parse_subscription,
    parse_tool_calls,
    parse_usage,
)
from firm_runtime.prompt_resources import prompt as resource_prompt
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.provider_support import standing
from firm_runtime.runtime import Invocation
from firm_runtime.summaries import SummaryLaunch


@pytest.fixture(autouse=True)
def restore_launcher_umask():
    # The guest launcher intentionally changes its process-wide mask. Calls
    # made in this test process must not change later fixtures in other modules.
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


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
                "private": "/home/agent", "scratch": "/home/agent/scratch",
                "workspace": "/work",
                "shared": "/shared",
                "drive": "/drive",
                "organization": "/org",
            },
            "execution_profiles": ["codex-heavy", "claude-heavy"],
            "inbox": {"messages": [], "count": 0},
        },
    )


def provider(tmp_path, *, auth=b'{"tokens":{"access_token":"host-secret"}}'):
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_bytes(auth)
    auth_file.chmod(0o600)
    return CodexProvider(model="gpt-6-astra", effort="high", auth_file=auth_file)


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra"])
def test_builds_fresh_sandbox_spec_without_exposing_credentials(tmp_path, model):
    adapter = provider(tmp_path)
    adapter = CodexProvider(model=model, effort="high", auth_file=adapter.auth_file)
    call = invocation()
    spec = adapter.build_spec(
        call,
        "r-example",
        "run-example",
        "http://firm.local:8421/mcp",
        layout=Layout(),
    )

    assert spec.role_id == "r-example"
    assert spec.run_id == "run-example"
    assert spec.argv == (Layout().python, Layout().credential("codex-launcher"))
    assert spec.secret_env == {"FIRM_ACTIVATION_TOKEN": "activation-token"}
    assert spec.secrets["activation-token"] == b"activation-secret"
    assert b"host-secret" in spec.secrets["codex-auth"]
    assert spec.env["FIRM_ACTIVATION_START_URL"] == ("http://firm.local:8421/activation/start")
    command = json.loads(spec.env["FIRM_CODEX_ARGV"])
    assert command[:3] == [f"{Layout().tools}/codex", "--model", model]
    assert "--cd" not in command
    assert "exec" in command
    assert "resume" not in command
    assert "--last" not in command
    assert "--ephemeral" not in command
    rendered = "\n".join(command)
    assert 'mcp_servers.firm.url="http://firm.local:8421/mcp"' in rendered
    assert 'mcp_servers.firm.bearer_token_env_var="FIRM_ACTIVATION_TOKEN"' in rendered
    assert "mcp_servers.firm.required=true" in rendered
    assert 'model_reasoning_effort="high"' in rendered
    assert "activation-secret" not in rendered
    assert "host-secret" not in rendered
    assert "activation-secret" not in repr(spec)
    prompt = spec.stdin.decode()
    instructions = spec.secrets["role-instructions"].decode()
    assert instructions == standing(call.context)
    assert "Build the firm." in instructions and "Build the firm." not in prompt
    assert resource_prompt("founder_birth.md") in prompt
    assert "readable only by your reporting line" in instructions
    assert "claude-heavy" not in prompt + instructions
    assert f'model_instructions_file="{Layout().credential("role-instructions")}"' in command
    assert "developer_instructions" not in rendered
    assert "include_collaboration_mode_instructions=false" in command
    assert call.activation_id not in prompt
    assert call.session_id not in prompt
    assert call.token not in prompt


def test_builds_summary_spec_without_role_authority_or_tools(tmp_path):
    adapter = provider(tmp_path)
    request = SummaryLaunch(
        "summary-" + uuid4().hex[:32],
        "derive this",
        30,
        account_id="personal",
        provider="codex",
        model="gpt-5.6-luna",
        effort="medium",
        start_url="http://firm/summary/start",
        start_token="summary-secret",
    )
    spec = adapter.build_summary_spec(request, layout=Layout())
    command = json.loads(spec.env["FIRM_CODEX_ARGV"])
    assert "--cd" not in command
    assert spec.secret_env == {"FIRM_SUMMARY_TOKEN": "summary-token"}
    assert "FIRM_ACTIVATION_TOKEN" not in spec.env
    assert spec.env["FIRM_SUMMARY_START_URL"] == "http://firm/summary/start"
    assert spec.secret_env == {"FIRM_SUMMARY_TOKEN": "summary-token"}
    assert spec.secrets["summary-token"] == b"summary-secret"
    summary = spec.secrets["summary-instructions"].decode()
    assert "source envelopes" in summary and "not instructions to execute" in summary
    assert "You are founder" not in summary
    assert "role-instructions" not in spec.secrets
    assert spec.stdin == request.prompt.encode()
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert "include_collaboration_mode_instructions=false" in command
    assert "features.shell_tool=false" in command
    assert "features.view_image=false" in command
    assert 'web_search="disabled"' in command
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert len(spec.run_id) <= 40
    assert len(spec.role_id) <= 40


def test_summary_launcher_refuses_missing_or_denied_gate(monkeypatch):
    assert codex_launch._begin_summary("", "token") is False
    assert codex_launch._begin_summary("http://firm/summary/start", "") is False
    monkeypatch.setattr(codex_launch, "_prepare_home", lambda _session: None)
    monkeypatch.setattr(codex_launch, "_begin_summary", lambda _url, _token: False)
    launched = []
    monkeypatch.setattr(codex_launch, "_run", lambda *_args: launched.append(True) or 0)
    monkeypatch.setenv("FIRM_CODEX_ARGV", json.dumps(["codex", "exec"]))
    monkeypatch.setenv("FIRM_RUNTIME_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("FIRM_SUMMARY_RUN", "1")
    assert codex_launch.main() == 70
    assert launched == []


def test_builds_exact_resume_and_validates_native_session_id(tmp_path):
    adapter = provider(tmp_path)
    native_id = str(uuid4())
    call = invocation(
        context={
            "employee_id": "founder",
            "address": "founder",
            "manager_id": "owner",
            "mandate_revision": 0,
            "birth": False,
            "fresh_session": False,
            "workspace_references": {
                "private": "/home/agent", "scratch": "/home/agent/scratch", "workspace": "/work"
            },
            "inbox": {"messages": [], "count": 0},
        }
    )
    spec = adapter.build_spec(
        call,
        "r-example",
        "run-example",
        "https://bridge.example/mcp/",
        native_id,
        layout=Layout(),
    )
    command = json.loads(spec.env["FIRM_CODEX_ARGV"])
    assert command[-2:] == [native_id, "-"]
    assert command[command.index("exec") + 1] == "resume"
    assert "--last" not in command
    assert "Your current mandate" not in spec.stdin.decode()

    with pytest.raises(ValueError, match="must be a UUID"):
        adapter.build_spec(
            call,
            "r-example",
            "run-example",
            "https://bridge.example/mcp",
            "../../auth.json",
            layout=Layout(),
        )


def test_auth_file_is_selected_explicitly_and_read_only_at_build(tmp_path):
    auth_file = tmp_path / "not-created-yet.json"
    adapter = CodexProvider(model="gpt-6-astra", effort="medium", auth_file=auth_file)
    with pytest.raises(ValueError, match="not readable"):
        adapter.build_spec(
            invocation(),
            "r-example",
            "run-example",
            "http://firm.local/mcp",
            layout=Layout(),
        )

    auth_file.write_text("[]")
    auth_file.chmod(0o600)
    with pytest.raises(ValueError, match="JSON object"):
        adapter.build_spec(
            invocation(),
            "r-example",
            "run-example",
            "http://firm.local/mcp",
            layout=Layout(),
        )

    target = tmp_path / "real.json"
    target.write_text("{}")
    auth_file.unlink()
    auth_file.symlink_to(target)
    with pytest.raises(ValueError, match="not readable"):
        adapter.build_spec(
            invocation(),
            "r-example",
            "run-example",
            "http://firm.local/mcp",
            layout=Layout(),
        )

    fifo = tmp_path / "auth-fifo"
    os.mkfifo(fifo)
    adapter.auth_file = fifo
    with pytest.raises(ValueError, match="regular file"):
        adapter.build_spec(
            invocation(),
            "r-example",
            "run-example",
            "http://firm.local/mcp",
            layout=Layout(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///mcp",
        "http://firm.local/other",
        "http://user:pass@firm.local/mcp",
        "http://firm.local/mcp?token=bad",
    ],
)
def test_rejects_non_direct_mcp_urls(tmp_path, url):
    with pytest.raises(ValueError, match="mcp_url"):
        provider(tmp_path).build_spec(
            invocation(), "r-example", "run-example", url, layout=Layout()
        )


def test_employee_birth_and_fresh_transition_inputs_are_attributed(tmp_path):
    call = invocation(
        context={
            "employee_id": str(uuid4()),
            "address": "founder/finance",
            "manager_id": "founder",
            "mandate_revision": 2,
            "mandate": {"revision": 2, "author_id": "founder", "body": "Own invoices."},
            "founder_mandate": {
                "revision": 4,
                "author_id": "owner",
                "body": "Serve customers.",
            },
            "birth": True,
            "fresh_session": True,
            "workspace_references": {
                "private": "/home/agent", "scratch": "/home/agent/scratch", "workspace": "/work"
            },
            "handoff_notes": "Reconcile invoice 42 next.",
            "recovery": {"cause": "host_restart", "recent_claims": []},
            "orientation": {
                "direct_reports": [],
                "top_level_roles": [{"address": "founder/sales"}],
                "top_level_included": True,
            },
            "inbox": {
                "pending_count": 1,
                "conversations": [
                    {
                        "conversation_id": "c1",
                        "pending_count": 1,
                        "participants": [{"address": "owner"}],
                        "waiting": [
                            {
                                "message_id": "m1",
                                "subject": "Month close",
                                "sender": {"address": "owner"},
                            }
                        ],
                    }
                ],
            },
        }
    )
    spec = provider(tmp_path).build_spec(
        call, "r-example", "run-example", "http://firm.local/mcp", layout=Layout()
    )
    prompt = spec.stdin.decode()
    assert resource_prompt("birth.md") in prompt
    instructions = spec.secrets["role-instructions"].decode()
    assert "Own invoices." in instructions and "Own invoices." not in prompt
    assert "Serve customers." not in prompt + instructions
    assert "Reconcile invoice 42 next." not in prompt
    assert "host_restart" not in prompt
    assert "Month close" in prompt
    assert "founder/sales" in prompt
    assert instructions == standing(call.context)


def test_parse_result_extracts_only_native_session_and_agent_message():
    native_id = str(uuid4())
    logs = "\n".join(
        [
            "non-json diagnostic",
            json.dumps({"type": "thread.started", "thread_id": native_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "mcp_tool_call", "text": "untrusted tool result"},
                }
            ),
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}}
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        ]
    )
    result = parse_result(logs, 0)
    assert result.success
    assert result.provider_session_id == native_id
    assert result.final_text == "Done"
    assert result.error is None


def test_parse_result_returns_safe_structured_failures():
    native_id = str(uuid4())
    sensitive = "prompt and bearer activation-secret"
    result = parse_result(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": native_id}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": sensitive},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {
                            "code": "rate_limit",
                            "message": sensitive,
                            "retry_after_ms": 1500,
                            "resetsAt": 2000,
                        },
                    }
                ),
            ]
        ),
        1,
    )
    assert not result.success
    assert result.error.kind == "turn_failed"
    assert result.error.category == "quota"
    assert result.error.code == "rate_limit"
    assert result.error.retry_after_seconds == 1.5
    assert result.error.reset_at == 2000
    assert result.final_text is None
    assert sensitive not in repr(result)

    gate = parse_result(
        json.dumps({"type": "firm.launch_failed", "kind": "activation_gate", "code": "http_401"}),
        70,
    )
    assert not gate.success
    assert gate.provider_session_id is None
    assert gate.error.kind == "activation_gate"
    assert gate.error.category == "unknown"
    assert gate.error.code == "http_401"


def test_usage_limit_error_carries_codex_structured_reset_timestamp():
    result = parse_result(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "code": "usage_limit_reached",
                    "resets_at": "2026-09-10T00:00:00Z",
                },
            }
        ),
        1,
    )
    assert result.error.category == "quota"
    assert result.error.reset_at == 1788998400


def test_parse_result_requires_canonical_thread_and_terminal_event():
    missing = parse_result(json.dumps({"type": "turn.completed"}), 0)
    assert not missing.success
    assert missing.error.kind == "invalid_output"
    assert missing.error.code == "invalid_session"

    malformed = parse_result(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "../../auth.json"}),
                json.dumps({"type": "turn.completed"}),
            ]
        ),
        0,
    )
    assert not malformed.success
    assert malformed.provider_session_id is None
    assert malformed.error.kind == "invalid_output"

    native_id = str(uuid4())
    incomplete = parse_result(json.dumps({"type": "thread.started", "thread_id": native_id}), 0)
    assert incomplete.provider_session_id == native_id
    assert incomplete.error.kind == "invalid_output"


@pytest.mark.parametrize("task", [False, True])
def test_launcher_keeps_only_session_tree_in_persistent_home(tmp_path, monkeypatch, task):
    runtime_session_id = str(uuid4())
    run_root = tmp_path / "run" / "firm"
    credentials = run_root / "credentials"
    credentials.mkdir(parents=True)
    auth_source = credentials / "codex-auth"
    auth_source.write_bytes(b'{"tokens":{"access_token":"secret"}}')
    role_home = tmp_path / "home" / "agent"
    role_home.mkdir(parents=True)
    monkeypatch.setenv("FIRM_STATE_DIR", str(run_root))
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", str(credentials))
    monkeypatch.setenv("FIRM_PRIVATE_HOME", str(role_home))
    monkeypatch.setenv("FIRM_TASK_RUN", "1" if task else "0")

    codex_launch._prepare_home(runtime_session_id)

    codex_home = run_root / "codex"
    assert (codex_home / "auth.json").read_bytes() == auth_source.read_bytes()
    assert (codex_home / "auth.json").is_symlink() is task
    if task:
        assert (codex_home / "auth.json").resolve() == auth_source.resolve()
    assert (codex_home / "sessions").is_symlink()
    persistent = role_home / ".firm" / "codex-sessions" / runtime_session_id
    assert (codex_home / "sessions").resolve() == (persistent / "sessions").resolve()
    assert list(persistent.iterdir()) == [persistent / "sessions"]
    assert not (role_home / "auth.json").exists()


def test_launcher_posts_empty_authenticated_gate_request(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def open_request(request, timeout):
        seen["url"] = request.full_url
        seen["data"] = request.data
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(codex_launch.urllib.request, "urlopen", open_request)
    assert codex_launch._begin_activation(
        "http://firm.local/activation/start", "activation-secret"
    )
    assert seen == {
        "url": "http://firm.local/activation/start",
        "data": b"",
        "authorization": "Bearer activation-secret",
        "timeout": 10,
    }


def test_structured_rollout_usage_keeps_current_cumulative_highwater_and_compaction_separate():
    events = [
        {
            "type": "firm.telemetry",
            "provider": "codex",
            "usage": {
                "last_token_usage": {"total_tokens": 80_000, "input_tokens": 79_000},
                "total_token_usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 60_000,
                    "output_tokens": 2_000,
                    "reasoning_output_tokens": 500,
                },
                "model_context_window": 258_400,
            },
        },
        {"type": "firm.telemetry", "provider": "codex", "native_compaction": True},
        {
            "type": "firm.telemetry",
            "provider": "codex",
            "usage": {
                "last_token_usage": {"total_tokens": 20_000},
                "total_token_usage": {"input_tokens": 120_000, "output_tokens": 3_000},
                "model_context_window": 258_400,
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 40_000, "cached_input_tokens": 30_000, "output_tokens": 900},
        },
    ]
    observed = parse_usage("\n".join(json.dumps(event) for event in events))
    assert observed.current_context_tokens == 20_000
    assert observed.high_water_context_tokens == 80_000
    assert observed.provider_context_window == 258_400
    assert observed.input_tokens == 40_000
    assert observed.session_input_tokens == 120_000
    assert observed.session_cached_input_tokens == 60_000
    assert observed.native_compactions == 1
    assert observed.source == "codex_rollout"


def test_tool_calls_count_completed_work_items_and_not_the_role_s_own_prose():
    events = [
        {"type": "thread.started", "thread_id": str(uuid4())},
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "rm -rf /secret",
                "aggregated_output": "confidential draft",
            },
        },
        # A repeated completion of the same item must not count twice.
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "file_change"}},
        {
            "type": "item.completed",
            "item": {
                "id": "item_3",
                "type": "mcp_tool_call",
                "server": "firm",
                "tool": "send",
                "arguments": {"body": "confidential draft"},
            },
        },
        {"type": "item.completed", "item": {"id": "item_4", "type": "reasoning"}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": "Hi"}},
        {"type": "turn.completed"},
    ]
    logs = "\n".join(json.dumps(event) for event in events)
    tally = parse_tool_calls(logs)
    assert tally == {"command_execution": 1, "file_change": 1, "mcp__firm__send": 1}
    assert parse_tool_calls(logs.encode()) == tally
    assert parse_tool_calls("not json\n") == {}
    with pytest.raises(TypeError):
        parse_tool_calls(None)


@pytest.mark.parametrize("invalid", [None, "", "  ", 42, {}, []])
def test_guest_rollout_model_identity_reaches_usage(tmp_path, capsys, invalid):
    records = [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "cwd": "private"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra", "prompt": "secret"}},
        {"type": "turn_context", "payload": {"model": invalid}},
        {"type": "session_meta", "payload": {"model": "not-turn-evidence"}},
    ]
    logs = "\n".join(json.dumps(r) for r in records) + "\n"
    (tmp_path / "rollout.jsonl").write_text(logs)
    codex_launch._scan_rollouts(tmp_path, {})
    projected = capsys.readouterr().out
    assert "private" not in projected and "secret" not in projected
    assert len(projected.splitlines()) == 2
    for observed in (parse_usage(projected), parse_usage(logs.encode())):
        assert observed.resolved_model == "gpt-6-astra"
        assert observed.input_tokens is None
        assert observed.source == "codex_rollout"


def test_guest_rollout_projection_emits_only_structured_numeric_facts():
    projected = codex_launch._usage_event(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 123, "prompt": "secret"},
                    "total_token_usage": {"input_tokens": 456},
                    "model_context_window": 1_000,
                },
                "message": "untrusted transcript text",
            },
        }
    )
    assert projected == {
        "type": "firm.telemetry",
        "provider": "codex",
        "usage": {
            "last_token_usage": {"total_tokens": 123},
            "total_token_usage": {"input_tokens": 456},
            "model_context_window": 1_000,
        },
    }
    assert codex_launch._usage_event(
        {"type": "event_msg", "payload": {"type": "context_compacted"}}
    ) == {"type": "firm.telemetry", "provider": "codex", "native_compaction": True}


def test_codex_rollout_subscription_windows_are_projected_and_parsed():
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {},
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": 87.5,
                    "window_minutes": 300,
                    "resets_at": 2000,
                },
                "secondary": {
                    "used_percent": 0,
                    "window_minutes": 10080,
                    "resets_at": 3000,
                },
                "credits": {"private": "omitted"},
                "plan_type": "plus",
            },
        },
    }
    projected = codex_launch._usage_event(record)
    assert projected == {
        "type": "firm.telemetry",
        "provider": "codex",
        "usage": {"last_token_usage": {}, "total_token_usage": {}},
        "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": 87.5, "resets_at": 2000},
            "secondary": {"used_percent": 0.0, "resets_at": 3000},
        },
    }
    assert parse_subscription(json.dumps(projected)) == {
        "event_id": "line:0",
        "windows": [
            {
                "window": "codex_primary",
                "limit_id": "codex",
                "used_percent": 87.5,
                "resets_at": 2000,
            },
            {
                "window": "codex_secondary",
                "limit_id": "codex",
                "used_percent": 0.0,
                "resets_at": 3000,
            },
        ],
    }


def test_codex_rate_limit_only_token_count_survives_null_usage_info():
    projected = codex_launch._usage_event(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {
                    "limit_id": "codex_other",
                    "primary": {"used_percent": 99, "resets_at": 2000},
                },
            },
        }
    )
    assert projected["rate_limits"] == {
        "limit_id": "codex_other",
        "primary": {"used_percent": 99.0, "resets_at": 2000.0},
    }
    assert parse_subscription(json.dumps(projected))["windows"][0]["window"] == (
        "codex_other_primary"
    )


def test_codex_subscription_parser_ignores_missing_rate_limit_snapshots():
    reading = parse_subscription(
        json.dumps({"type": "firm.telemetry", "provider": "codex", "usage": {}})
    )
    assert reading is not None and reading["windows"] == []


@pytest.mark.parametrize(
    "limits", [None, {}, {"primary": {"used_percent": True, "resets_at": 2000}}]
)
def test_newer_codex_missing_or_invalid_snapshot_supersedes_older_one(limits):
    first = {
        "type": "firm.telemetry",
        "provider": "codex",
        "usage": {},
        "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": 90, "resets_at": 2000},
        },
    }
    latest = {"type": "firm.telemetry", "provider": "codex", "usage": {}, "rate_limits": limits}
    assert parse_subscription("\n".join([json.dumps(first), json.dumps(latest)]))["windows"] == []


def test_turn_usage_does_not_claim_rollout_compaction_support():
    observed = parse_usage(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
    )
    assert observed.current_context_tokens is None
    assert observed.native_compactions is None
    assert observed.source == "codex_turn"
    assert observed.resolved_model is None


def test_guest_rollout_scanner_does_not_lose_a_partial_json_line(tmp_path, capsys):
    rollout = tmp_path / "rollout.jsonl"
    record = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"total_tokens": 321}},
            },
        }
    ).encode()
    rollout.write_bytes(record[:20])
    offsets = {}
    codex_launch._scan_rollouts(tmp_path, offsets)
    assert capsys.readouterr().out == ""
    with rollout.open("ab") as handle:
        handle.write(record[20:] + b"\n")
    codex_launch._scan_rollouts(tmp_path, offsets)
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["usage"]["last_token_usage"]["total_tokens"] == 321


@pytest.mark.parametrize("invalid", ["permissions", "owner"])
def test_auth_file_requires_private_permissions_and_current_owner(tmp_path, monkeypatch, invalid):
    adapter = provider(tmp_path)
    adapter.validate_credentials()
    if invalid == "permissions":
        adapter.auth_file.chmod(0o644)
    else:
        fstat = os.fstat

        def wrong_owner(fd):
            values = list(fstat(fd))
            values[4] = os.getuid() + 1
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(ProviderSetupError) as failure:
        adapter.validate_credentials()
    assert (
        failure.value.code == "credential_permissions"
        if invalid == "permissions"
        else failure.value.code == "credential_ownership"
    )
    assert "host-secret" not in str(failure.value)


@pytest.mark.parametrize("text", [{"secret": "private-value"}, ["private-value"], None])
@pytest.mark.parametrize("valid_position", ["before", "after", "absent"])
def test_malformed_agent_message_fails_even_with_other_valid_text(text, valid_position):
    events = [{"type": "thread.started", "thread_id": str(uuid4())}]
    valid = {"type": "item.completed", "item": {"type": "agent_message", "text": "Valid"}}
    malformed = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    events.extend(
        [valid, malformed]
        if valid_position == "before"
        else [malformed, valid]
        if valid_position == "after"
        else [malformed]
    )
    events.append({"type": "turn.completed"})
    logs = "\n".join(json.dumps(e) for e in events)
    result = parse_result(logs, 0)
    assert result.success is False
    assert result.final_text is None
    assert result.error.kind == "invalid_output"
    assert "private-value" not in repr(result)
    assert CodexProvider.parse_summary(logs, 0).error == "invalid_output"


@pytest.mark.parametrize("text", [None, ""])
def test_completed_turn_allows_absent_agent_message_or_empty_string(text):
    events = [{"type": "thread.started", "thread_id": str(uuid4())}]
    if text is not None:
        events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    events.append({"type": "turn.completed"})
    result = parse_result("\n".join(json.dumps(e) for e in events), 0)
    assert result.success
    assert result.final_text == text


@pytest.mark.parametrize("summary", [False, True])
def test_launcher_child_environment_uses_selected_credentials_for_both_paths(
    tmp_path, monkeypatch, summary
):
    from types import SimpleNamespace

    conflicts = (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
    )
    for key in conflicts:
        monkeypatch.setenv(key, "conflicting-value")
    monkeypatch.setenv("FIRM_CODEX_ARGV", '["codex", "exec"]')
    monkeypatch.setenv("FIRM_RUNTIME_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("FIRM_SUMMARY_RUN", "1" if summary else "0")
    monkeypatch.setenv("FIRM_ACTIVATION_TOKEN", "role-token")
    monkeypatch.setenv("FIRM_ACTIVATION_START_URL", "http://firm/start")
    monkeypatch.setenv("FIRM_SUMMARY_TOKEN", "summary-token")
    monkeypatch.setenv("FIRM_SUMMARY_START_URL", "http://firm/summary/start")
    monkeypatch.setenv("CODEX_HOME", "ambient-home")
    monkeypatch.setenv("FIRM_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(codex_launch, "_prepare_home", lambda _: None)
    monkeypatch.setattr(codex_launch, "_begin_activation", lambda *_: True)
    seen = {}

    def popen(command, *, env):
        seen.update(env)
        return SimpleNamespace(poll=lambda: 0, returncode=0)

    monkeypatch.setattr(codex_launch.subprocess, "Popen", popen)
    assert codex_launch.main() == 0
    assert not set(conflicts) & seen.keys()
    assert seen["CODEX_HOME"] == str(tmp_path / "codex")
    assert seen["PATH"] == os.environ["PATH"]
    token_key = "FIRM_SUMMARY_TOKEN" if summary else "FIRM_ACTIVATION_TOKEN"
    assert seen[token_key] == os.environ[token_key]


@pytest.mark.parametrize(
    "detail",
    [{"code": "authentication_failed"}, {"type": "authentication_error"}, "authentication_failed"],
)
def test_codex_auth_failure_before_thread_start_keeps_classification(detail):
    result = parse_result(json.dumps({"type": "turn.failed", "error": detail}), 1)
    assert not result.success
    assert result.error.category == "auth"


def test_codex_preserves_structured_error_before_generic_turn_failure():
    events = [
        {"type": "error", "error": {"code": "authentication_failed", "message": "private"}},
        {"type": "turn.failed", "error": {"message": "private"}},
    ]
    result = parse_result("\n".join(json.dumps(e) for e in events), 1)
    assert result.error.category == "auth"
    assert "private" not in repr(result)
