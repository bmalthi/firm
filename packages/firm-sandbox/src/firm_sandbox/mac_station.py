"""One account and desktop in the firm's Mac, leased to one activation at a time.

Each station keeps independent host records and a host lock while sharing one Mac VM,
guest transport and sshd with the firm's other stations. Enrolling those accounts is
``mac_enrollment``'s job.

Roles never receive the control account's SSH key or helper authority. The runtime
authenticates their role token and holds the lease and the helper relay on their
behalf. Each account keeps its own desktop and applications while machine resources,
loopback ports and Homebrew are shared.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .guest_control import lifecycle, private_directory, read_record, save
from .mac_vm import PYTHON
from .models import firm_identifier, identifier, role_address

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
# What a helper request may take. The helper bounds the input a request intends to post
# well inside it, leaving room for activation and the reply.
REQUEST_SECONDS = 30
STATUS_SECONDS = 5
# Reaching the helper: ssh into the guest (a channel on the persistent relay master, or
# a new connection when there is none), the relay's socket connect and its peer check
# (10 s each in the relay). Seen live on 2026-09-21: new connections through Lima's
# forward hung for about twenty seconds with the helper alive, and a release recorded
# that as an unusable desktop. Nothing has been asked of the helper before the relay
# says it is connected, so a connection that fails before then is simply made again.
RELAY_CONNECT_SECONDS = 30
RELAY_CONNECT_ATTEMPTS = 3
RELAY_CONNECT_RETRY_SECONDS = 3
SAID_CHARACTERS = 300  # of a failed simulator step's output, in the release's record
# sandbox.py's Sandbox.resources uses the same value for its own sample; mac_station.py
# sits below it and cannot import it without a cycle, so this stays a small copy.
DISK_SAMPLE_TIMEOUT = 10
# A host timeout only kills the host's SSH client; the guest bounds the work itself and
# reaps its own workers first, so the host must wait longer than that bound or a reply
# that arrives late is wrongly treated as lost. Sweep: 75 s simulator commands +
# 360 s retry deadline + two 10 s guest locks = 455 s; 480 s also allows 25 s transport.
# Verify: four 45 s part reads + 45 s session listing + 5 s mount read + 20 s locks
# = 250 s; 285 s leaves room for the documented 15–25 s transport stalls.
# Reset: 45 s sessions + 20 s unmount/check + 80 s login deadline + 20 s locks
# = 165 s; 210 s allows transport and reporting. Reclaim: 60 s worker + 20 s locks
# = 80 s; 110 s includes the same transport allowance.
SWEEP_RPC_TIMEOUT = 480
VERIFY_RPC_TIMEOUT = 285
RECLAIM_RPC_TIMEOUT = 110
SESSION_ENSURE_RPC_TIMEOUT = 90
SESSION_RESET_RPC_TIMEOUT = 210
MOUNT_RPC_TIMEOUT = 165
# Refuse a lease on a clean station below this much free: enough that macOS and one
# build do not fail on a full disk, not a margin for everything a role might do.
DISK_FLOOR_BYTES = 5 * 1024**3
ASSETS = Path(__file__).with_name("assets")
DIRTY_MARKER = "mac-station-dirty"
DIRTY_MARKER_CONTENT = b"Mac lease requires verified release\n"
LEASE_SSH_HOST = "127.0.0.1"
_STATION_LIMIT = 8
LEASE_ID = re.compile(r"[0-9a-f]{32}")
_HELD_STATION_LOCKS = ContextVar("held_mac_station_locks", default=frozenset())
OPERATIONS = frozenset(
    {
        "status",
        "bind_lease",
        "list_apps",
        "list_windows",
        "inspect_ax",
        "launch_app",
        "action",
        "focus",
        "type_text",
        "press_key",
        "capture_window",
        "release",
        "click",
        "drag",
        "scroll",
        "tap_sequence",
    }
)
RECOVERY_OPERATIONS = frozenset({"status", "list_apps", "release"})


class StationBusy(OSError):
    """A lease or an owner operation holds the station lock."""


class StationNeedsRecovery(RuntimeError):
    """An earlier lease left its record or dirty marker; release or recovery must verify it."""


class StationNotEnrolled(RuntimeError):
    """The Mac has no station account yet; enrollment must create it before a lease."""


class StationSessionNotReady(RuntimeError):
    """A station account exists, but its desktop session is not ready for a lease."""

    def __init__(self, result):
        station = result.get("station", "unknown")
        failure = result.get("failure", "session_not_ready")
        super().__init__(f"Mac station {station} session is not ready: {failure}")
        self.result = result


class StationDiskFull(RuntimeError):
    """A clean station's free space, after reclaiming, is still below the admission floor."""

    def __init__(self, message, *, station, disk, disk_reclaimed, reclaim=None):
        super().__init__(message)
        self.station = station
        self.disk = disk
        self.disk_reclaimed = disk_reclaimed
        self.reclaim = reclaim

    def evidence(self):
        """What the sandbox and the runtime record of this refusal."""
        return {
            "station": self.station, "disk": self.disk,
            "disk_reclaimed": self.disk_reclaimed, "reclaim": self.reclaim,
        }


class HelperNotReady(OSError):
    """The helper answered, but its desktop session cannot serve a lease."""


@dataclass(frozen=True)
class Lease:
    lease_id: str
    station: int
    ssh_host: str
    ssh_port: int
    user: str
    drive: str
    project_dir: str
    scratch: str


def lease_identifier(value):
    if not isinstance(value, str) or LEASE_ID.fullmatch(value) is None:
        raise ValueError("Invalid lease identifier")
    return value


def station_number(value):
    """Validate a station number accepted by the guest's eight-account protocol."""
    if type(value) is not int or not 1 <= value <= _STATION_LIMIT:
        raise ValueError(f"Station numbers run from 1 to {_STATION_LIMIT}")
    return value


def awake_settled(awake):
    """Whether a station's awake report says its desktop will neither lock nor sleep.

    The same reading enrollment requires of a station before its turn ends.
    """
    return (
        isinstance(awake, dict)
        and awake.get("screen_lock") == "screenLock is off"
        and awake.get("screen_saver_idle") == 0
        and awake.get("relock_at_login") is not True
        and all(awake.get("power", {}).get(key) == 0
                for key in ("displaysleep", "sleep", "disksleep"))
    )


def lease_ssh_port(station):
    """The Linux VM loopback port a station's lease tunnel listens on: 2222, then 2223.

    The holder reaches the shared guest sshd through the sandbox's per-lease tunnel
    from there; one Linux-side port per station keeps simultaneous leases apart.
    """
    return 2221 + station_number(station)


def generate_key_pair():
    """A fresh ed25519 pair; the private half exists only in an owner-only scratch."""
    keygen = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"
    with tempfile.TemporaryDirectory(prefix="firm-mac-key-") as directory:
        os.chmod(directory, 0o700)
        path = Path(directory) / "key"
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "firm-mac-lease", "-f", str(path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return path.read_bytes(), path.with_suffix(".pub").read_text().strip()


def station_lock(locks, station):
    """Take one station's lock in ``locks``, the firm's lock directory, without waiting.

    Closing the returned descriptor releases it. A lease holds it from acquire to
    verified release. Enrollment, sealing, recovery, helper status and replacement
    hold it too, so none meets a lease.
    """
    private_directory(locks)
    descriptor = os.open(
        Path(locks) / f"mac-station-{station_number(station)}",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe Mac station lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StationBusy(f"Mac station {station} is already leased") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def station_locks(locks, stations, *, skip_busy=False):
    """Hold station locks in number order, reusing locks held by this call context.

    Whole-machine host operations use this around all of a VM's stations. Public
    per-station methods can then take their ordinary guard inside that operation without
    trying to lock the same file twice. Descriptors acquired here remain private to this
    context and are closed in reverse order.

    The block receives the stations that were passed over, which is empty unless
    ``skip_busy`` allows an operation that writes nothing outside an account to leave
    another holder's station alone. A passed-over station is not recorded as held, so
    anything inside that touches it takes its ordinary guard and refuses there.
    """
    path = Path(locks).absolute()
    numbers = tuple(sorted({station_number(station) for station in stations}))
    held = _HELD_STATION_LOCKS.get()
    acquired, busy = [], []
    try:
        for station in numbers:
            if (str(path), station) in held:
                continue
            try:
                acquired.append(station_lock(path, station))
            except StationBusy:
                if not skip_busy:
                    raise
                busy.append(station)
        keys = {(str(path), station) for station in numbers if station not in busy}
        token = _HELD_STATION_LOCKS.set(held | keys)
        try:
            yield tuple(busy)
        finally:
            _HELD_STATION_LOCKS.reset(token)
    finally:
        for descriptor in reversed(acquired):
            os.close(descriptor)


def _verified_dirty_marker(path):
    """Validate the host marker before an owner recovery may remove it."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o600
        or info.st_size != len(DIRTY_MARKER_CONTENT)
        or path.read_bytes() != DIRTY_MARKER_CONTENT
    ):
        raise ValueError("Unsafe Mac dirty marker")
    return True


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, DIRTY_MARKER_CONTENT)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(path.parent)


def _remove(path):
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _disk_reading(vm):
    """The Mac's data-volume free/total bytes via ``sample``; evidence only.

    A failed sample, one missing a field, or one shaped as something other than a
    reply mapping, all yield None values. This never raises, never waits past its
    own short timeout, and must never dirty the acquire or release it rides along
    with.
    """
    try:
        sample = vm._rpc("sample", timeout=DISK_SAMPLE_TIMEOUT)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        sample = {}
    if not isinstance(sample, dict):
        sample = {}
    used, total = sample.get("disk_used_bytes"), sample.get("disk_total_bytes")
    return {
        "free_bytes": None if used is None or total is None else total - used,
        "total_bytes": total,
    }


def _reclaimed_bytes(reclaim):
    """Bytes a `station-reclaim` reply actually removed, summed across its categories.

    ``None`` when the reclaim never ran or answered with something else. A category's
    own bytes are its logical count, not guest or host space regained (see the guest's
    reclaim contract), and a refused reclaim's categories are all zero, not unknown.
    """
    if not isinstance(reclaim, dict) or not isinstance(reclaim.get("categories"), dict):
        return None
    return sum(category.get("bytes", 0) for category in reclaim["categories"].values())


def _reclaim_evidence(reclaim):
    """What a `station-reclaim` reply removed and what it left, for the owner to read.

    Reclaiming less than hoped never dirties a station, so this is where a timed-out
    worker or a skipped entry stays visible. Records, not a mapping by category, so the
    runtime's journal can carry it; the guest's static lists of what it keeps are left out.
    """
    if not isinstance(reclaim, dict) or not isinstance(reclaim.get("categories"), dict):
        return None
    return {
        "completed": reclaim.get("completed") is True,
        "timed_out": reclaim.get("timed_out") is True,
        "refused": reclaim.get("reason") if reclaim.get("refused") else None,
        "categories": [
            {
                "category": name,
                "count": counts.get("count", 0),
                "bytes": counts.get("bytes", 0),
                "skipped": sum(skip.get("count", 0) for skip in counts.get("skipped", {}).values()),
                "skipped_bytes": sum(
                    skip.get("bytes", 0) for skip in counts.get("skipped", {}).values()
                ),
                "skipped_reasons": sorted(counts.get("skipped", {})),
            }
            for name, counts in reclaim["categories"].items()
        ],
    }


def _simulator_evidence(sweep):
    """The sweep's timed simulator steps as records: which completed, which timed out.

    A step that did not complete also carries its exit status and the tail of what it
    said, because simctl says why it failed there and nowhere else. Both are None for a
    step killed at its timeout. A completed step carries neither. What it said is kept
    to its last SAID_CHARACTERS as ASCII, because the release is recorded as one journal
    event with a fixed size cap and escaped text would otherwise lose the whole event.
    """
    if not isinstance(sweep, dict) or not isinstance(sweep.get("simulators"), dict):
        return None
    records = []
    for step, outcome in sweep["simulators"].items():
        record = {
            "step": step, "completed": outcome.get("completed"),
            "timed_out": outcome.get("timed_out"),
        }
        if outcome.get("completed") is not True:
            said = outcome.get("said")
            if isinstance(said, str):
                said = said[-SAID_CHARACTERS:].encode("ascii", "replace").decode()
            record.update(returncode=outcome.get("returncode"), said=said)
        records.append(record)
    return records


def _drive_mounted(unmount, reclaim):
    """Whether the drive is still mounted, by the newest evidence there is.

    Reclaim runs after the unmount and refuses a mounted drive, so its refusal outranks
    an unmount that answered clean before the drive came back.
    """
    if isinstance(reclaim, dict) and reclaim.get("refused") and (
        reclaim.get("reason") == "drive_mounted"
    ):
        return True
    return None if unmount is None else unmount["mounted"]


class MacStation:
    """The lease over one station account; a file lock excludes another host process.

    The station lock is taken at the start of ``acquire`` and held through use and
    verified ``release``. Relay connections open and close under it. A lease record
    under ``<root>/leases`` and the host dirty marker outlive a runtime crash and
    block the next acquire until release or recovery verifies the four checks.

    ``revoke(lease_id)`` is the sandbox's part of access revocation: it closes the
    lease's tunnel and returns what it verified, folded into ``access_revoked``.
    """

    def __init__(self, vm, station, *, revoke, relay_seconds=1800):
        if type(relay_seconds) is not int or not 1 <= relay_seconds <= 3600:
            raise ValueError("Station relay connections must last between 1 and 3600 seconds")
        self.vm = vm
        self.station = station_number(station)
        self.revoke = revoke
        self.root = Path(vm.root).absolute() / "stations" / str(self.station)
        self.firm_locks = vm.firm_locks
        self.firm_id = firm_identifier(vm.settings.firm_id)
        self.relay_seconds = relay_seconds
        self._held = None

    @property
    def user(self):
        """The guest account represented by this station."""
        return f"firmstation{self.station}"

    @property
    def drive(self):
        """This station account's drive mount point."""
        return f"/Users/{self.user}/drive"

    def _station_rpc(self, operation, **payload):
        """Call one guest station operation with this station selected explicitly."""
        if "station" in payload:
            raise ValueError("The station is selected by MacStation")
        return self.vm._rpc(operation, station=self.station, **payload)

    # ---- records -------------------------------------------------------------

    def _record_path(self, lease_id):
        return self.root / "leases" / f"{lease_identifier(lease_id)}.json"

    def _records(self):
        directory = self.root / "leases"
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.iterdir() if path.suffix == ".json")

    def _binding(self, record):
        """The firm, role and lease the helper is bound to.

        The helper keeps that binding across relay connections, so a reopened connection
        presenting the same binding adopts the applications the lease left open.
        """
        return {
            "firm_id": self.firm_id,
            "role_id": record["role_id"],
            "lease_id": record["lease_id"],
        }

    def holds(self, lease_id):
        """Whether this station's records name the lease; how a fresh Sandbox routes it."""
        return self._record_path(lease_id).is_file()

    def enrolled(self):
        """Whether the Mac image records a sealed station enrollment."""
        return self.station in self.vm.stations and self.vm.enrolled()

    def status(self):
        held = self._held
        records = self._records()
        if held is not None:
            record = held["record"]
        else:
            record = read_record(records[-1]) if records else None
        return {
            "configured": True,
            "station": self.station,
            "enrolled": self.enrolled(),
            "held": held is not None,
            "lease_id": record["lease_id"] if record else None,
            "holder": record["role_id"] if record else None,
            "since": record["acquired_at"] if record else None,
            "dirty": held is None
            and (bool(records) or os.path.lexists(self.root / DIRTY_MARKER)),
        }

    def discard(self):
        """Forget the dirty marker and lease records; the guest they describe is gone.

        Only ``Sandbox.replace`` and ``Sandbox.mac_replace`` call this, holding the
        station lock, after the Mac VM was removed.
        """
        (self.root / DIRTY_MARKER).unlink(missing_ok=True)
        for record in self._records():
            record.unlink()

    # ---- lease ----------------------------------------------------------------

    @lifecycle()
    def acquire(self, lease_id, role_id, drive, public_key, *, address, activation_id=None):
        """Reclaim, baseline and measure, dirty marker, drive mount, key install.

        A clean station reclaims and measures its free space before anything else is
        touched, so a refusal below the admission floor leaves no marker or record: it
        granted nothing, so nothing needs recovery. Returns the ``Lease``, a best-effort
        disk reading and what the reclaim removed, never a ``Lease`` field so neither
        can leak into what a role's endpoint carries by accident.
        """
        lease_identifier(lease_id)
        identifier(role_id)
        role_address(address)
        if (
            not isinstance(public_key, str)
            or not public_key.startswith("ssh-ed25519 ")
            or "\n" in public_key
            or len(public_key) > 1024
        ):
            raise ValueError("Expected one ed25519 public key line")
        if self._held is not None:
            raise StationBusy(f"Mac station {self.station} is already leased")
        descriptor = station_lock(self.firm_locks, self.station)
        try:
            if os.path.lexists(self.root / DIRTY_MARKER) or self._records():
                raise StationNeedsRecovery(
                    "The Mac station needs a verified release or owner recovery before "
                    "a new lease"
                )
            if self._station_rpc("station-account")["present"] is not True:
                raise StationNotEnrolled(
                    "The Mac has no station account; run mac-prepare in an enrollment firm "
                    "or name an enrolled image"
                )
            session = self._prepare_session()
            if session.get("ready") is not True:
                raise StationSessionNotReady(session)
            try:
                reclaim = self._station_rpc("station-reclaim", timeout=RECLAIM_RPC_TIMEOUT)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                # Best effort: a station that cannot reclaim may still measure enough
                # free space to admit the lease, so its own reading still decides.
                reclaim = None
            # A lost reply leaves nothing unverified here: the station was clean, no lease
            # exists, and the guest bounds the reclaim and reaps it under its one lock.
            # The baseline takes that lock, so it returns only once the reclaim is over,
            # and the free space read after it is never a reading mid-reclaim.
            baseline = self._station_rpc("station-baseline")
            disk = _disk_reading(self.vm)
            disk_reclaimed = _reclaimed_bytes(reclaim)
            if disk["free_bytes"] is not None and disk["free_bytes"] < DISK_FLOOR_BYTES:
                raise StationDiskFull(
                    f"Mac station {self.station} has {disk['free_bytes']} bytes free after "
                    f"reclaiming, below the {DISK_FLOOR_BYTES} byte admission floor",
                    station=self.station, disk=disk, disk_reclaimed=disk_reclaimed,
                    reclaim=_reclaim_evidence(reclaim),
                )
            private_directory(self.root / "leases")
            record = {
                "lease_id": lease_id,
                "role_id": role_id,
                "activation_id": activation_id,
                "address": address,
                "acquired_at": time.time(),
                "baseline": baseline,
                "steps": [],
                "disk": {"acquired": disk},
                "disk_reclaimed": disk_reclaimed,
                "reclaim": _reclaim_evidence(reclaim),
            }
            path = self._record_path(lease_id)
            save(path, record)
            _write_marker(self.root / DIRTY_MARKER)
            mount = self._station_rpc(
                "station-mount", drive=drive.request(), timeout=MOUNT_RPC_TIMEOUT
            )
            if mount.get("point") != self.drive:
                raise ValueError("Guest mounted the station drive at an unexpected path")
            record["steps"].append("drive_mounted")
            save(path, record)
            self._station_rpc("station-key-install", public_key=public_key)
            record["steps"].append("key_installed")
            save(path, record)
        except BaseException:
            # The record and marker stay; they block the next acquire. A refusal
            # before either exists, such as StationDiskFull, leaves nothing to block.
            os.close(descriptor)
            raise
        self._held = {"lease_id": lease_id, "record": record, "lock": descriptor}
        return Lease(
            lease_id,
            self.station,
            LEASE_SSH_HOST,
            lease_ssh_port(self.station),
            self.user,
            self.drive,
            f"{self.drive}/org/{address}/work",
            mount["scratch"],
        ), disk, disk_reclaimed

    @lifecycle()
    def release(self, lease_id):
        """Verified cleanup of exactly this lease; an unknown ID reports the lease gone."""
        path = self._record_path(lease_id)
        held = self._held if self._held and self._held["lease_id"] == lease_id else None
        if held is None:
            if not path.is_file():
                return {"status": "ok", "lease_id": lease_id, "gone": True}
            descriptor = station_lock(self.firm_locks, self.station)
        else:
            descriptor = held["lock"]
        try:
            record = read_record(path)
            result = self._cleanup(record)
            if result["clean"]:
                _remove(self.root / DIRTY_MARKER)
                _remove(path)
            else:
                record["cleanup"] = result
                save(path, record)
            return result
        finally:
            if held is not None:
                self._held = None
            os.close(descriptor)

    def _cleanup(self, record, *, revoked=None):
        errors = []

        def step(name, action):
            try:
                return action()
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                errors.append(f"{name}: {exc}")
                return None

        rpc = self._station_rpc
        if revoked is None:
            revoked = step("tunnel", lambda: self.revoke(record["lease_id"]))
        key = step("key", lambda: rpc("station-key-remove"))
        sessions = step("sessions", lambda: rpc("station-kill-sessions"))
        # The helper serves one connection at a time, so this waits for the holder's
        # connection to end, then closes the applications its lease still tracks.
        step("helper", lambda: self._helper(record, "release"))
        # This also bounds the retry after the final assessment; transport and the
        # intervening checks consume the allowance rather than starting another budget.
        # One retry: work that appears while the drive and desktop are checked is worth
        # a second sweep, and a station that keeps producing it is the owner's to see.
        deadline = time.monotonic() + 75 + 360
        retry, sweeps = {}, []
        sweep_acknowledged = True
        while True:
            sweep = step(
                "sweep", lambda retry=retry: rpc("station-sweep", baseline=record["baseline"],
                                      timeout=SWEEP_RPC_TIMEOUT, **retry)
            )
            sweep_acknowledged &= sweep is not None and not sweep.get("cleanup_incomplete")
            if sweep is not None and not sweeps:
                # Only unused guest cleanup time can fund a retry; fast simulator
                # commands do not turn their unused allowance into extra sweep time.
                deadline = min(deadline, time.monotonic() + sweep.get("remaining_seconds", 0))
            if sweep is None:
                sweep = {"ok": False, **{
                    name: {"ok": False, "verified": False, "error": errors[-1]}
                    for name in ("processes", "services", "launch_agents", "crontab")
                }}
            else:
                errors.extend(f"sweep.{name}: {sweep[name][key]}"
                              for name in ("processes", "services", "launch_agents", "crontab")
                              for key in ("error", "cleanup_error") if key in sweep[name])
                if sweep["services"].get("unparsed"):
                    errors.append(
                        f"sweep.services: unparsed launchd rows: {sweep['services']['unparsed']}",
                    )
            drive = step("drive", lambda: rpc("station-unmount"))
            # After the unmount, which the guest's own reclaim also requires: it refuses
            # while the drive is still mounted rather than touch it.
            reclaim = step("reclaim", lambda: rpc("station-reclaim", timeout=RECLAIM_RPC_TIMEOUT))
            desktop = step("desktop", lambda: self._preflight(record))
            # The same current-state assessment finishes ordinary release and reset.
            # Earlier observations remain in sweep/errors; only missing mutation completion
            # acknowledgements survive a later clean read as a cleanliness prerequisite.
            verify = self._verify(record)
            diagnostics = [verify[name]["error"] for name in
                           ("services", "launch_agents", "crontab", "processes")
                           if "error" in verify[name]]
            diagnostics.extend(verify.get("status_errors", {}).values())
            if diagnostics:
                errors.append("verify: " + "; ".join(dict.fromkeys(diagnostics)))
            sweeps.append(sweep)
            remaining = deadline - time.monotonic()
            if (verify["ok"] or len(sweeps) > 1 or not sweep["ok"] or not sweep_acknowledged
                    or remaining <= 0 or reclaim is None or drive is None
                    or not all(verify[name].get("verified") for name in
                               ("services", "launch_agents", "crontab", "processes"))):
                break
            # Work appearing during reclaim or desktop checks returns to the same sweep,
            # spending only the time left from its original allowance.
            retry = {"seconds": remaining, "simulators": False}
        log = step("log", lambda: rpc("station-command-log"))
        if log is not None and log.get("data"):
            self._append_log(record["lease_id"], base64.b64decode(log["data"], validate=True))
        checks = self._checks(
            key_present=verify["key_present"],
            sessions=verify["sessions"],
            sweep=verify,
            mounted=verify["drive_mounted"],
            desktop=desktop,
            revoked=revoked,
        )
        # What cleanup met on the way is evidence the durable record keeps, beside the
        # final state that decides: the checks are what the runtime stores.
        for name in ("processes", "services", "launch_agents", "crontab"):
            for key_name in ("kill_errors", "remove_errors", "diagnostics"):
                seen = [sweep[name][key_name] for sweep in sweeps
                        if isinstance(sweep.get(name), dict) and key_name in sweep[name]]
                if seen:
                    checks["work_stopped"][name][key_name] = seen[-1]
        if key is None or sessions is None or sessions.get("cleanup_incomplete"):
            checks["access_revoked"].update(ok=False, cleanup_incomplete=True)
        if drive is None:
            checks["drive_detached"].update(ok=False, cleanup_incomplete=True)
        if not sweep_acknowledged:
            checks["work_stopped"].update(ok=False, cleanup_incomplete=True)
        return {
            "status": "ok",
            "lease_id": record["lease_id"],
            "sweeps": sweeps,
            "verify": verify,
            # Reclaiming less than hoped is reported below and never dirties; only a
            # reclaim the host could not confirm at all (a lost reply, an unreaped
            # worker) leaves the station dirty, like any other unverified step.
            "clean": (all(check["ok"] for check in checks.values())
                      and reclaim is not None and sessions is not None),
            "checks": checks,
            "errors": errors,
            # Read after every check, clean or not: evidence, never a reason to fail.
            "disk": _disk_reading(self.vm),
            "disk_reclaimed": _reclaimed_bytes(reclaim),
            "reclaim": _reclaim_evidence(reclaim),
            "simulators": _simulator_evidence(sweeps[0]),
        }

    @staticmethod
    def _checks(*, key_present, sessions, sweep, mounted, desktop, revoked):
        work = {
            key: sweep[key] for key in ("processes", "services", "launch_agents", "crontab")
        } if sweep is not None else {}
        status_errors = (sweep or {}).get("status_errors", {})
        return {
            "access_revoked": {
                "ok": key_present is False
                and sessions == []
                and revoked is not None
                and all(value is True for value in revoked.values()),
                "key_present": key_present,
                "sessions": sessions,
                **(revoked or {}),
                **({"errors": status_errors} if status_errors else {}),
            },
            "work_stopped": {"ok": sweep is not None and sweep["ok"] is True, **work},
            "drive_detached": {
                "ok": mounted is False, "mounted": mounted,
                **({"error": status_errors["drive_mounted"]}
                   if "drive_mounted" in status_errors else {}),
            },
            "desktop_usable": {
                "ok": desktop is not None
                and desktop.get("ready") is True
                and desktop.get("clean") is True
                and desktop.get("environment") == "vm-session"
                and desktop.get("apps") == [],
                **(desktop or {}),
            },
        }

    def _verify(self, record):
        try:
            return self._station_rpc(
                "station-verify", baseline=record["baseline"], timeout=VERIFY_RPC_TIMEOUT,
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = str(exc)
            return {
                "ok": False, "key_present": None, "sessions": None, "drive_mounted": None,
                "status_errors": {key: error for key in
                                  ("key_present", "sessions", "drive_mounted")},
                **{name: {"ok": False, "verified": False, "error": error} for name in
                   ("processes", "services", "launch_agents", "crontab")},
            }

    def _append_log(self, lease_id, data):
        private_directory(self.root / "leases")
        path = self.root / "leases" / f"{lease_id}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _helper(self, record, op):
        connection = self._relay(self._binding(record), recovery_only=True)
        try:
            return connection.request(op)
        finally:
            connection.close()

    def _preflight(self, record):
        """Fresh helper status plus its tracked-app ledger, after a helper release.

        The release closes the applications the helper's bound lease still
        tracks, and verifies that what only its ledger names — an earlier helper
        process's work — is gone, clearing it if so.
        """
        connection = self._relay(self._binding(record), recovery_only=True)
        try:
            released = connection.request("release")
            state = connection.request("status")
            apps = connection.request("list_apps").get("apps")
        finally:
            connection.close()
        result = {
            "ready": state.get("ready"),
            "clean": state.get("clean"),
            "environment": state.get("environment"),
            "missing": state.get("missing", []),
            "apps": apps,
        }
        if released["status"] != "ok":
            result["release"] = released.get("message")
        return result

    @lifecycle()
    def recover(self, *, confirm_cleanup=False):
        """Clear a stale marker and record only after the four checks hold and reclaim ran.

        This operation is for the owner (``firm-sandbox mac-recover``). It takes the station
        lock, closes a leftover lease tunnel, verifies against the baseline of the
        last lease record, lets the helper verify and clear its own ledger, terminates
        nothing on the station, reclaims once the four checks hold (never before: a
        station not yet verified stopped is not touched), and clears the marker only
        when every check holds and the reclaim itself was confirmed.
        """
        if confirm_cleanup is not True:
            raise ValueError(
                "Mac recovery requires the owner to confirm station cleanup and inspection"
            )
        if not self.vm.network.ready() or not self.vm._running():
            raise RuntimeError("Start the isolated VM before recovering its Mac station")
        marker = self.root / DIRTY_MARKER
        records = self._records()
        if not _verified_dirty_marker(marker) and not records:
            return {"status": "ok", "clean": True, "recovered": False}
        descriptor = station_lock(self.firm_locks, self.station)
        try:
            if not records:
                return {
                    "status": "ok",
                    "clean": False,
                    "recovered": False,
                    "message": "No lease record to verify against; replace the firm",
                }
            record = read_record(records[-1])
            verify = self._verify(record)
            try:
                desktop = self._preflight(record)
            except (OSError, ValueError, RuntimeError) as exc:
                desktop = {"ready": False, "clean": False, "apps": None, "error": str(exc)}
            checks = self._checks(
                key_present=verify["key_present"],
                sessions=verify["sessions"],
                sweep=verify,
                mounted=verify["drive_mounted"],
                desktop=desktop,
                revoked=self.revoke(record["lease_id"]),
            )
            verified = all(check["ok"] for check in checks.values())
            reclaim = None
            if verified:
                try:
                    reclaim = self._station_rpc(
                        "station-reclaim", timeout=RECLAIM_RPC_TIMEOUT
                    )
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    reclaim = None
                # The reclaim's refusal of a mounted drive is newer than the verify above.
                mounted = _drive_mounted({"mounted": verify["drive_mounted"]}, reclaim)
                checks["drive_detached"] = {"ok": mounted is False, "mounted": mounted}
            clean = all(check["ok"] for check in checks.values()) and reclaim is not None
            if clean:
                _remove(marker)
                for path in records:
                    _remove(path)
            return {
                "status": "ok", "clean": clean, "recovered": clean, "checks": checks,
                "disk": _disk_reading(self.vm), "disk_reclaimed": _reclaimed_bytes(reclaim),
                "reclaim": _reclaim_evidence(reclaim),
            }
        finally:
            os.close(descriptor)

    # ---- helper relay -----------------------------------------------------------

    def _ensure_session(self):
        """Return guest readiness, converting transport failures into not-ready evidence."""
        try:
            return self._station_rpc(
                "station-session-ensure", timeout=SESSION_ENSURE_RPC_TIMEOUT
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {
                "station": self.station,
                "user": self.user,
                "uid": 501 + self.station,
                "ready": False,
                "created": False,
                "helper": None,
                "failure": "session_not_ready",
                "error": str(exc),
            }

    def _prepare_session(self):
        """Ensure the desktop and restate its awake settings as one readiness report."""
        result = self._ensure_session()
        if result.get("failure") == "login_blocked":
            # The account rejects its recorded password: the awake step would offer it
            # once more, and every boot would then count one failed login toward lockout.
            return {**result, "awake": None}
        try:
            awake = self._station_rpc("station-keep-awake")
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {
                **result,
                "ready": False,
                "failure": result.get("failure", "session_not_ready"),
                "awake": None,
                "error": str(exc),
            }
        # What the guest read back governs, not what the setter said: a desktop that
        # would still lock or sleep takes no lease.
        if not awake_settled(awake):
            return {**result, "ready": False, "failure": "session_not_ready", "awake": awake}
        return {**result, "awake": awake}

    @lifecycle()
    def ensure_session(self):
        """Ensure this desktop and awake settings, reporting failures as not ready."""
        with station_locks(self.firm_locks, (self.station,)):
            return self._prepare_session()

    @lifecycle()
    def session_reset(self):
        """Reset this desktop and attest cleanup of any retained lease baseline."""
        with station_locks(self.firm_locks, (self.station,)):
            marker = self.root / DIRTY_MARKER
            records = self._records()
            dirty = _verified_dirty_marker(marker) or bool(records)
            if dirty and not records:
                return {
                    "station": self.station,
                    "user": self.user,
                    "uid": 501 + self.station,
                    "ready": False,
                    "created": False,
                    "helper": None,
                    "reset": False,
                    "failure": "cleanup_baseline_missing",
                    "cleanup": {"clean": False, "errors": ["lease baseline is missing"]},
                }
            record = read_record(records[-1]) if records else None
            revoked = self.revoke(record["lease_id"]) if record is not None else None
            try:
                self._station_rpc("station-key-remove")
                reset = self._station_rpc(
                    "station-session-reset", timeout=SESSION_RESET_RPC_TIMEOUT
                )
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                reset = {"station": self.station, "ready": False, "reset": False,
                         "checks": {"session_reset": {"ok": False, "error": str(exc)}}}
            if record is None:
                return reset
            cleanup = self._cleanup(record, revoked=revoked)
            clean = cleanup["clean"] and reset.get("reset") is True
            cleanup = {**cleanup, "clean": clean}
            if clean:
                _remove(marker)
                for path in records:
                    _remove(path)
            else:
                saved = {**record, "cleanup": cleanup}
                save(records[-1], saved)
            return {**reset, "cleanup": cleanup}

    def connect(self, lease_id):
        """A bound helper relay for the held lease; the lease and lock are untouched.

        The binding names the lease, so a connection opened after an earlier one's
        deadline adopts the existing binding and its tracked applications.
        """
        if self._held is None or self._held["lease_id"] != lease_id:
            raise OSError("The Mac lease is not held")
        binding = self._binding(self._held["record"])
        connection = self._relay(binding)
        try:
            state = connection.request("status")
            if state.get("ready") is not True or state.get("environment") != "vm-session":
                raise HelperNotReady(
                    "The station helper is not ready: " + ", ".join(state.get("missing", []))
                )
            bound = connection.bind_lease()
            if bound.get("lease_bound") is not True:
                raise HelperNotReady(
                    "The station helper could not bind the lease: "
                    + bound.get("message", "the helper refused the lease binding")
                )
        except BaseException:
            connection.close()
            raise
        return connection

    def probe(self):
        """A status-only relay for owner checks; the caller holds the station lock."""
        return self._relay(
            self._binding({"role_id": "owner-probe", "lease_id": "owner-probe"}),
            recovery_only=True,
        )

    @lifecycle()
    def helper_status(self):
        """Fresh helper readiness without a lease, for enrollment and diagnostics."""
        with station_locks(self.firm_locks, (self.station,)):
            connection = self.probe()
            try:
                return connection.request("status")
            finally:
                connection.close()

    @lifecycle()
    def awake_status(self):
        """The station's lock, sleep and screen-saver settings; refused during a lease."""
        with station_locks(self.firm_locks, (self.station,)):
            return self._station_rpc("station-awake-status")

    def _relay(self, binding, *, recovery_only=False):
        # ssh.config is in the Mac's runtime: whichever VM that holds gets the relay.
        self.vm.paths.verify_runtime()
        argv = [
            "/usr/bin/ssh",
            "-F",
            str(self.vm.ssh_config),
            "-T",
            "-oBatchMode=yes",
            # Every relay is a channel on one persistent connection into the guest, as
            # the firm's tunnels are: a fresh connection through Lima's forward of the
            # guest's ssh port has hung for twenty seconds at a time after bursts of
            # short connections, with the guest's sshd never seeing it. The master lives
            # in the Mac's runtime, owner-only; a relay's deadline ends its channel.
            "-oControlMaster=auto",
            f"-oControlPath={self.vm.socket_root / 'relay.sock'}",
            "-oControlPersist=yes",
            "-oConnectTimeout=20",
            "-oServerAliveInterval=5",
            "-oServerAliveCountMax=6",
            "lima-mac",
            shlex.join([
                PYTHON, "-I", "-c",
                (ASSETS / "mac-station-relay.py").read_text(),
                str(self.station),
            ]),
        ]
        for attempt in range(1, RELAY_CONNECT_ATTEMPTS + 1):
            connection = StationConnection(
                argv, binding, self.relay_seconds, recovery_only=recovery_only
            )
            try:
                connection.wait_connected()
                return connection
            except RelayNotConnected:
                if attempt == RELAY_CONNECT_ATTEMPTS:
                    raise
                time.sleep(RELAY_CONNECT_RETRY_SECONDS)


class RelayNotConnected(OSError):
    """The relay ended before reaching the helper; nothing was asked of the helper."""


class StationConnection:
    """Bounded JSON RPC over authenticated SSH, with a non-renewable connection deadline.

    The deadline bounds the connection. The helper keeps the lease state identified by
    ``binding`` until a release, so the next connection adopts it.
    """

    def __init__(self, argv, binding, relay_seconds, *, recovery_only=False,
                 popen=subprocess.Popen):
        self.recovery_only = recovery_only
        self.binding = dict(binding)
        self.relay_seconds = relay_seconds
        self.deadline = time.monotonic() + relay_seconds
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.closed = False
        # What ssh and the relay say about a failure, read only when one is reported.
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
                bufsize=0,
            )
        except BaseException:
            self.errors.close()
            raise
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.timer = threading.Timer(relay_seconds, self.close)
            self.timer.daemon = True
            self.timer.start()
        except BaseException:
            self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.errors.close()
            raise

    def wait_connected(self):
        """Read the relay's first line, which says it reached the helper.

        Until then a failure is the connection's, ssh or the relay's socket, and nothing
        has been asked of the helper; the caller may connect again.
        """
        deadline = time.monotonic() + RELAY_CONNECT_SECONDS
        with self.lock:
            try:
                while b"\n" not in self.buffer:
                    self._wait(deadline=deadline)
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise OSError("Station relay ended before connecting")
                    self.buffer.extend(chunk)
                    if len(self.buffer) > MAX_RESPONSE:
                        raise OSError("Station helper response exceeds limit")
                line, _, self.buffer = self.buffer.partition(b"\n")
                if json.loads(line) != {"relay": "connected"}:
                    raise ValueError("Station relay did not announce its connection")
            except (OSError, ValueError) as exc:
                said = self._error_output()
                self.close()
                raise RelayNotConnected(
                    f"Station helper unreachable: {exc}" + (f" ({said})" if said else "")
                ) from exc

    def _error_output(self):
        try:
            self.errors.seek(0, os.SEEK_END)
            size = self.errors.tell()
            self.errors.seek(max(0, size - 1024))
            return self.errors.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def usable(self):
        return (
            not self.closed
            and self.process.poll() is None
            and time.monotonic() < self.deadline
        )

    def _wait(self, *, write=False, deadline):
        remaining = min(deadline, self.deadline) - time.monotonic()
        if remaining <= 0:
            raise OSError("Helper request or relay connection deadline exceeded")
        stream = self.process.stdin if write else self.process.stdout
        readable, writable, _ = select.select(
            [] if write else [stream], [stream] if write else [], [], remaining
        )
        if not (writable if write else readable):
            raise OSError("Helper request or relay connection deadline exceeded")

    def bind_lease(self):
        return self.request("bind_lease", **self.binding)

    def request(self, op: str, **arguments):
        if op not in OPERATIONS or {"id", "op"} & arguments.keys():
            raise ValueError("Invalid helper operation")
        if self.recovery_only and op not in RECOVERY_OPERATIONS:
            raise ValueError("Recovery connection permits only status, list_apps or release")
        with self.lock:
            if self.closed:
                raise OSError("Relay connection ended")
            if op == "bind_lease":
                if arguments != self.binding:
                    raise ValueError("Helper lease binding does not match its lease")
                arguments = {**self.binding, "duration_seconds": self.relay_seconds}
            request_id = uuid4().hex
            encoded = (
                json.dumps({"id": request_id, "op": op, **arguments}, allow_nan=False).encode()
                + b"\n"
            )
            if len(encoded) > MAX_REQUEST:
                raise ValueError("Helper request exceeds 64 KiB")
            deadline = time.monotonic() + (
                STATUS_SECONDS if op == "status" else REQUEST_SECONDS
            )
            try:
                pending = memoryview(encoded)
                while pending:
                    self._wait(write=True, deadline=deadline)
                    count = os.write(self.process.stdin.fileno(), pending)
                    if not count:
                        raise OSError("Station helper disconnected")
                    pending = pending[count:]
                while b"\n" not in self.buffer:
                    self._wait(deadline=deadline)
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise OSError("Station helper disconnected")
                    self.buffer.extend(chunk)
                    if len(self.buffer) > MAX_RESPONSE:
                        raise OSError("Station helper response exceeds limit")
                line, _, self.buffer = self.buffer.partition(b"\n")
                response = json.loads(line)
                if (
                    not isinstance(response, dict)
                    or response.get("id") != request_id
                    or type(response.get("ok")) is not bool
                ):
                    raise ValueError("Helper response identity mismatch")
                value = response.get("result" if response["ok"] else "error")
                if not isinstance(value, dict):
                    raise ValueError("Invalid helper response")
                if response["ok"]:
                    return {**value, "status": "ok"}
                return {
                    "status": "unavailable",
                    "reason": value.get("code", "helper_error"),
                    "message": value.get("message", "Station helper rejected the operation"),
                }
            except (OSError, ValueError) as exc:
                said = self._error_output()
                self.close()
                raise OSError(
                    "Helper operation failed; outcome may be unknown: " + str(exc)
                    + (f" ({said})" if said else "")
                ) from exc

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.timer.cancel()
            # EOF makes the guest relay disconnect. The helper keeps the lease state
            # the binding names, so the applications it tracks stay open until a release.
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            finally:
                self.process.stdout.close()
                self.errors.close()
