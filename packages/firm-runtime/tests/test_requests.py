import json

import pytest
from runtime_test_support import (
    hire_running,
    mail_to,
    request_events,
    request_firm,
    rows,
)

from firm_runtime.analysis import AnalysisReader
from firm_runtime.errors import FirmError
from firm_runtime.requests import MAX_REASON, AskOwner, record_github
from firm_runtime.store import OWNER


@pytest.fixture
def clock():
    now = [1_900_000_000.0]
    tick = lambda: now[0]  # noqa: E731
    tick.now = now
    return tick


class Thing:
    """A request kind of the shared layer's own, with nothing to enforce."""

    name = "thing"

    def __init__(self, runtime):
        self.runtime = runtime

    def ask(self, actor, resource, purpose):
        return AskOwner(resource, "thing_needs_owner", {"size": 3})

    def _decide(self, row, status, note=None):
        with self.runtime.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            return {"answered": self.runtime.requests.answer(db, [row], status, note)}

    def grant(self, row):
        return self._decide(row, "granted")

    def deny(self, row, note):
        return self._decide(row, "denied", note)


def thing_firm(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.requests.kinds["thing"] = Thing(runtime)
    return runtime, founder


def test_automatic_and_unknown_request_attribution(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    with runtime.store.transaction() as db:
        record_github(
            db,
            clock(),
            {"employee_id": founder.employee_id, "activation_id": founder.activation_id},
            "example/repo",
            "Build",
        )
    assert runtime.requests.status(OWNER, "q1")["decided_by"] == "runtime"
    runtime.tools(founder.token).request_access("thing", "widget", "Build it", "ask")
    runtime.requests.grant(OWNER, "q2")  # This test kind supplies no attribution.
    assert runtime.requests.status(OWNER, "q2")["decided_by"] is None
    (notice,) = rows(runtime, "SELECT regarding_kind,regarding_id FROM messages")
    assert (notice["regarding_kind"], notice["regarding_id"]) == ("request", "q2")


def test_a_request_opens_once_per_resource_and_replays(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    first = tools.request_access("network_host", "pypi.org", "Install build tools", "ask-pypi")
    assert first["status"] == "pending" and first["request_id"] == "q1"
    assert tools.request_access(
        "network_host", "pypi.org", "Install build tools", "ask-pypi"
    ) == first
    assert tools.request_access("network_host", "PyPI.org", "Packages again", "ask-again") == first
    (request,) = rows(runtime, "SELECT * FROM owner_requests")
    assert request["status"] == "pending" and request["employee_id"] == founder.employee_id
    assert request_events(runtime) == [
        ("request.opened", {"request_id": "q1", "kind": "network_host", "resource": "pypi.org",
                            "purpose": "Install build tools"}),
    ]
    (posted,) = runtime.posted
    assert posted[:2] == ["osascript", "-e"]
    assert "pypi.org: Install build tools" in posted[2] and "founder asks" in posted[2]
    with pytest.raises(FirmError, match="purpose"):
        tools.request_access("network_host", "npmjs.org", " ", "no-purpose")


def test_an_unknown_kind_is_refused_without_a_request(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    result = runtime.tools(founder.token).request_access("credential", "pypi.org", "Fetch", "ask")
    assert result == {"status": "refused", "reason": "access_kind_unknown"}
    assert rows(runtime, "SELECT * FROM owner_requests") == []


def test_a_pending_answer_carries_a_cause_and_never_a_reason(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    scout = hire_running(runtime, founder, "scout")
    answer = runtime.tools(founder.token).request_access("thing", "widget", "Build it", "ask")
    assert set(answer) == {"status", "request_id", "cause", "message"}
    assert answer["status"] == "pending" and answer["cause"] == "thing_needs_owner"
    assert "reason" not in answer and answer["message"]
    (request,) = rows(runtime, "SELECT payload_json FROM owner_requests")
    assert json.loads(request["payload_json"]) == {
        "resource": "widget", "purpose": "Build it", "cause": "thing_needs_owner", "size": 3,
    }
    # Asking again returns the open request, with its cause.
    again = runtime.tools(founder.token).request_access("thing", "widget", "Again", "ask-again")
    assert again == answer
    # Another role waits on its own request for the same resource.
    other = runtime.tools(scout.token).request_access("thing", "widget", "Me too", "ask")
    assert other["request_id"] == "q2"


def test_a_denial_stands_for_the_same_kind_and_resource(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("thing", "widget", "Build it", "ask")
    runtime.requests.deny(OWNER, "q1", "Not this one.")
    again = tools.request_access("thing", "widget", "Please", "ask-again")
    again.pop("mail_waiting", None)
    assert again == {"status": "refused", "reason": "access_denied", "resource": "widget",
                     "owner_note": "Not this one."}
    assert len(rows(runtime, "SELECT * FROM owner_requests")) == 1
    # Only an exact match stands: another resource is a new question.
    other = tools.request_access("thing", "gadget", "Build that", "ask-other")
    assert other["status"] == "pending"


def test_denial_validates_the_trimmed_character_limit(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("thing", "widget", "Build it", "ask")
    note = "🧭" * MAX_REASON
    runtime.requests.deny(OWNER, "q1", "  " + note + "  ")
    assert runtime.requests.status(OWNER, "q1")["decision_note"] == note


@pytest.mark.parametrize("reason", [None, False, [], " \n", "x" * (MAX_REASON + 1)])
def test_invalid_denial_leaves_the_request_pending(tmp_path, clock, reason):
    runtime, founder = thing_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("thing", "widget", "Build it", "ask")
    with pytest.raises(FirmError, match="reason must contain"):
        runtime.requests.deny(OWNER, "q1", reason)
    assert runtime.requests.status(OWNER, "q1")["status"] == "pending"


def test_an_answer_decides_the_request_mails_the_role_and_wakes_it(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    scout = hire_running(runtime, founder, "scout")
    runtime.tools(founder.token).request_access("thing", "widget", "Build it", "a")
    runtime.tools(scout.token).request_access("thing", "gadget", "Build that", "b")
    clock.now[0] += 30
    assert runtime.requests.grant(OWNER, "q1") == {"answered": ["q1"]}
    assert runtime.requests.deny(OWNER, "q2", "Too big.") == {"answered": ["q2"]}
    assert rows(
        runtime, "SELECT request_id,status,decided_at,decision_note FROM owner_requests"
    ) == [
        {"request_id": "q1", "status": "granted", "decided_at": clock.now[0],
         "decision_note": None},
        {"request_id": "q2", "status": "denied", "decided_at": clock.now[0],
         "decision_note": "Too big."},
    ]
    (granted,) = mail_to(runtime, founder.employee_id)
    assert granted["author_id"] == "runtime" and granted["subject"] == "Access granted: widget"
    assert granted["scheduling_ready_at"] is not None
    (declined,) = mail_to(runtime, scout.employee_id)
    assert declined["subject"] == "Access declined: gadget" and "Too big." in declined["body"]
    assert declined["scheduling_ready_at"] is not None
    assert [event for event in request_events(runtime) if event[0] != "request.opened"] == [
        ("request.granted", {"request_id": "q1", "kind": "thing", "resource": "widget"}),
        ("request.denied", {"request_id": "q2", "kind": "thing", "resource": "gadget"}),
    ]
    with pytest.raises(FirmError, match="already granted"):
        runtime.requests.grant(OWNER, "q1")
    with pytest.raises(FirmError, match="unknown request"):
        runtime.requests.grant(OWNER, "q9")
    with pytest.raises(FirmError, match="only the owner"):
        runtime.requests.deny(founder.employee_id, "q2", "No.")


def test_the_listing_is_one_list_for_every_kind(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("thing", "widget", "Build it", "a")
    tools.request_access("network_host", "pypi.org", "Install", "b")
    tools.request_access("thing", "gadget", "Build that", "c")
    runtime.requests.grant(OWNER, "q2")
    listing = runtime.requests.listing(OWNER)
    assert set(listing) == {"pending", "recent"}
    assert [(item["kind"], item["resource"], item["cause"]) for item in listing["pending"]] == [
        ("thing", "widget", "thing_needs_owner"),
        ("thing", "gadget", "thing_needs_owner"),
    ]
    assert listing["pending"][0]["size"] == 3 and listing["pending"][0]["address"] == "founder"
    (recent,) = listing["recent"]
    assert (recent["kind"], recent["resource"], recent["status"]) == (
        "network_host", "pypi.org", "granted"
    )
    assert recent["decided_by"] == "owner"
    assert runtime.requests.status(OWNER, "q2")["decided_by"] == "owner"
    with pytest.raises(FirmError, match="only the owner"):
        runtime.requests.listing(founder.employee_id)


def test_a_retired_role_asks_for_nothing(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    hired = runtime.lifecycle.hire(
        founder.employee_id, "scout", "Scout", "default", request_id="hire-scout"
    )
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    runtime.tools(invocation.token).request_access("network_host", "pypi.org", "Look", "ask")
    runtime.finish(invocation.activation_id)
    runtime.lifecycle.fire(
        founder.employee_id, hired["employee_id"], reason="Done", request_id="fire-scout"
    )
    assert rows(runtime, "SELECT status FROM owner_requests") == [{"status": "withdrawn"}]
    assert rows(runtime, "SELECT decided_by FROM owner_requests") == [{"decided_by": "runtime"}]


def test_the_owner_is_nudged_about_mail_at_most_once_per_sender_per_ten_minutes(
    tmp_path, clock
):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    for index in range(2):
        tools.send(["owner"], f"Report {index}", f"report-{index}", subject=f"Report {index}")
    (posted,) = runtime.posted
    assert "Report 0" in posted[2] and "Mail from founder" in posted[2]
    clock.now[0] += 601
    tools.send(["owner"], "Report 2", "report-2", subject="Report 2")
    assert len(runtime.posted) == 2


def test_the_owner_can_turn_notifications_off(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    with runtime.store.transaction() as db:
        config = json.loads(runtime.store.firm(db)["config_json"])
        config["owner"] = {"notifications": False}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    runtime.tools(founder.token).send(["owner"], "Report", "report", subject="Report")
    assert runtime.posted == []


def test_readiness_names_the_owner_as_what_a_waiting_role_needs(tmp_path, clock):
    runtime, founder = thing_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("thing", "widget", "Build it", "ask")
    runtime.finish(founder.activation_id)
    reader = AnalysisReader(runtime.store.path, clock=clock)
    (row,) = reader.read("readiness")["rows"]
    assert row["reasons"] == ["owner_request_pending"]
    assert row["activity"] == "blocked" and row["actionable_by"] == "owner_or_manager"
    (asked,) = row["evidence"][0]["requests"]
    assert (asked["kind"], asked["resource"], asked["cause"]) == (
        "thing", "widget", "thing_needs_owner"
    )
    runtime.requests.grant(OWNER, "q1")
    (row,) = reader.read("readiness")["rows"]
    assert "owner_request_pending" not in row["reasons"]


def test_inbox_lists_pending_requests_beside_owner_mail(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "pypi.org", "Install", "ask")
    tools.send(["owner"], "Status", "status", subject="Status")
    clock.now[0] += 90
    inbox = AnalysisReader(runtime.store.path, clock=clock).read("inbox")
    assert inbox["pending_count"] == 1 and inbox["pending_request_count"] == 1
    assert inbox["needs_owner_count"] == 2
    (request,) = inbox["pending_requests"]
    assert {key: request[key] for key in ("request_id", "kind", "address", "resource", "purpose",
                                          "cause", "age_seconds")} == {
        "request_id": "q1", "kind": "network_host", "address": "founder",
        "resource": "pypi.org", "purpose": "Install", "cause": "network_host_not_allowed",
        "age_seconds": 90.0,
    }
