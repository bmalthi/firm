"""Exercise the shared launch boundary with all three real native adapters."""

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from firm_sandbox import Layout
from test_agy_credentials import account
from test_agy_provider import encode, stream
from test_codex_provider import invocation

from firm_runtime import agy_launch
from firm_runtime.errors import FirmError
from firm_runtime.mcp_server import TOOL_DESCRIPTIONS
from firm_runtime.provider_contract import (
    ProviderSetupError,
    RoleLaunchRequest,
    auth_repair,
    incident_code,
)
from firm_runtime.provider_registry import provider_implementation
from firm_runtime.provider_support import MCP_TIMEOUT_SECONDS, render_invocation, standing
from firm_runtime.providers import Providers, provider_adapter
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.summaries import SummaryLaunch


def worker_logs(name):
    session = "11111111-1111-4111-8111-111111111111"
    if name == "claude":
        events = [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done",
                "session_id": session,
            }
        ]
    else:
        events = [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "turn.completed"},
        ]
    return "\n".join(json.dumps(event) for event in events).encode()


@pytest.mark.parametrize("code,kept", [
    ("oauth_org_not_allowed", "oauth_org_not_allowed"),
    ("OAuth_Org_Not_Allowed", "oauth_org_not_allowed"),
    ("credential_refresh_rejected", "credential_refresh_rejected"),
    ("Credential_Invalid", "credential_invalid"),
    # The prefix alone proves nothing: a guest launcher event could carry such a token.
    ("credential_private_token", None),
    ("credential_refresh_unavailable", None),
    ("some_unknown_code", None),
    ("unsafe private /path", None),
    ("../etc/passwd", None),
    ("", None),
    (None, None),
    (7, None),
])
def test_incident_code_keeps_only_recognized_or_credential_codes(code, kept):
    assert incident_code(code) == kept


@pytest.mark.parametrize("code,repair", [
    ("oauth_org_not_allowed", "organization"),
    ("account_on_hold", "account"),
    ("credential_refresh_rejected", "sign_in"),
    ("invalid_api_key", "sign_in"),
    (None, "sign_in"),
])
def test_auth_repair_says_whether_signing_in_can_help(code, repair):
    assert auth_repair(code) == repair


@pytest.fixture(params=["claude", "codex", "agy"])
def real_provider(tmp_path, request):
    name = request.param
    if name == "agy":
        auth = account(tmp_path, expiry=4102444800)
        model = "gemini-4-hydrogen"
    else:
        auth = tmp_path / "auth"
        auth.write_text(
            '{"tokens":{"access_token":"synthetic-access"}}'
            if name == "codex" else "synthetic-access"
        )
        auth.chmod(0o600)
        model = "owner-model"
    profile = {"provider": name, "model": model, "effort": "high", "accounts": ["personal"]}
    settings = {"provider": name, "auth_file": str(auth), "max_active": 2}
    adapter = provider_adapter(profile, settings)
    return name, adapter, profile, settings


@pytest.mark.parametrize("resumed", [False, True])
def test_real_role_request_preserves_native_mcp_and_resume_behavior(
    real_provider, monkeypatch, resumed
):
    name, adapter, _, _ = real_provider
    call = invocation()
    native = str(uuid4()) if resumed else None
    request = RoleLaunchRequest(
        invocation=call, role_id="r-contract", run_id="run-contract",
        mcp_url="http://firm.local:8421/mcp", provider_session_id=native,
        offered_tools=("peek", "handoff"),
    )
    assert adapter.validate_credentials() is None
    spec = adapter.build_spec(request, layout=Layout())
    assert (spec.role_id, spec.run_id) == ("r-contract", "run-contract")
    assert spec.secrets["activation-token"] == call.token.encode()
    assert spec.secrets["role-instructions"] == standing(call.context).encode()
    assert spec.secret_env["FIRM_ACTIVATION_TOKEN"] == "activation-token"
    assert "summary-token" not in spec.secrets
    assert spec.env["FIRM_ACTIVATION_START_URL"] == "http://firm.local:8421/activation/start"
    command = json.loads(spec.env[f"FIRM_{name.upper()}_ARGV"])
    if name == "claude":
        mcp = json.loads(command[command.index("--mcp-config") + 1])
        assert mcp == {"mcpServers": {"firm": {
            "type": "http", "url": request.mcp_url,
            "headers": {"Authorization": "Bearer ${FIRM_ACTIVATION_TOKEN}"},
            "timeout": MCP_TIMEOUT_SECONDS * 1000,
        }}}
        assert "--strict-mcp-config" in command
        flag = "--resume" if resumed else "--session-id"
        assert command[command.index(flag) + 1] == (native if resumed else call.session_id)
        assert "--tools" not in command  # Native shell/file tools remain available.
        denied = command[command.index("--disallowedTools") + 1:command.index("--mcp-config")]
        assert denied == ["SendMessage", "ListAgents", "Task", "Agent", *[
            f"mcp__firm__{tool}" for tool in TOOL_DESCRIPTIONS
            if tool not in request.offered_tools
        ]]
    elif name == "codex":
        config = [command[i + 1] for i, value in enumerate(command) if value == "-c"]
        assert [value for value in config if value.startswith("mcp_servers.")] == [
            f'mcp_servers.firm.url="{request.mcp_url}"',
            'mcp_servers.firm.bearer_token_env_var="FIRM_ACTIVATION_TOKEN"',
            "mcp_servers.firm.required=true",
            'mcp_servers.firm.enabled_tools=["peek", "handoff"]',
            f"mcp_servers.firm.tool_timeout_sec={MCP_TIMEOUT_SECONDS}",
        ]
        assert ("resume" in command) == resumed
        if resumed:
            assert command[-2] == native
        assert 'sandbox_mode="danger-full-access"' in config
    else:
        assert spec.env["FIRM_AGY_NATIVE_ID"] == (native or "")
        assert json.loads(spec.env["FIRM_AGY_MCP_TOOLS"]) == list(request.offered_tools)
        for key, value in spec.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("FIRM_ACTIVATION_TOKEN", call.token)
        assert agy_launch._mcp_settings("role") == {"mcpServers": {"firm": {
            "serverUrl": request.mcp_url,
            "headers": {"Authorization": "Bearer " + call.token},
            "timeoutSeconds": MCP_TIMEOUT_SECONDS,
            "enabledTools": list(request.offered_tools),
            "tools": {tool: {"eager": True} for tool in request.offered_tools},
        }}}
        assert type(agy_launch._mcp_settings("role")["mcpServers"]["firm"]["timeoutSeconds"]) is int
    expected = render_invocation(call, native)
    actual = json.loads(spec.stdin)["message"]["content"] if name == "agy" else spec.stdin.decode()
    assert actual == expected
    with pytest.raises(FrozenInstanceError):
        request.run_id = "changed"
    call.context["launch_annotation"] = "mutable"
    assert request.invocation.context["launch_annotation"] == "mutable"


@pytest.mark.parametrize("tools", [tuple(TOOL_DESCRIPTIONS), ("peek", "send"), ()])
def test_real_role_catalog_policy_handles_full_subset_and_empty(real_provider, tools):
    name, adapter, _, _ = real_provider
    request = RoleLaunchRequest(
        invocation=invocation(), role_id="r-policy", run_id="run-policy",
        mcp_url="http://firm.local:8421/mcp", provider_session_id=None,
        offered_tools=tools,
    )
    if name == "agy" and not tools:
        with pytest.raises(ProviderSetupError) as caught:
            adapter.build_spec(request, layout=Layout())
        assert (caught.value.category, caught.value.code) == ("config", "unsupported_mcp_catalog")
        return
    spec = adapter.build_spec(request, layout=Layout())
    command = json.loads(spec.env[f"FIRM_{name.upper()}_ARGV"])
    if name == "codex":
        assert f"mcp_servers.firm.enabled_tools={json.dumps(tools)}" in command
        assert f"mcp_servers.firm.tool_timeout_sec={MCP_TIMEOUT_SECONDS}" in command
    elif name == "claude":
        denied = {value for value in command if value.startswith("mcp__firm__")}
        assert denied == {f"mcp__firm__{tool}" for tool in TOOL_DESCRIPTIONS if tool not in tools}
        assert json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]["firm"][
            "timeout"
        ] == MCP_TIMEOUT_SECONDS * 1000
    else:
        assert spec.env["FIRM_AGY_MCP_TIMEOUT"] == str(MCP_TIMEOUT_SECONDS)


@pytest.mark.parametrize("changes", [
    {"offered_tools": ("unknown_tool",)}, {"offered_tools": ("peek*",)},
    {"offered_tools": ("peek", "peek")}, {"offered_tools": (1,)},
    {"offered_tools": "peek"},
])
def test_real_role_rejects_invalid_mcp_policy_before_launch(real_provider, changes):
    _, adapter, _, _ = real_provider
    request = RoleLaunchRequest(
        invocation=invocation(), role_id="r-policy", run_id="run-policy",
        mcp_url="http://firm.local:8421/mcp", provider_session_id=None,
        offered_tools=tuple(TOOL_DESCRIPTIONS),
    )
    with pytest.raises(ProviderSetupError) as caught:
        adapter.build_spec(replace(request, **changes), layout=Layout())
    assert (caught.value.category, caught.value.code) == ("config", "invalid_invocation")


def test_role_mcp_settings_returns_fixed_timeout():
    from firm_runtime.provider_support import role_mcp_settings

    request = SimpleNamespace(offered_tools=("peek",))
    assert role_mcp_settings(request) == (("peek",), MCP_TIMEOUT_SECONDS)


def test_real_summary_builders_disable_tools_and_have_no_role_authority(real_provider):
    name, adapter, _, _ = real_provider
    request = SummaryLaunch(
        run_id="s-" + uuid4().hex, prompt="Read this exact input.", timeout=30,
        provider=name, model=adapter.model, effort="high",
        start_url="http://firm.local:8421/summary/start", start_token="summary-secret",
        instructions="Summarize only the supplied input.",
        tools_enabled=False, kind="summary",
    )
    spec = adapter.build_summary_spec(request, Layout())
    assert spec.secrets["summary-token"] == b"summary-secret"
    assert spec.secrets["summary-instructions"] == request.instructions.encode()
    assert spec.secret_env["FIRM_SUMMARY_TOKEN"] == "summary-token"
    assert "activation-token" not in spec.secrets
    assert "FIRM_ACTIVATION_TOKEN" not in spec.secret_env
    command = json.loads(spec.env[f"FIRM_{name.upper()}_ARGV"])
    if name == "claude":
        assert "--strict-mcp-config" in command
        assert "--mcp-config" not in command
        assert command[command.index("--tools") + 1] == ""
    elif name == "codex":
        assert "mcp_servers={}" in command
        assert "features.multi_agent=false" in command and "agents.enabled=false" in command
        assert "--dangerously-bypass-approvals-and-sandbox" not in command
        assert "features.shell_tool=false" in command
        assert 'web_search="disabled"' in command
    else:
        assert spec.env["FIRM_AGY_MODE"] == "tool_free"
        assert spec.env["FIRM_AGY_MCP_URL"] == ""
        assert agy_launch._mcp_settings("tool_free") is None
        settings = agy_launch._settings("tool_free")
        assert "mcp(*)" in settings["permissions"]["deny"]
        assert settings["toolPermission"] == "strict"


def provider_runtime(tmp_path, profile, account_settings):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("contract", "Build.", {
        "max_active": 2, "staffing": False, "founder_profile": "main",
        "profiles": {"main": profile}, "accounts": {"personal": account_settings},
    })
    return runtime


@pytest.mark.parametrize("same_provider", [False, True])
def test_retained_execution_uses_captured_model_and_only_matching_current_credentials(
    tmp_path, real_provider, same_provider
):
    name, original, profile, settings = real_provider
    runtime = provider_runtime(tmp_path, profile, settings)
    providers = Providers(runtime)
    runtime.start()
    call = runtime.admit()
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["profiles"]["main"].update(model="edited-model", effort="low")
        config["accounts"]["personal"].update(
            provider=name if same_provider else ("codex" if name != "codex" else "claude"),
            auth_file=str(tmp_path / "rotated-auth"),
        )
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    retained = providers.for_session(call.session_id)
    assert retained.name == name and retained.model == original.model
    assert retained.effort == "high"
    expected_auth = tmp_path / "rotated-auth" if same_provider else original.auth_file
    assert retained.auth_file == expected_auth
    # Active failures retain configured/native launch attribution; raw parsing cannot infer it.
    assert provider_implementation(name).parse_result(b"", 1).error.model is None
    expected_model = "gemini-4-hydrogen-high" if name == "agy" else "owner-model"
    assert retained.parse_result(b"", 1).error.model == expected_model


def test_stateless_summary_and_llm_parsing_survive_changed_accounts_and_credentials(
    tmp_path, real_provider, monkeypatch
):
    name, adapter, profile, settings = real_provider
    runtime = provider_runtime(tmp_path, profile, settings)
    providers = Providers(runtime)
    runner = providers.summary_runner(object(), mcp_url="http://firm.local/mcp")
    request = SummaryLaunch("captured-summary", "Input", 30, provider=name, account_id="personal")
    runtime.start()
    call = runtime.admit()
    runtime.mark_started(call.activation_id)
    with runtime.store.transaction() as db:
        now = runtime.store.clock()
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,provider,"
            "model,deadline,token_hash,provider_started_at,kind,caller_activation_id) "
            "VALUES('captured-llm',?,'running',0,'personal',?,?,?,?,?,'llm',?)",
            (now, name, adapter.model, now + 60, hashlib.sha256(b"llm-tok").hexdigest(), now,
             call.activation_id),
        )
        config = runtime.accounts.config(db)
        config["accounts"]["personal"] = {"provider": "changed", "auth_file": "/missing"}
        config["profiles"]["main"].update(provider="changed", model="changed", effort="invalid")
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    monkeypatch.setattr(
        type(adapter), "__init__", lambda *a, **kw: pytest.fail("constructed adapter")
    )
    output = encode(stream(usage={"input_tokens": 100, "output_tokens": 20})).encode() \
        if name == "agy" else worker_logs(name)
    expected = "FIRM_OK" if name == "agy" else "Done"
    assert runner.parse_output(request, output, 0).text == expected
    assert runtime.llm_service.finish("llm-tok", exit_code=0, logs=output)["status"] == "completed"
    with pytest.raises(FirmError, match="summary account/provider binding changed"):
        runner.build_spec(request)
    assert adapter.parse_subscription(b"") is None


def test_registry_metadata_imports_do_not_load_native_adapters():
    script = '''
import sys
import firm_runtime.provider_definitions
import firm_runtime.provider_contract
import firm_runtime.provider_registry
assert not any("firm_runtime." + name + "_provider" in sys.modules
               for name in ("claude", "codex", "agy"))
from firm_runtime.provider_registry import provider_implementation
provider_implementation("codex")
assert "firm_runtime.codex_provider" in sys.modules
assert "firm_runtime.claude_provider" not in sys.modules
assert "firm_runtime.agy_provider" not in sys.modules
'''
    subprocess.run([sys.executable, "-c", script], check=True)
    with pytest.raises(FirmError, match="unsupported provider in retained binding"):
        provider_implementation("unknown")
