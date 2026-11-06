"""Does a timed tap sequence on Device Hub's window reach the simulated app with its cadence?

The desktop tools report the offsets at which they posted a burst of taps, and the
simulated app records when it receives each touch. This check compares those two timing
streams end to end:

- an ordinary Linux role publishes the LeaseProbeApp fixture, as `xcode_build.py` does,
  and the leased Mac builds it, boots an iPhone simulator, installs it and relaunches it
  in tap-timing mode, recording wall-clock and monotonic times for every touch-down;
- through the lease-bound relay the host launches Device Hub out of this Xcode, waits for
  the window showing the device, captures it, and posts a `tap_sequence` at its centre;
- the station reads the app's own wall-clock and monotonic timestamps back out of its
  Documents directory with `xcrun simctl get_app_container`, and compares the monotonic
  spacing: taps received against taps posted, the app-side cadence and the helper's
  offsets.

It passes when every posted tap arrived and, for a multi tap run, the app-side spacing
held the requested cadence within 150 ms. Run from the repository root with `uv run`
against a running firm.
"""

import json
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
    helper_rpc,
    lease_probe_fixture,
    raise_failures,
    require_free_station,
    select_station,
    start_worker,
    station_after_release,
)
from firm_sandbox import Config, Sandbox

ROLE = "device-hub-tapper"
READY = "TAP_ROLE_READY"
APP_READY = "STATION_APP_READY "
COMPLETE = "TAP_ROLE_RESULT "
SPACING_TOLERANCE_MS = 150
MAXIMUM_TAPS = 64  # the helper's own bound on a planned burst
MAXIMUM_BURST_MS = 20_000  # the helper refuses a request intending more input than this

# The build, boot, install and launch are the shared iOS build station program unchanged;
# this tail reuses the names it leaves in scope (`run`, `project`, `udid`, `info`,
# `device`, `result`) to relaunch the app in tap-timing mode and read back what it
# recorded. Its own arguments follow the four the build program takes.
STATION_PROGRAM = BUILD_PROGRAM + r'''
expected = int(sys.argv[5])
cadence = int(sys.argv[6])
wait_seconds = float(sys.argv[7])
bundle_id = info["CFBundleIdentifier"]

# Device Hub ships inside Xcode, and its path has moved between releases: ask this Xcode
# where its applications are rather than naming one.
developer = Path(run("/usr/bin/xcode-select", "-p"))
roots = [developer.parent / "Applications", developer / "Applications"]
applications = sorted({str(bundle) for root in roots if root.is_dir()
                       for bundle in root.glob("*.app")})
hubs = [path for path in applications
        if Path(path).name.replace(" ", "").lower() == "devicehub.app"]

try:
    run("/usr/bin/xcrun", "simctl", "terminate", udid, bundle_id, timeout=120)
except subprocess.CalledProcessError:
    pass  # the build stage's launch may already have exited; the relaunch is what counts
container = Path(run("/usr/bin/xcrun", "simctl", "get_app_container", udid, bundle_id,
                     "data", timeout=120))
recording = container / "Documents" / "firm-taps.json"
recording.unlink(missing_ok=True)
timing_launch = run("/usr/bin/xcrun", "simctl", "launch", udid, bundle_id,
                    "FIRM_TAP_TIMING", timeout=120)
ready = {"udid": udid, "bundle_id": bundle_id, "device_name": device.get("name", name),
         "container": str(container), "recording": str(recording), "launch": timing_launch,
         "device_hub": hubs[0] if hubs else None, "xcode_applications": applications,
         "expected_taps": expected, "cadence_ms": cadence}
print("STATION_APP_READY " + json.dumps(ready), flush=True)

def recorded():
    """What the app has written so far; it rewrites the whole document atomically."""
    try:
        result = json.loads(recording.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"count": 0, "epoch_ms": [], "uptime_ms": []}
    if not isinstance(result, dict) or "uptime_ms" not in result:
        raise RuntimeError("tap fixture returned no monotonic uptime_ms evidence")
    return result

waiting = time.monotonic()
deadline = waiting + wait_seconds
while len(recorded().get("uptime_ms", [])) < expected and time.monotonic() < deadline:
    time.sleep(0.25)
time.sleep(1)  # a late tap may still be on its way once the expected count is reached
result["tap_timing"] = dict(ready, waited_seconds=round(time.monotonic() - waiting, 1),
                            received=recorded())
(project / "station-result.json").write_text(json.dumps(result, indent=2) + "\n")
print("STATION_TAPS_COMPLETE", flush=True)
'''

ROLE_PROGRAM = fixture_preamble("iOS tap timing fixture") + r'''
commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
print("TAP_ROLE_READY", flush=True)
''' + AWAIT_LEASE + STATION_COMMAND + r'''
try:
    # The station's own stdout streams through this call, so the host sees STATION_APP_READY
    # in the role's log and knows when the app is up and waiting for taps.
    subprocess.run(lease_ssh(station_command(os.environ["TAP_COUNT"],
                                             os.environ["TAP_CADENCE_MS"],
                                             os.environ["TAP_WAIT_SECONDS"])),
                   check=True, timeout=3600)
finally:
    if (project / "xcodebuild.log").exists():
        print("XCODEBUILD_LOG_BEGIN", flush=True)
        print((project / "xcodebuild.log").read_text(), flush=True)
        print("XCODEBUILD_LOG_END", flush=True)
result = json.loads((project / "station-result.json").read_text())
assert result["git_commit"] == commit, "Station checkout does not match the role's commit"
result["role_uid"] = os.getuid()
print("TAP_ROLE_RESULT " + json.dumps(result), flush=True)
while True:
    time.sleep(60)
'''


def device_window(windows, device_name):
    """The window showing the device: named for it if Device Hub says so, else the largest.

    Device Hub's window titles are not a contract, so the choice is recorded in
    `result.json` alongside every window that was on offer, and the capture shows what
    the taps were actually aimed at.
    """
    usable = [w for w in windows if w["width"] >= 200 and w["height"] >= 200]
    if not usable:
        return None, "none"
    named = [w for w in usable if device_name and device_name in w["title"]]
    if named:
        return named[0], "device name in title"
    phones = [w for w in usable if "iPhone" in w["title"]]
    if phones:
        return phones[0], "iPhone in title"
    return max(usable, key=lambda w: w["width"] * w["height"]), "largest window"


def nodes(tree):
    """Every node of an accessibility tree, depth first."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("children", []))


def device_entry(tree, device_name):
    """Device Hub's pressable entry for the device: its title text or its sidebar row.

    Device Hub opens on a placeholder and shows the device's screen only once the
    device is selected; pressing the entry that names it does that.
    """
    for node in nodes(tree):
        text = f"{node.get('title') or ''} {node.get('value') or ''}"
        if device_name in text and "AXPress" in (node.get("actions") or []):
            return node
    return None


def sidebar_width(tree, window_width):
    """The sidebar's width from the split view's divider, or Device Hub's usual 240."""
    for node in nodes(tree):
        if node.get("role") == "AXSplitter":
            try:
                value = int(float(node.get("value")))
            except (TypeError, ValueError):
                continue
            if 0 < value < window_width // 2:
                return value
    return 240


def ax_metadata(result, stage, *, entry_found=False):
    """The bounded inspection evidence needed to explain a partial tree."""
    return {
        "stage": stage,
        "pid": result.get("pid"),
        "count": result.get("count"),
        "truncated": result.get("truncated"),
        "truncation_reasons": result.get("truncation_reasons"),
        "elapsed_s": result.get("_elapsed"),
        "entry_found": entry_found,
    }


def compare(cadence, taps, offsets, received):
    """The posted burst against what the app recorded, in spacings rather than clocks.

    The app records both wall-clock and monotonic values. Use only monotonic uptime for
    spacing: wall-clock adjustments must not look like delayed input.
    """
    epochs = received.get("epoch_ms", [])
    uptimes = received.get("uptime_ms")
    if not isinstance(uptimes, list):
        raise ValueError("tap evidence must include uptime_ms")
    if not isinstance(epochs, list) or len(epochs) != len(uptimes):
        raise ValueError("tap evidence epoch_ms and uptime_ms must have equal lengths")
    app_spacing = [later - earlier for earlier, later in zip(uptimes, uptimes[1:], strict=False)]
    helper_spacing = [
        later - earlier for earlier, later in zip(offsets, offsets[1:], strict=False)
    ]
    every_tap_arrived = len(uptimes) == taps
    cadence_evaluable = len(app_spacing) > 0
    finding = {
        "posted": taps, "received": len(uptimes), "cadence_ms": cadence,
        "helper_offsets_ms": offsets, "app_epoch_ms": epochs, "app_uptime_ms": uptimes,
        "app_spacing_ms": app_spacing, "helper_spacing_ms": helper_spacing,
        "worst_cadence_drift_ms": max((abs(gap - cadence) for gap in app_spacing), default=None),
        "worst_helper_gap_ms": max(
            (abs(app - helper) for app, helper in zip(app_spacing, helper_spacing, strict=False)),
            default=None),
        "tolerance_ms": SPACING_TOLERANCE_MS,
        "cadence_evaluable": cadence_evaluable,
    }
    finding["every_tap_arrived"] = every_tap_arrived
    finding["cadence_held"] = (
        not cadence_evaluable
        or finding["worst_cadence_drift_ms"] <= SPACING_TOLERANCE_MS
    )
    return finding


def main():
    parser = acceptance_parser(__doc__)
    parser.add_argument("--cadence-ms", type=int, default=250, help="Planned gap between taps")
    parser.add_argument("--taps", type=int, default=8, help="Taps in the posted burst")
    args = parser.parse_args()
    if not 1 <= args.taps <= MAXIMUM_TAPS:
        raise SystemExit(f"--taps must be between 1 and {MAXIMUM_TAPS}")
    burst_ms = (args.taps - 1) * args.cadence_ms
    if args.cadence_ms < 1 or burst_ms > MAXIMUM_BURST_MS:
        raise SystemExit(
            f"--taps and --cadence-ms plan {burst_ms} ms of input; the helper accepts at most "
            f"{MAXIMUM_BURST_MS} ms in one request"
        )
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)

    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    role = f"{ROLE}-{args.station}"
    run = lease = client = None
    acquire_started = False
    report = {"passed": False, "role": role, "probe": "taps-" + uuid4().hex[:12],
              "cadence_ms": args.cadence_ms, "taps": args.taps}
    failure = None
    try:
        require_free_station(box, args.station)
        # The station waits for the taps with the app up; leave the host room to launch
        # Device Hub, find its window and post the whole burst.
        wait_seconds = 300 + burst_ms / 1000
        run = start_worker(
            box, role,
            env={"PROBE_NAME": report["probe"], "TAP_COUNT": str(args.taps),
                 "TAP_CADENCE_MS": str(args.cadence_ms),
                 "TAP_WAIT_SECONDS": str(wait_seconds)},
            secrets={"fixture": json.dumps(lease_probe_fixture()).encode(),
                     "role-program": ROLE_PROGRAM.encode(),
                     "station-program": STATION_PROGRAM.encode()},
        )
        report["run_id"] = run.run_id
        await_output(box, run, READY, args.evidence, timeout=240)
        acquire_started = True
        lease = acquire_station(box, role, "device-hub-acceptance-" + report["probe"],
                                args.station)
        lease_reply = lease.copy()
        report["lease_id"] = lease["lease_id"]
        # The build and the tap-timing relaunch happen inside the role's lease shell.
        report["app"] = json.loads(
            await_output(box, run, APP_READY, args.evidence, timeout=2700)
        )
        assert report["app"]["device_hub"], (
            "This Xcode has no Device Hub application; it offered "
            f"{report['app']['xcode_applications']}"
        )

        client = box.mac_connect(lease["lease_id"])
        rpc = helper_rpc(client, args.evidence)

        helper = rpc("status")
        assert helper["ready"] and helper["lease_bound"], helper
        # Xcode's Device Hub is not platform-constrained. Its trampoline registers and
        # execs in place; direct mode preserves that PID across the handoff.
        pid = rpc("launch_app", path=report["app"]["device_hub"], launch_mode="direct")["pid"]
        deadline = time.monotonic() + 120
        window = reason = None
        while window is None:
            windows = rpc("list_windows", pid=pid)["windows"]
            window, reason = device_window(windows, report["app"]["device_name"])
            if window is None and time.monotonic() >= deadline:
                raise RuntimeError(f"Device Hub showed no usable window; saw {windows}")
            if window is None:
                time.sleep(1)
        report["window"] = {"chosen": window, "chosen_because": reason, "offered": windows}
        # Device Hub opens on a placeholder; the device's screen appears once its entry
        # is pressed. The screen sits centred in the content area beside the sidebar.
        # The screen subtree can be slow even before selection, and sidebar rows sit
        # beyond the root inspection's depth cap. Continue from its outline handle.
        # Device Hub can answer the first AX request with only its application root while
        # the UI is still registering. Poll the bounded root tree, and when an outline is
        # available inspect just that subtree. Every attempt also has a helper evidence
        # file from rpc(), while these summaries make timeout/partial causes visible in
        # result.json.
        ax_deadline = time.monotonic() + 60
        ax_before_select_attempts = []
        ax_sidebar_attempts = []
        tree = None
        entry = None
        while entry is None and time.monotonic() < ax_deadline:
            ax_before_select = rpc("inspect_ax", pid=pid, depth=8, max_elements=500)
            tree = ax_before_select["tree"]
            entry = device_entry(tree, report["app"]["device_name"])
            ax_before_select_attempts.append(
                ax_metadata(ax_before_select, "root", entry_found=entry is not None))
            if entry is None:
                outline = next(
                    (node for node in nodes(tree) if node.get("role") == "AXOutline"), None)
                if outline is not None:
                    sidebar_tree = rpc(
                        "inspect_ax", pid=pid, handle=outline["handle"], depth=4,
                        max_elements=200)
                    focused_entry = device_entry(
                        sidebar_tree["tree"], report["app"]["device_name"])
                    ax_sidebar_attempts.append(
                        ax_metadata(sidebar_tree, "outline", entry_found=focused_entry is not None))
                    if focused_entry is not None:
                        entry = focused_entry
            if entry is None:
                time.sleep(0.5)
        report["window"]["ax_before_select_attempts"] = ax_before_select_attempts
        report["window"]["ax_sidebar_attempts"] = ax_sidebar_attempts
        if entry is None:
            last_root = ax_before_select_attempts[-1] if ax_before_select_attempts else None
            last_sidebar = ax_sidebar_attempts[-1] if ax_sidebar_attempts else None
            raise RuntimeError(
                "Device Hub did not expose a pressable device entry within 60 seconds: "
                f"root_attempts={len(ax_before_select_attempts)}, "
                f"sidebar_attempts={len(ax_sidebar_attempts)}, "
                f"last_root={last_root}, last_sidebar={last_sidebar}"
            )
        sidebar = sidebar_width(tree, window["width"])
        rpc("action", pid=pid, handle=entry["handle"], action="AXPress")
        # AXPress can replace or resize the window as the device screen appears. Use a
        # fresh listing for the target and record the offered set so stale geometry is
        # visible in the evidence rather than silently aiming at the old window.
        time.sleep(5)
        selected_deadline = time.monotonic() + 30
        selected_windows = []
        selected_window = None
        selected_reason = None
        while selected_window is None:
            selected_windows = rpc("list_windows", pid=pid)["windows"]
            selected_window, selected_reason = device_window(
                selected_windows, report["app"]["device_name"])
            if selected_window is None and time.monotonic() >= selected_deadline:
                raise RuntimeError(
                    f"Device Hub had no usable selected window; saw {selected_windows}")
            if selected_window is None:
                time.sleep(1)
        report["window"]["after_select_offered"] = selected_windows
        report["window"]["after_select"] = selected_window
        report["window"]["after_select_because"] = selected_reason
        report["window"]["window_id_changed"] = (
            selected_window["window_id"] != window["window_id"])
        window = selected_window
        report["window"]["device_entry"] = {
            k: entry.get(k) for k in ("role", "title", "value")
        }
        report["window"]["sidebar_width"] = sidebar = sidebar_width(tree, window["width"])
        # A capture, so the owner can see what the taps were aimed at.
        report["window"]["capture"] = rpc(
            "capture_window", pid=pid, window_id=window["window_id"])["image"]

        centre_x, centre_y = (sidebar + window["width"]) // 2, window["height"] // 2
        planned = [0] + [args.cadence_ms] * (args.taps - 1)
        burst = rpc("tap_sequence", pid=pid, window_id=window["window_id"],
                    taps=[{"x": centre_x, "y": centre_y, "after_ms": ms} for ms in planned])
        assert burst["count"] == args.taps, burst
        report["burst"] = {"x": centre_x, "y": centre_y, "planned_ms": planned,
                           "offsets_ms": burst["offsets_ms"]}
        report["after_taps_capture"] = rpc(
            "capture_window", pid=pid, window_id=window["window_id"])["image"]

        built = json.loads(await_output(box, run, COMPLETE, args.evidence, timeout=1200))
        report["station"] = built
        report["timing"] = compare(args.cadence_ms, args.taps, burst["offsets_ms"],
                                   built["tap_timing"]["received"])

        assert rpc("release")["clean"] is True
        assert rpc("list_apps")["apps"] == []
        client.close()
        client = None
        report["release"] = box.mac_release(lease["lease_id"])
        assert report["release"]["clean"], report["release"]
        lease = None
        after = station_after_release(box, lease_reply, built)
        report["after_release"] = after
        # Release deletes the lease's simulator devices.
        assert after["simulator"] == [], after

        timing = report["timing"]
        assert timing["every_tap_arrived"], (
            f"{timing['received']} of {timing['posted']} taps reached the app: {timing}")
        if args.taps > 1:
            assert timing["cadence_held"], f"tap cadence was not held: {timing}"
        report["passed"] = True
    except BaseException as exc:
        failure = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        close_out(box, args.evidence, report, station=args.station, role=role, run=run,
                  lease=lease, acquire_started=acquire_started, client=client)
    raise_failures(failure, report, args.evidence)
    print(json.dumps({"passed": True, "evidence": str(args.evidence), **report["timing"]},
                     indent=2))


if __name__ == "__main__":
    main()
