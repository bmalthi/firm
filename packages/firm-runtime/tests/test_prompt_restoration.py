import json
from string import Template
from uuid import uuid4

import pytest

from firm_runtime import prompt_resources
from firm_runtime.prompt_resources import prompt
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.provider_support import render_invocation, standing
from firm_runtime.runtime import Invocation, Runtime
from firm_runtime.store import OWNER, Store, encoded
from firm_runtime.telemetry import handoff_checklist


@pytest.fixture
def marked_prompts(monkeypatch):
    """Exercise real template substitution with prose-independent resource markers."""
    real_prompt = prompt_resources.prompt

    def marked(name):
        if not name.endswith(".json"):
            return f"<{name}>"
        templates = json.loads(real_prompt(name))
        return json.dumps(
            {
                key: f"<{name}:{key}> "
                + " ".join(
                    "${" + parameter + "}" for parameter in Template(value).get_identifiers()
                )
                for key, value in templates.items()
            }
        )

    # Patch the resource reader beneath render, retaining real substitution behavior.
    monkeypatch.setattr(prompt_resources, "prompt", marked)
    monkeypatch.setattr("firm_runtime.provider_support.prompt", marked)
    prompt_resources._group.cache_clear()
    try:
        yield
    finally:
        prompt_resources._group.cache_clear()


def render(**changes):
    context = {
        "employee_id": "employee",
        "address": "delivery",
        "manager_id": "founder",
        "birth": False,
        "fresh_session": False,
        "inbox": {"messages": [], "status": "empty"},
        **changes,
    }
    invocation = Invocation(str(uuid4()), "employee", "secret", str(uuid4()), context)
    return render_invocation(invocation, None if context["fresh_session"] else str(uuid4()))


def mandate(body, author=OWNER):
    return {"revision": 1, "author_id": author, "body": body}


def test_standing_places_exact_role_first_without_internal_metadata(marked_prompts):
    body = "First line.\n  Preserve indentation and trailing newlines.\n"
    text = standing(
        {
            "employee_id": "do-not-project-this-id",
            "address": "founder/delivery",
            "manager_id": "e1",
            "manager_address": "founder",
            "identity": "Delivery Lead",
            "mandate": mandate(body, "founder"),
            "execution_profiles": ["claude-heavy"],
            "activation_id": "internal",
            "workspace_references": {
                "workspace": "/work",
                "private": "/home/agent", "scratch": "/home/agent/scratch",
                "shared": "/shared",
                "drive": "/drive",
                "organization": "/org",
            },
        }
    )
    assert text.startswith("<workplace.json:identity>")
    assert "Delivery Lead (founder/delivery)" in text
    assert text.count(body) == 1
    assert text.index(body) < text.index("<workplace.json:workplace>") < text.index("<standing.md>")
    for forbidden in ("do-not-project-this-id", "claude-heavy", "activation_id"):
        assert forbidden not in text
    assert "<workplace.json:access> /drive" in text


@pytest.mark.parametrize("manager", [OWNER, "founder"])
@pytest.mark.parametrize("state", ["birth", "resume", "fresh", "handoff", "recovery"])
def test_founder_guidance_is_only_in_founder_birth(manager, state, marked_prompts):
    data = {
        "address": "founder" if manager == OWNER else "founder/design",
        "manager_address": manager,
        "mandate": mandate("ASSIGNED WORK", manager),
        "workspace_references": {
            "workspace": "/work", "private": "/home/agent", "scratch": "/home/agent/scratch"
        },
        "birth": state == "birth",
        "fresh_session": state != "resume",
    }
    if state == "handoff":
        data["handoff_notes"] = "Saved progress"
    if state == "recovery":
        data["recovery"] = {"cause": "owner_stop", "context_retained": False}
    instructions = standing(data)
    activation = render(**data)
    assert instructions.count("<standing.md>") == 1
    assert "<founder_birth.md>" not in instructions
    assert "<birth.md>" not in instructions
    assert "ASSIGNED WORK" in instructions
    assert "ASSIGNED WORK" not in activation
    for resource in ("founder_birth.md", "birth.md"):
        expected = state == "birth" and (
            resource == ("founder_birth.md" if manager == OWNER else "birth.md")
        )
        assert activation.count(f"<{resource}>") == int(expected)


def test_birth_has_onboarding_and_only_actual_mail(marked_prompts):
    founder = render(
        birth=True, fresh_session=True, manager_id=OWNER, mandate=mandate("OWNER PURPOSE")
    )
    assert "OWNER PURPOSE" not in founder
    assert "<founder_birth.md>" in founder
    assert "<wake.json:inbox>" not in founder
    assert "<wake.json:empty_inbox>" not in founder
    employee = render(
        birth=True,
        fresh_session=True,
        mandate=mandate("DELEGATED WORK", "founder"),
        founder_mandate=mandate("OWNER PURPOSE"),
        inbox={
            "pending_count": 1,
            "conversations": [
                {
                    "conversation_id": "c1",
                    "pending_count": 1,
                    "participants": [{"address": "owner"}],
                    "waiting": [
                        {
                            "message_id": "m9",
                            "subject": "Real first mail",
                            "sender": {"address": "owner"},
                        }
                    ],
                }
            ],
        },
    )
    assert "OWNER PURPOSE" not in employee
    assert "DELEGATED WORK" not in employee
    assert "m9" in employee and "Real first mail" in employee
    assert "<birth.md>" in employee
    assert "<wake.json:inbox>" in employee


def test_resume_handoff_and_recovery_preserve_distinct_memory_contracts(marked_prompts):
    resumed = render()
    assert "<wake.json:update>" in resumed and "<wake.json:empty_inbox>" in resumed
    fresh = render(fresh_session=True, handoff_notes="Await m17; consult /work/result.md.")
    assert "<wake.json:handoff>" in fresh and "Await m17; consult /work/result.md." in fresh
    assert "<wake.json:fresh>" not in fresh
    for retained in (True, False):
        recovery = render(
            fresh_session=not retained,
            handoff_notes="STALE HANDOFF",
            recovery={
                "context_retained": retained,
                "cause": "provider_broken",
                "recent_claims": [{"message_id": "m17", "subject": "Review"}],
            },
        )
        assert "<wake.json:check_results>" in recovery
        assert "<wake.json:interrupted>" in recovery
        assert "<wake.json:handoff>" not in recovery
        assert "<wake.json:retained>" in recovery if retained else "<wake.json:lost>" in recovery
        assert "m17" in recovery and "STALE HANDOFF" not in recovery
        assert "provider_broken" not in recovery


def test_handoff_notes_precede_orientation_and_inbox_in_wake_text(marked_prompts):
    text = render(
        fresh_session=True,
        handoff_notes="Continue the review.",
        orientation={
            "manager": {"address": "founder", "employee_id": "e1"},
            "direct_reports": [{"address": "founder/delivery/writer"}],
            "top_level_roles": [],
            "omitted": {"direct_reports": 0, "top_level_roles": 0},
        },
        inbox={
            "pending_count": 1,
            "owner": [{"message_id": "m1", "subject": "Hi", "sender": {"address": "owner"}}],
        },
    )
    assert (
        text.index("<wake.json:handoff>")
        < text.index("founder/delivery/writer")
        < text.index("<wake.json:inbox>")
    )


def test_fresh_reopen_without_handoff_explains_memory_boundary(marked_prompts):
    text = render(fresh_session=True, birth=False)
    assert "<wake.json:fresh>" in text
    assert "<wake.json:handoff>" not in text
    assert "<birth.md>" not in text


def test_activation_deduplicates_self_appointment_without_mutating_snapshot(marked_prompts):
    inbox = {
        "context": {
            "self": {"temporary_periods": {"periods_after_current": 1}},
            "direct_reports": [
                {"address": "founder/report", "temporary_periods": {"periods_after_current": 0}}
            ],
        }
    }
    text = render(temporary_appointment={"remaining_periods": 1}, inbox=inbox)
    assert text.count("<wake.json:temporary> 1") == 1
    assert text.count("<results.json:awareness_temporary>") == 1
    assert "founder/report" in text and "0" in text
    assert inbox["context"]["self"]["temporary_periods"]["periods_after_current"] == 1


@pytest.mark.parametrize("fresh", [False, True])
def test_changed_mandate_and_orientation_are_brief_updates(marked_prompts, fresh):
    text = render(
        fresh_session=fresh,
        mandate_changed=True,
        mandate=mandate("NEW MANDATE"),
        orientation={
            "manager": {"address": "founder", "employee_id": "e1"},
            "direct_reports": [{"address": "founder/delivery/writer"}],
            "top_level_roles": [],
            "omitted": {"direct_reports": 99, "top_level_roles": 0},
        },
    )
    assert ("<wake.json:mandate_changed>" in text) is (not fresh)
    assert "NEW MANDATE" not in text
    assert "founder/delivery/writer" in text
    assert "<wake.json:orientation_omitted>" in text
    assert "99" in text and "direct reports" in text
    assert "employee_id" not in text and "top_level_roles" not in text


def test_orientation_does_not_repeat_self_or_manager_as_peer_functions(marked_prompts):
    text = render(
        address="founder/delivery",
        manager_address="founder",
        orientation={
            "top_level_roles": [{"address": "founder/delivery"}, {"address": "founder"}],
            "top_level_included": True,
            "omitted": {"top_level_roles": 3},
        },
    )
    assert "Other functions:" not in text
    assert "founder/delivery" not in text
    assert "<wake.json:orientation_omitted>" in text
    assert "3" in text and "other functions" in text


def test_zero_mail_does_not_hide_an_active_drain_control(marked_prompts):
    text = render(
        birth=True,
        inbox={
            "pending_count": 0,
            "operations_control": {
                "kind": "drain",
                "instruction": "Save progress and stop; owner restart required.",
            },
        },
    )
    assert "Save progress and stop; owner restart required." in text
    assert "<wake.json:empty_inbox>" not in text


def test_temporary_appointment_is_actionable_and_provider_neutral(marked_prompts):
    text = render(
        temporary_appointment={
            "kind": "temporary",
            "remaining_periods": 2,
            "allocated_periods": 4,
            "provider": "claude",
        }
    )
    assert "<wake.json:temporary> 2" in text
    assert "allocated_periods" not in text and "claude" not in text


@pytest.mark.parametrize("pressure", [False, True])
def test_drain_delivers_complete_handoff_once_even_without_measured_context(tmp_path, pressure):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "PURPOSE",
        {"max_active": 1, "staffing": False, "founder_profile": "test", "profiles": {"test": {}}},
    )
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)
    if pressure:
        runtime.record_usage(
            invocation.activation_id,
            ProviderUsage(
                current_context_tokens=9500, provider_context_window=10000, source="test"
            ),
        )
    runtime.stop()
    result = runtime.tools(invocation.token).peek()
    checklist = handoff_checklist()
    enclosing = result["context_pressure"] if pressure else result["operations_control"]
    assert enclosing["handoff_checklist"] == checklist
    text = encoded(result)
    assert checklist == prompt("context-handoff.md")
    repeated = runtime.tools(invocation.token).peek()
    assert all(
        "handoff_checklist" not in repeated.get(key, {})
        for key in ("context_pressure", "operations_control")
    )
    assert len(text.encode()) <= 16 * 1024
