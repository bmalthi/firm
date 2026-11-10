import json

import pytest
from runtime_test_support import (
    FakeNetwork,
    hire_running,
    mail_to,
    request_events,
    request_firm,
    rows,
)

from firm_runtime.analysis import AnalysisReader
from firm_runtime.errors import FirmError
from firm_runtime.hosts import valid_host
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store
from firm_runtime.workspaces import sandbox_role_id


@pytest.fixture
def clock():
    now = [1_900_000_000.0]
    tick = lambda: now[0]  # noqa: E731
    tick.now = now
    return tick


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("pypi.org", "pypi.org"),
        (" Files.PythonHosted.org ", "files.pythonhosted.org"),
        ("*.githubusercontent.com", "*.githubusercontent.com"),
        ("localhost", None),
        ("*.com", None),
        ("10.0.0.1", None),
        ("::1", None),
        ("example.com:443", None),
        ("https://example.com", None),
        ("example.com/path", None),
        ("a.*.example.com", None),
        ("-bad.example.com", None),
    ],
)
def test_hosts_are_lower_case_names_without_ports_or_addresses(host, expected):
    assert valid_host(host) == expected


def test_every_role_waiting_on_a_host_is_answered_by_one_grant(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    scout = hire_running(runtime, founder, "scout")
    first = runtime.tools(founder.token).request_access("network_host", "pypi.org", "A", "a")
    second = runtime.tools(scout.token).request_access("network_host", "pypi.org", "B", "b")
    assert (first["request_id"], second["request_id"]) == ("q1", "q2")
    assert runtime.requests.grant(OWNER, "q1")["answered"] == ["q1", "q2"]
    for role in (founder, scout):
        (mail,) = mail_to(runtime, role.employee_id)
        assert mail["subject"] == "Access granted: pypi.org"
        assert mail["scheduling_ready_at"] is not None


def test_one_denial_answers_every_role_waiting_on_a_host_it_covers(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    scout = hire_running(runtime, founder, "scout")
    runtime.tools(founder.token).request_access("network_host", "*.example.org", "A", "a")
    runtime.tools(scout.token).request_access("network_host", "www.example.org", "B", "b")
    runtime.tools(scout.token).request_access("network_host", "example.net", "C", "c")
    assert runtime.requests.deny(OWNER, "q1", "No mirrors.")["answered"] == ["q1", "q2"]
    assert rows(runtime, "SELECT request_id,status FROM owner_requests ORDER BY rowid") == [
        {"request_id": "q1", "status": "denied"},
        {"request_id": "q2", "status": "denied"},
        {"request_id": "q3", "status": "pending"},
    ]
    for role, host in ((founder, "*.example.org"), (scout, "www.example.org")):
        (mail,) = mail_to(runtime, role.employee_id)
        assert mail["subject"] == f"Access declined: {host}"
        assert "No mirrors." in mail["body"] and mail["scheduling_ready_at"] is not None


class PartialNetwork(FakeNetwork):
    """A sandbox that applies a write on one machine and then fails on another."""

    def __init__(self):
        super().__init__()
        self.fail_after_apply = False

    def set_allowed_hosts(self, hosts):
        result = super().set_allowed_hosts(hosts)
        if self.fail_after_apply:
            raise RuntimeError("Allowed hosts change incomplete: mac: not confirmed")
        return result


def test_a_half_applied_grant_never_counts_and_the_next_decision_removes_it(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    scout = hire_running(runtime, founder, "scout")
    net = runtime.hosts.sandbox = PartialNetwork()
    runtime.tools(founder.token).request_access("network_host", "pypi.org", "A", "a")
    net.fail_after_apply = True
    with pytest.raises(RuntimeError, match="incomplete"):
        runtime.requests.grant(OWNER, "q1")
    net.fail_after_apply = False
    assert net.granted == ["pypi.org"]  # applied on one machine, recorded nowhere
    assert rows(runtime, "SELECT * FROM network_hosts") == []
    # The sandbox's stray "granted" entry is not the owner's grant.
    asked = runtime.tools(scout.token).request_access("network_host", "pypi.org", "B", "b")
    assert asked["status"] == "pending" and asked["request_id"] == "q2"
    runtime.requests.deny(OWNER, "q1", "Not this one.")
    assert net.granted == []


def test_concurrent_owner_decisions_take_turns_on_the_write_lock(tmp_path, clock):
    import threading
    import time

    runtime, _ = request_firm(tmp_path, clock)
    runtime.hosts.allow(OWNER, "a.example.com")
    other = Runtime(Store.scratch(runtime.store.path, clock=clock))
    revoking = []

    class Observing(FakeNetwork):
        def set_allowed_hosts(self, hosts):
            if not revoking:
                # A second owner command starts while this write is in flight.
                thread = threading.Thread(
                    target=lambda: revoking.append(other.hosts.revoke(OWNER, "a.example.com"))
                )
                revoking.append(thread)
                thread.start()
                time.sleep(0.3)
                assert len(revoking) == 1  # still waiting for the lock
            return super().set_allowed_hosts(hosts)

    net = Observing()
    runtime.hosts.sandbox = other.hosts.sandbox = net
    runtime.hosts.allow(OWNER, "b.example.com")
    revoking[0].join(timeout=10)
    assert net.writes == [["a.example.com", "b.example.com"], ["b.example.com"]]
    assert [row["host"] for row in rows(runtime, "SELECT host FROM network_hosts")] == [
        "b.example.com"
    ]


def test_a_denied_host_and_a_denied_wildcard_refuse_what_they_cover(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "*.example.org", "Mirror", "wild")
    tools.request_access("network_host", "paste.example.net", "Share", "exact")
    runtime.requests.deny(OWNER, "q1", "No mirrors.")
    runtime.requests.deny(OWNER, "q2", "No paste sites.")
    for host, note in (
        ("www.example.org", "No mirrors."),
        ("*.cdn.example.org", "No mirrors."),
        ("paste.example.net", "No paste sites."),
    ):
        answer = tools.request_access("network_host", host, "Again", f"again-{host}")
        answer.pop("mail_waiting", None)
        assert answer == {"status": "refused", "reason": "access_denied", "resource": host,
                          "owner_note": note}
    other = tools.request_access("network_host", "www.example.net", "Different", "other")
    assert other["status"] == "pending"


def test_a_request_under_a_wildcard_grant_leaves_inbox_and_readiness(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("network_host", "api.example.org", "Docs", "a")
    runtime.finish(founder.activation_id)
    reader = AnalysisReader(runtime.store.path, clock=clock)
    assert reader.read("inbox")["pending_request_count"] == 1
    runtime.hosts.allow(OWNER, "*.example.org")
    inbox = reader.read("inbox")
    assert inbox["pending_requests"] == [] and inbox["needs_owner_count"] == 0
    (row,) = reader.read("readiness")["rows"]
    assert "owner_request_pending" not in row["reasons"]
    (mail,) = mail_to(runtime, founder.employee_id)
    assert mail["subject"] == "Access granted: api.example.org"


def test_an_allowed_host_is_granted_at_once_without_asking(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock, baseline=["*.github.com", "pypi.org"])
    tools = runtime.tools(founder.token)
    for host in ("pypi.org", "api.github.com", "*.objects.github.com"):
        assert tools.request_access("network_host", host, "Fetch", f"ask-{host}") == {
            "status": "granted", "resource": host
        }
    assert rows(runtime, "SELECT * FROM owner_requests") == []
    assert runtime.posted == []


def test_host_outside_baseline_uses_normal_owner_request(tmp_path, clock):
    from firm_sandbox.egress_proxy import BASELINE_HOSTS

    runtime, founder = request_firm(tmp_path, clock, baseline=BASELINE_HOSTS)
    host = "storage.googleapis.com"
    answer = runtime.tools(founder.token).request_access(
        "network_host", host, "Fetch public test data", "archive-host")
    assert answer["status"] == "pending"
    assert runtime.hosts.sandbox.granted == []
    runtime.requests.grant(OWNER, answer["request_id"])
    assert runtime.hosts.sandbox.granted == [host]


def test_observe_mode_grants_hosts_without_requests_or_persistent_grants(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.hosts.sandbox.mode = "observe"
    tools = runtime.tools(founder.token)
    for host in ("research.example.org", "*.data.example.org"):
        assert tools.request_access("network_host", host, "Research", f"open-{host}") == {
            "status": "granted", "resource": host,
        }
    assert rows(runtime, "SELECT * FROM owner_requests") == []
    assert rows(runtime, "SELECT * FROM network_hosts") == []
    assert runtime.posted == []
    runtime.hosts.sandbox.mode = "enforce"
    assert tools.request_access(
        "network_host", "research.example.org", "Research", "restricted"
    )["status"] == "pending"


def test_unavailable_network_policy_does_not_assume_open_access(tmp_path, clock, monkeypatch):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.hosts.sandbox.mode = "observe"

    def unavailable():
        raise RuntimeError("network status unavailable")

    monkeypatch.setattr(runtime.hosts.sandbox, "network_status", unavailable)
    assert runtime.tools(founder.token).request_access(
        "network_host", "research.example.org", "Research", "ask"
    )["status"] == "pending"


@pytest.mark.parametrize("mode", ["enforce", "observe"])
@pytest.mark.parametrize("host", ["10.0.0.1", "pypi.org:443", "*.org"])
def test_hosts_outside_the_vocabulary_are_refused(tmp_path, clock, host, mode):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.hosts.sandbox.mode = mode
    result = runtime.tools(founder.token).request_access("network_host", host, "Fetch", "ask")
    assert result == {"status": "refused", "reason": "access_resource_invalid"}
    assert rows(runtime, "SELECT * FROM owner_requests") == []


def test_a_grant_writes_the_sandbox_records_and_wakes_the_role(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "pypi.org", "Install build tools", "ask")
    clock.now[0] += 60
    result = runtime.requests.grant(OWNER, "q1")
    assert result == {"resource": "pypi.org", "status": "granted", "answered": ["q1"],
                      "granted_hosts": ["pypi.org"]}
    assert runtime.hosts.sandbox.writes == [["pypi.org"]]
    (request,) = rows(runtime, "SELECT status,decided_at FROM owner_requests")
    assert request == {"status": "granted", "decided_at": clock.now[0]}
    assert rows(runtime, "SELECT host,source FROM network_hosts") == [
        {"host": "pypi.org", "source": "request"}
    ]
    assert request_events(runtime)[-1] == (
        "request.granted", {"request_id": "q1", "kind": "network_host", "resource": "pypi.org"}
    )
    (mail,) = mail_to(runtime, founder.employee_id)
    assert mail["author_id"] == "runtime" and mail["subject"] == "Access granted: pypi.org"
    assert mail["scheduling_ready_at"] is not None  # a wake, not just a letter
    again = tools.request_access("network_host", "pypi.org", "Again", "ask-again")
    assert again.pop("mail_waiting")  # the answer's mail is presented with the next call
    assert again == {"status": "granted", "resource": "pypi.org"}
    with pytest.raises(FirmError, match="already granted"):
        runtime.requests.grant(OWNER, "q1")
    with pytest.raises(FirmError, match="only the owner"):
        runtime.requests.grant(founder.employee_id, "q1")


@pytest.mark.parametrize("fail", [True, "applied"])
def test_a_grant_the_sandbox_refuses_records_nothing(tmp_path, clock, fail):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("network_host", "pypi.org", "Install", "ask")
    runtime.hosts.sandbox.fail = fail
    with pytest.raises(RuntimeError, match="Allowed hosts change incomplete"):
        runtime.requests.grant(OWNER, "q1")
    assert rows(runtime, "SELECT status FROM owner_requests") == [{"status": "pending"}]
    assert rows(runtime, "SELECT * FROM network_hosts") == []
    assert mail_to(runtime, founder.employee_id) == []


def test_a_denial_mails_the_reason_and_refuses_the_host_afterwards(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "pastebin.com", "Share logs", "ask")
    with pytest.raises(FirmError, match="reason"):
        runtime.requests.deny(OWNER, "q1", "")
    assert runtime.requests.deny(OWNER, "q1", "Use the firm drive.") == {
        "request_id": "q1", "resource": "pastebin.com", "status": "denied", "answered": ["q1"]
    }
    (request,) = rows(runtime, "SELECT status,decision_note FROM owner_requests")
    assert request == {"status": "denied", "decision_note": "Use the firm drive."}
    (mail,) = mail_to(runtime, founder.employee_id)
    assert mail["subject"] == "Access declined: pastebin.com"
    assert "Use the firm drive." in mail["body"] and mail["scheduling_ready_at"] is not None
    assert request_events(runtime)[-1][0] == "request.denied"
    assert runtime.hosts.sandbox.writes == [[]]  # a decision rewrites the record
    again = tools.request_access("network_host", "pastebin.com", "Please", "ask-again")
    again.pop("mail_waiting")
    assert again == {
        "status": "refused", "reason": "access_denied", "resource": "pastebin.com",
        "owner_note": "Use the firm drive.",
    }
    # Allowing the host later lifts the denial.
    runtime.hosts.allow(OWNER, "pastebin.com")
    assert tools.request_access("network_host", "pastebin.com", "Now", "ask-now")["status"] == (
        "granted"
    )


def test_allow_answers_covered_requests_and_revoke_withdraws_them(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    tools = runtime.tools(founder.token)
    tools.request_access("network_host", "api.example.com", "Docs", "ask-api")
    tools.request_access("network_host", "other.org", "Elsewhere", "ask-other")
    allowed = runtime.hosts.allow(OWNER, "*.example.com")
    assert allowed["answered"] == ["q1"]
    assert rows(runtime, "SELECT request_id,status FROM owner_requests ORDER BY rowid") == [
        {"request_id": "q1", "status": "granted"},
        {"request_id": "q2", "status": "pending"},
    ]
    assert rows(runtime, "SELECT source FROM network_hosts") == [{"source": "owner"}]
    with pytest.raises(FirmError, match="not a granted host"):
        runtime.hosts.revoke(OWNER, "other.org")
    runtime.hosts.allow(OWNER, "api.example.com")
    revoked = runtime.hosts.revoke(OWNER, "api.example.com")
    assert revoked["withdrawn"] == ["q1"] and revoked["granted_hosts"] == ["*.example.com"]
    assert runtime.hosts.sandbox.writes[-1] == ["*.example.com"]
    assert request_events(runtime)[-1] == (
        "request.withdrawn",
        {"request_id": "q1", "kind": "network_host", "resource": "api.example.com"},
    )
    listing = runtime.requests.listing(OWNER)
    assert [item["request_id"] for item in listing["pending"]] == ["q2"]
    assert listing["pending"][0]["address"] == "founder"
    assert listing["recent"][0] == {**listing["recent"][0], "request_id": "q1",
                                    "status": "withdrawn"}


def test_network_section_groups_traffic_by_role_and_carries_its_caveats(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock, baseline=["pypi.org"])
    runtime.hosts.allow(OWNER, "npmjs.org")
    net = runtime.hosts.sandbox
    role = sandbox_role_id(founder.employee_id)
    net.egress = {
        "live": False,
        "overflow": 3,
        "entries": [
            {"role": role, "host": "pypi.org", "requests": 4, "denied": 0, "bytes_up": 10,
             "bytes_down": 900, "last_seen": 1.0},
            {"role": role, "host": "evil.example", "requests": 2, "denied": 2, "bytes_up": 0,
             "bytes_down": 0, "last_seen": 2.0},
            {"role": "s-summary", "host": "api.anthropic.com", "requests": 1, "denied": 0,
             "bytes_up": 5, "bytes_down": 5, "last_seen": 3.0},
        ],
    }
    page = AnalysisReader(
        runtime.store.path, clock=clock, network_status=net.network_status
    ).read("network")
    assert page["live"] is True and page["mode"] == "enforce" and page["enabled"] is True
    assert {"host": "npmjs.org", "source": "granted"} in page["allowed_hosts"]
    assert page["egress"] == {"live": False, "overflow": 3}
    founder_row = next(row for row in page["rows"] if row["employee_id"] == "founder")
    assert (founder_row["requests"], founder_row["denied"]) == (6, 2)
    assert [host["host"] for host in founder_row["hosts"]] == ["pypi.org", "evil.example"]
    assert page["role_labels"]["founder"]["address"] == "founder"
    assert any(row["sandbox_role"] == "s-summary" for row in page["rows"])
    limits = " ".join(page["coverage"]["limits"])
    assert "proxy is not running" in limits and "3 role and host pairs" in limits

    offline = AnalysisReader(runtime.store.path, clock=clock).read("network")
    assert offline["live"] is False and offline["rows"] == []
    assert offline["allowed_hosts"] == [{"host": "npmjs.org", "source": "granted"}]
    assert "could not be read" in " ".join(offline["coverage"]["limits"])


def test_a_host_request_records_the_shared_payload(tmp_path, clock):
    runtime, founder = request_firm(tmp_path, clock)
    runtime.tools(founder.token).request_access("network_host", "PyPI.org", " Install ", "ask")
    (request,) = rows(runtime, "SELECT payload_json FROM owner_requests")
    assert json.loads(request["payload_json"]) == {
        "resource": "pypi.org", "purpose": "Install", "cause": "network_host_not_allowed",
    }
