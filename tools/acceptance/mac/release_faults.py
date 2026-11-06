"""Lose the host's connection to the Mac during each guest call of a release.

A release is clean only when the host heard every answer its checks rest on. For seven
calls a release makes through the guest controller (key removal, session kill, sweep,
unmount, reclaim, final verification, command log), this takes a lease from a real Linux
holder, kills that one call's connection shortly after it starts, and requires what the
lease promises: the release names the step it lost; a lost key removal, session kill,
sweep, unmount, reclaim or final verification leaves the station dirty and refuses the
next role until it is cleared; and a lost command log, which is evidence and not a
check, still releases clean. What clears a dirty station depends on whether the lost
call ran in the guest anyway. If it did, the four checks hold and owner recovery clears
it. If it did not, recovery refuses, because it verifies and terminates nothing, and
releasing the same lease again does the cleanup. The guest finishes or abandons the call
by itself; nothing here reaches into it. Not covered: the helper's relay, which is a
different connection, and the closing disk sample, which like the log is evidence.

An additional phase makes the station's LaunchAgents directory unreadable after
acquisition. Its failed read must leave the station dirty while process cleanup still
stops a background sleeper; restoring the directory permits owner recovery.

Requires an already running disposable firm with an enrolled, unlocked Mac, free and
clean. Leaves the VMs running. Run from the repository root with `uv run`.
"""

import json
import os
import shlex
import signal
import subprocess
import threading
import time

from acceptance_common import (
    Probe,
    acceptance_parser,
    cleanup_probe,
    control_probe,
    select_station,
    station_status,
    write_json,
)
from firm_sandbox import Config, Sandbox, guest_control
from firm_sandbox.commands import mac_sudo_command
from lease_cleanup import CONTENDER, ROLE

# Forty thousand files in the station's caches, so that reclaim runs for seconds and its
# connection dies while the guest is still deleting, not before the request arrived.
MANY_FILES = "/usr/local/firm/bin/python3 -I -c " + shlex.quote(
    "import pathlib\n"
    "root = pathlib.Path.home() / 'Library/Caches/lease-probe-many'\n"
    "for d in range(200):\n"
    "    (root / str(d)).mkdir(parents=True, exist_ok=True)\n"
    "    for f in range(200):\n"
    "        (root / str(d) / str(f)).write_bytes(b'x')\n"
    "print('lease-probe-many-files')\n"
)
# Tried in turn until the connection dies before its call has answered. The quick calls
# answer within a fifth of a second, so theirs mostly dies before the host hears anything,
# though the guest has usually run the call by then.
DELAYS = (0.5, 0.2, 0.05)
# Killed the moment it starts, so the request is never sent and the guest never runs it.
NEVER_SENT = (0.0,)
# The guest call, the release step that reports losing it, whether losing it dirties, what
# the holder does on the Mac first, and when the connection dies.
PHASES = (
    ("station-key-remove", "key", True, None, DELAYS),
    ("station-kill-sessions", "sessions", True, None, DELAYS),
    ("station-sweep", "sweep", True, None, DELAYS),
    ("station-unmount", "drive", True, None, DELAYS),
    ("station-reclaim", "reclaim", True, MANY_FILES, DELAYS),
    ("station-verify", "verify", True, None, DELAYS),
    ("station-command-log", "log", False, None, DELAYS),
    # The key is then still installed, so recovery must refuse and a second release clean.
    ("station-key-remove", "key", True, None, NEVER_SENT),
)


class LostConnection:
    """Stands in for `subprocess` inside guest_control and kills one named call's transport."""

    def __init__(self):
        self.operation = self.current = None
        self.delay = DELAYS[0]
        self.killed = False

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, *args, **kwargs):  # the name guest_control calls
        process = subprocess.Popen(*args, **kwargs)
        if self.operation is not None and self.current == self.operation:
            threading.Timer(self.delay, self.kill, [process]).start()
        return process

    def kill(self, process):
        if process.poll() is not None:
            return
        try:
            # guest_control starts each call in its own session, so this is the whole transport.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self.killed = True


def watch(vm, fault):
    """Tell the fault which guest call the Mac VM is making."""
    real = vm._rpc

    def rpc(operation, **payload):
        fault.current = operation
        try:
            return real(operation, **payload)
        finally:
            fault.current = None

    vm._rpc = rpc


def lose(firm, probe, fault, station, contender, operation, step, dirties, prepare, delays):
    """One lease whose release loses `operation`; returns what happened."""
    item = {"operation": operation, "step": step, "attempts": []}
    lost = False
    for delay in delays:
        probe.acquire(f"lose-{step}")
        lease_id = probe.lease["lease_id"]
        probe.command(f"printf 'lease-probe-lose-{step}\\n'")
        if prepare is not None:
            probe.command(prepare)
        fault.operation, fault.delay, fault.killed = operation, delay, False
        started = time.monotonic()
        try:
            released = firm.mac_release(lease_id)
        finally:
            fault.operation = None
        probe.lease = None
        # What the release says decides whether the answer was lost: a signal can land on
        # a connection that had just delivered it, and that is an ordinary release.
        lost = any(error.startswith(step + ":") for error in released["errors"])
        item["attempts"].append({
            "delay": delay, "killed": fault.killed, "lost": lost, "clean": released["clean"],
            "release_seconds": round(time.monotonic() - started, 1),
            "errors": released["errors"],
            "checks": {name: check["ok"] for name, check in released["checks"].items()},
        })
        if lost:
            break
        assert released["clean"], released
    assert lost, f"{operation} always answered before its connection could be killed"
    if not dirties:
        assert released["clean"] is True, released
        return item
    assert released["clean"] is False, released
    assert station_status(firm, station)["dirty"] is True
    try:
        unexpected = firm.mac_acquire(contender, f"refused-after-{step}")
    except (RuntimeError, OSError) as exc:
        item["refused"] = str(exc)
        # With another station leased the refusal is busy and names a leased station,
        # never the dirty one; otherwise it asks for recovery.
        another_busy = (
            len(firm.mac.stations) > 1
            and "already leased" in str(exc)
            and f"Mac station {station} " not in str(exc)
        )
        assert "verified release or owner recovery" in str(exc) or another_busy, item["refused"]
    else:
        # A firm with another free station serves the contender there; the dirty station
        # itself must never grant a lease.
        elsewhere = firm.mac_release(unexpected["lease_id"])
        assert unexpected["station"] != station, "A second role acquired the dirty station"
        assert elsewhere["clean"], elsewhere
        item["served_elsewhere"] = unexpected["station"]
    # Recovery verifies and terminates nothing. When the lost call had run in the guest
    # anyway, the four checks hold and recovery clears the station. When it never ran (the
    # key still installed, the drive still mounted) recovery refuses, rightly, and what
    # clears the station is the cleanup itself: releasing the same lease again.
    item["recovery"] = firm.mac_station(station).recover(confirm_cleanup=True)
    if not item["recovery"]["clean"]:
        assert station_status(firm, station)["dirty"] is True
        item["second_release"] = firm.mac_release(lease_id)
        assert item["second_release"]["clean"], item["second_release"]
    assert delays is not NEVER_SENT or "second_release" in item, (
        "A call that was never sent cannot have run, yet recovery cleared the station"
    )
    assert station_status(firm, station)["dirty"] is False
    return item


def unreadable_agents(firm, probe, station):
    """Inject a real account permission failure; no controller replacement or test hook."""
    acquired = probe.acquire("unreadable-agents")
    lease = acquired["lease"]
    directory = f"/Users/{lease['user']}/Library/LaunchAgents"
    probe.command("mkdir -p ~/Library/LaunchAgents")
    # The directory is the station's, and chmod does not follow any entries inside it.
    before = control_probe(firm, """import json, os, stat, sys
path = sys.argv[1]
print(json.dumps({'mode': stat.S_IMODE(os.stat(path).st_mode)}))
""", directory, user=lease["user"])
    item = {"operation": "unreadable-launch-agents"}
    try:
        started = probe.command("nohup /bin/sleep 600 </dev/null >/dev/null 2>&1 & "
                                "printf 'lease-probe-unreadable-agents %s\\n' $!")
        pid = int(started["stdout"].strip().split()[-1])
        processes = control_probe(firm, """import json, subprocess
print(json.dumps(subprocess.check_output(['/bin/ps', '-axo', 'uid=,pid=,lstart=,comm='],
                                         text=True).splitlines()))
""")
        sleepers = [line.split(None, 7) for line in processes
                    if line.split() and int(line.split()[0]) == 501 + station
                    and int(line.split()[1]) == pid]
        assert sleepers, processes
        command = mac_sudo_command("/usr/bin/sudo", "-H", "-u", lease["user"],
                                   "/bin/chmod", "000", directory)
        firm.mac._shell("/bin/zsh", "-c", command)
        released = firm.mac_release(lease["lease_id"])
        probe.lease = None
        item["release"] = released
        work = released["checks"]["work_stopped"]
        assert released["clean"] is False, released
        assert work["launch_agents"]["verified"] is False, work
        assert work["launch_agents"]["error"], work
        assert work["processes"]["ok"] is True, work
        after = control_probe(firm, """import json, subprocess
print(json.dumps(subprocess.check_output(['/bin/ps', '-axo', 'uid=,pid=,lstart=,comm='],
                                         text=True).splitlines()))
""")
        live = {tuple(line.split(None, 7)[:7]) for line in after}
        assert all(tuple(row[:7]) not in live for row in sleepers), after
        assert station_status(firm, station)["dirty"] is True
        item["failed_recovery"] = firm.mac_station(station).recover(confirm_cleanup=True)
        assert item["failed_recovery"]["clean"] is False, item
        assert item["failed_recovery"]["checks"]["work_stopped"]["launch_agents"]["error"]
    finally:
        command = mac_sudo_command("/usr/bin/sudo", "-H", "-u", lease["user"],
                                   "/bin/chmod", format(before["mode"], "o"), directory)
        firm.mac._shell("/bin/zsh", "-c", command)
    item["recovery"] = firm.mac_station(station).recover(confirm_cleanup=True)
    assert item["recovery"]["clean"] is True, item
    assert station_status(firm, station)["dirty"] is False
    return item


def leftover(firm, station, roles):
    """Release a lease this check's roles still hold, then say what state the station is in.

    `mac_acquire` can grant and then raise before its caller has the lease, so the station
    is asked, not the caller's own variables.
    """
    errors = []
    status = station_status(firm, station)
    if status["held"] and status["holder"] in roles and status["lease_id"]:
        try:
            if not firm.mac_release(status["lease_id"]).get("clean"):
                errors.append("A lease this check left held did not release clean")
        except Exception as exc:
            errors.append(f"Releasing a lease this check left held: {exc}")
    return errors


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(firm, args.station)
    role = f"{ROLE}-{args.station}"
    contender_role = f"{CONTENDER}-{args.station}"
    if not firm.linux._running() or not firm.mac._running():
        raise SystemExit("Start a disposable firm with an enrolled Mac before this check")
    before = station_status(firm, args.station)
    if before["held"] or before["dirty"]:
        raise SystemExit("The station must be free and clean before this check")
    # The contender may be served on any other station, so every station is watched.
    dirty_before = {number: station_status(firm, number)["dirty"] for number in firm.mac.stations}
    vm = firm.mac
    assert firm.mac_station(args.station).vm is vm
    fault = LostConnection()
    guest_control.subprocess = fault
    watch(vm, fault)
    probe = Probe(firm, args.evidence, args.station, role=role)
    contender = None
    result = {"passed": False, "phases": []}
    failure = None
    try:
        probe.start()
        # A role with its own run: with another station free the contender is served
        # there, and the lease's key is written into that run's credentials.
        contender = Probe(firm, args.evidence, args.station, role=contender_role)
        contender.start()
        for phase in PHASES:
            result["phases"].append(
                lose(firm, probe, fault, args.station, contender_role, *phase)
            )
        result["phases"].append(unreadable_agents(firm, probe, args.station))
        probe.acquire("after-faults")
        probe.command("printf lease-probe-after-faults")
        probe.release()
        result["passed"] = True
    except BaseException as exc:
        failure = exc
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        fault.operation = None
        cleanup_errors = []
        if probe.lease is not None:
            try:
                result["final_release"] = firm.mac_release(probe.lease["lease_id"])
            except Exception as exc:
                cleanup_errors.append(str(exc))
        for number in sorted(firm.mac.stations):
            cleanup_errors += leftover(firm, number, (role, contender_role))
        if station_status(firm, args.station)["dirty"]:
            try:
                result["final_recovery"] = firm.mac_station(args.station).recover(
                    confirm_cleanup=True
                )
                if not result["final_recovery"]["clean"] and probe.records:
                    # Recovery terminates nothing; the cleanup a lost call never did is
                    # the release's, so the last lease is released once more.
                    result["final_second_release"] = firm.mac_release(
                        probe.records[-1]["lease_id"]
                    )
            except Exception as exc:
                cleanup_errors.append(str(exc))
        for number in sorted(firm.mac.stations):
            final = station_status(firm, number)
            ours = final["held"] and final["holder"] in (role, contender_role)
            if ours or (final["dirty"] and not dirty_before[number]):
                cleanup_errors.append(
                    f"Mac station {number} is left held={final['held']} "
                    f"dirty={final['dirty']}"
                )
        cleanup_errors += cleanup_probe(firm, probe, "holder.log")
        cleanup_errors += cleanup_probe(firm, contender)
        if cleanup_errors:
            result["passed"] = False
            result["cleanup_errors"] = cleanup_errors
        write_json(args.evidence / "result.json", result)
    print(json.dumps({"passed": result["passed"], "evidence": str(args.evidence)}))
    if failure is not None:
        raise failure
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
