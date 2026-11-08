"""Durable admission and execution transitions, independent of a provider process."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from . import analysis_history
from .accounts import Accounts
from .discovery import Discovery, identity
from .errors import FirmError
from .execution_activity import record_activity
from .github import GitHubAccess
from .hosts import NetworkHosts
from .levels import Levels
from .lifecycle import Lifecycle
from .mac_lease import MacLease
from .messaging import Messaging
from .orientation import orientation_for_admission
from .owner import OwnerControls
from .prompt_resources import render
from .reminders import Reminders
from .requests import OwnerRequests
from .scheduling import acknowledge_opportunities
from .store import CONTROL_PRIORITY, OPERATIONS, OWNER, Store, encoded
from .summaries import SummaryService
from .tasks import TaskService
from .telemetry import ContextTelemetry, handoff_checklist

ACTIVE = ("admitted", "running")
RETRY_DELAYS = (60, 300, 1800)
WAKE_REASONS = {
    "message",
    "owner_resume",
    "owner_fresh_route",
    "renew_temporary",
    "promote",
    "authorized_retry",
    "dependency_recovery",
}


def _next_recovery(role: dict) -> str:
    revision = int(role["recovery_episode"].rsplit(":", 1)[1]) + 1
    return f"{role['employee_id']}:{revision}"


@dataclass(frozen=True)
class Invocation:
    activation_id: str
    employee_id: str
    token: str = field(repr=False)
    session_id: str
    context: dict


class Runtime:
    def __init__(self, store: Store, *, task_runner=None):
        self.store = store
        self.accounts = Accounts(self)
        self.task_service = TaskService(self, task_runner)
        self.summary_service = SummaryService(store=store, capacity=self.accounts)
        self.messaging = Messaging(store, summary_service=self.summary_service)
        self.lifecycle = Lifecycle(store, self.messaging)
        self.appointments = self.lifecycle.appointments
        self.context = ContextTelemetry(self)
        self.discovery = Discovery(store)
        self.levels = Levels(self)
        self.reminders = Reminders(store, self.messaging)
        self.owner = OwnerControls(self)
        self.mac = MacLease(self)
        self.github = GitHubAccess(self)
        self.hosts = NetworkHosts(self)
        self.requests = OwnerRequests(self, [self.hosts, self.github, self.mac])

    def status(self) -> dict:
        with self.store.read() as db:
            firm = self.store.firm(db)
            return {
                "firm_id": firm["firm_id"],
                "state": firm["state"],
                "active": [
                    dict(r)
                    for r in db.execute(
                        "SELECT activation_id,employee_id,state,drain_requested_at,"
                        "drain_presented_at,next_intent FROM activations "
                        "WHERE state IN ('admitted','running') ORDER BY created_at"
                    )
                ],
                "waiting": db.execute(
                    "SELECT COUNT(DISTINCT o.employee_id) FROM scheduling_opportunities o "
                    "WHERE NOT EXISTS(SELECT 1 FROM activations a WHERE "
                    "a.employee_id=o.employee_id AND a.state IN ('admitted','running'))"
                ).fetchone()[0],
                "temporary_appointments": [
                    dict(r)
                    for r in db.execute(
                        "SELECT t.* FROM appointments t JOIN roles r USING(employee_id) "
                        "WHERE t.kind='temporary' AND r.employment!='retired' ORDER BY r.address"
                    )
                ],
                "held": [
                    dict(r)
                    for r in db.execute(
                        "SELECT employee_id,hold_reason,recovery_episode,attempt_count,retry_at "
                        "FROM roles WHERE hold_reason IS NOT NULL OR retry_at IS NOT NULL"
                    )
                ],
            }

    def start(self) -> dict:
        self.summary_service.open()
        with self.store.transaction() as db:
            firm = self.store.firm(db)
            if firm["state"] in {"draining", "stopping"}:
                raise FirmError("finish stopping existing execution before starting")
            host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
            if host is not None and host["infrastructure_state"] != "running":
                raise FirmError("firm VMs are not ready")
            db.execute("UPDATE firm SET state='running'")
            analysis_history.firm_changed(db, self.store.clock(), firm["state"])
            return self.status()

    def stop(self, force: bool = False) -> dict:
        """Close admission. Dispatcher must separately verify actual execution exit."""
        self.summary_service.close()
        with self.store.transaction() as db:
            firm = self.store.firm(db)
            active = list(
                db.execute(
                    "SELECT employee_id FROM activations WHERE state IN ('admitted','running')"
                )
            )
            state = "stopping" if force or firm["state"] == "stopping" else "draining"
            host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
            stopping = active or (host is not None and host["infrastructure_state"] != "stopped")
            db.execute("UPDATE firm SET state=?", (state if stopping else "stopped",))
            analysis_history.firm_changed(db, self.store.clock(), firm["state"])
            if active and not force:
                db.execute(
                    "UPDATE activations SET drain_requested_at=COALESCE(drain_requested_at,?) "
                    "WHERE state IN ('admitted','running')",
                    (self.store.clock(),),
                )
            return self.status()

    def _mail_waiting(
        self, db, activation: dict, *, observed_sequence: int | None = None
    ) -> dict | None:
        """Mail after the last observed sequence, preserving arrivals beyond a peek watermark."""
        employee_id = activation["employee_id"]
        seen = activation["mail_presented_sequence"] or 0
        if observed_sequence is not None:
            seen = max(seen, observed_sequence)
            db.execute(
                "UPDATE activations SET mail_presented_sequence=? WHERE activation_id=?",
                (seen, activation["activation_id"]),
            )
        arrivals = self.messaging.pending_after(db, employee_id, seen)
        if not arrivals:
            return None
        db.execute(
            "UPDATE activations SET mail_presented_sequence=? WHERE activation_id=?",
            (arrivals[-1]["sequence"], activation["activation_id"]),
        )
        manager_id = self.store.role(db, employee_id)["manager_id"]
        senders: dict[str, dict] = {}
        for row in arrivals:
            sender = senders.setdefault(
                row["author_id"],
                {
                    "sender": {
                        "employee_id": row["author_id"],
                        "address": row["author_address"],
                        "identity": row["author_identity"],
                    },
                    "count": 0,
                    "oldest": row["sequence"],
                },
            )
            sender["count"] += 1
        def rank(author_id: str) -> int:
            if author_id == OWNER:
                return 0
            if author_id == OPERATIONS:
                return 1
            return 2 if author_id == manager_id else 3

        ordered = sorted(senders.items(), key=lambda item: (rank(item[0]), item[1]["oldest"]))
        # Order carries the priority: owner, Operations, manager, then oldest arrival.
        # The sequence range says which mail this notice covered, so a later claim of it is
        # measurable as a reaction to the notice rather than to the arrival.
        analysis_history.record(
            db,
            self.store.clock(),
            "mail.presented",
            employee_id,
            activation["activation_id"],
            previous=seen,
            value=arrivals[-1]["sequence"],
            messages=len(arrivals),
            senders=len(senders),
        )
        return {"senders": [[item["sender"], item["count"]] for _, item in ordered]}

    def present_controls(
        self, activation_id: str, result: dict, *, observed_inbox: bool = False
    ) -> dict:
        """Attach only controls targeting this call's activation; presentation isn't reading."""
        with self.store.transaction() as db:
            activation = self._activation(db, activation_id)
            presented = dict(result)
            observed_sequence = (
                self.messaging.observation_sequence(
                    db, activation["employee_id"], activation_id, result
                )
                if observed_inbox
                else None
            )
            mail = self._mail_waiting(db, activation, observed_sequence=observed_sequence)
            if mail is not None:
                presented["mail_waiting"] = mail
            if observed_inbox and result.get("seen_sequence") is not None:
                # A peek fixes what a later clear in this work period may set aside.
                db.execute(
                    "UPDATE activations SET inbox_seen_sequence=MAX("
                    "COALESCE(inbox_seen_sequence,0),?) WHERE activation_id=?",
                    (result["seen_sequence"], activation_id),
                )
            pressure = self.context.guidance(db, activation)
            if pressure is not None:
                presented["context_pressure"] = pressure
            drain = activation["drain_requested_at"] is not None
            saved = activation["next_intent"] is not None
            if drain:
                db.execute(
                    "UPDATE activations SET drain_presented_at=COALESCE(drain_presented_at,?) "
                    "WHERE activation_id=?",
                    (self.store.clock(), activation_id),
                )
                presented["operations_control"] = {
                    "kind": "drain",
                    "activation_id": activation_id,
                    "requested_at": activation["drain_requested_at"],
                    "instruction": render("controls", "saved" if saved else "drain"),
                }
            required = pressure is not None and pressure["level"] == "handoff_required"
            if (required or drain) and not saved:
                # One presentation per work period and observed memory compaction.
                # This is best-effort presentation history, independent of claim gating.
                session = db.execute(
                    "SELECT native_compactions FROM context_sessions WHERE session_id=?",
                    (activation["session_id"],),
                ).fetchone()
                compactions = (session["native_compactions"] if session else None) or 0
                if activation["handoff_presented_compactions"] != compactions:
                    control = pressure if required else presented["operations_control"]
                    control["handoff_checklist"] = handoff_checklist()
                    db.execute(
                        "UPDATE activations SET handoff_presented_compactions=? "
                        "WHERE activation_id=?",
                        (compactions, activation_id),
                    )
            return presented

    def admission_candidates(self, db, *, now=None):
        """Runnable employees in admission order."""
        if now is None:
            now = self.store.clock()
        return db.execute(
            "WITH due AS (SELECT *,MAX(priority) OVER (PARTITION BY employee_id) "
            "AS top_priority FROM scheduling_opportunities WHERE ready_at<=?) "
            "SELECT r.employee_id,w.priority,MIN(w.ready_at) AS oldest "
            "FROM roles r JOIN due w USING(employee_id) "
            "WHERE r.employment='active' AND r.hold_reason IS NULL "
            "AND (r.retry_at IS NULL OR r.retry_at<=?) AND w.priority=w.top_priority "
            "AND NOT EXISTS(SELECT 1 FROM hire_levels h WHERE h.employee_id=r.employee_id "
            "AND h.state='pending') "
            "AND NOT EXISTS(SELECT 1 FROM activations a WHERE a.employee_id=r.employee_id "
            "AND a.state IN ('admitted','running')) GROUP BY r.employee_id "
            "ORDER BY priority DESC,oldest,r.employee_id",
            (now, now),
        ).fetchall()

    def admit(self) -> Invocation | None:
        """Reserve a slot atomically; only a dispatcher owning execution may call this."""
        with self.store.transaction() as db:
            firm = self.store.firm(db)
            if firm["state"] != "running":
                return None
            self.reminders.release_due(db)
            active = db.execute(
                "SELECT COUNT(*) FROM activations WHERE state IN ('admitted','running')"
            ).fetchone()[0]
            if active >= json.loads(firm["config_json"])["max_active"]:
                return None
            now = self.store.clock()
            candidates = self.admission_candidates(db, now=now)
            managed = self.accounts.enabled(db)
            role = selected = None
            for candidate in candidates:
                if not self.appointments.eligible(db, candidate["employee_id"]):
                    continue
                candidate_role = self.store.role(db, candidate["employee_id"])
                selected = self.accounts.select(db, candidate_role) if managed else None
                if not managed or selected is not None:
                    role = candidate_role
                    break
            if role is None:
                return None
            reasons = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM scheduling_opportunities WHERE employee_id=? AND ready_at<=? "
                    "ORDER BY ready_at,created_at,reason,source_id,execution_request_id",
                    (role["employee_id"], now),
                )
            ]
            public = self.lifecycle.get_role(role["employee_id"], role["employee_id"])
            author = self.store.resolve(db, public["mandate"]["author_id"])
            if author:
                public["mandate"]["author_address"] = author["address"]
            fresh = role["session_id"] is None
            manager = self.store.resolve(db, role["manager_id"])
            context = {
                "employee_id": role["employee_id"],
                "address": role["address"],
                "identity": identity(role),
                "manager_id": role["manager_id"],
                "manager_address": manager["address"] if manager else role["manager_id"],
                "mandate_revision": public["mandate"]["revision"],
                "mandate": public["mandate"],
                "mandate_changed": not fresh
                and role["session_mandate_revision"] != public["mandate"]["revision"],
                "birth": not bool(role["birth_delivered"]),
                "fresh_session": fresh,
                "wake_reason": " ".join(
                    dict.fromkeys(
                        render("wake", item["reason"])
                        for item in reasons
                        if item["reason"] in WAKE_REASONS
                    )
                )
                or render("wake", "default"),
            }
            terms = self.appointments.terms(db, role["employee_id"])
            if terms["kind"] == "temporary":
                context["temporary_appointment"] = terms
            if fresh and role["handoff_notes"] is not None:
                context["handoff_notes"] = role["handoff_notes"]
            if role["recovery_json"] is not None:
                context["recovery"] = json.loads(role["recovery_json"])
            orientation = orientation_for_admission(
                self.store,
                db,
                role["employee_id"],
                birth=context["birth"],
                fresh_session=fresh,
            )
            if orientation is not None:
                context["orientation"] = orientation
            session_id = role["session_id"] or str(uuid4())
            if managed:
                self.accounts.bind(db, role, session_id, selected)
            self.context.ensure_session(db, role, session_id)
            activation_id, token = str(uuid4()), secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO activations(activation_id,employee_id,session_id,state,token_hash,"
                "wake_snapshot,context_json,created_at) VALUES(?,?,?,'admitted',?,?,?,?)",
                (
                    activation_id,
                    role["employee_id"],
                    session_id,
                    self._hash(token),
                    encoded(reasons),
                    encoded(context),
                    now,
                ),
            )
            db.execute(
                "UPDATE roles SET session_id=?,retry_at=NULL WHERE employee_id=?",
                (session_id, role["employee_id"]),
            )
            acknowledge_opportunities(db, role["employee_id"], now)
            context["inbox"] = self.messaging.peek(
                role["employee_id"], activation_id=activation_id
            )
            # The wake update is this activation's first sight of its inbox: it counts
            # as a peek, so a later clear may set aside what it showed.
            inbox_observation = self.messaging.observation_sequence(
                db, role["employee_id"], activation_id, context["inbox"]
            )
            db.execute(
                "UPDATE activations SET context_json=?,mail_presented_sequence=?,"
                "inbox_seen_sequence=? WHERE activation_id=?",
                (
                    encoded(context),
                    inbox_observation or 0,
                    context["inbox"]["seen_sequence"],
                    activation_id,
                ),
            )
            return Invocation(activation_id, role["employee_id"], token, session_id, context)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _authenticate(self, db, token: str) -> dict:
        if not isinstance(token, str) or not token:
            raise FirmError("invalid activation token")
        row = db.execute(
            "SELECT a.* FROM activations a JOIN roles r USING(employee_id) "
            "WHERE a.token_hash=? AND a.state='running' AND r.employment='active'",
            (self._hash(token),),
        ).fetchone()
        if row is None or self.store.firm(db)["state"] == "stopping":
            raise FirmError("invalid or expired activation token")
        return dict(row)

    def authenticate(self, token: str) -> dict:
        with self.store.read() as db:
            return self._authenticate(db, token)

    def begin_execution(self, token: str) -> dict:
        """Guest launcher handshake: gate-checked release of one admitted activation."""
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM activations WHERE token_hash=?", (self._hash(token),)
            ).fetchone()
            if row is None or row["state"] not in ACTIVE:
                raise FirmError("invalid or expired activation token")
            if row["state"] == "admitted":
                if row["launch_attempted_at"] is None:
                    raise FirmError("execution launch was not authorized")
                self.mark_started(row["activation_id"])
            else:
                self._authenticate(db, token)
            record_activity(self.store, row["activation_id"])
            return {
                "status": "ready",
                "activation_id": row["activation_id"],
                "employee_id": row["employee_id"],
            }

    @contextmanager
    def authorized(self, token: str):
        # Authorization and every nested domain mutation share a single write transaction.
        with self.store.transaction() as db:
            actor = self._authenticate(db, token)
            record_activity(self.store, actor["activation_id"])
            yield actor

    def mark_launch_attempted(self, activation_id: str) -> None:
        """Count actual launch attempts, never mere capacity reservations."""
        with self.store.transaction() as db:
            row = self._activation(db, activation_id)
            if row["state"] != "admitted":
                raise FirmError("activation is not awaiting launch")
            if self.store.firm(db)["state"] != "running":
                raise FirmError("firm admission is closed")
            if self.store.role(db, row["employee_id"])["employment"] != "active":
                raise FirmError("employee is no longer active")
            if row["launch_attempted_at"] is None:
                db.execute(
                    "UPDATE activations SET launch_attempted_at=? WHERE activation_id=?",
                    (self.store.clock(), activation_id),
                )
                db.execute(
                    "UPDATE roles SET attempt_count=attempt_count+1 WHERE employee_id=?",
                    (row["employee_id"],),
                )

    def mark_started(self, activation_id: str) -> None:
        with self.store.transaction() as db:
            self.mark_launch_attempted(activation_id)
            row = self._activation(db, activation_id)
            context = json.loads(row["context_json"])
            if row["started_at"] is None:
                self.appointments.consume(db, row["employee_id"])
            db.execute(
                "UPDATE activations SET state='running',started_at=? WHERE activation_id=?",
                (self.store.clock(), activation_id),
            )
            db.execute(
                "UPDATE roles SET birth_delivered=1,handoff_notes=NULL,recovery_json=NULL,"
                "session_mandate_revision=? WHERE employee_id=?",
                (context["mandate_revision"], row["employee_id"]),
            )

    def _activation(self, db, activation_id: str) -> dict:
        row = db.execute("SELECT * FROM activations WHERE activation_id=?", (activation_id,))
        result = row.fetchone()
        if result is None:
            raise FirmError("unknown activation")
        return dict(result)

    def handoff(self, token: str, notes: str, next: str, request_id: str) -> dict:
        if next not in {"continue", "wait"}:
            raise FirmError("next must be continue or wait")
        if not isinstance(notes, str) or len(notes.encode()) > 128 * 1024:
            raise FirmError("handoff notes must fit in 128 KiB")
        with self.authorized(token) as activation:
            with self.store.transaction() as db:

                def action():
                    if activation["next_intent"] is not None:
                        raise FirmError("this activation already has a handoff")
                    db.execute(
                        "UPDATE activations SET handoff_notes=?,next_intent=? "
                        "WHERE activation_id=?",
                        (notes, next, activation["activation_id"]),
                    )
                    analysis_history.record(
                        db,
                        self.store.clock(),
                        "activation.handoff_saved",
                        activation["employee_id"],
                        activation["activation_id"],
                        next=next,
                    )
                    return {"status": "saved", "next": next, "execution_still_active": True}

                return self.store.request(
                    db,
                    # A handoff saves this work period, unlike durable employee operations.
                    # A retry after recovery must not reuse another period's saved result.
                    f"activation:{activation['activation_id']}",
                    request_id,
                    "handoff",
                    {"notes": notes, "next": next},
                    action,
                    activation_id=activation["activation_id"],
                )

    def finish(
        self,
        activation_id: str,
        outcome: str = "completed",
        *,
        failure: str | None = None,
        category: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        """Called only after the executor confirms exit (or launch never occurred)."""
        if outcome not in {"completed", "failed", "interrupted"}:
            raise FirmError("invalid execution outcome")
        if category not in {
            "unknown",
            "transient",
            "refusal",
            "owner_stop",
            "host_restart",
            "auth",
            "config",
            "quota",
        }:
            raise FirmError("failure category requires a provider/account adapter")
        with self.store.transaction() as db:
            activation = self._activation(db, activation_id)
            if activation["state"] not in ACTIVE:
                return
            if outcome == "completed" and category != "unknown":
                raise FirmError("completed execution cannot have a failure category")
            if outcome == "failed" and activation["launch_attempted_at"] is None:
                raise FirmError("a failed launch must have a recorded attempt")
            role = self.store.role(db, activation["employee_id"])
            now = self.store.clock()
            if category in {"auth", "config", "quota"}:
                if (
                    self.accounts.record_failure(
                        db, activation["session_id"], category, retry_after
                    )
                    is None
                ):
                    raise FirmError("failure category requires a provider/account adapter")
            db.execute(
                "UPDATE activations SET state=?,token_hash=NULL,finished_at=?,failure_category=?,"
                "failure=? WHERE activation_id=?",
                (outcome, now, category, failure, activation_id),
            )
            db.execute(
                "DELETE FROM discovery_continuations WHERE activation_id=?", (activation_id,)
            )
            if outcome == "completed":
                if activation["state"] != "running":
                    raise FirmError("unstarted execution cannot complete")
                db.execute(
                    "UPDATE roles SET attempt_count=0,recovery_episode=?,retry_at=NULL "
                    "WHERE employee_id=?",
                    (_next_recovery(role), role["employee_id"]),
                )
                if activation["next_intent"] is not None:
                    db.execute(
                        "UPDATE roles SET session_id=NULL,handoff_notes=? WHERE employee_id=?",
                        (activation["handoff_notes"], role["employee_id"]),
                    )
                    if activation["next_intent"] == "continue" and role["employment"] != "retired":
                        self.store.request_execution(db, role["employee_id"], "handoff")
            else:
                if activation["started_at"] is None:
                    if json.loads(activation["context_json"])["fresh_session"]:
                        db.execute(
                            "UPDATE roles SET session_id=NULL WHERE employee_id=?",
                            (role["employee_id"],),
                        )
                if activation["started_at"] is not None:
                    recovery = {
                        "cause": category,
                        "time_utc": datetime.fromtimestamp(now, UTC).isoformat(),
                        "time_is_detection": True,
                        "context_retained": True,
                        "recent_claims": self.messaging.recent_claims(
                            db, role["employee_id"], activation["session_id"]
                        ),
                    }
                    db.execute(
                        "UPDATE roles SET recovery_json=?,handoff_notes=NULL WHERE employee_id=?",
                        (encoded(recovery), role["employee_id"]),
                    )
                setup_retry = category == "transient" and activation["launch_attempted_at"] is None
                attempts = role["attempt_count"] + int(setup_retry)
                if setup_retry:
                    db.execute(
                        "UPDATE roles SET attempt_count=? WHERE employee_id=?",
                        (attempts, role["employee_id"]),
                    )
                if (category == "owner_stop"
                        or (activation["launch_attempted_at"] is None and not setup_retry)):
                    if activation["launch_attempted_at"] is not None:
                        db.execute(
                            "UPDATE roles SET attempt_count=MAX(0,attempt_count-1) "
                            "WHERE employee_id=?",
                            (role["employee_id"],),
                        )
                    if role["employment"] != "retired":
                        self.store.request_execution(db, role["employee_id"], "interrupted_work")
                elif category in {"auth", "config", "quota"}:
                    db.execute(
                        "UPDATE roles SET attempt_count=MAX(0,attempt_count-1) WHERE employee_id=?",
                        (role["employee_id"],),
                    )
                    if role["employment"] != "retired":
                        self.store.request_execution(db, role["employee_id"], "dependency_recovery")
                elif role["employment"] != "retired":
                    if category == "refusal" or attempts >= 4:
                        reason = "refusal" if category == "refusal" else "attempts_exhausted"
                        db.execute(
                            "UPDATE roles SET hold_reason=?,retry_at=NULL WHERE employee_id=?",
                            (reason, role["employee_id"]),
                        )
                        self.messaging.notice(
                            db,
                            [role["manager_id"]],
                            render(
                                "notices",
                                "held",
                                address=role["address"],
                                reason=reason,
                                episode=role["recovery_episode"],
                            ),
                            subject=render("notices", "held_subject"),
                            priority=CONTROL_PRIORITY,
                        )
                    else:
                        config = json.loads(self.store.firm(db)["config_json"])
                        delays = config.get("retry_delays", RETRY_DELAYS)
                        ready = max(now + delays[attempts - 1], retry_after or 0)
                        db.execute(
                            "UPDATE roles SET retry_at=? WHERE employee_id=?",
                            (ready, role["employee_id"]),
                        )
                        self.store.request_execution(
                            db, role["employee_id"], "recovery", ready_at=ready
                        )
            analysis_history.role_changed(
                db,
                now,
                role,
                role["employee_id"],
                activation_id=activation_id,
                category=category,
            )
            self._settle_stop(db)

    def _settle_stop(self, db):
        if not db.execute(
            "SELECT 1 FROM activations WHERE state IN ('admitted','running')"
        ).fetchone():
            live_host = db.execute(
                "SELECT infrastructure_state FROM execution_host WHERE singleton=1"
            ).fetchone()
            if live_host is None or live_host["infrastructure_state"] == "stopped":
                changed = db.execute(
                    "UPDATE firm SET state='stopped' WHERE state IN ('draining','stopping')"
                )
                if changed.rowcount:
                    analysis_history.record(
                        db,
                        self.store.clock(),
                        "firm.state_changed",
                        after="stopped",
                    )

    def retry(self, actor_id: str, employee_id: str, episode: str, request_id: str) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            role = self.store.role(db, employee_id)
            if actor_id != OWNER and role["manager_id"] != actor_id:
                raise FirmError("only the current direct manager or owner can retry this role")

            def action():
                if role["employment"] == "retired":
                    raise FirmError("retired employees cannot be retried")
                if role["recovery_episode"] != episode:
                    raise FirmError("stale recovery episode")
                if role["hold_reason"] not in {"attempts_exhausted", "refusal"}:
                    raise FirmError("role is not held for an isolated execution failure")
                new_episode = _next_recovery(role)
                db.execute(
                    "UPDATE roles SET hold_reason=NULL,retry_at=NULL,attempt_count=0,"
                    "recovery_episode=? WHERE employee_id=?",
                    (new_episode, employee_id),
                )
                self.store.request_execution(db, employee_id, "authorized_retry")
                analysis_history.record(
                    db,
                    self.store.clock(),
                    "role.retry_authorized",
                    employee_id,
                    previous_episode=episode,
                    recovery_episode=new_episode,
                )
                return {
                    "status": "queued",
                    "employee_id": employee_id,
                    "recovery_episode": new_episode,
                    "execution_dependency_blocked": self.accounts.enabled(db)
                    and self.accounts.select(db, self.store.role(db, employee_id)) is None,
                }

            return self.store.request(
                db,
                actor_id,
                request_id,
                "retry",
                {"employee_id": employee_id, "episode": episode},
                action,
            )

    def tools(self, token: str):
        from .tools import RoleTools

        return RoleTools(self, token)

    def record_usage(self, activation_id: str, usage, *, historical: bool = False) -> bool:
        """Historical usage updates the activation without refreshing live session telemetry."""
        return self.context.record(activation_id, usage, historical=historical)

    def record_tool_calls(self, activation_id: str, tally: dict) -> None:
        self.context.record_tool_calls(activation_id, tally)

    def usage_diagnostics(
        self,
        actor_id: str = OWNER,
        *,
        employee_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        return self.context.diagnostics(actor_id, employee_id=employee_id, limit=limit)
