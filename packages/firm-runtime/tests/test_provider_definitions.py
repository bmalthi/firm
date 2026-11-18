"""Literal behavior guarantees for the built-in provider configuration defaults."""

import copy
import tomllib
from dataclasses import FrozenInstanceError

import pytest

from firm_runtime import accounts, levels, onboarding
from firm_runtime.agy_provider import AgyProvider, native_model_id
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_runtime.errors import FirmError
from firm_runtime.provider_contract import ProviderSetupError
from firm_runtime.provider_definitions import PROVIDER_DEFINITIONS, PROVIDER_NAMES
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.telemetry import context_policy


@pytest.mark.parametrize(
    "provider,initial,summary,task,staffing,window,efforts",
    [
        (
            "claude", ("sonnet", "medium"), ("sonnet", "medium"), ("opus", "medium"),
            [("sonnet", "high"), ("opus", "high"), ("claude-fable-5-1", "high"),
             ("sonnet", "medium")],
            500_000, {"low", "medium", "high", "xhigh", "max"},
        ),
        (
            "codex", ("gpt-6-luna", "low"), ("gpt-6-luna", "medium"),
            ("gpt-6.1-sol", "medium"),
            [("gpt-6-luna", "high"), ("gpt-6.1-sol", "high"), ("gpt-6-astra", "medium"),
             ("gpt-6-luna", "medium")],
            256_000, {"low", "medium", "high", "xhigh", "max", "ultra"},
        ),
        (
            "agy", ("gemini-4-hydrogen", "medium"), ("gemini-4-hydrogen", "medium"),
            ("gemini-4-hydrogen", "high"),
            [("gemini-4-hydrogen", "medium"), ("gemini-4-hydrogen", "high"),
             ("gemini-4-argon", "medium"), ("gemini-4-hydrogen", "low")],
            500_000, {"low", "medium", "high", "xhigh"},
        ),
    ],
)
def test_literal_defaults_reach_each_configuration_consumer(
    tmp_path, provider, initial, summary, task, staffing, window, efforts
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
        settings = runtime.accounts.config(db)["task"][provider]
        assert (settings["model"], settings["effort"]) == task
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


def test_global_task_limits_and_partial_overrides_remain_unchanged():
    expected = {
        "claude": {"model": "opus", "effort": "medium"},
        "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
        "agy": {"model": "gemini-4-hydrogen", "effort": "high"},
        "max_concurrent": 2,
        "timeout_seconds": 600,
        "max_instruction_bytes": 16384,
        "max_answer_bytes": 16384,
    }
    assert accounts.task_settings({}) == expected
    override = {"codex": {"model": "owner-model"}, "agy": {"effort": "xhigh"}}
    before = copy.deepcopy(override)
    expected["codex"]["model"] = "owner-model"
    expected["agy"]["effort"] = "xhigh"
    assert accounts.task_settings(override) == expected
    assert override == before


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("setting", [{"model": "owner-model"}, {"effort": "high"}])
def test_explicit_summary_requires_both_model_and_effort(provider, setting):
    with pytest.raises(FirmError):
        accounts.validate_accounts({"summary": {provider: setting}})


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("target", ["accounts", "profiles"])
def test_service_constraint_rejects_agy_in_either_configured_collection(
    tmp_path, provider, target
):
    generated = onboarding.generated_files(tmp_path, provider, "Build.")
    config = tomllib.loads(generated[tmp_path / "firm.toml"])
    config["google_cloud"] = {"project": "synthetic"}
    accounts.validate_accounts(config)  # Both other providers still support this service.
    config[target]["unused-agy"] = {"provider": "agy"}
    with pytest.raises(
        FirmError, match=r"AGY subscription inference cannot coexist with \[google_cloud\]"
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
    assert native_model_id("gemini-4-hydrogen", "xhigh") == "gemini-4-argon-high"
    assert context_policy({}) == (None, 0.6, 10_000)
