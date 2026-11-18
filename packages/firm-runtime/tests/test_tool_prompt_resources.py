import json
from importlib.resources import files

import pytest

from firm_runtime.errors import ConfigurationError, FirmError
from firm_runtime.mcp_server import MANDATE_GUIDANCE, TOOL_DESCRIPTIONS, _rendered
from firm_runtime.prompt_resources import prompt, render
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store


def common_standing() -> str:
    """Common standing resources, for wording that must hold across all of them."""
    return "\n\n".join(prompt(n) for n in (
        "standing-physics.md", "standing-good-worker.md", "standing-limits.md"
    ))


def test_tool_descriptions_are_resource_backed_and_keep_registered_operations():
    resources = json.loads(prompt("tools.json"))
    expected_names = {
        "task",
        "send",
        "reply",
        "peek",
        "introduce",
        "read",
        "dismiss",
        "clear",
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
        "network_host", "github_repository", "resource", "pending", "cause",
        "access_kind_unknown", "access_resource_invalid", "github_not_configured",
        "github_unavailable", "access_denied",
    ):
        assert term in text
    assert "finish normally" not in text
    assert "finish normally" in render("results", "access_pending")
    assert "hand off only when you need a fresh start" in render("results", "access_pending")
    assert "ask the owner" not in text.lower() and "host" not in text.split("Kinds:")[0]


def test_mail_tool_descriptions_explain_deliverable_attachments():
    assert "attachments are files the reader should open" in TOOL_DESCRIPTIONS["send"]
    assert "absolute paths under your drive" in TOOL_DESCRIPTIONS["send"]
    assert "attachments work as in send" in TOOL_DESCRIPTIONS["reply"]
    assert "attachments" not in TOOL_DESCRIPTIONS["introduce"]


def test_standing_and_starter_mandate_explain_deliverable_attachments():
    standing = " ".join(prompt("standing-physics.md").split())
    assert "Attach deliverables they should open with send or reply" in standing
    assert "using their absolute paths under your drive" in standing
    starter = " ".join(
        files("firm_runtime").joinpath("resources/starter_mandate.md").read_text().split()
    )
    assert "message the owner with the file attached" in starter
    assert "using its absolute path under your drive" in starter


def test_role_text_describes_pending_answers_and_never_sends_a_role_to_the_owner():
    common = " ".join(common_standing().split()).lower()
    assert "tell your manager so the owner" not in common
    assert "ask the owner" not in common
    assert "saves the image" not in common
    standing = " ".join(prompt("standing-physics.md").split())
    assert "answer `pending` with a request_id and a cause" in standing
    assert "Do not ask for it again" in render(
        "notices", "access_denied", resource="host", reason="declined"
    )
    assert "`access_denied`" not in common
    assert "finish normally to pause while retaining working memory" in standing
    assert "Set a reminder when it serves a real future check" in standing
    assert "public services that need no account" in standing
    assert "owner-specified limits" in prompt("standing-limits.md")
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
    rendered = render_tool_result("read", {
        "status": "read", "subject": "Literal", "message_id": "m1", "reader_id": "e1",
        "participants": [], "marked_read": [], "remaining": 0, "reread": True,
        "messages": [{"message_id": "m1", "author": {"employee_id": "founder"},
                      "created_at": 1700000000,
                      "body": marker}],
    })
    assert marker in rendered


def test_mac_guidance_describes_the_account_and_shared_machine_boundaries():
    standing = common_standing()
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
    assert render_tool_result("peek", {}).endswith(render("results", "no_unread_mail"))


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



def test_handoff_cleanup_parameter_and_description_contract(tmp_path):
    from firm_runtime.prompt_records import role_tool_catalog

    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    tool = next(tool for tool in role_tool_catalog(runtime) if tool["name"] == "handoff")
    parameter = tool["inputSchema"]["properties"]["clean_up"]
    assert parameter["type"] == "boolean" and parameter["default"] is False
    assert "clean_up" not in tool["inputSchema"].get("required", [])
    assert parameter["description"] == (
        "Before your next work period, stopped Docker containers, dangling images and "
        "build cache are removed and scratch is emptied; tagged images and volumes stay.")
    assert (
        "Set clean_up only when work in scratch and Docker is finished; leave it unset "
        "mid-task and name anything in scratch the next period relies on in your notes. "
        "Identical retries apply within this period."
    ) in TOOL_DESCRIPTIONS["handoff"]
    assert "dangling images" not in TOOL_DESCRIPTIONS["handoff"]



def test_handoff_keeps_baseline_mcp_annotations(tmp_path):
    from firm_runtime.mcp_server import build_server

    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    server = build_server(runtime, "")
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "handoff")
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    assert tool.annotations.openWorldHint is False


def test_disk_owner_mails_are_resource_backed():
    resources = json.loads(prompt("notices.json"))
    assert resources["linux_disk_low_subject"] == "Linux disk low"
    assert resources["linux_disk_recovered_subject"] == "Linux disk recovered"


def test_physics_and_limits_do_not_duplicate_authority_rules():
    physics = " ".join(prompt("standing-physics.md").split())
    limits = " ".join(prompt("standing-limits.md").split())
    for phrase in ("tool failure grants no authority", "Do not make purchases",
                   "Never print, copy or save a secret", "Never create an account",
                   "owner-specified limits", "Material published in the firm's name",
                   "signed, accepted or agreed"):
        assert phrase in limits
        assert phrase not in physics
    assert "access_denied" not in common_standing()


def test_prompts_use_us_english_and_task_worker_prompt_is_resource_backed():
    import re
    from pathlib import Path

    from firm_runtime.task_runner import WORKER_INSTRUCTIONS

    assert WORKER_INSTRUCTIONS == prompt("runtime_task.md")
    uk_pattern = re.compile(
        r"\b(?:"
        r"behaviours?|organis(?:e|ed|ing|ations?)|"
        r"(?:un)?authoris(?:e|ed|ing)|"
        r"recognis(?:e|ed|ing|able)|"
        r"labell(?:ed|ing)|"
        r"cancell(?:ed|ing)|"
        r"honour(?:s|ed|ing)?|"
        r"labour(?:s|ed|ing)?|"
        r"artefacts?|"
        r"defences?|"
        r"fulfil(?:ment)?|"
        r"licences?|"
        r"specialis(?:e|ed|ing)|"
        r"synthesising|"
        r"favoured|"
        r"neighbour(?:ing|s)?|"
        r"acknowledgements?"
        r")\b",
        re.IGNORECASE,
    )
    prompts_dir = Path(__file__).parents[1] / "src/firm_runtime/prompts"
    offenders = {}
    for path in sorted(prompts_dir.rglob("*")):
        if not path.is_file():
            continue
        matches = uk_pattern.findall(path.read_text(encoding="utf-8"))
        if matches:
            offenders[str(path.relative_to(prompts_dir))] = matches
    assert offenders == {}
