import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store

BASE_URL = "http://firm.test"
ALLOWED = {
    "allowed_hosts": ["firm.test"],
    "allowed_origins": [BASE_URL],
}


def _runtime(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )["founder"]["employee_id"]
    runtime.start()
    founder_run = runtime.admit()
    runtime.mark_started(founder_run.activation_id)
    analyst = runtime.tools(founder_run.token).hire("analyst", "Own analysis.", "hire-analyst")[
        "employee_id"
    ]
    analyst_run = runtime.admit()
    runtime.mark_started(analyst_run.activation_id)
    return runtime, founder, founder_run, analyst, analyst_run


@asynccontextmanager
async def _session(app, token):
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}", "Origin": BASE_URL}
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL, headers=headers) as client:
        async with streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def _result(result):
    assert not result.isError
    assert len(result.content) == 1
    return result.content[0].text


def test_streamable_http_keeps_concurrent_roles_separate_and_exposes_no_owner_tools(tmp_path):
    runtime, founder, founder_run, analyst, analyst_run = _runtime(tmp_path)
    runtime.messaging.send(
        OWNER, [founder], "founder body", subject="Message only for founder", request_id="f"
    )
    runtime.messaging.send(
        OWNER, [analyst], "analyst body", subject="Message only for analyst", request_id="w"
    )
    app = build_http_app(runtime, **ALLOWED)

    async def peek(token):
        async with _session(app, token) as session:
            tools = await session.list_tools()
            result = _result(await session.call_tool("peek", {}))
            return {tool.name for tool in tools.tools}, result

    async def check():
        async with app.router.lifespan_context(app):
            founder_result, analyst_result = await asyncio.gather(
                peek(founder_run.token), peek(analyst_run.token)
            )
        founder_tools, founder_inbox = founder_result
        analyst_tools, analyst_inbox = analyst_result
        assert founder_tools == analyst_tools
        assert not {"start", "stop", "status", "owner_send"} & founder_tools
        assert "Message only for founder" in founder_inbox
        assert "Message only for analyst" not in founder_inbox
        assert "Message only for analyst" in analyst_inbox
        assert "Message only for founder" not in analyst_inbox

    asyncio.run(asyncio.wait_for(check(), timeout=20))


def test_http_auth_is_checked_again_after_revocation_and_rejects_wrong_token(tmp_path):
    runtime, _, founder_run, _, _ = _runtime(tmp_path)
    app = build_http_app(runtime, **ALLOWED)

    async def post(token):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}", "Origin": BASE_URL},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

    async def check():
        async with app.router.lifespan_context(app):
            assert (await post("wrong-token")).status_code == 401
            runtime.finish(founder_run.activation_id)
            revoked = await post(founder_run.token)
            assert revoked.status_code == 401
            assert revoked.headers["www-authenticate"].startswith("Bearer")

    asyncio.run(asyncio.wait_for(check(), timeout=20))


def test_host_origin_validation_and_explicit_allowlists(tmp_path):
    runtime, _, founder_run, _, _ = _runtime(tmp_path)
    app = build_http_app(runtime, **ALLOWED)

    async def post(headers):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

    async def check():
        async with app.router.lifespan_context(app):
            auth = {"Authorization": f"Bearer {founder_run.token}"}
            bad_host = await post({**auth, "Host": "rebound.test", "Origin": BASE_URL})
            bad_origin = await post({**auth, "Host": "firm.test", "Origin": "http://evil.test"})
            assert bad_host.status_code == 421
            assert bad_origin.status_code == 403

    asyncio.run(asyncio.wait_for(check(), timeout=20))

    for field in ("allowed_hosts", "allowed_origins"):
        settings = {**ALLOWED, field: []}
        try:
            build_http_app(runtime, **settings)
        except ValueError as error:
            assert field in str(error)
        else:
            raise AssertionError(f"empty {field} was accepted")


def test_activation_start_accepts_only_authorized_admitted_token(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_launch_attempted(invocation.activation_id)
    app = build_http_app(runtime, **ALLOWED)

    async def start(token, **headers):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await client.post(
                "/activation/start",
                headers={"Authorization": f"Bearer {token}", "Origin": BASE_URL, **headers},
            )

    async def check():
        async with app.router.lifespan_context(app):
            assert (await start("wrong-token")).status_code == 401
            assert (await start(invocation.token, Host="rebound.test")).status_code == 421
            ready = await start(invocation.token)
            assert ready.status_code == 200
            assert ready.json() == {
                "status": "ready",
                "activation_id": invocation.activation_id,
                "employee_id": invocation.employee_id,
            }
            assert (await start(invocation.token)).json() == ready.json()
            runtime.finish(invocation.activation_id)
            assert (await start(invocation.token)).status_code == 401

    asyncio.run(asyncio.wait_for(check(), timeout=20))


@pytest.mark.parametrize("during_refresh", ["finish", "reconfigure", "reconfigure_error"])
def test_credential_endpoint_is_bound_revocable_and_access_only(
    tmp_path, monkeypatch, during_refresh
):
    import json
    import time

    from starlette.testclient import TestClient

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fake-access",
                    "refreshToken": "host-only-refresh",
                    "expiresAt": (time.time() + 7200) * 1000,
                }
            }
        )
    )
    auth.chmod(0o600)
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Work",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "p",
            "profiles": {
                "p": {"provider": "claude", "model": "opus", "effort": "high", "accounts": ["a"]}
            },
            "accounts": {"a": {"provider": "claude", "auth_file": str(auth), "max_active": 1}},
        },
    )
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    app = build_http_app(runtime, **ALLOWED)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/credentials").status_code == 401
        response = client.get(
            "/credentials?account=another", headers={"Authorization": f"Bearer {invocation.token}"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["data"]["claudeAiOauth"]["accessToken"] == "fake-access"
        assert "host-only-refresh" not in response.text
        import hashlib

        summary_token = "summary-start-token"
        with runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,"
                "provider,deadline,token_hash) VALUES('s-test',?,'running',1,'a','claude',?,?)",
                (time.time(), time.time() + 60, hashlib.sha256(summary_token.encode()).hexdigest()),
            )
        summary_headers = {"Authorization": f"Bearer {summary_token}"}
        assert client.get("/credentials", headers=summary_headers).status_code == 401
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE summary_runs SET provider_started_at=? WHERE run_id='s-test'",
                (time.time(),),
            )
        assert client.get("/credentials", headers=summary_headers).status_code == 200
        with runtime.store.transaction() as db:
            db.execute("UPDATE summary_runs SET deadline=0 WHERE run_id='s-test'")
        assert client.get("/credentials", headers=summary_headers).status_code == 401
        from firm_runtime.credential_store import CredentialStore
        from firm_runtime.provider_contract import ProviderSetupError

        original = CredentialStore.snapshot

        def change_during_refresh(store):
            result = original(store)
            if during_refresh == "finish":
                runtime.finish(invocation.activation_id, "completed")
            else:
                with runtime.store.read() as db:
                    config = runtime.accounts.config(db)
                config["accounts"]["a"]["auth_file"] = str(tmp_path / "new-auth.json")
                runtime.owner.configure(OWNER, config)
            if during_refresh == "reconfigure_error":
                raise ProviderSetupError(
                    category="auth", code="authentication_failed", message="safe"
                )
            return result

        monkeypatch.setattr(CredentialStore, "snapshot", change_during_refresh)
        assert (
            client.get(
                "/credentials", headers={"Authorization": f"Bearer {invocation.token}"}
            ).status_code
            == 401
        )
        assert runtime.accounts.status()["incidents"] == []
