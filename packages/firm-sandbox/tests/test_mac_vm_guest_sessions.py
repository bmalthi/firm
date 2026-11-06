"""Numbered guest accounts, session creation, and the guest lock boundary."""

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import mac_vm_guest as guest


@pytest.fixture
def account(monkeypatch):
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda user: SimpleNamespace(
        pw_uid=501 + int(user.removeprefix("firmstation")), pw_gid=20, pw_dir=f"/Users/{user}",
    ))
    monkeypatch.setattr(guest.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=700, gr_mem=[]))
    monkeypatch.setattr(guest.os, "getgrouplist", lambda user, gid: [gid, 700])
    return guest.station_info(2)


def test_station_info_resolves_each_account_and_its_private_paths(account):
    assert account == {
        "station": 2, "user": "firmstation2", "uid": 503, "gid": 20, "groups": [20, 700],
        "home": "/Users/firmstation2", "drive": "/Users/firmstation2/drive",
        "scratch": "/Users/firmstation2/scratch",
        "password": guest.PASSWORDS / "firmstation2",
        "command_log": guest.COMMAND_LOGS / "firmstation2" / "commands.log",
        "socket": "/Users/Shared/firm-station-helper/firmstation2/station.sock",
    }
    assert guest.station_info(8)["uid"] == 509


@pytest.mark.parametrize("fault", ["uid", "home", "group"])
def test_station_info_refuses_an_unexpected_identity(account, monkeypatch, fault):
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda user: SimpleNamespace(
        pw_uid=900 if fault == "uid" else 503, pw_gid=20,
        pw_dir="/Users/other" if fault == "home" else "/Users/firmstation2",
    ))
    if fault == "group":
        monkeypatch.setattr(guest.os, "getgrouplist", lambda user, gid: [gid])
    with pytest.raises(RuntimeError, match="Unexpected station identity"):
        guest.station_info(2)


STATION_OPERATIONS = [
    "station-account", "station-status", "station-baseline", "station-sweep", "station-verify",
    "station-kill-sessions", "station-key-install", "station-key-remove", "station-mount",
    "station-reclaim", "station-unmount", "station-command-log", "station-keep-awake",
    "station-awake-status", "station-session-ensure", "station-session-reset",
]


@pytest.mark.parametrize("operation", STATION_OPERATIONS)
@pytest.mark.parametrize("number", [None, True, False, 0, 9, -1, 1.0, "2"])
def test_every_station_operation_refuses_invalid_numbers_before_locking(
    operation, number, monkeypatch,
):
    monkeypatch.setattr(guest, "lock", lambda *a: pytest.fail("locked an invalid station"))
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        guest.dispatch({"operation": operation, "station": number})


@pytest.mark.parametrize("operation", STATION_OPERATIONS)
def test_every_station_operation_requires_a_number(operation, monkeypatch):
    monkeypatch.setattr(guest, "lock", lambda *a: pytest.fail("locked an unnamed station"))
    with pytest.raises(KeyError, match="station"):
        guest.dispatch({"operation": operation})


@pytest.mark.parametrize("operation", STATION_OPERATIONS)
def test_every_station_operation_routes_the_named_account(
    operation, account, tmp_path, monkeypatch,
):
    monkeypatch.setattr(guest, "STATE", tmp_path / "state")
    calls = []
    name = operation.replace("-", "_")
    monkeypatch.setattr(guest, name, lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"checked": True}
    ))
    request = {"operation": operation, "station": 2}
    extra = {"station-sweep": "baseline", "station-verify": "baseline",
             "station-key-install": "public_key", "station-mount": "drive"}.get(operation)
    if extra:
        request[extra] = {"input": extra}
    assert guest.dispatch(request) == {"checked": True}
    expected = (2,) if operation == "station-account" else (
        (request[extra], account) if extra else (account,)
    )
    kwargs = {}
    if operation == "station-sweep":
        expected = (*expected, guest.SWEEP_SECONDS)
        kwargs = {"simulators": True}
    assert calls == [(expected, kwargs)]


def test_station_locks_share_the_machine_but_exclude_the_same_station(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, "STATE", tmp_path)
    monkeypatch.setattr(guest, "LOCK_WAIT", 0.2)  # a held lock is waited for, then refused
    with guest.lock(1):
        with guest.lock(2):
            with pytest.raises(RuntimeError, match="busy with another operation"):
                with guest.lock():
                    pytest.fail("machine mutation entered")
        with pytest.raises(RuntimeError, match="busy with another operation"):
            with guest.lock(1):
                pytest.fail("second station operation entered")
    with guest.lock():
        with pytest.raises(RuntimeError, match="busy with another operation"):
            with guest.lock(2):
                pytest.fail("station entered during machine mutation")
    with guest.lock(1):
        pass


def test_dispatch_takes_station_or_machine_lock_and_sample_takes_none(monkeypatch):
    entered = []

    @contextmanager
    def lock(station=None):
        entered.append(station)
        yield

    monkeypatch.setattr(guest, "lock", lock)
    monkeypatch.setattr(guest, "station_operation", lambda operation, request: {"station": 2})
    monkeypatch.setattr(guest, "keep_awake", lambda: {"configured": True})
    monkeypatch.setattr(guest, "sample", lambda: {"sampled": True})
    assert guest.dispatch({"operation": "station-status", "station": 2}) == {"station": 2}
    assert guest.dispatch({"operation": "keep-awake"}) == {"configured": True}
    assert guest.dispatch({"operation": "sample"}) == {"sampled": True}
    assert entered == [2, None]


@pytest.fixture
def sharing(monkeypatch):
    state = {
        "remote_management": False, "disabled": '"com.apple.screensharing" => false',
        "record": {"dsAttrTypeStandard:NestedGroups": ["STATION-GROUP"]},
        "listener": "COMMAND PID USER\nlaunchd 1 root\n", "listener_code": 0,
        "executable": ("/System/Library/CoreServices/RemoteManagement/"
                       "screensharingd.bundle/Contents/MacOS/screensharingd"),
    }
    monkeypatch.setattr(Path, "exists", lambda path: state["remote_management"])
    monkeypatch.setattr(guest.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=700, gr_mem=[]))
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=501, pw_gid=20))
    monkeypatch.setattr(guest.os, "getgrouplist", lambda user, gid: [20])

    def command(argv, deadline, **kwargs):
        if argv[0] == "/bin/launchctl":
            output = state["disabled"]
        elif "-plist" in argv:
            output = plistlib.dumps(state["record"])
        elif argv[0] == "/usr/bin/dscl":
            output = "GeneratedUID: STATION-GROUP\n"
        elif argv[0] == "/usr/sbin/lsof":
            return SimpleNamespace(returncode=state["listener_code"], stdout=state["listener"])
        elif argv[0] == "/bin/ps":
            output = state["executable"]
        else:
            pytest.fail(f"Unexpected command: {argv}")
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(guest, "deadline_command", command)
    return state


@pytest.mark.parametrize("listener", ["launchd 1 root", "screensha 123 root"])
def test_screen_sharing_accepts_launchd_and_its_root_daemon(sharing, listener):
    sharing["listener"] = "COMMAND PID USER\n" + listener + "\n"
    guest.screen_sharing_ready(time.monotonic() + 5)


@pytest.mark.parametrize("fault", [
    "remote_management", "disabled", "direct-member", "member-guid", "nested-group",
    "nonroot", "wrong-executable", "absent", "control-member",
])
def test_screen_sharing_refuses_unscoped_access_or_an_untrusted_listener(
    sharing, monkeypatch, fault,
):
    if fault == "remote_management":
        sharing["remote_management"] = True
    elif fault == "disabled":
        sharing["disabled"] = '"com.apple.screensharing" => true'
    elif fault == "direct-member":
        sharing["record"]["dsAttrTypeStandard:GroupMembership"] = ["firmvm"]
    elif fault == "member-guid":
        sharing["record"]["dsAttrTypeStandard:GroupMembers"] = ["CONTROL-GUID"]
    elif fault == "nested-group":
        sharing["record"]["dsAttrTypeStandard:NestedGroups"].append("ANOTHER-GROUP")
    elif fault == "nonroot":
        sharing["listener"] = "COMMAND PID USER\nscreensha 123 firmstation2\n"
    elif fault == "wrong-executable":
        sharing["listener"] = "COMMAND PID USER\nscreensha 123 root\n"
        sharing["executable"] = "/tmp/screensharingd"
    elif fault == "absent":
        sharing["listener_code"] = 1
    elif fault == "control-member":
        monkeypatch.setattr(guest.grp, "getgrnam", lambda name: SimpleNamespace(
            gr_gid=700, gr_mem=["firmvm"],
        ))
        monkeypatch.setattr(guest.os, "getgrouplist", lambda user, gid: [20, 700])
    with pytest.raises(RuntimeError):
        guest.screen_sharing_ready(time.monotonic() + 5)


def test_session_login_sends_credentials_only_on_stdin(account, monkeypatch):
    calls = []
    monkeypatch.setattr(guest, "screen_sharing_ready", lambda end: calls.append("checked"))
    monkeypatch.setattr(guest, "station_password", lambda info: "private-password")

    def child(argv, *, data, timeout):
        assert calls == ["checked"]
        assert "private-password" not in " ".join(argv)
        assert json.loads(data) == {"username": "firmstation2", "password": "private-password"}
        assert 0 < timeout <= 20
        calls.append(argv)
        return 0, b'{"authenticated": true}\n'

    monkeypatch.setattr(guest, "control_child", child)
    assert guest.session_login(account, time.monotonic() + 30) is True
    assert calls[1] == [guest.SESSION_PYTHON, "-I", "-c", guest.SESSION_LOGIN]


@pytest.mark.parametrize("code,output", [(1, b'{"authenticated": true}'), (0, b""),
                                         (0, b"private-password"),
                                         (0, b'{"authenticated": false}')])
def test_session_login_does_not_report_failed_or_malformed_login_as_success(
    account, monkeypatch, tmp_path, code, output,
):
    monkeypatch.setattr(guest, "STATE", tmp_path)
    monkeypatch.setattr(guest, "screen_sharing_ready", lambda end: None)
    monkeypatch.setattr(guest, "station_password", lambda info: "private-password")
    monkeypatch.setattr(guest, "control_child", lambda *a, **k: (code, output))
    assert guest.session_login(account, time.monotonic() + 10) is False


def test_a_password_the_account_rejects_twice_is_not_offered_again(account, monkeypatch, tmp_path):
    # macOS locks an account out after a few failed logins, for a time that grows.
    monkeypatch.setattr(guest, "STATE", tmp_path)
    monkeypatch.setattr(guest, "screen_sharing_ready", lambda end: None)
    monkeypatch.setattr(guest, "station_password", lambda info: "private-password")
    attempts = []
    answer = [b'{"authenticated": false}']
    monkeypatch.setattr(guest, "control_child",
                        lambda *a, **k: attempts.append(1) or (0, answer[0]))
    # Screen Sharing rejecting a RIGHT password (macOS holding the account after failed
    # logins, which another local process can bring about) is not this station's fault
    # and is not counted: root settles locally whether the recorded password is wrong.
    verdict = ["right"]
    monkeypatch.setattr(guest, "password_verdict", lambda info, deadline: verdict[0])
    for _ in range(2):
        assert guest.session_login(account, time.monotonic() + 10) is False
    verdict[0] = "unknown"  # a stalled directory service is not a wrong password either
    for _ in range(2):
        assert guest.session_login(account, time.monotonic() + 10) is False
    assert len(attempts) == 4 and not guest.login_failures(account).exists()
    verdict[0] = "wrong"
    for _ in range(4):
        assert guest.session_login(account, time.monotonic() + 10) is False
    assert len(attempts) == 4 + guest.LOGIN_ATTEMPTS == 6
    # A timeout or a malformed reply is not the account's verdict and is not counted.
    guest.login_failures(account).unlink()
    answer[0] = b""
    for _ in range(4):
        guest.session_login(account, time.monotonic() + 10)
    assert len(attempts) == 10 and not guest.login_failures(account).exists()
    # A login that works clears the count.
    guest.login_failures(account).write_text("1")
    answer[0] = b'{"authenticated": true}'
    assert guest.session_login(account, time.monotonic() + 10) is True
    assert not guest.login_failures(account).exists()


def test_password_verdict_stays_inside_the_login_deadline(account, monkeypatch):
    # The verdict runs after a rejected login; past the caller's deadline it is not
    # asked for at all, so the host timeout over that deadline still holds.
    timeouts = []
    monkeypatch.setattr(guest, "station_password", lambda info: "private-password")
    monkeypatch.setattr(guest, "run_with_passwords",
                        lambda argv, passwords, enter, timeout: timeouts.append(timeout)
                        or (0, ""))
    assert guest.password_verdict(account, time.monotonic() - 1) == "unknown"
    assert timeouts == []
    assert guest.password_verdict(account, time.monotonic() + 5) == "right"
    assert 0 < timeouts[0] <= 5


def test_control_child_timeout_kills_and_reaps_its_process_group(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def start(argv, **kwargs):
        prefix = guest.account_command(501)
        assert argv[:len(prefix)] == prefix
        assert kwargs["start_new_session"] is True
        process = popen(argv[len(prefix):], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(guest.subprocess, "Popen", start)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        guest.control_child([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
    assert time.monotonic() - started < 2
    assert processes[0].returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)


def ready_state(account):
    return {"ready": True, "environment": "vm-session", "session": {
        "uid": account["uid"], "gui_uid": account["uid"], "login_done": True,
        "screen_locked": False, "on_console": False,
    }}


def test_session_ensure_reuses_a_ready_off_console_helper(account, monkeypatch):
    state = ready_state(account)
    monkeypatch.setattr(guest, "helper_status", lambda info, end: state)
    monkeypatch.setattr(guest, "session_login", lambda *a: pytest.fail("logged in again"))
    assert guest.station_session_ensure(account) == {
        "station": 2, "user": "firmstation2", "uid": 503,
        "ready": True, "created": False, "helper": state,
    }


@pytest.mark.parametrize("field,value", [("uid", 502), ("gui_uid", 502),
                                          ("login_done", False), ("screen_locked", True)])
def test_session_ensure_refuses_an_incomplete_or_wrong_account_helper(
    account, monkeypatch, field, value,
):
    state = ready_state(account)
    state["session"][field] = value
    monkeypatch.setattr(guest, "helper_status", lambda info, end: state)
    monkeypatch.setattr(guest, "session_login", lambda *a: pytest.fail("relogged a live session"))
    result = guest.station_session_ensure(account)
    assert result["ready"] is False and result["failure"] == "session_not_ready"


def test_session_ensure_waits_for_helper_confirmation_after_login(account, monkeypatch):
    state = ready_state(account)
    replies = iter([None, None, state])
    monkeypatch.setattr(guest, "helper_status", lambda info, end: next(replies))
    monkeypatch.setattr(guest, "session_login", lambda info, end: True)
    monkeypatch.setattr(guest.time, "sleep", lambda duration: None)
    result = guest.station_session_ensure(account)
    assert result["ready"] is True and result["created"] is True and result["helper"] == state


def test_session_ensure_cannot_succeed_on_login_alone(account, monkeypatch):
    clock = [0]
    monkeypatch.setattr(guest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guest.time, "sleep", lambda duration: clock.__setitem__(
        0, clock[0] + duration,
    ))
    monkeypatch.setattr(guest, "helper_status", lambda info, end: None)
    monkeypatch.setattr(guest, "session_login", lambda info, end: True)
    result = guest.station_session_ensure(account, deadline=1)
    assert result["ready"] is False and result["failure"] == "helper_not_ready"


def test_helper_status_uses_the_numbered_authenticated_relay(account, monkeypatch):
    state = ready_state(account)

    def child(argv, *, timeout):
        assert argv == ["/usr/local/firm/bin/python3", "-I", guest.RELAY, "2", "--status"]
        assert 0 < timeout <= 12
        return 0, json.dumps({"ok": True, "result": state}).encode()

    monkeypatch.setattr(guest, "control_child", child)
    assert guest.helper_status(account, time.monotonic() + 20) == state
    monkeypatch.setattr(guest, "control_child", lambda *a, **k: (1, b'{"ok": true, "result": {}}'))
    assert guest.helper_status(account, time.monotonic() + 20) is None


def test_session_reset_refuses_a_station_with_a_lease_key(account, tmp_path, monkeypatch):
    monkeypatch.setattr(guest, "LEASE_KEYS", tmp_path)
    (tmp_path / account["user"]).write_text("key")
    monkeypatch.setattr(guest, "station_kill_sessions", lambda *a: pytest.fail("touched lease"))
    with pytest.raises(RuntimeError, match="Remove the station lease key"):
        guest.station_session_reset(account)


def test_session_reset_ends_only_its_gui_and_gives_the_login_its_whole_deadline(
    account, tmp_path, monkeypatch,
):
    monkeypatch.setattr(guest, "LEASE_KEYS", tmp_path)
    monkeypatch.setattr(guest.time, "monotonic", lambda: 100.0)
    calls, deadlines = [], []
    monkeypatch.setattr(guest, "station_kill_sessions", lambda info: {"remaining": []})
    monkeypatch.setattr(guest, "unmount_drive", lambda info: calls.append("unmount"))
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: False)
    monkeypatch.setattr(guest, "station_processes", lambda *a: pytest.fail("read the station"))
    monkeypatch.setattr(guest, "station_session_ensure",
                        lambda info, end: deadlines.append(end) or {
                            "ready": True, "station": info["station"],
                        })

    def command(argv, deadline, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(guest, "deadline_command", command)
    result = guest.station_session_reset(account)
    assert result == {"ready": True, "station": 2, "reset": True}
    # The user domain is left alone, and what the station still runs is the sweep's to
    # report: nothing here spends the deadline the new session needs.
    assert calls == ["unmount", ["/bin/launchctl", "bootout", "gui/503"],
                     ["/bin/launchctl", "print", "gui/503"]]
    assert deadlines == [180.0]


def test_session_ensure_reports_login_failure_without_claiming_a_created_session(
    account, monkeypatch,
):
    monkeypatch.setattr(guest, "helper_status", lambda info, end: None)
    monkeypatch.setattr(guest, "session_login", lambda info, end: False)
    result = guest.station_session_ensure(account)
    assert result["ready"] is False and result["created"] is False
    assert result["failure"] == "login_failed"


@pytest.mark.parametrize("remaining,mounted", [([777], False), ([], True)])
def test_session_reset_requires_ssh_sessions_and_mount_to_end(
    account, tmp_path, monkeypatch, remaining, mounted,
):
    monkeypatch.setattr(guest, "LEASE_KEYS", tmp_path)
    monkeypatch.setattr(guest, "station_kill_sessions", lambda info: {"remaining": remaining})
    monkeypatch.setattr(guest, "unmount_drive", lambda info: None)
    monkeypatch.setattr(guest, "station_drive_mounted", lambda info: mounted)
    monkeypatch.setattr(guest, "deadline_command", lambda *a, **k: pytest.fail("ended GUI anyway"))
    with pytest.raises(RuntimeError, match="did not end" if remaining else "did not unmount"):
        guest.station_session_reset(account)


def test_screen_sharing_refuses_control_primary_group_membership(sharing, monkeypatch):
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=501, pw_gid=700))
    monkeypatch.setattr(guest.os, "getgrouplist", lambda user, gid: [gid])
    with pytest.raises(RuntimeError, match="control account must not belong"):
        guest.screen_sharing_ready(time.monotonic() + 5)


@pytest.mark.parametrize("field,value", [("ready", False), ("environment", "local")])
def test_session_ready_requires_the_vm_session_contract(account, field, value):
    state = ready_state(account)
    state[field] = value
    assert guest.session_ready(account, state) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS access control lists")
def test_a_credential_file_with_an_access_control_list_is_refused(tmp_path, monkeypatch):
    # Mode 0600 says nothing about an ACL entry that lets another account read the file.
    path = tmp_path / "password"
    path.write_text("synthetic-secret\n")
    path.chmod(0o600)
    original = os.fstat
    monkeypatch.setattr(guest.os, "fstat", lambda fd: SimpleNamespace(
        st_uid=0, st_mode=original(fd).st_mode, st_nlink=original(fd).st_nlink,
    ))
    os.close(guest.credential_fd(path, os.O_RDONLY))
    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(path)], check=True)
    with pytest.raises(RuntimeError, match="Unsafe station credential file"):
        guest.credential_fd(path, os.O_RDONLY)
    subprocess.run(["/bin/chmod", "-N", str(path)], check=True)
    os.close(guest.credential_fd(path, os.O_RDONLY))


def test_keep_awake_turns_the_lock_off_wherever_the_session_is(account, monkeypatch):
    # Lock settings are per host identity, so clones need them restated. A SecureToken
    # allows the change even when the station session is off console.
    prompts = []
    monkeypatch.setattr(guest, "station_password", lambda info: "synthetic-secret")
    monkeypatch.setattr(guest, "run_with_passwords",
                        lambda argv, secrets, enter, timeout: prompts.append((argv, secrets))
                        or (0, ""))
    monkeypatch.setattr(guest, "deadline_command", lambda argv, **kw: None)
    monkeypatch.setattr(guest, "station_awake_status", lambda info: {"checked": True})
    assert guest.station_keep_awake(account) == {"checked": True}
    expected = [(guest.account_command(503) + ["/usr/sbin/sysadminctl", "-screenLock", "off",
                                               "-password", "-"], ["synthetic-secret"])]
    assert prompts == expected


@pytest.fixture
def station_password(account, tmp_path, monkeypatch):
    """A station whose account password is whatever ``record.actual`` says."""
    path = tmp_path / "firmstation2"
    path.write_text("a" * 48 + "\n")
    path.chmod(0o600)
    kcpassword = tmp_path / "kcpassword"
    kcpassword.write_bytes(b"stale")
    kcpassword.chmod(0o600)
    monkeypatch.setattr(guest, "KCPASSWORD", kcpassword)
    original = os.fstat
    monkeypatch.setattr(guest.os, "fstat", lambda fd: SimpleNamespace(
        st_uid=0, st_mode=original(fd).st_mode, st_nlink=original(fd).st_nlink,
    ))
    record = SimpleNamespace(info={**account, "password": path}, path=path, kcpassword=kcpassword,
                             actual="a" * 48, change=True, automatic_login="firmvm", prompts=[])

    def prompted(argv, secrets, enter, timeout):
        record.prompts.append((argv, secrets))
        if "-authonly" in argv:
            return (0 if secrets == [record.actual] else 10), ""
        assert secrets[0] == record.actual
        if record.change:
            record.actual = secrets[1]
        return 0, ""

    monkeypatch.setattr(guest, "run_with_passwords", prompted)
    monkeypatch.setattr(guest, "deadline_command", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=record.automatic_login + "\n",
    ))
    return record


@pytest.mark.parametrize("typeable, shape", [(True, r"[0-9]{8}"), (False, r"[0-9a-f]{48}")])
def test_station_password_changes_by_self_service_and_is_never_in_arguments(
    station_password, typeable, shape,
):
    record = station_password
    assert guest.station_password_change(record.info, typeable) == {"typeable": typeable}
    new = record.path.read_text().strip()
    assert re.fullmatch(shape, new) and record.actual == new
    change = next(argv for argv, _ in record.prompts if "-newPassword" in argv)
    assert change == guest.account_command(503) + [
        "/usr/sbin/sysadminctl", "-newPassword", "-", "-oldPassword", "-",
    ]
    assert all(new not in " ".join(argv) for argv, _ in record.prompts)
    assert not record.path.with_name("firmstation2.pending").exists()
    # Automatic login names firmvm, so its kcpassword is not this station's to write.
    assert record.kcpassword.read_bytes() == b"stale"


def test_station_password_keeps_automatic_login_in_step_during_its_console_turn(station_password):
    record = station_password
    record.automatic_login = "firmstation2"
    guest.station_password_change(record.info, True)
    assert record.kcpassword.read_bytes() == guest.kcpassword(record.actual)


def test_station_password_that_did_not_change_keeps_the_old_one_and_its_pending_value(
    station_password,
):
    record = station_password
    record.change = False
    with pytest.raises(RuntimeError, match="did not take effect"):
        guest.station_password_change(record.info, True)
    assert record.path.read_text() == "a" * 48 + "\n"
    pending = record.path.with_name("firmstation2.pending").read_text().strip()
    # The next call finishes the pending change first, then makes the shape it was asked
    # for: a typeable password left by an interrupted turn never outlives the request.
    record.change = True
    guest.station_password_change(record.info, False)
    changes = [secrets for argv, secrets in record.prompts if "-newPassword" in argv]
    # The refused first attempt, the pending value taken up, then the long one asked for.
    assert [old for old, new in changes] == ["a" * 48, "a" * 48, pending]
    assert changes[1][1] == pending
    assert record.actual == record.path.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{48}", record.actual)
    assert not record.path.with_name("firmstation2.pending").exists()


def test_station_password_requires_a_boolean(station_password):
    with pytest.raises(ValueError, match="true or false"):
        guest.station_password_change(station_password.info, "yes")
