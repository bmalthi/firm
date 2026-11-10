"""The Linux guest controller in simulation: privileged argv recorded, everything else real."""

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from firm_sandbox import Layout
from firm_sandbox import linux_vm_guest as guest

ROLE = "r-e1"
ACCOUNT = guest.role_account(ROLE)
UID = guest.allocate_identity(
    ROLE, account_prefix="firmrole-", uid_start=2000, uid_count=48000, used_uids=set()
)[1]
DRIVE = {"user": "firmfile-abc", "password": "pw-secret", "port": 445, "share": "firm"}


@pytest.fixture
def sim(tmp_path, monkeypatch):
    for name, attribute in (
        ("state", "STATE"), ("run", "RUN_ROOT"), ("home", "HOMES"), ("etc", "SUBID"),
        ("units", "UNITS"), ("cgroup", "CGROUP"), ("proc", "PROC"),
    ):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(guest, attribute, tmp_path / name)
    monkeypatch.setattr(guest, "LOGIND_CONF", tmp_path / "etc" / "logind.conf.d" / "firm.conf")
    monkeypatch.setattr(guest, "SMB_CONF", tmp_path / "etc" / "samba" / "smb.conf")
    (tmp_path / "proc/self").mkdir()
    (tmp_path / "proc/self/mountinfo").write_text("")
    monkeypatch.setattr(guest, "boot", lambda: "boot-1")
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)
    with guest.simulation() as privileged:
        privileged.replies[("/usr/bin/setpriv",)] = (0, json.dumps({
            "ContainersDeleted": [], "ImagesDeleted": [], "CachesDeleted": [], "SpaceReclaimed": 0,
        }))
        original = privileged.run

        def run(argv, **options):
            result = original(argv, **options)
            if argv[-1] == "http://localhost/containers/json" and result.returncode == 0:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            return result

        monkeypatch.setattr(privileged, "run", run)
        privileged.tree = tmp_path
        yield privileged


def user_systemctl(*args):
    return ["systemctl", "--user", "--machine", f"{ACCOUNT}@.host", *args]


def scope(*argv):
    return ["systemd-run", "--quiet", "--collect", "--scope", f"--slice=user-{UID}.slice",
            f"--uid={UID}", f"--gid={UID}", "--", *argv]


SETTLE = [
    user_systemctl("stop", "docker.service"),
    ["systemctl", "stop", f"user-{UID}.slice"],
]
PROVISION = [
    ["groupadd", "-g", str(UID), ACCOUNT],
    ["useradd", "-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0",
     "-M", "-d", "HOME", "-s", "/bin/bash", "-u", str(UID), "-g", str(UID), ACCOUNT],
    ["systemctl", "daemon-reload"],
    ["systemctl", "start", f"user@{UID}.service"],
    scope("dockerd-rootless-setuptool.sh", "install"),
    user_systemctl("disable", "docker.service"),
    *SETTLE,
    ["sync", "-f", "HOME"],
]


def provision_commands(sim):
    home = str(sim.tree / "home" / ACCOUNT)
    return [[home if item == "HOME" else item for item in argv] for argv in PROVISION]


def journal(run_id, request, record=None):
    directory = guest.run_path(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lock").touch()
    guest.save(directory / "request.json", request)
    guest.save(directory / "record.json", record or {"role_id": ROLE, "run_id": run_id})
    return directory


# ---- identity, ranges, units -----------------------------------------------------------


def test_file_server_identities_keep_their_names_and_ranges():
    account, uid = guest.allocate_identity(
        "firm:analyst", account_prefix="firmfile-", uid_start=50000, uid_count=10000,
        used_uids=set(),
    )
    assert (account, uid) == ("firmfile-81c7873a830f", 57146)
    _, probed = guest.allocate_identity(
        "firm:analyst", account_prefix="firmfile-", uid_start=50000, uid_count=10000,
        used_uids={None, 57146, 57147},
    )
    assert probed == 57148
    _, wrapped = guest.allocate_identity(
        "firm:analyst", account_prefix="firmfile-", uid_start=50000, uid_count=10000,
        used_uids=set(range(57146, 60000)) | {50000},
    )
    assert wrapped == 50001


def test_role_account_is_the_layout_home_and_a_valid_identifier():
    assert ACCOUNT.startswith("firmrole-") and len(ACCOUNT) == len("firmrole-") + 12
    assert Layout.for_role(ROLE).private == f"/home/{ACCOUNT}"
    assert Layout.for_role(ROLE).scratch == f"/home/{ACCOUNT}/scratch"
    with pytest.raises(ValueError):
        guest.role_account("../escape")


def test_slice_cgroup_follows_systemd_user_layout(sim):
    assert guest.slice_unit(UID) == f"user-{UID}.slice"
    assert guest.slice_cgroup(UID) == (
        sim.tree / "cgroup" / "user.slice" / f"user-{UID}.slice"
    )
    assert guest.slice_empty(UID)
    nested = guest.slice_cgroup(UID) / f"user@{UID}.service" / "docker.service"
    nested.mkdir(parents=True)
    (nested / "cgroup.procs").write_text("12\n34\n")
    (guest.slice_cgroup(UID) / "cgroup.procs").write_text("7\n")
    assert not guest.slice_empty(UID)  # PID files cannot establish recursive emptiness.
    events = guest.slice_cgroup(UID) / "cgroup.events"
    events.write_text("populated 1\nfrozen 0\n")
    assert not guest.slice_empty(UID)
    events.write_text("populated 0\nfrozen 0\n")
    assert guest.slice_empty(UID)


@pytest.mark.parametrize("events", ["", "frozen 0\n", "populated unknown\n", "populated 2\n"])
def test_slice_empty_rejects_missing_or_malformed_populated_signal(sim, events):
    root = guest.slice_cgroup(UID)
    root.mkdir(parents=True)
    (root / "cgroup.events").write_text(events)
    assert not guest.slice_empty(UID)


@pytest.mark.parametrize("missing_events", [False, True])
def test_slice_empty_rejects_inspection_failure(sim, monkeypatch, missing_events):
    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable cgroup")

    if missing_events:
        monkeypatch.setattr(Path, "stat", unreadable)
    else:
        monkeypatch.setattr(Path, "read_text", unreadable)
    assert not guest.slice_empty(UID)


def test_provision_reserves_identity_range_units_and_docker(sim):
    info = guest.provision(ROLE)
    home = sim.tree / "home" / ACCOUNT
    assert sim.commands == provision_commands(sim)
    assert info == {
        "role_id": ROLE, "account": ACCOUNT, "uid": UID, "gid": UID, "index": 0,
        "home": str(home), "drive": str(home / "drive"),
        "state": str(sim.tree / "run" / ROLE / "state"),
        "credentials": str(sim.tree / "run" / ROLE / "credentials"),
        "subordinate": [100000, 65536], "slice": f"user-{UID}.slice",
    }
    layout = Layout.for_role(ROLE)
    assert (info["home"], str(home / "work"), info["state"], info["credentials"]) == (
        layout.private.replace("/home", str(sim.tree / "home"), 1),
        layout.private_work.replace("/home", str(sim.tree / "home"), 1),
        layout.state.replace("/run/firm", str(sim.tree / "run"), 1),
        layout.credentials.replace("/run/firm", str(sim.tree / "run"), 1),
    )
    for path in (home, home / "work", home / "drive"):
        assert path.stat().st_mode & 0o777 == 0o700
        assert sim.owned[path.stat().st_ino] == (UID, UID)
    for name in ("subuid", "subgid"):
        assert (sim.tree / "etc" / name).read_text() == f"{ACCOUNT}:100000:65536\n"
    unit = sim.tree / "units" / f"user-{UID}.slice"
    assert unit.read_text() == f"[Unit]\nDescription=Firm role {ROLE}\n"
    assert not (sim.tree / "units" / f"user@{UID}.service.d").exists()
    assert unit.stat().st_mode & 0o777 == 0o644
    # The setup tool runs inside the slice as the account with its own environment.
    environment = sim.environments[4]
    assert environment["HOME"] == str(home) and environment["XDG_RUNTIME_DIR"] == f"/run/user/{UID}"
    assert environment["DOCKER_HOST"] == f"unix:///run/user/{UID}/docker.sock"
    data = guest.database()
    assert data["roles"][ROLE] == info and data["next"] == 1 and "pending" not in data
    sim.commands.clear()
    assert guest.provision(ROLE) == info
    assert sim.commands == []


def test_provision_does_not_commit_role_before_docker_install_is_durable(sim, monkeypatch):
    run = sim.run

    def fail_sync(argv, **kwargs):
        if argv[0] == "sync":
            assert ROLE not in guest.database()["roles"]
            raise subprocess.CalledProcessError(1, argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(sim, "run", fail_sync)
    with pytest.raises(subprocess.CalledProcessError):
        guest.provision(ROLE)
    assert ROLE not in guest.database()["roles"]
    assert guest.database()["pending"]["role_id"] == ROLE
    monkeypatch.setattr(sim, "run", run)
    assert guest.provision(ROLE)["role_id"] == ROLE


def test_second_role_gets_the_next_range_and_retired_slots_stay_reserved(sim):
    first = guest.provision(ROLE)
    second = guest.provision("r-e2")
    assert second["index"] == 1 and second["subordinate"] == [165536, 65536]
    assert second["uid"] != first["uid"] and second["account"] != first["account"]
    assert (sim.tree / "etc" / "subuid").read_text().splitlines() == [
        f"{ACCOUNT}:100000:65536", f"{second['account']}:165536:65536",
    ]
    guest.retire_role(ROLE)
    assert guest.database()["roles"][ROLE]["uid"] == UID
    third = guest.provision("r-e3")
    assert third["index"] == 2 and third["uid"] not in (first["uid"], second["uid"])
    with pytest.raises(ValueError, match="Retired"):
        guest.provision(ROLE)


def test_provision_is_two_phase_and_resumes_the_pending_role(sim, monkeypatch):
    original = guest.subordinate_range

    def interrupted(account, index):
        raise KeyboardInterrupt("synthetic guest controller interruption")

    monkeypatch.setattr(guest, "subordinate_range", interrupted)
    with pytest.raises(KeyboardInterrupt):
        guest.provision(ROLE)
    data = guest.database()
    assert data["pending"] == {"role_id": ROLE, "account": ACCOUNT, "uid": UID, "index": 0}
    assert ROLE not in data["roles"]
    with pytest.raises(RuntimeError, match="pending role first"):
        guest.provision("r-e2")
    monkeypatch.setattr(guest, "subordinate_range", original)
    sim.commands.clear()
    info = guest.provision(ROLE)
    assert info["uid"] == UID and info["index"] == 0
    # The account exists, so the resumed provision creates nothing twice.
    assert sim.commands == provision_commands(sim)[2:]
    assert "pending" not in guest.database()


def test_provision_rejects_foreign_reserved_uid_before_creating_account(sim):
    sim.accounts["unrelated"] = SimpleNamespace(
        pw_name="unrelated", pw_uid=UID, pw_gid=UID, pw_dir="/home/unrelated"
    )
    with pytest.raises(ValueError, match="unrelated account"):
        guest.provision(ROLE)
    assert sim.commands == []
    sim.accounts.clear()
    sim.accounts[ACCOUNT] = SimpleNamespace(
        pw_name=ACCOUNT, pw_uid=UID + 1, pw_gid=UID, pw_dir="/elsewhere"
    )
    with pytest.raises(ValueError, match="name is occupied"):
        guest.provision(ROLE)
    assert sim.commands == []


def test_subordinate_range_refuses_a_conflicting_existing_line(sim):
    (sim.tree / "etc" / "subuid").write_text(f"{ACCOUNT}:200000:65536\n")
    with pytest.raises(ValueError, match="another subordinate range"):
        guest.subordinate_range(ACCOUNT, 0)


@pytest.mark.parametrize("name", ["subuid", "subgid"])
@pytest.mark.parametrize("first,count", [(100000, 65536), (99999, 2), (165535, 2)])
@pytest.mark.parametrize("already_assigned", [False, True])
def test_subordinate_range_refuses_overlap_without_changing_either_file(
    sim, name, first, count, already_assigned
):
    own = f"{ACCOUNT}:100000:65536\n" if already_assigned else ""
    paths = [sim.tree / "etc" / item for item in ("subuid", "subgid")]
    for path in paths:
        path.write_text(own + (f"firmowner:{first}:{count}\n" if path.name == name else ""))
    before = [path.read_bytes() for path in paths]
    with pytest.raises(ValueError, match=f"overlaps firmowner in {name}"):
        guest.subordinate_range(ACCOUNT, 0)
    assert [path.read_bytes() for path in paths] == before


def test_subordinate_range_accepts_adjacent_ranges_and_resumes_partial_write(sim):
    neighbors = "before:34464:65536\nafter:165536:65536\n"
    own = f"{ACCOUNT}:100000:65536\n"
    uid = sim.tree / "etc" / "subuid"
    gid = sim.tree / "etc" / "subgid"
    uid.write_text(neighbors + own)
    gid.write_text(neighbors)
    for _ in range(2):
        assert guest.subordinate_range(ACCOUNT, 0) == [100000, 65536]
        assert uid.read_text() == gid.read_text() == neighbors + own


def test_initialize_binds_one_firm(sim):
    assert guest.dispatch({"operation": "initialize", "firm_id": "demo"})["firm_id"] == "demo"
    assert guest.dispatch({"operation": "info"})["firm_id"] == "demo"
    assert guest.LOGIND_CONF.read_text() == "[Login]\nUserStopDelaySec=infinity\n"
    assert sim.commands == [["systemctl", "reload", "systemd-logind"]]
    guest.initialize({"firm_id": "demo"})
    assert sim.commands == [["systemctl", "reload", "systemd-logind"]] * 2
    with pytest.raises(ValueError, match="another firm"):
        guest.initialize({"firm_id": "other"})
    with pytest.raises(ValueError):
        guest.initialize({"firm_id": "Bad Firm"})


def test_initialize_retries_logind_reload_after_failure(sim):
    reload = ("systemctl", "reload", "systemd-logind")
    sim.replies[reload] = (1, "Temporary reload failure")
    with pytest.raises(subprocess.CalledProcessError):
        guest.initialize({"firm_id": "demo"})
    assert guest.LOGIND_CONF.read_text() == "[Login]\nUserStopDelaySec=infinity\n"
    assert "firm_id" not in guest.database()
    del sim.replies[reload]
    assert guest.initialize({"firm_id": "demo"})["firm_id"] == "demo"
    assert sim.commands == [list(reload)] * 2
    assert guest.database()["firm_id"] == "demo"


# ---- launch -----------------------------------------------------------------------------


def test_start_reconciles_only_the_same_journaled_request(sim, monkeypatch):
    request = {"role_id": ROLE, "run_id": "run-1", "argv": ["/bin/echo", "ok"],
               "env": {"EXAMPLE": "1"}, "stdin": ""}
    journal("run-1", request, {"role_id": ROLE, "run_id": "run-1",
                               "fingerprint": guest.request_fingerprint(request)})
    monkeypatch.setattr(guest, "status", lambda _run_id: {"run_id": "run-1", "running": False})
    assert guest.start(request)["run_id"] == "run-1"
    request["argv"] = ["/bin/echo", "changed"]
    with pytest.raises(ValueError, match="run-1"):
        guest.start(request)
    assert sim.commands == []


@pytest.mark.parametrize(
    "change",
    [
        {"secrets": {"../escape": "eA=="}},
        {"secrets": {"token": "not base64"}},
        {"secret_env": {"TOKEN": "absent"}},
        {"secrets": {"token": "AA=="}, "secret_env": {"TOKEN": "token"}},
        {"secrets": {"token": "/w=="}, "secret_env": {"TOKEN": "token"}},
        {"argv": "not-an-argv-array"},
        {"stdin": "bad encoding"},
        {"drive": DRIVE},
        {"drive": {**DRIVE, "user": "a,b"}, "organization": "x"},
    ],
)
def test_invalid_execution_inputs_fail_before_provision(sim, monkeypatch, change):
    monkeypatch.setattr(guest, "provision", lambda _role: pytest.fail("provisioned invalid input"))
    with pytest.raises(ValueError):
        guest.start({"role_id": ROLE, "run_id": "run-1", "argv": ["/bin/true"], **change})


def test_secrets_and_drive_change_request_identity():
    request = {"role_id": ROLE, "argv": ["/bin/true"]}
    original = guest.request_fingerprint(request)
    for change in (
        {"organization": "ceo"},
        {"secrets": {"token": "eA=="}},
        {"secret_env": {"TOKEN": "token"}},
        {"drive": DRIVE},
    ):
        assert guest.request_fingerprint({**request, **change}) != original


def test_start_settles_the_slice_prepares_tmpfs_state_and_spawns_the_supervisor(sim):
    record = guest.start({"role_id": ROLE, "run_id": "run-1", "argv": ["/bin/true"]})
    assert sim.commands == provision_commands(sim) + SETTLE
    scratch = sim.tree / "home" / ACCOUNT / "scratch"
    assert scratch.is_dir() and scratch.stat().st_mode & 0o777 == 0o700
    assert sim.owned[scratch.stat().st_ino] == (UID, UID)
    assert record["supervisor"] == 4242 and record["phase"] == "launching"
    assert record["boot"] == "boot-1" and record["account"] == ACCOUNT
    ((argv, options),) = sim.spawned
    assert argv == ["/usr/bin/python3", "-I", "/usr/local/libexec/firm-controller.py",
                    "--supervise", "run-1"]
    assert options["start_new_session"] and options["stdin"] == subprocess.DEVNULL
    role_root = sim.tree / "run" / ROLE
    assert role_root.stat().st_mode & 0o777 == 0o711
    for name in ("state", "credentials"):
        assert (role_root / name).stat().st_mode & 0o777 == 0o700
        assert sim.owned[(role_root / name).stat().st_ino] == (UID, UID)
    assert json.loads((guest.run_path("run-1") / "request.json").read_text())["argv"] == [
        "/bin/true"
    ]
    assert not (sim.tree / "state" / "launch-staging").exists()


def test_start_refuses_a_second_run_while_one_is_active(sim, monkeypatch):
    guest.start({"role_id": ROLE, "run_id": "run-1", "argv": ["/bin/true"]})
    monkeypatch.setattr(guest, "status", lambda run_id: {"role_id": ROLE, "run_id": run_id,
                                                          "running": True})
    with pytest.raises(ValueError, match="active or unresolved run: run-1"):
        guest.start({"role_id": ROLE, "run_id": "run-2", "argv": ["/bin/true"]})


def test_failed_supervisor_launch_cleans_request_and_state(sim, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("synthetic-secret must not enter metadata")

    monkeypatch.setattr(sim, "spawn", fail)
    with pytest.raises(RuntimeError, match="Supervisor launch failed") as error:
        guest.start({
            "role_id": ROLE, "run_id": "run-1", "argv": ["/bin/true"],
            "secrets": {"token": base64.b64encode(b"synthetic-secret").decode()},
        })
    assert "synthetic-secret" not in str(error.value)
    assert not (sim.tree / "run" / ROLE).exists()
    directory = guest.run_path("run-1")
    assert not (directory / "request.json").exists()
    assert "synthetic-secret" not in (directory / "record.json").read_text()
    assert guest.status("run-1")["status"] == "launch_failed"


def test_interrupted_journal_publication_is_retryable(sim, monkeypatch):
    request = {"role_id": ROLE, "run_id": "run-crash", "argv": ["/bin/true"]}
    original_save = guest.save

    def interrupt_record(path, value):
        if path.name == "record.json":
            raise KeyboardInterrupt("synthetic guest controller interruption")
        return original_save(path, value)

    monkeypatch.setattr(guest, "save", interrupt_record)
    with pytest.raises(KeyboardInterrupt):
        guest.start(request)
    assert not guest.run_path("run-crash").exists()
    assert not (sim.tree / "state" / "launch-staging").exists()
    assert guest.runs() == []
    monkeypatch.setattr(guest, "save", original_save)
    assert guest.start(request)["run_id"] == "run-crash"


@pytest.mark.parametrize("interrupt_at", ["record.json", "request.json"])
def test_killed_guest_controller_leaves_no_visible_partial_run(sim, monkeypatch, interrupt_at):
    script = f"""
import os, sys
from pathlib import Path
from firm_sandbox import linux_vm_guest as guest
guest.STATE = Path(sys.argv[1])
guest.RUN_ROOT = Path(sys.argv[1]) / "run"
guest.provision = lambda role: {{"role_id": role, "account": "test", "uid": {UID},
                                 "gid": {UID}}}
guest.settle = lambda info: None
guest.prepare_run_state = lambda info: None
guest.heal_scratch = lambda info: None
guest.boot = lambda: "boot"
save = guest.save
def interrupted(path, value):
    if path.name == sys.argv[2]:
        os._exit(77)
    return save(path, value)
guest.save = interrupted
guest.start({{"role_id": "analyst", "run_id": "run-killed", "argv": ["true"]}})
"""
    result = subprocess.run([sys.executable, "-c", script, str(guest.STATE), interrupt_at])
    assert result.returncode == 77
    assert (guest.STATE / "launch-staging").is_dir()
    assert guest.runs() == []
    assert not guest.run_path("run-killed").exists()
    # A retry sweeps unpublished staging and reaches normal launch handling.
    assert guest.start({"role_id": ROLE, "run_id": "run-killed", "argv": ["true"]})["phase"] == (
        "launching"
    )
    assert not (guest.STATE / "launch-staging").exists()


# ---- status -----------------------------------------------------------------------------


def test_boot_change_interrupts_the_run_and_drops_its_request(sim, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE}, {"role_id": ROLE, "run_id": "run-1",
                                                       "boot": "boot-0", "supervisor": 4242})
    state = guest.status("run-1")
    assert state["status"] == "interrupted" and state["exit_code"] == 125
    assert not (directory / "request.json").exists()
    assert sim.commands == []
    # A stop may have recorded evidence before power-off prevented exit publication.
    receipt = {"usage": {"cpu_usec": 42}, "error": None}
    guest.save(directory / "settlement.json", receipt)
    interrupted = guest.status("run-1")
    assert interrupted["usage"] == receipt["usage"]
    assert "reclaim" not in interrupted


def test_supervisor_identity_is_verified_before_a_run_counts_as_running(sim, monkeypatch):
    journal("run-1", {"role_id": ROLE}, {"role_id": ROLE, "run_id": "run-1", "boot": "boot-1",
                                          "supervisor": os.getpid()})
    sim.replies[("ps",)] = (0, f"/usr/bin/python3 -I {guest.CONTROLLER} --supervise run-1\n")
    assert guest.status("run-1")["status"] == "running"
    assert sim.commands[-1] == ["ps", "-p", str(os.getpid()), "-o", "args="]
    sim.replies[("ps",)] = (0, f"/usr/bin/python3 -I {guest.CONTROLLER} --supervise run-2\n")
    state = guest.status("run-1")
    assert state["status"] == "unresolved" and "identity" in state["error"]
    sim.replies[("ps",)] = (1, "")
    assert guest.status("run-1")["unresolved"] is True


def test_status_reports_the_log_size_in_bytes(sim, provisioned):
    directory = journal(
        "run-1", {"role_id": ROLE, "argv": ["/bin/true"]},
        {"role_id": ROLE, "run_id": "run-1", "boot": "boot-1"},
    )
    assert guest.status("run-1")["log_bytes"] == 0
    sim.processes.append(SimpleNamespace(pid=77, stdout=io.BytesIO(b"hello world"), wait=lambda: 0))
    guest.supervise("run-1")
    assert (directory / "log").read_bytes() == b"hello world"
    assert guest.status("run-1")["log_bytes"] == len(b"hello world")


def test_logs_are_empty_until_supervisor_creates_the_file(sim, monkeypatch):
    journal("run-1", {"role_id": ROLE}, {"run_id": "run-1", "role_id": ROLE, "boot": "boot-1"})
    monkeypatch.setattr(guest, "status", lambda _run_id: {"running": False})
    result = guest.dispatch({"operation": "logs", "run_id": "run-1"})
    assert base64.b64decode(result["data"]) == b""
    with pytest.raises(ValueError, match="log range"):
        guest.dispatch({"operation": "logs", "run_id": "run-1", "offset": -1})


# ---- supervise ------------------------------------------------------------------------


@pytest.fixture
def provisioned(sim):
    info = guest.provision(ROLE)
    guest.prepare_run_state(info)
    sim.commands.clear()
    sim.environments.clear()
    return info


def test_supervise_mounts_starts_services_runs_in_the_slice_settles_and_unmounts(
    sim, provisioned
):
    request = {
        "role_id": ROLE, "argv": ["/bin/sh", "-c", "work"], "env": {"EXAMPLE": "1"},
        "organization": "founder/dev", "drive": DRIVE,
        "secrets": {"token": base64.b64encode(b"synthetic-secret").decode()},
        "secret_env": {"TOKEN": "token"},
    }
    directory = journal("run-1", request)
    sim.processes.append(SimpleNamespace(pid=77, stdout=io.BytesIO(b"hello\n"), wait=lambda: 3))
    guest.supervise("run-1")
    assert sim.commands == [
        ["mount.cifs", "//127.0.0.1/firm", provisioned["drive"], "-o",
         f"uid={UID},gid={UID},{guest.CIFS_OPTIONS}"],
        ["systemctl", "start", f"user@{UID}.service"],
        user_systemctl("start", "docker.service"),
        *SETTLE,
        ["umount", "-f", provisioned["drive"]],
    ]
    mount_environment = sim.environments[0]
    assert mount_environment["PASSWD"] == "pw-secret" and mount_environment["USER"] == DRIVE["user"]
    assert "pw-secret" not in " ".join(sim.commands[0])
    ((argv, options),) = sim.spawned
    assert argv == scope("/bin/sh", "-c", "work")
    assert options["cwd"] == f"{provisioned['drive']}/org/founder/dev/work"
    environment = options["env"]
    assert environment["TOKEN"] == "synthetic-secret" and environment["EXAMPLE"] == "1"
    assert environment["FIRM_CREDENTIALS_DIR"] == provisioned["credentials"]
    assert environment["FIRM_STATE_DIR"] == provisioned["state"]
    assert environment["FIRM_RUN_ID"] == "run-1"
    assert environment["DOCKER_HOST"] == f"unix:///run/user/{UID}/docker.sock"
    assert environment["HOME"] == provisioned["home"] and environment["USER"] == ACCOUNT
    assert (directory / "log").read_bytes() == b"hello\n"
    exit_record = json.loads((directory / "exit.json").read_text())
    assert exit_record["status"] == "exited" and exit_record["exit_code"] == 3
    assert exit_record["unresolved"] is False
    # This simulated slice has no cgroup, so its usage is unknown rather than zero.
    assert exit_record["usage"] is None
    assert not (directory / "request.json").exists() and not (directory / "stdin").exists()
    # Settlement removed the tmpfs state; the credential never outlives the run.
    assert not (sim.tree / "run" / ROLE).exists()


def test_the_rootless_daemon_pulls_through_the_runs_proxy_without_it_in_argv(sim, provisioned):
    url = "http://r-e1:synthetic-token@192.168.5.2:23456"
    request = {
        "role_id": ROLE, "argv": ["/bin/true"],
        "env": {"NO_PROXY": "localhost,127.0.0.1,::1", "UNRELATED": "1"},
        "secrets": {"egress-proxy": base64.b64encode(url.encode()).decode()},
        "secret_env": {"HTTPS_PROXY": "egress-proxy", "http_proxy": "egress-proxy"},
    }
    journal("run-1", request)
    guest.supervise("run-1")
    imported = user_systemctl("import-environment", "HTTPS_PROXY", "NO_PROXY", "http_proxy")
    commands = sim.commands
    assert commands[:3] == [
        ["systemctl", "start", f"user@{UID}.service"], imported,
        user_systemctl("start", "docker.service"),
    ]
    environment = sim.environments[sim.commands.index(imported)]
    assert environment["HTTPS_PROXY"] == environment["http_proxy"] == url
    assert environment["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert all(url not in " ".join(argv) for argv in sim.commands)
    assert sim.spawned[0][1]["env"]["HTTPS_PROXY"] == url


def test_containers_and_builds_get_the_proxy_from_the_roles_docker_settings(sim, provisioned):
    url = "http://r-e1:synthetic-token@192.168.5.2:23456"
    home = Path(provisioned["home"])
    (home / ".docker").mkdir(mode=0o700)
    (home / ".docker" / "config.json").write_text(json.dumps({
        "auths": {"ghcr.io": {"auth": "kept"}},
        "proxies": {"registry.example": {"httpsProxy": "http://other:1"}},
    }))
    request = {
        "role_id": ROLE, "argv": ["/bin/true"],
        "env": {"NO_PROXY": "localhost,127.0.0.1,::1"},
        "secrets": {"egress-proxy": base64.b64encode(url.encode()).decode()},
        "secret_env": {"HTTPS_PROXY": "egress-proxy", "HTTP_PROXY": "egress-proxy"},
    }
    journal("run-1", request)
    guest.supervise("run-1")
    config_path = home / ".docker" / "config.json"
    assert json.loads(config_path.read_text()) == {
        "auths": {"ghcr.io": {"auth": "kept"}},
        "proxies": {
            "registry.example": {"httpsProxy": "http://other:1"},
            "default": {"httpProxy": url, "httpsProxy": url,
                        "noProxy": "localhost,127.0.0.1,::1"},
        },
    }
    daemon_path = home / ".config" / "docker" / "daemon.json"
    assert json.loads(daemon_path.read_text()) == {"dns": ["127.0.0.1"]}
    for path in (config_path, daemon_path):
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert sorted(entry.name for entry in (home / ".docker").iterdir()) == ["config.json"]
    assert all("synthetic-token" not in " ".join(argv) for argv in sim.commands)
    # A task's HOME is its own; DOCKER_CONFIG still leads its docker to the role's settings.
    assert sim.spawned[0][1]["env"]["DOCKER_CONFIG"] == f"{home}/.docker"


def test_a_run_without_a_proxy_leaves_docker_settings_alone(sim, provisioned):
    journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    guest.supervise("run-1")
    assert not (Path(provisioned["home"]) / ".docker").exists()


def test_a_plain_run_works_in_its_private_work_without_a_mount(sim, provisioned):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    guest.supervise("run-1")
    assert sim.commands == [
        ["systemctl", "start", f"user@{UID}.service"],
        user_systemctl("start", "docker.service"),
        *SETTLE,
    ]
    assert sim.spawned[0][1]["cwd"] == provisioned["home"] + "/work"
    assert json.loads((directory / "exit.json").read_text())["status"] == "exited"


def test_settlement_with_processes_left_in_the_slice_is_unresolved(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    cgroup = guest.slice_cgroup(UID) / "user@x.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text("")
    (guest.slice_cgroup(UID) / "cgroup.events").write_text("populated 1\n")
    now = [0.0]
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    guest.supervise("run-1")
    exit_record = json.loads((directory / "exit.json").read_text())
    assert exit_record["status"] == "unresolved" and exit_record["unresolved"] is True
    assert "emptiness could not be confirmed" in exit_record["cleanup_error"]
    assert guest.status("run-1")["unresolved"] is True


def test_daemon_stop_failure_does_not_skip_the_slice_stop(sim, provisioned):
    sim.replies[("systemctl", "--user")] = (1, "Failed to connect to bus")
    guest.settle(provisioned)
    assert sim.commands == SETTLE


def write_slice_usage(peak="734003200", cpu="9500000", kills="2"):
    cgroup = guest.slice_cgroup(UID)
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.peak").write_text(peak + "\n")
    (cgroup / "cpu.stat").write_text(f"usage_usec {cpu}\nuser_usec 1\n")
    (cgroup / "memory.events").write_text(f"low 0\noom_kill {kills}\n")
    (cgroup / "cgroup.events").write_text("populated 0\n")
    return cgroup


def test_settlement_reads_the_slice_usage_before_stopping_it(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    cgroup = write_slice_usage()
    run = sim.run

    def stopping(argv, **options):
        if argv[:2] == ["systemctl", "stop"]:
            shutil.rmtree(cgroup)  # systemd removes the slice's cgroup with its unit
        return run(argv, **options)

    monkeypatch.setattr(sim, "run", stopping)
    guest.settle_run(provisioned, directory)
    assert json.loads((directory / "settlement.json").read_text())["usage"] == {
        "memory_peak_bytes": 734003200, "cpu_usec": 9500000, "oom_kills": 2,
    }
    assert guest.slice_usage(UID) is None


def test_slice_usage_is_unknown_without_a_cgroup_or_a_readable_counter(sim, provisioned):
    assert guest.slice_usage(UID) is None
    cgroup = guest.slice_cgroup(UID)
    cgroup.mkdir(parents=True)
    (cgroup / "memory.peak").write_text("max\n")
    assert guest.slice_usage(UID) == {
        "memory_peak_bytes": None, "cpu_usec": None, "oom_kills": None
    }


def test_disposal_waits_for_stop_and_recursive_empty_before_scratch(
    sim, provisioned, monkeypatch,
):
    root = guest.slice_cgroup(UID)
    root.mkdir(parents=True)
    events = root / "cgroup.events"
    events.write_text("populated 1\n")
    now, stopped, cleaned = [0.0], [], []
    run = sim.run

    def stopping(argv, **options):
        if argv == SETTLE[-1]:
            assert "--no-block" not in argv and options["timeout"] == guest.SETTLE_TIMEOUT
            now[0] += 3  # The stop job itself is waited on inside the settlement budget.
            stopped.append(True)
        return run(argv, **options)

    def wait(seconds):
        assert stopped and not cleaned
        now[0] += seconds
        events.write_text("populated 0\n")

    def cleanup(info, deadline):
        assert stopped and guest.slice_empty(UID) and now[0] > 3
        assert deadline == now[0] + guest.DISPOSE_SCRATCH_TIMEOUT
        cleaned.append(True)
        return {**guest.reclaim_counts(), "completed": True}

    monkeypatch.setattr(sim, "run", stopping)
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest.time, "sleep", wait)
    monkeypatch.setattr(guest, "scratch_worker", cleanup)
    monkeypatch.setattr(guest.os, "walk", lambda *a: pytest.fail("must not walk cgroups"))
    guest.settle(provisioned, reclaim={})
    assert cleaned == [True]


@pytest.mark.parametrize("failure", ["timeout", "error", "deadline"])
def test_failed_slice_stop_never_authorizes_scratch(sim, provisioned, monkeypatch, failure):
    now = [0.0]
    run = sim.run

    def stopping(argv, **options):
        if argv == SETTLE[-1]:
            assert 0 < options["timeout"] <= guest.SETTLE_TIMEOUT - now[0]
            if failure == "timeout":
                now[0] += options["timeout"]
                raise subprocess.TimeoutExpired(argv, options["timeout"])
            if failure == "error":
                raise subprocess.CalledProcessError(1, argv)
            now[0] = guest.SETTLE_TIMEOUT
        return run(argv, **options)

    monkeypatch.setattr(sim, "run", stopping)
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest, "scratch_worker", lambda *a: pytest.fail("unsafe scratch cleanup"))
    assert guest.slice_empty(UID)  # Absence alone cannot make a failed stop safe.
    with pytest.raises(RuntimeError, match="Role slice"):
        guest.settle(provisioned, reclaim={})


def test_unreadable_events_leave_run_unresolved_until_retry(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["true"]})
    root = guest.slice_cgroup(UID)
    root.mkdir(parents=True)  # Missing events on an existing cgroup is an inspection failure.
    scratch_file = Path(provisioned["home"]) / "scratch" / "keep-until-empty"
    scratch_file.write_text("data")
    now = [0.0]
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    guest.supervise("run-1")
    assert json.loads((directory / "exit.json").read_text())["unresolved"]
    assert scratch_file.exists()
    assert sim.commands[-1] == SETTLE[-1]
    (root / "cgroup.events").write_text("populated 0\n")
    assert not guest.stop_run("run-1")["unresolved"]
    assert scratch_file.read_text() == "data"


def test_stop_hands_the_usage_it_read_to_the_exit_journal(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]},
                        {"role_id": ROLE, "run_id": "run-1", "boot": "boot-1"})
    cgroup = write_slice_usage(peak="1048576", cpu="42", kills="0")
    states = iter([{"role_id": ROLE, "running": True}] + [{"role_id": ROLE, "running": False}] * 2)
    monkeypatch.setattr(guest, "status", lambda _run_id: next(states))
    guest.stop_run("run-1")
    expected = {"memory_peak_bytes": 1048576, "cpu_usec": 42, "oom_kills": 0}
    assert json.loads((directory / "settlement.json").read_text())["usage"] == expected
    # The supervisor reaches its settlement after the stop emptied the slice, so the
    # handed-over reading is the only one the run has.
    shutil.rmtree(cgroup)
    guest.supervise("run-1")
    assert json.loads((directory / "exit.json").read_text())["usage"] == expected


def test_scratch_reclaim_keeps_home_work_tools_sessions_and_link_targets(sim, provisioned):
    home = Path(provisioned["home"])
    scratch = home / "scratch"
    for name in ("work/code", ".firm/claude/projects/transcript", ".cache/uv/wheel",
                 ".local/bin/tool", ".npm/package", ".local/share/docker/volumes/data"):
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    (scratch / "build").mkdir()
    (scratch / "build/output").write_bytes(b"build bytes")
    (scratch / "link").symlink_to(home / "work", target_is_directory=True)
    os.link(home / "work/code", scratch / "hardlink")
    report = guest.reclaim_scratch(provisioned, guest.time.monotonic() + 10)
    assert report["completed"] and not report["skipped"]
    assert report["count"] == 5  # file, build directory, two links, old scratch
    assert report["bytes"] == len(b"build bytes") + len(str(home / "work"))
    assert list(scratch.iterdir()) == []
    assert scratch.stat().st_mode & 0o777 == 0o700
    assert sim.owned[scratch.stat().st_ino] == (UID, UID)
    assert all(path.read_text() == "keep" for path in home.rglob("*") if path.is_file())


def test_scratch_chain_beyond_recursion_limit_uses_bounded_descriptors(
    sim, provisioned, monkeypatch,
):
    scratch = Path(provisioned["home"]) / "scratch"
    depth = sys.getrecursionlimit() + guest.SCRATCH_MAX_DEPTH + 10
    fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    opened, iterators, peaks = set(), set(), []
    real_open, real_close, real_scandir = os.open, os.close, os.scandir

    def track():
        peaks.append(len(opened) + len(iterators))
        assert peaks[-1] <= 2 * guest.SCRATCH_MAX_DEPTH + 1

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        track()
        return fd

    def tracked_close(fd):
        real_close(fd)
        opened.remove(fd)

    class Scan:
        def __init__(self, fd):
            self.iterator = real_scandir(fd)
            iterators.add(self)
            track()

        def __next__(self):
            return next(self.iterator)

        def close(self):
            self.iterator.close()
            iterators.discard(self)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        patch.setattr(os, "close", tracked_close)
        patch.setattr(os, "scandir", Scan)
        report = guest.reclaim_scratch(provisioned, guest.time.monotonic() + 30)
    assert report["completed"] and report["count"] == depth + 1
    assert not opened and not iterators and max(peaks) == 2 * guest.SCRATCH_MAX_DEPTH + 1
    assert list(scratch.iterdir()) == []


def test_large_scratch_tree_makes_progress_across_deadlines(sim, provisioned, monkeypatch):
    scratch = Path(provisioned["home"]) / "scratch"
    total = 0
    for directory in range(20):
        folder = scratch / str(directory)
        folder.mkdir()
        for item in range(100):
            (folder / str(item)).write_bytes(b"x")
            total += 1
    now = [0.0]
    unlink = os.unlink

    def costly_unlink(*args, **kwargs):
        unlink(*args, **kwargs)
        now[0] += 0.01  # Deterministic filesystem latency, not a wall-clock benchmark.

    monkeypatch.setattr(os, "unlink", costly_unlink)
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    removed, size, attempts = 0, 0, 0
    while attempts < 30:
        report = guest.reclaim_scratch(provisioned, now[0] + 1)
        removed += report["count"]
        size += report["bytes"]
        attempts += 1
        if report["completed"]:
            break
        assert report["timed_out"] and report["count"] > 0
        assert report["skipped"] == {"deadline": 1}
    assert 1 < attempts < 30 and report["completed"]
    assert removed == total + 20 + 1 and size == total
    assert list(scratch.iterdir()) == []


def test_scratch_regular_file_is_replaced(sim, provisioned):
    scratch = Path(provisioned["home"]) / "scratch"
    scratch.rmdir()
    scratch.write_bytes(b"discard")
    report = guest.reclaim_scratch(provisioned, guest.time.monotonic() + 10)
    assert report["completed"] and report["count"] == 1 and report["bytes"] == 7
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("obstacle", ["mount", "deadline"])
def test_scratch_refuses_unsafe_or_expired_work(sim, provisioned, obstacle):
    home = Path(provisioned["home"])
    scratch = home / "scratch"
    (home / "work/precious").write_text("keep")
    if obstacle == "mount":
        (guest.PROC / "self/mountinfo").write_text(
            f"1 2 0:1 / {scratch}/bind rw - ext4 /dev/disk rw\n"
        )
    deadline = 0 if obstacle == "deadline" else guest.time.monotonic() + 10
    report = guest.reclaim_scratch(provisioned, deadline)
    assert not report["completed"] and report["skipped"] == {obstacle: 1}
    assert (home / "work/precious").read_text() == "keep"


def test_docker_prune_reports_scoped_categories(sim, provisioned, monkeypatch):
    replies = iter([
        {"ContainersDeleted": ["container"], "SpaceReclaimed": 123},
        {"ImagesDeleted": [{"Untagged": "image"}, {"Deleted": "sha256:image"},
                           {"Deleted": "sha256:layer"}], "SpaceReclaimed": 456},
        {"CachesDeleted": ["build"], "SpaceReclaimed": 789},
    ])
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(stdout=("[]" if argv[-1].endswith("containers/json")
                                       else json.dumps(next(replies))))

    monkeypatch.setattr(sim, "run", run)
    result = guest.reclaim_docker(provisioned, guest.time.monotonic() + 10)
    assert {name: (r["count"], r["bytes"]) for name, r in result.items()} == {
        "docker_containers": (1, 123),
        "docker_image_deletion_records": (3, 456),
        "docker_build_cache": (1, 789),
    }
    assert all(r["completed"] and not r["skipped"] for r in result.values())
    assert [unquote(argv[-1]) for argv, _ in calls] == [
        "http://localhost/containers/json",
        "http://localhost/containers/prune",
        'http://localhost/images/prune?filters={"dangling":["true"]}',
        "http://localhost/build/prune?all=false",
    ]
    for argv, options in calls:
        assert argv[:4] == ["/usr/bin/setpriv", f"--reuid={UID}", f"--regid={UID}",
                            "--clear-groups"]
        assert f"/run/user/{UID}/docker.sock" in argv
        assert options["timeout"] <= guest.DOCKER_PRUNE_BUDGET
        assert options["env"]["HOME"] == provisioned["home"]


def test_reclaim_and_daemon_timeouts_leave_time_for_slice_stop(sim, provisioned, monkeypatch):
    now = [0.0]
    calls = []

    def run(argv, **options):
        calls.append(argv)
        if argv[0] == "/usr/bin/setpriv" or "--user" in argv:
            now[0] += options["timeout"]
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(sim, "run", run)
    reclaim = {}
    guest.settle(provisioned, reclaim=reclaim)
    assert now[0] == guest.DOCKER_PRUNE_BUDGET + 2
    assert calls[-1] == SETTLE[-1]
    for name, report in reclaim["categories"].items():
        if name != "scratch":
            assert report["timed_out"] and report["count"] is None
    assert reclaim["categories"]["scratch"]["completed"]


def test_scratch_worker_is_killed_at_its_deadline(sim, provisioned, monkeypatch, tmp_path):
    worker = tmp_path / "blocked-worker.py"
    worker.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(sim, "simulate", False)
    monkeypatch.setattr(guest, "PYTHON", sys.executable)
    monkeypatch.setattr(guest, "CONTROLLER", str(worker))
    started = guest.time.monotonic()
    report = guest.scratch_worker(provisioned, started + guest.SCRATCH_REPORT_MARGIN + 0.2)
    assert guest.time.monotonic() - started < 2
    assert report == guest.reclaim_skipped("deadline", timed_out=True)


def test_scratch_worker_receives_remaining_budget_with_reporting_margin(
    sim, provisioned, monkeypatch,
):
    monkeypatch.setattr(sim, "simulate", False)
    monkeypatch.setattr(guest.time, "monotonic", lambda: 8.0)
    calls = []

    def run(argv, **options):
        calls.append(options)
        return SimpleNamespace(stdout=json.dumps({**guest.reclaim_counts(), "completed": True}))

    monkeypatch.setattr(sim, "run", run)
    assert guest.scratch_worker(provisioned, 20)["completed"]
    assert calls[0]["timeout"] == 12 - guest.SCRATCH_REPORT_MARGIN
    assert json.loads(calls[0]["input"])["deadline"] == 20 - guest.SCRATCH_REPORT_MARGIN - 0.1


def test_scratch_symlink_is_replaced_without_touching_target(sim, provisioned):
    scratch = Path(provisioned["home"]) / "scratch"
    target = Path(provisioned["home"]) / "work"
    precious = target / "precious"
    precious.write_text("keep")
    scratch.rmdir()
    scratch.symlink_to(target, target_is_directory=True)
    result = guest.reclaim_scratch(provisioned, guest.time.monotonic() + 10)
    assert result["completed"] and not result["skipped"]
    assert list(scratch.iterdir()) == []
    assert precious.read_text() == "keep"
    assert scratch.is_dir() and not scratch.is_symlink()


# ---- resource sampling -----------------------------------------------------------------


def test_sample_reports_vm_wide_counters(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text("boot-9\n")
    (proc / "meminfo").write_text(
        "MemTotal:       24000000 kB\nMemAvailable:    9000000 kB\nHugePages_Total:       0\n"
    )
    (proc / "pressure" / "memory").write_text(
        "some avg10=0.00 avg60=0.00 total=1500\nfull avg10=0.00 total=700\n"
    )
    (proc / "pressure" / "cpu").write_text("some avg10=0.00 total=99\n")
    (proc / "stat").write_text("cpu  100 0 50 900 10 0 0 0 0 0\ncpu0 1 2 3 4 5\n")
    (proc / "vmstat").write_text("nr_free_pages 100\noom_kill 2\npswpout 5\n")
    monkeypatch.setattr(guest, "PROC", proc)
    monkeypatch.setattr(guest.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        guest.os, "statvfs",
        lambda path: SimpleNamespace(f_blocks=1000, f_bfree=400, f_frsize=4096),
    )
    assert guest.sample() == {
        "boot": "boot-9",
        "cpus": 8,
        "memory_total_bytes": 24000000 * 1024,
        "memory_available_bytes": 9000000 * 1024,
        "memory_stall_usec": 1500,
        "memory_full_stall_usec": 700,
        "cpu_stall_usec": 99,
        "cpu_usec": 1_500_000,
        "oom_kills": 2,
        "swapouts": 5,
        "disk_used_bytes": 600 * 4096,
        "disk_total_bytes": 1000 * 4096,
    }


def test_sample_reports_unreadable_counters_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, "PROC", tmp_path / "proc")
    monkeypatch.setattr(guest, "boot", lambda: "boot-9")
    monkeypatch.setattr(guest, "ROOT_FILESYSTEM", str(tmp_path / "gone"))
    sample = guest.sample()
    assert sample["boot"] == "boot-9"
    assert [key for key, value in sample.items() if value is None] == [
        "memory_total_bytes", "memory_available_bytes", "memory_stall_usec",
        "memory_full_stall_usec", "cpu_stall_usec", "cpu_usec", "oom_kills", "swapouts",
        "disk_used_bytes", "disk_total_bytes",
    ]


def test_failed_mount_is_a_launch_failure_with_the_reason_scrubbed(sim, provisioned):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"],
                                  "organization": "x", "drive": DRIVE})
    sim.replies[("mount.cifs",)] = (32, "mount error(13): pw-secret rejected\n")
    guest.supervise("run-1")
    assert sim.spawned == []
    exit_record = json.loads((directory / "exit.json").read_text())
    assert exit_record["status"] == "launch_failed" and exit_record["exit_code"] == 125
    assert exit_record["error"] == "Shared drive mount failed (32): mount error(13): *** rejected"
    assert not any(argv[0] == "umount" for argv in sim.commands)
    assert not (directory / "request.json").exists()


def test_service_start_failure_is_a_launch_failure(sim, provisioned):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    sim.replies[("systemctl", "--user", "--machine", f"{ACCOUNT}@.host", "start")] = (1, "boom")
    guest.supervise("run-1")
    exit_record = json.loads((directory / "exit.json").read_text())
    assert exit_record["status"] == "launch_failed"
    assert exit_record["error"].startswith("Role services did not start")
    assert sim.commands[-2:] == SETTLE


def test_supervisor_caps_logs_and_records_truncation(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]})
    monkeypatch.setattr(guest, "MAX_LOG_BYTES", 5)
    sim.processes.append(SimpleNamespace(pid=1, stdout=io.BytesIO(b"abcdefghij"), wait=lambda: 0))
    guest.supervise("run-1")
    assert (directory / "log").read_bytes() == b"abcde"
    assert json.loads((directory / "exit.json").read_text())["log_truncated"] is True


def test_stop_before_launch_and_stop_of_a_running_or_unresolved_run(sim, provisioned, monkeypatch):
    directory = journal("run-1", {"role_id": ROLE, "argv": ["/bin/true"]},
                        {"role_id": ROLE, "run_id": "run-1", "boot": "boot-1"})
    (directory / "stop").touch()
    guest.supervise("run-1")
    assert sim.spawned == []
    assert json.loads((directory / "exit.json").read_text())["error"] == (
        "Stop requested before the role's launch"
    )
    states = iter([{"role_id": ROLE, "running": True}, {"role_id": ROLE, "running": False},
                   {"role_id": ROLE, "unresolved": True}, {"role_id": ROLE, "running": False}])
    monkeypatch.setattr(guest, "status", lambda _run_id: next(states))
    sim.commands.clear()
    guest.stop_run("run-1")
    assert sim.commands == []  # The successful receipt makes settlement idempotent.
    sim.commands.clear()
    (directory / "exit.json").unlink()
    guest.stop_run("run-1")
    assert sim.commands == []  # The successful receipt makes settlement idempotent.
    exit_record = json.loads((directory / "exit.json").read_text())
    assert exit_record["status"] == "stopped" and exit_record["unresolved"] is False


def test_remove_rejects_unresolved_runs(sim, monkeypatch):
    journal("run-1", {"role_id": ROLE})
    monkeypatch.setattr(guest, "status", lambda _run: {"unresolved": True})
    with pytest.raises(ValueError, match="resolve"):
        guest.remove_run("run-1")
    monkeypatch.setattr(guest, "status", lambda _run: {"running": False})
    assert guest.remove_run("run-1") == {"removed": "run-1"}


# ---- retire -----------------------------------------------------------------------------


def test_retire_settles_only_that_role_and_tombstones_it(sim, monkeypatch):
    first = guest.provision(ROLE)
    guest.provision("r-e2")
    guest.prepare_run_state(first)
    sim.commands.clear()
    monkeypatch.setattr(guest, "runs", lambda: [{"role_id": "r-e2", "running": True}])
    assert guest.retire_role(ROLE) == {"retired": ROLE, "private_retained": True}
    assert sim.commands == SETTLE
    assert not (sim.tree / "run" / ROLE).exists()
    assert (sim.tree / "home" / ACCOUNT / "work").is_dir()
    assert guest.database()["roles"][ROLE]["retired"] is True
    assert guest.retire_role(ROLE) == {"retired": ROLE, "private_retained": True}
    monkeypatch.setattr(guest, "runs", lambda: [{"role_id": "r-e2", "running": True}])
    with pytest.raises(ValueError, match="Stop and resolve"):
        guest.retire_role("r-e2")


def test_retire_before_first_run_reserves_role_without_creating_account(sim):
    expected = {"retired": "unstarted", "private_retained": False}
    assert guest.retire_role("unstarted") == expected
    assert guest.retire_role("unstarted") == expected
    assert sim.commands == []
    with pytest.raises(ValueError, match="Retired"):
        guest.provision("unstarted")


def test_retire_finishes_a_pending_account_before_settling(sim, monkeypatch):
    data = guest.database()
    data["pending"] = {"role_id": ROLE, "account": ACCOUNT, "uid": UID, "index": 0}
    guest.save(guest.STATE / "firm.json", data)
    assert guest.retire_role(ROLE) == {"retired": ROLE, "private_retained": True}
    assert sim.commands == provision_commands(sim) + SETTLE
    assert guest.database()["next"] == 1 and "pending" not in guest.database()


# ---- credentials ------------------------------------------------------------------------


def test_stage_secrets_private_files_and_no_follow(sim, provisioned, tmp_path):
    credentials = sim.tree / "run" / ROLE / "credentials"
    request = {"secrets": {"token": base64.b64encode(b"synthetic-secret").decode()},
               "secret_env": {"TOKEN": "token"}}
    assert guest.stage_secrets(provisioned, request) == {"TOKEN": "synthetic-secret"}
    assert (credentials / "token").read_bytes() == b"synthetic-secret"
    assert (credentials / "token").stat().st_mode & 0o777 == 0o600
    (credentials / "token").unlink()
    outside = tmp_path / "outside"
    outside.write_text("preserved")
    (credentials / "token").symlink_to(outside)
    with pytest.raises(FileExistsError):
        guest.stage_secrets(provisioned, request)
    assert outside.read_text() == "preserved"


def test_write_credential_creates_one_private_file_under_the_active_run(sim, provisioned):
    credentials = sim.tree / "run" / ROLE / "credentials"
    payload = base64.b64encode(b"PRIVATE KEY").decode()
    result = guest.dispatch({"operation": "write-credential", "role_id": ROLE,
                             "name": "mac-abc", "data": payload})
    assert result == {"path": str(credentials / "mac-abc")}
    assert (credentials / "mac-abc").read_bytes() == b"PRIVATE KEY"
    assert (credentials / "mac-abc").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        guest.write_credential(provisioned, {"name": "mac-abc", "data": payload})
    for name in ("../escape", "Mac", ""):
        with pytest.raises(ValueError):
            guest.write_credential(provisioned, {"name": name, "data": payload})
    with pytest.raises(ValueError, match="encoding"):
        guest.write_credential(provisioned, {"name": "mac-x", "data": "***"})
    guest.clean_run_state(provisioned)
    with pytest.raises(RuntimeError, match="no active run"):
        guest.write_credential(provisioned, {"name": "mac-y", "data": payload})


# ---- the file server's steps ------------------------------------------------------------


def test_file_steps_create_service_accounts_with_passwords_on_stdin_only(sim, monkeypatch):
    inputs = []
    original = sim.run

    def run(argv, **options):
        inputs.append(options.get("input"))
        return original(argv, **options)

    monkeypatch.setattr(sim, "run", run)
    (files,) = guest.files({"root": str(sim.tree / "srv"), "steps": [
        {"action": "account", "account": "firmfile-example", "uid": 51234,
         "password": "only-on-stdin", "group": "firmfiles"},
    ]})
    assert files["commands"] == [
        ["useradd", "-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0",
         "-M", "-s", "/usr/sbin/nologin", "-u", "51234", "-G", "firmfiles",
         "firmfile-example"],
        ["smbpasswd", "-s", "-a", "firmfile-example"],
    ]
    assert inputs == [None, "only-on-stdin\nonly-on-stdin\n"]
    sim.commands.clear()
    (again,) = guest.files({"root": str(sim.tree / "srv"), "steps": [
        {"action": "account", "account": "firmfile-example", "uid": 51234,
         "password": "only-on-stdin", "group": "firmfiles"},
    ]})
    assert again["commands"] == [["smbpasswd", "-s", "-a", "firmfile-example"]]
    with pytest.raises(RuntimeError, match="another UID"):
        guest.ensure_account("firmfile-example", 51235)


def test_file_setup_writes_the_samba_configuration_and_retire_is_idempotent(sim):
    root = sim.tree / "srv"
    (setup,) = guest.files({"root": str(root), "steps": [
        {"action": "setup", "group": "firmfiles", "owner": "firmowner",
         "owner_password": "pw", "smb_conf": "[global]\n"},
    ]})
    assert setup["configured"] is True and guest.SMB_CONF.read_text() == "[global]\n"
    assert ["systemctl", "restart", "smbd"] in setup["commands"]
    assert all((root / name).stat().st_mode & 0o777 == 0o700 for name in ("org", "shared"))
    assert not (root / "teams").exists()
    (again,) = guest.files({"root": str(root), "steps": [
        {"action": "setup", "group": "firmfiles", "owner": "firmowner",
         "owner_password": "pw", "smb_conf": "[global]\n"},
    ]})
    assert again["configured"] is False
    (absent,) = guest.files({"root": str(root), "steps": [
        {"action": "retire", "account": "firmfile-gone", "group": "firmfiles"},
    ]})
    assert absent == {"absent": True, "commands": []}
    with pytest.raises(ValueError, match="absolute"):
        guest.files({"root": "srv", "steps": []})


@pytest.mark.parametrize("path", [
    "org/founder/analyst/work/reports/drafts",
    "org/.retired/r-old/analyst/work/reports/drafts",
    "shared/reports/drafts",
])
def test_browse_nested_directories_keep_file_protections(tmp_path, path):
    root = tmp_path / "drive"
    directory = root / path
    directory.mkdir(parents=True)
    content = b"nested report"
    (directory / "report.md").write_bytes(content)
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    (directory / "symlink").symlink_to(secret)
    (directory / "hardlink").hardlink_to(secret)
    (directory / "folder").mkdir()
    os.mkfifo(directory / "fifo")

    def read(name, limit=100):
        return guest.file_step(str(root), {
            "action": "browse-read", "path": path, "name": name, "limit": limit,
        })

    listing = guest.file_step(str(root), {"action": "browse-list", "path": path})
    assert {entry["name"]: entry["type"] for entry in listing["entries"]} == {
        "report.md": "file", "symlink": "unsupported", "hardlink": "unsupported",
        "fifo": "unsupported", "folder": "directory",
    }
    assert read("report.md") == {
        "status": "available", "data": base64.b64encode(content).decode(), "size": len(content),
    }
    assert read("missing.md") == {"status": "missing"}
    assert read("report.md", 1) == {"status": "too_large"}
    for name in ("symlink", "hardlink", "fifo", "folder"):
        assert read(name) == {"status": "unsupported"}
    for name in ("", ".", "..", "../secret", "folder/report.md", "a\\b", "a\x00b"):
        with pytest.raises(ValueError, match="Invalid browse file"):
            read(name)
    for limit in (0, True, 6 * 1024 * 1024 + 1):
        with pytest.raises(ValueError, match="Invalid browse file"):
            read("report.md", limit)
    for limit in (0, True, 201):
        with pytest.raises(ValueError, match="listing limit"):
            guest.file_step(str(root), {"action": "browse-list", "path": path, "limit": limit})


@pytest.mark.parametrize("action", ["browse-list", "browse-read"])
@pytest.mark.parametrize("path", [
    "", "/shared", ".", "..", "shared/..", "shared//reports", "shared/./reports",
    "org/founder/work/reports/../../private", "org/founder/work/a\\b", "shared/a\x00b",
    "org", "org/work", "org/founder", "org/founder/private/reports", "private/work/reports",
])
def test_browse_rejects_invalid_paths_before_opening(tmp_path, action, path):
    with pytest.raises(ValueError, match="browse path|outside workspaces"):
        guest.file_step(str(tmp_path), {
            "action": action, "path": path, "name": "secret", "limit": 100,
        })


@pytest.mark.parametrize("action", ["browse-list", "browse-read"])
@pytest.mark.parametrize("depth", range(6))
def test_browse_never_follows_directory_symlinks(tmp_path, action, depth):
    root = tmp_path / "drive"
    parts = ["org", "founder", "work", "reports", "drafts"]
    linked = root.joinpath(*parts[:depth])
    linked.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    target = outside.joinpath(*parts[depth:])
    target.mkdir(parents=True)
    (target / "secret").write_text("private")
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        guest.file_step(str(root), {
            "action": action, "path": "/".join(parts), "name": "secret", "limit": 100,
        })


def test_dispatch_rejects_unknown_operations(sim):
    with pytest.raises(ValueError, match="Unknown operation"):
        guest.dispatch({"operation": "transfer"})
    with contextlib.suppress(KeyError):
        guest.dispatch({})


# ---- clock -----------------------------------------------------------------------------


def test_launch_steps_a_lagging_guest_clock_to_the_host_time(monkeypatch):
    stepped = []
    monkeypatch.setattr(guest.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(guest.time, "clock_settime", lambda clock, value: stepped.append(
        (clock, value)))
    request = {"operation": "start", "host_time": 1_000.0 + 3_600}
    guest.step_clock(request)
    assert stepped == [(guest.time.CLOCK_REALTIME, 4_600.0)]
    assert "host_time" not in request  # never part of the launch fingerprint
    stepped.clear()
    guest.step_clock({"host_time": 1_000.0 + guest.CLOCK_TOLERANCE})
    guest.step_clock({})
    assert stepped == []
    for invalid in ("1000", float("nan"), -1, True):
        with pytest.raises(ValueError, match="Invalid host time"):
            guest.step_clock({"host_time": invalid})


@pytest.mark.parametrize("operation", ["start", "start-child"])
def test_a_launch_steps_the_clock_before_it_waits_for_the_global_lock(
    monkeypatch, capsys, operation
):
    order = []

    @contextlib.contextmanager
    def lock():
        order.append("lock")
        yield

    def step(request):
        order.append(("step", request.pop("host_time")))

    def dispatch(request):
        order.append(("dispatch", "host_time" in request))
        return {}

    monkeypatch.setattr(guest, "lock", lock)
    monkeypatch.setattr(guest, "step_clock", step)
    monkeypatch.setattr(guest, "dispatch", dispatch)
    request = json.dumps({"operation": operation, "host_time": 1234.5}).encode()
    monkeypatch.setattr(guest.sys, "argv", ["firm-controller.py"])
    monkeypatch.setattr(guest.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request)))
    # main()'s root-in-a-VM guards, and its umask, which must not change this process's.
    monkeypatch.setattr(guest.sys, "platform", "linux")
    monkeypatch.setattr(guest.os, "getuid", lambda: 0)
    monkeypatch.setattr(guest.os, "umask", lambda mask: mask)
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    guest.main()
    assert order == [("step", 1234.5), "lock", ("dispatch", False)]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": {}}


# ---- export ----------------------------------------------------------------------------


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("homes", [False, True])
def test_export_distinguishes_a_missing_drive_from_an_empty_drive(sim, present, homes):
    import tarfile

    root = sim.tree / "srv" / "firm"
    if present:
        root.mkdir(parents=True)
    stream = io.BytesIO()
    guest.export({"operation": "export", "root": str(root), "homes": homes}, stream)
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        assert [member.name for member in archive] == (["drive"] if present else [])
    assert root.exists() is present


def test_export_streams_the_drive_and_only_requested_home_directories(sim):
    import tarfile

    root = sim.tree / "srv"
    (root / "org" / "founder" / "work").mkdir(parents=True)
    (root / "org" / "founder" / "work" / "notes.md").write_text("drive")
    (root / "org" / "founder" / "work" / "outside").symlink_to("/etc")
    home = sim.tree / "home" / ACCOUNT
    for name in ("work", ".firm/claude-sessions", ".cache", "drive"):
        (home / name).mkdir(parents=True)
    (home / "work" / "draft.md").write_text("private")
    (home / ".cache" / "blob").write_text("cache")
    guest.save(guest.STATE / "firm.json", {"roles": {
        ROLE: {"role_id": ROLE, "home": str(home)},
        "r-gone": {"role_id": "r-gone", "retired": True},
    }, "next": 1})

    def members(homes):
        stream = io.BytesIO()
        guest.export({"operation": "export", "root": str(root), "homes": homes}, stream)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            return {member.name: member for member in archive}

    drive_only = members(False)
    assert set(drive_only) == {
        "drive", "drive/org", "drive/org/founder", "drive/org/founder/work",
        "drive/org/founder/work/notes.md", "drive/org/founder/work/outside",
    }
    assert drive_only["drive/org/founder/work/outside"].issym()
    with_homes = members(True)
    assert {name for name in with_homes if name.startswith("homes/")} == {
        f"homes/{ROLE}/work", f"homes/{ROLE}/work/draft.md",
        f"homes/{ROLE}/.firm", f"homes/{ROLE}/.firm/claude-sessions",
    }
    with pytest.raises(ValueError, match="absolute"):
        guest.export({"operation": "export", "root": "srv", "homes": False}, io.BytesIO())

# ---- task child contracts (cgroup kernel behavior is simulated) -------------------------


@pytest.fixture
def live_parent(sim, provisioned):
    record = {"run_id": "parent", "role_id": ROLE, "uid": UID, "boot": "boot-1",
              "supervisor": os.getpid(), "phase": "running"}
    directory = journal("parent", {"env": {"ACTIVATION_TOKEN": "never-inherit"}}, record)
    guest.save(directory / "execution.json", {
        "info": provisioned, "cwd": provisioned["drive"] + "/org/team/work",
        "env": guest.role_environment(provisioned),
    })
    guest.prepare_run_state(provisioned)
    guest.slice_cgroup(UID).mkdir(parents=True)
    (guest.slice_cgroup(UID) / "cgroup.events").write_text("populated 0\n")
    sim.replies[("ps",)] = (0, f"{guest.CONTROLLER} --supervise parent")
    sim.commands.clear()
    return record


def child_request(**changes):
    return {"operation": "start-child", "parent_run_id": "parent", "run_id": "child",
            "argv": ["/bin/sh", "-c", "printf answer"],
            "seconds_remaining": 120, **changes}


def child_process(sim, output=b"answer", code=0):
    reader, writer = os.pipe()
    os.write(writer, output)
    os.close(writer)
    process = SimpleNamespace(pid=99, stdout=os.fdopen(reader, "rb"),
                              poll=lambda: code, wait=lambda: code)
    sim.processes.append(process)
    return process


def test_child_inherits_parent_identity_cwd_clean_environment_and_deadline(
    sim, live_parent, monkeypatch,
):
    request = child_request()
    record = guest.dispatch(request)
    assert record["uid"] == live_parent["uid"]
    assert 119 < record["deadline"] - guest.time.time() <= 120
    group = guest.child_cgroup(record)
    assert group.parent == guest.slice_cgroup(UID)
    with pytest.raises(ValueError, match="already used or stopped"):
        guest.dispatch(request)
    assert len(sim.spawned) == 1
    child_process(sim)
    # Observe the pre-exec ordering without changing the test process's credentials.
    calls = []
    for name in ("setgroups", "setgid", "setuid", "chdir"):
        monkeypatch.setattr(guest.os, name, lambda value, name=name: calls.append((name, value)))
    spawn = sim.spawn

    def capture(argv, **options):
        if "preexec_fn" in options:
            options["preexec_fn"]()
            assert (group / "cgroup.procs").read_text() == str(os.getpid())
        return spawn(argv, **options)

    monkeypatch.setattr(sim, "spawn", capture)
    guest.supervise_child("child")
    assert calls == [("setgroups", [UID]), ("setgid", UID), ("setuid", UID),
                     ("chdir", str(sim.tree / "home" / ACCOUNT / "drive/org/team/work"))]
    argv, options = sim.spawned[-1]
    assert argv == request["argv"]
    assert options["env"]["USER"] == ACCOUNT
    assert options["env"]["HOME"].endswith("/task-homes/child/home")
    assert "ACTIVATION_TOKEN" not in options["env"]
    assert options["env"]["FIRM_STATE_DIR"].endswith("/task-homes/child/state")
    assert options["env"]["FIRM_CREDENTIALS_DIR"].endswith("/tasks/child/credentials")
    assert options["env"]["FIRM_PRIVATE_HOME"] == options["env"]["HOME"]
    assert not (guest.STATE / "task-homes/child").exists()
    assert all(command[0] == "ps" for command in sim.commands)  # no account/service/mount work
    assert guest.child_status("child")["exit_code"] == 0
    assert guest.status("parent")["running"]
    assert not group.exists()
    assert base64.b64decode(guest.child_logs("child", 1, 3)["data"]) == b"nsw"


@pytest.mark.parametrize("field,value", [("uid", 0), ("cwd", "/"), ("role_id", "other")])
def test_child_accepts_no_identity_or_cwd(sim, live_parent, field, value):
    with pytest.raises(ValueError, match="only a parent"):
        guest.start_child(child_request(**{field: value}))


def test_child_cannot_parent_and_second_top_level_still_refused(sim, live_parent):
    guest.start_child(child_request())
    with pytest.raises(ValueError, match="top-level"):
        guest.start_child(child_request(parent_run_id="child", run_id="grandchild"))
    with pytest.raises(ValueError, match="active or unresolved"):
        guest.start({"run_id": "another", "role_id": ROLE, "argv": ["true"]})
    assert [r["run_id"] for r in guest.runs()] == ["parent"]


@pytest.mark.parametrize("phase", ["stopping", "launching", "exited"])
def test_child_requires_live_nonstopping_parent(sim, live_parent, phase):
    directory = guest.run_path("parent")
    if phase == "stopping":
        (directory / "stop").touch()
    elif phase == "launching":
        guest.save(directory / "record.json", {**live_parent, "phase": "launching"})
    else:
        guest.save(directory / "exit.json", {"exit_code": 0})
    with pytest.raises(ValueError, match="not live"):
        guest.start_child(child_request())


@pytest.mark.parametrize("topology,pids", [
    ("fork", [11, 12]), ("double-fork", [11, 13]), ("setsid", [11, 14]),
])
def test_child_kill_targets_all_simulated_cgroup_members(sim, live_parent, topology, pids,
                                                       monkeypatch):
    record = guest.start_child(child_request())
    group = guest.child_cgroup(record)
    (group / "cgroup.procs").write_text("\n".join(map(str, pids)))
    killed = []
    kill = sim.kill_child_cgroup

    def kernel_kill(path):
        killed.extend(map(int, (path / "cgroup.procs").read_text().split()))
        kill(path)  # Simulates the kernel emptying the group, irrespective of PPID/SID.
        assert (path / "cgroup.kill").read_text() == "1"

    monkeypatch.setattr(sim, "kill_child_cgroup", kernel_kill)
    assert guest.stop_child("child")["confirmed"] is True
    assert killed == pids
    assert not group.exists()
    assert guest.status("parent")["running"]


def test_child_stop_unconfirmed_when_members_remain_then_retries(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    group = guest.child_cgroup(record)
    (group / "cgroup.procs").write_text("111\n")
    (group / "cgroup.events").write_text("populated 1\n")
    kill = sim.kill_child_cgroup
    monkeypatch.setattr(sim, "kill_child_cgroup", lambda p: (p / "cgroup.kill").write_text("1"))
    monkeypatch.setattr(guest, "CHILD_STOP_TIMEOUT", 0)
    result = guest.stop_child("child")
    assert result["confirmed"] is False and result["unresolved"] is True
    assert group.exists() and (group / "cgroup.kill").read_text() == "1"
    monkeypatch.setattr(sim, "kill_child_cgroup", kill)
    assert guest.stop_child("child")["confirmed"] is True


def test_child_watchdog_kills_without_host_polling(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    process = child_process(sim, output=b"", code=None)
    now = guest.time.time()
    clock = [now]
    monkeypatch.setattr(guest.time, "time", lambda: clock[0])
    select = guest.selectors.DefaultSelector.select

    def advance(selector, timeout=None):
        clock[0] = record["deadline"] + 1
        return select(selector, 0)

    monkeypatch.setattr(guest.selectors.DefaultSelector, "select", advance)
    guest.supervise_child("child")
    state = guest.child_status("child")
    assert state["reason"] == "timeout" and state["confirmed"]
    assert not guest.child_cgroup(record).exists()
    assert process.stdout.closed
    assert guest.status("parent")["running"]


def test_child_log_overflow_stops_and_read_is_bounded(sim, live_parent, monkeypatch):
    guest.start_child(child_request())
    monkeypatch.setattr(guest, "CHILD_LOG_BYTES", 5)
    child_process(sim, b"123456")
    guest.supervise_child("child")
    assert guest.child_status("child")["reason"] == "log_overflow"
    assert base64.b64decode(guest.child_logs("child", limit=1000)["data"]) == b"12345"
    with pytest.raises(ValueError, match="range"):
        guest.child_logs("child", offset=-1)


def test_parent_stop_kills_children_before_settlement(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    settle = guest.settle
    observed = []

    def parent_settle(info, **options):
        observed.append(True)
        assert not guest.child_cgroup(record).exists()
        assert guest.child_status("child")["confirmed"]
        return settle(info, **options)

    monkeypatch.setattr(guest, "settle", parent_settle)
    guest.stop_run("parent")
    assert observed == [True]


def test_unconfirmed_child_still_settles_parent_before_raising(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    (guest.child_cgroup(record) / "cgroup.procs").write_text("99")
    (guest.child_cgroup(record) / "cgroup.events").write_text("populated 1\n")
    monkeypatch.setattr(sim, "kill_child_cgroup", lambda p: None)
    monkeypatch.setattr(guest, "CHILD_STOP_TIMEOUT", 0)
    with pytest.raises(RuntimeError, match="Child cleanup unconfirmed"):
        guest.stop_run("parent")
    assert ["systemctl", "stop", f"user-{UID}.slice"] in sim.commands


def test_new_controller_can_stop_journaled_child_by_id(sim, live_parent):
    record = guest.start_child(child_request())
    (guest.child_cgroup(record) / "cgroup.procs").write_text("99")
    with guest.simulation():
        state = guest.stop_child("child")
    assert state["confirmed"] and state["parent_run_id"] == "parent"
    assert not guest.child_cgroup(record).exists()


def test_stop_before_late_start_fences_child_and_supervisor(sim, live_parent):
    assert guest.stop_child("late")["confirmed"]
    with pytest.raises(ValueError, match="already used or stopped"):
        guest.start_child(child_request(run_id="late"))
    guest.start_child(child_request())
    guest.stop_child("child")
    count = len(sim.spawned)
    guest.supervise_child("child")
    assert len(sim.spawned) == count


def test_parent_stop_between_child_journal_and_supervisor_prevents_exec(sim, live_parent):
    guest.start_child(child_request())
    guest.stop_run("parent")
    count = len(sim.spawned)
    guest.supervise_child("child")
    assert len(sim.spawned) == count
    assert guest.child_status("child")["confirmed"]


@pytest.mark.parametrize("failure", ["kill", "read", "remove"])
def test_child_cleanup_io_failure_never_confirms(sim, live_parent, monkeypatch, failure):
    record = guest.start_child(child_request())
    group = guest.child_cgroup(record)

    def fail(*args):
        raise OSError("kernel refused")

    if failure == "kill":
        monkeypatch.setattr(sim, "kill_child_cgroup", fail)
    elif failure == "remove":
        monkeypatch.setattr(sim, "remove_child_cgroup", fail)
    else:
        monkeypatch.setattr(sim, "kill_child_cgroup", lambda p: None)
        (group / "cgroup.events").unlink()
    assert not guest.stop_child("child")["confirmed"]
    assert group.exists()


def test_failed_child_supervisor_spawn_leaves_recoverable_journal(sim, live_parent, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(sim, "spawn", fail)
    with pytest.raises(OSError, match="spawn failed"):
        guest.start_child(child_request())
    state = guest.child_status("child")
    assert state["exit_code"] == 125 and state["confirmed"]
    assert not guest.child_cgroup(state).exists()
    assert guest.status("parent")["running"]


def test_parent_normal_settlement_also_stops_children(sim, live_parent):
    record = guest.start_child(child_request())
    info = guest.role_record(ROLE)
    with guest.lock(), guest.run_lock("parent"):
        guest.settle_run(info, guest.run_path("parent"))
    assert guest.child_status("child")["confirmed"]
    assert not guest.child_cgroup(record).exists()


def test_child_launch_and_parent_stop_serialize(sim, live_parent, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    spawn = sim.spawn

    def delayed_spawn(argv, **options):
        entered.set()
        assert release.wait(2)
        return spawn(argv, **options)

    monkeypatch.setattr(sim, "spawn", delayed_spawn)

    def start():
        with guest.lock():
            return guest.start_child(child_request())

    def stop():
        with guest.lock():
            result = guest.stop_run("parent")
        stopped.set()
        return result

    with ThreadPoolExecutor(2) as pool:
        launching = pool.submit(start)
        assert entered.wait(1)
        stopping = pool.submit(stop)
        try:
            assert not stopped.wait(0.05)
        finally:
            release.set()
        record = launching.result(2)
        stopping.result(2)
    assert not guest.child_cgroup(record).exists()
    count = len(sim.spawned)
    guest.supervise_child("child")
    assert len(sim.spawned) == count


def test_child_launch_stages_only_credentials_on_tmpfs(sim, live_parent, monkeypatch):
    request = child_request(env={"WORKER_ONLY": "yes"},
                            secrets={"provider-token": base64.b64encode(b"secret-token").decode()},
                            secret_env={"PROVIDER_TOKEN": "provider-token"},
                            stdin=base64.b64encode(b"worker instruction").decode())
    record = guest.start_child(request)
    credentials = guest.child_credentials(record)
    assert (credentials / "provider-token").read_bytes() == b"secret-token"
    journal = guest.child_path("child")
    assert "secret-token" not in (journal / "request.json").read_text()
    assert request["secrets"]["provider-token"] not in (journal / "request.json").read_text()
    assert (journal / "stdin").read_bytes() == b"worker instruction"
    child_process(sim)
    spawn = sim.spawn

    def capture(argv, **options):
        assert options["stdin"].read() == b"worker instruction"
        assert options["env"]["PROVIDER_TOKEN"] == "secret-token"
        assert options["env"]["WORKER_ONLY"] == "yes"
        home = Path(options["env"]["HOME"])
        assert home.is_relative_to(guest.STATE / "task-homes")
        assert home.stat().st_mode & 0o777 == 0o700
        assert home.parent.stat().st_mode & 0o777 == 0o700
        assert guest.STATE.stat().st_mode & 0o111 == 0o111
        assert sim.owned[home.stat().st_ino] == (UID, UID)
        return spawn(argv, **options)

    monkeypatch.setattr(sim, "spawn", capture)
    monkeypatch.setattr(guest, "lock", lambda: pytest.fail("child supervisor took global lock"))
    guest.supervise_child("child")
    assert not credentials.parent.exists()
    assert not (guest.STATE / "task-homes/child").exists()
    assert not (journal / "stdin").exists()
    assert guest.child_status("child")["confirmed"]


@pytest.mark.parametrize("seconds", [True, 0, -1, float("inf"), float("nan"), "123"])
def test_child_duration_must_be_finite_positive_number(sim, live_parent, seconds):
    with pytest.raises(ValueError, match="seconds remaining"):
        guest.start_child(child_request(seconds_remaining=seconds))


def test_child_deadline_uses_guest_clock(sim, live_parent, monkeypatch):
    monkeypatch.setattr(guest.time, "time", lambda: 500)
    record = guest.start_child(child_request(seconds_remaining=12))
    assert record["deadline"] == 512


def test_child_emptiness_uses_recursive_populated_event(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    group = guest.child_cgroup(record)
    (group / "cgroup.procs").write_text("")
    (group / "cgroup.events").write_text("populated 1\n")
    monkeypatch.setattr(sim, "kill_child_cgroup", lambda p: None)
    monkeypatch.setattr(guest, "CHILD_STOP_TIMEOUT", 0)
    assert not guest.stop_child("child")["confirmed"]
    assert group.exists()


def test_remove_parent_removes_only_its_child_journals(sim, live_parent):
    guest.start_child(child_request())
    guest.stop_child("unrelated")
    guest.stop_run("parent")
    guest.save(guest.run_path("parent") / "exit.json", {"exit_code": 0})
    guest.remove_run("parent")
    assert not guest.child_path("child").exists()
    assert guest.child_path("unrelated").exists()


def test_clean_role_state_sweeps_leftover_task_homes(sim, live_parent):
    guest.start_child(child_request())
    home = guest.STATE / "task-homes/child/home"
    home.mkdir(parents=True)
    guest.stop_run("parent")
    home.mkdir(parents=True)
    guest.clean_run_state(guest.role_record(ROLE))
    assert not home.parent.exists()


def test_reboot_confirmation_also_cleans_task_home(sim, live_parent, monkeypatch):
    record = guest.start_child(child_request())
    home = guest.STATE / "task-homes/child/home"
    home.mkdir(parents=True)
    monkeypatch.setattr(guest, "boot", lambda: "boot-2")
    state = guest.child_status("child")
    assert state["confirmed"] and state["reason"] == "interrupted"
    assert not home.parent.exists()
    assert not guest.child_credentials(record).parent.exists()


def test_child_stop_returns_before_supervisor_drains_logs(sim, live_parent, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    guest.start_child(child_request())
    child_process(sim, b'{"usage":{"input_tokens":17}}\n')
    monitoring, release = threading.Event(), threading.Event()

    def monitor(*args):
        monitoring.set()
        assert release.wait(2)
        return 137, "cancelled"

    monkeypatch.setattr(guest, "monitor_child", monitor)
    with ThreadPoolExecutor(2) as pool:
        supervisor = pool.submit(guest.supervise_child, "child")
        assert monitoring.wait(1)
        previous_commands = len(sim.commands)
        try:
            state = pool.submit(guest.stop_child, "child").result(1)
            assert state["confirmed"]
            assert not supervisor.done()
            assert not any(argv[0] == "ps" for argv in sim.commands[previous_commands:])
        finally:
            release.set()
        supervisor.result(2)
    assert base64.b64decode(guest.child_logs("child")["data"]) == (
        b'{"usage":{"input_tokens":17}}\n'
    )


def test_normal_child_exit_receipt_follows_final_log_flush(sim, live_parent, monkeypatch):
    guest.start_child(child_request())
    child_process(sim, b"answer")
    drain = guest.drain_child_output

    def checked_drain(process, directory):
        assert not (directory / "exit.json").exists()
        drain(process, directory)

    monkeypatch.setattr(guest, "drain_child_output", checked_drain)
    guest.supervise_child("child")
    assert guest.child_status("child")["confirmed"]
    assert base64.b64decode(guest.child_logs("child")["data"]) == b"answer"


def test_child_drain_is_nonblocking_and_preserves_log_cap(sim, live_parent, monkeypatch):
    guest.start_child(child_request())
    process = child_process(sim, b"buffered output")
    monkeypatch.setattr(guest, "CHILD_LOG_BYTES", 5)
    path = guest.child_path("child")
    (path / "log").write_bytes(b"abc")
    try:
        assert guest.drain_child_output(process, path) is True
        assert (path / "log").read_bytes() == b"abcbu"
    finally:
        process.stdout.close()


@pytest.mark.parametrize("operation", ["child-status", "child-logs", "sample",
                                       "start-child", "stop-child"])
def test_controller_child_reads_skip_global_lock(sim, live_parent, monkeypatch, capsys, operation):
    guest.start_child(child_request())
    (guest.child_path("child") / "log").write_bytes(b"answer")
    monkeypatch.setattr(guest.sys, "platform", "linux")
    monkeypatch.setattr(guest.os, "getuid", lambda: 0)
    monkeypatch.setattr(guest.os, "umask", lambda _: None)
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(guest.sys, "argv", ["controller"])
    request = {"operation": operation, "run_id": "child"}
    monkeypatch.setattr(guest.sys, "stdin", SimpleNamespace(
        buffer=io.BytesIO(json.dumps(request).encode()),
    ))
    monkeypatch.setattr(guest, "sample", lambda: {"sampled": True})

    @contextlib.contextmanager
    def occupied():
        raise RuntimeError("global lock occupied")
        yield

    monkeypatch.setattr(guest, "lock", occupied)
    guest.main()
    reply = json.loads(capsys.readouterr().out)
    if operation in {"start-child", "stop-child"}:
        assert reply == {"ok": False, "error": "global lock occupied"}
    else:
        assert reply["ok"]
        if operation == "child-status":
            assert reply["result"]["running"]
        elif operation == "child-logs":
            assert base64.b64decode(reply["result"]["data"]) == b"answer"


@pytest.mark.parametrize("completion", ["supervisor", "stop"])
def test_simulated_pause_resume_never_prunes_or_clears_and_preserves_files(
    sim, provisioned, completion, monkeypatch,
):
    home = Path(provisioned["home"])
    files = {"scratch/repo/.git/HEAD": b"ref: refs/heads/main\n",
             "scratch/repo/src/main.c": b"int main() { return 0; }\n",
             "scratch/repo/build/main.o": b"\x00object\xff",
             ".local/share/docker/containers/stopped/config.json": b"container",
             ".local/share/docker/overlay2/image/diff/built": b"image",
             ".local/share/docker/buildkit/cache": b"cache"}
    guest.start({"role_id": ROLE, "run_id": "first", "argv": ["true"]})
    for name, contents in files.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    sim.commands.clear()
    if completion == "stop":
        guest.stop_run("first")
    else:
        guest.supervise("first")
    result = guest.status("first")
    assert not result["unresolved"] and "reclaim" not in result
    assert result["usage"] is None
    assert not any(argv[0] == "du" or any("/prune" in arg for arg in argv)
                   for argv in sim.commands)
    assert all((home / name).read_bytes() == contents for name, contents in files.items())
    guest.start({"role_id": ROLE, "run_id": "second", "argv": ["true"]})
    assert all((home / name).read_bytes() == contents for name, contents in files.items())
    assert not any(argv[0] == "du" or any("/prune" in arg for arg in argv)
                   for argv in sim.commands)


@pytest.mark.parametrize("state", ["running", "unresolved"])
def test_dispose_refuses_active_or_unresolved(sim, provisioned, monkeypatch, state):
    monkeypatch.setattr(guest, "runs", lambda: [{"role_id": ROLE, state: True}])
    sim.commands.clear()
    with pytest.raises(ValueError, match="Stop and resolve.*disposal"):
        guest.dispose_role(ROLE)
    assert sim.commands == []


@pytest.mark.parametrize("retired", [False, True])
def test_dispose_prunes_scoped_docker_and_recreates_scratch(sim, provisioned, monkeypatch, retired):
    if retired:
        guest.retire_role(ROLE)
    before = guest.database()
    scratch = Path(provisioned["home"]) / "scratch"
    (scratch / "discard").write_text("gone")
    original = sim.run
    calls = []

    def reply(argv, **options):
        if argv[0] == "/usr/bin/setpriv":
            calls.append((argv, options))
            if argv[-1].endswith("containers/json"):
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout=json.dumps({"ContainersDeleted": ["container"],
                "ImagesDeleted": ["image"], "CachesDeleted": ["cache"], "SpaceReclaimed": 100}))
        return original(argv, **options)

    monkeypatch.setattr(sim, "run", reply)
    result = guest.dispatch({"operation": "dispose", "role_id": ROLE})
    assert result["disposed"] == ROLE
    assert [unquote(argv[-1]) for argv, _ in calls] == [
        "http://localhost/containers/json",
        "http://localhost/containers/prune",
        'http://localhost/images/prune?filters={"dangling":["true"]}',
        "http://localhost/build/prune?all=false",
    ]
    for argv, options in calls:
        assert argv[:4] == [
            "/usr/bin/setpriv", f"--reuid={UID}", f"--regid={UID}", "--clear-groups"]
        assert f"/run/user/{UID}/docker.sock" in argv
        assert options["env"]["HOME"] == provisioned["home"]
    assert all(r["completed"] for r in result["reclaim"]["categories"].values())
    assert list(scratch.iterdir()) == [] and scratch.stat().st_mode & 0o777 == 0o700
    assert sim.owned[scratch.stat().st_ino] == (UID, UID)
    assert guest.database() == before


def test_dispose_without_account_does_not_provision_or_change_retirement(sim):
    guest.retire_role(ROLE)
    before = guest.database()
    sim.commands.clear()
    assert guest.dispose_role(ROLE) == {"disposed": ROLE, "reclaim": {"categories": {}}}
    assert guest.database() == before and sim.commands == []


def test_dispose_without_docker_still_clears_scratch(sim, provisioned):
    scratch = Path(provisioned["home"]) / "scratch"
    (scratch / "discard").write_text("gone")
    sim.replies[tuple(user_systemctl("start", "docker.service"))] = (7, "unavailable")
    sim.replies[("/usr/bin/setpriv",)] = (7, "unavailable")
    result = guest.dispose_role(ROLE)["reclaim"]["categories"]
    assert result["scratch"]["completed"] and list(scratch.iterdir()) == []
    assert all(r["skipped"] == {"docker_unavailable": 1, **({"stop_failed": 1}
               if n == "docker_containers" else {})} for n, r in result.items() if n != "scratch")


def test_settlement_preserves_scratch_symlink_and_disposal_replaces_it(sim, provisioned):
    home = Path(provisioned["home"])
    scratch, target = home / "scratch", home / "work"
    (target / "precious").write_bytes(b"keep")
    scratch.rmdir()
    scratch.symlink_to(target, target_is_directory=True)
    directory = journal("symlink", {"role_id": ROLE, "argv": ["true"]})
    guest.supervise("symlink")
    assert not json.loads((directory / "exit.json").read_text())["unresolved"]
    assert scratch.is_symlink() and (target / "precious").read_bytes() == b"keep"
    guest.dispose_role(ROLE)
    assert not scratch.is_symlink() and list(scratch.iterdir()) == []
    assert (target / "precious").read_bytes() == b"keep"


def test_storage_includes_retired_accounts_and_omits_roles_without_accounts(sim, provisioned):
    other = guest.provision("other")
    guest.retire_role("other")
    guest.retire_role("never-started")
    before = guest.database()
    result = guest.dispatch({"operation": "storage"})["roles"]
    assert set(result) == {ROLE, "other"}
    assert result[ROLE]["retired"] is False and result["other"]["retired"] is True
    assert set(guest.storage("other")["roles"]) == {"other"}
    assert guest.storage("never-started") == guest.storage("unknown") == {"roles": {}}
    assert guest.database() == before and Path(other["home"]).exists()


def test_service_start_shares_the_supplied_budget_between_commands(sim, provisioned, monkeypatch):
    now, timeouts = [0.0], []
    original = sim.run
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])

    def start(argv, **options):
        if "start" in argv:
            timeouts.append(options["timeout"])
            now[0] += 6 if len(timeouts) == 1 else options["timeout"]
        return original(argv, **options)

    monkeypatch.setattr(sim, "run", start)
    guest.start_services(provisioned, timeout=10)
    assert timeouts == [10, 4] and now[0] == 10


def test_storage_main_dispatch_takes_no_execution_locks(sim, provisioned, monkeypatch, capsys):
    monkeypatch.setattr(guest.sys, "platform", "linux")
    monkeypatch.setattr(guest.os, "getuid", lambda: 0)
    monkeypatch.setattr(guest.os, "umask", lambda _: None)
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(guest.sys, "argv", ["firm-controller.py"])
    monkeypatch.setattr(guest.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        json.dumps({"operation": "storage", "role_id": ROLE}).encode())))
    monkeypatch.setattr(guest, "lock", lambda: pytest.fail("storage took global lock"))
    monkeypatch.setattr(sim, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="4096 x"))
    guest.main()
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] and reply["result"]["roles"][ROLE]["scratch"]["completed"]


@pytest.mark.parametrize("kind", ["symlink", "file", "foreign", "normal"])
def test_launch_heals_only_substituted_scratch(sim, provisioned, kind, capsys):
    home = Path(provisioned["home"])
    scratch = home / "scratch"
    target = home / "work/precious"
    target.write_bytes(b"keep target")
    if kind in {"symlink", "file"}:
        scratch.rmdir()
        if kind == "symlink":
            scratch.symlink_to(target)
        else:
            scratch.write_bytes(b"substitution")
    else:
        (scratch / "precious").write_bytes(b"retained checkout")
        if kind == "foreign":
            sim.owned[scratch.stat().st_ino] = (UID + 1, UID + 1)
            sim.owned[(scratch / "precious").stat().st_ino] = (UID + 2, UID + 2)
            (scratch / "precious").chmod(0o640)
    original_inode = scratch.lstat().st_ino
    guest.start({"role_id": ROLE, "run_id": "healed", "argv": ["true"]})
    diagnostic = capsys.readouterr().err
    assert scratch.is_dir() and not scratch.is_symlink()
    assert scratch.stat().st_mode & 0o777 == 0o700
    assert sim.owned[scratch.stat().st_ino] == (UID, UID)
    assert target.read_bytes() == b"keep target"
    if kind in {"normal", "foreign"}:
        assert scratch.stat().st_ino == original_inode
        assert (scratch / "precious").read_bytes() == b"retained checkout"
        if kind == "normal":
            assert diagnostic == ""
        else:
            assert "reowned_directory" in diagnostic
            assert sim.owned[(scratch / "precious").stat().st_ino] == (UID + 2, UID + 2)
            assert (scratch / "precious").stat().st_mode & 0o777 == 0o640
    else:
        assert not list(scratch.iterdir())
        assert "replaced_non_directory" in diagnostic


@pytest.mark.parametrize("code,reason", [(7, "docker_unavailable"), (22, "docker_error"),
                                         (1, "docker_error"), (28, "deadline")])
def test_docker_prune_distinguishes_connection_and_daemon_errors(sim, provisioned, code, reason):
    sim.replies[("/usr/bin/setpriv",)] = (code, "synthetic error")
    reports = guest.reclaim_docker(provisioned, guest.time.monotonic() + 60)
    assert len(reports) == 3
    assert all(report["skipped"] == {reason: 1, **({"stop_failed": 1}
               if name == "docker_containers" else {})} for name, report in reports.items())
    assert all(report["timed_out"] == (code == 28) for report in reports.values())


def test_disposal_stage_budgets_reserve_shutdown_and_scratch(sim, provisioned, monkeypatch):
    now, prune_timeouts, slice_timeouts, scratch_timeouts = [0.0], [], [], []
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])

    def services(info, *, timeout):
        assert timeout == 15
        now[0] += timeout

    def run(argv, **options):
        if argv[0] == "/usr/bin/setpriv":
            prune_timeouts.append(options["timeout"])
            now[0] += options["timeout"]
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        if argv == SETTLE[-1]:
            slice_timeouts.append(options["timeout"])
        now[0] += options["timeout"]
        return SimpleNamespace(returncode=0)

    def scratch(info, deadline):
        scratch_timeouts.append(deadline - now[0])
        now[0] = deadline
        return {**guest.reclaim_counts(), "completed": True}

    # Empty verification needs a little of the slice budget after systemctl returns.
    def run_with_margin(argv, **options):
        result = run(argv, **options)
        if argv == SETTLE[-1]:
            now[0] -= 0.1
        return result

    monkeypatch.setattr(guest, "start_services", services)
    monkeypatch.setattr(sim, "run", run_with_margin)
    monkeypatch.setattr(guest, "scratch_worker", scratch)
    guest.dispose_role(ROLE)
    assert prune_timeouts == [30]
    assert slice_timeouts == [18] and scratch_timeouts == [30]
    assert now[0] == pytest.approx(94.9)




@pytest.mark.parametrize("failure", [None, "docker_error", "scratch_deadline"])
def test_next_launch_cleanup_is_best_effort_and_published_retry_does_not_repeat(
    sim, provisioned, monkeypatch, capsys, failure,
):
    scratch = Path(provisioned["home"]) / "scratch"
    (scratch / "old").write_text("finished")
    if failure == "docker_error":
        sim.replies[("/usr/bin/setpriv",)] = (22, "HTTP error")
    elif failure == "scratch_deadline":
        monkeypatch.setattr(guest, "scratch_worker", lambda *a: guest.reclaim_skipped(
            "deadline", timed_out=True))
    request = {"role_id": ROLE, "run_id": "clean-start", "argv": ["true"], "clean_up": True}
    guest.start(request)
    record = json.loads((guest.run_path("clean-start") / "record.json").read_text())
    categories = record["cleanup"]["categories"]
    assert (scratch / "old").exists() is (failure == "scratch_deadline")
    assert categories["scratch"]["completed"] is (failure != "scratch_deadline")
    if failure == "docker_error":
        assert categories["docker_containers"]["skipped"] == {"docker_error": 1, "stop_failed": 1}
    assert ("Incomplete launch cleanup" in capsys.readouterr().err) is (failure is not None)
    # Work after publication survives a retry of exactly the same immutable request.
    (scratch / "new").write_text("keep")
    sim.commands.clear()
    guest.start(request)
    assert (scratch / "new").read_text() == "keep"
    assert not any("/prune" in arg for cmd in sim.commands for arg in cmd)
    with pytest.raises(ValueError, match="different request"):
        guest.start({**request, "clean_up": False})


@pytest.mark.parametrize("outcome", ["ok", "timeout", "error", "partial"])
def test_storage_du_arguments_and_unknown_results(sim, provisioned, monkeypatch, outcome):
    calls = []

    def du(argv, **options):
        calls.append((argv, options))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        return SimpleNamespace(returncode=0 if outcome == "ok" else 1,
                               stdout="" if outcome == "error" else "8192\tpath\n")

    monkeypatch.setattr(sim, "run", du)
    with guest.lock():  # storage reads do not take execution locks.
        result = guest.storage(ROLE)["roles"][ROLE]
    for (argv, options), relative in zip(calls, ("scratch", ".local/share/docker"), strict=True):
        assert argv == ["du", "-s", "-x", "-B1", str(Path(provisioned["home"]) / relative)]
        assert options["check"] is False and 0 < options["timeout"] <= 60
    for category in ("scratch", "docker"):
        assert result[category]["bytes"] == (8192 if outcome in {"ok", "partial"} else None)
        assert result[category]["completed"] is (outcome == "ok")
        assert result[category]["timed_out"] is (outcome == "timeout")


@pytest.mark.parametrize("failure", [None, "list", "stop"])
def test_disposal_stops_running_containers_before_pruning_with_shared_budget(
    sim, provisioned, monkeypatch, failure,
):
    now, calls = [0.0], []
    monkeypatch.setattr(guest.time, "monotonic", lambda: now[0])
    original = sim.run
    ids = ["a" * 64, "b" * 64]
    scratch = Path(provisioned["home"]) / "scratch"
    (scratch / "discard").write_text("finished")

    def run(argv, **options):
        if argv[0] != "/usr/bin/setpriv":
            return original(argv, **options)
        endpoint = unquote(argv[-1]).removeprefix("http://localhost/")
        method = argv[argv.index("--request") + 1]
        calls.append((method, endpoint, options["timeout"]))
        now[0] += 1
        if endpoint == "containers/json":
            if failure == "list":
                raise subprocess.CalledProcessError(22, argv)
            return SimpleNamespace(stdout=json.dumps([{"Id": cid} for cid in ids]))
        if "/stop?" in endpoint:
            if failure == "stop" and ids[0] in endpoint:
                raise subprocess.CalledProcessError(22, argv)
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout=json.dumps({
            "ContainersDeleted": ids, "ImagesDeleted": [], "CachesDeleted": [],
            "SpaceReclaimed": 100,
        }))

    monkeypatch.setattr(sim, "run", run)
    categories = guest.dispose_role(ROLE)["reclaim"]["categories"]
    expected = [("GET", "containers/json")]
    if failure != "list":
        expected += [("POST", f"containers/{cid}/stop?t=2") for cid in ids]
    expected += [("POST", "containers/prune"),
                 ("POST", 'images/prune?filters={"dangling":["true"]}'),
                 ("POST", "build/prune?all=false")]
    assert [(method, endpoint) for method, endpoint, _ in calls] == expected
    assert [timeout for _, _, timeout in calls] == list(range(30, 30 - len(calls), -1))
    assert categories["docker_containers"]["skipped"] == (
        {"stop_failed": 1} if failure else {})
    assert categories["docker_containers"]["completed"] is (failure is None)
    assert categories["scratch"]["completed"] and list(scratch.iterdir()) == []
