"""One SQLite database per firm, with short transactions for state transitions."""

import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .errors import FirmError

OWNER = "owner"
OPERATIONS = "runtime"
ORDINARY_PRIORITY = 0
OWNER_PRIORITY = 1
CONTROL_PRIORITY = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    firm_id TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    initial_mandate TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'stopped'
        CHECK(state IN ('stopped','running','draining','stopping')),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    employee_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    identity TEXT NOT NULL,
    public_profile TEXT NOT NULL DEFAULT '',
    public_profile_status TEXT NOT NULL DEFAULT 'missing',
    public_profile_digest TEXT,
    employment TEXT NOT NULL DEFAULT 'active'
        CHECK(employment IN ('active','paused','retired')),
    execution_profile TEXT NOT NULL,
    effort TEXT,
    session_id TEXT,
    session_mandate_revision INTEGER NOT NULL DEFAULT 0,
    birth_delivered INTEGER NOT NULL DEFAULT 0,
    handoff_notes TEXT,
    recovery_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    recovery_episode TEXT NOT NULL,
    hold_reason TEXT,
    retry_at REAL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS current_address ON roles(address)
    WHERE employment != 'retired';
CREATE TABLE IF NOT EXISTS mandates (
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    revision INTEGER NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY(employee_id, revision)
);
CREATE TABLE IF NOT EXISTS activations (
    activation_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    session_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('admitted','running','completed','failed','interrupted')),
    token_hash TEXT UNIQUE,
    wake_snapshot TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    launch_attempted_at REAL,
    started_at REAL,
    finished_at REAL,
    handoff_notes TEXT,
    next_intent TEXT CHECK(next_intent IN ('continue','wait')),
    drain_requested_at REAL,
    drain_presented_at REAL,
    handoff_presented_compactions INTEGER,
    mail_presented_sequence INTEGER,
    inbox_seen_sequence INTEGER,
    failure_category TEXT,
    failure TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution ON activations(employee_id)
    WHERE state IN ('admitted','running');
CREATE TABLE IF NOT EXISTS discovery_continuations (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    activation_id TEXT NOT NULL REFERENCES activations(activation_id),
    cursor TEXT NOT NULL,
    UNIQUE(activation_id,cursor)
);
CREATE TABLE IF NOT EXISTS execution_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    reason TEXT NOT NULL CHECK(reason != 'message'),
    priority INTEGER NOT NULL,
    ready_at REAL NOT NULL,
    created_at REAL NOT NULL,
    source_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS execution_request_by_employee
    ON execution_requests(employee_id, ready_at);
CREATE TABLE IF NOT EXISTS requests (
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    activation_id TEXT REFERENCES activations(activation_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(actor_id, request_id)
);
CREATE INDEX IF NOT EXISTS requests_by_activation ON requests(activation_id);
CREATE TABLE IF NOT EXISTS sandbox_runs (
    activation_id TEXT PRIMARY KEY REFERENCES activations(activation_id),
    role_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    vm_generation TEXT,
    provider TEXT NOT NULL,
    deadline REAL NOT NULL,
    stop_cause TEXT,
    memory_peak_bytes INTEGER,
    cpu_usec INTEGER,
    oom_kills INTEGER
);
CREATE TABLE IF NOT EXISTS execution_activity (
    activation_id TEXT PRIMARY KEY REFERENCES sandbox_runs(activation_id),
    idle_timeout REAL NOT NULL CHECK(idle_timeout > 0),
    output_digest TEXT
);
CREATE TABLE IF NOT EXISTS sandbox_run_settlements (
    run_id TEXT PRIMARY KEY,
    role_id TEXT NOT NULL,
    vm_generation TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    usage_json TEXT,
    reclaim_json TEXT
);
CREATE TABLE IF NOT EXISTS provider_sessions (
    session_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    native_session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_host (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    sandbox_root TEXT NOT NULL,
    sandbox_firm_id TEXT NOT NULL,
    sandbox_identity TEXT NOT NULL,
    infrastructure_state TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS runtime_secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employee_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    next_number INTEGER NOT NULL CHECK(next_number>0)
);
INSERT OR IGNORE INTO employee_sequence VALUES(1,1);
"""


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def allocate_employee_id(self, db: sqlite3.Connection) -> str:
        """Reserve outside rollback; the caller's IMMEDIATE transaction serializes writers.

        Published filesystem ownership can outlive a failed hire transaction. Keep
        the high-water mark beside the database so that identity is never reused.
        Whole-firm backup/restore must include this file; failed hires may leave gaps.
        """
        active = self._connection.get()
        if active is None or active[0] is not db or not active[1] or not db.in_transaction:
            raise RuntimeError("employee allocation requires the registration transaction")
        sequence = self.path.with_name(self.path.name + ".employee-sequence")
        temporary = sequence.with_name("." + sequence.name + "." + secrets.token_hex(8))
        try:
            try:
                descriptor = os.open(sequence, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                high_water = 0
            else:
                with os.fdopen(descriptor) as stream:
                    raw = stream.read(32)
                saved = raw.strip()
                if len(raw) == 32 or not saved.isascii() or not saved.isdecimal():
                    raise ValueError("invalid employee sequence")
                high_water = int(saved)
            floor = db.execute(
                "SELECT next_number FROM employee_sequence WHERE singleton=1"
            ).fetchone()[0]
            number = max(floor, high_water + 1)
            if number >= 2**63 - 1:
                raise ValueError("employee sequence is exhausted")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(f"{number}\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(sequence)
            directory = os.open(sequence.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError) as exc:
            raise FirmError("Operations could not reserve an employee identity") from exc
        finally:
            temporary.unlink(missing_ok=True)
        db.execute("UPDATE employee_sequence SET next_number=? WHERE singleton=1", (number + 1,))
        return f"e{number}"

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ):
        absolute = Path(path).expanduser().absolute()
        # Firm databases live in durable/. Standalone runtime stores use their
        # containing directory as the firm root. This read-only guard must run
        # before mkdir, SQLite, WAL, schema, or secret writes below.
        firm_directory = (
            absolute.parent.parent if absolute.parent.name == "durable" else absolute.parent
        )
        from .compatibility import validate_firm

        validate_firm(firm_directory)
        self._require_regular_database_path(absolute)
        self._initialize(absolute, clock)

    @staticmethod
    def _require_regular_database_path(path: Path) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError("Firm database path cannot be inspected safely") from exc
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("Firm database path must be a regular file")

    def _initialize(self, absolute: Path, clock: Callable[[], float]) -> None:
        self.path = absolute.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._connection: ContextVar[tuple[sqlite3.Connection, bool] | None] = ContextVar(
            f"firm_connection_{id(self)}", default=None
        )
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            from .messaging import SCHEMA as MESSAGE_SCHEMA

            db.executescript(MESSAGE_SCHEMA)
            from .scheduling import SCHEMA as SCHEDULING_SCHEMA

            db.executescript(SCHEDULING_SCHEMA)
            from .accounts import SCHEMA as ACCOUNT_SCHEMA

            db.executescript(ACCOUNT_SCHEMA)
            from .telemetry import SCHEMA as TELEMETRY_SCHEMA

            db.executescript(TELEMETRY_SCHEMA)
            from .reminders import SCHEMA as REMINDER_SCHEMA

            db.executescript(REMINDER_SCHEMA)
            from .appointments import SCHEMA as APPOINTMENT_SCHEMA

            db.executescript(APPOINTMENT_SCHEMA)
            from .analysis_history import SCHEMA as ANALYSIS_SCHEMA

            db.executescript(ANALYSIS_SCHEMA)
            from .summaries import SCHEMA as SUMMARY_SCHEMA

            db.executescript(SUMMARY_SCHEMA)
            from .levels import SCHEMA as LEVELS_SCHEMA

            db.executescript(LEVELS_SCHEMA)
            from .mac_lease import SCHEMA as MAC_SCHEMA

            db.executescript(MAC_SCHEMA)
            from .github import SCHEMA as GITHUB_SCHEMA

            db.executescript(GITHUB_SCHEMA)
            from .requests import SCHEMA as REQUESTS_SCHEMA

            db.executescript(REQUESTS_SCHEMA)
            from .hosts import SCHEMA as HOSTS_SCHEMA

            db.executescript(HOSTS_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO runtime_secrets VALUES('discovery_cursor',?)",
                (secrets.token_hex(32),),
            )

    @classmethod
    def scratch(
        cls, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> "Store":
        """Open disposable standalone state without a firm release marker.

        Use this for isolated tests and developer examples. Production firm
        databases must use the checked constructor after explicit initialization.
        """
        absolute = Path(path).expanduser().absolute()
        firm_directory = (
            absolute.parent.parent if absolute.parent.name == "durable" else absolute.parent
        )
        from firm_sandbox.compatibility import FIRM_RECORD

        if os.path.lexists(firm_directory / FIRM_RECORD):
            raise ValueError("Store.scratch cannot open a marked firm database")
        cls._require_regular_database_path(absolute)
        store = cls.__new__(cls)
        store._initialize(absolute, clock)
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Share one snapshot across nested observations; reuse an owned write if present."""
        current = self._connection.get()
        if current is not None:
            yield current[0]
            return
        with self._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            token = self._connection.set((db, False))
            try:
                yield db
            finally:
                self._connection.reset(token)
                db.rollback()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        current = self._connection.get()
        if current is not None:
            if not current[1]:
                raise RuntimeError("cannot upgrade a read snapshot to a write transaction")
            # Nested service operations share the caller's authorization and commit.
            yield current[0]
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            token = self._connection.set((db, True))
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
            finally:
                self._connection.reset(token)

    def firm(self, db: sqlite3.Connection) -> dict:
        row = db.execute("SELECT * FROM firm WHERE singleton=1").fetchone()
        if row is None:
            raise FirmError("firm is not initialized")
        return dict(row)

    def role(self, db: sqlite3.Connection, employee_id: str) -> dict:
        row = db.execute("SELECT * FROM roles WHERE employee_id=?", (employee_id,)).fetchone()
        if row is None:
            raise FirmError("unknown employee identity")
        return dict(row)

    def resolve(self, db: sqlite3.Connection, reference: str) -> dict | None:
        if reference in {OWNER, OPERATIONS}:
            return {
                "employee_id": reference,
                "address": reference,
                "identity": "Owner" if reference == OWNER else "Operations",
                "employment": "active",
            }
        row = db.execute("SELECT * FROM roles WHERE employee_id=?", (reference,)).fetchone()
        if row is None:
            row = db.execute(
                "SELECT * FROM roles WHERE address=? AND employment!='retired'", (reference,)
            ).fetchone()
        return dict(row) if row is not None else None

    def require_actor(self, db: sqlite3.Connection, actor_id: str) -> dict:
        self.firm(db)
        role = self.resolve(db, actor_id)
        if role is None or role["employee_id"] != actor_id or role["employment"] != "active":
            raise FirmError("actor is not an active employee")
        return role

    def request_execution(
        self,
        db: sqlite3.Connection,
        employee_id: str,
        reason: str,
        priority: int = ORDINARY_PRIORITY,
        *,
        ready_at: float | None = None,
        source_id: str = "",
    ) -> None:
        if employee_id in {OWNER, OPERATIONS}:
            return
        now = self.clock()
        db.execute(
            "INSERT INTO execution_requests(employee_id,reason,priority,ready_at,created_at,"
            "source_id) VALUES(?,?,?,?,?,?)",
            (employee_id, reason, priority, now if ready_at is None else ready_at, now, source_id),
        )

    def replay(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        request_id: str,
        operation: str,
        payload: Any,
    ) -> dict | None:
        """The stored result of an identical earlier request, or None for a new one."""
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise FirmError("request_id must contain 1–200 characters")
        prior = db.execute(
            "SELECT * FROM requests WHERE actor_id=? AND request_id=?", (actor_id, request_id)
        ).fetchone()
        if not prior:
            return None
        if prior["operation"] != operation or prior["payload_json"] != encoded(payload):
            raise FirmError("request_id already used with a different operation or payload")
        return json.loads(prior["result_json"])

    def operation_result(
        self, db: sqlite3.Connection, actor_id: str, request_id: str
    ) -> dict | None:
        """Return a durable mutation receipt without replaying its side effect."""
        row = db.execute(
            "SELECT operation,result_json FROM requests WHERE actor_id=? AND request_id=?",
            (actor_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return {"operation": row["operation"], "result": json.loads(row["result_json"])}

    def request(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        request_id: str,
        operation: str,
        payload: Any,
        action: Callable[[], dict],
        *,
        activation_id: str | None = None,
    ) -> dict:
        prior = self.replay(db, actor_id, request_id, operation, payload)
        if prior is not None:
            return prior
        body = encoded(payload)
        result = action()
        if activation_id is None:
            # The one_active_execution index allows an employee one open work period, so
            # its own mutating operations attribute to that period; the owner's and
            # operations' have none.
            row = db.execute(
                "SELECT activation_id FROM activations WHERE employee_id=? "
                "AND state IN ('admitted','running')",
                (actor_id,),
            ).fetchone()
            activation_id = row["activation_id"] if row else None
        db.execute(
            "INSERT INTO requests VALUES(?,?,?,?,?,?,?)",
            (actor_id, request_id, operation, body, encoded(result), activation_id, self.clock()),
        )
        return result
