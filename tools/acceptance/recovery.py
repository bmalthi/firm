"""Kill a synthetic runtime and recover its Linux role's run.

Use only a selected disposable firm. No model credentials are involved. The
child is killed deliberately; the VM disks and the role's output are retained,
and the recovered runtime must settle the adopted run with an empty slice.
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from firm_sandbox import Config, Organization, Run, RunSpec, Sandbox

ROLE = "runtime-recovery"


def child(config, evidence):
    box = Sandbox(config)
    box.up()
    organization = Organization(ROLE)
    box.file_server.provision(ROLE, organization)
    run = box.start(
        RunSpec(
            ROLE,
            (
                "/bin/bash",
                "-lc",
                """set -eu
docker run --name firm-recovery-output alpine:3.20 sh -ec \
    'printf container-retained >/recovery-output.txt'
docker cp firm-recovery-output:/recovery-output.txt recovery-output.txt
docker rm firm-recovery-output
test "$(cat recovery-output.txt)" = container-retained
docker run -d --name firm-recovery-live alpine:3.20 sleep 900
printf RUNTIME_RECOVERY_READY
exec sleep 900
""",
            ),
            organization=organization,
        )
    )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        output = box.logs(run)
        if b"RUNTIME_RECOVERY_READY" in output:
            ready = evidence / "ready.tmp"
            ready.write_text(json.dumps(asdict(run)))
            ready.replace(evidence / "ready.json")
            while True:
                time.sleep(1)
        state = box.status(run)
        if not state.get("running"):
            raise RuntimeError(f"Recovery role exited: {state}: {output!r}")
        time.sleep(0.5)
    raise TimeoutError("Recovery role did not become ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.root / "sandbox.toml")
    if args.child:
        child(config, args.evidence)
        return
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(config)
    process = None
    result = {}
    try:
        with (args.evidence / "runtime.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--root", str(args.root),
                 "--evidence", str(args.evidence), "--child"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            )
            deadline = time.monotonic() + 660
            ready = args.evidence / "ready.json"
            while not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError("Synthetic runtime exited; inspect runtime.log")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Synthetic runtime did not reach its crash point")
                time.sleep(0.5)
            run = Run(**json.loads(ready.read_text()))
            process.kill()
            process.wait(timeout=10)
            assert process.returncode == -signal.SIGKILL
        result["runtime_killed"] = True
        box.up()
        state = box.status(run)
        assert state.get("running")
        result["retained_run_observed"] = True
        terminal = box.stop(run)
        assert not terminal.get("running") and not terminal.get("unresolved")
        settled = box.status(run)
        assert not settled.get("running") and not settled.get("unresolved")
        role = box.linux.info()["guest"]["roles"][ROLE]
        slice_state = box.linux._shell(
            "sudo", "-n", "systemctl", "is-active", role["slice"], check=False, timeout=20
        ).stdout.decode().strip()
        assert slice_state == "inactive", slice_state
        account = role["account"]
        processes = box.linux._shell("sudo", "-n", "pgrep", "-u", account, check=False, timeout=20)
        assert processes.returncode == 1, (processes.stdout, processes.stderr)
        result["slice_empty_after_recovery"] = True
        check = box.start(RunSpec(
            ROLE,
            ("/bin/bash", "-lc",
             'test "$(cat recovery-output.txt)" = container-retained; '
             "docker ps -a --format '{{.Names}}' | grep -q firm-recovery-live; "
             "docker rm -f firm-recovery-live"),
            organization=Organization(ROLE),
        ))
        assert box.wait(check, timeout=120)["exit_code"] == 0, box.logs(check).decode()
        result["container_output_retained"] = True
        box.down()
        assert box.state() == "stopped"
        result["vms_stopped"] = True
        result["passed"] = True
        (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        box.down(force=True)


if __name__ == "__main__":
    main()
