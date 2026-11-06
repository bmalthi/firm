"""Durable self-reminders released as ordinary Operations mail when execution is open."""

import math

from .errors import FirmError
from .prompt_resources import render
from .summaries import utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id TEXT NOT NULL UNIQUE,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    due_at REAL NOT NULL,
    note TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','released','cancelled')),
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS due_reminders ON reminders(state,due_at);
"""


class Reminders:
    def __init__(self, store, messaging):
        self.store = store
        self.messaging = messaging

    def set(self, actor_id: str, due_at: float, note: str, request_id: str) -> dict:
        if type(due_at) not in (float, int) or not math.isfinite(due_at):
            raise FirmError("due_at must be a finite UTC epoch timestamp")
        if not isinstance(note, str) or not note.strip() or len(note) > 4096:
            raise FirmError("reminder note must contain 1–4096 characters")
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)

            def create():
                now = self.store.clock()
                if due_at <= now:
                    raise FirmError("due_at must be in the future")
                pending = db.execute(
                    "SELECT COUNT(*) FROM reminders WHERE employee_id=? AND state='pending'",
                    (actor_id,),
                ).fetchone()[0]
                if pending >= 100:
                    raise FirmError("at most 100 pending reminders per employee")
                number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM reminders").fetchone()[0]
                reminder_id = f"r{number}"
                db.execute(
                    "INSERT INTO reminders(number,reminder_id,employee_id,due_at,note,state,"
                    "created_at) "
                    "VALUES(?,?,?,?,?,'pending',?)",
                    (number, reminder_id, actor_id, due_at, note, now),
                )
                return {"reminder_id": reminder_id, "due_at": due_at, "state": "pending"}

            return self.store.request(
                db, actor_id, request_id, "set_reminder", {"due_at": due_at, "note": note}, create
            )

    def list(self, actor_id: str) -> dict:
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            return {
                "reminders": [
                    {
                        "reminder_id": row["reminder_id"],
                        "due_at": row["due_at"],
                        "note_preview": row["note"][:80],
                        "note_truncated": len(row["note"]) > 80,
                    }
                    for row in db.execute(
                        "SELECT reminder_id,due_at,note,state FROM reminders "
                        "WHERE employee_id=? AND state='pending' ORDER BY due_at,reminder_id",
                        (actor_id,),
                    )
                ]
            }

    def cancel(self, actor_id: str, reminder_id: str, request_id: str) -> dict:
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)

            def cancel():
                row = db.execute(
                    "SELECT * FROM reminders WHERE reminder_id=? AND employee_id=?",
                    (reminder_id, actor_id),
                ).fetchone()
                if row is None:
                    raise FirmError("unknown reminder for this employee")
                if row["state"] == "pending":
                    db.execute(
                        "UPDATE reminders SET state='cancelled',resolved_at=? WHERE reminder_id=?",
                        (self.store.clock(), reminder_id),
                    )
                return {
                    "reminder_id": reminder_id,
                    "state": "cancelled" if row["state"] == "pending" else row["state"],
                }

            return self.store.request(
                db, actor_id, request_id, "cancel_reminder", {"reminder_id": reminder_id}, cancel
            )

    def release_due(self, db) -> None:
        """Caller owns admission transaction; retries cannot duplicate released mail."""
        now = self.store.clock()
        db.execute(
            "UPDATE reminders SET state='cancelled',resolved_at=? WHERE state='pending' "
            "AND employee_id IN (SELECT employee_id FROM roles WHERE employment='retired')",
            (now,),
        )
        due = db.execute(
            "SELECT m.* FROM reminders m JOIN roles r USING(employee_id) "
            "WHERE m.state='pending' AND m.due_at<=? AND r.employment='active' "
            "ORDER BY m.due_at,m.reminder_id LIMIT 100",
            (now,),
        ).fetchall()
        for row in due:
            self.messaging.notice(
                db,
                [row["employee_id"]],
                render(
                    "notices",
                    "reminder",
                    reminder_id=row["reminder_id"],
                    due_at=utc_iso(row["due_at"]),
                    note=row["note"],
                ),
                subject=render("notices", "reminder_subject"),
                wake=True,
                ready_at=row["due_at"],
            )
            db.execute(
                "UPDATE reminders SET state='released',resolved_at=? WHERE reminder_id=?",
                (now, row["reminder_id"]),
            )
