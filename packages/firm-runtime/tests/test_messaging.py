import json

import pytest
from firm_sandbox import Config, Layout, Organization
from firm_sandbox.file_server import FileServer, local_runner

from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import Lifecycle
from firm_runtime.messaging import MAX_BODY_BYTES, MAX_PEEK_BYTES, MAX_RESULT_BYTES, Messaging
from firm_runtime.role_output import render_tool_result
from firm_runtime.store import OPERATIONS, OWNER, Store, encoded  # noqa: F401
from firm_runtime.workspaces import sandbox_role_id


@pytest.fixture
def mailroom(tmp_path):
    now = [1_000.0]
    store = Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0])
    messaging = Messaging(store)
    lifecycle = Lifecycle(store, messaging)
    initialized = lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 4,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )
    founder = initialized["founder"]["employee_id"]
    employees = [
        lifecycle.hire(
            founder,
            slug,
            f"Own {slug} work.",
            "default",
            request_id=f"hire-{slug}",
        )["employee_id"]
        for slug in ("alice", "bob", "carol", "dave")
    ]
    with store.transaction() as db:
        db.execute("DELETE FROM execution_requests")
    return store, messaging, founder, employees, now


def scalar(store, statement, parameters=()):
    with store.read() as db:
        return db.execute(statement, parameters).fetchone()[0]


def attachment_files(messaging, monkeypatch):
    """Provision a real ledger, then forbid guest I/O during attachment validation."""
    root = messaging.store.path.parent / "files"
    (root / "locks").mkdir(parents=True)
    files = FileServer(Config(root, firm_id="test"), local_runner(root / "guest"))
    ledger = files._load()
    ledger["port"] = 445
    files._save(ledger)
    with messaging.store.read() as db:
        for role in db.execute("SELECT employee_id,address FROM roles"):
            files.provision(sandbox_role_id(role["employee_id"]), Organization(role["address"]))
    monkeypatch.setattr(files, "require_ready", lambda: pytest.fail("checked readiness"))
    monkeypatch.setattr(files, "run", lambda request: pytest.fail("contacted guest"))
    messaging.files = files
    return files


def test_role_attachments_map_drive_paths_and_survive_receipts_and_agent_envelopes(
    mailroom, monkeypatch,
):
    store, messaging, founder, (alice, *_), _ = mailroom
    attachment_files(messaging, monkeypatch)
    drive = Layout.for_role(sandbox_role_id(founder)).drive
    paths = [f"{drive}/shared/reports/plan.md", f"{drive}/org/founder/work/draft.txt"]
    expected = [
        {"path": "shared/reports", "name": "plan.md"},
        {"path": "org/founder/work", "name": "draft.txt"},
    ]
    sent = messaging.send(founder, [alice], "Open these.", attachments=paths, request_id="files")
    assert messaging.send(founder, [alice], "Open these.", attachments=paths,
                          request_id="files") == sent
    with pytest.raises(FirmError):
        messaging.send(founder, [alice], "Open these.", attachments=paths[:1],
                       request_id="files")
    with store.read() as db:
        assert store.operation_result(db, founder, "files")["result"] == sent
        stored = db.execute("SELECT attachments_json FROM messages").fetchone()[0]
        assert json.loads(stored) == expected
    assert messaging.messages(alice, [sent["message_id"]])["messages"][0]["attachments"] == expected
    assert messaging.claim(alice, [sent["message_id"]], request_id="claim")["messages"][0][
        "attachments"
    ] == expected
    reply_drive = Layout.for_role(sandbox_role_id(alice)).drive
    reply_path = f"{reply_drive}/org/founder/alice/work/review.md"
    replied = messaging.reply(alice, sent["message_id"], "Reviewed.",
                              attachments=[reply_path], request_id="reply-files")
    assert messaging.messages(founder, [replied["message_id"]])["messages"][0][
        "attachments"
    ] == [{"path": "org/founder/alice/work", "name": "review.md"}]


def test_owner_can_attach_a_file_under_any_browse_root(mailroom, monkeypatch):
    _, messaging, founder, _, _ = mailroom
    attachment_files(messaging, monkeypatch)
    target = messaging.send(founder, [OWNER], "See report", request_id="target")["message_id"]
    attachment = {"path": "org/founder/work/reports", "name": "result.md"}
    sent = messaging.send(OWNER, [founder], "Review", attachments=[attachment],
                          request_id="owner-file")
    replied = messaging.reply(OWNER, target, "Here it is", attachments=[attachment],
                              request_id="owner-reply-file")
    for message_id in (sent["message_id"], replied["message_id"]):
        assert messaging.messages(OWNER, [message_id])["messages"][0]["attachments"] == [
            attachment
        ]
    assert messaging.messages(founder, [target])["messages"][0]["attachments"] == []


@pytest.mark.parametrize("operation", ["send", "reply"])
def test_role_attachments_follow_reporting_line_for_send_and_reply(
    mailroom, monkeypatch, operation,
):
    _, messaging, founder, (alice, bob, *_), _ = mailroom
    attachment_files(messaging, monkeypatch)
    for actor, workspace, allowed in [
        (alice, "org/founder/work", True),  # ancestor
        (founder, "org/founder/alice/work", True),  # descendant
        (alice, "org/founder/alice/work", True),  # own
        (bob, "shared", True),
        (bob, "org/founder/alice/work", False),  # sibling
    ]:
        target = messaging.send(OWNER, [actor], "Review", request_id=f"target-{actor}-{workspace}")
        drive = Layout.for_role(sandbox_role_id(actor)).drive
        # Neither the directory nor the file exists. Sending must not stat either.
        attachment = f"{drive}/{workspace}/not-created/yet.md"
        kwargs = {"attachments": [attachment], "request_id": f"file-{actor}-{workspace}"}

        def send(actor=actor, target=target, kwargs=kwargs):
            if operation == "reply":
                return messaging.reply(actor, target["message_id"], "File", **kwargs)
            return messaging.send(actor, [OWNER], "File", **kwargs)

        if allowed:
            result = send()
            envelope = messaging.messages(actor, [result["message_id"]])["messages"][0]
            assert envelope["attachments"] == [
                {"path": workspace + "/not-created", "name": "yet.md"}
            ]
        else:
            before = scalar(messaging.store, "SELECT COUNT(*) FROM messages")
            with pytest.raises(FirmError, match="readable workspaces"):
                send()
            assert scalar(messaging.store, "SELECT COUNT(*) FROM messages") == before


@pytest.mark.parametrize("archived", [False, True])
def test_attachments_use_ledger_permissions_for_vacated_workspaces(mailroom, monkeypatch, archived):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    files = attachment_files(messaging, monkeypatch)
    alice_role = sandbox_role_id(alice)
    with monkeypatch.context() as setup:
        setup.setattr(files, "run", local_runner(store.path.parent / "files/guest"))
        files.retire_role(alice_role)
        if archived:
            files.provision(
                "r-replacement", Organization("founder/alice"), replace_role_id=alice_role
            )
    work = files.workspace_path(alice_role)
    assert ("/.retired/" in work) == archived
    reference = {"path": work + "/reports", "name": "missing.md"}
    for actor in (founder, bob, OWNER, alice):
        attachment = reference if actor == OWNER else (
            f"{Layout.for_role(sandbox_role_id(actor)).drive}/{reference['path']}/{reference['name']}"
        )
        if actor in (founder, OWNER):
            messaging.send(actor, [OWNER if actor == founder else founder], "File",
                           attachments=[attachment], request_id=f"vacated-{actor}")
        else:
            with pytest.raises(FirmError, match="readable workspaces|no file access"):
                messaging.send(actor, [OWNER], "File", attachments=[attachment],
                               request_id=f"vacated-{actor}")


@pytest.mark.parametrize("suffix", [
    "shared/../secret.md", "shared//file.md", "shared/./file.md",
    "shared/a\\b.md", "private/file.md", "shared/",
])
def test_role_attachment_rejects_invalid_drive_path(mailroom, monkeypatch, suffix):
    store, messaging, founder, _, _ = mailroom
    attachment_files(messaging, monkeypatch)
    drive = Layout.for_role(sandbox_role_id(founder)).drive
    with pytest.raises(FirmError):
        messaging.send(founder, [OWNER], "File", attachments=[f"{drive}/{suffix}"],
                       request_id="bad-file")
    assert scalar(store, "SELECT COUNT(*) FROM messages") == 0


def test_attachment_rejections_cover_outside_drive_invalid_owner_file_and_limit(
    mailroom, monkeypatch,
):
    store, messaging, founder, _, _ = mailroom
    attachment_files(messaging, monkeypatch)
    drive = Layout.for_role(sandbox_role_id(founder)).drive
    bad_role = ["/tmp/file.md", f"{drive}2/shared/file.md", f"{drive}/shared/file.md"] * 4
    bad_owner = [
        {"path": "org/founder/private", "name": "file.md"},
        {"path": "shared", "name": "../file.md"},
        {"path": "shared/../other", "name": "file.md"},
        {"path": "shared", "name": "a/b.md"},
    ]
    for index, attachment in enumerate(bad_role[:2]):
        with pytest.raises(FirmError):
            messaging.send(founder, [OWNER], "File", attachments=[attachment],
                           request_id=f"outside-{index}")
    with pytest.raises(FirmError, match="at most 10"):
        messaging.send(founder, [OWNER], "File", attachments=bad_role,
                       request_id="too-many")
    for index, attachment in enumerate(bad_owner):
        with pytest.raises(FirmError):
            messaging.send(OWNER, [founder], "File", attachments=[attachment],
                           request_id=f"owner-bad-{index}")
    assert scalar(store, "SELECT COUNT(*) FROM messages") == 0


@pytest.mark.parametrize("already_dismissed", [False, True])
def test_owner_reply_dismisses_once_with_its_receipt(mailroom, already_dismissed):
    store, messaging, founder, _, now = mailroom
    target = messaging.send(founder, [OWNER], "Question", request_id="question")["message_id"]
    if already_dismissed:
        messaging.dismiss(OWNER, [target], request_id="done")
    now[0] += 10
    result = messaging.reply(OWNER, target, "Answer", request_id="answer")
    now[0] += 10
    assert messaging.reply(OWNER, target, "Answer", request_id="answer") == result
    with store.read() as db:
        delivery = db.execute(
            "SELECT disposition,disposition_at FROM message_deliveries "
            "WHERE message_id=? AND employee_id=?",
            (target, OWNER),
        ).fetchone()
        assert tuple(delivery) == ("dismissed", 1000 if already_dismissed else 1010)
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    with pytest.raises(FirmError, match="pending"):
        messaging.dismiss(OWNER, [target], request_id="done-again")


def test_failed_owner_reply_rolls_back_delivery_message_and_receipt(mailroom, monkeypatch):
    store, messaging, founder, _, _ = mailroom
    target = messaging.send(founder, [OWNER], "Question", request_id="question")["message_id"]
    write = messaging._write_disposition

    def fail_after_dismiss(*args, **kwargs):
        write(*args, **kwargs)
        raise FirmError("injected reply failure")

    monkeypatch.setattr(messaging, "_write_disposition", fail_after_dismiss)
    with pytest.raises(FirmError, match="injected"):
        messaging.reply(OWNER, target, "Answer", request_id="answer")
    with store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT disposition FROM message_deliveries WHERE employee_id=?", (OWNER,)
            ).fetchone()[0]
            == "pending"
        )
        assert store.operation_result(db, OWNER, "answer") is None
    monkeypatch.setattr(messaging, "_write_disposition", write)
    assert messaging.reply(OWNER, target, "Answer", request_id="answer")["status"] == "sent"


def test_role_reply_leaves_pending_delivery_and_notice_metadata_is_console_only(mailroom):
    store, messaging, founder, (alice, *_), _ = mailroom
    target = messaging.send(founder, [alice], "Question", request_id="question")["message_id"]
    messaging.reply(alice, target, "Answer", request_id="answer")
    with store.transaction() as db:
        assert (
            db.execute(
                "SELECT disposition FROM message_deliveries WHERE message_id=? AND employee_id=?",
                (target, alice),
            ).fetchone()[0]
            == "pending"
        )
        notice = messaging.notice(
            db, [alice], "Status", subject="Status", regarding={"kind": "request", "id": "q1"}
        )
        row = db.execute(
            "SELECT * FROM messages WHERE message_id=?", (notice["message_id"],)
        ).fetchone()
        assert (row["regarding_kind"], row["regarding_id"]) == ("request", "q1")
    assert "regarding" not in messaging.messages(alice, [notice["message_id"]])["messages"][0]


@pytest.mark.parametrize(
    ("reference", "employee_id"),
    [("owner", "owner"), ("runtime", "runtime"), ("founder/alice", "e1"), ("e1", "e1")],
)
def test_send_uses_plain_references_for_all_recipients(mailroom, reference, employee_id):
    _, messaging, founder, _, _ = mailroom
    result = messaging.send(founder, [reference], "Review requested.", request_id="plain-address")
    assert [person["employee_id"] for person in result["queued"]] == [employee_id]
    assert result["failed"] == []

    prefixed = messaging.send(
        founder, [f"@{reference}"], "Review requested.", request_id="prefixed-address"
    )
    assert prefixed["queued"] == []
    assert [person["reference"] for person in prefixed["failed"]] == [f"@{reference}"]


def test_send_is_atomic_idempotent_and_preserves_intended_identity(mailroom):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    result = messaging.send(
        founder,
        [alice, "missing-role", alice],
        "Please review the launch checklist.",
        subject="Launch checklist review",
        request_id="send-1",
    )

    assert result["message_id"] == "m1"
    assert [item["employee_id"] for item in result["queued"]] == [alice]
    assert result["failed"][0]["reference"] == "missing-role"
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == 1
    retry = messaging.send(
        founder,
        [alice, "missing-role", alice],
        "Please review the launch checklist.",
        subject="Launch checklist review",
        request_id="send-1",
    )
    assert retry == result
    assert scalar(store, "SELECT COUNT(*) FROM messages") == 1
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == 1

    with store.transaction() as db:
        original = db.execute(
            "SELECT address,identity FROM roles WHERE employee_id=?", (alice,)
        ).fetchone()
        db.execute(
            "UPDATE roles SET address='renamed',identity='A new display name' WHERE employee_id=?",
            (alice,),
        )
    envelope = messaging.messages(founder, ["m1"])["messages"][0]
    queued = next(item for item in envelope["recipients"] if item["status"] == "queued")
    assert (queued["address"], queued["identity"]) == tuple(original)
    assert {participant.get("reference") for participant in envelope["participants"]} == {
        None,
        "missing-role",
    }
    with store.transaction() as db:
        db.execute("UPDATE roles SET address='missing-role' WHERE employee_id=?", (bob,))
    reply = messaging.reply(
        founder,
        result["message_id"],
        "The unresolved reference must not become a recipient later.",
        request_id="reply",
    )
    assert [item["employee_id"] for item in reply["queued"]] == [alice]


def test_send_and_reply_results_render_queued_and_each_failed_recipient(mailroom):
    store, messaging, founder, (alice, bob, carol, _), _ = mailroom
    sent = messaging.send(
        founder, [alice, bob, "founder/legal", "owner"], "Status?", request_id="render-send"
    )
    assert render_tool_result("send", sent) == (
        "Message m1 queued for founder/alice, founder/bob and Owner.\n"
        "Not delivered to founder/legal: no active or paused role at that address.\n"
        "Use find_roles to find the intended recipient."
    )

    with store.transaction() as db:
        db.execute("UPDATE roles SET employment='retired' WHERE employee_id=?", (bob,))
    reply = messaging.reply(founder, "m1", "Following up.", request_id="render-reply")
    rendered = render_tool_result("reply", reply)
    assert rendered.startswith("Message m2 queued for founder/alice and Owner.\n")
    assert "Not delivered to founder/bob: no active or paused role at that address." in rendered
    assert rendered.endswith("Use find_roles to find the intended recipient.")

    nobody = messaging.send(founder, ["founder/legal"], "Hello?", request_id="render-nobody")
    assert render_tool_result("send", nobody).startswith("Message m3 was queued for no one.\n")
    clean = messaging.send(founder, [carol], "Thanks.", request_id="render-clean")
    assert render_tool_result("send", clean) == "Message m4 queued for founder/carol."


def test_claim_and_dismiss_are_independent_atomic_and_read_only_lookup(mailroom):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    sent = messaging.send(founder, [alice, bob], "A shared request", request_id="send")
    wakes = scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities")
    messaging.peek(alice)
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == wakes

    claimed = messaging.claim(alice, [sent["message_id"]], request_id="claim")
    assert claimed["messages"][0]["body"] == "A shared request"
    assert messaging.claim(alice, [sent["message_id"]], request_id="claim") == claimed
    assert messaging.dismiss(bob, [sent["message_id"]], request_id="dismiss") == {
        "status": "dismissed",
        "message_ids": [sent["message_id"]],
    }
    assert scalar(store, "SELECT COUNT(*) FROM scheduling_opportunities") == wakes - 2

    exact = messaging.messages(bob, [sent["message_id"]])
    assert exact["messages"][0]["body"] == "A shared request"
    with store.read() as db:
        states = db.execute(
            "SELECT employee_id,disposition FROM message_deliveries ORDER BY employee_id"
        ).fetchall()
    assert {row["employee_id"]: row["disposition"] for row in states} == {
        alice: "claimed",
        bob: "dismissed",
    }

    second = messaging.send(founder, [alice], "Another request", request_id="send-2")
    with pytest.raises(FirmError, match="every selected message"):
        messaging.claim(alice, [second["message_id"], "m-does-not-exist"], request_id="bad")
    assert (
        scalar(
            store,
            "SELECT COUNT(*) FROM message_deliveries WHERE message_id=? AND disposition='pending'",
            (second["message_id"],),
        )
        == 1
    )


def test_same_people_share_one_conversation_and_an_addition_starts_the_larger_one(mailroom):
    store, messaging, founder, (alice, bob, carol, dave), _ = mailroom
    opening = messaging.send(founder, [alice, bob], "Initial discussion", request_id="opening")
    again = messaging.send(bob, [founder, alice], "Same people, new topic", request_id="again")
    assert opening["conversation_id"] == again["conversation_id"] == "c1"

    introduced = messaging.reply(
        bob,
        opening["message_id"],
        "Bringing Carol into this discussion.",
        add=[carol],
        request_id="add-carol",
    )
    assert introduced["conversation_id"] == "c2" and introduced["continues"] == "c1"
    assert {item["employee_id"] for item in introduced["queued"]} == {founder, alice, carol}
    with store.read() as db:
        row = db.execute("SELECT * FROM conversations WHERE conversation_id='c2'").fetchone()
    assert row["continues"] == "c1"
    assert {p["employee_id"] for p in json.loads(row["audience_json"])} == {
        founder, alice, bob, carol
    }

    # The original three keep talking in c1; Carol never sees it.
    findings = messaging.reply(
        alice, opening["message_id"], "My findings.", request_id="findings"
    )
    assert findings["conversation_id"] == "c1" and "continues" not in findings
    assert {item["employee_id"] for item in findings["queued"]} == {founder, bob}
    assert messaging.peek(carol)["conversations"][0]["pending_count"] == 1

    # Adding Carol again from c1 lands in the existing c2; adding Dave from c2 makes c3.
    repeat = messaging.reply(
        founder, again["message_id"], "Carol, see this too.", add=[carol], request_id="re-add"
    )
    assert repeat["conversation_id"] == "c2"
    third = messaging.reply(
        carol, introduced["message_id"], "And Dave.", add=[dave], request_id="add-dave"
    )
    assert third["conversation_id"] == "c3" and third["continues"] == "c2"
    with pytest.raises(FirmError, match="member of this conversation"):
        messaging.reply(dave, opening["message_id"], "I was never here.", request_id="outsider")
    envelope = messaging.messages(dave, [third["message_id"]])["messages"][0]
    assert envelope["conversation_id"] == "c3" and envelope["reply_to"] == introduced["message_id"]


def test_peek_lists_conversations_with_waiting_headers_and_claims_nothing(mailroom):
    store, messaging, founder, (alice, bob, *_), now = mailroom
    first = messaging.send(alice, [bob], "Same topic", request_id="one")
    now[0] += 5
    second = messaging.send(alice, [bob], "Another topic", request_id="two")
    owner = messaging.send(OWNER, [bob], "Owner request", request_id="owner")
    with store.transaction() as db:
        operations = messaging.notice(
            db, [bob], "An informational runtime notice.", subject="Runtime information note"
        )

    overview = messaging.peek(bob)
    assert overview["pending_count"] == 4
    assert overview["seen_sequence"] == 4
    rows = overview["conversations"]
    # Operations first, then the owner, then the longest-waiting conversation.
    assert [row["participants"][0]["employee_id"] for row in rows] == [OPERATIONS, OWNER, alice]
    assert [h["message_id"] for h in rows[0]["waiting"]] == [operations["message_id"]]
    assert [h["message_id"] for h in rows[1]["waiting"]] == [owner["message_id"]]
    assert [h["message_id"] for h in rows[2]["waiting"]] == [
        first["message_id"],
        second["message_id"],
    ]
    assert [h["subject"] for h in rows[2]["waiting"]] == ["Same topic", "Another topic"]
    assert rows[2]["pending_count"] == 2 and rows[2]["oldest_age_seconds"] == 5
    assert rows[2]["participant_count"] == 2 and rows[2]["waiting_omitted"] == 0
    rendered = render_tool_result("peek", overview)
    assert f"- {rows[2]['conversation_id']} · with founder/alice ({alice}) · 2 waiting" in rendered
    assert "seen_sequence" not in rendered
    with pytest.raises(FirmError, match="invalid inbox cursor"):
        messaging.peek(bob, cursor="999")
    assert scalar(store, "SELECT COUNT(*) FROM message_deliveries WHERE disposition='pending'") == 4


def test_peek_abbreviates_large_identity_metadata_and_keeps_exact_source(mailroom):
    store, messaging, _, (alice, bob, *_), _ = mailroom
    large = "😀" * 3_500
    with store.transaction() as db:
        db.execute(
            "UPDATE roles SET address=?,identity=? WHERE employee_id=?",
            (large, large, alice),
        )
    sent = messaging.send(alice, [bob], "bounded overview", request_id="large-identity")

    overview = messaging.peek(bob)
    conversation = overview["conversations"][0]
    assert len(json.dumps(overview, ensure_ascii=False).encode()) <= 16 * 1024
    assert conversation["waiting"][0]["message_id"] == sent["message_id"]
    assert conversation["participant_count"] == 2
    assert all(person["identity"].endswith("…") for person in conversation["participants"])
    exact = messaging.messages(bob, [sent["message_id"]])["messages"][0]
    assert any(person.get("identity") == large for person in exact["participants"])


def test_byte_bounded_peek_pages_list_every_conversation_once(mailroom):
    store, messaging, founder, (alice, bob, carol, dave), _ = mailroom
    subject = "😀" * 80
    others = [founder, alice, carol, dave]
    expected = set()
    for bits in range(1, 16):
        members = [person for index, person in enumerate(others) if bits & (1 << index)]
        for index in range(5):
            result = messaging.send(
                members[0],
                [bob, *members[1:]],
                f"Message {index}",
                subject=subject,
                request_id=f"send-{bits}-{index}",
            )
            expected.add(result["conversation_id"])
    assert len(expected) == 15

    cursor = None
    seen = []
    pages = 0
    while True:
        page = messaging.peek(bob, cursor=cursor)
        assert len(encoded(page).encode()) <= MAX_PEEK_BYTES
        seen.extend(row["conversation_id"] for row in page["conversations"])
        assert all(row["pending_count"] == 5 for row in page["conversations"])
        pages += 1
        # Only the page that reaches the end fixes what a later clear may set aside.
        assert (page["seen_sequence"] is None) == (page["next_cursor"] is not None)
        if page["next_cursor"] is None:
            break
        assert len(page["next_cursor"]) <= 512
        cursor = page["next_cursor"]
    assert pages > 1
    assert len(seen) == len(set(seen)) == 15 and set(seen) == expected


def test_peek_cursor_keeps_coverage_across_claims_and_post_snapshot_arrivals(mailroom):
    store, messaging, founder, (alice, bob, carol, dave), _ = mailroom
    senders = [founder, alice, carol, dave]
    sent = {}
    first_original = None
    for bits in range(1, 16):
        members = [person for index, person in enumerate(senders) if bits & (1 << index)]
        result = messaging.send(
            members[0], [bob, *members[1:]], f"Message {bits}", request_id=f"original-{bits}"
        )
        sent[result["conversation_id"]] = result
        if bits == 1:
            first_original = result
    activation(store, bob, "page-activation")
    extra_old_message = messaging.send(
        founder, [bob], "Same thread, second waiting message", request_id="same-thread-old"
    )
    assert extra_old_message["conversation_id"] == first_original["conversation_id"]

    first = messaging.peek(bob, limit=1)
    first_ids = [item["conversation_id"] for item in first["conversations"]]
    claimed_id = first_ids[0]
    claimed_message = sent[claimed_id]["message_id"]
    messaging.claim(bob, [claimed_message], request_id="claim-before-next-page")
    late = messaging.send(
        founder,
        [bob],
        "Arrived after the first page",
        request_id="after-snapshot",
    )
    assert late["conversation_id"] == claimed_id
    late_owner = messaging.send(OWNER, [bob], "Owner priority arrival", request_id="owner-late")
    with store.transaction() as db:
        late_operations = messaging.notice(
            db, [bob], "Operations priority arrival", subject="Operations late"
        )

    seen = list(first_ids)
    cursor = first["next_cursor"]
    while cursor is not None:
        page = messaging.peek(bob, cursor=cursor, limit=1)
        seen.extend(item["conversation_id"] for item in page["conversations"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["seen_sequence"] is not None
            with store.transaction() as db:
                db.execute(
                    "UPDATE activations SET inbox_seen_sequence=? "
                    "WHERE activation_id='page-activation'",
                    (page["seen_sequence"],),
                )

    assert len(seen) > len(set(seen))  # Removing the oldest row may move a shown thread later.
    assert set(seen) == set(sent)
    cleared = messaging.clear(
        bob, request_id="clear-after-traversal", activation_id="page-activation"
    )
    cleared_ids = {
        message_id for row in cleared["conversations"] for message_id in row["message_ids"]
    }
    assert claimed_message not in cleared_ids
    assert extra_old_message["message_id"] in cleared_ids
    assert late["message_id"] not in cleared_ids
    assert late_owner["message_id"] not in cleared_ids
    assert late_operations["message_id"] not in cleared_ids
    assert scalar(
        store,
        "SELECT disposition FROM message_deliveries WHERE employee_id=? AND message_id=?",
        (bob, claimed_message),
    ) == "claimed"
    assert scalar(
        store,
        "SELECT disposition FROM message_deliveries WHERE employee_id=? AND message_id=?",
        (bob, late["message_id"]),
    ) == "pending"
    assert scalar(
        store,
        "SELECT disposition FROM message_deliveries WHERE employee_id=? AND message_id=?",
        (bob, late_owner["message_id"]),
    ) == "pending"
    assert scalar(
        store,
        "SELECT disposition FROM message_deliveries WHERE employee_id=? AND message_id=?",
        (bob, late_operations["message_id"]),
    ) == "pending"


def test_inbox_cursor_is_actor_scoped_signed_and_replay_safe(mailroom):
    store, messaging, founder, (alice, bob, carol, *_), _ = mailroom
    for index, sender in enumerate((founder, alice, carol)):
        messaging.send(sender, [bob], f"Mail {index}", request_id=f"cursor-mail-{index}")
    page = messaging.peek(bob, limit=1)
    cursor = page["next_cursor"]
    assert cursor
    replay_one = messaging.peek(bob, cursor=cursor, limit=1)
    replay_two = messaging.peek(bob, cursor=cursor, limit=1)
    assert replay_one == replay_two
    with pytest.raises(FirmError, match="invalid inbox cursor"):
        messaging.peek(alice, cursor=cursor)
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(FirmError, match="invalid inbox cursor"):
        messaging.peek(bob, cursor=tampered)
    with pytest.raises(FirmError, match="invalid inbox cursor"):
        messaging.peek(bob, cursor="x" * 513)

    activation(store, bob, "old-activation")
    bound_page = messaging.peek(bob, activation_id="old-activation", limit=1)
    bound_cursor = bound_page["next_cursor"]
    assert bound_cursor
    with store.transaction() as db:
        db.execute("UPDATE activations SET state='completed' WHERE activation_id='old-activation'")
    activation(store, bob, "new-activation")
    with pytest.raises(FirmError, match="invalid inbox cursor"):
        messaging.peek(bob, cursor=bound_cursor, activation_id="new-activation")


def test_empty_peek_is_a_complete_single_page(mailroom):
    _, messaging, _, (_, bob, *_), _ = mailroom
    page = messaging.peek(bob)
    assert page["conversations"] == []
    assert page["pending_count"] == 0
    assert page["next_cursor"] is None
    assert page["seen_sequence"] == 0


def test_claim_by_conversation_reads_what_fits_oldest_first_and_reports_the_rest(mailroom):
    store, messaging, _, (alice, bob, *_), _ = mailroom
    expected = [
        messaging.send(alice, [bob], "x" * 6_000, request_id=f"big-{index}")["message_id"]
        for index in range(120)
    ]
    conversation = expected and messaging.peek(bob)["conversations"][0]
    assert conversation["waiting_omitted"] == 115
    read = []
    calls = 0
    while True:
        result = messaging.claim(
            bob, conversation=conversation["conversation_id"], request_id=f"claim-{calls}"
        )
        calls += 1
        if result["status"] != "claimed":
            assert result == {"status": "nothing_claimed", "reason": "nothing_waiting"}
            break
        assert len(encoded(result).encode()) <= MAX_RESULT_BYTES
        assert result["conversation_id"] == conversation["conversation_id"]
        ids = [message["message_id"] for message in result["messages"]]
        assert result["remaining"] == 120 - len(read) - len(ids)
        read.extend(ids)
    assert read == expected and calls > 2
    with pytest.raises(FirmError, match="either message_ids or a conversation"):
        messaging.claim(bob, [expected[0]], conversation="c1", request_id="both")
    with pytest.raises(FirmError, match="unknown conversation"):
        messaging.dismiss(bob, conversation="c99", request_id="missing")


def test_conversation_claim_budget_includes_its_pagination_metadata(mailroom, monkeypatch):
    import firm_runtime.messaging as messaging_module

    _, messaging, founder, (alice, *_), _ = mailroom
    sent = [
        messaging.send(founder, [alice], f"Body {index}", request_id=f"send-{index}")
        for index in range(2)
    ]
    ids = [item["message_id"] for item in sent]
    conversation_id = sent[0]["conversation_id"]
    envelopes = messaging.messages(alice, ids)["messages"]
    envelope_only_size = len(encoded({"status": "claimed", "messages": envelopes}).encode())
    monkeypatch.setattr(messaging_module, "MAX_RESULT_BYTES", envelope_only_size)

    result = messaging.claim(alice, conversation=conversation_id, request_id="claim")

    assert [item["message_id"] for item in result["messages"]] == ids[:1]
    assert result["remaining"] == 1
    assert len(encoded(result).encode()) <= envelope_only_size


def activation(store, employee_id, activation_id, *, seen=None):
    with store.transaction() as db:
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at,inbox_seen_sequence) "
            "VALUES(?,?,'s','running','[]','{}',1000,?)",
            (activation_id, employee_id, seen),
        )


def test_clear_and_conversation_dismiss_cover_only_what_the_last_peek_could_show(mailroom):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    early = [
        messaging.send(alice, [bob], f"early {index}", request_id=f"early-{index}")
        for index in range(2)
    ]
    from_founder = messaging.send(founder, [bob], "also early", request_id="founder")
    activation(store, bob, "a1")
    assert messaging.clear(bob, request_id="too-soon", activation_id="a1") == {
        "status": "nothing_cleared",
        "reason": "no_peek",
    }
    # The peek's seen_sequence is what the runtime records on the activation.
    seen = messaging.peek(bob)["seen_sequence"]
    with store.transaction() as db:
        db.execute("UPDATE activations SET inbox_seen_sequence=? WHERE activation_id='a1'", (seen,))
    late = messaging.send(alice, [bob], "after the peek", request_id="late")

    cleared = messaging.clear(bob, request_id="clear", activation_id="a1")
    assert cleared == {
        "status": "cleared",
        "count": 3,
        "conversations": [
            {
                "conversation_id": early[0]["conversation_id"],
                "message_ids": [m["message_id"] for m in early],
            },
            {
                "conversation_id": from_founder["conversation_id"],
                "message_ids": [from_founder["message_id"]],
            },
        ],
    }
    assert messaging.clear(bob, request_id="clear", activation_id="a1") == cleared
    pending = messaging.peek(bob)["conversations"]
    assert [h["message_id"] for row in pending for h in row["waiting"]] == [late["message_id"]]
    assert messaging.clear(bob, request_id="clear-2", activation_id="a1") == {
        "status": "nothing_cleared",
        "reason": "nothing_waiting",
    }

    # Dismissing a conversation is bounded the same way; claiming it is not.
    dismissed = messaging.dismiss(
        bob, conversation=late["conversation_id"], request_id="dismiss", activation_id="a1"
    )
    assert dismissed == {"status": "nothing_dismissed", "reason": "nothing_waiting"}
    claimed = messaging.claim(
        bob, conversation=late["conversation_id"], request_id="claim", activation_id="a1"
    )
    assert [m["message_id"] for m in claimed["messages"]] == [late["message_id"]]
    assert claimed["remaining"] == 0
    with pytest.raises(FirmError, match="does not authorize"):
        messaging.clear(alice, request_id="wrong-role", activation_id="a1")
    # The owner has no work period and no peek marker, so no bound applies.
    to_owner = messaging.send(alice, [OWNER], "For the owner", request_id="to-owner")
    assert messaging.dismiss(
        OWNER, conversation=to_owner["conversation_id"], request_id="owner-dismiss"
    ) == {
        "status": "dismissed",
        "message_ids": [to_owner["message_id"]],
        "conversation_id": to_owner["conversation_id"],
    }


class RecordingSummaries:
    def __init__(self, outcome):
        self.outcome = outcome
        self.snapshots = []

    def summarize(self, snapshot, *, caller_session_id=None):
        self.snapshots.append((snapshot, caller_session_id))
        return dict(self.outcome)


def test_introduction_carries_a_derived_summary_once_and_context_follows_continues(mailroom):
    store, messaging, founder, (alice, bob, carol, _), _ = mailroom
    summaries = RecordingSummaries({"status": "summarized", "summary": "Derived: two asks."})
    messaging.summary_service = summaries
    opening = messaging.send(founder, [alice, bob], "First ask", request_id="opening")
    messaging.reply(alice, opening["message_id"], "Second ask", request_id="second")
    with pytest.raises(FirmError, match="summarize applies only"):
        messaging.reply(
            bob, opening["message_id"], "No newcomer.", summarize=True, request_id="plain"
        )
    assert summaries.snapshots == []

    introduced = messaging.reply(
        bob,
        opening["message_id"],
        "Carol, joining you in.",
        add=[carol],
        summarize=True,
        request_id="intro",
        session_id="bob-session",
    )
    assert introduced["summary"] == {"status": "attached"}
    assert introduced["continues"] == opening["conversation_id"]
    snapshot, session = summaries.snapshots[0]
    assert session == "bob-session"
    assert snapshot.conversation_id == opening["conversation_id"]
    assert snapshot.source_message_ids == ("m1", "m2")
    retry = messaging.reply(
        bob,
        opening["message_id"],
        "Carol, joining you in.",
        add=[carol],
        summarize=True,
        request_id="intro",
        session_id="bob-session",
    )
    assert retry == introduced and len(summaries.snapshots) == 1

    received = messaging.claim(carol, [introduced["message_id"]], request_id="read")["messages"][0]
    assert received["body"] == "Carol, joining you in."
    assert received["summary"] == "Derived: two asks."
    rendered = render_tool_result("reply", introduced)
    assert "continues c1" in rendered and "derived summary" in rendered.lower()

    # Context on the new conversation reaches back into the one it continues.
    messaging.context(carol, introduced["conversation_id"])
    snapshot, _ = summaries.snapshots[-1]
    assert snapshot.conversation_id == introduced["conversation_id"]
    assert snapshot.source_message_ids == ("m1", "m2", introduced["message_id"])
    assert snapshot.matching_count == 3
    # The conversation's own messages come before its history, however much newer the
    # history is: the original three keep talking, and the introduction still makes the cut.
    for index in range(60):
        messaging.reply(founder, opening["message_id"], f"Later {index}", request_id=f"l{index}")
    messaging.context(carol, introduced["conversation_id"])
    snapshot, _ = summaries.snapshots[-1]
    assert introduced["message_id"] in snapshot.source_message_ids
    assert len(snapshot.source_message_ids) == 50 and snapshot.matching_count == 63
    assert list(snapshot.source_message_ids) == sorted(
        snapshot.source_message_ids, key=lambda m: int(m[1:], 36)
    )
    with pytest.raises(FirmError, match="unknown conversation"):
        messaging.context(carol, "c9")

    summaries.outcome = {"status": "unavailable", "reason": "busy"}
    without = messaging.reply(
        carol, introduced["message_id"], "Dave too.", add=["founder/dave"], summarize=True,
        request_id="no-summary",
    )
    assert without["summary"] == {"status": "unavailable", "reason": "busy"}
    assert "summary" not in messaging.messages(carol, [without["message_id"]])["messages"][0]
    assert "unavailable (busy)" in render_tool_result("reply", without)


def test_near_limit_envelope_is_accepted_and_claimable_alone(mailroom):
    store, messaging, _, (alice, bob, *_), _ = mailroom
    width = 7_100
    with store.transaction() as db:
        db.execute(
            "UPDATE roles SET address=?,identity=? WHERE employee_id=?",
            ("a" * width, "A" * width, alice),
        )
        db.execute(
            "UPDATE roles SET address=?,identity=? WHERE employee_id=?",
            ("b" * width, "B" * width, bob),
        )
    sent = messaging.send(alice, [bob], "near the envelope limit", request_id="near-limit")
    claimed = messaging.claim(bob, [sent["message_id"]], request_id="claim-near-limit")
    rendered = json.dumps(claimed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert MAX_RESULT_BYTES - 3 * 1024 < len(rendered.encode()) <= MAX_RESULT_BYTES


def test_retirement_marks_only_pending_delivery_and_emits_one_bounce(mailroom):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    sent = messaging.send(founder, [alice, bob], "Please coordinate", request_id="send")
    with store.transaction() as db:
        db.execute("UPDATE roles SET employment='retired' WHERE employee_id=?", (alice,))
        db.execute("DELETE FROM execution_requests WHERE employee_id=?", (alice,))
        result = messaging.retire(db, alice)
        repeated = messaging.retire(db, alice)
    assert result["undeliverable"] == 1
    assert repeated["undeliverable"] == 0
    assert len(result["bounces"]) == 1
    assert scalar(store, "SELECT COUNT(*) FROM delivery_bounces") == 1
    with store.read() as db:
        states = db.execute(
            "SELECT employee_id,disposition FROM message_deliveries WHERE message_id=?",
            (sent["message_id"],),
        ).fetchall()
        bounce = db.execute(
            "SELECT author_id,body FROM messages WHERE message_id=?", (result["bounces"][0],)
        ).fetchone()
    assert {row["employee_id"]: row["disposition"] for row in states} == {
        alice: "undeliverable",
        bob: "pending",
    }
    assert bounce["author_id"] == OPERATIONS
    assert sent["message_id"] in bounce["body"]


def test_recent_claims_uses_claim_order_at_equal_timestamps(mailroom):
    store, messaging, founder, (alice, *_), _ = mailroom
    first = messaging.send(founder, [alice], "First sent", request_id="send-1")
    second = messaging.send(founder, [alice], "Second sent", request_id="send-2")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at) VALUES('a1',?,?,'running','[]','{}',1000)",
            (alice, "session"),
        )
    messaging.claim(alice, [second["message_id"]], request_id="claim-2", activation_id="a1")
    with store.transaction() as db:
        db.execute("UPDATE activations SET state='completed' WHERE activation_id='a1'")
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at) VALUES('a2',?,?,'running','[]','{}',1000)",
            (alice, "session"),
        )
    messaging.claim(alice, [first["message_id"]], request_id="claim-1", activation_id="a2")

    with store.read() as db:
        recent = messaging.recent_claims(db, alice, "session")
    assert [item["message_id"] for item in recent] == [
        first["message_id"],
        second["message_id"],
    ]
    assert all(set(item) == {"message_id", "subject", "claimed_at"} for item in recent)


def test_claim_retry_from_a_later_activation_keeps_original_claim_record(mailroom):
    store, messaging, founder, (alice, *_), _ = mailroom
    sent = messaging.send(founder, [alice], "Retry this claim", request_id="send")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at) VALUES('a1',?,?,'running','[]','{}',1000)",
            (alice, "first-session"),
        )
    original = messaging.claim(
        alice, [sent["message_id"]], request_id="same-claim", activation_id="a1"
    )
    with store.transaction() as db:
        db.execute("UPDATE activations SET state='completed' WHERE activation_id='a1'")
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at) VALUES('a2',?,?,'running','[]','{}',1000)",
            (alice, "second-session"),
        )
    retry = messaging.claim(
        alice, [sent["message_id"]], request_id="same-claim", activation_id="a2"
    )
    assert retry == original
    with store.read() as db:
        recorded = db.execute(
            "SELECT claimed_activation_id,claim_sequence FROM message_deliveries "
            "WHERE message_id=? AND employee_id=?",
            (sent["message_id"], alice),
        ).fetchone()
    assert dict(recorded) == {"claimed_activation_id": "a1", "claim_sequence": 1}


def test_bounds_validation_and_base36_ids(mailroom):
    _, messaging, founder, (alice, *_), _ = mailroom
    ids = [
        messaging.send(founder, [alice], f"body {index}", request_id=f"send-{index}")["message_id"]
        for index in range(36)
    ]
    assert ids[34:] == ["mz", "m10"]
    with pytest.raises(FirmError, match="1–80"):
        messaging.send(founder, [alice], "body", subject="", request_id="empty-subject")
    with pytest.raises(FirmError, match="1–80"):
        messaging.send(founder, [alice], "body", subject="s" * 81, request_id="long-subject")
    short = messaging.send(founder, [alice], "body", subject="Push again", request_id="short")
    sent = messaging.messages(founder, [short["message_id"]])["messages"][0]
    assert sent["subject"] == "Push again"
    with pytest.raises(FirmError, match=str(MAX_BODY_BYTES)):
        messaging.send(founder, [alice], "x" * (MAX_BODY_BYTES + 1), request_id="large")
    with pytest.raises(FirmError, match="unique"):
        messaging.messages(founder, [ids[0], ids[0]])
    assert len(encoded(messaging.peek(alice)).encode()) <= MAX_PEEK_BYTES


@pytest.mark.parametrize("operation", ["claim", "dismiss"])
def test_disposition_clears_only_matching_recipient_message_wakes(mailroom, operation):
    store, messaging, founder, (alice, bob, *_), _ = mailroom
    first = messaging.send(founder, [alice, bob], "Shared", request_id="shared")
    second = messaging.send(founder, [alice], "Other", request_id="other")
    with store.transaction() as db:
        store.request_execution(db, alice, "handoff", source_id=first["message_id"])
    getattr(messaging, operation)(alice, [first["message_id"]], request_id="dispose")
    with store.read() as db:
        remaining = {
            tuple(row)
            for row in db.execute(
                "SELECT employee_id,reason,source_id FROM scheduling_opportunities"
            )
        }
    assert remaining == {
        (bob, "message", first["message_id"]),
        (alice, "message", second["message_id"]),
        (alice, "handoff", first["message_id"]),
    }
    with pytest.raises(FirmError):
        getattr(messaging, operation)(
            alice, [second["message_id"], "missing"], request_id="invalid"
        )
    with store.read() as db:
        assert {
            tuple(row)
            for row in db.execute(
                "SELECT employee_id,reason,source_id FROM scheduling_opportunities"
            )
        } == remaining
