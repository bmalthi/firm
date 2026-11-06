"""The Mac leases: one per station, released under verification, recorded as events."""

import json
import threading

import pytest
from firm_sandbox.mac_station import (
    HelperNotReady,
    StationBusy,
    StationDiskFull,
    StationNeedsRecovery,
    StationNotEnrolled,
    StationSessionNotReady,
)
from runtime_test_support import mail_to, rows

from firm_runtime.errors import FirmError
from firm_runtime.mac_lease import ROLE_MESSAGES, MacLease
from firm_runtime.prompt_resources import prompt, render
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store

LEASE = {
    "ssh_host": "127.0.0.1",
    "user": "firmstation1",
    "drive": "/Users/firmstation1/drive",
    "project_dir": "/Users/firmstation1/drive/org/founder/work",
    "scratch": "/Users/firmstation1/scratch",
}
CLEAN = {name: {"ok": True} for name in
         ("access_revoked", "work_stopped", "drive_detached", "desktop_usable")}
GIB = 1 << 30
# Distinct so a test can tell the acquire and release readings apart.
ACQUIRE_DISK = {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
RELEASE_DISK = {"free_bytes": 30 * GIB, "total_bytes": 100 * GIB}
ACQUIRE_RECLAIMED = 2 * GIB
RELEASE_RECLAIMED = 5 * GIB


def endpoint(lease_id, station):
    """What a granted lease returns: the station's own SSH port, key and disk evidence."""
    user = f"firmstation{station}"
    paths = {
        "user": user,
        "drive": f"/Users/{user}/drive",
        "project_dir": f"/Users/{user}/drive/org/founder/work",
        "scratch": f"/Users/{user}/scratch",
    }
    return {**LEASE, **paths, "lease_id": lease_id, "station": station, "ssh_port": 2221 + station,
            "key_path": f"/state/credentials/mac-{lease_id}",
            "disk": ACQUIRE_DISK, "disk_reclaimed": ACQUIRE_RECLAIMED}


class Helper:
    def __init__(self, lease_id, apps=None, adopted=False):
        self.lease_id = lease_id
        # The lease's applications: the helper keeps them across connections,
        # so a relay opened after this one's deadline adopts the same open applications.
        self.apps = [] if apps is None else apps
        self.adopted = adopted
        self.calls = []
        self.closed = False
        self.fail = False
        self.expired = False
        self.answers = {}
        self.entered = threading.Event()
        self.hold = None  # an event the next request waits on, for the concurrency test

    def usable(self):
        return not self.closed and not self.expired

    def request(self, op, **arguments):
        self.calls.append((op, arguments))
        self.entered.set()
        if self.hold is not None:
            self.hold.wait(timeout=5)
        if self.fail:
            raise OSError("Station helper disconnected")
        if op in self.answers:
            return self.answers[op]
        if op == "launch_app":
            self.apps.append(arguments["path"])
        if op == "release":
            self.apps.clear()
        if op == "status":
            return {
                "status": "ok",
                "ready": True,
                "lease_bound": True,
                "clean": True,
                "configured_control_uid": 501,
                "configured_station_uid": 502,
                "session": {
                    "uid": 502,
                    "gui_uid": 502,
                    "console_uid": 501,
                    "login_done": True,
                    "screen_locked": False,
                },
            }
        if op == "release":
            return {"status": "ok", "clean": True}
        return {"status": "ok", "pid": 123}

    def bind_lease(self):
        self.calls.append(("bind_lease", {}))
        return {"lease_bound": True, "status": "ok"}

    def close(self):
        self.closed = True


class Station:
    """One station: its lease record and its dirty marker."""

    def __init__(self, number):
        self.station = number
        self.record = None
        self.held = False
        self.dirty = False

    def status(self):
        record = self.record or {}
        return {
            "station": self.station,
            "configured": True,
            "enrolled": True,
            "held": self.held,
            "lease_id": record.get("lease_id"),
            "holder": record.get("role_id"),
            "since": record.get("since"),
            "dirty": not self.held and (self.dirty or self.record is not None),
        }


class Macs:
    """The sandbox facade: it picks the station, and routes release and connect itself."""

    def __init__(self, count):
        self.stations = [Station(number) for number in range(1, count + 1)]
        self.calls = []
        self.next_id = 0
        self.release_result = {
            "clean": True, "checks": CLEAN, "disk": RELEASE_DISK,
            "disk_reclaimed": RELEASE_RECLAIMED,
        }
        self.acquire_disk = ACQUIRE_DISK
        self.acquire_reclaimed = ACQUIRE_RECLAIMED
        self.disk_refusals = []  # full stations the sandbox passed over before granting
        self.helpers = []
        self.lease_apps = {}  # lease_id -> its open applications
        self.acquire_error = None
        self.connect_error = None
        self.written = []
        self.write_error = None
        self.acquire_hold = None  # an event acquire waits on, for the concurrency tests
        self.acquire_entered = threading.Event()

    # -- the station the caller means

    def station(self, number):
        return self.stations[number - 1]

    def of(self, lease_id):
        return next(
            (s for s in self.stations
             if s.record is not None and s.record["lease_id"] == lease_id),
            None,
        )

    def helper(self, lease_id):
        return next(h for h in self.helpers if h.lease_id == lease_id and not h.closed)

    def recover(self, number):
        station = self.station(number)
        station.record, station.dirty = None, False

    # -- the calls the runtime is given

    def write_work_file(self, employee_id, directory, name, data):
        if self.write_error:
            raise self.write_error
        self.written.append((employee_id, directory, name, data))
        return f"org/founder/work/{directory}/{name}"

    def acquire(self, employee_id, activation_id):
        self.calls.append(("acquire", employee_id, activation_id))
        self.acquire_entered.set()
        if self.acquire_hold is not None:
            self.acquire_hold.wait(timeout=5)
        if self.acquire_error:
            raise self.acquire_error
        free = [s for s in self.stations if s.record is None and not s.dirty]
        if not free:
            if any(s.held for s in self.stations):
                raise StationBusy("Every Mac station is leased")
            raise StationNeedsRecovery(
                "The Mac station needs a verified release or owner recovery"
            )
        station = free[0]
        self.next_id += 1
        lease_id = f"{self.next_id:032x}"
        station.record = {"lease_id": lease_id, "role_id": "r-" + employee_id, "since": 100.0}
        station.held = station.dirty = True
        return {**endpoint(lease_id, station.station), "disk": self.acquire_disk,
                "disk_reclaimed": self.acquire_reclaimed,
                "disk_refusals": list(self.disk_refusals)}

    def release(self, lease_id):
        self.calls.append(("release", lease_id))
        # The station's release reaches the helper, which releases the lease binding.
        self.lease_apps.pop(lease_id, None)
        station = self.of(lease_id)
        if station is None:
            return {"status": "ok", "lease_id": lease_id, "gone": True}
        result = {"status": "ok", "lease_id": lease_id, **self.release_result}
        # The lock is dropped either way; an unclean release keeps record and marker.
        station.held = False
        if result["clean"]:
            station.record, station.dirty = None, False
        return result

    def status(self):
        return {
            "configured": True,
            "enrolled": True,
            "stations": [station.status() for station in self.stations],
        }

    def connect(self, lease_id):
        station = self.of(lease_id)
        if station is None or not station.held:
            raise OSError("The Mac lease is not held")
        if self.connect_error:
            raise self.connect_error
        # The binding names the lease, so a reopened connection adopts its tracked applications.
        adopted = lease_id in self.lease_apps
        helper = Helper(lease_id, self.lease_apps.setdefault(lease_id, []), adopted=adopted)
        self.helpers.append(helper)
        return helper


def build(tmp_path, stations):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test", "Build",
        {"max_active": 4, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    macs = Macs(stations)
    runtime.mac = runtime.requests.kinds["mac_station"] = MacLease(runtime)
    runtime.mac.open(
        acquire=macs.acquire, release=macs.release, status=macs.status,
        connect=macs.connect, write_work_file=macs.write_work_file,
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    return runtime, founder, macs


@pytest.fixture
def environment(tmp_path):
    runtime, founder, macs = build(tmp_path, 2)
    yield runtime, founder, macs
    runtime.mac.close()


@pytest.fixture
def one_mac(tmp_path):
    runtime, founder, macs = build(tmp_path, 1)
    yield runtime, founder, macs
    runtime.mac.close()


def hire_and_start(runtime, slug):
    hired = runtime.lifecycle.hire("founder", slug, "Work", request_id=slug)
    invocation = runtime.admit()
    assert invocation.employee_id == hired["employee_id"]
    runtime.mark_started(invocation.activation_id)
    return invocation


def events(runtime):
    with runtime.store.read() as db:
        return [tuple(r) for r in db.execute(
            "SELECT event,employee_id,activation_id,lease_id,station FROM mac_lease_events "
            "ORDER BY rowid"
        )]


def leases(runtime):
    with runtime.store.read() as db:
        return [dict(r) for r in db.execute("SELECT * FROM mac_leases ORDER BY acquired_at")]


def test_two_activations_hold_a_station_each_and_the_third_is_told_who_holds_them(environment):
    runtime, founder, macs = environment
    other, third = hire_and_start(runtime, "other"), hire_and_start(runtime, "third")
    first = runtime.tools(founder.token).mac_acquire()
    assert first == {
        "status": "granted", **endpoint(macs.station(1).record["lease_id"], 1),
        "message": prompt("mac-acquired.md"),
    }
    second = runtime.tools(other.token).mac_acquire()
    assert second == {
        "status": "granted", **endpoint(macs.station(2).record["lease_id"], 2),
        "message": prompt("mac-acquired.md"),
    }
    assert "build in the" in first["message"] and "scratch" in first["message"]
    assert "without `drive_path` there is no confirmed" in first["message"]
    guidance = " ".join(first["message"].split())
    for detail in (
        "/usr/local/libexec/firm-station-add-platform",
        "Homebrew installs persist",
        "do not remove what others may rely on",
        "`~/Applications`",
        "returned element's `handle`",
        "protected profiles",
        "cannot hear or make two-finger gestures",
    ):
        assert detail in guidance
    assert runtime.tools(founder.token).mac_acquire() == first, "the same lease comes back"
    busy = runtime.tools(third.token).mac_acquire()
    assert busy["status"] == "busy"
    assert ROLE_MESSAGES["mac_busy"] in render_tool_result("mac_acquire", busy)
    assert busy["holders"] == [
        {"station": 1, "holder": "founder", "since": leases(runtime)[0]["acquired_at"]},
        {"station": 2, "holder": "founder/other",
         "since": leases(runtime)[1]["acquired_at"]},
    ]
    assert [row["station"] for row in leases(runtime)] == [1, 2]
    status = runtime.tools(third.token).mac_status()
    assert [s["station"] for s in status["stations"]] == [1, 2]
    assert [s["holder"] for s in status["stations"]] == ["founder", "founder/other"]
    assert status["held_by_you"] is False and status["your_station"] is None
    assert runtime.tools(other.token).mac_status()["your_station"] == 2
    assert events(runtime) == [
        ("granted", "founder", founder.activation_id, first["lease_id"], 1),
        ("granted", other.employee_id, other.activation_id, second["lease_id"], 2),
        # A busy refusal is about every station at once, so it names none of them.
        ("busy", third.employee_id, third.activation_id, None, None),
    ]


@pytest.mark.parametrize("reminder", [False, True])
def test_releasing_a_busy_mac_does_not_wake_a_paused_role_but_a_reminder_does(
    one_mac, monkeypatch, reminder
):
    runtime, founder, _ = one_mac
    now = [runtime.store.clock()]
    monkeypatch.setattr(runtime.store, "clock", lambda: now[0])
    waiting = hire_and_start(runtime, "waiting")
    runtime.tools(founder.token).mac_acquire()
    tools = runtime.tools(waiting.token)
    result = tools.mac_acquire()
    assert result["status"] == "busy"
    assert "Mac availability does not itself wake you" in render_tool_result(
        "mac_acquire", result
    )
    if reminder:
        tools.set_reminder(now[0] + 60, "Retry the Mac lease", "retry-mac")
    runtime.finish(waiting.activation_id)
    runtime.tools(founder.token).mac_release()
    assert runtime.admit() is None

    now[0] += 60
    resumed = runtime.admit()
    if reminder:
        assert resumed.employee_id == waiting.employee_id
        assert resumed.session_id == waiting.session_id
    else:
        assert resumed is None


def test_the_owners_hold_on_the_mac_is_reported_without_a_holder(environment):
    runtime, founder, macs = environment
    macs.acquire_error = StationBusy("Every Mac station is leased")
    result = runtime.tools(founder.token).mac_acquire()
    assert result["status"] == "unavailable" and result["reason"] == "mac_busy"
    assert ROLE_MESSAGES["mac_busy"] in render_tool_result("mac_acquire", result)
    assert events(runtime) == []


def test_release_frees_only_the_callers_station(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    assert runtime.tools(other.token).mac_release() == {
        "status": "unavailable", "reason": "mac_not_held",
    }
    granted = runtime.tools(founder.token).mac_acquire()
    kept = runtime.tools(other.token).mac_acquire()
    runtime.tools(founder.token).desktop_status()
    runtime.tools(other.token).desktop_status()
    released = runtime.tools(founder.token).mac_release()
    assert released["clean"] is True and released["checks"] == CLEAN
    assert macs.helper(kept["lease_id"]).closed is False, "the other relay is untouched"
    assert macs.station(1).record is None and macs.station(2).record is not None
    assert macs.calls[-1] == ("release", granted["lease_id"])
    row = leases(runtime)[0]
    assert row["outcome"] == "released" and row["released_at"] is not None
    assert row["station"] == 1 and row["employee_id"] == "founder"
    assert runtime.tools(founder.token).mac_release()["reason"] == "mac_not_held"
    assert runtime.tools(founder.token).desktop_status()["reason"] == "mac_not_held"
    assert runtime.tools(other.token).desktop_status()["ready"] is True
    assert runtime.tools(founder.token).mac_acquire()["station"] == 1


def test_unclean_release_blocks_only_its_own_station(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    granted = runtime.tools(founder.token).mac_acquire()
    failed = {**CLEAN, "work_stopped": {"ok": False, "processes": {"extra": [{"pid": 7}]}}}
    macs.release_result = {"clean": False, "checks": failed}
    released = runtime.tools(founder.token).mac_release()
    assert released["clean"] is False and released["checks"] == failed
    row = leases(runtime)[0]
    assert row["outcome"] == "release_failed" and row["released_at"] is None
    assert events(runtime)[-1] == ("release_failed", "founder", founder.activation_id,
                                   granted["lease_id"], 1)
    # The holder can only release again; the firm's other station is unaffected.
    again = runtime.tools(founder.token).mac_acquire()
    assert again["status"] == "unavailable" and again["reason"] == "mac_release_failed"
    assert again["checks"] == failed
    assert runtime.tools(founder.token).desktop_status()["reason"] == "mac_not_held"
    assert runtime.tools(other.token).mac_acquire()["station"] == 2
    macs.release_result = {"clean": True, "checks": CLEAN}
    assert runtime.tools(founder.token).mac_release()["clean"] is True
    assert leases(runtime)[0]["outcome"] == "released"
    assert runtime.tools(founder.token).mac_acquire()["station"] == 1


@pytest.mark.parametrize("error,reason", [
    (StationBusy("The Mac station is already leased"), "mac_busy"),
    (RuntimeError("Start the isolated VM before recovering its Mac station"),
     "mac_unavailable"),
])
def test_station_refusal_is_reported_in_runtime_words(environment, error, reason):
    runtime, founder, macs = environment
    macs.acquire_error = error
    result = runtime.tools(founder.token).mac_acquire()
    assert result["status"] == "unavailable" and result["reason"] == reason
    assert result["message"] and str(error) not in result["message"]
    assert "Mac availability does not itself wake you" in result["message"]
    assert "station" not in json.dumps(result).lower()
    assert leases(runtime) == [] and events(runtime) == []


OWNER_BLOCKS = [
    (StationNeedsRecovery("The Mac station needs a verified release"), "mac_needs_recovery"),
    (StationNotEnrolled("The Mac has no station account; run mac-prepare"), "mac_not_enrolled"),
    (StationSessionNotReady({"station": 1, "failure": "session_not_ready"}),
     "mac_session_not_ready"),
]


def mac_requests(runtime):
    return rows(runtime, "SELECT * FROM owner_requests WHERE kind='mac_station' ORDER BY rowid")


@pytest.mark.parametrize("error,cause", OWNER_BLOCKS)
def test_a_refusal_only_the_owner_can_lift_opens_one_request_per_role(environment, error, cause):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    macs.acquire_error = error
    first = runtime.tools(founder.token).mac_acquire()
    assert set(first) == {"status", "request_id", "cause", "message"}
    assert first["status"] == "pending" and first["cause"] == cause
    assert first["message"] == ROLE_MESSAGES[cause] and "reason" not in first
    assert "finish normally to pause with working memory intact" in first["message"]
    assert "hand off only when you need a fresh start" in first["message"]
    assert str(error) not in first["message"] and "station" not in json.dumps(first).lower()
    assert runtime.tools(founder.token).mac_acquire() == first, "asking again joins the request"
    second = runtime.tools(other.token).mac_acquire()
    assert second["status"] == "pending" and second["request_id"] != first["request_id"]
    held = mac_requests(runtime)
    assert [(r["employee_id"], r["status"]) for r in held] == [
        (founder.employee_id, "pending"), (other.employee_id, "pending"),
    ]
    payload = json.loads(held[0]["payload_json"])
    assert payload["resource"] == "mac" and payload["cause"] == cause
    assert payload["purpose"] == render("notices", f"{cause}_purpose")
    assert leases(runtime) == [] and events(runtime) == []
    # A role cannot open it by asking for the kind.
    assert runtime.tools(founder.token).request_access("mac_station", "mac", "Lease", "ask") == {
        "status": "refused", "reason": "access_kind_unknown",
    }


def test_the_owners_grant_answers_every_role_waiting_on_the_mac(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    macs.acquire_error = StationNotEnrolled("The Mac has no station account")
    first = runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    macs.acquire_error = None
    result = runtime.requests.grant(OWNER, first["request_id"])
    assert result["status"] == "granted" and len(result["answered"]) == 2
    assert [r["status"] for r in mac_requests(runtime)] == ["granted", "granted"]
    assert [r["decided_by"] for r in mac_requests(runtime)] == ["owner", "owner"]
    for role in (founder, other):
        (notice,) = mail_to(runtime, role.employee_id)
        assert notice["subject"] == "Access granted: mac"
        assert notice["scheduling_ready_at"] is not None
    assert runtime.tools(founder.token).mac_acquire()["status"] == "granted"
    with pytest.raises(FirmError, match="already granted"):
        runtime.requests.grant(OWNER, first["request_id"])


def test_an_open_request_takes_the_cause_the_role_hits_now(environment):
    runtime, founder, macs = environment
    macs.acquire_error = StationNotEnrolled("The Mac has no station account")
    first = runtime.tools(founder.token).mac_acquire()
    macs.acquire_error = StationNeedsRecovery("The Mac station needs a verified release")
    now = runtime.tools(founder.token).mac_acquire()
    assert now["request_id"] == first["request_id"] and now["cause"] == "mac_needs_recovery"
    (held,) = mac_requests(runtime)
    payload = json.loads(held["payload_json"])
    assert payload["cause"] == "mac_needs_recovery"
    assert payload["purpose"] == render("notices", "mac_needs_recovery_purpose")


def test_a_denial_answers_one_request_and_does_not_stand(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    macs.acquire_error = StationSessionNotReady({"station": 1, "failure": "session_not_ready"})
    first = runtime.tools(founder.token).mac_acquire()
    waiting = runtime.tools(other.token).mac_acquire()
    result = runtime.requests.deny(OWNER, first["request_id"], "Not today.")
    assert result["status"] == "denied" and result["answered"] == [first["request_id"]]
    assert [r["status"] for r in mac_requests(runtime)] == ["denied", "pending"]
    (declined,) = mail_to(runtime, founder.employee_id)
    assert declined["subject"] == "Access declined: mac" and "Not today." in declined["body"]
    assert mail_to(runtime, other.employee_id) == []
    # The Mac's state changes, so the next refusal asks afresh, for any role.
    again = runtime.tools(founder.token).mac_acquire()
    assert again["status"] == "pending" and again["request_id"] not in {
        first["request_id"], waiting["request_id"],
    }
    assert runtime.tools(other.token).mac_acquire() == waiting


def test_a_grant_is_released_if_its_durable_record_cannot_be_written(one_mac, monkeypatch):
    runtime, founder, macs = one_mac

    def fail_record(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(runtime.mac, "_event", fail_record)
    with pytest.raises(RuntimeError, match="database unavailable"):
        runtime.tools(founder.token).mac_acquire()

    assert [call[0] for call in macs.calls] == ["acquire", "release"]
    assert macs.station(1).record is None
    assert leases(runtime) == []


def test_a_grant_is_released_if_prior_station_refusals_cannot_be_written(
    one_mac, monkeypatch
):
    runtime, founder, macs = one_mac
    macs.disk_refusals = [{"station": 2, "disk": ACQUIRE_DISK}]

    def fail_record(*_args):
        raise RuntimeError("refusal evidence unavailable")

    monkeypatch.setattr(runtime.mac, "_record_refusals", fail_record)
    with pytest.raises(RuntimeError, match="refusal evidence unavailable"):
        runtime.tools(founder.token).mac_acquire()

    assert [call[0] for call in macs.calls] == ["acquire", "release"]
    assert macs.station(1).record is None
    assert leases(runtime) == []


def wording(result):
    """Every string the role is shown; the structure names stations, the prose must not."""
    if isinstance(result, dict):
        return " ".join(wording(value) for value in result.values())
    if isinstance(result, list):
        return " ".join(wording(item) for item in result)
    return result if isinstance(result, str) else ""


def test_role_facing_mac_wording_never_uses_the_owner_word_station(environment):
    runtime, founder, macs = environment
    tools = runtime.tools(founder.token)
    seen = [tools.mac_acquire()]
    helper_missing = ["station Aqua GUI session is absent"]
    seen.append(tools.desktop_status())
    helper = macs.helpers[0]
    helper.answers["status"] = {"status": "ok", "ready": False, "clean": True,
                                "missing": helper_missing, "configured_station_uid": 502}
    not_ready = tools.desktop_status()
    assert not_ready["ready"] is False and "missing" not in not_ready
    assert not_ready["message"]
    helper.answers["list_apps"] = {"status": "unavailable", "reason": "not_ready",
                                   "message": "station screen is locked"}
    assert tools.desktop_apps()["reason"] == "desktop_not_ready"
    helper.answers["list_windows"] = {"status": "unavailable", "reason": "serialization_failed",
                                      "message": "station helper could not encode JSON"}
    assert tools.desktop_windows(123)["reason"] == "desktop_failed"
    # Closing the applications that leaves one running names what the role can do.
    helper.answers["release"] = {
        "status": "unavailable", "reason": "operation_failed",
        "message": "tracked application did not terminate",
    }
    apps_open = tools.desktop_close_apps()
    assert apps_open["reason"] == "desktop_apps_open" and "mac_release" in apps_open["message"]
    assert helper.calls[-1][0] == "release", "an unclean close is not rebound"
    helper.answers["release"] = {"status": "unavailable", "reason": "cleanup_failed",
                                 "message": "station helper cleanup failed"}
    assert tools.desktop_close_apps()["reason"] == "desktop_apps_open"
    seen.append(apps_open)
    rejected = {"status": "unavailable", "reason": "invalid_request",
                "message": "unknown, expired, or PID-mismatched element handle"}
    helper.answers["focus"] = rejected
    assert tools.desktop_focus(123, "h") == rejected, "the role's own request error stays"
    seen += [not_ready, tools.desktop_apps(), tools.desktop_windows(123)]
    helper.fail = True
    disconnected = tools.desktop_status()
    assert disconnected["reason"] == "helper_disconnected"
    seen.append(disconnected)
    macs.connect_error = HelperNotReady("The station helper is not ready: locked")
    unready = tools.desktop_status()
    assert unready["reason"] == "desktop_not_ready"
    seen.append(unready)
    macs.release_result = {
        "clean": False,
        "checks": {**CLEAN, "desktop_usable": {"ok": False, "ready": False,
                                               "missing": helper_missing, "apps": []}},
        "errors": ["helper: Station helper disconnected"],
    }
    released = tools.mac_release()
    assert "errors" not in released and "missing" not in released["checks"]["desktop_usable"]
    seen += [released, tools.mac_acquire(), tools.mac_status()]
    # The account name in the SSH user and paths is an identifier, not wording.
    for result in seen:
        rendered = wording(result).replace(LEASE["user"], "")
        rendered = rendered.replace("/usr/local/libexec/firm-station-add-platform", "")
        assert "station" not in rendered.lower(), result


def test_refresh_releases_only_the_lease_whose_activation_ended(environment):
    runtime, founder, macs = environment
    analyst = hire_and_start(runtime, "analyst")
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(analyst.token).mac_acquire()
    runtime.mac.refresh()
    assert macs.calls.count(("acquire", "founder", founder.activation_id)) == 1
    assert all(s.record is not None for s in macs.stations), "running activations keep theirs"
    runtime.finish(analyst.activation_id, "completed")
    runtime.mac.refresh()
    assert macs.station(1).record is not None and macs.station(2).record is None
    row = next(r for r in leases(runtime) if r["station"] == 2)
    assert row["outcome"] == "released" and row["released_at"] is not None
    assert runtime.tools(founder.token).desktop_status()["ready"] is True


def test_failed_release_of_an_ended_activation_awaits_owner_recovery(one_mac):
    runtime, founder, macs = one_mac
    analyst = hire_and_start(runtime, "analyst")
    runtime.tools(analyst.token).mac_acquire()
    macs.release_result = {"clean": False, "checks": {**CLEAN, "drive_detached": {"ok": False}}}
    runtime.finish(analyst.activation_id, "completed")
    runtime.mac.refresh()
    row = leases(runtime)[0]
    assert row["outcome"] == "release_failed" and row["released_at"] is not None
    assert macs.station(1).status()["dirty"] is True
    summary = runtime.mac.summary()
    assert summary["stations"][0]["held"] is False and summary["stations"][0]["dirty"] is True
    blocked = runtime.tools(founder.token).mac_acquire()
    assert blocked["status"] == "pending" and blocked["cause"] == "mac_needs_recovery"
    with pytest.raises(FirmError, match="force stop is available"):
        runtime.mac.drain()
    runtime.mac.refresh()
    assert [r["status"] for r in mac_requests(runtime)] == ["pending"], "not recovered yet"
    macs.recover(1)
    runtime.mac.refresh()
    assert leases(runtime)[0]["outcome"] == "recovered"
    assert [r["status"] for r in mac_requests(runtime)] == ["granted"]
    assert [r["decided_by"] for r in mac_requests(runtime)] == ["runtime"]
    (notice,) = mail_to(runtime, founder.employee_id)
    assert notice["subject"] == "Access granted: mac"
    assert events(runtime)[-1] == ("recovered", analyst.employee_id, analyst.activation_id,
                                   row["lease_id"], 1)
    runtime.mac.drain()
    assert runtime.tools(founder.token).mac_acquire()["status"] == "granted"


def test_recovering_one_station_leaves_the_others_failed_release_alone(environment):
    runtime, founder, macs = environment
    first, second = hire_and_start(runtime, "first"), hire_and_start(runtime, "second")
    runtime.tools(first.token).mac_acquire()
    runtime.tools(second.token).mac_acquire()
    macs.release_result = {"clean": False, "checks": {**CLEAN, "drive_detached": {"ok": False}}}
    runtime.finish(first.activation_id, "completed")
    runtime.finish(second.activation_id, "completed")
    runtime.mac.refresh()
    assert [row["outcome"] for row in leases(runtime)] == ["release_failed"] * 2
    macs.recover(1)
    runtime.mac.refresh()
    assert [(row["station"], row["outcome"]) for row in leases(runtime)] == [
        (1, "recovered"), (2, "release_failed"),
    ]
    macs.recover(2)
    runtime.mac.refresh()
    assert [row["outcome"] for row in leases(runtime)] == ["recovered"] * 2


def test_drain_releases_every_holder_before_shutdown(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    runtime.mac.drain()
    assert all(station.record is None for station in macs.stations)
    assert [row["outcome"] for row in leases(runtime)] == ["released"] * 2


def test_drain_releases_both_before_refusing_an_unclean_one(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    macs.release_result = {"clean": False, "checks": {**CLEAN, "work_stopped": {"ok": False}}}
    with pytest.raises(FirmError, match="force stop is available"):
        runtime.mac.drain()
    assert macs.calls[-2:] == [("release", leases(runtime)[0]["lease_id"]),
                               ("release", leases(runtime)[1]["lease_id"])]
    assert [row["outcome"] for row in leases(runtime)] == ["release_failed"] * 2
    assert all(row["released_at"] is not None for row in leases(runtime))


def test_desktop_operations_are_brokered_only_for_the_holder(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    assert runtime.tools(other.token).desktop_apps() == {
        "status": "unavailable", "reason": "mac_not_held",
    }
    runtime.tools(founder.token).mac_acquire()
    assert runtime.tools(other.token).desktop_launch("/Applications/X.app")["reason"] == (
        "mac_not_held"
    )
    assert macs.helpers == []
    result = runtime.tools(founder.token).desktop_launch("/Applications/X.app")
    assert result == {"status": "ok", "pid": 123}
    helper = macs.helpers[0]
    assert helper.calls == [("launch_app", {
        "path": "/Applications/X.app", "launch_mode": "launch_services",
    })]
    assert runtime.tools(founder.token).desktop_launch(
        "/Applications/DriveProbe.app", launch_mode="direct"
    ) == {"status": "ok", "pid": 123}
    assert helper.calls[-1] == ("launch_app", {
        "path": "/Applications/DriveProbe.app", "launch_mode": "direct",
    })
    status = runtime.tools(founder.token).desktop_status()
    assert status["ready"] is True
    assert status["lease_bound"] is True
    assert "configured_control_uid" not in status and "configured_station_uid" not in status
    assert status["session"] == {"login_done": True, "screen_locked": False}
    assert len(macs.helpers) == 1, "a usable relay is reused"
    # The helper is rebound after release so the lease continues.
    assert runtime.tools(founder.token).desktop_close_apps()["clean"] is True
    assert helper.calls[-1] == ("bind_lease", {})
    assert macs.station(1).record is not None
    long = runtime.tools(founder.token).desktop_type(123, "x" * 4097)
    assert long["reason"] == "input_limit" and len(helper.calls) == 5


def test_desktop_inspection_continuation_is_brokered_only_for_the_holder(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    unavailable = runtime.tools(other.token).desktop_inspect(123, handle="outline-handle")
    assert unavailable["reason"] == "mac_not_held"
    assert macs.helpers == []

    runtime.tools(founder.token).mac_acquire()
    tools = runtime.tools(founder.token)
    assert tools.desktop_inspect(123)["status"] == "ok"
    assert tools.desktop_inspect(
        123, depth=5, max_elements=80, handle="outline-handle"
    )["status"] == "ok"
    helper = macs.helpers[0]
    assert helper.calls == [
        ("inspect_ax", {"pid": 123, "depth": 4, "max_elements": 200}),
        ("inspect_ax", {
            "pid": 123, "depth": 5, "max_elements": 80, "handle": "outline-handle",
        }),
    ]


@pytest.mark.parametrize("bound", [False, None])
def test_failed_rebinding_after_close_reports_lease_unavailable(environment, monkeypatch, bound):
    runtime, founder, macs = environment
    tools = runtime.tools(founder.token)
    tools.mac_acquire()
    tools.desktop_launch("/Applications/X.app")
    helper = macs.helpers[0]
    monkeypatch.setattr(helper, "bind_lease", lambda: {"lease_bound": bound})
    result = tools.desktop_close_apps()
    assert result["reason"] == "desktop_lease_unavailable"
    assert helper.closed and helper.apps == []
    assert macs.station(1).record is not None
    assert tools.desktop_launch("/Applications/Y.app")["status"] == "ok"
    assert len(macs.helpers) == 2


def test_each_holders_desktop_calls_go_to_its_own_relay(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    first = runtime.tools(founder.token).mac_acquire()
    second = runtime.tools(other.token).mac_acquire()
    runtime.tools(founder.token).desktop_launch("/Applications/One.app")
    runtime.tools(other.token).desktop_launch("/Applications/Two.app")
    assert macs.helper(first["lease_id"]).calls == [
        ("launch_app", {
            "path": "/Applications/One.app", "launch_mode": "launch_services",
        })]
    assert macs.helper(second["lease_id"]).calls == [
        ("launch_app", {
            "path": "/Applications/Two.app", "launch_mode": "launch_services",
        })]


def test_one_holders_slow_helper_does_not_block_the_others_station(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    first = runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    runtime.tools(founder.token).desktop_status()
    slow = macs.helper(first["lease_id"])
    slow.hold, slow.entered = threading.Event(), threading.Event()
    waiting = threading.Thread(target=lambda: runtime.tools(founder.token).desktop_status())
    waiting.start()
    try:
        assert slow.entered.wait(timeout=5), "the first holder's request is in flight"
        assert runtime.tools(other.token).desktop_status()["ready"] is True
    finally:
        slow.hold.set()
        waiting.join(timeout=5)
    assert not waiting.is_alive()


def finished_within(seconds, work):
    """Run ``work`` on a thread; True when it returned in time."""
    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=seconds)
    return not worker.is_alive()


def test_a_slow_acquire_does_not_block_another_station_holders_desktop(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(founder.token).desktop_status()
    macs.acquire_hold = threading.Event()
    acquiring = threading.Thread(target=lambda: runtime.tools(other.token).mac_acquire())
    acquiring.start()
    try:
        assert macs.acquire_entered.wait(timeout=5), "the sandbox's acquire is in flight"
        seen = []
        assert finished_within(
            5, lambda: seen.append(runtime.tools(founder.token).desktop_status())
        ), "the holder's desktop call waited on the other role's acquire"
        assert seen[0]["ready"] is True
        # The same activation asking again waits for its own acquire's outcome.
        assert not finished_within(0.5, lambda: runtime.tools(other.token).mac_acquire())
    finally:
        macs.acquire_hold.set()
        acquiring.join(timeout=5)
    assert not acquiring.is_alive()
    assert macs.calls.count(("acquire", other.employee_id, other.activation_id)) == 1
    assert [row["station"] for row in leases(runtime)] == [1, 2]


def test_a_slow_acquire_is_released_if_its_work_period_ends_before_the_grant(one_mac):
    runtime, founder, macs = one_mac
    macs.acquire_hold = threading.Event()
    outcome = []

    def acquire():
        try:
            runtime.tools(founder.token).mac_acquire()
        except BaseException as exc:
            outcome.append(exc)

    acquiring = threading.Thread(target=acquire)
    acquiring.start()
    try:
        assert macs.acquire_entered.wait(timeout=5), "the sandbox's acquire is in flight"
        runtime.finish(founder.activation_id)
    finally:
        macs.acquire_hold.set()
        acquiring.join(timeout=5)

    assert not acquiring.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], FirmError)
    assert "expired activation token" in str(outcome[0])
    assert [call[0] for call in macs.calls] == ["acquire", "release"]
    assert macs.station(1).record is None
    assert leases(runtime)[0]["outcome"] == "released"


def test_releasing_with_a_request_in_flight_does_not_block_the_other_station(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    first = runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    runtime.tools(founder.token).desktop_status()
    slow = macs.helper(first["lease_id"])
    slow.hold, slow.entered = threading.Event(), threading.Event()

    def request_outliving_its_work_period():
        # The request completes on the helper; its token is refused once the period ends.
        with pytest.raises(FirmError, match="expired"):
            runtime.tools(founder.token).desktop_status()

    in_flight = threading.Thread(target=request_outliving_its_work_period)
    in_flight.start()
    releasing = None
    try:
        assert slow.entered.wait(timeout=5)
        # The founder's work period ends under its request; housekeeping must wait for
        # that request before closing its relay, and only that Mac waits with it.
        runtime.finish(founder.activation_id, "completed")
        releasing = threading.Thread(target=runtime.mac.refresh)
        releasing.start()
        assert not finished_within(0.5, releasing.join)
        assert runtime.tools(other.token).desktop_status()["ready"] is True
    finally:
        slow.hold.set()
        in_flight.join(timeout=5)
        if releasing is not None:
            releasing.join(timeout=5)
    assert not in_flight.is_alive() and not releasing.is_alive()
    assert macs.station(1).record is None and macs.station(2).record is not None
    assert macs.calls.count(("release", first["lease_id"])) == 1


def test_capture_is_saved_to_the_holders_workspace_and_a_failed_save_keeps_the_image(
    environment,
):
    runtime, founder, macs = environment
    runtime.store.clock = lambda: 1789568585.5  # 2026-09-16T14:23:05Z
    tools = runtime.tools(founder.token)
    tools.mac_acquire()
    tools.desktop_status()
    helper = macs.helpers[0]
    helper.answers["capture_window"] = {
        "status": "ok", "pid": 123, "window_id": 42, "width": 1, "height": 1,
        "png_base64": "iVBORw0K",
    }
    helper.answers["list_apps"] = {"status": "ok", "apps": [
        {"pid": 9, "name": "Other"}, {"pid": 123, "name": "Text Edit 2!"},
    ]}
    result = tools.desktop_capture(123, 42)
    name = "20260916T142305Z-text-edit-2.png"
    assert macs.written == [(founder.employee_id, "captures", name, b"\x89PNG\r\n")]
    assert result["png_base64"] == "iVBORw0K" and result["app"] == "Text Edit 2!"
    assert result["drive_path"] == f"org/founder/work/captures/{name}"
    assert result["mac_path"] == f"/Users/firmstation1/drive/org/founder/work/captures/{name}"
    assert helper.calls[-2:] == [
        ("capture_window", {"pid": 123, "window_id": 42}), ("list_apps", {}),
    ]

    # An application without a usable name is labelled by its window.
    helper.answers["list_apps"] = {"status": "ok", "apps": []}
    tools.desktop_capture(123, 42)
    assert macs.written[-1][2] == "20260916T142305Z-window-42.png"

    macs.write_error = RuntimeError("Guest controller reply deadline exceeded")
    failed = tools.desktop_capture(123, 42)
    assert failed["png_base64"] == "iVBORw0K" and "drive_path" not in failed
    assert failed["save_reason"] == "capture_not_saved"
    assert "no drive_path" in failed["save_message"]
    assert "deadline" not in json.dumps(failed)

    # A failed capture saves nothing.
    macs.write_error = None
    helper.answers["capture_window"] = {"status": "unavailable", "reason": "invalid_request",
                                        "message": "window is not on screen"}
    count = len(macs.written)
    assert tools.desktop_capture(123, 42)["reason"] == "invalid_request"
    assert len(macs.written) == count and helper.calls[-1][0] == "capture_window"


def test_expired_relay_is_reopened_without_touching_the_lease(environment):
    runtime, founder, macs = environment
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(founder.token).desktop_status()
    macs.helpers[0].expired = True
    runtime.tools(founder.token).desktop_status()
    assert len(macs.helpers) == 2 and macs.helpers[0].closed
    assert leases(runtime)[0]["released_at"] is None
    macs.helpers[1].fail = True
    result = runtime.tools(founder.token).desktop_status()
    assert result["reason"] == "helper_disconnected"
    assert macs.helpers[1].closed
    assert runtime.tools(founder.token).desktop_status()["ready"] is True
    assert len(macs.helpers) == 3
    assert [event[0] for event in events(runtime)] == ["granted"]


def test_reopened_relay_adopts_the_lease_apps_it_left_open(environment):
    runtime, founder, macs = environment
    granted = runtime.tools(founder.token).mac_acquire()
    runtime.tools(founder.token).desktop_launch("/Applications/X.app")
    macs.helpers[0].expired = True
    assert runtime.tools(founder.token).desktop_status()["ready"] is True
    adopted = macs.helpers[1]
    # The connection ended; the application it launched did not, and the lease's next
    # connection finds it tracked rather than relaunching it.
    assert adopted.adopted is True
    assert adopted.apps == ["/Applications/X.app"]
    assert ("release", {}) not in macs.helpers[0].calls
    # Closing the applications is the role's own request, or its release.
    assert runtime.tools(founder.token).desktop_close_apps()["clean"] is True
    assert adopted.apps == []
    runtime.tools(founder.token).desktop_launch("/Applications/Y.app")
    assert runtime.tools(founder.token).mac_release()["clean"] is True
    assert granted["lease_id"] not in macs.lease_apps


def test_key_material_never_reaches_the_runtime_or_the_role(environment):
    runtime, founder, macs = environment
    granted = runtime.tools(founder.token).mac_acquire()
    assert granted["key_path"].endswith("/mac-" + granted["lease_id"])
    assert not {"private_key", "public_key", "key", "password"} & granted.keys()
    with runtime.store.read() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(mac_leases)")}
    assert columns == {
        "lease_id", "station", "activation_id", "employee_id", "acquired_at", "released_at",
        "outcome", "checks_json",
    }


def disk_events(runtime):
    """The journal's `mac.disk` events: who, which activation, and what was read."""
    with runtime.store.read() as db:
        return [
            (row["employee_id"], row["activation_id"], json.loads(row["payload_json"]))
            for row in db.execute(
                "SELECT * FROM analysis_events WHERE kind='mac.disk' ORDER BY sequence"
            )
        ]


def test_disk_reading_is_recorded_at_acquire_and_release_and_reaches_the_role(
    environment,
):
    runtime, founder, macs = environment
    with runtime.store.read() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    granted = runtime.tools(founder.token).mac_acquire()
    assert granted["disk"] == ACQUIRE_DISK and granted["disk_reclaimed"] == ACQUIRE_RECLAIMED
    released = runtime.tools(founder.token).mac_release()
    assert released["disk"] == RELEASE_DISK and released["disk_reclaimed"] == RELEASE_RECLAIMED
    assert disk_events(runtime) == [
        ("founder", founder.activation_id,
         {"lease_id": granted["lease_id"], "station": 1, "phase": "acquire", **ACQUIRE_DISK,
          "reclaimed_bytes": ACQUIRE_RECLAIMED}),
        ("founder", founder.activation_id,
         {"lease_id": granted["lease_id"], "station": 1, "phase": "release", **RELEASE_DISK,
          "reclaimed_bytes": RELEASE_RECLAIMED}),
    ]
    # Evidence rides the journal: no table is added, so an analyzer still reads every
    # firm recorded before this existed.
    with runtime.store.read() as db:
        assert tables == {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_an_unknown_disk_reading_is_recorded_as_null_and_never_fails_the_lease(environment):
    runtime, founder, macs = environment
    macs.acquire_disk = {"free_bytes": None, "total_bytes": None}
    macs.acquire_reclaimed = None
    macs.release_result = {"clean": True, "checks": CLEAN}  # no "disk": an unreadable sample
    granted = runtime.tools(founder.token).mac_acquire()
    assert granted["status"] == "granted"
    assert granted["disk"] == {"free_bytes": None, "total_bytes": None}
    assert granted["disk_reclaimed"] is None
    released = runtime.tools(founder.token).mac_release()
    assert released["clean"] is True
    assert "disk" not in released and "disk_reclaimed" not in released
    readings = {payload["phase"]: payload for _, _, payload in disk_events(runtime)}
    for phase in ("acquire", "release"):
        assert readings[phase]["free_bytes"] is None and readings[phase]["total_bytes"] is None
        assert readings[phase]["reclaimed_bytes"] is None


def test_a_failed_simulator_steps_exit_code_and_output_reach_the_journal(environment):
    """Every step failing at once, each with the guest's full 512 bytes of output, beside
    a reclaim of every category: the release's evidence still fits one journal event."""
    runtime, founder, macs = environment
    said = ("An error was encountered processing the command (code=405):\n" * 9)[-512:]
    simulators = [
        {"step": step, "completed": False, "timed_out": False, "returncode": 1, "said": said}
        for step in ("shutdown", "default", "testing")
    ]
    reclaim = {
        "completed": True, "timed_out": False, "refused": None,
        "categories": [
            {"category": name, "count": 12345, "bytes": 12 * GIB, "skipped": 67,
             "skipped_bytes": 89 * GIB, "skipped_reasons": ["errno:1", "errno:13", "errno:62"]}
            for name in ("simulators", "home", "caches", "logs", "saved_application_state",
                         "trash", "swiftpm", "xcode")
        ],
    }
    macs.release_result = {
        "clean": True, "checks": CLEAN, "disk": RELEASE_DISK,
        "disk_reclaimed": RELEASE_RECLAIMED, "reclaim": reclaim, "simulators": simulators,
    }
    runtime.tools(founder.token).mac_acquire()
    assert runtime.tools(founder.token).mac_release()["clean"] is True
    released = [payload for _, _, payload in disk_events(runtime) if payload["phase"] == "release"]
    assert [payload["simulators"] for payload in released] == [simulators]
    assert released[0]["reclaim"] == reclaim


RECLAIM = {
    "completed": False, "timed_out": True, "refused": None,
    "categories": [{"category": "caches", "count": 3, "bytes": 2 * GIB, "skipped": 1,
                    "skipped_bytes": 9, "skipped_reasons": ["errno:1"]}],
}


def full(station, free_gib, reclaim=None):
    """A station's refusal, with the list the sandbox attaches to whatever it raises."""
    refusal = StationDiskFull(
        f"Mac station {station} has {free_gib * GIB} bytes free, below the floor",
        station=station, disk={"free_bytes": free_gib * GIB, "total_bytes": 100 * GIB},
        disk_reclaimed=0, reclaim=reclaim,
    )
    refusal.disk_refusals = [refusal.evidence()]
    return refusal


def test_disk_full_refusal_is_recorded_without_a_lease_and_reported_to_the_role(environment):
    runtime, founder, macs = environment
    macs.acquire_error = full(1, 1, RECLAIM)
    result = runtime.tools(founder.token).mac_acquire()
    assert result["status"] == "unavailable" and result["reason"] == "mac_disk_full"
    assert result["message"] and "1073741824" not in result["message"]
    assert "Mac availability does not itself wake you" in result["message"]
    assert result["disk"] == {"free_bytes": 1 * GIB, "total_bytes": 100 * GIB}
    assert result["disk_reclaimed"] == 0
    assert "station" not in json.dumps(result).lower()
    assert leases(runtime) == [] and events(runtime) == []
    # What the reclaim before the refusal left, and why, is kept with the reading.
    assert disk_events(runtime) == [
        ("founder", founder.activation_id,
         {"lease_id": None, "station": 1, "phase": "refused", "free_bytes": 1 * GIB,
          "total_bytes": 100 * GIB, "reclaimed_bytes": 0, "reclaim": RECLAIM}),
    ]


def test_a_full_station_passed_over_is_recorded_whatever_the_acquire_comes_to(environment):
    """Station 1 full and station 2 granted, or busy: station 1's refusal is not lost."""
    runtime, founder, macs = environment
    passed_over = full(1, 1).evidence()
    macs.disk_refusals = [passed_over]
    granted = runtime.tools(founder.token).mac_acquire()
    assert granted["status"] == "granted" and "disk_refusals" not in granted
    phases = [(payload["phase"], payload["station"]) for _, _, payload in disk_events(runtime)]
    assert phases == [("refused", 1), ("acquire", granted["station"])]
    runtime.tools(founder.token).mac_release()

    busy = StationBusy("every Mac is held")
    busy.disk_refusals = [passed_over]
    macs.acquire_error = busy
    assert runtime.tools(founder.token).mac_acquire()["status"] in {"busy", "unavailable"}
    refused = [payload for _, _, payload in disk_events(runtime) if payload["phase"] == "refused"]
    assert [payload["station"] for payload in refused] == [1, 1]

    # Station 2 granted, then its lease tunnel timed out: not one of the three refusals,
    # and not even a RuntimeError, but station 1's evidence is kept all the same.
    import subprocess

    timed_out = subprocess.TimeoutExpired(["/usr/bin/ssh"], 30)
    timed_out.disk_refusals = [passed_over]
    macs.acquire_error = timed_out
    result = runtime.tools(founder.token).mac_acquire()
    assert result["status"] == "unavailable" and result["reason"] == "mac_unavailable"
    refused = [payload for _, _, payload in disk_events(runtime) if payload["phase"] == "refused"]
    assert [payload["station"] for payload in refused] == [1, 1, 1]


def test_lease_calls_require_a_live_token(environment):
    runtime, founder, macs = environment
    for call in (
        lambda: runtime.tools("stale").mac_acquire(),
        lambda: runtime.tools("stale").mac_release(),
        lambda: runtime.tools("stale").mac_status(),
        lambda: runtime.tools("stale").desktop_status(),
    ):
        with pytest.raises(FirmError, match="activation token"):
            call()
    assert macs.calls == []


def test_unconfigured_lease_is_unavailable(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test", "Build",
        {"max_active": 4, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    assert runtime.tools(founder.token).mac_acquire() == {
        "status": "unavailable", "reason": "no_mac",
    }
    assert runtime.tools(founder.token).desktop_status()["reason"] == "mac_not_held"
    assert runtime.mac.summary() == {"configured": False, "enrolled": False, "stations": []}
    runtime.mac.drain()


def test_a_firm_without_a_mac_answers_no_mac_and_records_nothing(environment):
    runtime, founder, macs = environment
    absent = {"configured": False, "enrolled": False, "stations": []}
    runtime.mac.open(
        acquire=lambda employee_id, activation_id: {"status": "unavailable", "reason": "no_mac"},
        release=macs.release, status=lambda: absent, connect=macs.connect,
        write_work_file=macs.write_work_file,
    )
    tools = runtime.tools(founder.token)
    assert tools.mac_acquire() == {"status": "unavailable", "reason": "no_mac"}
    assert tools.mac_status() == {"status": "ok", **absent, "held_by_you": False,
                                  "your_station": None}
    assert tools.desktop_status()["reason"] == "mac_not_held"
    assert runtime.mac.summary() == absent
    with runtime.store.read() as db:
        assert db.execute("SELECT count(*) FROM mac_leases").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM mac_lease_events").fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM analysis_events WHERE kind='mac.disk'"
        ).fetchone()[0] == 0
    runtime.mac.refresh()
    runtime.mac.drain()
    assert macs.calls == []


def operations(runtime):
    with runtime.store.read() as db:
        return [dict(r) for r in db.execute("SELECT * FROM mac_operations ORDER BY rowid")]


def test_every_brokered_call_is_journalled_without_its_arguments_or_results(environment):
    runtime, founder, macs = environment
    other = hire_and_start(runtime, "other")
    tools = runtime.tools(founder.token)
    # Nothing that never reached a held lease is journalled.
    assert runtime.tools(other.token).desktop_apps()["reason"] == "mac_not_held"
    granted = tools.mac_acquire()
    tools.desktop_status()
    assert tools.desktop_type(123, "x" * 4097)["reason"] == "input_limit"
    tools.desktop_type(123, "secret passphrase")
    macs.helpers[0].answers["click"] = {"status": "unavailable", "reason": "invalid_request",
                                        "message": "window is not on screen"}
    tools.desktop_click(123, 42, 10, 20, modifiers=["command"], hold_ms=750)
    journal = operations(runtime)
    assert [row["op"] for row in journal] == ["status", "type_text", "click"]
    assert [row["outcome"] for row in journal] == ["ok", "ok", "invalid_request"]
    for row in journal:
        assert row["lease_id"] == granted["lease_id"] and row["station"] == 1
        assert row["employee_id"] == "founder" and row["activation_id"] == founder.activation_id
        assert row["duration_ms"] >= 0 and row["at"] > 0
    with runtime.store.read() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(mac_operations)")}
    assert columns == {"at", "lease_id", "station", "employee_id", "activation_id", "op",
                       "duration_ms", "outcome"}
    assert "secret passphrase" not in json.dumps(journal)
    assert "command" not in json.dumps(journal)


def test_a_passage_longer_than_the_helpers_ceiling_is_refused_before_it_is_sent(environment):
    runtime, founder, macs = environment
    tools = runtime.tools(founder.token)
    tools.mac_acquire()
    tools.desktop_status()
    helper = macs.helpers[0]
    calls = len(helper.calls)
    taps = [{"x": 1.0, "y": 2.0, "after_ms": 4000} for _ in range(6)]
    refused = tools.desktop_tap_sequence(123, 42, taps)
    assert refused["reason"] == "invalid_request"
    assert len(helper.calls) == calls, "nothing was posted"
    assert [row["op"] for row in operations(runtime)] == ["status"], "nothing reached the relay"
    helper.answers["tap_sequence"] = {"status": "ok", "count": 5, "offsets_ms": [0] * 5}
    within = tools.desktop_tap_sequence(123, 42, taps[:5])
    assert within["count"] == 5
    assert helper.calls[-1] == ("tap_sequence", {"pid": 123, "window_id": 42, "taps": taps[:5]})
    assert [row["op"] for row in operations(runtime)] == ["status", "tap_sequence"]


def test_analysis_reports_leases_and_busy_events_by_role(environment):
    from firm_runtime.analysis import AnalysisReader

    runtime, founder, macs = environment
    other, third = hire_and_start(runtime, "other"), hire_and_start(runtime, "third")
    runtime.tools(founder.token).mac_acquire()
    runtime.tools(other.token).mac_acquire()
    runtime.tools(third.token).mac_acquire()
    runtime.tools(founder.token).mac_release()
    data = AnalysisReader(runtime.store.path).read("mac")
    kinds = sorted(row["type"] for row in data["rows"])
    assert kinds == ["busy", "lease", "lease"]
    lease = next(row for row in data["rows"] if row["employee_id"] == "founder")
    assert lease["station"] == 1 and lease["outcome"] == "released"
    assert lease["duration_seconds"] is not None and lease["open"] is False
    assert next(row for row in data["rows"] if row["employee_id"] == other.employee_id)[
        "station"] == 2
    assert data["by_role"]["founder"]["leases"] == 1
    assert data["by_role"][third.employee_id]["busy"] == 1
    assert data["role_labels"]["founder"]["address"] == "founder"
    only_busy = AnalysisReader(runtime.store.path).read("mac", row_type="busy")
    assert [row["employee_id"] for row in only_busy["rows"]] == [third.employee_id]
    assert only_busy["rows"][0]["station"] is None


def test_analysis_summarizes_what_each_desktop_operation_cost(environment):
    from firm_runtime.analysis import AnalysisReader

    runtime, founder, macs = environment
    tools = runtime.tools(founder.token)
    tools.mac_acquire()
    tools.desktop_status()
    tools.desktop_click(123, 42, 10, 20)
    macs.helpers[0].answers["click"] = {"status": "unavailable", "reason": "invalid_request",
                                        "message": "window is not on screen"}
    tools.desktop_click(123, 42, 10, 20)
    data = AnalysisReader(runtime.store.path).read("mac")
    assert sorted(data["by_operation"]) == ["click", "status"]
    click = data["by_operation"]["click"]
    assert click["count"] == 2 and click["failures"] == 1
    assert click["median_duration_ms"] <= click["max_duration_ms"]
    assert data["by_operation"]["status"]["failures"] == 0
    only_operations = AnalysisReader(runtime.store.path).read("mac", row_type="operation")
    assert [row["op"] for row in only_operations["rows"]] == ["status", "click", "click"]
    assert [row["outcome"] for row in only_operations["rows"]] == [
        "ok", "ok", "invalid_request",
    ]
    assert only_operations["rows"][0]["station"] == 1
    assert only_operations["rows"][0]["employee_id"] == "founder"


@pytest.mark.parametrize("part", ["processes", "services", "launch_agents", "crontab"])
def test_unverified_release_leaves_diagnostics_to_the_owner(environment, caplog, part):
    runtime, founder, macs = environment
    tools = runtime.tools(founder.token)
    tools.mac_acquire()
    checks = {**CLEAN, "work_stopped": {"ok": False, part: {
        "ok": False, "verified": False, "error": "private read error",
    }}}
    macs.release_result = {"clean": False, "checks": checks,
                           "errors": [f"sweep.{part}: private read error"]}
    released = tools.mac_release()
    assert released["reason"] == "mac_release_unverified"
    assert "waiting for the owner" in released["message"]
    assert "private read error" not in str(released)
    assert "private read error" in caplog.text
    assert tools.mac_acquire()["reason"] == "mac_release_unverified"
