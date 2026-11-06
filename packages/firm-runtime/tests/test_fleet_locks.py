"""Delete cannot race a starting host: it holds the execution-host and lifecycle locks."""

from types import SimpleNamespace

import pytest

from firm_runtime import fleet
from firm_runtime.errors import FirmError


def test_delete_refuses_while_an_execution_host_owns_the_firm(tmp_path, monkeypatch):
    directory = tmp_path / "firm"
    directory.mkdir()
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    runtime = SimpleNamespace(status=lambda: {"firm_id": "demo", "state": "stopped", "active": []})
    monkeypatch.setattr(fleet, "sandbox_status", lambda path: ("stopped", None))
    with fleet.runtime_owner_lock(directory) as acquired:
        assert acquired
        with pytest.raises(FirmError, match="execution host owns"):
            fleet.delete(directory, "demo", runtime)
    assert directory.exists()
    assert fleet.delete(directory, "demo", runtime)["status"] == "deleted"
    assert not directory.exists()


def test_delete_holds_the_firm_lifecycle_lock_the_sandbox_takes(tmp_path, monkeypatch):
    import fcntl

    directory = tmp_path / "firm"
    directory.mkdir()
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    runtime = SimpleNamespace(status=lambda: {"firm_id": "demo", "state": "stopped", "active": []})
    held = []

    def status(path):
        with (directory / "locks" / "lifecycle").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        return "stopped", None

    monkeypatch.setattr(fleet, "sandbox_status", status)
    assert fleet.delete(directory, "demo", runtime)["status"] == "deleted"
    assert held == [True]
