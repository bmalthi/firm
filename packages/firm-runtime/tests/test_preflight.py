import json
import logging
from pathlib import Path

import pytest

from firm_runtime import providers as providers_module
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.provider_support import read_private_credential
from firm_runtime.providers import Providers
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import OWNER, Store


def runtime_with_accounts(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"tokens": {"access_token": "test-only"}}))
    good.chmod(0o600)
    broken = tmp_path / "missing.json"
    (tmp_path / "durable").mkdir()
    runtime = Runtime(Store.scratch(tmp_path / "durable" / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "codex",
            "accounts": {
                "broken": {"provider": "codex", "auth_file": str(broken), "max_active": 1},
                "good": {"provider": "codex", "auth_file": str(good), "max_active": 1},
            },
            "profiles": {
                "codex": {
                    "provider": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                    "accounts": ["broken", "good"],
                }
            },
        },
    )
    return runtime, broken


def test_private_credential_reader_normalizes_invalid_path_without_echoing_it():
    with pytest.raises(ProviderSetupError) as error:
        read_private_credential(Path("/tmp/\0do-not-print"), "Codex")

    assert error.value.code == "credential_unreadable"
    assert "do-not-print" not in str(error.value)


def test_bad_credentials_held_before_any_activation_good_account_still_eligible(tmp_path):
    runtime, _ = runtime_with_accounts(tmp_path)
    providers = Providers(runtime)
    report = providers.preflight()
    assert report["checked_accounts"] == ["good"]
    assert report["failures"] == [
        {"scope": "account", "dependency": "broken", "code": "credential_unreadable"}
    ]
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 0
        assert runtime.store.role(db, "founder")["attempt_count"] == 0
    runtime.start()
    invocation = runtime.admit()
    with runtime.store.read() as db:
        assert runtime.accounts.binding(db, invocation.session_id)["account_id"] == "good"


def test_preflight_deduplicates_incident_and_repair_needs_explicit_clear(tmp_path):
    runtime, broken = runtime_with_accounts(tmp_path)
    providers = Providers(runtime)
    providers.preflight()
    initial = runtime.accounts.status()["incidents"]
    providers.preflight()
    assert runtime.accounts.status()["incidents"] == initial
    broken.write_text(json.dumps({"OPENAI_API_KEY": "test-only"}))
    broken.chmod(0o600)
    assert providers.preflight()["failures"] == []
    assert runtime.accounts.status()["incidents"] == initial
    runtime.accounts.clear(OWNER, initial[0]["incident_id"])
    assert runtime.accounts.status()["incidents"] == []


def test_dispatcher_reports_new_preflight_failures_once_and_reports_local_repair(
    tmp_path, caplog
):
    runtime, broken = runtime_with_accounts(tmp_path)
    dispatcher = SandboxDispatcher(
        runtime,
        object(),
        Providers(runtime),
        mcp_url="http://127.0.0.1:9000/mcp",
    )

    with caplog.at_level(logging.INFO, logger="firm_runtime.sandbox_dispatcher"):
        dispatcher._preflight()
        dispatcher._preflight()
        broken.write_text(json.dumps({"OPENAI_API_KEY": "test-only"}))
        broken.chmod(0o600)
        dispatcher._preflight()

    failures = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert failures == [
        "Provider preflight failed for account broken (credential_unreadable). "
        f"Run `firm accounts {tmp_path}` to inspect the hold and configured authentication "
        f"path; repair it, then run `firm clear-hold {tmp_path} INCIDENT_ID`."
    ]
    assert any("Provider preflight now passes" in record.message for record in caplog.records)


@pytest.mark.parametrize("data", ["{}", '{"tokens": {}}', '{"OPENAI_API_KEY": ""}'])
def test_codex_preflight_rejects_empty_authentication_without_echoing_secrets(tmp_path, data):
    path = tmp_path / "auth.json"
    path.write_text(data)
    path.chmod(0o600)
    adapter = CodexProvider(model="gpt-5.6-sol", effort="medium", auth_file=path)
    with pytest.raises(ProviderSetupError, match="no account token"):
        adapter.validate_credentials()


def test_claude_preflight_checks_exported_auth_shape_and_private_permissions(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text('{"not_a_credential":"test-only"}')
    path.chmod(0o600)
    adapter = ClaudeProvider(model="sonnet", effort="high", auth_file=path)
    with pytest.raises(ProviderSetupError, match="no OAuth token"):
        adapter.validate_credentials()
    path.write_text('{"claudeAiOauth":{"accessToken":"test-only"}}')
    adapter.validate_credentials()
    path.chmod(0o644)
    with pytest.raises(ProviderSetupError, match="not private"):
        adapter.validate_credentials()


def test_primary_preflight_config_failure_leaves_independent_fallback_eligible(
    tmp_path, monkeypatch
):
    runtime, _ = runtime_with_accounts(tmp_path)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["profiles"]["codex"]["accounts"] = ["good"]
        config["profiles"]["fallback"] = {**config["profiles"]["codex"], "model": "fallback"}
        config["profiles"]["codex"]["alternates"] = ["fallback"]
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    providers = Providers(runtime)
    original = providers_module.provider_adapter

    def adapter(profile, account):
        if profile["model"] != "fallback":
            raise ProviderSetupError(
                category="config", code="invalid_model", message="Invalid model"
            )
        return original(profile, account)

    monkeypatch.setattr(providers_module, "provider_adapter", adapter)
    report = providers.preflight()
    assert report["failures"] == [
        {"scope": "profile", "dependency": "codex", "code": "invalid_model"}
    ]
    runtime.start()
    invocation = runtime.admit()
    assert invocation is not None
    with runtime.store.read() as db:
        assert runtime.accounts.binding(db, invocation.session_id)["profile"] == "fallback"
        assert runtime.store.role(db, invocation.employee_id)["attempt_count"] == 0


def test_claude_summary_output_records_streamed_allowance_for_its_account(tmp_path):
    from firm_runtime.summaries import SummaryLaunch

    runtime, _ = runtime_with_accounts(tmp_path)
    runner = Providers(runtime).summary_runner(object(), mcp_url="http://127.0.0.1:9000/mcp")
    request = SummaryLaunch("summary-run", "derive", 30, account_id="good", provider="claude")
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {"seven_day": {"utilization": 0.54, "resetsAt": 1789858800}}
        },
        "uuid": "event",
    }
    runner.parse_output(request, json.dumps(event).encode(), 0)
    with runtime.store.read() as db:
        reading = runtime.accounts.subscription_reading(db, "good")
        assert db.execute("SELECT COUNT(*) FROM account_observations").fetchone()[0] == 0
    assert reading["windows"] == [
        {"window": "seven_day", "used_percent": 54.0, "resets_at": 1789858800}
    ]
