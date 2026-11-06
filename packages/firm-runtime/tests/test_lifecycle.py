from __future__ import annotations

import hashlib
import json

import pytest

from firm_runtime.accounts import TASK_DEFAULTS
from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import FOUNDER_ID, Lifecycle
from firm_runtime.messaging import Messaging
from firm_runtime.store import CONTROL_PRIORITY, OPERATIONS, OWNER, Store

CONFIG = {
    "max_active": 2,
    "staffing": False,
    "founder_profile": "heavy",
    "profiles": {"heavy": {"model": "test"}, "light": {"model": "test-small"}},
}


class RecordingMessaging:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.notices: list[dict] = []
        self.retirements: list[str] = []

    def notice(
        self,
        db,
        recipients,
        body,
        *,
        subject,
        priority=0,
        wake=True,
    ) -> dict:
        call = {
            "recipients": recipients,
            "body": body,
            "subject": subject,
            "priority": priority,
            "wake": wake,
        }
        self.notices.append(call)
        if wake:
            for recipient in recipients:
                self.store.request_execution(db, recipient, "operations_notice", priority)
        return {"status": "sent", "message_id": f"m{len(self.notices)}"}

    def retire(self, db, employee_id) -> dict:
        self.retirements.append(employee_id)
        return {"employee_id": employee_id, "undeliverable": 0, "bounces": []}


@pytest.fixture
def services(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: 100.0)
    messaging = RecordingMessaging(store)
    ids = iter(["employee-a", "employee-b", "employee-c", "employee-d"])
    lifecycle = Lifecycle(store, messaging, id_factory=lambda: next(ids))
    initialized = lifecycle.initialize("test-firm", "Build useful things.", CONFIG)
    return store, messaging, lifecycle, initialized


def scalar(store: Store, query: str, parameters=()):
    with store.read() as db:
        return db.execute(query, parameters).fetchone()[0]


def test_initialize_is_stopped_atomic_and_exactly_once(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: 12.0)
    messaging = RecordingMessaging(store)
    lifecycle = Lifecycle(store, messaging)

    first = lifecycle.initialize("acme", "Keep the mandate verbatim.\n", CONFIG)
    second = lifecycle.initialize("acme", "Keep the mandate verbatim.\n", CONFIG)

    assert first == second
    assert first["firm"] == {
        "firm_id": "acme", "state": "stopped", "config": {**CONFIG, "task": TASK_DEFAULTS}
    }
    assert first["founder"]["employee_id"] == FOUNDER_ID
    assert first["founder"]["manager_id"] == OWNER
    assert first["founder"]["execution_profile"] == "heavy"
    assert first["founder"]["mandate"]["body"] == "Keep the mandate verbatim.\n"
    assert scalar(store, "SELECT COUNT(*) FROM roles") == 1
    assert scalar(store, "SELECT COUNT(*) FROM mandates") == 1
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == 1
    assert scalar(store, "SELECT priority FROM scheduling_opportunities") == CONTROL_PRIORITY
    assert scalar(store, "SELECT recovery_episode FROM roles")

    with pytest.raises(FirmError, match="different inputs"):
        lifecycle.initialize("acme", "A conflicting mandate.", CONFIG)
    with pytest.raises(FirmError, match="different inputs"):
        lifecycle.initialize("other", "Keep the mandate verbatim.\n", CONFIG)


@pytest.mark.parametrize(
    "config, message",
    [
        (
            {
                "max_active": 0,
                "staffing": False,
                "founder_profile": "heavy",
                "profiles": {"heavy": {}},
            },
            "positive integer",
        ),
        (
            {
                "max_active": True,
                "staffing": False,
                "founder_profile": "heavy",
                "profiles": {"heavy": {}},
            },
            "positive integer",
        ),
        (
            {
                "max_active": 1,
                "staffing": False,
                "founder_profile": "missing",
                "profiles": {"heavy": {}},
            },
            "configured profile",
        ),
        (
            {
                "max_active": 1,
                "staffing": False,
                "founder_profile": "heavy",
                "profiles": {"heavy": {}},
                "retry_delays": [60, 300],
            },
            "exactly three",
        ),
        (
            {"max_active": 1, "staffing": False, "founder_profile": "heavy", "profiles": []},
            "must be a mapping",
        ),
    ],
)
def test_initialize_validates_required_configuration(tmp_path, config, message):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, RecordingMessaging(store))

    with pytest.raises(FirmError, match=message):
        lifecycle.initialize("acme", "A mandate", config)
    assert scalar(store, "SELECT COUNT(*) FROM firm") == 0


def test_initialize_validates_explicit_summary_provider_settings(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, RecordingMessaging(store))
    config = {
        **CONFIG,
        "summary": {"codex": {"model": "gpt-test", "effort": "unsupported"}},
    }
    with pytest.raises(FirmError, match="unsupported summary effort"):
        lifecycle.initialize("acme", "A mandate", config)


def test_hire_is_durable_idempotent_and_binds_exact_identity(services):
    store, _, lifecycle, _ = services

    hired = lifecycle.hire(
        FOUNDER_ID, "engineering", "Own product engineering.", "heavy", request_id="hire-1"
    )
    retried = lifecycle.hire(
        FOUNDER_ID, "engineering", "Own product engineering.", "heavy", request_id="hire-1"
    )

    assert hired == retried
    assert hired["status"] == "pending"
    assert hired["employee_id"] == "employee-a"
    assert hired["address"] == "founder/engineering"
    assert hired["manager_id"] == FOUNDER_ID
    assert hired["mandate"]["revision"] == 0
    assert hired["execution_profile"] == "heavy"
    assert scalar(store, "SELECT COUNT(*) FROM roles") == 2
    assert scalar(store, "SELECT COUNT(DISTINCT recovery_episode) FROM roles") == 2
    assert (
        scalar(
            store, "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id='employee-a'"
        )
        == 1
    )

    with pytest.raises(FirmError, match="different operation or payload"):
        lifecycle.hire(FOUNDER_ID, "engineering", "Changed mandate", "heavy", request_id="hire-1")
    with pytest.raises(FirmError, match="already in use"):
        lifecycle.hire(FOUNDER_ID, "engineering", "Another employee", "light", request_id="hire-2")
    with pytest.raises(FirmError, match="not configured"):
        lifecycle.hire(FOUNDER_ID, "finance", "Own finance", "unknown", request_id="hire-3")
    for principal in (OWNER, OPERATIONS):
        with pytest.raises(FirmError, match="active employee"):
            lifecycle.hire(
                principal, "finance", "Own finance", "heavy", request_id=f"hire-{principal}"
            )


def test_role_identity_follows_the_discovery_public_name(services):
    store, _, lifecycle, _ = services
    lifecycle.hire(FOUNDER_ID, "engineering", "Own engineering.", "heavy", request_id="hire")
    with store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=? WHERE employee_id='employee-a'",
            ("# Engineering lead\n\nPublic name: **Ada Lovelace**\n",),
        )
    assert lifecycle.get_role(FOUNDER_ID, "employee-a")["identity"] == "Ada Lovelace"


def test_lifecycle_mutation_joins_the_facade_authorization_transaction(services):
    store, _, lifecycle, _ = services

    with pytest.raises(RuntimeError, match="facade failed"):
        with store.transaction():
            lifecycle.hire(
                FOUNDER_ID,
                "temporary",
                "This whole call must roll back.",
                "light",
                request_id="nested-hire",
            )
            raise RuntimeError("facade failed")

    assert scalar(store, "SELECT COUNT(*) FROM roles") == 1
    assert scalar(store, "SELECT COUNT(*) FROM requests WHERE request_id='nested-hire'") == 0
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == 1


def test_revision_authority_concurrency_and_owner_control_wake(services):
    store, messaging, lifecycle, _ = services
    manager = lifecycle.hire(
        FOUNDER_ID, "engineering", "Lead engineering.", "heavy", request_id="manager"
    )
    report = lifecycle.hire(
        manager["employee_id"], "api", "Own the API.", "light", request_id="report"
    )
    initial_report_wakes = scalar(
        store,
        "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
        (report["employee_id"],),
    )

    revised = lifecycle.revise_mandate(
        manager["employee_id"],
        report["employee_id"],
        0,
        "Own and operate the API.",
        reason="Operations added",
        request_id="revise-report",
    )
    assert revised["mandate"] == {
        "revision": 1,
        "author_id": manager["employee_id"],
        "reason": "Operations added",
        "digest": hashlib.sha256(b"Own and operate the API.").hexdigest(),
    }
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
            (report["employee_id"],),
        )
        == initial_report_wakes
    )
    assert messaging.notices == []

    with pytest.raises(FirmError, match="revision conflict"):
        lifecycle.revise_mandate(
            manager["employee_id"],
            report["employee_id"],
            0,
            "Stale edit",
            request_id="stale",
        )
    with pytest.raises(FirmError, match="direct manager"):
        lifecycle.revise_mandate(
            report["employee_id"],
            report["employee_id"],
            1,
            "Self edit",
            request_id="self",
        )

    founder_wakes = scalar(
        store, "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?", (FOUNDER_ID,)
    )
    owner_revision = lifecycle.revise_mandate(
        OWNER,
        FOUNDER_ID,
        0,
        "Build fewer, better things.",
        reason="Narrow the focus",
        request_id="owner-revision",
    )
    retried = lifecycle.revise_mandate(
        OWNER,
        FOUNDER_ID,
        0,
        "Build fewer, better things.",
        reason="Narrow the focus",
        request_id="owner-revision",
    )
    assert owner_revision == retried
    assert owner_revision["mandate"]["revision"] == 1
    assert len(messaging.notices) == 1
    assert messaging.notices[0]["recipients"] == [FOUNDER_ID]
    assert messaging.notices[0]["priority"] == CONTROL_PRIORITY
    assert "revision 1" in messaging.notices[0]["body"]
    assert "Narrow the focus" in messaging.notices[0]["body"]
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
            (FOUNDER_ID,),
        )
        == founder_wakes + 1
    )


def test_owner_revision_rolls_back_when_notice_fails(services):
    store, _, _, _ = services

    class BrokenMessaging(RecordingMessaging):
        def notice(self, *args, **kwargs):
            raise RuntimeError("message storage failed")

    lifecycle = Lifecycle(store, BrokenMessaging(store))
    with pytest.raises(RuntimeError, match="message storage failed"):
        lifecycle.revise_mandate(OWNER, FOUNDER_ID, 0, "Uncommitted mandate", request_id="atomic")

    assert scalar(store, "SELECT COUNT(*) FROM mandates WHERE employee_id='founder'") == 1
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM requests WHERE actor_id=? AND request_id=?",
            (OWNER, "atomic"),
        )
        == 0
    )


def test_fire_refuses_live_descendants_retires_mail_and_preserves_execution(services):
    store, messaging, lifecycle, _ = services
    manager = lifecycle.hire(
        FOUNDER_ID, "engineering", "Lead engineering.", "heavy", request_id="manager"
    )
    report = lifecycle.hire(
        manager["employee_id"], "api", "Own the API.", "light", request_id="report"
    )

    with pytest.raises(FirmError, match="live descendants"):
        lifecycle.fire(
            FOUNDER_ID, manager["employee_id"], reason="Flatten team", request_id="too-soon"
        )
    with pytest.raises(FirmError, match="founder cannot be fired"):
        lifecycle.fire(OWNER, FOUNDER_ID, reason="Replace founder", request_id="founder")

    with store.transaction() as db:
        db.execute(
            """INSERT INTO activations(
                   activation_id,employee_id,session_id,state,token_hash,wake_snapshot,
                   context_json,created_at,started_at
               ) VALUES('active-report',?,?,'running','token-hash','[]','{}',100,100)""",
            (report["employee_id"], "session-report"),
        )
    fired = lifecycle.fire(
        manager["employee_id"],
        report["employee_id"],
        reason="Role is no longer needed",
        request_id="fire-report",
    )
    assert fired["employment"] == "retired"
    assert fired["status"] == "retired"
    assert fired["files"] == "preserved"
    assert messaging.retirements == [report["employee_id"]]
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
            (report["employee_id"],),
        )
        == 0
    )
    assert (
        scalar(store, "SELECT state FROM activations WHERE activation_id='active-report'")
        == "running"
    )
    with pytest.raises(FirmError, match="active employee"):
        lifecycle.get_role(report["employee_id"], manager["employee_id"])

    lifecycle.fire(
        FOUNDER_ID, manager["employee_id"], reason="Flatten team", request_id="fire-manager"
    )
    replacement = lifecycle.hire(
        FOUNDER_ID,
        "engineering",
        "Lead the replacement team.",
        "heavy",
        request_id="replacement",
    )
    assert replacement["address"] == manager["address"]
    assert replacement["employee_id"] != manager["employee_id"]
    historical = lifecycle.get_role(FOUNDER_ID, manager["employee_id"])
    assert historical["employment"] == "retired"
    assert historical["accepts_mail"] is False
    assert "execution_profile" not in historical
    assert "session_id" not in historical


def test_public_role_read_requires_active_actor_and_hides_runtime_configuration(services):
    _, _, lifecycle, _ = services
    hired = lifecycle.hire(
        FOUNDER_ID, "research", "Explore options.", "light", request_id="research"
    )

    public = lifecycle.get_role(FOUNDER_ID, hired["employee_id"])
    assert public["mandate"]["body"] == "Explore options."
    assert public["manager_id"] == FOUNDER_ID
    assert "execution_profile" not in public
    assert "session_id" not in public
    assert "hold_reason" not in public
    with pytest.raises(FirmError, match="active employee"):
        lifecycle.get_role("unknown", hired["employee_id"])


def test_mandates_are_not_limited_by_the_paged_discovery_response(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, RecordingMessaging(store))

    lifecycle.initialize("acme", "M" * (70 * 1024), CONFIG)
    revised = lifecycle.revise_mandate(
        OWNER,
        FOUNDER_ID,
        0,
        "N" * (80 * 1024),
        reason="Large authoritative delegation",
        request_id="large-mandate",
    )
    assert revised["mandate"]["revision"] == 1
    assert revised["mandate"]["digest"] == hashlib.sha256(b"N" * (80 * 1024)).hexdigest()
    assert scalar(store, "SELECT COUNT(*) FROM mandates") == 2


def test_config_round_trip_uses_canonical_json(services):
    store, _, _, initialized = services
    with store.read() as db:
        stored = db.execute("SELECT config_json FROM firm").fetchone()[0]
    assert json.loads(stored) == initialized["firm"]["config"]


def test_real_messaging_notice_and_retirement_share_lifecycle_transactions(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: 100.0)
    messaging = Messaging(store)
    lifecycle = Lifecycle(store, messaging, id_factory=lambda: "employee-real")
    lifecycle.initialize("acme", "Build carefully.", CONFIG)
    hired = lifecycle.hire(FOUNDER_ID, "delivery", "Deliver the work.", "light", request_id="hire")
    sent = messaging.send(
        FOUNDER_ID,
        [hired["employee_id"]],
        "Please take the first task.",
        subject="First delivery work item",
        request_id="send",
    )

    lifecycle.revise_mandate(
        OWNER,
        FOUNDER_ID,
        0,
        "Build carefully and report progress.",
        reason="Clarify reporting",
        request_id="owner-revision",
    )
    with store.read() as db:
        notice = db.execute(
            "SELECT author_id,body FROM messages WHERE author_id=?", (OPERATIONS,)
        ).fetchone()
        notice_wake = db.execute(
            "SELECT priority,source_id FROM scheduling_opportunities "
            "WHERE employee_id=? AND source_id=?",
            (FOUNDER_ID, "m2"),
        ).fetchone()
    assert notice is not None
    assert "revision 1" in notice["body"]
    assert dict(notice_wake) == {"priority": CONTROL_PRIORITY, "source_id": "m2"}

    lifecycle.fire(
        FOUNDER_ID,
        hired["employee_id"],
        reason="Work was reassigned",
        request_id="fire",
    )
    with store.read() as db:
        disposition = db.execute(
            "SELECT disposition,failure_reason FROM message_deliveries "
            "WHERE message_id=? AND employee_id=?",
            (sent["message_id"], hired["employee_id"]),
        ).fetchone()
        retired_wakes = db.execute(
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
            (hired["employee_id"],),
        ).fetchone()[0]
        bounce = db.execute(
            "SELECT bounce_message_id FROM delivery_bounces "
            "WHERE original_message_id=? AND recipient_id=?",
            (sent["message_id"], hired["employee_id"]),
        ).fetchone()
    assert dict(disposition) == {
        "disposition": "undeliverable",
        "failure_reason": "recipient retired",
    }
    assert retired_wakes == 0
    assert bounce is not None


def test_short_employee_numbers_survive_restart_retirement_and_concurrent_hires(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, Messaging(store))
    lifecycle.initialize("test", "Build", CONFIG)
    first = lifecycle.hire(FOUNDER_ID, "analyst", "First employee", request_id="first")
    assert first["employee_id"] == "e1"
    lifecycle.fire(FOUNDER_ID, "e1", reason="Retire", request_id="retire")
    reopened = Store.scratch(store.path)
    lifecycle = Lifecycle(reopened, Messaging(reopened))
    replacement = lifecycle.hire(FOUNDER_ID, "analyst", "Replacement", request_id="replacement")
    assert replacement["employee_id"] == "e2"
    with reopened.read() as db:
        assert reopened.resolve(db, "e1")["employment"] == "retired"
        assert reopened.resolve(db, "founder/analyst")["employee_id"] == "e2"
    with pytest.raises(FirmError, match="retired"):
        lifecycle.revise_mandate(FOUNDER_ID, "e1", 0, "Stale delegation", request_id="stale")
    with pytest.raises(FirmError, match="unknown employee"):
        lifecycle.fire(FOUNDER_ID, "founder/analyst", reason="Unsafe address", request_id="unsafe")

    def hire(number):
        return lifecycle.hire(
            FOUNDER_ID, f"analyst-{number}", "Independent work", request_id=f"hire-{number}"
        )["employee_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(hire, range(12)))
    assert set(identities) == {f"e{number}" for number in range(3, 15)}


def test_owner_hiring_default_is_durable_and_does_not_follow_manager_profile(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, Messaging(store))
    lifecycle.initialize("test", "Build", {**CONFIG, "default_profile": "light"})
    manager = lifecycle.hire(FOUNDER_ID, "manager", "Manage", "heavy", request_id="manager")
    hired = lifecycle.hire(manager["employee_id"], "analyst", "Build", request_id="analyst")
    assert hired["execution_profile"] == "light"
    with store.transaction() as db:
        config = json.loads(store.firm(db)["config_json"])
        config["default_profile"] = "heavy"
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    assert lifecycle.hire(manager["employee_id"], "analyst", "Build", request_id="analyst") == hired
    next_hire = lifecycle.hire(manager["employee_id"], "another", "Build", request_id="another")
    assert next_hire["execution_profile"] == "heavy"
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?",
            (hired["employee_id"],),
        )
        == 1
    )


def test_provisioning_precedes_birth_and_failed_provisioning_can_retry(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, Messaging(store))
    lifecycle.initialize("test", "Build", CONFIG)
    observed = []

    def provision(employee):
        with store.read() as db:
            assert store.role(db, employee)["address"] == "founder/analyst"
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?", (employee,)
                ).fetchone()[0]
                == 0
            )
        observed.append(employee)
        if len(observed) == 1:
            raise FirmError("workspace preparation failed")

    lifecycle.provision_role = provision
    with pytest.raises(FirmError, match="workspace preparation"):
        lifecycle.hire(FOUNDER_ID, "analyst", "Build", request_id="analyst")
    assert scalar(store, "SELECT COUNT(*) FROM roles") == 1
    hired = lifecycle.hire(FOUNDER_ID, "analyst", "Build", request_id="analyst")
    assert lifecycle.hire(FOUNDER_ID, "analyst", "Build", request_id="analyst") == hired
    assert observed == ["e1", "e2"]
    assert (
        scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id='e2'") == 1
    )


def test_reserved_workspace_component_cannot_be_hired(services):
    _, _, lifecycle, _ = services
    with pytest.raises(FirmError, match="reserved"):
        lifecycle.hire(FOUNDER_ID, "work", "Build", request_id="work")


def test_employee_identity_reservation_survives_outer_rollback_and_different_hire(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, Messaging(store))
    lifecycle.initialize("test", "Build", CONFIG)
    with pytest.raises(RuntimeError, match="outer failure"):
        with store.transaction():
            first = lifecycle.hire("founder", "first", "First", request_id="first")
            assert first["employee_id"] == "e1"
            raise RuntimeError("outer failure")
    assert scalar(store, "SELECT COUNT(*) FROM roles") == 1
    reopened_store = Store.scratch(store.path)
    reopened = Lifecycle(reopened_store, Messaging(reopened_store))
    different = reopened.hire("founder", "different", "Different", request_id="different")
    assert different["employee_id"] == "e2"
    assert reopened.hire("founder", "different", "Different", request_id="different") == different
    assert reopened.hire("founder", "first", "First", request_id="first")["employee_id"] == "e3"
    sequence = store.path.with_name(store.path.name + ".employee-sequence")
    assert sequence.read_text() == "3\n"


def test_employee_sequence_keeps_committed_floor_and_rejects_corrupt_reservations(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    lifecycle = Lifecycle(store, Messaging(store))
    lifecycle.initialize("test", "Build", CONFIG)
    lifecycle.hire("founder", "first", "First", request_id="first")
    sequence = store.path.with_name(store.path.name + ".employee-sequence")
    sequence.write_text("0\n")
    assert lifecycle.hire("founder", "second", "Second", request_id="second")["employee_id"] == "e2"
    sequence.write_text("broken")
    with pytest.raises(FirmError, match="could not reserve"):
        lifecycle.hire("founder", "third", "Third", request_id="third")
    assert scalar(store, "SELECT COUNT(*) FROM roles") == 3
