import json

import pytest
from firm_sandbox import Config, Layout, Organization
from firm_sandbox.file_server import FileServer, local_runner

from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import Lifecycle
from firm_runtime.messaging import MAX_BODY_BYTES, Messaging
from firm_runtime.role_output import render_tool_result
from firm_runtime.store import OPERATIONS, OWNER, Store, encoded  # noqa: F401
from firm_runtime.summaries import (
    MAX_SUMMARY_TEXT_BYTES,
    SUMMARY_TRUNCATION_MARKER,
    SummaryOutput,
    SummaryService,
)
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
        db.execute("UPDATE message_deliveries SET disposition='dismissed'")
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
    sent = messaging.send(
        founder, [alice], [], "Open these.", attachments=paths, request_id="files",
    )
    assert messaging.send(
        founder, [alice], [], "Open these.", attachments=paths, request_id="files",
    ) == sent
    with pytest.raises(FirmError):
        messaging.send(
            founder, [alice], [], "Open these.", attachments=paths[:1], request_id="files",
        )
    with store.read() as db:
        assert store.operation_result(db, founder, "files")["result"] == sent
        stored = db.execute("SELECT attachments_json FROM messages").fetchone()[0]
        assert json.loads(stored) == expected
    assert messaging.read(alice, sent["message_id"])["messages"][0]["attachments"] == expected
    assert messaging.read(alice, sent["message_id"])["messages"][0][
        "attachments"
    ] == expected
    reply_drive = Layout.for_role(sandbox_role_id(alice)).drive
    reply_path = f"{reply_drive}/org/founder/alice/work/review.md"
    replied = messaging.reply(
        alice, sent["message_id"], [], "Reviewed.", attachments=[reply_path],
        request_id="reply-files",
    )
    assert messaging.read(founder, replied["message_id"])["messages"][0][
        "attachments"
    ] == [{"path": "org/founder/alice/work", "name": "review.md"}]


def test_owner_can_attach_a_file_under_any_browse_root(mailroom, monkeypatch):
    _, messaging, founder, _, _ = mailroom
    attachment_files(messaging, monkeypatch)
    target = messaging.send(founder, [OWNER], [], "See report", request_id="target")["message_id"]
    attachment = {"path": "org/founder/work/reports", "name": "result.md"}
    sent = messaging.send(
        OWNER, [founder], [], "Review", attachments=[attachment], request_id="owner-file",
    )
    replied = messaging.reply(
        OWNER, target, [], "Here it is", attachments=[attachment], request_id="owner-reply-file",
    )
    for message_id in (sent["message_id"], replied["message_id"]):
        assert messaging.read(OWNER, message_id)["messages"][0]["attachments"] == [
            attachment
        ]
    assert messaging.read(founder, target)["messages"][0]["attachments"] == []


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
        target = messaging.send(
            OWNER, [actor], [], "Review", request_id=f"target-{actor}-{workspace}",
        )
        drive = Layout.for_role(sandbox_role_id(actor)).drive
        # Neither the directory nor the file exists. Sending must not stat either.
        attachment = f"{drive}/{workspace}/not-created/yet.md"
        kwargs = {"attachments": [attachment], "request_id": f"file-{actor}-{workspace}"}

        def send(actor=actor, target=target, kwargs=kwargs):
            if operation == "reply":
                return messaging.reply(actor, target["message_id"], [], "File", **kwargs)
            return messaging.send(actor, [OWNER], [], "File", **kwargs)

        if allowed:
            result = send()
            messaging.read(actor, target["message_id"])
            envelope = messaging.read(actor, result["message_id"])["messages"][-1]
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
            messaging.send(
                actor, [OWNER if actor == founder else founder], [], "File",
                attachments=[attachment], request_id=f"vacated-{actor}",
            )
        else:
            with pytest.raises(FirmError, match="readable workspaces|no file access"):
                messaging.send(
                    actor, [OWNER], [], "File", attachments=[attachment],
                    request_id=f"vacated-{actor}",
                )


@pytest.mark.parametrize("suffix", [
    "shared/../secret.md", "shared//file.md", "shared/./file.md",
    "shared/a\\b.md", "private/file.md", "shared/",
])
def test_role_attachment_rejects_invalid_drive_path(mailroom, monkeypatch, suffix):
    store, messaging, founder, _, _ = mailroom
    attachment_files(messaging, monkeypatch)
    drive = Layout.for_role(sandbox_role_id(founder)).drive
    with pytest.raises(FirmError):
        messaging.send(
            founder, [OWNER], [], "File", attachments=[f"{drive}/{suffix}"],
            request_id="bad-file",
        )
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
            messaging.send(
                founder, [OWNER], [], "File", attachments=[attachment],
                request_id=f"outside-{index}",
            )
    with pytest.raises(FirmError, match="at most 10"):
        messaging.send(founder, [OWNER], [], "File", attachments=bad_role, request_id="too-many")
    for index, attachment in enumerate(bad_owner):
        with pytest.raises(FirmError):
            messaging.send(
                OWNER, [founder], [], "File", attachments=[attachment],
                request_id=f"owner-bad-{index}",
            )
    assert scalar(store, "SELECT COUNT(*) FROM messages") == 0




def message(messaging, actor, mid):
    return messaging.read(actor, mid)["messages"][-1]


def states(store, actor):
    with store.read() as db:
        return {r["message_id"]: r["disposition"] for r in db.execute(
            "SELECT message_id,disposition FROM message_deliveries WHERE employee_id=?", (actor,)
        )}


def summary_provider(messaging, callback=None, text="Derived: the author asks for a decision."):
    class Runner:
        def start(self, request):
            return request

        def wait(self, handle, timeout):
            return SummaryOutput(text)

        def stop(self, handle):
            return True

    class Service(SummaryService):
        def summarize(self, snapshot, **kwargs):
            if callback:
                callback(snapshot)
            return super().summarize(snapshot, **kwargs)

    messaging.summary_service = Service(provider=Runner())


def test_thread_creation_and_immutable_subject(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    a = mail.send(founder, [alice], [], "First", request_id="a")
    b = mail.send(alice, [], [founder], "Second", request_id="b")
    assert a["conversation_id"] == b["conversation_id"]
    named = [mail.send(founder, [alice], [], "Topic", subject="Fixed", request_id=f"n{i}")
             for i in range(2)]
    assert len({r["conversation_id"] for r in [a, *named]}) == 3
    reply = mail.reply(alice, named[0]["message_id"], [], "Answer", request_id="r")
    assert reply["conversation_id"] == named[0]["conversation_id"]
    assert message(mail, founder, reply["message_id"])["subject"] == "Fixed"
    assert message(mail, founder, a["message_id"])["subject"] is None
    with store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE audience_key IN "
                          "(SELECT audience_key FROM conversations WHERE conversation_id=?) "
                          "AND subject IS NULL", (a["conversation_id"],)).fetchone()[0] == 1


def test_send_lists_are_explicit_disjoint_by_person_and_schedule_only_action(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    with pytest.raises(TypeError):
        mail.send(founder, [alice], "Missing FYI", request_id="missing")
    for actions, fyi in [([], []), ([alice], ["founder/alice"]), (None, [alice])]:
        with pytest.raises(FirmError):
            mail.send(founder, actions, fyi, "Invalid", request_id=str(actions))
    action = mail.send(founder, [alice, "founder/alice", alice], [bob], "Act", request_id="act")
    quiet = mail.send(founder, [], [alice], "FYI", request_id="quiet")
    with store.read() as db:
        rows = db.execute("SELECT employee_id,scheduling_ready_at FROM message_deliveries "
                          "WHERE message_id=?", (action["message_id"],)).fetchall()
        assert {r["employee_id"]: r["scheduling_ready_at"] is not None for r in rows} == {
            alice: True, bob: False,
        }
        assert db.execute("SELECT scheduling_ready_at FROM message_deliveries WHERE message_id=?",
                          (quiet["message_id"],)).fetchone()[0] is None
    assert len(action["queued"]) == 2


def test_reply_inherits_audience_fresh_action_and_does_not_read(mailroom):
    store, mail, founder, (alice, bob, carol, *_), _ = mailroom
    target = mail.send(founder, [alice], [bob], "Question", subject="Topic", request_id="target")
    with pytest.raises(TypeError):
        mail.reply(alice, target["message_id"], body="Missing action", request_id="missing")
    with pytest.raises(FirmError, match="recipient"):
        mail.reply(alice, target["message_id"], [carol], "Bad", request_id="bad")
    reply = mail.reply(alice, target["message_id"], [bob], "Answer", request_id="reply")
    assert states(store, alice)[target["message_id"]] == "pending"
    assert {r["employee_id"]: r["action_required"] for r in reply["queued"]} == {
        founder: False, bob: True,
    }
    assert message(mail, founder, reply["message_id"])["subject"] == "Topic"
    assert reply["conversation_id"] == target["conversation_id"]


@pytest.mark.parametrize("operation", ["read", "dismiss", "clear", "reply", "introduce"])
def test_thread_operations_share_participant_authorization(mailroom, operation):
    _, mail, founder, (alice, bob, carol, *_), _ = mailroom
    target = mail.send(founder, [alice], [], "Private", request_id="private")
    def call(mid):
        if operation == "reply":
            return mail.reply(bob, mid, [], "No", request_id="reply")
        if operation == "introduce":
            return mail.introduce(bob, mid, [carol], [], False, "No", request_id="intro")
        return getattr(mail, operation)(bob, mid)
    with pytest.raises(FirmError, match="not a participant in that message's thread"):
        call(target["message_id"])
    with pytest.raises(FirmError, match="unknown message ID"):
        call("munknown")


@pytest.mark.parametrize("subject", [None, "Named"])
def test_introduce_expands_group_and_leaves_source_unread(mailroom, subject):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", subject=subject, request_id="source")
    expanded = mail.send(founder, [], [alice, bob], "Earlier", request_id="expanded")
    result = mail.introduce(alice, source["message_id"], [bob], [bob], False,
                            "Join us", request_id="intro")
    if subject is None:
        assert result["conversation_id"] == expanded["conversation_id"]
        assert result["continues"] is None
    else:
        assert result["conversation_id"] != expanded["conversation_id"]
        assert result["continues"] == source["conversation_id"]
    assert states(store, alice)[source["message_id"]] == "pending"
    assert message(mail, bob, result["message_id"])["subject"] == subject
    with pytest.raises(FirmError, match="participant"):
        mail.read(bob, source["message_id"])


def test_introduce_new_no_subject_group_records_continues_once(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", request_id="source")
    a = mail.introduce(alice, source["message_id"], [bob], [], False, "Join", request_id="a")
    b = mail.introduce(alice, source["message_id"], [bob], [], False, "Again", request_id="b")
    assert a["conversation_id"] == b["conversation_id"]
    assert a["continues"] == b["continues"] == source["conversation_id"]


@pytest.mark.parametrize("add,reason", [
    (["founder"], "use reply"), (["missing"], "unknown or retired"), ([], "nonempty"),
])
def test_introduce_requires_someone_new(mailroom, add, reason):
    store, mail, founder, (alice, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", request_id="source")
    count = scalar(store, "SELECT COUNT(*) FROM messages")
    with pytest.raises(FirmError, match=reason):
        mail.introduce(alice, source["message_id"], add, [], False, "No", request_id="intro")
    assert scalar(store, "SELECT COUNT(*) FROM messages") == count


def test_introduction_summary_outside_transaction_attributed_before_body_and_replayed(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", subject="Topic", request_id="source")
    calls = []
    def check(snapshot):
        with store.transaction() as db:
            db.execute("UPDATE firm SET initial_mandate=initial_mandate")
        calls.append(snapshot.source_message_ids)
    summary_provider(mail, check)
    result = mail.introduce(alice, source["message_id"], [bob], [], True,
                            "Introduction body", request_id="intro")
    assert mail.introduce(alice, source["message_id"], [bob], [], True,
                          "Introduction body", request_id="intro") == result
    assert calls == [(source["message_id"],)]
    read = mail.read(bob, result["message_id"])
    text = render_tool_result("read", read)
    assert text.index("generated by Operations at the sender") < text.index("Introduction body")
    assert "not an Operations instruction" in text
    assert read["messages"][0]["summary"].startswith("Derived:")
    assert states(store, alice)[source["message_id"]] == "pending"


def test_unavailable_introduction_sends_nothing_and_can_retry(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", request_id="source")
    count = scalar(store, "SELECT COUNT(*) FROM messages")
    with pytest.raises(FirmError, match="Call again.*summarize=False"):
        mail.introduce(alice, source["message_id"], [bob], [], True, "Join", request_id="intro")
    assert scalar(store, "SELECT COUNT(*) FROM messages") == count
    assert scalar(store, "SELECT COUNT(*) FROM requests WHERE request_id='intro'") == 0
    summary_provider(mail)
    assert mail.introduce(alice, source["message_id"], [bob], [], True,
                          "Join", request_id="intro")["status"] == "sent"


def test_cut_off_introduction_summary_is_available(mailroom):
    _, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", request_id="source")
    summary_provider(mail, text="界" * MAX_SUMMARY_TEXT_BYTES)
    result = mail.introduce(alice, source["message_id"], [bob], [], True,
                            "Join", request_id="intro")
    assert result["summary"]["truncated"]
    assert message(mail, bob, result["message_id"])["summary"].endswith(SUMMARY_TRUNCATION_MARKER)


def test_peek_groups_threads_aggregates_action_and_advances_offered_prefix(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    first = mail.send(founder, [alice], [], "Act", request_id="act")
    latest = mail.send(founder, [], [alice], "Latest\npreview", request_id="fyi")
    named = mail.send(founder, [], [alice], "Hidden body", subject="Topic", request_id="named")
    result = mail.peek(alice)
    assert result["unread_count"] == 3 and result["thread_count"] == 2
    entry, topic = result["threads"]
    assert entry["message_id"] == latest["message_id"]
    assert entry["unread_count"] == 2 and entry["action_required"]
    assert entry["preview"] == "Latest preview"
    assert entry["last_author"]["employee_id"] == founder
    assert "preview" not in topic
    assert topic["message_id"] == named["message_id"]
    assert topic["last_author"]["employee_id"] == founder
    assert result["newest_message_id"] == named["message_id"]
    assert states(store, alice)[first["message_id"]] == "pending"
    assert scalar(store, "SELECT last_offered_message_sequence FROM roles WHERE employee_id=?",
                  (alice,)) == scalar(store, "SELECT sequence FROM messages WHERE message_id=?",
                                    (named["message_id"],))
    text = render_tool_result("peek", result)
    assert "2 threads · 3 unread messages" in text
    assert "No subject · Action · 2 unread" in text
    assert "Last message: founder: Latest preview" in text
    assert f"newest {named['message_id']}." in text
    assert "conversation" not in text and first["conversation_id"] not in text


def test_peek_budget_omits_threads_and_offers_only_shown(mailroom, monkeypatch):
    import firm_runtime.messaging as module
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [], [alice], str(i), subject="Topic", request_id=f"s{i}")
            for i in range(20)]
    monkeypatch.setattr(module, "MAX_PEEK_BYTES", 1500)
    result = mail.peek(alice)
    assert 0 < len(result["threads"]) < 20
    assert result["omitted_threads"] == 20 - len(result["threads"])
    assert result["unread_count"] == 20
    assert result["newest_message_id"] == sent[-1]["message_id"]
    assert result["newest_message_id"] not in [t["message_id"] for t in result["threads"]]
    assert f"newest {sent[-1]['message_id']}." in render_tool_result("peek", result)
    assert "appear as the listed ones are read or dismissed" in render_tool_result("peek", result)
    last = result["threads"][-1]["message_id"]
    assert scalar(store, "SELECT last_offered_message_sequence FROM roles WHERE employee_id=?",
                  (alice,)) == scalar(
                      store, "SELECT sequence FROM messages WHERE message_id=?", (last,),
                  )
    for thread in result["threads"]:
        mail.dismiss(alice, thread["message_id"])
    next_id = sent[len(result["threads"])]["message_id"]
    assert mail.peek(alice)["threads"][0]["message_id"] == next_id


@pytest.mark.parametrize("subject", [None, "Topic"])
def test_peek_last_author_is_the_latest_unread_sender(mailroom, subject):
    _, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [bob], "Start", subject=subject, request_id="source")
    latest = mail.reply(bob, source["message_id"], [], "Latest body", request_id="latest")
    result = mail.peek(alice)
    assert result["threads"][0]["last_author"]["employee_id"] == bob
    assert result["threads"][0]["message_id"] == latest["message_id"]
    text = render_tool_result("peek", result)
    label = f"Last message: founder/bob ({bob})"
    assert "      " + label + (": Latest body" if subject is None else "") in text.splitlines()


def test_peek_ranks_operations_then_owner_then_oldest(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    ordinary = mail.send(founder, [alice], [], "Oldest", request_id="old")
    owner = mail.send(OWNER, [alice], [], "Owner", request_id="owner")
    with store.transaction() as db:
        operations = mail.notice(db, [alice], "Control", subject="Control")
    assert [t["message_id"] for t in mail.peek(alice)["threads"]] == [
        operations["message_id"], owner["message_id"], ordinary["message_id"],
    ]


def test_read_boundaries_batch_continuation_reread_and_other_threads(mailroom, monkeypatch):
    import firm_runtime.messaging as module
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], f"Body {i}", request_id=f"s{i}") for i in range(4)]
    other = mail.send(founder, [alice], [], "Other", subject="Other", request_id="other")
    monkeypatch.setattr(module, "MAX_SELECTION", 1)
    boundary = sent[2]["message_id"]
    for index in range(3):
        result = mail.read(alice, boundary)
        assert result["marked_read"] == [sent[index]["message_id"]]
        assert result["remaining"] == 2 - index
        assert result["messages"][0]["body"] == f"Body {index}"
        if result["remaining"]:
            assert f'Continue: read(message_id="{boundary}")' in render_tool_result("read", result)
    assert states(store, alice)[sent[3]["message_id"]] == "pending"
    assert states(store, alice)[other["message_id"]] == "pending"
    again = mail.read(alice, boundary)
    assert again["reread"] and again["marked_read"] == []
    assert "Already read." in render_tool_result("read", again)
    assert "Sent by you." in render_tool_result("read", mail.read(founder, boundary))


def test_full_read_uses_detail_byte_budget(mailroom, monkeypatch):
    import firm_runtime.messaging as module
    _, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], "x" * 1000, request_id=f"s{i}") for i in range(3)]
    monkeypatch.setattr(module, "MAX_RESULT_BYTES", 2800)
    result = mail.read(alice, sent[-1]["message_id"])
    assert len(result["marked_read"]) == 1 and result["remaining"] == 2
    assert len(encoded(result).encode()) <= 2800


def test_summary_reads_only_covered_messages_and_reread_is_newest_first(mailroom, monkeypatch):
    import firm_runtime.messaging as module
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], f"Body {i}", request_id=f"s{i}") for i in range(5)]
    snapshots = []
    summary_provider(mail, lambda s: snapshots.append(s))
    monkeypatch.setattr(module, "MAX_CONTEXT_MESSAGES", 2)
    boundary = sent[3]["message_id"]
    for expected in [sent[:2], sent[2:4]]:
        result = mail.read(alice, boundary, summarize=True)
        assert result["marked_read"] == [r["message_id"] for r in expected]
        assert result["covered_ids"] == result["marked_read"]
    assert states(store, alice)[sent[4]["message_id"]] == "pending"
    reread = mail.read(alice, boundary, summarize=True)
    assert reread["marked_read"] == [] and reread["reread"]
    assert reread["covered_ids"] == [r["message_id"] for r in sent[2:4]]
    assert all(len(s.messages) == 2 for s in snapshots)


def test_summary_byte_limit_complete_envelopes_and_failure_changes_nothing(mailroom, monkeypatch):
    import firm_runtime.messaging as module
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], "x" * 1000, request_id=f"s{i}") for i in range(3)]
    boundary = sent[-1]["message_id"]
    with pytest.raises(FirmError, match="nothing was marked read.*returns the full messages"):
        mail.read(alice, boundary, summarize=True)
    assert all(states(store, alice)[r["message_id"]] == "pending" for r in sent)
    snapshots = []
    summary_provider(mail, lambda s: snapshots.append(s))
    monkeypatch.setattr(module, "MAX_CONTEXT_BYTES", 2500)
    result = mail.read(alice, boundary, summarize=True)
    assert result["marked_read"] == [sent[0]["message_id"]]
    assert result["remaining"] == 2
    assert snapshots[0].messages[0]["body"] == "x" * 1000


def test_dismiss_boundary_requires_no_peek_and_is_idempotent(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], str(i), request_id=f"s{i}") for i in range(3)]
    result = mail.dismiss(alice, sent[1]["message_id"])
    assert result["message_ids"] == [r["message_id"] for r in sent[:2]]
    assert states(store, alice)[sent[-1]["message_id"]] == "pending"
    assert mail.dismiss(alice, sent[1]["message_id"])["message_ids"] == []
    assert "No unread mail" in render_tool_result(
        "dismiss", mail.dismiss(alice, sent[1]["message_id"]),
    )


def test_owner_reply_and_exact_dismiss_touch_only_target_with_receipts(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [OWNER], [], str(i), request_id=f"s{i}") for i in range(3)]
    result = mail.reply(OWNER, sent[1]["message_id"], [founder], "Answer", request_id="reply")
    assert mail.reply(
        OWNER, sent[1]["message_id"], [founder], "Answer", request_id="reply",
    ) == result
    assert [states(store, OWNER)[r["message_id"]] for r in sent] == [
        "pending", "dismissed", "pending",
    ]
    dismissed = mail.dismiss_exact(OWNER, [sent[-1]["message_id"]], request_id="done")
    assert mail.dismiss_exact(OWNER, [sent[-1]["message_id"]], request_id="done") == dismissed
    assert states(store, OWNER)[sent[0]["message_id"]] == "pending"


def test_retirement_only_bounces_unread_and_notifies_with_preview(mailroom, monkeypatch):
    store, mail, founder, (alice, *_), _ = mailroom
    notified = []
    monkeypatch.setattr(mail, "notify_owner", lambda db, title, body, **kw: notified.append(body))
    mail.send(founder, [], [OWNER], "Body\npreview", request_id="owner")
    assert notified == ["Body preview"]
    read = mail.send(founder, [alice], [], "Read", request_id="read")
    mail.read(alice, read["message_id"])
    unread = mail.send(founder, [alice], [], "Unread", request_id="unread")
    with store.transaction() as db:
        retired = mail.retire(db, alice)
        assert len(retired["bounces"]) == 1
        assert mail.retire(db, alice)["bounces"] == []
    assert states(store, alice)[read["message_id"]] == "read"
    assert states(store, alice)[unread["message_id"]] == "undeliverable"


def test_validation_and_failed_recipients_remain_inline(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    for body, subject in [("", None), ("x" * (MAX_BODY_BYTES + 1), None), ("X", ""),
                          ("X", "x" * 81)]:
        with pytest.raises(FirmError):
            mail.send(
                founder, [alice], [], body, subject=subject,
                request_id=repr((body, subject))[:80],
            )
    result = mail.send(founder, [alice, "unknown"], [], "Work", request_id="partial")
    assert result["failed"][0]["reference"] == "unknown"
    text = render_tool_result("send", result)
    assert "Not delivered to unknown" in text
    assert "find_roles" in text


def test_summary_marks_only_sources_still_unread_after_provider_returns(mailroom, monkeypatch):
    store, mail, founder, (alice, *_), _ = mailroom
    sent = [mail.send(founder, [alice], [], str(i), request_id=f"s{i}") for i in range(3)]
    later = []

    def summarize(snapshot, session_id):
        mail.dismiss(alice, sent[0]["message_id"])
        later.append(mail.send(founder, [alice], [], "Later", request_id="later"))
        return {"status": "summarized", "summary": "Derived summary."}

    monkeypatch.setattr(mail, "_summarize", summarize)
    result = mail.read(alice, sent[-1]["message_id"], summarize=True)
    assert result["covered_ids"] == [r["message_id"] for r in sent]
    assert result["marked_read"] == [r["message_id"] for r in sent[1:]]
    assert states(store, alice)[sent[0]["message_id"]] == "dismissed"
    assert states(store, alice)[later[0]["message_id"]] == "pending"


def test_operations_notices_share_no_subject_conversation_and_read_together(mailroom):
    store, mail, founder, *_ = mailroom
    with store.transaction() as db:
        a = mail.notice(db, [founder], "First notice", subject="First title")
        b = mail.notice(db, [founder], "Second notice", subject="Second title")
    assert a["conversation_id"] == b["conversation_id"]
    peek = mail.peek(founder)
    assert peek["thread_count"] == 1
    assert peek["threads"][0]["unread_count"] == 2
    assert peek["threads"][0]["subject"] is None
    assert peek["threads"][0]["last_author"]["employee_id"] == OPERATIONS
    assert "Last message: Operations: Second title Second notice" in render_tool_result(
        "peek", peek,
    )
    read = mail.read(founder, b["message_id"])
    assert read["marked_read"] == [a["message_id"], b["message_id"]]
    assert [m["body"] for m in read["messages"]] == [
        "First title\n\nFirst notice", "Second title\n\nSecond notice",
    ]
    assert all(m["subject"] is None for m in read["messages"])


def test_notice_combined_body_limit_and_owner_notification_title(mailroom, monkeypatch):
    store, mail, founder, *_ = mailroom
    notified = []
    monkeypatch.setattr(mail, "notify_owner", lambda db, title, body, **kw: notified.append(body))
    with store.transaction() as db:
        mail.notice(db, [OWNER], "Notice body", subject="Notice title", wake=False)
        with pytest.raises(FirmError, match="message body exceeds"):
            mail.notice(db, [founder], "x" * (MAX_BODY_BYTES - 2), subject="Title")
    assert notified == ["Notice title"]


def test_clear_across_threads_without_peek_preserves_later_and_other_readers(mailroom):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    sent = [mail.send(founder, [alice, bob], [], str(i),
                      subject="Topic" if i % 2 else None, request_id=f"clear-{i}")
            for i in range(40)]
    mail.read(alice, sent[0]["message_id"])
    boundary = sent[36]["message_id"]
    result = mail.clear(alice, boundary)
    assert result == {"status": "cleared", "message_id": boundary,
                      "cleared_count": 36, "thread_count": 19, "remaining": 3}
    assert render_tool_result("clear", result) == (
        f"Cleared 36 unread messages in 19 threads through {boundary}.\nUnread remaining: 3"
    )
    assert [states(store, alice)[m["message_id"]] for m in sent] == (
        ["read"] + ["dismissed"] * 36 + ["pending"] * 3
    )
    assert all(states(store, bob)[m["message_id"]] == "pending" for m in sent)
    empty = mail.clear(alice, boundary)
    assert empty["cleared_count"] == empty["thread_count"] == 0
    assert empty["remaining"] == 3
    assert render_tool_result("clear", empty) == f"Nothing unread through {boundary}."


def test_clear_accepts_a_boundary_authored_by_caller(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    first = mail.send(founder, [alice], [], "Question", request_id="question")
    boundary = mail.reply(alice, first["message_id"], [], "Answer", request_id="answer")
    later = mail.send(founder, [], [alice], "Later", request_id="later")
    result = mail.clear(alice, boundary["message_id"])
    assert result["cleared_count"] == result["thread_count"] == result["remaining"] == 1
    assert render_tool_result("clear", result) == (
        f"Cleared 1 unread message in 1 thread through {boundary['message_id']}.\n"
        "Unread remaining: 1"
    )
    assert states(store, alice)[first["message_id"]] == "dismissed"
    assert states(store, alice)[later["message_id"]] == "pending"


def test_idle_entries_include_older_unread_through_offered_boundary_in_offer_order(mailroom):
    store, mail, founder, (alice, *_), now = mailroom
    old = mail.send(founder, [alice], [], "Older action", subject="Topic", request_id="old")
    hidden = mail.send(OWNER, [], [alice], "Already offered", request_id="hidden")
    mail.peek(alice)
    ordinary = mail.send(founder, [], [alice], "Oldest new", request_id="new")
    latest = mail.reply(founder, old["message_id"], [], "New FYI", request_id="fyi")
    with store.transaction() as db:
        later = mail.notice(db, [alice], "Future", subject="Scheduled", ready_at=now[0] + 60)
    with store.read() as db:
        result = mail.idle_snapshot(db, alice, now[0])
    assert [t["message_id"] for t in result["threads"]] == [
        ordinary["message_id"], latest["message_id"],
    ]
    assert result["unread_count"] == 3
    assert result["threads"][1]["unread_count"] == 2
    assert result["threads"][1]["action_required"]
    assert result["threads"][1]["last_author"]["employee_id"] == founder
    text = render_tool_result("peek", result)
    assert "newest" not in text
    assert "Last message: founder" in text
    assert "Topic · Action · 2 unread" in text
    assert hidden["message_id"] not in [t["message_id"] for t in result["threads"]]
    assert later["message_id"] not in [t["message_id"] for t in result["threads"]]
    assert len(mail.read(alice, latest["message_id"])["marked_read"]) == 2


def test_summary_programming_errors_propagate_without_marking_read(mailroom, monkeypatch):
    store, mail, founder, (alice, bob, *_), _ = mailroom
    source = mail.send(founder, [alice], [], "Source", request_id="source")

    def broken(*args, **kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(mail.summary_service, "summarize", broken)
    with pytest.raises(RuntimeError, match="programming error"):
        mail.read(alice, source["message_id"], summarize=True)
    with pytest.raises(RuntimeError, match="programming error"):
        mail.introduce(alice, source["message_id"], [bob], [], True, "Join", request_id="intro")
    assert states(store, alice)[source["message_id"]] == "pending"
    assert scalar(store, "SELECT COUNT(*) FROM messages") == 1


def test_sequence_counters_keep_single_row_across_rollbacks_and_reopen(mailroom):
    store, mail, founder, (alice, *_), _ = mailroom
    first = mail.send(founder, [alice], [], "First", request_id="s1")
    with pytest.raises(RuntimeError):
        with store.transaction():
            mail.send(founder, [alice], [], "Rolled back", request_id="s2")
            raise RuntimeError("abort")
    mail.read(alice, first["message_id"])
    reopened = Messaging(Store.scratch(store.path))
    second = reopened.send(founder, [alice], [], "Second", request_id="s3")
    assert (first["message_id"], second["message_id"]) == ("m1", "m2")
    assert scalar(store, "SELECT COUNT(*) FROM message_sequence") == 1
    assert scalar(store, "SELECT COUNT(*) FROM message_read_sequence") == 1
    assert scalar(store, "SELECT sequence FROM message_read_sequence") == 1
