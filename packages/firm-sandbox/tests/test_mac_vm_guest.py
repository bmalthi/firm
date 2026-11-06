"""The Mac station's guest controller against a simulated station."""

import base64
import errno
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import mac_vm_guest as guest

LSTART = "Mon Sep 15 10:00:00 2026"


def root_owned(fstat):
    """The guest's credential files are root's; a test's own files are not."""
    return lambda fd: SimpleNamespace(
        st_uid=0, st_mode=fstat(fd).st_mode, st_nlink=fstat(fd).st_nlink
    )


class StationGuest:
    """A simulated station: process table, launchd services, agents, crontab."""

    def __init__(self, tmp_path, monkeypatch):
        # Simulated processes and command results live in this process. Exercise
        # the real fork/credential/deadline boundary separately below.
        monkeypatch.setattr(guest, "station_worker", lambda info, work, timeout: (
            work(guest.time.monotonic() + timeout, lambda value: None), False,
        ))
        monkeypatch.setattr(guest, "station_command", lambda info, argv, timeout: {
            "completed": self.run(argv).returncode == 0, "timed_out": False, "returncode": 0,
        })
        self.processes = {}  # pid -> (uid, start, command)
        self.services = {f"gui/{os.getuid()}/com.apple.Finder": 100}
        self.provenance = {}  # pid -> (service target, definition path) launchd reports
        self.programs = {}  # pid -> executable launchd records for an unmanaged process
        self.parents = {}  # pid -> parent pid
        self.unreadable = set()  # pids whose launchctl procinfo fails
        self.crontab = ""
        self.killed = []
        self.commands = []
        self.home = tmp_path / "firmstation1"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.temp = tmp_path / "var-folders" / "station1" / "T"
        self.cache = self.temp.with_name("C")
        self.temp.mkdir(parents=True)
        self.cache.mkdir()
        monkeypatch.setattr(guest.os, "confstr", lambda name: {65537: str(self.temp)}[name])
        self.keys = tmp_path / "lease-keys"
        self.log = tmp_path / "lease" / "commands.log"
        self.log.parent.mkdir()
        monkeypatch.setattr(guest, "STATE", tmp_path / "state")
        monkeypatch.setattr(guest, "station_info", lambda n: self.info)
        monkeypatch.setattr(guest, "LEASE_KEYS", self.keys)
        self.info = local_station_info(self.home)
        self.info["command_log"] = self.log
        monkeypatch.setattr(guest.subprocess, "run", self.run)
        monkeypatch.setattr(guest.os, "kill", self.kill)
        self.now = 0.0
        monkeypatch.setattr(guest.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(guest.time, "sleep", self.advance)
        monkeypatch.setattr(
            guest.pwd,
            "getpwnam",
            lambda name: SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home)
            ),
        )

    def advance(self, seconds):
        self.now += seconds

    def add(self, pid, command, *, uid=None, start=LSTART):
        self.processes[pid] = (os.getuid() if uid is None else uid, start, command)

    def kill(self, pid, signal):
        self.killed.append((pid, signal))
        if pid not in self.processes:
            raise ProcessLookupError
        del self.processes[pid]
        for label, service in list(self.services.items()):
            if service == pid:
                self.services[label] = None

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[:3] == ["/bin/ps", "-o", "ppid="]:
            pid = int(argv[-1])
            if pid not in self.processes:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(
                returncode=0, stdout=f"{self.parents.get(pid, 1)}\n",
            )
        if argv[0] == "/bin/ps":
            lines = [f"{uid} {pid} {start} {command}"
                     for pid, (uid, start, command) in self.processes.items()]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n")
        if argv[:2] == ["/bin/launchctl", "print"]:
            domain = argv[2]
            lines = [domain + " = {", "\tservices = {"] + [
                f"\t\t{pid or 0} - \t{target.removeprefix(domain + '/')}"
                for target, pid in self.services.items() if target.startswith(domain + "/")
            ] + ["\t}", "}"]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n")
        if argv[:2] == ["/bin/launchctl", "procinfo"]:
            pid = int(argv[2])
            if pid in self.unreadable:
                return SimpleNamespace(returncode=1, stdout="")
            if pid not in self.provenance:
                program = self.programs.get(pid, "x")  # None: launchd records no executable
                recorded = "" if program is None else f"program path = {program}\n"
                return SimpleNamespace(
                    returncode=0, stdout=f"{recorded}(pid {pid} is not managed by launchd)\n",
                )
            target, path, *main = self.provenance[pid]
            main = main[0] if main else pid  # the pid launchd names as the service's own
            program = self.processes[pid][2]
            recorded = self.programs.get(pid, program)  # None: launchd records no executable
            recorded = "" if recorded is None else f"program path = {recorded}\n"
            return SimpleNamespace(returncode=0, stdout=(
                f"{recorded}entitlements = {{\n\t\"a\" = true;\n}};\n"
                f"{target} = {{\n\tactive count = 1\n\tpath = {path}\n\ttype = LaunchAgent\n"
                f"\n\tprogram = {program}\n\tdomain = {target.rsplit('/', 1)[0]} [1]\n"
                f"\tpid = {main}\n}}\n"
            ))
        if argv[:2] == ["/bin/launchctl", "bootout"]:
            pid = self.services.pop(argv[2], None)
            self.processes.pop(pid, None)
            return SimpleNamespace(returncode=0, stdout="")
        if argv[0] == "/usr/bin/crontab":
            # As macOS does: -u resolves a user name only, so a uid is an unknown user.
            if argv[1:3] != ["-u", self.info["user"]]:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=f"crontab: user `{argv[2]}' unknown")
            if argv[-1] == "-r":
                self.crontab = ""
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=0 if self.crontab else 1, stdout=self.crontab,
                                   stderr="" if self.crontab else "crontab: no crontab for user")
        if argv[0] == "/usr/bin/xcode-select":
            return SimpleNamespace(
                returncode=0, stdout="/Applications/Xcode.app/Contents/Developer\n"
            )
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def station(tmp_path, monkeypatch):
    sim = StationGuest(tmp_path, monkeypatch)
    trusted = ("/System/", "/Library/Apple/System/", "/Applications/Xcode.app/", "/usr/libexec/")
    monkeypatch.setattr(guest, "trusted_service_path", lambda value, xcode: bool(
        value and value.startswith(trusted)
    ))
    monkeypatch.setattr(
        guest, "trusted_program", lambda value, xcode=None: bool(value)
        and value.startswith(trusted)
    )
    sim.add(100, "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder")
    sim.add(101, "/Applications/Firm Station Helper.app/Contents/MacOS/FirmStationHelper")
    sim.add(900, "/usr/sbin/sshd", uid=0)
    return sim


PROXY = {"host": "192.168.5.2", "port": 23456}


@pytest.fixture(autouse=True)
def proxy_ssh_config(tmp_path, monkeypatch):
    path = tmp_path / "sshd_config.d" / "050-firm-proxy.conf"
    path.parent.mkdir()
    monkeypatch.setattr(guest, "PROXY_SSH_CONFIG", path)
    return path


def test_initialize_binds_the_firm_and_creates_no_directories(station, monkeypatch):
    created = []
    monkeypatch.setattr(
        guest, "managed_directory", lambda path, **options: created.append((str(path), options))
    )
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda name: SimpleNamespace(
        pw_uid=501 if name == "firmvm" else os.getuid(), pw_gid=os.getgid(), pw_dir="/x",
    ))
    monkeypatch.setattr(guest, "boot", lambda: "boot")
    result = guest.dispatch({"operation": "initialize", "firm_id": "demo", "proxy": PROXY})
    assert result == {"firm_id": "demo"}
    assert created == []
    assert guest.dispatch({"operation": "info"})["firm_id"] == "demo"
    with pytest.raises(ValueError, match="another firm"):
        guest.initialize({"firm_id": "other", "proxy": PROXY})
    with pytest.raises(ValueError, match="Unknown operation"):
        guest.dispatch({"operation": "provision", "role_id": "analyst"})


@pytest.mark.parametrize("proxy", [
    None, {"host": "192.168.5.2"}, {"host": "proxy.example", "port": 1},
    {"host": "192.168.5.2", "port": "23456"}, {"host": "192.168.5.2", "port": 0},
    {"host": "192.168.5.2", "port": 1, "user": "x"},
])
def test_initialize_refuses_an_invalid_proxy_before_binding(station, proxy):
    with pytest.raises(ValueError, match="Invalid egress proxy"):
        guest.initialize({"firm_id": "demo", "proxy": proxy})
    assert guest.database() == {}


def test_every_service_proxies_the_web_resolves_nothing_and_ssh_sessions_carry_the_proxy(
    monkeypatch, proxy_ssh_config
):
    commands = []

    def run(argv, **options):
        commands.append(argv)
        if argv[1:] == ["-listallnetworkservices"]:
            return SimpleNamespace(returncode=0, stdout=(
                "An asterisk (*) denotes that a network service is disabled.\n"
                "Ethernet\n*Wi-Fi\n"
            ))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(guest.subprocess, "run", run)
    guest.configure_proxy(PROXY)
    tool = guest.NETWORKSETUP
    for service in ("Ethernet", "Wi-Fi"):
        for expected in (
            [tool, "-setwebproxy", service, "192.168.5.2", "23456"],
            [tool, "-setsecurewebproxy", service, "192.168.5.2", "23456"],
            [tool, "-setwebproxystate", service, "on"],
            [tool, "-setsecurewebproxystate", service, "on"],
            [tool, "-setproxybypassdomains", service, "localhost", "127.0.0.1", "::1"],
            [tool, "-setdnsservers", service, "127.0.0.1"],
        ):
            assert expected in commands
    assert commands[-1] == [guest.SSHD, "-t"]
    text = proxy_ssh_config.read_text()
    assert "SetEnv HTTP_PROXY=http://192.168.5.2:23456 HTTPS_PROXY=http://192.168.5.2:23456" in text
    assert "NO_PROXY=localhost,127.0.0.1,::1" in text
    assert oct(proxy_ssh_config.stat().st_mode & 0o777) == "0o644"
    # An unchanged setting leaves sshd's configuration alone.
    commands.clear()
    guest.configure_proxy(PROXY)
    assert [guest.SSHD, "-t"] not in commands


def test_a_proxy_setting_sshd_refuses_is_taken_back(monkeypatch, proxy_ssh_config):
    monkeypatch.setattr(guest.subprocess, "run", lambda argv, **options: SimpleNamespace(
        returncode=1 if argv[0] == guest.SSHD else 0, stdout="",
    ))
    with pytest.raises(RuntimeError, match="sshd refused"):
        guest.configure_proxy(PROXY)
    assert not proxy_ssh_config.exists()


def test_initialize_works_on_a_fresh_base_without_the_station_account(station, monkeypatch):
    def getpwnam(name):
        if name != "firmvm":
            raise KeyError(f"getpwnam(): name not found: '{name}'")
        return SimpleNamespace(pw_uid=501, pw_gid=20, pw_dir="/Users/firmvm")

    def missing_station(n):
        raise KeyError(f"firmstation{n}")

    monkeypatch.setattr(guest.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(guest, "station_info", missing_station)
    result = guest.dispatch({"operation": "initialize", "firm_id": "demo", "proxy": PROXY})
    assert result["firm_id"] == "demo"
    assert guest.dispatch({"operation": "station-account", "station": 1}) == {"present": False}
    monkeypatch.setattr(guest, "station_info", lambda n: station.info)
    assert guest.dispatch({"operation": "station-account", "station": 1}) == {"present": True}


def test_station_baseline_records_only_the_station_user(station):
    baseline = guest.dispatch({"operation": "station-baseline", "station": 1})
    assert baseline == {
        "processes": [[100, LSTART], [101, LSTART]],
        "services": [f"gui/{os.getuid()}/com.apple.Finder"],
        "launch_agents": [],
        "crontab": "",
    }


def test_station_services_preserve_domains_from_real_macos_output(monkeypatch):
    # Extracts from macOS 26.5.2. The inherited CoreSimulator endpoint appears
    # in both outputs, but the service itself belongs only to user/502. The "(pe)"
    # rows are Apple agents jetsam idle-exited under memory load; the row is still
    # launchd's own, and a running one keeps its pid.
    fixture = Path(__file__).parent / "fixtures"
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        domain = argv[-1].split("/")[0]
        return SimpleNamespace(
            returncode=0, stdout=(fixture / f"launchctl-{domain}-502.txt").read_text(),
        )

    monkeypatch.setattr(guest.subprocess, "run", run)
    assert guest.station_services({"uid": 502}) == {"unparsed": [], "errors": [], "services": {
        "user/502/com.apple.trustd.agent": 408,
        "user/502/com.apple.CoreSimulator.CoreSimulatorService": None,
        "user/502/com.apple.mdworker.shared": None,
        "user/502/com.apple.CoreSimulator.SimLaunchHost-arm64": None,
        "user/502/com.apple.filederivatived": None,
        "user/502/com.apple.ndoagent": 1099,
        "gui/502/com.apple.syncdefaultsd": 474,
        "gui/502/com.apple.DataDetectorsLocalSources": None,
        "gui/502/dev.firm.station-helper": 449,
    }}
    assert calls == [
        ["/bin/launchctl", "print", "user/502"],
        ["/bin/launchctl", "print", "gui/502"],
    ]


@pytest.mark.parametrize("output", [
    "", "user/501 = {\n\tservices = {\n\t}\n}",
    "user/502 = {\n\tendpoints = {\n\t}\n}",
    "user/502 = {\n\tservices = {\n\t\t0 - label\n}",
    "user/502 = {\n\tservices = {\n\t\t0 - label\n\t\t1 - label\n\t}\n}",
])
def test_domain_services_reject_unverified_or_malformed_snapshot(output):
    with pytest.raises(RuntimeError, match="launchd"):
        guest.domain_services("user/502", output)


@pytest.mark.parametrize("status", ["-", "(pe)", "??", "future-status", "123:unknown"])
def test_domain_services_treats_status_as_opaque(status):
    output = f"user/502 = {{\n\tservices = {{\n\t\t42 {status} lease.label\n\t}}\n}}"
    assert guest.domain_services("user/502", output) == {
        "services": {"user/502/lease.label": 42}, "unparsed": [],
    }


def test_station_sweep_removes_user_service_without_touching_same_label_in_gui(station):
    gui_target = f"gui/{os.getuid()}/com.example.same-label"
    user_target = f"user/{os.getuid()}/com.example.same-label"
    station.services[gui_target] = None
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(210, "/bin/sleep")
    station.services[user_target] = 210
    assert guest.station_sweep(baseline, guest.station_info(1))["ok"] is True
    assert ["/bin/launchctl", "bootout", user_target] in station.commands
    assert ["/bin/launchctl", "bootout", gui_target] not in station.commands
    assert gui_target in station.services and user_target not in station.services
    assert 210 not in station.processes


def test_service_details_reads_only_top_level_fields_from_real_spotlight_instance(monkeypatch):
    raw = (Path(__file__).parent / "fixtures/launchctl-spotlight-instance.txt").read_text()
    target = raw.split(" = {", 1)[0]
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=raw,
    ))
    detail = guest.service_details(target)
    assert detail["original"] == "com.apple.managedcorespotlightd"
    assert detail["domain"] == "gui/502 [100002]"
    assert "is copy" in detail["properties"].split(" | ")
    assert detail["path"] == "/System/Library/LaunchAgents/com.apple.managedcorespotlightd.plist"
    assert "type" in detail and "name" not in detail  # Nested coalition names are not metadata.


NOTES = "/System/Library/PrivateFrameworks/NotesShared.framework/Versions/A/XPCServices"


@pytest.mark.parametrize("fault", [
    None, "no-program", "in-baseline", "template-copy", "user-agent", "user-program",
    "forged-domain", "system-domain", "unreadable",
])
def test_trusted_service_registrations_explain_only_apple_definitions(monkeypatch, fault):
    monkeypatch.setattr(guest, "station_trust", lambda info, check, value, xcode, deadline: (
        check(value, xcode)
    ))
    # macOS registered Notes' datastore XPC service into user/502 on demand during a
    # live lease. A root-owned definition on the sealed volume is Apple's; an agent
    # the lease installs in its own home is not.
    target = "user/502/com.apple.Notes.datastore"
    detail = {"domain": "user/502", "path": NOTES + "/com.apple.Notes.datastore.xpc",
              "program": NOTES + "/com.apple.Notes.datastore.xpc/Contents/MacOS/datastore"}
    baseline = []
    if fault == "no-program":
        del detail["program"]
    elif fault == "in-baseline":
        baseline = [target]
    elif fault == "template-copy":
        detail.update(original="com.apple.template", properties="is copy")
    elif fault == "user-agent":
        detail["path"] = "/Users/firmstation1/Library/LaunchAgents/com.apple.Notes.datastore.plist"
    elif fault == "user-program":
        detail["program"] = "/Users/firmstation1/bin/datastore"
    elif fault == "forged-domain":
        detail["domain"] = "gui/502 [100002]"
    elif fault == "system-domain":
        target = "system/com.apple.Notes.datastore"
    elif fault == "unreadable":
        detail = None
    monkeypatch.setattr(guest, "service_details", lambda key, deadline=None: detail)
    monkeypatch.setattr(guest, "trusted_service_path", lambda value, xcode: bool(
        value and value.startswith("/System/")
    ))
    result = guest.trusted_service_registrations(
        {target: None}, baseline, None, info={"uid": 502},
    )
    if fault in (None, "no-program", "template-copy"):
        assert result == [{"target": target, "path": detail["path"],
                           "program": detail.get("program")}]
    else:
        assert result == []


def test_service_details_read_the_real_notes_datastore_registration(monkeypatch):
    # macOS 26.5.2 registers this service on demand, without a conventional plist path.
    raw = (Path(__file__).parent / "fixtures/launchctl-notes-datastore.txt").read_text()
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=raw,
    ))
    detail = guest.service_details("user/502/com.apple.Notes.datastore")
    assert detail["domain"] == "user/502"
    assert detail["path"] == NOTES + "/com.apple.Notes.datastore.xpc"
    assert detail["program"] == (
        NOTES + "/com.apple.Notes.datastore.xpc/Contents/MacOS/com.apple.Notes.datastore"
    )
    assert detail["type"] == "XPCService"


def test_station_report_tolerates_an_apple_registration_but_not_a_lease_agent(
    station, monkeypatch,
):
    domain = f"user/{os.getuid()}/"
    baseline = guest.station_baseline(guest.station_info(1))
    station.services[domain + "com.apple.Notes.datastore"] = None
    station.services[domain + "dev.lease.agent"] = None
    definitions = {
        domain + "com.apple.Notes.datastore": {
            "domain": domain.rstrip("/"), "path": NOTES + "/com.apple.Notes.datastore.xpc",
        },
        domain + "dev.lease.agent": {
            "domain": domain.rstrip("/"),
            "path": str(station.home / "Library/LaunchAgents/dev.lease.agent.plist"),
        },
    }
    monkeypatch.setattr(guest, "service_details", lambda key, deadline=None: definitions.get(key))
    report = guest.station_report(baseline, guest.station_info(1))
    assert report["services"]["extra"] == [domain + "dev.lease.agent"]
    assert [entry["target"] for entry in report["services"]["tolerated"]] == [
        domain + "com.apple.Notes.datastore"
    ]
    assert report["ok"] is False


@pytest.mark.parametrize("fault", [None, "file-owner", "directory-owner", "file-writable",
                                   "directory-writable", "outside-root"])
def test_trusted_service_path_checks_root_and_every_descendant(tmp_path, monkeypatch, fault):
    root = tmp_path / "Xcode.app"
    directory = root / "Contents"
    directory.mkdir(parents=True)
    program = directory / "tool"
    program.write_text("binary")
    directory.chmod(0o755)
    root.chmod(0o755)
    program.chmod(0o755)
    real_stat = Path.stat

    def ownership(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        untrusted = (fault == "file-owner" and path == program
                     or fault == "directory-owner" and path == directory)
        writable = (fault == "file-writable" and path == program
                    or fault == "directory-writable" and path == directory)
        return SimpleNamespace(st_uid=502 if untrusted else 0,
                               st_mode=info.st_mode | (0o020 if writable else 0))

    monkeypatch.setattr(Path, "stat", ownership)
    selected = tmp_path / "other" if fault == "outside-root" else root
    assert guest.trusted_service_path(str(program), str(selected)) is (fault is None)


@pytest.mark.parametrize("owner,under_xcode,trusted", [
    (0, True, True), (501, True, True), (502, True, False), (0, False, True),
    (501, False, False),
])
def test_xcode_copied_as_the_control_account_is_trusted_but_system_roots_stay_root_only(
    tmp_path, monkeypatch, owner, under_xcode, trusted,
):
    # The image build copies Xcode as firmvm (501) and keeps that owner.
    xcode = tmp_path / "Applications" / "Xcode.app"
    system = tmp_path / "System"
    program = (xcode if under_xcode else system) / "Contents" / "tool"
    program.parent.mkdir(parents=True)
    program.write_text("binary")
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *a, **k: SimpleNamespace(
        st_uid=owner, st_mode=real_stat(path, *a, **k).st_mode & ~0o022,
    ))
    monkeypatch.setattr(guest, "SYSTEM_ROOTS", (system,))
    assert guest.XCODE_OWNERS == (0, guest.CONTROL_UID) and guest.CONTROL_UID == 501
    assert guest.trusted_service_path(str(program), str(xcode)) is trusted


def test_station_sweep_preserves_trusted_registration_and_reports_evidence(station, monkeypatch):
    template = f"gui/{os.getuid()}/com.apple.template"
    target = template + ".UUID"
    station.services[template] = None
    baseline = guest.station_baseline(guest.station_info(1))
    station.services[target] = 210
    station.add(210, "/System/Library/template")
    station.provenance[210] = (target, "/System/Library/LaunchAgents/template.plist")
    evidence = {"target": target,
                "path": "/System/Library/LaunchAgents/template.plist",
                "program": "/System/Library/template"}
    monkeypatch.setattr(guest, "service_details", lambda key, deadline=None: {
        "domain": target.rsplit("/", 1)[0], **evidence,
    })
    report = guest.station_sweep(baseline, guest.station_info(1))
    assert report["ok"]
    assert report["services"] == {"extra": [], "tolerated": [evidence],
                                  "ok": True, "verified": True, "unparsed": []}
    assert report["processes"]["tolerated"] == [{
        "pid": 210, "started": LSTART, "command": "/System/Library/template",
        "service": target, "path": "/System/Library/LaunchAgents/template.plist",
    }]
    assert ["/bin/launchctl", "bootout", target] not in station.commands
    assert 210 in station.processes


@pytest.mark.parametrize("baseline_service,trusted,apple_root", [
    (True, True, True), (False, True, True), (True, False, True), (True, True, False),
])
def test_xprotect_requires_baseline_service_and_trusted_apple_system_path(
    station, monkeypatch, baseline_service, trusted, apple_root,
):
    target = f"gui/{os.getuid()}/com.apple.XProtect.agent.scan"
    if baseline_service:
        station.services[target] = None
    baseline = guest.station_baseline(guest.station_info(1))
    prefix = "/Library/Apple/System" if apple_root else "/Library/Other"
    command = prefix + "/Library/CoreServices/XProtect.app/Contents/MacOS/XProtect"
    station.services[target] = 2610
    station.add(2610, command)
    station.provenance[2610] = (target, prefix + "/Library/LaunchAgents/com.apple.XProtect.plist")
    if not trusted:
        monkeypatch.setattr(guest, "trusted_service_path", lambda value, xcode: False)
    report = guest.station_report(baseline, guest.station_info(1))
    assert report["ok"] is (baseline_service and trusted and apple_root)
    selected = "tolerated" if report["ok"] else "extra"
    assert report["processes"][selected][0]["pid"] == 2610


@pytest.mark.parametrize("kind,expected", [
    ("xpc", {
        "target": "pid/597/com.apple.MTLCompilerService.00000000-0000-4000-8000-000000000005",
        "path": "/System/Library/Frameworks/Metal.framework/Versions/A/XPCServices/"
                "MTLCompilerService.xpc",
        "program": "/System/Library/Frameworks/Metal.framework/Versions/A/XPCServices/"
                   "MTLCompilerService.xpc/Contents/MacOS/MTLCompilerService",
        "type": "XPCService", "domain": "pid/597 [mediaanalysisd]", "pid": "2635",
        "program path": "/System/Library/Frameworks/Metal.framework/Versions/A/XPCServices/"
                        "MTLCompilerService.xpc/Contents/MacOS/MTLCompilerService",
    }),
    ("agent", {
        "target": "gui/502/com.apple.dprivacyd",
        "path": "/System/Library/LaunchAgents/com.apple.dprivacyd.plist",
        "program": "/usr/libexec/dprivacyd", "type": "LaunchAgent",
        "domain": "gui/502 [100002]", "pid": "2908",
        "program path": "/usr/libexec/dprivacyd",
    }),
    ("adhoc", None),
])
def test_process_provenance_reads_real_procinfo_output(monkeypatch, kind, expected):
    # macOS 26.5.2: an on-demand XPC service, an Apple LaunchAgent whose program
    # lives outside /System, and a detached `sleep` started by the station account.
    raw = (Path(__file__).parent / f"fixtures/launchctl-procinfo-{kind}.txt").read_text()
    pid = {"xpc": 2635, "agent": 2908, "adhoc": 3409}[kind]
    calls = []
    monkeypatch.setattr(guest.subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv), SimpleNamespace(returncode=0, stdout=raw))[1])
    detail = guest.process_provenance(pid)
    assert calls == [["/bin/launchctl", "procinfo", str(pid)]]
    if expected is None:
        assert detail is None
    else:
        assert {key: detail[key] for key in expected} == expected
        assert "keepalive" not in detail  # Nested event descriptors are not metadata.


def test_process_provenance_needs_launchd_to_answer(monkeypatch):
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="gui/502/com.apple.x = {\n\tpath = /System/x.plist\n}\n",
    ))
    assert guest.process_provenance(7) is None
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="program path = /bin/sleep\nentitlements = {\n}\n",
    ))
    assert guest.process_provenance(7) is None


@pytest.mark.parametrize("fault", [
    None, "xpc", "loginwindow", "ad-hoc", "unexplained-service", "login-domain", "user-path",
    "other-pid", "child", "writable-program", "argv-is-a-bare-name", "no-recorded-program",
])
def test_explained_process_requires_launchd_and_trusted_definition(monkeypatch, fault):
    monkeypatch.setattr(guest, "station_trust", lambda info, check, value, xcode, deadline: (
        check(value, xcode)
    ))
    monkeypatch.setattr(guest, "trusted_service_path", lambda value, xcode: bool(
        value and value.startswith("/System/")
    ))
    monkeypatch.setattr(guest, "trusted_program", lambda value, xcode=None: (
        value.startswith("/") and value != "/Users/firmstation1/tool"
    ))
    pid, command = 2908, "/usr/libexec/dprivacyd"
    provenance = {"target": "gui/502/com.apple.dprivacyd",
                  "path": "/System/Library/LaunchAgents/com.apple.dprivacyd.plist",
                  "program": command, "program path": command, "pid": "2908"}
    explained = {"gui/502/com.apple.dprivacyd"}
    if fault == "xpc":
        provenance["target"] = "pid/597/com.apple.MTLCompilerService.UUID"
    elif fault == "loginwindow":
        provenance["target"] = "system/com.apple.loginwindow.UUID"
    elif fault == "ad-hoc":
        provenance = None
    elif fault == "unexplained-service":
        explained = set()
    elif fault == "login-domain":
        provenance["target"] = "login/100002/com.apple.dprivacyd"
    elif fault == "user-path":
        provenance["path"] = "/Users/firmstation1/Library/LaunchAgents/com.apple.dprivacyd.plist"
    elif fault == "other-pid":
        provenance["pid"] = "2909"
    elif fault == "child":
        pid = 2909  # launchd names the service's own process, never a child of it.
    elif fault == "writable-program":
        command = provenance["program"] = provenance["program path"] = "/Users/firmstation1/tool"
    elif fault == "argv-is-a-bare-name":
        # cloudphotod's argv[0] carries no path; launchd's own record decides.
        command = "cloudphotod"
    elif fault == "no-recorded-program":
        command = "cloudphotod"
        del provenance["program path"]
    result = guest.explained_process(pid, provenance, command, explained, None, info={"uid": 502})
    assert result is (fault in (None, "xpc", "loginwindow", "argv-is-a-bare-name"))


def test_trusted_service_path_prefers_the_sealed_cryptex_over_its_writable_mount(
    tmp_path, monkeypatch,
):
    # /System/Volumes/Preboot/Cryptexes is world-writable on macOS 26; the App
    # cryptex mounted inside it is sealed and root-owned, so it is its own root.
    cryptexes = tmp_path / "System/Volumes/Preboot/Cryptexes"
    program = cryptexes / "App/System/Applications/Safari.app/Contents/MacOS/Safari"
    program.parent.mkdir(parents=True)
    program.write_text("binary")
    for path in [program, *program.parents]:
        if path == tmp_path.parent:
            break
        path.chmod(0o777 if path == cryptexes else 0o755)
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *a, **k: SimpleNamespace(
        st_uid=0, st_mode=real_stat(path, *a, **k).st_mode,
    ))
    monkeypatch.setattr(guest, "SYSTEM_ROOTS", (tmp_path / "System",))
    assert guest.trusted_service_path(str(program), None) is False
    monkeypatch.setattr(guest, "SYSTEM_ROOTS", (cryptexes / "App", tmp_path / "System"))
    assert guest.trusted_service_path(str(program), None) is True


def test_trusted_program_requires_a_root_owned_unwritable_file(tmp_path, monkeypatch):
    program = tmp_path / "tool"
    program.write_text("binary")
    program.chmod(0o755)
    real_stat = Path.stat
    owner = {"uid": 0, "mode": 0}
    monkeypatch.setattr(Path, "stat", lambda path, *a, **k: SimpleNamespace(
        st_uid=owner["uid"], st_mode=real_stat(path, *a, **k).st_mode | owner["mode"],
    ))
    assert guest.trusted_program(str(program)) is True
    owner["uid"] = 502
    assert guest.trusted_program(str(program)) is False
    owner.update(uid=0, mode=0o002)
    assert guest.trusted_program(str(program)) is False
    assert guest.trusted_program(str(tmp_path)) is False
    assert guest.trusted_program(str(tmp_path / "missing")) is False
    assert guest.trusted_program("relative") is False and guest.trusted_program(None) is False
    # The control account's file is trusted only inside the selected Xcode it copied.
    owner.update(uid=501, mode=0)
    (tmp_path / "other").mkdir()
    assert guest.trusted_program(str(program)) is False
    assert guest.trusted_program(str(program), str(tmp_path / "other")) is False
    assert guest.trusted_program(str(program), str(tmp_path)) is True
    owner.update(uid=502)
    assert guest.trusted_program(str(program), str(tmp_path)) is False


@pytest.mark.parametrize("change", ["replace", "rewrite", "mode", "parent-mode", "remove"])
def test_station_trust_rechecks_changed_files(tmp_path, monkeypatch, change):
    program = tmp_path / "program"
    program.write_text("trusted")
    info = {"trust_cache": {}}
    calls = []

    def worker(info, work, timeout):
        calls.append(True)
        return work(None, None), False

    def check(value, xcode):
        path = Path(value)
        return (path.exists() and path.read_text() == "trusted"
                and not path.stat().st_mode & 0o022
                and not path.parent.stat().st_mode & 0o022)

    monkeypatch.setattr(guest, "station_worker", worker)
    program.chmod(0o644)
    assert guest.station_trust(info, check, str(program), None, None) is True
    assert guest.station_trust(info, check, str(program), None, None) is True
    assert len(calls) == 1  # A hit skips the worker, not just its check.
    before = program.stat()
    if change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_text("changed")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(program)
    elif change == "rewrite":
        program.write_text("changed")  # Same inode, size and mtime; ctime still changes.
        os.utime(program, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "mode":
        program.chmod(0o666)
    elif change == "parent-mode":
        tmp_path.chmod(0o777)
    else:
        program.unlink()
    assert guest.station_trust(info, check, str(program), None, None) is False
    assert len(calls) == 2


def test_station_trust_does_not_cache_failed_or_changing_checks(tmp_path, monkeypatch):
    program = tmp_path / "program"
    program.write_text("initial")
    info = {"trust_cache": {}}
    monkeypatch.setattr(guest, "station_worker", lambda info, work, timeout: (
        work(None, None), False,
    ))

    def changing(value, xcode):
        program.write_text("changed during check")
        return True

    assert guest.station_trust(info, changing, str(program), None, None) is True
    assert info["trust_cache"] == {}
    assert guest.station_trust(info, lambda *args: False, str(program), None, None) is False
    assert info["trust_cache"] == {}
    monkeypatch.setattr(guest, "station_worker", lambda *args: ((True, []), True))
    with pytest.raises(TimeoutError, match="provenance check"):
        guest.station_trust(info, changing, str(program), None, None)
    assert info["trust_cache"] == {}


def test_sweep_caches_trust_across_processes_and_passes_but_not_reports(
    station, tmp_path, monkeypatch,
):
    baseline = guest.station_baseline(station.info)
    program, definition = tmp_path / "program", tmp_path / "service.plist"
    program.touch()
    definition.touch()
    calls = []

    def trusted(value, xcode):
        calls.append(value)
        return True

    monkeypatch.setattr(guest, "trusted_program", trusted)
    monkeypatch.setattr(guest, "trusted_service_path", trusted)
    monkeypatch.setattr(guest, "installed_xcode", lambda deadline: None)
    for pid in (700, 701):
        station.add(pid, str(program))
        station.provenance[pid] = (f"gui/{os.getuid()}/com.apple.Finder", str(definition))
    station.add(702, "/bin/sleep")  # Forces cleanup and another inventory.
    assert guest.station_sweep(baseline, station.info, simulators=False)["ok"] is True
    assert calls == [str(definition), str(program)]
    assert [pid for pid, sig in station.killed] == [702]
    assert "trust_cache" not in station.info
    # Every process's launchd provenance is still read on every inventory.
    assert station.commands.count(["/bin/launchctl", "procinfo", "700"]) == 2
    assert guest.station_report(baseline, station.info)["ok"] is True
    assert calls == [str(definition), str(program)] * 2
    assert guest.station_sweep(baseline, station.info, simulators=False)["ok"] is True
    assert calls == [str(definition), str(program)] * 3


def test_station_sweep_records_kill_failure_and_continues_with_other_processes(
    station, monkeypatch,
):
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(301, "/Library/unexplained-protected-program")
    station.add(302, "/bin/sleep")

    def kill(pid, signal):
        if pid == 301:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        station.kill(pid, signal)

    monkeypatch.setattr(guest.os, "kill", kill)
    monkeypatch.setattr(guest, "SWEEP_SECONDS", 2)
    report = guest.station_sweep(baseline, guest.station_info(1))
    assert report["ok"] is False and report["processes"]["ok"] is False
    assert 301 in station.processes and 302 not in station.processes
    assert report["processes"]["extra"] == [{
        "pid": 301, "started": LSTART, "command": "/Library/unexplained-protected-program",
    }]
    assert report["processes"]["kill_errors"] == [{
        "pid": 301, "started": LSTART, "command": "/Library/unexplained-protected-program",
        "errno": errno.EPERM, "error": "[Errno 1] Operation not permitted",
    }]


XPROTECT_APP = "/Library/Apple/System/Library/CoreServices/XProtect.app"


@pytest.mark.parametrize("case", [
    "tolerated", "lease-shell-parent", "orphaned", "other-station-plugin",
    "user-domain-plugin", "parent-not-the-services-own", "untrusted-definition",
    "untrusted-parent-program", "missing-parent-program", "parent-lookup-fails",
    "outside-the-bundle", "wrong-directory", "not-a-remediator", "named-only-by-ps",
    "unrecorded-program", "child-lookup-fails", "untrusted-child-program", "another-account",
])
def test_an_xprotect_remediator_is_tolerated_only_as_the_plugin_services_child(
    station, monkeypatch, case,
):
    # On macOS 27, remediators are not managed by launchd; their parent is
    # the station's own XProtectPluginService, and SIP refuses them signals.
    uid = os.getuid()
    plugin = f"gui/{uid}/com.apple.XprotectFramework.PluginService"
    plist = XPROTECT_APP + "/Contents/Resources/com.apple.XprotectFramework.PluginService.plist"
    plugin_program = (XPROTECT_APP + "/Contents/XPCServices/XProtectPluginService.xpc/"
                      "Contents/MacOS/XProtectPluginService")
    if case == "untrusted-parent-program":
        plugin_program = "/Users/firmstation1/XProtectPluginService"
    station.services[plugin] = 1166
    station.add(1166, plugin_program)
    station.provenance[1166] = (plugin, plist)
    baseline = guest.station_baseline(guest.station_info(1))
    remediator = XPROTECT_APP + "/Contents/MacOS/XProtectRemediatorMRTv3"
    born = LSTART  # Parent and child can be born within the same displayed second.
    station.add(3275, remediator, uid=599 if case == "another-account" else None, start=born)
    station.programs[3275] = remediator
    station.parents[3275] = 1166
    if case == "lease-shell-parent":
        station.add(700, "/bin/zsh")
        station.parents[3275] = 700
    elif case == "orphaned":
        station.parents[3275] = 1
    elif case == "other-station-plugin":
        station.provenance[1166] = ("gui/599/com.apple.XprotectFramework.PluginService", plist)
    elif case == "user-domain-plugin":
        station.provenance[1166] = (f"user/{uid}/com.apple.XprotectFramework.PluginService",
                                    plist)
    elif case == "parent-not-the-services-own":
        station.provenance[1166] = (plugin, plist, 1170)
    elif case == "untrusted-definition":
        station.provenance[1166] = (plugin, "/Users/firmstation1/Library/LaunchAgents/x.plist")
    elif case == "outside-the-bundle":
        station.programs[3275] = "/Users/firmstation1/XProtectRemediatorMRTv3"
    elif case == "named-only-by-ps":
        station.programs[3275] = "/bin/sleep"
    elif case == "wrong-directory":  # trusted, but not where remediators live
        station.programs[3275] = XPROTECT_APP + "/Contents/Resources/XProtectRemediatorMRTv3"
    elif case == "not-a-remediator":  # trusted and in place, but not a remediator
        station.programs[3275] = XPROTECT_APP + "/Contents/MacOS/XProtect"
    elif case == "unrecorded-program":
        station.programs[3275] = None
    elif case == "missing-parent-program":
        station.programs[1166] = None
    elif case == "child-lookup-fails":
        station.unreadable.add(3275)
    elif case == "parent-lookup-fails":
        station.unreadable.add(1166)
    elif case == "untrusted-child-program":
        trusted = guest.trusted_program
        monkeypatch.setattr(guest, "trusted_program", lambda value, xcode=None: (
            value != remediator and trusted(value, xcode)
        ))
    report = guest.station_report(baseline, guest.station_info(1))
    tolerated = [entry["pid"] for entry in report["processes"]["tolerated"]]
    extra = [(entry["pid"], entry["started"]) for entry in report["processes"]["extra"]]
    if case == "tolerated":
        assert 3275 in tolerated and report["processes"]["ok"] is True
        entry = next(e for e in report["processes"]["tolerated"] if e["pid"] == 3275)
        assert entry["service"] == plugin and entry["parent"] == 1166
        # A sweep leaves it running rather than signalling it.
        sweep = guest.station_sweep(baseline, guest.station_info(1))
        assert sweep["ok"] is True and 3275 in station.processes
    elif case == "another-account":
        # Another account's process is never in this station's inventory at all.
        assert 3275 not in tolerated and not any(pid == 3275 for pid, _ in extra)
    else:
        assert (3275, born) in extra and 3275 not in tolerated
        assert report["processes"]["ok"] is False


def test_an_unkillable_process_that_exits_on_its_own_leaves_the_release_clean(
    station, monkeypatch,
):
    baseline = guest.station_baseline(station.info)
    station.add(304, "/bin/sleep")

    def kill(pid, signal):
        if pid == 304:
            del station.processes[304]
            raise PermissionError(errno.EPERM, "Operation not permitted")
        station.kill(pid, signal)

    monkeypatch.setattr(guest.os, "kill", kill)
    report = guest.station_sweep(baseline, station.info)
    assert report["ok"] is True and report["processes"]["extra"] == []
    assert [error["pid"] for error in report["processes"]["kill_errors"]] == [304]


@pytest.mark.parametrize("part", guest.WORK_PARTS)
def test_transient_read_failure_is_history_after_a_complete_clean_read(station, monkeypatch, part):
    baseline = guest.station_baseline(station.info)
    work_part = guest.work_part
    failed = False

    def failing_once(name, *args):
        nonlocal failed
        if name == part and not failed:
            failed = True
            raise RuntimeError("temporary read failure")
        return work_part(name, *args)

    monkeypatch.setattr(guest, "work_part", failing_once)
    report = guest.station_sweep(baseline, station.info)
    assert failed and report["ok"] is True
    assert report[part]["verified"] is True
    assert "error" not in report[part]
    assert {"error": "temporary read failure"} in report[part]["diagnostics"]


def test_a_failed_lookup_keeps_the_verdicts_already_reached(station, monkeypatch):
    # launchd answered for 2000 before 5000's lookup failed: the fallback stops 5000 but
    # never signals a process launchd has already accounted for.
    target = f"gui/{os.getuid()}/com.apple.example"
    station.services[target] = 2000
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(2000, "/System/Library/CoreServices/example")
    station.provenance[2000] = (target, "/System/Library/LaunchAgents/com.apple.example.plist")
    station.add(5000, "/bin/sleep")
    provenance = guest.process_provenance

    def failing(pid, deadline=None):
        if pid == 5000:
            raise TimeoutError("procinfo timed out")
        return provenance(pid, deadline)

    monkeypatch.setattr(guest, "process_provenance", failing)
    report = guest.station_sweep(baseline, guest.station_info(1))
    killed = [pid for pid, _ in station.killed]
    assert 5000 in killed and 5000 not in station.processes
    assert 2000 not in killed and 2000 in station.processes
    assert report["ok"] is True and report["processes"]["verified"] is True
    assert any("procinfo timed out" in entry.get("error", "")
               for entry in report["processes"]["diagnostics"])


def test_a_short_sweep_keeps_a_cleanup_opportunity_for_every_part(station, monkeypatch):
    # The host's retry hands the guest what is left of the release's allowance. An
    # unreadable part takes its share of that, never the share of the parts after it,
    # so ordinary killable work is still stopped rather than left for the owner.
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(302, "/bin/sleep")

    def unreadable(info, deadline=None):
        raise TimeoutError("launchctl print timed out")

    monkeypatch.setattr(guest, "station_services", unreadable)
    report = guest.station_sweep(baseline, guest.station_info(1), 8)
    assert 302 not in station.processes and report["processes"]["ok"] is True
    assert report["ok"] is False and report["services"]["verified"] is False


def test_station_sweep_reports_an_agent_it_cannot_remove_and_still_stops_work(
    station, monkeypatch,
):
    baseline = guest.station_baseline(guest.station_info(1))
    agents = station.home / "Library" / "LaunchAgents"
    (agents / "dev.example.stuck.plist").write_text("x")
    station.add(302, "/bin/sleep")
    real_unlink = os.unlink

    def unlink(name, *args, **kwargs):
        if name == "dev.example.stuck.plist":
            raise PermissionError(errno.EPERM, "Operation not permitted", name)
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(guest.os, "unlink", unlink)
    monkeypatch.setattr(guest, "SWEEP_SECONDS", 2)
    report = guest.station_sweep(baseline, guest.station_info(1))
    assert report["ok"] is False and report["launch_agents"]["ok"] is False
    assert 302 not in station.processes
    assert {k: v for k, v in report["launch_agents"].items()
            if k not in {"ok", "verified", "error", "cleanup_error", "diagnostics"}} == {
        "extra": ["dev.example.stuck.plist"],
        "remove_errors": {"dev.example.stuck.plist": {
            "errno": errno.EPERM,
            "error": "[Errno 1] Operation not permitted: 'dev.example.stuck.plist'",
        }},
    }


def test_station_sweep_stops_everything_the_baseline_does_not_explain(station):
    # A baseline service that respawns its process is tolerated; a new service,
    # agent, crontab or process is not, whatever program it runs.
    domain = f"gui/{os.getuid()}/"
    station.services[domain + "com.apple.xpc"] = None
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(200, "/usr/bin/xcodebuild")
    station.add(201, "/bin/sleep")
    station.add(202, "/Applications/Xcode.app/Contents/Developer/usr/bin/simd")
    station.add(203, "/System/Library/CoreServices/launchservicesd")
    station.add(204, "/System/Library/Frameworks/JavaScriptCore.framework/Helpers/jsc")
    station.services[domain + "com.example.leftover"] = 201
    station.services[domain + "com.apple.xpc"] = 202
    station.services[domain + "com.example.system"] = 203
    station.provenance[202] = (
        domain + "com.apple.xpc", "/Applications/Xcode.app/Contents/Library/LaunchAgents/xpc.plist",
    )
    station.provenance[203] = (
        domain + "com.example.system", "/System/Library/LaunchAgents/s.plist",
    )
    (station.home / "Library" / "LaunchAgents" / "com.example.leftover.plist").write_text("x")
    station.crontab = "* * * * * /bin/echo\n"
    report = guest.dispatch({"operation": "station-sweep", "station": 1, "baseline": baseline})
    assert report["ok"] is True
    assert report["processes"] == {
        "ok": True, "verified": True,
        "extra": [],
        "tolerated": [{"pid": 202, "started": LSTART,
                       "command": "/Applications/Xcode.app/Contents/Developer/usr/bin/simd",
                       "service": domain + "com.apple.xpc",
                       "path": "/Applications/Xcode.app/Contents/Library/LaunchAgents/xpc.plist"}],
    }
    assert report["services"]["extra"] == [] and report["launch_agents"]["extra"] == []
    assert report["crontab"]["empty"] is True
    commands = station.commands
    simctl = next(
        i for i, argv in enumerate(commands) if argv[-3:] == ["simctl", "shutdown", "all"]
    )
    assert all(argv[:2] != ["/bin/launchctl", "bootout"] for argv in commands[:simctl])
    assert ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/com.example.leftover"] in commands
    assert (200, 9) in station.killed and (201, 9) not in station.killed
    assert (204, 9) in station.killed  # A System program run ad hoc is lease work.
    assert 203 not in station.processes and domain + "com.example.system" not in station.services
    assert not (station.home / "Library" / "LaunchAgents" / "com.example.leftover.plist").exists()
    assert 202 in station.processes and 100 in station.processes


def test_station_sweep_stops_and_removes_a_homebrew_service_the_lease_started(station):
    # `brew services start redis` writes ~/Library/LaunchAgents/homebrew.mxcl.redis.plist
    # and bootstraps it into gui/<uid>; its program lives in the station-owned
    # Homebrew prefix. None of that is Apple's: it is lease work, never tolerated.
    domain = f"gui/{os.getuid()}/"
    baseline = guest.station_baseline(guest.station_info(1))
    agents = station.home / "Library" / "LaunchAgents"
    (agents / "homebrew.mxcl.redis.plist").write_text("<plist/>")
    station.add(400, "/opt/homebrew/opt/redis/bin/redis-server")
    station.services[domain + "homebrew.mxcl.redis"] = 400
    station.provenance[400] = (
        domain + "homebrew.mxcl.redis", str(agents / "homebrew.mxcl.redis.plist"),
    )
    before = guest.station_report(baseline, guest.station_info(1))
    assert before["ok"] is False
    assert before["services"]["extra"] == [domain + "homebrew.mxcl.redis"]
    assert before["launch_agents"]["extra"] == ["homebrew.mxcl.redis.plist"]
    assert [entry["pid"] for entry in before["processes"]["extra"]] == [400]
    assert before["processes"]["tolerated"] == [] and before["services"]["tolerated"] == []

    report = guest.dispatch({"operation": "station-sweep", "station": 1, "baseline": baseline})
    assert report["ok"] is True
    assert ["/bin/launchctl", "bootout", domain + "homebrew.mxcl.redis"] in station.commands
    assert not (agents / "homebrew.mxcl.redis.plist").exists()
    assert 400 not in station.processes
    # The real trust rules never explain a program or definition under Homebrew.
    assert all(not Path("/opt/homebrew").is_relative_to(root) for root in guest.SYSTEM_ROOTS)


def test_a_platform_installed_through_sudo_is_not_lease_work(station):
    # sudo firm-station-add-platform runs xcodebuild as root; the runtime mounts
    # through root's simdiskimaged. Neither is a station process, and the runtime
    # left installed on the system is not something release removes.
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(500, "/usr/bin/sudo", uid=0)
    station.add(501, "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild", uid=0)
    station.add(502, "/System/Library/PrivateFrameworks/CoreSimulator.framework/Resources/bin/"
                "simdiskimaged", uid=0)
    report = guest.dispatch({"operation": "station-sweep", "station": 1, "baseline": baseline})
    assert report["ok"] is True and report["processes"]["extra"] == []
    assert not any(pid in (500, 501, 502) for pid, _ in station.killed)


def test_station_sweep_leaves_the_home_alone_apart_from_agents_and_crontab(station):
    baseline = guest.station_baseline(guest.station_info(1))
    kept = [
        station.home / "Library" / "Developer" / "Xcode" / "DerivedData" / "App" / "info.plist",
        station.home / "Library" / "Preferences" / "com.example.app.plist",
        station.home / "Library" / "Caches" / "com.example.app" / "cache.db",
        station.home / "project" / "notes.txt",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kept")
    agents = station.home / "Library" / "LaunchAgents"
    (agents / "com.example.leftover.plist").write_text("x")
    station.crontab = "* * * * * /bin/echo\n"
    report = guest.dispatch({"operation": "station-sweep", "station": 1, "baseline": baseline})
    assert report["ok"] is True
    assert all(path.read_text() == "kept" for path in kept)
    assert os.listdir(agents) == [] and station.crontab == ""


def test_station_sweep_reports_what_survives_and_verify_terminates_nothing(station):
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(300, "/bin/sleep")
    real_kill = station.kill
    station.kill = lambda pid, signal: real_kill(pid, signal) if pid != 300 else None
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(guest.os, "kill", station.kill)
    monkeypatch.setattr(guest, "SWEEP_SECONDS", 2)
    try:
        report = guest.station_sweep(baseline, guest.station_info(1))
    finally:
        monkeypatch.undo()
    assert report["ok"] is False and report["processes"]["ok"] is False
    assert report["processes"]["extra"] == [
        {"pid": 300, "started": LSTART, "command": "/bin/sleep"},
    ]
    station.killed.clear()
    station.add(301, "/usr/sbin/sshd")
    verify = guest.dispatch({"operation": "station-verify", "station": 1, "baseline": baseline})
    assert verify["ok"] is False and verify["sessions"] == [301]
    assert verify["key_present"] is False and verify["drive_mounted"] is False
    assert guest.dispatch({"operation": "station-status", "station": 1}) == {
        "key_present": False, "sessions": [301], "drive_mounted": False,
    }
    assert station.killed == []


def test_station_sessions_are_killed_until_none_remain(station):
    station.add(400, "/usr/sbin/sshd-session")
    station.add(401, "/bin/zsh")
    assert guest.dispatch({"operation": "station-kill-sessions", "station": 1}) == {"remaining": []}
    assert station.killed == [(400, 9)]
    assert 401 in station.processes


def test_session_kill_reports_missing_worker_acknowledgement(station, monkeypatch):
    station.add(400, "/usr/sbin/sshd-session")
    worker = guest.station_worker

    def lost_acknowledgement(info, work, timeout):
        worker(info, work, timeout)
        return None, True

    monkeypatch.setattr(guest, "station_worker", lost_acknowledgement)
    report = guest.station_kill_sessions(station.info)
    assert 400 not in station.processes
    assert report["cleanup_incomplete"] is True


def test_station_key_is_installed_root_readable_and_removed(station):
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial firm-mac-lease"
    for bad in ("ssh-rsa AAAA", key + "\ncommand", "x" * 2000, 5):
        with pytest.raises(ValueError):
            guest.station_key_install(bad, guest.station_info(1))
    result = guest.dispatch({"operation": "station-key-install", "station": 1, "public_key": key})
    path = station.keys / "firmstation1"
    assert result == {"installed": True, "path": str(path)}
    assert path.read_text() == key + "\n"
    assert path.stat().st_mode & 0o777 == 0o644
    assert station.keys.stat().st_mode & 0o777 == 0o755
    assert guest.dispatch({
        "operation": "station-key-install", "station": 1, "public_key": key,
    })["installed"]
    assert guest.dispatch({"operation": "station-key-remove", "station": 1}) == {"present": False}
    assert not path.exists()
    assert guest.dispatch({"operation": "station-key-remove", "station": 1}) == {"present": False}


@pytest.mark.parametrize(
    "drive",
    [
        "not a mapping",
        {"user": "a", "password": "b", "port": 445},
        {"user": "a/b", "password": "b", "port": 445, "share": "firm"},
        {"user": "a", "password": "b\n", "port": 445, "share": "firm"},
        {"user": "a", "password": "b", "port": "445", "share": "firm"},
        {"user": "a", "password": "b", "port": 70000, "share": "firm"},
    ],
)
def test_drive_credentials_are_validated_before_mounting(drive):
    with pytest.raises(ValueError, match="drive credentials"):
        guest.validate_drive({"drive": drive})
    assert guest.validate_drive({}) is None


def test_mount_runs_as_the_station_with_the_password_off_argv(station, monkeypatch):
    info = guest.station_info(1)
    seen = []

    def prompted(argv, passwords, enter, timeout=120):
        seen.append((argv, passwords, enter))
        return 0, "Password for 127.0.0.1: "

    monkeypatch.setattr(guest, "run_with_passwords", prompted)
    monkeypatch.setattr(guest, "unmount_drive", lambda _info: None)
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: bool(seen))
    guest.mount_drive(info, {"user": "fa", "password": "pw-secret", "port": 4450, "share": "firm"})
    ((argv, passwords, enter),) = seen
    assert argv == ["/sbin/mount_smbfs", "//fa@127.0.0.1:4450/firm", str(station.home / "drive")]
    assert passwords == ["pw-secret"] and "pw-secret" not in " ".join(argv)
    assert callable(enter)
    monkeypatch.setattr(guest, "run_with_passwords", lambda *a, **k: (64, "pw-secret bad\n"))
    with pytest.raises(guest.DriveMountError, match=r"\(64\): \*\*\* bad"):
        guest.mount_drive(info, {"user": "fa", "password": "pw-secret", "port": 4450,
                                 "share": "firm"})


def test_mount_that_timed_out_connecting_is_tried_once_more(station, monkeypatch):
    """The tunnel into the Mac stalls for tens of seconds at a time; a wrong password does not."""
    info = guest.station_info(1)
    drive = {"user": "fa", "password": "pw-secret", "port": 4450, "share": "firm"}
    calls = []

    def stalled(argv, passwords, enter, timeout=120):
        calls.append(argv)
        if len(calls) == 1:
            return 68, "mount_smbfs: server connection failed: Operation timed out\n"
        return 0, ""

    monkeypatch.setattr(guest, "run_with_passwords", stalled)
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: len(calls) == 2)
    guest.mount_drive(info, drive)
    assert len(calls) == 2
    calls.clear()
    monkeypatch.setattr(guest, "run_with_passwords",
                        lambda *a, **k: (68, "server connection failed: Operation timed out\n"))
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: False)
    with pytest.raises(guest.DriveMountError, match="timed out"):
        guest.mount_drive(info, drive)
    calls.clear()
    monkeypatch.setattr(
        guest, "run_with_passwords", lambda *a, **k: (calls.append(a), 77, "bad")[1:]
    )
    with pytest.raises(guest.DriveMountError, match=r"\(77\)"):
        guest.mount_drive(info, drive)
    assert len(calls) == 1


def test_password_prompts_are_answered_in_order_on_a_pty_and_never_echoed():
    import sys

    code, output = guest.run_with_passwords(
        [sys.executable, "-c",
         "import getpass, sys; sys.exit(0 if getpass.getpass('New password:') == 'open'"
         " and getpass.getpass('Enter password for firmvm :') == 'sesame' else 3)"],
        ["open", "sesame"], lambda: None, timeout=20,
    )
    assert code == 0 and "open" not in output and "sesame" not in output
    code, _ = guest.run_with_passwords(
        [sys.executable, "-c",
         "import getpass; getpass.getpass('Password: '); raise SystemExit(5)"],
        ["wrong"], lambda: None, timeout=20,
    )
    assert code == 5


@pytest.fixture
def control_rotation(tmp_path, monkeypatch):
    password = tmp_path / "control-password"
    password.write_text("old-secret\n")
    password.chmod(0o600)
    kcpassword = tmp_path / "kcpassword"
    kcpassword.write_bytes(b"stale")
    kcpassword.chmod(0o600)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(guest, "STATE", state)
    monkeypatch.setattr(guest, "CONTROL_PASSWORD", password)
    monkeypatch.setattr(guest, "KCPASSWORD", kcpassword)
    keychain = tmp_path / "login.keychain-db"
    keychain.write_bytes(b"")
    monkeypatch.setattr(guest, "CONTROL_KEYCHAIN", keychain)
    original_fstat = os.fstat
    control_inode = password.stat().st_ino
    monkeypatch.setattr(guest.os, "fstat", lambda fd: SimpleNamespace(
        st_uid=501 if original_fstat(fd).st_ino == control_inode else 0,
        st_mode=original_fstat(fd).st_mode, st_nlink=original_fstat(fd).st_nlink,
    ))
    record = SimpleNamespace(password=password, kcpassword=kcpassword, keychain=keychain,
                             journal=state / "control-password-rotation.json",
                             actual="old-secret", change=True, unlock=True, prompts=[], commands=[],
                             automatic_login="firmvm")

    def prompted(argv, secrets, enter, timeout):
        record.prompts.append((argv, secrets))
        if "-authonly" in argv:
            return (0 if secrets == [record.actual] else 10), ""
        if "-newPassword" in argv:
            assert secrets[0] == record.actual
            assert json.loads(record.journal.read_text())["new"] == secrets[1]
            if record.change:
                record.actual = secrets[1]
            # sysadminctl exits zero even for some failures; authentication decides.
            return 0, ""
        assert "unlock-keychain" in argv
        return (0 if record.unlock else 10), ""

    monkeypatch.setattr(guest, "run_with_passwords", prompted)
    def command(argv, **kwargs):
        record.commands.append(argv)
        if argv[-1] == "autoLoginUser":
            return SimpleNamespace(returncode=0, stdout=record.automatic_login + "\n")
        return None

    monkeypatch.setattr(guest, "deadline_command", command)
    return record


@pytest.mark.parametrize("verified", [True, False])
def test_control_password_changes_with_keychain_and_autologin_in_step(
    control_rotation, verified
):
    record = control_rotation
    record.change = verified
    if verified:
        guest.rotate_control_password()
    else:
        with pytest.raises(RuntimeError, match="did not take effect"):
            guest.rotate_control_password()
    change, old_new = next(item for item in record.prompts if "-newPassword" in item[0])
    new = old_new[1]
    assert change == guest.account_command(501) + [
        "/usr/sbin/sysadminctl", "-newPassword", "-", "-oldPassword", "-",
    ]
    assert old_new == ["old-secret", new] and re.fullmatch(r"[0-9a-f]{48}", new)
    assert all(new not in " ".join(argv) for argv, _ in record.prompts)
    if verified:
        assert record.password.read_text() == new + "\n"
        assert record.password.stat().st_mode & 0o777 == 0o600
        assert guest.kcpassword(new) == record.kcpassword.read_bytes()
        assert new.encode() not in record.kcpassword.read_bytes()
        assert any("lock-keychain" in argv for argv in record.commands)
        assert record.prompts[-1][1] == [new]
        assert "unlock-keychain" in record.prompts[-1][0]
        assert not record.journal.exists()
    else:
        assert record.password.read_text() == "old-secret\n"
        assert record.kcpassword.read_bytes() == b"stale"
        assert json.loads(record.journal.read_text()) == {"old": "old-secret", "new": new}
        assert record.journal.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("partial", [False, True])
def test_control_password_rotation_resumes_after_account_changed(
    control_rotation, monkeypatch, partial,
):
    record = control_rotation
    write = guest.write_credential
    monkeypatch.setattr(guest, "write_credential", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("interrupted write")
    ))
    with pytest.raises(OSError, match="interrupted write"):
        guest.rotate_control_password()
    new = json.loads(record.journal.read_text())["new"]
    assert record.actual == new and record.password.read_text() == "old-secret\n"
    if partial:
        record.password.write_text("")  # Interrupted between truncation and writing.
    monkeypatch.setattr(guest, "write_credential", write)
    monkeypatch.setattr(guest.secrets, "token_hex",
                        lambda *a: pytest.fail("invented another secret"))
    guest.rotate_control_password()
    assert sum("-newPassword" in argv for argv, _ in record.prompts) == 1
    assert record.password.read_text() == new + "\n"
    assert record.kcpassword.read_bytes() == guest.kcpassword(new)
    assert not record.journal.exists()


def test_a_pending_typeable_rotation_is_finished_then_the_requested_shape_made(
    control_rotation,
):
    # Enrollment interrupted after firmvm's eight-digit password was set but before its
    # journal went: a clone's first boot asks for a long one and must not adopt the eight.
    record = control_rotation
    record.journal.write_text(json.dumps({"old": "old-secret", "new": "12345678"}))
    record.journal.chmod(0o600)
    record.actual = "12345678"
    guest.rotate_control_password(typeable=False)
    final = record.password.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{48}", final) and record.actual == final
    assert not record.journal.exists()


def test_settle_credentials_finishes_a_pending_rotation_in_its_own_shape_and_starts_none(
    control_rotation, monkeypatch,
):
    record = control_rotation
    monkeypatch.setattr(guest, "station_info", lambda n: (_ for _ in ()).throw(KeyError(n)))
    # Nothing pending: nothing changes.
    assert guest.settle_credentials() == {"control": False, "stations": []}
    assert record.password.read_text() == "old-secret\n" and not record.prompts
    # A pending eight-digit change is finished as eight digits: settling is not rotating.
    record.journal.write_text(json.dumps({"old": "old-secret", "new": "12345678"}))
    record.journal.chmod(0o600)
    assert guest.settle_credentials() == {"control": True, "stations": []}
    assert record.password.read_text() == "12345678\n" and record.actual == "12345678"
    assert not record.journal.exists()


def test_control_password_leaves_a_station_turns_automatic_login_alone(control_rotation):
    # During a station's enrollment turn /etc/kcpassword holds that station's password.
    record = control_rotation
    record.automatic_login = "firmstation1"
    guest.rotate_control_password(typeable=True)
    assert re.fullmatch(r"[0-9]{8}", record.password.read_text().strip())
    assert record.kcpassword.read_bytes() == b"stale"


def test_control_password_rotates_on_a_base_image_without_autologin_or_keychain(control_rotation):
    # A fresh base has no /etc/kcpassword and firmvm has never logged in on the console.
    record = control_rotation
    record.kcpassword.unlink()
    record.keychain.unlink()
    guest.rotate_control_password()
    new = record.password.read_text().strip()
    assert record.actual == new and re.fullmatch(r"[0-9a-f]{48}", new)
    assert not record.kcpassword.exists()
    assert not any("keychain" in " ".join(argv) for argv, _ in record.prompts)
    assert not record.journal.exists()


def test_control_password_rotation_retains_journal_until_keychain_verified(control_rotation):
    record = control_rotation
    record.unlock = False
    with pytest.raises(RuntimeError, match="keychain did not unlock"):
        guest.rotate_control_password()
    new = json.loads(record.journal.read_text())["new"]
    assert record.password.read_text() == new + "\n"
    record.unlock = True
    guest.rotate_control_password()
    assert sum("-newPassword" in argv for argv, _ in record.prompts) == 1
    assert record.actual == new
    assert not record.journal.exists()


def test_control_password_rotation_checks_both_files_before_changing_the_account(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(guest, "STATE", tmp_path / "state")
    password = tmp_path / "control-password"
    password.write_text("old-secret\n")
    password.chmod(0o600)
    monkeypatch.setattr(guest, "CONTROL_PASSWORD", password)
    monkeypatch.setattr(guest, "KCPASSWORD", tmp_path / "kcpassword")
    monkeypatch.setattr(guest, "run_with_passwords", lambda *a, **k: pytest.fail("changed anyway"))
    (tmp_path / "kcpassword").write_bytes(b"stale")
    (tmp_path / "kcpassword").chmod(0o600)
    # A file the guest's root does not own, one reached through a link and a second
    # name for it all refuse. (A missing automatic-login file is a base image, not a fault.)
    with monkeypatch.context() as patch:
        patch.setattr(guest.os, "fstat", root_owned(os.fstat))
        with pytest.raises(RuntimeError, match="Unsafe.*credential file"):
            guest.rotate_control_password()
    original_fstat = os.fstat
    control_inode = password.stat().st_ino
    monkeypatch.setattr(guest.os, "fstat", lambda fd: SimpleNamespace(
        st_uid=501 if original_fstat(fd).st_ino == control_inode else 0,
        st_mode=original_fstat(fd).st_mode, st_nlink=original_fstat(fd).st_nlink,
    ))
    os.link(password, tmp_path / "second-name")
    with pytest.raises(RuntimeError, match="Unsafe.*credential file"):
        guest.rotate_control_password()
    (tmp_path / "second-name").unlink()
    password.unlink()
    password.symlink_to("/etc/passwd")
    with pytest.raises(OSError):
        guest.rotate_control_password()


def test_station_mount_runs_as_the_station_and_unmount_reports_the_point(station, monkeypatch):
    seen = []
    monkeypatch.setattr(guest, "mount_drive", lambda info, drive: seen.append((info, drive)))
    monkeypatch.setattr(
        guest, "managed_directory",
        lambda path, **options: seen.append(("mkdir", str(path), options)),
    )
    drive = {"user": "fa", "password": "pw", "port": 4450, "share": "firm"}
    result = guest.dispatch({"operation": "station-mount", "station": 1, "drive": drive})
    assert result == {"mounted": True, "point": str(station.home / "drive"),
                      "scratch": str(station.home / "scratch")}
    assert seen[0] == ("mkdir", str(station.home / "drive"),
                       {"uid": os.getuid(), "gid": os.getgid()})
    assert seen[1] == ("mkdir", str(station.home / "scratch"),
                       {"uid": os.getuid(), "gid": os.getgid()})
    info, mounted = seen[2]
    assert info["uid"] == os.getuid() and info["drive"] == str(station.home / "drive")
    assert mounted == drive
    with pytest.raises(ValueError, match="drive credentials"):
        guest.dispatch({"operation": "station-mount", "station": 1, "drive": {"user": "x"}})
    unmounted = []
    monkeypatch.setattr(guest, "unmount_drive", lambda info: unmounted.append(info["drive"]))
    assert guest.dispatch({"operation": "station-unmount", "station": 1}) == {"mounted": False}
    assert unmounted == [str(station.home / "drive")]


def test_a_station_without_a_testing_set_has_nothing_to_delete_there(tmp_path):
    """Live, simctl exits 1 with "Provided set path does not exist" for a set never made,
    which read as a failed step on every healthy release."""
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    traced = guest.DELETE_TESTING_SET.replace("exec /usr/bin/xcrun", "exec /bin/echo")
    run = lambda: subprocess.run(  # noqa: E731
        ["/bin/sh", "-c", traced], env={"HOME": str(home)}, capture_output=True, text=True,
    )
    absent = run()
    assert (absent.returncode, absent.stdout) == (0, "")
    (home / "Library" / "Developer" / "XCTestDevices").mkdir(parents=True)
    present = run()
    assert present.stdout.strip() == "simctl --set testing delete all"


def test_sweep_deletes_both_simulator_sets_once_before_killing(station, monkeypatch):
    baseline = guest.station_baseline(guest.station_info(1))
    station.add(700, "/bin/sleep")
    calls = []

    def command(info, argv, timeout):
        assert info == guest.station_info(1)
        assert station.killed == []
        calls.append((argv, timeout))
        return {"completed": False, "timed_out": True, "returncode": None}

    monkeypatch.setattr(guest, "station_command", command)
    report = guest.station_sweep(baseline, guest.station_info(1))
    assert report["ok"] is True
    assert calls == [
        (["/usr/bin/xcrun", "simctl", "shutdown", "all"], 15),
        (["/usr/bin/xcrun", "simctl", "delete", "all"], 30),
        (["/bin/sh", "-c", guest.DELETE_TESTING_SET], 30),
    ]
    assert report["simulators"] == {
        name: {"completed": False, "timed_out": True, "returncode": None}
        for name in ("shutdown", "default", "testing")
    }
    assert station.killed == [(700, 9)]


@pytest.mark.parametrize("ancestor", ["Library", "Library/LaunchAgents"])
def test_launch_agents_refuses_linked_ancestors(station, tmp_path, ancestor):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("other account")
    target = station.home / ancestor
    target.rename(target.with_name(target.name + "-old"))
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        guest.station_launch_agents(guest.station_info(1))
    with pytest.raises(OSError):
        guest.remove_station_agents(
            guest.station_info(1), ["sentinel"], time.monotonic() + 5, lambda value: None,
        )
    assert sentinel.read_text() == "other account"


def test_sweep_removes_agent_directories_without_following_links(station, tmp_path):
    baseline = guest.station_baseline(guest.station_info(1))
    outside = tmp_path / "homebrew"
    outside.mkdir()
    (outside / "formula").write_text("kept")
    agent = station.home / "Library/LaunchAgents/not-a-plist"
    agent.mkdir()
    (agent / "linked").symlink_to(outside, target_is_directory=True)
    (agent / "file").write_text("deleted")
    assert guest.station_sweep(baseline, guest.station_info(1))["ok"] is True
    assert not agent.exists()
    assert (outside / "formula").read_text() == "kept"


def test_reclaim_categories_keep_policy_and_logical_bytes(station):
    files = {
        "simulators": ["Library/Developer/CoreSimulator/Devices/A/data",
                       "Library/Developer/XCTestDevices/B/data"],
        "temp": [],
        "home": ["scratch/build", "ad hoc checkout/output", "file\nwith newline"],
        "caches": ["Library/Caches/something/data"],
        "logs": ["Library/Logs/something/data"],
        "saved_application_state": ["Library/Saved Application State/app.savedState/data"],
        "trash": [".Trash/data", "Library/.Trash/data"],
        "swiftpm": [".swiftpm/data", "Library/org.swift.swiftpm/data"],
        "xcode": [f"Library/Developer/Xcode/{name}/data" for name in (
            "DerivedData", "Archives", "Products", "Previews",
        )],
    }
    kept = [
        "Library/Preferences/app.plist", "Library/Keychains/login.keychain-db",
        "Library/Application Support/com.apple.TCC/TCC.db",
        "Library/Developer/CoreSimulator/Caches/data",
        "Library/Caches/dev.firm.station-helper/data",
        "Library/Logs/dev.firm.station-helper/data",
        "Library/Saved Application State/dev.firm.station-helper.savedState/data",
        "Applications/app", "Desktop/data", "Documents/data", "Downloads/data", "Movies/data",
        "Music/data", "Pictures/data", "Public/data", "Sites/data", "drive/sentinel",
        ".zsh_history", ".bash_history", ".zsh_sessions/history", ".bash_sessions/history",
    ]
    for name in [name for names in files.values() for name in names] + kept:
        path = station.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"1234567")
    report = guest.dispatch({"operation": "station-reclaim", "station": 1})
    assert report["refused"] is False
    assert report["completed"] is True and report["timed_out"] is False
    assert set(report["categories"]) == set(files)
    for category, names in files.items():
        assert report["categories"][category]["bytes"] == 7 * len(names)
        assert report["categories"][category]["count"] >= len(names)
        assert report["categories"][category]["skipped"] == {}
        assert all(not (station.home / name).exists() for name in names)
    assert all((station.home / name).read_bytes() == b"1234567" for name in kept)
    assert list((station.home / "scratch").iterdir()) == []
    assert report["scratch"] == str(station.home / "scratch")
    assert "Library" in report["kept"]["home"]
    assert "CoreSimulator/Caches" in report["kept"]["outside_categories"]
    assert "ad hoc checkout" not in json.dumps(report)
    again = guest.station_reclaim(guest.station_info(1))
    assert all(item["bytes"] == 0 for item in again["categories"].values())


def test_reclaim_empties_only_own_darwin_temp_contents(station, monkeypatch):
    # Both simulator tombstones and unrelated temporary files are disposable; the
    # Darwin cache directory serves the still-logged-in session and stays.
    tombstone = station.temp / "Deleting-device" / "data"
    tombstone.parent.mkdir()
    tombstone.write_bytes(b"1234567")
    (station.temp / ".temporary").write_bytes(b"123")
    (station.cache / "cached-build").write_bytes(b"12345")
    (station.home / "Library/Caches").mkdir()
    (station.home / "Library/Caches/build").write_bytes(b"12")
    kept = [station.cache / "cached-build",
            station.temp.with_name("0") / "sentinel",
            station.temp.parent.parent / "station2/T/sentinel",
            station.temp.parent.parent / "station2/C/sentinel",
            station.temp.parent.parent / "root/T/sentinel"]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kept")
    monkeypatch.setenv("TMPDIR", str(kept[-1].parent))
    inode, mode = station.temp.stat().st_ino, station.temp.stat().st_mode
    report = guest.station_reclaim(station.info)
    assert report["completed"] is True
    assert report["categories"]["temp"] == {"count": 3, "bytes": 10, "skipped": {}}
    assert report["categories"]["caches"] == {"count": 1, "bytes": 2, "skipped": {}}
    assert list(station.temp.iterdir()) == []
    assert (station.temp.stat().st_ino, station.temp.stat().st_mode) == (inode, mode)
    assert all(path.read_text() == "kept" for path in kept)
    assert all(item["bytes"] == 0 for item in
               guest.station_reclaim(station.info)["categories"].values())


@pytest.mark.parametrize("value", [None, "", "relative/T"])
def test_reclaim_reports_missing_darwin_directory_without_fallback(station, monkeypatch, value):
    (station.temp / "sentinel").write_text("kept")
    monkeypatch.setattr(guest.os, "confstr", lambda name: value)
    report = guest.station_reclaim(station.info)
    assert report["categories"]["temp"]["skipped"] == {
        f"errno:{errno.EINVAL}": {"count": 1, "bytes": 0},
    }
    assert (station.temp / "sentinel").read_text() == "kept"


def test_reclaim_resolves_darwin_paths_after_dropping_credentials(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    info = local_station_info(home)
    calls = []
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(guest.os, name, lambda value, name=name: calls.append((name, value)))
    directories = {65537: tmp_path / "T"}
    directories[65537].mkdir()
    (directories[65537] / "data").write_bytes(b"123")

    def confstr(name):
        assert calls == [("setgroups", info["groups"]), ("setgid", info["gid"]),
                         ("setuid", info["uid"])]
        assert os.environ["HOME"] == str(home)
        return str(directories[name])

    monkeypatch.setattr(guest.os, "confstr", confstr)
    report = guest.station_reclaim(info)
    assert report["categories"]["temp"]["bytes"] == 3


def test_reclaim_refuses_mounted_drive_before_any_deletion(station, monkeypatch):
    (station.home / "work").write_text("kept")
    (station.temp / "Deleting-device").write_text("kept")
    monkeypatch.setattr(guest, "drive_mounted_at", lambda info, home: True)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["refused"] is True and report["reason"] == "drive_mounted"
    assert report["completed"] is False and report["timed_out"] is False
    assert all(item == guest.cleanup_counts() for item in report["categories"].values())
    assert (station.home / "work").read_text() == "kept"
    assert (station.temp / "Deleting-device").read_text() == "kept"


def test_reclaim_unlinks_symlinks_and_skips_hard_links(station, tmp_path):
    outside = tmp_path / "another-account"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("kept")
    (station.home / "linked").symlink_to(outside, target_is_directory=True)
    (station.home / "homebrew").symlink_to("/opt/homebrew")
    os.link(sentinel, station.home / "hard-link")
    report = guest.station_reclaim(guest.station_info(1))
    assert report["completed"] is True
    assert report["categories"]["home"]["skipped"] == {"hard_link": {"count": 1, "bytes": 4}}
    assert sentinel.read_text() == "kept" and (station.home / "hard-link").exists()
    assert not os.path.lexists(station.home / "linked")
    assert not os.path.lexists(station.home / "homebrew")


def changed_stat(entry, **changes):
    values = {key: getattr(entry, key) for key in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size",
    )}
    values["st_flags"] = getattr(entry, "st_flags", 0)
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("kind", ["other_uid", "mount", "schg"])
def test_reclaim_skips_unsafe_entries_without_listing_them(station, monkeypatch, kind):
    unsafe = station.home / "unsafe"
    unsafe.mkdir()
    (unsafe / "sentinel").write_text("kept")
    inode = unsafe.stat().st_ino
    original_stat = os.stat
    original_scandir = os.scandir

    def modified(path, *args, **kwargs):
        entry = original_stat(path, *args, **kwargs)
        if entry.st_ino == inode:
            changes = {"other_uid": {"st_uid": os.getuid() + 1},
                       "mount": {"st_dev": entry.st_dev + 1},
                       "schg": {"st_flags": stat.SF_IMMUTABLE}}[kind]
            return changed_stat(entry, **changes)
        return entry

    def scandir(fd):
        assert os.fstat(fd).st_ino != inode
        return original_scandir(fd)

    monkeypatch.setattr(guest.os, "stat", modified)
    monkeypatch.setattr(guest.os, "scandir", scandir)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["categories"]["home"]["skipped"] == {kind: {"count": 1, "bytes": 0}}
    assert (unsafe / "sentinel").read_text() == "kept"


@pytest.mark.parametrize("path", ["Library", "Library/Caches"])
def test_reclaim_refuses_substituted_category_ancestors(station, tmp_path, path):
    outside = tmp_path / "outside"
    (outside / "Caches").mkdir(parents=True)
    sentinel = outside / "Caches/sentinel"
    sentinel.write_text("kept")
    replaced = station.home / path
    if replaced.exists():
        replaced.rename(replaced.with_name(replaced.name + "-old"))
    replaced.symlink_to(outside, target_is_directory=True)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["categories"]["caches"]["skipped"]
    assert sentinel.read_text() == "kept"


def test_reclaim_keeps_open_directory_when_ancestor_is_replaced(station, tmp_path, monkeypatch):
    original = station.home / "work"
    original.mkdir()
    (original / "deleted").write_text("delete me")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("kept")
    moved = station.home / "renamed"
    original_open = os.open
    replaced = False

    def open_directory(path, *args, **kwargs):
        nonlocal replaced
        fd = original_open(path, *args, **kwargs)
        if path == "work" and not replaced:
            replaced = True
            original.rename(moved)
            original.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(guest.os, "open", open_directory)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["categories"]["home"]["skipped"]["changed"]["count"] == 1
    assert sentinel.read_text() == "kept"
    assert not (moved / "deleted").exists()


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="BSD file flags")
def test_reclaim_clears_user_immutable_files_and_directories(station):
    directory = station.home / "Library/Caches"
    directory.mkdir()
    file = directory / "immutable"
    file.write_bytes(b"abc")
    os.chflags(file, stat.UF_IMMUTABLE)
    os.chflags(directory, stat.UF_IMMUTABLE)
    try:
        report = guest.station_reclaim(guest.station_info(1))
        assert report["categories"]["caches"] == {"count": 1, "bytes": 3, "skipped": {}}
        assert not file.exists()
    finally:
        os.chflags(directory, 0)
        if file.exists():
            os.chflags(file, 0)


def test_reclaim_handles_a_tree_deeper_than_python_recursion_limit(station):
    directory = station.home / "deep"
    directory.mkdir()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(120):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    old_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(100)
        report = guest.station_reclaim(guest.station_info(1))
    finally:
        sys.setrecursionlimit(old_limit)
    assert report["completed"] is True
    assert report["categories"]["home"]["count"] == 121
    assert not directory.exists()


def test_reclaim_deadline_stops_before_touching_files(station):
    (station.home / "work").write_text("kept")
    report = guest.reclaim_station_files(guest.station_info(1), 0, lambda value: None)
    assert report["timed_out"] is True and report["completed"] is False
    assert (station.home / "work").read_text() == "kept"


def test_reclaim_reports_supervisor_timeout_as_partial(station, monkeypatch):
    partial = {"refused": False, "completed": True, "timed_out": False,
               "categories": {"home": {"count": 4, "bytes": 100, "skipped": {}}}}
    monkeypatch.setattr(guest, "station_worker", lambda *args: (partial, True))
    report = guest.station_reclaim(guest.station_info(1))
    assert report["completed"] is False and report["timed_out"] is True
    assert report["categories"]["home"]["bytes"] == 100


def test_reclaim_uses_retained_home_when_its_name_is_replaced(station, tmp_path, monkeypatch):
    home = station.home
    (home / "work").write_text("deleted")
    moved = tmp_path / "moved-home"
    outside = tmp_path / "other-home"
    outside.mkdir()
    (outside / "work").write_text("kept")
    check_mount = guest.drive_mounted_at

    def replace(info, fd):
        home.rename(moved)
        home.symlink_to(outside, target_is_directory=True)
        return check_mount(info, fd)

    monkeypatch.setattr(guest, "drive_mounted_at", replace)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["completed"] is True
    assert not (moved / "work").exists() and (moved / "scratch").is_dir()
    assert (outside / "work").read_text() == "kept"
    assert not (outside / "scratch").exists()


@pytest.mark.parametrize("reason", ["mount", "other_uid"])
def test_reclaim_checks_category_ancestor_device_and_owner(station, monkeypatch, reason):
    caches = station.home / "Library/Caches"
    caches.mkdir()
    (caches / "sentinel").write_text("kept")
    inode = caches.stat().st_ino
    fstat = os.fstat

    def changed(fd):
        entry = fstat(fd)
        if entry.st_ino == inode:
            return changed_stat(entry, **({"st_dev": entry.st_dev + 1} if reason == "mount"
                                         else {"st_uid": entry.st_uid + 1}))
        return entry

    monkeypatch.setattr(guest.os, "fstat", changed)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["categories"]["caches"]["skipped"] == {reason: {"count": 1, "bytes": 0}}
    assert (caches / "sentinel").read_text() == "kept"


def test_reclaim_skips_a_file_replaced_after_its_first_stat(station, monkeypatch):
    path = station.home / "work"
    path.write_text("old")
    inode = path.stat().st_ino
    original_stat = os.stat
    replaced = False

    def replace(name, *args, **kwargs):
        nonlocal replaced
        entry = original_stat(name, *args, **kwargs)
        if entry.st_ino == inode and not replaced:
            replaced = True
            path.rename(station.home / "renamed")
            path.write_text("replacement")
        return entry

    monkeypatch.setattr(guest.os, "stat", replace)
    report = guest.station_reclaim(guest.station_info(1))
    assert report["categories"]["home"]["skipped"]["changed"] == {"count": 1, "bytes": 11}
    assert path.read_text() == "replacement"


def test_new_operations_use_explicit_info_not_default_station(station, tmp_path, monkeypatch):
    baseline = guest.station_baseline(guest.station_info(1))
    info = local_station_info(tmp_path / "second-station")
    Path(info["home"]).mkdir()
    (Path(info["home"]) / "work").write_text("deleted")
    (station.home / "sentinel").write_text("kept")
    monkeypatch.setattr(guest, "station_info", lambda n: pytest.fail("looked up another account"))
    mounted = []
    monkeypatch.setattr(guest, "mount_drive", lambda account, drive: mounted.append(account))
    assert guest.station_sweep(baseline, info)["ok"] is True
    assert guest.station_reclaim(info)["categories"]["home"]["bytes"] == 7
    result = guest.station_mount({"user": "a", "password": "b", "port": 445, "share": "f"}, info)
    assert result == {"mounted": True, "point": info["drive"], "scratch": info["scratch"]}
    assert mounted == [info]
    assert (station.home / "sentinel").read_text() == "kept"
    for path in (info["drive"], info["scratch"]):
        assert Path(path).stat().st_uid == info["uid"]
        assert Path(path).stat().st_mode & 0o777 == 0o700


def test_mount_refuses_linked_scratch_before_mounting(station, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (station.home / "scratch").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(guest, "mount_drive", lambda *args: pytest.fail("mounted anyway"))
    with pytest.raises(OSError):
        guest.station_mount(
            {"user": "a", "password": "b", "port": 445, "share": "f"}, guest.station_info(1),
        )


def test_mount_unmounts_before_setting_up_directories(station, monkeypatch):
    steps = []
    monkeypatch.setattr(guest, "unmount_drive", lambda info: steps.append("unmount"))
    monkeypatch.setattr(guest, "managed_directory", lambda *args, **kwargs: steps.append("mkdir"))
    monkeypatch.setattr(guest, "mount_drive", lambda *args: steps.append("mount"))
    guest.station_mount(
        {"user": "a", "password": "b", "port": 445, "share": "f"}, guest.station_info(1),
    )
    assert steps == ["unmount", "mkdir", "mkdir", "mount"]


@pytest.mark.parametrize("point", ["drive", "scratch"])
def test_mount_never_changes_a_still_mounted_directory(station, monkeypatch, point):
    (station.home / point).mkdir()
    original_stat = os.stat

    def mounted(name, *args, **kwargs):
        entry = original_stat(name, *args, **kwargs)
        return changed_stat(entry, st_dev=entry.st_dev + 1) if name == point else entry

    monkeypatch.setattr(guest.os, "stat", mounted)
    monkeypatch.setattr(guest, "unmount_drive", lambda info: None)
    monkeypatch.setattr(guest, "mount_drive", lambda *args: pytest.fail("mounted anyway"))
    changed = []
    monkeypatch.setattr(guest, "managed_directory", lambda path, **kwargs: changed.append(path))
    with pytest.raises(guest.DriveMountError, match="still mounted"):
        guest.station_mount(
            {"user": "a", "password": "b", "port": 445, "share": "f"}, guest.station_info(1),
        )
    assert str(station.home / point) not in changed


def test_mount_detection_is_descriptor_relative(station, monkeypatch):
    drive = station.home / "drive"
    drive.mkdir()
    original = os.stat

    def mounted(name, *args, **kwargs):
        if name == "drive":
            assert isinstance(kwargs["dir_fd"], int)
            assert kwargs["follow_symlinks"] is False
            entry = original(name, *args, **kwargs)
            return changed_stat(entry, st_dev=entry.st_dev + 1)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(guest.os, "stat", mounted)
    assert guest.station_drive_mounted(guest.station_info(1)) is True
    assert guest.station_reclaim(guest.station_info(1))["reason"] == "drive_mounted"


def test_reused_pid_is_signalled_only_under_station_credentials(station, monkeypatch):
    station.add(700, "/bin/sleep", uid=os.getuid() + 1)
    station.add(701, "/bin/sleep")
    in_worker = False

    def worker(info, work, timeout):
        nonlocal in_worker
        assert info["uid"] == os.getuid()
        in_worker = True
        try:
            return work(time.monotonic() + timeout, lambda value: None), False
        finally:
            in_worker = False

    def kill(pid, sig):
        assert in_worker
        # Simulate the kernel's check after the listed pid changed owners.
        if station.processes[pid][0] != os.getuid():
            raise PermissionError(errno.EPERM, "Operation not permitted")
        station.kill(pid, sig)

    monkeypatch.setattr(guest, "station_worker", worker)
    monkeypatch.setattr(guest.os, "kill", kill)
    entries = [{"pid": pid, "started": LSTART, "command": "/bin/sleep"} for pid in (700, 701)]
    result, _ = guest.station_worker(
        guest.station_info(1), lambda end, publish: guest.kill_station_processes(
            guest.station_info(1), entries, end, publish,
        ), 5,
    )
    assert 700 in station.processes and 701 not in station.processes
    assert result["errors"][0]["pid"] == 700
    assert result["errors"][0]["errno"] == errno.EPERM


def test_sweep_bounds_report_commands_and_returns_unverified_on_timeout(station, monkeypatch):
    baseline = guest.station_baseline(guest.station_info(1))
    run = station.run

    def hung(argv, **kwargs):
        # A listing is bounded by its own cap and by the sweep's deadline, whichever ends first.
        assert 0 < kwargs["timeout"] <= guest.COMMAND_SECONDS
        if argv[0] == "/bin/ps":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return run(argv, **kwargs)

    monkeypatch.setattr(guest.subprocess, "run", hung)
    result = guest.station_sweep(baseline, guest.station_info(1))
    assert result["ok"] is False
    assert result["processes"]["verified"] is False
    assert "timed out" in result["processes"]["error"]
    assert result["crontab"]["ok"] is True


def local_station_info(tmp_path):
    return {"station": 1, "user": "firmstation1", "uid": os.getuid(), "gid": os.getgid(),
            "groups": [os.getgid()], "home": str(tmp_path), "drive": str(tmp_path / "drive"),
            "scratch": str(tmp_path / "scratch"), "password": tmp_path / "password",
            "command_log": tmp_path / "commands.log", "socket": str(tmp_path / "helper.sock")}


def test_worker_drops_credentials_in_order_before_work(tmp_path, monkeypatch):
    calls = []
    parent = os.getpid()
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(guest.os, name, lambda value, name=name: calls.append((name, value)))
    info = local_station_info(tmp_path)
    result, timed_out = guest.station_worker(
        info, lambda deadline, publish: [os.getpid(), calls, os.environ["HOME"]], 2,
    )
    assert timed_out is False and result[0] != parent
    assert result[1] == [["setgroups", info["groups"]], ["setgid", info["gid"]],
                         ["setuid", info["uid"]]]
    assert result[2] == info["home"] and calls == []


def test_worker_overrun_is_killed_and_reaped_with_partial_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, "enter_station", lambda info: None)

    def stuck(deadline, publish):
        publish({"pid": os.getpid(), "count": 7})
        time.sleep(30)

    started = time.monotonic()
    report, timed_out = guest.station_worker(local_station_info(tmp_path), stuck, 0.1)
    assert time.monotonic() - started < 2
    assert timed_out is True and report["count"] == 7
    with pytest.raises(ChildProcessError):
        os.waitpid(report["pid"], os.WNOHANG)


def test_worker_crash_after_progress_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, "enter_station", lambda info: None)

    def crash(deadline, publish):
        publish({"completed": False})
        os._exit(1)

    with pytest.raises(RuntimeError, match="Station worker failed"):
        guest.station_worker(local_station_info(tmp_path), crash, 2)


def test_station_command_hung_process_is_reaped(tmp_path, monkeypatch):
    processes = []
    popen = subprocess.Popen

    def start(argv, **kwargs):
        prefix = guest.account_command(os.getuid())
        assert argv[:len(prefix)] == prefix
        # Execute only our harmless child; never operate the host's launchctl.
        process = popen(argv[len(prefix):], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(guest.subprocess, "Popen", start)
    started = time.monotonic()
    result = guest.station_command(
        local_station_info(tmp_path), [sys.executable, "-c", "import time; time.sleep(30)"], 0.1,
    )
    assert result == {"completed": False, "timed_out": True, "returncode": None}
    assert time.monotonic() - started < 2
    assert processes[0].returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    # A command that fails in time keeps the tail of what it said; one that succeeds keeps none.
    info = local_station_info(tmp_path)
    complaint = "import sys; print('x' * 600 + 'no such device', file=sys.stderr); sys.exit(3)"
    failed = guest.station_command(info, [sys.executable, "-c", complaint], 5)
    assert failed["returncode"] == 3 and not failed["completed"]
    assert failed["said"].endswith("no such device") and len(failed["said"]) <= 512
    assert "said" not in guest.station_command(info, [sys.executable, "-c", ""], 5)


def test_password_command_closing_pty_does_not_escape_deadline():
    started = time.monotonic()
    code, _ = guest.run_with_passwords(
        [sys.executable, "-c", "import os,time; os.closerange(0,3); time.sleep(30)"],
        [], lambda: None, timeout=0.1,
    )
    assert code == -9 and time.monotonic() - started < 2


def test_station_keep_awake_sets_the_stations_own_lock_and_screen_saver(station, monkeypatch):
    lock_changes = []
    monkeypatch.setattr(guest, "station_password", lambda info: "synthetic-secret")
    monkeypatch.setattr(guest, "run_with_passwords",
                        lambda argv, secrets, enter, timeout: lock_changes.append(argv) or (0, ""))
    real_run = station.run
    idle, locked, answer = [0], [0], [None]

    def run(argv, **kwargs):
        if argv[-2:] == ["-screenLock", "status"]:
            # The account's own answer when it gives one; otherwise the old preference key.
            if answer[0] is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no session")
            return SimpleNamespace(returncode=0, stdout="",
                                   stderr=f"2026-09-21 sysadminctl[1:2] {answer[0]}\n")
        if argv[:2] == ["/usr/bin/defaults", "read"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="does not exist")
        if argv[-3:] == ["read", "com.apple.screensaver", "askForPassword"]:
            return SimpleNamespace(returncode=0, stdout=f"{locked[0]}\n", stderr="")
        if argv[-4:] == ["-currentHost", "read", "com.apple.screensaver", "idleTime"]:
            return SimpleNamespace(returncode=0, stdout=f"{idle[0]}\n", stderr="")
        if argv == ["/usr/bin/pmset", "-g"]:
            return SimpleNamespace(returncode=0, stdout=(
                "System-wide power settings:\nCurrently in use:\n powernap             1\n"
                " sleep                0 (sleep prevented by powerd)\n"
                " disksleep            0\n displaysleep         0\n"
            ))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(guest.subprocess, "run", run)
    as_station = guest.account_command(os.getuid())
    result = guest.dispatch({"operation": "station-keep-awake", "station": 1})
    assert result == {"screen_lock": "screenLock is off",
                      "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0},
                      "relock_at_login": False, "screen_saver_idle": 0}
    assert as_station + ["/usr/bin/defaults", "-currentHost", "write", "com.apple.screensaver",
                         "idleTime", "-int", "0"] in station.commands
    # The station's own settings only: no machine power policy, no loginwindow key.
    assert not any("-a" in argv or "delete" in argv for argv in station.commands)
    assert lock_changes == [as_station + ["/usr/sbin/sysadminctl", "-screenLock", "off",
                                          "-password", "-"]]
    assert guest.dispatch({"operation": "station-awake-status", "station": 1}) == result
    idle[0], locked[0] = 1200, 1
    status = guest.station_awake_status(guest.station_info(1))
    assert status["screen_saver_idle"] == 1200 and status["screen_lock"] == "screenLock is on"
    locked[0] = "unavailable"
    assert guest.station_awake_status(guest.station_info(1))["screen_lock"] == "unknown"
    answer[0] = "screenLock is off"
    assert guest.station_awake_status(guest.station_info(1))["screen_lock"] == "screenLock is off"


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              12345.
Pages active:                           234567.
Pages occupied by compressor:             4096.
Pageouts:                                   17.
Swapouts:                                  900.
"""
SYSCTL = {
    "hw.memsize": "12884901888",
    "hw.ncpu": "4",
    "kern.memorystatus_level": "37",
    "kern.memorystatus_vm_pressure_level": "1",
}


@pytest.fixture
def machine(monkeypatch, tmp_path):
    def run(argv, **kwargs):
        if argv[0] == "/usr/bin/vm_stat":
            return SimpleNamespace(returncode=0, stdout=VM_STAT)
        if argv[:2] == ["/usr/sbin/sysctl", "-n"]:
            return SimpleNamespace(returncode=0, stdout=SYSCTL.get(argv[2], "") + "\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(guest.subprocess, "run", run)
    monkeypatch.setattr(guest, "boot", lambda: "{ sec = 1758000000 }")
    monkeypatch.setattr(guest, "DATA_VOLUME", str(tmp_path))
    monkeypatch.setattr(
        guest.os, "statvfs",
        lambda path: SimpleNamespace(f_blocks=1000, f_bfree=250, f_frsize=4096),
    )


def test_sample_reports_machine_wide_counters(machine):
    assert guest.sample() == {
        "boot": "{ sec = 1758000000 }",
        "cpus": 4,
        "memory_total_bytes": 12884901888,
        # macOS reports free memory as a percentage, not as a usable page count.
        "memory_available_bytes": 12884901888 * 37 // 100,
        "pressure_level": 1,
        "compressed_bytes": 4096 * 16384,
        "swapouts": 900,
        "disk_used_bytes": 750 * 4096,
        "disk_total_bytes": 1000 * 4096,
    }


def test_sample_reports_unavailable_counters_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(guest, "boot", lambda: "boot")
    monkeypatch.setattr(guest, "DATA_VOLUME", str(tmp_path / "gone"))
    monkeypatch.setattr(
        guest.subprocess, "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert guest.sample() == {
        "boot": "boot",
        "cpus": None,
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "pressure_level": None,
        "compressed_bytes": None,
        "swapouts": None,
        "disk_used_bytes": None,
        "disk_total_bytes": None,
    }


def test_station_command_log_is_handed_over_and_truncated(station, monkeypatch):
    monkeypatch.setattr(guest.os, "fstat", root_owned(os.fstat))
    assert guest.dispatch({"operation": "station-command-log", "station": 1}) == {"data": ""}
    station.log.write_text("2026-09-15T10:00:00Z xcodebuild\n")
    result = guest.dispatch({"operation": "station-command-log", "station": 1})
    assert base64.b64decode(result["data"]) == b"2026-09-15T10:00:00Z xcodebuild\n"
    assert result["truncated"] is False
    assert station.log.read_bytes() == b""
    station.log.unlink()
    station.log.symlink_to("/etc/passwd")
    with pytest.raises(OSError):
        guest.station_command_log(guest.station_info(1))


def test_station_keys_and_command_logs_belong_to_the_named_station(station, tmp_path, monkeypatch):
    monkeypatch.setattr(guest.os, "fstat", root_owned(os.fstat))
    second = {**local_station_info(tmp_path / "second"), "station": 2, "user": "firmstation2"}
    Path(second["home"]).mkdir()
    accounts = {1: station.info, 2: second}
    monkeypatch.setattr(guest, "station_info", accounts.__getitem__)
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial firm-mac-lease"
    for number in accounts:
        guest.dispatch({"operation": "station-key-install", "station": number, "public_key": key})
    guest.dispatch({"operation": "station-key-remove", "station": 1})
    assert not (station.keys / "firmstation1").exists()
    assert (station.keys / "firmstation2").read_text() == key + "\n"
    station.log.write_text("station one\n")
    second["command_log"].write_text("station two\n")
    result = guest.dispatch({"operation": "station-command-log", "station": 2})
    assert base64.b64decode(result["data"]) == b"station two\n"
    assert second["command_log"].read_bytes() == b""
    assert station.log.read_text() == "station one\n"


@pytest.mark.parametrize("reader,part", [
    ("station_services", "services"), ("station_launch_agents", "launch_agents"),
    ("station_crontab", "crontab"), ("station_processes", "processes"),
])
@pytest.mark.parametrize("error", [RuntimeError("read failed"), TimeoutError("deadline")])
def test_sweep_isolates_failed_reads(station, monkeypatch, reader, part, error):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    station.services[f"user/{os.getuid()}/lease.agent"] = None
    agent = station.home / "Library/LaunchAgents/lease.plist"
    agent.write_text("work")
    station.crontab = "* * * * * /bin/echo work\n"

    def fail(*args):
        raise error

    monkeypatch.setattr(guest, reader, fail)
    report = guest.station_sweep(baseline, station.info)
    assert report["ok"] is False
    assert report[part]["verified"] is False
    assert report[part]["error"] == str(error)
    for name in set(guest.WORK_PARTS) - {part}:
        assert report[name]["ok"] is True, report
    assert (700 in station.processes) == (part == "processes")
    assert agent.exists() == (part == "launch_agents")
    assert bool(station.crontab) == (part == "crontab")


@pytest.mark.parametrize("row", ["bad row", "x ?? label", "- (pe) label", "0 - two labels"])
def test_unknown_launchd_row_is_evidence_and_does_not_stop_sweep(station, monkeypatch, row):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    run = station.run
    raw = "\t\t" + row

    def listing(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[:2] == ["/bin/launchctl", "print"] and argv[2].startswith("user/"):
            result.stdout = result.stdout.replace("\tservices = {", "\tservices = {\n" + raw)
        return result

    monkeypatch.setattr(guest.subprocess, "run", listing)
    report = guest.station_sweep(baseline, station.info)
    assert report["ok"] is False
    assert report["services"]["unparsed"] == [{"domain": f"user/{os.getuid()}", "line": raw}]
    assert report["services"]["verified"] is False
    assert report["processes"]["ok"] is True
    assert 700 not in station.processes
    with pytest.raises(RuntimeError, match="could not be verified"):
        guest.station_baseline(station.info)


def test_process_fallback_preserves_baseline_and_never_targets_other_uids(station, monkeypatch):
    station.info["uid"] = 502
    for pid in (100, 101):
        _, start, command = station.processes[pid]
        station.processes[pid] = (502, start, command)
    monkeypatch.setattr(guest, "station_launch_agents", lambda *args: [])
    baseline = guest.station_baseline(station.info)
    # Reused pid, different start time: this is lease work, not the baseline process.
    station.add(100, "/bin/sleep", uid=502, start="Tue Sep 16 11:00:00 2026")
    station.add(700, "/bin/sleep", uid=502)
    for pid, uid in ((701, 0), (702, 501), (703, 503)):
        station.add(pid, "/bin/sleep", uid=uid)
    monkeypatch.setattr(guest, "station_services", lambda *args: (_ for _ in ()).throw(
        RuntimeError("launchd failed before baseline comparison"),
    ))
    workers = []
    worker = guest.station_worker

    def owned_worker(info, action, timeout):
        workers.append(info["uid"])
        return worker(info, action, timeout)

    monkeypatch.setattr(guest, "station_worker", owned_worker)
    report = guest.station_sweep(baseline, station.info)
    assert report["ok"] is False
    assert report["processes"]["ok"] is True
    assert {pid for pid, _ in station.killed} == {100, 700}
    assert {101, 701, 702, 703} <= station.processes.keys()
    assert workers and set(workers) == {station.info["uid"]}


def test_provenance_deadline_leaves_budget_for_process_fallback(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    now = [time.monotonic()]
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])

    def timeout(pid, deadline):
        now[0] = deadline + 0.001
        raise TimeoutError("provenance deadline")

    monkeypatch.setattr(guest, "process_provenance", timeout)
    report = guest.station_sweep(baseline, station.info)
    assert 700 not in station.processes
    assert report["processes"]["verified"] is True
    assert any("deadline" in entry.get("error", "")
               for entry in report["processes"]["diagnostics"])
    assert report["ok"] is True


def test_crontab_command_failure_is_not_an_empty_crontab(station, monkeypatch):
    monkeypatch.setattr(guest, "deadline_command", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="crontab: permission denied",
    ))
    with pytest.raises(RuntimeError, match="permission denied"):
        guest.station_crontab(station.info)


def test_verify_preserves_other_checks_when_process_status_read_fails(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    monkeypatch.setattr(guest, "station_processes", lambda *a: (_ for _ in ()).throw(
        RuntimeError("ps unavailable"),
    ))
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: False)
    report = guest.station_verify(baseline, station.info)
    assert report["ok"] is False
    assert report["processes"]["error"] == "ps unavailable"
    assert report["sessions"] is None
    assert report["status_errors"]["sessions"] == "ps unavailable"
    assert report["drive_mounted"] is False
    assert report["services"]["ok"] is True


def test_one_read_consuming_its_deadline_leaves_other_cleanup_budgets(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    station.crontab = "* * * * * /bin/echo work\n"
    now = [time.monotonic()]
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def timeout(info, deadline):
        now[0] = deadline + 0.001
        raise TimeoutError("services read deadline")

    monkeypatch.setattr(guest, "station_services", timeout)
    report = guest.station_sweep(baseline, station.info)
    assert report["services"]["verified"] is False and report["ok"] is False
    assert 700 not in station.processes
    assert not station.crontab


def test_final_read_catches_work_started_during_another_parts_cleanup(station, monkeypatch):
    # Killing a process registers a service after its part was clean; the next inventory
    # finds it and cleans it through the same loop.
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    kill = station.kill
    target = f"user/{os.getuid()}/lease.respawned"

    def respawn(pid, sig):
        kill(pid, sig)
        station.services[target] = None

    monkeypatch.setattr(guest.os, "kill", respawn)
    report = guest.station_sweep(baseline, station.info)
    assert ["/bin/launchctl", "bootout", target] in station.commands
    assert target not in station.services
    assert report["ok"] is True and report["services"]["extra"] == []


def test_work_that_keeps_returning_stays_dirty_at_the_deadline(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    target = f"user/{os.getuid()}/lease.returning"
    station.services[target] = None
    run = station.run

    def returning(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv == ["/bin/launchctl", "bootout", target]:
            station.services[target] = None
        return result

    monkeypatch.setattr(guest.subprocess, "run", returning)
    monkeypatch.setattr(guest, "SWEEP_SECONDS", 2)
    report = guest.station_sweep(baseline, station.info)
    assert report["ok"] is False and report["services"]["ok"] is False
    assert target in station.services


def test_unacknowledged_cleanup_worker_blocks_even_after_a_clean_read(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    worker = guest.station_worker

    def lost_acknowledgement(info, work, timeout):
        result, timed_out = worker(info, work, timeout)
        if isinstance(result, dict) and result.get("completed") and "errors" in result:
            return result, True
        return result, timed_out

    monkeypatch.setattr(guest, "station_worker", lost_acknowledgement)
    report = guest.station_sweep(baseline, station.info)
    assert 700 not in station.processes
    assert all(report[name]["ok"] for name in guest.WORK_PARTS)
    assert report["cleanup_incomplete"] is True and report["ok"] is False


def test_process_report_classifies_each_process_in_one_inventory(station, monkeypatch):
    baseline = guest.station_baseline(station.info)
    station.add(700, "/bin/sleep")
    station.add(701, "/bin/sleep")
    processes, provenance = guest.station_processes, guest.process_provenance
    listings, lookups = [], []

    def listing(*args):
        listings.append(True)
        return processes(*args)

    def lookup(pid, deadline=None):
        lookups.append(pid)
        return provenance(pid, deadline)

    monkeypatch.setattr(guest, "station_processes", listing)
    monkeypatch.setattr(guest, "process_provenance", lookup)
    report = guest.station_report(baseline, station.info)
    assert len(listings) == 1 and lookups == [700, 701]
    assert [entry["pid"] for entry in report["processes"]["extra"]] == [700, 701]
