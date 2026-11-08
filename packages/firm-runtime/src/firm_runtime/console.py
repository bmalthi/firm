"""Authenticated loopback owner console, controls, and bounded file views."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import stat
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .accounts import credentials_path, incident_resolution
from .analysis import SECTIONS, AnalysisNotFound, AnalysisQueryError, AnalysisReader
from .errors import FirmError
from .levels import level_of
from .messaging import MAX_BODY_BYTES, MAX_PARTICIPANTS
from .quota_observer import QuotaObserver
from .requests import normalize_denial_reason, pending_requests
from .runtime import Runtime
from .store import OWNER
from .telemetry import context_policy

LOGGER = logging.getLogger(__name__)
SESSION_COOKIE = "firm_owner_console"
KEY_FILE = "console-key"
KEY_LIFETIME = 365 * 24 * 60 * 60
STATIC_ROOT = Path(__file__).resolve().parent / "web_console"
STATIC_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    **dict.fromkeys(
        (
            "app.js", "analysis.js", "format.js", "role_tree.js", "overview.js",
            "inbox.js", "results.js", "team.js", "system.js",
        ),
        "text/javascript; charset=utf-8",
    ),
}
MAX_DIAGNOSTIC_HISTORY = 50
MAX_MUTATION_BYTES = 20_000


def console_key(durable: Path) -> str:
    """The firm's console key: created once in `durable/`, owner-readable only, reusable.

    Loopback is reachable by every account on the machine, so the console is not open to
    anyone who can connect to the port. Keeping the key in the firm's durable directory —
    which only the owner can read — makes the console's address stable across host restarts
    instead of handing out a fresh one-use link every boot.
    """
    path = durable / KEY_FILE
    key = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="ascii") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise ValueError("key is not an owner-only regular file")
                key = handle.read(128).strip()
                if re.fullmatch(r"[A-Za-z0-9_-]{43}", key) is None:
                    raise ValueError("invalid key")
        except (OSError, ValueError):
            raise FirmError(f"console key is invalid or not private: {path}") from None
        return key
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def console_scope(store_path: Path, firm_id: str) -> str:
    """Bind browser state to both a firm's durable ID and its current location."""
    identity = str(Path(store_path).resolve()) + "\0" + firm_id
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def level_view(role: dict, record: dict | None, config: dict) -> dict:
    """The owner-facing hire level: class, effective effort, decision state and reason."""
    return {
        **level_of(role, config),
        "state": record["state"] if record else "configured",
        "reason": record["reason"] if record else None,
    }


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _local_host(allowed_host: str) -> bool:
    hostname = allowed_host
    if allowed_host.startswith("["):
        hostname = allowed_host.partition("]")[0] + "]"
    elif ":" in allowed_host:
        hostname = allowed_host.rsplit(":", 1)[0]
    return hostname in {"127.0.0.1", "localhost", "[::1]"}


def build_console_app(
    runtime: Runtime, *, allowed_host: str, sandbox=None, control=None
) -> Starlette:
    """Build the authenticated loopback owner console and its narrow service routes."""
    if not _local_host(allowed_host):
        raise ValueError("owner console allowed_host must be loopback")
    if sandbox is not None:
        runtime.messaging.files = sandbox.file_server

    assets = {
        name: (STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in STATIC_ASSETS
    }
    key = console_key(runtime.store.path.parent)
    cookie_suffix = allowed_host.replace("[", "").replace("]", "").replace(":", "_")
    cookie_name = f"{SESSION_COOKIE}_{cookie_suffix}"
    host_epoch = secrets.token_urlsafe(12)
    workers = anyio.CapacityLimiter(4)

    def quota_accounts():
        with runtime.store.read() as db:
            return runtime.accounts.config(db).get("accounts", {})

    quota_observer = QuotaObserver(quota_accounts)

    @asynccontextmanager
    async def lifespan(app):
        quota_observer.start()
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(quota_observer.stop)

    async def run(function, *args, **kwargs):
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=workers)

    def authenticated(request: Request) -> bool:
        supplied = request.cookies.get(cookie_name)
        return (
            isinstance(supplied, str)
            and supplied.isascii()
            and secrets.compare_digest(supplied, key)
        )

    def require_session(request: Request) -> Response | None:
        if authenticated(request):
            return None
        return _error(401, "authentication_required", "Open the owner-console unlock URL once.")

    async def unlock(request: Request) -> Response:
        supplied = request.path_params["key"]
        if not supplied.isascii() or not secrets.compare_digest(supplied, key):
            return _error(404, "unlock_failed", "Console key is not this firm's.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            cookie_name,
            key,
            max_age=KEY_LIFETIME,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    async def asset(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        name = request.url.path.lstrip("/") or "index.html"
        return Response(assets[name], media_type=STATIC_ASSETS[name])

    def snapshot_value() -> dict:
        with runtime.store.read() as db:
            status = runtime.status()
            firm = runtime.store.firm(db)
            host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
            tree = runtime.discovery.org_tree(OWNER, depth=2, max_nodes=50)
            tree_roles = [tree["root"], *tree["nodes"]]
            roles = hydrate_owner_facts(tree_roles)
            drain_outcomes = [
                {
                    "activation_id": row["activation_id"],
                    "employee_id": row["employee_id"],
                    "address": row["address"],
                    "requested": row["drain_requested_at"] is not None,
                    "requested_at": row["drain_requested_at"],
                    "request_presented": row["drain_presented_at"] is not None,
                    "handoff_saved": row["handoff_notes"] is not None,
                    "confirmed_exit": row["state"] not in {"admitted", "running"}
                    and row["finished_at"] is not None,
                    "exit_without_notes": row["state"] not in {"admitted", "running"}
                    and row["finished_at"] is not None
                    and row["handoff_notes"] is None,
                    "state": row["state"],
                    "finished_at": row["finished_at"],
                }
                for row in db.execute(
                    "SELECT a.*,r.address FROM activations a JOIN roles r USING(employee_id) "
                    "WHERE drain_requested_at IS NOT NULL ORDER BY a.created_at DESC LIMIT ?",
                    (MAX_DIAGNOSTIC_HISTORY,),
                )
            ]
            failure_history = [
                {
                    "activation_id": row["activation_id"],
                    "employee_id": row["employee_id"],
                    "address": row["address"],
                    "state": row["state"],
                    "failure_category": row["failure_category"],
                    "failure": row["failure"],
                    "finished_at": row["finished_at"],
                }
                for row in db.execute(
                    "SELECT a.*,r.address FROM activations a JOIN roles r USING(employee_id) "
                    "WHERE state IN ('failed','interrupted') "
                    "ORDER BY finished_at DESC LIMIT ?",
                    (MAX_DIAGNOSTIC_HISTORY,),
                )
            ]
            usage = runtime.usage_diagnostics(OWNER, employee_id=None, limit=100)
            names = {
                row["employee_id"]: row["address"]
                for row in db.execute("SELECT employee_id,address FROM roles")
            }
            for item in usage["sessions"]:
                item["address"] = names.get(item.get("employee_id"), "Runtime")
            github = {
                "repositories": [
                    dict(row)
                    for row in db.execute(
                        "SELECT g.repository,r.address,g.purpose,g.created_at "
                        "FROM github_repositories g JOIN roles r USING(employee_id) "
                        "ORDER BY g.created_at"
                    )
                ],
                "grants": [
                    dict(row)
                    for row in db.execute(
                        "SELECT repository,address,access,source,created_at FROM github_grants "
                        "ORDER BY repository,address"
                    )
                ],
                "revocations": [
                    dict(row)
                    for row in db.execute(
                        "SELECT repository,address,revoked_at FROM github_revocations "
                        "ORDER BY repository,address"
                    )
                ],
            }
            needs_you = {
                "owner_mail": db.execute(
                    "SELECT COUNT(*) FROM message_deliveries "
                    "WHERE employee_id=? AND disposition='pending'",
                    (OWNER,),
                ).fetchone()[0],
                "requests": pending_requests(db, runtime.store.clock()),
            }
            accounts = runtime.accounts.status()
            configured_accounts = runtime.accounts.config(db).get("accounts", {})
            last_started = {
                row["account_id"]: row["last_started_at"]
                for row in db.execute(
                    "SELECT b.account_id,MAX(a.created_at) AS last_started_at "
                    "FROM activations a JOIN execution_bindings b USING(session_id) "
                    "GROUP BY b.account_id"
                )
            }
            for account in accounts["accounts"]:
                account_id = account["account_id"]
                auth_file = Path(configured_accounts[account_id]["auth_file"]).expanduser()
                account["auth_file"] = str(auth_file)
                account["default_auth_file"] = auth_file == credentials_path(
                    account_id, account["provider"]
                )
                # Admission is the durable evidence, even if work finishes between polls.
                account["last_started_at"] = last_started.get(account_id)
                if account["provider"] in {"claude", "codex", "agy"}:
                    account["subscription_quota"] = quota_observer.snapshot(
                        account["account_id"],
                        runtime.accounts.subscription_reading(db, account["account_id"]),
                    )
            return {
                "firm": {
                    "firm_id": firm["firm_id"],
                    "console_scope": console_scope(runtime.store.path, firm["firm_id"]),
                    "state": firm["state"],
                    "created_at": firm["created_at"],
                },
                "host_epoch": host_epoch,
                "console": {"state": "available"},
                "needs_you": needs_you,
                "readiness": AnalysisReader.console_readiness(db, runtime.store.clock()),
                "infrastructure_state": host["infrastructure_state"] if host is not None else None,
                "mac": runtime.mac.summary(),
                "github": github,
                "active": status["active"],
                "waiting_roles": status["waiting"],
                "held": status["held"],
                "roles": roles,
                "role_tree": {
                    "remaining": tree["remaining"],
                    "continuation": tree["continuation"],
                },
                "drain_outcomes": drain_outcomes,
                "failure_history": failure_history,
                "usage": usage,
                "accounts": accounts,
            }

    async def snapshot(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        return JSONResponse(await run(snapshot_value))

    def regarding_value(db, message, now):
        kind, reference = message["regarding_kind"], message["regarding_id"]
        if kind is None:
            return None
        if kind == "incident":
            row = db.execute(
                "SELECT * FROM execution_incidents WHERE incident_id=?", (reference,)
            ).fetchone()
            resolution = (
                incident_resolution(row, now)
                if row
                else {
                    "resolved_at": None,
                    "resolved_by": None,
                }
            )
            status = (
                ("resolved" if resolution["resolved_at"] is not None else "open") if row else None
            )
        else:
            row = db.execute(
                "SELECT status,decided_at,decided_by FROM owner_requests WHERE request_id=?",
                (reference,),
            ).fetchone()
            status = row["status"] if row else None
            resolution = {
                "resolved_at": row["decided_at"] if row else None,
                "resolved_by": row["decided_by"] if row else None,
            }
        return {"kind": kind, "id": reference, "status": status, **resolution}

    def messages_value(
        before: int | None, limit: int, view: str | None = None, scope: str = "all"
    ) -> dict:
        with runtime.store.read() as db:
            conditions = []
            parameters: list[Any] = []
            join = ""
            columns = "m.*"
            if scope == "between_roles":
                conditions.append(
                    "NOT EXISTS (SELECT 1 FROM message_deliveries owner_delivery "
                    "WHERE owner_delivery.message_id=m.message_id "
                    "AND owner_delivery.employee_id=?)"
                )
                parameters.append(OWNER)
            if view is not None:
                join = " JOIN message_deliveries d ON d.message_id=m.message_id"
                columns += ",d.disposition,d.disposition_at"
                conditions.append("d.employee_id=?")
                parameters.append(OWNER)
                if view != "all":
                    conditions.append("d.disposition=?")
                    parameters.append("pending" if view == "new" else "dismissed")
            if before is not None:
                conditions.append("m.sequence<?")
                parameters.append(before)
            parameters.append(limit + 1)
            rows = list(
                db.execute(
                    "SELECT "
                    + columns
                    + " FROM messages m"
                    + join
                    + (" WHERE " + " AND ".join(conditions) if conditions else "")
                    + " ORDER BY m.sequence DESC LIMIT ?",
                    parameters,
                )
            )
            shown = rows[:limit]
            now = runtime.store.clock()
            envelopes = []
            for row in shown:
                envelope = runtime.messaging.envelope(row)
                envelope["regarding"] = regarding_value(db, row, now)
                if view is not None:
                    envelope["owner_delivery"] = {
                        "disposition": row["disposition"],
                        "disposition_at": row["disposition_at"],
                    }
                envelopes.append(envelope)
        return {
            "messages": envelopes,
            "next_cursor": str(shown[-1]["sequence"]) if len(rows) > limit else None,
        }

    def message_pagination(request: Request) -> tuple[int | None, int]:
        limit = int(request.query_params.get("limit", "50"))
        before_text = request.query_params.get("before")
        before = int(before_text) if before_text is not None else None
        if not 1 <= limit <= 100 or (before is not None and before <= 0):
            raise ValueError
        return before, limit

    async def messages(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            scope = request.query_params.get("scope", "all")
            if scope not in {"all", "between_roles"}:
                raise ValueError
            before, limit = message_pagination(request)
        except ValueError:
            return _error(400, "invalid_query", "scope, limit or before cursor is invalid")
        return JSONResponse(await run(messages_value, before, limit, None, scope))

    async def inbox(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            view = request.query_params.get("view", "new")
            if view not in {"new", "all", "done"}:
                raise ValueError
            before, limit = message_pagination(request)
        except ValueError:
            return _error(400, "invalid_query", "view, limit or before cursor is invalid")
        return JSONResponse(await run(messages_value, before, limit, view))

    def results_value(limit: int) -> dict:
        with runtime.store.read() as db:
            rows = db.execute(
                "SELECT m.*,a.value AS attachment FROM messages m "
                "JOIN message_deliveries d USING(message_id) "
                "JOIN json_each(m.attachments_json) a "
                "WHERE d.employee_id=? ORDER BY m.sequence DESC,CAST(a.key AS INTEGER) "
                "LIMIT ?",
                (OWNER, limit + 1),
            ).fetchall()
            return {
                "results": [
                    {
                        **json.loads(row["attachment"]),
                        "message_id": row["message_id"],
                        "conversation_id": row["conversation_id"],
                        "subject": row["subject"],
                        "author": {
                            "employee_id": row["author_id"],
                            "address": row["author_address"],
                            "identity": row["author_identity"],
                        },
                        "created_at": row["created_at"],
                    }
                    for row in rows[:limit]
                ],
                "truncated": len(rows) > limit,
                "sampled_at": runtime.store.clock(),
            }

    async def results(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            limit = int(request.query_params.get("limit", "50"))
            if not 1 <= limit <= 200:
                raise ValueError
        except ValueError:
            return _error(400, "invalid_query", "limit must be between 1 and 200")
        return JSONResponse(await run(results_value, limit))

    def incidents_value(recent):
        with runtime.store.read() as db:
            now = runtime.store.clock()
            readiness = AnalysisReader.console_readiness(db, now)
            blocked = {}
            for role in readiness["rows"]:
                if role["activity"] in {"admitted", "running", "retired"}:
                    continue
                for blocker in role["blockers"]:
                    if "incident_id" in blocker and not blocker["alternative_account_usable"]:
                        blocked.setdefault(blocker["incident_id"], []).append(
                            {key: role[key] for key in ("employee_id", "address", "identity")}
                        )
            result = {}
            for name, predicate, order, parameters in (
                (
                    "open",
                    "resolved_at IS NULL AND (until_at IS NULL OR until_at>?)",
                    "created_at,incident_id",
                    (now,),
                ),
                (
                    "recent",
                    "resolved_at IS NOT NULL OR until_at<=?",
                    "COALESCE(resolved_at,until_at) DESC,created_at DESC,incident_id DESC LIMIT ?",
                    (now, recent),
                ),
            ):
                result[name] = [
                    {
                        **dict(row),
                        **incident_resolution(row, now),
                        "blocked_roles": blocked.get(row["incident_id"], []),
                    }
                    for row in db.execute(
                        "SELECT * FROM execution_incidents WHERE "
                        + predicate
                        + " ORDER BY "
                        + order,
                        parameters,
                    )
                ]
            return result

    async def incidents(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            recent = int(request.query_params.get("recent", "50"))
            if not 0 <= recent <= 100:
                raise ValueError
        except ValueError:
            return _error(400, "invalid_query", "recent must be between 0 and 100")
        return JSONResponse(await run(incidents_value, recent))

    async def request_list(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        return JSONResponse(await run(runtime.requests.listing, OWNER, recent=50))

    async def parse_mutation(request: Request) -> dict | Response:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _error(415, "json_required", "Send a JSON request body.")
        try:
            body = await request.body()
            if len(body) > MAX_MUTATION_BYTES:
                return _error(413, "request_too_large", "Request body is too large.")
            value = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return _error(400, "invalid_json", "Request body must be valid JSON.")
        if not isinstance(value, dict):
            return _error(400, "invalid_json", "Request body must be a JSON object.")
        operation_id = value.get("operation_id")
        if (
            not isinstance(operation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id) is None
        ):
            return _error(
                400,
                "invalid_operation_id",
                "operation_id must be a nonempty browser operation key.",
            )
        return value

    async def dismiss_messages(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        try:
            result = await run(
                runtime.messaging.dismiss, OWNER, value.get("message_ids"),
                request_id=value["operation_id"],
            )
        except FirmError as exc:
            return _error(400, "message_refused", str(exc))
        return JSONResponse(result)

    def clear_incident_value(incident_id, operation_id):
        with runtime.store.transaction() as db:
            return runtime.store.request(
                db, OWNER, operation_id, "clear_incident", {"incident_id": incident_id},
                lambda: runtime.accounts.clear(OWNER, incident_id),
            )

    async def clear_incident(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        incident_id = value.get("incident_id")
        if not isinstance(incident_id, str) or not incident_id:
            return _error(400, "invalid_incident", "Provide an incident_id.")
        try:
            result = await run(clear_incident_value, incident_id, value["operation_id"])
        except FirmError as exc:
            return _error(400, "incident_refused", str(exc))
        return JSONResponse(result)

    async def send_message(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        recipients, subject, body = value.get("recipients"), value.get("subject"), value.get("body")
        if (
            not isinstance(recipients, list)
            or not recipients
            or len(recipients) > MAX_PARTICIPANTS
            or any(not isinstance(item, str) or not item.strip() for item in recipients)
            or not isinstance(body, str)
            or len(body.encode("utf-8")) > MAX_BODY_BYTES
            or (subject is not None and not isinstance(subject, str))
        ):
            return _error(
                400,
                "invalid_message",
                "Provide recipients and a message body within the service limits.",
            )
        try:
            result = await run(
                runtime.messaging.send,
                OWNER,
                recipients,
                body,
                subject=subject,
                attachments=value.get("attachments"),
                request_id=value["operation_id"],
            )
        except FirmError as exc:
            return _error(400, "message_refused", str(exc))
        return JSONResponse(result)

    async def reply_message(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        message_id, body = value.get("message_id"), value.get("body")
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(body, str)
            or len(body.encode("utf-8")) > MAX_BODY_BYTES
        ):
            return _error(
                400, "invalid_message", "Provide a message id and a body within the service limit."
            )
        try:
            result = await run(
                runtime.messaging.reply,
                OWNER,
                message_id,
                body,
                attachments=value.get("attachments"),
                request_id=value["operation_id"],
            )
        except FirmError as exc:
            return _error(400, "reply_refused", str(exc))
        return JSONResponse(result)

    async def message_operation(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        operation_id = request.query_params.get("operation_id", "")
        expected = request.query_params.get("operation", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id) is None or expected not in {
            "send",
            "reply",
        }:
            return _error(400, "invalid_query", "operation_id or operation is invalid.")
        with runtime.store.read() as db:
            receipt = runtime.store.operation_result(db, OWNER, operation_id)
        if receipt is None:
            return JSONResponse({"status": "not_recorded"})
        if receipt["operation"] != expected:
            return _error(
                409, "operation_conflict", "This operation key belongs to another action."
            )
        return JSONResponse({"status": "recorded", "result": receipt["result"]})

    async def decide_request(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        request_id, action, reason = (
            value.get("request_id"),
            value.get("action"),
            value.get("reason"),
        )
        if not isinstance(request_id, str) or re.fullmatch(r"q[1-9][0-9]*", request_id) is None:
            return _error(400, "invalid_request_id", "request_id is invalid.")
        if action not in {"grant", "deny"}:
            return _error(400, "invalid_decision", "action must be grant or deny.")
        if action == "deny":
            try:
                reason = normalize_denial_reason(reason)
            except FirmError as exc:
                return _error(400, "reason_required", str(exc))

        try:
            current = await run(runtime.requests.status, OWNER, request_id)
        except FirmError:
            return _error(404, "request_not_found", "Owner request no longer exists.")
        expected = "granted" if action == "grant" else "denied"
        if current["status"] != "pending":
            if current["status"] == expected and (
                action != "deny" or current.get("decision_note") == reason
            ):
                return JSONResponse({"request_id": request_id, **current, "replayed": True})
            return _error(409, "already_decided", f"Request is already {current['status']}.")
        if action == "grant" and sandbox is not None:
            runtime.hosts.sandbox = sandbox
        try:
            if action == "grant":
                result = await run(runtime.requests.grant, OWNER, request_id)
            else:
                result = await run(runtime.requests.deny, OWNER, request_id, reason)
        except (FirmError, OSError, RuntimeError, ValueError) as exc:
            # A service may have completed an external effect before its response was lost.
            # Prefer the durable request decision if it landed; otherwise force the owner
            # to inspect the refreshed request and service state before retrying.
            try:
                updated = await run(runtime.requests.status, OWNER, request_id)
            except FirmError:
                updated = None
            if updated and updated["status"] == expected and (
                action != "deny" or updated.get("decision_note") == reason
            ):
                return JSONResponse({"request_id": request_id, **updated, "replayed": True})
            if updated and updated["status"] in {"granted", "denied", "withdrawn"}:
                return _error(409, "already_decided", f"Request is now {updated['status']}.")
            message = (
                f"Decision outcome is not recorded: {exc}. Refresh and check the request "
                "and its resource before retrying."
            )
            return _error(
                503,
                "decision_uncertain",
                message,
            )
        return JSONResponse({"request_id": request_id, "status": expected, "result": result})

    async def file_roots(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        if sandbox is None:
            return JSONResponse({"available": False, "roots": []})
        try:

            def read_roots():
                sandbox.file_server.require_ready()
                return {"available": True, "roots": sandbox.file_server.browse_roots()}

            result = await run(read_roots)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"available": False, "roots": [], "reason": str(exc)})
        return JSONResponse(result)

    async def file_list(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        if sandbox is None:
            return _error(503, "files_unavailable", "File browser is unavailable.")
        path = request.query_params.get("path", "")
        try:
            result = await run(sandbox.file_server.browse_list, path)
        except ValueError as exc:
            return _error(400, "invalid_path", str(exc))
        except (OSError, RuntimeError) as exc:
            return _error(503, "files_unavailable", str(exc))
        return JSONResponse(result)

    async def file_read(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        if sandbox is None:
            return _error(503, "files_unavailable", "File browser is unavailable.")
        path, name = request.query_params.get("path", ""), request.query_params.get("name", "")
        try:
            result = await run(sandbox.file_server.browse_read, path, name)
        except ValueError as exc:
            return _error(400, "invalid_path", str(exc))
        except (OSError, RuntimeError) as exc:
            return _error(503, "files_unavailable", str(exc))
        if result["status"] != "available":
            return _error(
                404 if result["status"] == "missing" else 413,
                f"file_{result['status']}",
                "File is unavailable or exceeds the download limit.",
            )
        data = base64.b64decode(result["data"], validate=True)
        filename = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "download"
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                "Content-Length": str(len(data)),
                "X-Firm-File-Name": filename,
            },
        )

    async def file_preview(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        if sandbox is None:
            return _error(503, "files_unavailable", "File browser is unavailable.")
        path, name = request.query_params.get("path", ""), request.query_params.get("name", "")
        extension = Path(name).suffix.lower()
        if extension not in {".html", ".md", ".markdown", ".txt"}:
            return _error(
                415, "preview_type_unsupported", "Preview supports HTML, Markdown and text files."
            )
        try:
            result = await run(
                sandbox.file_server.browse_read,
                path,
                name,
                limit=2 * 1024 * 1024,
            )
        except ValueError as exc:
            return _error(400, "invalid_path", str(exc))
        except (OSError, RuntimeError) as exc:
            return _error(503, "files_unavailable", str(exc))
        if result["status"] != "available":
            return _error(
                404 if result["status"] == "missing" else 413,
                f"file_{result['status']}",
                "Preview is unavailable or exceeds the 2 MiB preview limit.",
            )
        data = base64.b64decode(result["data"], validate=True)
        policy = (
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
            "connect-src 'none'; form-action 'none'; object-src 'none'; base-uri 'none'; "
            "frame-src 'none'; "
            f"frame-ancestors http://{allowed_host}"
        )
        if extension != ".html":
            text = data.decode("utf-8", errors="replace")
            content = (
                f"<pre>{html.escape(text)}</pre>"
                if extension == ".txt"
                else MarkdownIt("commonmark", {"html": False}).enable("table").render(text)
            )
            data = (
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{html.escape(name)}</title><style>"
                "body{margin:0;padding:24px;color:#14181d;background:#fff;"
                "font:16px/1.6 system-ui,sans-serif;overflow-wrap:anywhere}"
                "h1,h2,h3{line-height:1.3}h1{font-size:26px}"
                "pre{white-space:pre-wrap}code{font-size:.9em}"
                "table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}"
                "td,th{border-bottom:1px solid #dce0e5;padding:8px;text-align:left}"
                "img{max-width:100%;height:auto}a{color:#1d4ed8}"
                "blockquote{border-left:3px solid #dce0e5;margin-left:0;padding-left:16px}"
                f"</style></head><body>{content}</body></html>"
            ).encode()
            policy = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                f"frame-ancestors http://{allowed_host}"
            )
        return Response(
            data,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": policy,
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    async def control_status(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        status = runtime.status()
        with runtime.store.read() as db:
            host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
        return JSONResponse(
            {
                "firm_state": status["state"],
                "infrastructure_state": host["infrastructure_state"] if host else "stopped",
                "can_resume": status["state"] == "stopped"
                and host is not None
                and host["infrastructure_state"] == "stopped",
                "can_close": status["state"] == "stopped"
                and (host is None or host["infrastructure_state"] == "stopped"),
                "startup_error": (
                    control.startup_error()
                    if control is not None and hasattr(control, "startup_error")
                    else None
                ),
            }
        )

    async def control_action(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        action = request.path_params["action"]
        if action not in {"resume", "stop", "force-stop", "close"}:
            return _error(404, "unknown_control_action", "Control action does not exist.")
        if control is None:
            return _error(503, "control_unavailable", "Lifecycle control is unavailable.")
        value = await parse_mutation(request)
        if isinstance(value, Response):
            return value
        operation_id = value["operation_id"]

        def mutate(operation: str, payload: dict, action_fn):
            with runtime.store.transaction() as db:
                runtime.store.require_actor(db, OWNER)
                return runtime.store.request(db, OWNER, operation_id, operation, payload, action_fn)

        def prior(operation: str, payload: dict):
            try:
                with runtime.store.read() as db:
                    return runtime.store.replay(db, OWNER, operation_id, operation, payload)
            except FirmError as exc:
                return _error(409, "operation_conflict", str(exc))

        async def apply(operation: str, payload: dict, action_fn):
            try:
                return await run(mutate, operation, payload, action_fn)
            except FirmError as exc:
                return _error(409, "operation_conflict", str(exc))

        if action == "resume":
            payload = {"action": action}
            prior_result = prior("console_control.resume", payload)
            if isinstance(prior_result, Response):
                return prior_result
            if prior_result is not None:
                return JSONResponse(prior_result, status_code=202)
            status = await run(runtime.status)
            if status["state"] != "stopped":
                return _error(409, "not_stopped", "Stop execution fully before resuming.")
            with runtime.store.read() as db:
                host = db.execute(
                    "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
                ).fetchone()
            if host is None or host["infrastructure_state"] != "stopped":
                return _error(409, "infrastructure_not_stopped", "VM shutdown is not confirmed.")
            try:
                from .compatibility import validate_firm

                await run(validate_firm, runtime.store.path.parent.parent)
            except (FirmError, OSError, ValueError, RuntimeError) as exc:
                return _error(409, "firm_incompatible", str(exc))
            result = await apply(
                "console_control.resume",
                payload,
                lambda: control.request_start() or {"status": "starting"},
            )
            if isinstance(result, Response):
                return result
            return JSONResponse(result, status_code=202)
        if action == "stop":
            result = await apply(
                "console_control.stop",
                {"action": action},
                lambda: control.stop(False),
            )
            if isinstance(result, Response):
                return result
            return JSONResponse(result)
        if action == "force-stop":
            if value.get("confirmed") is not True:
                return _error(
                    400, "force_confirmation_required", "Force stop requires explicit confirmation."
                )
            result = await apply(
                "console_control.force_stop",
                {"action": action, "confirmed": True},
                lambda: control.stop(True),
            )
            if isinstance(result, Response):
                return result
            return JSONResponse(result)
        if action == "close":
            payload = {"action": action}
            prior_result = prior("console_control.close", payload)
            if isinstance(prior_result, Response):
                return prior_result
            if prior_result is not None:
                return JSONResponse(prior_result, status_code=202)
            status = await run(runtime.status)
            with runtime.store.read() as db:
                host = db.execute(
                    "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
                ).fetchone()
            if status["state"] != "stopped" or (host and host["infrastructure_state"] != "stopped"):
                return _error(
                    409,
                    "firm_running",
                    "Stop the firm and confirm VM shutdown before closing the host.",
                )
            result = await apply(
                "console_control.close",
                payload,
                lambda: control.request_close() or {"status": "closing"},
            )
            if isinstance(result, Response):
                return result
            return JSONResponse(result, status_code=202)
        return _error(404, "unknown_control_action", "Control action does not exist.")

    def hydrate_owner_facts(value):
        """Add current execution facts and measured usage to compact role pages."""
        roles = value if isinstance(value, list) else [value.get("root"), *value.get("nodes", [])]
        roles = [role for role in roles if role is not None]
        if not roles:
            return value
        ids = [role["employee_id"] for role in roles]
        placeholders = ",".join("?" for _ in ids)
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
            rows = {
                row["employee_id"]: row
                for row in db.execute(
                    "SELECT employee_id,execution_profile,effort,hold_reason,recovery_episode,"
                    "retry_at,created_at,session_id "
                    f"FROM roles WHERE employee_id IN ({placeholders})",
                    ids,
                )
            }
            levels = {
                row["employee_id"]: dict(row)
                for row in db.execute(
                    "SELECT employee_id,state,reason FROM hire_levels "
                    f"WHERE employee_id IN ({placeholders})",
                    ids,
                )
            }
            mandate_revisions = {
                row["employee_id"]: row["revision"]
                for row in db.execute(
                    "SELECT employee_id,MAX(revision) AS revision FROM mandates "
                    f"WHERE employee_id IN ({placeholders}) GROUP BY employee_id",
                    ids,
                )
            }
            activations = {
                row["employee_id"]: dict(row)
                for row in db.execute(
                    "SELECT employee_id,activation_id,state,started_at,finished_at "
                    "FROM activations "
                    f"WHERE employee_id IN ({placeholders}) AND state IN ('admitted','running')",
                    ids,
                )
            }
            pending_messages = {
                row["employee_id"]: row["count"]
                for row in db.execute(
                    f"SELECT employee_id,COUNT(*) AS count FROM message_deliveries "
                    f"WHERE employee_id IN ({placeholders}) AND disposition='pending' "
                    "GROUP BY employee_id",
                    ids,
                )
            }
            tokens = {
                row["employee_id"]: dict(row)
                for row in db.execute(
                    "SELECT employee_id,SUM(input_tokens) AS input_tokens,"
                    "SUM(output_tokens) AS output_tokens,"
                    "SUM(cached_input_tokens) AS cached_input_tokens "
                    f"FROM activation_usage WHERE employee_id IN ({placeholders}) "
                    "GROUP BY employee_id",
                    ids,
                )
            }
            native_models = {
                row["session_id"]: row["resolved_model"]
                for row in db.execute(
                    "SELECT u.session_id,u.resolved_model FROM activation_usage u "
                    "JOIN roles r ON r.session_id=u.session_id "
                    f"WHERE r.employee_id IN ({placeholders}) AND u.resolved_model IS NOT NULL "
                    "ORDER BY u.observed_at,u.rowid",
                    ids,
                )
            }
            contexts = {
                row["session_id"]: dict(row)
                for row in db.execute(
                    f"SELECT * FROM context_sessions WHERE employee_id IN ({placeholders})", ids
                )
            }
            bindings = {
                row["session_id"]: dict(row)
                for row in db.execute(
                    "SELECT b.session_id,b.profile,b.provider,b.settings_json "
                    "FROM execution_bindings b JOIN roles r ON r.session_id=b.session_id "
                    f"WHERE r.employee_id IN ({placeholders})",
                    ids,
                )
            }
        enriched = []
        for role in roles:
            row = rows.get(role["employee_id"])
            if row is None:
                enriched.append(role)
                continue
            binding = bindings.get(row["session_id"])
            settings = (
                json.loads(binding["settings_json"])
                if binding
                else config["profiles"].get(row["execution_profile"], {})
                if row["session_id"] is None
                else {}
            )
            if not binding and row["effort"] and settings:
                settings = {**settings, "effort": row["effort"]}
            window, fraction, reserve = context_policy(settings)
            context = contexts.get(row["session_id"])
            execution = {
                "source": "retained_session"
                if binding
                else ("configured_profile" if row["session_id"] is None else "unavailable"),
                "profile": binding["profile"] if binding else row["execution_profile"],
                "provider": binding["provider"] if binding else settings.get("provider"),
                "model": settings.get("model"),
                "effort": settings.get("effort"),
                "context_window": context["soft_context_window"] if context else window,
                "context_advisory_fraction": context["advisory_fraction"] if context else fraction,
                "handoff_reserve": context["handoff_reserve"] if context else reserve,
            }
            if execution["provider"] == "agy":
                execution["resolved_model"] = native_models.get(row["session_id"])
            enriched.append(
                {
                    **role,
                    "execution_profile": row["execution_profile"],
                    "execution": execution,
                    "level": level_view(row, levels.get(role["employee_id"]), config),
                    "created_at": row["created_at"],
                    "mandate_revision": mandate_revisions.get(role["employee_id"]),
                    "tokens": tokens.get(role["employee_id"], {}),
                    "context": contexts.get(row["session_id"]),
                    "hold_reason": row["hold_reason"],
                    "recovery_episode": row["recovery_episode"],
                    "retry_at": row["retry_at"],
                    "pending_messages": pending_messages.get(role["employee_id"], 0),
                    "activation": activations.get(role["employee_id"]),
                }
            )
        if isinstance(value, list):
            return enriched
        return {**value, "root": enriched[0], "nodes": enriched[1:]}

    def role_tree_value(**kwargs):
        with runtime.store.read():
            return hydrate_owner_facts(runtime.discovery.org_tree(OWNER, **kwargs))

    def role_search_value(**kwargs):
        with runtime.store.read():
            result = runtime.discovery.find_roles(OWNER, **kwargs)
            result["roles"] = hydrate_owner_facts(result["roles"])
            return result

    async def role_tree(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            depth = int(request.query_params.get("depth", "2"))
            limit = int(request.query_params.get("limit", "50"))
            root = request.query_params.get("root") or None
            cursor = request.query_params.get("cursor") or None
            result = await run(
                role_tree_value,
                root=root,
                depth=depth,
                max_nodes=limit,
                cursor=cursor,
            )
        except (FirmError, TypeError, ValueError) as exc:
            return _error(400, "invalid_role_tree", str(exc))
        return JSONResponse(result)

    async def role_search(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            limit = int(request.query_params.get("limit", "20"))
            query = request.query_params.get("query", "")
            cursor = request.query_params.get("cursor") or None
            result = await run(
                role_search_value,
                query=query,
                limit=limit,
                cursor=cursor,
            )
        except (FirmError, TypeError, ValueError) as exc:
            return _error(400, "invalid_role_search", str(exc))
        return JSONResponse(result)

    def role_detail_value(employee_id: str, section: str, cursor: str | None) -> dict:
        with runtime.store.read() as db:
            result = runtime.discovery.get_role(
                OWNER,
                employee_id=employee_id,
                section=section,
                cursor=cursor,
                activation_id=None,
            )
            role = runtime.store.role(db, employee_id)
            activation = db.execute(
                "SELECT activation_id,state,started_at,finished_at FROM activations "
                "WHERE employee_id=? AND state IN ('admitted','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (employee_id,),
            ).fetchone()
            pending_messages = db.execute(
                "SELECT COUNT(*) FROM message_deliveries "
                "WHERE employee_id=? AND disposition='pending'",
                (employee_id,),
            ).fetchone()[0]
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
            record = runtime.levels.record(db, employee_id)
        result["owner_facts"] = {
            "execution_profile": role["execution_profile"],
            "level": level_view(role, record, config),
            "hold_reason": role["hold_reason"],
            "attempt_count": role["attempt_count"],
            "retry_at": role["retry_at"],
            "pending_messages": pending_messages,
            "activation": dict(activation) if activation is not None else None,
        }
        return result

    async def role_detail(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        try:
            section = request.query_params.get("section", "profile")
            if section not in {"profile", "mandate"}:
                raise ValueError("the console exposes profiles and mandates only")
            cursor = request.query_params.get("cursor") or None
            employee_id = request.path_params["employee_id"]
            result = await run(role_detail_value, employee_id, section, cursor)
            return JSONResponse(result)
        except (FirmError, TypeError, ValueError) as exc:
            return _error(400, "invalid_role_detail", str(exc))

    async def session_info(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        with runtime.store.read() as db:
            firm_id = runtime.store.firm(db)["firm_id"]
        return JSONResponse(
            {
                "principal": OWNER,
                "firm_id": firm_id,
                "console_scope": console_scope(runtime.store.path, firm_id),
            }
        )

    async def analysis(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        section = request.path_params.get(
            "section",
            "conversations" if "conversation_id" in request.path_params else "readiness",
        )
        if section not in SECTIONS:
            return _error(404, "not_found", "Analysis section does not exist")
        try:
            result = await run(
                AnalysisReader(
                    runtime.store.path, network_status=runtime.hosts.network_status
                ).read,
                section,
                from_at=request.query_params.get("from"),
                until=request.query_params.get("until"),
                employee_id=request.path_params.get("employee_id")
                or request.query_params.get("employee_id"),
                conversation_id=request.path_params.get("conversation_id"),
                limit=int(request.query_params.get("limit", "100")),
                cursor=request.query_params.get("cursor"),
                row_type=request.query_params.get("row_type"),
            )
            return JSONResponse(result)
        except AnalysisNotFound as exc:
            return _error(404, "not_found", str(exc))
        except (AnalysisQueryError, ValueError) as exc:
            return _error(400, "invalid_query", str(exc))
        except FirmError as exc:
            return _error(503, "analysis_unavailable", str(exc))

    async def missing(request: Request) -> Response:
        denied = require_session(request)
        if denied is not None:
            return denied
        return _error(404, "not_found", "Console route does not exist.")

    async def local_security(request: Request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if host != allowed_host:
            response: Response = _error(421, "invalid_host", "Host is not allowed.")
        elif request.method == "POST" and (
            origin != f"http://{allowed_host}"
            or fetch_site != "same-origin"
            or request.headers.get("x-firm-console") != "same-origin"
        ):
            response = _error(403, "cross_origin_refused", "Same-origin console request required.")
        elif (
            origin is not None and origin != f"http://{allowed_host}"
        ) or fetch_site == "cross-site":
            response = _error(403, "cross_origin_refused", "Cross-origin request refused.")
        else:
            try:
                response = await call_next(request)
            except Exception:
                LOGGER.exception("owner console request failed")
                response = _error(503, "unavailable", "Owner service is temporarily unavailable.")
        response.headers["Cache-Control"] = "no-store"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                f"form-action 'self'; frame-src http://{allowed_host}/api/files/preview"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=local_security)],
        routes=[
            Route("/unlock/{key}", unlock, methods=["GET"]),
            Route("/", asset, methods=["GET", "HEAD"]),
            *(Route(f"/{name}", asset, methods=["GET", "HEAD"], name=name)
              for name in STATIC_ASSETS),
            Route("/api/session", session_info, methods=["GET"]),
            Route("/api/snapshot", snapshot, methods=["GET"]),
            Route("/api/messages", messages, methods=["GET"]),
            Route("/api/inbox", inbox, methods=["GET"]),
            Route("/api/results", results, methods=["GET"]),
            Route("/api/incidents", incidents, methods=["GET"]),
            Route("/api/incidents/clear", clear_incident, methods=["POST"]),
            Route("/api/messages/operation", message_operation, methods=["GET"]),
            Route("/api/messages/send", send_message, methods=["POST"]),
            Route("/api/messages/reply", reply_message, methods=["POST"]),
            Route("/api/messages/dismiss", dismiss_messages, methods=["POST"]),
            Route("/api/requests", request_list, methods=["GET"]),
            Route("/api/requests/decide", decide_request, methods=["POST"]),
            Route("/api/files/roots", file_roots, methods=["GET"]),
            Route("/api/files/list", file_list, methods=["GET"]),
            Route("/api/files/read", file_read, methods=["GET"]),
            Route("/api/files/preview", file_preview, methods=["GET"]),
            Route("/api/control/status", control_status, methods=["GET"]),
            Route("/api/control/{action}", control_action, methods=["POST"]),
            Route("/api/roles/tree", role_tree, methods=["GET"]),
            Route("/api/roles/search", role_search, methods=["GET"]),
            Route("/api/roles/{employee_id}", role_detail, methods=["GET"]),
            Route("/api/analysis/roles/{employee_id}/readiness", analysis, methods=["GET"]),
            Route("/api/analysis/conversations/{conversation_id}", analysis, methods=["GET"]),
            Route("/api/analysis/{section}", analysis, methods=["GET"]),
            Route("/{path:path}", missing),
        ],
    )
    app.state.console_key = key
    return app
