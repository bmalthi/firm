"""Check account boundaries and explicitly shared Homebrew through real lease SSH.

Needs a running disposable enrolled Mac with at least two free, clean stations.
--station N is checked in both directions against every other account while all
leases are held. Also: no file that holds a password opens through any lease; `su` from
a station to another station and to firmvm is refused even with the target's correct
password (so no guess is made and no account is locked out), while firmvm's still works;
root has no password; and sshd's effective settings for each station on loopback deny
passwords and keyboard-interactive. Leaves the VMs running; removes its unique shared
Homebrew file.
"""

import json
import shlex
from contextlib import closing
from uuid import uuid4

from acceptance_common import (
    acceptance_parser,
    select_station,
    station_holders,
    station_home,
    station_uid,
    write_json,
)
from firm_sandbox import Config, Sandbox

PYTHON = "/usr/local/firm/bin/python3"
DENIALS = r'''
import errno, json, os, signal, socket, sys
target = json.loads(sys.argv[1])
assert os.getuid() == int(sys.argv[2])
result = {}
for kind in ('home', 'drive', 'log'):
    try:
        with open(target[kind], 'rb') as stream:
            stream.read(1)
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM), (kind, str(exc))
        result[kind] = {'denied': True, 'errno': exc.errno}
    else:
        raise AssertionError('Cross-account read succeeded: ' + kind)
try:
    # SIGCONT is harmless if isolation is broken; it still requires signal permission.
    os.kill(target['pid'], signal.SIGCONT)
except OSError as exc:
    assert exc.errno == errno.EPERM, str(exc)
    result['signal'] = {'denied': True, 'errno': exc.errno}
else:
    raise AssertionError('Cross-account signal succeeded')
with socket.socket(socket.AF_UNIX) as peer:
    peer.settimeout(5)
    try:
        peer.connect(target['helper'])
        peer.sendall(b'{"id":"isolation","op":"status"}\n')
        reply = peer.recv(4096)
        assert reply == b'', ('Another account helper answered', reply)
        result['helper'] = {'denied': True, 'closed': True}
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM, errno.ECONNRESET,
                             errno.EPIPE), str(exc)
        result['helper'] = {'denied': True, 'errno': exc.errno}
print(json.dumps(result))
'''
# Run through the lease's own SSH: no file that holds a password opens for a station, its
# own included. A path that does not exist is recorded as absent, not as a denial.
CREDENTIALS = r'''
import errno, json, os, sys
assert os.getuid() == int(sys.argv[2])
result = {}
for path in json.loads(sys.argv[1]):
    try:
        with open(path, 'rb') as stream:
            stream.read(1)
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT), (path, str(exc))
        result[path] = 'absent' if exc.errno == errno.ENOENT else 'denied'
    else:
        raise AssertionError('A station opened a credential file: ' + path)
print(json.dumps(result))
'''
# Run as the guest's root. su is answered with the TARGET's correct password, so a refusal
# is the policy's and not a wrong guess: a wrong guess is a failed login, and macOS locks an
# account out after a few. su says "Authentication failed" for an account rule and for a
# wrong password alike, so the cause is shown by contrast: the same password is accepted
# by the directory just before; the rule is in the policy; a station is refused su to
# ITSELF, which no target-specific cause explains; and firmvm, outside the group, is let
# through. An exit without that line (a timeout, a signal, an authentication service that
# failed after the prompt) is the probe's own failure, never a refusal. Every child is
# bounded and reaped, and what su said is kept in the evidence, the password blanked.
SU_POLICY = r'''
import json, os, pty, select, signal, subprocess, sys, time
RULE = 'account    required       pam_group.so no_warn deny group=firmstations ruser fail_safe'
REFUSAL = 'Authentication failed'  # su's one line for a refusal by the rule or the password
def secret(path):
    with open(path) as source:
        return source.read().strip()
def su(uid, source, target, password):
    argv = ['/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-u', source,
            '/usr/bin/su', target, '-c', '/usr/bin/id -un']
    pid, fd = pty.fork()  # the child leads a new session, so its group is its own
    if pid == 0:
        os.execv(argv[0], argv)
    output, sent, status, deadline = b'', False, None, time.monotonic() + 30
    closed = False  # the pty reaches its end (EOF or EIO) as the child exits, before it is reaped
    try:
        while status is None and time.monotonic() < deadline:
            if closed:
                time.sleep(0.1)
            else:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        chunk = b''
                    if chunk:
                        output += chunk
                        if not sent and b'assword' in output:
                            os.write(fd, password.encode() + b'\n')
                            sent = True
                    else:
                        closed = True
            done, waited = os.waitpid(pid, os.WNOHANG)
            if done:
                status = waited
        if status is None:
            # Only a child still running at the deadline lands here. It leads the pty's
            # session, so killing its group reaches what it started. Nothing was decided
            # by the policy: a timeout is this probe's failure.
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise RuntimeError('su did not finish within 30 seconds; the probe proves nothing')
    finally:
        os.close(fd)
    assert sent, 'su never asked for a password: nothing was decided by the policy'
    if os.WIFSIGNALED(status):
        raise RuntimeError('su ended by a signal; the probe proves nothing')
    after = output.split(b'\n', 1)[-1]  # what su said after the prompt was answered
    said = after.replace(password.encode(), b'<password>').decode('utf-8', 'replace').strip()
    return os.waitstatus_to_exitcode(status), target.encode() in after, said
def refused(case, code, entered, said):
    # su exits 1 for a rule's refusal and for an authentication service that failed after
    # the prompt alike; only its refusal message tells them apart. Anything else it said
    # is not the policy deciding, and the probe fails rather than record a refusal.
    assert code != 0 and not entered, f'A station reached {case} through su: {said}'
    if REFUSAL not in said:
        raise RuntimeError(f'su ended for {case} without the policy deciding: {said!r}')
    result[case] = {'verdict': 'refused', 'said': said}
def accepted(user, password):
    return subprocess.run(['/usr/bin/dscl', '.', '-authonly', user, password],
                          capture_output=True, timeout=30).returncode == 0
source, other = int(sys.argv[1]), int(sys.argv[2])
station = lambda n: secret(f'/var/db/firm-station/passwords/firmstation{n}')
control = secret('/Users/firmvm/password')
result = {}
rules = [line.strip() for line in open('/etc/pam.d/su') if line.split()[:1] == ['account']]
assert rules[:1] == [RULE], rules
result['rule'] = 'first account rule'
for user, password in ((f'firmstation{other}', station(other)),
                       (f'firmstation{source}', station(source)), ('firmvm', control)):
    assert accepted(user, password), f'the directory refuses {user}: nothing below would prove'
result['passwords'] = 'accepted by the directory'
me = f'firmstation{source}'
refused('station_to_station', *su(501 + source, me, f'firmstation{other}', station(other)))
refused('station_to_itself', *su(501 + source, me, me, station(source)))
refused('station_to_control', *su(501 + source, me, 'firmvm', control))
code, entered, said = su(501, 'firmvm', f'firmstation{other}', station(other))
assert code == 0 and entered, f'firmvm could not su: the refusals above prove nothing: {said}'
result['control_to_station'] = {'verdict': 'allowed', 'said': said}
print(json.dumps(result))
'''
# What sshd will actually do for a station on loopback, not what its file says.
SSHD_REQUIRED = {
    "passwordauthentication": "no", "kbdinteractiveauthentication": "no",
    "pubkeyauthentication": "yes", "permituserrc": "no",
    "allowtcpforwarding": "no", "allowagentforwarding": "no", "x11forwarding": "no",
}


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    if len(box.mac.stations) < 2:
        parser.error("Account isolation needs at least two stations")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    shared = "/opt/homebrew/bin/firm-acceptance-" + uuid4().hex
    report = {"passed": False, "station": args.station, "boundaries": []}
    try:
        with station_holders(box, args.evidence) as holders:
            targets = {}
            try:
                for number, holder in holders.items():
                    lease = holder.lease
                    home = str(station_home(lease) / "isolation-private")
                    drive = lease["project_dir"] + "/isolation-private"
                    own = holder.command(
                        f"printf lease-probe-home > {shlex.quote(home)}; "
                        f"printf lease-probe-drive > {shlex.quote(drive)}; "
                        "nohup /bin/sleep 1800 >/dev/null 2>&1 & echo $!"
                    )
                    pid = int(own["stdout"].strip())
                    log = f"/var/log/firm-station/{lease['user']}/commands.log"
                    helper = f"/Users/Shared/firm-station-helper/{lease['user']}/station.sock"
                    target = {"home": home, "drive": drive, "log": log,
                              "helper": helper, "pid": pid}
                    # A denial only counts for existing, working targets.
                    holder.command(f"cat {shlex.quote(home)} {shlex.quote(drive)}; kill -0 {pid}")
                    holder.owner(f"test -s {shlex.quote(log)} && test -S {shlex.quote(helper)}")
                    with closing(box.mac_connect(lease["lease_id"])) as client:
                        status = client.request("status")
                        assert status["ready"], status
                        assert status["session"]["uid"] == station_uid(number), status
                    targets[number] = target
                report["targets"] = targets
                for other in holders:
                    if other == args.station:
                        continue
                    for source, target in ((args.station, other), (other, args.station)):
                        answer = holders[source].command(shlex.join([
                            PYTHON, "-I", "-c", DENIALS, json.dumps(targets[target]),
                            str(station_uid(holders[source].lease["station"])),
                        ]))
                        report["boundaries"].append({
                            "source": source, "target": target,
                            "checks": json.loads(answer["stdout"]),
                        })
                        # The attempted signal must not have made the target disappear.
                        holders[target].command(f"kill -0 {targets[target]['pid']}")
                # Nothing that holds a password opens for a station, through its own lease.
                secrets = [f"/var/db/firm-station/passwords/firmstation{n}{suffix}"
                           for n in box.mac.stations
                           for suffix in ("", ".pending", ".pending.draft")]
                secrets += ["/Users/firmvm/password", "/etc/kcpassword",
                            "/var/db/firm/control-password-rotation.json",
                            "/var/db/firm/control-password-rotation.json.draft",
                            "/var/db/firm-station/stations.json"]
                # The files that are always there must be found and refused: an absence
                # says nothing about the protection of a file that comes and goes.
                always = {f"/var/db/firm-station/passwords/firmstation{n}"
                          for n in box.mac.stations}
                always |= {"/Users/firmvm/password", "/etc/kcpassword",
                           "/var/db/firm-station/stations.json"}
                report["credentials"] = {}
                for number, holder in holders.items():
                    answer = holder.command(shlex.join([
                        PYTHON, "-I", "-c", CREDENTIALS, json.dumps(secrets),
                        str(station_uid(number)),
                    ]))
                    opened = json.loads(answer["stdout"])
                    missing = {path for path in always if opened.get(path) != "denied"}
                    assert not missing, (number, missing)
                    report["credentials"][number] = opened
                # su is closed to the station group by policy, shown with correct passwords.
                other = next(n for n in holders if n != args.station)
                policy = holders[args.station].owner(shlex.join([
                    PYTHON, "-I", "-c", SU_POLICY, str(args.station), str(other),
                ]))
                report["su"] = json.loads(policy.stdout.decode().strip().splitlines()[-1])
                # A root that was never given a password has no AuthenticationAuthority at
                # all on macOS 27 (dscl exits 0 and says "No such key"), and the directory
                # refuses an empty one for it. Both are required; "||" would hide a failure.
                root = holders[args.station].owner(
                    "/usr/bin/dscl . -read /Users/root AuthenticationAuthority"
                )
                text = root.stdout.decode() + root.stderr.decode()
                assert root.returncode == 0 and "No such key" in text, text
                assert "ShadowHash" not in text, text
                empty = holders[args.station].owner(
                    "/usr/bin/dscl . -authonly root ''", check=False
                )
                assert empty.returncode != 0, "root authenticates with an empty password"
                report["su"]["root"] = "no password"
                # What sshd does for each station on loopback, keyboard-interactive included.
                report["sshd"] = {}
                for number in holders:
                    effective = holders[number].owner(
                        f"/usr/sbin/sshd -T -C user=firmstation{number},addr=127.0.0.1,"
                        "host=localhost"
                    ).stdout.decode().splitlines()
                    settings = dict(line.split(" ", 1) for line in effective if " " in line)
                    wrong = {key: settings.get(key) for key, value in SSHD_REQUIRED.items()
                             if settings.get(key) != value}
                    assert not wrong, (number, wrong)
                    assert settings.get("authorizedkeysfile") == "/etc/ssh/firm-lease-keys/%u"
                    assert settings.get("forcecommand") == "/usr/local/libexec/firm-lease-shell"
                    report["sshd"][number] = {key: settings[key] for key in SSHD_REQUIRED}
                # No chmod or root fixup: the installed inherited ACL must allow every
                # account to create in the prefix and modify another account's file.
                shared_evidence = []
                for creator, holder in holders.items():
                    holder.command(f"printf '{creator}\\n' > {shlex.quote(shared)}")
                    expected = f"{creator}\n"
                    for writer, peer in holders.items():
                        peer.command(f"printf '{writer}\\n' >> {shlex.quote(shared)}")
                        expected += f"{writer}\n"
                    observed = holder.command(f"cat {shlex.quote(shared)}")["stdout"]
                    assert observed == expected, (expected, observed)
                    shared_evidence.append({"creator": creator, "writers": list(holders),
                                            "contents": observed})
                    holder.command(f"rm {shlex.quote(shared)}")
                report["homebrew"] = {"shared": True, "path": shared, "writes": shared_evidence}
            finally:
                # Only this check's unique file, even if a later station could not write it.
                next(iter(holders.values())).owner(f"rm -f {shlex.quote(shared)}")
        report["passed"] = True
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        write_json(args.evidence / "result.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
