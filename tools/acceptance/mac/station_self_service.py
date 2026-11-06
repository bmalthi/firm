"""Check what a role may install on the leased Mac, and nothing more.

An ordinary Linux role holds a lease and, over the lease's SSH endpoint as the station
account: reports Homebrew's version, installs a small formula and runs it, lists the
simulator runtimes, re-runs the platform installer through sudo for a runtime the
image already has (it must succeed), and confirms sudo refuses an unknown platform
and any other command. It also leaves a Homebrew-named LaunchAgent behind, which the
release must remove. After a clean release, a read-only probe as the control account
confirms the formula and the runtime persist and the agent is gone. Both VMs remain
running; evidence survives failures.
"""

import json
from uuid import uuid4

from acceptance_common import (
    AWAIT_LEASE,
    acceptance_parser,
    acquire_station,
    await_output,
    close_out,
    control_probe,
    raise_failures,
    require_free_station,
    select_station,
    start_worker,
)
from firm_sandbox import Config, Sandbox

ROLE = "mac-self-service"
READY = "SELF_SERVICE_ROLE_READY"
COMPLETE = "SELF_SERVICE_ROLE_RESULT "
INSTALLER = "/usr/local/libexec/firm-station-add-platform"
# GNU hello: macOS ships no `hello` anywhere on a lease's PATH, so `command -v` can only
# resolve to Homebrew's. (macOS 27 ships /usr/bin/jq, which shadows a brewed jq.)
FORMULA = "hello"
GREETING = "firm-self-service-probe"

ROLE_PROGRAM = r'''
import json, os, shlex, subprocess, time
from pathlib import Path

credentials = Path(os.environ["FIRM_CREDENTIALS_DIR"])
installer = os.environ["PLATFORM_INSTALLER"]
formula = os.environ["FORMULA"]
greeting = os.environ["GREETING"]
agent = os.environ["LEFTOVER_AGENT"]
# Runs on the Mac: reduce simctl's JSON to the fields the check needs, so the output
# stays small and is parsed whole.
runtimes_program = """import json,subprocess
listing=json.loads(subprocess.check_output(['/usr/bin/xcrun','simctl','list','runtimes','-j']))
keys=('platform','version','identifier','isAvailable')
print(json.dumps([{k: r.get(k) for k in keys} for r in listing['runtimes']]))
"""
print("SELF_SERVICE_ROLE_READY", flush=True)
''' + AWAIT_LEASE + r'''
def mac(command, timeout=600):
    print("MAC_COMMAND " + command, flush=True)
    result = subprocess.run(lease_ssh(command), capture_output=True, text=True, timeout=timeout)
    # Logs keep a tail; output a step parses is kept whole in memory.
    record = {"command": command, "returncode": result.returncode,
              "stdout": result.stdout[-8000:], "stderr": result.stderr[-8000:]}
    print("MAC_RESULT " + json.dumps(record), flush=True)
    return record, result.stdout

def step(command, timeout=600):
    return mac(command, timeout)[0]

steps = {}
steps["identity"] = step("/usr/bin/id -un; /usr/bin/id -Gn")
steps["brew_version"] = step("brew --version")
steps["brew_install"] = step(f"brew install {shlex.quote(formula)}", timeout=1800)
steps["formula_path"] = step(f"command -v {shlex.quote(formula)}")
steps["formula_use"] = step(f"{shlex.quote(formula)} --greeting={shlex.quote(greeting)}")
steps["runtimes"], listing = mac(
    shlex.join(["/usr/local/firm/bin/python3", "-I", "-c", runtimes_program]), timeout=300)
assert steps["runtimes"]["returncode"] == 0, steps["runtimes"]
runtimes = json.loads(listing)
steps["runtime_list"] = runtimes
available = [r["platform"] for r in runtimes if r.get("isAvailable")]
platform = next((name for name in ("iOS", "watchOS", "tvOS", "visionOS") if name in available),
                None)
assert platform, f"The image has no available simulator runtime: {available}"
steps["platform"] = platform
steps["installer_repeat"] = step(f"sudo -n {installer} {platform}", timeout=3600)
steps["installer_bogus"] = step(f"sudo -n {installer} bogus")
steps["installer_two"] = step(f"sudo -n {installer} {platform} {platform}")
steps["sudo_true"] = step("sudo -n true")
steps["sudo_shell"] = step("sudo -n /bin/sh -c /usr/bin/true")
steps["leftover_agent"] = step(
    "/bin/mkdir -p ~/Library/LaunchAgents && /usr/bin/printf '%s' "
    + shlex.quote('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                  '<key>Label</key><string>' + agent[:-len(".plist")] + '</string>'
                  '<key>ProgramArguments</key><array><string>/bin/sleep</string>'
                  '<string>600</string></array></dict></plist>')
    + " > ~/Library/LaunchAgents/" + shlex.quote(agent))
print("SELF_SERVICE_ROLE_RESULT " + json.dumps(steps), flush=True)
while True:
    time.sleep(60)
'''


def check_steps(steps, formula, greeting, expected_user):
    """Every assertion about what the lease could and could not do."""
    identity = steps["identity"]
    assert identity["returncode"] == 0, identity
    user, groups = identity["stdout"].splitlines()[:2]
    assert user == expected_user and "admin" not in groups.split(), identity
    version = steps["brew_version"]
    assert version["returncode"] == 0 and version["stdout"].startswith("Homebrew "), version
    assert steps["brew_install"]["returncode"] == 0, steps["brew_install"]
    path = steps["formula_path"]
    assert path["returncode"] == 0, path
    assert path["stdout"].strip() == f"/opt/homebrew/bin/{formula}", path
    use = steps["formula_use"]
    assert use["returncode"] == 0 and use["stdout"].strip() == greeting, use
    assert steps["runtimes"]["returncode"] == 0, steps["runtimes"]
    assert steps["installer_repeat"]["returncode"] == 0, steps["installer_repeat"]
    for refused in ("installer_bogus", "installer_two", "sudo_true", "sudo_shell"):
        assert steps[refused]["returncode"] != 0, steps[refused]
    assert steps["leftover_agent"]["returncode"] == 0, steps["leftover_agent"]


def installs_after_release(box, lease, formula, platform, agent):
    """What the lease installed, and what it left, once the station is released."""
    script = """import json,os,subprocess,sys
from pathlib import Path
listing=subprocess.check_output(['/usr/bin/xcrun','simctl','list','runtimes','-j'])
runtimes=json.loads(listing)['runtimes']
print(json.dumps({
    'formula': os.access('/opt/homebrew/bin/'+sys.argv[1], os.X_OK),
    'runtime': any(r['platform']==sys.argv[2] and r.get('isAvailable') for r in runtimes),
    'agent_removed': not (Path.home()/'Library'/'LaunchAgents'/sys.argv[3]).exists(),
}))
"""
    return control_probe(box, script, formula, platform, agent,
                         user=lease["user"], timeout=120)


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    role = f"{ROLE}-{args.station}"
    run, lease = None, None
    acquire_started = False
    probe = uuid4().hex[:12]
    agent = f"homebrew.mxcl.firm-acceptance-{probe}.plist"
    report = {"passed": False, "role": role, "probe": probe, "formula": FORMULA}
    failure = None
    try:
        require_free_station(box, args.station)
        run = start_worker(
            box, role,
            env={"PLATFORM_INSTALLER": INSTALLER, "FORMULA": FORMULA, "GREETING": GREETING,
                 "LEFTOVER_AGENT": agent},
            secrets={"role-program": ROLE_PROGRAM.encode()},
        )
        report["run_id"] = run.run_id
        await_output(box, run, READY, args.evidence, timeout=240)
        acquire_started = True
        lease = acquire_station(box, role, "self-service-acceptance-" + probe, args.station)
        lease_reply = lease.copy()
        report["lease_id"] = lease["lease_id"]
        report["steps"] = json.loads(
            await_output(box, run, COMPLETE, args.evidence, timeout=5400)
        )
        check_steps(report["steps"], FORMULA, GREETING, lease["user"])
        report["release"] = box.mac_release(lease["lease_id"])
        assert report["release"]["clean"], report["release"]
        lease = None
        after = installs_after_release(
            box, lease_reply, FORMULA, report["steps"]["platform"], agent
        )
        report["after_release"] = after
        assert after == {"formula": True, "runtime": True, "agent_removed": True}, after
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
