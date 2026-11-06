import json
import os

import pytest

from firm_sandbox import Config
from firm_sandbox.identity import ensure_runtime_id, load_runtime_id


def test_ensure_runtime_id_writes_the_identity_file(tmp_path):
    config = Config(tmp_path)
    runtime_id = ensure_runtime_id(config)
    assert len(runtime_id) == 16 and int(runtime_id, 16) >= 0
    assert json.loads(config.identity_file.read_text()) == {"runtime_id": runtime_id}
    assert config.identity_file.stat().st_mode & 0o777 == 0o600
    assert load_runtime_id(config) == runtime_id


def test_ensure_runtime_id_is_idempotent(tmp_path):
    config = Config(tmp_path)
    first = ensure_runtime_id(config)
    second = ensure_runtime_id(config)
    assert first == second
    assert load_runtime_id(config) == first


def test_load_raises_when_the_identity_file_is_absent(tmp_path):
    config = Config(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_runtime_id(config)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p.write_text("{}"),
        lambda p: p.write_text(json.dumps({"runtime_id": "not-hex!!!!!!!!!"})),
        lambda p: p.write_text(json.dumps({"runtime_id": "a" * 16, "unknown": True})),
    ],
)
def test_load_rejects_invalid_identity_content(tmp_path, corrupt):
    config = Config(tmp_path)
    config.durable.mkdir(mode=0o700)
    corrupt(config.identity_file)
    config.identity_file.chmod(0o600)
    with pytest.raises(RuntimeError, match="Invalid firm identity file"):
        load_runtime_id(config)


def test_load_rejects_a_group_or_world_writable_identity_file(tmp_path):
    config = Config(tmp_path)
    ensure_runtime_id(config)
    config.identity_file.chmod(0o622)
    with pytest.raises(RuntimeError, match="Unsafe firm identity file"):
        load_runtime_id(config)


def test_load_rejects_an_identity_file_owned_by_another_user(tmp_path, monkeypatch):
    config = Config(tmp_path)
    ensure_runtime_id(config)
    monkeypatch.setattr(os, "getuid", lambda: -1)
    with pytest.raises(RuntimeError, match="Unsafe firm identity file"):
        load_runtime_id(config)


def test_load_rejects_an_oversized_identity_file(tmp_path):
    config = Config(tmp_path)
    ensure_runtime_id(config)
    padded = config.identity_file.read_text()[:-1] + ', "pad": "' + "x" * (256 * 1024) + '"}'
    config.identity_file.write_text(padded)
    with pytest.raises(RuntimeError, match="Unsafe firm identity file"):
        load_runtime_id(config)
