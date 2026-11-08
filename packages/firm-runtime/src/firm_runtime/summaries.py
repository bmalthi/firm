"""Bounded, read-only correspondence summaries.

The context operation deliberately has two phases.  It takes an authorized
SQLite snapshot, releases the database, and only then calls a provider.  This
keeps a slow provider turn from blocking writers or making a stale activation
look live for the duration of the call.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from . import analysis_history
from .errors import FirmError
from .store import encoded

MAX_CONTEXT_MESSAGES = 50
MAX_CONTEXT_BYTES = 64 * 1024
SUMMARY_TIMEOUT_SECONDS = 30.0
MAX_SUMMARY_CONCURRENCY = 4
MAX_SUMMARY_TEXT_BYTES = 16 * 1024
SUMMARY_TRUNCATION_MARKER = "[THIS SUMMARY WAS CUT OFF at 16 KiB; the rest was not delivered.]"
MAX_SUMMARY_PROMPT_BYTES = MAX_CONTEXT_BYTES + 4 * 1024
MAX_SUMMARY_LOG_BYTES = 256 * 1024
PROVIDER_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


class SummaryInputTooLarge(ValueError):
    """Input validation failed before any provider launch was attempted."""


def summary_role_id(run_id: str) -> str:
    """Summary runs have no employee; their sandbox role id derives from the run."""
    return "s-" + run_id[-32:]


def check_prompt_size(prompt: str, max_input_bytes: int) -> int:
    size = len(prompt.encode("utf-8"))
    if size > max_input_bytes:
        raise SummaryInputTooLarge(
            f"prompt is {size} UTF-8 bytes; the input limit is {max_input_bytes} bytes"
        )
    return size


SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    state TEXT NOT NULL CHECK(state IN ('running','summarized','unavailable')),
    input_bytes INTEGER NOT NULL,
    output_tokens INTEGER,
    latency_ms INTEGER,
    reason TEXT,
    account_id TEXT,
    profile TEXT,
    provider TEXT,
    deadline REAL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_token_usage INTEGER,
    reasoning_output_tokens INTEGER,
    provider_context_window INTEGER,
    native_compactions INTEGER,
    resolved_model TEXT,
    token_hash TEXT UNIQUE,
    provider_started_at REAL,
    kind TEXT NOT NULL DEFAULT 'summary',
    caller_activation_id TEXT REFERENCES activations(activation_id)
);
CREATE INDEX IF NOT EXISTS summary_runs_by_account
    ON summary_runs(account_id, state);
CREATE INDEX IF NOT EXISTS task_runs_by_caller
    ON summary_runs(caller_activation_id, state) WHERE kind='task';
"""


@dataclass(frozen=True)
class SummaryLaunch:
    """The complete input to an isolated, no-authority summary process."""

    run_id: str
    prompt: str
    timeout: float
    account_id: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    start_url: str | None = None
    start_token: str | None = None
    # The workload this turn serves and its standing instructions; a summary uses
    # the provider's summary instructions when none are supplied.
    kind: str = "summary"
    instructions: str | None = None
    max_input_bytes: int = MAX_SUMMARY_PROMPT_BYTES
    # These are contract fields consumed by a real sandbox adapter.  A summary
    # process must never receive role workspaces, MCP authority, or tools.
    tools_enabled: bool = False
    workspace_grants: tuple[str, ...] = ()
    mcp_url: str | None = None


class SummaryRunner(Protocol):
    """Sandbox runner with explicit start, wait, and confirmed cancellation."""

    def start(self, request: SummaryLaunch) -> Any: ...

    def wait(self, handle: Any, timeout: float) -> str | SummaryOutput: ...

    def stop(self, handle: Any) -> bool:
        """Stop and return True only after the process is confirmed exited."""
        ...

    def active_run_ids(self) -> set[str]: ...


@dataclass
class _SummaryHandle:
    request: SummaryLaunch
    result: Future = field(default_factory=Future)
    cancel: threading.Event = field(default_factory=threading.Event)
    run: Any = None
    launch_pending: bool = True
    exit_confirmed: bool = False


class SandboxSummaryRunner:
    """Bound the caller's wait while retaining unresolved external execution.

    Container creation and log streaming can block independently of provider work.
    Each admitted summary gets one sandbox thread. A timeout cancels it without
    pretending that its container exited. The dispatcher reconciles the same run
    IDs and retains pending launches through host shutdown.
    """

    def __init__(self, sandbox, build_spec, parse_output, *, start_allowed=None, start_url=None):
        self.sandbox = sandbox
        self.build_spec = build_spec
        self.parse_output = parse_output
        self.start_allowed = start_allowed
        self.start_url = start_url
        self._handles: dict[str, _SummaryHandle] = {}
        self._maintenance: dict[tuple[str, str], threading.Thread] = {}
        self._retired: set[str] = set()
        self._lock = threading.RLock()

    def start(self, request: SummaryLaunch):
        if request.tools_enabled or request.workspace_grants or request.mcp_url is not None:
            raise ValueError("summary runs cannot receive tools, workspaces, or MCP authority")
        check_prompt_size(request.prompt, request.max_input_bytes)
        handle = _SummaryHandle(replace(request, start_url=self.start_url))
        with self._lock:
            self._handles[request.run_id] = handle
            threading.Thread(
                target=self._execute, args=(handle,), daemon=True, name="firm-summary"
            ).start()
        return handle

    def _execute(self, handle):
        attempted = False
        try:
            request = handle.request
            if handle.cancel.is_set() or (
                self.start_allowed is not None and not self.start_allowed()
            ):
                raise SummaryUnavailable("summary admission is closed")
            spec = self.build_spec(request)
            if spec.organization is not None:
                raise ValueError("summary RunSpec cannot grant organization authority")
            if "FIRM_ACTIVATION_TOKEN" in spec.env or "FIRM_ACTIVATION_TOKEN" in spec.secret_env:
                raise ValueError("summary RunSpec cannot receive activation authority")
            if handle.cancel.is_set():
                raise SummaryTimedOut
            attempted = True
            try:
                handle.run = self.sandbox.start(spec)
            finally:
                with self._lock:
                    handle.launch_pending = False
            if handle.cancel.is_set():
                raise SummaryTimedOut
            logs = bytearray()
            stream = self.sandbox.follow(handle.run)
            try:
                for chunk in stream:
                    if handle.cancel.is_set():
                        raise SummaryTimedOut
                    if len(logs) + len(chunk) > MAX_SUMMARY_LOG_BYTES:
                        raise SummaryUnavailable("output_limit")
                    logs.extend(chunk)
            finally:
                stream.close()
            status = self.sandbox.status(handle.run)
            if status["running"] or status.get("unresolved"):
                raise SummaryUnavailable("provider_exit_unconfirmed")
            handle.exit_confirmed = True
            output = self.parse_output(request, bytes(logs), status["exit_code"])
            handle.result.set_result(output)
        except BaseException as exc:
            if not attempted:
                handle.exit_confirmed = True
            handle.result.set_exception(exc)
        finally:
            with self._lock:
                handle.launch_pending = False
            if handle.run is not None:
                if handle.exit_confirmed:
                    self._maintain("retire", handle.run)
                else:
                    self.stop(handle)

    def _maintain(self, action, run):
        """Keep cancellation/cleanup off the request and host-power-off paths."""
        key = (action, run.run_id)
        with self._lock:
            if key in self._maintenance:
                return
            # Completed journals remain in inventory for a week. Retire each only
            # once per host, and drain a restart's backlog one at a time; the next
            # reconciliation retries anything deferred or failed. Stops are independent.
            if action == "retire" and (
                run.run_id in self._retired
                or any(pending == "retire" for pending, _ in self._maintenance)
            ):
                return

            def perform():
                try:
                    if action == "stop":
                        status = self.sandbox.stop(run)
                        if not status["running"] and not status.get("unresolved"):
                            with self._lock:
                                handle = self._handles.get(run.run_id)
                                if handle:
                                    handle.exit_confirmed = True
                            if handle is None or (
                                not handle.launch_pending and handle.result.done()
                            ):
                                self._maintain("retire", run)
                    else:
                        self.sandbox.retire(run.role_id)
                        with self._lock:
                            self._retired.add(run.run_id)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                    # A later healthy inventory retries incomplete cleanup.
                    pass
                finally:
                    with self._lock:
                        self._maintenance.pop(key, None)

            thread = threading.Thread(target=perform, daemon=True, name=f"summary-{action}")
            self._maintenance[key] = thread
            thread.start()

    def wait(self, handle, timeout: float):
        try:
            return handle.result.result(timeout=timeout)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raise SummaryTimedOut from exc

    def stop(self, handle) -> bool:
        handle.cancel.set()
        if handle.run is not None and not handle.exit_confirmed:
            self._maintain("stop", handle.run)
        return handle.exit_confirmed and not handle.launch_pending

    def cancel_all(self):
        with self._lock:
            handles = tuple(self._handles.values())
        for handle in handles:
            self.stop(handle)

    def close(self):
        self.cancel_all()
        with self._lock:
            pending = [handle for handle in self._handles.values() if handle.launch_pending]
        for handle in pending:
            try:
                handle.result.result()
            except BaseException:
                pass

    def pending_launches(self) -> set[str]:
        with self._lock:
            return {key for key, value in self._handles.items() if value.launch_pending}

    def confirm_host_stop(self, *, exclude=frozenset()) -> set[str]:
        """Host-off proof applies only to launches already settled before that proof."""
        with self._lock:
            pending = self.pending_launches() | set(exclude)
            for key, handle in self._handles.items():
                handle.cancel.set()
                if key not in pending:
                    handle.exit_confirmed = True
            return pending

    def reconcile(
        self, inventory, records, *, now: float, stopping=False, protected=()
    ) -> set[str]:
        """``inventory`` carries each run's status from the same healthy fetch the
        dispatcher already reconciled against, so absence from it is already
        confirmed and no per-run status call is needed to double-check it.
        """
        active = self.pending_launches()
        for row in records:
            entry = inventory.get(row["run_id"])
            if entry is None:
                with self._lock:
                    handle = self._handles.get(row["run_id"])
                    if (
                        handle
                        and handle.result.done()
                        and not handle.launch_pending
                        and row["run_id"] not in protected
                    ):
                        handle.exit_confirmed = True
                continue
            run, status = entry
            if run.role_id != summary_role_id(row["run_id"]):
                raise FirmError("summary sandbox identity does not match its reservation")
            if status["running"] or status.get("unresolved"):
                active.add(run.run_id)
                with self._lock:
                    handle = self._handles.get(run.run_id)
                if (status.get("unresolved") or stopping or row["deadline"] <= now
                        or handle is None or handle.cancel.is_set()):
                    self._maintain("stop", run)
            else:
                with self._lock:
                    handle = self._handles.get(run.run_id)
                    if handle:
                        handle.exit_confirmed = True
                # The collector still needs logs until its result is ready.
                if handle is None or handle.result.done():
                    self._maintain("retire", run)
        with self._lock:
            for key, handle in tuple(self._handles.items()):
                if key not in active and handle.result.done() and handle.exit_confirmed:
                    del self._handles[key]
        return active


class SummaryCapacity(Protocol):
    """Durably reserve one configured provider account for a summary run."""

    def reserve_summary(
        self, db: Any, run_id: str, *, caller_session_id: str | None = None
    ) -> dict[str, Any] | None: ...


class SummaryUnavailable(Exception):
    """A provider cannot accept this attempt without retrying it here."""


class SummaryTimedOut(Exception):
    """The provider exceeded the one-turn deadline."""


@dataclass(frozen=True)
class SummaryOutput:
    text: str
    output_tokens: int | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ContextSnapshot:
    conversation_id: str
    messages: tuple[dict[str, Any], ...]
    source_message_ids: tuple[str, ...]
    omissions: tuple[dict[str, str], ...]
    input_bytes: int
    matching_count: int

    def prompt(self) -> str:
        """Render bounded source data; the provider supplies standing task instructions."""
        sources = "\n".join(encoded(message) for message in self.messages)
        return "SOURCE ENVELOPES (chronological JSON):\n" + sources + "\n"


def utc_iso(seconds: float) -> str:
    """An epoch time as ISO-8601 UTC with second precision, e.g. 2026-09-16T17:24:43Z."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _model_facing(envelope: dict[str, Any]) -> dict[str, Any]:
    """Present stored epoch seconds as dates a model reads correctly.

    Models misread raw epoch floats (1789604683.1 became the year 2789). Stored
    values and ordering stay epoch floats; only the rendered source changes, and
    it changes before any byte measurement.
    """
    created = envelope.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return {**envelope, "created_at": utc_iso(created)}
    return envelope


def _encoded_size(value: Any) -> int:
    return len(encoded(value).encode("utf-8"))


def _bounded_envelope(envelope: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    """Fit one envelope, preserving its identity and explicitly marking loss."""
    if _encoded_size(envelope) <= limit:
        return envelope, False
    if limit <= 0:
        return {"message_id": envelope["message_id"], "omitted": "source envelope"}, True

    # The body is the only unstructured, potentially large field.  Binary
    # search avoids cutting UTF-8 and leaves all attribution metadata intact.
    candidate = dict(envelope)
    body = str(candidate.pop("body", ""))
    candidate["body"] = ""
    candidate["body_omitted"] = True
    if _encoded_size(candidate) > limit:
        minimal = {"message_id": envelope["message_id"], "omitted": "source envelope"}
        return minimal, True
    low, high, best = 0, len(body), ""
    while low <= high:
        middle = (low + high) // 2
        candidate["body"] = body[:middle]
        if _encoded_size(candidate) <= limit:
            best = candidate["body"]
            low = middle + 1
        else:
            high = middle - 1
    candidate["body"] = best
    return candidate, True


def select_context(
    rows: list[dict[str, Any]],
    conversation_id: str,
    *,
    envelope,
    matching_count: int | None = None,
) -> ContextSnapshot:
    """Take rows in the caller's priority order while they fit; return them chronologically.

    The caller orders rows newest first within the conversation asked about, then the
    conversations it continues, so the budget serves the conversation itself before its
    history. The first row is the one an excerpt may be made of.
    """
    matching = list(rows)
    selected: list[dict[str, Any]] = []
    omissions: list[dict[str, str]] = []
    used = 0
    for index, row in enumerate(matching):
        if index >= MAX_CONTEXT_MESSAGES:
            omissions.append({"message_id": row["message_id"], "reason": "message_count_limit"})
            continue
        item = _model_facing(envelope(row))
        # JSON-lines rendering has one newline between envelopes.  Reserve it
        # during selection so the prompt's measured source bytes stay bounded.
        remaining = MAX_CONTEXT_BYTES - used - (1 if selected else 0)
        if remaining <= 0:
            omissions.append({"message_id": row["message_id"], "reason": "source_byte_limit"})
            omissions.extend(
                {"message_id": older["message_id"], "reason": "source_byte_limit"}
                for older in matching[index + 1 :]
            )
            break
        if index == 0:
            bounded, truncated = _bounded_envelope(item, remaining)
        else:
            # Preserve complete older envelopes. Once the newest envelope fits,
            # an older envelope that does not fit is an omission, never a silent
            # excerpt that could displace attribution fields.
            bounded, truncated = item, False
        size = _encoded_size(bounded)
        if size > remaining:
            omissions.append({"message_id": row["message_id"], "reason": "source_byte_limit"})
            omissions.extend(
                {"message_id": older["message_id"], "reason": "source_byte_limit"}
                for older in matching[index + 1 :]
            )
            break
        selected.append(bounded)
        used += size + (1 if len(selected) > 1 else 0)
        if truncated:
            omissions.append(
                {"message_id": row["message_id"], "reason": "source_envelope_truncated"}
            )
        # A bounded excerpt of the newest message is the required fallback;
        # older messages cannot displace it.
        if truncated:
            for older in matching[index + 1 :]:
                omissions.append({"message_id": older["message_id"], "reason": "source_byte_limit"})
            break
    order = {row["message_id"]: (row["sequence"], row["message_id"]) for row in matching}
    selected.sort(key=lambda item: order[item["message_id"]])
    selected_ids = tuple(item["message_id"] for item in selected)
    return ContextSnapshot(
        conversation_id=conversation_id,
        messages=tuple(selected),
        source_message_ids=selected_ids,
        omissions=tuple(omissions),
        input_bytes=used,
        matching_count=len(matching) if matching_count is None else matching_count,
    )


class SummaryService:
    """Run at most MAX_SUMMARY_CONCURRENCY independent, non-retried summary turns."""

    def __init__(
        self,
        provider: SummaryRunner | None = None,
        *,
        store=None,
        capacity: SummaryCapacity | None = None,
    ):
        self.provider = provider
        self.store = store
        self.capacity = capacity
        self._slots = threading.BoundedSemaphore(MAX_SUMMARY_CONCURRENCY)
        self._launch_lock = threading.RLock()
        self._accepting = True
        self._active: dict[str, Any] = {}
        self._inflight: set[str] = set()

    def open(self) -> None:
        with self._launch_lock:
            self._accepting = True

    def close(self, *, force: bool = False) -> bool:
        """Close admission, optionally stop active runs with confirmed exits."""
        with self._launch_lock:
            self._accepting = False
            active = tuple(self._active.values())
        if not force:
            return True
        confirmed = True
        for handle in active:
            try:
                if not self.provider.stop(handle):  # type: ignore[union-attr]
                    confirmed = False
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                confirmed = False
        return confirmed

    def _summary_reservation(self, caller_session_id: str | None):
        """The bound-account reservation for a summary; None keeps provider-free callers usable."""
        if self.capacity is None:
            return None
        if caller_session_id is None:
            # Keep provider-free callers and old injected test capacities usable.
            # Production context calls always carry the authenticated activation
            # session and therefore take the bound-account path.
            return lambda db, run_id: self.capacity.reserve_summary(db, run_id)
        return lambda db, run_id: self.capacity.reserve_summary(
            db, run_id, caller_session_id=caller_session_id
        )

    def _reserve(
        self, snapshot: ContextSnapshot, caller_session_id: str | None
    ) -> tuple[str, dict[str, Any]] | None:
        return self._reserve_run(
            snapshot.input_bytes,
            self._summary_reservation(caller_session_id),
            "summary",
            caller_session_id,
        )

    def _reserve_run(
        self, input_bytes: int, reserve, kind: str, caller_session_id: str | None
    ) -> tuple[str, dict[str, Any]] | None:
        run_id = "run-" + uuid4().hex
        start_token = secrets.token_urlsafe(32)
        if self.store is None:
            self._inflight.add(run_id)
            return run_id, {"start_token": start_token}
        now = self.store.clock()
        with self.store.transaction() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM summary_runs WHERE state='running' AND kind!='task'"
            ).fetchone()[0]
            if active >= MAX_SUMMARY_CONCURRENCY:
                return None
            reservation = {} if reserve is None else reserve(db, run_id)
            if reservation is None:
                return None
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,profile,"
                "provider,deadline,token_hash,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    now,
                    "running",
                    input_bytes,
                    reservation.get("account_id"),
                    reservation.get("profile"),
                    reservation.get("provider"),
                    now + SUMMARY_TIMEOUT_SECONDS,
                    hashlib.sha256(start_token.encode()).hexdigest(),
                    kind,
                ),
            )
            analysis_history.summary_bound(db, now, run_id, caller_session_id, reservation)
            self._inflight.add(run_id)
        return run_id, {**reservation, "start_token": start_token}

    def begin_execution(self, token: str) -> dict:
        """One-use launch permission; this token grants no role or MCP access."""
        if self.store is None or not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise FirmError("invalid summary launch token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._launch_lock, self.store.transaction() as db:
            row = db.execute("SELECT * FROM summary_runs WHERE token_hash=?", (digest,)).fetchone()
            now = self.store.clock()
            if (
                not self._accepting
                or row is None
                or row["state"] != "running"
                or row["deadline"] <= now
                or row["provider_started_at"] is not None
                or self.store.firm(db)["state"] != "running"
            ):
                raise FirmError("summary launch is no longer admitted")
            if row["kind"] == "task":
                parent = db.execute(
                    "SELECT a.state,s.stop_cause,s.deadline FROM activations a "
                    "JOIN sandbox_runs s USING(activation_id) WHERE a.activation_id=?",
                    (row["caller_activation_id"],),
                ).fetchone()
                if (not parent or parent["state"] != "running" or parent["stop_cause"]
                        or parent["deadline"] <= now):
                    raise FirmError("task parent is no longer live")
            db.execute(
                "UPDATE summary_runs SET provider_started_at=? WHERE run_id=?", (now, row["run_id"])
            )
            return {"run_id": row["run_id"], "state": "running"}

    def _finish(self, run_id: str, result: dict[str, Any], started: float) -> None:
        if self.store is None:
            return
        now = self.store.clock()
        usage = self._safe_usage(result.get("usage"))
        with self.store.transaction() as db:
            db.execute(
                "UPDATE summary_runs SET finished_at=?,state=?,output_tokens=?,"
                "latency_ms=?,reason=?,"
                "input_tokens=?,cached_input_tokens=?,output_token_usage=?,reasoning_output_tokens=?,"
                "provider_context_window=?,native_compactions=?,resolved_model=? "
                "WHERE run_id=?",
                (
                    now,
                    "summarized" if result["status"] == "completed" else result["status"],
                    result.get("output_tokens"),
                    max(0, round((now - started) * 1000)),
                    result.get("reason"),
                    usage.get("input_tokens") if usage else None,
                    usage.get("cached_input_tokens") if usage else None,
                    usage.get("output_tokens") if usage else None,
                    usage.get("reasoning_output_tokens") if usage else None,
                    usage.get("provider_context_window") if usage else None,
                    usage.get("native_compactions") if usage else None,
                    usage.get("resolved_model") if usage else None,
                    run_id,
                ),
            )

    def inflight(self) -> set[str]:
        with self._launch_lock:
            return set(self._inflight)

    def reconcile(self, active_run_ids: set[str], *, host_stopped=False, candidate_ids=None) -> int:
        """Release durable summary slots only for runs absent from live inventory."""
        if self.store is None:
            return 0
        if not isinstance(active_run_ids, set) or any(
            not isinstance(run_id, str) or not run_id for run_id in active_run_ids
        ):
            raise TypeError("active_run_ids must be a set of non-empty strings")
        if candidate_ids is not None and not candidate_ids:
            return 0
        now = self.store.clock()
        with self._launch_lock, self.store.transaction() as db:
            active_run_ids = active_run_ids | (set() if host_stopped else self._inflight)
            if active_run_ids:
                marks = ",".join("?" for _ in active_run_ids)
                query = (
                    "UPDATE summary_runs SET state='unavailable',finished_at=?,reason=? "
                    f"WHERE state='running' AND kind!='task' AND run_id NOT IN ({marks})"
                )
                params = (now, "host_restart", *sorted(active_run_ids))
            else:
                query = (
                    "UPDATE summary_runs SET state='unavailable',finished_at=?,reason=? "
                    "WHERE state='running' AND kind!='task'"
                )
                params = (now, "host_restart")
            if candidate_ids is not None:
                query += " AND run_id IN (" + ",".join("?" for _ in candidate_ids) + ")"
                params += tuple(candidate_ids)
            return db.execute(query, params).rowcount

    @staticmethod
    def _safe_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
        if usage is None:
            return None
        if not isinstance(usage, dict):
            return None
        allowed = {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "provider_context_window",
            "native_compactions",
            "source",
        }
        result = {}
        model = usage.get("resolved_model")
        if isinstance(model, str) and model.strip():
            result["resolved_model"] = model
        for key in allowed:
            value = usage.get(key)
            if type(value) is int and 0 <= value <= 10_000_000:
                result[key] = value
            elif key == "source" and isinstance(value, str) and 0 < len(value) <= 64:
                result[key] = value
        return result or None

    def _isolated_output(self, request: SummaryLaunch) -> tuple[str | SummaryOutput, str | None]:
        """Run a strict runner and return output plus a terminal failure reason."""
        run_id = request.run_id
        handle = None
        deadline = time.monotonic() + SUMMARY_TIMEOUT_SECONDS

        def confirmed_stop() -> bool:
            if handle is None:
                return True
            try:
                return self.provider.stop(handle)
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                return False

        try:
            with self._launch_lock:
                if not self._accepting:
                    raise SummaryUnavailable("summary host is not accepting new work")
                handle = self.provider.start(request)
                self._active[run_id] = handle
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SummaryTimedOut
            output = self.provider.wait(handle, remaining)
            return output, None
        except (SummaryTimedOut, TimeoutError):
            if handle is not None and confirmed_stop():
                return "", "timeout"
            return "", "timeout_unconfirmed"
        except SummaryInputTooLarge:
            if handle is None:
                return "", "input_limit"
            if confirmed_stop():
                return "", "provider_error"
            return "", "provider_exit_unconfirmed"
        except SummaryUnavailable as exc:
            if handle is None or confirmed_stop():
                return "", "output_limit" if str(exc) == "output_limit" else "provider_unavailable"
            return "", "provider_exit_unconfirmed"
        except Exception:
            if handle is None:
                # Sandbox.start can create a guest process before reporting an
                # error. Without a returned Run there is no proof of
                # exit; retain the durable reservation until inventory recovery.
                return "", "provider_exit_unconfirmed"
            if confirmed_stop():
                return "", "provider_error"
            return "", "provider_exit_unconfirmed"
        finally:
            if handle is not None:
                with self._launch_lock:
                    self._active.pop(run_id, None)

    def run_isolated(
        self,
        prompt: str,
        *,
        kind: str = "summary",
        instructions: str | None = None,
        reserve=None,
        max_input_bytes: int = MAX_SUMMARY_PROMPT_BYTES,
        caller_session_id: str | None = None,
        input_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Run one bounded, no-authority provider turn and return its text.

        ``reserve(db, run_id)`` returns the account reservation for this workload, ``{}``
        when the firm has no named accounts, or ``None`` when nothing is available.
        The result carries ``status`` ``completed`` with ``text``, or
        ``unavailable`` with a ``reason``; usage is included when reported.
        """
        try:
            prompt_bytes = check_prompt_size(prompt, max_input_bytes)
        except SummaryInputTooLarge:
            return {"status": "unavailable", "reason": "input_limit"}
        if self.provider is None:
            return {"status": "unavailable", "reason": "provider_unavailable"}
        if not self._slots.acquire(blocking=False):
            return {"status": "unavailable", "reason": "busy"}
        if input_bytes is None:
            input_bytes = prompt_bytes
        try:
            with self._launch_lock:
                reserved = (
                    self._reserve_run(input_bytes, reserve, kind, caller_session_id)
                    if self._accepting
                    else None
                )
        except Exception:
            self._slots.release()
            raise
        if reserved is None:
            self._slots.release()
            return {"status": "unavailable", "reason": "busy"}
        run_id, reservation = reserved
        started = self.store.clock() if self.store is not None else 0.0
        request = SummaryLaunch(
            run_id=run_id,
            prompt=prompt,
            timeout=SUMMARY_TIMEOUT_SECONDS,
            account_id=reservation.get("account_id"),
            profile=reservation.get("profile"),
            provider=reservation.get("provider"),
            model=reservation.get("model"),
            effort=reservation.get("effort"),
            start_token=reservation.get("start_token"),
            kind=kind,
            instructions=instructions,
            max_input_bytes=max_input_bytes,
        )
        try:
            try:
                output, terminal_reason = self._isolated_output(request)
            except SummaryTimedOut:
                terminal_reason = "timeout"
                output = ""
            if terminal_reason is not None:
                result = {"status": "unavailable", "reason": terminal_reason}
                if terminal_reason.endswith("unconfirmed"):
                    if self.store is not None:
                        with self.store.transaction() as db:
                            db.execute(
                                "UPDATE summary_runs SET reason=?,latency_ms=? WHERE run_id=?",
                                (
                                    terminal_reason,
                                    max(0, round((self.store.clock() - started) * 1000)),
                                    run_id,
                                ),
                            )
                    return result
                self._finish(run_id, result, started)
                return result
            if isinstance(output, SummaryOutput):
                text, output_tokens, usage = output.text, output.output_tokens, output.usage
            else:
                text, output_tokens, usage = "", None, None
            safe_usage = self._safe_usage(usage)

            def unavailable(reason: str) -> dict[str, Any]:
                result = {"status": "unavailable", "reason": reason}
                if safe_usage is not None:
                    result["usage"] = safe_usage
                self._finish(run_id, result, started)
                return result

            if isinstance(output, SummaryOutput) and output.error:
                # Keep the provider's short failure kind (process_exit, result_error,
                # ...) so the run record says why; anything else is provider_error.
                error = output.error
                known = isinstance(error, str) and PROVIDER_REASON.fullmatch(error)
                return unavailable(error if known else "provider_error")
            if not isinstance(text, str) or not text.strip():
                return unavailable("empty_summary")
            raw = text.encode("utf-8")
            truncated = len(raw) > MAX_SUMMARY_TEXT_BYTES
            if truncated:
                if kind != "summary":
                    return unavailable("output_limit")
                suffix = "\n" + SUMMARY_TRUNCATION_MARKER
                kept_bytes = MAX_SUMMARY_TEXT_BYTES - len(suffix.encode("utf-8"))
                text = raw[:kept_bytes].decode("utf-8", errors="ignore") + suffix
            result = {"status": "completed", "text": text}
            if truncated:
                result["truncated"] = True
            if output_tokens is not None:
                result["output_tokens"] = output_tokens
            if safe_usage is not None:
                result["usage"] = safe_usage
            self._finish(run_id, result, started)
            return result
        finally:
            with self._launch_lock:
                self._inflight.discard(run_id)
            self._slots.release()

    def summarize(
        self, snapshot: ContextSnapshot, *, caller_session_id: str | None = None
    ) -> dict[str, Any]:
        result_base = {
            "derived": True,
            "attribution": "derived from the listed source messages",
            "source_message_ids": list(snapshot.source_message_ids),
            "omissions": list(snapshot.omissions),
            "coverage": {
                "matching_messages": snapshot.matching_count,
                "selected_messages": len(snapshot.messages),
                "input_bytes": snapshot.input_bytes,
                "omitted_messages": max(0, snapshot.matching_count - len(snapshot.messages)),
                "oldest_selected_message_id": (
                    snapshot.source_message_ids[0] if snapshot.source_message_ids else None
                ),
                "newest_selected_message_id": (
                    snapshot.source_message_ids[-1] if snapshot.source_message_ids else None
                ),
            },
        }
        outcome = self.run_isolated(
            snapshot.prompt(),
            reserve=self._summary_reservation(caller_session_id),
            caller_session_id=caller_session_id,
            input_bytes=snapshot.input_bytes,
        )
        if outcome["status"] != "completed":
            result = {**result_base, "status": "unavailable", "reason": outcome["reason"]}
        else:
            result = {**result_base, "status": "summarized", "summary": outcome["text"]}
            if outcome.get("truncated"):
                result["truncated"] = True
            if "output_tokens" in outcome:
                result["output_tokens"] = outcome["output_tokens"]
        if "usage" in outcome:
            result["usage"] = outcome["usage"]
        return result
