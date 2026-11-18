"""Durable threads with attributed mail and independent recipient dispositions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from firm_sandbox import Layout

from .errors import FirmError
from .inbox_context import overview as context_overview
from .prompt_resources import render
from .response_limits import CONTROL_RESERVE_BYTES, DETAIL_RESULT_BYTES, LIST_RESULT_BYTES
from .store import OPERATIONS, ORDINARY_PRIORITY, OWNER, OWNER_PRIORITY, Store, encoded
from .summaries import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_MESSAGES,
    ContextSnapshot,
    SummaryService,
    select_context,
    utc_iso,
)
from .workspaces import sandbox_role_id

MAX_BODY_BYTES = 16 * 1024
MAX_PARTICIPANTS = 50
MAX_ATTACHMENTS = 10
MAX_PEEK_BYTES = LIST_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_RESULT_BYTES = DETAIL_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_SELECTION = 100
# How far back context follows a conversation into the ones it continues.
MAX_CONTINUATION_DEPTH = 5
NO_RECIPIENT = "no active or paused role at that address"

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence>=0)
);
INSERT OR IGNORE INTO message_sequence VALUES(1,0);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    audience_key TEXT NOT NULL,
    subject TEXT,
    audience_json TEXT NOT NULL,
    continues TEXT REFERENCES conversations(conversation_id),
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS no_subject_audience
    ON conversations(audience_key) WHERE subject IS NULL;
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    author_id TEXT NOT NULL,
    author_address TEXT NOT NULL,
    author_identity TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    created_at REAL NOT NULL,
    reply_to TEXT REFERENCES messages(message_id),
    regarding_kind TEXT CHECK(regarding_kind IN ('incident','request')),
    regarding_id TEXT,
    CHECK((regarding_kind IS NULL) = (regarding_id IS NULL))
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, sequence);
CREATE TABLE IF NOT EXISTS message_deliveries (
    message_id TEXT NOT NULL REFERENCES messages(message_id),
    employee_id TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'pending'
        CHECK(disposition IN ('pending','read','dismissed','undeliverable')),
    disposition_at REAL,
    read_sequence INTEGER,
    read_activation_id TEXT REFERENCES activations(activation_id),
    failure_reason TEXT,
    action_required INTEGER NOT NULL DEFAULT 1 CHECK(action_required IN (0, 1)),
    scheduling_priority INTEGER NOT NULL DEFAULT 0,
    scheduling_ready_at REAL,
    PRIMARY KEY(message_id, employee_id)
);
CREATE TABLE IF NOT EXISTS message_read_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence>=0)
);
INSERT OR IGNORE INTO message_read_sequence VALUES(1,0);
CREATE INDEX IF NOT EXISTS pending_mail
    ON message_deliveries(employee_id, disposition, message_id);
CREATE INDEX IF NOT EXISTS delivery_scheduling
    ON message_deliveries(employee_id, scheduling_ready_at)
    WHERE disposition='pending' AND scheduling_ready_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS reads_by_activation
    ON message_deliveries(read_activation_id, disposition_at);
CREATE TABLE IF NOT EXISTS delivery_bounces (
    original_message_id TEXT NOT NULL REFERENCES messages(message_id),
    recipient_id TEXT NOT NULL,
    bounce_message_id TEXT NOT NULL REFERENCES messages(message_id),
    PRIMARY KEY(original_message_id, recipient_id)
);
"""


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    digits = ""
    while value:
        value, remainder = divmod(value, 36)
        digits = alphabet[remainder] + digits
    return digits or "0"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _identity(role: dict[str, Any]) -> dict[str, str]:
    return {
        "employee_id": role["employee_id"],
        "address": role["address"],
        "identity": role["identity"],
    }


def _sorted_audience(people: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id = {person["employee_id"]: person for person in people}
    return [by_id[employee_id] for employee_id in sorted(by_id)]


def _audience_key(audience: list[dict[str, str]]) -> str:
    return _json(sorted(person["employee_id"] for person in audience))


class Messaging:
    def __init__(self, store: Store, *, summary_service: SummaryService | None = None):
        self.store = store
        self.summary_service = summary_service or SummaryService(store=store)
        # The execution host installs an OwnerNotifier; without one nothing is posted.
        self.notifier = None
        # The bound sandbox supplies the same owner browse roots used by the console.
        self.files = None

    def notify_owner(self, db: sqlite3.Connection, title: str, body: str, *, key) -> None:
        """Nudge the owner unless the firm's `[owner] notifications` turns it off."""
        if self.notifier is None:
            return
        firm = self.store.firm(db)
        enabled = json.loads(firm["config_json"]).get("owner", {}).get("notifications", True)
        self.notifier.notify_owner(
            f"{firm['firm_id']}: {title}", body, key=key, enabled=enabled
        )

    # ----------------------------------------------------------------- sending

    def send(
        self, actor_id: str, to_action: list[str], to_fyi: list[str], body: str, *,
        subject: str | None = None, attachments: list | None = None, request_id: str,
    ) -> dict:
        payload = {"to_action": to_action, "to_fyi": to_fyi, "body": body,
                   "subject": subject, "attachments": attachments}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            return self.store.request(
                db, actor_id, request_id, "send", payload,
                lambda: self._send_lists(db, actor_id, to_action, to_fyi, body,
                                        subject, attachments),
            )

    def _send_lists(self, db, actor_id, to_action, to_fyi, body, subject, attachments):
        for supplied in (to_action, to_fyi):
            self._validate_recipients(supplied, allow_empty=True)
        if not to_action and not to_fyi:
            raise FirmError("to_action and to_fyi may not both be empty")
        actions = {self._participant_key(p) for p in self._intended_recipients(db, to_action)}
        if actions & {self._participant_key(p) for p in self._intended_recipients(db, to_fyi)}:
            raise FirmError("to_action and to_fyi may not overlap")
        return self._send(db, actor_id, to_action + to_fyi, body, subject,
                          attachments=attachments, action_keys=sorted(actions))

    def _send(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        recipients: list[str],
        body: str,
        subject: str | None = None,
        priority: int | None = None,
        *,
        action_keys: list[str] | None = None,
        ready_at: float | None = None,
        regarding: dict | None = None,
        attachments: list | None = None,
        notification_title: str | None = None,
    ) -> dict:
        author = _identity(self.store.require_actor(db, actor_id))
        self._validate_content(body, subject)
        attached = self._attachments(actor_id, attachments)
        references = self._validate_recipients(recipients)
        accepted, failed = self._resolve_recipients(db, references)
        audience = _sorted_audience([author, *accepted])
        participants = self._intended_participants(author, audience, failed)
        if len(participants) > MAX_PARTICIPANTS:
            raise FirmError(f"a thread may contain at most {MAX_PARTICIPANTS} people")
        conversation_id, _ = self._conversation_for(db, audience, subject=subject)
        message_id = self._accept(
            db,
            author,
            accepted,
            failed,
            participants,
            body,
            subject,
            conversation_id=conversation_id,
            priority=OWNER_PRIORITY
            if actor_id == OWNER and priority is None
            else (ORDINARY_PRIORITY if priority is None else priority),
            action_keys=action_keys or [],
            ready_at=ready_at,
            regarding=regarding,
            attachments=attached,
            notification_title=notification_title,
        )
        return self._send_result(db, message_id)

    def notice(
        self,
        db: sqlite3.Connection,
        recipients: list[str],
        body: str,
        *,
        subject: str,
        priority: int = ORDINARY_PRIORITY,
        wake: bool = True,
        ready_at: float | None = None,
        regarding: dict | None = None,
    ) -> dict:
        if regarding is not None and (
            not isinstance(regarding, dict)
            or set(regarding) != {"kind", "id"}
            or regarding["kind"] not in ("incident", "request")
            or not isinstance(regarding["id"], str)
            or not regarding["id"]
        ):
            raise FirmError("regarding must name an incident or request")
        self._validate_content(body, subject)
        return self._send(
            db,
            OPERATIONS,
            recipients,
            subject + "\n\n" + body,
            priority=priority,
            action_keys=(self._normalize_actions(
                db, recipients, self._intended_recipients(db, recipients)
            ) if wake else []),
            ready_at=ready_at,
            regarding=regarding,
            notification_title=subject,
        )

    def reply(
        self, actor_id: str, message_id: str, action: list[str], body: str, *,
        attachments: list | None = None, request_id: str,
    ) -> dict:
        payload = {"message_id": message_id, "action": action, "body": body,
                   "attachments": attachments}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            return self.store.request(
                db, actor_id, request_id, "reply", payload,
                lambda: self._reply(db, actor_id, message_id, action, body, attachments),
            )

    def _thread(self, db, actor_id, message_id):
        source = self._message_row(db, message_id)
        thread = self._conversation_row(db, source["conversation_id"])
        audience = json.loads(thread["audience_json"])
        if actor_id not in {p["employee_id"] for p in audience}:
            raise FirmError("you are not a participant in that message's thread")
        return source, thread, audience

    def _reply(self, db, actor_id, message_id, action, body, attachments):
        source, thread, audience = self._thread(db, actor_id, message_id)
        result = self._continue(db, actor_id, source, thread["conversation_id"],
                                audience, [], action, body, attachments)
        if actor_id == OWNER:
            self._write_disposition(db, actor_id, [message_id], "dismissed")
        return result

    def introduce(
        self, actor_id: str, message_id: str, add: list[str], action: list[str],
        summarize: bool, body: str, *,
        request_id: str, session_id: str | None = None, activation_id: str | None = None,
    ) -> dict:
        if not isinstance(summarize, bool):
            raise FirmError("summarize must be true or false")
        payload = {"message_id": message_id, "add": add, "action": action,
                   "summarize": summarize, "body": body}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            prior = self.store.replay(db, actor_id, request_id, "introduce", payload)
            if prior is not None:
                return prior
            source, thread, audience, failed = self._introduction(db, actor_id, message_id, add)
            self._normalize_actions(db, action, [p for p in audience
                                                 if p["employee_id"] != actor_id])
            self._validate_content(body, thread["subject"])
            snapshot = self._context_snapshot(
                db, thread["conversation_id"],
            ) if summarize else None
        summary = self._summarize(snapshot, session_id) if snapshot is not None else None
        if summary is not None and summary.get("status") != "summarized":
            raise FirmError("summary unavailable; nothing sent. Call again, or call with "
                            "summarize=False and put the needed context in the body")
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            return self.store.request(
                db, actor_id, request_id, "introduce", payload,
                lambda: self._introduce(db, actor_id, message_id, add, action, body, summary),
            )

    def _introduction(self, db, actor_id, message_id, add):
        source, thread, current = self._thread(db, actor_id, message_id)
        accepted, failed = self._resolve_recipients(db, self._validate_recipients(add))
        current_ids = {p["employee_id"] for p in current}
        if not any(p["employee_id"] not in current_ids for p in accepted):
            if failed:
                names = ", ".join(p.get("reference", p.get("address", "")) for p in failed)
                raise FirmError("no deliverable new participants; unknown or retired: " + names)
            raise FirmError("everyone is already in this thread; use reply")
        audience = _sorted_audience([*current, *accepted])
        if len(audience) > MAX_PARTICIPANTS:
            raise FirmError(f"a thread may contain at most {MAX_PARTICIPANTS} people")
        return source, thread, audience, failed

    def _introduce(self, db, actor_id, message_id, add, action, body, summary):
        source, thread, audience, failed = self._introduction(db, actor_id, message_id, add)
        conversation_id, _ = self._conversation_for(
            db, audience, subject=thread["subject"], continues=thread["conversation_id"]
        )
        result = self._continue(db, actor_id, source, conversation_id, audience,
                                failed, action, body, None,
                                summary["summary"] if summary else None)
        result["continues"] = self._conversation_row(db, conversation_id)["continues"]
        if summary:
            result["summary"] = {"status": "attached", "truncated": bool(summary.get("truncated"))}
        return result

    def _continue(self, db, actor_id, source, conversation_id, audience, failed,
                  action, body, attachments, summary=None):
        self._validate_content(body, source["subject"])
        attached = self._attachments(actor_id, attachments)
        author = _identity(self.store.require_actor(db, actor_id))
        intended = [p for p in audience if p["employee_id"] != actor_id]
        actions = self._normalize_actions(db, action, intended)
        accepted = []
        failed = list(failed)
        for person in intended:
            if self._is_deliverable(db, person["employee_id"]):
                accepted.append(person)
            else:
                failed.append({**person, "reason": NO_RECIPIENT})
        new_id = self._accept(
            db, author, accepted, failed, self._intended_participants(author, audience, failed),
            body, source["subject"], conversation_id=conversation_id,
            reply_to=source["message_id"], summary=summary, attachments=attached,
            priority=OWNER_PRIORITY if actor_id == OWNER else ORDINARY_PRIORITY,
            action_keys=actions,
        )
        return self._send_result(db, new_id)

    def _summarize(self, snapshot, session_id):
        return self.summary_service.summarize(snapshot, caller_session_id=session_id)

    def _conversation_for(
        self,
        db: sqlite3.Connection,
        audience: list[dict[str, str]],
        *,
        continues: str | None = None,
        subject: str | None = None,
    ) -> tuple[str, bool]:
        """Reuse only the no-subject conversation; named sends always create a thread."""
        key = _audience_key(audience)
        row = db.execute(
            "SELECT conversation_id FROM conversations WHERE audience_key=? AND subject IS NULL",
            (key,),
        ).fetchone()
        if subject is None and row is not None:
            return row["conversation_id"], False
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversations"
        ).fetchone()[0]
        conversation_id = "c" + _base36(sequence)
        db.execute(
            "INSERT INTO conversations(conversation_id,sequence,audience_key,audience_json,"
            "continues,subject,created_at) VALUES(?,?,?,?,?,?,?)",
            (conversation_id, sequence, key, _json(audience), continues, subject,
             self.store.clock()),
        )
        return conversation_id, True

    # ------------------------------------------------------------------ inbox

    @staticmethod
    def pending_after(db, employee_id: str, sequence: int) -> list[sqlite3.Row]:
        """Pending deliveries that arrived after a recorded sequence, oldest first."""
        return db.execute(
            "SELECT m.sequence,m.author_id,m.author_address,m.author_identity "
            "FROM message_deliveries d JOIN messages m USING(message_id) "
            "WHERE d.employee_id=? AND d.disposition='pending' AND m.sequence>? "
            "ORDER BY m.sequence",
            (employee_id, sequence),
        ).fetchall()

    def peek(self, actor_id: str, *, activation_id: str | None = None) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            rows = self._pending_rows(db, actor_id)
            observed = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM message_sequence"
            ).fetchone()[0]
            result = self._overview(
                rows, actor_id, observed_sequence=observed, context=context_overview(
                    db, actor_id, self.store.clock(), byte_limit=min(4096, MAX_PEEK_BYTES // 3)
                ),
            )
            self.advance_offered(db, actor_id, self.offered_ids(db, actor_id, result))
            return result

    @staticmethod
    def _pending_rows(db, actor_id):
        return db.execute(
            "SELECT m.*,c.audience_json,d.action_required FROM message_deliveries d "
            "JOIN messages m USING(message_id) JOIN conversations c USING(conversation_id) "
            "WHERE d.employee_id=? AND d.disposition='pending' ORDER BY m.sequence",
            (actor_id,),
        ).fetchall()

    def _overview(self, rows, actor_id, *, context=None, newly_offered=False,
                  observed_sequence=None):
        grouped = {}
        for row in rows:
            grouped.setdefault(row["conversation_id"], []).append(row)

        def rank(messages):
            members = {p["employee_id"] for p in json.loads(messages[0]["audience_json"])}
            return (0 if OPERATIONS in members else 1 if OWNER in members else 2,
                    messages[0]["sequence"])

        # Idle review offers the oldest unoffered prefix so its frontier can advance.
        ordered = grouped.values() if newly_offered else sorted(grouped.values(), key=rank)
        items = [self._thread_entry(messages, actor_id) for messages in ordered]
        result = {"unread_count": len(rows), "thread_count": len(items), "threads": [],
                  "omitted_threads": len(items), "newly_offered": newly_offered}
        if rows and not newly_offered:
            result["newest_message_id"] = max(rows, key=lambda row: row["sequence"])["message_id"]
        if context is not None:
            result["context"] = context
        if observed_sequence is not None:
            result["observed_sequence"] = observed_sequence
        for item in items:
            trial = {**result, "threads": [*result["threads"], item],
                     "omitted_threads": result["omitted_threads"] - 1}
            if len(encoded(trial).encode("utf-8")) > MAX_PEEK_BYTES:
                break
            result = trial
        if items and not result["threads"]:
            raise FirmError("inbox entry exceeds the peek result limit")
        self._check_budget(result, MAX_PEEK_BYTES, "peek metadata")
        return result

    def _thread_entry(self, messages, actor_id):
        latest = messages[-1]
        others = [p for p in json.loads(latest["audience_json"]) if p["employee_id"] != actor_id]
        result = {
            "message_id": latest["message_id"], "subject": latest["subject"],
            "last_author": self._participant_preview([{
                "employee_id": latest["author_id"], "address": latest["author_address"],
                "identity": latest["author_identity"],
            }])[0],
            "participants": self._participant_preview(others),
            "participants_omitted": max(0, len(others) - 8),
            "unread_count": len(messages),
            "action_required": any(bool(row["action_required"]) for row in messages),
        }
        if latest["subject"] is None:
            result["preview"] = self._excerpt(" ".join(latest["body"].split()), 80)
        return result

    def _unread(self, db, actor_id, source):
        return db.execute(
            "SELECT m.* FROM message_deliveries d JOIN messages m USING(message_id) "
            "WHERE d.employee_id=? AND d.disposition='pending' "
            "AND m.conversation_id=? AND m.sequence<=? ORDER BY m.sequence",
            (actor_id, source["conversation_id"], source["sequence"]),
        ).fetchall()

    @staticmethod
    def _reading_base(source, audience, actor_id):
        return {"subject": source["subject"], "message_id": source["message_id"],
                "reader_id": actor_id,
                "participants": [p for p in audience if p["employee_id"] != actor_id]}

    def read(self, actor_id: str, message_id: str, summarize: bool = False, *,
             activation_id: str | None = None, session_id: str | None = None) -> dict:
        if not isinstance(summarize, bool):
            raise FirmError("summarize must be true or false")
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            source, _, audience = self._thread(db, actor_id, message_id)
            rows = self._unread(db, actor_id, source)
            result = {**self._reading_base(source, audience, actor_id),
                      "status": "read", "marked_read": [], "remaining": len(rows),
                      "reread": not rows, "sent_by_you": source["author_id"] == actor_id}
            if not summarize:
                selected = []
                for row in rows[:MAX_SELECTION] if rows else [source]:
                    item = self.envelope(row)
                    trial = {**result, "messages": [*selected, item],
                             "marked_read": [m["message_id"] for m in [*selected, item]]
                             if rows else [],
                             "remaining": max(0, len(rows) - len(selected) - 1)}
                    if len(encoded(trial).encode("utf-8")) > MAX_RESULT_BYTES:
                        break
                    selected.append(item)
                    result = trial
                if not selected:
                    raise FirmError("message exceeds read result limit")
                self._write_disposition(db, actor_id, result["marked_read"], "read",
                                        activation_id=activation_id)
                return result
            if rows:
                snapshot = self._read_snapshot(rows, source["conversation_id"])
            else:
                history = db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND sequence<=? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (source["conversation_id"], source["sequence"], MAX_CONTEXT_MESSAGES + 1),
                ).fetchall()
                snapshot = self._read_snapshot(history, source["conversation_id"])
        summary = self._summarize(snapshot, session_id)
        if summary.get("status") != "summarized":
            raise FirmError("summary unavailable; nothing was marked read. "
                            f'read(message_id="{message_id}") returns the full messages')
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            source, _, _ = self._thread(db, actor_id, message_id)
            pending = {r["message_id"] for r in self._unread(
                db, actor_id, source,
            )} if rows else set()
            covered = list(snapshot.source_message_ids)
            marked = [mid for mid in covered if mid in pending]
            self._write_disposition(db, actor_id, marked, "read", activation_id=activation_id)
            return {**result, "summary": summary["summary"], "covered_ids": covered,
                    "marked_read": marked,
                    "remaining": len(self._unread(db, actor_id, source))}

    def _read_snapshot(self, rows, conversation_id):
        selected, used = [], 0
        for row in rows[:MAX_CONTEXT_MESSAGES]:
            item = self.envelope(row)
            item["created_at"] = utc_iso(item["created_at"])
            size = len(encoded(item).encode("utf-8")) + bool(selected)
            if used + size > MAX_CONTEXT_BYTES:
                break
            used += size
            selected.append(item)
        selected.sort(key=lambda m: next(r["sequence"] for r in rows
                                        if r["message_id"] == m["message_id"]))
        return ContextSnapshot(conversation_id, tuple(selected),
                               tuple(m["message_id"] for m in selected), (), used, len(rows))

    def dismiss(self, actor_id: str, message_id: str, *,
                activation_id: str | None = None) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            source, _, audience = self._thread(db, actor_id, message_id)
            ids = [r["message_id"] for r in self._unread(db, actor_id, source)]
            self._write_disposition(db, actor_id, ids, "dismissed")
            return {**self._reading_base(source, audience, actor_id),
                    "status": "dismissed", "message_ids": ids, "remaining": 0}

    def clear(self, actor_id: str, message_id: str, *,
              activation_id: str | None = None) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            source, _, _ = self._thread(db, actor_id, message_id)
            rows = self._pending_rows(db, actor_id)
            selected = [row for row in rows if row["sequence"] <= source["sequence"]]
            self._write_disposition(db, actor_id, [row["message_id"] for row in selected],
                                    "dismissed")
            return {"status": "cleared", "message_id": message_id,
                    "cleared_count": len(selected),
                    "thread_count": len({row["conversation_id"] for row in selected}),
                    "remaining": len(rows) - len(selected)}

    def dismiss_exact(self, actor_id: str, message_ids: list[str], *, request_id: str) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            return self.store.request(db, actor_id, request_id, "dismiss",
                                      {"message_ids": message_ids},
                                      lambda: self._dismiss_exact(db, actor_id, message_ids))

    def _dismiss_exact(self, db, actor_id, message_ids):
        ids = self._validate_ids(message_ids)
        for mid in ids:
            self._thread(db, actor_id, mid)
        placeholders = ",".join("?" for _ in ids)
        waiting = db.execute(
            f"SELECT message_id FROM message_deliveries WHERE employee_id=? "
            f"AND disposition='pending' AND message_id IN ({placeholders})", (actor_id, *ids)
        ).fetchall()
        if len(waiting) != len(ids):
            raise FirmError("every selected message must be pending for this recipient")
        self._write_disposition(db, actor_id, ids, "dismissed")
        return {"status": "dismissed", "message_ids": ids}

    def _write_disposition(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        message_ids: list[str],
        disposition: str,
        *,
        activation_id: str | None = None,
    ) -> None:
        if not message_ids:
            return
        read_sequence = None
        if disposition == "read":
            read_sequence = db.execute(
                "UPDATE message_read_sequence SET sequence=sequence+1 "
                "WHERE singleton=1 RETURNING sequence"
            ).fetchone()[0]
        placeholders = ",".join("?" for _ in message_ids)
        db.execute(
            f"UPDATE message_deliveries SET disposition=?,disposition_at=?,read_sequence=?,"
            f"read_activation_id=? WHERE employee_id=? AND disposition='pending' "
            f"AND message_id IN ({placeholders})",
            (
                disposition,
                self.store.clock(),
                read_sequence,
                activation_id if disposition == "read" else None,
                actor_id,
                *message_ids,
            ),
        )

    @staticmethod
    def _activation(db: sqlite3.Connection, actor_id: str, activation_id: str) -> sqlite3.Row:
        activation = db.execute(
            "SELECT employee_id,session_id,state "
            "FROM activations WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
        if (
            activation is None
            or activation["employee_id"] != actor_id
            or activation["state"] not in {"admitted", "running"}
        ):
            raise FirmError("activation does not authorize this inbox operation")
        return activation

    # ---------------------------------------------------------------- lookup

    def _context_snapshot(self, db: sqlite3.Connection, conversation_id: str):
        """Newest messages of the conversation and, behind them, of what it continues."""
        chain = []
        current: str | None = conversation_id
        while current is not None and len(chain) <= MAX_CONTINUATION_DEPTH:
            chain.append(current)
            current = self._conversation_row(db, current)["continues"]
        placeholders = ",".join("?" for _ in chain)
        matching_count = db.execute(
            f"SELECT COUNT(*) FROM messages WHERE conversation_id IN ({placeholders})", chain
        ).fetchone()[0]
        # The conversation's own messages first, newest first, then the one it continues,
        # so its history never displaces what was said in it.
        rows: list[dict] = []
        for member in chain:
            rows.extend(
                dict(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? "
                    "ORDER BY sequence DESC, message_id DESC LIMIT ?",
                    (member, MAX_CONTEXT_MESSAGES + 1 - len(rows)),
                )
            )
            if len(rows) > MAX_CONTEXT_MESSAGES:
                break
        return select_context(
            rows, conversation_id, envelope=self.envelope, matching_count=matching_count
        )

    def recent_reads(
        self,
        db: sqlite3.Connection,
        employee_id: str,
        session_id: str,
        limit: int = 5,
    ) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise FirmError("limit must be a nonnegative integer")
        rows = db.execute(
            "SELECT m.message_id,m.subject,m.body,d.disposition_at AS read_at "
            "FROM message_deliveries d "
            "JOIN messages m ON m.message_id=d.message_id "
            "JOIN activations a ON a.activation_id=d.read_activation_id "
            "WHERE d.employee_id=? AND d.disposition='read' AND a.session_id=? "
            "ORDER BY d.read_sequence DESC,m.sequence DESC LIMIT ?",
            (employee_id, session_id, limit),
        ).fetchall()
        return [{"message_id": row["message_id"], "subject": row["subject"],
                 "read_at": row["read_at"],
                 **({"preview": self._excerpt(" ".join(row["body"].split()), 80)}
                    if row["subject"] is None else {})} for row in rows]

    def retire(self, db: sqlite3.Connection, employee_id: str) -> dict:
        pending = db.execute(
            "SELECT d.message_id,m.author_id,m.subject FROM message_deliveries d "
            "JOIN messages m USING(message_id) WHERE d.employee_id=? AND d.disposition='pending' "
            "ORDER BY m.sequence",
            (employee_id,),
        ).fetchall()
        now = self.store.clock()
        db.execute(
            "UPDATE message_deliveries SET disposition='undeliverable',disposition_at=?,"
            "failure_reason='recipient retired' WHERE employee_id=? AND disposition='pending'",
            (now, employee_id),
        )
        bounces: list[str] = []
        for delivery in pending:
            author_id = delivery["author_id"]
            if author_id == OPERATIONS or not self._is_deliverable(db, author_id):
                continue
            prior = db.execute(
                "SELECT bounce_message_id FROM delivery_bounces "
                "WHERE original_message_id=? AND recipient_id=?",
                (delivery["message_id"], employee_id),
            ).fetchone()
            if prior is not None:
                bounces.append(prior["bounce_message_id"])
                continue
            body = render(
                "notices",
                "delivery_bounce",
                message_id=delivery["message_id"],
                employee_id=employee_id,
            )
            bounce = self.notice(
                db,
                [author_id],
                body,
                subject=render(
                    "notices", "delivery_bounce_subject", message_id=delivery["message_id"]
                ),
            )
            bounce_id = bounce["message_id"]
            db.execute(
                "INSERT INTO delivery_bounces(original_message_id,recipient_id,bounce_message_id) "
                "VALUES(?,?,?)",
                (delivery["message_id"], employee_id, bounce_id),
            )
            bounces.append(bounce_id)
        return {"employee_id": employee_id, "undeliverable": len(pending), "bounces": bounces}

    # --------------------------------------------------------------- storage

    def _accept(
        self,
        db: sqlite3.Connection,
        author: dict[str, str],
        accepted: list[dict[str, str]],
        failed: list[dict[str, str]],
        participants: list[dict[str, str]],
        body: str,
        subject: str | None,
        *,
        conversation_id: str,
        reply_to: str | None = None,
        summary: str | None = None,
        priority: int,
        action_keys: list[str],
        ready_at: float | None = None,
        regarding: dict | None = None,
        attachments: list[dict[str, str]] | None = None,
        notification_title: str | None = None,
    ) -> str:
        attached = [] if attachments is None else attachments
        sequence = db.execute(
            "UPDATE message_sequence SET sequence=sequence+1 WHERE singleton=1 RETURNING sequence"
        ).fetchone()[0]
        message_id = "m" + _base36(sequence)
        recipient_records = [{**person, "status": "queued",
                              "action_required": self._participant_key(person) in action_keys}
                             for person in accepted] + [
            {**failure, "status": "failed"} for failure in failed
        ]
        candidate = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "author": author,
            "recipients": recipient_records,
            "participants": participants,
            "subject": subject,
            "body": body,
            "attachments": attached,
            "created_at": self.store.clock(),
            "reply_to": reply_to,
        }
        if summary is not None:
            candidate["summary"] = summary
        self._check_budget(
            {**self._reading_base(
                candidate, json.loads(self._conversation_row(db, conversation_id)["audience_json"]),
                "",
            ), "status": "read", "messages": [candidate], "marked_read": [message_id],
             "remaining": 0, "reread": False, "sent_by_you": False},
            MAX_RESULT_BYTES,
            "message envelope",
        )
        db.execute(
            "INSERT INTO messages(message_id,sequence,conversation_id,author_id,author_address,"
            "author_identity,recipients_json,participants_json,subject,body,attachments_json,"
            "summary,created_at,reply_to,regarding_kind,regarding_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                sequence,
                conversation_id,
                author["employee_id"],
                author["address"],
                author["identity"],
                _json(recipient_records),
                _json(participants),
                subject,
                body,
                _json(attached),
                summary,
                candidate["created_at"],
                reply_to,
                regarding["kind"] if regarding else None,
                regarding["id"] if regarding else None,
            ),
        )
        for recipient in accepted:
            employee_id = recipient["employee_id"]
            action_required = self._participant_key(recipient) in action_keys
            scheduling_ready_at = (
                (candidate["created_at"] if ready_at is None else ready_at)
                if action_required and employee_id not in {OWNER, OPERATIONS}
                else None
            )
            db.execute(
                "INSERT INTO message_deliveries(message_id,employee_id,action_required,"
                "scheduling_priority,scheduling_ready_at) VALUES(?,?,?,?,?)",
                (message_id, employee_id, int(action_required), priority, scheduling_ready_at),
            )
            if employee_id == OWNER:
                self.notify_owner(
                    db,
                    render("notices", "owner_mail_title", address=author["address"]),
                    notification_title or subject or self._excerpt(" ".join(body.split()), 80),
                    key=(author["employee_id"], "mail"),
                )
        return message_id

    def _send_result(self, db: sqlite3.Connection, message_id: str) -> dict:
        message = self._envelopes(db, [message_id])[0]
        return {
            "status": "sent",
            "message_id": message_id,
            "conversation_id": message["conversation_id"],
            "queued": [item for item in message["recipients"] if item["status"] == "queued"],
            "failed": [item for item in message["recipients"] if item["status"] == "failed"],
        }

    def _envelopes(self, db: sqlite3.Connection, message_ids: list[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in message_ids)
        rows = db.execute(
            f"SELECT * FROM messages WHERE message_id IN ({placeholders})", message_ids
        ).fetchall()
        by_id = {row["message_id"]: row for row in rows}
        if len(by_id) != len(message_ids):
            raise FirmError("one or more message IDs are unknown")
        return [self.envelope(by_id[message_id]) for message_id in message_ids]

    @staticmethod
    def envelope(row: sqlite3.Row) -> dict:
        """Serialize a message row without reading or changing runtime state."""
        envelope = {
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "author": {
                "employee_id": row["author_id"],
                "address": row["author_address"],
                "identity": row["author_identity"],
            },
            "recipients": json.loads(row["recipients_json"]),
            "participants": json.loads(row["participants_json"]),
            "subject": row["subject"],
            "body": row["body"],
            "attachments": json.loads(row["attachments_json"]),
            "created_at": row["created_at"],
            "reply_to": row["reply_to"],
        }
        if row["summary"] is not None:
            envelope["summary"] = row["summary"]
        return envelope

    def _attachments(self, actor_id: str, attachments: list | None) -> list[dict[str, str]]:
        supplied = [] if attachments is None else attachments
        if not isinstance(supplied, list) or len(supplied) > MAX_ATTACHMENTS:
            raise FirmError(f"attachments must be a list of at most {MAX_ATTACHMENTS} files")
        if not supplied:
            return []
        if self.files is None:
            raise FirmError("file browser is unavailable for attachments")
        mapped = []
        drive = Layout.for_role(sandbox_role_id(actor_id)).drive if actor_id != OWNER else None
        for item in supplied:
            if actor_id == OWNER:
                if not isinstance(item, dict) or set(item) != {"path", "name"}:
                    raise FirmError("owner attachments require path and name")
                path, name = item["path"], item["name"]
            else:
                if not isinstance(item, str) or not item.startswith(drive + "/"):
                    raise FirmError("attachment must be an absolute path under the sender's drive")
                if ".." in item.split("/"):
                    raise FirmError("attachment path must not contain '..'")
                relative = item[len(drive) + 1:]
                path, _, name = relative.rpartition("/")
            try:
                if actor_id == OWNER:
                    path, name = self.files.validate_browse_file(path, name)
                else:
                    path, name = self.files.validate_role_file(
                        sandbox_role_id(actor_id), path, name
                    )
            except ValueError as exc:
                raise FirmError(str(exc)) from exc
            mapped.append({"path": path, "name": name})
        return mapped

    @classmethod
    def _participant_preview(cls, participants: list[dict[str, str]]) -> list[dict[str, str]]:
        preview: list[dict[str, str]] = []
        for person in participants[:8]:
            if "employee_id" in person:
                preview.append(
                    {
                        "employee_id": cls._excerpt(person["employee_id"]),
                        "address": cls._excerpt(person["address"]),
                        "identity": cls._excerpt(person["identity"]),
                    }
                )
            else:
                preview.append({"reference": cls._excerpt(person["reference"])})
        return preview

    @staticmethod
    def _excerpt(value: str, limit: int = 60) -> str:
        return value if len(value) <= limit else value[:limit] + "…"

    def _intended_recipients(self, db, references: list[str]) -> list[dict]:
        accepted, failed = self._resolve_recipients(
            db, self._validate_recipients(references, allow_empty=True),
        )
        by_key = {self._participant_key(p): p for p in [*accepted, *failed]}
        return list(by_key.values())

    def _normalize_actions(self, db, supplied, intended: list[dict]) -> list[str]:
        roster = {self._participant_key(p): p for p in intended}
        if not isinstance(supplied, list) or len(supplied) > MAX_PARTICIPANTS:
            raise FirmError("action must be a list of recipient addresses or IDs")
        actions = set()
        for reference in supplied:
            if not isinstance(reference, str) or not reference or reference != reference.strip():
                raise FirmError("action must contain plain recipient addresses or IDs")
            # Prefer the immutable audience over whatever now owns a reused address.
            matches = {key for key, p in roster.items() if reference in {
                p.get("employee_id"), p.get("address"), p.get("reference")
            }}
            if len(matches) != 1:
                raise FirmError("action recipient must identify one of this message's recipients")
            actions.update(matches)
        return sorted(actions)

    def offered_ids(self, db, actor_id, result):
        ids = []
        for thread in result.get("threads", []):
            source = self._message_row(db, thread["message_id"])
            ids.extend(r["message_id"] for r in self._unread(db, actor_id, source))
        return ids

    def advance_offered(self, db, actor_id: str, offered_ids: list[str]) -> int:
        """Cross only pending mail actually returned in this serialized write transaction."""
        if actor_id in {OWNER, OPERATIONS}:
            return 0
        old = self.store.role(db, actor_id)["last_offered_message_sequence"]
        offered = set(offered_ids)
        rows = db.execute(
            "SELECT m.sequence,m.message_id FROM message_deliveries d "
            "JOIN messages m USING(message_id) WHERE d.employee_id=? "
            "AND d.disposition='pending' AND m.sequence>? ORDER BY m.sequence",
            (actor_id, old),
        ).fetchall()
        frontier = old
        for row in rows:
            if row["message_id"] not in offered:
                break
            frontier = row["sequence"]
        if frontier > old:
            db.execute("UPDATE roles SET last_offered_message_sequence=? WHERE employee_id=?",
                       (frontier, actor_id))
        return frontier

    def idle_rows(self, db, actor_id: str, now: float) -> list[sqlite3.Row]:
        rows = db.execute(
            "SELECT m.*,c.audience_json,d.action_required,d.scheduling_ready_at "
            "FROM message_deliveries d "
            "JOIN messages m USING(message_id) JOIN conversations c USING(conversation_id) "
            "JOIN roles r ON r.employee_id=d.employee_id WHERE d.employee_id=? "
            "AND d.disposition='pending' AND m.sequence>r.last_offered_message_sequence "
            "ORDER BY m.sequence", (actor_id,),
        ).fetchall()
        prefix = []
        for row in rows:
            if row["scheduling_ready_at"] is not None and row["scheduling_ready_at"] > now:
                break
            prefix.append(row)
        return prefix

    def idle_snapshot(self, db, actor_id: str, now: float) -> dict:
        boundaries = {}
        for row in self.idle_rows(db, actor_id, now):
            boundaries[row["conversation_id"]] = row["sequence"]
        pending = self._pending_rows(db, actor_id)
        rows = [row for thread, boundary in boundaries.items() for row in pending
                if row["conversation_id"] == thread and row["sequence"] <= boundary]
        return self._overview(rows, actor_id, newly_offered=True)

    def _resolve_recipients(
        self, db: sqlite3.Connection, references: list[str]
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        accepted_by_id: dict[str, dict[str, str]] = {}
        failed: list[dict[str, str]] = []
        seen_failed: set[str] = set()
        for reference in references:
            role = self.store.resolve(db, reference)
            if role is None or role["employment"] == "retired":
                if reference not in seen_failed:
                    evidence: dict[str, str] = {
                        "reference": reference,
                        "reason": NO_RECIPIENT,
                    }
                    if role is not None:
                        evidence.update(_identity(role))
                    failed.append(evidence)
                    seen_failed.add(reference)
                continue
            person = _identity(role)
            accepted_by_id.setdefault(person["employee_id"], person)
        return _sorted_audience(list(accepted_by_id.values())), failed

    @classmethod
    def _intended_participants(
        cls,
        author: dict[str, str],
        addressed: list[dict[str, str]],
        failed: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        participants: dict[str, dict[str, str]] = {
            cls._participant_key(author): author,
        }
        for person in [*addressed, *failed]:
            key = cls._participant_key(person)
            if key in participants:
                continue
            if "employee_id" in person:
                participants[key] = {
                    field: person[field] for field in ("employee_id", "address", "identity")
                }
            else:
                participants[key] = {"reference": person["reference"]}
        return [participants[key] for key in sorted(participants)]

    @staticmethod
    def _participant_key(person: dict[str, str]) -> str:
        if "employee_id" in person:
            return "employee:" + person["employee_id"]
        return "unresolved:" + person["reference"]

    def _is_deliverable(self, db: sqlite3.Connection, employee_id: str) -> bool:
        role = self.store.resolve(db, employee_id)
        return role is not None and role["employment"] in {"active", "paused"}

    @staticmethod
    def _validate_content(body: str, subject: str | None) -> None:
        if not isinstance(body, str) or not body:
            raise FirmError("message body must be a nonempty string")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise FirmError(f"message body exceeds {MAX_BODY_BYTES} UTF-8 bytes")
        if subject is not None and (not isinstance(subject, str) or not 0 < len(subject) <= 80):
            raise FirmError("a supplied subject must contain 1–80 characters")

    @staticmethod
    def _validate_recipients(recipients: list[str], *, allow_empty: bool = False) -> list[str]:
        if not isinstance(recipients, list) or (not recipients and not allow_empty):
            raise FirmError("recipients must be a nonempty list")
        if len(recipients) > MAX_PARTICIPANTS:
            raise FirmError(f"at most {MAX_PARTICIPANTS} recipients may be supplied")
        if any(not isinstance(item, str) or not item.strip() for item in recipients):
            raise FirmError("recipient references must be nonempty strings")
        return recipients

    @staticmethod
    def _validate_ids(message_ids: list[str]) -> list[str]:
        if not isinstance(message_ids, list) or not message_ids:
            raise FirmError("message_ids must be a nonempty list")
        if len(message_ids) > MAX_SELECTION:
            raise FirmError(f"at most {MAX_SELECTION} message IDs may be selected")
        if any(not isinstance(message_id, str) or not message_id for message_id in message_ids):
            raise FirmError("message IDs must be nonempty strings")
        if len(set(message_ids)) != len(message_ids):
            raise FirmError("message IDs must be unique")
        return message_ids

    @staticmethod
    def _check_budget(value: Any, limit: int, label: str) -> None:
        if len(encoded(value).encode("utf-8")) > limit:
            raise FirmError(render("references", "message_budget", label=label, limit=limit))

    @staticmethod
    def _message_row(db: sqlite3.Connection, message_id: str) -> sqlite3.Row:
        if not isinstance(message_id, str) or not message_id:
            raise FirmError("message_id must be a nonempty string")
        row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise FirmError("unknown message ID")
        return row

    @staticmethod
    def _conversation_row(db: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        if not isinstance(conversation_id, str) or not conversation_id:
            raise FirmError("conversation must be a nonempty string")
        row = db.execute(
            "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise FirmError("unknown conversation ID")
        return row
