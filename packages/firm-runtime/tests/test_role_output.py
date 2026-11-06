from firm_runtime.role_output import render_inbox, render_tool_result


def test_context_notices_render_as_plain_lines_ahead_of_awareness():
    empty = {"pending_count": 0, "conversations": [], "next_cursor": None}
    observed = render_inbox({
        **empty,
        "context": {
            "notices": ["The Mac you hold is low on disk: about 5.0 GiB free."],
            "self": {}, "direct_reports": [],
        },
    })
    assert observed == "The Mac you hold is low on disk: about 5.0 GiB free."
    # A non-string notice is not a hook another producer could smuggle a control into.
    assert render_inbox({**empty, "context": {"notices": [{"level": "handoff_required"}]}}) == ""


def test_empty_inbox_omits_routine_counters_but_preserves_stop_and_pressure_controls():
    empty = {"pending_count": 0, "conversations": [], "next_cursor": None}
    assert render_inbox(empty) == ""
    assert (
        render_inbox(
            {**empty, "context_pressure": {"level": "normal", "provider_context_window": 1000000}}
        )
        == ""
    )
    observed = render_inbox(
        {
            **empty,
            "context_pressure": {
                "level": "handoff_required",
                "instruction": "Save progress and hand off now.",
                "current_context_tokens": 90000,
                "provider_context_window": 100000,
            },
            "operations_control": {"kind": "drain", "instruction": "The owner is closing work."},
        }
    )
    assert "Save progress and hand off now." in observed
    assert "The owner is closing work." in observed
    assert "provider" not in observed
    assert "90000" not in observed


def test_mail_retains_source_identity_audience_and_exact_body_without_runtime_metadata():
    body = "We are evaluating Claude for the client.\n\nKeep this exact text."
    rendered = render_tool_result(
        "claim",
        {
            "status": "claimed",
            "messages": [
                {
                    "message_id": "m7",
                    "conversation_id": "c3",
                    "author": {"employee_id": "e2", "address": "founder/design"},
                    "recipients": [{"employee_id": "e3", "address": "founder/testing"}],
                    "created_at": 1700000000,
                    "subject": "Run tests",
                    "reply_to": "m6",
                    "body": body,
                    "summary": "Derived: the designer asked for tests.",
                }
            ],
            "remaining": 2,
            "conversation_id": "c3",
            "session_id": "private-native-session",
            "provider": "private-provider",
        },
    )
    assert "founder/design (e2)" in rendered
    assert "founder/testing (e3)" in rendered
    assert "m7" in rendered and "m6" in rendered and "c3" in rendered
    assert body in rendered
    assert "not the author's words" in rendered
    assert "Derived: the designer asked for tests." in rendered
    assert "2 more waiting in c3" in rendered
    assert "private-native-session" not in rendered
    assert "private-provider" not in rendered


def test_mail_renders_attachment_locations_for_claim_and_lookup():
    envelope = {
        "message_id": "m8",
        "conversation_id": "c3",
        "author": {"employee_id": "e2", "address": "founder/design"},
        "recipients": [{"employee_id": "e3", "address": "founder/testing"}],
        "created_at": 1700000000,
        "subject": "Results",
        "reply_to": None,
        "body": "Please open the report.",
        "attachments": [
            {"path": "shared/reports", "name": "findings.md"},
            {"path": "org/founder/work", "name": "notes.txt"},
        ],
    }
    for name, result in (
        ("claim", {"status": "claimed", "messages": [envelope]}),
        ("messages", {"messages": [envelope]}),
    ):
        rendered = render_tool_result(name, result)
        assert "Attachments (relative to your drive; prepend its path to open):" in rendered
        assert "- shared/reports/findings.md" in rendered
        assert "- org/founder/work/notes.txt" in rendered
        assert "Please open the report." in rendered


def test_clear_and_dismiss_results_say_what_happened_and_why_not():
    cleared = render_tool_result(
        "clear",
        {
            "status": "cleared",
            "count": 3,
            "conversations": [
                {"conversation_id": "c1", "message_ids": ["m1", "m2"]},
                {"conversation_id": "c4", "message_ids": ["m9"]},
            ],
        },
    )
    assert "Cleared 3 messages" in cleared and "c1 (m1, m2); c4 (m9)" in cleared
    assert "peek first" in render_tool_result(
        "clear", {"status": "nothing_cleared", "reason": "no_peek"}
    )
    assert "no mail was waiting" in render_tool_result(
        "dismiss", {"status": "nothing_dismissed", "reason": "nothing_waiting"}
    )
    assert "Set aside 2 messages" in render_tool_result(
        "dismiss", {"status": "dismissed", "message_ids": ["m1", "m2"]}
    )


def test_inbox_lists_conversations_with_waiting_headers_and_page_cursor():
    rendered = render_inbox(
        {
            "pending_count": 4,
            "conversations": [
                {
                    "conversation_id": "c7",
                    "pending_count": 3,
                    "participants": [{"employee_id": "e1", "address": "founder/tests"}],
                    "waiting": [
                        {
                            "message_id": "m2",
                            "sender": {"employee_id": "e1", "address": "founder/tests"},
                            "subject": "Testing",
                        }
                    ],
                    "waiting_omitted": 2,
                }
            ],
            "omitted_rows": 1,
            "next_cursor": "1",
            "context": {
                "direct_reports": [
                    {
                        "employee_id": "e1",
                        "address": "founder/tests",
                        "context": {
                            "level": "handoff_required",
                            "state": "stale",
                            "observed_tokens": 90000,
                        },
                    }
                ]
            },
        }
    )
    assert "c7 · with founder/tests (e1) · 3 waiting" in rendered
    assert "m2 · founder/tests (e1): Testing" in rendered
    assert '2 more waiting; claim(conversation="c7") reads them all.' in rendered
    assert "peek(cursor='1')" in rendered
    assert "1 conversations omitted" in rendered
    assert "last observed working memory full" in rendered
    assert "90000" not in rendered


def test_discovery_inspection_preserves_authority_and_incomplete_document_cursor():
    rendered = render_tool_result(
        "get_role",
        {
            "role": {"employee_id": "e1", "address": "founder/tests"},
            "documents": {
                "mandate": {
                    "status": "available",
                    "author_id": "founder",
                    "revision": 3,
                    "digest": "internal-digest",
                    "text": "Test the game.",
                    "complete": False,
                }
            },
            "continuation": "copy-this-cursor",
        },
    )
    assert "revision 3" in rendered and "delegated by founder" in rendered
    assert "Test the game." in rendered
    assert "continues on the next page" in rendered
    assert "copy-this-cursor" in rendered
    assert "internal-digest" not in rendered
