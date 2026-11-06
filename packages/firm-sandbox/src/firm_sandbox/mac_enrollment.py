"""Prepare and enroll every station account in one shared Mac VM.

Preparation creates the accounts and installs the signed helper once. Repeated
``mac-prepare`` calls then advance the console through each station and finally back
to ``firmvm``. The owner completes first login and Network Volumes consent during each
station's turn; no consent database or setup state is synthesized.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from .commands import mac_sudo_command
from .guest_control import lifecycle, save
from .mac_station import ASSETS, DIRTY_MARKER, awake_settled, station_locks
from .mac_vm import CONTROL_USER, PYTHON

PREPARE_TIMEOUT = 1860
INSTALL_TIMEOUT = 660
AUTO_LOGIN_SCRIPT = r'''import fcntl,json,os,stat,subprocess,sys,time
KEY=bytes.fromhex("7d895223d2bcddeaa3b91f")
lock=os.open("/var/db/firm/lock",os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
deadline=time.monotonic()+10
while True:
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic()>=deadline:
            raise RuntimeError("The Mac is busy with another operation; try again")
        time.sleep(.1)
user=sys.argv[1]
if user == "firmvm":
    path="/Users/firmvm/password"
elif user.startswith("firmstation") and user.removeprefix("firmstation").isdigit():
    path=f"/var/db/firm-station/passwords/{user}"
else:
    raise RuntimeError("Invalid automatic-login account")
fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd) as source:
    info=os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:
        raise RuntimeError("Unsafe automatic-login credential")
    password=source.read(1024).strip()
if not password or "\n" in password or "\r" in password:
    raise RuntimeError("Invalid automatic-login credential")
secret=password.encode()+b"\0"
secret+=b"\0"*((-len(secret))%len(KEY))
encoded=bytes(value^KEY[index%len(KEY)] for index,value in enumerate(secret))
fd=os.open("/etc/kcpassword",os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,"wb") as output:
    info=os.fstat(output.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1:
        raise RuntimeError("Unsafe automatic-login file")
    os.fchmod(output.fileno(),0o600)
    output.write(encoded)
    output.flush()
    os.fsync(output.fileno())
subprocess.run(["/usr/bin/defaults","write","/Library/Preferences/com.apple.loginwindow",
                "autoLoginUser","-string",user],check=True)
print(json.dumps({"automatic_login":user}))
'''


# macOS keeps /etc/pam.d among its administrative files: root may write there only from a
# context with Full Disk Access, which Remote Login has once the owner grants it on the
# first console turn. So su is closed to the station group on the last turn, not at
# preparation. A station can know no password but its lease's either way; this rule is what
# still holds if one ever leaks.
CLOSE_SU_SCRIPT = r'''import fcntl,json,os,time
RULE="account    required       pam_group.so no_warn deny group=firmstations ruser fail_safe\n"
lock=os.open("/var/db/firm/lock",os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
deadline=time.monotonic()+10
while True:
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic()>=deadline:
            raise RuntimeError("The Mac is busy with another operation; try again")
        time.sleep(.1)
path="/etc/pam.d/su"
if os.path.islink(path):
    raise RuntimeError("The su PAM policy must not be a symlink")
with open(path) as source:
    text=source.read()
reason=None
if RULE not in text:
    lines=text.splitlines(keepends=True)
    # Before the first account rule, so it is decided for every target account.
    index=next((i for i,line in enumerate(lines) if line.split()[:1]==["account"]),len(lines))
    draft=path+".firm-new"
    try:
        # Whole or not at all: a half-written su policy would lock nobody out and everybody in.
        fd=os.open(draft,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
        with os.fdopen(fd,"w") as output:
            output.write("".join(lines[:index]+[RULE]+lines[index:]))
            output.flush()
            os.fsync(output.fileno())
        os.chown(draft,0,0)
        os.replace(draft,path)
    except PermissionError as error:
        reason=f"macOS refused the write (errno {error.errno})"
        try:
            os.unlink(draft)
        except OSError:
            pass
with open(path) as source:
    print(json.dumps({"su_closed":RULE in source.read(),"reason":reason}))
'''


def _close_su(vm):
    result = vm._shell(
        "/bin/zsh", "-c", mac_sudo_command(PYTHON, "-I", "-c", CLOSE_SU_SCRIPT), timeout=60
    )
    reply = json.loads(result.stdout)
    if type(reply.get("su_closed")) is not bool:
        raise RuntimeError("Guest did not report the su policy")
    return reply


@lifecycle(exclusive=True)
def install_helper(vm, app: Path, *, mode="update", stations=None):
    """Install a host-signed helper across the unleased, running Mac VM.

    ``prepare`` additionally creates the recipe's station accounts before the one
    machine-wide install. Every existing station lock is held, so installation refuses
    while any account is leased.
    """
    if mode not in {"update", "enroll", "development", "prepare"}:
        raise ValueError("Invalid station helper installation mode")
    if mode == "prepare":
        if type(stations) is not int or not 1 <= stations <= 8:
            raise ValueError("prepare requires a station count from 1 to 8")
    elif stations is not None:
        raise ValueError("stations is only valid in prepare mode")
    app = Path(app).resolve(strict=True)
    if app.name != "Firm Station Helper.app" or not app.is_dir():
        raise ValueError("Expected the built Firm Station Helper.app")
    if not vm.network.ready() or not vm._running():
        raise RuntimeError("Start the isolated Mac VM before installing its station helper")
    locked_stations = range(1, stations + 1) if mode == "prepare" else vm.stations
    with station_locks(vm.firm_locks, locked_stations):
        if any(
            os.path.lexists(vm.root / "stations" / str(number) / DIRTY_MARKER)
            for number in vm.stations
        ):
            raise RuntimeError(
                "Mac cleanup is unverified; run mac-recover --station N --confirm-cleanup "
                "after inspecting station activity, or replace the firm"
            )
        remote = f"/Users/firmvm/firm-mac-install-helper-{uuid4().hex}"
        prepared = None
        try:
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--strict", str(app)],
                check=True,
                capture_output=True,
            )
            vm._shell("/bin/mkdir", "-m", "700", remote)
            with tempfile.TemporaryDirectory(prefix="firm-mac-install-helper-") as directory:
                archive = Path(directory) / "helper.zip"
                subprocess.run(
                    ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)],
                    check=True,
                    capture_output=True,
                )
                for source in (
                    archive,
                    ASSETS / "mac-station-install.sh",
                    ASSETS / "mac-station-prepare.py",
                    ASSETS / "mac-station-add-platform.py",
                    ASSETS / "mac-station-relay.py",
                ):
                    vm._lima("copy", str(source), f"mac:{remote}/{source.name}")
            vm._shell("/usr/bin/ditto", "-x", "-k", f"{remote}/helper.zip", remote)
            if mode == "prepare":
                command = mac_sudo_command(
                    PYTHON, "-I", f"{remote}/mac-station-prepare.py", str(stations)
                )
                result = vm._shell("/bin/zsh", "-c", command, timeout=PREPARE_TIMEOUT)
                prepared = json.loads(result.stdout)
                if prepared.get("stations") != [
                    {"station": number, "user": f"firmstation{number}", "uid": 501 + number}
                    for number in range(1, stations + 1)
                ]:
                    raise RuntimeError("Guest preparation returned an unexpected station inventory")
                state = vm._state()
                save(vm.record, {**state, "stations": stations, "enrolled": False})
            command = mac_sudo_command(
                "/bin/zsh",
                f"{remote}/mac-station-install.sh",
                f"{remote}/Firm Station Helper.app",
                "enroll" if mode == "prepare" else mode,
            )
            vm._shell("/bin/zsh", "-c", command, timeout=INSTALL_TIMEOUT)
            return prepared
        finally:
            vm._shell("/bin/rm", "-rf", remote, check=False)


def _automatic_login(vm):
    result = vm._shell(
        "/usr/bin/defaults", "read", "/Library/Preferences/com.apple.loginwindow",
        "autoLoginUser", timeout=30,
    )
    return result.stdout.decode().strip()


def console_user(vm):
    """The account whose session the window server has on the console, or "".

    Not /dev/console's owner: the last graphical login takes that file, and a station
    logged in off console took it from the station whose console turn it was.
    """
    result = vm._shell("/usr/sbin/ioreg", "-a", "-n", "Root", "-d", "1", timeout=30)
    for session in plistlib.loads(result.stdout).get("IOConsoleUsers", []):
        if session.get("kCGSSessionOnConsoleKey"):
            return session.get("kCGSSessionUserNameKey", "")
    return ""


def _select_automatic_login(vm, user):
    result = vm._shell(
        "/bin/zsh", "-c", mac_sudo_command(PYTHON, "-I", "-c", AUTO_LOGIN_SCRIPT, user),
        timeout=60,
    )
    reply = json.loads(result.stdout)
    if reply != {"automatic_login": user}:
        raise RuntimeError("Guest did not select the requested automatic-login account")


def _control_credentials(vm, station=None):
    """Write what the owner types on the console to an owner-private file, never stdout.

    The administrator always; the station whose console turn it is, whose password is
    typeable only for that turn. No other station's password leaves the guest.
    """
    script = """import json,os,stat,sys
def read(path,owner):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner or info.st_mode&0o077:
            raise RuntimeError('Unsafe enrollment credential file')
        value=source.read(1024).strip()
    if not value or len(value)>256 or '\\n' in value or '\\r' in value:
        raise RuntimeError('Invalid enrollment credential')
    return value
reply={'administrator':read('/Users/firmvm/password',501)}
if len(sys.argv)>1:
    reply['station']=read('/var/db/firm-station/passwords/firmstation'+str(int(sys.argv[1])),0)
print(json.dumps(reply))
"""
    arguments = [] if station is None else [str(station)]
    result = vm._shell(
        "/bin/zsh", "-c", mac_sudo_command(PYTHON, "-I", "-c", script, *arguments), timeout=30
    )
    reply = json.loads(result.stdout)
    destination = vm.root / "enrollment-credentials.txt"
    text = (f"Administrator username: {CONTROL_USER}\n"
            f"Administrator password: {reply['administrator']}\n")
    if station is not None:
        text += (f"\nStation username: firmstation{station}\n"
                 f"Station password: {reply['station']}\n")
    with tempfile.NamedTemporaryFile(
        mode="w", dir=vm.root, prefix=".enrollment-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {"credentials_file": str(destination), "administrator": CONTROL_USER}


def _awake(vm, station):
    state = vm._rpc("station-awake-status", station=station)
    if not awake_settled(state):
        raise RuntimeError(
            f"Station {station} is not ready to leave the console: its screen lock is not off"
        )
    return state


def _session_ready(vm, station):
    state = vm._rpc("station-session-ensure", station=station, timeout=75)
    if (
        state.get("ready") is not True
        or state.get("station") != station
        or state.get("user") != f"firmstation{station}"
        or not isinstance(state.get("helper"), dict)
        or state["helper"].get("ready") is not True
    ):
        raise RuntimeError(f"Station {station}'s helper session is not ready")
    return state


def _station_of(user):
    return int(user.removeprefix("firmstation"))


def _turn_credentials(vm, progress):
    """The credentials file for the turn now on the console."""
    target = progress["target"]
    return _control_credentials(vm, None if target == CONTROL_USER else _station_of(target))


def _reboot(vm, progress, user):
    state = vm._state()
    selecting = {**progress, "phase": "selecting", "target": user}
    save(vm.record, {**state, "enrollment_progress": selecting})
    if user != CONTROL_USER:
        # macOS asks the account on the console for its own password, and the VM window
        # takes no pasted text: typeable for this turn, private again after it.
        vm._rpc("station-password", station=_station_of(user), typeable=True, timeout=150)
    _select_automatic_login(vm, user)
    vm.down()
    vm.up()
    deadline = time.monotonic() + 90
    while console_user(vm) != user and time.monotonic() < deadline:
        time.sleep(0.5)
    if _automatic_login(vm) != user or console_user(vm) != user:
        raise RuntimeError(f"The Mac did not return with {user} on the console")
    selected = {**selecting, "phase": "console"}
    save(vm.record, {**vm._state(), "enrollment_progress": selected})
    return selected


def _turn_reply(credentials, vm, progress, *, prior=0):
    user = progress["target"]
    if user == CONTROL_USER:
        return {
            **credentials,
            "stations": list(vm.stations),
            "automatic_login": user,
            "complete": False,
            "next": f"Confirm firmvm completed its login on the console, run "
            f"project_handoff.py --station {prior} off console, then run mac-prepare with "
            "the same recipe once more to verify every off-console session.",
        }
    station = _station_of(user)
    grants = (
        " Grant the helper Accessibility and Screen Recording and enable Remote Login with "
        "full disk access; these machine-wide grants are needed only on this first turn."
        if station == 1 else ""
    )
    previous = (
        f" First run project_handoff.py --station {prior} off console to verify its grant."
        if prior else ""
    )
    return {
        **credentials,
        "stations": list(vm.stations),
        "turn": station,
        "automatic_login": user,
        "complete": False,
        "next": (
            f"Complete first login for {user} and run project_handoff.py --station {station} "
            "to raise and allow Network Volumes. The screen lock is turned off for you."
            f"{grants}{previous} Then run mac-prepare with the same recipe again. The "
            f"credentials file holds {user}'s password for this turn, and the administrator's; "
            "both are eight digits you can type."
        ),
    }


@lifecycle(exclusive=True)
def prepare(vm, app: Path, recipe):
    """Advance one owner-attended console turn for the recipe's station accounts.

    Invoke it once to create and boot station 1, then again after each account's
    console work. The penultimate invocation returns the console to ``firmvm``; the
    final invocation verifies all station sessions off console. Between calls, the owner
    runs ``project_handoff.py --station N`` on the current console station and again
    after advancing, when that station is off console.
    """
    if not vm.network.ready() or not vm._running():
        raise RuntimeError("Start the isolated enrollment VM before preparing it")
    if vm.stations and len(vm.stations) != recipe.stations:
        raise ValueError(
            f"The Mac has {len(vm.stations)} stations and the recipe names {recipe.stations}"
        )
    with station_locks(vm.firm_locks, range(1, recipe.stations + 1)):
        # Before the console check and before any password-authenticated shell: a change
        # the previous call left half done is finished through the controller, whose sudo
        # rule needs no password.
        if vm._state().get("ready"):
            vm._rpc("settle-credentials", timeout=400)
        credentials = _control_credentials(vm)
        saved_progress = vm._state().get("enrollment_progress")
        prepared = None
        if not vm.stations:
            prepared = install_helper(vm, app, mode="prepare", stations=recipe.stations)
            # Consent and Remote Login ask for the administrator at the console. Every
            # clone of the sealed image gives firmvm its own long password at first boot.
            vm._rpc("rotate-control-password", typeable=True, timeout=210)
            progress = {
                "stations": recipe.stations,
                "recipe_sha256": recipe.sha256,
                "completed": [],
                "phase": "new",
                "target": None,
            }
        elif not isinstance(saved_progress, dict):
            # Accounts but no progress is a first call that died between preparation and
            # its first record. A sealed image's clone arrives the same way and is not
            # that: enrolling it again would walk console turns the image has been through.
            if vm.enrolled():
                raise RuntimeError(
                    "This Mac was cloned from an enrolled image; its accounts are already "
                    "enrolled. Prepare a firm cloned from the base image instead."
                )
            install_helper(vm, app, mode="enroll")
            progress = {
                "stations": recipe.stations,
                "recipe_sha256": recipe.sha256,
                "completed": [],
                "phase": "new",
                "target": None,
            }
        else:
            progress = saved_progress
        if (
            progress.get("stations") != recipe.stations
            or progress.get("recipe_sha256") != recipe.sha256
            or not isinstance(progress.get("completed"), list)
        ):
            raise ValueError("Continue enrollment with the same image recipe")
        if progress.get("phase") == "selecting":
            # Repeat an interrupted account selection and reboot; both are idempotent.
            progress = _reboot(vm, progress, progress["target"])
            prior = progress["completed"][-1] if progress["completed"] else 0
            return _turn_reply(_turn_credentials(vm, progress), vm, progress, prior=prior)
        if progress.get("phase") == "complete":
            # Complete means su was closed then; seal reads the policy itself, so a later
            # change to the file (a macOS update) is caught where the image is made.
            return {
                **credentials,
                "stations": list(vm.stations),
                "automatic_login": CONTROL_USER,
                "complete": True,
                "su_closed": True,
                "reason": None,
                "next": "Apply the recipe and seal the image.",
            }

        if progress.get("phase") == "console":
            target = progress.get("target")
            if _automatic_login(vm) != target or console_user(vm) != target:
                raise RuntimeError(f"Finish the console turn for {target} before continuing")
            if target == CONTROL_USER:
                for station in vm.stations:
                    _session_ready(vm, station)
                    _awake(vm, station)
                su = _close_su(vm)
                # Not complete until su is closed: seal checks the policy itself as well,
                # but enrollment must not say it is done while a step of it is not.
                phase = "complete" if su["su_closed"] else "console"
                progress = {**progress, "phase": phase, "su_closed": su["su_closed"]}
                save(vm.record, {**vm._state(), "enrollment_progress": progress})
                return {
                    **credentials,
                    "stations": list(vm.stations),
                    "automatic_login": CONTROL_USER,
                    "complete": su["su_closed"],
                    **su,
                    "next": "Apply the recipe and seal the image." if su["su_closed"] else (
                        "su could not be closed to the station group; check that Remote Login "
                        "allows full disk access, then run mac-prepare again."
                    ),
                }
            current = _station_of(target)
            # Its turn is over: nobody types this password again. First, before anything
            # that needs the recorded password: a change this call's predecessor left
            # unfinished is taken up here, so the session and awake steps below never
            # offer a password the account has already moved on from.
            vm._rpc("station-password", station=current, typeable=False, timeout=150)
            _session_ready(vm, current)
            vm._rpc("station-keep-awake", station=current, timeout=120)
            _awake(vm, current)
            completed = [*progress["completed"], current]
            progress = {**progress, "completed": completed}
        elif progress.get("phase") == "new":
            current = 0
        else:
            raise RuntimeError("The Mac has invalid enrollment progress")

        next_station = len(progress["completed"]) + 1
        user = f"firmstation{next_station}" if next_station <= recipe.stations else CONTROL_USER
        progress = _reboot(vm, progress, user)
        if current:
            _session_ready(vm, current)
            _awake(vm, current)
        reply = _turn_reply(_turn_credentials(vm, progress), vm, progress, prior=current)
        if prepared is None:
            return reply
        # What the guest reported as it created the accounts: the whole enrollment's
        # work, beside this turn's own next step.
        return {**reply, "prepared": prepared.get("next")}
