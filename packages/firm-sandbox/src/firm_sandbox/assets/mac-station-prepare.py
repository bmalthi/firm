#!/usr/local/firm/bin/python3
"""Prepare a fresh VM's ordinary station accounts and shared tools, without consent."""

import fcntl
import json
import os
import pwd
import runpy
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

HOMEBREW = "/opt/homebrew"
STATE = "/var/db/firm-station"
SESSION_PROJECT = "/usr/local/firm/session"
DEADLINE = None
# This script runs standalone in a fresh guest, so it repeats the controller's form.
KCPASSWORD_KEY = bytes.fromhex("7d895223d2bcddeaa3b91f")


def remaining(limit):
    seconds = limit if DEADLINE is None else min(limit, DEADLINE - time.monotonic())
    if seconds <= 0:
        raise TimeoutError("Station preparation deadline reached")
    return seconds


def run(*arguments):
    timeout = remaining(600)
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
    if process.returncode:
        # Provisioning handles credentials; neither commands nor stderr are diagnostic output.
        raise RuntimeError(f"Station account setup failed: {Path(arguments[0]).name}")
    return output.strip()


def prompted(arguments, passwords):
    controller = runpy.run_path("/usr/local/libexec/firm-controller.py")
    code, _ = controller["run_with_passwords"](
        arguments, passwords, lambda: None, timeout=remaining(60),
    )
    if code:
        raise RuntimeError("Station account password operation failed")


def token_status(user):
    # sysadminctl reports on stderr and exits zero either way.
    result = subprocess.run(["/usr/sbin/sysadminctl", "-secureTokenStatus", user],
                            capture_output=True, text=True, timeout=remaining(60))
    return result.stderr + result.stdout


def private_write(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("Unsafe station enrollment file")
        os.ftruncate(output.fileno(), 0)
        os.fchmod(output.fileno(), 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def kcpassword(password):
    """loginwindow's standard obfuscated automatic-login form."""
    secret = password.encode() + b"\0"
    secret += b"\0" * ((-len(secret)) % len(KCPASSWORD_KEY))
    return bytes(value ^ KCPASSWORD_KEY[index % len(KCPASSWORD_KEY)]
                 for index, value in enumerate(secret))


def share_homebrew():
    try:
        info = os.lstat(Path(HOMEBREW))
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("The image's Homebrew is not the root-owned prefix its build made")
    # An inheritable ACL also covers new files made by either station, regardless of umask.
    run("/bin/chmod", "-R", "+a",
        "group:firmstations allow read,write,execute,delete,append,readattr,writeattr,"
        "readextattr,writeextattr,readsecurity,list,search,add_file,add_subdirectory,"
        "delete_child,file_inherit,directory_inherit", HOMEBREW)
    return HOMEBREW


def configure_screen_sharing():
    run("/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart",
        "-deactivate", "-stop")
    # Remote Management bypasses Screen Sharing's access group. Both must be set explicitly.
    run("/usr/bin/defaults", "write", "/Library/Preferences/com.apple.RemoteManagement",
        "ARD_AllLocalUsers", "-bool", "false")
    group = "com.apple.access_screensharing"
    # The group exists once Screen Sharing has ever been on; creating it again is refused.
    subprocess.run(["/usr/sbin/dseditgroup", "-o", "create", group],
                   capture_output=True, timeout=30)
    run("/usr/bin/dscl", ".", "-read", f"/Groups/{group}", "RecordName")
    for attribute in ("GroupMembership", "GroupMembers", "NestedGroups"):
        subprocess.run(["/usr/bin/dscl", ".", "-delete", f"/Groups/{group}", attribute],
                       capture_output=True, timeout=30)
    run("/usr/sbin/dseditgroup", "-o", "edit", "-a", "firmstations", "-t", "group", group)
    run("/bin/launchctl", "enable", "system/com.apple.screensharing")
    subprocess.run(["/bin/launchctl", "bootout", "system/com.apple.screensharing"],
                   capture_output=True, timeout=30)
    run("/bin/launchctl", "bootstrap", "system",
        "/System/Library/LaunchDaemons/com.apple.screensharing.plist")
    # su is closed to the station group later, on enrollment's last turn: /etc/pam.d is
    # among macOS's administrative files, which root may write only from a context with
    # Full Disk Access, and Remote Login has none until the owner's first console turn.


def prepare(station_count):
    if type(station_count) is not int or not 1 <= station_count <= 8:
        raise ValueError("stations must be an integer from 1 to 8")
    if os.getuid() != 0 or run("/usr/sbin/sysctl", "-n", "kern.hv_vmm_present") != "1":
        raise RuntimeError("Station preparation requires the disposable Mac VM")
    control_account = pwd.getpwnam("firmvm")
    if control_account.pw_uid != 501 or control_account.pw_dir != "/Users/firmvm":
        raise RuntimeError("Unexpected control account identity")
    accounts = [account for account in pwd.getpwall() if 500 <= account.pw_uid < 65534]
    if any(account.pw_name != "firmvm" for account in accounts):
        raise RuntimeError(
            "Station preparation requires a fresh image with only the control account"
        )
    for number in range(1, station_count + 1):
        home = Path(f"/Users/firmstation{number}")
        if home.exists() or home.is_symlink():
            raise RuntimeError("Station home already exists")
    if Path(STATE).exists() or Path(STATE).is_symlink():
        raise RuntimeError("Station preparation state already exists")
    run("/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "700",
        STATE, f"{STATE}/passwords")
    run("/usr/sbin/dseditgroup", "-o", "create", "firmstations")
    control = Path("/Users/firmvm/password").read_text().strip()
    stations = []
    for number in range(1, station_count + 1):
        user, uid = f"firmstation{number}", 501 + number
        # 48 characters: Screen Sharing's account authentication takes at most 63.
        password = secrets.token_hex(24)
        # The controller answers terminal prompts without echoing secrets or exposing argv.
        private_write(f"{STATE}/passwords/{user}", (password + "\n").encode())
        prompted(
            ["/usr/sbin/sysadminctl", "-addUser", user, "-fullName", f"Firm station {number}",
             "-UID", str(uid), "-home", f"/Users/{user}", "-shell", "/bin/zsh", "-password", "-",
             "-adminUser", "firmvm", "-adminPassword", "-"], [password, control],
        )
        prompted(["/usr/bin/dscl", ".", "-authonly", user], [password])
        # The screen-lock delay lives in the account's keybag, which it has only with a
        # SecureToken, and only a login after the grant sees it: so before its first one.
        # sysadminctl asks for the administrator's password first, then the account's.
        prompted(["/usr/sbin/sysadminctl", "-secureTokenOn", user, "-password", "-",
                  "-adminUser", "firmvm", "-adminPassword", "-"], [control, password])
        if "ENABLED" not in token_status(user):
            raise RuntimeError("The station account did not receive a SecureToken")
        station = pwd.getpwnam(user)
        if station.pw_uid != uid or station.pw_dir != f"/Users/{user}":
            raise RuntimeError("Unexpected station account identity")
        admins = run("/usr/bin/dscl", ".", "-read", "/Groups/admin", "GroupMembership").split()
        if user in admins:
            raise RuntimeError("The station account must be an ordinary account")
        run("/usr/sbin/dseditgroup", "-o", "edit", "-a", user, "-t", "user", "firmstations")
        run("/usr/sbin/createhomedir", "-c", "-u", user)
        os.chmod(f"/Users/{user}", 0o700, follow_symlinks=False)
        run("/usr/bin/sudo", "-H", "-u", user, "/usr/bin/defaults", "write",
            "com.apple.loginwindow", "TALBlockSavingAndLaunch", "-bool", "true")
        run("/usr/bin/sudo", "-H", "-u", user, "/bin/mkdir", "-p", f"/Users/{user}/scratch")
        stations.append({"station": number, "user": user, "uid": uid})
    homebrew = share_homebrew()
    configure_screen_sharing()
    run("/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "755", SESSION_PROJECT)
    Path(f"{SESSION_PROJECT}/pyproject.toml").write_text(
        '[project]\nname = "firm-session"\nversion = "0.0.0"\n'
        # A pair known to log a station in on macOS 27; Apple's account
        # authentication needs pycryptodomex, which vncdotool does not itself require.
        'requires-python = ">=3.12"\n'
        'dependencies = ["vncdotool==1.4.2", "pycryptodomex==3.23.0"]\n'
    )
    run("/usr/local/firm/bin/uv", "sync", "--project", SESSION_PROJECT,
        "--python", "/usr/local/firm/bin/python3", "--no-python-downloads")
    # uv leaves .venv/.lock writable by everyone, inside the tree root runs the login
    # client from; seal refuses any entry there a group or other could change.
    run("/bin/chmod", "-R", "go-w", SESSION_PROJECT)
    # The console stays with firmvm; enrollment gives every station its own console turn.
    private_write("/etc/kcpassword", kcpassword(control))
    run("/usr/bin/defaults", "write", "/Library/Preferences/com.apple.loginwindow",
        "autoLoginUser", "-string", "firmvm")
    private_write(f"{STATE}/stations.json",
                  (json.dumps({"stations": station_count}) + "\n").encode())
    print(json.dumps({
        "stations": stations, "automatic_login": "firmvm", "homebrew": homebrew,
        "next": "Complete first login and Network Volumes consent on the console for every "
                "station; "
                "grant helper Accessibility and Screen Recording once, return to firmvm and verify "
                "each station's drive read off console",
    }))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: mac-station-prepare.py STATIONS")
    os.makedirs("/var/db/firm", mode=0o700, exist_ok=True)
    with open("/var/db/firm/lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        DEADLINE = time.monotonic() + 1800
        prepare(int(sys.argv[1]))
