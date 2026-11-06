"""Synchronous task admission and durable answers with an injected execution runner."""

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from . import analysis_history
from .errors import FirmError
from .provider_contract import ProviderSetupError
from .store import encoded


@dataclass(frozen=True)
class TaskLaunch:
    run_id: str
    caller_activation_id: str
    instruction: str
    account_id: str
    provider: str
    model: str
    effort: str
    deadline: float
    max_answer_bytes: int
    start_token: str = ""


class TaskRunner(Protocol):
    """Execution boundary; provider command construction is supplied separately.

    start returns promptly with an owned handle; raising guarantees no launch.
    wait returns complete text, or raises TimeoutError while still running.
    stop returns promptly and confirms all execution empty, including after success.
    """

    def start(self, request: TaskLaunch) -> Any: ...
    def wait(self, handle: Any, timeout: float) -> str: ...
    def stop(self, handle: Any) -> bool: ...


class TaskExecutionError(Exception):
    """A confirmed guest outcome with a task-level refusal reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def unavailable(reason):
    return {"status": "unavailable", "reason": reason}


class TaskService:
    def __init__(self, runtime, runner: TaskRunner | None = None):
        self.runtime, self.store, self.runner = runtime, runtime.store, runner
        self._changed = threading.Condition()
        self._launch_lock = threading.Lock()
        self._inflight = set()

    def _reserve(self, token, instruction, request_id):
        with self.runtime.authorized(token) as actor, self.store.transaction() as db:
            payload = {"instruction": instruction}
            prior = self.store.replay(db, actor["employee_id"], request_id, "task", payload)
            if prior is not None:
                return actor, prior, None
            config = self.runtime.accounts.config(db)
            settings = config["task"]
            result, launch = None, None
            if not isinstance(instruction, str) or not instruction.strip():
                result = unavailable("invalid_instruction")
            elif len(instruction.encode("utf-8")) > settings["max_instruction_bytes"]:
                result = unavailable("instruction_too_large")
            elif self.store.firm(db)["state"] != "running":
                result = unavailable("cancelled")
            else:
                result, launch = self._admit(db, actor, instruction, config)
            self.store.request(
                db,
                actor["employee_id"],
                request_id,
                "task",
                payload,
                lambda: result,
                activation_id=actor["activation_id"],
            )
            return actor, result, launch

    def _admit(self, db, actor, instruction, config):
        settings = config["task"]
        outstanding = db.execute(
            "SELECT COUNT(*) FROM summary_runs t JOIN activations a "
            "ON a.activation_id=t.caller_activation_id "
            "WHERE t.kind='task' AND t.state='running' AND a.employee_id=?",
            (actor["employee_id"],),
        ).fetchone()[0]
        if outstanding >= settings["max_concurrent"]:
            return unavailable("busy"), None
        accounts = self.runtime.accounts
        binding = accounts.binding(db, actor["session_id"])
        if binding is None:
            return unavailable("binding"), None
        account_id, provider = binding["account_id"], binding["provider"]
        account = config.get("accounts", {}).get(account_id)
        if account is None or account["provider"] != provider:
            return unavailable("configuration"), None
        accounts._refresh_quota_threshold(db, account_id, account)
        for scope, key in (("profile", binding["profile"]), ("account", account_id)):
            if accounts._incident(db, scope, key):
                return unavailable("hold"), None
        borrowing = db.execute(
            "SELECT 1 FROM summary_runs WHERE kind='task' AND state='running' "
            "AND caller_activation_id=? AND reason IS NULL LIMIT 1",
            (actor["activation_id"],),
        ).fetchone()
        if borrowing and accounts._usage(db, account_id) >= account["max_active"]:
            return unavailable("capacity"), None
        if self.runner is None:
            return unavailable("runner_unavailable"), None
        now = self.store.clock()
        launch = TaskLaunch(
            "task-" + uuid4().hex,
            actor["activation_id"],
            instruction,
            account_id,
            provider,
            **settings[provider],
            deadline=now + settings["timeout_seconds"],
            max_answer_bytes=settings["max_answer_bytes"],
            start_token=secrets.token_urlsafe(32),
        )
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,profile,"
            "provider,deadline,kind,caller_activation_id,token_hash) "
            "VALUES(?,?,'running',?,?,?,?,?,'task',?,?)",
            (
                launch.run_id,
                now,
                len(instruction.encode("utf-8")),
                account_id,
                binding["profile"],
                provider,
                launch.deadline,
                actor["activation_id"],
                hashlib.sha256(launch.start_token.encode()).hexdigest(),
            ),
        )
        analysis_history.summary_bound(
            db,
            now,
            launch.run_id,
            actor["session_id"],
            {"account_id": account_id, "provider": provider, **settings[provider]},
            activation_id=actor["activation_id"],
        )
        return {"status": "pending", "run_id": launch.run_id}, launch

    def call(self, token, instruction, request_id, *, cancelled=None):
        cancelled = cancelled or threading.Event()
        with self._launch_lock:
            actor, result, launch = self._reserve(token, instruction, request_id)
            if launch is not None:
                self._inflight.add(launch.run_id)
        if launch is None:
            # Another call owns the launch. The durable journal works across service
            # instances too; no authorization transaction survives a wait.
            while result["status"] == "pending":
                with self._changed:
                    if cancelled.is_set():
                        return unavailable("cancelled")
                    with self.store.read() as db:
                        try:
                            self.runtime._authenticate(db, token)
                        except FirmError:
                            return unavailable("cancelled")
                        result = self.store.replay(
                            db, actor["employee_id"], request_id, "task",
                            {"instruction": instruction},
                        )
                    if result["status"] == "pending":
                        self._changed.wait(1.0)
            return result
        try:
            return self._run(token, actor, launch, request_id, cancelled)
        finally:
            with self._launch_lock:
                self._inflight.discard(launch.run_id)
            with self._changed:
                self._changed.notify_all()

    def recovery_candidates(self):
        """Snapshot abandoned reservations without racing admission or answer commit."""
        with self._launch_lock, self.store.read() as db:
            return {row[0] for row in db.execute(
                "SELECT run_id FROM summary_runs WHERE kind='task' AND state='running'"
            )} - self._inflight

    def _run(self, token, actor, launch, request_id, cancelled):
        runner = self.runner
        handle = None
        confirmed = True
        end = time.monotonic() + max(0, launch.deadline - self.store.clock())
        try:
            # Revalidate immediately before handing execution to the injected runner.
            self.runtime.authenticate(token)
            if cancelled.is_set():
                result = unavailable("cancelled")
            elif self.store.clock() >= launch.deadline:
                result = unavailable("timeout")
            else:
                handle = runner.start(launch)
                while True:
                    self.runtime.authenticate(token)
                    if cancelled.is_set():
                        result = unavailable("cancelled")
                        break
                    remaining = min(end - time.monotonic(), launch.deadline - self.store.clock())
                    if remaining <= 0:
                        result = unavailable("timeout")
                        break
                    try:
                        answer = runner.wait(handle, min(1.0, remaining))
                    except TimeoutError:
                        continue
                    if not isinstance(answer, str) or not answer.strip():
                        result = unavailable("invalid_answer")
                    elif len(answer.encode("utf-8")) > launch.max_answer_bytes:
                        result = unavailable("answer_too_large")
                    else:
                        result = {"status": "completed", "answer": answer}
                    break
        except ProviderSetupError as exc:
            if exc.category in {"auth", "config", "quota"}:
                with self.store.transaction() as db:
                    self.runtime.accounts.hold(db, "account", launch.account_id, exc.category)
            result = unavailable(
                "configuration" if exc.category == "config" else "provider_failure"
            )
        except TaskExecutionError as exc:
            result = unavailable(exc.reason)
        except FirmError:
            result = unavailable("cancelled")
        except Exception:
            result = unavailable("provider_failure")
        finally:
            if handle is not None:
                try:
                    confirmed = runner.stop(handle) is True
                except Exception:
                    confirmed = False
        with self.store.transaction() as db:
            try:
                self.runtime._authenticate(db, token)
                parent = db.execute(
                    "SELECT deadline,stop_cause FROM sandbox_runs WHERE activation_id=?",
                    (launch.caller_activation_id,),
                ).fetchone()
                if parent and (parent["stop_cause"] or parent["deadline"] <= self.store.clock()):
                    result = unavailable("cancelled")
            except FirmError:
                result = unavailable("cancelled")
            if cancelled.is_set():
                result = unavailable("cancelled")
            if result["status"] == "completed" and (
                time.monotonic() >= end or self.store.clock() >= launch.deadline
            ):
                result = unavailable("timeout")
            if not confirmed:
                result = unavailable(result.get("reason", "exit") + "_unconfirmed")
            result = self._finish(
                db, launch.run_id, actor["employee_id"], request_id, result, confirmed
            )
        return result

    def _finish(self, db, run_id, employee_id, request_id, result, confirmed):
        state = db.execute("SELECT state FROM summary_runs WHERE run_id=?", (run_id,)).fetchone()
        if state["state"] != "running":
            # Confirmed shutdown/recovery may have finalized this call while its
            # worker was unwinding. Preserve the one durable answer; never resurrect it.
            row = db.execute(
                "SELECT result_json FROM requests WHERE actor_id=? AND request_id=?",
                (employee_id, request_id),
            ).fetchone()
            return json.loads(row["result_json"])
        now = self.store.clock()
        db.execute(
            "UPDATE summary_runs SET state=?,finished_at=?,reason=?,"
            "latency_ms=MAX(0,ROUND((?-started_at)*1000)) WHERE run_id=?",
            (
                ("summarized" if result["status"] == "completed" else "unavailable")
                if confirmed
                else "running",
                now if confirmed else None,
                result.get("reason"),
                now,
                run_id,
            ),
        )
        db.execute(
            "UPDATE requests SET result_json=? WHERE actor_id=? AND request_id=?",
            (encoded(result), employee_id, request_id),
        )
        return result

    def recover_confirmed(self, run_ids):
        """Finalize abandoned reservations only after external confirmation of exit.

        A stage 2 host must close launches and confirm child cleanup before calling.
        No missing in-memory handle or summary inventory implies task exit.
        """
        with self.store.transaction() as db:
            for run_id in run_ids:
                row = db.execute(
                    "SELECT q.actor_id,q.request_id,q.result_json FROM requests q "
                    "JOIN summary_runs t ON t.run_id=json_extract(q.result_json,'$.run_id') "
                    "WHERE q.operation='task' AND t.kind='task' AND t.state='running' "
                    "AND t.run_id=?",
                    (run_id,),
                ).fetchone()
                if row:
                    self._finish(
                        db,
                        run_id,
                        row["actor_id"],
                        row["request_id"],
                        unavailable("cancelled"),
                        True,
                    )
                else:
                    # Already-returned unconfirmed refusals stay immutable on replay.
                    db.execute(
                        "UPDATE summary_runs SET state='unavailable',finished_at=? "
                        "WHERE run_id=? AND kind='task' AND state='running'",
                        (self.store.clock(), run_id),
                    )
        with self._changed:
            self._changed.notify_all()
