"""Execution host for real sandbox invocations, with durable ownership and reconciliation."""

import fcntl
import json
import logging
import math
import os
import shlex
import sqlite3
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from firm_sandbox import Run, Sandbox

from . import google_cloud
from .errors import FirmError
from .execution_activity import DEFAULT_IDLE_TIMEOUT, record_activity
from .prompt_records import record_inputs, role_tool_catalog
from .provider_support import activation_url
from .public_profiles import ingest_public_profiles
from .runtime import Runtime
from .summaries import summary_role_id
from .task_runner import reconcile_tasks
from .workspaces import grants_for, provision_runtime_role, sandbox_role_id, sandbox_run_id

LOGGER = logging.getLogger(__name__)
RUN_JOURNAL_RETENTION = 7 * 24 * 60 * 60  # A week for postmortems; host history is permanent.
RUN_JOURNAL_SWEEP_INTERVAL = 60 * 60


class SandboxDispatcher:
    def __init__(
        self,
        runtime: Runtime,
        sandbox: Sandbox,
        providers: dict,
        *,
        mcp_url: str,
        execution_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ):
        if not math.isfinite(execution_timeout) or execution_timeout <= 0:
            raise FirmError("execution inactivity timeout must be finite and positive")
        self.runtime, self.sandbox, self.providers = runtime, sandbox, providers
        self.mcp_url, self.execution_timeout = mcp_url, execution_timeout
        self._locks = []
        self._launches: dict[str, Future] = {}
        # Last streamed allowance event recorded per activation; a quiet run must
        # not re-record an older reading over a concurrent run's newer one.
        self._subscription_events: dict[str, str] = {}
        # Each activation's output as read so far, so a tick fetches only a log's new
        # tail instead of paging it from byte 0. Its length is the next read's offset.
        self._logs: dict[str, bytearray] = {}
        self._pool = None
        self._retired = set()
        self._cleaned = set()
        self._retire_not_before = {}
        self._summary_runner = None
        self._task_runner = None
        self._provision_role = None
        self._next_journal_sweep = 0
        self._preflight_failures: frozenset[tuple[str, str, str]] | None = None
        # A tick whose probe failed recorded "unknown"; the next healthy one restores it.
        self._probe_failed = False

    def __enter__(self):
        if self._locks:
            raise FirmError("dispatcher is already open")
        try:
            for path in (
                self.runtime.store.path.with_suffix(".host.lock"),
                self.sandbox.config.locks / "runtime-owner.lock",
            ):
                # The firm lock directory also holds the sandbox's lifecycle lock,
                # which requires it to be owner-only.
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                handle = path.open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise FirmError("another execution host owns this firm or sandbox") from exc
                self._locks.append(handle)
            with self.runtime.store.transaction() as db:
                firm = self.runtime.store.firm(db)
                config = json.loads(firm["config_json"])
                existing = db.execute("SELECT * FROM execution_host WHERE singleton=1").fetchone()
                root, firm_id = str(self.sandbox.config.root), self.sandbox.config.firm_id
                identity = self.sandbox.identity()
                if existing is not None:
                    self._validate_binding(db, existing, root, firm_id, identity)
                    db.execute(
                        "UPDATE execution_host SET sandbox_root=? WHERE singleton=1", (root,)
                    )
                    # The previous runtime process owned those runs; reacquiring
                    # its host locks cannot resume them, so they are swept before new work.
                    db.execute(
                        "UPDATE sandbox_runs SET stop_cause='host_restart' "
                        "WHERE stop_cause IS NULL AND activation_id IN "
                        "(SELECT activation_id FROM activations "
                        "WHERE state IN ('admitted','running'))"
                    )
                else:
                    db.execute(
                        "INSERT INTO execution_host(singleton,sandbox_root,sandbox_firm_id,"
                        "sandbox_identity) VALUES(1,?,?,?)",
                        (root, firm_id, identity),
                    )
            self._pool = ThreadPoolExecutor(
                max_workers=config["max_active"], thread_name_prefix="firm-launch"
            )
            self._provision_role = lambda employee_id: provision_runtime_role(
                self.runtime.store,
                self.sandbox.config.root,
                employee_id,
                files=self.sandbox.file_server,
            )
            self.runtime.lifecycle.provision_role = self._provision_role
            self.runtime.discovery.files = self.sandbox.file_server
            self.runtime.messaging.files = self.sandbox.file_server
            self.runtime.hosts.sandbox = self.sandbox
            self.runtime.lifecycle.layout_for = lambda employee_id: self.sandbox.layout(
                sandbox_role_id(employee_id)
            )
            self._tool_catalog = role_tool_catalog(self.runtime)
            self.runtime.mac.open(
                acquire=lambda employee_id, activation_id: self.sandbox.mac_acquire(
                    sandbox_role_id(employee_id), activation_id
                ),
                release=self.sandbox.mac_release,
                status=self.sandbox.mac_status,
                connect=self.sandbox.mac_connect,
                write_work_file=lambda employee_id, directory, name, data: (
                    self.sandbox.file_server.write_work_file(
                        sandbox_role_id(employee_id), directory, name, data
                    )
                ),
            )
            if hasattr(self.providers, "summary_runner"):
                # Context summaries share this already-running sandbox host; they
                # do not admit a role, boot another VM, or receive role authority.
                self._summary_runner = self.providers.summary_runner(
                    self.sandbox, mcp_url=self.mcp_url
                )
                self.runtime.summary_service.provider = self._summary_runner
            self._open_task_runner()
            return self
        except BaseException:
            self._release()
            raise

    def _open_task_runner(self):
        if self._task_runner is None or self._task_runner.closed:
            if hasattr(self.providers, "task_runner"):
                self._task_runner = self.providers.task_runner(self.sandbox, mcp_url=self.mcp_url)
            self.runtime.task_service.runner = self._task_runner

    def _validate_binding(self, db, existing, root: str, firm_id: str, identity: str) -> None:
        if (existing["sandbox_firm_id"], existing["sandbox_identity"]) != (firm_id, identity):
            raise FirmError("firm is bound to a different sandbox identity")
        if existing["sandbox_root"] == root:
            return
        # A copied database or directory must not turn one firm into two hosts.
        # lstat semantics count even a dangling symlink as an occupied old path.
        previous = Path(existing["sandbox_root"])
        if os.path.lexists(previous):
            raise FirmError("cannot relocate firm while its previous sandbox root exists")
        active = db.execute(
            "SELECT 1 FROM activations WHERE state IN ('admitted','running') LIMIT 1"
        ).fetchone()
        if self.runtime.store.firm(db)["state"] != "stopped" or active is not None:
            raise FirmError("stop the firm and settle its executions before relocating it")
        if self.sandbox.infrastructure_status().get("running") is not False:
            raise FirmError("firm VMs must be confirmed stopped before relocating the firm")
        db.execute("UPDATE execution_host SET infrastructure_state='stopped' WHERE singleton=1")

    def _require_open(self):
        if not self._locks:
            raise FirmError("execution host must own the firm and sandbox")

    def _infrastructure_state(self, value: str):
        with self.runtime.store.transaction() as db:
            db.execute("UPDATE execution_host SET infrastructure_state=?", (value,))

    def _records(self, *, active_only=False):
        with self.runtime.store.read() as db:
            sql = "SELECT s.*,a.state,a.started_at,a.launch_attempted_at,a.session_id "
            sql += "FROM sandbox_runs s JOIN activations a USING(activation_id)"
            if active_only:
                sql += " WHERE a.state IN ('admitted','running')"
            return [dict(r) for r in db.execute(sql)]

    def _inventory(self) -> dict[str, tuple[Run, dict]]:
        # Sandbox.inventory carries the current VM generation, so a run from a
        # discarded generation is simply absent rather than mismatched.
        return self.sandbox.inventory()

    def _summary_records(self, run_ids=()):
        with self.runtime.store.read() as db:
            query = "SELECT * FROM summary_runs WHERE kind!='task' AND (state='running'"
            if run_ids:
                query += " OR run_id IN (" + ",".join("?" for _ in run_ids) + ")"
            query += ")"
            return [dict(row) for row in db.execute(query, tuple(run_ids))]

    def _save_run(self, activation_id: str, run: Run):
        with self.runtime.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM sandbox_runs WHERE activation_id=?", (activation_id,)
            ).fetchone()
            if not row or (row["role_id"], row["run_id"]) != (run.role_id, run.run_id):
                raise FirmError("sandbox returned a mismatched invocation")
            if row["vm_generation"] and row["vm_generation"] != run.vm_generation:
                raise FirmError("sandbox invocation VM generation changed")
            db.execute(
                "UPDATE sandbox_runs SET vm_generation=? WHERE activation_id=?",
                (run.vm_generation, activation_id),
            )

    def start(self):
        self._require_open()
        if self._launches or (self._summary_runner and self._summary_runner.pending_launches()):
            raise FirmError("finish pending launch calls before restarting the firm VMs")
        if hasattr(self.providers, "preflight"):
            self._preflight()
        with self.runtime.store.transaction() as db:
            if self.runtime.store.firm(db)["state"] in {"draining", "stopping"}:
                raise FirmError("finish stopping current execution before starting")
            db.execute(
                "UPDATE execution_host SET infrastructure_state='starting' WHERE singleton=1"
            )
        try:
            self.sandbox.up()
            try:
                # The runtime is the record of the owner's grants; a restarted firm keeps
                # them. Roles never run against a policy that differs from the record.
                self.runtime.hosts.push()
            except (OSError, RuntimeError, ValueError) as exc:
                # Runs adopted from before a host restart are still executing under a
                # policy that is not the record; nothing may keep running past this.
                try:
                    self.sandbox.down(force=True)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    LOGGER.exception("Could not power off the firm VMs after a failed start")
                raise FirmError(
                    f"the owner's granted hosts could not be applied to the sandbox: {exc}; "
                    "the firm VMs were powered off"
                ) from exc
            self._infrastructure_state("running")
            self.reconcile()
            self._open_task_runner()
            self.runtime.start()
        except BaseException:
            self._infrastructure_state("unknown")
            raise
        return self.runtime.status()

    def _preflight(self):
        """Report newly observed local setup failures without spamming every tick."""
        report = self.providers.preflight()
        firm = shlex.quote(str(self.runtime.store.path.parent.parent))
        failures = frozenset(
            (failure["scope"], failure["dependency"], failure["code"])
            for failure in report.get("failures", [])
        )
        previous = self._preflight_failures or frozenset()
        for scope, dependency, code in sorted(failures - previous):
            LOGGER.warning(
                "Provider preflight failed for %s %s (%s). Run `firm accounts %s` to "
                "inspect the hold and configured authentication path; repair it, then run "
                "`firm clear-hold %s INCIDENT_ID`.",
                scope,
                dependency,
                code,
                firm,
                firm,
            )
        if self._preflight_failures and not failures:
            LOGGER.info(
                "Provider preflight now passes. Existing holds remain until cleared with "
                "`firm clear-hold %s INCIDENT_ID`.",
                firm,
            )
        self._preflight_failures = failures
        return report

    def reconcile(self):
        """Reattach matching runs. Missing runs count as exited only after a healthy inventory."""
        self._require_open()
        ingest_public_profiles(
            self.runtime.store,
            files=self.sandbox.file_server,
        )
        if self._task_runner:
            self._task_runner.reconcile(stopping=self.runtime.status()["state"] == "stopping")
        else:
            reconcile_tasks(self.runtime, self.sandbox)
        summary_candidates = {row["run_id"] for row in self._summary_records()}
        summary_protected = self.runtime.summary_service.inflight()
        if self._summary_runner:
            summary_protected |= self._summary_runner.pending_launches()
        inventory = self._inventory()
        summary_records = self._summary_records(inventory)
        for row in summary_records:
            entry = inventory.get(row["run_id"])
            if entry is not None:
                run, info = entry
                if (run.role_id == summary_role_id(row["run_id"])
                        and not info.get("running")):
                    self._record_settlement(run, info)
        summary_protected |= {row["run_id"] for row in summary_records} - summary_candidates
        if self._summary_runner:
            active_summaries = self._summary_runner.reconcile(
                inventory,
                summary_records,
                now=self.runtime.store.clock(),
                stopping=self.runtime.status()["state"] == "stopping",
                protected=summary_protected,
            )
            self.runtime.summary_service.reconcile(
                active_summaries | summary_protected,
                candidate_ids=summary_candidates,
            )
        records = self._records()
        hard_stopping = self.runtime.status()["state"] == "stopping"
        registered = {r["run_id"] for r in records} | {r["run_id"] for r in summary_records}
        for run_id, (_run, info) in inventory.items():
            if run_id not in registered and info["running"]:
                raise FirmError("sandbox has active execution not registered to this firm")
        for row in records:
            entry = inventory.get(row["run_id"])
            if row["state"] not in {"admitted", "running"}:
                if entry is not None and entry[1]["running"]:
                    raise FirmError("a completed activation still has active sandbox execution")
                continue
            # A hard stop may act on a deterministically identified run even
            # while its sandbox call is unresolved. Keep the Future reservation
            # until it resolves so the VM cannot shut down ahead of a late create.
            if row["activation_id"] in self._launches and (
                entry is None or not (hard_stopping or row["stop_cause"] == "owner_stop")
            ):
                continue
            if entry is None:
                self._missing(row)
            else:
                run, info = entry
                self._save_run(row["activation_id"], run)
                self._poll_run(row, run, info)
        # Crash between admission and creation of a launch record has no external process.
        with self.runtime.store.read() as db:
            orphans = [
                r[0]
                for r in db.execute(
                    "SELECT activation_id FROM activations WHERE state IN ('admitted','running') "
                    "AND activation_id NOT IN (SELECT activation_id FROM sandbox_runs)"
                )
            ]
        for aid in orphans:
            self.runtime.finish(aid, "interrupted", category="host_restart")
        self._prune_run_journals(inventory)
        return inventory

    def _record_settlement(self, run, info):
        with self.runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO sandbox_run_settlements "
                "(run_id,role_id,vm_generation,recorded_at,usage_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "usage_json=excluded.usage_json "
                "WHERE usage_json IS NOT excluded.usage_json",
                (run.run_id, run.role_id, run.vm_generation, self.runtime.store.clock(),
                 json.dumps(info.get("usage"))),
            )

    def _prune_run_journals(self, inventory):
        now = self.runtime.store.clock()
        if now < self._next_journal_sweep:
            return
        self._next_journal_sweep = now + RUN_JOURNAL_SWEEP_INTERVAL
        cutoff = now - RUN_JOURNAL_RETENTION
        with self.runtime.store.read() as db:
            roles = {row["run_id"]: dict(row) for row in db.execute(
                "SELECT s.*,a.session_id FROM sandbox_runs s "
                "JOIN activations a USING(activation_id) "
                "WHERE a.state NOT IN ('admitted','running') AND a.finished_at<?", (cutoff,),
            )}
            summaries = {row[0] for row in db.execute(
                "SELECT run_id FROM summary_runs WHERE kind!='task' "
                "AND state!='running' AND finished_at<?",
                (cutoff,),
            )}
        protected = self.runtime.summary_service.inflight()
        if self._summary_runner:
            protected |= self._summary_runner.pending_launches()
        for run_id, (run, info) in inventory.items():
            ended = info.get("ended")
            if (info.get("running") or info.get("unresolved") or run_id in protected
                    or (ended is not None and ended >= cutoff)
                    or (ended is None and not info.get("interrupted"))):
                continue
            row = roles.get(run_id)
            if row is None and run_id not in summaries:
                continue  # Operator journals and unknown execution are never ours to discard.
            if row is None and run.role_id != summary_role_id(run_id):
                continue
            if row is not None and (row["role_id"], row["vm_generation"]) != (
                run.role_id, run.vm_generation,
            ):
                continue
            try:
                if row is not None:
                    logs = self._read_logs(row, run, info)
                    if logs is None:
                        continue
                    self._record_tool_calls(row, logs, strict=True)
                    self._observe_usage(row, logs, historical=True, strict=True)
                    self._record_usage(row["activation_id"], info.get("usage"))
                self._record_settlement(run, info)
                self.sandbox.remove(run)
            except (
                OSError, RuntimeError, TypeError, ValueError, OverflowError, sqlite3.Error,
                subprocess.TimeoutExpired,
            ) as exc:
                LOGGER.warning("Run journal %s retained (%s)", run_id, type(exc).__name__)
            finally:
                if row is not None:
                    self._forget_log(row["activation_id"])

    def _missing(self, row):
        if row["stop_cause"] == "owner_stop" or self.runtime.status()["state"] == "stopping":
            self.runtime.finish(row["activation_id"], "interrupted", category="owner_stop")
        else:
            self.runtime.finish(
                row["activation_id"],
                "interrupted",
                category="host_restart",
                failure="sandbox invocation is absent from healthy inventory",
            )
        self._forget_log(row["activation_id"])
        self._clear_unresumable_session(row)

    def _clear_unresumable_session(self, row):
        with self.runtime.store.transaction() as db:
            if db.execute(
                "SELECT 1 FROM provider_sessions WHERE session_id=?", (row["session_id"],)
            ).fetchone():
                return
            activation = self.runtime._activation(db, row["activation_id"])
            role = self.runtime.store.role(db, activation["employee_id"])
            recovery = json.loads(role["recovery_json"]) if role["recovery_json"] else None
            if recovery:
                recovery["context_retained"] = False
            db.execute(
                "UPDATE roles SET session_id=NULL,recovery_json=? WHERE employee_id=?",
                (json.dumps(recovery) if recovery else None, role["employee_id"]),
            )

    def _provider(self, row):
        if hasattr(self.providers, "for_session"):
            return self.providers.for_session(row["session_id"])
        provider = self.providers.get(row["provider"])
        if provider is None:
            raise FirmError("execution profile needed for reconciliation is not configured")
        return provider

    def _remember_session(self, row, parsed):
        native = parsed.provider_session_id
        if native:
            with self.runtime.store.transaction() as db:
                old = db.execute(
                    "SELECT * FROM provider_sessions WHERE session_id=?", (row["session_id"],)
                ).fetchone()
                if old and (old["provider"], old["native_session_id"]) != (row["provider"], native):
                    raise FirmError("provider resumed a different conversation")
                db.execute(
                    "INSERT OR IGNORE INTO provider_sessions VALUES(?,?,?)",
                    (row["session_id"], row["provider"], native),
                )

    def _observe_usage(self, row, logs, *, historical=False, strict=False):
        provider = self._provider(row)
        parser = getattr(provider, "parse_usage", None)
        if parser is not None:
            try:
                self.runtime.record_usage(row["activation_id"], parser(logs), historical=historical)
            except (TypeError, ValueError, OverflowError):
                if strict:
                    raise
                # Usage is advisory telemetry. A malformed or changed provider event
                # must not fail otherwise valid role execution.
                pass
        if historical:
            return  # Old journal windows must never become fresh subscription readings.
        subscription = getattr(provider, "parse_subscription", None)
        reading = subscription(logs) if subscription is not None else None
        aid = row["activation_id"]
        if reading is None or self._subscription_events.get(aid) == reading["event_id"]:
            return
        with self.runtime.store.read() as db:
            binding = self.runtime.accounts.binding(db, row["session_id"])
        if binding:
            self.runtime.accounts.record_subscription(
                binding["account_id"],
                reading["windows"],
                status=reading.get("status"),
                warning=reading.get("warning"),
            )
        self._subscription_events[aid] = reading["event_id"]

    def _read_logs(self, row, run, info):
        """The run's full output so far, fetched only past what was already read.

        ``info["log_bytes"]`` is the guest's own stat of the log file, taken at the
        same moment as ``info["running"]``, so a size equal to what is already
        buffered means nothing new was written and costs no call at all. A run's
        log only ever grows.
        """
        buffer = self._logs.setdefault(row["activation_id"], bytearray())
        size = info["log_bytes"]
        if size > len(buffer):
            try:
                buffer.extend(self.sandbox.logs(run, offset=len(buffer), until=size))
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                LOGGER.warning(
                    "Log snapshot unavailable for %s (%s)", run.run_id, type(exc).__name__
                )
                return None
        if len(buffer) < size:
            LOGGER.warning("Log snapshot incomplete for %s", run.run_id)
            return None
        return bytes(buffer)

    def _forget_log(self, activation_id):
        self._logs.pop(activation_id, None)
        self._subscription_events.pop(activation_id, None)

    def _record_usage(self, activation_id, usage):
        """What the role's slice used, as the guest read it before stopping the slice.

        Unknown stays NULL: the peak is an upper bound (it counts page cache), and a
        run whose slice was already gone has no reading at all.
        """
        if not usage:
            return
        with self.runtime.store.transaction() as db:
            db.execute(
                "UPDATE sandbox_runs SET memory_peak_bytes=?,cpu_usec=?,oom_kills=? "
                "WHERE activation_id=?",
                (
                    usage.get("memory_peak_bytes"),
                    usage.get("cpu_usec"),
                    usage.get("oom_kills"),
                    activation_id,
                ),
            )

    def _record_tool_calls(self, row, logs, *, strict=False):
        """Tally what the period called, from the log this collection already holds."""
        extract = getattr(self._provider(row), "parse_tool_calls", None)
        if extract is None:
            return
        try:
            self.runtime.record_tool_calls(row["activation_id"], extract(logs))
        except (TypeError, ValueError, OverflowError, sqlite3.Error) as exc:
            if strict:
                raise
            # A changed provider log shape or a failed write leaves the period
            # unmeasured, which coverage reports; it must not fail an otherwise valid
            # role execution or the tick collecting it.
            LOGGER.warning("Tool calls for %s not recorded: %s", row["activation_id"], exc)

    def _finish_run(self, row, run, info, *, parsed=None, logs=None):
        # A pending sandbox call can still start its guest run after a concurrent stop.
        # Keep the role reservation until that launch call has also settled.
        if row["activation_id"] in self._launches:
            return
        self._record_usage(row["activation_id"], info.get("usage"))
        self._record_settlement(run, info)
        if info.get("unresolved"):
            raise FirmError("sandbox has not confirmed execution cleanup")
        with self.runtime.store.read() as db:
            activation = self.runtime._activation(db, row["activation_id"])
            row = {**row, "state": activation["state"], "started_at": activation["started_at"]}
        if row["stop_cause"] == "host_restart":
            logs = self._read_logs(row, run, info)
            if logs is not None:
                self._observe_usage(row, logs, historical=True)
                self._record_tool_calls(row, logs)
            # Runtime loss is established when the host locks are reacquired;
            # partial provider output is not needed to classify interruption.
            self.runtime.finish(row["activation_id"], "interrupted", category="host_restart")
            self._forget_log(row["activation_id"])
            self._clear_unresumable_session(row)
            return
        if parsed is None:
            logs = self._read_logs(row, run, info)
            if logs is not None:
                parsed = self._provider(row).parse_result(logs, info["exit_code"])
            elif row["stop_cause"] not in {"owner_stop", "timeout"}:
                return  # Retry terminal collection; missing logs are not a provider outcome.
        if logs is not None:
            self._observe_usage(row, logs)
            self._record_tool_calls(row, logs)
        if parsed is not None:
            self._remember_session(row, parsed)
        # A run can finish its work in the instant between the last refresh and the stop:
        # it then exited by itself, successfully, and the claim does not make that a timeout.
        completed = (
            row["state"] == "running"
            and info.get("exit_code") == 0
            and parsed is not None
            and parsed.success
        )
        if row["stop_cause"] == "owner_stop":
            self.runtime.finish(row["activation_id"], "interrupted", category="owner_stop")
        elif row["stop_cause"] == "timeout" and not completed:
            self.runtime.finish(
                row["activation_id"], "failed", failure="execution inactivity timeout expired"
            )
        elif parsed is not None and getattr(parsed, "interrupted", False):
            self.runtime.finish(row["activation_id"], "interrupted", category="transient")
        elif row["state"] == "running" and parsed.success:
            self.runtime.finish(row["activation_id"])
        else:
            kind = parsed.error.kind if parsed.error else "invalid_output"
            category = getattr(parsed.error, "category", "unknown") if parsed.error else "unknown"
            retry_at = getattr(parsed.error, "reset_at", None)
            retry_seconds = getattr(parsed.error, "retry_after_seconds", None)
            if retry_seconds is not None:
                retry_at = max(retry_at or 0, self.runtime.store.clock() + retry_seconds)
            # Raw provider text can contain secrets. Persist only the bounded
            # classification, plus the bounded provider code and configured model
            # when the adapter supplied them - e.g. "provider: result_error
            # not_found_error model=claude-x" names exactly what was rejected.
            failure = f"provider: {kind}"
            if code := getattr(parsed.error, "code", None):
                failure += f" {code}"
            if model := getattr(parsed.error, "model", None):
                failure += f" model={model}"
            self.runtime.finish(
                row["activation_id"],
                "failed",
                failure=failure,
                category=category,
                retry_after=retry_at,
            )
        self._subscription_events.pop(row["activation_id"], None)
        self._forget_log(row["activation_id"])
        self._clear_unresumable_session(row)

    def _poll_run(self, row, run, info):
        if info.get("unresolved"):
            self._record_usage(row["activation_id"], info.get("usage"))
            self._record_settlement(run, info)
            if row["stop_cause"] != "host_restart":
                info = self.sandbox.stop(run)
        if row["stop_cause"] == "host_restart":
            # An unsettled launch retains its reservation and ownership until
            # its sandbox call settles, even when a restart stop is pending.
            if row["activation_id"] in self._launches:
                return
            info = self.sandbox.stop(run)
            if info.get("running") or info.get("unresolved"):
                raise FirmError("interrupted execution has not confirmed cleanup")
            self._finish_run(row, run, info)
            return
        logs = self._read_logs(row, run, info)
        if logs is not None:
            record_activity(self.runtime.store, row["activation_id"], output=logs)
            self._observe_usage(row, logs)
        if not info["running"]:
            self._finish_exited_run(row, run, info, logs)
            return
        with self.runtime.store.read() as db:
            session = db.execute(
                "SELECT 1 FROM provider_sessions WHERE session_id=?", (row["session_id"],)
            ).fetchone()
        if not session and logs is not None:
            parsed = self._provider(row).parse_result(logs, 0)
            self._remember_session(row, parsed)
        # One reading of the clock decides both whether a stop may be due and, below,
        # whether the deadline passed, so no timeout is claimed that was not refreshed for.
        now = self.runtime.store.clock()
        if (
            row["stop_cause"] is None
            and now < row["deadline"]
            and self.runtime.status()["state"] != "stopping"
        ):
            return
        # The inventory this tick polled from is a snapshot taken before this run was
        # read; a run that wrote its last output and exited since then is still seen here
        # as running and silent. Refresh status and activity before claiming any
        # termination from that snapshot: a run that turns out to have already exited is
        # settled by its own exit, and output the refresh picks up can still push the
        # deadline past the claim that follows.
        info = self.sandbox.status(run)
        logs = self._read_logs(row, run, info)
        if logs is not None:
            record_activity(self.runtime.store, row["activation_id"], output=logs)
            self._observe_usage(row, logs)
        if not info["running"]:
            self._finish_exited_run(row, run, info, logs)
            return
        # Read the refreshed deadline and claim termination in one transaction,
        # serialized with concurrent role tool calls. Once claimed, new output
        # cannot revoke a stop already in progress.
        with self.runtime.store.transaction() as db:
            current = db.execute(
                "SELECT deadline,stop_cause FROM sandbox_runs WHERE activation_id=?",
                (row["activation_id"],),
            ).fetchone()
            cause = current["stop_cause"]
            if self.runtime.store.firm(db)["state"] == "stopping":
                cause = "owner_stop"
            elif cause is None and now >= current["deadline"]:
                cause = "timeout"
            if cause:
                db.execute(
                    "UPDATE sandbox_runs SET stop_cause=? WHERE activation_id=?",
                    (cause, row["activation_id"]),
                )
        if cause:
            info = self.sandbox.stop(run)
            if info["running"]:
                raise FirmError("sandbox has not confirmed execution termination")
            self._finish_run({**row, "stop_cause": cause}, run, info)

    def _finish_exited_run(self, row, run, info, logs):
        """Settle a run whose status already shows it not running, by its own exit."""
        if logs is None:
            if row["stop_cause"] in {"owner_stop", "timeout"}:
                self._finish_run(row, run, info)
            return
        parsed = self._provider(row).parse_result(logs, info["exit_code"])
        state = self.runtime.status()["state"]
        if (
            row["stop_cause"] is None
            and row["state"] == "admitted"
            and state in {"draining", "stopping"}
        ):
            error = getattr(parsed, "error", None)
            # Admission closure itself causes this specific prestart exit.
            # Other exits during drain remain genuine provider/host failures.
            if error is not None and error.kind == "activation_gate":
                with self.runtime.store.transaction() as db:
                    db.execute(
                        "UPDATE sandbox_runs SET stop_cause='owner_stop' WHERE activation_id=?",
                        (row["activation_id"],),
                    )
                row = {**row, "stop_cause": "owner_stop"}
        self._finish_run(row, run, info, parsed=parsed, logs=logs)

    def _hard_stop(self):
        # Power off first: guest inventory and SSH may be unavailable precisely
        # when an owner needs this control. No activation settles before proof.
        # Close new starts and make a bounded attempt to settle pending
        # summary launches before the host-only hard stop. A failed guest stop is
        # intentionally tolerated; VM shutdown and inventory reconciliation are
        # the authoritative exit proof.
        if self._task_runner:
            self._task_runner.cancel_all()
        self.runtime.summary_service.close()
        if self._summary_runner:
            self._summary_runner.cancel_all()
        summary_before = self._summary_runner.pending_launches() if self._summary_runner else set()
        self.sandbox.down(force=True)
        if self.sandbox.infrastructure_status()["running"] is not False:
            raise FirmError("firm VM shutdown is not confirmed")
        if self._task_runner:
            self._task_runner.close()
            if self.sandbox.infrastructure_status()["running"] is not False:
                raise FirmError("Task cleanup is not confirmed after launch settled")
        self._tasks_host_stopped()
        summary_pending = self._summary_runner.pending_launches() if self._summary_runner else set()
        if summary_before - summary_pending:
            # A summary launch may have settled after the first power-off. No late
            # create can follow this second proof once all launches are settled.
            self.sandbox.down(force=True)
            if self.sandbox.infrastructure_status()["running"] is not False:
                raise FirmError("firm VM shutdown is not confirmed after summary launch settled")
        if self._summary_runner:
            self._summary_runner.confirm_host_stop(exclude=summary_pending)
        self.runtime.summary_service.reconcile(summary_pending, host_stopped=True)
        for aid, future in list(self._launches.items()):
            if not future.done():
                continue
            try:
                run = future.result()
            except Exception:
                run = None
            # A late sandbox result is not proof of termination. Reassert the
            # host boundary after it settles, before releasing its reservation.
            self.sandbox.down(force=True)
            if self.sandbox.infrastructure_status()["running"] is not False:
                raise FirmError("firm VM shutdown is not confirmed after launch settled")
            if run is not None:
                self._save_run(aid, run)
            del self._launches[aid]
        for row in self._records(active_only=True):
            if row["activation_id"] not in self._launches:
                self._missing(row)
        if not self._launches and not summary_pending:
            with self.runtime.store.read() as db:
                active = db.execute(
                    "SELECT activation_id FROM activations WHERE state IN ('admitted','running')"
                ).fetchall()
            for row in active:
                self.runtime.finish(row[0], "interrupted", category="owner_stop")
            self._infrastructure_state("stopped")
            with self.runtime.store.transaction() as db:
                self.runtime._settle_stop(db)
            self.runtime.github.settle()
        return []

    def _tasks_host_stopped(self):
        # Called only after VM shutdown proof and closing this host's launch gate.
        with self.runtime.store.read() as db:
            ids = [row[0] for row in db.execute(
                "SELECT run_id FROM summary_runs WHERE kind='task' AND state='running'"
            )]
        self.runtime.task_service.recover_confirmed(ids)

    def _cleanup_activation(self, db, employee_id):
        request = db.execute(
            "SELECT activation_id,payload_json FROM requests WHERE operation='handoff' "
            "AND activation_id=(SELECT activation_id FROM activations WHERE employee_id=? "
            "AND state='completed' AND next_intent IS NOT NULL ORDER BY rowid DESC LIMIT 1)",
            (employee_id,),
        ).fetchone()
        if (request and request["activation_id"] not in self._cleaned
                and json.loads(request["payload_json"]).get("clean_up", False)):
            return request["activation_id"]
        return None

    def _clean_up(self, invocation):
        if invocation.context.get("handoff_notes") is None:
            return False
        with self.runtime.store.read() as db:
            return self._cleanup_activation(db, invocation.employee_id) is not None

    def _expired_cleanup_activation(self, db, employee_id):
        activation = db.execute(
            "SELECT a.activation_id,a.state,a.failure_category,s.stop_cause FROM activations AS a "
            "JOIN appointments AS p USING(employee_id) "
            "LEFT JOIN sandbox_runs AS s ON s.activation_id=a.activation_id WHERE a.employee_id=? "
            "AND p.kind='temporary' AND p.remaining_periods=0 AND a.finished_at IS NOT NULL "
            "ORDER BY a.finished_at DESC,a.rowid DESC LIMIT 1", (employee_id,),
        ).fetchone()
        if (activation and activation["activation_id"] not in self._cleaned
                and activation["stop_cause"] is None
                and (activation["state"] == "completed" or (
                    activation["state"] == "failed"
                    and activation["failure_category"] in {"unknown", "refusal"}))):
            return activation["activation_id"]
        return None

    def _clean_idle_roles(self, inventory):
        now = self.runtime.store.clock()
        busy = {run.role_id for run, info in inventory.values()
                if info.get("running") or info.get("unresolved")}
        with self.runtime.store.read() as db:
            roles = []
            for row in db.execute(
                "SELECT employee_id,employment,handoff_notes FROM roles WHERE "
                "(employment='retired' OR handoff_notes IS NOT NULL OR "
                "(employment='active' AND employee_id IN (SELECT employee_id FROM appointments "
                "WHERE kind='temporary' AND remaining_periods=0))) AND employee_id NOT IN "
                "(SELECT employee_id FROM activations WHERE state IN ('admitted','running')) "
                "ORDER BY rowid"
            ):
                cleanup = (self._cleanup_activation(db, row["employee_id"])
                           if row["handoff_notes"] is not None else None)
                expired = cleanup is None and row["employment"] == "active"
                if expired:
                    cleanup = self._expired_cleanup_activation(db, row["employee_id"])
                if row["employment"] == "retired" or cleanup is not None:
                    roles.append((row["employee_id"], row["employment"] == "retired",
                                  cleanup, expired))
            busy |= {sandbox_role_id(row[0]) for row in db.execute(
                "SELECT employee_id FROM activations WHERE activation_id IN "
                f"({','.join('?' for _ in self._launches)})", tuple(self._launches),
            )}
        if not roles:
            return
        retired = self.sandbox.file_server.retired_roles()
        for employee_id, retiring, cleanup, expired in roles:
            role_id = sandbox_role_id(employee_id)
            if (role_id in busy or now < self._retire_not_before.get(role_id, 0)
                    or (retiring and role_id in retired | self._retired)):
                continue
            try:
                if expired:
                    with self.runtime.store.read() as db:
                        if not db.execute(
                            "SELECT 1 FROM roles JOIN appointments USING(employee_id) "
                            "WHERE employee_id=? AND employment='active' "
                            "AND kind='temporary' AND remaining_periods=0", (employee_id,),
                        ).fetchone():
                            continue
                receipt = self.sandbox.dispose(role_id)
                if retiring:
                    self.sandbox.retire(role_id)
                    self._retired.add(role_id)
                else:
                    self._cleaned.add(cleanup)
                    LOGGER.info("Idle-role cleanup for %s (%s): %s", role_id, cleanup, receipt)
            except Exception:
                LOGGER.warning("Idle-role cleanup failed for %s; retrying in 15 minutes", role_id,
                               exc_info=True)
                self._retire_not_before[role_id] = now + 15 * 60
            return

    def tick(self):
        self._require_open()
        if self.runtime.status()["state"] == "stopping":
            return self._hard_stop()
        summary_before = self._summary_runner.pending_launches() if self._summary_runner else set()
        observed = self.sandbox.infrastructure_status()
        if observed.get("state") == "recovery_required" or observed["running"] is None:
            self._infrastructure_state("unknown")
            self._probe_failed = True
            raise FirmError("Sandbox state needs recovery; execution ownership remains unresolved")
        if observed["running"] is False:
            state = self.runtime.status()["state"]
            if state == "stopped":
                return []
            if (
                self._launches
                or summary_before
                or (self._summary_runner and self._summary_runner.pending_launches())
            ):
                raise FirmError(
                    "firm VMs stopped while a launch was in flight; ownership is unresolved"
                )
            if self._task_runner:
                self._task_runner.close()
                # Closing waits for launch RPCs; recheck the external boundary.
                if self.sandbox.infrastructure_status()["running"] is not False:
                    raise FirmError("Task cleanup is not confirmed")
            self._tasks_host_stopped()
            for row in self._records(active_only=True):
                self._missing(row)
            if self._summary_runner:
                self._summary_runner.confirm_host_stop()
            self.runtime.summary_service.reconcile(set(), host_stopped=True)
            self._infrastructure_state("stopped")
            with self.runtime.store.transaction() as db:
                self.runtime._settle_stop(db)
            if state == "running":
                raise FirmError("firm VMs stopped unexpectedly; restart the execution host")
            return []
        if self._probe_failed:
            self._infrastructure_state("running")
            self._probe_failed = False
        for aid, future in list(self._launches.items()):
            if not future.done():
                continue
            del self._launches[aid]
            try:
                run = future.result()
            except Exception:
                # A failed sandbox call does not establish that creation/launch failed.
                # Reconcile the deterministic run ID against a healthy engine before settling.
                self.reconcile()
            else:
                self._save_run(aid, run)
        inventory = self.reconcile()
        # After reconciliation has ended this tick's exited activations: free their
        # Mac lease before a graceful drain or the next admission.
        self.runtime.mac.refresh()
        self.runtime.github.settle()
        state = self.runtime.status()
        if state["state"] in {"draining", "stopping"}:
            if not state["active"] and not self._launches and not self._summary_records():
                inventory = self._inventory()
                owned = tuple(
                    inventory[r["run_id"]][0]
                    for r in self._records()
                    if r["run_id"] in inventory
                )
                self.runtime.mac.drain()
                self.sandbox.down(expected_runs=owned)
                if self.sandbox.infrastructure_status()["running"] is not False:
                    raise FirmError("firm VM shutdown is not confirmed")
                with self.runtime.store.transaction() as db:
                    db.execute("UPDATE execution_host SET infrastructure_state='stopped'")
                    self.runtime._settle_stop(db)
            return []
        if state["state"] != "running":
            return []
        self._clean_idle_roles(inventory)
        if hasattr(self.providers, "preflight"):
            self._preflight()
        # Pending hire levels run as isolated turns off this thread; admission
        # skips those roles until a decision or the fallback is recorded.
        self.runtime.levels.tick()
        launched = []
        while invocation := self.runtime.admit():
            try:
                provision_runtime_role(
                    self.runtime.store,
                    self.sandbox.config.root,
                    invocation.employee_id,
                    files=self.sandbox.file_server,
                )
                role_id, run_id = (
                    sandbox_role_id(invocation.employee_id),
                    sandbox_run_id(invocation.activation_id),
                )
                layout = self.sandbox.layout(role_id)
                grants = grants_for(self.runtime.store, invocation.employee_id, layout)
                invocation.context["workspace_references"] = grants.references
                with self.runtime.store.read() as db:
                    role = self.runtime.store.role(db, invocation.employee_id)
                    config = self.runtime.accounts.config(db)
                    session = db.execute(
                        "SELECT * FROM provider_sessions WHERE session_id=?",
                        (invocation.session_id,),
                    ).fetchone()
                    binding = self.runtime.accounts.binding(db, invocation.session_id)
                image_access = config.get("google_cloud")
                invocation.context["capabilities"] = {
                    "mac": self.sandbox.mac is not None,
                    "github": bool(config.get("github")),
                    "google_cloud": (
                        {"project": image_access["project"]} if image_access else None
                    ),
                }
                profile = binding["profile"] if binding else role["execution_profile"]
                if session and session["provider"] != profile:
                    raise FirmError("retained conversation cannot silently change providers")
                adapter = self._provider({"provider": profile, "session_id": invocation.session_id})
                if getattr(adapter, "name", None) == "agy" and image_access:
                    raise FirmError("AGY subscription inference cannot coexist with [google_cloud]")
                spec = adapter.build_spec(
                    invocation,
                    role_id,
                    run_id,
                    self.mcp_url,
                    session["native_session_id"] if session else None,
                    layout=layout,
                    **({"mcp_tools": [tool["name"] for tool in self._tool_catalog],
                        "mcp_timeout": config["task"]["timeout_seconds"]}
                       if getattr(adapter, "name", None) == "agy" else {}),
                )
                spec = replace(
                    spec,
                    organization=grants.organization,
                    clean_up=self._clean_up(invocation),
                )
                for settings in (
                    self.runtime.github.launch_settings(
                        invocation.employee_id,
                        activation_url(self.mcp_url).replace("/activation/start", "/github/token"),
                    ),
                    google_cloud.launch_settings(self.runtime.store),
                ):
                    if settings is None:
                        continue
                    env, secrets, secret_env = settings
                    spec = replace(
                        spec,
                        env={**spec.env, **env},
                        secrets={**spec.secrets, **secrets},
                        secret_env={**spec.secret_env, **secret_env},
                    )
                record_inputs(
                    self.runtime.store.path.parent / "inputs",
                    invocation.activation_id,
                    spec,
                    kind="activations",
                    metadata={
                        "activation_id": invocation.activation_id,
                        "employee_id": invocation.employee_id,
                        "session_id": invocation.session_id,
                        "native_session_id": session["native_session_id"] if session else None,
                        "execution_profile": profile,
                        "offered_role_tools": self._tool_catalog,
                    },
                )
            except Exception as exc:
                with self.runtime.store.read() as db:
                    managed = self.runtime.accounts.binding(db, invocation.session_id) is not None
                if managed:
                    category = getattr(exc, "category", "config")
                    if category not in {"auth", "config", "transient"}:
                        category = "config"
                    self.runtime.finish(invocation.activation_id, "interrupted", category=category)
                    continue
                # Owner/configuration repair is required. Close admission before
                # returning the unlaunched reservation to its durable wake state.
                self.runtime.stop()
                self.runtime.finish(
                    invocation.activation_id, "interrupted", category="host_restart"
                )
                # Only Firm's own errors carry owner-facing text; any other
                # exception may hold credential material, so only its kind is named.
                detail = str(exc) if isinstance(exc, FirmError) else type(exc).__name__
                raise FirmError(
                    f"provider setup failed ({detail}); firm admission is closed until the "
                    "owner corrects the configuration and explicitly restarts it"
                ) from None
            with self.runtime.store.transaction() as db:
                # Stop can arrive while credentials and the launch spec are assembled.
                # No external call has occurred yet, so release this reservation directly.
                if (
                    self.runtime.store.firm(db)["state"] != "running"
                    or self.runtime.store.role(db, invocation.employee_id)["employment"] != "active"
                ):
                    self.runtime.finish(
                        invocation.activation_id, "interrupted", category="owner_stop"
                    )
                    continue
                db.execute(
                    "UPDATE activations SET context_json=? WHERE activation_id=?",
                    (json.dumps(invocation.context), invocation.activation_id),
                )
                self.runtime.mark_launch_attempted(invocation.activation_id)
                db.execute(
                    "INSERT INTO sandbox_runs(activation_id,role_id,run_id,provider,deadline) "
                    "VALUES(?,?,?,?,?)",
                    (
                        invocation.activation_id,
                        role_id,
                        run_id,
                        profile,
                        self.runtime.store.clock() + self.execution_timeout,
                    ),
                )
                db.execute(
                    "INSERT INTO execution_activity(activation_id,idle_timeout) VALUES(?,?)",
                    (invocation.activation_id, self.execution_timeout),
                )
            self._launches[invocation.activation_id] = self._pool.submit(self.sandbox.start, spec)
            launched.append(invocation)
        return launched

    def stop(self, force=False):
        self._require_open()
        return self.runtime.stop(force)

    def _release(self):
        self.runtime.mac.close()
        # The token endpoint closes with this host; nothing it issued should outlive it.
        self.runtime.github.revoke_all()
        if self._provision_role is not None:
            if self.runtime.lifecycle.provision_role is self._provision_role:
                self.runtime.lifecycle.provision_role = None
                self.runtime.discovery.files = None
            self._provision_role = None
        for handle in reversed(self._locks):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        self._locks.clear()
        # A reopened dispatcher must not retain another activation's buffered output.
        self._logs.clear()

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._task_runner:
                self._task_runner.close()
                if self.runtime.task_service.runner is self._task_runner:
                    self.runtime.task_service.runner = None
                self._task_runner = None
            self.runtime.summary_service.close()
            if self._summary_runner:
                self._summary_runner.close()
            if self._pool is not None:
                self._pool.shutdown(wait=True)
                self._pool = None
        finally:
            self._release()
