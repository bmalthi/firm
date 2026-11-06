"""Headful browser and Apple app smoke test through a Mac lease on an enrolled disposable VM.

An ordinary role holds the lease; the probe drives the helper through the
lease-bound relay, exercises browser input/capture, releases its tracked
applications, then launches, captures and closes an Apple platform app, which the
helper launches through LaunchServices instead of spawning it, and then
releases the lease, which must verify clean.
"""

import json
import time
from pathlib import Path

from acceptance_common import (
    AWAIT_LEASE,
    acceptance_parser,
    await_output,
    close_out,
    helper_rpc,
    publish_lease,
    raise_failures,
    select_station,
    station_status,
    station_uid,
)
from firm_sandbox import Config, Organization, RunSpec, Sandbox

ROLE = "browser-probe"
READY = "BROWSER_ROLE_READY"
STAGED = "BROWSER_PAGE_STAGED "

# Copy the fixture from the ordinary role's drive into this lease's disposable scratch.
# The helper does not get a file-writing operation; the role uses its lease SSH endpoint.
ROLE_PROGRAM = r'''import json, os, shlex, subprocess, time
from pathlib import Path

credentials = Path(os.environ["FIRM_CREDENTIALS_DIR"])
name = "browser-smoke"
project = Path.cwd() / name
print("BROWSER_ROLE_READY", flush=True)
''' + AWAIT_LEASE + r'''
source = project / "browser-test" / "index.html"
source.parent.mkdir(parents=True, exist_ok=True)
page = credentials / "browser-page"
if page.is_file():
    source.write_bytes(page.read_bytes())
    destination = Path(lease["scratch"]) / "browser-test" / "index.html"
    drive_source = Path(lease["project_dir"]) / name / "browser-test" / "index.html"
    command = "set -eu\nmkdir -p " + shlex.quote(str(destination.parent)) + "\n"
    command += "install -m 644 " + shlex.quote(str(drive_source))
    command += " " + shlex.quote(str(destination)) + "\ntest -s " + shlex.quote(str(destination))
    subprocess.run(lease_ssh(command), check=True, timeout=60)
    print("BROWSER_PAGE_STAGED " + json.dumps({"path": str(destination)}), flush=True)
else:
    print("BROWSER_PAGE_STAGED " + json.dumps({"path": None}), flush=True)
while True:
    time.sleep(60)
'''


def usable_windows(windows):
    """Content windows, excluding menu bars, tooltips and tiny Chromium utility windows."""
    return [w for w in windows if w["width"] >= 200 and w["height"] >= 200]


def main():
    parser = acceptance_parser(__doc__)
    parser.add_argument("--browser", required=True, help="Full browser .app path in guest")
    parser.add_argument("--page", help="Guest URL; default is the fixture staged in lease scratch")
    parser.add_argument("--apple-app", default="/System/Applications/TextEdit.app")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)

    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(firm, args.station)
    role = f"{ROLE}-{args.station}"
    report = {"passed": False, "role": role, "station": args.station,
              "browser": args.browser, "external_page": args.page is not None,
              "stage": "start"}
    station_status(firm, args.station)
    organization = Organization("founder/" + role)
    firm.file_server.provision(role, organization)
    layout = firm.layout(role)
    secrets = {"browser-page": Path(__file__).with_name("browser_page.html").read_bytes()}
    if args.page:
        secrets = {}
    run = firm.start(RunSpec(
        role, (layout.python, "-c", ROLE_PROGRAM), organization=organization,
        secrets=secrets,
    ))
    lease = client = None
    acquire_started = False
    failure = None
    try:
        report["stage"] = "role ready"
        await_output(firm, run, READY, args.evidence, timeout=120)
        report["stage"] = "acquire station lease"
        acquire_started = True
        lease = firm.mac_acquire(role, "browser-acceptance", station=args.station)
        if lease["station"] != args.station:
            raise SystemExit(
                f"Mac station {args.station} was not free; the firm granted station "
                f"{lease['station']}"
            )
        report["lease_id"] = lease["lease_id"]
        report["stage"] = "stage browser page"
        publish_lease(firm, role, lease)
        staged = json.loads(await_output(firm, run, STAGED, args.evidence, timeout=120))
        report["staged_page"] = staged["path"]
        page = args.page or Path(staged["path"]).as_uri()
        client = firm.mac_connect(lease["lease_id"])
        # Let focus and keyboard transitions settle before the next event.
        rpc = helper_rpc(client, args.evidence, pause=0.25)

        report["stage"] = "helper readiness"
        status = rpc("status")
        assert status["ready"] and status["lease_bound"]
        assert status["environment"] == "vm-session"
        assert status["session"]["uid"] == station_uid(lease["station"])
        report["stage"] = "launch browser"
        pid = rpc("launch_app", path=args.browser)["pid"]
        # Wait for a usable launch window before taking the baseline. This avoids a fixed
        # startup delay and makes the later Command-N window genuinely new.
        report["stage"] = "wait for initial browser window"
        deadline = time.monotonic() + 20
        windows = []
        while not usable_windows(windows):
            windows = rpc("list_windows", pid=pid)["windows"]
            if usable_windows(windows):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Browser showed no usable initial window: {windows}")
            time.sleep(0.2)
        existing = {w["window_id"] for w in usable_windows(windows)}
        report["initial_windows"] = windows
        report["stage"] = "create fresh browser window"
        rpc("press_key", pid=pid, key_code=45, modifiers=["command"])
        deadline = time.monotonic() + 10
        while True:
            created = [
                w
                for w in usable_windows(rpc("list_windows", pid=pid)["windows"])
                if w["window_id"] not in existing
            ]
            if created:
                chosen = max(created, key=lambda w: w["width"] * w["height"])
                new_window = chosen["window_id"]
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Expected a fresh browser content window: {created}")
            time.sleep(0.2)
        report["fresh_window"] = chosen
        report["stage"] = "navigate to browser fixture"
        rpc("press_key", pid=pid, key_code=37, modifiers=["command"])
        rpc("type_text", pid=pid, text=page)
        rpc("press_key", pid=pid, key_code=36)

        def wait_title(title):
            deadline = time.monotonic() + 10
            while True:
                windows = rpc("list_windows", pid=pid)["windows"]
                matches = [
                    w
                    for w in windows
                    if w["title"] == title and w["window_id"] == new_window
                ]
                if matches:
                    return matches[0]["window_id"]
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Window title did not become {title!r}: {windows}")
                time.sleep(0.2)

        report["stage"] = "verify fixture and form behavior"
        window = wait_title("Firm helper browser test")
        rpc("capture_window", pid=pid, window_id=window)
        rpc("press_key", pid=pid, key_code=48)
        rpc("press_key", pid=pid, key_code=0, modifiers=["command"])
        rpc("type_text", pid=pid, text="Firm helper browser verified")
        rpc("press_key", pid=pid, key_code=48)
        rpc("press_key", pid=pid, key_code=49)
        window = wait_title("Saved: Firm helper browser verified")
        rpc("capture_window", pid=pid, window_id=window)
        report["stage"] = "release browser"
        assert rpc("release")["clean"]
        assert rpc("list_apps")["apps"] == []

        # The helper is rebound to the lease after release, before the next launch.
        report["stage"] = "launch Apple platform app"
        assert client.bind_lease()["lease_bound"] is True
        pid = rpc("launch_app", path=args.apple_app)["pid"]
        existing = {w["window_id"] for w in rpc("list_windows", pid=pid)["windows"]}
        rpc("press_key", pid=pid, key_code=45, modifiers=["command"])
        deadline = time.monotonic() + 10
        while True:
            created = [
                w
                for w in rpc("list_windows", pid=pid)["windows"]
                if w["window_id"] not in existing and w["title"].startswith("Untitled")
                and w["width"] > 0 and w["height"] > 0
            ]
            if created:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Expected a new untitled {args.apple_app} window")
            time.sleep(0.2)
        rpc("capture_window", pid=pid, window_id=created[0]["window_id"])
        report["stage"] = "release Apple platform app"
        assert rpc("release")["clean"]
        assert rpc("list_apps")["apps"] == []
        status = rpc("status")
        assert status["clean"] and status["ready"], status
        client.close()
        client = None
        report["stage"] = "release Mac lease"
        released = firm.mac_release(lease["lease_id"])
        assert released["clean"] is True, released
        report["release"] = released
        lease = None
        report["stage"] = "complete"
        report["passed"] = True
    except BaseException as exc:
        failure = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        close_out(firm, args.evidence, report, station=args.station, role=role, run=run,
                  lease=lease, acquire_started=acquire_started, client=client)
    raise_failures(failure, report, args.evidence)
    print(json.dumps({**report, "evidence": str(args.evidence)}))


if __name__ == "__main__":
    main()
