import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from firm_runtime import accounts, owner
from firm_runtime.errors import FirmError
from firm_runtime.prompt_resources import render
from firm_runtime.runtime import Runtime
from firm_runtime.store import ORDINARY_PRIORITY, OWNER, Store


def configuration():
    return {
        "max_active": 4,
        "staffing": False,
        "founder_profile": "primary",
        "accounts": {
            "a": {"provider": "codex", "auth_file": "/missing/a", "max_active": 1},
            "b": {"provider": "codex", "auth_file": "/missing/b", "max_active": 1},
            "c": {"provider": "claude", "auth_file": "/missing/c", "max_active": 1},
        },
        "profiles": {
            "primary": {
                "provider": "codex",
                "model": "test",
                "effort": "low",
                "accounts": ["a", "b"],
                "alternates": ["alternate"],
            },
            "alternate": {
                "provider": "claude",
                "model": "test",
                "effort": "low",
                "accounts": ["c"],
            },
        },
    }


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build useful things.", configuration())
    runtime.start()
    return runtime, now


def binding(runtime, invocation):
    with runtime.store.read() as db:
        return runtime.accounts.binding(db, invocation.session_id)


@pytest.mark.parametrize("materialize", [False, True])
def test_incident_expiry_keeps_its_deadline_and_first_resolver(system, materialize):
    runtime, now = system
    incident = runtime.accounts.pause(OWNER, "account", "a", "quota", 1010)["incident_id"]
    now[0] = 1020
    with runtime.store.read() as db:
        row = db.execute(
            "SELECT * FROM execution_incidents WHERE incident_id=?", (incident,)
        ).fetchone()
        assert row["resolved_at"] is None
        assert accounts.incident_resolution(row, now[0]) == {
            "resolved_at": 1010,
            "resolved_by": "expired",
        }
    assert runtime.accounts.status()["incidents"] == []
    if materialize:
        with runtime.store.transaction() as db:
            assert runtime.accounts._incident(db, "account", "a") is None
    runtime.accounts.clear(OWNER, incident)
    now[0] += 10
    runtime.accounts.clear(OWNER, incident)
    with runtime.store.read() as db:
        row = db.execute(
            "SELECT * FROM execution_incidents WHERE incident_id=?", (incident,)
        ).fetchone()
        assert (row["resolved_at"], row["resolved_by"]) == (1010, "expired")


def test_superseded_and_owner_resolutions_survive_repeated_clear(system):
    runtime, now = system
    first = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    now[0] += 10
    second = runtime.accounts.pause(OWNER, "account", "a", "outage")["incident_id"]
    now[0] += 10
    runtime.accounts.clear(OWNER, first)
    runtime.accounts.clear(OWNER, second)
    now[0] += 10
    runtime.accounts.clear(OWNER, second)
    with runtime.store.read() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT resolved_at,resolved_by FROM execution_incidents ORDER BY created_at"
            )
        ] == [(1010, "superseded"), (1020, "owner")]


def test_hold_and_manager_notices_reference_the_same_incident(system):
    runtime, _ = system
    invocation = start(runtime)
    with runtime.store.transaction() as db:
        incident = runtime.accounts.record_failure(db, invocation.session_id, "auth")
        notices = list(
            db.execute(
                "SELECT regarding_kind,regarding_id FROM messages WHERE regarding_id=?", (incident,)
            )
        )
        assert [tuple(row) for row in notices] == [("incident", incident)] * 2


def open_incidents(runtime):
    return {item["incident_id"]: item for item in runtime.accounts.status()["incidents"]}


@pytest.mark.parametrize("code,repair", [
    ("oauth_org_not_allowed", "organization"),
    ("account_on_hold", "account"),
    ("credential_refresh_rejected", "sign_in"),
    (None, "sign_in"),
])
def test_hold_stores_code_and_status_carries_recovery(system, code, repair):
    runtime, _ = system
    incident = runtime.accounts.pause(OWNER, "account", "a", "auth", code=code)["incident_id"]
    row = open_incidents(runtime)[incident]
    assert row["code"] == code and row["repair"] == repair
    assert row["guidance"] == render("notices", f"auth_repair_{repair}")
    assert row["login_command"] == "firm login a --provider codex --auth-file /missing/a"


def test_login_command_omits_auth_file_for_the_default_path(system, monkeypatch):
    runtime, _ = system
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", Path("~/creds"))
    config = configuration()
    del config["accounts"]["c"]["auth_file"]
    runtime.owner.configure(OWNER, config)
    default = runtime.accounts.pause(OWNER, "account", "c", "auth")["incident_id"]
    assert open_incidents(runtime)[default]["login_command"] == "firm login c --provider claude"
    config["accounts"]["a"]["auth_file"] = "/tmp/my creds/a.json"
    runtime.owner.configure(OWNER, config)
    custom = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    assert open_incidents(runtime)[custom]["login_command"] == (
        "firm login a --provider codex --auth-file '/tmp/my creds/a.json'"
    )


def test_non_auth_incidents_carry_no_sign_in_advice(system):
    runtime, _ = system
    incident = runtime.accounts.pause(OWNER, "profile", "primary", "config")["incident_id"]
    row = open_incidents(runtime)[incident]
    assert row["repair"] is None and "guidance" not in row and "login_command" not in row


def test_same_kind_repeat_keeps_first_code_and_fills_a_missing_one(system):
    runtime, _ = system
    first = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    assert open_incidents(runtime)[first]["code"] is None
    again = runtime.accounts.pause(OWNER, "account", "a", "auth", code="account_on_hold")
    assert again["incident_id"] == first
    assert open_incidents(runtime)[first]["code"] == "account_on_hold"
    runtime.accounts.pause(OWNER, "account", "a", "auth", code="oauth_org_not_allowed")
    assert open_incidents(runtime)[first]["code"] == "account_on_hold"


def test_timed_hold_does_not_replace_an_open_owner_hold_of_another_kind(system):
    runtime, now = system
    auth = runtime.accounts.pause(OWNER, "account", "a", "auth", code="invalid_api_key")
    quota = runtime.accounts.pause(OWNER, "account", "a", "quota", now[0] + 100)
    assert quota["incident_id"] == auth["incident_id"]
    assert [i["kind"] for i in runtime.accounts.status()["incidents"]] == ["auth"]


def test_untimed_hold_still_supersedes_a_timed_one(system):
    runtime, now = system
    quota = runtime.accounts.pause(OWNER, "account", "a", "quota", now[0] + 100)["incident_id"]
    auth = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    assert auth != quota and list(open_incidents(runtime)) == [auth]


@pytest.mark.parametrize("first,second,kept", [(4000, 100, 4000), (100, 4000, 4000)])
def test_of_two_timed_holds_of_different_kinds_the_later_end_stands(system, first, second, kept):
    runtime, now = system
    outage = runtime.accounts.pause(OWNER, "profile", "primary", "outage", now[0] + first)
    quota = runtime.accounts.pause(OWNER, "profile", "primary", "quota", now[0] + second)
    (incident,) = runtime.accounts.status()["incidents"]
    # The newer cause is the one shown, but a short pause cannot cut a long one.
    assert quota["incident_id"] != outage["incident_id"]
    assert (incident["incident_id"], incident["kind"]) == (quota["incident_id"], "quota")
    assert incident["until_at"] == now[0] + kept


def role(runtime, employee="founder"):
    with runtime.store.read() as db:
        return runtime.store.role(db, employee)


def wake(runtime, employee="founder", request="wake"):
    runtime.owner.send_mail(OWNER, [employee], "Continue work", request_id=request)


def start(runtime):
    invocation = runtime.admit()
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    return invocation


def observe(runtime, now, account, fraction, valid_for=100):
    return runtime.accounts.observe(
        OWNER, account, fraction, observed_at=now[0], valid_for=valid_for, source="test adapter"
    )


def test_account_reservations_are_atomic_and_release_after_exit(system):
    runtime, _ = system
    founder = start(runtime)
    children = [
        runtime.tools(founder.token).hire(f"child{i}", "Own tasks", f"hire{i}")["employee_id"]
        for i in range(3)
    ]
    with ThreadPoolExecutor(3) as pool:
        invocations = list(pool.map(lambda _: runtime.admit(), children))
    admitted = [i for i in invocations if i]
    assert len(admitted) == 2
    assert {binding(runtime, i)["account_id"] for i in [founder, *admitted]} == {"a", "b", "c"}
    assert runtime.admit() is None
    runtime.finish(founder.activation_id)
    assert runtime.admit() is not None


def test_summary_reservation_takes_no_account_slot(system):
    runtime, _ = system
    invocation = runtime.admit()
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    caller_account = binding(runtime, invocation)["account_id"]
    children = [
        runtime.tools(invocation.token).hire(
            f"summary-child-{index}", "Own summary test work", f"summary-hire-{index}"
        )["employee_id"]
        for index in range(2)
    ]
    admitted = [runtime.admit() for _ in children]
    assert all(item is not None for item in admitted)
    assert runtime.admit() is None  # every configured slot is held by a role

    with runtime.store.transaction() as db:
        reservation = runtime.accounts.reserve_summary(
            db, "summary-run", caller_session_id=invocation.session_id
        )
        assert reservation["account_id"] == caller_account


def test_summary_does_not_switch_accounts_when_callers_bound_account_was_removed(system):
    runtime, _ = system
    invocation = start(runtime)
    assert binding(runtime, invocation)["account_id"] == "a"
    updated = configuration()
    del updated["accounts"]["a"]
    updated["profiles"]["primary"]["accounts"] = ["b"]
    with runtime.store.transaction() as db:
        db.execute("UPDATE firm SET config_json=?", (json.dumps(updated),))
        assert (
            runtime.accounts.reserve_summary(
                db,
                "summary-check",
                caller_session_id=invocation.session_id,
            )
            is None
        )


def test_unknown_stale_and_out_of_order_quota_readings(system):
    runtime, now = system
    observe(runtime, now, "a", 0.01)
    runtime.accounts.observe(OWNER, "a", 1, observed_at=900, valid_for=1000, source="late result")
    assert runtime.accounts.status()["accounts"][0]["remaining_fraction"] == 0.01
    first = start(runtime)
    assert binding(runtime, first)["account_id"] == "b"
    runtime.tools(first.token).handoff("Next phase", "continue", "handoff")
    runtime.finish(first.activation_id)
    now[0] += 101
    assert runtime.accounts.status()["accounts"][0]["headroom"] == "unknown"
    # With the reading stale and nothing running, the primary's first account is eligible again.
    assert binding(runtime, start(runtime))["account_id"] == "a"


def pinned_and_choosing_configuration():
    """A founder pinned to the only codex account, and hires that share it but can
    also fall back to the only claude account."""
    return {
        "max_active": 6,
        "staffing": False,
        "founder_profile": "pinned",
        "default_profile": "choosing",
        "accounts": {
            "a": {"provider": "codex", "auth_file": "/missing/a", "max_active": 3},
            "c": {"provider": "claude", "auth_file": "/missing/c", "max_active": 3},
        },
        "profiles": {
            "pinned": {"provider": "codex", "model": "test", "effort": "low", "accounts": ["a"]},
            "choosing": {
                "provider": "codex",
                "model": "test",
                "effort": "low",
                "accounts": ["a"],
                "alternates": ["alternate"],
            },
            "alternate": {
                "provider": "claude",
                "model": "test",
                "effort": "low",
                "accounts": ["c"],
            },
        },
    }


def test_a_pinned_role_is_load_the_choosers_balance_around_and_nothing_more(tmp_path):
    """The founder can only ever run on "a". Its presence there counts as load, so the
    first chooser goes to claude; after that the choosers alternate. Past founder
    sessions leave no trace, so a founder that has cycled many times does not push
    later choosers anywhere."""
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test", "Build useful things.", pinned_and_choosing_configuration()
    )
    runtime.start()
    for index in range(3):
        founder = start(runtime)
        assert binding(runtime, founder)["account_id"] == "a"
        runtime.tools(founder.token).handoff("Keep going", "continue", f"handoff{index}")
        runtime.finish(founder.activation_id)
    founder = start(runtime)
    children = [
        runtime.tools(founder.token).hire(f"child{i}", "Own tasks", f"hire{i}")["employee_id"]
        for i in range(4)
    ]
    chosen = []
    for _ in children:
        invocation = start(runtime)
        assert invocation.employee_id in children
        chosen.append(binding(runtime, invocation)["account_id"])
    assert chosen == ["c", "a", "c", "a"]


def test_low_quota_ranks_last_but_a_sole_option_runs_to_empty(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test", "Build useful things.", pinned_and_choosing_configuration()
    )
    runtime.start()
    observe(runtime, now, "a", 0.01)
    founder = start(runtime)
    assert binding(runtime, founder)["account_id"] == "a"  # pinned: nowhere else to go
    child = runtime.tools(founder.token).hire("child", "Own tasks", "hire")["employee_id"]
    runtime.finish(founder.activation_id)
    invocation = start(runtime)
    assert invocation.employee_id == child
    # Both accounts are empty, so load alone would keep the primary's "a".
    assert binding(runtime, invocation)["account_id"] == "c"


def test_streamed_subscription_threshold_holds_retained_session_until_reset(system):
    runtime, now = system
    config = configuration()
    config["accounts"]["a"]["stop_admitting_at"] = 0.8
    runtime.owner.configure(OWNER, config)
    invocation = start(runtime)
    assert binding(runtime, invocation)["account_id"] == "a"
    runtime.accounts.record_subscription(
        "a", [{"window": "five_hour", "used_percent": 80, "resets_at": 1100}]
    )
    with runtime.store.transaction() as db:
        assert runtime.accounts.reserve_summary(
            db, "summary-paused", caller_session_id=invocation.session_id
        ) is None
    runtime.finish(invocation.activation_id)
    wake(runtime)

    assert runtime.admit() is None  # retained conversations cannot change accounts
    incident = next(
        item for item in runtime.accounts.status()["incidents"] if item["dependency"] == "a"
    )
    assert incident["kind"] == "quota"
    assert incident["until_at"] == 1100
    now[0] = 1100
    assert runtime.admit() is not None


def test_stale_subscription_threshold_does_not_create_a_hold(system):
    runtime, now = system
    config = configuration()
    config["accounts"]["a"]["stop_admitting_at"] = 0.8
    runtime.owner.configure(OWNER, config)
    now[0] += accounts.SUBSCRIPTION_READING_FRESH_SECONDS + 1
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO subscription_readings VALUES(?,?,?)",
            (
                "a",
                json.dumps(
                    [{"window": "five_hour", "used_percent": 99, "resets_at": now[0] + 60}]
                ),
                now[0] - accounts.SUBSCRIPTION_READING_FRESH_SECONDS - 1,
            ),
        )
        account = runtime.accounts.config(db)["accounts"]["a"]
        assert runtime.accounts._refresh_quota_threshold(db, "a", account) is None
    assert runtime.accounts.status()["incidents"] == []


@pytest.mark.parametrize("age,held", [(299.999, True), (300, False), (300.001, False)])
def test_subscription_admission_freshness_boundary_and_codex_account_hold(system, age, held):
    runtime, now = system
    config = configuration()
    config["accounts"]["a"]["stop_admitting_at"] = 0.8
    runtime.owner.configure(OWNER, config)
    # A model-specific bucket keeps the existing account-wide admission policy.
    window = {
        "window": "codex_other_primary",
        "limit_id": "codex_other",
        "used_percent": 80,
        "resets_at": 2000,
    }
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO subscription_readings VALUES(?,?,?)",
            ("a", json.dumps([window]), now[0] - age),
        )
        account = runtime.accounts.config(db)["accounts"]["a"]
        incident = runtime.accounts._refresh_quota_threshold(db, "a", account)
        assert bool(incident) is held
        if held:
            incident = runtime.accounts._incident(db, "account", "a")
            assert incident["kind"] == "quota" and incident["until_at"] == 2000


def test_empty_subscription_clears_reading_and_preserves_existing_timed_hold(system):
    runtime, now = system
    config = configuration()
    config["accounts"]["a"]["stop_admitting_at"] = 0.8
    runtime.owner.configure(OWNER, config)
    runtime.accounts.record_subscription(
        "a", [{"window": "codex_other_primary", "used_percent": 99, "resets_at": 1200}]
    )
    now[0] += 1
    runtime.accounts.record_subscription("a", [])
    with runtime.store.transaction() as db:
        assert runtime.accounts.subscription_reading(db, "a") == {
            "observed_at": now[0],
            "windows": [],
        }
        incident = runtime.accounts._incident(db, "account", "a")
        assert incident["kind"] == "quota" and incident["until_at"] == 1200
    now[0] = 1200
    assert runtime.accounts.status()["incidents"] == []


def test_new_crossed_window_extends_a_timed_quota_hold(system):
    runtime, now = system
    config = configuration()
    config["accounts"]["a"]["stop_admitting_at"] = 0.8
    runtime.owner.configure(OWNER, config)
    runtime.accounts.record_subscription(
        "a", [{"window": "five_hour", "used_percent": 90, "resets_at": 1100}]
    )
    now[0] += 10
    runtime.accounts.record_subscription(
        "a",
        [
            {"window": "five_hour", "used_percent": 90, "resets_at": 1100},
            {"window": "seven_day", "used_percent": 85, "resets_at": 1200},
        ],
    )
    incident = next(
        item for item in runtime.accounts.status()["incidents"] if item["dependency"] == "a"
    )
    assert incident["until_at"] == 1200


def test_subscription_warning_is_notified_once_per_window(system):
    runtime, now = system
    warning = {
        "window": "five_hour",
        "resets_at": now[0] + 100,
        "status": "allowed_warning",
    }
    for _ in range(2):
        runtime.accounts.record_subscription(
            "a",
            [{"window": "five_hour", "used_percent": 70, "resets_at": warning["resets_at"]}],
            status="allowed_warning",
            warning=warning,
        )
    with runtime.store.read() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM requests WHERE actor_id=? "
            "AND operation='subscription_limit_warning'",
            (accounts.OPERATIONS,),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE body LIKE '%subscription limit%\n\n%'"
        ).fetchone()[0] == 1


def one_provider_configuration(a_slots):
    return {
        "max_active": 6,
        "staffing": False,
        "founder_profile": "shared",
        "default_profile": "shared",
        "accounts": {
            "a": {"provider": "codex", "auth_file": "/missing/a", "max_active": a_slots},
            "b": {"provider": "codex", "auth_file": "/missing/b", "max_active": 6},
        },
        "profiles": {
            "shared": {
                "provider": "codex",
                "model": "test",
                "effort": "low",
                "accounts": ["a", "b"],
            },
        },
    }


def test_retained_conversations_outlive_a_lowered_limit_and_fresh_work_goes_elsewhere(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build useful things.", one_provider_configuration(1))
    runtime.start()
    founder = start(runtime)
    assert binding(runtime, founder)["account_id"] == "a"
    children = [
        runtime.tools(founder.token).hire(f"child{i}", "Own tasks", f"hire{i}")["employee_id"]
        for i in range(2)
    ]
    runtime.finish(founder.activation_id)
    # "a" is idle but holds the founder's conversation, so fresh work goes to "b".
    first = start(runtime)
    assert first.employee_id in children
    assert binding(runtime, first)["account_id"] == "b"
    runtime.finish(first.activation_id)
    runtime.owner.configure(OWNER, one_provider_configuration(12))
    second = start(runtime)
    assert binding(runtime, second)["account_id"] == "a"  # raised: now the emptier account
    runtime.finish(second.activation_id)
    runtime.owner.configure(OWNER, one_provider_configuration(1))
    wake(runtime, "founder", "wake-founder")
    wake(runtime, second.employee_id, "wake-second")
    resumed = [start(runtime), start(runtime)]
    # Lowered to one slot, yet both conversations on "a" keep running there.
    assert {binding(runtime, i)["account_id"] for i in resumed} == {"a"}
    assert {i.session_id for i in resumed} == {founder.session_id, second.session_id}


def test_retained_session_ignores_fresh_headroom_and_keeps_captured_settings(system):
    runtime, now = system
    first = start(runtime)
    runtime.finish(first.activation_id)
    observe(runtime, now, "a", 0)
    config = configuration()
    config["profiles"]["primary"]["model"] = "new-model"
    config["profiles"]["primary"]["accounts"] = ["b"]
    runtime.owner.configure(OWNER, config)
    wake(runtime)
    retained = start(runtime)
    assert retained.session_id == first.session_id
    assert binding(runtime, retained)["account_id"] == "a"
    assert json.loads(binding(runtime, retained)["settings_json"])["model"] == "test"


def test_fallback_lasts_for_session_then_primary_is_reconsidered(system):
    runtime, now = system
    for account in ("a", "b"):
        observe(runtime, now, account, 0)
    first = start(runtime)
    assert binding(runtime, first)["profile"] == "alternate"
    runtime.finish(first.activation_id)
    now[0] += 101
    wake(runtime)
    second = start(runtime)
    assert second.session_id == first.session_id
    assert binding(runtime, second)["profile"] == "alternate"
    runtime.tools(second.token).handoff("Start the next phase", "continue", "handoff")
    runtime.finish(second.activation_id)
    third = start(runtime)
    assert third.session_id != second.session_id
    assert binding(runtime, third)["profile"] == "primary"


def test_retained_quota_wait_deduplicates_notices_and_expires(system):
    runtime, now = system
    first = start(runtime)
    runtime.finish(first.activation_id, "failed", category="quota", retry_after=1100)
    assert runtime.admit() is None
    with runtime.store.read() as db:
        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    for _ in range(3):
        assert runtime.admit() is None
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == count
    observe(runtime, now, "a", 1)
    assert runtime.admit() is None  # readings do not clear an explicit quota block
    now[0] = 1100
    second = start(runtime)
    assert second.session_id == first.session_id
    assert role(runtime)["attempt_count"] == 1
    assert runtime.accounts.status()["incidents"] == []


@pytest.mark.parametrize("category,scope", [("auth", "account"), ("config", "profile")])
def test_setup_failures_pin_affected_role_without_spending_attempt(system, category, scope):
    runtime, _ = system
    first = runtime.admit()
    runtime.finish(first.activation_id, "interrupted", category=category)
    assert role(runtime)["attempt_count"] == 0
    assert role(runtime)["session_id"] is None
    assert runtime.admit() is None
    incident = runtime.accounts.status()["incidents"][0]
    assert incident["scope"] == scope
    runtime.accounts.clear(OWNER, incident["incident_id"])
    assert runtime.admit() is not None


@pytest.mark.parametrize("category", ["auth", "config", "quota"])
@pytest.mark.parametrize("generic_failures", [0, 1, 3])
def test_dependency_failures_preserve_generic_budget_and_session(
    system, category, generic_failures
):
    runtime, now = system
    for _ in range(generic_failures):
        attempt = start(runtime)
        runtime.finish(attempt.activation_id, "failed", category="transient")
        now[0] = role(runtime)["retry_at"]
    session = None
    for _ in range(5):
        attempt = start(runtime)
        session = session or attempt.session_id
        assert attempt.session_id == session
        runtime.finish(attempt.activation_id, "failed", category=category)
        assert role(runtime)["attempt_count"] == generic_failures
        assert role(runtime)["hold_reason"] is None
        assert runtime.admit() is None
        incident = runtime.accounts.status()["incidents"][0]
        runtime.accounts.clear(OWNER, incident["incident_id"])
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE body LIKE 'Employee execution requires manager action\n\n%'"
            ).fetchone()[0]
            == 0
        )
    attempt = start(runtime)
    runtime.finish(attempt.activation_id, "failed", category="transient")
    assert role(runtime)["attempt_count"] == generic_failures + 1
    if generic_failures == 3:
        assert role(runtime)["hold_reason"] == "attempts_exhausted"
    else:
        assert role(runtime)["retry_at"] == now[0] + (60, 300)[generic_failures]


@pytest.mark.parametrize("kind", ["config", "outage"])
def test_fresh_profile_hold_uses_fallback_but_retained_session_waits(system, kind):
    runtime, _ = system
    first = start(runtime)
    child = runtime.tools(first.token).hire("child", "Own work", "hire")["employee_id"]
    runtime.finish(first.activation_id)
    runtime.accounts.pause(OWNER, "profile", "primary", kind)
    wake(runtime)
    fallback = start(runtime)
    assert fallback.employee_id == child
    assert binding(runtime, fallback)["profile"] == "alternate"
    assert runtime.admit() is None
    with runtime.store.read() as db:
        notices = db.execute("SELECT employee_id FROM incident_roles").fetchall()
        assert [row[0] for row in notices] == [first.employee_id]


def two_profiles_one_account_configuration():
    return {
        "max_active": 6,
        "staffing": False,
        "founder_profile": "first",
        "default_profile": "second",
        "accounts": {
            "a": {"provider": "codex", "auth_file": "/missing/a", "max_active": 3},
        },
        "profiles": {
            "first": {"provider": "codex", "model": "one", "effort": "low", "accounts": ["a"]},
            "second": {"provider": "codex", "model": "two", "effort": "low", "accounts": ["a"]},
        },
    }


def test_model_quota_refusal_pauses_only_the_profile_and_spends_no_attempt(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test", "Build useful things.", two_profiles_one_account_configuration()
    )
    runtime.start()
    founder = start(runtime)
    assert binding(runtime, founder)["profile"] == "first"
    child = runtime.tools(founder.token).hire("child", "Own tasks", "hire")["employee_id"]
    runtime.finish(
        founder.activation_id,
        "failed",
        category="quota",
        retry_after=now[0] + 500,
        failure_code="model_quota_reached",
    )
    incidents = runtime.accounts.status()["incidents"]
    assert [(i["scope"], i["dependency"], i["kind"], i["until_at"]) for i in incidents] == [
        ("profile", "first", "quota", 1500)
    ]
    assert incidents[0]["code"] == "model_quota_reached"
    with runtime.store.read() as db:
        pinned = db.execute(
            "SELECT COUNT(*) FROM role_dependencies WHERE employee_id=?", ("founder",)
        ).fetchone()[0]
    assert pinned == 0
    assert role(runtime)["attempt_count"] == 0
    assert role(runtime)["hold_reason"] is None
    with runtime.store.read() as db:
        selected = runtime.accounts.select(db, runtime.store.role(db, child))
    assert selected is not None and selected["profile"] == "second"
    assert selected["account_id"] == "a"


def agy_two_profiles(model, effort):
    """The first profile on an AGY account, where effort xhigh always selects Carbon."""
    config = two_profiles_one_account_configuration()
    config["accounts"]["a"]["provider"] = "agy"
    config["profiles"]["first"].update(provider="agy", model=model, effort=effort)
    config["profiles"]["second"].update(provider="agy", model="gemini-4-hydrogen", effort="low")
    return config


def model_quota_system(tmp_path, effort=None, config=None):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize(
        "test", "Build useful things.", config or two_profiles_one_account_configuration()
    )
    runtime.start()
    if effort:
        with runtime.store.transaction() as db:
            db.execute("UPDATE roles SET effort=? WHERE employee_id='founder'", (effort,))
    return runtime, now


def activation_row(runtime, activation_id):
    with runtime.store.read() as db:
        row = db.execute("SELECT * FROM activations WHERE activation_id=?", (activation_id,))
        return dict(row.fetchone())


def test_model_quota_on_the_profiles_own_settings_is_timed_and_refunds_the_attempt(tmp_path):
    runtime, now = model_quota_system(tmp_path)
    founder = start(runtime)
    runtime.finish(
        founder.activation_id, "failed", category="quota",
        failure_code="model_quota_reached", retry_after=now[0] + 500,
    )
    assert activation_row(runtime, founder.activation_id)["failure_category"] == "quota"
    assert [(i["scope"], i["dependency"]) for i in runtime.accounts.status()["incidents"]] == [
        ("profile", "first")
    ]
    assert role(runtime)["attempt_count"] == 0
    assert role(runtime)["retry_at"] is None


@pytest.mark.parametrize("model,profile_effort,role_effort", [
    ("one", "low", "high"),  # effort alone does not change the model
    ("gemini-4-carbon", "medium", "high"),
    ("gemini-4-carbon", "high", "xhigh"),  # xhigh selects Carbon, which the profile names
])
def test_model_quota_on_an_override_that_keeps_the_model_still_holds_the_profile(
    tmp_path, model, profile_effort, role_effort
):
    config = (
        agy_two_profiles(model, profile_effort) if model.startswith("gemini") else None
    )
    runtime, now = model_quota_system(tmp_path, effort=role_effort, config=config)
    founder = start(runtime)
    assert json.loads(binding(runtime, founder)["settings_json"])["effort"] == role_effort
    runtime.finish(
        founder.activation_id, "failed", category="quota",
        failure_code="model_quota_reached", retry_after=now[0] + 500,
    )
    assert activation_row(runtime, founder.activation_id)["failure_category"] == "quota"
    assert [(i["scope"], i["dependency"]) for i in runtime.accounts.status()["incidents"]] == [
        ("profile", "first")
    ]
    assert role(runtime)["attempt_count"] == 0 and role(runtime)["retry_at"] is None


def test_model_quota_on_an_override_that_selects_another_model_is_the_roles_own_failure(
    tmp_path
):
    # The profile names Hydrogen, but this role's xhigh effort ran Carbon: holding the
    # profile would pause Hydrogen roles for a refusal that was never about Hydrogen.
    runtime, now = model_quota_system(
        tmp_path, effort="xhigh", config=agy_two_profiles("gemini-4-hydrogen", "medium")
    )
    founder = start(runtime)
    assert json.loads(binding(runtime, founder)["settings_json"])["effort"] == "xhigh"
    runtime.finish(
        founder.activation_id, "failed", category="quota",
        failure_code="model_quota_reached", retry_after=now[0] + 500000,
    )
    assert runtime.accounts.status()["incidents"] == []
    assert activation_row(runtime, founder.activation_id)["failure_category"] == "unknown"
    assert role(runtime)["attempt_count"] == 1
    assert role(runtime)["hold_reason"] is None
    assert now[0] < role(runtime)["retry_at"] < now[0] + 3600
    for attempt in (2, 3, 4):
        now[0] = role(runtime)["retry_at"]
        again = start(runtime)
        runtime.finish(
            again.activation_id, "failed", category="quota",
            failure_code="model_quota_reached", retry_after=now[0] + 500000,
        )
        assert role(runtime)["attempt_count"] == attempt
    assert role(runtime)["hold_reason"] == "attempts_exhausted"
    assert runtime.accounts.status()["incidents"] == []


def test_quota_failure_without_a_binding_or_model_code_still_needs_an_adapter(tmp_path):
    runtime, now = model_quota_system(tmp_path)
    founder = start(runtime)
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM execution_bindings WHERE session_id=?", (founder.session_id,))
    with pytest.raises(FirmError, match="requires a provider/account adapter"):
        runtime.finish(
            founder.activation_id, "failed", category="quota", failure_code="quota_exceeded",
            retry_after=now[0] + 60,
        )


def test_report_llm_setup_notifies_once_per_model_and_code(system):
    runtime, _ = system

    def notices():
        with runtime.store.read() as db:
            return db.execute(
                "SELECT COUNT(*) FROM messages WHERE body LIKE ?",
                (render("notices", "llm_setup_rejected_subject", provider="codex") + "\n\n%",),
            ).fetchone()[0]

    for _ in range(2):
        with runtime.store.transaction() as db:
            runtime.accounts.report_llm_setup(db, "codex", "m1", "model_rejected")
    assert notices() == 1
    with runtime.store.transaction() as db:
        runtime.accounts.report_llm_setup(db, "codex", "m2", "model_rejected")
    assert notices() == 2
    with runtime.store.transaction() as db:
        runtime.accounts.report_llm_setup(db, "codex", "m2", "other_code")
    assert notices() == 3
    assert runtime.accounts.status()["incidents"] == []


def test_account_auth_hold_does_not_block_other_roles(system):
    runtime, _ = system
    first = start(runtime)
    child = runtime.tools(first.token).hire("child", "Own tasks", "hire")["employee_id"]
    runtime.finish(first.activation_id, "failed", category="auth")
    next_run = start(runtime)
    assert next_run.employee_id == child
    # The held account offers codex no room; its other account and claude tie, so route
    # order picks the primary's.
    assert binding(runtime, next_run)["account_id"] == "b"
    assert runtime.admit() is None


def test_retry_does_not_clear_shared_hold_or_newer_incident(system):
    runtime, _ = system
    invocation = start(runtime)
    runtime.finish(invocation.activation_id, "failed", category="refusal")
    old = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    current = runtime.accounts.pause(OWNER, "account", "a", "quota")["incident_id"]
    runtime.accounts.clear(OWNER, old)
    result = runtime.retry(OWNER, "founder", role(runtime)["recovery_episode"], "retry")
    assert result["execution_dependency_blocked"] is True
    assert runtime.admit() is None
    assert runtime.accounts.status()["incidents"][0]["incident_id"] == current


def test_immediate_owner_retry_preserves_budget_and_cancels_future_wake(system):
    runtime, now = system
    invocation = start(runtime)
    runtime.finish(invocation.activation_id, "failed", category="transient")
    before = role(runtime)
    old_deadline = before["retry_at"]
    assert old_deadline > now[0]
    with pytest.raises(FirmError, match="only the owner"):
        runtime.retry("founder", "founder", before["recovery_episode"], "manager",
                      immediate=True)
    with pytest.raises(FirmError, match="not held"):
        runtime.retry(OWNER, "founder", before["recovery_episode"], "ordinary")
    result = runtime.retry(OWNER, "founder", before["recovery_episode"], "now", immediate=True)
    assert runtime.retry(OWNER, "founder", before["recovery_episode"], "now",
                         immediate=True) == result
    assert role(runtime)["attempt_count"] == before["attempt_count"]
    assert role(runtime)["retry_at"] is None
    with runtime.store.read() as db:
        assert not db.execute(
            "SELECT 1 FROM execution_requests WHERE employee_id='founder' AND reason='recovery'"
        ).fetchone()
    # Competing scheduler admission cannot create two activations from the retry.
    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = list(executor.map(lambda _: runtime.admit(), range(2)))
    admitted = [item for item in admissions if item is not None]
    assert len(admitted) == 1
    runtime.mark_started(admitted[0].activation_id)
    runtime.finish(admitted[0].activation_id)
    now[0] = old_deadline + 1
    assert runtime.admit() is None
    with pytest.raises(FirmError, match="stale recovery"):
        runtime.retry(OWNER, "founder", before["recovery_episode"], "stale", immediate=True)


@pytest.mark.parametrize("kind", ["auth", "config", "quota"])
def test_immediate_retry_does_not_clear_dependency_holds(system, kind):
    runtime, _ = system
    invocation = start(runtime)
    runtime.finish(invocation.activation_id, "failed", category="transient")
    before = role(runtime)
    scope, dependency = ("profile", "primary") if kind == "config" else ("account", "a")
    incident = runtime.accounts.pause(OWNER, scope, dependency, kind)["incident_id"]
    result = runtime.retry(OWNER, "founder", before["recovery_episode"], "now", immediate=True)
    assert result["execution_dependency_blocked"]
    assert runtime.admit() is None
    assert role(runtime)["attempt_count"] == before["attempt_count"]
    assert incident in {item["incident_id"] for item in runtime.accounts.status()["incidents"]}


@pytest.mark.parametrize("code,remedy", [
    ("oauth_org_not_allowed", "organization does not allow subscription access"),
    ("account_on_hold", "provider has put this account on hold"),
    ("invalid_api_key", "saved login was rejected"),
    ("unsafe private /path", "saved login was rejected"),
    ("synthetic-secret-token-looks-like-code", "saved login was rejected"),
])
def test_account_hold_notice_names_safe_code_exact_incident_and_remedy(system, code, remedy):
    runtime, _ = system
    invocation = start(runtime)
    runtime.finish(invocation.activation_id, "failed", category="auth", failure_code=code)
    incident = runtime.accounts.status()["incidents"][0]
    with runtime.store.read() as db:
        notices = db.execute(
            "SELECT body FROM messages WHERE body LIKE 'Shared execution dependency paused\n\n%'"
        ).fetchall()
    assert len(notices) == 1
    body = notices[0][0]
    assert "Shared account a" in body and incident["incident_id"] in body
    assert remedy in body and "clear this exact incident" in body
    assert "unsafe private" not in body
    assert "To sign in, run `firm login a --provider codex --auth-file /missing/a`." in body
    if code not in {"unsafe private /path", "synthetic-secret-token-looks-like-code"}:
        assert f"Code: {code}." in body
    else:
        assert "Code: not reported." in body
        assert code not in body


def test_shared_incident_manager_copy_has_ordinary_priority(system):
    runtime, _ = system
    founder = start(runtime)
    child = runtime.tools(founder.token).hire("child", "Own tasks", "hire-priority-child")[
        "employee_id"
    ]
    runtime.finish(founder.activation_id)
    child_run = start(runtime)
    assert child_run.employee_id == child
    runtime.finish(child_run.activation_id, "failed", category="quota")

    with runtime.store.read() as db:
        incident_notice = db.execute(
            "SELECT m.message_id FROM messages m JOIN message_deliveries d USING(message_id) "
            "WHERE d.employee_id='founder' "
            "AND m.body LIKE 'Employee waiting for shared capacity\n\n%'"
        ).fetchone()
        assert incident_notice is not None
        wake = db.execute(
            "SELECT priority FROM scheduling_opportunities "
            "WHERE employee_id='founder' AND source_id=?",
            (incident_notice["message_id"],),
        ).fetchone()
        assert wake["priority"] == ORDINARY_PRIORITY


def test_owner_fresh_routing_requires_exit_preserves_budget_mail_and_holds(system):
    runtime, _ = system
    first = start(runtime)
    runtime.finish(first.activation_id, "failed", category="refusal")
    incident = runtime.accounts.pause(OWNER, "account", "a", "auth")["incident_id"]
    runtime.owner.route(OWNER, "founder", "alternate", "route")
    assert role(runtime)["attempt_count"] == 1
    assert role(runtime)["hold_reason"] == "refusal"
    assert runtime.admit() is None
    runtime.retry(OWNER, "founder", role(runtime)["recovery_episode"], "retry")
    second = start(runtime)
    assert binding(runtime, second)["provider"] == "claude"
    assert second.session_id != first.session_id
    assert second.context["recovery"]["context_retained"] is False
    result = runtime.owner.stop_role(OWNER, "founder", "stop")
    assert result["execution_exit_confirmed"] is False
    with pytest.raises(FirmError, match="confirm.*stopped"):
        runtime.owner.route(OWNER, "founder", "primary", "unsafe-route")
    with pytest.raises(FirmError):
        runtime.tools(second.token).peek()
    wake(runtime)
    runtime.finish(second.activation_id, "interrupted", category="owner_stop")
    assert role(runtime)["attempt_count"] == 0
    assert runtime.admit() is None
    runtime.owner.resume_role(OWNER, "founder", "resume")
    third = start(runtime)
    assert third.session_id == second.session_id
    assert third.context["inbox"]["threads"]
    assert runtime.accounts.status()["incidents"][0]["incident_id"] == incident


@pytest.mark.parametrize(
    "action",
    [
        lambda r: r.accounts.clear("founder", "anything"),
        lambda r: r.accounts.pause("founder", "account", "a", "auth"),
        lambda r: r.owner.configure("founder", configuration()),
        lambda r: r.owner.route("founder", "founder", "alternate", "route"),
        lambda r: r.owner.stop_role("founder", "founder", "stop"),
    ],
)
def test_account_and_routing_controls_are_owner_only(system, action):
    runtime, _ = system
    with pytest.raises(FirmError, match="only the owner"):
        action(runtime)


def test_configuration_accepts_context_policy_and_rejects_mismatched_accounts(system):
    runtime, _ = system
    config = configuration()
    config["profiles"]["primary"]["accounts"] = ["c"]
    with pytest.raises(FirmError, match="invalid accounts"):
        runtime.owner.configure(OWNER, config)
    config = copy.deepcopy(configuration())
    config["profiles"]["primary"]["context_window"] = 400000
    assert runtime.owner.configure(OWNER, config)["status"] == "applied"
    config["profiles"]["primary"]["handoff_reserve"] = 400000
    with pytest.raises(FirmError, match="smaller"):
        runtime.owner.configure(OWNER, config)


def test_accounts_without_auth_file_record_the_default_credentials_path(system, monkeypatch):
    runtime, _ = system
    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", Path("~/creds"))
    config = configuration()
    del config["accounts"]["c"]["auth_file"]
    supplied = copy.deepcopy(config)
    runtime.owner.configure(OWNER, config)
    assert config == supplied
    with runtime.store.read() as db:
        stored = runtime.accounts.config(db)["accounts"]
    assert stored["c"]["auth_file"] == str(Path.home() / "creds" / "c.json")
    assert stored["a"]["auth_file"] == "/missing/a"
    config = configuration()
    config["accounts"]["../c"] = config["accounts"].pop("c")
    del config["accounts"]["../c"]["auth_file"]
    config["profiles"]["alternate"]["accounts"] = ["../c"]
    with pytest.raises(FirmError, match="account names"):
        runtime.owner.configure(OWNER, config)

    config = configuration()
    config["accounts"]["c"]["auth_file"] = "~a-user-that-does-not-exist/credentials"
    with pytest.raises(FirmError, match="absolute auth_file"):
        runtime.owner.configure(OWNER, config)

    config = configuration()
    config["accounts"]["../c"] = config["accounts"].pop("c")
    config["profiles"]["alternate"]["accounts"] = ["../c"]
    with pytest.raises(FirmError, match="account names"):
        runtime.owner.configure(OWNER, config)


def test_invalid_reconfiguration_is_rejected_before_github_setup(system, monkeypatch):
    runtime, _ = system
    config = configuration()
    config["github"] = {"organization": "example"}
    config["founder_profile"] = "alternate"
    del config["profiles"]["primary"]
    called = []
    monkeypatch.setattr(owner.github, "connect", lambda settings: called.append(settings))

    with pytest.raises(FirmError, match="retain current roles"):
        runtime.owner.configure(OWNER, config)

    assert called == []


def test_resume_requires_a_stopped_role(system):
    runtime, _ = system
    with pytest.raises(FirmError, match="stopped role"):
        runtime.owner.resume_role(OWNER, "founder", "resume-active")
    with runtime.store.read() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM execution_requests WHERE reason='owner_resume'"
        ).fetchone()[0] == 0


def test_configuration_accepts_provider_supported_claude_xhigh(system):
    runtime, _ = system
    config = configuration()
    config["profiles"]["alternate"]["effort"] = "xhigh"
    assert runtime.owner.configure(OWNER, config)["status"] == "applied"


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda c: c["accounts"]["a"].update(provider=[]), "invalid account provider"),
        (lambda c: c["accounts"]["a"].update(stop_admitting_at=1.1), "stop_admitting_at"),
        (
            lambda c: c["profiles"]["primary"].update(provider=[]),
            "invalid execution profile provider",
        ),
        (lambda c: c["profiles"]["primary"].update(effort=[]), "unsupported effort"),
        (
            lambda c: c.update(summary={"codex": {"model": "gpt-test", "effort": []}}),
            "unsupported summary effort",
        ),
    ],
)
def test_invalid_configuration_value_types_raise_firm_errors(system, edit, message):
    runtime, _ = system
    config = configuration()
    edit(config)

    with pytest.raises(FirmError, match=message):
        runtime.owner.configure(OWNER, config)


def test_account_specific_holds_cannot_be_mislabelled_as_profile_holds(system):
    runtime, _ = system
    with pytest.raises(FirmError, match="scoped to an account"):
        runtime.accounts.pause(OWNER, "profile", "primary", "auth")


def test_profile_scoped_quota_hold_is_accepted(system):
    runtime, now = system
    runtime.accounts.pause(OWNER, "profile", "primary", "quota", until=now[0] + 60)
    incident = runtime.accounts.status()["incidents"][0]
    assert (incident["scope"], incident["dependency"], incident["kind"]) == (
        "profile",
        "primary",
        "quota",
    )


@pytest.mark.parametrize("kind", ["auth", "config"])
def test_repair_holds_cannot_expire_without_explicit_owner_clearance(system, kind):
    runtime, now = system
    scope, dependency = ("account", "a") if kind == "auth" else ("profile", "primary")
    with pytest.raises(FirmError, match="explicit clearance"):
        runtime.accounts.pause(OWNER, scope, dependency, kind, until=now[0] + 60)


def test_removed_retained_account_pins_role_and_notifies_manager_until_repaired(system):
    runtime, _ = system
    first = start(runtime)
    assert binding(runtime, first)["account_id"] == "a"
    runtime.finish(first.activation_id)
    config = configuration()
    del config["accounts"]["a"]
    config["profiles"]["primary"]["accounts"] = ["b"]
    runtime.owner.configure(OWNER, config)
    wake(runtime)

    assert runtime.admit() is None
    incident = runtime.accounts.status()["incidents"][0]
    assert (incident["scope"], incident["dependency"], incident["kind"]) == (
        "account",
        "a",
        "config",
    )
    with runtime.store.read() as db:
        pinned = db.execute(
            "SELECT incident_id FROM role_dependencies WHERE employee_id='founder'"
        ).fetchone()
        assert pinned["incident_id"] == incident["incident_id"]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM incident_roles WHERE incident_id=? "
                "AND employee_id='founder' AND manager_id=?",
                (incident["incident_id"], OWNER),
            ).fetchone()[0]
            == 1
        )

    runtime.owner.configure(OWNER, configuration())
    runtime.accounts.clear(OWNER, incident["incident_id"])
    with runtime.store.read() as db:
        assert not db.execute(
            "SELECT 1 FROM role_dependencies WHERE employee_id='founder'"
        ).fetchone()
    resumed = start(runtime)
    assert resumed.session_id == first.session_id
    assert binding(runtime, resumed)["account_id"] == "a"


def test_provider_interruption_resumes_without_failure_budget_or_holds(system):
    runtime, _ = system
    session = None
    for _ in range(6):
        attempt = start(runtime)
        session = session or attempt.session_id
        assert attempt.session_id == session
        runtime.finish(attempt.activation_id, "interrupted", category="transient")
        assert role(runtime)["attempt_count"] == 0
        assert role(runtime)["hold_reason"] is None
        assert runtime.accounts.status()["incidents"] == []


def test_transient_setup_failures_use_bounded_existing_budget_and_notice(system):
    runtime, now = system
    for count in range(1, 5):
        attempt = runtime.admit()
        assert attempt is not None
        runtime.finish(attempt.activation_id, "interrupted", category="transient")
        current = role(runtime)
        assert current["attempt_count"] == count
        assert runtime.accounts.status()["incidents"] == []
        if count < 4:
            assert current["retry_at"] > now[0]
            now[0] = current["retry_at"]
    assert role(runtime)["hold_reason"] == "attempts_exhausted"
    assert runtime.admit() is None
    with runtime.store.read() as db:
        notices = db.execute("SELECT body FROM messages WHERE body LIKE ?",
                             ("Employee work requires manager action\n\n%",)).fetchall()
    assert len(notices) == 1 and "firm route --fresh" in notices[0][0]
    episode = role(runtime)["recovery_episode"]
    runtime.owner.route(OWNER, "founder", "primary", "fresh")
    assert role(runtime)["session_id"] is None
    assert role(runtime)["hold_reason"] == "attempts_exhausted"
    runtime.retry(OWNER, "founder", episode, "retry")
    assert runtime.admit() is not None


def test_guidance_change_keeps_native_session_binding_and_execution_profile(system):
    runtime, _ = system
    first = runtime.admit()
    runtime.mark_started(first.activation_id)
    original = binding(runtime, first)
    with runtime.store.read() as db:
        profile = runtime.store.role(db, "founder")["execution_profile"]
    runtime.finish(first.activation_id)
    runtime.owner.set_guidance(OWNER, "founder", "specialist-adviser", ["legal"], "guides")
    runtime.owner.send_mail(OWNER, ["founder"], "Continue", request_id="wake")
    next_run = runtime.admit()
    assert next_run.context["guidance_changed"] is True
    assert next_run.session_id == first.session_id
    assert binding(runtime, next_run) == original
    with runtime.store.read() as db:
        assert runtime.store.role(db, "founder")["execution_profile"] == profile


def test_role_dependency_is_deleted_on_expiry_and_supersession(system):
    runtime, now = system
    first = start(runtime)
    with runtime.store.transaction() as db:
        timed = runtime.accounts.hold(db, "account", "a", "quota", until=now[0] + 50)
        runtime.accounts._pin_role(db, role(runtime), timed)
    now[0] = 1060
    with runtime.store.transaction() as db:
        assert runtime.accounts._incident(db, "account", "a") is None
        assert not db.execute("SELECT 1 FROM role_dependencies").fetchone()
        held = runtime.accounts.hold(db, "account", "a", "quota", until=now[0] + 50)
        runtime.accounts._pin_role(db, role(runtime), held)
        runtime.accounts.hold(db, "account", "a", "auth")
        assert not db.execute(
            "SELECT 1 FROM role_dependencies WHERE incident_id=?", (held,)
        ).fetchone()
    assert first.employee_id == "founder"
