#!/usr/local/firm/bin/python3 -I
"""Install one simulator runtime with the image's Xcode: the station account's only sudo.

Installed root-owned at /usr/local/libexec/firm-station-add-platform. The sudoers
drop-in lets the station account run it as root without a password with exactly one
argument from PLATFORMS, each spelled out; there is no other sudo rule for it, so a
lease installs a platform's current runtime and cannot name a version. The wrapper
takes nothing else from its caller: it validates its arguments again, finds Xcode
through xcode-select with a fixed environment, and runs
``xcodebuild -downloadPlatform <platform>`` under that same fixed environment.

``mac-image-apply`` runs this same file as root through the control account, copied to
a temporary path, and may add the VERSION a recipe pins, which becomes
``-buildVersion <version>``.

One install runs at a time in a Mac, under LOCK, and bounds itself: the download is
this process's child in its own process group, and this process holds the lock until
that whole group is gone, at the download's own exit, at DEADLINE, or when a caller,
whose signals sudo relays, tells it to stop. A caller whose connection is lost
therefore finds the lock held, not a second download racing the first, and
``mac-image-seal`` holds the same lock while it inspects the Mac.

A runtime it installs is system-wide and stays in the firm's Mac across leases.
"""

import fcntl
import os
import re
import signal
import stat
import subprocess
import sys
import time

WRAPPER = "/usr/local/libexec/firm-station-add-platform"
# Root's, and gone at reboot with whatever held it. mac-station-seal.py names it too.
LOCK = "/var/run/firm-station-add-platform.lock"
# Seconds; mac-image-apply waits longer than this for the reply (SIMULATOR_TIMEOUT).
DEADLINE = 6600
# Every signal a caller can have relayed here that would otherwise end or suspend this
# process with its download still running. SIGKILL and SIGSTOP are root's alone.
STOP_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM", "SIGALRM", "SIGUSR1", "SIGUSR2",
                 "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGXCPU", "SIGXFSZ", "SIGVTALRM", "SIGPROF")
)
PLATFORMS = ("iOS", "watchOS", "tvOS", "visionOS")
# A runtime's version as simctl reports it, which is what a recipe pins.
VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
ENVIRONMENT = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME": "/var/root",
    "LANG": "en_US.UTF-8",
}
# The image build copies Xcode as the control account, so its files keep that owner.
TRUSTED_OWNERS = (0, 501)
SELECTED_XCODE = re.compile(r"/Applications/[^/]+\.app/Contents/Developer")
SUDOERS_FILE = "/etc/sudoers.d/firm-station"
SUDOERS = (
    "# Firm station: the station account may install simulator platforms, nothing else.\n"
    f"Defaults!{WRAPPER} env_reset, !requiretty\n"
    "%firmstations ALL = (root) NOPASSWD: "
    + ", ".join(f"{WRAPPER} {platform}" for platform in PLATFORMS)
    + "\n"
)
USAGE = "usage: sudo -n firm-station-add-platform " + "|".join(PLATFORMS) + " [VERSION]"


def runtime(arguments):
    """The one allowed platform name and, when given, a well-formed version; or refuse."""
    if not 1 <= len(arguments) <= 2 or arguments[0] not in PLATFORMS:
        raise SystemExit(USAGE)
    if len(arguments) == 2 and not VERSION.fullmatch(arguments[1]):
        raise SystemExit(USAGE)
    return arguments[0], arguments[1] if len(arguments) == 2 else None


def xcodebuild(selected):
    """xcodebuild inside the selected /Applications Xcode, when nobody else can change it.

    Every component from xcodebuild up to the ``/Applications/<name>.app`` bundle must
    itself be no symlink, belong to a trusted owner and be writable by no group or
    other, so no one but root and the control account can swap any part of the path.
    """
    developer = selected.strip()
    if not SELECTED_XCODE.fullmatch(developer) or os.path.realpath(developer) != developer:
        raise SystemExit("The selected Xcode is not an application in /Applications")
    tool = developer + "/usr/bin/xcodebuild"
    app = developer.removesuffix("/Contents/Developer")
    path = tool
    while True:
        try:
            info = os.lstat(path)
        except OSError:
            raise SystemExit("The selected Xcode has no xcodebuild") from None
        kind = stat.S_ISREG if path == tool else stat.S_ISDIR
        if (
            not kind(info.st_mode)
            or info.st_uid not in TRUSTED_OWNERS
            or info.st_mode & 0o022
        ):
            raise SystemExit(f"The selected Xcode is not trusted: {path}")
        if path == app:
            return tool
        path = os.path.dirname(path)


def install_lock(path=LOCK):
    """The Mac's install lock, or refuse while another install holds it."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise SystemExit(
            "Another simulator platform install is still running on this Mac"
        ) from None
    return descriptor


def supervise(command, *, deadline=DEADLINE):
    """Run the download bounded, in its own process group, and leave nothing of it behind.

    A signal only sets a flag, so nothing interrupts starting the download or clearing
    up after it. The group is killed however the download ended, because a helper it
    started can outlive it. Returns the download's exit status, or refuses.
    """
    stops = []
    for signum in STOP_SIGNALS:
        signal.signal(signum, lambda received, _frame: stops.append(received))
    child = subprocess.Popen(command, env=ENVIRONMENT, start_new_session=True)
    expires = time.monotonic() + deadline
    try:
        while child.poll() is None and not stops and time.monotonic() < expires:
            time.sleep(0.2)
    finally:
        status = child.poll()
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
    if status is None:
        raise SystemExit(
            "The simulator platform install was stopped"
            if stops
            else "The simulator platform install passed its deadline and was stopped"
        )
    return status


def install(command):
    """One bounded install under the Mac's install lock, held until its group is gone."""
    lock = install_lock()
    try:
        return supervise(command)
    finally:
        os.close(lock)


def main(
    arguments, *, run=subprocess.run, install=install, effective_uid=os.geteuid,
    chdir=os.chdir,
):
    name, version = runtime(arguments)
    if effective_uid() != 0:
        raise SystemExit("Run it through sudo: " + USAGE)
    # Nothing runs as root in the caller's working directory.
    chdir("/")
    selected = run(
        ["/usr/bin/xcode-select", "-p"],
        capture_output=True,
        text=True,
        env=ENVIRONMENT,
        check=False,
    )
    if selected.returncode:
        raise SystemExit("No Xcode is selected on this Mac")
    tool = xcodebuild(selected.stdout)
    command = [tool, "-downloadPlatform", name]
    if version is not None:
        command += ["-buildVersion", version]
    raise SystemExit(install(command))


if __name__ == "__main__":
    main(sys.argv[1:])
