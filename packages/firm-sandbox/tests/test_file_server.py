"""File server contracts against a local tree; the guest's file steps run in simulation."""

import json
import os
from types import SimpleNamespace

import pytest

from firm_sandbox import Config, Organization
from firm_sandbox.file_server import (
    OWNER,
    SHARE,
    SMB_CONF,
    WORK_FILE_LIMIT,
    FileServer,
    local_runner,
)


class Forward:
    def __init__(self, port, log):
        self.port, self.log = port, log

    def start(self):
        self.log.append(("start", self.port))

    def close(self):
        self.log.append(("close", self.port))

    def require_ready(self):
        self.log.append(("ready", self.port))


@pytest.fixture
def server(tmp_path):
    (tmp_path / "firm").mkdir()
    config = Config(tmp_path / "firm", firm_id="demo", mac_image="/private/base")
    tree = tmp_path / "tree"
    tree.mkdir()
    runner = local_runner(tree)
    log = []

    def recording(request):
        replies = runner(request)
        for step, reply in zip(request["steps"], replies, strict=True):
            log.append((step["action"], reply["commands"]))
        return replies

    server = FileServer(config, recording, forwards=lambda port: [Forward(port, log)])
    server.tree, server.log = tree, log
    return server


def provision(server, role_id):
    server.provision(role_id, Organization(role_id))
    return server.credentials(role_id)


def test_smb_conf_binds_loopback_and_keeps_clients_from_changing_permissions():
    assert "interfaces = 127.0.0.1" in SMB_CONF and "bind interfaces only = yes" in SMB_CONF
    assert f"admin users = {OWNER}" in SMB_CONF
    assert "inherit owner = yes" in SMB_CONF and "nt acl support = no" in SMB_CONF
    assert "map readonly = no" in SMB_CONF and "unix extensions" not in SMB_CONF
    assert f"[{SHARE}]\n  path = /srv/firm" in SMB_CONF


def test_start_configures_samba_owner_and_forwards_a_stable_port(server):
    access = server.start()
    port = json.loads(server.record.read_text())["port"]
    assert access["url"] == f"smb://{OWNER}@127.0.0.1:{port}/{SHARE}"
    assert access["password"] and access["path"] == "/srv/firm"
    setup = dict(server.log)["setup"]
    assert ["groupadd", "-f", "firmfiles"] in setup
    assert ["smbpasswd", "-s", "-a", OWNER] in setup
    assert ["systemctl", "enable", "--now", "smbd"] in setup
    assert not any("password" in " ".join(argv) for argv in setup)
    assert ("start", port) in server.log
    assert (server.tree / "org").is_dir() and (server.tree / "shared").is_dir()
    assert server.record.stat().st_mode & 0o777 == 0o600
    server.log.clear()
    assert server.start()["url"] == access["url"]
    assert dict(server.log)["setup"] and ("start", port) in server.log
    server.stop()
    assert ("close", port) in server.log
    server.require_ready()
    assert ("ready", port) in server.log


def test_roles_get_logins_with_passwords_that_never_reach_argv(server):
    server.start()
    with pytest.raises(RuntimeError, match="no file server login"):
        server.credentials("r-one")
    first = provision(server, "r-one")
    assert first.user.startswith("firmfile-") and len(first.password) >= 24
    assert server.credentials("r-one") == first
    assert provision(server, "r-one") == first  # the same credential converges
    commands = [argv for action, argv in server.log if action == "account"]
    assert all(first.password not in " ".join(step) for step in commands[0])
    assert ["smbpasswd", "-s", "-a", first.user] in commands[0]
    useradd = next(argv for argv in commands[0] if argv[0] == "useradd")
    assert "-G" in useradd and "firmfiles" in useradd and "-u" in useradd
    other = provision(server, "r-two")
    assert other.user != first.user and other.password != first.password


def test_retirement_revokes_the_login_and_refuses_reuse(server):
    server.start()
    creds = provision(server, "r-one")
    server.retire_role("r-one")
    retire = dict((a, c) for a, c in server.log if a == "retire")["retire"]
    assert ["smbpasswd", "-x", creds.user] in retire
    assert ["gpasswd", "-d", creds.user, "firmfiles"] in retire
    assert any(
        argv[:4] == ["setfacl", "-R", "-x", f"u:{creds.user},d:u:{creds.user}"] for argv in retire
    )
    with pytest.raises(RuntimeError, match="retired"):
        provision(server, "r-one")
    with pytest.raises(RuntimeError, match="no file server login"):
        server.credentials("r-one")
    assert creds.password not in server.record.read_text()
    server.retire_role("r-never")  # a role that never had an account is tombstoned too
    with pytest.raises(RuntimeError, match="retired"):
        provision(server, "r-never")


def test_retirement_leaves_its_work_read_only_to_its_reporting_line(server):
    server.start()
    server.provision("r-eng", Organization("founder/eng"))
    server.provision("r-api", Organization("founder/eng/api"))
    server.provision("r-web", Organization("founder/eng/web"))
    eng, api, web = (server.credentials(role).user for role in ("r-eng", "r-api", "r-web"))
    server.log.clear()
    server.retire_role("r-api")
    steps = dict(server.log)
    (strip,) = [argv for argv in steps["retire"] if argv[0] == "setfacl"]
    assert strip[:4] == ["setfacl", "-R", "-x", f"u:{api},d:u:{api}"]
    assert [path.rsplit("/", 1)[1] for path in strip[4:]] == ["org", "shared"]
    read_only = [
        "setfacl",
        "-R",
        "-m",
        f"u:{eng}:rx,d:u:{eng}:rx",
        "/srv/firm/org/founder/eng/api/work",
    ]
    assert read_only in steps["grants"]
    assert not any(web in argv[-2] or api in argv[-2] for argv in steps["grants"])
    server.log.clear()
    server.start()
    grants = dict(server.log)["grants"]
    assert read_only in grants
    assert not any(
        argv[-1] == "/srv/firm/org/founder/eng/api/work" and "rwx" in argv[-2] for argv in grants
    )
    assert not any(api in " ".join(argv) for argv in grants)


def test_provision_creates_the_trees_and_grants_in_one_round_trip(server):
    server.start()
    assert server.database_identity() is None
    server.bind_identity("a" * 64)
    assert server.database_identity() == "a" * 64
    with pytest.raises(ValueError, match="different runtime"):
        server.bind_identity("b" * 64)
    assert server.database_identity() == "a" * 64
    server.log.clear()
    assert server.provision("r-founder", Organization("founder")) == "org/founder/work"
    assert [action for action, _ in server.log] == ["provision", "account", "grants"]
    assert server.provision("r-api", Organization("founder/api")) == "org/founder/api/work"
    for relative in ("org/founder/work", "org/founder/api/work", "shared"):
        assert (server.tree / relative).is_dir()
    assert not (server.tree / "teams").exists()
    grants = [argv for action, argv in server.log if action == "grants"][-1]
    api = server.credentials("r-api").user
    founder = server.credentials("r-founder").user
    assert [
        "setfacl",
        "-R",
        "-m",
        f"u:{founder}:rwx,d:u:{founder}:rwx",
        "/srv/firm/org/founder/api/work",
    ] in grants
    assert [
        "setfacl",
        "-R",
        "-m",
        f"u:{api}:rx,d:u:{api}:rx",
        "/srv/firm/org/founder/work",
    ] in grants
    assert server.role_at("founder/api") == "r-api"
    assert server.members() == {"founder": founder, "founder/api": api}
    with pytest.raises(ValueError, match="another role"):
        server.provision("r-other", Organization("founder/api"))
    with pytest.raises(ValueError, match="change organization identity"):
        server.provision("r-api", Organization("founder/web"))
    server.retire_role("r-founder")
    assert server.members() == {"founder/api": api}


def test_replacement_archives_the_subtree_for_the_reporting_line_only(server):
    server.start()
    server.provision("r-founder", Organization("founder"))
    server.provision("r-eng", Organization("founder/eng"))
    server.provision("r-api", Organization("founder/eng/api"))
    server.provision("r-ops", Organization("founder/ops"))
    founder, ops = server.credentials("r-founder").user, server.credentials("r-ops").user
    (server.tree / "org/founder/eng/work/notes.md").write_text("old")
    server.retire_role("r-api")
    server.retire_role("r-eng")
    with pytest.raises(ValueError, match="another role"):
        server.provision("r-eng2", Organization("founder/eng"))
    server.log.clear()
    assert server.provision("r-eng2", Organization("founder/eng"), replace_role_id="r-eng") == (
        "org/founder/eng/work"
    )
    eng2 = server.credentials("r-eng2").user
    assert [action for action, _ in server.log] == [
        "archive",
        "provision",
        "grants",
        "provision",
        "account",
        "grants",
    ]
    archive, hire = server.log[2][1], server.log[5][1]
    assert archive[0] == ["setfacl", "-R", "-b", "/srv/firm/org/.retired/r-eng"]
    for work in ("org/.retired/r-eng/work", "org/.retired/r-eng/api/work"):
        assert [
            "setfacl",
            "-R",
            "-m",
            f"u:{founder}:rx,d:u:{founder}:rx",
            f"/srv/firm/{work}",
        ] in archive
        assert ["setfacl", "-R", "-m", f"u:{eng2}:rx,d:u:{eng2}:rx", f"/srv/firm/{work}"] in hire
    assert ["setfacl", "-m", f"u:{founder}:x", "/srv/firm/org/.retired"] in archive
    assert not any(ops in " ".join(argv) for argv in archive + hire)
    assert server.workspace_path("r-eng") == "org/.retired/r-eng/work"
    assert server.workspace_path("r-api") == "org/.retired/r-eng/api/work"
    assert (server.tree / "org/.retired/r-eng/work/notes.md").read_text() == "old"
    assert not (server.tree / "org/founder/eng/work/notes.md").exists()
    assert server.role_at("founder/eng") == "r-eng2"
    assert server.role_at("founder/eng/api") is None
    ledger = json.loads(server.record.read_text())
    assert ledger["pending_archive"] is None and ledger["roles"]["r-api"]["archived"]
    server.log.clear()
    server.start()
    grants = dict(server.log)["grants"]
    archived = [argv for argv in grants if ".retired" in argv[-1]]
    assert archived and all(founder in argv[-2] or eng2 in argv[-2] for argv in archived)
    assert all(argv[-2].endswith(("rx", ":x")) for argv in archived)
    accounts = json.loads(server.record.read_text())["accounts"]
    for role in ("r-eng", "r-api"):
        assert not any(accounts[role]["account"] in " ".join(argv) for argv in grants)


def test_start_replays_directories_and_accounts_lost_before_the_guest(server):
    server.start()
    server.provision("r-one", Organization("one"))
    import shutil

    shutil.rmtree(server.tree / "org/one")
    server.log.clear()
    server.start()
    actions = [action for action, detail in server.log if isinstance(detail, list)]
    assert actions == ["setup", "provision", "account", "grants"]
    grants = dict(server.log)["grants"]
    assert grants[:3] == [
        ["setfacl", "-R", "-b", "/srv/firm/org"],
        ["setfacl", "-R", "-b", "/srv/firm/shared"],
        ["setfacl", "-m", "g:firmfiles:x", "/srv/firm"],
    ]
    assert (server.tree / "org/one/work").is_dir()
    account = dict(server.log)["account"]
    assert ["smbpasswd", "-s", "-a", server.credentials("r-one").user] in account


def test_owner_access_needs_a_started_server(server):
    with pytest.raises(RuntimeError, match="not started"):
        server.owner_access()


def test_interrupted_archive_finishes_on_the_next_provision(server):
    server.start()
    server.provision("r-eng", Organization("founder/eng"))
    server.retire_role("r-eng")
    ledger = json.loads(server.record.read_text())
    ledger["pending_archive"] = {
        "role": "r-eng",
        "replacement": "r-eng2",
        "source": "org/founder/eng",
    }
    server._save(ledger)
    with pytest.raises(ValueError, match="must finish"):
        server.provision("r-other", Organization("founder/web"))
    server.provision("r-eng2", Organization("founder/eng"), replace_role_id="r-eng")
    assert (server.tree / "org/.retired/r-eng/work").is_dir()
    assert server.role_at("founder/eng") == "r-eng2"


def test_profiles_are_read_in_one_call_and_bounded(server):
    server.start()
    assert server.profiles() == {}
    server.provision("r-one", Organization("one"))
    server.provision("r-two", Organization("two"))
    (server.tree / "org/one/work/profile.md").write_text("hello")
    (server.tree / "org/two/work/profile.md").symlink_to(server.record)
    result = server.profiles()
    assert result["r-one"]["status"] == "available" and result["r-one"]["text"] == "hello"
    assert len(result["r-one"]["sha256"]) == 64
    assert result["r-two"] == {"status": "unreadable"}
    (server.tree / "org/two/work/profile.md").unlink()
    assert server.profiles()["r-two"] == {"status": "missing"}
    assert [action for action, _ in server.log].count("profiles") == 2


def test_work_files_are_created_in_the_workspace_without_replacing_or_following_links(server):
    server.start()
    server.provision("r-one", Organization("one"))
    first = server.write_work_file("r-one", "captures", "20260916T142305Z-app.png", b"one")
    assert first == "org/one/work/captures/20260916T142305Z-app.png"
    again = server.write_work_file("r-one", "captures", "20260916T142305Z-app.png", b"two")
    assert again == "org/one/work/captures/20260916T142305Z-app-2.png"
    assert (server.tree / first).read_bytes() == b"one"
    assert (server.tree / again).read_bytes() == b"two"
    # A link in the role-writable tree is never followed as root.
    elsewhere = server.tree / "elsewhere"
    elsewhere.mkdir()
    (server.tree / "org/one/work/links").symlink_to(elsewhere)
    with pytest.raises(OSError):
        server.write_work_file("r-one", "links", "a.png", b"x")
    assert list(elsewhere.iterdir()) == []
    for directory, name in (("../x", "a.png"), ("captures", "../a.png"), ("captures", "a")):
        with pytest.raises(ValueError):
            server.write_work_file("r-one", directory, name, b"x")
    with pytest.raises(ValueError, match="size limit"):
        server.write_work_file("r-one", "captures", "a.png", b"x" * (WORK_FILE_LIMIT + 1))
    with pytest.raises(RuntimeError, match="no current workspace"):
        server.write_work_file("r-two", "captures", "a.png", b"x")


def test_the_largest_work_file_fits_one_guest_request():
    import base64
    import json

    from firm_sandbox.guest_control import MAX_FILE

    request = {
        "operation": "files",
        "root": "/srv/firm",
        "steps": [
            {
                "action": "write",
                "directory": "org/" + "/".join(["a" * 64] * 16) + "/work/captures",
                "name": "n" * 120 + ".png",
                "data": base64.b64encode(b"x" * WORK_FILE_LIMIT).decode(),
            }
        ],
    }
    assert len(json.dumps(request).encode()) <= 2 * MAX_FILE


def test_discard_forgets_everything_with_the_guest_disk(server):
    server.start()
    provision(server, "r-one")
    server.discard()
    assert not server.record.exists()
    with pytest.raises(RuntimeError, match="Start the file server"):
        provision(server, "r-one")


def test_credentials_are_a_start_request_secret_shape(server):
    server.start()
    request = provision(server, "r-one").request()
    assert set(request) == {"user", "password", "port", "share"}
    assert isinstance(request["port"], int) and request["share"] == SHARE


def test_runner_failures_surface_as_errors(tmp_path):
    (tmp_path / "firm").mkdir()
    config = Config(tmp_path / "firm", firm_id="demo", mac_image="/private/base")

    def broken(request):
        raise RuntimeError("Samba is not installed")

    server = FileServer(config, broken, forwards=lambda port: [])
    with pytest.raises(RuntimeError, match="Samba"):
        server.start()
    assert server.record.exists()  # the port survives so the retry forwards the same one
    assert not hasattr(SimpleNamespace(), "credentials")


def test_reconnect_restores_forwards_without_touching_live_accounts_or_grants(server):
    server.start()
    port = json.loads(server.record.read_text())["port"]
    before = server.record.read_bytes()
    server.log.clear()
    server.reconnect()
    assert server.log == [("start", port)]
    assert server.record.read_bytes() == before


def test_reconnect_requires_an_existing_file_server(server):
    with pytest.raises(RuntimeError, match="not started"):
        server.reconnect()
    assert server.log == []


def test_owner_browse_is_bounded_and_does_not_follow_guest_links(server, tmp_path):
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    provision(server, "r-one")
    root = server.workspace_path("r-one")
    (server.tree / root / "weekly-planner.html").write_bytes(b"<h1>Plan</h1>")
    (server.tree / root / "outside").symlink_to(tmp_path, target_is_directory=True)
    (server.tree / "shared" / "deliverable.txt").write_text("done")

    assert server.browse_roots() == [
        {"path": "shared", "label": "Shared deliverables", "address": None},
        {"path": root, "label": "r-one", "address": "r-one"},
    ]
    listing = server.browse_list(root)
    assert {entry["name"]: entry["type"] for entry in listing["entries"]} == {
        "outside": "unsupported",
        "weekly-planner.html": "file",
    }
    assert server.browse_read(root, "weekly-planner.html") == {
        "status": "available",
        "data": "PGgxPlBsYW48L2gxPg==",
        "size": 13,
    }
    assert server.browse_list("shared")["entries"][0]["name"] == "deliverable.txt"
    with pytest.raises(ValueError, match="outside"):
        server.browse_list("org/r-one/home")
    with pytest.raises(ValueError, match="Invalid"):
        server.browse_list(f"{root}/../shared")
    with pytest.raises(ValueError, match="Invalid"):
        server.browse_read(root, "../outside")


def test_owner_browse_rejects_symlinks_fifos_hardlinks_and_traversal(server, tmp_path):
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    provision(server, "r-one")
    root = server.workspace_path("r-one")
    workspace = server.tree / root
    secret = tmp_path / "outside-secret"
    secret.write_text("private")
    (workspace / "symlink").symlink_to(secret)
    os.mkfifo(workspace / "fifo")
    (workspace / "hardlink").hardlink_to(secret)
    assert server.browse_read(root, "symlink")["status"] == "unsupported"
    assert server.browse_read(root, "fifo")["status"] == "unsupported"
    assert server.browse_read(root, "hardlink")["status"] == "unsupported"
    assert {row["name"]: row["type"] for row in server.browse_list(root)["entries"]} == {
        "fifo": "unsupported",
        "hardlink": "unsupported",
        "symlink": "unsupported",
    }
    for path in ("../shared", "shared/../../outside"):
        with pytest.raises(ValueError):
            server.run(
                {
                    "operation": "files",
                    "root": "/srv/firm",
                    "steps": [{"action": "browse-list", "path": path}],
                }
            )


def test_archived_reference_permissions_follow_current_reporting_line(server, monkeypatch):
    server.start()
    for role, address in [
        ("r-founder", "founder"), ("r-old", "founder/analyst"),
        ("r-sibling", "founder/analyst-two"),
    ]:
        server.provision(role, Organization(address))
    server.retire_role("r-old")
    server.provision("r-new", Organization("founder/analyst"), replace_role_id="r-old")
    server.provision("r-child", Organization("founder/analyst/reviewer"))
    path = server.workspace_path("r-old") + "/reports/not-created"
    monkeypatch.setattr(server, "run", lambda request: pytest.fail("contacted guest"))
    for role in ("r-founder", "r-new", "r-child"):
        assert server.validate_role_file(role, path, "missing.md") == (path, "missing.md")
    for role in ("r-old", "r-unknown", "r-sibling"):
        with pytest.raises(ValueError, match="no file access|readable workspaces"):
            server.validate_role_file(role, path, "missing.md")
    assert server.validate_browse_file(path, "missing.md") == (path, "missing.md")


def test_owner_browse_reports_large_files_without_returning_contents(server):
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    provision(server, "r-one")
    root = server.workspace_path("r-one")
    (server.tree / root / "large.bin").write_bytes(b"x" * (6 * 1024 * 1024 + 1))
    assert server.browse_read(root, "large.bin")["status"] == "too_large"


def test_owner_browse_listing_stops_after_its_bound(server):
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    provision(server, "r-one")
    root = server.workspace_path("r-one")
    workspace = server.tree / root
    for number in range(250):
        (workspace / f"entry-{number:03}.txt").touch()
    result = server._remote({"action": "browse-list", "path": root, "limit": 17})[0]
    assert len(result["entries"]) == 17
    assert result["truncated"] is True
