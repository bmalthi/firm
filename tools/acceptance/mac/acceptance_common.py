"""What more than one host-side Mac acceptance check needs.

Every check imports this module, so it holds only what two or more of them share; a
step that one check alone takes stays in that check.
"""

import argparse
import base64
import json
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

from firm_sandbox import Organization, RunSpec
from firm_sandbox.commands import mac_sudo_command
from firm_sandbox.mac_vm import PYTHON

# The iOS fixture a role publishes on its drive for the leased station to clone.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "LeaseProbeApp"
# Helper operations that post input: their replies acknowledge posting, not consumption.
INPUT_OPERATIONS = frozenset({"press_key", "type_text", "click", "drag", "tap_sequence"})

PROGRAM = r"""
import json, os, pathlib, subprocess, time
root = pathlib.Path(os.environ['FIRM_CREDENTIALS_DIR'])
print(json.dumps({'event': 'ready'}), flush=True)
number = 0
while True:
    path = root / ('probe-command-%04d' % number)
    if not path.exists():
        time.sleep(0.1)
        continue
    try:
        request = json.loads(path.read_text())
    except json.JSONDecodeError:
        time.sleep(0.1)
        continue
    lease = request['lease']
    argv = ['/usr/bin/ssh', '-i', lease['key_path'], '-p', str(lease['ssh_port']),
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=accept-new']
    mode = request.get('mode', 'command')
    socket = str(root / 'lease-probe-control')
    if mode == 'master':
        argv += ['-M', '-N', '-f', '-S', socket]
    elif mode == 'channel':
        argv += ['-S', socket, '-o', 'ControlMaster=no', '-o', 'ProxyCommand=false']
    argv += [lease['user'] + '@' + lease['ssh_host']]
    if mode != 'master':
        argv += [request['command']]
    try:
        if mode == 'master':
            # A detached SSH master must not retain subprocess.run's output pipes.
            with (pathlib.Path.home() / 'lease-master.stderr').open('w+') as error:
                process = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=error,
                                         timeout=45, text=True)
                error.seek(0)
                result = {'returncode': process.returncode, 'stdout': '',
                          'stderr': error.read()}
        else:
            process = subprocess.run(argv, capture_output=True, text=True, timeout=45)
            result = {'returncode': process.returncode, 'stdout': process.stdout,
                      'stderr': process.stderr}
    except subprocess.TimeoutExpired as exc:
        result = {'returncode': None, 'error': str(exc)}
    print(json.dumps({'event': 'command', 'number': number, **result}), flush=True)
    number += 1
"""


def station_preamble(purpose):
    """The head of a station program, before the check's own imports and steps.

    A station program is run as the leased account with four fixed arguments — the
    account's uid, the lease's scratch directory, the project directory on the drive and
    the probe's name — and refuses to run as anyone else; `purpose` names the run in that
    refusal. A check that needs more modules imports them in its own text, which follows
    this one and may add further arguments after the fourth.
    """
    return f'''
import json, os, shlex, subprocess, sys, time
from pathlib import Path

assert os.getuid() == int(sys.argv[1]), "{purpose} must run as the leased station account"
project = Path(sys.argv[3])
name = sys.argv[4]
# Work in the scratch directory returned by this lease.
scratch = Path(sys.argv[2])
source = scratch / "src" / name
derived = scratch / "DerivedData" / name
source.parent.mkdir(parents=True, exist_ok=True)
derived.parent.mkdir(parents=True, exist_ok=True)
'''


# Every command a station program runs, echoed into the role's log with its output.
STATION_RUN = r'''
def run(*argv, timeout=120):
    print("STATION_COMMAND " + shlex.join(map(str, argv)), flush=True)
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", flush=True)
    result.check_returncode()
    return result.stdout.strip()
'''

# Boot an iPhone simulator on `runtime` from the simctl listing `inventory`: an available
# one, else one created under the probe's name. It needs `run` and `name`, and leaves
# `device`, `udid` and `created` for the rest of the station program.
BOOT_IPHONE = r'''
devices = [d for d in inventory["devices"].get(runtime["identifier"], [])
           if d.get("isAvailable") and "iPhone" in d.get("deviceTypeIdentifier", d["name"])]
created = not devices
if devices:
    device = devices[0]
    udid = device["udid"]
else:
    types = runtime.get("supportedDeviceTypes", inventory["devicetypes"])
    phones = [d for d in types if "iPhone" in d["identifier"]]
    assert phones, "The available iOS runtime has no iPhone simulator device type"
    udid = run("/usr/bin/xcrun", "simctl", "create", name,
               phones[0]["identifier"], runtime["identifier"])
    device = {"udid": udid, "state": "Shutdown"}
if device["state"] != "Booted":
    run("/usr/bin/xcrun", "simctl", "boot", udid, timeout=180)
run("/usr/bin/xcrun", "simctl", "bootstatus", udid, "-b", timeout=600)
'''

# Clone the role's fixture, build it, boot a simulator, install and launch the app, and
# copy the product back to the drive. `xcode_build.py` runs this as its whole station
# program; `device_hub_taps.py` adds a tail that reuses the names it leaves in scope
# (`run`, `project`, `udid`, `info`, `device`, `result`).
BUILD_PROGRAM = station_preamble("The build") + STATION_RUN + r'''
import hashlib, plistlib, shutil

# `--ios=VERSION` names the iOS runtime to boot, as simctl reports it, met by itself or a patch
# of it; without it, the newest. Named, not positional: device_hub_taps.py runs this program
# with its own arguments after uid, scratch, project and name.
wanted = next((a.partition("=")[2] for a in sys.argv[5:] if a.startswith("--ios=")), "")

run("/usr/bin/git", "clone", "--no-hardlinks", project / "LeaseProbeApp", source, timeout=300)
commit = run("/usr/bin/git", "-C", source, "rev-parse", "HEAD")
command = ["/usr/bin/xcodebuild", "-project", str(source / "LeaseProbeApp.xcodeproj"),
           "-scheme", "LeaseProbeApp", "-configuration", "Debug", "-sdk", "iphonesimulator",
           "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(derived),
           "CODE_SIGNING_ALLOWED=NO", "build"]
print("STATION_COMMAND " + shlex.join(command), flush=True)
started = time.monotonic()
with (project / "xcodebuild.log").open("w") as log:
    built = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
print((project / "xcodebuild.log").read_text()[-12000:], flush=True)
built.check_returncode()
app = derived / "Build/Products/Debug-iphonesimulator/LeaseProbeApp.app"
assert app.is_dir(), "xcodebuild did not produce LeaseProbeApp.app"

inventory = json.loads(run("/usr/bin/xcrun", "simctl", "list", "-j"))
(project / "simulators-before.json").write_text(json.dumps(inventory, indent=2) + "\n")
def version(runtime):
    return tuple(int(part) for part in runtime["version"].split("."))
runtimes = sorted((r for r in inventory["runtimes"]
                   if r.get("isAvailable") and ".iOS-" in r["identifier"]
                   and (not wanted or (r["version"] + ".").startswith(wanted + "."))),
                  key=version, reverse=True)
assert runtimes, (f"No available iOS {wanted} simulator runtime: name it in the image recipe "
                  "and run mac-image-apply, or for the newest one "
                  "sudo -n /usr/local/libexec/firm-station-add-platform iOS")
runtime = runtimes[0]
''' + BOOT_IPHONE + r'''
booted = json.loads(run("/usr/bin/xcrun", "simctl", "list", "devices", "-j"))
assert any(d["udid"] == udid and d["state"] == "Booted"
           for rows in booted["devices"].values() for d in rows), booted
run("/usr/bin/xcrun", "simctl", "install", udid, app, timeout=180)
with (app / "Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
launched = run("/usr/bin/xcrun", "simctl", "launch", udid,
               info["CFBundleIdentifier"], timeout=120)
deliverable = project / "deliverables" / app.name
deliverable.parent.mkdir()
shutil.copytree(app, deliverable, symlinks=True, copy_function=shutil.copyfile)
files = {}
for path in sorted(app.rglob("*")):
    if path.is_symlink():
        files[path.relative_to(app).as_posix()] = {"symlink": os.readlink(path)}
    elif path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.relative_to(app).as_posix()] = {"sha256": digest}
result = {"station_uid": os.getuid(), "source": str(source), "git_commit": commit,
          "derived_data": str(derived), "build_command": command,
          "seconds": round(time.monotonic() - started, 1), "deliverable": str(deliverable),
          "files": files, "executable": info["CFBundleExecutable"],
          "simulator": {"udid": udid, "runtime": runtime["identifier"], "created": created,
                        "state_before_release": "Booted", "launch": launched}}
(project / "station-result.json").write_text(json.dumps(result, indent=2) + "\n")
print("STATION_BUILD_COMPLETE", flush=True)
'''


def fixture_preamble(summary):
    """The head of a role program that publishes the iOS fixture as a Git repository.

    The fixture arrives whole in the role's `fixture` credential and is committed with
    `summary` as its message. A check that compares the station's checkout against this
    commit reads the revision in its own text, which follows this one.
    """
    return f'''
import json, os, shlex, subprocess, time
from pathlib import Path

credentials = Path(os.environ["FIRM_CREDENTIALS_DIR"])
name = os.environ["PROBE_NAME"]
project = Path.cwd() / name
repository = project / "LeaseProbeApp"
repository.mkdir(parents=True)
for relative, content in json.loads((credentials / "fixture").read_text()).items():
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
git = ["/usr/bin/git", "-C", str(repository)]
subprocess.run(git + ["init", "-q"], check=True)
subprocess.run(git + ["add", "."], check=True)
subprocess.run(git + ["-c", "user.name=Firm acceptance", "-c", "user.email=probe@example.invalid",
                      "commit", "-qm", "{summary}"], check=True)
'''


# Wait for the lease reply the host publishes once it has checked the granted station,
# and offer the lease's own SSH endpoint. `credentials` must already be in scope.
AWAIT_LEASE = r'''
deadline = time.monotonic() + 180
while True:
    lease_path = credentials / "acceptance-lease"
    if (credentials / "acceptance-lease-ready").exists():
        break
    if time.monotonic() >= deadline:
        raise TimeoutError("No checked Mac lease reply arrived")
    time.sleep(0.2)
lease = json.loads(lease_path.read_text())

def lease_ssh(command):
    """This lease's SSH argv: the station account, ending in the command to run there."""
    return ["/usr/bin/ssh", "-i", lease["key_path"], "-p", str(lease["ssh_port"]),
            "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            lease["user"] + "@" + lease["ssh_host"], command]
'''

# The station program's command line, for a role that has the lease and the program.
STATION_COMMAND = r'''
def station_command(*arguments):
    """The four fixed arguments every station program takes, then the check's own."""
    return shlex.join(["/usr/local/firm/bin/python3", "-I", "-c",
                       (credentials / "station-program").read_text(),
                       str(501 + lease["station"]), lease["scratch"],
                       lease["project_dir"] + "/" + name, name, *arguments])
'''


def acceptance_parser(description, *, station_help="Mac station to lease"):
    """The three options every check takes; a check adds its own after these."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--station", type=int, help=station_help)
    return parser


def select_station(box, requested):
    if box.mac is None or not box.mac.stations:
        raise SystemExit("This check needs a prepared Mac with station accounts")
    stations = box.mac.stations
    if requested is None:
        if len(stations) != 1:
            raise SystemExit(f"--station is required; this Mac has stations {stations}")
        return stations[0]
    if requested not in stations:
        raise SystemExit(f"This Mac has stations {stations}, not station {requested}")
    return requested


def station_uid(station):
    """The account uid a station number stands for."""
    return 501 + station


def station_home(lease):
    """The station account's home: the parent of the scratch directory this lease returns."""
    return Path(lease["scratch"]).parent


def station_status(box, station):
    """The station's own row in the sandbox's Mac status."""
    for row in box.mac_status()["stations"]:
        if row["station"] == station:
            return row
    raise SystemExit(f"This firm has no Mac station {station}")


def write_json(path, payload):
    """Every evidence document: indented JSON, one trailing newline."""
    path.write_text(json.dumps(payload, indent=2) + "\n")


def publish_lease(box, role, lease):
    """Wake the Linux worker only after the host has checked the granted station."""
    box.linux._rpc(
        "write-credential", role_id=role, name="acceptance-lease",
        data=base64.b64encode(json.dumps(lease).encode()).decode(),
    )
    # Credential creation is create-only, but the file can be seen before its
    # contents have finished writing. Publish readiness after that RPC returns.
    box.linux._rpc(
        "write-credential", role_id=role, name="acceptance-lease-ready",
        data=base64.b64encode(b"ready").decode(),
    )


def control_probe(box, script, *arguments, user=None, timeout=120):
    """Run a read-only probe inside the Mac VM from the control account, and read its JSON.

    With `user`, the probe runs as that station account through sudo, so a check can ask
    what a lease left behind without issuing another lease or leaving an authenticated
    lease connection alive.
    """
    account = ("/usr/bin/sudo", "-H", "-u", user) if user is not None else ()
    command = mac_sudo_command(*account, PYTHON, "-I", "-c", script, *arguments)
    result = box.mac._shell("/bin/zsh", "-c", command, timeout=timeout)
    return json.loads(result.stdout)


def station_after_release(box, lease, build):
    """What became of the build's simulator, checkout and build tree once released."""
    script = """import json,subprocess,sys
from pathlib import Path
devices=json.loads(subprocess.check_output(['/usr/bin/xcrun','simctl','list','devices','-j']))
selected=[d for rows in devices['devices'].values() for d in rows if d['udid']==sys.argv[1]]
print(json.dumps({'simulator':selected,'derived_data_retained':Path(sys.argv[2]).is_dir(),
                  'source_retained':Path(sys.argv[3]).is_dir()}))
"""
    return control_probe(
        box, script, build["simulator"]["udid"], build["derived_data"], build["source"],
        user=lease["user"], timeout=120,
    )


def require_free_station(box, station):
    """Both VMs running and this station free and clean: what an application check needs."""
    assert box.infrastructure_status()["state"] == "running", "Start both VMs first"
    status = station_status(box, station)
    assert not status["held"] and not status["dirty"], status


def lease_probe_fixture():
    """The iOS fixture as {path: text}, for a role program to commit on its drive."""
    fixture = {path.relative_to(FIXTURE).as_posix(): path.read_text()
               for path in sorted(FIXTURE.rglob("*")) if path.is_file()}
    assert fixture, f"Missing iOS fixture: {FIXTURE}"
    return fixture


def start_worker(box, role, *, env, secrets):
    """Provision the role's drive and start the Linux worker that runs its role program.

    The check waits for the program's ready line itself, so that the run is in the
    check's hands — to be stopped, and its log kept — however that wait ends.
    """
    organization = Organization("founder/" + role)
    box.file_server.provision(role, organization)
    layout = box.layout(role)
    return box.start(RunSpec(
        role, (layout.python, layout.credential("role-program")),
        organization=organization, env=env, secrets=secrets,
    ))


def sleeper_run(firm, role):
    """Provision the role and start the idle run a check leases the Mac against.

    The lease key lands in the role's active run, so a check whose work is all on the
    host keeps one long-sleeping run alive for as long as it holds the lease.
    """
    organization = Organization("founder/" + role)
    firm.file_server.provision(role, organization)
    return firm.start(RunSpec(
        role, ("/usr/bin/python3", "-c", "import time; time.sleep(900)"),
        organization=organization,
    ))


def acquire_station(box, role, purpose, station):
    """Lease the named station for this role, and publish the checked reply to its worker."""
    lease = box.mac_acquire(role, purpose, station=station)
    assert lease["station"] == station, (
        f"Mac station {station} was not free; the firm granted station "
        f"{lease['station']}"
    )
    publish_lease(box, role, lease)
    return lease


def held_lease(box, station, role):
    """The lease this role holds on the station, as the station itself records it.

    `mac_acquire` can grant and then raise before its caller has the reply, so a check
    that may have acquired asks the station rather than its own variables.
    """
    status = station_status(box, station)
    if status["holder"] == role and status["lease_id"]:
        return {"lease_id": status["lease_id"]}
    return None


def release_if_still_held(firm, lease):
    """Release a lease the check may still hold; the station's record decides."""
    if lease is not None and station_status(firm, lease["station"])["held"]:
        firm.mac_release(lease["lease_id"])


def cleanup_release(box, report, lease):
    """Release a lease the check still holds, and report what that release failed to do."""
    if lease is None:
        return []
    try:
        report["cleanup_release"] = box.mac_release(lease["lease_id"])
        if not report["cleanup_release"].get("clean"):
            return ["Mac lease cleanup is unverified"]
    except Exception as exc:
        return [f"Mac release: {exc}"]
    return []


def cleanup_worker(box, evidence, report, run):
    """Stop the check's Linux worker and keep its log, whatever the stop did."""
    if run is None:
        return []
    errors = []
    try:
        report["role_stop"] = box.stop(run)
        assert not report["role_stop"].get("unresolved"), report["role_stop"]
    except Exception as exc:
        errors.append(f"Role stop: {exc}")
    try:
        (evidence / "role.log").write_bytes(box.logs(run))
    except Exception as exc:
        errors.append(f"Role log: {exc}")
    return errors


def close_out(box, evidence, report, *, station, role, run, lease, acquire_started,
              client=None):
    """Disconnect `client`, release the lease, stop the worker, and write `result.json`.

    A check that began acquiring, has no lease reply and made no release of its own asks
    the station whether it was granted one. Every step is attempted; any that fails
    fails the report.
    """
    errors = []
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            errors.append(f"Helper disconnect: {exc}")
    if acquire_started and lease is None and "release" not in report:
        try:
            lease = held_lease(box, station, role)
        except Exception as exc:
            errors.append(f"Partial acquisition status: {exc}")
    errors += cleanup_release(box, report, lease)
    errors += cleanup_worker(box, evidence, report, run)
    if errors:
        report.update(passed=False, cleanup_errors=errors)
    write_json(evidence / "result.json", report)


def cleanup_probe(firm, probe, log_name=None):
    """Stop a lease probe's Linux holder, keeping its log under `log_name` when asked."""
    if probe is None or probe.run is None:
        return []
    try:
        if log_name is not None:
            (probe.evidence / log_name).write_bytes(firm.logs(probe.run))
        firm.stop(probe.run)
    except Exception as exc:
        return [str(exc)]
    return []


def raise_failures(failure, report, evidence):
    """The check's own failure first; a cleanup that failed alone is its own error."""
    if failure is not None:
        raise failure
    if not report["passed"]:
        raise RuntimeError(f"Acceptance cleanup failed; inspect {evidence / 'result.json'}")


def await_output(box, run, prefix, evidence, timeout):
    """Wait for a line the role's program prints, keeping the role's log while it waits."""
    deadline = time.monotonic() + timeout
    while True:
        logs = box.logs(run)
        (evidence / "role.log").write_bytes(logs)
        for line in logs.decode(errors="replace").splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
        state = box.status(run)
        if not state.get("running"):
            raise RuntimeError(f"{run.role_id} exited: {state}; inspect role.log")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Waiting for {prefix}; inspect role.log")
        time.sleep(1)


def helper_rpc(client, evidence, *, pause=0.0):
    """A caller for the helper relay that times every call and keeps each reply.

    Each reply is written to the evidence directory as `NNN-op.json` with the seconds
    the call took, a PNG reply is written beside it and named in the record, and the
    reply the caller sees carries those seconds as `_elapsed`. After an operation that
    posts input, `pause` seconds let the application consume it: an input reply
    acknowledges posting, not consumption.
    """
    sequence = 0

    def rpc(op, **fields):
        nonlocal sequence
        sequence += 1
        started = time.monotonic()
        result = client.request(op, **fields)
        elapsed = time.monotonic() - started
        if "png_base64" in result:
            name = f"{sequence:03d}-{op}.png"
            (evidence / name).write_bytes(
                base64.b64decode(result.pop("png_base64"), validate=True)
            )
            result["image"] = name
        status = result.pop("status", None)
        write_json(evidence / f"{sequence:03d}-{op}.json",
                   {"id": sequence, "elapsed_s": elapsed,
                    "status": status, "result": result})
        if status != "ok":
            raise RuntimeError({"status": status, **result})
        if pause and op in INPUT_OPERATIONS:
            time.sleep(pause)
        result["_elapsed"] = elapsed
        return result

    return rpc


def rows(runtime):
    with runtime.store.read() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT activation_id,employee_id,state,failure_category FROM activations"
            )
        ]


def lease_tables(runtime):
    with runtime.store.read() as db:
        return {
            "leases": [dict(row) for row in db.execute("SELECT * FROM mac_leases")],
            "events": [dict(row) for row in db.execute("SELECT * FROM mac_lease_events")],
        }


def capture_logs(box, evidence):
    captured = {}
    for run in box.runs():
        content = box.logs(run)
        path = evidence / f"{run.run_id}.log"
        path.write_bytes(content)
        path.chmod(0o600)
        captured[run.role_id] = run.run_id
    return captured


class Probe:
    def __init__(self, firm, evidence, station, *, role):
        self.firm, self.evidence, self.station = firm, evidence, station
        self.role = role
        self.number = 0
        self.run = None
        self.lease = None
        self.records = []

    def wait_event(self, predicate):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            for line in self.firm.logs(self.run).decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and predicate(event):
                    return event
            if not self.firm.status(self.run).get("running"):
                raise RuntimeError("The Linux holder exited before responding")
            time.sleep(0.2)
        raise TimeoutError("The Linux holder did not respond")

    def start(self):
        organization = Organization("founder/" + self.role)
        self.firm.file_server.provision(self.role, organization)
        layout = self.firm.layout(self.role)
        self.run = self.firm.start(RunSpec(
            self.role, (layout.python, layout.credential("probe-program")),
            organization=organization,
            secrets={"probe-program": PROGRAM.encode()},
        ))
        self.wait_event(lambda event: event.get("event") == "ready")

    def command(self, command="", *, mode="command", check=True):
        request = {"lease": self.lease, "command": command, "mode": mode}
        self.firm.linux._rpc(
            "write-credential", role_id=self.role, name=f"probe-command-{self.number:04d}",
            data=base64.b64encode(json.dumps(request).encode()).decode(),
        )
        number = self.number
        self.number += 1
        result = self.wait_event(lambda event: event.get("number") == number)
        if check and result["returncode"] != 0:
            raise RuntimeError(f"Role SSH command failed: {result}")
        return result

    def acquire(self, name):
        self.lease = self.firm.mac_acquire(self.role, "cleanup-" + name, station=self.station)
        lease_id = self.lease["lease_id"]
        root = self.firm.mac_station(self.lease["station"]).root
        record = json.loads((root / "leases" / f"{lease_id}.json").read_text())
        item = {"name": name, "lease_id": lease_id, "baseline": record["baseline"],
                "lease": self.lease.copy()}
        self.records.append(item)
        if self.lease["station"] != self.station:
            raise RuntimeError(
                f"The firm granted Mac station {self.lease['station']}, not {self.station}"
            )
        return item

    def release(self, *, clean=True):
        item = self.records[-1]
        released = self.firm.mac_release(self.lease["lease_id"])
        item["release"] = released
        # The handle stays until the release proved what was asked of it: a release that
        # left the station dirty is still this lease's to reconcile.
        if released.get("clean") is clean:
            self.lease = None
        root = self.firm.mac_station(item["lease"]["station"]).root
        log = root / "leases" / f"{item['lease_id']}.log"
        item["command_log"] = str(log)
        if log.exists():
            (self.evidence / log.name).write_bytes(log.read_bytes())
        assert released.get("clean") is clean, released
        assert log.is_file() and b"lease-probe-" in log.read_bytes(), str(log)
        if clean:
            # Reclaiming less than hoped never dirties, so a healthy cycle says so here.
            assert released["reclaim"]["completed"], released["reclaim"]
            assert all(step["completed"] for step in released["simulators"]), released["simulators"]
        return released

    def owner(self, script, *, check=True):
        # This shell runs inside the disposable Mac VM, never on the host.
        return self.firm.mac._shell(
            "/bin/sh", "-c", mac_sudo_command("/bin/sh", "-c", script), check=check
        )

    def remove_immutable(self, path):
        quoted = shlex.quote(path)
        self.owner(
            f"set -eu; if [ -e {quoted} ]; then "
            f"/usr/bin/chflags noschg,nouchg {quoted}; /bin/rm -f {quoted}; fi"
        )


@contextmanager
def station_holders(box, evidence):
    """Real Linux workers on a disposable firm; all accounts must start free and clean."""
    holders = {}
    try:
        for number in box.mac.stations:
            status = station_status(box, number)
            assert not status["held"] and not status["dirty"], status
        for number in box.mac.stations:
            directory = evidence / f"station-{number}"
            directory.mkdir(mode=0o700)
            holder = Probe(box, directory, number, role=f"acceptance-station-{number}")
            holders[number] = holder
            holder.start()
            holder.acquire("initial")
            holder.command("printf lease-probe-initial")
        yield holders
        body_failed = False
    except BaseException:
        body_failed = True
        raise
    finally:
        # These holders are what the check measures, so a cleanup that fails fails the
        # check; but never in place of the check's own failure, and every step is
        # attempted on its own: a partial acquire or a release that left the station held
        # is released again from the sandbox's record, and the worker is stopped whatever
        # its log did.
        errors = []
        for holder in reversed(list(holders.values())):
            own = []
            for step in ("release", "reconcile", "log", "stop"):
                try:
                    if step == "release" and holder.lease is not None:
                        holder.release()
                    elif step == "reconcile":
                        status = station_status(box, holder.station)
                        if status["holder"] == holder.role and status["lease_id"]:
                            result = box.mac_release(status["lease_id"])
                            assert result["clean"], result
                        assert not station_status(box, holder.station)["dirty"]
                    elif step == "log" and holder.run is not None:
                        (holder.evidence / "role.log").write_bytes(box.logs(holder.run))
                    elif step == "stop" and holder.run is not None:
                        box.stop(holder.run)
                except Exception as exc:
                    own.append(f"{step}: {exc}")
            errors.extend(f"Station {holder.station} {error}" for error in own)
            try:
                write_json(holder.evidence / "result.json",
                           {"leases": holder.records, "errors": own})
            except OSError as exc:
                errors.append(f"Station {holder.station} evidence: {exc}")
        if errors and not body_failed:
            raise RuntimeError(f"Station cleanup failed: {errors}")
