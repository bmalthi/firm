import json
import re
import stat
import time

import pytest
from starlette.testclient import TestClient

from firm_runtime.analysis import AnalysisReader
from firm_runtime.console import build_console_app
from firm_runtime.errors import FirmError
from firm_runtime.messaging import MAX_BODY_BYTES
from firm_runtime.requests import MAX_REASON
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store

HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"


def _system(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )["founder"]["employee_id"]
    analyst = runtime.lifecycle.hire(
        founder, "analyst", "Own analyst tasks.", "default", request_id="hire-analyst"
    )["employee_id"]
    app = build_console_app(runtime, allowed_host=HOST)
    return runtime, founder, analyst, app


def _login(app):
    client = TestClient(app, base_url=ORIGIN)
    response = client.get(f"/unlock/{app.state.console_key}", follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/session").status_code == 200
    return client


def test_console_authentication_and_local_origin_boundary(tmp_path):
    _, _, _, app = _system(tmp_path)
    with TestClient(app, base_url=ORIGIN) as anonymous:
        for path in (
            "/", "/api/snapshot", "/api/messages", "/api/inbox", "/api/incidents",
            "/api/results", "/api/roles/tree",
        ):
            assert anonymous.get(path).status_code == 401
        assert anonymous.get("/unlock/wrong").status_code == 404
        assert anonymous.get("/unlock/%C3%A9").status_code == 404
    with _login(app) as client:
        # The unlock link is reusable: the console's address is stable and bookmarkable.
        assert client.get(f"/unlock/{app.state.console_key}").status_code == 200
        assert client.get("/", headers={"Host": "rebound.test"}).status_code == 421
        assert (
            client.get("/api/snapshot", headers={"Origin": "http://evil.test"}).status_code == 403
        )
        response = client.get("/")
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
        assert response.headers["cache-control"] == "no-store"


def test_console_serves_every_module_its_pages_import(tmp_path):
    _, _, _, app = _system(tmp_path)
    with _login(app) as client:
        page = client.get("/").text
        imported = set(re.findall(r'(?:src|href)="/([\w.]+)"', page))
        for name in ("app.js", "analysis.js", "styles.css"):
            assert name in imported
        for module in ("/app.js", "/analysis.js"):
            imported.update(re.findall(r'from "\./([\w.]+)"', client.get(module).text))
        assert "format.js" in imported
        for name in sorted(imported):
            response = client.get(f"/{name}")
            assert response.status_code == 200, name


def test_console_resources_route_carries_current_per_machine_disk_state(tmp_path):
    """System health's disk line is fed by this same, already-polled route."""
    from firm_runtime import analysis_history

    runtime, _, _, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        for machine in ("mac", "mac-1", "mac-2"):
            analysis_history.record_resources(
                db,
                time.time(),
                machine,
                {"disk_used_bytes": 55 * (1 << 30), "disk_total_bytes": 60 * (1 << 30)},
            )
    with _login(app) as client:
        page = client.get("/api/analysis/resources?row_type=role&from=0&limit=200").json()
    assert set(page["disk"]) == {"mac"}
    disk = page["disk"]["mac"]
    assert disk["low_disk"] is True
    assert disk["free_bytes"] == 5 * (1 << 30)
    assert disk["stale"] is False and disk["unknown"] is False


def test_console_key_is_durable_owner_readable_and_survives_a_restart(tmp_path):
    runtime, _, _, app = _system(tmp_path)
    key_file = tmp_path / "console-key"
    assert key_file.read_text() == app.state.console_key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    restarted = build_console_app(runtime, allowed_host=HOST)
    assert restarted.state.console_key == app.state.console_key
    with _login(restarted) as client:
        assert client.get("/api/session").status_code == 200


@pytest.mark.parametrize("contents", ["", "guessable", "\u00e9" * 43])
def test_damaged_console_key_cannot_start_an_unprotected_console(tmp_path, contents):
    runtime, _, _, _ = _system(tmp_path)
    (tmp_path / "console-key").write_text(contents)
    with pytest.raises(FirmError, match="console key"):
        build_console_app(runtime, allowed_host=HOST)


def test_console_refuses_a_public_or_linked_key(tmp_path):
    runtime, _, _, _ = _system(tmp_path)
    path = tmp_path / "console-key"
    path.chmod(0o644)
    with pytest.raises(FirmError, match="not private"):
        build_console_app(runtime, allowed_host=HOST)
    path.chmod(0o600)
    target = path.with_name("other-key")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(FirmError, match="console key"):
        build_console_app(runtime, allowed_host=HOST)


@pytest.mark.parametrize(
    "path",
    [
        "/api/messages?scope=invalid",
        "/api/control/start",
        "/api/control/drain",
        "/api/control/hard-stop",
        "/api/mandate",
        "/api/accounts/incidents/clear",
        "/api/roles/stop",
        "/api/roles/resume",
        "/api/roles/route",
        "/api/roles/retry",
    ],
)
def test_console_rejects_unavailable_owner_routes(tmp_path, path):
    runtime, _, analyst, app = _system(tmp_path)
    with runtime.store.read() as db:
        before = list(db.iterdump())
    with _login(app) as client:
        response = client.post(
            path,
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "X-Firm-Console": "same-origin",
            },
            json={
                "employee_id": analyst,
                "recipients": [analyst],
                "body": "Do work",
                "request_id": "console-mutation",
                "expected_revision": 0,
                "mandate": "Changed",
            },
        )
        assert response.status_code in {404, 405}
        snapshot = client.get("/api/snapshot").json()
        assert snapshot["firm"]["state"] == "stopped"
        assert snapshot["mac"] == {"configured": False, "enrolled": False, "stations": []}
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def _post(client, path, value):
    return client.post(
        path,
        headers={
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-Firm-Console": "same-origin",
        },
        json=value,
    )


def test_owner_inbox_filters_before_paging_and_dismissal_replays(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    ids = []
    for i in range(5):
        ids.append(
            runtime.messaging.send(founder, [OWNER], f"Report {i}", request_id=f"owner-{i}")[
                "message_id"
            ]
        )
        runtime.messaging.send(founder, [analyst], "Internal", request_id=f"internal-{i}")
    with _login(app) as client:
        assert client.get("/api/snapshot").json()["needs_you"]["owner_mail"] == 5
        payload = {"operation_id": "dismiss", "message_ids": [ids[1], ids[3]]}
        result = _post(client, "/api/messages/dismiss", payload)
        assert result.json() == {"status": "dismissed", "message_ids": payload["message_ids"]}
        assert _post(client, "/api/messages/dismiss", payload).json() == result.json()
        assert (
            _post(client, "/api/messages/dismiss", {**payload, "operation_id": "new"}).status_code
            == 400
        )
        assert (
            _post(client, "/api/messages/dismiss", {**payload, "message_ids": [ids[0]]}).status_code
            == 400
        )
        first = client.get("/api/inbox?view=new&limit=2").json()
        assert [m["message_id"] for m in first["messages"]] == [ids[4], ids[2]]
        assert first["messages"][0]["owner_delivery"] == {
            "disposition": "pending",
            "disposition_at": None,
        }
        assert first["messages"][0]["regarding"] is None
        last = client.get("/api/inbox", params={"before": first["next_cursor"], "limit": 2}).json()
        assert [m["message_id"] for m in last["messages"]] == [ids[0]]
        assert last["next_cursor"] is None
        done = client.get("/api/inbox?view=done&limit=1").json()
        assert [m["message_id"] for m in done["messages"]] == [ids[3]]
        assert done["messages"][0]["owner_delivery"]["disposition_at"] is not None
        assert client.get("/api/snapshot").json()["needs_you"]["owner_mail"] == 3
        assert len(client.get("/api/inbox?view=all").json()["messages"]) == 5
        assert len(client.get("/api/messages").json()["messages"]) == 10
        reply = _post(
            client,
            "/api/messages/reply",
            {
                "operation_id": "answer",
                "message_id": ids[0],
                "body": "Thanks",
            },
        )
        assert reply.status_code == 200
        assert client.get("/api/snapshot").json()["needs_you"]["owner_mail"] == 2


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/messages/dismiss", {"message_ids": ["m1"]}),
        ("/api/incidents/clear", {"incident_id": "i1"}),
    ],
)
def test_new_console_mutations_use_all_existing_guards(tmp_path, path, payload):
    _, _, _, app = _system(tmp_path)
    payload = {**payload, "operation_id": "operation"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert _post(client, path, payload).status_code == 401
    with _login(app) as client:
        assert client.post(path, json=payload).status_code == 403
        assert _post(client, path, {**payload, "operation_id": "bad key"}).status_code == 400
        assert _post(client, path, {**payload, "padding": "x" * 20000}).status_code == 413
        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-Firm-Console": "same-origin",
        }
        assert client.post(path, headers=headers, content="{}").status_code == 415
        assert (
            client.post(
                path, headers={**headers, "Content-Type": "application/json"}, content="{"
            ).status_code
            == 400
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/inbox?view=invalid",
        "/api/inbox?limit=0",
        "/api/inbox?before=no",
        "/api/inbox?before=0",
        "/api/incidents?recent=-1",
        "/api/incidents?recent=101",
        "/api/incidents?recent=no",
        "/api/results?limit=0",
        "/api/results?limit=201",
        "/api/results?limit=no",
    ],
)
def test_new_console_queries_validate_parameters(tmp_path, path):
    _, _, _, app = _system(tmp_path)
    with _login(app) as client:
        assert client.get(path).status_code == 400


def test_between_role_mail_filters_owner_deliveries_before_paging(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    expected = [
        runtime.messaging.send(
            analyst, [founder], f"Between roles {index}", request_id=f"between-{index}"
        )["message_id"]
        for index in range(55)
    ]
    dismissed = [
        runtime.messaging.send(
            analyst, [OWNER], f"Dismissed {index}", request_id=f"dismissed-{index}"
        )["message_id"]
        for index in range(210)
    ]
    for offset in range(0, len(dismissed), 100):
        runtime.messaging.dismiss(
            OWNER, dismissed[offset:offset + 100], request_id=f"done-{offset}"
        )
    pending = runtime.messaging.send(analyst, [OWNER], "Still new", request_id="pending")
    with _login(app) as client:
        # The unfiltered feed really is buried under more than the UI's 200-message cap.
        assert all(
            message["message_id"] in dismissed + [pending["message_id"]]
            for message in client.get("/api/messages?limit=100").json()["messages"]
        )
        first = client.get("/api/messages?scope=between_roles&limit=50").json()
        assert [m["message_id"] for m in first["messages"]] == list(reversed(expected))[:50]
        assert first["next_cursor"] is not None
        second = client.get(
            "/api/messages", params={
                "scope": "between_roles", "limit": 50, "before": first["next_cursor"],
            },
        ).json()
        assert [m["message_id"] for m in second["messages"]] == list(reversed(expected))[50:]
        assert second["next_cursor"] is None
        assert [m["message_id"] for m in client.get("/api/inbox?view=new").json()["messages"]] == [
            pending["message_id"]
        ]


def test_console_role_mandate_uses_discovery_contract(tmp_path):
    runtime, _, analyst, app = _system(tmp_path)
    expected = runtime.discovery.get_role(OWNER, employee_id=analyst, section="mandate")
    with _login(app) as client:
        result = client.get(f"/api/roles/{analyst}?section=mandate")
        assert result.status_code == 200
        value = result.json()
        assert value.pop("owner_facts")["execution_profile"] == "default"
        assert value == expected
        assert client.get(f"/api/roles/{analyst}?section=profile").status_code == 200
        assert client.get(f"/api/roles/{analyst}?section=all").status_code == 400


def test_incident_listing_uses_readiness_and_hydrates_immutable_notices(tmp_path, monkeypatch):
    runtime, founder, analyst, app = _system(tmp_path)
    now = [1000.0]
    runtime.store.clock = lambda: now[0]
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["accounts"] = {
            name: {"provider": "codex", "max_active": 4, "auth_file": "/missing"}
            for name in ("a", "b")
        }
        config["profiles"]["default"] = {"provider": "codex", "accounts": ["a", "b"]}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
        incident = runtime.accounts.hold(db, "account", "a", "auth")
        # Founder is pinned; the analyst can use b, despite a stale notification record.
        runtime.accounts._pin_role(db, runtime.store.role(db, founder), incident)
        runtime.accounts._notify_block(
            db, runtime.store.role(db, analyst), {"incident_id": incident, "until_at": None}
        )
    with _login(app) as client:
        snapshot = client.get("/api/snapshot").json()
        by_id = {r["employee_id"]: r for r in snapshot["readiness"]["rows"]}
        assert snapshot["readiness"]["sampled_at"] == 1000
        assert by_id[founder]["blockers"] == [
            {"incident_id": incident, "alternative_account_usable": False}
        ]
        assert by_id[analyst]["blockers"] == [
            {"incident_id": incident, "alternative_account_usable": True}
        ]
        listing = client.get("/api/incidents").json()
        assert listing["recent"] == []
        assert listing["open"][0]["blocked_roles"] == [
            {key: by_id[founder][key] for key in ("employee_id", "address", "identity")}
        ]
        original = client.get("/api/inbox").json()["messages"][0]
        assert original["regarding"] == {
            "kind": "incident",
            "id": incident,
            "status": "open",
            "resolved_at": None,
            "resolved_by": None,
        }
        now[0] = 1010
        payload = {"incident_id": incident, "operation_id": "clear"}
        result = _post(client, "/api/incidents/clear", payload)
        assert result.json() == {"status": "cleared", "incident_id": incident}
        now[0] = 1020

        def forbidden(*args, **kwargs):
            raise AssertionError("receipt replay must not clear again")

        with monkeypatch.context() as patch:
            patch.setattr(runtime.accounts, "clear", forbidden)
            assert _post(client, "/api/incidents/clear", payload).json() == result.json()
        assert (
            _post(client, "/api/incidents/clear", {**payload, "operation_id": "clear-again"}).json()
            == result.json()
        )
        listing = client.get("/api/incidents").json()
        assert listing["open"] == []
        assert listing["recent"][0]["resolved_at"] == 1010
        assert listing["recent"][0]["resolved_by"] == "owner"
        assert listing["recent"][0]["blocked_roles"] == []
        changed = client.get("/api/inbox").json()["messages"][0]
        assert changed.pop("regarding") == {
            "kind": "incident",
            "id": incident,
            "status": "resolved",
            "resolved_at": 1010,
            "resolved_by": "owner",
        }
        original.pop("regarding")
        assert changed == original

        second = runtime.accounts.pause(OWNER, "account", "b", "auth")["incident_id"]
        assert (
            _post(client, "/api/incidents/clear", {**payload, "incident_id": second}).status_code
            == 400
        )
        open_incidents = client.get("/api/incidents").json()["open"]
        assert [item["incident_id"] for item in open_incidents] == [second]
        assert open_incidents[0]["resolved_at"] is None
        assert open_incidents[0]["resolved_by"] is None


def test_incident_clear_and_receipt_roll_back_together(tmp_path, monkeypatch):
    runtime, _, _, app = _system(tmp_path)
    incident = runtime.accounts.pause(OWNER, "profile", "default", "config")["incident_id"]
    clear = runtime.accounts.clear

    def fail_after_clear(*args):
        clear(*args)
        raise FirmError("injected clear failure")

    monkeypatch.setattr(runtime.accounts, "clear", fail_after_clear)
    payload = {"incident_id": incident, "operation_id": "clear"}
    with _login(app) as client:
        assert _post(client, "/api/incidents/clear", payload).status_code == 400
        with runtime.store.read() as db:
            assert runtime.store.operation_result(db, OWNER, "clear") is None
        assert client.get("/api/incidents").json()["open"][0]["resolved_by"] is None
        monkeypatch.setattr(runtime.accounts, "clear", clear)
        assert _post(client, "/api/incidents/clear", payload).status_code == 200
        with runtime.store.read() as db:
            assert runtime.store.operation_result(db, OWNER, "clear") == {
                "operation": "clear_incident",
                "result": {"status": "cleared", "incident_id": incident},
            }


def test_expired_incident_reads_are_effective_and_do_not_write(tmp_path):
    runtime, _, _, app = _system(tmp_path)
    runtime.store.clock = lambda: 1000.0
    incident = runtime.accounts.pause(OWNER, "profile", "default", "outage", 1010)["incident_id"]
    runtime.store.clock = lambda: 1020.0
    with runtime.store.read() as db:
        before = list(db.iterdump())
    with _login(app) as client:
        listing = client.get("/api/incidents?recent=1").json()
        assert listing == {
            "open": [],
            "recent": [
                {
                    "incident_id": incident,
                    "scope": "profile",
                    "dependency": "default",
                    "kind": "outage",
                    "created_at": 1000,
                    "until_at": 1010,
                    "resolved_at": 1010,
                    "resolved_by": "expired",
                    "blocked_roles": [],
                }
            ],
        }
        assert client.get("/api/incidents?recent=0").json()["recent"] == []
        for path in ("/api/inbox", "/api/messages"):
            assert client.get(path).json()["messages"][0]["regarding"] == {
                "kind": "incident",
                "id": incident,
                "status": "resolved",
                "resolved_at": 1010,
                "resolved_by": "expired",
            }
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("decided_by", ["owner", "runtime", None])
def test_console_request_notice_status_is_hydrated_at_read_time(tmp_path, decided_by):
    runtime, founder, _, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        request = runtime.requests.ask_owner(
            db,
            {"employee_id": founder, "activation_id": None},
            "thing",
            "widget",
            "Build",
            "needs_owner",
        )
        notice = runtime.messaging.notice(
            db,
            [OWNER],
            "Request",
            subject="Request",
            regarding={"kind": "request", "id": request["request_id"]},
        )
    with _login(app) as client:
        pending_messages = client.get("/api/inbox").json()["messages"]
        assert [m["message_id"] for m in pending_messages] == [notice["message_id"]]
        pending = pending_messages[0]
        assert pending["regarding"] == {
            "kind": "request",
            "id": request["request_id"],
            "status": "pending",
            "resolved_at": None,
            "resolved_by": None,
        }
        with runtime.store.transaction() as db:
            runtime.requests.answer(
                db,
                [runtime.requests.pending(db, request["request_id"])],
                "granted",
                decided_by=decided_by,
            )
            decision_notices = db.execute(
                "SELECT message_id FROM messages WHERE message_id!=?", (notice["message_id"],)
            ).fetchall()
            assert len(decision_notices) == 1
            decision_notice_id = decision_notices[0]["message_id"]
        messages = client.get("/api/messages").json()["messages"]
        assert [m["message_id"] for m in messages] == [decision_notice_id, notice["message_id"]]
        assert all(m["regarding"]["status"] == "granted" for m in messages)
        assert all(m["regarding"]["resolved_by"] == decided_by for m in messages)
        assert all(m["regarding"]["resolved_at"] is not None for m in messages)
        assert client.get("/api/requests").json()["recent"][0]["decided_by"] == decided_by
    assert (
        "regarding"
        not in runtime.messaging.messages(founder, [notice["message_id"]])["messages"][0]
    )


@pytest.mark.parametrize("kind", ["incident", "request"])
def test_console_missing_message_reference_preserves_id_with_unknown_status(tmp_path, kind):
    runtime, _, _, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        notice = runtime.messaging.notice(
            db, [OWNER], "Missing reference", subject="Missing reference",
            regarding={"kind": kind, "id": "missing"},
        )
    with _login(app) as client:
        for path in ("/api/inbox", "/api/messages"):
            messages = client.get(path).json()["messages"]
            assert [m["message_id"] for m in messages] == [notice["message_id"]]
            assert messages[0]["regarding"] == {
                "kind": kind,
                "id": "missing",
                "status": None,
                "resolved_at": None,
                "resolved_by": None,
            }


def test_console_message_send_and_reply_replay_the_same_service_request(tmp_path):
    runtime, _, analyst, app = _system(tmp_path)
    with _login(app) as client:
        payload = {
            "operation_id": "op-send-1",
            "recipients": ["founder/analyst"],
            "subject": "Review the plan",
            "body": "Please review the draft.",
        }
        sent = _post(client, "/api/messages/send", payload)
        assert sent.status_code == 200, sent.text
        result = sent.json()
        replay = _post(client, "/api/messages/send", payload)
        assert replay.json() == result
        receipt = client.get(
            "/api/messages/operation", params={"operation": "send", "operation_id": "op-send-1"}
        )
        assert receipt.json() == {"status": "recorded", "result": result}
        assert (
            client.get(
                "/api/messages/operation",
                params={"operation": "reply", "operation_id": "op-send-1"},
            ).status_code
            == 409
        )
        assert (
            runtime.messaging.messages(analyst, [result["message_id"]])["messages"][0]["body"]
            == payload["body"]
        )
        with runtime.store.read() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM messages WHERE author_id='owner'").fetchone()[0]
                == 1
            )

        reply_payload = {
            "operation_id": "op-reply-1",
            "message_id": result["message_id"],
            "body": "Reviewed and ready.",
        }
        reply = _post(client, "/api/messages/reply", reply_payload)
        assert reply.status_code == 200, reply.text
        assert _post(client, "/api/messages/reply", reply_payload).json() == reply.json()
        with runtime.store.read() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM messages WHERE author_id='owner'").fetchone()[0]
                == 2
            )


def test_console_lifecycle_mutation_replays_its_durable_receipt(tmp_path):
    runtime, _, _, _ = _system(tmp_path)

    class Control:
        calls = 0

        def stop(self, force=False):
            self.calls += 1
            return {"status": "stopped", "force": force}

        def request_start(self):
            raise AssertionError("stop replay must not start")

        def request_close(self):
            raise AssertionError("stop replay must not close")

    control = Control()
    app = build_console_app(runtime, allowed_host=HOST, control=control)
    with _login(app) as client:
        payload = {"operation_id": "op-stop-1", "action": "stop"}
        first = _post(client, "/api/control/stop", payload)
        second = _post(client, "/api/control/stop", payload)
        conflict = _post(
            client,
            "/api/control/force-stop",
            {"operation_id": "op-stop-1", "action": "force-stop", "confirmed": True},
        )
    assert first.json() == second.json() == {"status": "stopped", "force": False}
    assert control.calls == 1
    assert conflict.status_code == 409


def test_console_surfaces_startup_failure_and_holds_resume_until_vm_shutdown(tmp_path):
    runtime, _, _, _ = _system(tmp_path)

    class Control:
        def startup_error(self):
            return "Firm startup failed; VM shutdown is not yet confirmed."

        def request_start(self):
            raise AssertionError("resume must remain disabled until shutdown is confirmed")

        def request_close(self):
            raise AssertionError("close must remain disabled until shutdown is confirmed")

        def stop(self, force=False):
            raise AssertionError("stop is not expected")

    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO execution_host(singleton,sandbox_root,sandbox_firm_id,"
            "sandbox_identity,infrastructure_state) VALUES(1,?,?,?,'unknown')",
            (str(tmp_path), "test", "sandbox-identity"),
        )
    app = build_console_app(runtime, allowed_host=HOST, control=Control())
    with _login(app) as client:
        status = client.get("/api/control/status").json()
        assert status["startup_error"] == "Firm startup failed; VM shutdown is not yet confirmed."
        assert status["can_resume"] is False and status["can_close"] is False
        with runtime.store.transaction() as db:
            db.execute("UPDATE execution_host SET infrastructure_state='stopped' WHERE singleton=1")
        stopped = client.get("/api/control/status").json()
        assert stopped["startup_error"] == status["startup_error"]
        assert stopped["can_resume"] is True and stopped["can_close"] is True
        assert 'id="startup-error"' in client.get("/").text


def test_console_mutations_require_session_exact_origin_and_fetch_marker(tmp_path):
    _, _, _, app = _system(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/messages/send",
            json={},
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "X-Firm-Console": "same-origin",
            },
        )
        assert response.status_code == 401
    with _login(app) as client:
        payload = {"operation_id": "op", "recipients": ["founder/analyst"], "body": "Hi"}
        assert (
            client.post(
                "/api/messages/send",
                json=payload,
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                },
            ).status_code
            == 403
        )
        response = client.post(
            "/api/messages/send",
            json=payload,
            headers={
                "Origin": "http://evil.test",
                "Sec-Fetch-Site": "same-origin",
                "X-Firm-Console": "same-origin",
            },
        )
        assert response.status_code == 403


def _pending_request(runtime, analyst):
    class DecisionKind:
        name = "console_test"

        def __init__(self, runtime):
            self.runtime = runtime

        def grant(self, row):
            with self.runtime.store.transaction() as db:
                self.runtime.requests.pending(db, row["request_id"])
                return {"answered": self.runtime.requests.answer(db, [row], "granted")}

        def deny(self, row, reason):
            with self.runtime.store.transaction() as db:
                self.runtime.requests.pending(db, row["request_id"])
                return {"answered": self.runtime.requests.answer(db, [row], "denied", reason)}

    runtime.requests.kinds["console_test"] = DecisionKind(runtime)
    with runtime.store.transaction() as db:
        runtime.requests.ask_owner(
            db,
            {"employee_id": analyst, "activation_id": None},
            "console_test",
            "example/resource",
            "review access",
            "needs_owner",
        )


def test_console_request_decisions_replay_recorded_state_and_reject_contradictions(tmp_path):
    runtime, _, analyst, app = _system(tmp_path)
    _pending_request(runtime, analyst)
    with _login(app) as client:
        listing = client.get("/api/requests")
        assert listing.status_code == 200
        request = listing.json()["pending"][0]
        assert request["request_id"] == "q1" and request["resource"] == "example/resource"
        payload = {"operation_id": "op-grant", "request_id": "q1", "action": "grant"}
        granted = _post(client, "/api/requests/decide", payload)
        assert granted.status_code == 200 and granted.json()["status"] == "granted"
        replay = _post(client, "/api/requests/decide", payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        denied = _post(
            client,
            "/api/requests/decide",
            {
                "operation_id": "op-deny",
                "request_id": "q1",
                "action": "deny",
                "reason": "No",
            },
        )
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "already_decided"


def test_console_denial_normalizes_before_limit_validation_and_replay(tmp_path):
    runtime, _, analyst, app = _system(tmp_path)
    _pending_request(runtime, analyst)
    note = "🧭" * MAX_REASON
    payload = {
        "operation_id": "deny-normalized",
        "request_id": "q1",
        "action": "deny",
        "reason": "  " + note + "  ",
    }
    with _login(app) as client:
        response = _post(client, "/api/requests/decide", payload)
        assert response.status_code == 200, response.text
        assert runtime.requests.status(OWNER, "q1")["decision_note"] == note
        replay = _post(client, "/api/requests/decide", {**payload, "reason": note})
        assert replay.status_code == 200 and replay.json()["replayed"] is True


@pytest.mark.parametrize("reason", [None, True, [], " \n", "x" * (MAX_REASON + 1)])
def test_console_invalid_denial_is_input_error_without_a_decision(tmp_path, reason):
    runtime, _, analyst, app = _system(tmp_path)
    _pending_request(runtime, analyst)
    with _login(app) as client:
        response = _post(
            client,
            "/api/requests/decide",
            {
                "operation_id": "deny-invalid",
                "request_id": "q1",
                "action": "deny",
                "reason": reason,
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reason_required"
    assert runtime.requests.status(OWNER, "q1")["status"] == "pending"


@pytest.mark.parametrize("recorded_note", ["My reason", "A different decision"])
def test_console_reconciles_failed_denial_response_with_the_exact_note(
    tmp_path, monkeypatch, recorded_note
):
    runtime, _, analyst, app = _system(tmp_path)
    _pending_request(runtime, analyst)
    real_deny = runtime.requests.deny

    def interrupted_deny(actor, request_id, reason):
        real_deny(actor, request_id, recorded_note)
        raise OSError("decision response was interrupted")

    monkeypatch.setattr(runtime.requests, "deny", interrupted_deny)
    with _login(app) as client:
        response = _post(
            client,
            "/api/requests/decide",
            {
                "operation_id": "deny-interrupted",
                "request_id": "q1",
                "action": "deny",
                "reason": "  My reason  ",
            },
        )
    assert runtime.requests.status(OWNER, "q1")["decision_note"] == recorded_note
    if recorded_note == "My reason":
        assert response.status_code == 200 and response.json()["replayed"] is True
    else:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "already_decided"


@pytest.mark.parametrize("action", ["send", "reply"])
def test_console_message_limit_counts_utf8_bytes_for_send_and_reply(tmp_path, action):
    runtime, _, analyst, app = _system(tmp_path)
    original = runtime.messaging.send(analyst, [OWNER], "A question", request_id="question")
    payload = {"recipients": ["founder/analyst"]} if action == "send" else {
        "message_id": original["message_id"]
    }
    body = "é" * (MAX_BODY_BYTES // 2)
    with _login(app) as client:
        allowed = _post(
            client, f"/api/messages/{action}",
            {**payload, "operation_id": "within-limit", "body": body},
        )
        assert allowed.status_code == 200, allowed.text
        refused = _post(
            client, f"/api/messages/{action}",
            {**payload, "operation_id": "over-limit", "body": body + "x"},
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "invalid_message"


def test_console_browses_bounded_files_as_attachments_without_starting_a_vm(tmp_path):
    from firm_sandbox import Config, Organization
    from firm_sandbox.file_server import FileServer, local_runner

    runtime, _, _, _ = _system(tmp_path)
    root = tmp_path / "guest-tree"
    root.mkdir()
    firm_root = tmp_path / "sandbox-firm"
    (firm_root / "locks").mkdir(parents=True)
    server = FileServer(
        Config(firm_root, firm_id="test"),
        local_runner(root),
        forwards=lambda port: [],
    )
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    server.provision("r-analyst", Organization("founder/analyst"))
    work = server.workspace_path("r-analyst")
    (root / work / "weekly-planner.html").write_bytes(b"<h1>Week</h1>")

    class Box:
        file_server = server

    app = build_console_app(runtime, allowed_host=HOST, sandbox=Box())
    with _login(app) as client:
        roots = client.get("/api/files/roots").json()
        assert roots["available"] is True
        assert any(
            item["path"] == work and item["address"] == "founder/analyst" for item in roots["roots"]
        )
        listing = client.get("/api/files/list", params={"path": work}).json()
        assert listing["entries"][0]["name"] == "weekly-planner.html"
        response = client.get(
            "/api/files/read", params={"path": work, "name": "weekly-planner.html"}
        )
        assert response.content == b"<h1>Week</h1>"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        preview = client.get(
            "/api/files/preview", params={"path": work, "name": "weekly-planner.html"}
        )
        assert preview.content == b"<h1>Week</h1>"
        assert preview.headers["content-type"] == "text/html; charset=utf-8"
        policy = preview.headers["content-security-policy"]
        assert "sandbox allow-scripts" in policy
        assert "connect-src 'none'" in policy and "form-action 'none'" in policy
        assert "frame-src 'none'" in policy
        assert "frame-ancestors http://127.0.0.1:8765" in policy
        assert client.get("/api/files/list", params={"path": "org/../shared"}).status_code == 400
        page_response = client.get("/")
        page = page_response.text
        assert "frame-src http://127.0.0.1:8765/api/files/preview" in page_response.headers[
            "content-security-policy"
        ]
        assert 'sandbox="allow-scripts"' in page
        assert "frame.src = `/api/files/preview?" in client.get("/results.js").text
        assert "frame.srcdoc" not in client.get("/results.js").text


def _file_console(tmp_path):
    from firm_sandbox import Config, Organization
    from firm_sandbox.file_server import FileServer, local_runner

    from firm_runtime.workspaces import sandbox_role_id

    runtime, founder, analyst, _ = _system(tmp_path)
    root = tmp_path / "guest-tree"
    root.mkdir()
    firm_root = tmp_path / "sandbox-firm"
    (firm_root / "locks").mkdir(parents=True)
    server = FileServer(
        Config(firm_root, firm_id="test"), local_runner(root), forwards=lambda port: []
    )
    server._save(
        {
            "port": 445,
            "database_identity": None,
            "owner_password": "owner",
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }
    )
    server.provision(sandbox_role_id(founder), Organization("founder"))
    server.provision(sandbox_role_id(analyst), Organization("founder/analyst"))

    class Box:
        file_server = server

    app = build_console_app(runtime, allowed_host=HOST, sandbox=Box())
    return runtime, founder, analyst, app, root, server


@pytest.mark.parametrize("extension", ["md", "markdown"])
def test_console_markdown_preview_renders_tables_without_active_html(tmp_path, extension):
    _, _, _, app, root, _ = _file_console(tmp_path)
    work = "org/founder/analyst/work"
    (root / work / f"report.{extension}").write_text(
        "# Report\n\n| Topic | Result |\n| --- | --- |\n| Safety | passed |\n\n"
        "<script>alert('bad')</script>\n\n<img src=x onerror=alert(1)>\n"
    )
    with _login(app) as client:
        response = client.get(
            "/api/files/preview", params={"path": work, "name": f"report.{extension}"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        f"frame-ancestors {ORIGIN}"
    )
    page = response.text
    assert "<h1>Report</h1>" in page
    assert "<table>" in page and "<td>passed</td>" in page
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_console_text_preview_escapes_markup_and_enforces_size_and_type(tmp_path):
    _, _, _, app, root, _ = _file_console(tmp_path)
    work = "org/founder/analyst/work"
    (root / work / "notes.txt").write_text("<script>bad()</script>\nA & B")
    (root / work / "too-big.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with _login(app) as client:
        response = client.get(
            "/api/files/preview", params={"path": work, "name": "notes.txt"}
        )
        missing = client.get(
            "/api/files/preview", params={"path": work, "name": "missing.md"}
        )
        oversized = client.get(
            "/api/files/preview", params={"path": work, "name": "too-big.md"}
        )
        unsupported = client.get(
            "/api/files/preview", params={"path": work, "name": "image.png"}
        )
    assert response.status_code == 200
    assert "<pre>&lt;script&gt;bad()&lt;/script&gt;\nA &amp; B</pre>" in response.text
    assert "<script>" not in response.text
    assert response.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        f"frame-ancestors {ORIGIN}"
    )
    assert missing.status_code == 404
    assert oversized.status_code == 413
    assert unsupported.status_code == 415


@pytest.mark.parametrize("archived", [False, True])
def test_nested_attachment_round_trip_uses_real_guest_validation(tmp_path, archived):
    from firm_sandbox import Layout, Organization

    from firm_runtime.workspaces import sandbox_role_id

    runtime, founder, analyst, app, root, server = _file_console(tmp_path)
    analyst_role = sandbox_role_id(analyst)
    if archived:
        server.retire_role(analyst_role)
        server.provision(
            "r-successor", Organization("founder/analyst"), replace_role_id=analyst_role
        )
    work = server.workspace_path(analyst_role)
    attachment = {"path": work + "/reports/drafts", "name": "nested.md"}
    drive = Layout.for_role(sandbox_role_id(founder)).drive
    sent = runtime.messaging.send(
        founder, [OWNER], "Nested report", subject="Nested", request_id="nested",
        attachments=[f"{drive}/{attachment['path']}/{attachment['name']}"],
    )
    with _login(app) as client:
        result = client.get("/api/results").json()["results"][0]
        assert result["message_id"] == sent["message_id"]
        assert {key: result[key] for key in ("path", "name")} == attachment
        # Materialize after send: references never require an existing file.
        directory = root / result["path"]
        directory.mkdir(parents=True)
        content = b"# Nested report\n\nDelivered through the guest boundary.\n"
        (directory / result["name"]).write_bytes(content)
        listing = client.get("/api/files/list", params={"path": result["path"]})
        assert listing.status_code == 200
        assert listing.json()["entries"][0]["name"] == result["name"]
        preview = client.get("/api/files/preview", params=attachment)
        assert preview.status_code == 200
        assert "<h1>Nested report</h1>" in preview.text
        download = client.get("/api/files/read", params=attachment)
        assert download.status_code == 200
        assert download.content == content
        assert download.headers["content-disposition"].startswith("attachment;")


def test_console_results_lists_owner_deliveries_by_message_then_attachment(tmp_path):
    from firm_sandbox.models import Layout

    from firm_runtime.workspaces import sandbox_role_id

    runtime, founder, analyst, app, _, _ = _file_console(tmp_path)
    drive = Layout.for_role(sandbox_role_id(analyst)).drive
    first = runtime.messaging.send(
        analyst, [OWNER], "First report", subject="First", request_id="first",
        attachments=[f"{drive}/shared/first.md"],
    )
    internal = runtime.messaging.send(
        analyst, [founder], "Internal report", request_id="internal",
        attachments=[f"{drive}/shared/private.md"],
    )
    latest = runtime.messaging.send(
        analyst, [OWNER], "Latest report", subject="Latest", request_id="latest",
        attachments=[f"{drive}/shared/second.md", f"{drive}/shared/third.txt"],
    )
    runtime.messaging.dismiss(OWNER, [latest["message_id"]], request_id="done-latest")
    with _login(app) as client:
        page = client.get("/api/results").json()
        latest_message = next(
            item for item in client.get("/api/inbox?view=all").json()["messages"]
            if item["message_id"] == latest["message_id"]
        )
        one = client.get("/api/results?limit=1").json()
        two = client.get("/api/results?limit=2").json()
        max_page = client.get("/api/results?limit=200").json()
    assert isinstance(page["sampled_at"], (int, float))
    assert page["truncated"] is False
    assert max_page["results"] == page["results"]
    assert [item["name"] for item in page["results"]] == [
        "second.md", "third.txt", "first.md"
    ]
    assert [item["message_id"] for item in page["results"]] == [
        latest["message_id"], latest["message_id"], first["message_id"]
    ]
    assert all(item["message_id"] != internal["message_id"] for item in page["results"])
    assert all(item["path"] == "shared" for item in page["results"])
    assert page["results"][0] == {
        "path": "shared",
        "name": "second.md",
        "message_id": latest["message_id"],
        "conversation_id": latest["conversation_id"],
        "subject": "Latest",
        "author": latest_message["author"],
        "created_at": latest_message["created_at"],
    }
    assert [item["name"] for item in one["results"]] == ["second.md"]
    assert one["truncated"] is True
    assert [item["name"] for item in two["results"]] == ["second.md", "third.txt"]
    assert two["truncated"] is True


def test_console_owner_replies_with_reattachment_and_replays_receipt(tmp_path):
    from firm_sandbox.models import Layout

    from firm_runtime.workspaces import sandbox_role_id

    runtime, _, analyst, app, _, _ = _file_console(tmp_path)
    drive = Layout.for_role(sandbox_role_id(analyst)).drive
    source = runtime.messaging.send(
        analyst, [OWNER], "Please review", request_id="source",
        attachments=[f"{drive}/shared/draft.md"],
    )
    attachment = {"path": "shared", "name": "draft.md"}
    payload = {
        "operation_id": "reply-with-file", "message_id": source["message_id"],
        "body": "Please revise this draft.", "attachments": [attachment],
    }
    with _login(app) as client:
        sent_new = _post(
            client, "/api/messages/send",
            {
                "operation_id": "send-with-file", "recipients": ["founder/analyst"],
                "body": "Here is a separate file.", "attachments": [attachment],
            },
        )
        assert sent_new.status_code == 200, sent_new.text
        sent = _post(client, "/api/messages/reply", payload)
        assert sent.status_code == 200, sent.text
        assert _post(client, "/api/messages/reply", payload).json() == sent.json()
        assert _post(
            client, "/api/messages/reply", {**payload, "attachments": []}
        ).status_code == 400
        inbox = client.get("/api/inbox?view=done").json()["messages"]
        assert inbox[0]["message_id"] == source["message_id"]
        assert inbox[0]["owner_delivery"]["disposition"] == "dismissed"
        conversation = client.get("/api/messages").json()["messages"]
        new_message = next(
            item for item in conversation if item["message_id"] == sent_new.json()["message_id"]
        )
        assert new_message["attachments"] == [attachment]
        reply = next(
            item for item in conversation if item["message_id"] == sent.json()["message_id"]
        )
        assert reply["attachments"] == [attachment]
        assert reply["reply_to"] == source["message_id"]
        assert [item["message_id"] for item in client.get("/api/results").json()["results"]] == [
            source["message_id"]
        ]


@pytest.mark.parametrize(
    "attachment",
    [
        {"path": "org/../shared", "name": "draft.md"},
        {"path": "shared", "name": "../draft.md"},
        {"path": "outside", "name": "draft.md"},
        {"path": "shared", "name": "draft.md", "extra": "bad"},
    ],
)
def test_console_owner_refuses_invalid_attachment_references(tmp_path, attachment):
    runtime, _, _, app, _, _ = _file_console(tmp_path)
    with _login(app) as client:
        response = _post(
            client, "/api/messages/send",
            {
                "operation_id": "bad-file", "recipients": ["founder/analyst"],
                "body": "Review this", "attachments": [attachment],
            },
        )
    assert response.status_code == 400
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_console_owner_attachment_count_is_bounded(tmp_path):
    runtime, _, _, app, _, _ = _file_console(tmp_path)
    with _login(app) as client:
        response = _post(
            client, "/api/messages/send",
            {
                "operation_id": "too-many-files", "recipients": ["founder/analyst"],
                "body": "Review these", "attachments": [
                    {"path": "shared", "name": f"file-{index}.md"} for index in range(11)
                ],
            },
        )
    assert response.status_code == 400
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_console_file_browser_reports_stopped_vm_without_booting_it(tmp_path):
    runtime, _, _, _ = _system(tmp_path)

    class Files:
        def require_ready(self):
            raise RuntimeError("VM is stopped")

    class Box:
        file_server = Files()

    app = build_console_app(runtime, allowed_host=HOST, sandbox=Box())
    with _login(app) as client:
        response = client.get("/api/files/roots")
        assert response.json() == {"available": False, "roots": [], "reason": "VM is stopped"}


def test_snapshot_carries_one_row_per_mac_station_with_its_holder(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    runtime.start()
    activation = runtime.admit()
    runtime.mark_started(activation.activation_id)
    lease = {
        "lease_id": "l1",
        "station": 1,
        "ssh_host": "127.0.0.1",
        "ssh_port": 2222,
        "user": "firmstation1",
        "drive": "/drive",
        "project_dir": "/drive/work",
        "key_path": "/state/credentials/mac-l1",
    }
    stations = [
        {
            "station": 1,
            "configured": True,
            "enrolled": True,
            "held": True,
            "lease_id": "l1",
            "holder": "r-" + activation.employee_id,
            "since": 100.0,
            "dirty": False,
        },
        {
            "station": 2,
            "configured": True,
            "enrolled": True,
            "held": False,
            "lease_id": None,
            "holder": None,
            "since": None,
            "dirty": True,
        },
    ]
    runtime.mac.open(
        acquire=lambda employee_id, activation_id: lease,
        release=lambda lease_id: {"status": "ok", "clean": True, "checks": {}},
        status=lambda: {"configured": True, "enrolled": True, "stations": stations},
        connect=lambda lease_id: None,
        write_work_file=lambda *arguments: "",
    )
    assert runtime.mac.acquire(activation.token)["status"] == "granted"
    with _login(app) as client:
        mac = client.get("/api/snapshot").json()["mac"]
    assert mac["configured"] is True and len(mac["stations"]) == 2
    assert mac["stations"][0]["holder"] == "founder"
    assert mac["stations"][1] == {**stations[1], "holder": None}


def test_snapshot_separates_past_drain_from_current_execution(tmp_path):
    runtime, founder, _, app = _system(tmp_path)
    runtime.start()
    activation = runtime.admit()
    runtime.stop()
    runtime.finish(activation.activation_id, outcome="interrupted")
    runtime.start()
    with _login(app) as client:
        response = client.get("/api/snapshot")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["firm"]["state"] == "running"
        past = data["drain_outcomes"][0]
        assert past["confirmed_exit"] and past["requested_at"] is not None
        assert past["address"] == "founder"
        assert past["finished_at"] is not None
        assert "mandate" not in data["roles"][0]
        assert data["roles"][0]["created_at"] is not None


def test_accounts_are_observable_without_exposing_credentials(tmp_path):
    runtime, _, _, app = _system(tmp_path)
    incident = runtime.accounts.pause(OWNER, "profile", "default", "config")
    with _login(app) as client:
        data = client.get("/api/snapshot").json()
        assert data["accounts"]["incidents"][0]["incident_id"] == incident["incident_id"]
        assert "auth_file" not in json.dumps(data)


def test_snapshot_account_recovery_paths_and_admissions(tmp_path, monkeypatch):
    from firm_runtime import accounts

    monkeypatch.setattr(accounts, "CREDENTIALS_DIR", tmp_path / "credentials")
    custom = tmp_path / "custom-login.json"
    secret = "credential-contents-must-never-reach-the-console"
    custom.write_text(json.dumps({"token": secret}))
    runtime, founder, _, app = _system(tmp_path)
    runtime.owner.configure(
        OWNER,
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {
                "default": {
                    "provider": "codex", "model": "test", "effort": "low",
                    "accounts": ["main"],
                }
            },
            "accounts": {
                "main": {"provider": "codex", "max_active": 2},
                "explicit-default": {
                    "provider": "codex", "max_active": 2,
                    "auth_file": str(accounts.credentials_path("explicit-default")),
                },
                "custom": {
                    "provider": "codex", "max_active": 2, "auth_file": str(custom),
                },
            },
        },
    )
    now = [time.time()]
    monkeypatch.setattr(runtime.store, "clock", lambda: now[0])
    # No lifespan/observer thread: this test never reads credentials or calls a provider.
    client = _login(app)

    def read_accounts():
        response = client.get("/api/snapshot")
        assert response.status_code == 200, response.text
        assert secret not in response.text
        return {a["account_id"]: a for a in response.json()["accounts"]["accounts"]}

    before = read_accounts()
    assert before["main"]["auth_file"] == str(accounts.credentials_path("main"))
    assert before["main"]["default_auth_file"] is True
    assert before["explicit-default"]["default_auth_file"] is True
    assert before["custom"]["auth_file"] == str(custom)
    assert before["custom"]["default_auth_file"] is False
    assert all(a["last_started_at"] is None for a in before.values())

    runtime.start()
    first = runtime.admit()
    assert first.employee_id == founder
    first_at = now[0]
    assert read_accounts()["main"]["last_started_at"] == first_at
    now[0] += 10
    runtime.mark_started(first.activation_id)
    runtime.finish(first.activation_id)
    # Starting later and finishing do not rewrite the admission timestamp.
    assert read_accounts()["main"]["last_started_at"] == first_at
    now[0] += 10
    second = runtime.admit()
    latest_at = now[0]
    runtime.mark_started(second.activation_id)
    runtime.finish(second.activation_id)
    # A whole work period may occur between polls, on another role's session.
    final = read_accounts()
    assert final["main"]["last_started_at"] == latest_at
    assert final["custom"]["last_started_at"] is None
    assert final["explicit-default"]["last_started_at"] is None
    client.close()


def test_snapshot_mandate_revision_tracks_latest_without_loading_body(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    with _login(app) as client:
        roles = {r["employee_id"]: r for r in client.get("/api/snapshot").json()["roles"]}
        assert roles[founder]["mandate_revision"] == 0
        assert roles[analyst]["mandate_revision"] == 0
        runtime.lifecycle.revise_mandate(
            OWNER, founder, 0, "A revised mandate.", request_id="revise-founder"
        )
        roles = {r["employee_id"]: r for r in client.get("/api/snapshot").json()["roles"]}
        assert roles[founder]["mandate_revision"] == 1
        assert roles[analyst]["mandate_revision"] == 0
        assert "A revised mandate." not in json.dumps(roles)


def test_snapshot_leads_with_what_needs_the_owner(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    tools = runtime.tools(invocation.token)
    tools.request_access("network_host", "pypi.org", "Install packages", "ask")
    tools.send([OWNER], "Blocked on packages", "status")
    with _login(app) as client:
        needs = client.get("/api/snapshot").json()["needs_you"]
        page = client.get("/").text
        network = client.get("/api/analysis/network").json()
    assert network["live"] is False and network["pending_requests"][0]["resource"] == "pypi.org"
    assert needs["owner_mail"] == 1
    (request,) = needs["requests"]
    assert (request["request_id"], request["resource"], request["purpose"]) == (
        "q1",
        "pypi.org",
        "Install packages",
    )
    assert request["employee_id"] == invocation.employee_id and request["address"]
    assert 'id="decision-list"' in page
    assert 'id="decision-count"' in page
    assert 'id="mail-count"' in page


def test_github_repositories_requested_grants_and_revocations_are_listed(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO github_repositories VALUES('firmery/test-app',?,'The web app',5)",
            (founder,),
        )
        db.execute(
            "INSERT INTO github_grants VALUES('firmery/test-app','founder/analyst','write',"
            "'requested',6)"
        )
        db.execute("INSERT INTO github_revocations VALUES('firmery/test-app','',7)")
    with _login(app) as client:
        data = client.get("/api/snapshot").json()["github"]
    assert data == {
        "repositories": [
            {
                "repository": "firmery/test-app",
                "address": "founder",
                "purpose": "The web app",
                "created_at": 5,
            }
        ],
        "grants": [
            {
                "repository": "firmery/test-app",
                "address": "founder/analyst",
                "access": "write",
                "source": "requested",
                "created_at": 6,
            }
        ],
        "revocations": [{"repository": "firmery/test-app", "address": "", "revoked_at": 7}],
    }


@pytest.mark.parametrize(
    "provider,model,next_model,window",
    [
        ("claude", "opus", "sonnet", 500000),
        ("codex", "gpt-5.6-sol", "gpt-6-astra", 256000),
    ],
)
def test_role_parameters_use_retained_binding_before_new_profile_settings(
    tmp_path,
    provider,
    model,
    next_model,
    window,
):
    runtime, founder, analyst, app = _system(tmp_path)
    config = {
        "max_active": 2,
        "staffing": False,
        "founder_profile": "default",
        "profiles": {
            "default": {
                "provider": provider,
                "model": model,
                "effort": "high",
                "accounts": ["a"],
            }
        },
        "accounts": {
            "a": {
                "provider": provider,
                "auth_file": "/private/secret-auth",
                "max_active": 2,
            }
        },
    }
    runtime.owner.configure(OWNER, config)
    runtime.start()
    invocation = runtime.admit()
    assert invocation.employee_id == founder
    config["profiles"]["default"].update(model=next_model, effort="low")
    runtime.owner.configure(OWNER, config)
    with runtime.store.read() as db:
        before = list(db.iterdump())
    with _login(app) as client:
        data = client.get("/api/snapshot").json()
        roles = {r["employee_id"]: r for r in data["roles"]}
        retained = roles[founder]["execution"]
        assert retained == {
            "source": "retained_session",
            "profile": "default",
            "provider": provider,
            "model": model,
            "effort": "high",
            "context_window": window,
            "context_advisory_fraction": 0.6,
            "handoff_reserve": 10000,
        }
        fresh = roles[analyst]["execution"]
        assert fresh["source"] == "configured_profile"
        assert (fresh["model"], fresh["effort"]) == (next_model, "low")
        tree = client.get("/api/roles/tree").json()
        assert tree["root"]["execution"] == retained
        search = client.get("/api/roles/search?query=analyst").json()
        assert search["roles"][0]["execution"] == fresh
        assert "/private/secret-auth" not in json.dumps(roles)
        assert data["accounts"]["accounts"][0]["auth_file"] == "/private/secret-auth"
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_console_attaches_display_quota_for_both_providers(tmp_path, monkeypatch):
    from firm_runtime import console
    from firm_runtime.quota_observer import QuotaObserver

    class TestObserver(QuotaObserver):
        def __init__(self, accounts):
            def reading(_):
                return [{"label": "Weekly allowance", "used_percent": 100, "resets_at": 200}]

            super().__init__(
                accounts, fetchers={"claude": reading, "codex": reading}, clock=lambda: 100
            )

        def start(self):
            self.refresh()

    monkeypatch.setattr(console, "QuotaObserver", TestObserver)
    runtime, _, _, app = _system(tmp_path)
    runtime.owner.configure(
        OWNER,
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {
                "default": {
                    "provider": "claude",
                    "model": "opus",
                    "effort": "high",
                    "accounts": ["claude"],
                }
            },
            "accounts": {
                p: {"provider": p, "auth_file": "/private/secret", "max_active": 2}
                for p in ("claude", "codex")
            },
        },
    )
    with runtime.store.read() as db:
        before = list(db.iterdump())
    with _login(app) as client:
        data = client.get("/api/snapshot").json()
        assert len(data["accounts"]["accounts"]) == 2
        for account in data["accounts"]["accounts"]:
            assert account["subscription_quota"]["state"] == "current"
            assert account["subscription_quota"]["windows"][0]["used_percent"] == 100
        assert all(a["auth_file"] == "/private/secret" for a in data["accounts"]["accounts"])
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_console_shows_recorded_stream_allowance_for_setup_token_accounts(tmp_path, monkeypatch):
    from firm_runtime import console
    from firm_runtime.quota_observer import QuotaObserver

    class TestObserver(QuotaObserver):
        def __init__(self, accounts):
            super().__init__(accounts, fetchers={"claude": lambda _: None})

        def start(self):
            self.refresh()

    monkeypatch.setattr(console, "QuotaObserver", TestObserver)
    runtime, _, _, app = _system(tmp_path)
    runtime.owner.configure(
        OWNER,
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {
                "default": {
                    "provider": "claude",
                    "model": "opus",
                    "effort": "high",
                    "accounts": ["waiting", "streamed"],
                }
            },
            "accounts": {
                name: {"provider": "claude", "auth_file": "/private/secret", "max_active": 1}
                for name in ("waiting", "streamed")
            },
        },
    )
    window = {"window": "seven_day", "used_percent": 54.0, "resets_at": 4_000_000_000}
    runtime.accounts.record_subscription("streamed", [window])
    with _login(app) as client:
        accounts = {
            account["account_id"]: account["subscription_quota"]
            for account in client.get("/api/snapshot").json()["accounts"]["accounts"]
        }
    assert accounts["waiting"] == {"state": "awaiting_stream", "windows": []}
    assert accounts["streamed"]["state"] == "current"
    assert accounts["streamed"]["windows"][0]["label"] == "Weekly allowance"
    assert accounts["streamed"]["windows"][0]["used_percent"] == 54.0
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM account_observations").fetchone()[0] == 0


def test_console_role_browsing_is_paged_and_full_documents_are_on_demand(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    runtime.lifecycle.hire(
        founder, "analyst-two", "Own analyst two tasks.", "default", request_id="hire-analyst-two"
    )
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=?,public_profile_status='available' "
            "WHERE employee_id=?",
            ("A private profile should be returned only by explicit inspection.", analyst),
        )
    client = _login(app)
    with client:
        tree = client.get("/api/roles/tree?depth=1&limit=1").json()
        assert tree["root"]["employee_id"] == founder
        assert tree["remaining"] == 1
        assert tree["continuation"]
        search = client.get("/api/roles/search?query=analyst-two&limit=1").json()
        assert search["total_matches"] == 2
        assert search["roles"][0]["address"] == "founder/analyst-two"
        detail = client.get(f"/api/roles/{analyst}?section=profile").json()
        assert detail["documents"]["profile"]["text"] == (
            "A private profile should be returned only by explicit inspection."
        )
        assert detail["documents"]["profile"]["complete"] is True
        assert detail["owner_facts"]["execution_profile"] == "default"
        assert client.get(f"/api/roles/{analyst}?section=mandate").status_code == 200
        assert "mandate" not in detail["documents"]


def test_console_signed_role_document_cursor_rejects_source_change(tmp_path):
    runtime, _, analyst, app = _system(tmp_path)
    long_profile = "profile " * 10000
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=?,public_profile_status='available' "
            "WHERE employee_id=?",
            (long_profile, analyst),
        )
    client = _login(app)
    with client:
        page = client.get(f"/api/roles/{analyst}?section=profile").json()
        assert page["continuation"]
        with runtime.store.transaction() as db:
            db.execute(
                "UPDATE roles SET public_profile=? WHERE employee_id=?", ("changed", analyst)
            )
        stale = client.get(f"/api/roles/{analyst}?section=profile&cursor={page['continuation']}")
        assert stale.status_code == 400
        assert "restart" in stale.json()["error"]["message"]


def test_console_uses_explicit_public_names_in_role_cards(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        db.executemany(
            "UPDATE roles SET public_profile=?,public_profile_status='available' "
            "WHERE employee_id=?",
            [
                ("# Profile\n\n**Public name:** founder\n**Title:** Director", founder),
                (
                    "# Profile — analyst\n\n**Public name:** Wren Calloway\n**Title:** Designer",
                    analyst,
                ),
            ],
        )
    with _login(app) as client:
        snapshot = client.get("/api/snapshot").json()
        names = {role["employee_id"]: role["identity"] for role in snapshot["roles"]}
        assert names == {founder: "founder", analyst: "Wren Calloway"}
        tree = client.get("/api/roles/tree").json()
        assert tree["nodes"][0]["identity"] == "Wren Calloway"
        search = client.get("/api/roles/search?query=Wren").json()
        assert search["roles"][0]["identity"] == "Wren Calloway"


def test_console_pages_all_firm_messages_without_claiming_mail(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    colleague = runtime.messaging.send(founder, [analyst], "Colleague note", request_id="colleague")
    first = runtime.messaging.send(analyst, [OWNER], "First owner update", request_id="owner-1")
    second = runtime.messaging.send(analyst, [OWNER], "Second owner update", request_id="owner-2")
    outgoing = runtime.messaging.send(
        OWNER, [founder, analyst], "Owner to both roles", request_id="owner-3"
    )
    with runtime.store.read() as db:
        before = list(db.iterdump())
    client = _login(app)
    with client:
        page = client.get("/api/messages?limit=2").json()
        assert [m["message_id"] for m in page["messages"]] == [
            outgoing["message_id"],
            second["message_id"],
        ]
        assert {r["employee_id"] for r in page["messages"][0]["recipients"]} == {founder, analyst}
        older = client.get(f"/api/messages?limit=2&before={page['next_cursor']}").json()
        assert [m["message_id"] for m in older["messages"]] == [
            first["message_id"],
            colleague["message_id"],
        ]
        assert older["next_cursor"] is None
        note = older["messages"][1]
        assert note["body"] == "Colleague note"
        assert note["author"]["address"] == "founder"
        assert note["recipients"][0]["address"] == "founder/analyst"
        assert client.get("/api/messages?before=1").json()["messages"] == []
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_console_renders_measured_usage_facts_when_runtime_provides_them(tmp_path):
    runtime, _, _, app = _system(tmp_path)
    calls = []

    def usage(actor, *, employee_id=None, limit=100):
        calls.append((actor, employee_id, limit))
        return {
            "sessions": [
                {
                    "employee_id": "founder",
                    "current_context_tokens": 250,
                    "high_water_context_tokens": 400,
                }
            ],
            "activations": [{"employee_id": "founder", "input_tokens": 123}],
            "sessions_omitted": 0,
            "activations_omitted": 0,
        }

    runtime.usage_diagnostics = usage
    client = _login(app)
    with client:
        snapshot = client.get("/api/snapshot").json()
        assert snapshot["usage"]["sessions"][0]["current_context_tokens"] == 250
        assert snapshot["usage"]["activations"][0]["input_tokens"] == 123
        assert calls == [(OWNER, None, 100)]


def test_role_pages_refresh_current_execution_facts(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    with _login(app) as client:

        def analyst_facts():
            tree = client.get(f"/api/roles/tree?root={founder}").json()
            search = client.get("/api/roles/search?query=analyst").json()
            return [
                next(role for role in roles if role["employee_id"] == analyst)
                for roles in (tree["nodes"], search["roles"])
            ]

        assert all(role["employment"] == "active" for role in analyst_facts())
        runtime.owner.stop_role(OWNER, analyst, "pause-outside-console")
        assert all(role["employment"] == "paused" for role in analyst_facts())


def test_snapshot_keeps_status_and_role_facts_on_same_commit(tmp_path, monkeypatch):
    runtime, _, _, app = _system(tmp_path)
    runtime.start()
    actor = runtime.admit()
    runtime.mark_started(actor.activation_id)
    writer = Runtime(Store.scratch(runtime.store.path))
    original = runtime.status

    def status_then_finish():
        result = original()
        writer.finish(actor.activation_id)
        return result

    monkeypatch.setattr(runtime, "status", status_then_finish)
    with _login(app) as client:
        result = client.get("/api/snapshot").json()
    assert result["active"][0]["activation_id"] == actor.activation_id
    role = next(r for r in result["roles"] if r["employee_id"] == actor.employee_id)
    assert role["activation"]["state"] == "running"
    readiness = next(
        r for r in result["readiness"]["rows"] if r["employee_id"] == actor.employee_id
    )
    assert readiness["activity"] == "running"
    with writer.store.read() as db:
        assert writer._activation(db, actor.activation_id)["state"] == "completed"


def test_snapshot_timing_with_50_roles_and_accumulated_history(tmp_path, monkeypatch):
    runtime, founder, _, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["accounts"] = {
            "codex-main": {"provider": "codex", "max_active": 2, "auth_file": "/missing"}
        }
        config["profiles"]["default"] = {
            "provider": "codex", "model": "test", "effort": "low", "accounts": ["codex-main"]
        }
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
    for index in range(48):
        runtime.lifecycle.hire(
            founder, f"worker-{index}", "Own a piece of the work.", "default",
            request_id=f"hire-{index}",
        )
    runtime.start()
    with runtime.store.read() as db:
        roles = [row[0] for row in db.execute("SELECT employee_id FROM roles")]
    assert len(roles) == 50
    for cycle in range(10):
        for employee_id in roles:
            runtime.messaging.send(
                OWNER, [employee_id], "Continue", request_id=f"wake-{cycle}-{employee_id}"
            )
            runtime.messaging.send(employee_id, [OWNER], "Report", request_id=f"report-{cycle}")
        for _ in roles:
            invocation = runtime.admit()
            assert invocation is not None
            runtime.mark_started(invocation.activation_id)
            runtime.finish(invocation.activation_id)
        for _ in range(10):
            incident = runtime.accounts.pause(OWNER, "profile", "default", "outage")["incident_id"]
            runtime.accounts.clear(OWNER, incident)
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 500
        assert db.execute(
            "SELECT COUNT(*) FROM execution_bindings WHERE account_id='codex-main'"
        ).fetchone()[0] == 50
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] >= 1000
        assert db.execute("SELECT COUNT(*) FROM execution_incidents").fetchone()[0] == 100

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot must not open an analysis reader snapshot")

    monkeypatch.setattr(AnalysisReader, "snapshot", forbidden)
    with _login(app) as client:
        start = time.perf_counter()
        response = client.get("/api/snapshot")
        elapsed = time.perf_counter() - start
    assert response.status_code == 200, response.text
    assert len(response.json()["readiness"]["rows"]) == 50
    print(
        f"50-role snapshot: {elapsed * 1000:.2f} ms "
        "(50 roles using codex-main, 500 work periods, >=1000 messages, 100 incidents)"
    )
    assert elapsed < 5  # Catch gross regressions, without a machine-specific microbenchmark limit.


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("/api/roles/tree", "org_tree"),
        ("/api/roles/search?query=analyst", "find_roles"),
        ("/api/roles/founder", "get_role"),
    ],
)
def test_console_discovery_and_enrichment_share_snapshot(tmp_path, monkeypatch, endpoint, method):
    runtime, _, _, app = _system(tmp_path)
    writer = Store.scratch(runtime.store.path)
    original = getattr(runtime.discovery, method)

    def read_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        with writer.transaction() as db:
            db.execute("UPDATE roles SET hold_reason='refusal'")
        return result

    monkeypatch.setattr(runtime.discovery, method, read_then_change)
    with _login(app) as client:
        response = client.get(endpoint)
        assert response.status_code == 200
        result = response.json()
    if method == "get_role":
        facts = [result["owner_facts"]]
    elif method == "org_tree":
        facts = [result["root"], *result["nodes"]]
    else:
        facts = result["roles"]
    assert facts
    assert all(r["hold_reason"] is None for r in facts)


def test_console_identifies_role_waiting_for_retry(tmp_path):
    runtime, founder, _, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        db.execute("UPDATE roles SET retry_at=1234567890 WHERE employee_id=?", (founder,))
    with runtime.store.read() as db:
        before = list(db.iterdump())
    with _login(app) as client:
        held = client.get("/api/snapshot").json()["held"]
        assert held[0]["employee_id"] == founder
        assert held[0]["retry_at"] == 1234567890
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_console_exposes_role_hiring_level_fields(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    with runtime.store.transaction() as db:
        config = json.loads(db.execute("SELECT config_json FROM firm").fetchone()[0])
        config["staffing"] = {"classes": {"high": "default"}}
        db.execute("UPDATE firm SET config_json=?", (json.dumps(config),))
        db.execute("UPDATE roles SET effort='xhigh' WHERE employee_id=?", (analyst,))
        db.execute(
            "INSERT INTO hire_levels(employee_id,state,reason,updated_at) VALUES(?,?,?,?)",
            (analyst, "routed", "owner routed to premium capacity", 0.0),
        )
    with _login(app) as client:
        roles = {r["employee_id"]: r for r in client.get("/api/snapshot").json()["roles"]}
        assert roles[analyst]["level"] == {
            "model_class": "high",
            "effort": "xhigh",
            "state": "routed",
            "reason": "owner routed to premium capacity",
        }
        assert roles[founder]["level"] == {
            "model_class": "high",
            "effort": None,
            "state": "configured",
            "reason": None,
        }


def test_console_counts_waiting_messages_independently_of_wakes_and_execution(tmp_path):
    runtime, founder, analyst, app = _system(tmp_path)
    runtime.start()
    activation = runtime.admit()
    assert activation.employee_id == founder
    runtime.mark_started(activation.activation_id)
    first = runtime.messaging.send(OWNER, [founder], "First", request_id="first")
    runtime.messaging.send(OWNER, [founder], "Second", request_id="second")

    def check(client, expected):
        roles = {role["employee_id"]: role for role in client.get("/api/snapshot").json()["roles"]}
        assert roles[founder]["pending_messages"] == expected
        assert roles[founder]["activation"]["state"] == "running"
        assert roles[analyst]["pending_messages"] == 0
        assert "queued_wakes" not in roles[founder]
        assert client.get("/api/roles/tree").json()["root"]["pending_messages"] == expected
        search = client.get("/api/roles/search?query=founder").json()
        assert search["roles"][0]["pending_messages"] == expected
        detail = client.get(f"/api/roles/{founder}").json()
        assert detail["owner_facts"]["pending_messages"] == expected

    with _login(app) as client:
        check(client, 2)
        runtime.messaging.claim(founder, [first["message_id"]], request_id="claim-first")
        check(client, 1)
    with runtime.store.read() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?", (founder,)
            ).fetchone()[0]
            == 1
        )
