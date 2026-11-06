"""What roles need from the owner: one record, one owner decision, one wake on the answer.

A request is a row in ``owner_requests`` with a free-text ``kind`` and a payload of
``{resource, purpose, cause, ...}``. This module reads only those three payload fields; a
kind owns what its own fields mean, what granting does and how it is enforced. A kind
offers ``name``, ``ask`` (for kinds a role asks for; it answers a result, ``GrantNow``,
``AskOwner`` or ``Unrecorded``), ``grant(row)`` and ``deny(row, note)``; those two open
their own transaction, re-read the row as pending, apply the decision and call ``answer``.
"""

import json
from collections.abc import Callable
from typing import NamedTuple

from . import analysis_history
from .errors import FirmError
from .prompt_resources import render
from .store import OWNER, encoded

MAX_PURPOSE = 300
MAX_REASON = 1000
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_requests (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    activation_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','granted','denied','withdrawn')),
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT CHECK(decided_by IN ('owner','runtime')),
    decision_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_request_per_role_kind_and_resource
    ON owner_requests(employee_id,kind,json_extract(payload_json,'$.resource'))
    WHERE status='pending';
"""


class AskOwner(NamedTuple):
    """A kind's answer to ``ask`` when only the owner can lift the block."""

    resource: str
    cause: str
    payload: dict | None = None


class GrantNow(NamedTuple):
    """A kind's answer to ``ask`` that grants at once: ``apply(db)`` writes the grant in the
    transaction that records the call, and returns the role's answer."""

    apply: Callable


class Unrecorded(NamedTuple):
    """A kind's answer to ``ask`` that is returned but not stored, so a retry asks again."""

    result: dict


def require_owner(actor):
    if actor != OWNER:
        raise FirmError("only the owner may decide requests")


def normalize_denial_reason(reason: object) -> str:
    """Validate the owner note after trimming surrounding whitespace."""
    if not isinstance(reason, str):
        raise FirmError(f"reason must contain 1–{MAX_REASON} characters")
    reason = reason.strip()
    if not 1 <= len(reason) <= MAX_REASON:
        raise FirmError(f"reason must contain 1–{MAX_REASON} characters")
    return reason


def pending_requests(db, now):
    """Requests waiting on the owner, oldest first, with the requesting role's address."""
    return [
        {
            "request_id": row["request_id"],
            "kind": row["kind"],
            "employee_id": row["employee_id"],
            "address": row["address"],
            **json.loads(row["payload_json"]),
            "created_at": row["created_at"],
            "age_seconds": max(0.0, now - row["created_at"]),
        }
        for row in db.execute(
            "SELECT q.*,r.address FROM owner_requests q JOIN roles r USING(employee_id) "
            "WHERE q.status='pending' ORDER BY q.created_at,q.rowid"
        )
    ]


def record_github(db, now, actor, repository, purpose):
    """GitHub grants are immediate; they join the owner's one list of what roles were given."""
    _insert(
        db,
        now,
        "github_repository",
        actor["employee_id"],
        actor["activation_id"],
        {"resource": repository, "purpose": purpose, "cause": None},
        "granted",
        decided_at=now,
        decided_by="runtime",
    )


def withdraw_role(db, now, employee_id):
    """A retired role asks for nothing any more."""
    for row in db.execute(
        "SELECT * FROM owner_requests WHERE employee_id=? AND status='pending'", (employee_id,)
    ).fetchall():
        _decide(db, now, row, "withdrawn", decided_by="runtime")


def _insert(
    db, now, kind, employee_id, activation_id, payload, status, *,
    decided_at=None, decided_by=None,
):
    number = db.execute("SELECT COALESCE(MAX(rowid),0)+1 FROM owner_requests").fetchone()[0]
    request_id = f"q{number}"
    db.execute(
        "INSERT INTO owner_requests(request_id,kind,employee_id,activation_id,payload_json,"
        "status,created_at,decided_at,decided_by) VALUES(?,?,?,?,?,?,?,?,?)",
        (request_id, kind, employee_id, activation_id, encoded(payload), status, now,
         decided_at, decided_by),
    )
    return request_id


def _decide(db, now, row, status, note=None, *, decided_by=None):
    db.execute(
        "UPDATE owner_requests SET status=?,decided_at=?,decision_note=?,decided_by=? "
        "WHERE request_id=?",
        (status, now, note, decided_by, row["request_id"]),
    )
    analysis_history.record(
        db,
        now,
        f"request.{status}",
        row["employee_id"],
        request_id=row["request_id"],
        kind=row["kind"],
        resource=json.loads(row["payload_json"]).get("resource"),
    )


class OwnerRequests:
    """Open requests for roles; decide them for the owner. ``kinds`` are the request kinds."""

    def __init__(self, runtime, kinds):
        self.runtime, self.store = runtime, runtime.store
        self.kinds = {kind.name: kind for kind in kinds}

    # ------------------------------------------------------------------ roles

    def request_access(self, actor, request_id, kind, resource, purpose):
        """A role asks for access to something that exists: granted, refused or one request."""
        employee_id = actor["employee_id"]
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > MAX_PURPOSE:
            raise FirmError(f"purpose must contain 1–{MAX_PURPOSE} characters")
        payload = {"kind": kind, "resource": resource, "purpose": purpose}
        with self.store.read() as db:
            prior = self.store.replay(db, employee_id, request_id, "request_access", payload)
        if prior is not None:
            return prior
        # The kind may consult the sandbox or a service, so it is asked outside any transaction.
        # A kind without ``ask`` is opened by the runtime, never by a role.
        handler = self.kinds.get(kind)
        outcome = (
            {"status": "refused", "reason": "access_kind_unknown"}
            if handler is None or not hasattr(handler, "ask")
            else handler.ask(actor, resource, purpose.strip())
        )
        if isinstance(outcome, Unrecorded):
            return outcome.result
        with self.store.transaction() as db:

            def action():
                if isinstance(outcome, AskOwner):
                    return self.ask_owner(
                        db,
                        actor,
                        kind,
                        outcome.resource,
                        purpose.strip(),
                        outcome.cause,
                        **(outcome.payload or {}),
                    )
                if isinstance(outcome, GrantNow):
                    return outcome.apply(db)
                return outcome

            return self.store.request(
                db, employee_id, request_id, "request_access", payload, action
            )

    def ask_owner(self, db, actor, kind, resource, purpose, cause, *, standing=True, **payload):
        """Open the role's request, inside the caller's transaction, and answer ``pending``.

        A standing denial of the same kind and resource refuses instead, with the owner's
        note, unless the kind says its denials do not stand (the resource's state changes);
        a role that already has the request open gets that one back, with the latest cause
        and purpose, since the block it hits now is what the owner must lift.
        """
        employee_id, activation_id = actor["employee_id"], actor["activation_id"]
        denied = None
        if standing:
            denied = db.execute(
                "SELECT decision_note FROM owner_requests WHERE kind=? AND status='denied' "
                "AND json_extract(payload_json,'$.resource')=? ORDER BY rowid DESC",
                (kind, resource),
            ).fetchone()
        if denied is not None:
            return {
                "status": "refused",
                "reason": "access_denied",
                "resource": resource,
                "owner_note": denied["decision_note"],
            }
        # Each role holds its own request, so each is answered when the owner decides.
        existing = db.execute(
            "SELECT request_id,payload_json FROM owner_requests WHERE kind=? "
            "AND json_extract(payload_json,'$.resource')=? AND employee_id=? "
            "AND status='pending'",
            (kind, resource, employee_id),
        ).fetchone()
        if existing is not None:
            stored = json.loads(existing["payload_json"])
            if stored["cause"] != cause:
                db.execute(
                    "UPDATE owner_requests SET payload_json=? WHERE request_id=?",
                    (
                        encoded({**stored, "purpose": purpose, "cause": cause, **payload}),
                        existing["request_id"],
                    ),
                )
            return self._pending_answer(existing["request_id"], cause)
        now = self.store.clock()
        opened = _insert(
            db,
            now,
            kind,
            employee_id,
            activation_id,
            {"resource": resource, "purpose": purpose, "cause": cause, **payload},
            "pending",
        )
        analysis_history.record(
            db,
            now,
            "request.opened",
            employee_id,
            activation_id,
            request_id=opened,
            kind=kind,
            resource=resource,
            purpose=purpose,
        )
        address = self.store.role(db, employee_id)["address"]
        self.runtime.messaging.notify_owner(
            db,
            render("notices", "access_requested_title", address=address),
            f"{resource}: {purpose}",
            key=(employee_id, "request"),
        )
        return self._pending_answer(opened, cause)

    @staticmethod
    def _pending_answer(request_id, cause):
        # The key is ``cause``, not ``reason``: nothing here is a refusal.
        return {
            "status": "pending",
            "request_id": request_id,
            "cause": cause,
            "message": render("results", "access_pending"),
        }

    def answer(self, db, rows, status, note=None, *, decided_by=None):
        """Decide each row and tell its role. Granted and denied answers wake the role."""
        for row in rows:
            _decide(db, self.store.clock(), row, status, note, decided_by=decided_by)
            if status in {"granted", "denied"}:
                resource = json.loads(row["payload_json"])["resource"]
                notice = "access_granted" if status == "granted" else "access_denied"
                self.runtime.messaging.notice(
                    db,
                    [row["employee_id"]],
                    render("notices", notice, resource=resource, reason=note),
                    subject=render("notices", f"{notice}_subject", resource=resource),
                    regarding={"kind": "request", "id": row["request_id"]},
                )
        return [row["request_id"] for row in rows]

    # ------------------------------------------------------------------ owner

    def listing(self, actor, *, recent=20):
        """Pending requests first, then the most recent decisions."""
        require_owner(actor)
        with self.store.read() as db:
            now = self.store.clock()
            decided = [
                {
                    "request_id": row["request_id"],
                    "kind": row["kind"],
                    "employee_id": row["employee_id"],
                    "address": row["address"],
                    **json.loads(row["payload_json"]),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "decided_at": row["decided_at"],
                    "decided_by": row["decided_by"],
                    "decision_note": row["decision_note"],
                }
                for row in db.execute(
                    "SELECT q.*,r.address FROM owner_requests q JOIN roles r USING(employee_id) "
                    "WHERE q.status!='pending' ORDER BY q.decided_at DESC,q.rowid DESC LIMIT ?",
                    (recent,),
                )
            ]
            return {"pending": pending_requests(db, now), "recent": decided}

    def status(self, actor, request_id):
        """The current durable outcome for one owner request, including its retry state."""
        require_owner(actor)
        with self.store.read() as db:
            row = db.execute(
                "SELECT q.request_id,q.kind,q.employee_id,r.address,q.payload_json,q.status,"
                "q.created_at,q.decided_at,q.decided_by,q.decision_note FROM owner_requests q "
                "JOIN roles r USING(employee_id) WHERE q.request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise FirmError(f"unknown request {request_id}")
            return {
                "request_id": row["request_id"],
                "kind": row["kind"],
                "employee_id": row["employee_id"],
                "address": row["address"],
                **json.loads(row["payload_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                "decision_note": row["decision_note"],
            }

    def pending(self, db, request_id):
        """The request, which must exist and still be waiting on the owner."""
        row = db.execute(
            "SELECT * FROM owner_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise FirmError(f"unknown request {request_id}")
        if row["status"] != "pending":
            raise FirmError(f"request {request_id} is already {row['status']}")
        return row

    def _kind(self, row):
        return self.kinds[row["kind"]]

    def grant(self, actor, request_id):
        require_owner(actor)
        with self.store.read() as db:
            row = self.pending(db, request_id)
        return self._kind(row).grant(row)

    def deny(self, actor, request_id, reason):
        """Decline a request; the kind says who else the decision covers."""
        require_owner(actor)
        reason = normalize_denial_reason(reason)
        with self.store.read() as db:
            row = self.pending(db, request_id)
        return self._kind(row).deny(row, reason)
