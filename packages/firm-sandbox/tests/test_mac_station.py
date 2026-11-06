import fcntl
import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from firm_sandbox.file_server import Credentials
from firm_sandbox.mac_station import (
    DIRTY_MARKER,
    DIRTY_MARKER_CONTENT,
    SAID_CHARACTERS,
    Lease,
    MacStation,
    RelayNotConnected,
    StationConnection,
    StationSessionNotReady,
    station_locks,
)

LEASE = "0" * 31 + "1"
OTHER = "0" * 31 + "2"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial firm-mac-lease"
GIB = 1 << 30
# What the guest reads back from a desktop that will neither lock nor sleep.
AWAKE = {"screen_lock": "screenLock is off", "screen_saver_idle": 0, "relock_at_login": False,
         "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0}}
BASELINE = {
    "processes": [[100, "Mon Sep 15 10:00:00 2026"]],
    "services": ["com.apple.Finder"],
    "launch_agents": [],
    "crontab": "",
}


class FakeVM:
    """Guest controller replies for the station operations, all clean by default."""

    def __init__(self, root, tmp_path):
        self.root = root
        self.stations = (1, 2)
        self.firm_locks = tmp_path / "locks"
        self.network = SimpleNamespace(ready=lambda: True)
        self.running = True
        self.ssh_config = root / "lima" / "mac" / "ssh.config"
        self.socket_root = root / "lima" / "mac"
        self.paths = SimpleNamespace(verify_runtime=lambda: None)
        self.settings = SimpleNamespace(firm_id="test-firm")
        self.image = tmp_path / "image"
        self.image.mkdir()
        (self.image / "image.json").write_text(
            json.dumps({"enrollment": {"stations": 2}})
        )
        (self.image / "image.json").chmod(0o600)
        self.enrolled = lambda: True
        self.calls = []
        self.replies = {
            "station-account": {"present": True},
            "station-session-ensure": {
                "station": 1, "user": "firmstation1", "uid": 502,
                "ready": True, "created": False, "helper": {"ready": True},
            },
            "station-session-reset": {
                "station": 1, "user": "firmstation1", "uid": 502,
                "ready": True, "created": True, "helper": {"ready": True},
                "reset": True,
            },
            "station-baseline": BASELINE,
            "station-reclaim": {
                "refused": False, "completed": True, "timed_out": False,
                "categories": {},
                "kept": {"home": [], "entries": {}, "outside_categories": []},
                "scratch": "/Users/firmstation1/scratch",
            },
            "station-mount": {"mounted": True, "point": "/Users/firmstation1/drive",
                              "scratch": "/Users/firmstation1/scratch"},
            "station-key-install": {"installed": True},
            "station-key-remove": {"present": False},
            "station-kill-sessions": {"remaining": []},
            "station-sweep": {
                "ok": True, "remaining_seconds": 360,
                "processes": {"extra": [], "tolerated": []},
                "services": {"extra": []},
                "launch_agents": {"extra": []},
                "crontab": {"empty": True},
            },
            "station-unmount": {"mounted": False},
            "station-command-log": {"data": "MjAyNiBjbWQK"},
            "station-keep-awake": AWAKE,
            "station-awake-status": AWAKE,
            "sample": {"disk_used_bytes": 40 * GIB, "disk_total_bytes": 100 * GIB},
        }
        self.failures = {}

    def _running(self):
        return self.running

    def _rpc(self, operation, **payload):
        self.calls.append((operation, payload))
        if operation in self.failures:
            failure = self.failures[operation]
            if isinstance(failure, list):
                failure = failure.pop(0)
                if not self.failures[operation]:
                    del self.failures[operation]
            raise RuntimeError(failure)
        if operation == "station-verify" and operation not in self.replies:
            # Verification observes the current fake guest state.  Keep an explicit
            # reply available for tests that need an unreadable or otherwise special
            # final read, but do not carry an earlier sweep failure into a later read.
            sweep = self.replies["station-sweep"]
            key = self.replies["station-key-remove"]
            sessions = self.replies["station-kill-sessions"]
            mounted = self.replies["station-unmount"]
            return {
                **sweep,
                "key_present": key.get("present", False),
                "sessions": sessions.get("remaining", []),
                "drive_mounted": mounted.get("mounted", False),
            }
        reply = self.replies[operation]
        if isinstance(reply, list):
            reply = reply.pop(0)
            if not self.replies[operation]:
                self.replies[operation] = reply
        return reply


def tunnel_closed(lease_id):
    return {"tunnel_closed": True}


class FakeRelay:
    def __init__(self, state, apps):
        self.state, self.apps = state, apps
        self.calls = []
        self.closed = False

    def request(self, op, **arguments):
        self.calls.append(op)
        if op == "status":
            return {**self.state, "status": "ok"}
        if op == "list_apps":
            return {"apps": self.apps, "status": "ok"}
        return {"clean": True, "status": "ok"}

    def close(self):
        self.closed = True


@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    vm = FakeVM(root, tmp_path)
    station = MacStation(vm, 1, revoke=tunnel_closed)
    helper = {
        "state": {"ready": True, "clean": True, "environment": "vm-session"},
        "apps": [],
        "relays": [],
    }

    def relay(binding, *, recovery_only=False):
        connection = FakeRelay(helper["state"], helper["apps"])
        helper["relays"].append((binding, recovery_only, connection))
        return connection

    monkeypatch.setattr(station, "_relay", relay)
    station.helper = helper
    return station


def acquire(station, lease_id=LEASE, role_id="r-e1"):
    """The lease alone; ``station.acquire`` also returns a best-effort disk reading
    and what its reclaim removed."""
    lease, _disk, _reclaimed = station.acquire(
        lease_id,
        role_id,
        Credentials("firmfile-abc", "secret", 4450),
        KEY,
        address="founder/dev",
        activation_id="a-1",
    )
    return lease


def lock_is_free(station):
    # The station lock is a firm host lock, not state of the Mac VM, and names its station.
    assert not (station.root / "locks").exists()
    with (station.firm_locks / f"mac-station-{station.station}").open() as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ---- the lease ---------------------------------------------------------------------


def test_acquire_takes_baseline_marker_drive_and_key_and_returns_the_endpoint(station):
    lease = acquire(station)
    assert lease == Lease(
        LEASE, 1, "127.0.0.1", 2222, "firmstation1", "/Users/firmstation1/drive",
        "/Users/firmstation1/drive/org/founder/dev/work", "/Users/firmstation1/scratch",
    )
    operations = [call[0] for call in station.vm.calls]
    # The baseline takes the guest's one lock, so the free space read after it is never
    # a reading taken while a reclaim whose reply was lost is still deleting.
    assert operations == [
        "station-account", "station-session-ensure", "station-keep-awake",
        "station-reclaim", "station-baseline", "sample", "station-mount",
        "station-key-install",
    ]
    assert station.vm.calls[6][1]["drive"]["password"] == "secret"
    assert station.vm.calls[6][1]["timeout"] == 165
    assert station.vm.calls[7][1]["public_key"] == KEY
    assert all(
        payload["station"] == 1
        for operation, payload in station.vm.calls
        if operation.startswith("station-")
    )
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["role_id"] == "r-e1" and record["activation_id"] == "a-1"
    assert record["baseline"] == BASELINE
    assert record["steps"] == ["drive_mounted", "key_installed"]
    assert record["disk"] == {"acquired": {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}}
    assert record["disk_reclaimed"] == 0
    assert (station.root / DIRTY_MARKER).read_bytes() == DIRTY_MARKER_CONTENT
    status = station.status()
    assert status == {
        "configured": True, "station": 1, "enrolled": True, "held": True, "lease_id": LEASE,
        "holder": "r-e1", "since": record["acquired_at"], "dirty": False,
    }
    assert station.holds(LEASE) and not station.holds(OTHER)
    with pytest.raises(OSError, match="already leased"):
        acquire(station, OTHER, "r-e2")


def test_release_verifies_four_checks_and_clears_marker_record_and_lock(station):
    acquire(station)
    result = station.release(LEASE)
    assert result["clean"] is True and result["errors"] == []
    assert set(result["checks"]) == {
        "access_revoked", "work_stopped", "drive_detached", "desktop_usable",
    }
    assert all(check["ok"] for check in result["checks"].values())
    assert result["disk"] == {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
    assert result["disk_reclaimed"] == 0
    operations = [call[0] for call in station.vm.calls[8:]]
    assert operations == [
        "station-key-remove", "station-kill-sessions", "station-sweep", "station-unmount",
        "station-reclaim", "station-verify", "station-command-log", "sample",
    ]
    assert station.vm.calls[10][1] == {"station": 1, "baseline": BASELINE, "timeout": 480}
    # The helper is released before the sweep; after the unmount a fresh connection
    # verifies its ledger again, then reports.
    assert [relay.calls for _, _, relay in station.helper["relays"]] == [
        ["release"], ["release", "status", "list_apps"],
    ]
    assert all(recovery for _, recovery, _ in station.helper["relays"])
    assert not (station.root / DIRTY_MARKER).exists()
    assert not (station.root / "leases" / f"{LEASE}.json").exists()
    assert (station.root / "leases" / f"{LEASE}.log").read_bytes() == b"2026 cmd\n"
    assert station.status()["held"] is False and station.status()["dirty"] is False
    lock_is_free(station)
    assert acquire(station, OTHER, "r-e2").lease_id == OTHER


def test_release_of_a_stale_or_unknown_lease_is_a_no_op(station):
    assert station.release(OTHER) == {"status": "ok", "lease_id": OTHER, "gone": True}
    acquire(station)
    assert station.release(OTHER)["gone"] is True
    assert station.status()["held"] is True
    assert [call[0] for call in station.vm.calls] == [
        "station-account", "station-session-ensure", "station-keep-awake",
        "station-reclaim", "station-baseline", "sample", "station-mount",
        "station-key-install",
    ]
    station.release(LEASE)
    assert station.release(LEASE)["gone"] is True


def test_acquire_and_release_each_record_their_own_disk_reading(station):
    station.vm.replies["sample"] = {"disk_used_bytes": 40 * GIB, "disk_total_bytes": 100 * GIB}
    lease, disk, reclaimed = station.acquire(
        LEASE, "r-e1", Credentials("firmfile-abc", "secret", 4450), KEY,
        address="founder/dev", activation_id="a-1",
    )
    assert lease.lease_id == LEASE
    assert disk == {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
    assert reclaimed == 0
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["disk"] == {"acquired": disk}
    # The station filled between acquire and release; release takes its own reading.
    station.vm.replies["sample"] = {"disk_used_bytes": 70 * GIB, "disk_total_bytes": 100 * GIB}
    result = station.release(LEASE)
    assert result["clean"] is True
    assert result["disk"] == {"free_bytes": 30 * GIB, "total_bytes": 100 * GIB}
    assert result["disk_reclaimed"] == 0


def test_a_failing_sample_leaves_acquire_and_release_unchanged(station):
    station.vm.failures["sample"] = "guest controller unreachable"
    lease, disk, reclaimed = station.acquire(
        LEASE, "r-e1", Credentials("firmfile-abc", "secret", 4450), KEY,
        address="founder/dev", activation_id="a-1",
    )
    assert lease.lease_id == LEASE
    assert disk == {"free_bytes": None, "total_bytes": None}
    # An unknown free-space reading cannot be compared with the floor, so it never
    # refuses on its own account.
    assert reclaimed == 0
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["disk"] == {"acquired": disk}
    assert record["steps"] == ["drive_mounted", "key_installed"]
    result = station.release(LEASE)
    assert result["clean"] is True and result["errors"] == []
    assert result["disk"] == {"free_bytes": None, "total_bytes": None}
    assert result["disk_reclaimed"] == 0
    assert not (station.root / DIRTY_MARKER).exists()


def test_a_sample_reply_shaped_wrong_leaves_acquire_and_release_unchanged(station):
    station.vm.replies["sample"] = ["not", "a", "mapping"]
    lease, disk, reclaimed = station.acquire(
        LEASE, "r-e1", Credentials("firmfile-abc", "secret", 4450), KEY,
        address="founder/dev", activation_id="a-1",
    )
    assert lease.lease_id == LEASE
    assert disk == {"free_bytes": None, "total_bytes": None}
    assert reclaimed == 0
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["disk"] == {"acquired": disk}
    result = station.release(LEASE)
    assert result["clean"] is True and result["errors"] == []
    assert result["disk"] == {"free_bytes": None, "total_bytes": None}
    assert not (station.root / DIRTY_MARKER).exists()


def test_acquire_refuses_a_mac_without_the_station_account_and_leaves_nothing(station):
    from firm_sandbox.mac_station import StationNotEnrolled

    station.vm.replies["station-account"] = {"present": False}
    with pytest.raises(StationNotEnrolled, match="no station account"):
        acquire(station)
    assert [call[0] for call in station.vm.calls] == ["station-account"]
    assert not (station.root / DIRTY_MARKER).exists() and station.status()["dirty"] is False
    lock_is_free(station)


def test_acquire_skips_a_station_whose_desktop_cannot_be_made(station):
    station.vm.replies["station-session-ensure"] = {
        "station": 1, "user": "firmstation1", "uid": 502,
        "ready": False, "created": False, "helper": None,
        "failure": "login_failed",
    }
    with pytest.raises(StationSessionNotReady, match="login_failed") as excinfo:
        acquire(station)
    assert excinfo.value.result["ready"] is False
    assert [call[0] for call in station.vm.calls] == [
        "station-account", "station-session-ensure", "station-keep-awake",
    ]
    assert not (station.root / DIRTY_MARKER).exists()
    assert not (station.root / "leases").exists()
    lock_is_free(station)


def test_ensure_session_reports_transport_failure_as_not_ready(station):
    station.vm.failures["station-session-ensure"] = "guest controller unreachable"
    assert station.ensure_session() == {
        "station": 1,
        "user": "firmstation1",
        "uid": 502,
        "ready": False,
        "created": False,
        "helper": None,
        "failure": "session_not_ready",
        "awake": AWAKE,
        "error": "guest controller unreachable",
    }
    assert station.vm.calls == [
        ("station-session-ensure", {"station": 1, "timeout": 90}),
        ("station-keep-awake", {"station": 1}),
    ]


@pytest.mark.parametrize("bad", [
    {"screen_lock": "screenLock delay is immediate"}, {"screen_lock": "unknown"},
    {"screen_saver_idle": 1200}, {"relock_at_login": True},
    {"power": {"displaysleep": 10, "sleep": 0, "disksleep": 0}},
])
def test_a_desktop_that_would_lock_or_sleep_is_not_ready_whatever_the_setter_said(station, bad):
    station.vm.replies["station-keep-awake"] = {**AWAKE, **bad}
    result = station.ensure_session()
    assert result["ready"] is False and result["failure"] == "session_not_ready"
    assert result["awake"] == {**AWAKE, **bad}


def test_a_blocked_login_is_not_followed_by_the_awake_step(station):
    # It would offer the rejected password once more, one failed login per boot.
    station.vm.replies["station-session-ensure"] = {
        "station": 1, "user": "firmstation1", "uid": 502, "ready": False, "created": False,
        "helper": None, "failure": "login_blocked",
    }
    result = station.ensure_session()
    assert result["failure"] == "login_blocked" and result["awake"] is None
    assert not any(op == "station-keep-awake" for op, _ in station.vm.calls)


def test_ensure_session_reports_awake_failure_as_not_ready(station):
    station.vm.failures["station-keep-awake"] = "preferences unavailable"
    result = station.ensure_session()
    assert result["ready"] is False
    assert result["failure"] == "session_not_ready"
    assert result["awake"] is None
    assert result["error"] == "preferences unavailable"


def test_session_reset_is_numbered_bounded_and_refused_under_a_lease(station):
    result = station.session_reset()
    assert result["reset"] is True and result["ready"] is True
    assert station.vm.calls == [
        ("station-key-remove", {"station": 1}),
        ("station-session-reset", {"station": 1, "timeout": 210}),
    ]
    acquire(station)
    with pytest.raises(OSError, match="already leased"):
        station.session_reset()


def test_session_reset_attests_and_clears_a_retained_lease_record(station):
    acquire(station)
    descriptor = station._held["lock"]
    station._held = None
    os.close(descriptor)
    station.vm.replies["station-verify"] = {
        **station.vm.replies["station-sweep"],
        "key_present": False, "sessions": [], "drive_mounted": False,
    }
    result = station.session_reset()
    assert result["ready"] is True
    assert result["cleanup"]["clean"] is True
    assert result["cleanup"]["verify"]["ok"] is True
    operations = [operation for operation, _payload in station.vm.calls]
    reset = operations.index("station-session-reset")
    assert "station-key-remove" in operations[:reset]
    assert "station-sweep" in operations[reset:]
    assert "station-reclaim" in operations[reset:]
    assert operations[-3:] == ["station-verify", "station-command-log", "sample"]
    assert not (station.root / DIRTY_MARKER).exists()
    assert station._records() == []


def test_session_reset_leaves_a_marker_without_its_baseline_dirty(station):
    station.root.mkdir(mode=0o700, parents=True)
    marker = station.root / DIRTY_MARKER
    marker.write_bytes(DIRTY_MARKER_CONTENT)
    marker.chmod(0o600)
    result = station.session_reset()
    assert result["ready"] is False
    assert result["failure"] == "cleanup_baseline_missing"
    assert result["cleanup"]["clean"] is False
    assert marker.exists()
    assert station.vm.calls == []


def test_whole_machine_station_locks_are_reentrant_for_per_station_calls(station):
    with station_locks(station.firm_locks, (2, 1)):
        assert station.ensure_session()["ready"] is True
        assert station.awake_status() == AWAKE
    lock_is_free(station)


# ---- admission and reclaim -----------------------------------------------------------


def test_acquire_refuses_below_the_disk_floor_and_leaves_nothing(station):
    from firm_sandbox.mac_station import StationDiskFull

    station.vm.replies["sample"] = {"disk_used_bytes": 99 * GIB, "disk_total_bytes": 100 * GIB}
    station.vm.replies["station-reclaim"] = {
        **station.vm.replies["station-reclaim"], "completed": False, "timed_out": True,
        "categories": {"caches": {
            "count": 3, "bytes": 2 * GIB,
            "skipped": {"errno:1": {"count": 2, "bytes": 5}, "flags": {"count": 1, "bytes": 1}},
        }},
    }
    with pytest.raises(StationDiskFull) as excinfo:
        acquire(station)
    assert excinfo.value.station == 1
    assert excinfo.value.disk == {"free_bytes": 1 * GIB, "total_bytes": 100 * GIB}
    assert excinfo.value.disk_reclaimed == 2 * GIB
    # Why the station is still full after reclaiming travels with the refusal.
    assert excinfo.value.evidence()["reclaim"] == {
        "completed": False, "timed_out": True, "refused": None,
        "categories": [{"category": "caches", "count": 3, "bytes": 2 * GIB, "skipped": 3,
                        "skipped_bytes": 6, "skipped_reasons": ["errno:1", "flags"]}],
    }
    assert [call[0] for call in station.vm.calls] == [
        "station-account", "station-session-ensure", "station-keep-awake",
        "station-reclaim", "station-baseline", "sample",
    ]
    # Nothing was granted, so there is nothing for a later release or recovery to verify.
    assert not (station.root / DIRTY_MARKER).exists()
    assert not (station.root / "leases").exists()
    assert station.status()["dirty"] is False
    lock_is_free(station)
    # A fresh station may try again right away.
    station.vm.replies["sample"] = {"disk_used_bytes": 40 * GIB, "disk_total_bytes": 100 * GIB}
    assert acquire(station).lease_id == LEASE


def test_a_reclaim_refusal_is_reported_when_final_state_is_clean(station):
    """A current clean assessment supersedes an earlier reclaim refusal."""
    acquire(station)
    station.vm.replies["station-reclaim"] = {
        **station.vm.replies["station-reclaim"], "refused": True, "reason": "drive_mounted",
        "completed": False,
    }
    result = station.release(LEASE)
    assert station.vm.replies["station-unmount"] == {"mounted": False}
    assert result["clean"] is True
    assert result["checks"]["drive_detached"] == {"ok": True, "mounted": False}
    assert result["reclaim"]["refused"] == "drive_mounted"
    assert not (station.root / DIRTY_MARKER).exists()
    # The owner's recovery reads the same evidence the same way.
    station.vm.replies["station-verify"] = {
        "ok": True, "key_present": False, "sessions": [], "drive_mounted": False,
        "processes": {"extra": [], "tolerated": []}, "services": {"extra": []},
        "launch_agents": {"extra": []}, "crontab": {"empty": True},
    }
    assert station.recover(confirm_cleanup=True)["clean"] is True


def test_a_clean_release_still_says_what_reclaim_left_and_which_simulator_steps_failed(station):
    """Reclaiming less than hoped never dirties, so the release result is where it shows."""
    acquire(station)
    station.vm.replies["station-reclaim"] = {
        **station.vm.replies["station-reclaim"], "completed": False, "timed_out": True,
        "categories": {
            "xcode": {"count": 9, "bytes": 5 * GIB,
                      "skipped": {"errno:1": {"count": 2, "bytes": 7}}},
            "trash": {"count": 0, "bytes": 0, "skipped": {}},
        },
    }
    station.vm.replies["station-sweep"] = {
        **station.vm.replies["station-sweep"],
        "simulators": {
            "shutdown": {"completed": True, "timed_out": False, "returncode": 0},
            "default": {"completed": False, "timed_out": True, "returncode": None},
            "testing": {
                "completed": False, "timed_out": False, "returncode": 149,
                "said": "Unable to delete devices: CoreSimulatorService connection became invalid",
            },
        },
    }
    result = station.release(LEASE)
    assert result["clean"] is True and result["disk_reclaimed"] == 5 * GIB
    assert result["reclaim"] == {
        "completed": False, "timed_out": True, "refused": None,
        "categories": [
            {"category": "xcode", "count": 9, "bytes": 5 * GIB, "skipped": 2,
             "skipped_bytes": 7, "skipped_reasons": ["errno:1"]},
            {"category": "trash", "count": 0, "bytes": 0, "skipped": 0, "skipped_bytes": 0,
             "skipped_reasons": []},
        ],
    }
    # Only a step that did not complete says how it ended.
    assert result["simulators"] == [
        {"step": "shutdown", "completed": True, "timed_out": False},
        {"step": "default", "completed": False, "timed_out": True, "returncode": None,
         "said": None},
        {"step": "testing", "completed": False, "timed_out": False, "returncode": 149,
         "said": "Unable to delete devices: CoreSimulatorService connection became invalid"},
    ]


def test_a_failed_steps_output_is_kept_short_and_ascii(station):
    """The release is one journal event with a size cap; escaped output must not overrun it."""
    acquire(station)
    station.vm.replies["station-sweep"] = {
        **station.vm.replies["station-sweep"],
        "simulators": {
            "testing": {"completed": False, "timed_out": False, "returncode": 1,
                        "said": "é" * 1000 + "ends here"},
        },
    }
    (step,) = station.release(LEASE)["simulators"]
    assert len(step["said"]) == SAID_CHARACTERS and step["said"].isascii()
    assert step["said"].endswith("ends here")


def test_acquire_does_not_refuse_when_free_space_is_unknown(station):
    station.vm.failures["sample"] = "guest controller unreachable"
    assert acquire(station).lease_id == LEASE


def test_acquire_tolerates_a_failed_reclaim_and_grants_from_measured_free_space(station):
    station.vm.failures["station-reclaim"] = "guest controller unreachable"
    lease, disk, reclaimed = station.acquire(
        LEASE, "r-e1", Credentials("firmfile-abc", "secret", 4450), KEY,
        address="founder/dev", activation_id="a-1",
    )
    assert lease.lease_id == LEASE
    assert disk == {"free_bytes": 60 * GIB, "total_bytes": 100 * GIB}
    assert reclaimed is None
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["disk_reclaimed"] is None


def test_lease_scratch_comes_from_the_guest_mount_reply(station):
    station.vm.replies["station-mount"] = {
        "mounted": True, "point": "/Users/firmstation1/drive",
        "scratch": "/Users/firmstation1/scratch-fresh",
    }
    assert acquire(station).scratch == "/Users/firmstation1/scratch-fresh"


def test_reclaim_failure_leaves_the_station_dirty_though_all_four_checks_pass(station):
    acquire(station)
    station.vm.failures["station-reclaim"] = "guest controller unreachable"
    result = station.release(LEASE)
    assert result["clean"] is False
    assert all(check["ok"] for check in result["checks"].values())
    assert result["disk_reclaimed"] is None
    assert result["errors"] == ["reclaim: guest controller unreachable"]
    assert (station.root / DIRTY_MARKER).exists()
    # A later release, once the guest answers again, may still succeed.
    del station.vm.failures["station-reclaim"]
    assert station.release(LEASE)["clean"] is True


def test_reclaiming_less_than_hoped_is_reported_evidence_not_a_failure(station):
    acquire(station)
    station.vm.replies["station-reclaim"] = {
        "refused": False, "completed": False, "timed_out": True,
        "categories": {"simulators": {"count": 3, "bytes": 5 * GIB, "skipped": {}}},
        "kept": {"home": [], "entries": {}, "outside_categories": []},
        "scratch": "/Users/firmstation1/scratch",
    }
    result = station.release(LEASE)
    assert result["clean"] is True
    assert result["disk_reclaimed"] == 5 * GIB
    assert not (station.root / DIRTY_MARKER).exists()


def test_the_two_long_guest_calls_use_a_host_timeout_above_the_guests_own_bound(station):
    acquire(station)
    station.release(LEASE)
    timeouts = {op: payload["timeout"] for op, payload in station.vm.calls
                if op in {"station-sweep", "station-reclaim"} and "timeout" in payload}
    from firm_sandbox.mac_vm_guest import SWEEP_SECONDS

    # Simulator commands + one sweep deadline + two locks + transport allowance.
    assert timeouts["station-sweep"] >= 75 + SWEEP_SECONDS + 20 + 25
    assert timeouts["station-reclaim"] >= 60 + 20 + 25


@pytest.mark.parametrize(
    "failure, check",
    [
        ({"station-key-remove": {"present": True}}, "access_revoked"),
        ({"station-kill-sessions": {"remaining": [4242]}}, "access_revoked"),
        ({"station-sweep": {"ok": False, "processes": {"extra": [{"pid": 7}], "tolerated": []},
                            "services": {"extra": []}, "launch_agents": {"extra": ["x.plist"]},
                            "crontab": {"empty": True}}}, "work_stopped"),
        ({"station-unmount": {"mounted": True}}, "drive_detached"),
        ({"helper": {"ready": True, "clean": False}}, "desktop_usable"),
        ({"helper": {"environment": "not-a-station-environment"}}, "desktop_usable"),
        ({"apps": [{"pid": 9}]}, "desktop_usable"),
    ],
)
def test_each_failed_check_leaves_the_marker_and_blocks_the_next_acquire(
    station, failure, check
):
    acquire(station)
    for name, value in failure.items():
        if name == "helper":
            station.helper["state"].update(value)
        elif name == "apps":
            station.helper["apps"].extend(value)
        else:
            station.vm.replies[name] = value
    result = station.release(LEASE)
    assert result["clean"] is False
    assert result["checks"][check]["ok"] is False
    assert all(item["ok"] for name, item in result["checks"].items() if name != check)
    assert (station.root / DIRTY_MARKER).exists()
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["cleanup"]["checks"][check]["ok"] is False
    lock_is_free(station)
    status = station.status()
    assert status["held"] is False and status["dirty"] is True and status["lease_id"] == LEASE
    with pytest.raises(RuntimeError, match="verified release or owner recovery"):
        acquire(station, OTHER, "r-e2")


def test_failed_guest_step_is_reported_not_raised(station):
    acquire(station)
    station.vm.failures["station-sweep"] = "guest controller unreachable"
    result = station.release(LEASE)
    assert result["clean"] is False
    assert result["checks"]["work_stopped"]["ok"] is False
    assert result["errors"] == ["sweep: guest controller unreachable"]
    assert (station.root / DIRTY_MARKER).exists()


def test_new_work_in_final_read_retries_with_remaining_budget_and_no_simulators(
    station, monkeypatch,
):
    acquire(station)
    from firm_sandbox import mac_station

    clock = {"now": 0.0}
    monkeypatch.setattr(mac_station.time, "monotonic", lambda: clock["now"])
    clean_sweep = {**station.vm.replies["station-sweep"], "remaining_seconds": 120}
    station.vm.replies["station-sweep"] = [clean_sweep, clean_sweep]
    dirty_verify = {
        **clean_sweep, "processes": {"extra": [{"pid": 9}], "tolerated": [], "verified": True},
        "services": {"extra": [], "verified": True},
        "launch_agents": {"extra": [], "verified": True},
        "crontab": {"empty": True, "verified": True}, "ok": False,
        "key_present": False, "sessions": [], "drive_mounted": False}
    clean_verify = {**clean_sweep, "key_present": False, "sessions": [], "drive_mounted": False}
    station.vm.replies["station-verify"] = [dirty_verify, clean_verify]
    rpc = station._station_rpc

    def elapsed_assessment(operation, **payload):
        if operation == "station-verify":
            clock["now"] += 40
        return rpc(operation, **payload)

    monkeypatch.setattr(station, "_station_rpc", elapsed_assessment)
    result = station.release(LEASE)
    sweeps = [payload for operation, payload in station.vm.calls if operation == "station-sweep"]
    assert result["clean"] is True and len(result["sweeps"]) == 2
    assert len(sweeps) == 2
    assert sweeps[1]["simulators"] is False
    assert sweeps[1]["seconds"] == 80


def test_missing_reclaim_ack_blocks_even_when_final_read_is_clean(station):
    acquire(station)
    station.vm.failures["station-reclaim"] = "guest controller unreachable"
    result = station.release(LEASE)
    assert result["clean"] is False
    assert result["checks"]["work_stopped"]["ok"] is True
    assert result["reclaim"] is None
    assert len(result["sweeps"]) == 1
    assert station.status()["dirty"] is True


def test_session_cleanup_incomplete_ack_blocks_a_clean_final_read(station):
    acquire(station)
    station.vm.replies["station-kill-sessions"] = {
        "remaining": [], "cleanup_incomplete": True,
    }
    result = station.release(LEASE)
    assert result["clean"] is False
    assert result["checks"]["access_revoked"]["ok"] is False
    assert result["checks"]["access_revoked"]["cleanup_incomplete"] is True
    assert result["verify"]["sessions"] == []


def test_transient_sweep_diagnostic_is_superseded_by_clean_final_read(station):
    acquire(station)
    station.vm.replies["station-sweep"] = {
        **station.vm.replies["station-sweep"],
        "ok": False,
        "services": {"ok": False, "verified": False, "extra": [],
                     "error": "temporary launchd read stall"},
    }
    station.vm.replies["station-verify"] = {
        **station.vm.replies["station-sweep"],
        "ok": True, "services": {"ok": True, "verified": True, "extra": []},
        "key_present": False, "sessions": [], "drive_mounted": False,
    }
    result = station.release(LEASE)
    assert result["clean"] is True
    assert result["errors"] == ["sweep.services: temporary launchd read stall"]
    assert result["checks"]["work_stopped"]["ok"] is True
    assert not (station.root / DIRTY_MARKER).exists()


def test_the_sandbox_revocation_hook_is_part_of_access_revoked(tmp_path, monkeypatch):
    root = tmp_path / "hooked"
    root.mkdir(mode=0o700)
    revoked = []
    outcome = {"tunnel_closed": True}
    station = MacStation(FakeVM(root, tmp_path), 1, revoke=lambda lease_id: (
        revoked.append(lease_id) or outcome
    ))
    monkeypatch.setattr(station, "_relay", lambda binding, **options: FakeRelay(
        {"ready": True, "clean": True, "environment": "vm-session"}, []
    ))
    acquire(station)
    result = station.release(LEASE)
    assert revoked == [LEASE] and result["clean"] is True
    assert result["checks"]["access_revoked"]["tunnel_closed"] is True
    acquire(station, OTHER)
    outcome["tunnel_closed"] = False
    result = station.release(OTHER)
    assert result["clean"] is False and result["checks"]["access_revoked"]["ok"] is False
    assert result["checks"]["access_revoked"]["tunnel_closed"] is False
    station.vm.replies["station-verify"] = {
        **station.vm.replies["station-sweep"],
        "key_present": False, "sessions": [], "drive_mounted": False,
    }
    assert station.recover(confirm_cleanup=True)["clean"] is False
    outcome["tunnel_closed"] = True
    assert station.recover(confirm_cleanup=True)["clean"] is True
    assert revoked == [LEASE, OTHER, OTHER, OTHER]


def test_failure_after_a_side_effect_keeps_the_record_and_marker(station):
    station.vm.failures["station-key-install"] = "sshd key directory missing"
    with pytest.raises(RuntimeError, match="key directory"):
        acquire(station)
    record = json.loads((station.root / "leases" / f"{LEASE}.json").read_text())
    assert record["steps"] == ["drive_mounted"]
    assert (station.root / DIRTY_MARKER).exists()
    assert station.status()["held"] is False and station.status()["dirty"] is True
    lock_is_free(station)
    with pytest.raises(RuntimeError, match="verified release"):
        acquire(station, OTHER)
    # Release by the failed ID runs the cleanup and reopens the station.
    del station.vm.failures["station-key-install"]
    assert station.release(LEASE)["clean"] is True
    assert acquire(station, OTHER).lease_id == OTHER


def test_dirty_marker_alone_blocks_acquire(station):
    marker = station.root / DIRTY_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)  # a station's records start at first use
    marker.write_bytes(DIRTY_MARKER_CONTENT)
    marker.chmod(0o600)
    with pytest.raises(RuntimeError, match="verified release"):
        acquire(station)
    assert station.vm.calls == []
    assert station.status()["dirty"] is True


def test_recovery_verifies_all_four_and_terminates_nothing(station):
    acquire(station)
    station.vm.replies["station-unmount"] = {"mounted": True}
    assert station.release(LEASE)["clean"] is False
    verify = {
        **station.vm.replies["station-sweep"],
        "key_present": False, "sessions": [], "drive_mounted": True,
    }
    station.vm.replies["station-verify"] = verify
    with pytest.raises(ValueError, match="confirm"):
        station.recover()
    before_recovery = len(station.vm.calls)
    result = station.recover(confirm_cleanup=True)
    assert result["recovered"] is False and result["checks"]["drive_detached"]["ok"] is False
    assert (station.root / DIRTY_MARKER).exists()
    # A station not yet verified stopped is never reclaimed.
    assert "station-reclaim" not in [
        call[0] for call in station.vm.calls[before_recovery:]
    ]
    station.vm.replies["station-verify"] = {**verify, "drive_mounted": False}
    before_second = len(station.vm.calls)
    result = station.recover(confirm_cleanup=True)
    assert result["status"] == "ok" and result["clean"] is True and result["recovered"] is True
    assert result["disk_reclaimed"] == 0
    assert [call[0] for call in station.vm.calls if call[0] == "station-verify"] == [
        "station-verify", "station-verify", "station-verify",
    ]
    # Once verified stopped, this second recovery reclaims before it clears.
    assert "station-reclaim" in [call[0] for call in station.vm.calls[before_second:]]
    assert not any(call[0] in {"station-sweep", "station-kill-sessions"}
                   for call in station.vm.calls[-2:])
    assert not (station.root / DIRTY_MARKER).exists()
    assert station.status() == {
        "configured": True, "station": 1, "enrolled": True, "held": False, "lease_id": None,
        "holder": None, "since": None, "dirty": False,
    }
    assert station.recover(confirm_cleanup=True) == {
        "status": "ok", "clean": True, "recovered": False,
    }


def test_recovery_lets_the_helper_verify_what_an_earlier_lease_left(station, monkeypatch):
    """A helper ledger naming leftover apps clears through its release, never a kill."""
    acquire(station)
    helper = {"running": True, "cleared": False}

    class LedgerRelay(FakeRelay):
        def request(self, op, **arguments):
            self.calls.append(op)
            if op == "release":
                if helper["running"]:
                    return {"status": "unavailable", "reason": "operation_failed",
                            "message": "applications are still running: pid 935 TextEdit"}
                helper["cleared"] = True
                return {"clean": True, "status": "ok"}
            if op == "status":
                left = [] if helper["cleared"] else ["earlier helper lease left applications"]
                return {
                    "ready": not left,
                    "clean": not left,
                    "environment": "vm-session",
                    "missing": left,
                    "status": "ok",
                }
            return {"apps": [], "status": "ok"}

    relays = []
    monkeypatch.setattr(station, "_relay", lambda binding, **options: (
        relays.append(LedgerRelay({}, [])) or relays[-1]
    ))
    result = station.release(LEASE)
    desktop = result["checks"]["desktop_usable"]
    assert result["clean"] is False and desktop["ok"] is False
    assert desktop["release"] == "applications are still running: pid 935 TextEdit"
    station.vm.replies["station-verify"] = {
        **station.vm.replies["station-sweep"],
        "key_present": False, "sessions": [], "drive_mounted": False,
    }
    assert station.recover(confirm_cleanup=True)["recovered"] is False
    assert (station.root / DIRTY_MARKER).exists()
    # The owner stopped the app; recovery's helper release now verifies and clears it.
    helper["running"] = False
    result = station.recover(confirm_cleanup=True)
    assert result["recovered"] is True and result["checks"]["desktop_usable"]["ok"] is True
    assert relays[-1].calls == ["release", "status", "list_apps"]
    assert not any(call[0] in {"station-sweep", "station-kill-sessions"}
                   for call in station.vm.calls[-2:])
    assert not (station.root / DIRTY_MARKER).exists()


def test_recovery_requires_the_running_vm_behind_its_network_boundary(station):
    station.vm.running = False
    with pytest.raises(RuntimeError, match="Start the isolated VM"):
        station.recover(confirm_cleanup=True)
    station.vm.running = True
    station.vm.network = SimpleNamespace(ready=lambda: False)
    with pytest.raises(RuntimeError, match="Start the isolated VM"):
        station.recover(confirm_cleanup=True)
    assert station.vm.calls == []


def test_recovery_refuses_while_the_lease_is_held(station):
    acquire(station)
    with pytest.raises(OSError, match="already leased"):
        station.recover(confirm_cleanup=True)


def test_recovery_takes_the_shared_lifecycle_lock(station, monkeypatch):
    from firm_sandbox import guest_control

    acquired = []

    @contextmanager
    def lifecycle_locked(locks, *, exclusive=False):
        acquired.append((locks, exclusive))
        yield

    monkeypatch.setattr(guest_control, "lifecycle_locked", lifecycle_locked)
    assert station.recover(confirm_cleanup=True)["recovered"] is False
    assert acquired == [(station.firm_locks, False)]


def test_connect_requires_the_held_lease_and_binds_it(station, monkeypatch):
    with pytest.raises(OSError, match="not held"):
        station.connect(LEASE)
    acquire(station)

    bound = {"lease_bound": True, "status": "ok"}

    class Relay(FakeRelay):
        def bind_lease(self):
            self.calls.append("bind_lease")
            return bound

    connections = []
    state = {"ready": True, "clean": True, "environment": "vm-session"}

    def relay(binding, *, recovery_only=False):
        connection = Relay(state, [])
        connections.append((binding, recovery_only, connection))
        return connection

    monkeypatch.setattr(station, "_relay", relay)
    connection = station.connect(LEASE)
    assert connection.calls == ["status", "bind_lease"]
    binding, recovery_only, _ = connections[0]
    # The binding names the lease, so a later connection adopts this lease's
    # tracked applications.
    assert binding == {"firm_id": "test-firm", "role_id": "r-e1", "lease_id": LEASE}
    assert recovery_only is False
    # A helper holding another lease's applications refuses the binding, in its words.
    bound = {
        "status": "unavailable",
        "reason": "not_ready",
        "message": "another lease's applications are still open (pid 9 /Applications/A)",
    }
    with pytest.raises(OSError, match="another lease's applications"):
        station.connect(LEASE)
    assert connections[-1][2].closed
    bound = {"lease_bound": True, "status": "ok"}
    with pytest.raises(OSError, match="not held"):
        station.connect(OTHER)
    # Relay connections never touch the lease or its lock.
    assert station.status()["held"] is True
    state.update(ready=False, missing=["Accessibility permission is missing"])
    with pytest.raises(OSError, match="Accessibility"):
        station.connect(LEASE)
    assert connections[-1][2].closed


def test_station_lock_excludes_another_host_process(station):
    acquire(station)
    other = MacStation(station.vm, 1, revoke=tunnel_closed)
    with pytest.raises(OSError, match="already leased"):
        acquire(other, OTHER)


def test_each_station_locks_and_publishes_its_own_lease_endpoint(tmp_path, monkeypatch):
    """Station 2's lock and lease port are its own, so a lease on 1 does not reach it."""
    from firm_sandbox.mac_station import lease_ssh_port, station_lock

    assert (lease_ssh_port(1), lease_ssh_port(2)) == (2222, 2223)
    for invalid in (0, 9, True, "1"):
        with pytest.raises(ValueError):
            lease_ssh_port(invalid)
    root = tmp_path / "vm2"
    root.mkdir(mode=0o700)
    vm = FakeVM(root, tmp_path)
    vm.replies["station-session-ensure"] = {
        "station": 2, "user": "firmstation2", "uid": 503,
        "ready": True, "created": False, "helper": {"ready": True},
    }
    vm.replies["station-mount"] = {
        "mounted": True, "point": "/Users/firmstation2/drive",
        "scratch": "/Users/firmstation2/scratch",
    }
    second = MacStation(vm, 2, revoke=tunnel_closed)
    monkeypatch.setattr(second, "_relay", lambda binding, **options: FakeRelay(
        {"ready": True, "clean": True, "environment": "vm-session"}, []
    ))
    held = station_lock(second.firm_locks, 1)
    try:
        lease = acquire(second, OTHER, "r-e2")
    finally:
        os.close(held)
    assert (lease.station, lease.ssh_port, lease.user, lease.drive) == (
        2, 2223, "firmstation2", "/Users/firmstation2/drive",
    )
    assert second.root == root / "stations" / "2"
    assert second.status()["station"] == 2
    with (second.firm_locks / "mac-station-1").open() as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_station_rejects_symlink_lock(tmp_path):
    (tmp_path / "locks").mkdir(mode=0o700)
    (tmp_path / "locks" / "mac-station-1").symlink_to(tmp_path / "victim")
    (tmp_path / "vm").mkdir(mode=0o700)
    vm = FakeVM(tmp_path / "vm", tmp_path)
    with pytest.raises(OSError):
        acquire(MacStation(vm, 1, revoke=tunnel_closed))
    assert not (tmp_path / "victim").exists()


@pytest.mark.parametrize("seconds", [0, -1, 3601, True, 1.5])
def test_station_rejects_unbounded_deadline(station, seconds):
    with pytest.raises(ValueError):
        MacStation(station.vm, 1, revoke=tunnel_closed, relay_seconds=seconds)


def test_relay_argv_keeps_control_account_keys_and_sudo_out_of_the_guest(tmp_path, monkeypatch):
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    station = MacStation(FakeVM(root, tmp_path), 1, revoke=tunnel_closed)
    captured = []

    class Connected:
        def __init__(self, argv, binding, seconds, **options):
            captured.append((argv, binding, options))

        def wait_connected(self):
            pass

        def request(self, op, **arguments):
            return {"status": "ok", "ready": True, "clean": True, "environment": "vm-session"}

        def close(self):
            pass

    monkeypatch.setattr("firm_sandbox.mac_station.StationConnection", Connected)
    station.probe()
    argv, binding, options = captured[0]
    assert "--verify-peer" in argv[-1]
    assert "/usr/local/firm/bin/python3 -I -c" in argv[-1]
    assert argv[-1].endswith(" 1")
    assert "lima-mac" in argv and "sudo" not in " ".join(argv)
    assert argv[argv.index("-F") + 1] == str(root / "lima" / "mac" / "ssh.config")
    # One persistent connection in the Mac's runtime carries every relay as a channel.
    assert "-oControlMaster=auto" in argv and "-oControlPersist=yes" in argv
    assert f"-oControlPath={root / 'lima' / 'mac' / 'relay.sock'}" in argv
    assert options == {"recovery_only": True}
    assert binding == {
        "firm_id": "test-firm", "role_id": "owner-probe", "lease_id": "owner-probe",
    }


def test_no_relay_is_opened_through_a_runtime_the_mac_refuses(tmp_path, monkeypatch):
    """ssh.config is in the Mac's runtime; a helper there may be another firm's."""
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    station = MacStation(FakeVM(root, tmp_path), 1, revoke=tunnel_closed)
    opened = []

    def refuse():
        raise RuntimeError("Short VM runtime is owned by another firm")

    station.vm.paths.verify_runtime = refuse
    monkeypatch.setattr(
        "firm_sandbox.mac_station.StationConnection", lambda *a, **k: opened.append(a)
    )
    with pytest.raises(RuntimeError, match="owned by another firm"):
        station.probe()
    assert opened == []


# ---- the relay connection -----------------------------------------------------------


def connection(script, *, seconds=30, recovery_only=False):
    binding = {"firm_id": "test-firm", "role_id": "designer", "lease_id": LEASE}
    return StationConnection(
        [sys.executable, "-u", "-c", script], binding, seconds, recovery_only=recovery_only
    )


def test_bounded_authenticated_binding_and_keyboard():
    script = """
import sys,json
for line in sys.stdin:
    data=json.loads(line)
    print(json.dumps({'id':data['id'],'ok':True,'result':{'received':data}}),flush=True)
"""
    client = connection(script)
    try:
        result = client.bind_lease()
        assert result["received"]["op"] == "bind_lease"
        assert result["received"]["duration_seconds"] == 30
        assert result["received"]["firm_id"] == "test-firm"
        # The helper keeps this lease's binding; the deadline is the connection's.
        assert result["received"]["lease_id"] == LEASE
        result = client.request("press_key", pid=12, key_code=0, modifiers=["command"])
        assert result["received"]["op"] == "press_key"
        for changed in ({"firm_id": "other-firm"}, {"lease_id": OTHER}):
            with pytest.raises(ValueError, match="binding"):
                client.request("bind_lease", **{**client.binding, **changed})
        with pytest.raises(ValueError, match="operation"):
            client.request("status", id="forged")
        with pytest.raises(ValueError, match="64 KiB"):
            client.request("type_text", pid=12, text="x" * 65536)
        assert client.request("status")["status"] == "ok"
        assert client.usable()
    finally:
        client.close()
    assert client.process.poll() == 0
    assert not client.usable()
    client.close()


@pytest.mark.parametrize(
    "reply",
    [
        "{'id':'wrong','ok':True,'result':{}}",
        "{'id':data['id'],'ok':1,'result':{}}",
        "{'id':data['id'],'ok':True,'result':[]}",
    ],
)
def test_bad_reply_closes_connection(reply):
    client = connection(
        f"""
import sys,json
for line in sys.stdin:
    data=json.loads(line)
    print(json.dumps({reply}),flush=True)
""",
    )
    with pytest.raises(OSError, match="outcome may be unknown"):
        client.request("status")
    assert client.closed
    assert client.process.poll() is not None


def test_helper_rejection_is_reported_as_unavailable():
    client = connection(
        """
import sys,json
for line in sys.stdin:
    data=json.loads(line)
    print(json.dumps({'id':data['id'],'ok':False,
      'error':{'code':'not_ready','message':'dirty process ledger'}}),flush=True)
""",
    )
    try:
        assert client.request("status") == {
            "status": "unavailable",
            "reason": "not_ready",
            "message": "dirty process ledger",
        }
    finally:
        client.close()


def test_connection_expires_even_without_requests():
    client = connection("import sys; sys.stdin.read()", seconds=1)
    client.timer.join(timeout=3)
    assert not client.timer.is_alive()
    assert client.closed
    assert client.process.poll() == 0


def test_request_cannot_outlast_the_connection_deadline():
    client = connection("import sys; sys.stdin.read()", seconds=1)
    with pytest.raises(OSError, match="deadline"):
        client.request("launch_app", path="/Applications/Test.app")
    assert client.closed


def test_recovery_connection_accepts_only_status_apps_or_release():
    client = connection("import sys; sys.stdin.read()", recovery_only=True)
    try:
        for op, arguments in (
            ("launch_app", {"path": "/Applications/Test.app"}),
            ("bind_lease", client.binding),
        ):
            with pytest.raises(ValueError, match="Recovery connection"):
                client.request(op, **arguments)
    finally:
        client.close()


def test_connected_relay_answers_after_its_announcement():
    client = connection("""
import sys,json
print(json.dumps({'relay':'connected'}),flush=True)
for line in sys.stdin:
    data=json.loads(line)
    print(json.dumps({'id':data['id'],'ok':True,'result':{}}),flush=True)
""")
    try:
        client.wait_connected()
        assert client.request("status")["status"] == "ok"
    finally:
        client.close()


def test_relay_that_ends_before_connecting_reports_what_it_said():
    client = connection(
        "import sys; print('ssh: connect to host lima-mac: refused', file=sys.stderr); exit(255)"
    )
    with pytest.raises(RelayNotConnected, match="unreachable.*lima-mac: refused"):
        client.wait_connected()
    assert client.closed


def test_relay_answering_before_connecting_is_not_connected():
    client = connection("""
import sys,json
print(json.dumps({'id':'x','ok':True,'result':{}}),flush=True)
sys.stdin.read()
""")
    with pytest.raises(RelayNotConnected, match="did not announce"):
        client.wait_connected()


def test_relay_connection_is_made_again_after_a_connection_failure(tmp_path, monkeypatch):
    """Nothing has been asked of the helper before the relay connects."""
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    station = MacStation(FakeVM(root, tmp_path), 1, revoke=tunnel_closed)
    attempts = []

    class Flaky:
        def __init__(self, argv, binding, seconds, **options):
            attempts.append(options)

        def wait_connected(self):
            if len(attempts) < 3:
                raise RelayNotConnected("Station helper unreachable")

        def close(self):
            pass

    monkeypatch.setattr("firm_sandbox.mac_station.StationConnection", Flaky)
    monkeypatch.setattr("firm_sandbox.mac_station.time.sleep", lambda seconds: None)
    assert isinstance(station.probe(), Flaky)
    assert len(attempts) == 3

    class Down(Flaky):
        def wait_connected(self):
            raise RelayNotConnected("Station helper unreachable")

    attempts.clear()
    monkeypatch.setattr("firm_sandbox.mac_station.StationConnection", Down)
    with pytest.raises(RelayNotConnected):
        station.probe()
    assert len(attempts) == 3


def test_station_uses_the_normal_dated_firm_identifier_contract(tmp_path):
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    vm = FakeVM(root, tmp_path)
    vm.settings.firm_id = "2026-09-14_mac-firm"
    assert MacStation(vm, 1, revoke=tunnel_closed).firm_id == "2026-09-14_mac-firm"
    vm.settings.firm_id = "different__contract"
    with pytest.raises(ValueError):
        MacStation(vm, 1, revoke=tunnel_closed)


def test_generate_key_pair_never_leaves_the_private_key_on_disk(tmp_path, monkeypatch):
    from firm_sandbox import mac_station

    seen = {}

    def keygen(argv, **kwargs):
        path = argv[argv.index("-f") + 1]
        seen["directory"] = os.path.dirname(path)
        assert os.stat(seen["directory"]).st_mode & 0o777 == 0o700
        assert argv[argv.index("-t") + 1] == "ed25519"
        with open(path, "wb") as stream:
            stream.write(b"PRIVATE")
        with open(path + ".pub", "w") as stream:
            stream.write(KEY + "\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mac_station.subprocess, "run", keygen)
    assert mac_station.generate_key_pair() == (b"PRIVATE", KEY)
    assert not os.path.exists(seen["directory"])


@pytest.mark.parametrize("operation", ["station-verify", "station-session-reset"])
def test_owner_failed_reads_are_structured_and_retain_dirty_lease(station, operation):
    acquire(station)
    station.vm.failures["station-sweep"] = "unreadable services"
    assert station.release(LEASE)["clean"] is False
    del station.vm.failures["station-sweep"]
    station.vm.replies["station-verify"] = {
        **station.vm.replies["station-sweep"],
        "key_present": False, "sessions": [], "drive_mounted": False,
    }
    station.vm.failures[operation] = "transport lost"
    if operation == "station-verify":
        result = station.recover(confirm_cleanup=True)
        assert result["clean"] is False
        for name in ("processes", "services", "launch_agents", "crontab"):
            assert result["checks"]["work_stopped"][name]["error"] == "transport lost"
        result = station.session_reset()
        assert result["cleanup"]["verify"]["services"]["error"] == "transport lost"
    else:
        result = station.session_reset()
        assert result["checks"]["session_reset"]["error"] == "transport lost"
    assert result["cleanup"]["clean"] is False
    assert (station.root / DIRTY_MARKER).exists()
    assert station._record_path(LEASE).exists()
