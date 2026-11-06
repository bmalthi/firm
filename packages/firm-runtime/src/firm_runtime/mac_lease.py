"""The firm's optional Mac, whose stations are leased and brokered independently.

A firm has one Mac with one or more numbered stations. An activation holds at most one;
a firm without the Mac answers every acquire with ``unavailable`` and reason ``no_mac``.
"""

import base64
import json
import logging
import re
import subprocess
import threading
import time
from datetime import UTC, datetime

from firm_sandbox.mac_station import (
    HelperNotReady,
    StationBusy,
    StationDiskFull,
    StationNeedsRecovery,
    StationNotEnrolled,
    StationSessionNotReady,
)

from . import analysis_history
from .errors import FirmError
from .prompt_resources import prompt, render
from .store import encoded

LOGGER = logging.getLogger(__name__)
MAX_DESKTOP_INPUT_BYTES = 4096
MAX_DESKTOP_REQUEST_MS = 20000  # the helper's ceiling on one request's intended duration
CAPTURES = "captures"  # under the holder's workspace on the drive

SCHEMA = """
CREATE TABLE IF NOT EXISTS mac_leases (
    lease_id TEXT PRIMARY KEY,
    station INTEGER NOT NULL,
    activation_id TEXT NOT NULL REFERENCES activations(activation_id),
    employee_id TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    released_at REAL,
    outcome TEXT CHECK(outcome IN ('released','release_failed','recovered')),
    checks_json TEXT
);
CREATE TABLE IF NOT EXISTS mac_lease_events (
    at REAL NOT NULL,
    employee_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    event TEXT NOT NULL
        CHECK(event IN ('granted','busy','released','release_failed','recovered')),
    lease_id TEXT,
    -- A 'busy' event is about every station at once, so it names no lease or station.
    station INTEGER
);
-- What each brokered desktop call cost. Never its arguments or its result: typed text
-- and captured images stay out of the database.
CREATE TABLE IF NOT EXISTS mac_operations (
    at REAL NOT NULL,
    lease_id TEXT NOT NULL,
    station INTEGER NOT NULL,
    employee_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    op TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    outcome TEXT NOT NULL  -- 'ok', or the reason the request was refused
);
"""

OPERATIONS = {
    "status",
    "list_apps",
    "list_windows",
    "inspect_ax",
    "launch_app",
    "action",
    "focus",
    "type_text",
    "capture_window",
    "release",
    "click",
    "drag",
    "scroll",
    "press_key",
    "tap_sequence",
}
UNCLEAN = "Mac release is not confirmed; VM remains running; force stop is available"

# Role-facing failures. Sandbox and helper wording is written for the owner; it goes
# to the runtime log, and the role sees one of these reasons and its message.
WAIT = (
    "Mac availability does not itself wake you. If you still need the Mac and no reply "
    "or other wake is expected, set a reminder for a useful retry. Take up other available "
    "work, or finish normally to pause with working memory intact. Hand off only when "
    "you need a fresh start or Operations requires it. A blocked work period that "
    "produces nothing is fine."
)
# What a pending answer means: only the owner can lift these, and has been asked.
ASKED = (
    "The owner has been asked; the answer arrives as mail. Take up work you already have, "
    "or finish normally to pause with working memory intact. Set a reminder when a future "
    "check is useful; hand off only when you need a fresh start or Operations requires it. "
    "A blocked work period that produces nothing is fine."
)
ROLE_MESSAGES = {
    "mac_busy": "No desktop can be leased now; at least one is held or the owner is using the Mac. "
    f"{WAIT}",
    "mac_needs_recovery": "A desktop on the Mac is waiting for cleanup to be verified "
    f"after an earlier lease, and no other is free. {ASKED}",
    "mac_not_enrolled": f"The firm's Mac is not set up yet. {ASKED}",
    "mac_session_not_ready": "No desktop account on the firm's Mac is ready for a lease. "
    f"{ASKED}",
    "mac_disk_full": "The Mac is too low on free disk space to admit a new lease, even "
    f"after reclaiming. {WAIT}",
    "mac_unavailable": f"No Mac could be leased. {WAIT}",
    "mac_release_failed": "Your work is still running on the Mac; "
    "end it and call mac_release again.",
    "mac_release_unverified": "The Mac could not confirm that your work stopped; the "
    "desktop is waiting for the owner to verify it.",
    "desktop_not_ready": "The desktop on the Mac is not ready for the desktop tools this "
    "work period; use SSH, or release the Mac and try again later.",
    "desktop_lease_unavailable": "Your applications were closed, but the desktop on the "
    "Mac could not be rebound to your lease; retry the desktop tools or release the Mac.",
    "helper_disconnected": "The desktop tools lost their connection to the Mac, so the "
    "outcome is unknown; check the application before retrying.",
    "invalid_request": "The desktop request was invalid or too large.",
    "input_limit": "Desktop text input is limited to 4096 UTF-8 bytes.",
    "desktop_apps_open": "Some applications could not be closed. Close them yourself, or "
    "call mac_release, which reports what is still running.",
    "desktop_failed": "The desktop tools could not complete the request on the Mac; "
    "check the application before retrying.",
    "capture_not_saved": "The image is returned here but was not saved on your drive; "
    "there is no drive_path. Capture again if you still need a saved image.",
}
# Helper rejections whose message describes the role's own request.
REQUEST_REJECTIONS = {"invalid_request", "operation_failed"}


def intended_ms(arguments):
    """How long the helper is asked to spend posting one request's events."""
    taps = arguments.get("taps")
    if taps is not None:
        return sum(tap.get("after_ms", 0) for tap in taps)
    return (arguments.get("hold_ms") or 0) + (arguments.get("duration_ms") or 0)


def unavailable(reason, **details):
    return {"status": "unavailable", "reason": reason, "message": ROLE_MESSAGES[reason],
            **details}


def release_failure_reason(checks):
    """Ask the role to act only when the checks identify remaining work."""
    work = checks.get("work_stopped", {})
    parts = [work.get(name, {}) for name in ("processes", "services", "launch_agents", "crontab")]
    if any(part.get("verified") is False or part.get("error") or part.get("unparsed")
           for part in parts):
        return "mac_release_unverified"
    remaining = any(part.get("extra") or part.get("empty") is False for part in parts)
    remaining = remaining or bool(checks.get("desktop_usable", {}).get("apps"))
    return "mac_release_failed" if remaining else "mac_release_unverified"


def public_result(value):
    """Keep physical host identity and the helper's owner diagnostics out of role reports."""
    private = {
        "configured_control_uid",
        "configured_station_uid",
        "uid",
        "gui_uid",
        "console_uid",
        "missing",
        "error",
        "errors",
        "unparsed",
        "cleanup_error",
    }
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items() if key not in private}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value


class MacLease:
    """Durable lease rows and events in the store; the relay connections in memory.

    Locking: ``self.lock`` guards the in-memory maps and the read-then-write steps on
    the store, and is never held across the sandbox's slow calls (acquire, release, the
    station probe) or relay I/O, so one station's work never waits on another's. An
    acquire or release in flight is named in ``self._settling`` (the activation for an
    acquire, the lease for a release), and a second caller for the same one waits on
    ``self._settled`` rather than starting another. Each lease has its own lock in
    ``self.relays`` serializing its relay I/O; a relay lock is only ever taken while
    already holding ``self.lock`` or holding nothing.

    The Mac is also an owner-request kind: an acquire only the owner can unblock opens a
    ``mac_station`` request for resource ``mac``, and the owner's grant or a recovery the
    runtime notices answers it.
    """

    name = "mac_station"

    def __init__(self, runtime):
        self.runtime = runtime
        self.attached = False  # a dispatcher has supplied the sandbox's Mac calls
        self._acquire = self._release_station = self._status = self._connect = None
        self._write_work_file = None
        self.lock = threading.RLock()
        self._settling = set()  # activations mid-acquire and leases mid-release
        self._settled = threading.Condition(self.lock)
        # lease_id -> {"lease_id", "station", "activation_id", "employee_id", **endpoint}
        self.leases = {}
        self.connections = {}  # lease_id -> relay connection
        self.relays = {}  # lease_id -> the lock serializing that lease's relay I/O

    def open(self, *, acquire, release, status, connect, write_work_file):
        with self.lock:
            self._acquire, self._release_station = acquire, release
            self._status, self._connect = status, connect
            self._write_work_file = write_work_file
            self.attached = True

    def close(self):
        with self.lock:
            self.attached = False
            open_leases = list(self.connections)
        for lease_id in open_leases:
            # Under the relay lock, so a request in flight finishes before its
            # connection goes.
            with self._relay(lease_id):
                self._close_connection(lease_id)

    def _settle(self, key):
        """Claim one acquire or release; a caller for the same one waits for its outcome."""
        with self.lock:
            while key in self._settling:
                self._settled.wait()
            self._settling.add(key)

    def _done(self, key):
        with self.lock:
            self._settling.discard(key)
            self._settled.notify_all()

    # ---- durable state -----------------------------------------------------------

    def _holders(self, db):
        """Every lease that has not been released, by station."""
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM mac_leases WHERE released_at IS NULL ORDER BY station"
            )
        ]

    def _own(self, db, activation_id):
        """The caller's own lease; an activation holds at most one."""
        row = db.execute(
            "SELECT * FROM mac_leases WHERE released_at IS NULL AND activation_id=?",
            (activation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _event(self, db, event, employee_id, activation_id, lease_id, station):
        db.execute(
            "INSERT INTO mac_lease_events(at,employee_id,activation_id,event,lease_id,station) "
            "VALUES(?,?,?,?,?,?)",
            (self.runtime.store.clock(), employee_id, activation_id, event, lease_id, station),
        )

    def _record_disk(
        self, db, employee_id, activation_id, lease_id, station, phase, disk,
        reclaimed_bytes=None, reclaim=None, simulators=None,
    ):
        """The sandbox's ``disk`` entry and what its reclaim removed, as a journal event.

        A missing figure stays null. The journal rather than a table of its own: it is
        evidence, recorded best effort, and an analyzer reading a firm from before this
        existed simply finds none. ``lease_id`` is null for a refusal: nothing was
        granted, so there is no lease to attach the reading to. ``reclaim`` and
        ``simulators`` say what a reclaim left and which simulator step timed out or
        failed, a failed one with its exit code and the end of what it said: a release
        that reclaimed less than hoped is still clean, so this is where it shows.
        """
        disk = disk or {}
        evidence = {"reclaim": reclaim, "simulators": simulators}
        analysis_history.record(
            db,
            self.runtime.store.clock(),
            "mac.disk",
            employee_id,
            activation_id,
            lease_id=lease_id,
            station=station,
            phase=phase,
            free_bytes=disk.get("free_bytes"),
            total_bytes=disk.get("total_bytes"),
            reclaimed_bytes=reclaimed_bytes,
            **{key: value for key, value in evidence.items() if value is not None},
        )

    def _record_refusals(self, employee_id, activation_id, refusals):
        """Every station found too full on the way, whatever the acquire came to."""
        if not refusals:
            return
        with self.runtime.store.transaction() as db:
            for refusal in refusals:
                self._record_disk(
                    db, employee_id, activation_id, None, refusal["station"], "refused",
                    refusal["disk"], refusal["disk_reclaimed"], refusal.get("reclaim"),
                )

    def _address(self, db, employee_id):
        return self.runtime.store.role(db, employee_id)["address"]

    def summary(self):
        """What the console shows: each station's own status with its holder's address."""
        with self.lock:
            probe = self._status if self.attached else None
        reported = probe() if probe is not None else {}
        stations = [
            {
                "station": entry.get("station"),
                "configured": bool(entry.get("configured", False)),
                "enrolled": bool(entry.get("enrolled", False)),
                "held": bool(entry.get("held", False)),
                "lease_id": entry.get("lease_id"),
                "holder": None,
                "since": entry.get("since"),
                "dirty": bool(entry.get("dirty", False)),
            }
            for entry in reported.get("stations", [])
        ]
        leased = [entry for entry in stations if entry["lease_id"] is not None]
        if leased:
            with self.runtime.store.read() as db:
                for entry in leased:
                    row = db.execute(
                        "SELECT employee_id FROM mac_leases WHERE lease_id=?",
                        (entry["lease_id"],),
                    ).fetchone()
                    if row is not None:
                        entry["holder"] = self._address(db, row["employee_id"])
        return {
            "configured": bool(reported.get("configured", False)),
            "enrolled": bool(reported.get("enrolled", False)),
            "stations": stations,
        }

    # ---- role operations ----------------------------------------------------------

    def _granted(self, lease_id):
        return {
            "status": "granted",
            **self._endpoint(lease_id),
            "message": prompt("mac-acquired.md"),
        }

    def acquire(self, token):
        actor = self.runtime.authenticate(token)
        aid, employee_id = actor["activation_id"], actor["employee_id"]
        self._settle(aid)
        try:
            with self.lock:
                if not self.attached:
                    return {"status": "unavailable", "reason": "no_mac"}
                with self.runtime.store.read() as db:
                    own = self._own(db, aid)
                if own is not None:
                    if own["outcome"] == "release_failed":
                        return unavailable(
                            release_failure_reason(json.loads(own["checks_json"] or "{}")),
                            checks=public_result(json.loads(own["checks_json"] or "{}")),
                        )
                    if own["lease_id"] in self.leases:
                        return self._granted(own["lease_id"])
                    # The row outlived the relay's memory of the lease; the Mac is still
                    # held and the caller cannot be given its endpoint back.
                    return self._busy(employee_id, aid)
            # The sandbox's acquire is slow; other station holders keep working meanwhile.
            try:
                lease = dict(self._acquire(employee_id, aid))
            except BaseException as exc:
                # The sandbox attaches every station it found too full to whatever it
                # raises, so they are kept before deciding what this failure is.
                self._record_refusals(employee_id, aid, getattr(exc, "disk_refusals", ()))
                # A lease tunnel that timed out is a Mac that could not be reached.
                if not isinstance(
                    exc, (OSError, ValueError, RuntimeError, subprocess.SubprocessError)
                ):
                    raise
                LOGGER.warning("Mac acquire for %s failed: %s", employee_id, exc)
                if isinstance(exc, StationBusy):
                    return self._busy(employee_id, aid)
                if isinstance(exc, StationNeedsRecovery):
                    return self._ask_owner(actor, "mac_needs_recovery")
                if isinstance(exc, StationNotEnrolled):
                    return self._ask_owner(actor, "mac_not_enrolled")
                if isinstance(exc, StationSessionNotReady):
                    return self._ask_owner(actor, "mac_session_not_ready")
                if isinstance(exc, StationDiskFull):
                    return self._disk_full(exc)
                return unavailable("mac_unavailable")
            if lease.get("status") == "unavailable":
                # The firm has no Mac; nothing was granted, so nothing is recorded.
                return lease
            try:
                # A full station passed over on the way to this one is not part of the
                # lease, but its disk evidence is still durable acquire state. Keep it
                # under the same compensation boundary as the grant record.
                self._record_refusals(employee_id, aid, lease.pop("disk_refusals", ()))
                with self.lock:
                    with self.runtime.store.transaction() as db:
                        db.execute(
                            "INSERT INTO mac_leases"
                            "(lease_id,station,activation_id,employee_id,acquired_at) "
                            "VALUES(?,?,?,?,?)",
                            (
                                lease["lease_id"],
                                lease["station"],
                                aid,
                                employee_id,
                                self.runtime.store.clock(),
                            ),
                        )
                        self._event(
                            db, "granted", employee_id, aid, lease["lease_id"], lease["station"]
                        )
                        self._record_disk(
                            db, employee_id, aid, lease["lease_id"], lease["station"], "acquire",
                            lease.get("disk"), lease.get("disk_reclaimed"),
                        )
                    self.leases[lease["lease_id"]] = {
                        **lease, "activation_id": aid, "employee_id": employee_id,
                    }
                    holder = self.leases[lease["lease_id"]]
            except BaseException:
                self._release_unrecorded(lease["lease_id"])
                raise
            try:
                # The external acquire may outlive the activation that requested it.
                # Keep the physical grant durable first, then refuse an expired caller
                # and run the same verified cleanup used by housekeeping.
                self.runtime.authenticate(token)
            except BaseException:
                self._release(holder)
                raise
            return self._granted(lease["lease_id"])
        finally:
            self._done(aid)

    def _ask_owner(self, actor, cause):
        """Only the owner can lift this refusal: open the role's request and say so.

        A denial does not stand: the Mac's state changes, so the next refusal asks afresh.
        """
        with self.runtime.store.transaction() as db:
            answer = self.runtime.requests.ask_owner(
                db, actor, self.name, "mac", render("notices", f"{cause}_purpose"), cause,
                standing=False,
            )
        return {**answer, "message": ROLE_MESSAGES[cause]}

    def _busy(self, employee_id, aid):
        """No station granted and at least one held: name the holders the firm knows about."""
        with self.runtime.store.read() as db:
            holders = [
                {
                    "station": row["station"],
                    "holder": self._address(db, row["employee_id"]),
                    "since": row["acquired_at"],
                }
                for row in self._holders(db)
            ]
        if not holders:
            # No firm lease explains the refusal; the owner holds the Mac lifecycle lock.
            return unavailable("mac_busy")
        with self.runtime.store.transaction() as db:
            self._event(db, "busy", employee_id, aid, None, None)
        return {"status": "busy", "holders": holders, "message": ROLE_MESSAGES["mac_busy"]}

    def _disk_full(self, exc):
        """Every station tried was too full; _record_refusals already kept the evidence."""
        return unavailable("mac_disk_full", disk=exc.disk, disk_reclaimed=exc.disk_reclaimed)

    def _release_unrecorded(self, lease_id):
        """Compensate a physical grant that could not be made durable."""
        try:
            result = dict(self._release_station(lease_id))
        except BaseException as exc:
            LOGGER.warning("Unrecorded Mac lease %s could not be released: %s", lease_id, exc)
            return
        if result.get("clean") is not True and result.get("gone") is not True:
            LOGGER.warning("Unrecorded Mac lease %s was not released cleanly", lease_id)

    def _endpoint(self, lease_id):
        return {
            key: value
            for key, value in self.leases[lease_id].items()
            if key not in {"activation_id", "employee_id"}
        }

    def _own_lease(self, activation_id):
        """The caller's in-memory lease, or None."""
        for lease in self.leases.values():
            if lease["activation_id"] == activation_id:
                return lease
        return None

    def release(self, token):
        actor = self.runtime.authenticate(token)
        with self.runtime.store.read() as db:
            own = self._own(db, actor["activation_id"])
        if own is None:
            return {"status": "unavailable", "reason": "mac_not_held"}
        result = self._release(own)
        if result.get("clean") is False:
            reason = release_failure_reason(result.get("checks", {}))
            result = {**result, "reason": reason, "message": ROLE_MESSAGES[reason]}
        return public_result(result)

    def status(self, token):
        actor = self.runtime.authenticate(token)
        summary = self.summary()
        with self.runtime.store.read() as db:
            own = self._own(db, actor["activation_id"])
        return {
            "status": "ok",
            **summary,
            "held_by_you": own is not None,
            "your_station": own["station"] if own is not None else None,
        }

    def _release(self, holder):
        """Close the relay, run the station's verified release, record the outcome.

        A second release of the same lease while one is in flight waits for it and
        answers with what it recorded, so the station is asked once.
        """
        lease_id = holder["lease_id"]
        self._settle(lease_id)
        try:
            with self.runtime.store.read() as db:
                row = db.execute(
                    "SELECT outcome,checks_json,released_at FROM mac_leases WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
            if row is not None and row["released_at"] is not None:
                # Settled while this caller waited; answer with what was recorded.
                return {"status": "ok", "lease_id": lease_id,
                        "clean": row["outcome"] in {"released", "recovered"},
                        "checks": json.loads(row["checks_json"] or "{}")}
            # Under the relay lock, so a request in flight finishes before its
            # connection goes; self.lock is not held, so other stations keep working.
            with self._relay(lease_id):
                self._close_connection(lease_id)
            try:
                result = dict(self._release_station(lease_id))
            except (OSError, ValueError, RuntimeError) as exc:
                result = {"status": "ok", "lease_id": lease_id, "clean": False,
                          "checks": {}, "errors": [str(exc)]}
            if result.get("errors"):
                LOGGER.warning("Mac release of %s reported: %s", lease_id,
                               "; ".join(map(str, result["errors"])))
            if result.get("clean") is False and not result.get("errors"):
                LOGGER.warning("Mac release of %s could not verify cleanup: %s", lease_id,
                               result.get("checks", {}))
            gone = result.get("gone") is True
            clean = gone or result.get("clean") is True
            outcome = "recovered" if gone else "released" if clean else "release_failed"
            with self.lock:
                with self.runtime.store.transaction() as db:
                    now = self.runtime.store.clock()
                    db.execute(
                        "UPDATE mac_leases SET released_at=?,outcome=?,checks_json=? "
                        "WHERE lease_id=?",
                        (
                            now if clean else None,
                            outcome,
                            encoded(result.get("checks", {})),
                            lease_id,
                        ),
                    )
                    self._event(db, outcome, holder["employee_id"], holder["activation_id"],
                                lease_id, holder["station"])
                    self._record_disk(
                        db, holder["employee_id"], holder["activation_id"], lease_id,
                        holder["station"], "release", result.get("disk"),
                        result.get("disk_reclaimed"), result.get("reclaim"),
                        result.get("simulators"),
                    )
                # Unclean or not, the key is gone and the drive detached; the holder
                # may only release again until the station verifies clean.
                self.leases.pop(lease_id, None)
                self.relays.pop(lease_id, None)
            return result
        finally:
            self._done(lease_id)

    # ---- housekeeping --------------------------------------------------------------

    def refresh(self):
        """Release leases whose activation ended; notice owner recovery of a failed release."""
        with self.lock:
            if not self.attached:
                return
            with self.runtime.store.read() as db:
                ended = [
                    holder
                    for holder in self._holders(db)
                    if db.execute(
                        "SELECT 1 FROM activations WHERE activation_id=? "
                        "AND state IN ('admitted','running')",
                        (holder["activation_id"],),
                    ).fetchone() is None
                ]
        for holder in ended:
            if self._release(holder).get("clean") is not True:
                # The activation is gone; nobody can retry. The station keeps
                # its record and marker until the owner recovers it.
                self._settle_failed(holder)
        self._notice_recovery()

    def _settle_failed(self, holder):
        with self.runtime.store.transaction() as db:
            db.execute(
                "UPDATE mac_leases SET released_at=? WHERE lease_id=? AND released_at IS NULL",
                (self.runtime.store.clock(), holder["lease_id"]),
            )

    def _notice_recovery(self):
        """A station the owner has cleaned clears the failed releases of that station."""
        with self.runtime.store.read() as db:
            if db.execute(
                "SELECT 1 FROM mac_leases WHERE outcome='release_failed'"
            ).fetchone() is None:
                return  # refresh runs every tick; ask the stations only when it matters
        clean = {
            entry["station"]
            for entry in self._status().get("stations", [])
            if not entry.get("dirty") and not entry.get("held")
        }
        if not clean:
            return
        with self.runtime.store.transaction() as db:
            recovered = False
            for row in db.execute(
                "SELECT lease_id,station,employee_id,activation_id FROM mac_leases "
                "WHERE outcome='release_failed'"
            ).fetchall():
                if row["station"] not in clean:
                    continue
                db.execute(
                    "UPDATE mac_leases SET outcome='recovered' WHERE lease_id=?",
                    (row["lease_id"],),
                )
                self._event(db, "recovered", row["employee_id"], row["activation_id"],
                            row["lease_id"], row["station"])
                recovered = True
            if recovered:
                self.runtime.requests.answer(
                    db, self._pending(db, "mac_needs_recovery"), "granted", decided_by="runtime"
                )

    # ---- owner requests -------------------------------------------------------------

    def _pending(self, db, cause=None):
        """Roles waiting on the Mac, optionally only those waiting for one cause."""
        return [
            row
            for row in db.execute(
                "SELECT * FROM owner_requests WHERE kind=? AND status='pending' ORDER BY rowid",
                (self.name,),
            )
            if cause is None or json.loads(row["payload_json"])["cause"] == cause
        ]

    def grant(self, row):
        """The owner says the Mac is ready again: every role waiting on it is told."""
        with self.runtime.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            answered = self.runtime.requests.answer(
                db, self._pending(db), "granted", decided_by="owner"
            )
        return {"resource": "mac", "status": "granted", "answered": answered}

    def deny(self, row, note):
        """The owner declines one request; the next refusal asks afresh."""
        with self.runtime.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            answered = self.runtime.requests.answer(db, [row], "denied", note, decided_by="owner")
        return {"request_id": row["request_id"], "resource": "mac", "status": "denied",
                "answered": answered}

    def drain(self):
        """Release every holder before a graceful shutdown; refuse it on an unclean station."""
        with self.lock:
            if not self.attached:
                return
            with self.runtime.store.read() as db:
                holders = self._holders(db)
        unclean = False
        for holder in holders:
            result = self._release(holder)
            if result.get("clean") is not True and result.get("gone") is not True:
                self._settle_failed(holder)
                unclean = True
        if unclean or any(
            entry.get("dirty") for entry in self._status().get("stations", [])
        ):
            raise FirmError(UNCLEAN)

    def _relay(self, lease_id):
        """The lock serializing one lease's relay I/O; two holders never share one."""
        with self.lock:
            return self.relays.setdefault(lease_id, threading.Lock())

    def _close_connection(self, lease_id):
        connection = self.connections.pop(lease_id, None)
        if connection is not None:
            connection.close()

    # ---- brokered desktop operations -------------------------------------------------

    def call(self, token, op, **arguments):
        if op not in OPERATIONS:
            raise FirmError("unknown desktop operation")
        actor = self.runtime.authenticate(token)
        aid = actor["activation_id"]
        if op == "type_text" and (
            not isinstance(arguments.get("text"), str)
            or not arguments["text"]
            or len(arguments["text"].encode("utf-8")) > MAX_DESKTOP_INPUT_BYTES
        ):
            result = unavailable("input_limit")
        elif intended_ms(arguments) > MAX_DESKTOP_REQUEST_MS:
            # A planned passage the helper would spend longer than its ceiling posting.
            result = unavailable("invalid_request")
        else:
            with self.lock:
                # Taken together, so a release in between cannot leave a stray lock.
                lease = self._own_lease(aid)
                relay = self._relay(lease["lease_id"]) if lease is not None else None
            if lease is None:
                result = {"status": "unavailable", "reason": "mac_not_held"}
            else:
                # Only this lease's relay lock is held, so another account's desktop
                # keeps working while this helper request runs.
                with relay:
                    started = time.monotonic()
                    result = self._brokered(token, lease, op, arguments)
                    elapsed_ms = (time.monotonic() - started) * 1000
                self._journal(lease, op, elapsed_ms, result)
                if op == "capture_window" and result.get("png_base64") is not None:
                    # An image means the lease was held. The save goes to the Linux VM's
                    # file server, so it runs outside the relay lock.
                    result = self._save_capture(actor["employee_id"], lease, arguments, result)
        self.runtime.authenticate(token)
        return self.runtime.present_controls(aid, public_result(result))

    def _journal(self, lease, op, duration_ms, result):
        """What one brokered call cost, so `firm analyze` can say what the loop costs.

        Only the operation's name, its duration and its outcome: what the role sent and
        what came back are never recorded.
        """
        outcome = "ok" if result.get("status") != "unavailable" else result.get("reason")
        with self.runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO mac_operations"
                "(at,lease_id,station,employee_id,activation_id,op,duration_ms,outcome) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.runtime.store.clock(),
                    lease["lease_id"],
                    lease["station"],
                    lease["employee_id"],
                    lease["activation_id"],
                    op,
                    duration_ms,
                    outcome,
                ),
            )

    def _brokered(self, token, lease, op, arguments):
        lease_id = lease["lease_id"]
        try:
            self.runtime.authenticate(token)
            with self.runtime.store.read() as db:
                own = self._own(db, lease["activation_id"])
            if own is None or own["lease_id"] != lease_id:
                raise FirmError("Mac lease ended")
            connection = self.connections.get(lease_id)
            if connection is None or not connection.usable():
                # The relay deadline is per connection; the lease is untouched, and the
                # new connection adopts the lease binding with its open applications.
                self._close_connection(lease_id)
                connection = self.connections[lease_id] = self._connect(lease_id)
            try:
                result = connection.request(op, **arguments)
            except ValueError as exc:
                # Request encoding/validation failures are local to this
                # healthy connection; expose them as a controlled result.
                LOGGER.warning("Desktop %s request rejected: %s", op, exc)
                return unavailable("invalid_request")
            result = role_helper_result(op, result)
            if op == "capture_window" and result.get("status") == "ok":
                result = {**result, "app": self._app_name(connection, arguments.get("pid"))}
            if op == "release" and result.get("clean") is True:
                try:
                    if connection.bind_lease().get("lease_bound") is not True:
                        raise ValueError("The desktop lease could not be rebound")
                except (OSError, ValueError, RuntimeError) as exc:
                    LOGGER.warning("Desktop lease rebinding failed: %s", exc)
                    self._close_connection(lease_id)
                    return unavailable("desktop_lease_unavailable")
            return result
        except HelperNotReady as exc:
            LOGGER.warning("Desktop %s refused: %s", op, exc)
            self._close_connection(lease_id)
            return unavailable("desktop_not_ready")
        except OSError as exc:
            LOGGER.warning("Desktop %s lost its relay: %s", op, exc)
            self._close_connection(lease_id)
            return unavailable("helper_disconnected")

    def _app_name(self, connection, pid):
        """The tracked application's name, for the capture's file name; None if unknown."""
        try:
            listed = connection.request("list_apps")
        except (OSError, ValueError) as exc:
            LOGGER.warning("Desktop capture could not name its application: %s", exc)
            return None
        for app in listed.get("apps", []) if listed.get("status") == "ok" else []:
            if app.get("pid") == pid and isinstance(app.get("name"), str):
                return app["name"]
        return None

    def _save_capture(self, employee_id, lease, arguments, result):
        """Save the PNG under the holder's workspace; a failed save keeps the image."""
        when = datetime.fromtimestamp(self.runtime.store.clock(), UTC)
        name = capture_name(when, result.get("app"), arguments.get("window_id"))
        try:
            data = base64.b64decode(result["png_base64"], validate=True)
            drive_path = self._write_work_file(employee_id, CAPTURES, name, data)
        except (OSError, ValueError, RuntimeError) as exc:
            LOGGER.warning("Desktop capture for %s was not saved: %s", employee_id, exc)
            return {**result, "save_reason": "capture_not_saved",
                    "save_message": ROLE_MESSAGES["capture_not_saved"]}
        return {**result, "drive_path": drive_path, "mac_path": f"{lease['drive']}/{drive_path}"}


def capture_name(when, app, window_id):
    """``<UTC time>-<app>.png``, or ``-window-<id>`` when the application has no usable name."""
    label = re.sub(r"[^a-z0-9]+", "-", (app or "").lower()).strip("-")[:40].strip("-")
    if not label:
        label = f"window-{window_id}" if isinstance(window_id, int) else "window"
    return f"{when:%Y%m%dT%H%M%SZ}-{label}.png"


def role_helper_result(op, result):
    """Replace helper wording the role cannot act on with a runtime reason and message.

    A helper rejection of the role's own request keeps its reason and message; a
    helper that is not ready or failed on its own side is reported in runtime words.
    Closing the applications (``release``) that leaves one running is its own reason.
    """
    if result.get("status") == "unavailable":
        reason = result.get("reason")
        if reason == "cleanup_failed" or (op == "release" and reason == "operation_failed"):
            LOGGER.warning("Desktop %s left applications running: %s", op, result)
            return unavailable("desktop_apps_open")
        if reason in REQUEST_REJECTIONS:
            return result
        LOGGER.warning("Desktop %s rejected by the helper: %s", op, result)
        if reason in {"not_ready", "not_configured"}:
            return unavailable("desktop_not_ready")
        return unavailable("desktop_failed")
    if op == "status" and result.get("ready") is False:
        LOGGER.warning("Station helper not ready: %s", result.get("missing"))
        return {**result, "message": ROLE_MESSAGES["desktop_not_ready"]}
    return result
