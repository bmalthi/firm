import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from firm_runtime.cli import main
from firm_runtime.lifecycle import validate_config
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store


def _ports():
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(2)]
    try:
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return tuple(listener.getsockname()[1] for listener in sockets)
    finally:
        for listener in sockets:
            listener.close()


def _mock_supported_host(monkeypatch):
    monkeypatch.setattr(
        "firm_runtime.setup_check._check_host_platform",
        lambda: {"check": "host_platform", "status": "good", "message": "test Apple Silicon"},
    )
    monkeypatch.setattr(
        "firm_runtime.setup_check._check_lima",
        lambda: {"check": "lima", "status": "good", "message": "test Lima"},
    )
    monkeypatch.setattr("firm_runtime.setup_check._host_memory_gib", lambda: 32.0)
    monkeypatch.setattr(
        "firm_runtime.setup_check.shutil.disk_usage",
        lambda path: type("Disk", (), {"free": 128 * 1024**3})(),
    )


def _invoke(monkeypatch, capsys, directory, *, ports=None, fails=False):
    ports = ports or _ports()
    monkeypatch.setattr("firm_runtime.setup_check.shutil.which", lambda command: "/usr/bin/true")
    _mock_supported_host(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm",
            "check",
            str(directory),
            "--mcp-port",
            str(ports[0]),
            "--console-port",
            str(ports[1]),
        ],
    )
    if fails:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
    else:
        main()
    return json.loads(capsys.readouterr().out)


def _write_controlled_files(directory):
    directory.mkdir()
    (directory / "mandate.md").write_text("Build a useful firm.\n")
    (directory / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n[profiles.controlled]\n'
    )
    (directory / "sandbox.toml").write_text(f'[firm]\nid = "{directory.name}"\n')


def _checks(report):
    return {item["check"]: item for item in report["checks"]}


def test_host_platform_requires_arm_macos_and_lima_vz_os_baseline():
    from firm_runtime.setup_check import _check_host_platform

    assert (
        _check_host_platform(system="Darwin", machine="arm64", mac_version="13.0")["status"]
        == "good"
    )
    assert (
        _check_host_platform(system="Darwin", machine="x86_64", mac_version="15.0")["status"]
        == "failure"
    )
    assert (
        _check_host_platform(system="Darwin", machine="arm64", mac_version="12.7")["status"]
        == "failure"
    )
    assert (
        _check_host_platform(system="Linux", machine="aarch64", mac_version="")["status"]
        == "failure"
    )


def test_lima_check_requires_tested_minimum_and_reports_version(monkeypatch):
    from types import SimpleNamespace

    from firm_runtime.setup_check import _check_lima

    monkeypatch.setattr(
        "firm_runtime.setup_check.shutil.which", lambda _: "/opt/homebrew/bin/limactl"
    )
    monkeypatch.setattr(
        "firm_runtime.setup_check.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="limactl version 2.2.0\n", stderr=""
        ),
    )
    assert "2.2.0" in _check_lima()["message"]
    monkeypatch.setattr(
        "firm_runtime.setup_check.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="limactl version 2.1.0\n", stderr=""
        ),
    )
    old = _check_lima()
    assert old["status"] == "failure"
    assert "brew upgrade lima" in old["action"]


def test_resource_check_uses_configured_vm_memory_and_disk(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from firm_runtime.setup_check import _check_vm_resources

    monkeypatch.setattr("firm_runtime.setup_check._host_memory_gib", lambda: 11.5)
    monkeypatch.setattr(
        "firm_runtime.setup_check.shutil.disk_usage",
        lambda path: SimpleNamespace(free=50 * 1024**3),
    )
    checks = {item["check"]: item for item in _check_vm_resources(tmp_path, 8, 60)}
    assert checks["host_memory"]["status"] == "failure"
    assert checks["host_disk"]["status"] == "failure"
    monkeypatch.setattr("firm_runtime.setup_check._host_memory_gib", lambda: 32.0)
    monkeypatch.setattr(
        "firm_runtime.setup_check.shutil.disk_usage",
        lambda path: SimpleNamespace(free=80 * 1024**3),
    )
    checks = {item["check"]: item for item in _check_vm_resources(tmp_path, 8, 60)}
    assert checks["host_memory"]["status"] == "good"
    assert checks["host_disk"]["status"] == "good"


def test_database_initialization_state_distinguishes_safe_partial_and_live_data(tmp_path):
    from firm_runtime.compatibility import initialize_firm
    from firm_runtime.setup_check import database_initialization_state

    initialize_firm(tmp_path)
    database_path = tmp_path / "durable" / "firm.sqlite3"
    assert database_initialization_state(database_path) == "missing"
    Runtime(Store(database_path))
    assert database_initialization_state(database_path) == "partial"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrecognized (value TEXT)")
        connection.execute("INSERT INTO unrecognized VALUES ('keep')")
    assert database_initialization_state(database_path) == "invalid"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE unrecognized")
    runtime = Runtime(Store(database_path))
    runtime.lifecycle.initialize(
        "demo",
        "Build a useful firm.",
        validate_config(
            tomllib.loads(
                'max_active = 1\nstaffing = false\nfounder_profile = "controlled"\n'
                "[profiles.controlled]\n"
            )
        ),
    )
    assert database_initialization_state(database_path) == "initialized"


def _snapshot(root):
    snapshot = {}
    for path in sorted((root, *root.rglob("*"))):
        details = path.lstat()
        snapshot[str(path.relative_to(root))] = (
            stat.S_IFMT(details.st_mode),
            stat.S_IMODE(details.st_mode),
            details.st_mtime_ns,
            details.st_size,
            path.read_bytes() if path.is_file() else None,
        )
    return snapshot


def test_check_before_init_is_read_only_and_controlled_execution_skips_credentials(
    tmp_path, monkeypatch, capsys
):
    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    before = _snapshot(directory)

    report = _invoke(monkeypatch, capsys, directory)

    assert report["ok"] is True
    assert report["initialized"] is False
    assert _checks(report)["database"]["status"] == "skipped"
    assert _checks(report)["credentials"]["status"] == "skipped"
    assert _snapshot(directory) == before
    assert not (directory / "durable").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "writable", "foreign_owned"])
def test_check_rejects_directory_init_would_refuse(tmp_path, monkeypatch, capsys, unsafe):
    target = tmp_path / "demo"
    _write_controlled_files(target)
    directory = target
    if unsafe == "symlink":
        directory = tmp_path / "firm-link"
        directory.symlink_to(target, target_is_directory=True)
    elif unsafe == "writable":
        target.chmod(0o777)
    else:
        owner_uid = target.stat().st_uid
        monkeypatch.setattr("firm_runtime.setup_check.os.getuid", lambda: owner_uid + 1)

    report = _invoke(monkeypatch, capsys, directory, fails=True)

    assert _checks(report)["directory"]["status"] == "failure"
    if unsafe == "symlink":
        assert directory.is_symlink()
    elif unsafe == "writable":
        assert stat.S_IMODE(target.stat().st_mode) == 0o777


@pytest.mark.parametrize(
    ("filename", "check_name"),
    [("mandate.md", "mandate"), ("firm.toml", "firm_config"), ("sandbox.toml", "sandbox_config")],
)
def test_check_rejects_control_file_fifo_without_blocking(tmp_path, filename, check_name):
    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    path = directory / filename
    path.unlink()
    os.mkfifo(path)
    ports = _ports()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "firm_runtime.cli",
            "check",
            str(directory),
            "--mcp-port",
            str(ports[0]),
            "--console-port",
            str(ports[1]),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 2
    assert _checks(report)[check_name]["status"] == "failure"
    assert "regular file" in _checks(report)[check_name]["message"]


@pytest.mark.parametrize("kind", ["directory", "fifo", "broken_symlink"])
def test_check_rejects_nonregular_database_path(tmp_path, monkeypatch, capsys, kind):
    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    durable = directory / "durable"
    durable.mkdir()
    database = durable / "firm.sqlite3"
    if kind == "directory":
        database.mkdir()
    elif kind == "fifo":
        os.mkfifo(database)
    else:
        database.symlink_to(durable / "missing.sqlite3")

    report = _invoke(monkeypatch, capsys, directory, fails=True)

    assert _checks(report)["database"]["status"] == "failure"
    assert report["initialized"] is False


def test_check_initialized_firm_uses_saved_config_without_mutating_database_or_credentials(
    tmp_path, monkeypatch, capsys
):
    directory = tmp_path / "demo"
    directory.mkdir()
    credential = tmp_path / "codex.json"
    credential.write_text('{"tokens":{"access_token":"secret","refresh_token":"renew"}}')
    credential.chmod(0o600)
    config_text = (
        'max_active = 1\nstaffing = false\nfounder_profile = "codex"\n'
        '[profiles.codex]\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
        'effort = "medium"\naccounts = ["personal"]\n'
        '[accounts.personal]\nprovider = "codex"\nmax_active = 1\n'
        f'auth_file = "{credential}"\n'
    )
    mandate = "Build a useful firm.\n"
    (directory / "firm.toml").write_text(config_text)
    (directory / "mandate.md").write_text(mandate)
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    (directory / "durable").mkdir()
    from firm_runtime.compatibility import initialize_firm

    initialize_firm(directory)
    Runtime(Store(directory / "durable" / "firm.sqlite3")).lifecycle.initialize(
        "demo", mandate, validate_config(tomllib.loads(config_text))
    )
    database = directory / "durable" / "firm.sqlite3"
    database_before = database.read_bytes(), database.stat().st_mtime_ns
    directories_before = {
        path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()
    }
    credential_before = credential.read_bytes(), credential.stat().st_mtime_ns

    report = _invoke(monkeypatch, capsys, directory)

    assert report["ok"] is True
    assert report["initialized"] is True
    assert _checks(report)["saved_configuration"]["status"] == "good"
    assert _checks(report)["credential:personal"]["status"] == "good"
    assert (database.read_bytes(), database.stat().st_mtime_ns) == database_before
    directories_after = {
        path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()
    }
    assert directories_after == directories_before
    assert (credential.read_bytes(), credential.stat().st_mtime_ns) == credential_before
    assert not (directory / "locks").exists()
    assert not (directory / "durable" / "console-key").exists()


@pytest.mark.parametrize(
    ("contents", "warned"),
    [("sk-ant-oat01-setup-token\n", True), ('{"claudeAiOauth":{"accessToken":"a"}}', False)],
)
def test_check_and_accounts_warn_about_a_long_lived_claude_setup_token(
    tmp_path, monkeypatch, capsys, contents, warned
):
    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    credential = tmp_path / "claude-auth"
    credential.write_text(contents)
    credential.chmod(0o600)
    (directory / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "claude"\n'
        '[profiles.claude]\nprovider = "claude"\nmodel = "opus"\neffort = "high"\n'
        'accounts = ["personal"]\n'
        f'[accounts.personal]\nprovider = "claude"\nmax_active = 1\nauth_file = "{credential}"\n'
    )
    monkeypatch.setattr("firm_runtime.setup_check.shutil.which", lambda command: "/usr/bin/true")
    _mock_supported_host(monkeypatch)
    ports = _ports()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm",
            "check",
            str(directory),
            "--mcp-port",
            str(ports[0]),
            "--console-port",
            str(ports[1]),
        ],
    )
    main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    check = _checks(report)["credential:personal"]
    assert report["ok"] is True
    assert check["status"] == ("warning" if warned else "good")
    assert ("setup-token" in captured.err) is warned
    assert ("firm save-credentials personal --provider claude" in check.get("action", "")) is warned

    (directory / "durable").mkdir()
    from firm_runtime.compatibility import initialize_firm

    initialize_firm(directory)
    Runtime(Store(directory / "durable" / "firm.sqlite3")).lifecycle.initialize(
        "demo", "Build.", validate_config(tomllib.loads((directory / "firm.toml").read_text()))
    )
    monkeypatch.setattr(sys, "argv", ["firm", "accounts", str(directory)])
    main()
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["warnings"]) == int(warned)
    assert ("setup-token" in captured.err) is warned


def test_check_reads_current_saved_configuration_from_wal(tmp_path, monkeypatch, capsys):
    from firm_runtime.compatibility import initialize_firm

    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    database = directory / "durable" / "firm.sqlite3"
    database.parent.mkdir()
    initialize_firm(directory)
    initial = validate_config(tomllib.loads((directory / "firm.toml").read_text()))
    Runtime(Store(database)).lifecycle.initialize(
        "demo", (directory / "mandate.md").read_text(), initial
    )
    changed_text = (directory / "firm.toml").read_text().replace("max_active = 1", "max_active = 2")
    changed = validate_config(tomllib.loads(changed_text))
    (directory / "firm.toml").write_text(changed_text)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE firm SET config_json=? WHERE singleton=1",
            (json.dumps(changed, sort_keys=True, separators=(",", ":")),),
        )
        writer.commit()
        assert Path(f"{database}-wal").is_file()

        report = _invoke(monkeypatch, capsys, directory)
    finally:
        writer.close()

    assert report["ok"] is True
    assert _checks(report)["firm_config_consistency"]["status"] == "good"


def test_check_reports_unapplied_config_and_malformed_credentials_without_secret_output(
    tmp_path, monkeypatch, capsys
):
    directory = tmp_path / "demo"
    directory.mkdir()
    credential = tmp_path / "codex.json"
    credential.write_text("super-secret credential material")
    credential.chmod(0o600)
    original_text = (
        'max_active = 1\nstaffing = false\nfounder_profile = "codex"\n'
        '[profiles.codex]\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
        'effort = "medium"\naccounts = ["personal"]\n'
        '[accounts.personal]\nprovider = "codex"\nmax_active = 1\n'
        f'auth_file = "{credential}"\n'
    )
    mandate = "Build.\n"
    (directory / "mandate.md").write_text(mandate)
    (directory / "firm.toml").write_text(original_text)
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    (directory / "durable").mkdir()
    Runtime(Store.scratch(directory / "durable" / "firm.sqlite3")).lifecycle.initialize(
        "demo", mandate, validate_config(tomllib.loads(original_text))
    )
    (directory / "firm.toml").write_text(original_text.replace("max_active = 1", "max_active = 2"))

    report = _invoke(monkeypatch, capsys, directory, fails=True)
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert _checks(report)["firm_config_consistency"]["status"] == "failure"
    credential_check = _checks(report)["credential:personal"]
    assert credential_check["status"] == "failure"
    assert credential_check["code"] == "credential_invalid"
    assert "super-secret" not in serialized
    assert str(credential) not in serialized


def test_check_nul_credential_path_reports_safe_failure(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "mandate.md").write_text("Build.\n")
    (directory / "firm.toml").write_text(
        'max_active = 1\nstaffing = false\nfounder_profile = "codex"\n'
        '[profiles.codex]\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
        'effort = "medium"\naccounts = ["personal"]\n'
        '[accounts.personal]\nprovider = "codex"\nmax_active = 1\n'
        'auth_file = "/tmp/\\u0000do-not-print"\n'
    )
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    ports = _ports()
    monkeypatch.setattr("firm_runtime.setup_check.shutil.which", lambda command: "/usr/bin/true")
    _mock_supported_host(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm",
            "check",
            str(directory),
            "--mcp-port",
            str(ports[0]),
            "--console-port",
            str(ports[1]),
        ],
    )

    with pytest.raises(SystemExit) as error:
        main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert error.value.code == 2
    assert _checks(report)["credential:personal"]["code"] == "credential_unreadable"
    assert "do-not-print" not in captured.out
    assert "do-not-print" not in captured.err


def test_check_reports_port_collision_and_duplicate_requested_ports(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "demo"
    _write_controlled_files(directory)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    occupied = listener.getsockname()[1]
    free = _ports()[0]
    try:
        report = _invoke(monkeypatch, capsys, directory, ports=(occupied, free), fails=True)
    finally:
        listener.close()
    assert _checks(report)["mcp_port"]["status"] == "failure"
    assert _checks(report)["console_port"]["status"] == "good"

    duplicate = _ports()[0]
    report = _invoke(monkeypatch, capsys, directory, ports=(duplicate, duplicate), fails=True)
    assert _checks(report)["mcp_port"]["status"] == "good"
    assert _checks(report)["console_port"]["status"] == "failure"
