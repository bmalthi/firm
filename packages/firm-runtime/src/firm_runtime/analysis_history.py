"""Best-effort observations, including sample-derived owner disk notices."""

import json
import logging
import math
import shlex
import threading
from functools import wraps
from uuid import uuid4

from .prompt_resources import render

LOGGER = logging.getLogger(__name__)
_failures = set()
_failure_lock = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at REAL NOT NULL,
    kind TEXT NOT NULL,
    employee_id TEXT,
    activation_id TEXT,
    summary_run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_events_by_time ON analysis_events(observed_at,sequence);
CREATE INDEX IF NOT EXISTS analysis_events_by_role_kind
    ON analysis_events(employee_id,kind,sequence);
CREATE TABLE IF NOT EXISTS resource_samples (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at REAL NOT NULL,
    machine TEXT NOT NULL,
    boot TEXT,
    cpus INTEGER,
    memory_total_bytes INTEGER,
    memory_available_bytes INTEGER,
    memory_stall_usec INTEGER,
    memory_full_stall_usec INTEGER,
    cpu_stall_usec INTEGER,
    cpu_usec INTEGER,
    oom_kills INTEGER,
    swapouts INTEGER,
    compressed_bytes INTEGER,
    pressure_level INTEGER,
    disk_used_bytes INTEGER,
    disk_total_bytes INTEGER,
    host_footprint_bytes INTEGER,
    host_cpu_usec INTEGER
);
CREATE INDEX IF NOT EXISTS resource_samples_by_machine
    ON resource_samples(machine,observed_at);
"""
RESOURCE_INTERVAL = 60.0
DISK_RECOVERY_FRACTION = 0.80
RESOURCE_FIELDS = (
    "boot",
    "cpus",
    "memory_total_bytes",
    "memory_available_bytes",
    "memory_stall_usec",
    "memory_full_stall_usec",
    "cpu_stall_usec",
    "cpu_usec",
    "oom_kills",
    "swapouts",
    "compressed_bytes",
    "pressure_level",
    "disk_used_bytes",
    "disk_total_bytes",
    "host_footprint_bytes",
    "host_cpu_usec",
)
ROLE_FIELDS = (
    "employment",
    "execution_profile",
    "hold_reason",
    "retry_at",
    "recovery_episode",
    "attempt_count",
)
EVENT_KINDS = frozenset(
    "analysis.started analysis.stopped analysis.gap firm.state_changed role.created "
    "role.retired role.paused role.resumed role.routed role.retry_scheduled role.held "
    "role.retry_authorized activation.handoff_saved appointment.started appointment.renewed "
    "appointment.promoted summary.bound context.pressure_changed context.compaction_observed "
    "readiness.observed mail.presented level.chosen level.defaulted github.token_minted "
    "github.token_refused github.repository_created github.access_granted "
    "github.request_refused mac.disk request.opened request.granted request.denied "
    "request.withdrawn".split()
)
EVENT_FIELDS = frozenset(
    "session_id provider model effort account_id profile fresh before after employment "
    "execution_profile hold_reason retry_at recovery_episode attempt_count previous_episode "
    "failure_category next kind remaining_periods used_periods added_periods employee_id epoch "
    "value previous cause baseline current_context_tokens context_window activity reasons "
    "next_check_at evidence blockers incidents model_class reason attempts repositories access "
    "expires_at error repository source messages senders lease_id station phase free_bytes "
    "total_bytes reclaimed_bytes reclaim completed timed_out refused categories category "
    "count bytes skipped skipped_bytes skipped_reasons simulators step returncode said "
    "request_id resource purpose".split()
)


def public_event(value):
    if isinstance(value, dict):
        return {k: public_event(v) for k, v in value.items() if k in EVENT_FIELDS}
    if isinstance(value, list):
        return [public_event(v) for v in value]
    return value


def best_effort(function):
    """Isolate only observation work; never wrap the underlying domain operation."""

    @wraps(function)
    def guarded(db, *args, **kwargs):
        opened = False
        try:
            db.execute("SAVEPOINT analysis_recording")
            opened = True
            result = function(db, *args, **kwargs)
            db.execute("RELEASE analysis_recording")
            return result
        except Exception:
            LOGGER.exception("Analysis recording failed; observation coverage is incomplete")
            if opened and db.in_transaction:
                try:
                    db.execute("ROLLBACK TO analysis_recording")
                    db.execute("RELEASE analysis_recording")
                except Exception:
                    LOGGER.exception("Could not restore analysis savepoint")
            try:
                with _failure_lock:
                    _failures.add(db.execute("PRAGMA database_list").fetchone()[2])
            except Exception:
                # The original failure is logged; the database may be unavailable.
                pass
            return None

    return guarded


def recording_failed(path):
    with _failure_lock:
        return str(path) in _failures


def _record(
    db, now, event_kind, employee_id=None, activation_id=None, summary_run_id=None, **payload
):
    if event_kind not in EVENT_KINDS:
        raise ValueError("unknown analysis event kind")
    body = json.dumps(public_event(payload), separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(body.encode()) > 4096:
        raise ValueError("analysis event exceeds 4 KiB")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    if recording_failed(path):
        db.execute(
            "INSERT INTO analysis_events(observed_at,kind,payload_json) "
            "VALUES(?,'analysis.gap','{}')",
            (now,),
        )
    result = db.execute(
        "INSERT INTO analysis_events(observed_at,kind,employee_id,activation_id,"
        "summary_run_id,payload_json) VALUES(?,?,?,?,?,?)",
        (now, event_kind, employee_id, activation_id, summary_run_id, body),
    ).lastrowid
    with _failure_lock:
        _failures.discard(path)
    return result


record = best_effort(_record)


@best_effort
def role_changed(db, now, before, employee_id, *, activation_id=None, category=None):
    after = dict(
        db.execute(
            "SELECT " + ",".join(ROLE_FIELDS) + " FROM roles WHERE employee_id=?",
            (employee_id,),
        ).fetchone()
    )
    old = {key: before[key] for key in ROLE_FIELDS} if before else None
    kinds = []
    if old is None:
        kinds.append("role.created")
    else:
        if old["employment"] != after["employment"]:
            kinds.append(
                {"active": "role.resumed", "paused": "role.paused", "retired": "role.retired"}[
                    after["employment"]
                ]
            )
        if old["execution_profile"] != after["execution_profile"]:
            kinds.append("role.routed")
        if old["hold_reason"] != after["hold_reason"] and after["hold_reason"]:
            kinds.append("role.held")
        if old["retry_at"] != after["retry_at"] and after["retry_at"] is not None:
            kinds.append("role.retry_scheduled")
    for kind in kinds:
        _record(
            db,
            now,
            kind,
            employee_id,
            activation_id,
            before=old,
            after=after,
            failure_category=category,
        )


@best_effort
def firm_changed(db, now, before):
    state = db.execute("SELECT state FROM firm").fetchone()[0]
    if before != state:
        _record(db, now, "firm.state_changed", before=before, after=state)


@best_effort
def appointment_started(db, now, employee_id):
    activation = db.execute(
        "SELECT activation_id FROM activations WHERE employee_id=? AND state='admitted'",
        (employee_id,),
    ).fetchone()
    terms = dict(
        db.execute("SELECT * FROM appointments WHERE employee_id=?", (employee_id,)).fetchone()
    )
    _record(
        db,
        now,
        "appointment.started",
        employee_id,
        activation[0] if activation else None,
        **{k: terms[k] for k in ("kind", "remaining_periods", "used_periods")},
    )


@best_effort
def summary_bound(db, now, run_id, caller_session_id, reservation, *, activation_id=None):
    caller = db.execute(
        "SELECT employee_id FROM context_sessions WHERE session_id=?", (caller_session_id,)
    ).fetchone()
    _record(
        db,
        now,
        "summary.bound",
        caller[0] if caller else None,
        activation_id,
        summary_run_id=run_id,
        session_id=caller_session_id,
        **{key: reservation.get(key) for key in ("provider", "model", "effort", "account_id")},
    )


@best_effort
def context_observed(db, now, session_id, cause):
    # Observe accepted counters; do not invoke the claim gate or guidance writer.
    row = dict(
        db.execute("SELECT * FROM context_sessions WHERE session_id=?", (session_id,)).fetchone()
    )
    window = row["soft_context_window"] or row["provider_context_window"]
    current = row["current_context_tokens"]
    level = None
    if window is not None and current is not None:
        import math

        level = (
            "handoff_required"
            if current >= max(0, window - row["handoff_reserve"])
            else "advisory"
            if current >= math.ceil(window * row["advisory_fraction"])
            else "normal"
        )
    for kind, value in (
        ("context.pressure_changed", level),
        ("context.compaction_observed", row["native_compactions"]),
    ):
        prior = db.execute(
            "SELECT payload_json FROM analysis_events WHERE employee_id=? AND kind=? "
            "AND json_extract(payload_json,'$.session_id')=? ORDER BY sequence DESC LIMIT 1",
            (row["employee_id"], kind, session_id),
        ).fetchone()
        previous = json.loads(prior[0])["value"] if prior else None
        if value == previous:
            continue
        if kind.endswith("compaction_observed") and (value is None or value <= (previous or 0)):
            continue
        _record(
            db,
            now,
            kind,
            row["employee_id"],
            session_id=session_id,
            value=value,
            previous=previous,
            cause=cause,
            baseline=prior is None,
            current_context_tokens=current,
            context_window=window,
        )


def _linux_disk_alerted(db):
    from .analysis import LOW_DISK_FRACTION

    # resource_samples_by_machine(machine, observed_at) scans newest readings first.
    outside_band = db.execute(
        "SELECT 1.0*disk_used_bytes/disk_total_bytes AS fraction FROM resource_samples "
        "WHERE machine='linux' AND disk_used_bytes IS NOT NULL AND disk_total_bytes>0 "
        "AND (1.0*disk_used_bytes/disk_total_bytes>=? "
        "OR 1.0*disk_used_bytes/disk_total_bytes<?) "
        "ORDER BY observed_at DESC,sequence DESC LIMIT 1",
        (LOW_DISK_FRACTION, DISK_RECOVERY_FRACTION),
    ).fetchone()
    return outside_band is not None and outside_band["fraction"] >= LOW_DISK_FRACTION


def record_resources(db, now, machine, sample, *, notice=None, sandbox_toml=None):
    from .analysis import disk_state
    from .store import OWNER

    state = disk_state({**sample, "observed_at": now, "machine": machine,
                        "disk_used_bytes": sample.get("disk_used_bytes"),
                        "disk_total_bytes": sample.get("disk_total_bytes")}, now)
    notify = (machine == "linux" and notice is not None
              and not state["unknown"])
    before = _linux_disk_alerted(db) if notify else None
    columns = ",".join(RESOURCE_FIELDS)
    marks = ",".join("?" for _ in RESOURCE_FIELDS)
    db.execute(
        f"INSERT INTO resource_samples(observed_at,machine,{columns}) VALUES(?,?,{marks})",
        (now, machine, *(sample.get(field) for field in RESOURCE_FIELDS)),
    )
    if not notify:
        return
    after = _linux_disk_alerted(db)
    if before == after:
        return
    used_percent = state["used_bytes"] / sample["disk_total_bytes"] * 100
    free = state["free_bytes"] / (1024**3)
    if after:
        config = shlex.quote(str(sandbox_toml)) if sandbox_toml is not None else "PATH"
        body = render("notices", "linux_disk_low_body", config=config,
                      used_percent=f"{used_percent:.0f}", free=f"{free:.1f}")
        if sandbox_toml is None:
            body += render("notices", "sandbox_config_path_suffix")
        subject = render("notices", "linux_disk_low_subject")
    else:
        subject = render("notices", "linux_disk_recovered_subject")
        body = render("notices", "linux_disk_recovered_body",
                      used_percent=math.floor(used_percent), free=f"{free:.1f}")
    notice(db, [OWNER], body, subject=subject)


class AnalysisObserver:
    """Post-tick samples in separate transactions; errors never alter host control flow."""

    def __init__(self, store, sandbox, *, messaging=None, sandbox_toml=None):
        from .messaging import Messaging

        self.messaging = messaging if messaging is not None else Messaging(store)
        self.sandbox_toml = sandbox_toml
        self.store = store
        self.sandbox = sandbox
        self.epoch = uuid4().hex
        self.previous = {}
        self.failed = False
        self.sampled_resources_at = None

    def event(self, kind):
        try:
            with self.store.transaction() as db:
                record(db, self.store.clock(), kind, epoch=self.epoch)
        except Exception:
            self.failed = True
            LOGGER.exception("Analysis host observation failed")

    def start(self):
        self.event("analysis.started")

    def stop(self):
        self.event("analysis.stopped")

    def resources(self):
        """A machine-wide sample a minute while a VM runs; counters are cumulative.

        Nothing is recorded while both VMs are stopped: the host's own counters say
        nothing about a firm that is not running.
        """
        now = self.store.clock()
        if (
            self.sampled_resources_at is not None
            and now - self.sampled_resources_at < RESOURCE_INTERVAL
        ):
            return
        self.sampled_resources_at = now
        try:
            samples = self.sandbox.resources()
            if not any(sample for machine, sample in samples.items() if machine != "host"):
                return
            with self.store.transaction() as db:
                for machine, sample in samples.items():
                    if sample is not None:
                        record_resources(
                            db, now, machine, sample, notice=self.messaging.notice,
                            sandbox_toml=self.sandbox_toml,
                        )
        except Exception:
            LOGGER.exception("Resource sample failed; observation coverage is incomplete")

    def sample(self):
        try:
            from .analysis import AnalysisReader

            reader = AnalysisReader(self.store.path, clock=self.store.clock)
            # Internal sampling visits all roles; public responses are paged.
            with reader.snapshot() as (db, now):
                rows = reader.readiness(db, now)
            with self.store.transaction() as db:
                if self.failed:
                    record(db, now, "analysis.gap", epoch=self.epoch)
                updates = {}
                for row in rows:
                    if row["activity"] == "retired":
                        continue
                    key = json.dumps(
                        [
                            row["activity"],
                            row["reasons"],
                            row["next_check_at"],
                            row["stable_evidence"],
                        ],
                        sort_keys=True,
                    )
                    if self.previous.get(row["employee_id"]) != key:
                        saved = record(
                            db,
                            now,
                            "readiness.observed",
                            row["employee_id"],
                            epoch=self.epoch,
                            activity=row["activity"],
                            reasons=row["reasons"],
                            next_check_at=row["next_check_at"],
                            evidence=row["stable_evidence"],
                        )
                        if saved is None:
                            raise RuntimeError("readiness observation was not recorded")
                        updates[row["employee_id"]] = key
            self.previous.update(updates)
            self.failed = False
        except Exception:
            self.failed = True
            LOGGER.exception("Analysis sample failed; observation coverage is incomplete")
