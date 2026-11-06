"""Guest controller of the Mac VM, installed root-owned; JSON over the control channel.

The macOS VM holds several station accounts, each leased to one activation at a
time. This controller binds the guest to its firm and carries out each lease's steps.
The station's drive is mounted from the file server with the holder's own credentials.
This module is self-contained so the image need not contain the host's environment.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import grp
import json
import os
import plistlib
import pwd
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

STATE = Path("/var/db/firm")
DATA_VOLUME = "/System/Volumes/Data"
LIMIT = 8 * 1024 * 1024
CONTROL_USER = "firmvm"
CONTROL_UID = 501
STATION_GROUP = "firmstations"
STATIONS = Path("/var/db/firm-station/stations.json")
PASSWORDS = Path("/var/db/firm-station/passwords")
COMMAND_LOGS = Path("/var/log/firm-station")
SESSION_PYTHON = "/usr/local/firm/session/.venv/bin/python3"
RELAY = "/usr/local/libexec/firm-station-relay.py"
LEASE_KEYS = Path("/etc/ssh/firm-lease-keys")
CONTROL_PASSWORD = Path("/Users/firmvm/password")
CONTROL_KEYCHAIN = Path("/Users/firmvm/Library/Keychains/login.keychain-db")
KCPASSWORD = Path("/etc/kcpassword")
KCPASSWORD_KEY = bytes.fromhex("7d895223d2bcddeaa3b91f")
LOGINWINDOW_PREFERENCES = "/Library/Preferences/com.apple.loginwindow"
# Root-owned, sealed trees launchd may run station work from without a lease:
# the sealed system volume, the mounted cryptexes (their mount point itself is
# world-writable, so they are named before /System), Apple's system extras, and
# the installed station helper. Xcode is added from xcode-select at run time.
SYSTEM_ROOTS = (
    Path("/System/Volumes/Preboot/Cryptexes/App"),
    Path("/System/Volumes/Preboot/Cryptexes/OS"),
    Path("/System"),
    Path("/Library/Apple/System"),
    Path("/Library/LaunchAgents/dev.firm.station-helper.plist"),
    Path("/Applications/Firm Station Helper.app"),
)
# The station's drive is the host's file server through a tunnel; when that path stalls
# the drive's SMB session reconnects, processes touching the drive block meanwhile, and
# `ps` can block behind them for over twenty seconds under concurrent station load.
# These budgets outlast such a reconnect; the host's timeouts sit above them.
LEASE_SWEEP_TIMEOUT = 45.0
COMMAND_SECONDS = 45  # one listing or launchctl call, inside the operation's deadline
# Run with the station account's HOME: delete the parallel-testing
# device set only if there is one.
DELETE_TESTING_SET = (
    '[ ! -d "$HOME/Library/Developer/XCTestDevices" ] || '
    "exec /usr/bin/xcrun simctl --set testing delete all"
)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ValueError("Invalid identifier")
    return value


def firm_identifier(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
        r"[a-z0-9]+(?:[-_][a-z0-9]+)*", value
    ):
        raise ValueError("Invalid firm identifier")
    return value


def save(path, value):
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def station_number(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError("Station must be an integer from 1 to 8")
    return value


LOCK_WAIT = 10.0


def acquire(handle, mode):
    """Wait briefly for a lock, then refuse: a guest call must not outlive its deadline."""
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(handle, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("The Mac is busy with another operation; try again") from None
            time.sleep(0.1)


@contextmanager
def lock(station=None):
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("a") as handle:
        # Station work shares the machine; machine work has it alone.
        acquire(handle, fcntl.LOCK_EX if station is None else fcntl.LOCK_SH)
        if station is None:
            yield
        else:
            with (STATE / f"station-{station_number(station)}.lock").open("a") as station_lock:
                acquire(station_lock, fcntl.LOCK_EX)
                yield


def database():
    path = STATE / "firm.json"
    return json.loads(path.read_text()) if path.exists() else {}


def boot():
    return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"]).decode().strip()


def managed_directory(path, *, uid=0, gid=0, mode=0o700):
    """Make a trusted directory without accepting a substituted ancestor."""
    path = Path(path)
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            os.mkdir(path.name, mode=mode, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if os.fstat(fd).st_uid not in (0, uid):
                raise ValueError("Managed guest directory has an unexpected owner")
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def initialize(request):
    firm_id = firm_identifier(request["firm_id"])
    proxy = validate_proxy(request["proxy"])
    if pwd.getpwnam(CONTROL_USER).pw_uid != CONTROL_UID:
        raise ValueError("Unexpected control account identity")
    data = database()
    if data.get("firm_id", firm_id) != firm_id:
        raise ValueError("This guest is already bound to another firm")
    data["firm_id"] = firm_id
    STATE.mkdir(mode=0o700, exist_ok=True)
    save(STATE / "firm.json", data)
    configure_proxy(proxy)
    # A fresh base has no station account until enrollment creates it; the drive's
    # mount point is made when a lease mounts it.
    return {"firm_id": firm_id}


# ---- egress: the firm's proxy is the only way out -----------------------------------------

NETWORKSETUP = "/usr/sbin/networksetup"
SSHD = "/usr/sbin/sshd"
PROXY_SSH_CONFIG = Path("/etc/ssh/sshd_config.d/050-firm-proxy.conf")
# Nothing listens here, so a lookup is refused at once instead of timing out: the proxy
# resolves names on the host, and the guest's DNS would go nowhere through the filter.
UNREACHABLE_RESOLVER = "127.0.0.1"
NO_PROXY = ("localhost", "127.0.0.1", "::1")


def validate_proxy(proxy):
    if (
        not isinstance(proxy, dict)
        or set(proxy) != {"host", "port"}
        or not isinstance(proxy["host"], str)
        or not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", proxy["host"])
        or type(proxy["port"]) is not int
        or not 1 <= proxy["port"] <= 65535
    ):
        raise ValueError("Invalid egress proxy")
    return proxy


def configure_proxy(proxy):
    """Point every network service at the proxy and SSH sessions at it too.

    The system setting covers what reads it (URLSession, Xcode, softwareupdate); station
    leases and the host reach the Mac over SSH, so sshd hands their shells the proxy
    environment that curl, git, brew and other command-line tools read instead.
    """
    host, port = proxy["host"], str(proxy["port"])
    listing = subprocess.run(
        [NETWORKSETUP, "-listallnetworkservices"], capture_output=True, text=True,
        timeout=COMMAND_SECONDS,
    )
    if listing.returncode:
        raise RuntimeError("Cannot list the Mac's network services")
    # The first line explains the asterisk that marks a disabled service.
    services = [line.lstrip("*") for line in listing.stdout.splitlines()[1:] if line.strip()]
    for service in services:
        for arguments in (
            ("-setwebproxy", service, host, port),
            ("-setsecurewebproxy", service, host, port),
            ("-setwebproxystate", service, "on"),
            ("-setsecurewebproxystate", service, "on"),
            ("-setproxybypassdomains", service, *NO_PROXY),
            ("-setdnsservers", service, UNREACHABLE_RESOLVER),
        ):
            result = subprocess.run(
                [NETWORKSETUP, *arguments], capture_output=True, text=True,
                timeout=COMMAND_SECONDS,
            )
            if result.returncode:
                raise RuntimeError(f"networksetup {arguments[0]} failed for {service}")
    url = f"http://{host}:{port}"
    variables = {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url,
                 "https_proxy": url, "NO_PROXY": ",".join(NO_PROXY), "no_proxy": ",".join(NO_PROXY)}
    text = ("# The firm's egress proxy, restated by the guest controller on every boot.\n"
            "SetEnv " + " ".join(f"{name}={value}" for name, value in variables.items()) + "\n")
    if PROXY_SSH_CONFIG.exists() and PROXY_SSH_CONFIG.read_text() == text:
        return
    temporary = PROXY_SSH_CONFIG.with_name(PROXY_SSH_CONFIG.name + ".new")
    temporary.write_text(text)
    temporary.chmod(0o644)
    temporary.replace(PROXY_SSH_CONFIG)
    # A configuration sshd refuses would lock the host out of the Mac: take it back.
    if subprocess.run([SSHD, "-t"], capture_output=True, timeout=COMMAND_SECONDS).returncode:
        PROXY_SSH_CONFIG.unlink(missing_ok=True)
        raise RuntimeError("sshd refused the egress proxy environment")


# ---- the drive mount -------------------------------------------------------------------


def validate_drive(request):
    drive = request.get("drive")
    if drive is None:
        return None
    if (
        not isinstance(drive, dict)
        or set(drive) != {"user", "password", "port", "share"}
        or not all(
            isinstance(drive[key], str) and drive[key] and not set(drive[key]) & set("\0\n/@:")
            for key in ("user", "password", "share")
        )
        or type(drive["port"]) is not int
        or not 1 <= drive["port"] <= 65535
    ):
        raise ValueError("Invalid drive credentials")
    return drive


class DriveMountError(RuntimeError):
    pass


def mount_drive(info, drive):
    """Mount the firm's shared trees at the station's drive, as the station account.

    mount_smbfs's password prompt is answered on a pseudo-terminal, so the secret
    never reaches argv or the guest disk.
    """
    argv = [
        "/sbin/mount_smbfs",
        f"//{drive['user']}@127.0.0.1:{drive['port']}/{drive['share']}",
        info["drive"],
    ]

    def enter_user():
        os.setgroups(info["groups"])
        os.setgid(info["gid"])
        os.setuid(info["uid"])

    for attempt in range(2):
        code, output = run_with_passwords(argv, [drive["password"]], enter_user)
        if not code and station_drive_mounted(info):
            return
        detail = output.replace(drive["password"], "***").strip().splitlines()
        # The share is the host's file server through a tunnel into this Mac; that path
        # can stall for tens of seconds while both endpoints remain alive.
        # A connection that timed out is tried once more, inside the operation's bound.
        if attempt == 0 and "timed out" in output and not station_drive_mounted(info):
            continue
        raise DriveMountError(
            f"Shared drive mount failed ({code}): {detail[-1] if detail else ''}"
        )


def run_with_passwords(argv, passwords, enter, timeout=120):
    """Run argv on a pseudo-terminal and answer its password prompts in order."""
    import pty
    import termios

    pid, fd = pty.fork()
    if pid == 0:
        try:
            enter()
            os.execv(argv[0], argv)
        finally:
            os._exit(126)
    output, sent, status = b"", 0, None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0, min(1, deadline - time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break  # The child closed its terminal.
            if not chunk:
                break
            output += chunk
            if len(output) > LIMIT:
                raise RuntimeError("Password command output exceeded its limit")
            if sent < min(output.count(b"assword"), len(passwords)):
                attributes = termios.tcgetattr(fd)
                attributes[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, attributes)
                os.write(fd, passwords[sent].encode() + b"\n")
                sent += 1
        # Closing the PTY does not prove the child exited. Keep the exit wait
        # inside the same deadline too, then kill and reap our unreaped child.
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                break
            time.sleep(0.01)
        else:
            status = None
    finally:
        os.close(fd)
        if status is None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")


def fresh_password(typeable):
    """Long and random, or eight digits for an enrollment console turn.

    The owner's VM window takes no pasted text, and macOS asks the account on the console
    for its own password (Lock Screen settings) and for the administrator's (consent,
    Remote Login). A typeable password lasts only while its account is being enrolled.
    """
    # 48 characters, not 64: Screen Sharing's account authentication carries a password in
    # a 64-byte field that needs its terminator, and a station with a 64-character
    # password could not be logged in. 192 random bits is ample.
    return f"{secrets.randbelow(10 ** 8):08d}" if typeable else secrets.token_hex(24)


def rotate_control_password(typeable=False):
    with lock():
        rotate_control_credential(typeable)


def settle_credentials():
    """Finish any password change an earlier call left half done; start none.

    Reached by the host through this controller's passwordless sudo rule before anything
    that needs a recorded password: the sudo the host's shell commands use, a station's
    login, the console check of an enrollment turn. A change is pending when its journal
    or pending file exists; finishing it keeps the shape the pending value has.
    """
    settled = {"control": False, "stations": []}
    with lock():
        journal = STATE / "control-password-rotation.json"
        if os.path.lexists(journal):
            with os.fdopen(credential_fd(journal, os.O_RDONLY)) as source:
                pending = json.load(source).get("new", "")
            rotate_control_credential(shaped(pending, True))
            settled["control"] = True
        for number in range(1, 9):
            try:
                info = station_info(number)
            except (KeyError, RuntimeError):
                continue
            pending = info["password"].with_name(info["password"].name + ".pending")
            if os.path.lexists(pending):
                with os.fdopen(credential_fd(pending, os.O_RDONLY)) as source:
                    value = source.read().strip()
                station_password_once(info, shaped(value, True))
                settled["stations"].append(number)
    return settled


def write_private_file(path, content):
    """Publish a root-only file whole or not at all: a draft beside it, then a rename.

    A journal or pending value that a crash left half written would make every later
    rotation fail on it although the old password still works.
    """
    draft = path.with_name(path.name + ".draft")
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(draft, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def shaped(password, typeable):
    return re.fullmatch(r"[0-9]{8}" if typeable else r"[0-9a-f]{48}", password) is not None


def rotate_control_credential(typeable=False):
    """A self-service change, journaled, that ends with a password of the requested shape.

    A pending journal is finished first, whatever shape it holds: that transaction's new
    password may already be the account's. If what it leaves is not the shape asked for,
    a second transaction gives it; an enrollment interrupted after its typeable password
    must not leave every clone adopting that one.
    """
    if not shaped(rotate_control_once(typeable), typeable):
        rotate_control_once(typeable)


def rotate_control_once(typeable):
    with os.fdopen(credential_fd(CONTROL_PASSWORD, os.O_RDONLY, owner=CONTROL_UID)) as source:
        current = source.read().strip()
    os.close(credential_fd(CONTROL_PASSWORD, os.O_WRONLY, owner=CONTROL_UID))
    # A base image has no automatic login yet, and firmvm has no login keychain until
    # its first console login: each is kept in step only once it exists. During a
    # station's enrollment turn automatic login is that station's, not firmvm's.
    selected = deadline_command(
        ["/usr/bin/defaults", "read", LOGINWINDOW_PREFERENCES, "autoLoginUser"],
        check=False, capture_output=True, text=True,
    )
    automatic_login = (os.path.lexists(KCPASSWORD) and selected.returncode == 0
                       and selected.stdout.strip() == CONTROL_USER)
    if automatic_login:
        os.close(credential_fd(KCPASSWORD, os.O_WRONLY))
    journal = STATE / "control-password-rotation.json"
    try:
        with os.fdopen(credential_fd(journal, os.O_RDONLY)) as source:
            record = json.load(source)
        resumed = True
    except FileNotFoundError:
        record = {"old": current, "new": fresh_password(typeable)}
        write_private_file(journal, json.dumps(record).encode())
        resumed = False
    if (not isinstance(record, dict) or set(record) != {"old", "new"}
            or not all(isinstance(value, str) and value for value in record.values())
            or not re.fullmatch(r"[0-9a-f]{48}|[0-9]{8}", record["new"])):
        raise RuntimeError("Invalid pending control password rotation")
    old, new = record["old"], record["new"]

    def authenticates(password):
        code, _ = run_with_passwords(
            ["/usr/bin/dscl", ".", "-authonly", CONTROL_USER],
            [password], lambda: None, timeout=30,
        )
        return code == 0

    if not resumed or not authenticates(new):
        if not authenticates(old):
            raise RuntimeError("Neither pending control credential authenticates; "
                               "rotation retained")
        # Self-service prompts old then new; an administrator reset loses the keychain.
        run_with_passwords(
            account_command(CONTROL_UID) +
            ["/usr/sbin/sysadminctl", "-newPassword", "-", "-oldPassword", "-"],
            [old, new], lambda: None, timeout=60,
        )
        if not authenticates(new):
            raise RuntimeError("The control account password change did not take effect; "
                               "rotation retained")
    # Restore sudo and automatic login before checking the keychain, so a retry remains reachable.
    credentials = [(CONTROL_PASSWORD, (new + "\n").encode(), CONTROL_UID)]
    if automatic_login:
        credentials.append((KCPASSWORD, kcpassword(new), 0))
    for path, content, owner in credentials:
        write_credential(path, content, owner=owner)
        with os.fdopen(credential_fd(path, os.O_RDONLY, owner=owner), "rb") as source:
            os.fsync(source.fileno())
            if source.read() != content:
                raise RuntimeError("Control credential write did not verify; rotation retained")
    keychain = str(CONTROL_KEYCHAIN)
    if os.path.lexists(keychain):
        deadline_command(account_command(CONTROL_UID) +
                         ["/usr/bin/security", "lock-keychain", keychain],
                         check=True, capture_output=True)
        code, _ = run_with_passwords(
            account_command(CONTROL_UID) + ["/usr/bin/security", "unlock-keychain", keychain],
            [new], lambda: None, timeout=30,
        )
        if code:
            raise RuntimeError("Control login keychain did not unlock; rotation retained")
    journal.unlink()
    return new


def unmount_drive(info):
    """Best effort as the mounting account; the lease's processes are already gone."""
    if station_drive_mounted(info):
        station_command(info, ["/sbin/umount", "-f", info["drive"]], 10)


def station_drive_mounted(info):
    def mounted(deadline, publish):
        with station_directory(info) as home:
            return drive_mounted_at(info, home)

    result, timed_out = station_worker(info, mounted, 5)
    if timed_out:
        raise TimeoutError("Station mount check timed out")
    return result


def drive_mounted_at(info, home):
    """The SMB volume has a different device from its home mount point."""
    try:
        entry = os.stat(Path(info["drive"]).name, dir_fd=home, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(entry.st_mode) and entry.st_dev != os.fstat(home).st_dev


# ---- the Mac station lease --------------------------------------------------------------
#
# Each station account is leased to one activation at a time. These
# operations run as root, are idempotent, and each returns what it checked. The
# helper only knows its own tracked apps; the baseline sweep is the operational
# guarantee that lease work stopped.


def station_info(n):
    n = station_number(n)
    user = f"firmstation{n}"
    entry = pwd.getpwnam(user)
    groups = os.getgrouplist(user, entry.pw_gid)
    if (entry.pw_uid != 501 + n or entry.pw_dir != f"/Users/{user}"
            or grp.getgrnam(STATION_GROUP).gr_gid not in groups):
        raise RuntimeError("Unexpected station identity")
    return {
        "station": n,
        "user": user,
        "uid": entry.pw_uid,
        "gid": entry.pw_gid,
        "groups": groups,
        "home": entry.pw_dir,
        "drive": str(Path(entry.pw_dir) / "drive"),
        "scratch": str(Path(entry.pw_dir) / "scratch"),
        "password": PASSWORDS / user,
        "command_log": COMMAND_LOGS / user / "commands.log",
        "socket": f"/Users/Shared/firm-station-helper/{user}/station.sock",
    }


def enter_station(info):
    os.setgroups(info["groups"])
    os.setgid(info["gid"])
    os.setuid(info["uid"])
    os.environ["HOME"] = info["home"]


def station_worker(info, work, timeout):
    """Run station-owned work unprivileged; stream progress and always reap it.

    Work accepts a deadline and a progress callback. A killed worker's last
    checkpoint is partial evidence, never a claim that its work completed.
    Workers must not spawn children: external commands use station_command.
    """
    reader, writer = os.pipe()
    deadline = time.monotonic() + timeout
    pid = os.fork()
    if pid == 0:
        os.close(reader)

        def publish(value):
            data = (json.dumps(value) + "\n").encode()
            while data:
                data = data[os.write(writer, data):]

        try:
            enter_station(info)
            publish({"result": work(deadline, lambda value: publish({"result": value})),
                     "done": True})
        except BaseException as exc:
            # Do not expose station-controlled names through exception messages.
            publish({"error": type(exc).__name__})
        finally:
            os._exit(0)
    os.close(writer)
    latest, pending, status = None, b"", None
    expired = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expired = True
                break
            ready, _, _ = select.select([reader], [], [], remaining)
            if not ready:
                expired = True
                break
            chunk = os.read(reader, 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                latest = json.loads(line)
    finally:
        os.close(reader)
        # Even after EOF the worker could have been interrupted before exiting.
        waited, status = os.waitpid(pid, os.WNOHANG)
        if not waited:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
    if expired:
        return (latest or {}).get("result"), True
    if latest is None or "error" in latest or pending or not latest.get("done"):
        raise RuntimeError("Station worker failed: " + (latest or {}).get("error", "no reply"))
    return latest["result"], False


@contextmanager
def station_directory(info, relative="", *, home_fd=None):
    """Open from the home descriptor, refusing links at every component.

    Called only after entering the station credentials. Never reopen a walked
    directory by its absolute path, even if a lease concurrently renames it.
    """
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if home_fd is None else (
        os.dup(home_fd)
    )
    try:
        for component in Path(info["home"]).parts[1:] if home_fd is None else ():
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        home = os.fstat(fd)
        if home.st_uid != info["uid"]:
            raise UnsafeStationDirectory("other_uid")
        for component in Path(relative).parts:
            if component in ("..", "/"):
                raise ValueError("Expected a fixed relative path")
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            entry = os.fstat(fd)
            if entry.st_uid != info["uid"]:
                raise UnsafeStationDirectory("other_uid")
            if entry.st_dev != home.st_dev:
                raise UnsafeStationDirectory("mount")
        yield fd
    finally:
        os.close(fd)


class UnsafeStationDirectory(OSError):
    """A fixed category path could not be traversed safely."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def cleanup_counts():
    return {"count": 0, "bytes": 0, "skipped": {}}


def cleanup_skip(counts, reason, entry=None):
    bucket = counts["skipped"].setdefault(reason, {"count": 0, "bytes": 0})
    bucket["count"] += 1
    bucket["bytes"] += (
        entry.st_size if entry is not None and not stat.S_ISDIR(entry.st_mode) else 0
    )


def clear_user_immutable(fd, flags):
    """Python exposes chflags but not fchflags; use macOS's descriptor syscall."""
    if not flags & stat.UF_IMMUTABLE:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fchflags.argtypes = (ctypes.c_int, ctypes.c_uint)
    libc.fchflags.restype = ctypes.c_int
    if libc.fchflags(fd, flags & ~stat.UF_IMMUTABLE):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def clear_station_tree(info, parent, names, deadline, counts, progress):
    """Iterative fd-relative removal, with no symlink or filesystem traversal.

    Hard links are skipped: clearing flags would change the other name too, and
    its bytes cannot be attributed to this cleanup. Skipped directory sizes are
    not recursive estimates. Concurrent replacements are reported, not retried.
    """
    parent_info = os.fstat(parent)
    device = parent_info.st_dev
    flags = getattr(parent_info, "st_flags", 0)
    if parent_info.st_uid != info["uid"] or flags & stat.SF_IMMUTABLE:
        cleanup_skip(counts, "other_uid" if parent_info.st_uid != info["uid"] else "schg")
        return True
    clear_user_immutable(parent, flags)
    stack = [(os.dup(parent), iter(names), None)]
    try:
        while stack:
            if time.monotonic() >= deadline:
                return False
            fd, children, removal = stack[-1]
            try:
                name = next(children)
            except StopIteration:
                if removal is not None:
                    parent_fd, name, original = removal
                    try:
                        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                            cleanup_skip(counts, "changed", current)
                        else:
                            os.rmdir(name, dir_fd=parent_fd)
                            counts["count"] += 1
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        cleanup_skip(counts, f"errno:{exc.errno}")
                if hasattr(children, "close"):
                    children.close()
                os.close(fd)
                stack.pop()
                progress()
                continue
            name = name.name if isinstance(name, os.DirEntry) else name
            entry = None
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if entry.st_dev != device:
                    cleanup_skip(counts, "mount", entry)
                    continue
                if entry.st_uid != info["uid"]:
                    cleanup_skip(counts, "other_uid", entry)
                    continue
                flags = getattr(entry, "st_flags", 0)
                if flags & stat.SF_IMMUTABLE:
                    cleanup_skip(counts, "schg", entry)
                    continue
                if not stat.S_ISDIR(entry.st_mode) and entry.st_nlink > 1:
                    cleanup_skip(counts, "hard_link", entry)
                    continue
                is_directory = stat.S_ISDIR(entry.st_mode)
                opened = None
                try:
                    if is_directory or flags & stat.UF_IMMUTABLE:
                        # Never open a FIFO/device; a link with flags is skipped.
                        if not is_directory and not stat.S_ISREG(entry.st_mode):
                            cleanup_skip(counts, "flags", entry)
                            continue
                        opened = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                            | (os.O_DIRECTORY if is_directory else 0), dir_fd=fd,
                        )
                        actual = os.fstat(opened)
                        if (actual.st_dev, actual.st_ino, actual.st_uid) != (
                            entry.st_dev, entry.st_ino, info["uid"],
                        ):
                            cleanup_skip(counts, "changed", actual)
                            continue
                        flags = getattr(actual, "st_flags", 0)
                        if flags & stat.SF_IMMUTABLE or (not is_directory and actual.st_nlink > 1):
                            cleanup_skip(
                                counts, "schg" if flags & stat.SF_IMMUTABLE else "hard_link",
                                actual,
                            )
                            continue
                        clear_user_immutable(opened, flags)
                    if is_directory:
                        children = os.scandir(opened)
                        stack.append((opened, children, (fd, name, entry)))
                        opened = None
                    else:
                        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino, current.st_uid) != (
                            entry.st_dev, entry.st_ino, info["uid"],
                        ):
                            cleanup_skip(counts, "changed", current)
                            continue
                        os.unlink(name, dir_fd=fd)
                        counts["count"] += 1
                        counts["bytes"] += entry.st_size
                finally:
                    if opened is not None:
                        os.close(opened)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_skip(counts, f"errno:{exc.errno}", entry)
            progress()
        return True
    finally:
        for fd, children, _ in stack:
            if hasattr(children, "close"):
                children.close()
            os.close(fd)


def reclaim_directory(info, path, home):
    # Darwin's confstr constant (not exposed by Python by name). Resolve only inside
    # the station worker, after setuid; never trust inherited TMPDIR. simctl delete
    # renames devices into Deleting-* here and lets a background job remove them,
    # which release then stops, so this is where deleted simulators actually live.
    user_directories = {"DARWIN_USER_TEMP_DIR": 65537}
    if path in user_directories:
        value = os.confstr(user_directories[path])
        if not value or not os.path.isabs(value):
            raise OSError(errno.EINVAL, "Missing Darwin user directory")
        # Darwin may return /var, whose system alias is /private/var.
        return station_directory({**info, "home": str(Path(value).resolve())})
    return station_directory(info, path, home_fd=home)


def reclaim_station_files(info, deadline, publish):
    """Fixed cleanup policy; names never leave this credential-dropped worker."""
    categories = {
        "simulators": (
            "Library/Developer/CoreSimulator/Devices", "Library/Developer/XCTestDevices",
        ),
        "temp": ("DARWIN_USER_TEMP_DIR",),
        "home": ("",),
        "caches": ("Library/Caches",),
        "logs": ("Library/Logs",),
        "saved_application_state": ("Library/Saved Application State",),
        "trash": (".Trash", "Library/.Trash"),
        "swiftpm": (".swiftpm", "Library/org.swift.swiftpm"),
        "xcode": tuple("Library/Developer/Xcode/" + name for name in (
            "DerivedData", "Archives", "Products", "Previews",
        )),
    }
    keep_home = {
        "Library", "Applications", "Desktop", "Documents", "Downloads", "Movies", "Music",
        "Pictures", "Public", "Sites", Path(info["drive"]).name,
        ".bash_history", ".zsh_history", ".bash_sessions", ".zsh_sessions",
    }
    keep_entries = {
        # CoreSimulatorService may outlive the sweep as a trusted service and owns this
        # index; `simctl delete all` has already emptied the set it describes.
        "Library/Developer/CoreSimulator/Devices": {"device_set.plist"},
        "Library/Caches": {"dev.firm.station-helper"},
        "Library/Logs": {"dev.firm.station-helper"},
        "Library/Saved Application State": {"dev.firm.station-helper.savedState"},
    }
    report = {
        "refused": False, "completed": False, "timed_out": False,
        "categories": {name: cleanup_counts() for name in categories},
        "kept": {
            "home": sorted(keep_home), "entries": {k: sorted(v) for k, v in keep_entries.items()},
            "outside_categories": ["Homebrew", "simulator runtimes", "preferences", "keychain",
                                   "consent", "CoreSimulator/Caches",
                                   "the Darwin user cache directory",
                                   "other users' temporary directories"],
        },
        "scratch": info["scratch"],
    }
    with station_directory(info) as home:
        if drive_mounted_at(info, home):
            return {**report, "refused": True, "reason": "drive_mounted"}
        publish(report)
        last_progress = time.monotonic()

        def progress():
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= 0.1:
                publish(report)
                last_progress = now

        for category, paths in categories.items():
            counts = report["categories"][category]
            for path in paths:
                if time.monotonic() >= deadline:
                    return {**report, "timed_out": True}
                try:
                    with reclaim_directory(info, path, home) as fd:
                        kept = keep_home | {".Trash", ".swiftpm"} if not path else (
                            keep_entries.get(path, set())
                        )
                        with os.scandir(fd) as entries:
                            names = (entry.name for entry in entries if entry.name not in kept)
                            if not clear_station_tree(info, fd, names, deadline, counts, progress):
                                return {**report, "timed_out": True}
                except FileNotFoundError:
                    pass
                except UnsafeStationDirectory as exc:
                    cleanup_skip(counts, exc.reason)
                except OSError as exc:
                    cleanup_skip(counts, f"errno:{exc.errno}")
                publish(report)
        try:
            # Recreate only via the home descriptor, still as the station.
            os.mkdir(Path(info["scratch"]).name, mode=0o700, dir_fd=home)
        except FileExistsError:
            try:
                with station_directory(info, Path(info["scratch"]).name, home_fd=home):
                    pass
            except OSError as exc:
                cleanup_skip(report["categories"]["home"], f"errno:{exc.errno}")
        except OSError as exc:
            cleanup_skip(report["categories"]["home"], f"errno:{exc.errno}")
        report["completed"] = time.monotonic() < deadline
        report["timed_out"] = not report["completed"]
        return report


def station_reclaim(info):
    # 60 s permits large local trees; the worker stops at 58 s, leaving 2 s to
    # deliver its final counters. The supervisor enforces 60 s even on a stuck IO.
    report, timed_out = station_worker(
        info, lambda deadline, publish: reclaim_station_files(info, deadline - 2, publish), 60,
    )
    if report is None:
        raise RuntimeError("Station reclaim returned no evidence")
    if timed_out:
        report.update(completed=False, timed_out=True)
    return report


def station_account(n):
    """Whether enrollment has created the station account; a lease needs it."""
    try:
        station_info(n)
    except KeyError:
        return {"present": False}
    return {"present": True}


def station_processes(info, deadline=None):
    """``(pid, start, executable)`` for every process of the station account."""
    station_uid = info["uid"]
    result = deadline_command(
        ["/bin/ps", "-axo", "uid=,pid=,lstart=,comm="], deadline=deadline,
        check=True,
        capture_output=True,
        text=True,
    )
    processes = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) < 7:
            continue
        try:
            uid, pid = int(fields[0]), int(fields[1])
        except ValueError as exc:
            raise RuntimeError("Unexpected process listing for the station") from exc
        if uid == station_uid:
            processes.append((pid, " ".join(fields[2:7]), fields[7] if len(fields) > 7 else ""))
    return processes


# The exit-status column is opaque; cleanup uses only the pid and label.
def domain_services(domain, output):
    """Read only the domain's own service table, not its inherited endpoints."""
    lines = output.splitlines()
    if not lines or lines[0] != domain + " = {":
        raise RuntimeError(f"Unexpected launchd domain listing: {domain}")
    services, unparsed = {}, []
    inside = False
    for line in lines[1:]:
        if line == "\tservices = {":
            inside = True
        elif inside:
            if line == "\t}":
                return {"services": services, "unparsed": unparsed}
            fields = line.split(None, 2)
            if (len(fields) != 3 or not fields[0].isdigit()
                    or not fields[2] or any(c.isspace() for c in fields[2])
                    or "/" in fields[2]):
                unparsed.append({"domain": domain, "line": line})
                continue
            target = domain + "/" + fields[2]
            if target in services:
                raise RuntimeError(f"Duplicate launchd service in {domain}")
            services[target] = int(fields[0]) or None
    raise RuntimeError(f"Missing launchd service table in {domain}")


def station_services(info, deadline=None):
    """Keep readable domains and raw rows even when another domain cannot be read."""
    services, unparsed, errors = {}, [], []
    for kind in ("user", "gui"):
        domain = f"{kind}/{info['uid']}"
        try:
            result = deadline_command(
                ["/bin/launchctl", "print", domain], deadline=deadline,
                check=True, capture_output=True, text=True,
            )
            listing = domain_services(domain, result.stdout)
            services.update(listing["services"])
            unparsed.extend(listing["unparsed"])
        except Exception as exc:
            errors.append(f"{domain}: {exc}")
    return {"services": services, "unparsed": unparsed, "errors": errors}


def station_launch_agents(info, deadline=None):

    def listing(_deadline, _publish):
        try:
            with station_directory(info, "Library/LaunchAgents") as fd:
                return sorted(os.listdir(fd))
        except FileNotFoundError:
            return []

    result, timed_out = station_worker(info, listing, time_left(deadline))
    if timed_out:
        raise TimeoutError("Station LaunchAgents listing timed out")
    return result


def station_crontab(info, deadline=None):
    # macOS crontab resolves -u by name only; a numeric uid is an unknown user.
    result = deadline_command(
        ["/usr/bin/crontab", "-u", info["user"], "-l"],
        deadline=deadline, check=False, capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "no crontab for" in result.stderr:
        return ""
    raise RuntimeError(f"Station crontab read failed: {result.stderr.strip()}")


def station_sessions(info):
    return [pid for pid, _, command in station_processes(info)
            if os.path.basename(command) in ("sshd", "sshd-session")]


def installed_xcode(deadline=None):
    result = deadline_command(
        ["/usr/bin/xcode-select", "-p"], deadline=deadline,
        check=False, capture_output=True, text=True,
    )
    path = result.stdout.strip()
    return path.split("/Contents/", 1)[0] if result.returncode == 0 and path else None


def station_baseline(info):
    deadline = time.monotonic() + LEASE_SWEEP_TIMEOUT
    services = station_services(info, deadline)
    if services["unparsed"] or services["errors"]:
        raise RuntimeError(f"Station services could not be verified: {services}")
    return {
        "processes": [[pid, start] for pid, start, _ in station_processes(info, deadline)],
        "services": sorted(services["services"]),
        "launch_agents": station_launch_agents(info, deadline),
        "crontab": station_crontab(info, deadline),
    }


def service_details(target, deadline=None):
    result = deadline_command(
        ["/bin/launchctl", "print", target], deadline=deadline,
        check=False, capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    if result.returncode or not lines or lines[0] != target + " = {":
        return None
    return dict(
        line.strip().split(" = ", 1)
        for line in lines[1:]
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line
    )


# The image build copies applications, Xcode included, as the control account and
# keeps that owner, so Xcode's files belong to root or the control account; nothing
# under a system root belongs to anyone but root. The station's platform installer
# trusts the same two owners for Xcode.
SYSTEM_OWNERS = (0,)
XCODE_OWNERS = (0, CONTROL_UID)


def trusted_service_path(value, xcode):
    """Require System/Xcode provenance and trusted-owner, non-writable descendants."""
    if not value or not os.path.isabs(value):
        return False
    try:
        path = Path(value).resolve(strict=True)
        root = next((root for root in SYSTEM_ROOTS if path.is_relative_to(root)), None)
        owners = SYSTEM_OWNERS
        if root is None:
            if not xcode:
                return False
            root = Path(xcode).resolve(strict=True)
            owners = XCODE_OWNERS
            if not path.is_relative_to(root):
                return False
        while True:
            info = path.stat()
            if info.st_uid not in owners or info.st_mode & 0o022:
                return False
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return False
            if path == root:
                return True
            path = path.parent
    except (OSError, RuntimeError):
        return False


def trust_fingerprint(value, xcode):
    """File identity plus metadata for every directory affecting a trust decision."""
    if not value or not os.path.isabs(value):
        return None
    try:
        paths = []
        for source in (value, xcode):
            if source is None:
                continue
            path = Path(source).resolve(strict=True)
            paths.extend((path, *path.parents))
        result = []
        for path in dict.fromkeys(paths):
            entry = path.stat()
            result.append([str(path), entry.st_dev, entry.st_ino, entry.st_size,
                           entry.st_mtime_ns, entry.st_ctime_ns, entry.st_uid,
                           entry.st_gid, entry.st_mode])
        return result
    except (OSError, RuntimeError):
        return None


def station_trust(info, check, value, xcode, deadline):
    cache = info.get("trust_cache")
    key = (check, value, xcode)
    time_left(deadline)
    # Only previously trusted paths are inspected by the parent. Unknown paths
    # still go to the bounded, credential-dropped worker. Cache successes only.
    if cache is not None and key in cache:
        fingerprint = cache.pop(key)
        if trust_fingerprint(value, xcode) == fingerprint:
            time_left(deadline)
            cache[key] = fingerprint
            return True

    def inspect(end, publish):
        before = trust_fingerprint(value, xcode) if cache is not None else None
        result = check(value, xcode)
        fingerprint = before if (
            result and before is not None and before == trust_fingerprint(value, xcode)
        ) else None
        return result, fingerprint

    result, timed_out = station_worker(
        info, inspect, time_left(deadline),
    )
    if timed_out:
        raise TimeoutError("Station provenance check timed out")
    trusted, fingerprint = result
    if cache is not None and trusted and fingerprint is not None:
        cache[key] = fingerprint
    return trusted


def trusted_service_registrations(services, baseline, xcode, info, deadline=None):
    """Explain Apple services launchd registers on demand in the station's own domains.

    macOS registers some XPC services into the station's ``user/`` or ``gui/`` domain when
    something in the session looks them up; Notes' datastore, shipped inside a system
    framework on the sealed volume, is one. Such a registration is not lease work when
    launchd names a definition that is a root-owned, non-writable file under a trusted
    root, which a lease cannot produce: its own agents live under
    ``~/Library/LaunchAgents``. A declared program must be trusted too.
    """
    tolerated = []
    for target in sorted(set(services) - set(baseline)):
        domain = target.rsplit("/", 1)[0]
        if domain not in {f"user/{info['uid']}",
                          f"gui/{info['uid']}"}:
            continue
        detail = service_details(target, deadline)
        if detail is None or detail.get("domain", "").split(" ", 1)[0] != domain:
            continue
        if not station_trust(info, trusted_service_path, detail.get("path"), xcode, deadline):
            continue
        program = detail.get("program")
        if program is not None and not station_trust(
            info, trusted_service_path, program, xcode, deadline,
        ):
            continue
        tolerated.append({"target": target, "path": detail["path"], "program": program})
    return tolerated


SERVICE_TARGET = re.compile(r"^(?:system|user|gui|pid|login|session)/\S+ = \{$")
PROGRAM_PATH = "program path = "


def process_provenance(pid, deadline=None):
    """launchd's account of one process: the service it runs, or None when ad hoc.

    ``launchctl procinfo`` ends with the managing service's dictionary, keyed by
    its full target; a process launchd did not spawn ends with a note instead. Its
    ``program path`` is kept as well: that is the executable launchd records for
    this pid, where ``ps`` reports only argv[0], which the program itself chooses
    and which Apple daemons such as ``cloudphotod`` give as a bare name.
    """
    result = deadline_command(
        ["/bin/launchctl", "procinfo", str(pid)], deadline=deadline,
        check=False, capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    if result.returncode or f"(pid {pid} is not managed by launchd)" in lines:
        return None
    header = next((index for index, line in enumerate(lines) if SERVICE_TARGET.match(line)), None)
    if header is None:
        return None
    detail = {"target": lines[header][: -len(" = {")]}
    program = next(
        (line for line in lines[:header] if line.startswith(PROGRAM_PATH)), None
    )
    if program is not None:
        detail["program path"] = program[len(PROGRAM_PATH):]
    for line in lines[header + 1:]:
        if line == "}":
            break
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            key, value = line.strip().split(" = ", 1)
            detail.setdefault(key, value)
    return detail


def trusted_program(value, xcode=None):
    """A root-owned regular file nobody else can write; the lease cannot supply one.

    Inside the selected Xcode, which the image build copied as the control account,
    the control account's files are trusted too.
    """
    if not value or not os.path.isabs(value):
        return False
    try:
        path = Path(value).resolve(strict=True)
        info = path.stat()
        in_xcode = bool(xcode) and path.is_relative_to(Path(xcode).resolve(strict=True))
    except (OSError, RuntimeError):
        return False
    owners = XCODE_OWNERS if in_xcode else SYSTEM_OWNERS
    return stat.S_ISREG(info.st_mode) and info.st_uid in owners and not info.st_mode & 0o022


def explained_process(pid, provenance, command, explained, xcode, info, deadline=None):
    """Whether launchd spawned the process from a trusted definition the baseline explains.

    A service in the station's own domains must be one the baseline explains; a
    service in a requesting process's domain is an on-demand XPC service, and one
    in the system domain (loginwindow itself) is root's. Either way its definition
    must be a root-owned System, Xcode or helper file, launchd must name this pid
    as the service's process, and the running program must be root-owned itself.

    The service's declared program need not equal the running one: a definition
    may name a launcher, as the station helper's does. Ownership of the running
    binary carries the weight, and a lease cannot produce a root-owned file. That
    binary is launchd's recorded ``program path``, falling back to the listed
    command: argv[0] is the program's own choice and need not be a path at all.
    """
    if provenance is None:
        return False
    domain = provenance["target"].rsplit("/", 1)[0]
    if domain in {f"user/{info['uid']}",
                      f"gui/{info['uid']}"}:
        if provenance["target"] not in explained:
            return False
    elif domain != "system" and not domain.startswith("pid/"):
        return False
    return (
        station_trust(info, trusted_service_path, provenance.get("path"), xcode, deadline)
        and provenance.get("pid") == str(pid)
        and station_trust(
            info, trusted_program, provenance.get("program path") or command, xcode, deadline,
        )
    )


WORK_PARTS = ("services", "launch_agents", "crontab", "processes")


def checked_part(action):
    """A failed read is evidence, not permission to skip the other checks."""
    try:
        return action()
    except Exception as exc:
        return {"ok": False, "verified": False, "error": str(exc)}


def service_report(baseline, info, deadline):
    listing = station_services(info, deadline)
    services = listing["services"]
    result = {"extra": sorted(set(services) - set(baseline["services"])),
              "tolerated": [], "unparsed": listing["unparsed"]}
    try:
        xcode = installed_xcode(deadline)
        result["tolerated"] = trusted_service_registrations(
            services, baseline["services"], xcode, info, deadline,
        )
        result["extra"] = sorted(set(result["extra"]) - {
            entry["target"] for entry in result["tolerated"]
        })
    except Exception as exc:
        listing["errors"].append(str(exc))
    result["verified"] = not listing["errors"] and not listing["unparsed"]
    result["ok"] = result["verified"] and not result["extra"]
    if listing["errors"]:
        result["error"] = "; ".join(listing["errors"])
    return result


# XProtect lives on the sealed system volume. Its plugin service, a launchd service in
# each station's gui domain, spawns remediators as the station account.
XPROTECT = Path("/Library/Apple/System/Library/CoreServices/XProtect.app")
XPROTECT_PLUGIN = "com.apple.XprotectFramework.PluginService"


def recorded_program(pid, deadline=None):
    """The executable launchd records for a pid, managed by launchd or not."""
    result = deadline_command(
        ["/bin/launchctl", "procinfo", str(pid)], deadline=deadline,
        check=False, capture_output=True, text=True,
    )
    line = next((line for line in result.stdout.splitlines()
                 if line.startswith(PROGRAM_PATH)), None)
    return None if result.returncode or line is None else line[len(PROGRAM_PATH):]


def process_parent(pid, deadline=None):
    """The current parent pid reported by ps."""
    result = deadline_command(
        ["/bin/ps", "-o", "ppid=", "-p", str(pid)], deadline=deadline,
        check=False, capture_output=True, text=True,
    )
    value = result.stdout.strip()
    return int(value) if not result.returncode and value.isdigit() else None


def xprotect_remediator(entry, info, deadline=None):
    """The station's own XProtect plugin service's remediator, or None.

    XProtect scans a station soon after boot. Its plugin service spawns remediators as
    the station account; launchd does not manage them, SIP refuses them signals, and
    they exit when their scan ends, sometimes minutes later. One is tolerated only when
    launchd records its executable as a root-owned XProtectRemediator inside
    XProtect.app and its parent is the process launchd names for that service in this
    station's gui domain, from a trusted definition. A lease can run the same binaries,
    but never as that process's children.
    """
    pid = entry["pid"]
    parent = process_parent(pid, deadline)
    if parent is None or parent <= 1:
        return None
    program = recorded_program(pid, deadline)
    if (program is None or Path(program).parent != XPROTECT / "Contents" / "MacOS"
            or not Path(program).name.startswith("XProtectRemediator")):
        return None
    service = process_provenance(parent, deadline)
    if not (
        service is not None
        and service["target"] == f"gui/{info['uid']}/{XPROTECT_PLUGIN}"
        and service.get("pid") == str(parent)
        and station_trust(info, trusted_service_path, service.get("path"), None, deadline)
        and station_trust(info, trusted_program, service.get("program path"), None, deadline)
        and station_trust(info, trusted_program, program, None, deadline)
    ):
        return None
    return {"service": service["target"], "path": program, "parent": parent}


def judge_processes(entries, baseline, info, deadline, services, verdicts):
    """Record in ``verdicts`` each entry launchd accounts for, with its explaining service.

    Verdicts are recorded as they are reached, so a later lookup's failure keeps them:
    the fallback must never signal a process launchd has already answered for.
    """
    xcode = installed_xcode(deadline)
    explained = set(baseline["services"]) | {entry["target"] for entry in services["tolerated"]}
    for entry in entries:
        key = (entry["pid"], entry["started"])
        provenance = process_provenance(entry["pid"], deadline)
        if explained_process(entry["pid"], provenance, entry["command"], explained,
                             xcode, info, deadline):
            verdicts[key] = {
                **entry, "service": provenance["target"], "path": provenance["path"],
            }
        # ps's name is only a cheap filter; the check reads launchd's own record.
        elif "XProtectRemediator" in entry["command"]:
            remediator = xprotect_remediator(entry, info, deadline)
            if remediator is not None:
                verdicts[key] = {**entry, **remediator}


def process_report(baseline, info, deadline, services):
    # Capture the fallback before consulting launchd. Only ps and the baseline are
    # needed to identify station work; a failed provenance read cannot erase it.
    known = {(pid, start) for pid, start in baseline["processes"]}

    entries = [{"pid": pid, "started": start, "command": command}
               for pid, start, command in station_processes(info, deadline)
               if (pid, start) not in known]
    result = {"verified": True}
    verdicts = {}
    try:
        if entries and services.get("verified") is True:
            judge_processes(entries, baseline, info, deadline, services, verdicts)
    except Exception as exc:
        result.update(verified=False, error=str(exc))
    result["tolerated"] = [verdicts[(e["pid"], e["started"])] for e in entries
                           if (e["pid"], e["started"]) in verdicts]
    result["extra"] = [e for e in entries if (e["pid"], e["started"]) not in verdicts]
    result["ok"] = result["verified"] and not result["extra"]
    return result


def work_part(name, baseline, info, deadline, services):
    if name == "services":
        return service_report(baseline, info, deadline)
    if name == "processes":
        return process_report(baseline, info, deadline, services)
    if name == "launch_agents":
        extra = sorted(set(station_launch_agents(info, deadline)) - set(baseline[name]))
        return {"ok": not extra, "verified": True, "extra": extra}
    empty = not station_crontab(info, deadline).strip()
    return {"ok": empty, "verified": True, "empty": empty}


def station_report(baseline, info):
    """Compare each part independently; one deadline cannot consume another's budget."""
    info = {**info, "trust_cache": {}}
    report = {}
    for name in WORK_PARTS:
        end = time.monotonic() + LEASE_SWEEP_TIMEOUT
        report[name] = checked_part(lambda name=name, end=end: work_part(
            name, baseline, info, end, report.get("services", {}),
        ))
    return {**report, "ok": all(part["ok"] for part in report.values())}


def time_left(deadline, limit=5):
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Station operation deadline reached")
    return min(limit, remaining)


def deadline_command(argv, deadline=None, **kwargs):
    """A plain listing or launchctl call, bounded by the operation's deadline.

    Its cap is wider than a worker slice because process listings can exceed five
    seconds while concurrent stations build and boot simulators.
    """
    return subprocess.run(argv, timeout=time_left(deadline, COMMAND_SECONDS), **kwargs)


def station_command(info, argv, timeout):
    """Enter the station's bootstrap domain and uid, then run a bounded command.

    launchctl asuser enters the domain; the trusted Python drops credentials
    before exec, preserving the process group the supervisor can reap.
    """
    import tempfile

    # What the command said is kept only for a failure, as its last few hundred bytes:
    # a simctl step that exits non-zero under load says why there and nowhere else.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            account_command(info["uid"]) + argv,
            start_new_session=True,
            stdout=output, stderr=output,
        )
        try:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # The unreaped child still reserves its pid/process-group identity.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return {"completed": False, "timed_out": True, "returncode": None}
            result = {"completed": code == 0, "timed_out": False, "returncode": code}
            if code != 0:
                output.seek(max(0, output.seek(0, os.SEEK_END) - 512))
                result["said"] = output.read().decode("utf-8", "replace").strip()
            return result
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def remove_station_agents(info, names, deadline, publish):
    errors = {}
    with station_directory(info, "Library/LaunchAgents") as fd:
        for name in names:
            counts = cleanup_counts()
            if not clear_station_tree(info, fd, [name], deadline, counts, lambda: None):
                return {"completed": False, "errors": errors}
            if counts["skipped"]:
                reason = next(iter(counts["skipped"]))
                number = int(reason.split(":")[1]) if reason.startswith("errno:") else None
                errors[name] = {
                    "errno": number,
                    "error": str(OSError(number, os.strerror(number), name)) if number else reason,
                }
    return {"completed": True, "errors": errors}


def kill_station_processes(info, entries, deadline, publish):
    errors = []
    for entry in entries:
        if time.monotonic() >= deadline:
            return {"completed": False, "errors": errors}
        try:
            # The kernel checks the caller's uid even if this pid was reused
            # after the parent collected the report.
            os.kill(entry["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append({**entry, "errno": exc.errno, "error": str(exc)})
    return {"completed": True, "errors": errors}


def clean_part(name, info, part, deadline):
    """Clean one inventory once; only a subsequent read can attest its current state."""
    if name == "services":
        for target in part.get("extra", []):
            deadline_command(["/bin/launchctl", "bootout", target], deadline,
                             check=False, capture_output=True)
    elif name == "crontab" and part.get("empty") is False:
        deadline_command(["/usr/bin/crontab", "-u", info["user"], "-r"],
                         deadline, check=True, capture_output=True)
    elif name in {"launch_agents", "processes"} and part.get("extra"):
        worker = remove_station_agents if name == "launch_agents" else kill_station_processes
        # Once dispatched, a missing completion acknowledgement remains blocking even
        # if a later inventory is clean. A read cannot attest a mutation's completion.
        timeout = time_left(deadline)
        part["cleanup_incomplete"] = True
        result, timed_out = station_worker(
            info, lambda end, publish: worker(info, part["extra"], end, publish),
            timeout,
        )
        key = "remove_errors" if name == "launch_agents" else "kill_errors"
        if result:
            part[key] = result["errors"]
        if timed_out or not result or not result["completed"]:
            raise TimeoutError("Station cleanup worker did not acknowledge completion")
        part.pop("cleanup_incomplete")


# Four 45 s cleanup budgets and four 45 s assessment budgets share one deadline,
# so retries can use time left by faster parts of the sweep.
SWEEP_SECONDS = 8 * LEASE_SWEEP_TIMEOUT
SWEEP_PASS_FLOOR = 1  # a pass with less time left than this would read nothing


def share(deadline, parts):
    """The deadline for one read or one cleanup: half this part's share of what is left."""
    left = max(0, deadline - time.monotonic()) / parts
    return time.monotonic() + min(LEASE_SWEEP_TIMEOUT, left) / 2


def station_sweep(baseline, info, seconds=None, *, simulators=True):
    """Inspect and clean every part until a whole inventory is clean or time expires."""
    seconds = SWEEP_SECONDS if seconds is None else seconds
    simulator_results = {}
    for name, argv, timeout in (
        ("shutdown", ["/usr/bin/xcrun", "simctl", "shutdown", "all"], 15),
        ("default", ["/usr/bin/xcrun", "simctl", "delete", "all"], 30),
        ("testing", ["/bin/sh", "-c", DELETE_TESTING_SET], 30),
    ):
        if simulators:
            simulator_results[name] = checked_part(
                lambda argv=argv, timeout=timeout: station_command(info, argv, timeout),
            )
    info = {**info, "trust_cache": {}}
    deadline = time.monotonic() + min(SWEEP_SECONDS, max(0, seconds))
    history = {name: [] for name in WORK_PARTS}
    evidence = {name: {} for name in WORK_PARTS}
    incomplete = False
    report = {}
    while True:
        # Never start a pass too short to read anything: the inventory this returns
        # should be one a pass could actually take.
        if report and deadline - time.monotonic() < SWEEP_PASS_FLOOR:
            break
        report = {}
        for index, name in enumerate(WORK_PARTS):
            # Each part gets its share of what is left, halved between reading and
            # cleaning: a stalled part cannot spend the parts after it, and a slow
            # provenance read cannot spend its own process cleanup. Whatever a part
            # leaves unspent falls to the parts after it.
            end = share(deadline, len(WORK_PARTS) - index)
            part = checked_part(lambda name=name, end=end, report=report: work_part(
                name, baseline, info, end, report.get("services", {}),
            ))
            report[name] = part
            if not part["ok"] and time.monotonic() < deadline:
                try:
                    clean_part(name, info, part, share(deadline, len(WORK_PARTS) - index))
                except Exception as exc:
                    part["cleanup_error"] = str(exc)
            incomplete |= part.get("cleanup_incomplete", False)
            diagnostic = {key: part[key] for key in ("error", "cleanup_error", "unparsed",
                                                                  "kill_errors", "remove_errors")
                          if part.get(key)}
            if diagnostic and diagnostic not in history[name]:
                history[name].append(diagnostic)
            for key in ("kill_errors", "remove_errors"):
                if part.get(key):
                    evidence[name][key] = part[key]
            part.update(evidence[name])
            if history[name]:
                part["diagnostics"] = history[name]
        clean = all(part["ok"] for part in report.values())
        if clean or time.monotonic() >= deadline:
            break
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    return {**report, "simulators": simulator_results, "cleanup_incomplete": incomplete,
            "remaining_seconds": max(0, deadline - time.monotonic()),
            "ok": clean and not incomplete}


def station_status(info):
    """What a lease leaves behind, without a baseline: key, sessions, drive."""
    return {
        "key_present": os.path.lexists(LEASE_KEYS / info["user"]),
        "sessions": station_sessions(info),
        "drive_mounted": station_drive_mounted(info),
    }


def station_verify(baseline, info):
    """Every release check except the helper's, terminating nothing."""
    report = station_report(baseline, info)
    errors = {}
    for key, action in (
        ("key_present", lambda: os.path.lexists(LEASE_KEYS / info["user"])),
        ("sessions", lambda: station_sessions(info)),
        ("drive_mounted", lambda: station_drive_mounted(info)),
    ):
        try:
            report[key] = action()
        except Exception as exc:
            report[key] = None
            errors[key] = str(exc)
    return {**report, "status_errors": errors}


def station_kill_sessions(info):
    # Process listings can exceed five seconds under concurrent station load. The
    # host allows two minutes, so session cleanup uses the sweep's wider budget.
    deadline = time.monotonic() + LEASE_SWEEP_TIMEOUT
    while True:
        entries = [{"pid": pid, "started": start, "command": command}
                   for pid, start, command in station_processes(info, deadline)
                   if os.path.basename(command) in ("sshd", "sshd-session")]
        remaining = [entry["pid"] for entry in entries]
        if not remaining:
            return {"remaining": []}
        killed, timed_out = station_worker(
            info, lambda end, publish, entries=entries: kill_station_processes(
                info, entries, end, publish,
            ), time_left(deadline),
        )
        incomplete = timed_out or not killed or not killed["completed"]
        if incomplete or time.monotonic() >= deadline:
            return {"remaining": remaining, "cleanup_incomplete": incomplete}
        time.sleep(0.05)


def station_key_install(public_key, info):
    if (
        not isinstance(public_key, str)
        or not public_key.startswith("ssh-ed25519 ")
        or len(public_key) > 1024
        or any(ord(c) < 32 for c in public_key)
    ):
        raise ValueError("Expected one ed25519 public key line")
    if LEASE_KEYS.is_symlink():
        raise RuntimeError("Lease key directory must not be a symlink")
    LEASE_KEYS.mkdir(mode=0o755, exist_ok=True)
    os.chmod(LEASE_KEYS, 0o755)
    path = LEASE_KEYS / info["user"]
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as stream:
        stream.write(public_key + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    return {"installed": True, "path": str(path)}


def station_key_remove(info):
    path = LEASE_KEYS / info["user"]
    path.unlink(missing_ok=True)
    return {"present": os.path.lexists(path)}


def station_mount(drive, info):
    drive = validate_drive({"drive": drive})
    if drive is None:
        raise ValueError("Station mount requires drive credentials")
    unmount_drive(info)

    def directories(deadline, publish):
        with station_directory(info) as home:
            for path in (info["drive"], info["scratch"]):
                try:
                    entry = os.stat(Path(path).name, dir_fd=home, follow_symlinks=False)
                    if entry.st_dev != os.fstat(home).st_dev:
                        return False
                except FileNotFoundError:
                    pass
                managed_directory(path, uid=info["uid"], gid=info["gid"])
        return True

    created, timed_out = station_worker(info, directories, 5)
    if timed_out:
        raise TimeoutError("Station directories timed out")
    if not created:
        raise DriveMountError("A station directory is still mounted")
    mount_drive(info, drive)
    return {"mounted": True, "point": info["drive"], "scratch": info["scratch"]}


def station_unmount(info):
    unmount_drive(info)
    return {"mounted": station_drive_mounted(info)}


ACL_TYPE_EXTENDED = 0x100


def has_acl(fd):
    """Whether the open file carries an access control list.

    Mode 0600 says nothing about one: an ACL entry can let another account read a file
    whose mode lets nobody. macOS answers ENOENT for a file without a list.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    ctypes.set_errno(0)
    acl = libc.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    if not acl:
        # Only "no list" is an answer; any other failure to look is not "none".
        error = ctypes.get_errno()
        if error != errno.ENOENT:
            raise RuntimeError(f"Could not read the file's access control list: errno {error}")
        return False
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free(acl)
    return True


def credential_fd(path, flags, *, owner=0):
    """Open a private guest credential file, never through a link or a second name."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                or info.st_mode & 0o077 or has_acl(fd)):
            raise RuntimeError(f"Unsafe station credential file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def station_password(info):
    """The station's guest-local password from its root-only file."""
    with os.fdopen(credential_fd(info["password"], os.O_RDONLY)) as source:
        return source.read().strip()


def station_password_change(info, typeable):
    """Change a station's own password by self-service, so its login keychain follows.

    The new value is written beside the password file before the account changes, so
    an interruption never leaves an account whose password nobody holds; a later call
    adopts whichever of the two authenticates, finishes that change, and then makes the
    shape asked for if that is not what it left. Automatic login is kept in step when it
    names this account, which is only during its enrollment turn.
    """
    if type(typeable) is not bool:
        raise ValueError("typeable must be true or false")
    if not shaped(station_password_once(info, typeable), typeable):
        station_password_once(info, typeable)
    return {"typeable": typeable}


def station_password_once(info, typeable):
    pending = info["password"].with_name(info["password"].name + ".pending")

    def authenticates(password):
        code, _ = run_with_passwords(
            ["/usr/bin/dscl", ".", "-authonly", info["user"]], [password], lambda: None,
            timeout=30,
        )
        return code == 0

    old = station_password(info)
    try:
        with os.fdopen(credential_fd(pending, os.O_RDONLY)) as source:
            new = source.read().strip()
    except FileNotFoundError:
        new = fresh_password(typeable)
        write_private_file(pending, (new + "\n").encode())
    if not re.fullmatch(r"[0-9a-f]{48}|[0-9]{8}", new):
        raise RuntimeError("Invalid pending station password")
    # Every wrong guess is a failed login, and macOS locks an account out after a few:
    # the recorded password is tried first, the pending one only if that fails, which
    # it does only after an interruption between the change and the file.
    if authenticates(old):
        run_with_passwords(
            account_command(info["uid"]) +
            ["/usr/sbin/sysadminctl", "-newPassword", "-", "-oldPassword", "-"],
            [old, new], lambda: None, timeout=60,
        )
        if not authenticates(new):
            raise RuntimeError("The station password change did not take effect; "
                               "change retained")
    elif not authenticates(new):
        raise RuntimeError("Neither station credential authenticates; change retained")
    # The replacements are on disk and read back before the pending value goes: a power
    # loss must never keep the account's change and the deletion but lose these bytes.
    selected = deadline_command(
        ["/usr/bin/defaults", "read", LOGINWINDOW_PREFERENCES, "autoLoginUser"],
        check=False, capture_output=True, text=True,
    )
    replacements = [(info["password"], (new + "\n").encode())]
    if (selected.returncode == 0 and selected.stdout.strip() == info["user"]
            and os.path.lexists(KCPASSWORD)):
        replacements.append((KCPASSWORD, kcpassword(new)))
    for path, content in replacements:
        write_credential(path, content)
        with os.fdopen(credential_fd(path, os.O_RDONLY), "rb") as source:
            os.fsync(source.fileno())
            if source.read() != content:
                raise RuntimeError("Station credential write did not verify; change retained")
    login_failures(info).unlink(missing_ok=True)
    pending.unlink()
    return new


def write_credential(path, content, *, owner=0):
    """Replace a private guest credential file in place."""
    with os.fdopen(credential_fd(path, os.O_WRONLY, owner=owner), "wb") as target:
        os.ftruncate(target.fileno(), 0)
        os.fchmod(target.fileno(), 0o600)
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def kcpassword(password):
    """loginwindow's standard obfuscated automatic-login form, as preparation writes it."""
    secret = password.encode() + b"\0"
    secret += b"\0" * ((-len(secret)) % len(KCPASSWORD_KEY))
    return bytes(value ^ KCPASSWORD_KEY[index % len(KCPASSWORD_KEY)]
                 for index, value in enumerate(secret))


def account_command(uid):
    # Not sudo, which can introduce a monitor/PTY in a different process group: this
    # execs directly, so every worker stays in the group its supervisor reaps.
    drop = ("import os,pwd,sys; u=pwd.getpwuid(int(sys.argv[1])); "
            "os.initgroups(u.pw_name,u.pw_gid); os.setgid(u.pw_gid); os.setuid(u.pw_uid); "
            "os.environ.update(HOME=u.pw_dir,USER=u.pw_name,LOGNAME=u.pw_name); "
            "os.execv(sys.argv[2],sys.argv[2:])")
    return ["/bin/launchctl", "asuser", str(uid), "/usr/local/firm/bin/python3",
            "-I", "-c", drop, str(uid)]


def keep_awake():
    """Restate machine power policy and the console account's idle preference."""
    deadline_command(
        ["/usr/bin/pmset", "-a", "displaysleep", "0", "sleep", "0", "disksleep", "0"],
        check=True, capture_output=True,
    )
    deadline_command(
        ["/usr/bin/defaults", "delete", LOGINWINDOW_PREFERENCES, "autoLoginUserScreenLocked"],
        check=False, capture_output=True,
    )
    deadline_command(
        account_command(CONTROL_UID) +
        ["/usr/bin/defaults", "-currentHost", "write", "com.apple.screensaver",
         "idleTime", "-int", "0"], check=True, capture_output=True,
    )
    return {"configured": True}


def station_keep_awake(info):
    """Turn this account's screen lock off and restate its idle time, on or off the console.

    Both settings are kept per host identity, which differs for each image clone.
    Restating them at every boot prevents a clone from retaining an immediate lock
    delay. Changing the delay requires the account's password on a pseudo-terminal
    and the SecureToken granted before its first login; without that token macOS
    refuses the change with MKBDeviceSetGracePeriod error -14, even on the console.
    """
    if login_blocked(info):
        # The account has rejected its recorded password: offering it here too would be
        # one more failed login toward macOS's lockout on every boot.
        raise RuntimeError("The station rejects its recorded password; change it first")
    code, _ = run_with_passwords(
        account_command(info["uid"]) +
        ["/usr/sbin/sysadminctl", "-screenLock", "off", "-password", "-"],
        [station_password(info)], lambda: None, timeout=60,
    )
    if code:
        raise RuntimeError(f"Screen lock configuration failed ({code})")
    deadline_command(
        account_command(info["uid"]) +
        ["/usr/bin/defaults", "-currentHost", "write", "com.apple.screensaver",
         "idleTime", "-int", "0"], check=True, capture_output=True,
    )
    return station_awake_status(info)


def station_awake_status(info):
    """The lock delay and screen saver idle time as the station reports them, the
    relock flag, and the power settings in force."""
    # Reading the setting, unlike changing it, asks for no password; it is the account's
    # own answer. The legacy preference key is only a fallback. A new session
    # can leave sysadminctl slow or unable to answer, so allow thirty seconds and
    # classify a missing answer as "unknown".
    try:
        result = deadline_command(
            account_command(info["uid"]) + ["/usr/sbin/sysadminctl", "-screenLock", "status"],
            time.monotonic() + 30, check=False, capture_output=True, text=True,
        )
        lock = (result.stderr + result.stdout).strip().rsplit("] ", 1)[-1]
    except subprocess.TimeoutExpired:
        result, lock = None, ""
    if result is None or result.returncode or not lock.startswith("screenLock "):
        result = deadline_command(
            account_command(info["uid"]) +
            ["/usr/bin/defaults", "read", "com.apple.screensaver", "askForPassword"],
            check=False, capture_output=True, text=True,
        )
        lock = {"0": "screenLock is off", "1": "screenLock is on"}.get(
            result.stdout.strip() if result.returncode == 0 else "", "unknown",
        )
    power = deadline_command(["/usr/bin/pmset", "-g"], check=True, capture_output=True, text=True)
    settings = {}
    for line in power.stdout.splitlines():
        fields = line.split()
        # A value may carry a note, e.g. "sleep 0 (sleep prevented by powerd)".
        if len(fields) >= 2 and fields[1].lstrip("-").isdigit():
            settings[fields[0]] = int(fields[1])
    relock = deadline_command(
        ["/usr/bin/defaults", "read", LOGINWINDOW_PREFERENCES, "autoLoginUserScreenLocked"],
        check=False, capture_output=True, text=True,
    )
    # An unset idle time means the macOS default, which starts the screen saver.
    idle = deadline_command(
        account_command(info["uid"]) +
        ["/usr/bin/defaults", "-currentHost", "read", "com.apple.screensaver", "idleTime"],
        check=False, capture_output=True, text=True,
    )
    return {
        "screen_lock": lock,
        "power": {key: settings.get(key) for key in ("displaysleep", "sleep", "disksleep")},
        "relock_at_login": relock.returncode == 0 and relock.stdout.strip() == "1",
        "screen_saver_idle": (
            int(idle.stdout) if idle.returncode == 0 and idle.stdout.strip().isdigit() else None
        ),
    }


def station_command_log(info):
    """Hand the lease's command log to the host and start the next lease empty."""
    try:
        fd = os.open(info["command_log"], os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"data": ""}
    with os.fdopen(fd, "r+b") as stream:
        entry = os.fstat(stream.fileno())
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0 or entry.st_nlink != 1:
            raise RuntimeError("Lease command log is not a root-owned regular file")
        data = stream.read(LIMIT)
        os.ftruncate(stream.fileno(), 0)
    return {"data": base64.b64encode(data).decode(), "truncated": len(data) == LIMIT}


SESSION_LOGIN = '''
import json, sys
from twisted.internet import reactor
from vncdotool.client import VNCDoToolClient, VNCDoToolFactory
result = {'authenticated': None}
class Client(VNCDoToolClient):
    def vncAuthFailed(self, reason):
        result.update(authenticated=False)
        self.transport.loseConnection()
        if reactor.running: reactor.callLater(0.1, reactor.stop)
p = json.load(sys.stdin)
f = VNCDoToolFactory()
f.protocol = Client
f.username, f.password = p['username'], p['password']
def connected(client):
    result['authenticated'] = True
    client.transport.loseConnection()
    reactor.callLater(0.2, reactor.stop)
def failed(error):
    if reactor.running: reactor.stop()
f.deferred.addCallbacks(connected, failed)
reactor.connectTCP('127.0.0.1', 5900, f, timeout=10)
reactor.callLater(15, lambda: reactor.running and reactor.stop())
reactor.run()
print(json.dumps(result))
'''


def control_child(argv, *, data=None, timeout=20):
    """Bound and reap the unprivileged login/relay process and its process group."""
    process = subprocess.Popen(
        account_command(CONTROL_UID) + argv, start_new_session=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        output, _ = process.communicate(data, timeout=timeout)
        return process.returncode, output
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def screen_sharing_ready(deadline):
    """Require plain, scoped Screen Sharing and only Apple's root listener."""
    if Path("/Library/Application Support/Apple/Remote Desktop/RemoteManagement.launchd").exists():
        raise RuntimeError("Remote Management must be disabled")
    disabled = deadline_command(
        ["/bin/launchctl", "print-disabled", "system"], deadline,
        check=True, capture_output=True, text=True,
    ).stdout
    # The map's values are "enabled"/"disabled" on macOS 27 and were false/true before.
    if not re.search(r'"com\.apple\.screensharing"\s*=>\s*(enabled|false)\b', disabled):
        raise RuntimeError("Screen Sharing must be enabled")
    group = deadline_command(
        ["/usr/bin/dscl", "-plist", ".", "-read", "/Groups/com.apple.access_screensharing"],
        deadline, check=True, capture_output=True,
    ).stdout
    record = plistlib.loads(group)
    nested = deadline_command(
        ["/usr/bin/dscl", ".", "-read", "/Groups/firmstations", "GeneratedUID"],
        deadline, check=True, capture_output=True, text=True,
    ).stdout.split()
    if (len(nested) != 2 or record.get("dsAttrTypeStandard:NestedGroups") != [nested[1]]
            or record.get("dsAttrTypeStandard:GroupMembership")
            or record.get("dsAttrTypeStandard:GroupMembers")):
        raise RuntimeError("Screen Sharing must admit only the station group")
    control = pwd.getpwnam(CONTROL_USER)
    if grp.getgrnam(STATION_GROUP).gr_gid in os.getgrouplist(CONTROL_USER, control.pw_gid):
        raise RuntimeError("The control account must not belong to the station group")
    listener = deadline_command(
        ["/usr/sbin/lsof", "-nP", "-a", "-iTCP:5900", "-sTCP:LISTEN"], deadline,
        check=False, capture_output=True, text=True,
    )
    rows = [line.split() for line in listener.stdout.splitlines()[1:]]
    if listener.returncode or not rows:
        raise RuntimeError("Screen Sharing has no listener")
    for row in rows:
        if len(row) < 3 or row[2] != "root":
            raise RuntimeError("Untrusted Screen Sharing listener")
        if row[:2] == ["launchd", "1"]:
            continue
        executable = deadline_command(
            ["/bin/ps", "-o", "comm=", "-p", row[1]], deadline,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if executable != ("/System/Library/CoreServices/RemoteManagement/"
                          "screensharingd.bundle/Contents/MacOS/screensharingd"):
            raise RuntimeError("Untrusted Screen Sharing listener")


LOGIN_ATTEMPTS = 2


def login_failures(info):
    return STATE / f"station-{info['station']}-login-failures"


def login_blocked(info):
    """Whether the account has rejected its recorded password LOGIN_ATTEMPTS times.

    Cleared by a login that works, by a session found ready (the owner may have logged
    the account in by hand after clearing macOS's own lockout), and by a password change.
    """
    try:
        return int(login_failures(info).read_text()) >= LOGIN_ATTEMPTS
    except (FileNotFoundError, ValueError):
        return False


def session_login(info, deadline):
    """One account login over Screen Sharing; refused after two that the account rejected.

    macOS locks an account out after a few failed logins, for a time that grows, and a
    station is logged in on every boot and before a lease: a password the account does
    not take must stop being offered, not be offered again by the next caller.
    """
    screen_sharing_ready(deadline)
    record = login_failures(info)
    try:
        failures = int(record.read_text())
    except (FileNotFoundError, ValueError):
        failures = 0
    if failures >= LOGIN_ATTEMPTS:
        return False
    payload = json.dumps({"username": info["user"], "password": station_password(info)}).encode()
    rejected = False
    try:
        code, output = control_child(
            [SESSION_PYTHON, "-I", "-c", SESSION_LOGIN], data=payload,
            timeout=time_left(deadline, 20),
        )
        result = json.loads(output.splitlines()[-1])
        if code == 0 and isinstance(result, dict) and result.get("authenticated") is True:
            record.unlink(missing_ok=True)
            return True
        rejected = isinstance(result, dict) and result.get("authenticated") is False
    except (ValueError, IndexError, subprocess.TimeoutExpired):
        # Third-party diagnostics and transcripts may contain credentials.
        pass
    if rejected and password_verdict(info, deadline) == "wrong":
        # Counted only when the recorded password itself is wrong, which root settles
        # locally. Anything else Screen Sharing rejects for (a stalled directory service,
        # a held account) is not this station's password and must not become its refusal.
        # On macOS 27, wrong passwords offered on loopback 5900 by another account
        # do not raise the failure count or prevent use of the right password.
        record.write_text(str(failures + 1))
    return False


def password_verdict(info, deadline):
    """``"right"``, ``"wrong"`` (eDSAuthFailed, the directory's own word) or ``"unknown"``.

    Bounded by the caller's deadline, so a login's host timeout covers it.
    """
    try:
        code, output = run_with_passwords(
            ["/usr/bin/dscl", ".", "-authonly", info["user"]], [station_password(info)],
            lambda: None, timeout=time_left(deadline, 30),
        )
    except (OSError, TimeoutError):
        return "unknown"
    if code == 0:
        return "right"
    return "wrong" if "eDSAuthFailed" in output else "unknown"


def helper_status(info, deadline):
    try:
        code, output = control_child(
            ["/usr/local/firm/bin/python3", "-I", RELAY, str(info["station"]), "--status"],
            timeout=time_left(deadline, 12),
        )
        reply = json.loads(output)
        if (code == 0 and isinstance(reply, dict) and reply.get("ok") is True
                and isinstance(reply.get("result"), dict)):
            return reply["result"]
        return None
    except (ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def session_ready(info, state):
    session = (state or {}).get("session", {})
    return bool(state and state.get("ready") is True and state.get("environment") == "vm-session"
                and session.get("uid") == info["uid"] and session.get("gui_uid") == info["uid"]
                and session.get("login_done") is True and session.get("screen_locked") is False)


def station_session_ensure(info, deadline=None):
    """A login is complete only when the authenticated helper confirms its Aqua session."""
    deadline = time.monotonic() + 60 if deadline is None else deadline
    state = helper_status(info, deadline)
    result = {key: info[key] for key in ("station", "user", "uid")}
    result.update(ready=False, created=False, helper=state)
    if session_ready(info, state):
        # A session that is there, however it was made, ends any earlier refusals.
        login_failures(info).unlink(missing_ok=True)
        return {**result, "ready": True}
    # A reachable but locked/incomplete helper needs enrollment or an explicit reset.
    if state is not None:
        return {**result, "failure": "session_not_ready"}
    if not session_login(info, deadline):
        # Blocked: the account rejected its recorded password twice. Nothing offers it
        # again until station-password gives the account one that works.
        return {**result, "failure": "login_blocked" if login_blocked(info)
                else "login_failed"}
    while time.monotonic() < deadline:
        state = helper_status(info, deadline)
        if session_ready(info, state):
            return {**result, "ready": True, "created": True, "helper": state}
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return {**result, "failure": "helper_not_ready", "helper": state}


def station_session_reset(info):
    """End only gui/<uid>, leaving user/<uid> services, then log in again.

    launchd owns user services and may respawn them. They are not blindly killed or
    adopted as a clean lease: what the station is left running is the sweep's and the
    verification's to report, so the whole deadline here goes to the new session.
    """
    if os.path.lexists(LEASE_KEYS / info["user"]):
        raise RuntimeError("Remove the station lease key before resetting its session")
    if station_kill_sessions(info)["remaining"]:
        raise RuntimeError("Station SSH sessions did not end")
    unmount_drive(info)
    if station_drive_mounted(info):
        raise RuntimeError("Station drive did not unmount")
    # One deadline bounds the bootout, the wait for the session to end and the new
    # login; the host's SESSION_RESET_RPC_TIMEOUT is set above it.
    deadline = time.monotonic() + 80
    domain = f"gui/{info['uid']}"
    deadline_command(["/bin/launchctl", "bootout", domain], deadline,
                     check=False, capture_output=True)
    ended = time.monotonic() + 15
    while True:
        result = deadline_command(["/bin/launchctl", "print", domain], deadline,
                                  check=False, capture_output=True)
        if result.returncode:
            break
        if time.monotonic() >= ended:
            raise RuntimeError("Station GUI session did not end")
        time.sleep(0.2)
    return {**station_session_ensure(info, deadline), "reset": True}


# ---- resource sampling -----------------------------------------------------------------


def sysctl_number(name):
    try:
        output = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, check=False
        )
        return int(output.stdout.strip())
    except (OSError, ValueError):
        return None


def memory_statistics():
    """Page size, compressor pages and cumulative swapouts as ``vm_stat`` reports them."""
    page_size, values = None, {}
    try:
        output = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        output = ""
    for line in output.splitlines():
        heading = re.search(r"page size of (\d+) bytes", line)
        if heading:
            page_size = int(heading.group(1))
            continue
        name, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            values[name.strip()] = int(value)
    compressed = values.get("Pages occupied by compressor")
    return {
        "compressed_bytes": None if compressed is None or page_size is None
        else compressed * page_size,
        "swapouts": values.get("Swapouts"),
    }


def disk_usage(path):
    try:
        info = os.statvfs(path)
    except OSError:
        return {"disk_used_bytes": None, "disk_total_bytes": None}
    return {
        "disk_used_bytes": (info.f_blocks - info.f_bfree) * info.f_frsize,
        "disk_total_bytes": info.f_blocks * info.f_frsize,
    }


def sample():
    """Machine-wide counters for the host's resource history, for a guest or the host itself.

    Read without the global lock and without failing: an unreadable field is unknown.
    macOS reports free memory as a percentage of the total; its free page count is not
    it, and it has no cheap cumulative CPU counter, so the host measures a VM's CPU.
    """
    total = sysctl_number("hw.memsize")
    level = sysctl_number("kern.memorystatus_level")
    return {
        "boot": boot(),
        "cpus": sysctl_number("hw.ncpu"),
        "memory_total_bytes": total,
        "memory_available_bytes": None if total is None or level is None else total * level // 100,
        "pressure_level": sysctl_number("kern.memorystatus_vm_pressure_level"),
        **memory_statistics(),
        **disk_usage(DATA_VOLUME),
    }


def station_operation(operation, request):
    n = station_number(request["station"])
    if operation == "station-account":
        return station_account(n)
    info = station_info(n)
    if operation == "station-status":
        return station_status(info)
    if operation == "station-baseline":
        return station_baseline(info)
    if operation == "station-sweep":
        return station_sweep(request["baseline"], info, request.get("seconds", SWEEP_SECONDS),
                             simulators=request.get("simulators", True))
    if operation == "station-verify":
        return station_verify(request["baseline"], info)
    if operation == "station-kill-sessions":
        return station_kill_sessions(info)
    if operation == "station-key-install":
        return station_key_install(request["public_key"], info)
    if operation == "station-key-remove":
        return station_key_remove(info)
    if operation == "station-mount":
        return station_mount(request["drive"], info)
    if operation == "station-reclaim":
        return station_reclaim(info)
    if operation == "station-unmount":
        return station_unmount(info)
    if operation == "station-command-log":
        return station_command_log(info)
    if operation == "station-keep-awake":
        return station_keep_awake(info)
    if operation == "station-awake-status":
        return station_awake_status(info)
    if operation == "station-password":
        return station_password_change(info, request["typeable"])
    if operation == "station-session-ensure":
        return station_session_ensure(info)
    if operation == "station-session-reset":
        return station_session_reset(info)
    raise ValueError("Unknown operation")


def dispatch(request):
    operation = request["operation"]
    if operation == "sample":
        return sample()
    if operation == "info":
        # Read-only, and asked before every transport is used: it never waits for a lease step.
        count = json.loads(STATIONS.read_text())["stations"] if STATIONS.exists() else 0
        return {"firm_id": database().get("firm_id"), "boot": boot(), "stations": count}
    if operation == "settle-credentials":
        return settle_credentials()
    if operation == "rotate-control-password":
        typeable = request.get("typeable", False)
        if type(typeable) is not bool:
            raise ValueError("typeable must be true or false")
        rotate_control_password(typeable)
        return {"rotated": True, "typeable": typeable}
    station = station_number(request["station"]) if operation.startswith("station-") else None
    with lock(station):
        if operation == "initialize":
            return initialize(request)
        if operation == "keep-awake":
            return keep_awake()
        if operation.startswith("station-"):
            return station_operation(operation, request)
        raise ValueError("Unknown operation")


def main():
    if sys.platform != "darwin" or os.getuid() != 0:
        raise RuntimeError("The guest controller runs only as root inside a macOS VM")
    if subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"]).strip() != b"1":
        raise RuntimeError("Refusing to run the guest controller on a physical Mac")
    os.umask(0o077)
    try:
        raw = sys.stdin.buffer.read(2 * LIMIT + 1)
        if len(raw) > 2 * LIMIT:
            raise ValueError("Request is too large")
        request = json.loads(raw)
        result = dispatch(request)
        reply = {"ok": True, "result": result}
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
