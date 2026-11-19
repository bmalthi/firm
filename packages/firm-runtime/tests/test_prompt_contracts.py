"""Prompt boundaries and memory transitions, independent of provider execution."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from firm_sandbox import Layout
from test_agy_credentials import account

from firm_runtime.agy_provider import AgyProvider
from firm_runtime.claude_provider import ClaudeProvider
from firm_runtime.codex_provider import CodexProvider
from firm_runtime.mcp_server import TOOL_DESCRIPTIONS
from firm_runtime.prompt_resources import prompt, render
from firm_runtime.provider_contract import ProviderUsage, RoleLaunchRequest
from firm_runtime.provider_support import _orientation, render_invocation, standing
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Invocation, Runtime
from firm_runtime.store import OWNER, Store

SOURCE = "  Exact {body} ${notes} $money {{mandate}}\n\nIndented:\n  next\n"
CONFIG = {
    "max_active": 2,
    "staffing": False,
    "founder_profile": "test",
    "profiles": {"test": {"context_window": 10_000, "handoff_reserve": 1000}},
}


def context(**changes):
    return {
        "address": "founder/delivery",
        "manager_id": "e1",
        "manager_address": "founder",
        "employee_id": "e2",
        "mandate": {"body": SOURCE, "author_id": "e1"},
        "workspace_references": {
            "workspace": "/work", "private": "/home/agent", "scratch": "/home/agent/scratch"
        },
        "birth": False,
        "fresh_session": False,
        "inbox": {},
        **changes,
    }


@pytest.mark.parametrize("state", ["birth", "resume", "handoff", "recovery", "fresh"])
@pytest.mark.parametrize("selection", [
    {"scope": None, "functions": []},
    {"scope": "team-leader", "functions": ["legal", "software-engineering"]},
])
def test_provider_parity_and_literal_source_across_memory_states(tmp_path, state, selection):
    changes = {
        "birth": {"birth": True, "fresh_session": True},
        "resume": {"mandate_changed": True},
        "handoff": {"fresh_session": True, "handoff_notes": SOURCE},
        "recovery": {"recovery": {"cause": "owner_stop", "context_retained": True}},
        "fresh": {"fresh_session": True},
    }[state]
    data = context(guidance=json.dumps(selection), **changes)
    invocation = Invocation(str(uuid4()), "e2", "test-token", str(uuid4()), data)
    native = None if data["fresh_session"] else str(uuid4())
    codex_auth = tmp_path / "codex.json"
    codex_auth.write_text('{"tokens":{"access_token":"test-token"}}')
    claude_auth = tmp_path / "claude-token"
    claude_auth.write_text("test-token")
    codex_auth.chmod(0o600)
    claude_auth.chmod(0o600)
    adapters = (
        CodexProvider(model="test", effort="high", auth_file=codex_auth),
        ClaudeProvider(model="test", effort="high", auth_file=claude_auth),
        AgyProvider(
            model="gemini-4-hydrogen", effort="medium",
            auth_file=account(tmp_path, expiry=4102444800),
        ),
    )
    specs = [
        adapter.build_spec(
            RoleLaunchRequest(
                invocation=invocation,
                role_id='e2',
                run_id='run-test',
                mcp_url='http://firm.local/mcp',
                provider_session_id=native,
                offered_tools=tuple(TOOL_DESCRIPTIONS),
            ),
            layout=Layout(),
        )
        for adapter in adapters
    ]
    assert specs[0].stdin == specs[1].stdin == render_invocation(invocation, native).encode()
    assert (
        specs[0].secrets["role-instructions"]
        == specs[1].secrets["role-instructions"]
        == specs[2].secrets["role-instructions"]
    )
    instructions = specs[0].secrets["role-instructions"].decode()
    assert instructions == standing(data)
    physics = prompt("standing-physics.md")
    good_worker = prompt("standing-good-worker.md")
    assert instructions.count(physics) == instructions.count(good_worker) == 1
    assert instructions.index(physics) < instructions.index(good_worker)
    limits = prompt("standing-limits.md")
    assert instructions.count(limits) == 1
    assert instructions.endswith(limits + "\n")
    from firm_runtime import guidance

    for guide_id in ([selection["scope"]] if selection["scope"] else []) + selection["functions"]:
        assert instructions.count(guidance.body(guide_id)) == 1
    assert instructions.count(SOURCE) == 1
    assert "profile.md" in instructions and "track-record.md" in instructions
    assert "/work/profile.md" not in instructions and "/work/track-record.md" not in instructions
    if state == "handoff":
        assert specs[0].stdin.decode().count(SOURCE) == 1
    else:
        assert SOURCE not in specs[0].stdin.decode()


@pytest.mark.parametrize("birth", [False, True])
def test_workspace_path_is_supplied_while_prompt_records_stay_relative(birth):
    workspace = "/home/firmrole-0123456789ab/drive/org/founder/employee/work"
    private = "/home/firmrole-0123456789ab/private"
    scratch = "/home/firmrole-0123456789ab/scratch"
    mandate_body = "Preserve this user request exactly: consult /work/request.md."
    data = context(
        birth=birth,
        fresh_session=birth,
        mandate={"body": mandate_body, "author_id": "e1"},
        workspace_references={
            "workspace": workspace,
            "private": private,
            "scratch": scratch,
        },
    )
    invocation = Invocation(str(uuid4()), "e2", "test-token", str(uuid4()), data)
    role_instructions = standing(data)
    prompt_text = render_invocation(invocation, None)

    assert all(reference in role_instructions for reference in (workspace, private, scratch))
    assert "profile.md" in role_instructions and "track-record.md" in role_instructions
    assert "/work/profile.md" not in role_instructions
    assert "/work/track-record.md" not in role_instructions
    if birth:
        assert "profile.md" in prompt_text and "track-record.md" not in prompt_text
        assert "/work/profile.md" not in prompt_text
        assert "/work/track-record.md" not in prompt_text
    assert mandate_body in role_instructions


@pytest.mark.parametrize(
    "name",
    [
        "standing-physics.md",
        "standing-good-worker.md",
        "standing-limits.md",
        "guidance-introduction.md",
        "birth.md",
        "founder_birth.md",
        "mac-acquired.md",
        "context-handoff.md",
        "runtime_summary.md",
        "runtime_level-choice.md",
        "runtime_llm.md",
    ],
)
def test_standalone_prompt_resources_are_packaged_and_nonempty(name):
    assert prompt(name).strip()


def test_old_standing_resource_is_not_packaged():
    with pytest.raises(FileNotFoundError):
        prompt("standing.md")


def test_templates_fail_on_missing_values_and_do_not_reinterpret_inserted_text():
    with pytest.raises(KeyError):
        render("wake", "handoff")
    assert SOURCE in render("wake", "handoff", notes=SOURCE)
    assert "${notes}" in render("workplace", "mandate", author="${notes}", body=SOURCE)


def test_birth_and_wake_resources_leave_routine_judgment_to_the_role():
    birth = " ".join(prompt("birth.md").split())
    founder_birth = " ".join(prompt("founder_birth.md").split())
    wake = render("wake", "inbox", inbox="No unread mail.")
    assert "missing information would materially change the work" not in birth
    assert "routine implementation decisions need not block progress" not in birth
    assert "current mandate supplied in your responsibility above" in founder_birth
    assert "Do work yourself only when it is small or no role would fit" in founder_birth
    assert "responsibilities, authority and coordination clear in its mandate" in founder_birth
    assert "Unread mail already offered will not repeatedly trigger idle review." in wake


def test_standing_describes_only_configured_optional_capabilities():
    baseline = standing(context())
    assert "No optional external service or Mac capability is configured" in baseline
    assert "GitHub is configured" not in baseline
    assert "## Configured LLM CLIs" not in baseline
    assert "built-in image generation and editing" not in baseline
    configured = standing(
        context(
            capabilities={
                "github": True,
                "mac": True,
            }
        )
    )
    assert "GitHub is configured" in configured
    assert "task workers" not in configured
    assert "A Mac is configured for this firm" in configured
    assert "mac_status to check availability without taking a lease" in configured
    assert "mac_acquire when work needs macOS" in configured
    assert "No optional external service or Mac capability is configured" not in configured
    github_only = standing(context(capabilities={"github": True}))
    assert "plain git over HTTPS or gh from Linux" in github_only
    assert "Clone and push from Linux" in github_only
    assert "Within your mandate, choose branches, pull requests, reviews and merges" in github_only
    assert "Name your role address in pull request bodies" in github_only
    assert "## Configured LLM CLIs" not in github_only

    claude_only = standing(context(capabilities={"llm": ["claude"], "llm_max_concurrent": 2}))
    assert "## Configured LLM CLIs" in claude_only
    assert "`claude`:" in claude_only
    assert "built-in image generation and editing" not in claude_only

    llm_configured = standing(
        context(capabilities={"llm": ["claude", "codex"], "llm_max_concurrent": 3})
    )
    assert "## Configured LLM CLIs" in llm_configured
    assert "`claude`:" in llm_configured
    assert "`codex`:" in llm_configured
    assert "`agy`:" not in llm_configured
    assert "built-in image generation and editing" in llm_configured
    assert "at most 3 may run concurrently in your role" in llm_configured

    agy_only = standing(context(capabilities={"llm": ["agy"], "llm_max_concurrent": 1}))
    assert "`agy`:" in agy_only
    assert "built-in image generation and editing" in agy_only


@pytest.mark.parametrize(
    ("included", "omitted", "expected"),
    [
        (False, 0, ""),
        (True, 0, "Other functions: none."),
        (True, 3, render("wake", "orientation_omitted", omitted=3, label="other functions")),
    ],
)
def test_orientation_distinguishes_exclusion_empty_and_bounded_omission(
    included, omitted, expected
):
    data = context(
        orientation={
            "top_level_roles": [],
            "top_level_included": included,
            "omitted": {"top_level_roles": omitted},
        }
    )
    assert _orientation(data) == expected
    assert _orientation(context()) == ""


def running(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build", CONFIG)
    runtime.start()
    first = runtime.admit()
    runtime.mark_started(first.activation_id)
    return runtime, first


def test_removing_final_report_updates_retained_context(tmp_path):
    runtime, first = running(tmp_path)
    employee = runtime.tools(first.token).hire("report", "Help", "hire")["employee_id"]
    runtime.finish(first.activation_id)
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM execution_requests")
        runtime.store.request_execution(db, first.employee_id, "test")
    has_report = runtime.admit()
    assert employee == has_report.context["orientation"]["direct_reports"][0]["employee_id"]
    runtime.mark_started(has_report.activation_id)
    runtime.tools(has_report.token).fire(employee, "Done", "retire")
    runtime.finish(has_report.activation_id)
    runtime.owner.send_mail(OWNER, [first.employee_id], "Continue", request_id="wake")
    empty = runtime.admit()
    assert not empty.context["fresh_session"]
    assert "Direct reports: none." in render_invocation(empty, str(uuid4()))


def measured(runtime, activation, compactions=0, current=9500):
    runtime.record_usage(
        activation,
        ProviderUsage(
            current_context_tokens=current,
            native_compactions=compactions,
            provider_context_window=10000,
            source="test",
        ),
    )


def checklists(result):
    return sum(
        "handoff_checklist" in result.get(key, {})
        for key in ("context_pressure", "operations_control")
    )


def test_concurrent_controls_compaction_escalation_and_new_work_period(tmp_path):
    runtime, first = running(tmp_path)
    measured(runtime, first.activation_id, current=6500)
    assert checklists(runtime.tools(first.token).peek()) == 0
    measured(runtime, first.activation_id)
    runtime.stop()
    with ThreadPoolExecutor(max_workers=4) as workers:
        responses = list(workers.map(lambda _: runtime.tools(first.token).peek(), range(4)))
    assert sum(map(checklists, responses)) == 1
    # Read gating doesn't depend on presentation history; the pending mail survives.
    message = runtime.owner.send_mail(OWNER, [first.employee_id], "Work", request_id="mail")
    blocked = runtime.tools(first.token).read(message["message_id"])
    assert blocked["status"] == "blocked" and checklists(blocked) == 0
    measured(runtime, first.activation_id, compactions=1)
    assert checklists(runtime.tools(first.token).peek()) == 1
    assert checklists(runtime.tools(first.token).peek()) == 0
    runtime.finish(first.activation_id)
    runtime.start()
    second = runtime.admit()
    assert second is not None
    runtime.mark_started(second.activation_id)
    measured(runtime, second.activation_id)
    assert checklists(runtime.tools(second.token).peek()) == 1


@pytest.mark.parametrize("drain", [False, True])
def test_saved_handoff_finishes_without_repeating_save_guidance(tmp_path, drain):
    runtime, first = running(tmp_path)
    measured(runtime, first.activation_id)
    if drain:
        runtime.stop()
    tools = runtime.tools(first.token)
    saved = tools.handoff(SOURCE, "continue", "save")
    assert saved["execution_still_active"] is True
    assert checklists(saved) == 0
    text = render_tool_result("handoff", saved)
    assert text.count(render("controls", "saved")) == 1
    for response in (tools.peek(), tools.handoff(SOURCE, "continue", "save")):
        assert checklists(response) == 0
        assert all(
            c.get("instruction", render("controls", "saved")) == render("controls", "saved")
            for c in (response.get("context_pressure", {}), response.get("operations_control", {}))
        )
    assert runtime.status()["active"][0]["state"] == "running"
    runtime.finish(first.activation_id)
    if drain:
        runtime.start()
    next_work = runtime.admit()
    assert next_work.context["handoff_notes"] == SOURCE
    assert next_work.context["fresh_session"]


def test_all_guidance_resources_are_packaged_and_nonempty():
    from firm_runtime import guidance

    for entry in guidance.catalog():
        assert prompt("guidance/" + entry["id"] + ".md").strip()


@pytest.mark.parametrize("extra", [
    {"fresh_session": True},
    {"handoff_notes": "Unfinished commitment"},
    {"recovery": {"cause": "interrupted", "working_memory": "retained"}},
])
def test_idle_review_guidance_survives_opening_precedence(extra):
    invocation = Invocation("a1", "founder", "token", "session", {
        "employee_id": "founder", "manager_id": OWNER, "birth": False,
        "idle_inbox_review": True, "inbox": {}, **extra,
    })
    rendered = render_invocation(invocation, None)
    assert "Idle inbox review" in rendered
    assert "keep unfinished commitments" in rendered
    assert "Do not send acknowledgment-only replies" in rendered
    assert "Reading records receipt, not completed work" in rendered
    assert "This snapshot lists only newly offered mail; peek shows the whole inbox." in rendered


@pytest.mark.parametrize("name,expected", [
    ("peek", "List threads with unread mail without marking anything read: participants, last "
     "sender, unread count, latest unread message_id (for read or dismiss), Action vs FYI, "
     "and the inbox's newest message_id (for clear)."),
    ("clear", "Set aside all unread mail across every thread up to and including message_id "
     "(pass the newest ID from peek) without reading it; later arrivals stay unread."),
    ("introduce", "Bring add into the thread of message_id: starts a new thread with the same "
     "subject for a named thread, or posts to the larger group's no-subject conversation, "
     "leaving the source thread unchanged and unreadable to newcomers. action lists who in the "
     "larger group is asked to act ([] for all FYI). summarize=true prepends a labeled "
     "Operations summary of the source thread (if generation fails, nothing is sent; retry or "
     "use summarize=false); otherwise include needed context in body."),
])
def test_second_pass_mail_tool_wording(name, expected):
    assert TOOL_DESCRIPTIONS[name] == expected


def test_idle_review_wording():
    assert render("wake", "idle_review_instructions") == (
        "## Idle inbox review\n\nLook through the offered unread mail, reading it in full or "
        "as a summary as useful. Act only where useful, and dismiss what needs nothing. This "
        "snapshot lists only newly offered mail; peek shows the whole inbox. Reading records "
        "receipt, not completed work: keep unfinished commitments in working memory or handoff "
        "notes. Do not send acknowledgment-only replies or repeat status updates just because "
        "this review happened. Once you have read or dismissed what was offered and nothing "
        "needs further work, finish normally."
    )


def test_mail_physics_wording_and_discussion_craft():
    physics = " ".join(prompt("standing-physics.md").split())
    assert "## Workspace records" in prompt("standing-physics.md")
    assert "## Role playbooks" not in prompt("standing-physics.md")
    assert "playbooks/index.md" in physics
    assert "Attach deliverables they should open with send or reply" in physics
    assert (
        "Mail is kept in threads: each set of people shares one no-subject conversation, while "
        "each send with a subject starts a separate named thread (a repeated subject starts "
        "another thread rather than joining the first). reply continues a thread with the same "
        "people; introduce starts one with a larger group, optionally prepending a generated "
        "summary without exposing earlier messages."
    ) in physics
    assert (
        "peek lists unread threads without marking anything read; read marks unread messages up "
        "to the named ID read (or returns a generated summary with summarize); dismiss and "
        "clear set unread mail aside without reading it. Sending, replying and introducing "
        "change nothing unread, and reading, dismissing or clearing notifies no one and does "
        "not mean requested work is done: keep unfinished commitments in working memory or "
        "handoff notes, and reread an earlier message by its ID when needed."
    ) in physics
    assert "use it as a cue to peek and read relevant updates" in physics
    assert "visually inspect any image" not in physics
    worker = " ".join(prompt("standing-good-worker.md").split())
    assert (
        "visually inspect any image, SVG, diagram, UI or page layout you generate or convert "
        "before delivering it."
    ) in worker
    assert "answers or deliverables they are waiting on" in worker
    discussion = (
        "- Keep a discussion in one place. Reply in the thread it is already in rather than "
        "starting a new message, and send without a subject for everyday mail to people you "
        "already talk to. Give a subject only to a topic worth following on its own, such as a "
        "piece of work that will take several exchanges. - Read the full messages when you will "
        "act on what they say; a summary is for catching up. Dismiss or clear mail that needs "
        "nothing from you, so your inbox shows what does."
    )
    assert "acknowledgment is still unnecessary. " + discussion in worker
    assert "Keep a discussion in one place" not in physics
    assert (
        "Read mail is no longer unread; keep its message ID if you still need it."
        in " ".join(prompt("context-handoff.md").split())
    )
    assert "Preserve message IDs" in " ".join(prompt("runtime_summary.md").split())


def test_image_generation_and_cli_output_guidance():
    physics = " ".join(prompt("standing-physics.md").split())
    worker = " ".join(prompt("standing-good-worker.md").split())
    runtime_llm = " ".join(prompt("runtime_llm.md").split())
    assert "copying finished files from private tool output to the firm drive" in physics
    assert "generating or editing images through a CLI that supports it" in physics
    assert (
        "Where image generation is available, use it for illustrations, photography, "
        "icons or textures"
    ) in worker
    assert (
        "copy or write any generated image or output file to the caller's requested path "
        "before finishing"
    ) in runtime_llm


def test_task_switch_and_closeout_guidance_across_prompt_layers():
    raw_physics = prompt("standing-physics.md")
    physics = " ".join(raw_physics.split())
    assert (
        "keep it across ordinary pauses, awaited replies or grants, likely follow-up on a "
        "delivery you just sent, small course corrections on the same job, and bounded local "
        "checks or CLI runs."
    ) in physics
    for term in (
        "When closing out finished or superseded work on a significant change of direction",
        "put the previous work away first",
        "track-record.md",
        "profile.md",
        "dismiss or clear",
        "cancel_reminder",
        "not your final temporary work period",
        "clean_up=True",
        "When Operations requires a handoff, do not start extra cleanup work:",
        "remove the checkouts, containers, images and volumes it no longer needs.",
    ):
        assert term in physics
    assert "dangling images" not in physics
    assert "Mac" not in raw_physics and "mac_release" not in raw_physics

    worker = " ".join(prompt("standing-good-worker.md").split())
    assert (
        "When your assignment or approach changes significantly, close out or clearly park the "
        "previous work before moving on so old drafts, open requests and dead-end assumptions "
        "don't linger."
    ) in worker

    handoff = " ".join(prompt("context-handoff.md").split())
    assert 'handoff(notes, next="continue"|"wait")' in handoff
    assert "clean_up" not in handoff
    assert "Omit finished or discarded work" in handoff
    assert "Read mail is no longer unread; keep its message ID if you still need it." in handoff

    mandate_changed = render("wake", "mandate_changed")
    assert "Update profile.md if your responsibilities changed" in mandate_changed
    assert "put away or park superseded work first" in mandate_changed
    assert "affected direct reports" in mandate_changed
    assert "not your final temporary work period" in mandate_changed
    assert 'next="continue"' in mandate_changed

    capability_mac = render("workplace", "capability_mac")
    assert "call mac_release once your macOS step is done" in capability_mac
    assert "while your working memory stays intact" in capability_mac

    for guide in ("guidance/team-leader.md", "guidance/firm-leader.md"):
        text = " ".join(prompt(guide).split())
        assert "direct reports" in text
        assert "revise_mandate" in text
        assert "fire" in text
        assert "superseded" in text
