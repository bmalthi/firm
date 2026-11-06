"""Role-built macOS app on the drive -> leased station click/capture -> role read.

Run against a disposable firm cloned from an enrolled image, or against the
enrollment firm before sealing to raise the helper's Network Volumes consent. The
VMs must already be running and the station unlocked. Helper status's
`session.on_console` selects console-consent (raise Network Volumes and wait for
Allow) or off-console-verification (the existing grant must already work).
An ordinary Linux role holds the selected Mac lease from its run, builds an AppKit/Metal app
and the Metal compute probe on the station into its workspace on the drive
through the lease's SSH endpoint and runs the probe on the station's GPU, the
helper launches the app from the station's mount of the same drive, and the role
reads the app's and the probe's results back from its own mount.
"""

import base64
import json
import time
from pathlib import Path

from acceptance_common import (
    acceptance_parser,
    publish_lease,
    release_if_still_held,
    select_station,
    station_status,
    station_uid,
    write_json,
)
from firm_sandbox import Config, Organization, RunSpec, Sandbox

ROLE = "app-builder"
METAL_PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "metal_probe.m"
SWIFT = r"""
import AppKit
import Metal
@MainActor final class Probe: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 520, height: 240),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Firm role-built app"
        let button = NSButton(title: "Save project result", target: self, action: #selector(save))
        button.frame = NSRect(x: 100, y: 100, width: 320, height: 40)
        window.contentView?.addSubview(button)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func save() {
        let unreachable = !FileManager.default.fileExists(atPath: PRIVATE_MARKER)
        let result: [String: Any] = [
            "station_uid": getuid(), "private_home_unreachable": unreachable,
            "metal_available": MTLCreateSystemDefaultDevice() != nil]
        let directory = Bundle.main.bundleURL.deletingLastPathComponent()
        let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        // Keep the main thread available to Accessibility while the owner answers TCC.
        Task {
            let saved = await Task.detached {
                do {
                    let marker = try String(
                        contentsOf: directory.appendingPathComponent("read-probe"), encoding: .utf8)
                    guard marker == "role-drive-read" else { return false }
                    try data.write(to: directory.appendingPathComponent("station-result.json"))
                    return true
                } catch { return false }
            }.value
            window.title = saved ? "Firm result saved" : "Firm save failed"
        }
    }
}
@main enum Main {
    @MainActor static func main() {
        let app = NSApplication.shared
        let probe = Probe()
        app.setActivationPolicy(.regular)
        app.delegate = probe
        withExtendedLifetime(probe) { app.run() }
    }
}
"""

# The role waits for the lease key the sandbox writes into its credentials, then
# builds on the station, in the same project directory it sees on its own drive.
BUILD_ON_STATION = r"""
import json, os, shlex, subprocess, time
from pathlib import Path
credentials = Path(os.environ['FIRM_CREDENTIALS_DIR'])
project = Path.cwd() / 'handoff-probe'
project.mkdir(exist_ok=True)
(project / 'RoleProbe.swift').write_bytes((credentials / 'swift-source').read_bytes())
(project / 'metal_probe.m').write_bytes((credentials / 'metal-source').read_bytes())
(project / 'read-probe').write_text('role-drive-read')
(Path.home() / 'private-sentinel').write_text('private')
deadline = time.monotonic() + 600
while not (credentials / 'acceptance-lease-ready').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('No checked lease reply arrived')
    time.sleep(0.2)
lease = json.loads((credentials / 'acceptance-lease').read_text())
command = 'set -eu\ncd ' + shlex.quote(lease['project_dir'] + '/handoff-probe') + '\n'
command += (credentials / 'build-script').read_text()
subprocess.run(['/usr/bin/ssh', '-i', lease['key_path'], '-p', str(lease['ssh_port']),
                '-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
                lease['user'] + '@' + lease['ssh_host'], command], check=True, timeout=300)
for _ in range(50):
    if ((project / 'RoleProbe.app/Contents/MacOS/RoleProbe').is_file()
            and (project / 'metal.json').exists() and (project / 'metal.json').stat().st_size > 0):
        break
    time.sleep(0.2)
else:
    raise AssertionError('The built app and Metal result did not reach the role drive')
print('BUILT_ON_STATION', flush=True)
time.sleep(1200)
"""

BUILD_SCRIPT = """set -eu
app=RoleProbe.app; mkdir -p "$app/Contents/MacOS"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>RoleProbe</string>
<key>CFBundleIdentifier</key><string>dev.firm.handoff-probe</string>
<key>CFBundleName</key><string>RoleProbe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
</dict></plist>
PLIST
xcrun swiftc -target arm64-apple-macos14.0 -parse-as-library RoleProbe.swift \\
    -o "$app/Contents/MacOS/RoleProbe" -framework AppKit -framework Metal
xcrun clang -fobjc-arc -framework Foundation -framework Metal metal_probe.m -o metal_probe
./metal_probe > metal.json
"""


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(firm, args.station)
    station_status(firm, args.station)
    check(firm, args)


def check(firm, args):
    role = f"{ROLE}-{args.station}"
    address = f"founder/{role}"
    organization = Organization(address)
    firm.file_server.provision(role, organization)
    layout = firm.layout(role)
    source = SWIFT.replace("PRIVATE_MARKER", json.dumps(layout.private + "/private-sentinel"))
    project = f"{layout.workspace(address)}/handoff-probe"
    builder = firm.start(RunSpec(
        role, (layout.python, "-c", BUILD_ON_STATION), organization=organization,
        secrets={
            "swift-source": source.encode(), "metal-source": METAL_PROBE.read_bytes(),
            "build-script": BUILD_SCRIPT.encode(),
        },
    ))
    lease = None
    client = None
    try:
        lease = firm.mac_acquire(role, "handoff-acceptance", station=args.station)
        if lease["station"] != args.station:
            raise SystemExit(
                f"Mac station {args.station} was not free; the firm granted station "
                f"{lease['station']}"
            )
        publish_lease(firm, role, lease)
        station_project = lease["project_dir"] + "/handoff-probe"
        deadline = time.monotonic() + 300
        while b"BUILT_ON_STATION" not in firm.logs(builder):
            if not firm.status(builder).get("running"):
                raise RuntimeError("Build failed: " + firm.logs(builder).decode(errors="replace"))
            if time.monotonic() >= deadline:
                raise TimeoutError("The role did not build the app on the station")
            time.sleep(0.5)
        (args.evidence / "role-build.log").write_bytes(firm.logs(builder))
        client = firm.mac_connect(lease["lease_id"])
        status = client.request("status")
        assert status["ready"] and status["environment"] == "vm-session", status
        assert status["session"]["uid"] == station_uid(lease["station"]), status
        mode = "console-consent" if status["session"]["on_console"] else "off-console-verification"
        write_json(args.evidence / "helper-status.json", status)
        print(f"Station {lease['station']}: {mode}", flush=True)
        if status["session"]["on_console"]:
            print("Allow the helper's Network Volumes prompt in the guest when it appears.",
                  flush=True)
        launched = client.request(
            "launch_app", path=station_project + "/RoleProbe.app", launch_mode="direct")
        write_json(args.evidence / "launch.json", launched)
        assert launched["status"] == "ok", launched
        pid = launched["pid"]
        deadline = time.monotonic() + 15
        while True:
            tree = client.request("inspect_ax", pid=pid, depth=6)["tree"]

            def walk(node):
                yield node
                for child in node.get("children", []):
                    yield from walk(child)

            buttons = [
                node
                for node in walk(tree)
                if node.get("title") == "Save project result"
                and "AXPress" in node.get("actions", [])
            ]
            if buttons:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Built app did not expose its save button")
            time.sleep(0.2)
        deadline = time.monotonic() + (600 if status["session"]["on_console"] else 30)
        pressed = client.request("action", pid=pid, handle=buttons[0]["handle"], action="AXPress")
        write_json(args.evidence / "press.json", pressed)
        assert pressed["status"] == "ok", (
            f"{pressed}; a press that cannot complete usually means the app is blocked on "
            "the helper's Files and Folders > Network Volumes prompt in the guest"
        )
        while True:
            windows = client.request("list_windows", pid=pid)["windows"]
            saved = [window for window in windows if window["title"] == "Firm result saved"]
            if saved:
                break
            if any(window["title"] == "Firm save failed" for window in windows):
                raise RuntimeError(
                    f"Drive read/write failed during {mode}; inspect Network Volumes"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("Built app did not save the project result to the drive")
            time.sleep(0.2)
        capture = client.request("capture_window", pid=pid, window_id=saved[0]["window_id"])
        (args.evidence / "role-built-app.png").write_bytes(
            base64.b64decode(capture["png_base64"], validate=True)
        )
        client.close()
        client = None
        firm.stop(builder)

        def read_from_drive(name):
            reader = firm.start(RunSpec(
                role, ("/bin/cat", f"{project}/{name}"), organization=organization,
            ))
            assert firm.wait(reader, timeout=120)["exit_code"] == 0, firm.logs(reader)
            return json.loads(firm.logs(reader))

        result = read_from_drive("station-result.json")
        assert result == {
            "station_uid": station_uid(lease["station"]),
            "private_home_unreachable": True, "metal_available": True,
        }, result
        metal = read_from_drive("metal.json")
        assert metal["passed"] is True and metal["uid"] == station_uid(lease["station"]), metal
        result.update(metal_compute=metal, helper_status=status, mode=mode)
        released = firm.mac_release(lease["lease_id"])
        assert released["clean"] is True, released
        result["release"] = released
        write_json(args.evidence / "result.json", result)
        print(json.dumps(result))
    finally:
        if client is not None:
            client.close()
        release_if_still_held(firm, lease)
        for run in firm.runs():
            if run.role_id == role and firm.status(run).get("running"):
                firm.stop(run)


if __name__ == "__main__":
    main()
