import json
from importlib.resources import files

import pytest

from firm_runtime.errors import ConfigurationError, FirmError
from firm_runtime.mcp_server import MANDATE_GUIDANCE, TOOL_DESCRIPTIONS, _rendered
from firm_runtime.prompt_resources import prompt, render
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store


def test_tool_descriptions_are_resource_backed_and_keep_registered_operations():
    resources = json.loads(prompt("tools.json"))
    expected_names = {
        "task",
        "send",
        "reply",
        "peek",
        "claim",
        "dismiss",
        "clear",
        "messages",
        "context",
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
        "hire",
        "renew_temporary",
        "promote",
        "get_role",
        "find_roles",
        "org_tree",
        "revise_mandate",
        "fire",
        "retry",
        "handoff",
        "create_repository",
        "request_access",
        "mac_acquire", "mac_release", "mac_status",
        "desktop_status", "desktop_apps", "desktop_windows", "desktop_inspect",
        "desktop_launch", "desktop_action", "desktop_focus", "desktop_type", "desktop_press_key",
        "desktop_capture", "desktop_click", "desktop_drag", "desktop_scroll",
        "desktop_tap_sequence", "desktop_close_apps",
    }
    assert set(TOOL_DESCRIPTIONS) == expected_names
    assert MANDATE_GUIDANCE == resources["mandate_guidance"]
    for name in expected_names - {"hire", "revise_mandate"}:
        assert TOOL_DESCRIPTIONS[name] == resources[name]
    assert TOOL_DESCRIPTIONS["hire"] == render("tools", "hire", mandate_guidance=MANDATE_GUIDANCE)
    assert TOOL_DESCRIPTIONS["revise_mandate"] == render(
        "tools", "revise_mandate", mandate_guidance=MANDATE_GUIDANCE
    )
    assert "${" not in "".join(TOOL_DESCRIPTIONS.values())


def test_request_access_names_its_kinds_resources_and_refusals():
    text = TOOL_DESCRIPTIONS["request_access"]
    for term in (
        "network_host", "resource", "pending", "cause", "finish normally",
        "access_kind_unknown", "access_resource_invalid", "access_denied",
    ):
        assert term in text
    assert "hand off only when you need a fresh start" in text
    assert "ask the owner" not in text.lower() and "host" not in text.split("Kinds:")[0]


def test_mail_tool_descriptions_explain_deliverable_attachments():
    for name in ("send", "reply"):
        description = TOOL_DESCRIPTIONS[name]
        assert "Attach a file the reader should open, such as a report." in description
        assert "absolute path under your drive" in description


def test_standing_and_starter_mandate_explain_deliverable_attachments():
    standing = " ".join(prompt("standing.md").split())
    assert "Attach deliverables they should open with send or reply" in standing
    assert "using their absolute paths under your drive" in standing
    starter = " ".join(
        files("firm_runtime").joinpath("resources/starter_mandate.md").read_text().split()
    )
    assert "message the owner with the file attached" in starter
    assert "using its absolute path under your drive" in starter


def test_role_text_describes_pending_answers_and_never_sends_a_role_to_the_owner():
    standing = " ".join(prompt("standing.md").split())
    assert "tell your manager so the owner" not in standing.lower()
    assert "ask the owner" not in standing.lower()
    assert "answer `pending` with a request_id and a cause" in standing
    assert "`access_denied` is the owner's answer" in standing
    assert "finish normally to pause while retaining working memory" in standing
    assert "Set a reminder when it serves a real future check" in standing
    assert "public services that need no account" in standing
    assert "owner-specified limits" in standing
    assert "saves the image" not in standing
    for name in ("mac_acquire", "request_access", "configuration_error"):
        assert "ask the owner" not in json.loads(prompt("tools.json"))[name].lower()
    assert "pending" in TOOL_DESCRIPTIONS["mac_acquire"]
    assert "finish normally" in render("tools", "configuration_error")
    assert "Hand off only when you need a fresh start" in render("tools", "configuration_error")


def test_a_configuration_error_renders_as_one_message_and_other_errors_pass_through():
    def misconfigured():
        raise ConfigurationError("execution_profile is not configured for this firm")

    def mentions_account():
        raise FirmError("only the owner may change the account")

    with pytest.raises(FirmError) as raised:
        _rendered(misconfigured, "hire")()
    assert str(raised.value) == render("tools", "configuration_error")
    assert type(raised.value) is FirmError
    with pytest.raises(FirmError, match="only the owner may change the account"):
        _rendered(mentions_account, "hire")()


def test_hiring_with_an_unconfigured_profile_is_a_configuration_error(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test", "Build",
        {"max_active": 2, "staffing": False, "founder_profile": "d", "profiles": {"d": {}}},
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    with pytest.raises(ConfigurationError, match="not configured"):
        runtime.lifecycle.hire(founder.employee_id, "scout", "Work", "nowhere", request_id="h")


def test_template_values_are_substituted_once_and_user_text_stays_literal():
    marker = "${employee_id} and {mandate} cost $$"
    result = render("results", "hire_queued", address=marker, employee_id="e1")
    assert result.count(marker) == 1
    assert "e1" in result
    assert render("tools", "hire", mandate_guidance=marker).count(marker) == 1
    rendered = render_tool_result(
        "messages",
        {
            "messages": [
                {
                    "message_id": "m1",
                    "conversation_id": "c1",
                    "author": "founder",
                    "recipients": ["analyst"],
                    "created_at": 1700000000,
                    "subject": "Literal",
                    "body": marker,
                }
            ]
        },
    )
    assert marker in rendered


def test_mac_guidance_describes_the_account_and_shared_machine_boundaries():
    standing = prompt("standing.md")
    acquire = TOOL_DESCRIPTIONS["mac_acquire"]
    lowered = " ".join(acquire.lower().split())
    assert "your own account" in lowered
    assert "ports, cpu and homebrew are shared" in lowered
    assert "not private" in lowered
    assert "optional Mac capability" not in standing


def test_capture_description_reports_success_path_and_save_failure():
    description = TOOL_DESCRIPTIONS["desktop_capture"]
    assert "drive_path is the file relative to your drive" in description
    assert "If the save fails, save_message says so and the image is still returned." in description


def test_action_result_guidance_comes_from_results_resources():
    assert render_tool_result("handoff", {"status": "saved", "next": "wait"}) == render(
        "controls", "saved"
    )
    assert render_tool_result("claim", {"status": "claimed", "messages": []}).startswith(
        render("results", "claim_reading")
    )
    assert render_tool_result("peek", {}).endswith(render("results", "no_pending_mail"))


def test_saved_handoff_orders_distinct_controls_once_and_omits_checklist():
    rendered = render_tool_result(
        "handoff",
        {
            "status": "saved",
            "next": "wait",
            "context_pressure": {
                "level": "handoff_required",
                "instruction": "Working memory is full.",
            },
            "operations_control": {
                "instruction": "The owner is pausing the firm.",
                "handoff_checklist": "FULL HANDOFF CHECKLIST",
            },
        },
    )
    pressure = "Operations: Working memory is full."
    drain = "Operations: The owner is pausing the firm."
    saved = render("controls", "saved")
    assert rendered.index(pressure) < rendered.index(drain) < rendered.index(saved)
    assert rendered.count(saved) == 1
    assert "FULL HANDOFF CHECKLIST" not in rendered

    duplicate = render_tool_result(
        "peek",
        {
            "context_pressure": {"level": "advisory", "instruction": "Same instruction."},
            "operations_control": {"instruction": "Same instruction."},
        },
    )
    assert duplicate.count("Operations: Same instruction.") == 1
