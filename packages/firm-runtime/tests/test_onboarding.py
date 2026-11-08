import io
import json
import os
import signal
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_runtime import accounts, onboarding
from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import validate_config


def test_data_directory_and_derived_firm_names_are_stable_and_unique(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert onboarding.default_data_directory() == tmp_path / "firms"
    root = tmp_path / "firms"
    first = onboarding.choose_directory(root, "Make a weekly planner", today=date(2026, 10, 28))
    assert first.name == "2026-10-28_make-a-weekly-planner"
    first.mkdir(parents=True)
    second = onboarding.choose_directory(root, "Make a weekly planner", today=date(2026, 10, 28))
    assert second.name == "2026-10-28_make-a-weekly-planner_2"


@pytest.mark.parametrize("provider", ["codex", "claude", "agy"])
def test_generated_files_validate_and_are_owner_only_without_overwrite(tmp_path, provider):
    firm = tmp_path / "2026-10-28_my-task"
    onboarding.create_firm_files(firm, provider, "Do the user's own task.\n")
    assert {path.name for path in firm.iterdir()} == {"mandate.md", "firm.toml", "sandbox.toml"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in firm.iterdir())
    assert (firm / "mandate.md").read_text() == "Do the user's own task.\n"
    config = validate_config(__import__("tomllib").loads((firm / "firm.toml").read_text()))
    assert config["founder_profile"] == provider
    assert config["profiles"][provider]["model"]
    before = (firm / "mandate.md").read_bytes()
    with pytest.raises(FirmError, match="refusing to replace"):
        onboarding.create_firm_files(firm, provider, "Replace it")
    assert (firm / "mandate.md").read_bytes() == before


def test_create_firm_files_refuses_symlink_directory(tmp_path):
    target = tmp_path / "owner-data"
    target.mkdir()
    link = tmp_path / "firm-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FirmError, match="owner-controlled"):
        onboarding.create_firm_files(link, "codex", "Do work")
    assert not list(target.iterdir())


def test_resume_fills_only_missing_files_and_lists_initialized_or_incomplete(tmp_path):
    root = tmp_path / "firms"
    incomplete = root / "2026-10-28_resume"
    incomplete.mkdir(parents=True)
    (incomplete / "firm.toml").write_text("owner edit\n")
    empty = root / "2026-10-28_interrupted"
    empty.mkdir()
    database = root / "2026-10-28_ready"
    onboarding.create_firm_files(database, "codex", "Build a firm.")
    from firm_runtime.compatibility import initialize_firm
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store

    initialize_firm(database)
    Runtime(Store(database / "durable" / "firm.sqlite3")).lifecycle.initialize(
        database.name,
        "Build a firm.\n",
        validate_config(__import__("tomllib").loads((database / "firm.toml").read_text())),
    )

    listing = onboarding.list_firms(root)
    assert [(item["name"], item["state"]) for item in listing] == [
        ("2026-10-28_interrupted", "incomplete"),
        ("2026-10-28_ready", "initialized"),
        ("2026-10-28_resume", "incomplete"),
    ]
    onboarding.create_missing_files(incomplete, "codex", "Save me")
    assert (incomplete / "firm.toml").read_text() == "owner edit\n"
    assert (incomplete / "mandate.md").read_text() == "Save me\n"


def test_unmarked_database_is_listed_for_attention_not_as_a_resumable_firm(tmp_path):
    root = tmp_path / "firms"
    old = root / "legacy"
    old.mkdir(parents=True)
    onboarding.create_firm_files(old, "codex", "Legacy firm")
    (old / "durable").mkdir()
    (old / "durable" / "firm.sqlite3").touch()
    listing = onboarding.list_firms(root)
    assert listing[0]["state"] == "needs attention"
    assert "no release compatibility record" in listing[0]["reason"].lower()


def test_provider_login_saves_valid_credential_and_removes_temp_state(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["codex"], "codex"))
    monkeypatch.setenv("CODEX_API_KEY", "ambient-executor-key-must-not-be-used")
    state = tmp_path / "firm" / ".setup-login" / "codex"

    def provider_login(command, *, env, cwd, check):
        assert command == ["codex", "login"]
        assert "OPENAI_API_KEY" not in env
        assert "CODEX_API_KEY" not in env
        config = Path(env["CODEX_HOME"]) / "config.toml"
        assert config.read_text() == 'cli_auth_credentials_store = "file"\n'
        assert config.stat().st_mode & 0o777 == 0o600
        credential = Path(env["CODEX_HOME"]) / "auth.json"
        credential.write_text(
            json.dumps({"tokens": {"access_token": "test", "refresh_token": "r"}})
        )
        credential.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(onboarding.subprocess, "run", provider_login)
    saved = onboarding.login_provider("codex", "codex-personal", state_directory=state)
    assert saved.is_file()
    assert json.loads(saved.read_text())["tokens"]["access_token"] == "test"
    assert not state.exists()
    assert not state.parent.exists()
    assert "test" not in capsys.readouterr().err


def test_claude_setup_token_is_captured_and_never_reads_global_keychain(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["claude"], "claude"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-api-key-must-not-be-used")
    state = tmp_path / "firm" / ".setup-login" / "claude"
    token = "sk-ant-oat01-test-only-token"
    invocations = []

    def provider_login(command, **kwargs):
        invocations.append((command, kwargs))
        return SimpleNamespace(stdout=io.BytesIO((token + "\n").encode()), wait=lambda: 0)

    monkeypatch.setattr(onboarding.subprocess, "Popen", provider_login)
    saved = onboarding.login_provider("claude", "claude-personal", state_directory=state)

    assert saved.read_bytes() == token.encode()
    assert invocations[0][0] == ["claude", "setup-token"]
    assert invocations[0][1]["stdout"] == onboarding.subprocess.PIPE
    assert invocations[0][1]["stderr"] == onboarding.subprocess.STDOUT
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in invocations[0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in invocations[0][1]["env"]
    assert invocations[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(state / "provider-home")
    assert not state.exists()
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_interrupted_claude_setup_stops_and_reaps_child_preserving_setup_state(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["claude"], "claude"))
    state = tmp_path / "firm" / ".setup-login" / "claude"
    stopped = []
    interrupted = KeyboardInterrupt("cancel setup")

    class InterruptedOutput:
        def readline(self):
            raise interrupted

        def close(self):
            stopped.append("stdout closed")

    class Child:
        stdout = InterruptedOutput()

        def poll(self):
            return None

        def terminate(self):
            stopped.append("terminated")

        def kill(self):
            stopped.append("killed")

        def wait(self, *, timeout=None):
            stopped.append(("wait", timeout))
            if timeout is not None and stopped.count(("wait", timeout)) == 1:
                raise onboarding.subprocess.TimeoutExpired(["claude"], timeout)
            return -9

    child = Child()
    monkeypatch.setattr(onboarding.subprocess, "Popen", lambda *args, **kwargs: child)

    with pytest.raises(KeyboardInterrupt) as error:
        onboarding.login_provider("claude", "claude-personal", state_directory=state)

    assert error.value is interrupted
    assert stopped == [
        "terminated",
        ("wait", 1),
        "killed",
        ("wait", 1),
        "stdout closed",
    ]
    assert state.is_dir()
    assert not (accounts.CREDENTIALS_DIR / "claude-personal.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="bounded signal escalation uses POSIX child signals")
def test_stop_child_kills_and_reaps_a_real_child_ignoring_termination():
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(30)"
    )
    process = onboarding.subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=onboarding.subprocess.PIPE,
        stderr=onboarding.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        onboarding._stop_child(process)
        assert process.wait(timeout=0.1) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_failed_claude_setup_does_not_persist_or_print_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["claude"], "claude"))
    state = tmp_path / "firm" / ".setup-login" / "claude"
    token = "sk-ant-oat01-untrusted-failed-output"

    def failed_login(command, **kwargs):
        assert command == ["claude", "setup-token"]
        return SimpleNamespace(
            stdout=io.BytesIO((token + "\n").encode() + b"failed\n"), wait=lambda: 1
        )

    monkeypatch.setattr(onboarding.subprocess, "Popen", failed_login)
    with pytest.raises(FirmError, match="exited with status 1") as error:
        onboarding.login_provider("claude", "claude-personal", state_directory=state)
    output = capsys.readouterr()
    assert token not in str(error.value) + output.out + output.err
    assert not (accounts.CREDENTIALS_DIR / "claude-personal.json").exists()
    assert not (state / "claude-setup-token").exists()
    assert state.is_dir()


def test_claude_setup_token_is_saved_private_and_resumes_after_owner_save_failure(
    tmp_path, monkeypatch
):
    from firm_runtime import credential_store
    from firm_runtime.provider_contract import ProviderSetupError

    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["claude"], "claude"))
    state = tmp_path / "firm" / ".setup-login" / "claude"
    token = "sk-ant-oat01-resumable-token"
    runs = []

    def provider_login(command, **kwargs):
        runs.append(command)
        return SimpleNamespace(stdout=io.BytesIO(token.encode()), wait=lambda: 0)

    original_save = credential_store.CredentialStore.save
    saves = 0

    def failing_owner_save(store, raw):
        nonlocal saves
        saves += 1
        if saves == 1:
            raise ProviderSetupError(category="transient", code="credential_io", message="safe")
        return original_save(store, raw)

    monkeypatch.setattr(onboarding.subprocess, "Popen", provider_login)
    monkeypatch.setattr(credential_store.CredentialStore, "save", failing_owner_save)
    with pytest.raises(FirmError, match="temporary login state is kept") as error:
        onboarding.login_provider("claude", "claude-personal", state_directory=state)
    assert token not in str(error.value)
    staging = state / "claude-setup-token"
    assert staging.read_bytes() == token.encode()
    assert staging.stat().st_mode & 0o777 == 0o600

    saved = onboarding.login_provider("claude", "claude-personal", state_directory=state)
    assert saved.read_bytes() == token.encode()
    assert runs == [["claude", "setup-token"]]
    assert not state.exists()


def test_provider_login_failure_retains_isolated_state_and_resume_saves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "owner-credentials")
    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["codex"], "codex"))
    state = tmp_path / "firm" / ".setup-login" / "codex"

    def failed_login(command, *, env, cwd, check):
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "test", "refresh_token": "r"}}))
        auth.chmod(0o600)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        failed_login,
    )
    with pytest.raises(FirmError, match="temporary state is kept"):
        onboarding.login_provider("codex", "codex-personal", state_directory=state)
    assert state.is_dir()
    saved = onboarding.login_provider("codex", "codex-personal", state_directory=state)
    assert saved.is_file()
    assert not state.exists()


def test_forced_provider_login_uses_configured_destination_and_ignores_cached_login(
    tmp_path, monkeypatch
):
    from firm_runtime.credential_store import CredentialStore

    monkeypatch.setattr(onboarding, "_provider_command", lambda provider: (["codex"], "codex"))
    target = tmp_path / "firm-auth" / "configured.json"
    target.parent.mkdir()
    CredentialStore("codex", target).save(
        json.dumps({"tokens": {"access_token": "old", "refresh_token": "old-refresh"}}).encode()
    )
    state = tmp_path / "firm" / ".setup-login" / "codex"

    def provider_login(command, *, env, cwd, check):
        credential = Path(env["CODEX_HOME"]) / "auth.json"
        assert command == ["codex", "login"]
        assert not credential.exists()
        credential.write_text(
            json.dumps({"tokens": {"access_token": "new", "refresh_token": "new-refresh"}})
        )
        credential.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(onboarding.subprocess, "run", provider_login)
    saved = onboarding.login_provider(
        "codex",
        "codex-personal",
        state_directory=state,
        target_credentials_path=target,
        force_login=True,
    )

    assert saved == target
    assert json.loads(target.read_text())["tokens"]["access_token"] == "new"
    assert not state.exists()


def test_agy_selection_discloses_experimental_subscription_limits(tmp_path, capsys):
    from firm_runtime.cli import _announce_first_run, _choose_provider

    assert _choose_provider(input_fn=lambda _: "3") == "agy"
    _announce_first_run("agy", directory=tmp_path)
    text = capsys.readouterr().err
    assert "experimental" in text.lower()
    assert "subscription" in text and "overages are disabled" in text
    assert "Quota and context are unavailable" in text
    assert "implicit native tools are not fully isolated" in text
