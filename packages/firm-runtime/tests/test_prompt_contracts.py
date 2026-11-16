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
                task_timeout_seconds=600,
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
    wake = render("wake", "inbox", inbox="No pending mail.")
    assert "missing information would materially change the work" not in birth
    assert "routine implementation decisions need not block progress" not in birth
    assert "current mandate supplied in your responsibility above" in founder_birth
    assert "Do work yourself only when it is small or no role would fit" in founder_birth
    assert "responsibilities, authority and coordination clear in its mandate" in founder_birth
    assert "Already pending mail will not cause another automatic wake." in wake


def test_standing_describes_only_configured_optional_capabilities():
    baseline = standing(context())
    assert "No optional external service or Mac capability is configured" in baseline
    assert "GitHub is configured" not in baseline
    assert "The firm has enabled the Gemini API" not in baseline
    configured = standing(
        context(
            capabilities={
                "github": True,
                "google_cloud": {"project": "firm-project"},
                "mac": True,
            }
        )
    )
    assert "GitHub is configured" in configured
    assert "The firm has enabled the Gemini API" in configured
    assert "GEMINI_API_KEY" in configured and "GOOGLE_CLOUD_PROJECT" in configured
    assert "configured Google Cloud project is firm-project" in configured
    assert "task workers and the Mac do not receive these settings" in configured
    assert "A Mac is configured for this firm" in configured
    assert "mac_status to check availability without taking a lease" in configured
    assert "mac_acquire when work needs macOS" in configured
    assert "No optional external service or Mac capability is configured" not in configured
    github_only = standing(context(capabilities={"github": True}))
    assert "plain git over HTTPS or gh from Linux" in github_only
    assert "Clone and push from Linux" in github_only
    assert "Within your mandate, choose branches, pull requests, reviews and merges" in github_only
    assert "Name your role address in pull request bodies" in github_only
    assert "The firm has enabled the Gemini API" not in github_only


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
    runtime.messaging.send(OWNER, [first.employee_id], "Continue", request_id="wake")
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
    # Claim gating doesn't depend on presentation history; the pending mail survives.
    message = runtime.messaging.send(OWNER, [first.employee_id], "Work", request_id="mail")
    blocked = runtime.tools(first.token).claim("claim", [message["message_id"]])
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
