"""Exercise per-account reset, shared-machine lifecycle and failed-session admission.

Use a disposable enrolled firm with at least two stations, all free and clean.
--station N names the account reset and temporarily disabled; another account
keeps working through the reset. Runs the actual mac-session-reset CLI, overlaps
acquire/release/reset/up, checks machine refusals, then restarts and cold boots.
Faults disable only that account's helper and hold a bounded guest account lock;
finally paths restore the helper and release leases. Leaves both VMs running.
"""

import json
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

from acceptance_common import (
    Probe,
    acceptance_parser,
    select_station,
    station_holders,
    station_uid,
    write_json,
)
from firm_sandbox import Config, Sandbox
from firm_sandbox.commands import mac_sudo_command
from firm_sandbox.mac_enrollment import console_user
from firm_sandbox.mac_station import StationBusy
from firm_sandbox.mac_vm import PYTHON


def cli(box, *arguments, timeout=600):
    started = time.monotonic()
    process = subprocess.run(
        [sys.executable, "-m", "firm_sandbox.cli", "--config",
         str(box.config.root / "sandbox.toml"), *arguments],
        capture_output=True, text=True, timeout=timeout,
    )
    return {"arguments": arguments, "returncode": process.returncode,
            "stdout": process.stdout, "stderr": process.stderr,
            "seconds": round(time.monotonic() - started, 3)}


def ready(report, stations):
    rows = report["stations"]
    assert {row["station"] for row in rows} == set(stations), rows
    for row in rows:
        assert row["ready"] and row["uid"] == station_uid(row["station"]), row
        helper = row["helper"]
        assert helper["environment"] == "vm-session" and helper["ready"], helper
        assert helper["session"]["gui_uid"] == row["uid"], helper
        assert helper["session"]["login_done"] and not helper["session"]["screen_locked"], helper


def work(box, holder):
    """Exercise both the live lease endpoint and its authenticated helper."""
    lease = holder.lease
    command = shlex.join([PYTHON, "-I", "-c", r'''
import hashlib,json,os,sys
from pathlib import Path
assert os.getuid() == int(sys.argv[1])
path = Path(sys.argv[2]) / 'session-work'
count = int(path.read_text()) + 1 if path.exists() else 1
path.write_text(str(count))
digest = hashlib.sha256(b'lease-probe-session-work' * 100000).hexdigest()
print(json.dumps({'count':count,'uid':os.getuid(),'sha256':digest}))
''', str(station_uid(lease["station"])), lease["scratch"]])
    result = json.loads(holder.command(command)["stdout"])
    with closing(box.mac_connect(lease["lease_id"])) as client:
        status = client.request("status")
        assert status["ready"] and status["session"]["uid"] == result["uid"], status
        result["helper"] = status
    return result


def owner(box, *arguments, timeout=120, check=True):
    return box.mac._shell("/bin/zsh", "-c", mac_sudo_command(*arguments),
                          timeout=timeout, check=check)


def contention(box, station):
    """Force a real guest busy reply, then a retry bounded by the original deadline."""
    results = []
    for hold, budget, succeeds in ((16, 30, True), (26, 14, False)):
        marker = "/var/tmp/firm-acceptance-lock-" + uuid4().hex
        script = r'''
import fcntl,sys,time
from pathlib import Path
with open('/var/db/firm/lock', 'a') as machine:
    fcntl.flock(machine, fcntl.LOCK_SH)
    with open('/var/db/firm/station-' + sys.argv[1] + '.lock', 'a') as account:
        fcntl.flock(account, fcntl.LOCK_EX)
        Path(sys.argv[3]).touch()
        try: time.sleep(float(sys.argv[2]))
        finally: Path(sys.argv[3]).unlink(missing_ok=True)
'''
        with ThreadPoolExecutor(max_workers=1) as pool:
            held = pool.submit(owner, box, PYTHON, "-I", "-c", script,
                               str(station), str(hold), marker, timeout=45)
            deadline = time.monotonic() + 10
            while True:
                # macOS has /bin/test only. Exit 1 is "not there yet"; anything else is a
                # fault in this check and must not read as waiting.
                seen = owner(box, "/bin/test", "-e", marker, check=False)
                if seen.returncode == 0:
                    break
                assert seen.returncode == 1, seen.stderr.decode(errors="replace")
                if held.done():
                    held.result()
                    raise AssertionError("Guest lock holder exited before the check")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Guest lock holder did not start")
                time.sleep(0.1)
            started = time.monotonic()
            result = {"held_seconds": hold, "budget_seconds": budget}
            try:
                result["reply"] = box.mac._rpc("station-status", station=station, timeout=budget)
            except TimeoutError as exc:
                result["timeout"] = str(exc)
            result["seconds"] = time.monotonic() - started
            assert ("reply" in result) == succeeds, result
            # The first guest busy response takes ten seconds. A fresh retry budget
            # would run past the original 14-second deadline.
            assert 10 <= result["seconds"] < (budget + 4), result
            held.result(timeout=45)
            results.append(result)
    return results


def failed_session(box, holders, station, report):
    """Disable the account's launchd job, never replace a controller/helper reply."""
    target = f"gui/{station_uid(station)}/dev.firm.station-helper"
    for holder in holders.values():
        if holder.lease is not None:
            holder.release()
    # The helper must start from its enrolled state, enabled, or this check would change
    # a persistent setting it did not make; a disabled but still-loaded helper answers
    # readiness above just the same.
    disabled = owner(box, "/bin/launchctl", "print-disabled", f"gui/{station_uid(station)}")
    listing = disabled.stdout.decode(errors="replace")
    assert '"dev.firm.station-helper" => disabled' not in listing, listing
    report["helper_override_before"] = "enabled"
    try:
        owner(box, "/bin/launchctl", "disable", target)
        owner(box, "/bin/launchctl", "bootout", f"gui/{station_uid(station)}")
        up = report["up_with_failed_session"] = box.up()
        rows = up["mac"]["stations"]
        bad = next(row for row in rows if row["station"] == station)
        assert not bad["ready"] and bad.get("failure"), bad
        assert all(row["ready"] for row in rows if row["station"] != station), rows
        assert box.infrastructure_status()["state"] == "running"
        grants = []
        try:
            # Fill all remaining accounts: this proves the failed account was passed
            # even when --station is not first in acquisition order.
            for index in range(len(holders) - 1):
                role = list(holders.values())[index].role
                lease = box.mac_acquire(role, f"failed-session-skip-{index}")
                grants.append(lease)
                assert lease["station"] != station, lease
            assert {lease["station"] for lease in grants} == set(holders) - {station}, grants
            report["failed_session_grants"] = grants
        finally:
            releases = report["failed_session_releases"] = []
            for grant in grants:
                try:
                    releases.append(box.mac_release(grant["lease_id"]))
                except Exception as exc:
                    releases.append({"clean": False, "error": str(exc)})
            assert all(r["clean"] for r in releases), releases
    finally:
        owner(box, "/bin/launchctl", "enable", target)
        # A reset creates a fresh GUI domain with the now-enabled helper.
        restored = report["failed_session_restored"] = cli(
            box, "mac-session-reset", "--station", str(station)
        )
        assert restored["returncode"] == 0, restored
        assert json.loads(restored["stdout"])["ready"], restored


def overlap(box, holders, selected, keeper, evidence):
    """All four operations start at a barrier; lease contention is an expected outcome."""
    spare_dir = evidence / "overlap-worker"
    spare_dir.mkdir(mode=0o700)
    spare = Probe(box, spare_dir, selected, role="acceptance-overlap")
    barrier = threading.Barrier(4)
    results = {}

    def call(action):
        barrier.wait(timeout=30)
        started = time.monotonic()
        try:
            answer = action()
            return {"result": answer, "seconds": time.monotonic() - started}
        except StationBusy as exc:
            return {"refused": str(exc), "seconds": time.monotonic() - started}

    def acquire():
        spare.lease = box.mac_acquire(spare.role, "overlap-acquire")
        spare.station = spare.lease["station"]
        return {"lease": spare.lease, "work": work(box, spare)}

    try:
        spare.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                "acquire": pool.submit(call, acquire),
                "release": pool.submit(call, holders[keeper].release),
                "reset": pool.submit(call, lambda: cli(
                    box, "mac-session-reset", "--station", str(selected))),
                "up": pool.submit(call, lambda: cli(box, "up")),
            }
            for name, future in futures.items():
                results[name] = future.result(timeout=600)
        assert results["release"]["result"]["clean"], results
        for name in ("reset", "up"):
            answer = results[name]["result"]
            if answer["returncode"]:
                assert "already leased" in answer["stderr"], answer
            elif name == "reset":
                assert json.loads(answer["stdout"])["ready"], answer
            else:
                # Whatever was leased while up ran is left to its holder and named; every
                # other station is restated and ready.
                mac = json.loads(answer["stdout"])["mac"]
                skipped = {row["station"] for row in mac["stations_skipped"]}
                assert all(row["skipped"] == "leased" for row in mac["stations_skipped"]), mac
                ready(mac, [n for n in holders if n not in skipped])
        held = [row for row in box.mac_status()["stations"] if row["held"]]
        assert len({row["lease_id"] for row in held}) == len(held), held
        if spare.lease is None:
            spare.lease = box.mac_acquire(spare.role, "after-overlap")
        results["work_after"] = work(box, spare)
        body_failed = False
    except BaseException:
        body_failed = True
        raise
    finally:
        # Each on its own, the evidence written whatever failed, and a cleanup failure
        # raised only when the body did not fail, so its failure is not replaced.
        errors = []
        for step in ("release", "log", "stop"):
            try:
                if step == "release" and spare.lease is not None:
                    results["spare_release"] = box.mac_release(spare.lease["lease_id"])
                    assert results["spare_release"]["clean"], results["spare_release"]
                elif step == "log" and spare.run is not None:
                    (spare_dir / "role.log").write_bytes(box.logs(spare.run))
                elif step == "stop" and spare.run is not None:
                    box.stop(spare.run)
            except Exception as exc:
                errors.append(f"{step}: {exc}")
        results["cleanup_errors"] = errors
        try:
            write_json(spare_dir / "result.json", results)
        except OSError as exc:
            errors.append(f"evidence: {exc}")
        if errors and not body_failed:
            raise RuntimeError(f"Overlap worker cleanup failed: {errors}")
    return results


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    stations = box.mac.stations
    if len(stations) < 2:
        parser.error("Session checks need at least two stations")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"passed": False, "station": args.station}
    try:
        report["initial_up"] = box.up()
        ready(report["initial_up"]["mac"], stations)
        with station_holders(box, args.evidence) as holders:
            keeper = next(n for n in stations if n != args.station)
            holder = holders[keeper]
            before = work(box, holder)
            heartbeat = holder.lease["scratch"] + "/session-heartbeat"
            worker = shlex.join([PYTHON, "-I", "-c", r'''
import hashlib,os,sys,time
from pathlib import Path
assert os.getuid() == int(sys.argv[1])
path = Path(sys.argv[2])
count = 0
while True:
    hashlib.sha256(b'lease-probe-background' * 100000).digest()
    count += 1
    temporary = path.with_suffix('.tmp')
    temporary.write_text(str(count))
    temporary.replace(path)
    time.sleep(0.1)
''', str(station_uid(holder.lease["station"])), heartbeat])
            pid = int(holder.command(f"nohup {worker} >/dev/null 2>&1 & echo $!")["stdout"])
            # The worker must have started before reset, and the same PID must
            # survive it with a growing counter, not merely accept new SSH calls.
            first = int(holder.command(
                f"while [ ! -s {shlex.quote(heartbeat)} ]; do sleep 0.1; done; "
                f"cat {shlex.quote(heartbeat)}"
            )["stdout"])
            holders[args.station].release()
            samples = report["work_during_reset"] = []
            with ThreadPoolExecutor(max_workers=1) as pool:
                reset = pool.submit(cli, box, "mac-session-reset", "--station", str(args.station))
                while not reset.done():
                    samples.append(work(box, holder))
                    time.sleep(0.5)
                report["reset"] = reset.result()
            assert samples and samples[-1]["count"] > before["count"], samples
            assert report["reset"]["returncode"] == 0, report["reset"]
            reset = json.loads(report["reset"]["stdout"])
            assert reset["ready"] and reset["reset"], reset
            if "cleanup" in reset:
                assert reset["cleanup"]["clean"], reset
            report["work_after_reset"] = work(box, holder)
            last = int(holder.command(
                f"kill -0 {pid} && cat {shlex.quote(heartbeat)}"
            )["stdout"])
            report["surviving_worker"] = {"pid": pid, "before": first, "after": last}
            assert last > first, report["surviving_worker"]
            boot = box.mac._rpc("info")["boot"]
            refusals = report["machine_refusals"] = []
            for operation in ("mac-restart", "down", "mac-replace"):
                answer = cli(box, operation)
                refusals.append(answer)
                assert answer["returncode"] != 0, answer
                assert ("already leased" in answer["stderr"]
                        or (operation == "mac-replace"
                            and "Stop the firm" in answer["stderr"])), answer
                assert box.mac._rpc("info")["boot"] == boot
                work(box, holder)
            # An up on the running firm writes nothing outside the accounts, so it goes
            # ahead: it restates the station just released and leaves every station
            # still held to its holder, saying so.
            answer = report["up_under_lease"] = cli(box, "up")
            assert answer["returncode"] == 0, answer
            mac = json.loads(answer["stdout"])["mac"]
            assert mac["stations_skipped"] == [
                {"station": n, "skipped": "leased"} for n in stations if n != args.station
            ], mac
            assert [row["station"] for row in mac["stations"]] == [args.station], mac
            assert box.mac._rpc("info")["boot"] == boot
            work(box, holder)
            report["overlap"] = overlap(box, holders, args.station, keeper, args.evidence)
            failed_session(box, holders, args.station, report)
            report["contention"] = contention(box, args.station)
        previous = box.mac._rpc("info")["boot"]
        report["password_rotation"] = box.mac._rpc("rotate-control-password", timeout=240)
        assert report["password_rotation"]["rotated"], report["password_rotation"]
        report["restart"] = box.mac_restart()
        ready(report["restart"], stations)
        assert box.mac._rpc("info")["boot"] != previous
        # A fresh clone rotates firmvm's password. These boots verify the configured
        # autologin still reaches the control console and all stations stay off console.
        # The window server's answer, not /dev/console's owner: the last graphical login
        # takes that file, and every station logs in after firmvm.
        report["console_after_restart"] = console_user(box.mac)
        assert report["console_after_restart"] == "firmvm", report
        assert all(not row["helper"]["session"]["on_console"]
                   for row in report["restart"]["stations"])
        previous = box.mac._rpc("info")["boot"]
        box.down()
        assert box.state() == "stopped"
        box = Sandbox(Config.load(args.root / "sandbox.toml"))
        report["cold_up"] = box.up()
        ready(report["cold_up"]["mac"], stations)
        assert box.mac._rpc("info")["boot"] != previous
        report["console_after_cold_boot"] = console_user(box.mac)
        assert report["console_after_cold_boot"] == "firmvm", report
        boot_evidence = args.evidence / "cold-boot-leases"
        boot_evidence.mkdir(mode=0o700)
        with station_holders(box, boot_evidence) as holders:
            report["work_after_cold_boot"] = {n: work(box, holder) for n, holder in holders.items()}
        report["passed"] = True
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        write_json(args.evidence / "result.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
