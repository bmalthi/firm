"""Offline unit tests for tools/acceptance/llm_cli.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from firm_sandbox import RunSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
import llm_cli as llm_cli_acceptance  # noqa: E402


def _source(provider="claude", max_active=2):
    return {
        "profiles": {
            "p": {
                "provider": provider,
                "model": "opus" if provider == "claude" else "gpt-6.1-sol",
                "effort": "medium",
                "accounts": ["acct"],
            }
        },
        "accounts": {
            "acct": {"provider": provider, "auth_file": "/private/auth", "max_active": max_active}
        },
        "llm": {
            provider: {"model": "opus" if provider == "claude" else "gpt-6.1-sol", "effort": "high"}
        },
    }


def test_argument_parser_and_program_compile():
    args = llm_cli_acceptance.argument_parser().parse_args(
        [
            "--root",
            "/tmp/firm",
            "--evidence",
            "/tmp/evidence",
            "--provider-config",
            "/tmp/providers.toml",
            "--profile",
            "p",
            "--provider",
            "codex",
            "--timeout",
            "90",
        ]
    )
    assert args.provider == "codex"
    assert args.timeout == 90
    compile(llm_cli_acceptance.PROGRAM, "llm-cli-probe-program", "exec")


def test_build_firm_config_uses_llm_section_and_validates_timeout():
    from firm_runtime.lifecycle import validate_config

    cfg = llm_cli_acceptance.build_firm_config(_source("claude"), "p", "claude", timeout=60)
    validated = validate_config(cfg)
    assert validated["llm"]["enabled"] is True
    assert validated["llm"]["timeout_seconds"] == 60
    assert validated["llm"]["claude"] == {"model": "opus", "effort": "high"}

    with pytest.raises(ValueError, match="at least 30 seconds"):
        llm_cli_acceptance.build_firm_config(_source("claude"), "p", "claude", timeout=10)


def test_probe_provider_build_spec_keeps_activation_token_secret():
    layout = SimpleNamespace(
        python="/usr/bin/python3",
        state="/tmp/state",
        private="/home/role",
        credential=lambda name: f"/run/credentials/{name}",
    )
    probe = llm_cli_acceptance.ProbeProvider("claude", "secret-nonce", 120)
    spec = probe.build_spec(
        SimpleNamespace(
            role_id="r-founder",
            run_id="run-1",
            mcp_url="http://127.0.0.1:9100/mcp",
            invocation=SimpleNamespace(token="secret-activation-token"),
        ),
        layout=layout,
    )
    assert isinstance(spec, RunSpec)
    assert spec.argv == ("/usr/bin/python3", "/run/credentials/probe-program")
    assert spec.env["FIRM_LLM_URL"] == "http://127.0.0.1:9100/llm"
    assert spec.secret_env == {"FIRM_ACTIVATION_TOKEN": "activation-token"}
    assert "secret-activation-token" not in json.dumps(spec.env)
    assert "llm-cli" in spec.secrets and "llm-instructions" in spec.secrets


def test_sanitize_evidence_omits_secrets_and_validates_all_checks():
    runs = [
        {
            "run_id": "llm-secret-1",
            "kind": "llm",
            "provider": "claude",
            "state": "summarized",
            "reason": None,
            "input_tokens": 10,
            "output_tokens": 4,
            "latency_ms": 200,
        },
        {
            "run_id": "llm-secret-2",
            "kind": "llm",
            "provider": "claude",
            "state": "summarized",
            "reason": None,
            "input_tokens": 12,
            "output_tokens": 6,
            "latency_ms": 250,
        },
    ]
    probe = {
        "probe": "llm_cli",
        "fresh_ok": True,
        "recalled": True,
        "session_present": True,
        "concurrent_succeeded": True,
        "concurrent_refused": True,
        "cleanup_clean": True,
        "session_persisted": True,
    }
    ev = llm_cli_acceptance.sanitize_evidence("claude", runs, probe)
    assert ev["passed"] is True
    assert "llm-secret-1" not in json.dumps(ev)

    bad = llm_cli_acceptance.sanitize_evidence(
        "claude", runs[:1], {**probe, "concurrent_refused": False}
    )
    assert bad["passed"] is False
