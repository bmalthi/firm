"""Build an iOS app through an ordinary Linux role's Mac lease.

The disposable firm must already be running, with Xcode, an available iOS
simulator runtime and an enrolled station. The role commits the LeaseProbeApp
fixture on its drive; the station clones it into its home, builds, boots a
simulator, installs and launches the app, and copies the product back. The role
verifies every delivered file before the host releases the lease and checks
simulator shutdown. Both VMs remain running; evidence survives failures.
"""

import json
import os
import subprocess
import time
from uuid import uuid4

from acceptance_common import (
    AWAIT_LEASE,
    BUILD_PROGRAM,
    STATION_COMMAND,
    acceptance_parser,
    acquire_station,
    await_output,
    close_out,
    fixture_preamble,
    lease_probe_fixture,
    raise_failures,
    require_free_station,
    select_station,
    start_worker,
    station_after_release,
)
from firm_sandbox import Config, Sandbox
from firm_sandbox.resources import process_usage

ROLE = "xcode-builder"
READY = "XCODE_ROLE_READY"
COMPLETE = "XCODE_ROLE_RESULT "

# The station program is the shared iOS build, run with `--ios=VERSION` when the check
# was given one.
ROLE_PROGRAM = fixture_preamble("iOS build fixture") + r'''
import hashlib

commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
print("XCODE_ROLE_READY", flush=True)
''' + AWAIT_LEASE + STATION_COMMAND + r'''
ios = ["--ios=" + os.environ["IOS_VERSION"]] if os.environ["IOS_VERSION"] else []
try:
    subprocess.run(lease_ssh(station_command(*ios)), check=True, timeout=2700)
finally:
    if (project / "xcodebuild.log").exists():
        print("XCODEBUILD_LOG_BEGIN", flush=True)
        print((project / "xcodebuild.log").read_text(), flush=True)
        print("XCODEBUILD_LOG_END", flush=True)
result = json.loads((project / "station-result.json").read_text())
assert result["git_commit"] == commit, "Station checkout does not match the role's commit"
app = project / "deliverables" / "LeaseProbeApp.app"
actual = {}
for path in sorted(app.rglob("*")):
    if path.is_symlink():
        actual[path.relative_to(app).as_posix()] = {"symlink": os.readlink(path)}
    elif path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        actual[path.relative_to(app).as_posix()] = {"sha256": digest}
assert actual == result["files"] and actual, "The role's delivered app differs from the built app"
assert (app / result["executable"]).stat().st_size > 0
result.update(role_uid=os.getuid(), role_deliverable=str(app), verified_files=len(actual))
print("XCODE_ROLE_RESULT " + json.dumps(result), flush=True)
while True:
    time.sleep(60)
'''


def virtual_machine_processes():
    """Every virtual-machine process of this user, with its own footprint and CPU time.

    The host attributes a process to a VM by the instance disk it holds open. If that
    mapping does not hold on this machine, this total is the fallback evidence, and the
    per-VM `host_footprint_bytes` must then be read as unknown, not as zero.
    """
    listing = subprocess.run(
        ["/bin/ps", "-u", str(os.getuid()), "-o", "pid=,comm="],
        capture_output=True, text=True, check=True, timeout=30,
    )
    processes, footprint, cpu = [], 0, 0
    for line in listing.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or "Virtualization.VirtualMachine" not in command:
            continue
        usage = process_usage(int(pid))
        processes.append({"pid": int(pid), **usage})
        footprint += usage["host_footprint_bytes"] or 0
        cpu += usage["host_cpu_usec"] or 0
    return {"processes": processes, "host_footprint_bytes": footprint, "host_cpu_usec": cpu}


def resource_evidence(box):
    """What the leased Mac, the Linux VM and the host report while the lease is held."""
    started = time.monotonic()
    observed = box.resources()
    return {
        "seconds": round(time.monotonic() - started, 3),
        "sample": observed,
        "machine": "mac",
        "mac_attributed": (observed["mac"] or {}).get("host_footprint_bytes")
        is not None,
        "linux_attributed": (observed["linux"] or {}).get("host_footprint_bytes") is not None,
        "all_virtual_machine_processes": virtual_machine_processes(),
    }


def main():
    parser = acceptance_parser(__doc__)
    parser.add_argument("--ios", default="", metavar="VERSION",
                        help="Boot this iOS runtime, as simctl reports it (26.0); default newest")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    role = f"{ROLE}-{args.station}"
    run, lease = None, None
    acquire_started = False
    report = {"passed": False, "role": role, "probe": "xcode-" + uuid4().hex[:12]}
    failure = None
    try:
        require_free_station(box, args.station)
        run = start_worker(
            box, role,
            env={"PROBE_NAME": report["probe"], "IOS_VERSION": args.ios},
            secrets={"fixture": json.dumps(lease_probe_fixture()).encode(),
                     "role-program": ROLE_PROGRAM.encode(),
                     "station-program": BUILD_PROGRAM.encode()},
        )
        report["run_id"] = run.run_id
        await_output(box, run, READY, args.evidence, timeout=240)
        acquire_started = True
        lease = acquire_station(box, role, "xcode-acceptance-" + report["probe"], args.station)
        lease_reply = lease.copy()
        report["lease_id"] = lease["lease_id"]
        report["resources"] = resource_evidence(box)
        mac_sample = report["resources"]["sample"]["mac"]
        assert mac_sample and mac_sample["pressure_level"] is not None, report["resources"]
        assert mac_sample["memory_total_bytes"] and mac_sample["swapouts"] is not None
        report["build"] = json.loads(
            await_output(box, run, COMPLETE, args.evidence, timeout=2700)
        )
        # The station program must have built inside the returned scratch directory.
        assert report["build"]["source"].startswith(lease["scratch"] + "/"), (
            lease["scratch"], report["build"]["source"]
        )
        report["release"] = box.mac_release(lease["lease_id"])
        assert report["release"]["clean"], report["release"]
        lease = None
        after = station_after_release(box, lease_reply, report["build"])
        report["after_release"] = after
        # Release deletes the lease's simulator devices and empties scratch, where the
        # checkout and the build tree were, and says how much that was.
        assert after["simulator"] == [], after
        assert not after["derived_data_retained"] and not after["source_retained"], after
        removed = {row["category"]: row["bytes"]
                   for row in report["release"]["reclaim"]["categories"]}
        assert report["release"]["reclaim"]["completed"] and removed["home"] > 0, (
            report["release"]["reclaim"]
        )
        report["passed"] = True
    except BaseException as exc:
        failure = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        close_out(box, args.evidence, report, station=args.station, role=role, run=run,
                  lease=lease, acquire_started=acquire_started)
    raise_failures(failure, report, args.evidence)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
