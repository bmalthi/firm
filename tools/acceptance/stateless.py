"""Exercise reusable stateless accounts in a disposable Linux VM, without a model call.

Checks writable-path confinement, private temp files, detached descendants, repeated
UID reuse, four simultaneous slots, cancellation, and cleanup after a VM restart.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from firm_sandbox import Config, RunSpec, Sandbox

PROBE = r"""
import array, ctypes, errno, json, os, re, socket, subprocess, sys, time
from pathlib import Path
home = Path.home()
marker = 'firm-stateless-marker'
paths = [home / marker, Path('/tmp') / marker, Path('/var/tmp') / marker,
         Path('/dev/shm') / marker, Path(os.environ['FIRM_STATE_DIR']) / marker]
assert not any(p.exists() for p in paths), 'previous call files survived'
assert not Path('/home/firm-stateless-witness/reference').exists(), 'other homes visible'
assert not Path('/run/firm-stateless-witness').exists(), 'shared /run visible'
for p in paths:
    p.write_text('synthetic private call data')
for p in ['/etc/firm-stateless-marker', '/run/firm-stateless-marker',
          '/var/lib/firm-stateless-marker']:
    try:
        Path(p).write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('persistent path writable: ' + p)
assert subprocess.run(['unshare', '-Ur', 'true'], capture_output=True).returncode != 0
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
except OSError:
    pass
else:
    raise AssertionError('Unix socket creation must be denied')
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
fd = os.open(home / marker, os.O_RDONLY)
for transfer in [lambda: left.sendmsg([b'fd'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array('i', [fd]))]),
                 lambda: right.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))]:
    try:
        transfer()
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        raise AssertionError('Private descriptor transfer must be denied in both directions')
os.close(fd)
left.close()
right.close()
header = Path('/usr/include/asm-generic/unistd.h').read_text()
libc = ctypes.CDLL(None, use_errno=True)
for name in ['sendmmsg', 'recvmmsg', 'io_uring_setup', 'io_uring_enter', 'io_uring_register',
             'add_key', 'request_key', 'keyctl']:
    number = int(re.search(r'#define __NR_' + name + r'\s+(\d+)', header)[1])
    assert libc.syscall(number, 0, 0, 0, 0, 0, 0) == -1
    assert ctypes.get_errno() == errno.EPERM, name + ' was not explicitly denied'
child = subprocess.Popen([sys.executable, '-c',
                         'import signal,time; from pathlib import Path; '
                         'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                         '(Path.home()/"child-ready").touch(); time.sleep(120)'],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 5
while not (home / 'child-ready').exists():
    assert time.monotonic() < deadline, 'child did not start'
    time.sleep(0.01)
print(json.dumps({'uid': os.getuid(), 'child': child.pid, 'isolated': True}), flush=True)
time.sleep(float(sys.argv[1]))
"""


def wait(box, run, *, output=False):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = box.status(run)
        if output and box.logs(run):
            return status
        if not status["running"]:
            if status.get("unresolved") or status["exit_code"] != 0:
                raise RuntimeError(f"Stateless probe failed: {status}; {box.logs(run)!r}")
            return status
        time.sleep(0.1)
    raise TimeoutError("Stateless probe did not settle")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    results, launched = [], []
    report = {"passed": False, "calls": results}

    def launch(slot, delay=0):
        run = box.start(RunSpec(
            f"s-slot-{slot}", ("/usr/bin/python3", "-c", PROBE, str(delay)),
            run_id="probe-" + uuid4().hex, stateless=True,
        ))
        launched.append(run)
        return run

    def record(run, *, stopped=False):
        status = box.status(run)
        assert not status["running"] and not status.get("unresolved"), status
        output = json.loads(box.logs(run))
        assert output["isolated"]
        # A zombie cannot execute and awaits PID 1's final reap; neither may be live.
        state = box.linux._shell(
            "ps", "-p", str(output["child"]), "-o", "stat=", check=False,
        ).stdout.decode().strip()
        assert not state or state.startswith("Z"), state
        info = box.linux.rpc("info")["roles"][run.role_id]
        assert info["stateless"] and not info.get("subordinate"), info
        listing = box.linux._shell(
            "sudo", "-n", "find", info["home"], "-mindepth", "1", "-print",
        ).stdout
        assert not listing, listing
        results.append({"run": run.run_id, "slot": run.role_id, **output, "stopped": stopped})

    try:
        box.up()
        box.linux._shell("sudo", "-n", "sh", "-c",
                         "mkdir -p /home/firm-stateless-witness; "
                         "echo public-canary > /home/firm-stateless-witness/reference; "
                         "echo runtime-canary > /run/firm-stateless-witness")
        for _ in range(8):
            run = launch(0)
            wait(box, run)
            record(run)
        assert len({row["uid"] for row in results}) == 1
        # Concurrent calls belong to this single acceptance case, not parallel suites.
        concurrent = [launch(slot, 30) for slot in range(4)]
        for run in concurrent:
            wait(box, run, output=True)
        assert all(box.status(run)["running"] for run in concurrent)
        for run in concurrent:
            box.stop(run, timeout=60)
            deadline = time.monotonic() + 60
            while box.status(run)["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            record(run, stopped=True)
        assert len({row["uid"] for row in results[-4:]}) == 4
        for slot in range(4):
            run = launch(slot)
            wait(box, run)
            record(run)
        # A root-created immutable marker deliberately makes account cleanup fail.
        # The account must stay quarantined until the obstacle is explicitly removed.
        blocked = launch(0, 120)
        wait(box, blocked, output=True)
        home = box.linux.rpc("info")["roles"]["s-slot-0"]["home"]
        marker = home + "/firm-stateless-marker"
        box.linux._shell("sudo", "-n", "chattr", "+i", marker)
        try:
            assert box.linux.flush(), "Dirty canary was not durable before emergency poweroff"
            box.down(force=True)
            box.up()
            assert box.status(blocked).get("unresolved"), box.status(blocked)
            report["dirty_reboot_quarantined"] = True
            try:
                box.stop(blocked, timeout=60)
            except RuntimeError:
                pass
            deadline = time.monotonic() + 60
            while box.status(blocked)["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            assert box.status(blocked).get("unresolved"), box.status(blocked)
            try:
                launch(0)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Slot reused despite unconfirmed cleanup")
            report["cleanup_failure_quarantined"] = True
        finally:
            box.linux._shell("sudo", "-n", "chattr", "-i", marker, check=False)
        box.stop(blocked, timeout=60)
        record(blocked, stopped=True)
        run = launch(0)
        wait(box, run)
        record(run)
        # Failure before a supervisor exists must also leave a durable quarantine.
        box.linux._shell("sudo", "-n", "touch", marker)
        box.linux._shell("sudo", "-n", "chattr", "+i", marker)
        blocked_id = "probe-" + uuid4().hex
        try:
            try:
                box.start(RunSpec("s-slot-0", ("true",), run_id=blocked_id, stateless=True))
            except RuntimeError:
                pass
            else:
                raise AssertionError("Before-launch cleanup failure was ignored")
            blocked = next(run for run in box.runs() if run.run_id == blocked_id)
            launched.append(blocked)
            assert box.status(blocked).get("unresolved") and box.logs(blocked) == b""
            try:
                launch(0)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Before-launch failure did not quarantine the slot")
            other = launch(1)
            wait(box, other)
            record(other)
            report["before_launch_failure_quarantined"] = True
        finally:
            box.linux._shell("sudo", "-n", "chattr", "-i", marker, check=False)
        box.stop(blocked, timeout=60)
        assert not box.status(blocked).get("unresolved")
        box.down()
        box.up()
        run = launch(0)
        wait(box, run)
        record(run)
        roles = box.linux.rpc("info")["roles"]
        assert all(not roles[f"s-slot-{slot}"].get("retired") for slot in range(4))
        report["passed"] = True
    finally:
        for run in launched:
            try:
                status = box.status(run)
                if status["running"] or status.get("unresolved"):
                    box.stop(run, timeout=60)
            except (OSError, RuntimeError, ValueError):
                pass
        box.linux.flush()
        box.down(force=True)
        (args.evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
