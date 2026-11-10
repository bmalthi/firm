import fcntl
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from firm_runtime import fleet
from firm_runtime.cli import main


class FakeSandbox:
    """Shared filesystem markers model stopped and running VMs."""

    def __init__(self, config):
        self.config = config

    def state(self):
        if (self.config.root / "running").exists():
            return "running"
        return "stopped"


def _mark_sandbox(directory, *, state):
    """Write a minimal sandbox.toml and a running marker matching FakeSandbox's rules."""
    (directory / "sandbox.toml").write_text("")
    if state == "running":
        (directory / "running").touch()


def _write_controlled_setup_files(directory):
    directory.mkdir()
    (directory / "mandate.md").write_text("Build a useful firm.\n")
    (directory / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    (directory / "sandbox.toml").write_text(f'[firm]\nid = "{directory.name}"\n')


def _invoke(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["firm", *map(str, args)])
    main()
    return json.loads(capsys.readouterr().out)


def _init_firm(tmp_path, monkeypatch, capsys, firm_id):
    mandate = tmp_path / f"{firm_id}-mandate.md"
    mandate.write_text("Build a useful firm.")
    config = tmp_path / f"{firm_id}-firm.toml"
    config.write_text(
        'max_active = 2\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    directory = tmp_path / firm_id
    _invoke(
        monkeypatch,
        capsys,
        "init",
        directory,
        "--firm-id",
        firm_id,
        "--mandate",
        mandate,
        "--config",
        config,
    )
    return directory


def _insert_active_activation(database):
    conn = sqlite3.connect(str(database))
    try:
        conn.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,"
            "wake_snapshot,context_json,created_at) "
            "VALUES('a1','founder','s1','admitted','[]','{}',0)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("firm_id", ["demo", "2026-09-09_game-studio"])
def test_owner_cli_initializes_and_queues_without_execution(tmp_path, monkeypatch, capsys, firm_id):
    mandate = tmp_path / "mandate.md"
    mandate.write_text("Build a useful firm.")
    config = tmp_path / "firm.toml"
    config.write_text(
        'max_active = 2\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    directory = tmp_path / firm_id

    def invoke(*args):
        return _invoke(monkeypatch, capsys, *args)

    initial = invoke(
        "init", directory, "--firm-id", firm_id, "--mandate", mandate, "--config", config
    )
    assert initial["firm"]["state"] == "stopped"
    assert initial["firm"]["firm_id"] == firm_id
    assert (
        invoke("init", directory, "--firm-id", firm_id, "--mandate", mandate, "--config", config)
        == initial
    )
    assert (directory / "durable").stat().st_mode & 0o777 == 0o700
    assert directory.stat().st_mode & 0o777 == 0o700
    sent = invoke("send", directory, "founder", "--body", "Owner request", "--request-id", "mail")
    assert sent["message_id"] == "m1"
    status = invoke("status", directory)
    assert status["state"] == "stopped" and status["active"] == []
    assert "sandbox_state" not in status


def test_status_does_not_create_an_unknown_firm(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["firm", "status", str(directory)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "does not exist" in capsys.readouterr().err
    assert not directory.exists()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_starter_configuration_initializes_using_directory_defaults(
    tmp_path, monkeypatch, capsys, provider
):
    from firm_runtime import accounts
    from firm_runtime.onboarding import generated_files

    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "credentials")
    examples = Path(__file__).resolve().parents[3] / "examples" / "miniature-city"
    directory = tmp_path / "2026-09-22_miniature-city"
    directory.mkdir()
    files = generated_files(directory, provider, (examples / "mandate.md").read_text())
    for name in ("firm.toml", "mandate.md"):
        path = directory / name
        path.write_text(files[path])
    result = _invoke(monkeypatch, capsys, "init", directory)
    assert result["firm"]["firm_id"] == directory.name
    assert result["firm"]["state"] == "stopped"
    assert _invoke(monkeypatch, capsys, "init", directory) == result
    assert _invoke(monkeypatch, capsys, "status", directory)["active"] == []


def test_init_missing_inputs_does_not_leave_an_empty_database(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "new-firm"
    monkeypatch.setattr(sys, "argv", ["firm", "init", str(directory)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "mandate.md" in capsys.readouterr().err
    assert not directory.exists()


def test_init_resumes_a_marked_database_schema_before_firm_record(tmp_path, monkeypatch, capsys):
    from firm_runtime.compatibility import initialize_firm
    from firm_runtime.store import Store

    directory = tmp_path / "interrupted"
    mandate = tmp_path / "interrupted-mandate.md"
    mandate.write_text("Build a useful firm.\n")
    config = tmp_path / "interrupted-firm.toml"
    config.write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    directory.mkdir()
    (directory / "mandate.md").write_text(mandate.read_text())
    (directory / "firm.toml").write_text(config.read_text())
    initialize_firm(directory)
    database = directory / "durable" / "firm.sqlite3"
    Store(database)

    result = _invoke(monkeypatch, capsys, "init", directory)

    assert result["firm"]["firm_id"] == directory.name
    assert result["firm"]["state"] == "stopped"
    assert (directory / "firm-release.json").is_file()


def test_init_never_stamps_or_opens_an_unmarked_legacy_database(tmp_path, monkeypatch, capsys):
    from firm_runtime.store import Store

    directory = tmp_path / "legacy"
    mandate = tmp_path / "legacy-mandate.md"
    mandate.write_text("Build a useful firm.\n")
    config = tmp_path / "legacy-firm.toml"
    config.write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    directory.mkdir()
    (directory / "mandate.md").write_text(mandate.read_text())
    (directory / "firm.toml").write_text(config.read_text())
    database = directory / "durable" / "firm.sqlite3"
    Store.scratch(database)
    assert not (directory / "firm-release.json").exists()

    monkeypatch.setattr(sys, "argv", ["firm", "init", str(directory)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "no release compatibility record" in capsys.readouterr().err.lower()
    assert not (directory / "firm-release.json").exists()


@pytest.mark.parametrize("filename", ["mandate.md", "firm.toml"])
@pytest.mark.parametrize("invalid", ["fifo", "oversized"])
def test_init_rejects_invalid_control_files_without_blocking_or_creating_state(
    tmp_path, filename, invalid
):
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "mandate.md").write_text("Build useful things.")
    (directory / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    bad_input = directory / filename
    bad_input.unlink()
    if invalid == "fifo":
        os.mkfifo(bad_input)
    else:
        bad_input.write_bytes(b"x" * (1024 * 1024 + 1))

    result = subprocess.run(
        [sys.executable, "-m", "firm_runtime.cli", "init", str(directory)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert filename in result.stderr
    assert not (directory / "durable").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "writable_by_others"])
def test_init_checks_directory_before_registering_github(tmp_path, monkeypatch, capsys, unsafe):
    from firm_runtime import github

    target = tmp_path / "target"
    target.mkdir()
    (target / "mandate.md").write_text("Build useful things.")
    (target / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n'
        '[profiles.controlled]\n[github]\norganization = "firmery"\n'
    )
    directory = target
    if unsafe == "symlink":
        directory = tmp_path / "firm-link"
        directory.symlink_to(target, target_is_directory=True)
    else:
        target.chmod(0o770)
    registrations = []
    monkeypatch.setattr(github, "ensure_app", lambda *args: registrations.append(args))
    monkeypatch.setattr(sys, "argv", ["firm", "init", str(directory)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert registrations == []
    assert not (target / "durable").exists()


@pytest.mark.parametrize("command", ["init", "start"])
def test_valid_marker_does_not_allow_durable_symlink_redirection(
    tmp_path, monkeypatch, capsys, command
):
    from firm_runtime.compatibility import initialize_firm

    directory = tmp_path / "firm"
    _write_controlled_setup_files(directory)
    initialize_firm(directory)
    external = tmp_path / "external"
    durable = external / "durable"
    durable.mkdir(parents=True)
    database = durable / "firm.sqlite3"
    database.write_bytes(b"external database sentinel")
    before = (database.read_bytes(), durable.stat().st_mode, durable.stat().st_mtime_ns)
    (directory / "durable").symlink_to(durable, target_is_directory=True)

    monkeypatch.setattr(sys, "argv", ["firm", command, str(directory)])
    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    assert (database.read_bytes(), durable.stat().st_mode, durable.stat().st_mtime_ns) == before
    assert (directory / "durable").is_symlink()


def test_check_refuses_durable_symlink_before_opening_external_database(tmp_path, monkeypatch):
    from firm_runtime.compatibility import initialize_firm
    from firm_runtime.setup_check import check_setup

    directory = tmp_path / "firm"
    _write_controlled_setup_files(directory)
    initialize_firm(directory)
    durable = tmp_path / "external" / "durable"
    durable.mkdir(parents=True)
    database = durable / "firm.sqlite3"
    database.write_bytes(b"external database sentinel")
    before = (database.read_bytes(), durable.stat().st_mode, durable.stat().st_mtime_ns)
    (directory / "durable").symlink_to(durable, target_is_directory=True)
    monkeypatch.setattr(
        "firm_runtime.setup_check._check_host_platform",
        lambda: {"check": "host_platform", "status": "good", "message": "test"},
    )
    monkeypatch.setattr(
        "firm_runtime.setup_check._check_lima",
        lambda: {"check": "lima", "status": "good", "message": "test"},
    )
    monkeypatch.setattr("firm_runtime.setup_check._check_vm_resources", lambda *args: [])
    monkeypatch.setattr(
        "firm_runtime.setup_check._check_linux_base",
        lambda directory: {"check": "linux_base", "status": "good", "message": "test"},
    )

    report = check_setup(directory, check_ports=False)
    database_check = next(item for item in report["checks"] if item["check"] == "database")
    assert database_check["status"] == "failure"
    assert "durable parent" in database_check["message"]
    assert (database.read_bytes(), durable.stat().st_mode, durable.stat().st_mtime_ns) == before


def test_check_reports_linux_base_age_only_for_a_firm_without_a_vm(tmp_path, monkeypatch):
    from firm_sandbox import linux_base

    from firm_runtime.setup_check import _check_linux_base

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(linux_base, "cache_directory", lambda: cache)
    firm = tmp_path / "firm"
    assert "builds one" in _check_linux_base(firm)["message"]
    image = cache / f"{linux_base.image_key()}.img"
    image.touch()
    assert _check_linux_base(firm)["status"] == "good"
    old = time.time() - (linux_base.MAX_AGE_DAYS + 1) * 86400
    os.utime(image, (old, old))
    report = _check_linux_base(firm)
    assert report["status"] == "warning" and str(cache) in report["action"]
    (firm / "durable" / "linux").mkdir(parents=True)
    (firm / "durable" / "linux" / "vm.json").write_text("{}")
    assert _check_linux_base(firm)["status"] == "skipped"


def test_start_uses_local_sandbox_config_and_reports_startup_failure(tmp_path, monkeypatch, capsys):
    from firm_runtime import foreground

    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")

    def unavailable(runtime, *, sandbox_config, **kwargs):
        assert sandbox_config == directory / "sandbox.toml"
        raise RuntimeError("Lima is not installed")

    monkeypatch.setattr(foreground, "serve", unavailable)
    monkeypatch.setattr(sys, "argv", ["firm", "start", str(directory), "--foreground"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "Lima is not installed" in capsys.readouterr().err


def test_start_uses_detached_local_host_by_default(tmp_path, monkeypatch, capsys):
    from firm_runtime import foreground

    directory = _init_firm(tmp_path, monkeypatch, capsys, firm_id="demo")
    received = {}

    def launch(firm_directory, **kwargs):
        received["directory"] = firm_directory
        received.update(kwargs)
        return {"url": "http://127.0.0.1:8766/", "reused": False}

    monkeypatch.setattr(foreground, "launch_local_host", launch)
    result = _invoke(monkeypatch, capsys, "start", directory, "--no-browser")
    assert result == {"url": "http://127.0.0.1:8766/", "reused": False}
    assert received["directory"] == directory
    assert received["sandbox_config"] == directory / "sandbox.toml"
    assert received["open_browser"] is False


def test_guided_start_creates_starter_firm_and_starts_it(tmp_path, monkeypatch, capsys):
    import builtins

    import firm_runtime.cli as cli

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    answers = iter(["1", "1"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    data_dir = tmp_path / "Firm Data"
    monkeypatch.setattr(
        cli, "login_provider", lambda provider, account, **kwargs: data_dir / "credential.json"
    )
    monkeypatch.setattr(
        "firm_runtime.setup_check.check_setup", lambda *args, **kwargs: {"ok": True, "checks": []}
    )
    launch = {}

    def fake_launch(directory, **kwargs):
        launch["directory"] = directory
        launch.update(kwargs)
        return {
            "pid": 12345,
            "url": "http://127.0.0.1:8766/",
            "unlock_url": "http://127.0.0.1:8766/unlock/test-key",
            "reused": False,
        }

    monkeypatch.setattr("firm_runtime.foreground.launch_local_host", fake_launch)

    cli.main(["start", "--data-dir", str(data_dir)])

    output = capsys.readouterr().out

    from firm_runtime import compatibility

    firm = launch["directory"]
    assert firm.parent == data_dir
    assert (firm / "mandate.md").read_text().startswith("# Make a useful weekly planner")
    assert 'model = "gpt-6-luna"' in (firm / "firm.toml").read_text()
    assert (firm / "durable" / "firm.sqlite3").is_file()
    assert compatibility.validate_firm(firm)["compatibility_generation"] > 0
    assert "Owner console: http://127.0.0.1:8766/unlock/test-key" in output
    assert "Owner console is ready; the firm is starting." in output
    assert f"Firm data directory: {firm}" in output
    assert "Use Stop to stop execution, then Close Firm to close the host." in output
    assert f"Resume later with: firm start '{firm}'" in output
    assert '"pid"' not in output and '"firm_id"' not in output


def test_guided_start_refuses_symlinked_data_root_without_chmodding_target(tmp_path, monkeypatch):
    import io
    import sys

    import firm_runtime.cli as cli

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    target = tmp_path / "real-data"
    target.mkdir(mode=0o755)
    alias = tmp_path / "firm-data"
    alias.symlink_to(target, target_is_directory=True)
    before = target.stat().st_mode & 0o777
    monkeypatch.setattr(sys, "stdin", FakeTTY())

    with pytest.raises(SystemExit) as error:
        cli.main(["start", "--data-dir", str(alias)])

    assert error.value.code == 2
    assert target.stat().st_mode & 0o777 == before
    assert list(target.iterdir()) == []


def test_guided_resume_checks_credentials_without_rejecting_live_console_ports(
    tmp_path, monkeypatch, capsys
):
    import builtins

    import firm_runtime.cli as cli
    from firm_runtime import accounts, foreground, onboarding
    from firm_runtime.credential_store import CredentialStore

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "credentials")
    root = tmp_path / "firms"
    firm = root / "2026-10-28_codex"
    onboarding.create_firm_files(firm, "codex", "Build a useful firm.")
    _invoke(monkeypatch, capsys, "init", firm)
    CredentialStore("codex", accounts.credentials_path("codex-personal")).save(
        json.dumps({"tokens": {"access_token": "test", "refresh_token": "refresh"}}).encode()
    )
    answers = iter(["2"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    checked = []
    monkeypatch.setattr(
        "firm_runtime.setup_check.check_setup",
        lambda *args, **kwargs: checked.append(kwargs["check_ports"]) or {"ok": True, "checks": []},
    )
    launched = []
    monkeypatch.setattr(
        foreground,
        "launch_local_host",
        lambda directory, **kwargs: launched.append(directory)
        or {"url": "http://127.0.0.1:8766/", "unlock_url": "http://127.0.0.1:8766/unlock/key"},
    )

    cli.main(["start", "--data-dir", str(root)])

    assert checked == [False, False]
    assert launched == [firm]


@pytest.mark.parametrize("login_succeeds", [True, False])
def test_guided_resume_refreshes_only_selected_account_auth_incidents(
    tmp_path, monkeypatch, capsys, login_succeeds
):
    import builtins

    import firm_runtime.cli as cli
    from firm_runtime import foreground, onboarding
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import OWNER, Store

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    root = tmp_path / "firms"
    firm = root / "2026-10-28_codex"
    onboarding.create_firm_files(firm, "codex", "Build a useful firm.")
    configured_auth = tmp_path / "credentials" / "chosen-auth.json"
    unrelated_auth = tmp_path / "credentials" / "other-auth.json"
    with (firm / "firm.toml").open("a", encoding="utf-8") as output:
        output.write(
            f'auth_file = "{configured_auth}"\n\n'
            '[accounts.codex-other]\nprovider = "codex"\nmax_active = 1\n'
            f'auth_file = "{unrelated_auth}"\n'
        )
    _invoke(monkeypatch, capsys, "init", firm)
    runtime = Runtime(Store(firm / "durable" / "firm.sqlite3"))
    selected_auth = runtime.accounts.pause(
        OWNER, "account", "codex-personal", "auth"
    )["incident_id"]
    other_auth = runtime.accounts.pause(OWNER, "account", "codex-other", "auth")["incident_id"]
    profile_config = runtime.accounts.pause(OWNER, "profile", "codex", "config")["incident_id"]

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "2")
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    monkeypatch.setattr(
        "firm_runtime.setup_check.check_setup", lambda *args, **kwargs: {"ok": True, "checks": []}
    )
    login = {}

    def refresh(provider, account, **kwargs):
        login.update(provider=provider, account=account, **kwargs)
        if not login_succeeds:
            raise cli.FirmError("simulated sign-in failure")
        return configured_auth

    monkeypatch.setattr(cli, "login_provider", refresh)
    launched = []
    monkeypatch.setattr(
        foreground,
        "launch_local_host",
        lambda directory, **kwargs: launched.append(directory)
        or {"url": "http://127.0.0.1:8766/", "unlock_url": "http://127.0.0.1:8766/unlock/key"},
    )

    if login_succeeds:
        cli.main(["start", "--data-dir", str(root)])
    else:
        with pytest.raises(SystemExit) as error:
            cli.main(["start", "--data-dir", str(root)])
        assert error.value.code == 2

    assert login["provider"] == "codex" and login["account"] == "codex-personal"
    assert login["target_credentials_path"] == configured_auth
    assert login["force_login"] is True
    incidents = {item["incident_id"] for item in runtime.accounts.status()["incidents"]}
    assert (selected_auth in incidents) is (not login_succeeds)
    assert other_auth in incidents
    assert profile_config in incidents
    assert launched == ([firm] if login_succeeds else [])


def test_save_credentials_installs_private_default_file(tmp_path, monkeypatch, capsys):
    from firm_runtime import accounts

    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "credentials")
    target = tmp_path / "credentials" / "claude-personal.json"
    bundle = b'{"claudeAiOauth":{"accessToken":"secret","expiresAt":1}}\n'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(bundle)))
    result = _invoke(
        monkeypatch, capsys, "save-credentials", "claude-personal", "--provider", "claude"
    )
    assert result["auth_file"] == str(target)
    assert target.read_bytes() == bundle
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert "secret" not in json.dumps(result)

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.setattr(
        sys, "argv", ["firm", "save-credentials", "claude-personal", "--provider", "codex"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "not usable codex credentials" in capsys.readouterr().err
    assert target.read_bytes() == bundle


@pytest.mark.parametrize("terminal", [False, True])
def test_init_registers_a_github_app_only_from_a_terminal(tmp_path, monkeypatch, capsys, terminal):
    from firm_runtime import github

    monkeypatch.setattr(github, "CREDENTIALS_DIR", tmp_path / "credentials")
    registered = []
    monkeypatch.setattr(github, "register", registered.append)
    monkeypatch.setattr(
        github,
        "connect",
        lambda settings: {
            "app_id": 1,
            "organization": "firmery",
            "installation_id": 42,
            "bot_login": "firmery-roles[bot]",
            "bot_user_id": 777,
        },
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: terminal)
    mandate = tmp_path / "mandate.md"
    mandate.write_text("Build a useful firm.")
    config = tmp_path / "firm.toml"
    config.write_text(
        'max_active = 2\nstaffing = false\nfounder_profile = "controlled"\n'
        '[profiles.controlled]\n[github]\norganization = "firmery"\n'
    )
    arguments = ["init", tmp_path / "demo", "--firm-id", "demo", "--mandate", mandate]
    if terminal:
        _invoke(monkeypatch, capsys, *arguments, "--config", config)
        assert registered == ["firmery"]
        return
    monkeypatch.setattr(sys, "argv", ["firm", *map(str, arguments), "--config", str(config)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "run this command in a terminal" in capsys.readouterr().err
    assert registered == []


def test_github_revoke_and_grant_move_access_for_a_repository_the_firm_created(
    tmp_path, monkeypatch, capsys
):
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    conn = sqlite3.connect(str(directory / "durable" / "firm.sqlite3"))
    try:
        conn.execute(
            "INSERT INTO github_repositories "
            "SELECT 'firmery/demo-app',employee_id,'The app',0 FROM roles LIMIT 1"
        )
        conn.executemany(
            "INSERT INTO github_grants VALUES(?,?,'write',?,0)",
            [
                ("firmery/demo-app", "founder", "created"),
                ("firmery/demo-app", "founder/web", "requested"),
            ],
        )
        # Granting asks GitHub whether the App reaches the repository.
        conn.execute(
            "UPDATE firm SET config_json=json_set(config_json,'$.github',json(?))",
            ('{"organization":"firmery","auto_create":true,"max_repositories":20,"grants":[]}',),
        )
        conn.execute("INSERT INTO github_app VALUES(1,1,'firmery',42,'firmery-roles[bot]',7)")
        conn.commit()
    finally:
        conn.close()
    from firm_runtime import github

    probes = []
    monkeypatch.setattr(github, "_mint", lambda *args: probes.append(args) or ("probe", 0))
    monkeypatch.setattr(github, "_revoke", lambda token: None)
    one = _invoke(
        monkeypatch,
        capsys,
        "github",
        "revoke",
        directory,
        "firmery/Demo-App",
        "--address",
        "founder/web",
    )
    assert one == {
        "resource": "firmery/demo-app",
        "status": "revoked",
        "address": "founder/web",
        "revoked": ["founder/web"],
        "withdrawn": [],
    }
    rest = _invoke(monkeypatch, capsys, "github", "revoke", directory, "firmery/demo-app")
    assert rest["revoked"] == ["founder", "founder/web"]
    # Granting one address back would lift the firm-wide revocation for everyone.
    monkeypatch.setattr(
        sys,
        "argv",
        ["firm", "github", "grant", str(directory), "firmery/demo-app", "--address", "founder/web"],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert "is revoked for every role, which covers founder/web" in capsys.readouterr().err
    restored = _invoke(monkeypatch, capsys, "github", "grant", directory, "firmery/demo-app")
    assert restored == {
        "resource": "firmery/demo-app",
        "status": "granted",
        "address": None,
        "cleared": ["every role", "founder/web"],
        "granted": None,
        "answered": [],
    }
    assert len(probes) == 2
    monkeypatch.setattr(
        sys, "argv", ["firm", "github", "revoke", str(directory), "firmery/elsewhere"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "not a repository this firm created" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["invalid_profile", "remove_current_profile"])
def test_configure_rejects_local_errors_before_registering_github(
    tmp_path, monkeypatch, capsys, change
):
    from firm_runtime import github

    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    config = tmp_path / "replacement.toml"
    profile = "absent" if change == "invalid_profile" else "replacement"
    config.write_text(
        f'max_active = 2\nstaffing = false\nfounder_profile = "{profile}"\n'
        '[profiles.replacement]\n[github]\norganization = "firmery"\n'
    )
    registrations = []
    monkeypatch.setattr(github, "ensure_app", lambda *args: registrations.append(args))
    monkeypatch.setattr(sys, "argv", ["firm", "configure", str(directory), "--config", str(config)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert registrations == []
    message = capsys.readouterr().err
    assert ("founder_profile" if change == "invalid_profile" else "retain") in message


@pytest.mark.parametrize("flags,expected", [([], 1800), (["--timeout", "90"], 90)])
def test_start_passes_idle_timeout_to_foreground(tmp_path, monkeypatch, capsys, flags, expected):
    from firm_runtime import foreground

    received = {}
    monkeypatch.setattr(foreground, "serve", lambda runtime, **kwargs: received.update(kwargs))
    directory = _init_firm(tmp_path, monkeypatch, capsys, firm_id="demo")
    sandbox = tmp_path / "custom-sandbox.toml"
    sandbox.write_text("custom")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm",
            "start",
            str(directory),
            "--sandbox-config",
            str(sandbox),
            "--foreground",
            *flags,
        ],
    )
    main()
    assert received["timeout"] == expected
    assert received["wait_for_resume"] is True
    assert received["sandbox_config"] == sandbox


def test_status_reports_sandbox_state_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    _mark_sandbox(directory, state="stopped")
    status = _invoke(monkeypatch, capsys, "status", directory)
    assert status["sandbox_state"] == "stopped"
    assert "sandbox_error" not in status


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_status_reports_host_startup_error_until_cleared(tmp_path, monkeypatch, capsys, flags):
    from starlette.testclient import TestClient

    from firm_runtime import foreground
    from firm_runtime.console import build_console_app, console_scope
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store

    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    _mark_sandbox(directory, state="stopped")
    runtime = Runtime(Store(directory / "durable" / "firm.sqlite3"))
    control = foreground.HostControl(lambda **kwargs: None)
    app = build_console_app(runtime, allowed_host="127.0.0.1:8766", control=control)
    record = {
        "pid": os.getpid(),
        "mcp_port": 8765,
        "console_port": 8766,
        "firm_id": "demo",
        "console_scope": console_scope(runtime.store.path, "demo"),
    }
    foreground._write_host_record(directory / "durable" / foreground.HOST_RECORD, record)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:

        class Response(io.BytesIO):
            status = 200

        def console_read(request, timeout):
            response = client.get(request.full_url, headers=dict(request.header_items()))
            assert response.status_code == 200
            return Response(response.content)

        monkeypatch.setattr(foreground.urllib.request, "urlopen", console_read)
        assert "startup_error" not in _invoke(monkeypatch, capsys, "status", directory, *flags)
        for message in (
            "Firm startup failed: provisioning. The host is confirming VM shutdown before "
            "allowing retry.",
            "Firm startup failed: provisioning. VM shutdown is confirmed; correct the cause, "
            "then retry.",
        ):
            control.set_startup_error(message)
            status = _invoke(monkeypatch, capsys, "status", directory, *flags)
            assert status["startup_error"] == message
            assert status["state"] == status["sandbox_state"] == "stopped"
        control.clear_startup_error()
        assert "startup_error" not in _invoke(monkeypatch, capsys, "status", directory, *flags)

    def offline(request, timeout):
        raise OSError("host is unavailable")

    monkeypatch.setattr(foreground.urllib.request, "urlopen", offline)
    assert "startup_error" not in _invoke(monkeypatch, capsys, "status", directory, *flags)


def test_status_reports_unknown_sandbox_state_on_broken_config(tmp_path, monkeypatch, capsys):
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    (directory / "sandbox.toml").write_text("[firm")
    status = _invoke(monkeypatch, capsys, "status", directory)
    assert status["sandbox_state"] == "unknown"
    assert status["sandbox_error"]


def test_delete_refuses_when_database_state_is_not_stopped(tmp_path, monkeypatch, capsys):
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    conn = sqlite3.connect(str(directory / "durable" / "firm.sqlite3"))
    conn.execute("UPDATE firm SET state='running'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["firm", "delete", str(directory), "--confirm", "demo"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "stop the firm" in capsys.readouterr().err


def test_delete_refuses_with_active_activations(tmp_path, monkeypatch, capsys):
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    _insert_active_activation(directory / "durable" / "firm.sqlite3")
    monkeypatch.setattr(sys, "argv", ["firm", "delete", str(directory), "--confirm", "demo"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "admitted or running" in capsys.readouterr().err


def test_delete_refuses_while_execution_host_lock_is_held(tmp_path, monkeypatch, capsys):
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    lock_dir = directory / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / "runtime-owner.lock").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(sys, "argv", ["firm", "delete", str(directory), "--confirm", "demo"])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
        assert "execution host owns" in capsys.readouterr().err
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def test_delete_refuses_when_vm_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    _mark_sandbox(directory, state="running")
    firm_id = _invoke(monkeypatch, capsys, "status", directory)["firm_id"]

    monkeypatch.setattr(sys, "argv", ["firm", "delete", str(directory), "--confirm", firm_id])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "stopped" in capsys.readouterr().err
    assert directory.exists()


def test_delete_refuses_with_wrong_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    _mark_sandbox(directory, state="stopped")

    monkeypatch.setattr(sys, "argv", ["firm", "delete", str(directory), "--confirm", "wrong-id"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "does not match" in capsys.readouterr().err
    assert directory.exists()


def test_delete_removes_a_stopped_firm(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    directory = _init_firm(tmp_path, monkeypatch, capsys, "demo")
    _mark_sandbox(directory, state="stopped")
    firm_id = _invoke(monkeypatch, capsys, "status", directory)["firm_id"]

    result = _invoke(monkeypatch, capsys, "delete", directory, "--confirm", firm_id)
    assert result == {"directory": str(directory), "firm_id": firm_id, "status": "deleted"}
    assert not directory.exists()
    assert tmp_path.exists()


def test_list_reports_directory_state_size_and_newest_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    alpha = _init_firm(tmp_path, monkeypatch, capsys, "alpha")
    beta = _init_firm(tmp_path, monkeypatch, capsys, "beta")

    _mark_sandbox(alpha, state="stopped")
    (alpha / "durable" / "firm.json").write_text("{}")
    (alpha / "durable" / "blob").write_bytes(b"x" * 4096)
    (alpha / "durable" / "events.jsonl").write_text(
        '{"time": "2020-01-01T00:00:00+00:00", "kind": "firm.stopped"}\n'
        '{"time": "2021-06-15T12:00:00+00:00", "kind": "firm.stopped"}\n'
    )

    _mark_sandbox(beta, state="running")
    (beta / "durable" / "firm.json").write_text("{}")

    # An immediate subdirectory without an identity file, and a dotted one, are both skipped.
    (tmp_path / "not-a-firm").mkdir()
    hidden = tmp_path / ".hidden"
    (hidden / "durable").mkdir(parents=True)
    (hidden / "durable" / "firm.json").write_text("{}")

    entries = _invoke(monkeypatch, capsys, "list", tmp_path)

    by_directory = {entry["directory"]: entry for entry in entries}
    assert set(by_directory) == {"alpha", "beta"}
    assert by_directory["alpha"]["firm_id"] == "alpha"
    assert by_directory["alpha"]["sandbox_state"] == "stopped"
    assert by_directory["alpha"]["newest_event_at"] == "2021-06-15T12:00:00+00:00"
    expected_size = sum(
        path.stat().st_size
        for path in (alpha / "durable").rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert by_directory["alpha"]["durable_bytes"] == expected_size
    assert by_directory["beta"]["firm_id"] == "beta"
    assert by_directory["beta"]["sandbox_state"] == "running"
    assert by_directory["beta"]["newest_event_at"] is None


def test_list_durable_size_ignores_symlinks(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    directory = _init_firm(tmp_path, monkeypatch, capsys, "alpha")
    _mark_sandbox(directory, state="stopped")
    (directory / "durable" / "firm.json").write_text("{}")

    def alpha_size():
        entries = _invoke(monkeypatch, capsys, "list", tmp_path)
        return next(e for e in entries if e["directory"] == "alpha")["durable_bytes"]

    # Take a baseline after the database has settled, then confirm that a symlink to a
    # much larger file outside durable/ contributes nothing once it is added.
    before = alpha_size()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"y" * 10_000)
    (directory / "durable" / "link").symlink_to(outside)

    assert alpha_size() == before


@pytest.mark.parametrize("mode,enabled", [("on", True), ("off", False), ("status", None)])
def test_owner_network_command_controls_only_selected_vm(
    tmp_path, monkeypatch, capsys, mode, enabled
):
    import firm_sandbox
    from runtime_test_support import FakeNetwork

    directory = _init_firm(tmp_path, monkeypatch, capsys, "2026-09-14_network-probe")
    (directory / "sandbox.toml").write_text("")
    calls = []

    class NetworkSandbox(FakeNetwork):
        def __init__(self, config):
            assert config.root == directory
            super().__init__()

        def set_network_enabled(self, value):
            calls.append(value)
            return super().set_network_enabled(value)

    monkeypatch.setattr(firm_sandbox, "Sandbox", NetworkSandbox)
    result = _invoke(monkeypatch, capsys, "network", directory, mode)
    if enabled is None:
        assert result["enabled"] is True and result["mode"] == "enforce"
        assert set(result) == {"enabled", "mode", "allowed_hosts", "egress", "vms"}
    else:
        assert result == {"linux": {"egress": enabled, "live": False}}
    assert calls == ([] if enabled is None else [enabled])


def test_owner_decides_requests_and_hosts_through_the_sandbox(tmp_path, monkeypatch, capsys):
    import firm_sandbox

    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store

    directory = _init_firm(tmp_path, monkeypatch, capsys, "2026-10-26_requests")
    (directory / "sandbox.toml").write_text("")
    from runtime_test_support import FakeNetwork

    writes = []

    class NetworkSandbox(FakeNetwork):
        def __init__(self, config):
            assert config.root == directory
            super().__init__()
            self.writes = writes

    monkeypatch.setattr(firm_sandbox, "Sandbox", NetworkSandbox)
    runtime = Runtime(Store(directory / "durable" / "firm.sqlite3"))
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "pypi.org", "Install packages", "ask-pypi")
    tools.request_access("network_host", "pastebin.com", "Share a log", "ask-paste")

    listing = _invoke(monkeypatch, capsys, "requests", directory)
    assert [item["resource"] for item in listing["pending"]] == ["pypi.org", "pastebin.com"]
    assert _invoke(monkeypatch, capsys, "requests", directory, "grant", "q1")["status"] == (
        "granted"
    )
    assert writes == [["pypi.org"]]
    denied = _invoke(
        monkeypatch, capsys, "requests", directory, "deny", "q2", "--reason", "Use the drive."
    )
    # A denial rewrites the recorded grants, removing anything a failed write left behind.
    assert denied["status"] == "denied" and writes == [["pypi.org"], ["pypi.org"]]
    _invoke(monkeypatch, capsys, "network", directory, "allow", "npmjs.org")
    assert writes[-1] == ["npmjs.org", "pypi.org"]
    _invoke(monkeypatch, capsys, "network", directory, "revoke", "pypi.org")
    assert writes[-1] == ["npmjs.org"]
    listing = _invoke(monkeypatch, capsys, "requests", directory)
    assert listing["pending"] == []
    assert {item["request_id"]: item["status"] for item in listing["recent"]} == {
        "q1": "withdrawn",
        "q2": "denied",
    }
    monkeypatch.setattr(sys, "argv", ["firm", "network", str(directory), "allow"])
    with pytest.raises(SystemExit):
        main()
    assert "take one host" in capsys.readouterr().err


def test_task_config_applied_by_init_and_configure(tmp_path, monkeypatch, capsys):
    from firm_runtime.accounts import TASK_DEFAULTS
    from firm_runtime.store import Store

    directory = _init_firm(tmp_path, monkeypatch, capsys, "task-config")
    store = Store(directory / "durable" / "firm.sqlite3")

    def stored():
        with store.read() as db:
            return json.loads(store.firm(db)["config_json"])

    assert stored()["task"] == TASK_DEFAULTS
    config = tmp_path / "replacement.toml"
    base = 'max_active = 2\nstaffing = false\nfounder_profile = "controlled"\n'
    config.write_text(
        base + "[profiles.controlled]\n[task]\nmax_concurrent = 1\n"
        '[task.claude]\nmodel = "custom-opus"\neffort = "high"\n'
        '[summary.codex]\nmodel = "summary-test"\neffort = "low"\n'
    )
    result = _invoke(monkeypatch, capsys, "configure", directory, "--config", config)
    assert result["status"] == "applied"
    assert stored()["task"] == {
        **TASK_DEFAULTS,
        "max_concurrent": 1,
        "claude": {"model": "custom-opus", "effort": "high"},
    }
    assert stored()["summary"]["codex"]["model"] == "summary-test"
    before = stored()
    config.write_text(base + "[profiles.controlled]\n[task]\nmax_concurrent = true\n")
    monkeypatch.setattr(sys, "argv", ["firm", "configure", str(directory), "--config", str(config)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "positive integer" in capsys.readouterr().err
    assert stored() == before
