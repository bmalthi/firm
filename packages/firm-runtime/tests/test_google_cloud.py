"""The owner's generative image key: validated once, staged per run, stored nowhere."""

from __future__ import annotations

import json
import os
import pwd

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.google_cloud import (
    key_path,
    launch_settings,
    read_key,
    validate_google_cloud,
)
from firm_runtime.lifecycle import Lifecycle
from firm_runtime.store import Store

KEY = "AIzaSyExampleKeyForTestsOnly0123456789"

CONFIG = {
    "max_active": 2,
    "staffing": False,
    "founder_profile": "heavy",
    "profiles": {"heavy": {"model": "test"}},
}


class SilentMessaging:
    def notice(self, db, recipients, body, *, subject, priority=0, wake=True):
        return {"status": "sent", "message_id": "m1"}

    def retire(self, db, employee_id):
        return {"status": "retired"}


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "demo-game.key"
    path.write_text(KEY + "\n")
    path.chmod(0o600)
    return path


def firm(tmp_path, settings):
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: 100.0)
    config = dict(CONFIG)
    if settings is not None:
        config["google_cloud"] = settings
    ids = iter(["employee-a", "employee-b"])
    Lifecycle(store, SilentMessaging(), id_factory=lambda: next(ids)).initialize(
        "test-firm", "Build useful things.", config
    )
    return store


def test_no_table_configures_nothing(tmp_path):
    config = dict(CONFIG)
    validate_google_cloud(config)
    assert "google_cloud" not in config
    assert launch_settings(firm(tmp_path, None)) is None


def test_the_key_file_defaults_to_the_owner_private_directory():
    config = {"google_cloud": {"project": "demo-game"}}
    validate_google_cloud(config)
    assert config["google_cloud"]["api_key_file"] == str(key_path("demo-game"))


@pytest.mark.parametrize(
    "settings",
    [
        {"project": "demo-game", "spend_cap": 100},
        {"project": "Demo-Game"},
        {"project": "no"},
        {},
        {"project": "demo-game", "api_key_file": "keys/demo-game.key"},
        "demo-game",
    ],
)
def test_unusable_tables_are_refused(settings):
    with pytest.raises(FirmError):
        validate_google_cloud({"google_cloud": settings})


def test_the_key_is_read_without_its_surrounding_whitespace(key_file):
    assert read_key(key_file) == KEY


@pytest.mark.parametrize(
    "content",
    [b"", b"   \n", b"AIza one two\n", b"AIza\nAIzb\n", b"\xff\xfe", b"A" * 2048],
)
def test_a_file_that_is_not_one_key_is_refused(tmp_path, content):
    path = tmp_path / "bad.key"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(FirmError) as refusal:
        read_key(path)
    assert KEY not in str(refusal.value)


def test_a_missing_key_file_names_the_file_and_not_the_key(tmp_path):
    with pytest.raises(FirmError, match="cannot be read"):
        read_key(tmp_path / "absent.key")


def test_unknown_home_account_has_an_actionable_credential_error(monkeypatch):
    def unknown_account(name):
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", unknown_account)
    path = "~missing-firm-user/key"
    with pytest.raises(FirmError, match="absolute api_key_file"):
        validate_google_cloud({"google_cloud": {"project": "demo-game", "api_key_file": path}})
    with pytest.raises(FirmError, match="cannot be read"):
        read_key(path)


@pytest.mark.parametrize("kind", ["public", "symlink", "fifo", "directory"])
def test_key_source_must_be_private_regular_and_not_a_link(tmp_path, key_file, kind):
    if kind == "public":
        key_file.chmod(0o644)
        path = key_file
    else:
        path = tmp_path / "invalid.key"
        if kind == "symlink":
            path.symlink_to(key_file)
        elif kind == "fifo":
            os.mkfifo(path, mode=0o600)
        else:
            path.mkdir()
    with pytest.raises(FirmError, match="owner-private regular file") as error:
        read_key(path)
    assert KEY not in str(error.value)


def test_a_run_receives_the_key_as_a_secret_variable(tmp_path, key_file):
    store = firm(tmp_path, {"project": "demo-game", "api_key_file": str(key_file)})
    env, secrets, secret_env = launch_settings(store)
    assert env == {"GOOGLE_CLOUD_PROJECT": "demo-game"}
    assert secrets == {"google-cloud-key": KEY.encode()}
    assert secret_env == {"GEMINI_API_KEY": "google-cloud-key"}


def test_the_key_never_enters_the_firm_configuration(tmp_path, key_file):
    store = firm(tmp_path, {"project": "demo-game", "api_key_file": str(key_file)})
    with store.read() as db:
        config = store.firm(db)["config_json"]
    assert KEY not in config
    assert json.loads(config)["google_cloud"]["api_key_file"] == str(key_file)


def test_each_run_reads_the_configuration_again_so_configure_reaches_a_running_firm(
    tmp_path, key_file
):
    store = firm(tmp_path, None)
    assert launch_settings(store) is None
    config = dict(CONFIG)
    config["google_cloud"] = {"project": "demo-game", "api_key_file": str(key_file)}
    with store.transaction() as db:
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    assert launch_settings(store)[2] == {"GEMINI_API_KEY": "google-cloud-key"}


def test_an_unreadable_key_stops_the_launch_rather_than_running_without_it(tmp_path, key_file):
    store = firm(tmp_path, {"project": "demo-game", "api_key_file": str(key_file)})
    key_file.unlink()
    with pytest.raises(FirmError, match="cannot be read"):
        launch_settings(store)
