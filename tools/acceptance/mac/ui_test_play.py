"""Prove the test-driven play path through an ordinary Linux role's Mac lease.

The disposable firm must already be running, with Xcode, an available iOS
simulator runtime and an enrolled station. The role commits the
LeaseProbeApp fixture on its drive; the station clones it and runs `xcodebuild
test` on a simulator, driving a pinch, a long press and a swipe through the
device's real touch pipeline into the app's gesture recognisers, once against an
idle screen and once against a continuously rendering SpriteKit scene, with every
gesture call timed. It keeps frames from the run (test screenshots, `simctl io
screenshot`, a short `recordVideo`) in the role's workspace, counts the tests that
actually ran against the tests that exist, and runs a second scheme whose test
action names a local Swift package's tests to see whether that count check catches
the silent skip. No helper and no window are involved. Both VMs remain running;
evidence survives failures.
"""

import json
from uuid import uuid4

from acceptance_common import (
    AWAIT_LEASE,
    BOOT_IPHONE,
    STATION_COMMAND,
    STATION_RUN,
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
    station_preamble,
)
from firm_sandbox import Config, Sandbox

ROLE = "ui-test-player"
READY = "PLAY_ROLE_READY"
COMPLETE = "PLAY_ROLE_RESULT "
GESTURES = ("pinch", "press", "swipe")

STATION_PROGRAM = station_preamble("The UI test run") + STATION_RUN + r'''
import plistlib, re, shutil, signal

video_seconds = float(sys.argv[5])
frames = project / "frames"
# simctl hands screenshots and recordings to the simulator service, a separate process
# without the lease shell's access to the drive's mount, so it writes them in the
# station's home and this shell copies them to the drive.
staging = scratch / "frames" / name
frames.mkdir(parents=True, exist_ok=True)
staging.mkdir(parents=True, exist_ok=True)

def capture(*argv, timeout=120):
    """Like run, without echoing a large payload into the role's log."""
    print("STATION_COMMAND " + shlex.join(map(str, argv)), flush=True)
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    result.check_returncode()
    return result.stdout

run("/usr/bin/git", "clone", "--no-hardlinks", project / "LeaseProbeApp", source, timeout=300)
commit = run("/usr/bin/git", "-C", source, "rev-parse", "HEAD")

inventory = json.loads(capture("/usr/bin/xcrun", "simctl", "list", "-j", timeout=180))
def version(runtime):
    return tuple(int(part) for part in runtime["version"].split("."))
runtimes = sorted((r for r in inventory["runtimes"]
                   if r.get("isAvailable") and ".iOS-" in r["identifier"]),
                  key=version, reverse=True)
assert runtimes, ("No available iOS simulator runtime: name iOS in the image recipe and run "
                  "mac-image-apply, or sudo -n /usr/local/libexec/firm-station-add-platform iOS")
runtime = runtimes[0]
''' + BOOT_IPHONE + r'''
def declared(path):
    """Test methods that exist in a fixture source file."""
    return len(re.findall(r"\bfunc\s+test\w*\s*\(", path.read_text()))

def executed(bundle, text):
    """How many test cases actually ran.

    The result bundle is the authority. `xcresulttool get test-results tests` reports one
    node per test case from what the run itself recorded, so it is unaffected by
    xcodebuild's console formatting, which changes between Xcode releases and can be
    suppressed entirely by a reporter flag. The console scan is kept only as
    corroboration in the evidence, and as the fallback if the bundle cannot be read.
    """
    console = len(re.findall(r"Test Case '.*?' (?:passed|failed)", text))
    cases, error = None, None
    try:
        payload = json.loads(capture("/usr/bin/xcrun", "xcresulttool", "get", "test-results",
                                     "tests", "--path", str(bundle), "--format", "json",
                                     timeout=300))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    else:
        def walk(nodes):
            total = 0
            for node in nodes:
                total += 1 if node.get("nodeType") == "Test Case" else 0
                total += walk(node.get("children", []))
            return total
        cases = walk(payload.get("testNodes", []))
    return {"tests": console if cases is None else cases,
            "counted_from": "console" if cases is None else "xcresult",
            "xcresult_cases": cases, "xcresult_error": error, "console_cases": console}

def timings(text):
    found = []
    for phase, gesture, seconds in re.findall(r"GESTURE_TIMING (\S+) (\S+) ([0-9.]+)", text):
        found.append({"phase": phase, "gesture": gesture, "seconds": float(seconds)})
    return found

def xcodebuild_test(scheme, log_name, bundle_name, expected, timeout=2700):
    bundle = derived / bundle_name
    shutil.rmtree(bundle, ignore_errors=True)
    command = ["/usr/bin/xcodebuild", "-project", str(source / "LeaseProbeApp.xcodeproj"),
               "-scheme", scheme, "-configuration", "Debug", "-sdk", "iphonesimulator",
               "-destination", "platform=iOS Simulator,id=" + udid,
               "-derivedDataPath", str(derived), "-resultBundlePath", str(bundle),
               "-test-timeouts-enabled", "YES", "-default-test-execution-time-allowance", "300",
               "CODE_SIGNING_ALLOWED=NO", "test"]
    print("STATION_COMMAND " + shlex.join(command), flush=True)
    log = project / log_name
    started = time.monotonic()
    with log.open("w") as stream:
        completed = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                   timeout=timeout)
    text = log.read_text(errors="replace")
    print(text[-20000:], flush=True)
    ran = executed(bundle, text)
    return {"scheme": scheme, "command": command, "exit_status": completed.returncode,
            "reported_success": "TEST SUCCEEDED" in text, "log": str(log),
            "result_bundle": str(bundle), "seconds": round(time.monotonic() - started, 1),
            "expected_tests": expected, "ran": ran,
            "count_matches": ran["tests"] == expected, "timings": timings(text)}

tests = source / "LeaseProbeUITests"
package_tests = source / "LeaseProbeKit/Tests/LeaseProbeKitTests/ScoreTests.swift"
gesture_tests = declared(tests / "GestureUITests.swift")
smoke_tests = declared(tests / "SmokeUITests.swift")
package_declared = declared(package_tests)
assert gesture_tests and smoke_tests and package_declared, "The fixture declares no tests"

play = xcodebuild_test("LeaseProbeUITests", "xcodebuild-test.log", "play.xcresult",
                       gesture_tests + smoke_tests)

# The silent-skip scheme skips the gesture class and names the local package's tests,
# which Xcode is reported to ignore without a warning. Whatever Xcode does with it, the
# run is reported rather than asserted: the claim under test is that the count check
# notices, not that this Xcode still has the defect.
skip = xcodebuild_test("LeaseProbeSilentSkip", "xcodebuild-silent-skip.log",
                       "silent-skip.xcresult", smoke_tests + package_declared, timeout=1800)
silent_skip = {
    "expected_tests": skip["expected_tests"], "ran": skip["ran"]["tests"],
    "reported_success": skip["reported_success"], "exit_status": skip["exit_status"],
    "reproduced": bool(skip["reported_success"] and skip["exit_status"] == 0
                       and skip["ran"]["tests"] < skip["expected_tests"]),
    "caught_by_count_check": not skip["count_matches"],
}

attachments = derived / "attachments"
shutil.rmtree(attachments, ignore_errors=True)
export = {"exported": False, "error": None}
try:
    capture("/usr/bin/xcrun", "xcresulttool", "export", "attachments",
            "--path", play["result_bundle"], "--output-path", str(attachments), timeout=300)
except Exception as exc:
    export["error"] = f"{type(exc).__name__}: {exc}"
else:
    export["exported"] = True
    kept = 0
    for path in sorted(attachments.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".png", ".jpeg", ".jpg", ".heic"):
            shutil.copyfile(path, frames / ("xcuiscreen-%02d%s" % (kept, path.suffix.lower())))
            kept += 1
    export["screenshots"] = kept

# The app under test is installed by the test run; reinstall and launch it so the
# simulator frames show the rendering scene rather than the home screen.
app = derived / "Build/Products/Debug-iphonesimulator/LeaseProbeApp.app"
assert app.is_dir(), "The test run did not produce LeaseProbeApp.app"
with (app / "Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
run("/usr/bin/xcrun", "simctl", "install", udid, app, timeout=180)
launched = run("/usr/bin/xcrun", "simctl", "launch", udid, info["CFBundleIdentifier"],
               "FIRM_ANIMATE", timeout=120)
time.sleep(3)
screenshot = staging / "simctl-screenshot.png"
run("/usr/bin/xcrun", "simctl", "io", udid, "screenshot", str(screenshot), timeout=180)
shutil.copyfile(screenshot, frames / screenshot.name)
video = staging / "simctl-recording.mp4"
recorder_command = ["/usr/bin/xcrun", "simctl", "io", udid, "recordVideo",
                    "--codec", "h264", "--force", str(video)]
print("STATION_COMMAND " + shlex.join(recorder_command), flush=True)
recorder = subprocess.Popen(recorder_command)
time.sleep(video_seconds)
recorder.send_signal(signal.SIGINT)
recorder.wait(timeout=180)
assert video.is_file() and video.stat().st_size > 0, "recordVideo produced no file"
shutil.copyfile(video, frames / video.name)

# afinfo ships with macOS; nothing is installed to answer this. If it cannot say, the
# answer is unknown rather than no.
audio = {"tool": "/usr/bin/afinfo", "has_audio_track": None, "output": None}
try:
    probe = subprocess.run([audio["tool"], str(video)], capture_output=True, text=True,
                           timeout=120)
except Exception as exc:
    audio["error"] = f"{type(exc).__name__}: {exc}"
else:
    audio["exit_status"] = probe.returncode
    audio["output"] = (probe.stdout + probe.stderr)[-2000:]
    if probe.returncode == 0 and "Data format" in probe.stdout:
        audio["has_audio_track"] = True
    elif probe.returncode != 0:
        audio["has_audio_track"] = False

result = {"station_uid": os.getuid(), "source": str(source), "git_commit": commit,
          "derived_data": str(derived), "play": play, "silent_skip": silent_skip,
          "silent_skip_run": skip, "declared_tests": {"gesture": gesture_tests,
          "smoke": smoke_tests, "package": package_declared},
          "attachments": export, "launch": launched, "video_seconds": video_seconds,
          "audio": audio,
          "frames": [{"name": path.name, "bytes": path.stat().st_size, "path": str(path)}
                     for path in sorted(frames.iterdir()) if path.is_file()],
          "simulator": {"udid": udid, "runtime": runtime["identifier"], "created": created,
                        "state_before_release": "Booted"}}
(project / "station-result.json").write_text(json.dumps(result, indent=2) + "\n")
print("STATION_PLAY_COMPLETE", flush=True)
'''

ROLE_PROGRAM = fixture_preamble("iOS UI test fixture") + r'''
commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
print("PLAY_ROLE_READY", flush=True)
''' + AWAIT_LEASE + STATION_COMMAND + r'''
try:
    subprocess.run(lease_ssh(station_command(os.environ["VIDEO_SECONDS"])),
                   check=True, timeout=5400)
finally:
    for log in sorted(project.glob("xcodebuild-*.log")):
        print("XCODEBUILD_LOG_BEGIN " + log.name, flush=True)
        print(log.read_text(errors="replace"), flush=True)
        print("XCODEBUILD_LOG_END " + log.name, flush=True)
result = json.loads((project / "station-result.json").read_text())
assert result["git_commit"] == commit, "Station checkout does not match the role's commit"
observed = {path.name: path.stat().st_size
            for path in sorted((project / "frames").iterdir()) if path.is_file()}
recorded = {row["name"]: row["bytes"] for row in result["frames"]}
assert observed == recorded and observed, f"The role sees different frames: {observed}"
assert all(size > 0 for size in observed.values()), observed
result.update(role_uid=os.getuid(), role_frames=observed,
              role_frames_directory=str(project / "frames"))
print("PLAY_ROLE_RESULT " + json.dumps(result), flush=True)
while True:
    time.sleep(60)
'''


def check_gestures(play, bound):
    """Every gesture was driven in both phases, and none of the calls stalled."""
    timings = play["play"]["timings"]
    for phase in ("idle", "rendering"):
        driven = {row["gesture"] for row in timings if row["phase"] == phase}
        assert set(GESTURES) <= driven, f"{phase} phase drove only {sorted(driven)}"
    slow = [row for row in timings if row["seconds"] > bound]
    assert not slow, f"Gesture calls exceeded {bound} s: {slow}"
    return {
        "bound_seconds": bound,
        "slowest": max(timings, key=lambda row: row["seconds"]),
        "by_phase": {
            phase: {row["gesture"]: row["seconds"] for row in timings if row["phase"] == phase}
            for phase in sorted({row["phase"] for row in timings})
        },
    }


def main():
    parser = acceptance_parser(__doc__)
    parser.add_argument("--gesture-seconds", type=float, default=30.0,
                        help="Longest a single gesture call may take before this check fails")
    parser.add_argument("--video-seconds", type=float, default=5.0,
                        help="Length of the simctl recording")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    role = f"{ROLE}-{args.station}"
    run, lease = None, None
    acquire_started = False
    report = {"passed": False, "role": role, "probe": "play-" + uuid4().hex[:12]}
    failure = None
    try:
        require_free_station(box, args.station)
        run = start_worker(
            box, role,
            env={"PROBE_NAME": report["probe"], "VIDEO_SECONDS": str(args.video_seconds)},
            secrets={"fixture": json.dumps(lease_probe_fixture()).encode(),
                     "role-program": ROLE_PROGRAM.encode(),
                     "station-program": STATION_PROGRAM.encode()},
        )
        report["run_id"] = run.run_id
        await_output(box, run, READY, args.evidence, timeout=240)
        acquire_started = True
        lease = acquire_station(box, role, "ui-test-acceptance-" + report["probe"], args.station)
        lease_reply = lease.copy()
        report["lease_id"] = lease["lease_id"]
        report["play"] = json.loads(
            await_output(box, run, COMPLETE, args.evidence, timeout=5400)
        )
        # The station program must have worked inside the returned scratch directory.
        assert report["play"]["source"].startswith(lease["scratch"] + "/"), (
            lease["scratch"], report["play"]["source"]
        )
        play = report["play"]["play"]
        assert play["reported_success"] and play["exit_status"] == 0, play
        # Exit status is not evidence: the count is.
        assert play["count_matches"], (
            f"{play['ran']['tests']} tests ran, {play['expected_tests']} exist in the fixture"
        )
        report["gestures"] = check_gestures(report["play"], args.gesture_seconds)
        # The claim is that the count check catches a silent skip, not that this Xcode
        # still has the defect. If Xcode ran the package's tests, that is reported.
        silent_skip = report["play"]["silent_skip"]
        assert not silent_skip["reproduced"] or silent_skip["caught_by_count_check"], (
            silent_skip
        )
        frames = report["play"]["role_frames"]
        assert "simctl-screenshot.png" in frames and "simctl-recording.mp4" in frames, frames
        assert any(name.startswith("xcuiscreen-") for name in frames), frames
        report["release"] = box.mac_release(lease["lease_id"])
        assert report["release"]["clean"], report["release"]
        lease = None
        after = station_after_release(box, lease_reply, report["play"])
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
