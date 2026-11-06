"""Staffing and fresh roles share explicit routes and account capacity."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from firm_runtime.levels import CLASSES, DEFAULT_MODELS
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    config = {
        "max_active": 4,
        "founder_profile": "codex",
        "accounts": {
            "c1": {"provider": "codex", "auth_file": "/missing/c1", "max_active": 1},
            "c2": {"provider": "codex", "auth_file": "/missing/c2", "max_active": 1},
            "a1": {"provider": "claude", "auth_file": "/missing/a1", "max_active": 1},
            "a2": {"provider": "claude", "auth_file": "/missing/a2", "max_active": 1},
        },
        "profiles": {
            "codex": {
                "provider": "codex",
                "model": "test",
                "effort": "low",
                "accounts": ["c1", "c2"],
                "alternates": ["claude"],
                "context_window": 256000,
            },
            "claude": {
                "provider": "claude",
                "model": "test",
                "effort": "low",
                "accounts": ["a1", "a2"],
                "context_window": 500000,
            },
        },
    }
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build.", config)
    runtime.start()
    return runtime, now


def reserve(runtime):
    return runtime.summary_service._reserve_run(
        0, runtime.accounts.reserve_staffing, "staffing", None
    )


def test_defaults_keep_provider_class_and_context_on_each_route(system):
    runtime, _ = system
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    staffing = config["staffing"]
    for index, level in enumerate((*CLASSES, "chooser")):
        primary = staffing["profile"] if level == "chooser" else staffing["classes"][level]
        first = config["profiles"][primary]
        second = config["profiles"][first["alternates"][0]]
        model_index = 0 if level == "chooser" else index
        assert (first["provider"], first["model"], first["accounts"], first["context_window"]) == (
            "codex",
            DEFAULT_MODELS["codex"][model_index],
            ["c1", "c2"],
            256000,
        )
        assert (
            second["provider"],
            second["model"],
            second["accounts"],
            second["context_window"],
        ) == ("claude", DEFAULT_MODELS["claude"][model_index], ["a1", "a2"], 500000)
        assert first["effort"] == ("medium" if level in {"high", "chooser"} else "high")
        assert second["effort"] == ("medium" if level == "chooser" else "high")


def test_fresh_hires_use_both_providers_up_to_firm_capacity(system):
    runtime, _ = system
    for index in range(4):
        runtime.lifecycle.hire("founder", f"analyst{index}", "Work.", request_id=f"hire{index}")
    runtime.levels.chooser = lambda _: {"model_class": "medium", "effort": "high", "reason": "Work"}
    runtime.levels.tick(background=False)
    with ThreadPoolExecutor(5) as pool:
        admitted = [r for r in pool.map(lambda _: runtime.admit(), range(5)) if r]
    assert len(admitted) == 4
    with runtime.store.read() as db:
        bindings = [runtime.accounts.binding(db, r.session_id) for r in admitted]
    assert {b["account_id"] for b in bindings} == {"c1", "c2", "a1", "a2"}
    assert {b["provider"] for b in bindings} == {"codex", "claude"}
    assert runtime.admit() is None


def test_staffing_reserves_with_every_account_slot_taken(system):
    runtime, _ = system
    for index in range(4):
        runtime.lifecycle.hire("founder", f"analyst{index}", "Work.", request_id=f"hire{index}")
    runtime.levels.chooser = lambda _: {"model_class": "medium", "effort": "high", "reason": "Work"}
    runtime.levels.tick(background=False)
    with ThreadPoolExecutor(5) as pool:
        assert len([r for r in pool.map(lambda _: runtime.admit(), range(5)) if r]) == 4
    assert runtime.admit() is None
    _, reservation = reserve(runtime)  # an assessment takes no slot
    assert reservation["account_id"] in {"c1", "c2", "a1", "a2"}


@pytest.mark.parametrize("block", ["quota", "profile_hold", "account_hold"])
def test_staffing_uses_same_fresh_route_rules_as_roles(system, block):
    runtime, now = system
    with runtime.store.transaction() as db:
        if block == "quota":
            for account in ("c1", "c2"):
                runtime.accounts.observe(
                    OWNER, account, 0.0, observed_at=now[0], valid_for=60, source="test"
                )
        elif block == "profile_hold":
            runtime.accounts.hold(db, "profile", "staffing-chooser", "config")
            runtime.accounts.hold(db, "profile", "codex", "config")
        elif block == "account_hold":
            for account in ("c1", "c2"):
                runtime.accounts.hold(db, "account", account, "quota", now[0] + 60)
    with runtime.store.transaction() as db:
        role = dict(runtime.store.role(db, "founder"))
        role["session_id"] = None
        expected = runtime.accounts.select(db, role)
    run_id, reservation = reserve(runtime)
    assert (reservation["provider"], reservation["account_id"]) == (
        expected["provider"],
        expected["account_id"],
    )
    assert reservation["provider"] == "claude"
    assert reservation["model"] == DEFAULT_MODELS["claude"][0]
    assert reservation["effort"] == "medium"
    with runtime.store.read() as db:
        run = db.execute("SELECT * FROM summary_runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["profile"] == reservation["profile"]
    assert run["account_id"] == reservation["account_id"]


def test_staffing_and_role_reservations_do_not_overbook(system):
    runtime, _ = system
    # Staffing runs count as load, so the second reservation and the role admission
    # each go to the provider with more room.
    with ThreadPoolExecutor(2) as pool:
        reserved = list(pool.map(lambda _: reserve(runtime), range(2)))
    assert {reservation["account_id"] for _, reservation in reserved} == {"c1", "a1"}
    founder = runtime.admit()
    with runtime.store.read() as db:
        assert runtime.accounts.binding(db, founder.session_id)["account_id"] == "c2"
        assert all(runtime.accounts._usage(db, a) <= 1 for a in ("c1", "c2", "a1", "a2"))


def test_staffing_returns_to_primary_after_temporary_quota_block(system):
    runtime, now = system
    for account in ("c1", "c2"):
        runtime.accounts.observe(
            OWNER, account, 0.0, observed_at=now[0], valid_for=10, source="test"
        )
    run_id, first = reserve(runtime)
    assert first["provider"] == "claude"
    runtime.summary_service._finish(run_id, {"status": "completed"}, now[0])
    now[0] += 11
    _, second = reserve(runtime)
    assert second["provider"] == "codex"


def test_staffing_waits_when_all_accounts_are_held_and_after_stop(system):
    runtime, _ = system
    with runtime.store.transaction() as db:
        for account in ("c1", "c2", "a1", "a2"):
            runtime.accounts.hold(db, "account", account, "quota")
    assert reserve(runtime) is None
    runtime.stop()
    assert reserve(runtime) is None


def test_running_staffing_spreads_by_load_and_finished_runs_leave_no_trace(system):
    runtime, now = system
    reserved = [reserve(runtime) for _ in range(4)]
    assert [reservation["account_id"] for _, reservation in reserved] == ["c1", "a1", "c2", "a2"]
    for run_id, _ in reserved:
        runtime.summary_service._finish(run_id, {"status": "completed"}, now[0])
    runtime = Runtime(Store.scratch(runtime.store.path, clock=lambda: now[0]))
    assert reserve(runtime)[1]["account_id"] == "c1"


def test_failed_reservation_does_not_count_as_load(system):
    runtime, _ = system
    with pytest.raises(RuntimeError):
        with runtime.store.transaction() as db:
            assert runtime.accounts.reserve_staffing(db, "not-committed")["account_id"] == "c1"
            raise RuntimeError("reservation failed")
    assert reserve(runtime)[1]["account_id"] == "c1"
