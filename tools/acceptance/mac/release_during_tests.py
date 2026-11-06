"""Release a Mac lease while parallel UI tests are still running on it.

Parallel testing is what leaves the most behind: `xcodebuild` clones the device into its
own `testing` device set under `~/Library/Developer/XCTestDevices`, boots the clones and
keeps runners attached to them. A Linux role publishes the LeaseProbeApp fixture, the
leased Mac builds it for testing and starts `xcodebuild test-without-building` with
parallel testing in the background. Once a clone is booted with a test runner attached,
and the host has seen from outside the lease that `xcodebuild` and the runner are still
alive, it releases the lease under them. The release must be clean with every simulator
step completed, and afterwards the station account runs no `xcodebuild`, test runner or
simulated device, both device sets are empty, and the checkout and build tree are gone.
CoreSimulator's own service, which launchd starts on demand and the release's `simctl`
calls start themselves, is recorded and not counted. Both VMs remain running; evidence
survives failures. Run from the repository root with `uv run`.
"""

import json
from uuid import uuid4

from acceptance_common import (
    AWAIT_LEASE,
    STATION_COMMAND,
    acceptance_parser,
    await_output,
    cleanup_release,
    control_probe,
    fixture_preamble,
    held_lease,
    lease_probe_fixture,
    publish_lease,
    require_free_station,
    select_station,
    start_worker,
    station_preamble,
    station_status,
    station_uid,
    write_json,
)
from firm_sandbox import Config, Sandbox

ROLE = "parallel-tester"
READY = "PARALLEL_ROLE_READY"
RUNNING = "PARALLEL_ROLE_RUNNING "

# This station program's own `run`: the build's output is large and only interesting when
# a command fails, so it is kept out of the role's log until then.
STATION_PROGRAM = station_preamble("The tests") + r'''
def run(*argv, timeout=120):
    print("STATION_COMMAND " + shlex.join(map(str, argv)), flush=True)
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        print(result.stdout[-4000:], result.stderr[-4000:], flush=True)
    result.check_returncode()
    return result.stdout.strip()

run("/usr/bin/git", "clone", "--no-hardlinks", project / "LeaseProbeApp", source, timeout=300)
inventory = json.loads(run("/usr/bin/xcrun", "simctl", "list", "-j", timeout=180))
def version(runtime):
    return tuple(int(part) for part in runtime["version"].split("."))
runtimes = sorted((r for r in inventory["runtimes"]
                   if r.get("isAvailable") and ".iOS-" in r["identifier"]),
                  key=version, reverse=True)
assert runtimes, "No available iOS simulator runtime"
runtime = runtimes[0]
types = runtime.get("supportedDeviceTypes", inventory["devicetypes"])
phones = [d for d in types if "iPhone" in d["identifier"]]
assert phones, "The available iOS runtime has no iPhone simulator device type"
udid = run("/usr/bin/xcrun", "simctl", "create", name, phones[0]["identifier"],
           runtime["identifier"])
common = ["-project", str(source / "LeaseProbeApp.xcodeproj"), "-scheme", "LeaseProbeUITests",
          "-destination", "platform=iOS Simulator,id=" + udid, "-derivedDataPath", str(derived),
          "CODE_SIGNING_ALLOWED=NO"]
with (project / "xcodebuild-build-for-testing.log").open("w") as log:
    built = subprocess.run(["/usr/bin/xcodebuild", *common, "build-for-testing"],
                           stdout=log, stderr=subprocess.STDOUT, timeout=1800)
if built.returncode:
    print((project / "xcodebuild-build-for-testing.log").read_text()[-8000:], flush=True)
built.check_returncode()

# In its own session and with no terminal, so it outlives this SSH call: only the
# release can end it.
log = (project / "xcodebuild-parallel.log").open("w")
tests = subprocess.Popen(
    ["/usr/bin/xcodebuild", *common, "-parallel-testing-enabled", "YES",
     "-parallel-testing-worker-count", "2", "test-without-building"],
    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

def clones():
    listing = subprocess.run(["/usr/bin/xcrun", "simctl", "--set", "testing", "list",
                              "devices", "-j"], capture_output=True, text=True, timeout=120)
    if listing.returncode:
        return []
    return [{"udid": d["udid"], "name": d["name"], "state": d["state"]}
            for rows in json.loads(listing.stdout)["devices"].values() for d in rows]

def runners():
    """Test runner processes of this account: the UI test runner app, or xctest."""
    listing = subprocess.run(["/bin/ps", "-U", sys.argv[1], "-o", "pid=,comm="],
                             capture_output=True, text=True, timeout=60)
    return [row.strip() for row in listing.stdout.splitlines()
            if any(word in row for word in ("-Runner", "XCTRunner", "xctest"))]

# A booted clone alone is not a running test: the runner must be attached to it, and
# xcodebuild still alive, when this reports.
deadline = time.monotonic() + 900
while True:
    found, attached = clones(), runners()
    assert tests.poll() is None, (
        "xcodebuild ended before its tests were running: "
        + (project / "xcodebuild-parallel.log").read_text()[-6000:])
    if any(d["state"] == "Booted" for d in found) and attached:
        break
    assert time.monotonic() < deadline, f"No test ran in a clone; saw {found} and {attached}"
    time.sleep(2)
testing_set = Path.home() / "Library/Developer/XCTestDevices"
result = {"station_uid": os.getuid(), "source": str(source), "derived_data": str(derived),
          "device": udid, "xcodebuild_pid": tests.pid, "clones": found, "runners": attached,
          "testing_set_entries": sorted(p.name for p in testing_set.iterdir())}
(project / "station-result.json").write_text(json.dumps(result, indent=2) + "\n")
print("STATION_TESTS_RUNNING", flush=True)
'''

ROLE_PROGRAM = fixture_preamble("iOS UI test fixture") + r'''
print("PARALLEL_ROLE_READY", flush=True)
''' + AWAIT_LEASE + STATION_COMMAND + r'''
subprocess.run(lease_ssh(station_command()), check=True, timeout=3600)
result = json.loads((project / "station-result.json").read_text())
print("PARALLEL_ROLE_RUNNING " + json.dumps(result), flush=True)
while True:
    time.sleep(60)
'''

AFTER = """import json,subprocess,sys
from pathlib import Path
def devices(*where):
    listing=subprocess.run(['/usr/bin/xcrun','simctl',*where,'list','devices','-j'],
                           capture_output=True,text=True,timeout=120)
    if listing.returncode: return {'error':listing.stderr[-300:]}
    return [d['udid'] for rows in json.loads(listing.stdout)['devices'].values() for d in rows]
processes=subprocess.run(['/bin/ps','-U',sys.argv[3],'-o','pid=,comm='],capture_output=True,text=True)
rows=[line.strip() for line in processes.stdout.splitlines()]
# A build, a test runner, a simulated device's launchd, or the Simulator application.
words=('xcodebuild','-Runner','XCTRunner','xctest','launchd_sim','Simulator.app/')
left=[row for row in rows if any(w in row for w in words)]
# CoreSimulator's own service, which launchd starts on demand: the release's simctl calls
# start it themselves, so it is recorded and is not the lease's work.
service=[row for row in rows if 'CoreSimulator.framework' in row]
testing=Path.home()/'Library/Developer/XCTestDevices'
entries=sorted(p.name for p in testing.iterdir()) if testing.is_dir() else []
print(json.dumps({'default_set':devices(),'testing_set':devices('--set','testing'),
                  'processes':left,'core_simulator_service':service,
                  'testing_set_entries':entries,
                  'derived_data_retained':Path(sys.argv[1]).is_dir(),
                  'source_retained':Path(sys.argv[2]).is_dir()}))
"""


BOUNDARY = """import json,subprocess,sys
rows=subprocess.run(['/bin/ps','-U',sys.argv[2],'-o','pid=,comm='],capture_output=True,text=True)
rows=[row.strip() for row in rows.stdout.splitlines()]
build=[row for row in rows if row.split(' ',1)[0]==sys.argv[1] and 'xcodebuild' in row]
runners=[row for row in rows if any(w in row for w in ('-Runner','XCTRunner','xctest'))]
print(json.dumps({'xcodebuild':build,'runners':runners}))
"""


def at_release_boundary(box, lease, running):
    """From the control account, just before the release: is the workload still there?"""
    return control_probe(box, BOUNDARY, str(running["xcodebuild_pid"]),
                         str(station_uid(lease["station"])), timeout=120)


def workload_after_release(box, lease, running):
    """What the released station is still running, and whether its trees are gone."""
    return control_probe(box, AFTER, running["derived_data"], running["source"],
                         str(station_uid(lease["station"])),
                         user=lease["user"], timeout=300)


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    role = f"{ROLE}-{args.station}"
    run, lease = None, None
    report = {"passed": False, "role": role, "probe": "parallel-" + uuid4().hex[:12]}
    failure = None
    try:
        require_free_station(box, args.station)
        run = start_worker(
            box, role,
            env={"PROBE_NAME": report["probe"]},
            secrets={"fixture": json.dumps(lease_probe_fixture()).encode(),
                     "role-program": ROLE_PROGRAM.encode(),
                     "station-program": STATION_PROGRAM.encode()},
        )
        await_output(box, run, READY, args.evidence, timeout=240)
        lease = box.mac_acquire(role, "release-during-tests-" + report["probe"],
                                station=args.station)
        assert lease["station"] == args.station, lease
        publish_lease(box, role, lease)
        lease_reply = lease.copy()
        report["lease_id"] = lease["lease_id"]
        report["running"] = json.loads(
            await_output(box, run, RUNNING, args.evidence, timeout=3600))
        assert any(d["state"] == "Booted" for d in report["running"]["clones"]), (
            report["running"]
        )
        # Asked again from outside the lease, at the last moment: a run that had already
        # ended would make this an ordinary release.
        boundary = report["at_release"] = at_release_boundary(
            box, lease, report["running"])
        assert boundary["xcodebuild"] and boundary["runners"], boundary
        report["release"] = box.mac_release(lease["lease_id"])
        lease = None
        release = report["release"]
        assert release["clean"], release
        assert release["simulators"] and all(
            step["completed"] for step in release["simulators"]
        ), release["simulators"]
        assert release["reclaim"]["completed"], release["reclaim"]
        after = report["after_release"] = workload_after_release(
            box, lease_reply, report["running"])
        assert after["default_set"] == [] and after["testing_set"] == [], after
        assert after["processes"] == [] and after["testing_set_entries"] == [], after
        assert not after["derived_data_retained"] and not after["source_retained"], after
        report["passed"] = True
    except BaseException as exc:
        failure = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        cleanup_errors = []
        if lease is None and "release" not in report:
            try:
                lease = held_lease(box, args.station, role)
            except Exception as exc:
                cleanup_errors.append(f"Partial acquisition status: {exc}")
        cleanup_errors += cleanup_release(box, report, lease)
        try:
            final = station_status(box, args.station)
            if final["held"] or final["dirty"]:
                cleanup_errors.append(
                    f"The station is left held={final['held']} dirty={final['dirty']}"
                )
        except Exception as exc:
            cleanup_errors.append(f"Final station status: {exc}")
        if run is not None:
            # The log first: this check's worker is stopped while its SSH call may still
            # be open, and the log is the evidence of what it had reached.
            try:
                (args.evidence / "role.log").write_bytes(box.logs(run))
                report["role_stop"] = box.stop(run)
            except Exception as exc:
                cleanup_errors.append(f"Role stop: {exc}")
        if cleanup_errors:
            report["passed"] = False
            report["cleanup_errors"] = cleanup_errors
        write_json(args.evidence / "result.json", report)
    print(json.dumps({"passed": report["passed"], "evidence": str(args.evidence)}))
    if failure is not None:
        raise failure
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
