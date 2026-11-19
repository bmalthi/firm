"""Literal behavior guarantees for the built-in provider configuration defaults."""

import copy
import json
import tomllib
from dataclasses import FrozenInstanceError

import pytest

from firm_runtime import accounts, levels, onboarding
from firm_runtime.agy_provider import AgyProvider, native_model_id
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_runtime.errors import FirmError
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.provider_definitions import (
    PROVIDER_DEFINITIONS,
    PROVIDER_NAMES,
    effective_model,
    resolve_model,
)
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.telemetry import context_policy


@pytest.mark.parametrize(
    "provider,initial,summary,llm,staffing,window,efforts",
    [
        (
            "claude", ("sonnet", "medium"), ("sonnet", "medium"), ("opus", "medium"),
            [("sonnet", "high"), ("opus", "high"), ("fable", "high"),
             ("sonnet", "medium")],
            500_000, {"low", "medium", "high", "xhigh", "max"},
        ),
        (
            "codex", ("gpt-6.1-terra", "low"), ("gpt-6.1-terra", "medium"),
            ("gpt-6.2-sol", "medium"),
            [("gpt-6.1-terra", "high"), ("gpt-6.2-sol", "high"), ("gpt-6.2-astra", "medium"),
             ("gpt-6.1-terra", "medium")],
            256_000, {"low", "medium", "high", "xhigh", "max", "ultra"},
        ),
        (
            "agy", ("gemini-4-neon", "medium"), ("gemini-4-hydrogen", "medium"),
            ("gemini-4-neon", "medium"),
            [("gemini-4-hydrogen", "medium"), ("gemini-4-neon", "medium"),
             ("gemini-4-neon", "high"), ("gemini-4-hydrogen", "low")],
            500_000, {"low", "medium", "high", "xhigh"},
        ),
    ],
)
def test_literal_defaults_reach_each_configuration_consumer(
    tmp_path, provider, initial, summary, llm, staffing, window, efforts
):
    generated = onboarding.generated_files(tmp_path, provider, "Build a firm.")
    config = tomllib.loads(generated[tmp_path / "firm.toml"])
    profile = config["profiles"][provider]
    assert (profile["model"], profile["effort"]) == initial
    original = copy.deepcopy(config)
    expanded = levels.with_staffing_defaults(config)
    assert config == original
    assert [
        (expanded["profiles"][f"staffing-{level}"]["model"],
         expanded["profiles"][f"staffing-{level}"]["effort"])
        for level in ("low", "medium", "high", "chooser")
    ] == staffing
    assert context_policy(profile) == (window, 0.6, 10_000)
    assert context_policy({**profile, "context_window": 123_456})[0] == 123_456
    assert context_policy({**profile, "context_window": None})[0] is None
    config["staffing"] = False
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build.", config)
    runtime.start()
    invocation = runtime.admit()
    with runtime.store.transaction() as db:
        reservation = runtime.accounts.reserve_summary(
            db, "summary-defaults", caller_session_id=invocation.session_id
        )
        assert (reservation["model"], reservation["effort"]) == summary
        settings = runtime.accounts.config(db)["llm"][provider]
        assert (settings["model"], settings["effort"]) == llm
    adapter = {"claude": ClaudeProvider, "codex": CodexProvider, "agy": AgyProvider}[provider]
    for effort in efforts:
        assert adapter(model=initial[0], effort=effort, auth_file=tmp_path / "unused")
        selected = copy.deepcopy(config)
        selected["profiles"][provider]["effort"] = effort
        accounts.validate_accounts(selected)
    with pytest.raises(ProviderSetupError) as error:
        adapter(model=initial[0], effort="invalid", auth_file=tmp_path / "unused")
    assert error.value.code == "invalid_effort"
    assert PROVIDER_DEFINITIONS[provider].supported_efforts == efforts


def test_global_llm_limits_and_partial_overrides_remain_unchanged():
    expected = {
        "enabled": True,
        "claude": {"model": "opus", "effort": "medium"},
        "codex": {"model": "gpt-6.2-sol", "effort": "medium"},
        "agy": {"model": "gemini-4-neon", "effort": "medium"},
        "max_concurrent": 2,
        "timeout_seconds": 1800,
    }
    assert accounts.llm_settings({}) == expected
    override = {"codex": {"model": "owner-model"}, "agy": {"effort": "xhigh"}}
    before = copy.deepcopy(override)
    expected["codex"]["model"] = "owner-model"
    expected["agy"]["effort"] = "xhigh"
    assert accounts.llm_settings(override) == expected
    assert override == before


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("setting", [{"model": "owner-model"}, {"effort": "high"}])
def test_explicit_summary_requires_both_model_and_effort(provider, setting):
    with pytest.raises(FirmError):
        accounts.validate_accounts({"summary": {provider: setting}})


def test_removed_google_cloud_section_is_rejected_with_clear_message(tmp_path):
    generated = onboarding.generated_files(tmp_path, "claude", "Build.")
    config = tomllib.loads(generated[tmp_path / "firm.toml"])
    accounts.validate_accounts(config)
    config["google_cloud"] = {"project": "synthetic"}
    with pytest.raises(
        FirmError, match=r"\[google_cloud\] has been removed"
    ):
        accounts.validate_accounts(config)


def test_definitions_are_immutable_and_model_validation_remains_native(tmp_path):
    assert PROVIDER_NAMES == ("claude", "codex", "agy")
    with pytest.raises(TypeError):
        PROVIDER_DEFINITIONS["new"] = PROVIDER_DEFINITIONS["claude"]
    definition = PROVIDER_DEFINITIONS["claude"]
    with pytest.raises(FrozenInstanceError):
        definition.name = "new"
    with pytest.raises(FrozenInstanceError):
        definition.summary.model = "new"
    for adapter in (ClaudeProvider, CodexProvider):
        assert adapter(model="owner-future-model", effort="high", auth_file=tmp_path / "auth")
    with pytest.raises(ProviderSetupError) as error:
        AgyProvider(model="owner-future-model", effort="high", auth_file=tmp_path / "auth")
    assert error.value.code == "invalid_model"
    assert native_model_id("gemini-4-hydrogen", "xhigh") == "gemini-4-carbon-high"
    assert context_policy({}) == (None, 0.6, 10_000)


def test_class_and_family_aliases_route_at_execution_boundaries(tmp_path):
    assert resolve_model("claude", "high") == "fable"
    assert resolve_model("codex", "terra") == "gpt-6.1-terra"
    assert resolve_model("codex", "sol") == "gpt-6.2-sol"
    assert resolve_model("codex", "astra") == "gpt-6.2-astra"
    assert resolve_model("codex", "medium") == "gpt-6.2-sol"
    assert resolve_model("agy", "hydrogen") == "gemini-4-hydrogen"
    assert resolve_model("agy", "neon") == "gemini-4-neon"
    assert resolve_model("agy", "carbon") == "gemini-4-carbon"
    assert resolve_model("agy", "medium") == "gemini-4-neon"
    assert resolve_model("agy", "high") == "gemini-4-neon"
    assert effective_model("agy", "neon", "medium") == "gemini-4-neon"
    assert effective_model("agy", "neon", "xhigh") == "gemini-4-carbon"

    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"tok"}}')
    auth.chmod(0o600)
    assert ClaudeProvider(model="high", effort="high", auth_file=auth).model == "fable"
    assert CodexProvider(model="astra", effort="high", auth_file=auth).model == "gpt-6.2-astra"
    assert AgyProvider(model="neon", effort="medium", auth_file=auth).model == "gemini-4-neon"

    config = {
        "max_active": 2,
        "staffing": False,
        "founder_profile": "main",
        "accounts": {
            "codex_acct": {"provider": "codex", "auth_file": str(auth), "max_active": 2},
        },
        "profiles": {
            "main": {
                "provider": "codex",
                "model": "sol",
                "effort": "medium",
                "accounts": ["codex_acct"],
            }
        },
        "summary": {"codex": {"model": "low", "effort": "medium"}},
        "llm": {"codex": {"model": "astra", "effort": "high"}},
    }
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build.", config)
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    with runtime.store.transaction() as db:
        bound = runtime.accounts.binding(db, invocation.session_id)
        assert bound is not None
        assert json.loads(bound["settings_json"])["model"] == "gpt-6.2-sol"
        reservation = runtime.accounts.reserve_summary(
            db, "sum-alias", caller_session_id=invocation.session_id
        )
        assert reservation["model"] == "gpt-6.1-terra"
    llm = runtime.llm_service.start(invocation.token, "codex")
    assert llm["status"] == "granted" and llm["model"] == "gpt-6.2-astra"
