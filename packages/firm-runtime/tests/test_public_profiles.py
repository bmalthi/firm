import hashlib
from types import SimpleNamespace

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import FOUNDER_ID
from firm_runtime.public_profiles import ingest_public_profiles
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id


@pytest.fixture
def system(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build", {
        "max_active": 2, "staffing": False, "founder_profile": "default",
        "profiles": {"default": {}},
    })
    records = {}
    files = SimpleNamespace(profiles=lambda: dict(records))
    return runtime, records, files, files, tmp_path / "unmounted-guest"


def available(text):
    return {"status": "available", "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


def test_guest_profiles_update_identity_without_waking_roles(system):
    runtime, records, _, storage, root = system
    analyst = runtime.lifecycle.hire(FOUNDER_ID, "analyst", "Work", "default", request_id="hire")
    records[sandbox_role_id(FOUNDER_ID)] = available("# Founder — Builder\nOwn the firm.\n")
    with runtime.store.read() as db:
        before = db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0]
    result = ingest_public_profiles(runtime.store, files=storage)
    assert result == {"scanned": 2, "changed": 1,
                      "statuses": {"available": 1, "missing": 1, "unreadable": 0}}
    with runtime.store.read() as db:
        founder = runtime.store.role(db, FOUNDER_ID)
        assert founder["public_profile"] == records[sandbox_role_id(FOUNDER_ID)]["text"]
        assert founder["identity"] == "Founder — Builder"
        assert founder["public_profile_digest"] == records[sandbox_role_id(FOUNDER_ID)]["sha256"]
        analyst_role = runtime.store.role(db, analyst["employee_id"])
        assert analyst_role["public_profile_status"] == "missing"
        assert db.execute("SELECT COUNT(*) FROM scheduling_opportunities").fetchone()[0] == before
    assert ingest_public_profiles(runtime.store, files=storage)["changed"] == 0
    assert not root.exists(), "Profile ingestion must not materialize guest paths on the host"


@pytest.mark.parametrize("status", ["missing", "unreadable"])
def test_guest_profile_status_clears_previous_public_text(system, status):
    runtime, records, _, storage, root = system
    role = sandbox_role_id(FOUNDER_ID)
    records[role] = available("# Public identity")
    ingest_public_profiles(runtime.store, files=storage)
    records[role] = {"status": status}
    assert ingest_public_profiles(runtime.store, files=storage)["statuses"][status] == 1
    with runtime.store.read() as db:
        founder = runtime.store.role(db, FOUNDER_ID)
        assert founder["public_profile_status"] == status
        assert founder["public_profile"] == ""
        assert founder["public_profile_digest"] is None


def test_profile_scan_failure_commits_nothing(system):
    runtime, records, files, storage, root = system
    runtime.lifecycle.hire(FOUNDER_ID, "analyst", "Work", "default", request_id="hire")
    records[sandbox_role_id(FOUNDER_ID)] = available("# Original founder")
    ingest_public_profiles(runtime.store, files=storage)

    def disconnected():
        raise RuntimeError("Guest controller disconnected")

    files.profiles = disconnected
    with pytest.raises(RuntimeError, match="disconnected"):
        ingest_public_profiles(runtime.store, files=storage)
    with runtime.store.read() as db:
        assert runtime.store.role(db, FOUNDER_ID)["public_profile"] == "# Original founder"


@pytest.mark.parametrize(
    "malformed",
    [
        {"status": "corrupt", "text": "# Replacement"},
        {"status": ["available"], "text": "# Replacement", "sha256": "digest"},
        {"status": "available", "text": ["# Replacement"], "sha256": "digest"},
        {"status": "available", "text": "# Replacement"},
    ],
)
def test_malformed_profile_scan_commits_nothing(system, malformed):
    runtime, records, _, storage, _ = system
    role_id = sandbox_role_id(FOUNDER_ID)
    records[role_id] = available("# Original founder")
    ingest_public_profiles(runtime.store, files=storage)
    records[role_id] = malformed

    with pytest.raises(FirmError, match="profile scan"):
        ingest_public_profiles(runtime.store, files=storage)

    with runtime.store.read() as db:
        founder = runtime.store.role(db, FOUNDER_ID)
        assert founder["public_profile_status"] == "available"
        assert founder["public_profile"] == "# Original founder"


def test_retirement_during_guest_scan_preserves_last_profile(system):
    runtime, records, files, storage, root = system
    analyst = runtime.lifecycle.hire(
        FOUNDER_ID, "analyst", "Work", "default", request_id="hire"
    )["employee_id"]
    role_id = sandbox_role_id(analyst)
    records[role_id] = available("# Original analyst")
    ingest_public_profiles(runtime.store, files=storage)

    def scan():
        runtime.lifecycle.fire(FOUNDER_ID, analyst, reason="Done", request_id="retire")
        return {**records, role_id: available("# Too late")}

    files.profiles = scan
    ingest_public_profiles(runtime.store, files=storage)
    with runtime.store.read() as db:
        assert runtime.store.role(db, analyst)["public_profile"] == "# Original analyst"


def test_profiles_require_explicit_guest_storage(system):
    runtime, records, organization, storage, _ = system
    records[sandbox_role_id(FOUNDER_ID)] = available("# Guest founder")
    assert ingest_public_profiles(runtime.store, files=storage)["statuses"]["available"] == 1
