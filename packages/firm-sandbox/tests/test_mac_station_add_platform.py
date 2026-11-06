"""The station account's only sudo: the simulator platform installer and its sudoers rule."""

import hashlib
import importlib.util
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import mac_image
from firm_sandbox.mac_station import ASSETS

ASSET = ASSETS / "mac-station-add-platform.py"
WRAPPER = "/usr/local/libexec/firm-station-add-platform"
XCODE = "/Applications/Xcode.app/Contents/Developer"
STATIONS = [
    {"station": 1, "user": "firmstation1", "uid": 502},
    {"station": 2, "user": "firmstation2", "uid": 503},
]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer():
    return load(ASSET, "mac_station_add_platform")


@pytest.fixture
def seal():
    return load(ASSETS / "mac-station-seal.py", "mac_station_seal")


@pytest.mark.parametrize("name", ["iOS", "watchOS", "tvOS", "visionOS"])
def test_each_allowed_platform_is_accepted(installer, name):
    assert installer.runtime([name]) == (name, None)


@pytest.mark.parametrize("version", ["26.0", "17.5", "18.3.1"])
def test_a_version_as_simctl_reports_it_is_accepted(installer, version):
    assert installer.runtime(["iOS", version]) == ("iOS", version)


@pytest.mark.parametrize("arguments", [
    [], ["bogus"], ["ios"], ["IOS"], ["macOS"], ["iOS", "watchOS"], ["iOS", "-exportPath"],
    ["-downloadAllPlatforms"], ["iOS "], [" iOS"], ["iOS;true"], ["iOS\n"], ["--help"],
    ["-buildVersion"], ["../iOS"], [""], ["iOS", "26"], ["iOS", "26.0\n"], ["iOS", " 26.0"],
    ["iOS", "26.0", "27.0"], ["iOS", "-buildVersion"], ["iOS", "26.0;true"], ["iOS", ""],
    ["26.0", "iOS"], ["iOS", "23A343"],
])
def test_anything_else_is_refused_before_touching_the_system(installer, arguments):
    ran = []
    with pytest.raises(SystemExit, match="usage: sudo -n firm-station-add-platform"):
        installer.main(
            arguments, run=lambda *a, **k: ran.append(a),
            install=lambda command: ran.append(command), effective_uid=lambda: 0,
        )
    assert ran == []


def test_it_requires_root_and_refuses_before_running_anything(installer):
    ran = []
    with pytest.raises(SystemExit, match="through sudo"):
        installer.main(["iOS"], run=lambda *a, **k: ran.append(a),
                       install=lambda command: ran.append(command), effective_uid=lambda: 502,
                       chdir=lambda path: ran.append(path))
    assert ran == []


def test_it_runs_only_the_selected_xcodes_download_with_a_fixed_environment(
    installer, monkeypatch
):
    calls, installed = [], []
    monkeypatch.setattr(installer, "xcodebuild", lambda selected: (
        calls.append(("xcodebuild", selected)) or XCODE + "/usr/bin/xcodebuild"
    ))

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=XCODE + "\n")

    monkeypatch.setenv("DEVELOPER_DIR", "/Users/firmstation1/FakeXcode.app/Contents/Developer")
    with pytest.raises(SystemExit) as finished:
        installer.main(["watchOS"], run=run, install=lambda command: installed.append(command) or 0,
                       effective_uid=lambda: 0, chdir=lambda path: calls.append(("chdir", path)))
    assert finished.value.code == 0
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/var/root",
                   "LANG": "en_US.UTF-8"}
    assert installer.ENVIRONMENT == environment
    # The caller's working directory is left before anything runs as root.
    assert calls.pop(0) == ("chdir", "/")
    assert calls[0] == (["/usr/bin/xcode-select", "-p"], {
        "capture_output": True, "text": True, "env": environment, "check": False,
    })
    assert calls[1] == ("xcodebuild", XCODE + "\n")
    tool = XCODE + "/usr/bin/xcodebuild"
    assert installed == [[tool, "-downloadPlatform", "watchOS"]]


def test_a_pinned_version_becomes_build_version_and_the_downloads_status_is_the_exit(
    installer, monkeypatch
):
    installed = []
    tool = XCODE + "/usr/bin/xcodebuild"
    monkeypatch.setattr(installer, "xcodebuild", lambda selected: tool)
    with pytest.raises(SystemExit) as finished:
        installer.main(
            ["iOS", "26.0"],
            run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=XCODE + "\n"),
            install=lambda command: installed.append(command) or 70,
            effective_uid=lambda: 0, chdir=lambda path: None,
        )
    assert installed == [[tool, "-downloadPlatform", "iOS", "-buildVersion", "26.0"]]
    assert finished.value.code == 70


# ---- one bounded install at a time ------------------------------------------------------


def test_a_second_install_is_refused_while_the_first_holds_the_lock(installer, tmp_path):
    path = str(tmp_path / "install.lock")
    held = installer.install_lock(path)
    try:
        with pytest.raises(SystemExit, match="still running"):
            installer.install_lock(path)
    finally:
        os.close(held)
    os.close(installer.install_lock(path))  # free again once the holder is gone
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_install_lock_is_never_taken_through_a_link(installer, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / "install.lock").symlink_to(target)
    with pytest.raises(OSError):
        installer.install_lock(str(tmp_path / "install.lock"))
    assert target.read_text() == "keep"


def test_the_download_runs_under_the_fixed_environment_and_its_status_is_returned(
    installer, tmp_path
):
    report = tmp_path / "environment"
    script = (
        f"import os, sys; open({str(report)!r}, 'w').write(repr(sorted(os.environ))); sys.exit(7)"
    )
    assert installer.supervise([sys.executable, "-c", script]) == 7
    assert "'HOME'" in report.read_text() and "'DEVELOPER_DIR'" not in report.read_text()


def _gone(pid_file, seconds=5):
    """The process that wrote its pid there is gone, give or take the kill's delivery."""
    pid = pid_file.read_text()
    expires = time.monotonic() + seconds
    while time.monotonic() < expires:
        listed = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", pid], capture_output=True, text=True
        )
        # A zombie awaiting its reaper is as gone as a process gets.
        if listed.returncode != 0 or listed.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def _wait_for(path, seconds=10):
    expires = time.monotonic() + seconds
    while not path.exists() or not path.read_text():
        assert time.monotonic() < expires, f"{path} never appeared"
        time.sleep(0.05)


SLEEPER = "import os, time; open({path!r}, 'w').write(str(os.getpid())); time.sleep(600)"


def test_a_download_past_its_deadline_is_stopped_and_nothing_of_it_is_left(installer, tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(SystemExit, match="passed its deadline"):
        installer.supervise(
            [sys.executable, "-c", SLEEPER.format(path=str(pid_file))], deadline=2
        )
    assert _gone(pid_file)


def test_a_helper_the_download_leaves_behind_does_not_outlive_it(installer, tmp_path):
    """xcodebuild can exit, or crash, with something it started still running."""
    pid_file = tmp_path / "straggler"
    straggler = SLEEPER.format(path=str(pid_file))
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {straggler!r}]); "
        f"import pathlib; p = pathlib.Path({str(pid_file)!r}); "
        "exec('while not (p.exists() and p.read_text()): time.sleep(0.05)'); sys.exit(0)"
    )
    assert installer.supervise([sys.executable, "-c", script]) == 0
    assert _gone(pid_file)


@pytest.mark.parametrize("signum", [
    signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGUSR1, signal.SIGTSTP,
])
def test_a_relayed_signal_stops_the_download_instead_of_orphaning_it(
    installer, tmp_path, signum
):
    """sudo relays its caller's signals; the default action would end the only deadline."""
    pid_file, lock = tmp_path / "pid", str(tmp_path / "install.lock")
    harness = (
        "import importlib.util, sys; "
        f"spec = importlib.util.spec_from_file_location('installer', {str(ASSET)!r}); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        f"module.LOCK = {lock!r}; module.install_lock.__defaults__ = ({lock!r},); "
        f"raise SystemExit(module.install([sys.executable, '-c', "
        f"{SLEEPER.format(path=str(pid_file))!r}]))"
    )
    supervisor = subprocess.Popen([sys.executable, "-c", harness], stderr=subprocess.PIPE)
    try:
        _wait_for(pid_file)
        # While it runs, the lock is the supervisor's: a second install is refused.
        with pytest.raises(SystemExit, match="still running"):
            installer.install_lock(lock)
        supervisor.send_signal(signum)
        assert supervisor.wait(timeout=20) == 1
        assert b"install was stopped" in supervisor.stderr.read()
    finally:
        supervisor.kill()
        supervisor.wait()
    assert _gone(pid_file)
    os.close(installer.install_lock(lock))  # and the lock went with the supervisor


def test_every_signal_sudo_relays_is_handled(installer):
    """sudo forwards these to the command; Python already ignores SIGPIPE."""
    relayed = {"SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM", "SIGTSTP", "SIGALRM", "SIGUSR1",
               "SIGUSR2"}
    assert relayed <= {signal.Signals(signum).name for signum in installer.STOP_SIGNALS}
    assert signal.SIGKILL not in installer.STOP_SIGNALS


def test_the_host_waits_longer_than_the_install_bounds_itself(installer):
    assert installer.DEADLINE < mac_image.SIMULATOR_TIMEOUT


def test_the_station_accounts_sudo_rule_names_no_version(installer):
    """sudoers matches a listed command's arguments exactly, so a lease cannot add one."""
    rule = installer.SUDOERS.splitlines()[-1]
    commands = rule.split("NOPASSWD: ")[1].split(", ")
    assert commands == [f"{WRAPPER} {name}" for name in installer.PLATFORMS]


def test_no_selected_xcode_is_refused(installer):
    with pytest.raises(SystemExit, match="No Xcode is selected"):
        installer.main(["iOS"], run=lambda *a, **k: SimpleNamespace(returncode=2, stdout=""),
                       install=lambda command: 0, effective_uid=lambda: 0,
                       chdir=lambda path: None)


def test_main_leaves_the_callers_working_directory_by_default(installer):
    import inspect

    assert inspect.signature(installer.main).parameters["chdir"].default is os.chdir


APP = "/Applications/Xcode.app"
XCODE_CHAIN = [
    XCODE + "/usr/bin/xcodebuild", XCODE + "/usr/bin", XCODE + "/usr", XCODE,
    APP + "/Contents", APP,
]


@pytest.mark.parametrize("fault", [
    None, "outside", "relative", "nested", "symlinked", "missing", "link", "owner", "writable",
    "world-writable", "parent-owner", "parent-writable", "parent-link", "parent-file",
    "developer-writable", "app-owner", "app-writable", "app-link", "root-owned",
])
def test_xcodebuild_and_every_component_up_to_the_app_must_be_trusted(
    installer, monkeypatch, fault
):
    selected = XCODE
    tool = XCODE_CHAIN[0]
    infos = {
        path: SimpleNamespace(
            st_mode=(stat.S_IFREG if path == tool else stat.S_IFDIR) | 0o755, st_uid=501
        )
        for path in XCODE_CHAIN
    }
    real = {"realpath": lambda path: path}
    if fault == "outside":
        selected = "/Users/firmstation1/Xcode.app/Contents/Developer"
    elif fault == "relative":
        selected = "Applications/Xcode.app/Contents/Developer"
    elif fault == "nested":
        selected = "/Applications/Tools/Xcode.app/Contents/Developer"
    elif fault == "symlinked":
        real["realpath"] = lambda path: "/Users/firmstation1/Xcode.app/Contents/Developer"
    elif fault == "link":
        infos[tool].st_mode = stat.S_IFLNK | 0o755
    elif fault == "owner":
        infos[tool].st_uid = 502
    elif fault == "writable":
        infos[tool].st_mode = stat.S_IFREG | 0o775
    elif fault == "world-writable":
        infos[tool].st_mode = stat.S_IFREG | 0o757
    elif fault == "parent-owner":
        infos[XCODE + "/usr/bin"].st_uid = 502
    elif fault == "parent-writable":
        infos[XCODE + "/usr/bin"].st_mode = stat.S_IFDIR | 0o777
    elif fault == "parent-link":
        infos[XCODE + "/usr"].st_mode = stat.S_IFLNK | 0o755
    elif fault == "parent-file":
        infos[XCODE + "/usr"].st_mode = stat.S_IFREG | 0o755
    elif fault == "developer-writable":
        infos[XCODE].st_mode = stat.S_IFDIR | 0o775
    elif fault == "app-owner":
        infos[APP].st_uid = 502
    elif fault == "app-writable":
        infos[APP].st_mode = stat.S_IFDIR | 0o775
    elif fault == "app-link":
        infos[APP].st_mode = stat.S_IFLNK | 0o755
    elif fault == "root-owned":
        for info in infos.values():
            info.st_uid = 0
    visited = []

    def lstat(path):
        visited.append(path)
        # Nothing above the application bundle is consulted: /Applications is admin-writable.
        assert path in infos
        if fault == "missing" and path == tool:
            raise FileNotFoundError(path)
        return infos[path]

    monkeypatch.setattr(installer.os, "lstat", lstat)
    monkeypatch.setattr(installer.os.path, "realpath", real["realpath"])
    if fault in (None, "root-owned"):
        assert installer.xcodebuild(selected + "\n") == XCODE + "/usr/bin/xcodebuild"
        assert visited == XCODE_CHAIN
    else:
        with pytest.raises(SystemExit):
            installer.xcodebuild(selected)


def test_sudoers_names_each_platform_and_nothing_else(installer):
    assert installer.SUDOERS == (
        "# Firm station: the station account may install simulator platforms, nothing else.\n"
        f"Defaults!{WRAPPER} env_reset, !requiretty\n"
        f"%firmstations ALL = (root) NOPASSWD: {WRAPPER} iOS, {WRAPPER} watchOS, "
        f"{WRAPPER} tvOS, {WRAPPER} visionOS\n"
    )
    assert "*" not in installer.SUDOERS and "ALL," not in installer.SUDOERS
    assert installer.SUDOERS_FILE == "/etc/sudoers.d/firm-station"
    assert "." not in Path(installer.SUDOERS_FILE).name  # sudo's includedir skips dotted names


@pytest.mark.skipif(not os.path.exists("/usr/sbin/visudo"), reason="visudo is macOS-only here")
def test_sudoers_drop_in_parses(installer, tmp_path):
    draft = tmp_path / "firm-station"
    draft.write_text(installer.SUDOERS)
    draft.chmod(0o440)
    checked = subprocess.run(
        ["/usr/sbin/visudo", "-c", "-f", str(draft)], capture_output=True, text=True
    )
    assert "parsed OK" in checked.stdout + checked.stderr, checked.stdout + checked.stderr


def test_platform_sets_agree(installer, seal):
    assert installer.PLATFORMS == seal.PLATFORMS == mac_image.SIMULATOR_PLATFORMS
    assert str(seal.PLATFORM_INSTALLER) == installer.WRAPPER == WRAPPER
    assert str(seal.STATION_SUDOERS) == installer.SUDOERS_FILE


def test_seal_pins_the_bytes_the_installer_writes(installer, seal):
    assert seal.PLATFORM_INSTALLER_SHA256 == hashlib.sha256(ASSET.read_bytes()).hexdigest()
    assert seal.STATION_SUDOERS_SHA256 == hashlib.sha256(installer.SUDOERS.encode()).hexdigest()
    install = (ASSETS / "mac-station-install.sh").read_text()
    assert f'/usr/bin/install -o root -g wheel -m 755 "$add_platform" {WRAPPER}' in install
    assert "'/usr/sbin/visudo', '-c', '-f', draft" in install
    assert "verify_platform_installer(stations)" in (
        ASSETS / "mac-station-seal.py"
    ).read_text()


@pytest.mark.parametrize("fault", [
    None, "none", "file", "directory", "app", "world", "symlink", "symlinked-app", "other-app",
])
def test_seal_refuses_an_xcode_with_a_group_or_world_writable_entry(
    seal, tmp_path, monkeypatch, fault
):
    applications = tmp_path / "Applications"
    xcode = applications / "Xcode-beta.app"
    tool = xcode / "Contents" / "Developer" / "usr" / "bin" / "xcodebuild"
    tool.parent.mkdir(parents=True)
    tool.write_text("xcodebuild")
    (xcode / "Contents" / "Frameworks").mkdir()
    for path in [tool, *tool.parents]:
        if path == applications:
            break
        path.chmod(0o755)
    other = applications / "Other.app"
    other.mkdir()
    monkeypatch.setattr(seal, "APPLICATIONS", applications)
    if fault == "none":
        shutil.rmtree(xcode)
    elif fault == "file":
        tool.chmod(0o775)
    elif fault == "directory":
        (xcode / "Contents" / "Frameworks").chmod(0o775)
    elif fault == "app":
        xcode.chmod(0o775)
    elif fault == "world":
        tool.chmod(0o757)
    elif fault == "symlink":
        # A symlink's own mode is not a permission; its target is checked where it lives.
        (xcode / "Contents" / "link").symlink_to("Developer")
    elif fault == "symlinked-app":
        (applications / "Xcode.app").symlink_to(tmp_path)
    elif fault == "other-app":
        other.chmod(0o777)
    if fault in (None, "none", "symlink", "symlinked-app", "other-app"):
        seal.verify_xcode_permissions()
    else:
        with pytest.raises(SystemExit, match="group- or world-writable entry"):
            seal.verify_xcode_permissions()
    assert "verify_xcode_permissions()" in (ASSETS / "mac-station-seal.py").read_text()


def test_helper_installation_copies_the_platform_installer(tmp_path, monkeypatch):
    import firm_sandbox.mac_enrollment as enrollment
    from firm_sandbox.mac_station import station_lock

    tmp_path.chmod(0o700)
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    copies = []
    vm = SimpleNamespace(
        root=tmp_path / "mac", firm_locks=tmp_path / "locks", stations=(1,),
        network=SimpleNamespace(ready=lambda: True), _running=lambda: True,
        _shell=lambda *args, **kwargs: None,
        _lima=lambda *args, **kwargs: copies.append(args),
        _rpc=lambda operation, **payload: None,
    )
    vm.root.mkdir(mode=0o700)
    monkeypatch.setattr(enrollment.subprocess, "run", lambda *args, **kwargs: None)
    enrollment.install_helper(vm, app)
    names = [Path(args[1]).name for args in copies if args[0] == "copy"]
    assert "mac-station-add-platform.py" in names and "mac-station-install.sh" in names
    os.close(station_lock(vm.firm_locks, 1))


SUDO_DEFAULTS = f"""Matching Defaults entries for firmstation1 on station:
    env_reset, env_keep+=BLOCKSIZE, env_keep+="COLORFGBG COLORTERM",
    env_keep+=__CF_USER_TEXT_ENCODING

Runas and Command-specific defaults for firmstation1:
    Defaults!{WRAPPER} env_reset, !requiretty

User firmstation1 may run the following commands on station:
"""
# sudo -l without a terminal: wrapped at 80 columns on spaces, continuations indented.
SUDO_LIST = SUDO_DEFAULTS + f"""    (root) NOPASSWD: {WRAPPER} iOS,
        {WRAPPER} watchOS,
        {WRAPPER} tvOS,
        {WRAPPER} visionOS
"""
# The same rule as a terminal wide enough for one line shows it.
SUDO_LIST_UNWRAPPED = SUDO_DEFAULTS + (
    f"    (root) NOPASSWD: {WRAPPER} iOS, {WRAPPER} watchOS, {WRAPPER} tvOS, {WRAPPER} visionOS\n"
)
EXPECTED_COMMANDS = [f"{WRAPPER} {name}" for name in ("iOS", "watchOS", "tvOS", "visionOS")]


def test_sudo_listing_fixture_is_wrapped_as_sudo_wraps_without_a_terminal():
    assert max(len(line) for line in SUDO_LIST.splitlines()) <= 80
    assert max(len(line) for line in SUDO_LIST_UNWRAPPED.splitlines()) > 80


@pytest.mark.parametrize("listing", [SUDO_LIST, SUDO_LIST_UNWRAPPED])
def test_seal_reads_the_one_rule_wrapped_or_not(seal, listing):
    assert seal.sudo_commands(listing) == EXPECTED_COMMANDS


def test_seal_joins_a_rule_wrapped_between_a_command_and_its_argument(seal):
    listing = SUDO_DEFAULTS + (
        f"    (root) NOPASSWD:\n        {WRAPPER}\n        iOS, {WRAPPER} watchOS,\n"
        f"        {WRAPPER} tvOS, {WRAPPER}\n        visionOS\n"
    )
    assert seal.sudo_commands(listing) == EXPECTED_COMMANDS


def test_seal_joins_a_wrapped_command_header(seal):
    listing = SUDO_LIST.replace(
        "on station:\n", "on\n    a-very-long-station-host-name.example.internal:\n"
    )
    assert seal.sudo_commands(listing) == EXPECTED_COMMANDS


@pytest.mark.parametrize("listing", [
    SUDO_LIST + "    (root) NOPASSWD: /bin/sh\n",
    SUDO_LIST + "    (ALL) NOPASSWD: ALL\n",
    SUDO_LIST_UNWRAPPED + "\n    (root) NOPASSWD: /usr/bin/true\n",
    SUDO_LIST.replace("(root) NOPASSWD: ", "(root : wheel) NOPASSWD: "),
    SUDO_LIST.replace("(root) NOPASSWD: ", "(ALL) NOPASSWD: "),
    SUDO_LIST.replace("(root) NOPASSWD: ", "(root) NOPASSWD: SETENV: "),
    SUDO_LIST.replace("(root) NOPASSWD: ", "(root) SETENV: NOPASSWD: "),
    SUDO_LIST.replace("(root) NOPASSWD: ", "(root) "),
    SUDO_LIST.replace(f"{WRAPPER} tvOS", "ALL"),
    "User firmstation1 is not allowed to run sudo on station.\n",
    "",
])
def test_seal_refuses_any_other_sudo_listing(seal, listing):
    commands = seal.sudo_commands(listing)
    assert commands is None or sorted(commands) != sorted(EXPECTED_COMMANDS)


@pytest.mark.parametrize("fault", [
    None, "unwrapped", "missing", "content", "owner", "mode", "sudoers-content", "visudo",
    "extra-rule", "extra-continued-rule", "all", "password", "runas", "duplicate",
    "second-account",
])
def test_seal_verifies_the_installer_and_that_it_is_the_only_sudo(
    installer, seal, tmp_path, monkeypatch, fault
):
    wrapper = tmp_path / "firm-station-add-platform"
    shutil.copyfile(ASSET, wrapper)
    wrapper.chmod(0o755)
    sudoers = tmp_path / "firm-station"
    sudoers.write_text(installer.SUDOERS)
    sudoers.chmod(0o440)
    monkeypatch.setattr(seal, "PLATFORM_INSTALLER", wrapper)
    monkeypatch.setattr(seal, "STATION_SUDOERS", sudoers)
    monkeypatch.setattr(seal, "ROOT_UID", os.getuid())
    listing, visudo, listed = SUDO_LIST.replace(WRAPPER, str(wrapper)), 0, []
    if fault == "missing":
        wrapper.unlink()
    elif fault == "content":
        wrapper.write_text(ASSET.read_text() + "\n# changed\n")
    elif fault == "owner":
        monkeypatch.setattr(seal, "ROOT_UID", os.getuid() + 1)
    elif fault == "mode":
        wrapper.chmod(0o775)
    elif fault == "sudoers-content":
        sudoers.chmod(0o640)
        sudoers.write_text(installer.SUDOERS + "firmstation1 ALL = (root) NOPASSWD: ALL\n")
        sudoers.chmod(0o440)
    elif fault == "visudo":
        visudo = 1
    elif fault == "unwrapped":
        listing = SUDO_LIST_UNWRAPPED.replace(WRAPPER, str(wrapper))
    elif fault == "extra-rule":
        listing += "    (root) NOPASSWD: /bin/sh\n"
    elif fault == "extra-continued-rule":
        listing += "    (root) NOPASSWD: /bin/sh,\n        /usr/bin/true\n"
    elif fault == "all":
        listing = listing.replace(f"{wrapper} visionOS", "ALL")
    elif fault == "password":
        listing = listing.replace("(root) NOPASSWD: ", "(root) ")
    elif fault == "runas":
        listing = listing.replace("(root) NOPASSWD: ", "(root : ALL) NOPASSWD: ")
    elif fault == "duplicate":
        listing = listing.replace(f"{wrapper} tvOS", f"{wrapper} iOS")

    def run(argv, **kwargs):
        if argv[0] == "/usr/sbin/visudo":
            return SimpleNamespace(returncode=visudo, stdout="", stderr="")
        user = argv[-1]
        assert argv == ["/usr/bin/sudo", "-n", "-l", "-U", user]
        listed.append(user)
        output = listing
        if fault == "second-account" and user == "firmstation2":
            output += "    (root) NOPASSWD: /bin/sh\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(seal.subprocess, "run", run)
    if fault in (None, "unwrapped"):
        seal.verify_platform_installer(STATIONS)
        assert listed == ["firmstation1", "firmstation2"]
    else:
        with pytest.raises(SystemExit):
            seal.verify_platform_installer(STATIONS)
        if fault == "second-account":
            assert listed == ["firmstation1", "firmstation2"]
