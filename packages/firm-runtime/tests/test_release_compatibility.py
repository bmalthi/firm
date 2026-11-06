import json
import os
import sqlite3
import tomllib
from pathlib import Path

import pytest

from firm_runtime.compatibility import (
    COMPATIBILITY_GENERATION,
    FIRM_RECORD,
    initialize_firm,
    release_identifier,
    validate_firm,
)
from firm_runtime.store import Store

ROOT = Path(__file__).parents[3]


def test_package_versions_and_runtime_sandbox_pin_are_one_release():
    runtime = tomllib.loads((ROOT / "packages/firm-runtime/pyproject.toml").read_text())
    sandbox = tomllib.loads((ROOT / "packages/firm-sandbox/pyproject.toml").read_text())
    runtime_version = runtime["project"]["version"]
    sandbox_version = sandbox["project"]["version"]
    assert runtime_version == sandbox_version
    assert f"firm-sandbox=={sandbox_version}" in runtime["project"]["dependencies"]
    assert release_identifier() == runtime_version


def test_new_firm_record_is_private_and_validated(tmp_path):
    record = initialize_firm(tmp_path)
    path = tmp_path / FIRM_RECORD
    assert record == json.loads(path.read_text())
    assert record["compatibility_generation"] == COMPATIBILITY_GENERATION
    assert record["created_with_release"] == release_identifier()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert validate_firm(tmp_path) == record
    assert initialize_firm(tmp_path) == record


def test_init_cannot_stamp_an_existing_legacy_database(tmp_path):
    database = tmp_path / "durable" / "firm.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"legacy database")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="does not stamp or migrate"):
        initialize_firm(tmp_path)
    assert database.read_bytes() == before
    assert not (tmp_path / FIRM_RECORD).exists()


def test_missing_marker_refuses_without_creating_anything(tmp_path):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="does not migrate unmarked firms"):
        validate_firm(tmp_path)
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("marker", [None, b"{", b'{"record_version":1}'])
def test_store_refuses_unmarked_or_malformed_firm_before_any_database_write(tmp_path, marker):
    database = tmp_path / "durable" / "firm.sqlite3"
    if marker is not None:
        path = tmp_path / FIRM_RECORD
        path.write_bytes(marker)
        path.chmod(0o600)

    with pytest.raises(ValueError):
        Store(database)

    assert not database.parent.exists()
    assert not database.exists()
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_store_refuses_incompatible_firm_before_any_database_write(tmp_path, monkeypatch):
    from firm_sandbox import compatibility as sandbox_compatibility

    initialize_firm(tmp_path)
    monkeypatch.setattr(
        sandbox_compatibility,
        "COMPATIBILITY_GENERATION",
        COMPATIBILITY_GENERATION + 1,
    )
    database = tmp_path / "durable" / "firm.sqlite3"

    with pytest.raises(ValueError, match="requires generation"):
        Store(database)

    assert not database.parent.exists()
    assert not database.exists()
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_store_preserves_existing_incompatible_database_and_wal(tmp_path):
    initialize_firm(tmp_path)
    database = tmp_path / "durable" / "firm.sqlite3"
    Store(database)
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE runtime_secrets SET value='existing-state' WHERE name='discovery_cursor'"
        )
        paths = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        assert all(path.is_file() for path in paths)
        before = {path: path.read_bytes() for path in paths}

        marker = tmp_path / FIRM_RECORD
        record = json.loads(marker.read_text())
        record["compatibility_generation"] += 1
        marker.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(ValueError, match="requires generation"):
            Store(database)

        assert {path: path.read_bytes() for path in paths} == before
    finally:
        writer.close()


def test_store_refuses_a_database_symlink_without_touching_its_target(tmp_path):
    initialize_firm(tmp_path)
    database = tmp_path / "durable" / "firm.sqlite3"
    database.parent.mkdir()
    target = tmp_path / "external.sqlite3"
    target.write_bytes(b"external sentinel")
    database.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        Store(database)

    assert target.read_bytes() == b"external sentinel"


def test_store_opens_compatible_marked_firm_and_scratch_requires_explicit_api(tmp_path):
    initialize_firm(tmp_path)
    database = tmp_path / "durable" / "firm.sqlite3"

    store = Store(database)
    assert store.path == database.resolve()
    with store.read() as db:
        assert db.execute("SELECT value FROM runtime_secrets").fetchone()[0]
    with pytest.raises(ValueError, match="cannot open a marked firm"):
        Store.scratch(database)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{", "malformed"),
        (b'{"record_version":1,"created_with_release":"0.1.0"}', "malformed"),
        (
            json.dumps(
                {
                    "record_version": 1,
                    "created_with_release": "0.0.9",
                    "compatibility_generation": COMPATIBILITY_GENERATION,
                }
            ).encode(),
            "0.0.9",
        ),
        (
            json.dumps(
                {
                    "record_version": 1,
                    "created_with_release": "0.2.0",
                    "compatibility_generation": COMPATIBILITY_GENERATION + 1,
                }
            ).encode(),
            "0.2.0",
        ),
    ],
)
def test_invalid_or_incompatible_markers_refuse_read_only(tmp_path, monkeypatch, payload, message):
    path = tmp_path / FIRM_RECORD
    path.write_bytes(payload)
    path.chmod(0o600)
    before = path.read_bytes()
    if message == "0.0.9":
        from firm_sandbox import compatibility

        monkeypatch.setattr(compatibility, "COMPATIBILITY_GENERATION", COMPATIBILITY_GENERATION + 1)
    with pytest.raises(ValueError, match=message):
        from firm_runtime import compatibility

        compatibility.validate_firm(tmp_path)
    assert path.read_bytes() == before
