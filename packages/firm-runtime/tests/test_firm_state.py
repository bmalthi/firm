"""Development state has no release gates; filesystem protections still apply."""
import tomllib
from pathlib import Path

import pytest
from firm_sandbox.release import release_identifier

from firm_runtime.store import Store

ROOT = Path(__file__).parents[3]


def test_package_versions_and_runtime_sandbox_pin_are_one_release():
    runtime = tomllib.loads((ROOT / "packages/firm-runtime/pyproject.toml").read_text())
    sandbox = tomllib.loads((ROOT / "packages/firm-sandbox/pyproject.toml").read_text())
    version = sandbox["project"]["version"]
    assert runtime["project"]["version"] == version
    assert f"firm-sandbox=={version}" in runtime["project"]["dependencies"]
    assert release_identifier() == version


@pytest.mark.parametrize("marker", [None, "malformed", '{"compatibility_generation": -1}'])
def test_store_does_not_require_or_interpret_release_markers(tmp_path, marker):
    path = tmp_path / "firm-release.json"
    if marker is not None:
        path.write_text(marker)
    database = tmp_path / "durable" / "firm.sqlite3"
    Store(database)
    Store(database)
    if marker is None:
        assert not path.exists()
    else:
        assert path.read_text() == marker


def test_store_refuses_database_symlink(tmp_path):
    database = tmp_path / "durable" / "firm.sqlite3"
    database.parent.mkdir()
    target = tmp_path / "external.sqlite3"
    target.write_bytes(b"external sentinel")
    database.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        Store(database)
    assert target.read_bytes() == b"external sentinel"


def test_store_refuses_durable_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "durable").symlink_to(external)
    with pytest.raises(ValueError, match="owner-controlled"):
        Store(tmp_path / "durable" / "firm.sqlite3")
    assert not list(external.iterdir())
