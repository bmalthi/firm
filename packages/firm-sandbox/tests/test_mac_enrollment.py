import importlib.util
import json
import os
import re
from types import SimpleNamespace

import pytest

from firm_sandbox.mac_enrollment import (
    AUTO_LOGIN_SCRIPT,
    install_helper,
    prepare,
)
from firm_sandbox.mac_station import ASSETS, DIRTY_MARKER, DIRTY_MARKER_CONTENT


@pytest.fixture
def setup(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "mac_station_prepare", ASSETS / "mac-station-prepare.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Path", lambda value: tmp_path / str(value).lstrip("/"))
    password = tmp_path / "Users/firmvm/password"
    password.parent.mkdir(parents=True)
    password.write_text("synthetic-control-password")
    pam = tmp_path / "etc/pam.d/su"
    pam.parent.mkdir(parents=True)
    pam.write_text("auth required pam_opendirectory.so\n")
    (tmp_path / "usr/local/firm/session").mkdir(parents=True)
    control = SimpleNamespace(pw_name="firmvm", pw_uid=501, pw_dir="/Users/firmvm")
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.pwd, "getpwall", lambda: [control])
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: (
        control if name == "firmvm" else SimpleNamespace(
            pw_name=name, pw_uid=501 + int(name.removeprefix("firmstation")),
            pw_dir=f"/Users/{name}",
        )
    ))
    calls, files = [], {}

    def run(*args):
        calls.append(args)
        if args[0] == "/usr/sbin/sysctl":
            return "1"
        if args[0] == "/usr/bin/dscl":
            return "GroupMembership: root firmvm"
        return ""

    def prompted(args, passwords):
        calls.append(tuple(args))
        if "-addUser" in args:
            user = args[args.index("-addUser") + 1]
            files[f"prompts:{user}"] = passwords
        elif "-secureTokenOn" in args:
            # sysadminctl asks for the administrator first, then the account.
            user = args[args.index("-secureTokenOn") + 1]
            saved = files[f"/var/db/firm-station/passwords/{user}"].decode().strip()
            assert passwords == ["synthetic-control-password", saved]
            assert saved not in args and "synthetic-control-password" not in args
        else:
            assert args[:3] == ["/usr/bin/dscl", ".", "-authonly"]
            saved = files[f"/var/db/firm-station/passwords/{args[3]}"].decode().strip()
            assert passwords == [saved]

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "prompted", prompted)
    monkeypatch.setattr(module, "token_status", lambda user: "Secure token is ENABLED for user")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, "private_write", lambda path, data: files.update({path: data}))
    monkeypatch.setattr(module.os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.os, "chown", lambda *args, **kwargs: None)
    return module, calls, files


def test_preparation_creates_numbered_accounts_without_faking_consent(setup, capsys):
    module, calls, files = setup
    module.prepare(2)
    creates = [call for call in calls if "-addUser" in call]
    assert [call[call.index("-addUser") + 1] for call in creates] == [
        "firmstation1", "firmstation2",
    ]
    assert [call[call.index("-UID") + 1] for call in creates] == ["502", "503"]
    for number, create in enumerate(creates, 1):
        assert "-admin" not in create
        assert create[create.index("-password") + 1] == "-"
        password = files[f"/var/db/firm-station/passwords/firmstation{number}"]
        assert re.fullmatch(rb"[a-f0-9]{48}\n", password)
        assert password.decode().strip() not in str(calls)
        assert files[f"prompts:firmstation{number}"] == [
            password.decode().strip(), "synthetic-control-password",
        ]
    assert files["/var/db/firm-station/passwords/firmstation1"] != files[
        "/var/db/firm-station/passwords/firmstation2"
    ]
    assert json.loads(files["/var/db/firm-station/stations.json"]) == {"stations": 2}
    assert files["/etc/kcpassword"] == module.kcpassword("synthetic-control-password")
    assert not any("tcc" in str(call).lower() or "DidSee" in str(call) for call in calls)
    output = capsys.readouterr().out
    assert "synthetic-control-password" not in output
    assert "Network Volumes consent on the console for every station" in output
    assert json.loads(output)["automatic_login"] == "firmvm"


def test_preparation_shares_homebrew_with_inheritable_acl(setup, monkeypatch, capsys):
    import stat

    module, calls, files = setup
    real_lstat = module.os.lstat

    def lstat(path, *args, **kwargs):
        if str(path).endswith("/opt/homebrew"):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", lstat)
    module.prepare(2)
    acl = next(call for call in calls if call[:3] == ("/bin/chmod", "-R", "+a"))
    assert "group:firmstations allow" in acl[3]
    assert "file_inherit,directory_inherit" in acl[3]
    assert "add_file,add_subdirectory,delete_child" in acl[3]
    assert acl[-1] == "/opt/homebrew"
    assert json.loads(capsys.readouterr().out)["homebrew"] == "/opt/homebrew"


def test_preparation_without_homebrew_changes_no_ownership(setup, capsys):
    module, calls, files = setup
    module.prepare(1)
    assert not any(call[0] == "/usr/sbin/chown" for call in calls)
    assert json.loads(capsys.readouterr().out)["homebrew"] is None


def test_preparation_scopes_screen_sharing_and_installs_private_login(setup):
    module, calls, files = setup
    module.prepare(2)
    deactivate = next(i for i, call in enumerate(calls) if "-deactivate" in call)
    enable = calls.index(("/bin/launchctl", "enable", "system/com.apple.screensharing"))
    assert deactivate < enable
    assert ("/usr/sbin/dseditgroup", "-o", "edit", "-a", "firmstations", "-t", "group",
            "com.apple.access_screensharing") in calls
    # macOS 27 lets not even root write the su policy, so preparation leaves it alone.
    assert module.Path("/etc/pam.d/su").read_text() == "auth required pam_opendirectory.so\n"
    assert ("/usr/local/firm/bin/uv", "sync", "--project", "/usr/local/firm/session",
            "--python", "/usr/local/firm/bin/python3", "--no-python-downloads") in calls
    assert '"vncdotool==1.4.2", "pycryptodomex==3.23.0"' in module.Path(
        "/usr/local/firm/session/pyproject.toml"
    ).read_text()


@pytest.mark.parametrize("owner, mode", [(501, 0o040755), (0, 0o120755)])
def test_preparation_refuses_a_homebrew_the_build_did_not_leave(setup, monkeypatch, owner, mode):
    module, calls, files = setup
    real_lstat = module.os.lstat
    monkeypatch.setattr(module.os, "lstat", lambda path, *a, **k: (
        SimpleNamespace(st_mode=mode, st_uid=owner) if str(path).endswith("/opt/homebrew")
        else real_lstat(path, *a, **k)
    ))
    with pytest.raises(RuntimeError, match="root-owned prefix"):
        module.prepare(1)
    assert not any(call[0] == "/usr/sbin/chown" for call in calls)


@pytest.mark.parametrize("count", [0, 9, True, "2", 1.5])
def test_preparation_rejects_invalid_count_before_mutation(setup, count):
    module, calls, files = setup
    with pytest.raises(ValueError, match="1 to 8"):
        module.prepare(count)
    assert not calls
    assert not files


def test_preparation_rejects_a_used_image_before_mutation(setup):
    module, calls, files = setup
    module.pwd.getpwall = lambda: [SimpleNamespace(pw_name="firmvm", pw_uid=501),
                                   SimpleNamespace(pw_name="firmstation1", pw_uid=502)]
    with pytest.raises(RuntimeError, match="only the control account"):
        module.prepare(2)
    assert not any("-addUser" in call for call in calls)
    assert not files


def test_private_enrollment_file_rejects_links_before_truncation(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "mac_station_prepare", ASSETS / "mac-station-prepare.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = tmp_path / "original"
    original.write_bytes(b"keep")
    link = tmp_path / "link"
    os.link(original, link)
    with pytest.raises(RuntimeError, match="Unsafe"):
        module.private_write(link, b"overwrite")
    assert original.read_bytes() == b"keep"
    link.unlink()
    link.symlink_to(original)
    with pytest.raises(OSError):
        module.private_write(link, b"overwrite")
    assert original.read_bytes() == b"keep"


def test_install_helper_refuses_any_station_with_unverified_cleanup(tmp_path):
    tmp_path.chmod(0o700)
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    calls = []
    vm = SimpleNamespace(
        root=tmp_path / "mac",
        firm_locks=tmp_path / "locks",
        stations=(1, 2),
        network=SimpleNamespace(ready=lambda: True),
        _running=lambda: True,
        _shell=lambda *args, **kwargs: calls.append(args),
        _lima=lambda *args, **kwargs: calls.append(args),
    )
    vm.root.mkdir(mode=0o700)
    dirty = vm.root / "stations/2" / DIRTY_MARKER
    dirty.parent.mkdir(parents=True)
    dirty.write_bytes(DIRTY_MARKER_CONTENT)
    with pytest.raises(RuntimeError, match="cleanup is unverified"):
        install_helper(vm, app)
    assert calls == []


class EnrollmentVM:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(mode=0o700)
        self.record = root / "vm.json"
        self.record.write_text(json.dumps({"stations": 0, "enrolled": False}))
        self.firm_locks = root.parent / "locks"
        self.network = SimpleNamespace(ready=lambda: True)
        self.login = self.console = "firmvm"
        self.reboots = 0
        self.rpcs = []

    def _state(self):
        return json.loads(self.record.read_text())

    @property
    def stations(self):
        return tuple(range(1, self._state()["stations"] + 1))

    def enrolled(self):
        return self._state()["enrolled"]

    def _running(self):
        return True

    def _shell(self, *args, **kwargs):
        if args[:2] == ("/usr/bin/defaults", "read"):
            return SimpleNamespace(stdout=(self.login + "\n").encode())
        if args[0] == "/usr/sbin/ioreg":
            # The window server's answer; an off-console session is listed too.
            import plistlib

            return SimpleNamespace(stdout=plistlib.dumps({"IOConsoleUsers": [
                {"kCGSSessionUserNameKey": "firmstation9", "kCGSSessionOnConsoleKey": False},
                {"kCGSSessionUserNameKey": self.console, "kCGSSessionOnConsoleKey": True},
            ]}))
        raise AssertionError(args)

    def _rpc(self, operation, **payload):
        if operation == "rotate-control-password":
            self.rpcs.append((operation, payload["typeable"]))
            return {"rotated": True, "typeable": payload["typeable"]}
        station = payload["station"]
        if operation == "station-password":
            self.rpcs.append((operation, station, payload["typeable"]))
            return {"typeable": payload["typeable"]}
        self.rpcs.append((operation, station))
        if operation == "station-session-ensure":
            return {
                "station": station, "user": f"firmstation{station}", "ready": True,
                "helper": {"ready": True},
            }
        assert operation in ("station-awake-status", "station-keep-awake")
        return {
            "screen_lock": "screenLock is off", "screen_saver_idle": 0,
            "relock_at_login": False,
            "power": {"displaysleep": 0, "sleep": 0, "disksleep": 0},
        }

    def down(self):
        return None

    def up(self):
        self.reboots += 1
        self.console = self.login


def test_prepare_walks_each_console_then_returns_to_firmvm(tmp_path, monkeypatch):
    import firm_sandbox.mac_enrollment as enrollment

    vm = EnrollmentVM(tmp_path / "mac")
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    recipe = SimpleNamespace(stations=2, sha256="recipe-hash")

    def install(vm, app, *, mode, stations=None):
        assert (mode, stations) == ("prepare", 2)
        state = vm._state()
        vm.record.write_text(json.dumps({**state, "stations": 2}))
        return {"stations": [], "next": "what the guest asks of the whole enrollment"}

    monkeypatch.setattr(enrollment, "install_helper", install)
    monkeypatch.setattr(
        enrollment, "_control_credentials",
        lambda vm, station=None: {
            "credentials_file": str(vm.root / "enrollment-credentials.txt"),
            "administrator": "firmvm",
        },
    )
    monkeypatch.setattr(
        enrollment, "_select_automatic_login", lambda vm, user: setattr(vm, "login", user)
    )

    first = prepare(vm, app, recipe)
    assert first["turn"] == 1 and first["automatic_login"] == "firmstation1"
    assert first["complete"] is False and "Network Volumes" in first["next"]
    # The guest's own account-creation report reaches the owner beside this turn's step.
    assert first["prepared"] == "what the guest asks of the whole enrollment"
    assert vm._state()["enrollment_progress"]["phase"] == "console"

    second = prepare(vm, app, recipe)
    assert second["turn"] == 2 and second["automatic_login"] == "firmstation2"
    assert "--station 1 off console" in second["next"] and "prepared" not in second

    third = prepare(vm, app, recipe)
    assert third["automatic_login"] == "firmvm" and third["complete"] is False
    assert "--station 2 off console" in third["next"]

    # su is closed to the station group on the last turn, when Remote Login can write
    # macOS's administrative files; a refusal is reported and the next call tries again.
    answers = iter([{"su_closed": False, "reason": "macOS refused the write (errno 1)"},
                    {"su_closed": True, "reason": None}])
    monkeypatch.setattr(enrollment, "_close_su", lambda vm: next(answers))
    final = prepare(vm, app, recipe)
    # Not complete until su is closed: seal would otherwise take an image without it.
    assert final["complete"] is False and final["automatic_login"] == "firmvm"
    assert final["su_closed"] is False and "full disk access" in final["next"]
    assert vm._state()["enrollment_progress"]["phase"] == "console"
    again = prepare(vm, app, recipe)
    assert again["complete"] is True and again["su_closed"] is True
    assert again["next"] == "Apply the recipe and seal the image."
    assert vm._state()["enrollment_progress"]["phase"] == "complete"
    assert vm._state()["enrollment_progress"]["su_closed"] is True
    assert vm.reboots == 3
    assert set(vm.rpcs) >= {
        ("station-session-ensure", 1), ("station-session-ensure", 2),
        ("station-awake-status", 1), ("station-awake-status", 2),
    }
    # The owner types at the console: the administrator's password is typeable for the
    # whole enrollment, each station's only for its own turn and private again after it.
    passwords = [call for call in vm.rpcs if call[0] in ("rotate-control-password",
                                                         "station-password")]
    assert passwords == [
        ("rotate-control-password", True),
        ("station-password", 1, True), ("station-password", 1, False),
        ("station-password", 2, True), ("station-password", 2, False),
    ]


def test_prepare_resumes_an_interrupted_account_selection_without_skipping_its_turn(
    tmp_path, monkeypatch
):
    import firm_sandbox.mac_enrollment as enrollment

    vm = EnrollmentVM(tmp_path / "mac")
    vm.record.write_text(json.dumps({
        "stations": 2,
        "enrolled": False,
        "enrollment_progress": {
            "stations": 2, "recipe_sha256": "recipe-hash", "completed": [1],
            "phase": "selecting", "target": "firmstation2",
        },
    }))
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    monkeypatch.setattr(
        enrollment, "_control_credentials",
        lambda vm, station=None: {"administrator": "firmvm"},
    )
    monkeypatch.setattr(
        enrollment, "_select_automatic_login", lambda vm, user: setattr(vm, "login", user)
    )
    result = prepare(vm, app, SimpleNamespace(stations=2, sha256="recipe-hash"))
    assert result["turn"] == 2 and result["complete"] is False
    assert vm._state()["enrollment_progress"] == {
        "stations": 2, "recipe_sha256": "recipe-hash", "completed": [1],
        "phase": "console", "target": "firmstation2",
    }
    # Repeating the selection repeats its typeable password; no session is touched.
    assert vm.rpcs == [("station-password", 2, True)]


def test_prepare_resumes_a_preparation_that_died_before_its_first_progress_record(
    tmp_path, monkeypatch
):
    import firm_sandbox.mac_enrollment as enrollment

    vm = EnrollmentVM(tmp_path / "mac")
    # Accounts exist and the helper install may not have finished: no progress was saved.
    vm.record.write_text(json.dumps({"stations": 2, "enrolled": False}))
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    installs = []
    monkeypatch.setattr(enrollment, "install_helper", lambda vm, app, *, mode, stations=None: (
        installs.append((mode, stations))
    ))
    monkeypatch.setattr(
        enrollment, "_control_credentials", lambda vm, station=None: {"administrator": "firmvm"}
    )
    monkeypatch.setattr(
        enrollment, "_select_automatic_login", lambda vm, user: setattr(vm, "login", user)
    )
    result = prepare(vm, app, SimpleNamespace(stations=2, sha256="recipe-hash"))
    assert installs == [("enroll", None)]
    assert result["turn"] == 1 and "prepared" not in result
    assert vm._state()["enrollment_progress"] == {
        "stations": 2, "recipe_sha256": "recipe-hash", "completed": [],
        "phase": "console", "target": "firmstation1",
    }


def test_prepare_refuses_to_enroll_a_clone_of_an_enrolled_image_again(tmp_path, monkeypatch):
    import firm_sandbox.mac_enrollment as enrollment

    vm = EnrollmentVM(tmp_path / "mac")
    # A production firm: stations from the sealed image, and no enrollment of its own.
    vm.record.write_text(json.dumps({"stations": 2, "enrolled": True}))
    app = tmp_path / "Firm Station Helper.app"
    app.mkdir()
    monkeypatch.setattr(enrollment, "install_helper", lambda *args, **kwargs: pytest.fail(
        "installed the helper on an enrolled firm"
    ))
    monkeypatch.setattr(
        enrollment, "_control_credentials", lambda vm, station=None: {"administrator": "firmvm"}
    )
    with pytest.raises(RuntimeError, match="already enrolled"):
        prepare(vm, app, SimpleNamespace(stations=2, sha256="recipe-hash"))
    assert vm.rpcs == [] and vm.reboots == 0
    assert "enrollment_progress" not in vm._state()


def test_automatic_login_script_reads_guest_secrets_and_never_accepts_one_in_argv():
    assert "/var/db/firm-station/passwords/{user}" in AUTO_LOGIN_SCRIPT
    assert "/Users/firmvm/password" in AUTO_LOGIN_SCRIPT
    assert "sys.argv[1]" in AUTO_LOGIN_SCRIPT
    assert "password=sys.argv" not in AUTO_LOGIN_SCRIPT
