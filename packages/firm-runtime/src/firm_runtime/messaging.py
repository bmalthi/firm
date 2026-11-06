"""Durable, attributed mail with independent per-recipient dispositions.

A conversation is a set of people. Every message belongs to the conversation of its
author plus the recipients it reached, so the same set of people always talk in the same
place, and bringing someone new in moves the talk to the conversation of the larger set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from typing import Any

from firm_sandbox import Layout

from .errors import FirmError
from .inbox_context import overview as context_overview
from .prompt_resources import render
from .response_limits import CONTROL_RESERVE_BYTES, DETAIL_RESULT_BYTES, LIST_RESULT_BYTES
from .store import OPERATIONS, ORDINARY_PRIORITY, OWNER, OWNER_PRIORITY, Store, encoded
from .summaries import MAX_CONTEXT_MESSAGES, SummaryService, select_context
from .workspaces import sandbox_role_id

MAX_BODY_BYTES = 16 * 1024
MAX_PARTICIPANTS = 50
MAX_ATTACHMENTS = 10
MAX_PEEK_BYTES = LIST_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_RESULT_BYTES = DETAIL_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_SELECTION = 100
# Waiting messages listed under one conversation in a peek; the rest are counted.
MAX_LISTED_PER_CONVERSATION = 5
# Inbox cursors are signed continuation keys, not numeric offsets.
MAX_INBOX_CURSOR_CHARACTERS = 512
# How far back context follows a conversation into the ones it continues.
MAX_CONTINUATION_DEPTH = 5
NO_RECIPIENT = "no active or paused role at that address"

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_sequence (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    audience_key TEXT NOT NULL UNIQUE,
    audience_json TEXT NOT NULL,
    continues TEXT REFERENCES conversations(conversation_id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    author_id TEXT NOT NULL,
    author_address TEXT NOT NULL,
    author_identity TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    subject TEXT NOT NULL,
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
        CHECK(disposition IN ('pending','claimed','dismissed','undeliverable')),
    disposition_at REAL,
    claim_sequence INTEGER,
    claimed_activation_id TEXT REFERENCES activations(activation_id),
    failure_reason TEXT,
    scheduling_priority INTEGER NOT NULL DEFAULT 0,
    scheduling_ready_at REAL,
    PRIMARY KEY(message_id, employee_id)
);
CREATE TABLE IF NOT EXISTS message_claim_sequence (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS pending_mail
    ON message_deliveries(employee_id, disposition, message_id);
CREATE INDEX IF NOT EXISTS delivery_scheduling
    ON message_deliveries(employee_id, scheduling_ready_at)
    WHERE disposition='pending' AND scheduling_ready_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS claims_by_activation
    ON message_deliveries(claimed_activation_id, disposition_at);
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
        self,
        actor_id: str,
        recipients: list[str],
        body: str,
        *,
        subject: str | None = None,
        attachments: list | None = None,
        request_id: str,
    ) -> dict:
        supplied = [] if attachments is None else attachments
        payload = {"recipients": recipients, "body": body, "subject": subject,
                   "attachments": supplied}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            return self.store.request(
                db,
                actor_id,
                request_id,
                "send",
                payload,
                lambda: self._send(db, actor_id, recipients, body, subject=subject,
                                   attachments=supplied),
            )

    def _send(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        recipients: list[str],
        body: str,
        subject: str | None = None,
        priority: int | None = None,
        *,
        wake: bool = True,
        ready_at: float | None = None,
        regarding: dict | None = None,
        attachments: list | None = None,
    ) -> dict:
        author = _identity(self.store.require_actor(db, actor_id))
        self._validate_content(body, subject)
        attached = self._attachments(actor_id, attachments)
        references = self._validate_recipients(recipients)
        accepted, failed = self._resolve_recipients(db, references)
        audience = _sorted_audience([author, *accepted])
        participants = self._intended_participants(author, audience, failed)
        if len(participants) > MAX_PARTICIPANTS:
            raise FirmError(f"a conversation may contain at most {MAX_PARTICIPANTS} people")
        conversation_id, _ = self._conversation_for(db, audience)
        message_id = self._accept(
            db,
            author,
            accepted,
            failed,
            participants,
            body,
            self._subject(body, subject),
            conversation_id=conversation_id,
            priority=OWNER_PRIORITY
            if actor_id == OWNER and priority is None
            else (ORDINARY_PRIORITY if priority is None else priority),
            wake=wake,
            ready_at=ready_at,
            regarding=regarding,
            attachments=attached,
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
        return self._send(
            db,
            OPERATIONS,
            recipients,
            body,
            subject=subject,
            priority=priority,
            wake=wake,
            ready_at=ready_at,
            regarding=regarding,
        )

    def reply(
        self,
        actor_id: str,
        message_id: str,
        body: str,
        *,
        add: list[str] | None = None,
        summarize: bool = False,
        attachments: list | None = None,
        request_id: str,
        session_id: str | None = None,
    ) -> dict:
        """Reply into the message's conversation, or into the larger one an addition makes.

        A summary of the conversation being continued is derived before the write
        transaction opens, because it is a provider call; a retry of the same request
        returns the stored result without deriving it again.
        """
        additions = [] if add is None else add
        supplied = [] if attachments is None else attachments
        if not isinstance(summarize, bool):
            raise FirmError("summarize must be true or false")
        payload = {"message_id": message_id, "body": body, "add": additions,
                   "attachments": supplied}
        if summarize:
            payload["summarize"] = True
        summary: dict | None = None
        if summarize:
            with self.store.read() as db:
                self.store.require_actor(db, actor_id)
                prior = self.store.replay(db, actor_id, request_id, "reply", payload)
                if prior is not None:
                    return prior
                target = self._reply_target(db, actor_id, message_id, additions)
                if target["conversation_id"] == target["source"]["conversation_id"]:
                    raise FirmError(
                        "summarize applies only when add brings someone new into the conversation"
                    )
                snapshot = self._context_snapshot(db, target["source"]["conversation_id"])
            summary = self.summary_service.summarize(snapshot, caller_session_id=session_id)
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            return self.store.request(
                db,
                actor_id,
                request_id,
                "reply",
                payload,
                lambda: self._reply(db, actor_id, message_id, body, additions, summary,
                                    supplied),
            )

    def _reply_target(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        message_id: str,
        additions: list[str],
    ) -> dict:
        """Where a reply goes: the source's conversation, or the one its additions make."""
        source = self._message_row(db, message_id)
        conversation = self._conversation_row(db, source["conversation_id"])
        current = json.loads(conversation["audience_json"])
        current_ids = {person["employee_id"] for person in current}
        if actor_id not in current_ids:
            raise FirmError("only a member of this conversation may reply to it")
        failed: list[dict[str, str]] = []
        audience = current
        if additions:
            accepted, failed = self._resolve_recipients(db, self._validate_recipients(additions))
            audience = _sorted_audience([*current, *accepted])
            if len(audience) > MAX_PARTICIPANTS:
                raise FirmError(f"a conversation may contain at most {MAX_PARTICIPANTS} people")
        target_id = source["conversation_id"]
        if {person["employee_id"] for person in audience} != current_ids:
            existing = db.execute(
                "SELECT conversation_id FROM conversations WHERE audience_key=?",
                (_audience_key(audience),),
            ).fetchone()
            target_id = existing["conversation_id"] if existing else None
        return {
            "source": source,
            "conversation_id": target_id,
            "audience": audience,
            "failed": failed,
        }

    def _reply(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        message_id: str,
        body: str,
        additions: list[str],
        summary: dict | None,
        attachments: list,
    ) -> dict:
        self._validate_content(body, None)
        attached = self._attachments(actor_id, attachments)
        target = self._reply_target(db, actor_id, message_id, additions)
        source = target["source"]
        author = _identity(self.store.require_actor(db, actor_id))
        accepted: list[dict[str, str]] = []
        failed = list(target["failed"])
        for person in target["audience"]:
            if person["employee_id"] == actor_id:
                continue
            role = self.store.resolve(db, person["employee_id"])
            if role is None or role["employment"] == "retired":
                failed.append({**person, "reason": NO_RECIPIENT})
            else:
                accepted.append(person)
        participants = self._intended_participants(author, accepted, failed)
        if len(participants) > MAX_PARTICIPANTS:
            raise FirmError(f"a conversation may contain at most {MAX_PARTICIPANTS} people")
        conversation_id = target["conversation_id"]
        created = False
        if conversation_id is None:
            conversation_id, created = self._conversation_for(
                db, target["audience"], continues=source["conversation_id"]
            )
        moved = conversation_id != source["conversation_id"]
        summary_text = None
        if moved and summary is not None and summary.get("status") == "summarized":
            summary_text = summary["summary"]
        new_id = self._accept(
            db,
            author,
            accepted,
            failed,
            participants,
            body,
            source["subject"],
            conversation_id=conversation_id,
            reply_to=source["message_id"],
            summary=summary_text,
            priority=OWNER_PRIORITY if actor_id == OWNER else ORDINARY_PRIORITY,
            attachments=attached,
        )
        result = self._send_result(db, new_id)
        if actor_id == OWNER and db.execute(
            "SELECT 1 FROM message_deliveries WHERE message_id=? AND employee_id=? "
            "AND disposition='pending'",
            (message_id, OWNER),
        ).fetchone():
            self._write_disposition(db, OWNER, [message_id], "dismissed")
        if created:
            result["continues"] = source["conversation_id"]
        if summary is not None:
            if summary_text is not None:
                result["summary"] = {"status": "attached"}
            else:
                reason = summary.get("reason", "unavailable") if moved else "nobody_new"
                result["summary"] = {"status": "unavailable", "reason": reason}
        return result

    def _conversation_for(
        self,
        db: sqlite3.Connection,
        audience: list[dict[str, str]],
        *,
        continues: str | None = None,
    ) -> tuple[str, bool]:
        """The conversation of exactly these people, created on first use."""
        key = _audience_key(audience)
        row = db.execute(
            "SELECT conversation_id FROM conversations WHERE audience_key=?", (key,)
        ).fetchone()
        if row is not None:
            return row["conversation_id"], False
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversations"
        ).fetchone()[0]
        conversation_id = "c" + _base36(sequence)
        db.execute(
            "INSERT INTO conversations(conversation_id,sequence,audience_key,audience_json,"
            "continues,created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, sequence, key, _json(audience), continues, self.store.clock()),
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

    def peek(
        self,
        actor_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        activation_id: str | None = None,
    ) -> dict:
        """Waiting mail by conversation, with stable actor-scoped pagination."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SELECTION:
            raise FirmError(f"limit must be an integer from 1 to {MAX_SELECTION}")
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            if activation_id is not None:
                self._activation(db, actor_id, activation_id)
            current_sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM message_sequence"
            ).fetchone()[0]
            if cursor is None:
                watermark, after = current_sequence, None
            else:
                watermark, after = self._decode_inbox_cursor(
                    db, actor_id, activation_id, cursor
                )
                if watermark > current_sequence:
                    raise FirmError("invalid inbox cursor")
            rows = db.execute(
                "SELECT m.*,c.audience_json FROM message_deliveries d "
                "JOIN messages m USING(message_id) "
                "JOIN conversations c USING(conversation_id) "
                "WHERE d.employee_id=? AND d.disposition='pending' AND m.sequence<=? "
                "ORDER BY m.sequence",
                (actor_id, watermark),
            ).fetchall()
            result = self._overview(
                db,
                rows,
                actor_id,
                watermark=watermark,
                after=after,
                activation_id=activation_id,
                limit=limit,
                context=context_overview(
                    db, actor_id, self.store.clock(), byte_limit=min(4096, MAX_PEEK_BYTES // 3)
                ),
            )
            return result

    def _overview(
        self,
        db: sqlite3.Connection,
        rows: list[sqlite3.Row],
        actor_id: str,
        *,
        watermark: int,
        after: tuple[int, int, str] | None,
        activation_id: str | None,
        limit: int,
        context: dict | None = None,
    ) -> dict:
        now = self.store.clock()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["conversation_id"], []).append(row)

        def rank(messages: list[sqlite3.Row]) -> tuple[int, int, str]:
            # Operations first, then the owner, then whoever has waited longest.
            members = {person["employee_id"] for person in json.loads(messages[0]["audience_json"])}
            prominence = 0 if OPERATIONS in members else 1 if OWNER in members else 2
            # The oldest pending message is the age key. Claims can only remove
            # pending rows, so a conversation's key can move forward, never backward.
            return (prominence, messages[0]["sequence"], messages[0]["conversation_id"])

        ordered = sorted(grouped.items(), key=lambda item: rank(item[1]))
        if after is not None:
            ordered = [item for item in ordered if rank(item[1]) > after]
        items = [
            (self._conversation_summary(conversation_id, messages, actor_id, now), rank(messages))
            for conversation_id, messages in ordered
        ]
        page = items[:limit]
        had_page = bool(page)
        while True:
            shown = len(page)
            more = len(items) > shown
            cursor = (
                self._encode_inbox_cursor(
                    db, actor_id, activation_id, watermark, page[-1][1]
                )
                if more and page
                else None
            )
            result = {
                "pending_count": len(rows),
                "conversations": [item[0] for item in page],
                "next_cursor": cursor,
                "omitted_rows": max(0, len(items) - shown),
                # Only a completed traversal grants clear coverage.
                "seen_sequence": watermark if cursor is None else None,
            }
            if context is not None:
                result["context"] = context
            if len(encoded(result).encode("utf-8")) <= MAX_PEEK_BYTES:
                if had_page and not page:
                    raise FirmError(render("references", "row_budget"))
                return result
            if not page:
                raise FirmError("peek metadata exceeds the rendered result limit")
            page.pop()

    def observation_sequence(
        self, db: sqlite3.Connection, actor_id: str, activation_id: str, result: dict
    ) -> int | None:
        """Sequence covered by a peek result, including an incomplete traversal page."""
        seen = result.get("seen_sequence")
        if seen is not None:
            return seen
        cursor = result.get("next_cursor")
        if cursor is None:
            return None
        watermark, _ = self._decode_inbox_cursor(db, actor_id, activation_id, cursor)
        return watermark

    def _inbox_cursor_key(self, db: sqlite3.Connection) -> bytes:
        row = db.execute(
            "SELECT value FROM runtime_secrets WHERE name='discovery_cursor'"
        ).fetchone()
        if row is None:
            raise FirmError("inbox cursor signing key is unavailable")
        try:
            return bytes.fromhex(row["value"])
        except ValueError as error:
            raise FirmError("inbox cursor signing key is invalid") from error

    def _encode_inbox_cursor(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        activation_id: str | None,
        watermark: int,
        rank: tuple[int, int, str],
    ) -> str:
        payload = encoded(
            {"a": actor_id, "i": activation_id, "w": watermark, "r": list(rank)}
        ).encode("utf-8")
        signature = hmac.new(self._inbox_cursor_key(db), payload, hashlib.sha256).digest()
        token = f"{self._b64(payload)}.{self._b64(signature)}"
        if len(token) > MAX_INBOX_CURSOR_CHARACTERS:
            raise FirmError("inbox cursor exceeds its size limit")
        return token

    def _decode_inbox_cursor(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        activation_id: str | None,
        cursor: str,
    ) -> tuple[int, tuple[int, int, str]]:
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > MAX_INBOX_CURSOR_CHARACTERS
        ):
            raise FirmError("invalid inbox cursor")
        try:
            payload_part, signature_part = cursor.split(".", 1)
            payload = self._unb64(payload_part)
            signature = self._unb64(signature_part)
            if self._b64(payload) != payload_part or self._b64(signature) != signature_part:
                raise ValueError("noncanonical base64")
            expected = hmac.new(self._inbox_cursor_key(db), payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("bad signature")
            value = json.loads(payload)
            watermark = value["w"]
            rank = value["r"]
            if (
                set(value) != {"a", "i", "w", "r"}
                or value["a"] != actor_id
                or value["i"] != activation_id
                or not isinstance(watermark, int)
                or isinstance(watermark, bool)
                or watermark < 0
                or not isinstance(rank, list)
                or len(rank) != 3
                or not isinstance(rank[0], int)
                or isinstance(rank[0], bool)
                or rank[0] not in (0, 1, 2)
                or not isinstance(rank[1], int)
                or isinstance(rank[1], bool)
                or rank[1] < 1
                or rank[1] > watermark
                or not isinstance(rank[2], str)
                or not rank[2]
            ):
                raise ValueError("invalid fields")
            return watermark, (rank[0], rank[1], rank[2])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise FirmError("invalid inbox cursor") from error

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(value: str) -> bytes:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if not value or any(character not in alphabet for character in value):
            raise ValueError("invalid base64")
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def _conversation_summary(
        self, conversation_id: str, messages: list[sqlite3.Row], actor_id: str, now: float
    ) -> dict:
        audience = json.loads(messages[0]["audience_json"])
        # The reader knows they are in it; show who else is.
        others = [person for person in audience if person["employee_id"] != actor_id]
        listed = messages[:MAX_LISTED_PER_CONVERSATION]
        return {
            "conversation_id": conversation_id,
            "participants": self._participant_preview(others),
            "participant_count": len(audience),
            "participants_omitted": max(0, len(others) - 8),
            "pending_count": len(messages),
            "oldest_age_seconds": max(0.0, now - messages[0]["created_at"]),
            "body_bytes": sum(len(message["body"].encode("utf-8")) for message in messages),
            "waiting": [self._header(row, now) for row in listed],
            "waiting_omitted": len(messages) - len(listed),
        }

    @staticmethod
    def _header(row: sqlite3.Row, now: float) -> dict:
        return {
            "message_id": row["message_id"],
            "sender": {
                "employee_id": Messaging._excerpt(row["author_id"]),
                "address": Messaging._excerpt(row["author_address"]),
                "identity": Messaging._excerpt(row["author_identity"]),
            },
            "subject": row["subject"],
            "age_seconds": max(0.0, now - row["created_at"]),
            "body_bytes": len(row["body"].encode("utf-8")),
        }

    # ----------------------------------------------------------- dispositions

    def claim(
        self,
        actor_id: str,
        message_ids: list[str] | None = None,
        *,
        conversation: str | None = None,
        request_id: str,
        activation_id: str | None = None,
    ) -> dict:
        return self._dispose(
            actor_id,
            "claimed",
            message_ids=message_ids,
            conversation=conversation,
            request_id=request_id,
            activation_id=activation_id,
        )

    def dismiss(
        self,
        actor_id: str,
        message_ids: list[str] | None = None,
        *,
        conversation: str | None = None,
        request_id: str,
        activation_id: str | None = None,
    ) -> dict:
        return self._dispose(
            actor_id,
            "dismissed",
            message_ids=message_ids,
            conversation=conversation,
            request_id=request_id,
            activation_id=activation_id,
        )

    def clear(self, actor_id: str, *, request_id: str, activation_id: str) -> dict:
        """Dismiss everything that was waiting when this work period last peeked."""
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            activation = self._activation(db, actor_id, activation_id)
            seen = activation["inbox_seen_sequence"]
            if seen is None:
                return {"status": "nothing_cleared", "reason": "no_peek"}
            return self.store.request(
                db,
                actor_id,
                request_id,
                "clear",
                {"through": seen},
                lambda: self._clear(db, actor_id, seen),
            )

    def _clear(self, db: sqlite3.Connection, actor_id: str, seen: int) -> dict:
        rows = db.execute(
            "SELECT m.message_id,m.conversation_id FROM message_deliveries d "
            "JOIN messages m USING(message_id) WHERE d.employee_id=? AND d.disposition='pending' "
            "AND m.sequence<=? ORDER BY m.sequence",
            (actor_id, seen),
        ).fetchall()
        if not rows:
            return {"status": "nothing_cleared", "reason": "nothing_waiting"}
        by_conversation: dict[str, list[str]] = {}
        for row in rows:
            by_conversation.setdefault(row["conversation_id"], []).append(row["message_id"])
        self._write_disposition(db, actor_id, [row["message_id"] for row in rows], "dismissed")
        return {
            "status": "cleared",
            "count": len(rows),
            "conversations": [
                {"conversation_id": conversation_id, "message_ids": ids}
                for conversation_id, ids in by_conversation.items()
            ],
        }

    def _dispose(
        self,
        actor_id: str,
        disposition: str,
        *,
        message_ids: list[str] | None,
        conversation: str | None,
        request_id: str,
        activation_id: str | None,
    ) -> dict:
        if (message_ids is None) == (conversation is None):
            raise FirmError("select either message_ids or a conversation, not both")
        operation = "claim" if disposition == "claimed" else "dismiss"
        if message_ids is not None:
            ids = self._validate_ids(message_ids)
            payload: dict[str, Any] = {"message_ids": ids}
        else:
            if not isinstance(conversation, str) or not conversation:
                raise FirmError("conversation must be a nonempty string")
            payload = {"conversation": conversation}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            # A work period may set aside only what its last complete peek could show;
            # the owner and Operations have no work periods and no such bound.
            seen: int | None = None
            if activation_id is not None:
                seen = self._activation(db, actor_id, activation_id)["inbox_seen_sequence"]
                if seen is None:
                    seen = -1
            if message_ids is not None:
                action = lambda: self._apply_disposition(  # noqa: E731
                    db, actor_id, ids, disposition, activation_id=activation_id
                )
            else:
                self._conversation_row(db, conversation)
                action = lambda: self._dispose_conversation(  # noqa: E731
                    db, actor_id, conversation, disposition, seen, activation_id=activation_id
                )
            return self.store.request(db, actor_id, request_id, operation, payload, action)

    def _dispose_conversation(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        conversation_id: str,
        disposition: str,
        seen: int | None,
        *,
        activation_id: str | None,
    ) -> dict:
        """Claim reads whatever waits, oldest first, as much as one result can carry.

        Dismiss sets aside only what the last peek could have shown, like clear, so mail
        that arrived since is never set aside unseen. `seen` is None for a caller with no
        work period, and -1 for one that has not peeked.
        """
        query = (
            "SELECT m.message_id,m.sequence FROM message_deliveries d "
            "JOIN messages m USING(message_id) WHERE d.employee_id=? AND d.disposition='pending' "
            "AND m.conversation_id=?"
        )
        params: list[Any] = [actor_id, conversation_id]
        if disposition == "dismissed" and seen is not None:
            if seen < 0:
                return {"status": "nothing_dismissed", "reason": "no_peek"}
            query += " AND m.sequence<=?"
            params.append(seen)
        rows = db.execute(query + " ORDER BY m.sequence", params).fetchall()
        if not rows:
            return {
                "status": "nothing_claimed" if disposition == "claimed" else "nothing_dismissed",
                "reason": "nothing_waiting",
            }
        ids = [row["message_id"] for row in rows]
        if disposition == "dismissed":
            result = self._apply_disposition(
                db, actor_id, ids, disposition, activation_id=activation_id
            )
            result["conversation_id"] = conversation_id
            return result
        # Take the oldest messages that fit, measured once each; the caller is told how
        # many remain. Every accepted envelope fits alone, so at least one is taken.
        envelopes = self._envelopes(db, ids[:MAX_SELECTION])
        envelope_bytes = 0
        selected: list[str] = []
        for envelope in envelopes:
            size = len(encoded(envelope).encode("utf-8"))
            candidate_count = len(selected) + 1
            metadata = len(
                encoded(
                    {
                        "status": "claimed",
                        "messages": [],
                        "conversation_id": conversation_id,
                        "remaining": len(ids) - candidate_count,
                    }
                ).encode("utf-8")
            )
            candidate_size = metadata + envelope_bytes + size + len(selected)
            if candidate_size > MAX_RESULT_BYTES:
                break
            envelope_bytes += size
            selected.append(envelope["message_id"])
        if not selected:
            raise FirmError(
                render("references", "message_budget", label="claim result", limit=MAX_RESULT_BYTES)
            )
        result = self._apply_disposition(
            db, actor_id, selected, disposition, activation_id=activation_id
        )
        result["conversation_id"] = conversation_id
        result["remaining"] = len(ids) - len(selected)
        return result

    def _apply_disposition(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        message_ids: list[str],
        disposition: str,
        *,
        activation_id: str | None,
    ) -> dict:
        placeholders = ",".join("?" for _ in message_ids)
        rows = db.execute(
            f"SELECT message_id FROM message_deliveries WHERE employee_id=? "
            f"AND disposition='pending' AND message_id IN ({placeholders})",
            (actor_id, *message_ids),
        ).fetchall()
        if {row["message_id"] for row in rows} != set(message_ids):
            raise FirmError("every selected message must be pending for this recipient")
        if disposition == "claimed":
            envelopes = self._envelopes(db, message_ids)
            result = {"status": "claimed", "messages": envelopes}
            self._check_budget(result, MAX_RESULT_BYTES, "claim result")
        else:
            result = {"status": "dismissed", "message_ids": message_ids}
        self._write_disposition(
            db, actor_id, message_ids, disposition, activation_id=activation_id
        )
        return result

    def _write_disposition(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        message_ids: list[str],
        disposition: str,
        *,
        activation_id: str | None = None,
    ) -> None:
        claim_sequence = None
        if disposition == "claimed":
            claim_sequence = db.execute(
                "INSERT INTO message_claim_sequence DEFAULT VALUES"
            ).lastrowid
        placeholders = ",".join("?" for _ in message_ids)
        db.execute(
            f"UPDATE message_deliveries SET disposition=?,disposition_at=?,claim_sequence=?,"
            f"claimed_activation_id=? WHERE employee_id=? AND message_id IN ({placeholders})",
            (
                disposition,
                self.store.clock(),
                claim_sequence,
                activation_id if disposition == "claimed" else None,
                actor_id,
                *message_ids,
            ),
        )

    @staticmethod
    def _activation(db: sqlite3.Connection, actor_id: str, activation_id: str) -> sqlite3.Row:
        activation = db.execute(
            "SELECT employee_id,state,inbox_seen_sequence FROM activations WHERE activation_id=?",
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

    def messages(self, actor_id: str, message_ids: list[str]) -> dict:
        ids = self._validate_ids(message_ids)
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            result = {"messages": self._envelopes(db, ids)}
            self._check_budget(result, MAX_RESULT_BYTES, "message lookup result")
            return result

    def context(
        self, actor_id: str, conversation_id: str, *, session_id: str | None = None
    ) -> dict:
        """Summarize a conversation, reaching back into the ones it continues.

        Authorization and source selection happen in a short read transaction.
        The provider is called after that connection closes, then the actor is
        checked again before the derived result is returned.  This operation
        never changes delivery dispositions or creates wake requests.
        """
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            self._conversation_row(db, conversation_id)
            snapshot = self._context_snapshot(db, conversation_id)
        result = self.summary_service.summarize(snapshot, caller_session_id=session_id)
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
        return result

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

    def recent_claims(
        self,
        db: sqlite3.Connection,
        employee_id: str,
        session_id: str,
        limit: int = 5,
    ) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise FirmError("limit must be a nonnegative integer")
        rows = db.execute(
            "SELECT m.message_id,m.subject,d.disposition_at AS claimed_at "
            "FROM message_deliveries d "
            "JOIN messages m ON m.message_id=d.message_id "
            "JOIN activations a ON a.activation_id=d.claimed_activation_id "
            "WHERE d.employee_id=? AND d.disposition='claimed' AND a.session_id=? "
            "ORDER BY d.claim_sequence DESC,m.sequence DESC LIMIT ?",
            (employee_id, session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

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
        subject: str,
        *,
        conversation_id: str,
        reply_to: str | None = None,
        summary: str | None = None,
        priority: int,
        wake: bool = True,
        ready_at: float | None = None,
        regarding: dict | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> str:
        attached = [] if attachments is None else attachments
        sequence = db.execute("INSERT INTO message_sequence DEFAULT VALUES").lastrowid
        message_id = "m" + _base36(sequence)
        recipient_records = [{**person, "status": "queued"} for person in accepted] + [
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
            {"status": "claimed", "messages": [candidate]},
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
            scheduling_ready_at = (
                (candidate["created_at"] if ready_at is None else ready_at)
                if wake and employee_id not in {OWNER, OPERATIONS}
                else None
            )
            db.execute(
                "INSERT INTO message_deliveries("
                "message_id,employee_id,scheduling_priority,scheduling_ready_at) VALUES(?,?,?,?)",
                (message_id, employee_id, priority, scheduling_ready_at),
            )
            if employee_id == OWNER:
                self.notify_owner(
                    db,
                    render("notices", "owner_mail_title", address=author["address"]),
                    subject,
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
    def _subject(body: str, subject: str | None) -> str:
        if subject is not None:
            return subject
        return body if len(body) <= 80 else body[:80] + "…"

    @staticmethod
    def _validate_recipients(recipients: list[str]) -> list[str]:
        if not isinstance(recipients, list) or not recipients:
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
