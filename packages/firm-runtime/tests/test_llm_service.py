"""Tests for [llm] configuration, LlmService admission/billing, and HTTP broker routes."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_agy_credentials import account as agy_account
from test_agy_provider import encode as agy_encode
from test_agy_provider import stream as agy_stream

from firm_runtime import accounts
from firm_runtime.errors import FirmError
from firm_runtime.llm_service import LEASE_TTL_SECONDS, LlmService
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store


def _setup_runtime(
    tmp_path,
    *,
    max_active=1,
    extra_accounts=None,
    llm_config=None,
    clock=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    claude_auth = tmp_path / "claude-auth"
    claude_auth.write_text("claude-oauth-token")
    claude_auth.chmod(0o600)

    codex_auth = tmp_path / "codex-auth.json"
    codex_auth.write_text('{"tokens":{"access_token":"codex-access"}}')
    codex_auth.chmod(0o600)

    accounts_map = {
        "claude_main": {
            "provider": "claude",
            "auth_file": str(claude_auth),
            "max_active": max_active,
        },
        "codex_main": {
            "provider": "codex",
            "auth_file": str(codex_auth),
            "max_active": max_active,
        },
    }
    if extra_accounts:
        accounts_map.update(extra_accounts)

    config = {
        "max_active": 4,
        "staffing": False,
        "founder_profile": "main",
        "profiles": {
            "main": {
                "provider": "claude",
                "model": "sonnet",
                "effort": "medium",
                "accounts": ["claude_main"],
            },
            "codex_profile": {
                "provider": "codex",
                "model": "gpt-6-luna",
                "effort": "low",
                "accounts": ["codex_main"],
            },
        },
        "accounts": accounts_map,
    }
    if llm_config is not None:
        config["llm"] = llm_config

    store = Store.scratch(tmp_path / "firm.sqlite3", **({"clock": clock} if clock else {}))
    runtime = Runtime(store)
    runtime.lifecycle.initialize("test", "Build helpful software.", config)
    return runtime


def _start_founder(runtime):
    runtime.start()
    founder = runtime.admit()
    assert founder is not None
    runtime.mark_started(founder.activation_id)
    return founder


def test_llm_config_defaults_validation_and_task_rejection(tmp_path):
    assert accounts.llm_settings({}) == {
        "enabled": True,
        "max_concurrent": 2,
        "timeout_seconds": 1800,
        "agy": {"model": "gemini-4-neon", "effort": "medium"},
        "codex": {"model": "gpt-6.2-sol", "effort": "medium"},
        "claude": {"model": "opus", "effort": "medium"},
    }
    with pytest.raises(FirmError, match="replaced by \\[llm\\]"):
        accounts.validate_accounts({"task": {"max_concurrent": 1}})
    with pytest.raises(FirmError, match="llm.enabled must be a boolean"):
        accounts.llm_settings({"enabled": "yes"})
    with pytest.raises(FirmError, match="between 1 and 86400"):
        accounts.llm_settings({"timeout_seconds": 0})
    with pytest.raises(FirmError, match="between 1 and 86400"):
        accounts.llm_settings({"timeout_seconds": 86401})
    with pytest.raises(FirmError, match="unsupported llm effort"):
        accounts.llm_settings({"claude": {"effort": "ultra"}})

    agy_file = agy_account(tmp_path, expiry=4102444800)
    runtime = _setup_runtime(
        tmp_path,
        extra_accounts={
            "agy_main": {"provider": "agy", "auth_file": str(agy_file), "max_active": 1}
        },
    )
    with runtime.store.read() as db:
        cfg = runtime.accounts.config(db)
    assert LlmService.enabled_clis(cfg) == ["claude", "codex", "agy"]
    assert LlmService.enabled_clis({**cfg, "llm": {**cfg["llm"], "enabled": False}}) == []


def test_slot_borrowing_cross_provider_and_multi_account_capacity(tmp_path):
    runtime = _setup_runtime(tmp_path, max_active=1)
    founder = _start_founder(runtime)

    # First call on role's bound account (claude_main, max_active=1) borrows caller's slot.
    first = runtime.llm_service.start(founder.token, "claude")
    assert first["status"] == "granted"
    assert first["model"] == "opus"
    assert first["effort"] == "medium"

    # Second concurrent same-account call on max_active=1 hits capacity (cannot borrow twice).
    second = runtime.llm_service.start(founder.token, "claude")
    assert second == {"status": "unavailable", "reason": "capacity"}

    # Cross-provider call uses codex_main's own slot; caller does not hold codex_main.
    cross = runtime.llm_service.start(founder.token, "codex")
    assert cross["status"] == "granted"
    assert cross["model"] == "gpt-6.2-sol"

    # Third call hits per-role max_concurrent=2 (1 claude + 1 codex already running).
    busy = runtime.llm_service.start(founder.token, "codex")
    assert busy == {"status": "unavailable", "reason": "busy", "max_concurrent": 2}

    # Finish codex call, verify max_active=2 allows two concurrent same-account claude calls.
    runtime.llm_service.finish(cross["run_token"], exit_code=0, logs="", reason="cancelled")
    runtime.llm_service.finish(first["run_token"], exit_code=0, logs="", reason="cancelled")

    runtime2 = _setup_runtime(tmp_path / "cap2", max_active=2)
    f2 = _start_founder(runtime2)
    g1 = runtime2.llm_service.start(f2.token, "claude")
    g2 = runtime2.llm_service.start(f2.token, "claude")
    assert g1["status"] == "granted" and g2["status"] == "granted"
    assert runtime2.llm_service.start(f2.token, "claude") == {
        "status": "unavailable",
        "reason": "busy",
        "max_concurrent": 2,
    }


def test_start_refusal_reasons_disabled_unconfigured_hold_stopping_and_effort(tmp_path):
    runtime = _setup_runtime(tmp_path, max_active=2)
    founder = _start_founder(runtime)

    # Unconfigured provider reports configured CLIs.
    assert runtime.llm_service.start(founder.token, "agy") == {
        "status": "unavailable",
        "reason": "provider_not_configured",
        "configured": ["claude", "codex"],
    }

    # Unsupported effort reports supported efforts in order.
    bad_effort = runtime.llm_service.start(founder.token, "claude", effort="ultra")
    assert bad_effort == {
        "status": "unavailable",
        "reason": "invalid_effort",
        "supported": ["low", "medium", "high", "xhigh", "max"],
    }

    # Account hold blocks that provider.
    runtime.accounts.pause(OWNER, "account", "codex_main", "quota", until=9e12)
    assert runtime.llm_service.start(founder.token, "codex") == {
        "status": "unavailable",
        "reason": "hold",
    }

    # Disabled in config.
    with runtime.store.transaction() as db:
        cfg = runtime.accounts.config(db)
        cfg["llm"]["enabled"] = False
        db.execute("UPDATE firm SET config_json=?", (json.dumps(cfg),))
    assert runtime.llm_service.start(founder.token, "claude") == {
        "status": "unavailable",
        "reason": "llm_disabled",
    }

    # Draining returns firm_stopping.
    runtime.stop()
    assert runtime.llm_service.start(founder.token, "claude") == {
        "status": "unavailable",
        "reason": "firm_stopping",
    }


def test_finish_token_accounting_differs_between_fresh_and_resumed_runs(tmp_path):
    runtime = _setup_runtime(tmp_path, max_active=2)
    founder = _start_founder(runtime)

    # Interrupted Codex rollout with session total_token_usage only (no turn.completed).
    partial_rollout = "\n".join(
        [
            json.dumps(
                {"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}
            ),
            json.dumps(
                {
                    "type": "firm.telemetry",
                    "provider": "codex",
                    "model": "gpt-6.1-sol",
                    "usage": {
                        "last_token_usage": {"total_tokens": 50},
                        "total_token_usage": {"input_tokens": 500, "output_tokens": 80},
                    },
                }
            ),
        ]
    )

    fresh_grant = runtime.llm_service.start(founder.token, "codex", resumed=False)
    fresh_res = runtime.llm_service.finish(
        fresh_grant["run_token"],
        exit_code=0,
        logs=partial_rollout,
        reason="timeout",
        resumed=False,
    )
    assert fresh_res["reason"] == "timeout"

    resumed_grant = runtime.llm_service.start(founder.token, "codex", resumed=True)
    resumed_res = runtime.llm_service.finish(
        resumed_grant["run_token"],
        exit_code=0,
        logs=partial_rollout,
        reason="timeout",
        resumed=True,
    )
    assert resumed_res["reason"] == "timeout"

    with runtime.store.read() as db:
        fresh_row = dict(
            db.execute(
                "SELECT * FROM summary_runs WHERE run_id=?", (fresh_grant["run_id"],)
            ).fetchone()
        )
        resumed_row = dict(
            db.execute(
                "SELECT * FROM summary_runs WHERE run_id=?", (resumed_grant["run_id"],)
            ).fetchone()
        )
    # Fresh run fell back from None turn counters to session totals; resumed run did NOT.
    assert fresh_row["input_tokens"] == 500 and fresh_row["output_tokens"] == 80
    assert resumed_row["input_tokens"] is None and resumed_row["output_tokens"] is None


def test_lease_expiry_runtime_finish_cleanup_and_controls_notice(tmp_path):
    now = [1000.0]
    runtime = _setup_runtime(tmp_path, max_active=1, clock=lambda: now[0])
    founder = _start_founder(runtime)

    g1 = runtime.llm_service.start(founder.token, "claude")
    assert g1["status"] == "granted"

    # Extend lease with heartbeat before TTL expires.
    now[0] += 50.0
    hb = runtime.llm_service.heartbeat(g1["run_token"], progress=True)
    assert hb["status"] == "ok" and hb["remaining_seconds"] > 0

    # Let lease expire (>90s without heartbeat); next start sweeps it and grants again.
    now[0] += LEASE_TTL_SECONDS + 1.0
    g2 = runtime.llm_service.start(founder.token, "claude")
    assert g2["status"] == "granted"
    with runtime.store.read() as db:
        settled = db.execute(
            "SELECT state,reason FROM summary_runs WHERE run_id=?", (g1["run_id"],)
        ).fetchone()
    assert tuple(settled) == ("unavailable", "timeout")

    # Send owner mail while g2 is running; finish() surfaces waiting mail in controls_notice.
    runtime.owner.send_mail(OWNER, [founder.employee_id], "Check inbox", request_id="m-cue")
    res = runtime.llm_service.finish(
        g2["run_token"],
        exit_code=0,
        logs=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "22222222-2222-4222-8222-222222222222",
            }
        ),
    )
    assert res["status"] == "completed"
    assert res["session_id"] == "22222222-2222-4222-8222-222222222222"
    assert "New mail:" in res["controls_notice"] and "Owner" in res["controls_notice"]

    # Open CLI call is cancelled when parent activation finishes.
    g3 = runtime.llm_service.start(founder.token, "codex")
    assert g3["status"] == "granted"
    runtime.finish(founder.activation_id)
    with runtime.store.read() as db:
        row = db.execute(
            "SELECT state,reason FROM summary_runs WHERE run_id=?", (g3["run_id"],)
        ).fetchone()
    assert tuple(row) == ("unavailable", "cancelled")
    assert runtime.llm_service.heartbeat(g3["run_token"]) == {
        "status": "stop",
        "reason": "cancelled",
    }


def test_http_llm_endpoints_and_credentials_lease_gate(tmp_path):
    now = [1000.0]
    agy_file = agy_account(tmp_path, expiry=4102444800)
    runtime = _setup_runtime(
        tmp_path,
        max_active=2,
        extra_accounts={
            "agy_main": {"provider": "agy", "auth_file": str(agy_file), "max_active": 2}
        },
        clock=lambda: now[0],
    )
    founder = _start_founder(runtime)
    app = build_http_app(runtime, allowed_hosts=["firm.test"], allowed_origins=["http://firm.test"])

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://firm.test",
                headers={"Origin": "http://firm.test"},
            ) as client:
                # Bad bearer -> 401
                r_unauth = await client.post(
                    "/llm/start",
                    json={"provider": "agy"},
                    headers={"Authorization": "Bearer wrong"},
                )
                assert r_unauth.status_code == 401

                r_start = await client.post(
                    "/llm/start",
                    json={"provider": "agy", "effort": "high"},
                    headers={"Authorization": f"Bearer {founder.token}"},
                )
                assert r_start.status_code == 200
                grant = r_start.json()
                assert grant["status"] == "granted"
                run_token = grant["run_token"]

                # /credentials accepts run_token for live LLM lease.
                r_creds = await client.get(
                    "/credentials",
                    headers={"Authorization": f"Bearer {run_token}"},
                )
                assert r_creds.status_code == 200
                assert r_creds.json()["kind"] == "agy-access"

                # Past lease_until without heartbeat -> /credentials refuses.
                now[0] += LEASE_TTL_SECONDS + 5.0
                r_stale = await client.get(
                    "/credentials",
                    headers={"Authorization": f"Bearer {run_token}"},
                )
                assert r_stale.status_code == 401

                # Heartbeat fails once deadline wasn't exceeded? Wait: lease expired only in
                # query check above, heartbeat still checks parent live and refreshes lease.
                r_hb = await client.post(
                    "/llm/heartbeat",
                    json={"progress": False},
                    headers={"Authorization": f"Bearer {run_token}"},
                )
                assert r_hb.status_code == 200 and r_hb.json()["status"] == "ok"

                logs_bytes = agy_encode(agy_stream(usage={"input_tokens": 5, "output_tokens": 2}))
                r_finish = await client.post(
                    "/llm/finish",
                    json={
                        "exit_code": 0,
                        "logs": logs_bytes,
                        "reason": None,
                        "resumed": False,
                    },
                    headers={"Authorization": f"Bearer {run_token}"},
                )
                assert r_finish.status_code == 200
                assert r_finish.json()["status"] == "completed"

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


def test_expired_llm_lease_does_not_block_admit(tmp_path):
    now = [1000.0]
    runtime = _setup_runtime(tmp_path, max_active=1, clock=lambda: now[0])
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["default_profile"] = "codex_profile"
    runtime.owner.configure(OWNER, config)

    founder = _start_founder(runtime)
    hired = runtime.tools(founder.token).hire(
        "codex-worker", "Handle codex work.", "hire-codex-worker"
    )
    # Founder uses claude_main (max_active=1); now start a codex CLI run taking codex_main's slot.
    grant = runtime.llm_service.start(founder.token, "codex")
    assert grant["status"] == "granted"
    # While lease is live, codex worker cannot admit because codex_main (max_active=1) is busy.
    assert runtime.admit() is None

    # Advance clock past LEASE_TTL_SECONDS without heartbeat; admit() sweeps and admits worker.
    now[0] += LEASE_TTL_SECONDS + 1.0
    next_call = runtime.admit()
    assert next_call is not None and next_call.employee_id == hired["employee_id"]
    with runtime.store.read() as db:
        row = db.execute(
            "SELECT state,reason FROM summary_runs WHERE run_id=?", (grant["run_id"],)
        ).fetchone()
    assert tuple(row) == ("unavailable", "timeout")

