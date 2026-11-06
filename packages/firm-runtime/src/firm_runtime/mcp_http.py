"""Authenticated Streamable HTTP MCP endpoint for sandboxed role executions."""

import hashlib
from collections.abc import Sequence

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from .errors import FirmError
from .github import GitHubRefused, GitHubUnavailable
from .mcp_server import register_tools
from .provider_contract import ProviderSetupError
from .runtime import Runtime
from .store import OWNER

ROLE_SCOPE = "firm:role"


class ActivationTokenVerifier:
    """Resolve a live activation on every HTTP request without caching credentials."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            activation = self.runtime.authenticate(token)
        except FirmError:
            return None
        return AccessToken(
            token=token,
            client_id=activation["activation_id"],
            subject=activation["employee_id"],
            scopes=[ROLE_SCOPE],
        )


def _request_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise FirmError("invalid or expired activation token")
    return access_token.token


def _bearer_token(request: Request) -> str | None:
    parts = request.headers.get("authorization", "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _auth_error() -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def build_http_app(
    runtime: Runtime,
    *,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str],
    json_response: bool = True,
    stateless_http: bool = True,
) -> ASGIApp:
    """Build the authenticated `/mcp` app and admitted-token launch handshake."""
    if not allowed_hosts:
        raise ValueError("allowed_hosts must explicitly name at least one address")
    if not allowed_origins:
        raise ValueError("allowed_origins must explicitly name at least one origin")

    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=list(allowed_origins),
    )
    security = TransportSecurityMiddleware(security_settings)
    server = FastMCP(
        "firm-role-tools",
        token_verifier=ActivationTokenVerifier(runtime),
        auth=AuthSettings(
            issuer_url="http://firm.local",
            resource_server_url=None,
            required_scopes=[ROLE_SCOPE],
        ),
        json_response=json_response,
        stateless_http=stateless_http,
        transport_security=security_settings,
    )
    register_tools(server, runtime, _request_token)

    @server.custom_route("/activation/start", methods=["POST"])
    async def activation_start(request: Request):
        rejection = await security.validate_request(request)
        if rejection is not None:
            return rejection
        token = _bearer_token(request)
        if token is None:
            return _auth_error()
        try:
            result = runtime.begin_execution(token)
        except FirmError:
            return _auth_error()
        return JSONResponse(result)

    @server.custom_route("/summary/start", methods=["POST"])
    async def summary_start(request: Request):
        rejection = await security.validate_request(request)
        if rejection is not None:
            return rejection
        token = _bearer_token(request)
        if token is None:
            return _auth_error()
        try:
            result = runtime.summary_service.begin_execution(token)
        except FirmError:
            return _auth_error()
        return JSONResponse(result)

    def credential_account(token):
        with runtime.store.read() as db:
            try:
                activation = runtime._authenticate(db, token)
                binding = runtime.accounts.binding(db, activation["session_id"])
                if not binding:
                    raise FirmError("No bound account")
                account_id = binding["account_id"]
                provider = binding["provider"]
            except FirmError:
                summary = db.execute(
                    "SELECT account_id,provider,kind,caller_activation_id FROM summary_runs "
                    "WHERE token_hash=? "
                    "AND state='running' AND provider_started_at IS NOT NULL AND deadline>?",
                    (hashlib.sha256(token.encode()).hexdigest(), runtime.store.clock()),
                ).fetchone()
                if summary is None or runtime.store.firm(db)["state"] not in {
                    "running",
                    "draining",
                }:
                    raise FirmError("Invalid credential token") from None
                if summary["kind"] == "task":
                    parent = db.execute(
                        "SELECT a.state,s.stop_cause,s.deadline FROM activations a "
                        "JOIN sandbox_runs s USING(activation_id) WHERE a.activation_id=?",
                        (summary["caller_activation_id"],),
                    ).fetchone()
                    if (not parent or parent["state"] != "running" or parent["stop_cause"]
                            or parent["deadline"] <= runtime.store.clock()):
                        raise FirmError("Task parent is no longer live") from None
                account_id, provider = summary["account_id"], summary["provider"]
            account = runtime.accounts.config(db).get("accounts", {}).get(account_id)
            if not account or account["provider"] != provider:
                raise FirmError("Account binding changed")
        return account_id, provider, account["auth_file"]

    @server.custom_route("/credentials", methods=["GET"])
    async def credentials(request: Request):
        """Access-only credentials for the caller's bound account, never arbitrary paths."""
        rejection = await security.validate_request(request)
        if rejection is not None:
            return rejection
        token = _bearer_token(request)
        if token is None or len(token) > 256:
            return _auth_error()
        try:
            identity = credential_account(token)
            account_id, provider, auth_file = identity
        except FirmError:
            return _auth_error()
        from .credential_store import CredentialStore

        try:
            snapshot = await anyio.to_thread.run_sync(CredentialStore(provider, auth_file).snapshot)
        except ProviderSetupError as exc:
            try:
                if credential_account(token) != identity:
                    return _auth_error()
            except FirmError:
                return _auth_error()
            if exc.category == "auth":
                runtime.accounts.pause(OWNER, "account", account_id, "auth")
            return JSONResponse(
                {
                    "error": "authentication_failed"
                    if exc.category == "auth"
                    else "service_unavailable"
                },
                status_code=403 if exc.category == "auth" else 503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            if credential_account(token) != identity:
                return _auth_error()
        except FirmError:
            return _auth_error()
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    @server.custom_route("/github/token", methods=["GET"])
    async def github_token(request: Request):
        """An installation token scoped to the caller's grant, for its running activation."""
        rejection = await security.validate_request(request)
        if rejection is not None:
            return rejection
        token = _bearer_token(request)
        if token is None or len(token) > 256:
            return _auth_error()
        repo = request.query_params.get("repo")
        try:
            issued = await anyio.to_thread.run_sync(runtime.github.issue, token, repo)
        except FirmError:
            return _auth_error()
        except GitHubRefused as refusal:
            status, body = 403, {"error": refusal.code, "repo": repo}
        except GitHubUnavailable:
            status, body = 503, {"error": "github_unavailable"}
        else:
            status, body = 200, issued
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    return server.streamable_http_app()
