"""Root-only checks that a VM holds only a completed, released station enrollment.

Refuses extra ordinary accounts, an unfinished helper ledger, an installed lease
key, an unsealed installation, an account that can lock or sleep, an unfinished
first login, shared Homebrew without the station-group ACL, a Homebrew service or
system PATH entry, and any station sudo beyond the simulator platform installer.

Takes a JSON object with the recipe's application names and station count. Besides the checks above,
it reports what ``mac_image.seal_enrolled_image`` records in the sealed manifest:
each named application's bundle identifier and version and the selected Xcode's
version and build, both read from their plists, and the Homebrew version, asked of
``brew`` as station 1. Root runs nothing of Xcode's or Homebrew's here; the
host reads the simulator runtimes itself, as the control account.

Print only public image metadata. The control account's synthetic password and SSH
authorization remain image bootstrap material and rotate normally after cloning.
"""

import fcntl
import grp
import json
import os
import plistlib
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

LEASE_DIRECTIVES = (
    "PubkeyAuthentication yes",
    "PasswordAuthentication no",
    "AllowTcpForwarding no",
    "AllowAgentForwarding no",
    "X11Forwarding no",
    "PermitTTY yes",
    "PermitUserRC no",
    "AuthorizedKeysFile /etc/ssh/firm-lease-keys/%u",
    "ForceCommand /usr/local/libexec/firm-lease-shell",
)
ROOT_UID = 0
HOMEBREW = Path("/opt/homebrew")
# path_helper builds every login shell's PATH, root's under sudo and the control
# account's included, from these. The station-owned Homebrew must not be in them.
SYSTEM_PATHS = (Path("/etc/paths"), Path("/etc/paths.d"))
LEASE_SHELL = Path("/usr/local/libexec/firm-lease-shell")
LEASE_PATH = "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/homebrew/sbin"
# Station accounts' zsh startup files: /etc/zshenv sends them to a root-owned
# ZDOTDIR, so nothing a lease leaves in the home runs in a later lease's shell. The
# texts are the installer's; a unit test keeps them in step.
ZSHENV = Path("/etc/zshenv")
ZSHENV_BLOCK = """# firm-lease begin
# The station account's zsh reads startup files from a root-owned directory, never its home.
if /usr/sbin/dseditgroup -o checkmember -m "$(/usr/bin/id -un)" firmstations |
    /usr/bin/grep -q '^yes '; then
  ZDOTDIR=/usr/local/libexec/firm-lease-zsh
fi
# firm-lease end
"""
LEASE_ZDOTDIR = Path("/usr/local/libexec/firm-lease-zsh")
LEASE_ZDOTDIR_FILES = {
    ".zprofile": (
        "# Lease login shells: after /etc/zprofile's path_helper, Homebrew follows the"
        " system directories.\n"
        "path=(${path:#/opt/homebrew/*} /opt/homebrew/bin /opt/homebrew/sbin)\n"
    ),
    ".zshrc": (
        "# Interactive lease shells keep their history in the station home, as before.\n"
        "HISTFILE=$HOME/.zsh_history\n"
    ),
}
SYSTEM_SERVICE_DIRECTORIES = (Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons"))
# The station group's only sudo. The digests pin the exact bytes the installer
# writes (mac-station-add-platform.py and its SUDOERS text); a unit test keeps
# them in step with that asset.
PLATFORM_INSTALLER = Path("/usr/local/libexec/firm-station-add-platform")
# The installer's own LOCK, held for as long as an install runs.
PLATFORM_INSTALL_LOCK = "/var/run/firm-station-add-platform.lock"
# The one argument that only asks whether an install is running; mac_image.py sends it.
NO_INSTALL_RUNNING = "--no-install-running"
WORKER = "--seal-worker"
PLATFORM_INSTALLER_SHA256 = "ef8052a4f19659c5c5ba2d24de962f2d429f08fe159012c251158f8a5671dd0b"
# The platform installer runs the selected Xcode's xcodebuild as root; no part of an
# Xcode bundle may be writable by a group or other.
APPLICATIONS = Path("/Applications")
SELECTED_XCODE = re.compile(r"/Applications/[^/]+\.app/Contents/Developer")
# The first line of `brew --version`, which the station account could have rewritten.
HOMEBREW_VERSION = re.compile(r"Homebrew (\d+(?:\.\d+)+(?:-[0-9A-Za-z.-]+)?)$", re.MULTILINE)
# The guest controller's interpreter, which root runs; the image build owns it.
FIRM_TREE = Path("/usr/local/firm")
FIRM_PYTHON = FIRM_TREE / "bin" / "python3"
STATION_SUDOERS = Path("/etc/sudoers.d/firm-station")
STATION_INVENTORY = Path("/var/db/firm-station/stations.json")
HELPER_LAUNCHER = Path("/usr/local/libexec/firm-station-launch")
REMOTE_MANAGEMENT_MARKER = Path(
    "/Library/Application Support/Apple/Remote Desktop/RemoteManagement.launchd"
)
STATION_SUDOERS_SHA256 = "0b5975a045b57a9581b16637983d5c121b6a55f178536c70924bba200dce29af"
PLATFORMS = ("iOS", "watchOS", "tvOS", "visionOS")


def station_info(number):
    return {"station": number, "user": f"firmstation{number}", "uid": 501 + number}


def as_station(station, *command):
    """``command`` run as the station account, inside that account's own launchd session."""
    return [
        "/bin/launchctl", "asuser", str(station["uid"]), "/usr/bin/sudo", "-H", "-u",
        station["user"], *command,
    ]


def verify_accounts(station_count):
    """Return exactly the recipe's numbered ordinary accounts and verify inventory."""
    stations = [station_info(number) for number in range(1, station_count + 1)]
    if pwd.getpwnam("firmvm").pw_uid != 501:
        raise SystemExit("Unexpected control account identity")
    for station in stations:
        account = pwd.getpwnam(station["user"])
        if account.pw_uid != station["uid"] or account.pw_dir != f"/Users/{station['user']}":
            raise SystemExit("Unexpected station account identity")
    ordinary = [
        entry.pw_name for entry in pwd.getpwall() if entry.pw_uid >= 500 and entry.pw_uid < 65534
    ]
    if set(ordinary) != {"firmvm", *(station["user"] for station in stations)}:
        raise SystemExit("Image has additional ordinary accounts; create a fresh enrollment VM")
    info = STATION_INVENTORY.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != ROOT_UID
        or info.st_mode & 0o077
        or json.loads(STATION_INVENTORY.read_text()) != {"stations": station_count}
    ):
        raise SystemExit("The private station inventory does not match the recipe")
    return stations


def verify_lease_access(stations):
    """The group-keyed sshd block, wrapper, empty keys and per-account command logs."""
    config = Path("/etc/ssh/sshd_config").read_text()
    block = re.search(r"# firm-lease begin\n(.*?)# firm-lease end\n", config, re.S)
    if block is None or not block.group(1).startswith("Match Group firmstations\n"):
        raise SystemExit("sshd_config has no firm-lease Match block for firmstations")
    lines = {line.strip() for line in block.group(1).splitlines()}
    missing = [directive for directive in LEASE_DIRECTIVES if directive not in lines]
    if missing:
        raise SystemExit("sshd_config lease block lacks: " + ", ".join(missing))
    if subprocess.run(["/usr/sbin/sshd", "-t"], capture_output=True).returncode:
        raise SystemExit("sshd_config does not validate")
    wrapper = LEASE_SHELL
    text = wrapper.read_text()
    if not wrapper.stat().st_mode & 0o111 or "SSH_ORIGINAL_COMMAND" not in text:
        raise SystemExit("The lease shell wrapper is not installed")
    if LEASE_PATH not in text.splitlines():
        raise SystemExit("The lease shell wrapper does not set the lease PATH")
    if any(Path("/etc/ssh/firm-lease-keys").iterdir()):
        raise SystemExit("A lease key is installed; release the station before sealing")
    for station in stations:
        log = Path(f"/var/log/firm-station/{station['user']}/commands.log")
        directory = log.parent
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or directory.stat().st_uid != ROOT_UID
            or directory.stat().st_mode & 0o022
            or log.is_symlink()
            or not log.is_file()
            or log.stat().st_uid != ROOT_UID
        ):
            raise SystemExit(f"The lease command log is unsafe for {station['user']}")


def root_file(path, *, directory=False):
    """True for a root-owned file or directory, not a symlink, that no one else writes."""
    try:
        info = path.lstat()
    except OSError:
        return False
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return kind(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o022


def verify_lease_shell_startup():
    """The station account's zsh reads only the root-owned startup files the installer wrote."""
    if not root_file(ZSHENV) or not ZSHENV.read_text().startswith(ZSHENV_BLOCK):
        raise SystemExit("/etc/zshenv does not keep station shells out of the home's startup files")
    if not root_file(LEASE_ZDOTDIR, directory=True) or set(os.listdir(LEASE_ZDOTDIR)) != set(
        LEASE_ZDOTDIR_FILES
    ):
        raise SystemExit("The lease shell startup directory is not the root-owned one installed")
    for name, content in LEASE_ZDOTDIR_FILES.items():
        path = LEASE_ZDOTDIR / name
        if not root_file(path) or path.read_text() != content:
            raise SystemExit(f"{path} is not the root-owned startup file the installer writes")


def verify_access_scope(stations):
    """Plain Screen Sharing and every installed access policy select only firmstations."""
    if REMOTE_MANAGEMENT_MARKER.exists():
        raise SystemExit("Remote Management must be disabled")
    remote = subprocess.run(
        [
            "/usr/bin/defaults", "read",
            "/Library/Preferences/com.apple.RemoteManagement", "ARD_AllLocalUsers",
        ],
        capture_output=True, text=True, check=False,
    )
    if remote.returncode or remote.stdout.strip() not in {"0", "false"}:
        raise SystemExit("Remote Management must be disabled")
    disabled = subprocess.check_output(
        ["/bin/launchctl", "print-disabled", "system"], text=True
    )
    if not re.search(r'"com\.apple\.screensharing"\s*=>\s*(enabled|false)\b', disabled):
        raise SystemExit("Screen Sharing must be enabled")
    group = plistlib.loads(
        subprocess.check_output(
            [
                "/usr/bin/dscl", "-plist", ".", "-read",
                "/Groups/com.apple.access_screensharing",
            ]
        )
    )
    nested = subprocess.check_output(
        ["/usr/bin/dscl", ".", "-read", "/Groups/firmstations", "GeneratedUID"], text=True
    ).split()
    if (
        len(nested) != 2
        or group.get("dsAttrTypeStandard:NestedGroups") != [nested[1]]
        or group.get("dsAttrTypeStandard:GroupMembership")
        or group.get("dsAttrTypeStandard:GroupMembers")
    ):
        raise SystemExit("Screen Sharing must admit only firmstations")
    station_group = grp.getgrnam("firmstations")
    # The group's own member list and anyone whose primary group it is. Not
    # os.getgrouplist over every account: macOS's nobody has gid 4294967294, which it
    # cannot pass on (OverflowError, met on the first live seal).
    members = set(station_group.gr_mem) | {
        account.pw_name for account in pwd.getpwall()
        if account.pw_gid == station_group.gr_gid
    }
    if members != {station["user"] for station in stations}:
        raise SystemExit("firmstations must contain exactly the enrolled station accounts")
    automatic = subprocess.check_output(
        [
            "/usr/bin/defaults", "read", "/Library/Preferences/com.apple.loginwindow",
            "autoLoginUser",
        ],
        text=True,
    ).strip()
    if automatic != "firmvm":
        raise SystemExit("Automatic login must return to firmvm before sealing")
    launcher = HELPER_LAUNCHER.read_text()
    sudoers = STATION_SUDOERS.read_text()
    if "checkmember -m \"$user\" firmstations" not in launcher:
        raise SystemExit("The helper launcher is not keyed by firmstations")
    if "%firmstations ALL = (root) NOPASSWD:" not in sudoers:
        raise SystemExit("The station sudoers rule is not keyed by firmstations")


SU_POLICY = Path("/etc/pam.d/su")
SU_RULE = "account    required       pam_group.so no_warn deny group=firmstations ruser fail_safe"


def verify_su_closed():
    """The image itself says su is closed to the station group, not enrollment's memory of it.

    The rule must be the first account rule, so it is decided for every target account;
    a macOS update that rewrites the file would take it away without a word.
    """
    if SU_POLICY.is_symlink():
        raise SystemExit("The su PAM policy must not be a symlink")
    account_rules = [line.strip() for line in SU_POLICY.read_text().splitlines()
                     if line.split()[:1] == ["account"]]
    if not account_rules or account_rules[0] != SU_RULE:
        raise SystemExit("su is not closed to firmstations; finish enrollment (mac-prepare)")


CONTROL_JOURNAL = Path("/var/db/firm/control-password-rotation.json")
STATION_PASSWORDS = Path("/var/db/firm-station/passwords")


def verify_credentials_settled(stations):
    """No unfinished password change, and no station password a clone's owner could type.

    An enrollment console turn makes a password eight digits so the owner can type it;
    every clone would otherwise inherit that one. The control account keeps whichever
    form enrollment left it in, because every clone rotates it at first boot.
    """
    if CONTROL_JOURNAL.exists() or CONTROL_JOURNAL.is_symlink():
        raise SystemExit("A control password rotation is unfinished; run up again first")
    for station in stations:
        pending = STATION_PASSWORDS / (station["user"] + ".pending")
        if pending.exists() or pending.is_symlink():
            raise SystemExit(f"{station['user']}'s password change is unfinished")
        recorded = (STATION_PASSWORDS / station["user"]).read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{48}", recorded):
            raise SystemExit(f"{station['user']} still has an enrollment-turn password")
    control = Path("/Users/firmvm/password").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{48}|[0-9]{8}", control):
        raise SystemExit("The control password is not one this image's tools made")


def verify_system_paths():
    """No system PATH source names Homebrew, which the station account can rewrite."""
    sources = []
    for source in SYSTEM_PATHS:
        if source.is_dir() and not source.is_symlink():
            sources.extend(sorted(source.iterdir()))
        elif os.path.lexists(source):
            sources.append(source)
    for source in sources:
        if "homebrew" in source.name.lower():
            raise SystemExit(f"A system PATH entry names Homebrew: {source}")
        try:
            text = source.read_text(errors="replace")
        except OSError:
            continue
        if any(str(HOMEBREW) in line for line in text.splitlines()):
            raise SystemExit(f"A system PATH entry names Homebrew: {source}")


def verify_homebrew(stations):
    """Every station can write shared Homebrew, no system PATH names it, and it runs nothing.

    An image without Homebrew (built without Xcode) passes. A service a role would
    start with ``brew services`` is lease work; none may be baked into the image.
    """
    verify_system_paths()
    if not os.path.lexists(HOMEBREW):
        return None
    info = HOMEBREW.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID:
        raise SystemExit("Homebrew is not the root-owned shared prefix made by mac-prepare")
    acl = subprocess.run(
        ["/bin/ls", "-lde", str(HOMEBREW)], capture_output=True, text=True, check=True
    ).stdout
    if "group:firmstations allow" not in acl or not all(
        right in acl for right in ("file_inherit", "directory_inherit", "read", "write")
    ):
        raise SystemExit("Homebrew lacks the inheritable firmstations ACL")
    for station in stations:
        writable = subprocess.run(
            ["/usr/bin/sudo", "-n", "-u", station["user"], "/bin/test", "-w", str(HOMEBREW)]
        )
        if writable.returncode:
            raise SystemExit(f"Homebrew is not writable by {station['user']}")
    directories = [
        *(Path(f"/Users/{station['user']}/Library/LaunchAgents") for station in stations),
        *SYSTEM_SERVICE_DIRECTORIES,
    ]
    for directory in directories:
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            continue
        if any(name.startswith("homebrew.mxcl.") for name in names):
            raise SystemExit("A Homebrew service is installed; stop it before sealing")
    for domain in (
        *(f"gui/{station['uid']}" for station in stations),
        *(f"user/{station['uid']}" for station in stations),
        "system",
    ):
        listing = subprocess.run(
            ["/bin/launchctl", "print", domain], capture_output=True, text=True, check=False
        )
        if "homebrew.mxcl." in listing.stdout:
            raise SystemExit("A Homebrew service is loaded; stop it before sealing")
    # The installed and loaded checks above are authoritative for services. This one
    # also catches a detached program, by the executable the kernel reports for each
    # process rather than an argv[0] the process can set.
    prefixes = (f"{HOMEBREW}/", f"/System/Volumes/Data{HOMEBREW}/")
    if any(path.startswith(prefixes) for path in running_executables()):
        raise SystemExit("A Homebrew program is running; stop it before sealing")
    return str(HOMEBREW)


def running_executables():
    """The executable path of every process, from the kernel through libproc."""
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    estimate = libproc.proc_listallpids(None, 0)
    if estimate <= 0:
        raise SystemExit("Could not list the running processes")
    pids = (ctypes.c_int * (estimate + 1024))()
    count = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    if count <= 0:
        raise SystemExit("Could not list the running processes")
    buffer = ctypes.create_string_buffer(4 * 1024)
    paths = []
    for pid in pids[:count]:
        if libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer)) > 0:
            paths.append(os.fsdecode(buffer.value))
    return paths


def sudo_rules(listing):
    """Each rule ``sudo -l -U`` lists for the account, joined back across sudo's wrapping.

    Without a terminal sudo wraps its listing at 80 columns on spaces and indents each
    continuation deeper than the rule it continues, so a rule is its first line plus
    every more deeply indented line after it, joined with single spaces. Every line
    after the command header belongs to some rule; a listing without the header has none.
    """
    lines = listing.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if "may run the following commands" in line),
        None,
    )
    if start is None:
        return []
    # A long host name wraps the header itself, which ends with a colon.
    while not lines[start].rstrip().endswith(":") and start + 1 < len(lines):
        start += 1
    rules, rule_indent = [], None
    for line in lines[start + 1:]:
        text = line.strip()
        if not text:
            continue
        indent = len(line) - len(line.lstrip())
        if rule_indent is None or indent <= rule_indent:
            rule_indent = indent if rule_indent is None else rule_indent
            rules.append(text)
        else:
            rules[-1] += " " + text
    return rules


def sudo_commands(listing):
    """The commands of the account's single ``(root) NOPASSWD:`` rule, or None otherwise.

    Any second rule, other run-as user or group, or other tag makes it None; a tag
    after NOPASSWD would show up as part of the first command and not match.
    """
    rules = sudo_rules(listing)
    prefix = "(root) NOPASSWD: "
    if len(rules) != 1 or not rules[0].startswith(prefix):
        return None
    return [command.strip() for command in rules[0][len(prefix):].split(",")]


def exclude_platform_installs():
    """Take the platform installer's lock, or refuse while an install holds it.

    An image sealed under an install would carry half a runtime. The caller keeps the
    returned descriptor open, so nothing this script runs later, the station account's
    own Homebrew included, can start one either.
    """
    descriptor = os.open(PLATFORM_INSTALL_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise SystemExit(
            "A simulator platform install is still running; wait for it before sealing"
        ) from None
    return descriptor


def machine_lock():
    """Hold the controller's exclusive machine lock, bounded like its RPC operations."""
    descriptor = os.open("/var/db/firm/lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise SystemExit("The Mac is busy with another operation; try again") from None
            time.sleep(0.1)


def supervise(recipe):
    """Bound and reap the whole seal inspection before its guest machine lock can escape."""
    machine_excluded = machine_lock()
    try:
        installs_excluded = exclude_platform_installs()
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", __file__, WORKER, recipe],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
                raise SystemExit("Image seal inspection exceeded its 600 second deadline") from None
            finally:
                # A worker that failed after starting a grandchild cannot leave it behind
                # after the parent releases either guest-wide lock.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.returncode is None:
                    process.wait()
        finally:
            os.close(installs_excluded)
    finally:
        os.close(machine_excluded)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if process.returncode:
        raise SystemExit(process.returncode)


def verify_platform_installer(stations):
    """The root-owned platform installer and sudoers drop-in, byte for byte, and no other sudo."""
    import hashlib

    for path, digest, mode in (
        (PLATFORM_INSTALLER, PLATFORM_INSTALLER_SHA256, 0o755),
        (STATION_SUDOERS, STATION_SUDOERS_SHA256, 0o440),
    ):
        try:
            info = path.lstat()
            data = path.read_bytes()
        except OSError:
            raise SystemExit(f"{path} is missing; install the helper again") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_UID
            or stat.S_IMODE(info.st_mode) != mode
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise SystemExit(f"{path} is not the root-owned file the installer writes")
    # Validate the files Firm owns. A whole-configuration `visudo -c` also checks
    # Lima's /etc/sudoers.d/90-cloud-init-users, which Lima writes mode 0400 and
    # visudo's strict mode check rejects; sudo itself still reads it, and the
    # listings below are the authoritative check of what each station may run.
    for sudoers in (Path("/etc/sudoers"), STATION_SUDOERS):
        if subprocess.run(
            ["/usr/sbin/visudo", "-c", "-f", str(sudoers)], capture_output=True
        ).returncode:
            raise SystemExit("The sudoers configuration does not validate")
    expected = sorted(f"{PLATFORM_INSTALLER} {platform}" for platform in PLATFORMS)
    for station in stations:
        listing = subprocess.run(
            ["/usr/bin/sudo", "-n", "-l", "-U", station["user"]],
            capture_output=True, text=True, check=False,
        )
        commands = sudo_commands(listing.stdout)
        if listing.returncode or commands is None or sorted(commands) != expected:
            raise SystemExit(
                f"{station['user']} may sudo more or less than the platform installer"
            )


def writable_entries(root):
    """Non-symlink paths in ``root``, itself included, that a group or other can write."""
    found = []

    def check(path):
        info = os.lstat(path)
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
            found.append(path)

    check(root)
    for directory, names, files in os.walk(root):
        for name in names + files:
            check(os.path.join(directory, name))
        if found:
            break
    return found


def verify_interpreter_tree():
    """Root's interpreter and everything under /usr/local/firm belong to root alone."""
    python = Path(os.path.realpath(FIRM_PYTHON))
    try:
        info = python.lstat()
    except OSError:
        raise SystemExit("The guest controller's Python is missing") from None
    if (
        not python.is_relative_to(FIRM_TREE)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != ROOT_UID
        or info.st_mode & 0o022
    ):
        raise SystemExit(f"The guest controller's Python is not root's alone: {python}")
    found = writable_entries(str(FIRM_TREE))
    if found:
        raise SystemExit(f"The firm interpreter tree has a writable entry: {found[0]}")
    for directory, names, files in os.walk(FIRM_TREE):
        for path in (directory, *(os.path.join(directory, name) for name in names + files)):
            if os.lstat(path).st_uid != ROOT_UID:
                raise SystemExit(f"The firm interpreter tree is not root-owned throughout: {path}")


def verify_xcode_permissions():
    """No Xcode in /Applications holds an entry a group or other could change."""
    for app in sorted(APPLICATIONS.glob("Xcode*.app")):
        if app.is_symlink():
            continue
        found = writable_entries(str(app))
        if found:
            raise SystemExit(f"Xcode has a group- or world-writable entry: {found[0]}")


def verify_station_awake(station):
    """One account never locks or sleeps its desktop; a lease cannot unlock one.

    The host restates all of this at every boot, machine-wide and per account, so a
    refusal here is answered by restarting the Mac, not by enrolling again.
    """
    # The account's own answer, which sysadminctl writes to stderr. Not the old
    # com.apple.screensaver askForPassword key: macOS 27 does not write it, and the first
    # live seal refused two stations whose lock was off.
    lock = subprocess.run(
        as_station(station, "/usr/sbin/sysadminctl", "-screenLock", "status"),
        capture_output=True, text=True, check=False,
    )
    answer = (lock.stderr + lock.stdout).strip().rsplit("] ", 1)[-1]
    if lock.returncode or answer != "screenLock is off":
        raise SystemExit(
            f"{station['user']} locks its screen ({answer or 'no answer'}); run mac-restart, "
            "which turns it off for every station"
        )
    power = subprocess.check_output(["/usr/bin/pmset", "-g"], text=True)
    settings = {
        line.split()[0]: line.split()[1] for line in power.splitlines() if len(line.split()) >= 2
    }
    if any(settings.get(key) != "0" for key in ("displaysleep", "sleep", "disksleep")):
        raise SystemExit("The Mac sleeps; run mac-restart first")
    idle = subprocess.run(
        as_station(station, "/usr/bin/defaults", "-currentHost", "read",
                   "com.apple.screensaver", "idleTime"),
        capture_output=True, text=True, check=False,
    )
    if idle.returncode != 0 or idle.stdout.strip() != "0":
        raise SystemExit(
            f"{station['user']} starts its screen saver; run mac-restart first"
        )
    relock = subprocess.run(
        ["/usr/bin/defaults", "read", "/Library/Preferences/com.apple.loginwindow",
         "autoLoginUserScreenLocked"],
        capture_output=True, text=True, check=False,
    )
    if relock.returncode == 0 and relock.stdout.strip() == "1":
        raise SystemExit("The Mac would relock at login; run mac-restart first")


def read_application(name):
    """One recipe-named application's bundle identifier and version, from its Info.plist."""
    plist_path = APPLICATIONS / name / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as source:
            plist = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SystemExit(f"{name} has no readable Info.plist") from exc
    identifier = plist.get("CFBundleIdentifier")
    version = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
    if not isinstance(identifier, str) or not isinstance(version, str):
        raise SystemExit(f"{name} has no bundle identifier or version")
    return {"name": name, "bundle_id": identifier, "version": version}


def read_xcode():
    """The selected Xcode's version and build from its version.plist, or None without one.

    Nothing of Xcode's runs here: root only asks xcode-select which one is selected.
    """
    selected = subprocess.run(
        ["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, check=False
    )
    developer = selected.stdout.strip()
    if selected.returncode or not SELECTED_XCODE.fullmatch(developer):
        return None
    try:
        with (Path(developer).parent / "version.plist").open("rb") as source:
            plist = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException):
        raise SystemExit("The selected Xcode has no readable version.plist") from None
    version, build = plist.get("CFBundleShortVersionString"), plist.get("ProductBuildVersion")
    if not isinstance(version, str) or not isinstance(build, str):
        raise SystemExit("The selected Xcode does not state its version and build")
    return {"version": version, "build": build}


def read_homebrew(prefix, station):
    """``brew --version`` as one station account; never run it under root."""
    if prefix is None:
        return None
    try:
        result = subprocess.run(
            as_station(station, f"{prefix}/bin/brew", "--version"),
            capture_output=True, text=True, check=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("Homebrew did not report its version") from None
    version = HOMEBREW_VERSION.match(result.stdout)
    if not version:
        raise SystemExit("Homebrew did not report its version")
    return {"prefix": prefix, "version": version.group(1)}


def main():
    if (
        os.getuid() != 0
        or subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"], text=True
        ).strip()
        != "1"
    ):
        raise SystemExit("Image sealing requires root inside a disposable VM")
    if sys.argv[1:] == [NO_INSTALL_RUNNING]:
        # The host asks this before it lists the Mac's runtimes. An install that outlived
        # a lost mac-image-apply is either refused here, or over and in that listing.
        os.close(exclude_platform_installs())
        return
    if len(sys.argv) == 2:
        supervise(sys.argv[1])
        return
    if len(sys.argv) != 3 or sys.argv[1] != WORKER:
        raise SystemExit("usage: mac-station-seal.py RECIPE_JSON")
    try:
        request = json.loads(sys.argv[2])
    except ValueError:
        raise SystemExit("Invalid seal recipe") from None
    if not isinstance(request, dict) or set(request) != {"applications", "stations"}:
        raise SystemExit("Invalid seal recipe")
    application_names, station_count = request["applications"], request["stations"]
    if not isinstance(application_names, list) or not all(
        isinstance(name, str) and name.endswith(".app") and "/" not in name
        for name in application_names
    ) or type(station_count) is not int or not 1 <= station_count <= 8:
        raise SystemExit("Invalid seal recipe")
    stations = verify_accounts(station_count)
    record = Path("/var/db/firm/firm.json")
    for station in stations:
        ledger = (
            Path("/Users/Shared/firm-station-helper")
            / station["user"]
            / ".firm-station-helper-ledger.json"
        )
        if ledger.exists():
            raise SystemExit(f"{station['user']} has an unfinished application ledger")
    os_build = subprocess.check_output(["/usr/bin/sw_vers", "-buildVersion"], text=True).strip()
    for station in stations:
        defaults = as_station(station, "/usr/bin/defaults")
        # macOS 27 Setup Assistant no longer shows the privacy pane, so DidSeePrivacy
        # stays 0 after a genuine first login; these panes are shown on 26 and 27.
        for key, expected in (
            ("LastSeenBuddyBuildVersion", os_build),
            ("DidSeeCloudSetup", "1"),
            ("DidSeeAppearanceSetup", "1"),
        ):
            completed = subprocess.check_output(
                defaults + ["read", "com.apple.SetupAssistant", key], text=True
            ).strip()
            if completed != expected:
                raise SystemExit(f"Complete {station['user']}'s first login before sealing")
        # Apply the reviewed post-enrollment repair only after genuine completion.
        # A stale global resume flag otherwise relaunches Setup Assistant on reboot.
        for scope in ([], ["-currentHost"]):
            subprocess.run(
                defaults
                + scope
                + ["write", "com.apple.loginwindow", "MiniBuddyLaunch", "-bool", "false"],
                check=True,
            )
        subprocess.run(
            defaults
            + ["write", "com.apple.loginwindow", "TALBlockSavingAndLaunch", "-bool", "true"],
            check=True,
        )
    # The relay is installed root-owned and authenticates each account's helper socket.
    app = Path("/Applications/Firm Station Helper.app")
    for path in (
        app,
        Path("/usr/local/libexec/firm-station-launch"),
        Path("/usr/local/libexec/firm-station-relay.py"),
        Path("/usr/local/libexec/firm-lease-shell"),
        Path("/usr/local/libexec/firm-lease-zsh"),
        Path("/usr/local/libexec/firm-station-add-platform"),
        Path("/Library/LaunchAgents/dev.firm.station-helper.plist"),
        Path("/etc/ssh/firm-lease-keys"),
    ):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit("The station helper installation is not root-owned and sealed")
    verify_lease_access(stations)
    verify_lease_shell_startup()
    verify_access_scope(stations)
    verify_su_closed()
    verify_credentials_settled(stations)
    for station in stations:
        verify_station_awake(station)
    homebrew_prefix = verify_homebrew(stations)
    verify_platform_installer(stations)
    verify_xcode_permissions()
    verify_interpreter_tree()
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "-r-", str(app)], capture_output=True, check=True, text=True
    )
    requirement = next(
        (
            line
            for line in (result.stdout + result.stderr).splitlines()
            if line.startswith("designated => ")
        ),
        "",
    )
    if (
        not requirement
        or "cdhash" in requirement
        or not re.search(r"certificate|anchor", requirement)
    ):
        raise SystemExit("An enrolled image requires a persistent signing identity")
    # Everything that can still refuse comes before the identity below is cleared.
    applications = [read_application(name) for name in application_names]
    xcode = read_xcode()
    homebrew = read_homebrew(homebrew_prefix, stations[0])
    # Remove only the template's firm identity; each new clone binds its own. Never
    # scan or wipe the station account's data or TCC; this is a dedicated enrollment image.
    temporary = record.with_suffix(".seal-new")
    with temporary.open("x") as stream:
        json.dump({}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(record)
    print(
        json.dumps(
            {
                "os_build": os_build,
                "enrollment": {
                    "stations": station_count,
                    "control_uid": 501,
                    "environment": "vm-session",
                    "designated_requirement": requirement,
                },
                "applications": applications,
                "xcode": xcode,
                "homebrew": homebrew,
            }
        )
    )


if __name__ == "__main__":
    main()
