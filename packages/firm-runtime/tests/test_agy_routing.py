"""AGY account, staffing, parser dispatch and unavailable telemetry integration."""

import copy
import json

import pytest
from test_agy_credentials import account
from test_agy_provider import encode, stream

from firm_runtime import accounts, levels
from firm_runtime.agy_provider import AgyProvider
from firm_runtime.errors import FirmError
from firm_runtime.providers import Providers, provider_adapter, record_task_output
from firm_runtime.quota_observer import QuotaObserver
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.summaries import SummaryLaunch
from firm_runtime.telemetry import context_policy


def configuration(path):
    return {
        "max_active": 3,
        "staffing": False,
        "founder_profile": "main",
        "accounts": {"subscription": {"provider": "agy", "auth_file": str(path), "max_active": 3}},
        "profiles": {
            "main": {
                "provider": "agy",
                "model": "gemini-3.8-flash",
                "effort": "medium",
                "accounts": ["subscription"],
            }
        },
    }


def runtime_for(tmp_path, *, staffing=False):
    config = configuration(account(tmp_path))
    if staffing:
        config.pop("staffing")
    runtime = Runtime(Store.scratch(tmp_path / "runtime.sqlite3"))
    runtime.lifecycle.initialize("test", "Synthetic work.", config)
    return runtime


def test_defaults_and_factory_keep_model_effort_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path)
    config = configuration("unused")
    config["accounts"]["subscription"].pop("auth_file")
    accounts.validate_accounts(config)
    path = config["accounts"]["subscription"]["auth_file"]
    assert path.endswith("/subscription-agy/home/.gemini/antigravity-cli/antigravity-oauth-token")
    assert accounts.SUMMARY_DEFAULTS["agy"] == {"model": "gemini-3.8-flash", "effort": "medium"}
    assert config["task"]["agy"] == {"model": "gemini-3.8-flash", "effort": "high"}
    assert isinstance(
        provider_adapter(config["profiles"]["main"], config["accounts"]["subscription"]),
        AgyProvider,
    )
    assert context_policy(config["profiles"]["main"])[0] is None


@pytest.mark.parametrize("target", ["account", "profile"])
def test_gemini_alias_stays_rejected(tmp_path, target):
    config = configuration(tmp_path / "auth")
    if target == "account":
        config["accounts"]["subscription"]["provider"] = "gemini"
    else:
        config["profiles"]["main"]["provider"] = "gemini"
    with pytest.raises(FirmError, match="provider"):
        accounts.validate_accounts(config)


@pytest.mark.parametrize("target", ["profile", "task", "summary"])
def test_configuration_accepts_owner_xhigh_mapping(tmp_path, target):
    config = configuration(tmp_path / "auth")
    if target == "profile":
        config["profiles"]["main"]["effort"] = "xhigh"
    else:
        config[target] = {"agy": {"model": "gemini-4-argon", "effort": "xhigh"}}
    accounts.validate_accounts(config)


def test_google_cloud_is_rejected_even_if_agy_only_in_alternate(tmp_path):
    config = configuration(tmp_path / "auth")
    config["accounts"]["codex"] = {"provider": "codex", "max_active": 1}
    config["profiles"]["primary"] = {
        "provider": "codex",
        "model": "test",
        "effort": "high",
        "accounts": ["codex"],
        "alternates": ["main"],
    }
    config["founder_profile"] = "primary"
    config["google_cloud"] = {"project": "synthetic"}
    with pytest.raises(FirmError, match="cannot coexist"):
        accounts.validate_accounts(config)


def test_owner_staffing_model_defaults(tmp_path):
    config = configuration(tmp_path / "auth")
    config.pop("staffing")
    result = levels.with_staffing_defaults(config)
    assert [
        (
            result["profiles"][f"staffing-{name}"]["model"],
            result["profiles"][f"staffing-{name}"]["effort"],
        )
        for name in ("low", "medium", "high", "chooser")
    ] == [
        ("gemini-3.8-flash", "medium"),
        ("gemini-3.8-flash", "high"),
        ("gemini-4-argon", "medium"),
        ("gemini-3.8-flash", "medium"),
    ]
    result.pop("founder_profile")
    assert levels.founder_execution(result) == ("staffing-high", "high")


def test_xhigh_chooser_pick_routes_to_owner_selected_native_variant(tmp_path):
    from firm_runtime.agy_provider import native_model_id

    runtime = runtime_for(tmp_path, staffing=True)
    employee = runtime.lifecycle.hire("founder", "reviewer", "Synthetic review.", request_id="h")[
        "employee_id"
    ]
    runtime.levels.chooser = lambda _: {
        "model_class": "low",
        "effort": "xhigh",
        "reason": "Synthetic choice.",
    }
    runtime.start()
    assert runtime.levels.tick(background=False) == [employee]
    with runtime.store.read() as db:
        record = runtime.levels.record(db, employee)
        assert record["state"] == "chosen" and record["effort"] == "xhigh"
        role = runtime.store.role(db, employee)
        profile = runtime.accounts.config(db)["profiles"][role["execution_profile"]]
        assert profile["model"] == "gemini-3.8-flash"
        assert native_model_id(profile["model"], role["effort"]) == "gemini-4-argon-high"


def test_retained_account_and_model_survive_config_edits_and_collision_guard(tmp_path):
    runtime = runtime_for(tmp_path)
    runtime.start()
    invocation = runtime.admit()
    assert invocation is not None
    providers = Providers(runtime)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["profiles"]["main"]["model"] = "gemini-4-argon"
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    adapter = providers.for_session(invocation.session_id)
    assert adapter.model == "gemini-3.8-flash"
    assert adapter.auth_file == account(tmp_path)
    with runtime.store.transaction() as db:
        config["google_cloud"] = {"project": "synthetic"}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    assert providers.for_session(invocation.session_id).name == "agy"


def test_both_parser_dispatch_maps_use_captured_agy_provider(tmp_path):
    runtime = runtime_for(tmp_path)
    providers = Providers(runtime)
    runner = providers.summary_runner(object(), mcp_url="http://localhost:8421/mcp")
    request = SummaryLaunch(
        "summary-run", "synthetic", 30, provider="agy", account_id="subscription"
    )
    logs = encode(stream(usage={"input_tokens": 100, "output_tokens": 20}))
    result = runner.parse_output(request, logs, 0)
    assert result.text == "FIRM_OK" and result.usage["input_tokens"] == 100
    assert (
        record_task_output(
            runtime, "task-synthetic", "agy", "subscription", logs, 0, apply_holds=False
        )
        == "FIRM_OK"
    )
    with runtime.store.read() as db:
        assert runtime.accounts.subscription_reading(db, "subscription") is None


def test_quota_unsupported_never_polls_or_uses_stale_stream():
    observer = QuotaObserver(
        lambda: {"subscription": {"provider": "agy"}},
        fetchers={"agy": lambda _: pytest.fail("AGY quota must not be polled")},
    )
    expected = {"state": "unsupported", "windows": [], "polled": False}
    assert observer.snapshot("subscription") == expected
    observer.refresh()
    assert observer.snapshot("subscription", {"observed_at": 100, "windows": []}) == expected


def test_validation_does_not_mutate_callers_staffing_input(tmp_path):
    config = configuration(tmp_path / "auth")
    config.pop("staffing")
    original = copy.deepcopy(config)
    levels.with_staffing_defaults(config)
    assert config == original


def test_console_shows_reported_native_variant_separately_from_configured_base(tmp_path):
    from test_console import HOST, _login

    from firm_runtime.agy_provider import parse_usage
    from firm_runtime.console import build_console_app

    runtime = runtime_for(tmp_path)
    runtime.start()
    invocation = runtime.admit()
    events = stream()
    events[0]["init"]["model"] = "gemini-4-argon-high"
    runtime.record_usage(invocation.activation_id, parse_usage(encode(events)))
    app = build_console_app(runtime, allowed_host=HOST)
    with _login(app) as client:
        roles = client.get("/api/snapshot").json()["roles"]
    founder = next(role for role in roles if role["employee_id"] == "founder")
    assert founder["execution"]["model"] == "gemini-3.8-flash"
    assert founder["execution"]["resolved_model"] == "gemini-4-argon-high"


def test_transient_build_failure_retries_without_shared_hold(tmp_path, monkeypatch):
    from runtime_test_support import FakeSandbox

    from firm_runtime.provider_contract import ProviderSetupError
    from firm_runtime.sandbox_dispatcher import SandboxDispatcher

    runtime = runtime_for(tmp_path)
    box = FakeSandbox(tmp_path / "sandbox")
    def unavailable(*args, **kwargs):
        raise ProviderSetupError(category="transient", code="credential_lock_timeout",
                                 message="synthetic outage")
    monkeypatch.setattr(AgyProvider, "build_spec", unavailable)
    with SandboxDispatcher(runtime, box, Providers(runtime),
                           mcp_url="http://127.0.0.1:9000/mcp") as dispatcher:
        dispatcher.start()
        assert dispatcher.tick() == []
        assert runtime.accounts.status()["incidents"] == []
        with runtime.store.read() as db:
            activation = db.execute("SELECT * FROM activations").fetchone()
            assert activation["state"] == "interrupted"
            assert activation["failure_category"] == "transient"
            role = runtime.store.role(db, "founder")
            assert role["attempt_count"] == 1
            assert role["retry_at"] > runtime.store.clock()


@pytest.mark.parametrize("code", ["resume_state_missing", "settings_invalid", "token_headroom"])
def test_local_task_failure_never_creates_account_hold(tmp_path, code):
    from firm_runtime.task_runner import TaskExecutionError

    runtime = runtime_for(tmp_path)
    logs = encode([{"type": "firm.launch_failed", "kind": "launcher_setup", "code": code}])
    with pytest.raises(TaskExecutionError):
        record_task_output(runtime, "synthetic", "agy", "subscription", logs, 71,
                           apply_holds=True)
    assert runtime.accounts.status()["incidents"] == []
