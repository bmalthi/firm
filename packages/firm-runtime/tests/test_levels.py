"""Runtime-chosen hiring levels: config, admission gating, decisions and fallback."""

from __future__ import annotations

import copy
import json
import threading

import pytest

from firm_runtime import guidance, levels
from firm_runtime.errors import FirmError
from firm_runtime.runtime import Runtime
from firm_runtime.simulation import ControlledExecutor, SimulationDispatcher
from firm_runtime.store import OWNER, Store
from firm_runtime.summaries import MAX_SUMMARY_PROMPT_BYTES, SummaryOutput, SummaryService

CONFIG = {
    "max_active": 4,
    "profiles": {
        "fable": {"model": "fable", "effort": "high"},
        "opus": {"model": "opus", "effort": "medium"},
        "sonnet": {"model": "sonnet", "effort": "low"},
        "chooser": {"provider": "claude", "model": "sonnet", "effort": "medium"},
    },
    "staffing": {
        "profile": "chooser",
        "classes": {"high": "fable", "medium": "opus", "low": "sonnet"},
    },
}


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build useful things.", copy.deepcopy(CONFIG))
    return runtime, now


def role(runtime, employee_id):
    with runtime.store.read() as db:
        return runtime.store.role(db, employee_id)


def record(runtime, employee_id):
    with runtime.store.read() as db:
        return runtime.levels.record(db, employee_id)


def guide_record(runtime, employee_id="founder"):
    with runtime.store.read() as db:
        return guidance.record(db, employee_id)


def events(runtime, kind):
    with runtime.store.read() as db:
        return [
            json.loads(r["payload_json"])
            for r in db.execute("SELECT payload_json FROM analysis_events WHERE kind=?", (kind,))
        ]


def hire_through_founder(runtime, dispatcher, slug="finance", mandate="Prepare invoices.", **kw):
    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    (founder,) = dispatcher.tick()
    return runtime.tools(founder.token).hire(slug, mandate, f"hire-{slug}", **kw)["employee_id"]


def test_founder_defaults_to_high_class_at_high_effort(system):
    runtime, _ = system
    founder = role(runtime, "founder")
    assert (founder["execution_profile"], founder["effort"]) == ("fable", "high")


def test_explicit_founder_profile_keeps_its_own_effort(tmp_path):
    config = {**copy.deepcopy(CONFIG), "founder_profile": "sonnet"}
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build useful things.", config)
    founder = role(runtime, "founder")
    assert (founder["execution_profile"], founder["effort"]) == ("sonnet", None)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_omitted_staffing_enables_chooser_with_same_provider_and_accounts(tmp_path, provider):
    config = {
        "max_active": 2,
        "founder_profile": "main",
        "profiles": {
            "main": {
                "provider": provider,
                "model": "owner-model",
                "effort": "low",
                "accounts": ["personal"],
                "context_window": 256000,
            },
        },
        "accounts": {
            "personal": {"provider": provider, "auth_file": "/tmp/auth.json", "max_active": 2},
        },
    }
    original = copy.deepcopy(config)
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    result = runtime.lifecycle.initialize("test", "Build.", config)
    resolved = result["firm"]["config"]
    assert config == original, "default expansion must not modify the caller's config"
    assert runtime.lifecycle.initialize("test", "Build.", config) == result
    staffing = resolved["staffing"]
    chooser = resolved["profiles"][staffing["profile"]]
    assert chooser["provider"] == provider
    assert chooser["accounts"] == ["personal"]
    assert chooser["effort"] == "medium"
    assert [resolved["profiles"][staffing["classes"][c]]["model"] for c in levels.CLASSES] == list(
        {"codex": ("gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"),
         "claude": ("sonnet", "opus", "fable")}[provider]
    )
    assert all(p["provider"] == provider for p in resolved["profiles"].values())
    assert (role(runtime, "founder")["execution_profile"], role(runtime, "founder")["effort"]) == (
        "main",
        None,
    )
    analyst = runtime.lifecycle.hire("founder", "reviewer", "Review the memo.", request_id="h")[
        "employee_id"
    ]
    assert record(runtime, analyst)["state"] == "pending"
    runtime.levels.chooser = lambda _: {
        "scope_guidance": None, "functional_guidance": [],
        "model_class": "low",
        "effort": "low",
        "reason": "Small review.",
    }
    runtime.start()
    runtime.levels.tick(background=False)
    assert record(runtime, analyst)["state"] == "chosen"
    assert (role(runtime, analyst)["execution_profile"], role(runtime, analyst)["effort"]) == (
        staffing["classes"]["low"],
        "low",
    )


def test_default_staffing_can_choose_founder_from_single_profile(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "test",
        "Build.",
        {
            "max_active": 2,
            "profiles": {"main": {"provider": "codex", "model": "owner-model"}},
        },
    )
    assert (role(runtime, "founder")["execution_profile"], role(runtime, "founder")["effort"]) == (
        "staffing-high",
        "high",
    )


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda c: c.update(founder_profile=[]), "founder_profile or default_profile"),
        (lambda c: c.update(default_profile=[]), "founder_profile or default_profile"),
        (lambda c: c["profiles"]["main"].update(alternates=1), "default alternates"),
        (lambda c: c["profiles"]["main"].update(alternates=[[]]), "default alternates"),
    ],
)
def test_default_staffing_rejects_malformed_route_names(tmp_path, edit, message):
    config = {
        "max_active": 2,
        "profiles": {"main": {"provider": "codex", "model": "owner-model"}},
    }
    edit(config)

    with pytest.raises(FirmError, match=message):
        Runtime(Store.scratch(tmp_path / "firm.sqlite3")).lifecycle.initialize(
            "test", "Build.", config
        )


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda c: c["staffing"]["classes"].pop("low"), "low, medium and high"),
        (lambda c: c["staffing"]["classes"].update(high="missing"), "configured profile"),
        (
            lambda c: c["profiles"]["chooser"].update(effort="ultra"),
            "unsupported staffing profile effort",
        ),
        (
            lambda c: c["profiles"]["chooser"].update(effort=[]),
            "unsupported staffing profile effort",
        ),
        (lambda c: c["profiles"]["chooser"].update(provider="gemini"), "codex, claude or agy"),
        (lambda c: c["profiles"]["chooser"].update(provider=[]), "codex, claude or agy"),
        (lambda c: c["staffing"].update(profile="missing"), "configured profile"),
        (lambda c: c.pop("staffing"), "configure \\[staffing\\]"),
        (lambda c: c.update(staffing=None), "configure \\[staffing\\]"),
        (lambda c: c.update(staffing=True), "staffing accepts"),
    ],
)
def test_staffing_configuration_is_validated(tmp_path, edit, message):
    config = copy.deepcopy(CONFIG)
    edit(config)
    with pytest.raises(FirmError, match=message):
        Runtime(Store.scratch(tmp_path / "firm.sqlite3")).lifecycle.initialize(
            "t", "Build.", config
        )


def test_hire_waits_for_the_chooser_then_starts_at_the_chosen_level(system):
    runtime, _ = system
    prompts = []

    def chooser(prompt):
        prompts.append(json.loads(prompt))
        return {
            "scope_guidance": None,
            "functional_guidance": [],
            "model_class": "low",
            "effort": "medium",
            "reason": "Routine invoicing.",
        }

    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher, temporary_periods=3)
        assert record(runtime, analyst)["state"] == "pending"
        pending = role(runtime, analyst)
        assert (pending["execution_profile"], pending["effort"]) == ("opus", "high")
        # Nothing may run the hire before a decision; the chooser is not configured yet.
        assert dispatcher.tick() == []
        assert record(runtime, analyst)["attempts"] == 1
        runtime.levels.chooser = chooser
        with runtime.store.transaction() as db:
            db.execute("UPDATE hire_levels SET retry_at=NULL")
        (started,) = dispatcher.tick()
        assert started.employee_id == analyst

    chosen = role(runtime, analyst)
    assert (chosen["execution_profile"], chosen["effort"]) == ("sonnet", "medium")
    decision = record(runtime, analyst)
    assert (decision["state"], decision["model_class"], decision["effort"]) == (
        "chosen",
        "low",
        "medium",
    )
    assert decision["reason"] == "Routine invoicing."
    (event,) = events(runtime, "level.chosen")
    assert (event["model_class"], event["effort"], event["profile"]) == ("low", "medium", "sonnet")

    (prompt,) = prompts
    assert prompt["hire"] == {
        "address": "founder/finance",
        "employment": "temporary",
        "temporary_work_periods": 3,
        "mandate": "Prepare invoices.",
    }
    assert prompt["hiring_manager"]["address"] == "founder"
    assert prompt["hiring_manager"]["level"] == {"model_class": "high", "effort": "high"}
    assert prompt["hiring_manager"]["mandate"] == "Build useful things."
    assert prompt["hiring_manager"]["mandate_truncated"] is False
    assert prompt["managers_manager"]["address"] == "owner"
    assert prompt["existing_reports_of_hiring_manager"] == []
    assert prompt["existing_reports_total"] == 0
    assert prompt["existing_reports_included"] == 0
    assert prompt["existing_reports_omitted"] == 0
    assert prompt["levels_available"]["effort"] == ["low", "medium", "high", "xhigh"]


def test_prompt_describes_peers_and_the_managers_manager(system):
    runtime, _ = system
    runtime.levels.chooser = lambda prompt: {
        "scope_guidance": None,
        "functional_guidance": [],
        "model_class": "medium",
        "effort": "low",
    }
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        lead = hire_through_founder(runtime, dispatcher, "lead", "Lead the studio.")
        dispatcher.tick()
        (lead_run,) = [i for i in dispatcher.executor.running.values() if i.employee_id == lead]
        lead_tools = runtime.tools(lead_run.token)
        assert lead_tools.hire("artist", "Draw sprites.", "hire-artist")["status"] == "pending"
        prompts = []

        def chooser(prompt):
            prompts.append(json.loads(prompt))
            return {
                "scope_guidance": None,
                "functional_guidance": [],
                "model_class": "low",
                "effort": "low",
            }

        runtime.levels.chooser = chooser
        dispatcher.tick()
        # A sibling hired later sees the artist as an existing report with an excerpt.
        lead_tools.hire("coder", "Write the game loop.", "hire-coder")
        dispatcher.tick()

    assert [p["hire"]["address"] for p in prompts] == ["founder/lead/artist", "founder/lead/coder"]
    second = prompts[1]
    assert second["hiring_manager"]["address"] == "founder/lead"
    assert second["hiring_manager"]["level"] == {"model_class": "medium", "effort": "low"}
    assert second["managers_manager"]["address"] == "founder"
    assert second["managers_manager"]["level"] == {"model_class": "high", "effort": "high"}
    assert second["existing_reports_of_hiring_manager"] == [
        {
            "address": "founder/lead/artist",
            "level": {"model_class": "low", "effort": "low"},
            "mandate_excerpt": "Draw sprites.",
            "mandate_excerpt_truncated": False,
        }
    ]
    assert second["existing_reports_total"] == 1
    assert second["existing_reports_included"] == 1
    assert second["existing_reports_omitted"] == 0
    assert role(runtime, lead)["effort"] == "low"


def test_build_prompt_preserves_hire_mandate_and_bounds_contextual_mandates(tmp_path):
    manager_constraint = "Only use artwork with a commercial redistribution license."
    founder_constraint = "Products must work without any network connection."
    manager_mandate = "manager-start\n" + "m" * 6000 + manager_constraint + "m" * 2100
    founder_mandate = "owner-start\n" + "o" * 6000 + founder_constraint + "o" * 2100
    hire_mandate = "{literal}\n" + "h" * 9000 + "\n{{placeholder}}"
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", founder_mandate, copy.deepcopy(CONFIG))
    manager = runtime.lifecycle.hire("founder", "lead", manager_mandate, request_id="hire-lead")
    analyst = runtime.lifecycle.hire(
        manager["employee_id"], "analyst", hire_mandate, request_id="hire-analyst"
    )

    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
        analyst_row = runtime.store.role(db, analyst["employee_id"])
        prompt = json.loads(levels.build_prompt(db, runtime.store, analyst_row, config)[0])

    assert prompt["hire"]["mandate"] == hire_mandate
    assert len(prompt["hiring_manager"]["mandate"]) == 8000
    assert manager_constraint in prompt["hiring_manager"]["mandate"]
    assert prompt["hiring_manager"]["mandate"].startswith("manager-start\n")
    assert prompt["hiring_manager"]["mandate"].endswith(levels.TRUNCATION_MARKER)
    assert prompt["hiring_manager"]["mandate_truncated"] is True
    assert len(prompt["managers_manager"]["mandate"]) == 8000
    assert founder_constraint in prompt["managers_manager"]["mandate"]
    assert prompt["managers_manager"]["mandate"].startswith("owner-start\n")
    assert prompt["managers_manager"]["mandate"].endswith(levels.TRUNCATION_MARKER)
    assert prompt["managers_manager"]["mandate_truncated"] is True


def test_build_prompt_describes_only_a_current_temporary_appointment(system):
    runtime, _ = system
    runtime.start()
    hired = runtime.lifecycle.hire(
        "founder", "analyst", "Do the work.", request_id="hire-analyst", temporary_periods=3
    )

    def hire_terms():
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
            row = runtime.store.role(db, hired["employee_id"])
            return json.loads(levels.build_prompt(db, runtime.store, row, config)[0])["hire"]

    temporary = hire_terms()
    assert temporary["employment"] == "temporary"
    assert temporary["temporary_work_periods"] == 3
    runtime.appointments.promote("founder", hired["employee_id"], "promote-analyst")
    durable = hire_terms()
    assert durable["employment"] == "durable"
    assert "temporary_work_periods" not in durable


def test_build_prompt_reports_omitted_peers_and_stable_earliest_order(system):
    runtime, now = system
    runtime.start()
    manager = runtime.lifecycle.hire("founder", "lead", "Manage the team.", request_id="hire-lead")
    for index in range(levels.MAX_REPORTS + 2):
        # Equal timestamps exercise the explicit address tie-breaker.
        now[0] = 1000.0
        peer = runtime.lifecycle.hire(
            manager["employee_id"],
            f"peer-{index:02d}",
            f"Peer mandate {index}",
            request_id=f"hire-peer-{index}",
        )
        assert peer["status"] == "pending"
    analyst = runtime.lifecycle.hire(
        manager["employee_id"], "analyst", "Do the work.", request_id="hire-analyst"
    )

    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
        role_row = runtime.store.role(db, analyst["employee_id"])
        first = json.loads(levels.build_prompt(db, runtime.store, role_row, config)[0])
        second = json.loads(levels.build_prompt(db, runtime.store, role_row, config)[0])

    peers = first["existing_reports_of_hiring_manager"]
    assert first == second
    assert first["existing_reports_total"] == levels.MAX_REPORTS + 2
    assert first["existing_reports_included"] == levels.MAX_REPORTS
    assert first["existing_reports_omitted"] == 2
    assert len(peers) == levels.MAX_REPORTS
    assert [item["address"] for item in peers] == [
        f"founder/lead/peer-{index:02d}" for index in range(levels.MAX_REPORTS)
    ]


def test_chooser_failures_retry_then_default_with_an_owner_notice(system):
    runtime, now = system

    def broken(prompt):
        raise ValueError("chooser exploded")

    runtime.levels.chooser = broken
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher)
        for attempt in (1, 2):
            assert dispatcher.tick() == []
            current = record(runtime, analyst)
            assert (current["state"], current["attempts"]) == ("pending", attempt)
            assert current["retry_at"] == now[0] + levels.RETRY_DELAY_SECONDS * attempt
            assert current["reason"] == "chooser exploded"
            # Not due yet: the pending role stays out of admission and is not retried.
            assert dispatcher.tick() == []
            now[0] = current["retry_at"]
        (started,) = dispatcher.tick()
        assert started.employee_id == analyst

    defaulted = record(runtime, analyst)
    assert (defaulted["state"], defaulted["model_class"], defaulted["effort"]) == (
        "defaulted",
        "medium",
        "high",
    )
    fallback = role(runtime, analyst)
    assert (fallback["execution_profile"], fallback["effort"]) == ("opus", "high")
    (event,) = events(runtime, "level.defaulted")
    assert event["attempts"] == 3
    with runtime.store.read() as db:
        (notice,) = db.execute(
            "SELECT subject,body,recipients_json FROM messages WHERE body LIKE ?",
            ("Hire level defaulted\n\n%",),
        ).fetchall()
    assert "founder/finance" in notice["body"] and "medium class at high effort" in notice["body"]
    assert OWNER in notice["recipients_json"]


def test_isolated_chooser_failure_records_the_provider_reason_through_to_the_notice(system):
    runtime, now = system

    class Runner:
        def start(self, request):
            return request

        def wait(self, handle, timeout):
            return SummaryOutput("", error="process_exit")

        def stop(self, handle):
            return True

    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    runtime.summary_service.provider = Runner()
    runtime.start()
    employee_id = runtime.lifecycle.hire(
        "founder", "analyst", "Analyse.", request_id="hire-analyst"
    )["employee_id"]
    for attempt in range(1, levels.MAX_ATTEMPTS + 1):
        assert runtime.levels.tick(background=False) == [employee_id]
        current = record(runtime, employee_id)
        assert (current["attempts"], current["reason"]) == (attempt, "process_exit")
        if current["retry_at"] is not None:
            now[0] = current["retry_at"]
    assert current["state"] == "defaulted"
    with runtime.store.read() as db:
        reasons = {
            tuple(row)
            for row in db.execute("SELECT kind,state,reason FROM summary_runs").fetchall()
        }
        (notice,) = db.execute(
            "SELECT body FROM messages WHERE body LIKE 'Hire level defaulted\n\n%'"
        ).fetchall()
    assert reasons == {("staffing", "unavailable", "process_exit")}
    assert "Last reason: process_exit." in notice["body"]


@pytest.mark.parametrize("mandate", ["A" * 70_000, "界" * 30_000], ids=["ascii", "utf8"])
def test_large_hire_mandate_reaches_isolated_chooser_complete(system, mandate):
    runtime, _ = system
    requests = []

    class Runner:
        def start(self, request):
            requests.append(request)
            return request

        def wait(self, handle, timeout):
            return SummaryOutput(
                '{"scope_guidance": null, "functional_guidance": [], '
                '"model_class":"high","effort":"xhigh"}',
                output_tokens=12,
            )

        def stop(self, handle):
            return True

    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    runtime.summary_service.provider = Runner()
    runtime.start()
    analyst = runtime.lifecycle.hire("founder", "analyst", mandate, request_id="hire-analyst")
    employee_id = analyst["employee_id"]
    assert runtime.levels.tick(background=False) == [employee_id]
    decision = record(runtime, employee_id)
    assert (decision["state"], decision["attempts"], decision["retry_at"]) == ("chosen", 1, None)
    assert (decision["model_class"], decision["effort"]) == ("high", "xhigh")
    (request,) = requests
    assert json.loads(request.prompt)["hire"]["mandate"] == mandate
    assert MAX_SUMMARY_PROMPT_BYTES < len(request.prompt.encode()) <= request.max_input_bytes
    assert request.max_input_bytes == levels.MAX_STAFFING_PROMPT_BYTES
    assert request.kind == "staffing"
    with runtime.store.read() as db:
        assert levels._mandate_body(db, employee_id) == mandate
        assert db.execute("SELECT state FROM summary_runs").fetchone()[0] == "summarized"
    assert runtime.summary_service.inflight() == set()


@pytest.mark.parametrize("injected", [False, True], ids=["isolated", "injected"])
@pytest.mark.parametrize("character", ["a", "💼"], ids=["ascii", "utf8"])
def test_oversized_hire_defaults_once_without_reserving_or_rewriting_mandate(
    system, monkeypatch, injected, character
):
    runtime, now = system
    mandate = character * (levels.MAX_STAFFING_PROMPT_BYTES // len(character.encode()) + 1)

    def unexpected(*args):
        pytest.fail("oversized staffing input must fail before reservation or provider startup")

    class Runner:
        start = staticmethod(unexpected)

    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    runtime.summary_service.provider = Runner()
    monkeypatch.setattr(runtime.accounts, "reserve_staffing", unexpected)
    if injected:
        runtime.levels.chooser = unexpected
    runtime.start()
    employee_id = runtime.lifecycle.hire("founder", "analyst", mandate, request_id="hire-analyst")[
        "employee_id"
    ]
    assert runtime.levels.tick(background=False) == [employee_id]
    decision = record(runtime, employee_id)
    assert (decision["state"], decision["attempts"], decision["retry_at"]) == (
        "defaulted",
        1,
        None,
    )
    assert "UTF-8 bytes" in decision["reason"] and "262144 bytes" in decision["reason"]
    assert (decision["model_class"], decision["effort"]) == levels.FALLBACK_LEVEL
    guide = guide_record(runtime, employee_id)
    assert (guide["state"], guide["attempts"]) == ("defaulted", 1)
    assert guide["reason"] == decision["reason"]
    now[0] += 1000
    assert runtime.levels.tick(background=False) == []
    assert record(runtime, employee_id) == decision
    (event,) = events(runtime, "level.defaulted")
    assert event["attempts"] == 1 and event["reason"] == decision["reason"]
    with runtime.store.read() as db:
        assert levels._mandate_body(db, employee_id) == mandate
        assert db.execute("SELECT COUNT(*) FROM summary_runs").fetchone()[0] == 0
        (notice,) = db.execute(
            "SELECT body,recipients_json FROM messages WHERE body LIKE 'Hire level defaulted\n\n%'"
        ).fetchall()
        assert "Failed assessments: 1." in notice["body"]
        assert decision["reason"] in notice["body"] and OWNER in notice["recipients_json"]
    assert runtime.summary_service.inflight() == set()
    assert runtime.summary_service._slots.acquire(blocking=False)
    assert runtime.summary_service._slots.acquire(blocking=False)
    runtime.summary_service._slots.release()
    runtime.summary_service._slots.release()


def test_invalid_chooser_replies_count_as_failed_attempts(system):
    runtime, _ = system
    runtime.levels.chooser = lambda prompt: {
        "scope_guidance": None,
        "functional_guidance": [],
        "model_class": "enormous",
        "effort": "low",
    }
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher)
        dispatcher.tick()
    current = record(runtime, analyst)
    assert (current["state"], current["attempts"]) == ("pending", 1)
    assert "model_class" in current["reason"]


def test_owner_routing_overrides_the_choice_and_clears_effort(system):
    runtime, _ = system
    runtime.levels.chooser = lambda prompt: {
        "scope_guidance": None,
        "functional_guidance": [],
        "model_class": "low",
        "effort": "xhigh",
    }
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher)
        # Closing the dispatcher stops the firm, and a stopped firm assesses nothing.
        runtime.levels.tick(background=False)
        assert role(runtime, analyst)["effort"] == "xhigh"
    runtime.owner.route(OWNER, analyst, "fable", "route-1")
    routed = role(runtime, analyst)
    assert (routed["execution_profile"], routed["effort"]) == ("fable", None)
    assert record(runtime, analyst)["state"] == "routed"


def test_a_late_decision_never_overwrites_an_owner_route(system):
    runtime, _ = system
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher)
    runtime.owner.route(OWNER, analyst, "fable", "route-1")
    runtime.levels._apply(analyst, "low", "low", "late", request=record(runtime, analyst))
    assert role(runtime, analyst)["execution_profile"] == "fable"
    assert record(runtime, analyst)["state"] == "routed"


def test_explicit_fixed_hiring_takes_the_default_profile(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    config = {
        "max_active": 2,
        "staffing": False,
        "founder_profile": "fable",
        "default_profile": "sonnet",
        "profiles": {"fable": {"model": "fable"}, "sonnet": {"model": "sonnet"}},
    }
    runtime.lifecycle.initialize("test", "Build useful things.", config)
    analyst = runtime.lifecycle.hire("founder", "w", "Work.", request_id="h")["employee_id"]
    assert record(runtime, analyst) is None
    hired = role(runtime, analyst)
    assert (hired["execution_profile"], hired["effort"]) == ("sonnet", None)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"model_class": "high", "effort": "xhigh", "reason": "Novel work."}', ("high", "xhigh")),
        ('Sure.\n```json\n{"model_class":"low","effort":"low"}\n```', ("low", "low")),
    ],
)
def test_parse_choice_accepts_a_json_object_with_surrounding_text(text, expected):
    assert levels.parse_choice(text)[:2] == expected


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        '{"model_class": "high"}',
        '{"model_class": "high", "effort": "max"}',
        '{"model_class": "high", "effort": "high", "reason": null}',
        "[1]",
    ],
)
def test_parse_choice_rejects_unusable_replies(text):
    with pytest.raises(ValueError):
        levels.parse_choice(text)


def test_isolated_staffing_turn_carries_instructions_kind_and_reservation(system):
    runtime, _ = system
    seen = []

    class Runner:
        def start(self, request):
            seen.append(request)
            return request

        def wait(self, handle, timeout):
            return SummaryOutput('{"model_class":"medium","effort":"high"}', output_tokens=12)

        def stop(self, handle):
            return True

        def active_run_ids(self):
            return set()

    runtime.start()
    service = SummaryService(Runner(), store=runtime.store, capacity=runtime.accounts)
    runtime.summary_service = service
    assert runtime.levels._choose("{}") == {"model_class": "medium", "effort": "high"}
    (request,) = seen
    assert request.kind == "staffing" and request.instructions
    assert request.run_id.startswith("run-")
    assert request.max_input_bytes == levels.MAX_STAFFING_PROMPT_BYTES
    assert request.timeout == levels.CHOOSER_TIMEOUT_SECONDS == 120.0
    with runtime.store.read() as db:
        (row,) = db.execute("SELECT kind,state,output_tokens FROM summary_runs").fetchall()
    assert tuple(row) == ("staffing", "summarized", 12)


def test_binding_and_staffing_reservation_use_configured_accounts(tmp_path):
    config = {
        "max_active": 4,
        "accounts": {
            "a": {"provider": "claude", "auth_file": "/missing/a", "max_active": 2},
            "b": {"provider": "claude", "auth_file": "/missing/b", "max_active": 1},
        },
        "profiles": {
            name: {"provider": "claude", "model": name, "effort": "low", "accounts": ["a"]}
            for name in ("fable", "opus", "sonnet")
        },
        "staffing": {
            "profile": "chooser",
            "classes": {"high": "fable", "medium": "opus", "low": "sonnet"},
        },
    }
    config["profiles"]["chooser"] = {
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
        "accounts": ["b"],
    }
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build useful things.", config)
    runtime.start()
    # The founder's high-effort default overrides its profile's low effort at binding.
    founder = runtime.admit()
    with runtime.store.transaction() as db:
        binding = runtime.accounts.binding(db, founder.session_id)
        reservation = runtime.accounts.reserve_staffing(db, "staffing-1")
    assert json.loads(binding["settings_json"])["effort"] == "high"
    assert reservation == {
        "account_id": "b",
        "profile": "chooser",
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
    }


def test_a_decision_is_not_applied_against_configuration_that_changed(system, monkeypatch):
    runtime, _ = system
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    model_class = next(iter(config["staffing"]["classes"]))
    current = copy.deepcopy(config)
    current["staffing"]["classes"] = {}
    runtime.start()
    with SimulationDispatcher(runtime, ControlledExecutor()) as dispatcher:
        analyst = hire_through_founder(runtime, dispatcher)
    monkeypatch.setattr(runtime.accounts, "config", lambda db: current)
    with pytest.raises(FirmError, match="no longer maps"):
        runtime.levels._apply(analyst, model_class, "high", "why", request=record(runtime, analyst))
    assert record(runtime, analyst)["state"] == "pending"


def guidance_reply(**changes):
    return {"model_class": "low", "effort": "low", "scope_guidance": None,
            "functional_guidance": [], "reason": "Fits the mandate.", **changes}


def guide_hire(runtime, **kwargs):
    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    return runtime.lifecycle.hire("founder", "worker", "Do the work.", request_id="hire", **kwargs)[
        "employee_id"
    ]


def test_founder_and_explicit_temporary_hires_request_guidance_at_revision_zero(system):
    runtime, _ = system
    assert guide_record(runtime, "founder")["state"] == "pending"
    seen = []
    runtime.levels.chooser = lambda p: seen.append(json.loads(p)) or guidance_reply()
    employee = runtime.lifecycle.hire("founder", "worker", "Do the work.", "sonnet",
                                      request_id="h", temporary_periods=2)["employee_id"]
    assert guide_record(runtime, employee)["revision"] == 0
    assert record(runtime, employee) is None
    runtime.start()
    runtime.levels.tick(background=False)
    founder = next(p for p in seen if p["hire"]["address"] == "founder")
    assert founder["hiring_manager"] == {"address": "owner", "note": "The firm's owner."}
    assert founder["managers_manager"] is None
    assert founder["existing_reports_of_hiring_manager"] == []
    assert founder["existing_reports_total"] == 0
    assert all("levels_available" not in p for p in seen)
    assert guide_record(runtime, employee)["state"] == "chosen"
    runtime.lifecycle.hire("founder", "worker", "Do the work.", "sonnet", request_id="h",
                           temporary_periods=2)
    assert guide_record(runtime, employee)["state"] == "chosen"
    runtime.lifecycle.revise_mandate("founder", employee, 0, "Different work.", request_id="rev")
    runtime.levels.tick(background=False)
    current = (role(runtime, employee), guide_record(runtime, employee))
    runtime.lifecycle.revise_mandate("founder", employee, 0, "Different work.", request_id="rev")
    assert (role(runtime, employee), guide_record(runtime, employee)) == current


def test_chooser_input_has_only_descriptions_of_chooser_entries(system):
    from firm_runtime import guidance

    runtime, _ = system
    with runtime.store.read() as db:
        payload, revision = levels.build_prompt(db, runtime.store, role(runtime, "founder"),
                                                runtime.accounts.config(db), level=False)
    value = json.loads(payload)
    assert revision == 0
    assert "levels_available" not in value
    assert value["guidance_catalog"] == guidance.chooser_entries()
    assert not set(guidance.ORG_CHART_SCOPES) & {e["id"] for e in value["guidance_catalog"]}
    assert all(set(e) == {"id", "dimension", "title", "applies"} for e in value["guidance_catalog"])
    assert not any(guidance.body(e["id"]) in payload for e in guidance.chooser_entries())


@pytest.mark.parametrize("changes", [
    {"scope_guidance": "firm-leader"}, {"scope_guidance": "team-leader"},
    {"scope_guidance": "legal"}, {"scope_guidance": "unknown"}, {"scope_guidance": []},
    {"scope_guidance": "../legal"}, {"scope_guidance": 1},
    {"functional_guidance": ["firm-leader"]}, {"functional_guidance": ["unknown"]},
    {"functional_guidance": ["../legal"]}, {"functional_guidance": [None]},
    {"functional_guidance": "legal"}, {"functional_guidance": {}},
])
def test_guidance_parser_refuses_invalid_ids_and_dimensions(changes):
    from firm_runtime import guidance

    with pytest.raises(ValueError):
        guidance.parse_selection(guidance_reply(**changes), guidance.chooser_entries())


@pytest.mark.parametrize("key", ["scope_guidance", "functional_guidance"])
def test_guidance_parser_requires_both_fields(key):
    from firm_runtime import guidance

    value = guidance_reply()
    del value[key]
    with pytest.raises(ValueError):
        guidance.parse_selection(value, guidance.chooser_entries())


def test_guidance_dedup_catalog_order_and_bounded_choice_reasons(system):
    runtime, _ = system
    employee = guide_hire(runtime)
    reason = "界\n\"" * 2000
    runtime.levels.chooser = lambda _: guidance_reply(
        functional_guidance=["sales", "legal", "sales"], reason=reason
    )
    runtime.start()
    runtime.levels.tick(background=False)
    assert json.loads(role(runtime, employee)["functional_guidance"]) == ["legal", "sales"]
    bounded = levels._clip(reason, levels.MAX_CHOICE_REASON_CHARS)
    assert len(bounded) == levels.MAX_CHOICE_REASON_CHARS
    assert bounded.endswith(levels.TRUNCATION_MARKER)
    assert guide_record(runtime, employee)["reason"] == bounded
    assert record(runtime, employee)["reason"] == bounded
    for kind in ("level.chosen", "guidance.chosen"):
        (event,) = events(runtime, kind)
        assert event["reason"].endswith(levels.TRUNCATION_MARKER)
        assert len(json.dumps(event).encode()) < 4096


@pytest.mark.parametrize("invalid", ["level", "guidance"])
def test_decisions_validate_and_retry_independently_in_one_call(system, invalid):
    runtime, now = system
    employee = guide_hire(runtime)
    seen = []
    change = {"model_class": "invalid"} if invalid == "level" else {"scope_guidance": "legal"}
    runtime.levels.chooser = lambda p: seen.append(json.loads(p)) or guidance_reply(**change)
    runtime.start()
    runtime.levels.tick(background=False)
    assert len(seen) == 1
    assert "levels_available" in seen[0] and "guidance_catalog" in seen[0]
    assert record(runtime, employee)["state"] == ("pending" if invalid == "level" else "chosen")
    assert guide_record(runtime, employee)["state"] == (
        "chosen" if invalid == "level" else "pending"
    )
    now[0] += 60
    runtime.levels.chooser = lambda p: (
        seen.append(json.loads(p))
        or guidance_reply(
            **({"scope_guidance": "invalid"} if invalid == "level" else {"model_class": "invalid"})
        )
    )
    runtime.levels.tick(background=False)
    assert ("levels_available" in seen[1]) == (invalid == "level")
    assert ("guidance_catalog" in seen[1]) == (invalid == "guidance")
    assert guide_record(runtime, employee)["state"] == "chosen"
    assert record(runtime, employee)["state"] == "chosen"


def test_separate_due_times_and_owner_guidance_make_level_only_calls(system):
    runtime, now = system
    employee = guide_hire(runtime)
    seen = []
    runtime.levels.chooser = lambda p: seen.append(json.loads(p)) or guidance_reply()
    runtime.start()
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE role_guidance SET retry_at=? WHERE employee_id=?", (now[0] + 30, employee)
        )
    runtime.levels.tick(background=False)
    assert "guidance_catalog" not in seen[0]
    now[0] += 30
    runtime.levels.tick(background=False)
    assert "levels_available" not in seen[1]
    second = runtime.lifecycle.hire("founder", "second", "Work", request_id="h2")["employee_id"]
    runtime.owner.set_guidance(OWNER, second, None, [], "set-second")
    runtime.levels.tick(background=False)
    assert "guidance_catalog" not in seen[2]


@pytest.mark.parametrize("same", ["level", "function"])
def test_level_and_function_are_not_inferred_from_each_other(system, same):
    runtime, _ = system
    first = guide_hire(runtime)
    second = runtime.lifecycle.hire("founder", "second", "Work", request_id="h2")["employee_id"]
    replies = iter(
        [
            guidance_reply(functional_guidance=["legal"]),
            guidance_reply(
                model_class="low" if same == "level" else "high",
                functional_guidance=["sales"] if same == "level" else ["legal"],
            ),
        ]
    )
    runtime.levels.chooser = lambda _: next(replies)
    runtime.start()
    runtime.levels.tick(background=False)
    assert (record(runtime, first)["model_class"] == record(runtime, second)["model_class"]) == (
        same == "level"
    )
    assert (
        role(runtime, first)["functional_guidance"] == role(runtime, second)["functional_guidance"]
    ) == (same == "function")


def test_cheap_model_can_have_senior_scope_and_guidance_does_not_change_execution(system):
    runtime, _ = system
    employee = guide_hire(runtime)
    runtime.levels.chooser = lambda _: guidance_reply(scope_guidance="specialist-adviser")
    runtime.start()
    runtime.levels.tick(background=False)
    assert role(runtime, employee)["scope_guidance"] == "specialist-adviser"
    assert role(runtime, employee)["execution_profile"] == "sonnet"
    runtime.owner.automatic_guidance(OWNER, employee, "auto")
    runtime.levels.chooser = lambda _: guidance_reply(
        model_class="high", effort="xhigh", functional_guidance=["legal"]
    )
    runtime.levels.tick(background=False)
    assert (role(runtime, employee)["execution_profile"], role(runtime, employee)["effort"]) == (
        "sonnet",
        "low",
    )


def test_prompt_construction_failure_uses_both_retry_paths(system, monkeypatch):
    runtime, _ = system
    employee = guide_hire(runtime)
    runtime.levels.chooser = lambda _: pytest.fail("must not call chooser")
    runtime.start()

    def broken(*args, **kwargs):
        raise FirmError("prompt construction failed")

    monkeypatch.setattr(levels, "build_prompt", broken)
    runtime.levels.tick(background=False)
    assert record(runtime, employee)["attempts"] == 1
    assert guide_record(runtime, employee)["attempts"] == 1
    assert guide_record(runtime, employee)["reason"] == "prompt construction failed"


@pytest.mark.parametrize("action", ["revise", "fire", "owner", "set-clear"])
@pytest.mark.parametrize("failure", [False, True])
def test_inflight_guidance_success_and_failure_guards(system, action, failure):
    runtime, _ = system
    employee = guide_hire(runtime)
    runtime.start()

    def chooser(_):
        if action == "revise":
            runtime.lifecycle.revise_mandate("founder", employee, 0, "New work", request_id="rev")
        elif action == "fire":
            runtime.lifecycle.fire("founder", employee, reason="Finished", request_id="fire")
        else:
            runtime.owner.set_guidance(OWNER, employee, "team-leader", ["sales"], "set")
            if action == "set-clear":
                runtime.owner.automatic_guidance(OWNER, employee, "clear")
        if failure:
            raise FirmError("late failure")
        return guidance_reply(scope_guidance="specialist-adviser", functional_guidance=["legal"])

    runtime.levels.chooser = chooser
    runtime.levels.tick(background=False)
    result = role(runtime, employee)
    guide = guide_record(runtime, employee)
    if action == "revise":
        assert (
            guide["state"],
            guide["revision"],
            guide["attempts"],
        ) == ("pending", 1, 0)
        assert not events(runtime, "guidance.chosen") and not events(runtime, "guidance.defaulted")
    elif action == "fire":
        assert guide["state"] == "pending" and guide["attempts"] == 0
        assert record(runtime, employee)["state"] == "pending"
        assert record(runtime, employee)["attempts"] == 0
        assert not events(runtime, "level.chosen") and not events(runtime, "level.defaulted")
        assert not events(runtime, "guidance.chosen") and not events(runtime, "guidance.defaulted")
        with runtime.store.read() as db:
            assert not db.execute(
                "SELECT 1 FROM messages WHERE body LIKE 'Hire level defaulted\n\n%' "
                "OR body LIKE 'Role guidance not selected\n\n%'"
            ).fetchone()
    elif action == "owner":
        assert guide["state"] == "owner" and result["scope_guidance"] == "team-leader"
    else:
        assert guide["state"] == ("pending" if failure else "chosen")
        assert guide["attempts"] == 1


def test_duplicate_completions_are_guarded_by_captured_attempt_count(system):
    from firm_runtime import guidance

    runtime, _ = system
    employee = guide_hire(runtime)
    level_request = record(runtime, employee)
    runtime.levels._failed(employee, "first", request=level_request)
    level_after = record(runtime, employee)
    runtime.levels._apply(employee, "high", "high", "duplicate", request=level_request)
    runtime.levels._failed(employee, "duplicate", request=level_request)
    assert record(runtime, employee) == level_after
    request = guide_record(runtime, employee)
    guidance.failed(runtime, employee, request, "first")
    after = (role(runtime, employee), guide_record(runtime, employee))
    guidance.failed(runtime, employee, request, "duplicate")
    guidance.apply(runtime, employee, request, {"scope": None, "functions": ["legal"]}, "duplicate")
    assert (role(runtime, employee), guide_record(runtime, employee)) == after


@pytest.mark.parametrize("scope", ["firm-leader", "specialist-adviser"])
def test_automatic_clears_owner_selection_and_uses_current_revision(system, scope):
    from firm_runtime import guidance

    runtime, now = system
    employee = guide_hire(runtime)
    runtime.owner.set_guidance(OWNER, employee, scope, ["legal"], "set")
    runtime.lifecycle.revise_mandate("founder", employee, 0, "New mandate", request_id="rev")
    assert guide_record(runtime, employee)["state"] == "owner"
    runtime.owner.automatic_guidance(OWNER, employee, "auto")
    assert guide_record(runtime, employee)["revision"] == 1
    runtime.owner.route(OWNER, employee, "sonnet", "route")
    runtime.start()

    def fail(_):
        raise FirmError("failure")

    runtime.levels.chooser = fail
    for attempt in range(4):
        with runtime.store.read() as db:
            assert guidance.effective(
                db, role(runtime, employee), runtime.accounts.config(db)
            ) == {"scope": None, "functions": []}
        if attempt < 3:
            runtime.levels.tick(background=False)
            now[0] += 60 * (attempt + 1)
    assert guide_record(runtime, employee)["state"] == "defaulted"
    runtime.owner.automatic_guidance(OWNER, employee, "again")
    assert guide_record(runtime, employee)["state"] == "pending"
    assert guide_record(runtime, employee)["attempts"] == 0


@pytest.mark.parametrize("level_due", [False, True])
def test_guidance_fallback_keeps_ids_and_sends_one_notice_then_no_retries(system, level_due):
    from firm_runtime import guidance

    runtime, now = system
    employee = guide_hire(runtime)
    guidance.apply(runtime, employee, guide_record(runtime, employee),
                   {"scope": "specialist-adviser", "functions": ["legal"]}, "chosen")
    with runtime.store.transaction() as db:
        guidance.request_reassessment(db, employee, 0)
    if not level_due:
        runtime.owner.route(OWNER, employee, "sonnet", "route")
    calls = []

    def fail(_):
        calls.append(1)
        raise FirmError("unavailable")

    runtime.levels.chooser = fail
    runtime.start()
    for attempt in (1, 2, 3):
        runtime.levels.tick(background=False)
        assert guide_record(runtime, employee)["attempts"] == attempt
        now[0] += 60 * attempt
    assert guide_record(runtime, employee)["state"] == "defaulted"
    assert role(runtime, employee)["scope_guidance"] == "specialist-adviser"
    with runtime.store.read() as db:
        (notice,) = db.execute("SELECT body FROM messages WHERE "
                              "body LIKE 'Hire level defaulted\n\n%' OR "
                              "body LIKE 'Role guidance not selected\n\n%'").fetchall()
        assert "Specialist adviser, Legal work" in notice[0]
        assert ("default level" in notice[0]) == level_due
    runtime.admit()
    runtime.levels.tick(background=False)
    assert len(calls) == 3


@pytest.mark.parametrize("reason", ["staffing disabled", "no isolated runner"])
def test_pending_guidance_settles_without_notice_when_selection_is_unavailable(system, reason):
    runtime, _ = system
    employee = guide_hire(runtime, execution_profile="sonnet")
    if reason == "staffing disabled":
        with runtime.store.transaction() as db:
            config = runtime.accounts.config(db)
            config["staffing"] = False
            db.execute("UPDATE firm SET config_json=?", (levels.encoded(config),))
        runtime.levels.chooser = lambda _: pytest.fail("staffing off must not call")
    runtime.start()
    assert runtime.levels.tick(background=False) == []
    assert guide_record(runtime, employee)["state"] == "defaulted"
    assert guide_record(runtime, employee)["reason"] == reason
    with runtime.store.read() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()


def test_staffing_off_lifecycle_makes_no_guidance_requests_or_calls(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Work", {
        "max_active": 2, "staffing": False, "founder_profile": "main", "profiles": {"main": {}}
    })
    employee = runtime.lifecycle.hire("founder", "worker", "Work", request_id="h")["employee_id"]
    runtime.lifecycle.revise_mandate("founder", employee, 0, "New work", request_id="r")
    runtime.lifecycle.revise_mandate(OWNER, "founder", 0, "New firm", request_id="rf")
    runtime.levels.chooser = lambda _: pytest.fail("staffing off must not call")
    runtime.start()
    assert runtime.levels.tick(background=False) == []
    for eid in ("founder", employee):
        assert guide_record(runtime, eid) is None
        assert role(runtime, eid)["functional_guidance"] == "[]"


def test_due_status_is_rechecked_before_assessment_and_no_empty_call_is_made(system):
    runtime, now = system
    employee = guide_hire(runtime)
    runtime.levels.chooser = lambda _: pytest.fail("no due decision")
    runtime.start()
    with runtime.store.transaction() as db:
        db.execute("UPDATE hire_levels SET retry_at=? WHERE employee_id=?", (now[0] + 60, employee))
        db.execute(
            "UPDATE role_guidance SET retry_at=? WHERE employee_id=?", (now[0] + 60, employee)
        )
    runtime.levels._assess(employee)
    assert record(runtime, employee)["attempts"] == 0
    assert guide_record(runtime, employee)["attempts"] == 0


def test_long_default_reason_is_whole_in_storage_notice_and_inspection(system):
    from firm_runtime import guidance

    runtime, now = system
    employee = guide_hire(runtime)
    reason = "A detailed failure. " * 300

    def fail(_):
        raise FirmError(reason)

    runtime.levels.chooser = fail
    runtime.start()
    for attempt in range(3):
        runtime.levels.tick(background=False)
        now[0] += 60 * (attempt + 1)
    assert guide_record(runtime, employee)["reason"] == reason
    assert record(runtime, employee)["reason"] == reason
    with runtime.store.read() as db:
        assert (
            guidance.view(db, role(runtime, employee), runtime.accounts.config(db))["reason"]
            == reason
        )
        (notice,) = db.execute(
            "SELECT body FROM messages WHERE body LIKE 'Hire level defaulted\n\n%'"
        ).fetchall()
        assert reason in notice[0]
    for kind in ("level.defaulted", "guidance.defaulted"):
        assert events(runtime, kind)[0]["reason"].endswith(levels.TRUNCATION_MARKER)


def _staffed_runtime(tmp_path):
    now = [1000.0]
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=lambda: now[0]))
    runtime.lifecycle.initialize("test", "Build.", copy.deepcopy(CONFIG))
    runtime.owner.set_guidance(OWNER, "founder", "firm-leader", [], "founder-guidance")
    return runtime, now


def _set_staffing(runtime, staffing):
    with runtime.store.transaction() as db:
        config = runtime.accounts.config(db)
        config["staffing"] = staffing
        db.execute("UPDATE firm SET config_json=?", (levels.encoded(config),))


def test_switching_staffing_off_still_settles_a_pending_hire_level(tmp_path):
    runtime, now = _staffed_runtime(tmp_path)
    employee = runtime.lifecycle.hire("founder", "worker", "Work", request_id="hire")[
        "employee_id"
    ]
    _set_staffing(runtime, False)
    runtime.start()
    assert runtime.levels.tick(background=False) == []
    with runtime.store.read() as db:
        level = runtime.levels.record(db, employee)
        assert level["state"] == "defaulted" and level["reason"] == "staffing disabled"
        assert employee in [r["employee_id"] for r in runtime.admission_candidates(db)]


def test_disabling_staffing_inflight_defaults_guidance_instead_of_choosing(system):
    runtime, _ = system
    employee = guide_hire(runtime)

    def chooser(_):
        _set_staffing(runtime, False)
        return guidance_reply(scope_guidance="specialist-adviser", functional_guidance=["legal"])

    runtime.levels.chooser = chooser
    runtime.start()
    runtime.levels.tick(background=False)
    guide = guide_record(runtime, employee)
    assert (guide["state"], guide["reason"]) == ("defaulted", "staffing disabled")
    assert role(runtime, employee)["scope_guidance"] is None


def test_a_stale_stored_revision_still_charges_and_settles_one_call(tmp_path):
    runtime, now = _staffed_runtime(tmp_path)
    runtime.owner.automatic_guidance(OWNER, "founder", "automatic")
    with runtime.store.read() as db:
        staffing = copy.deepcopy(runtime.accounts.config(db)["staffing"])
    _set_staffing(runtime, False)
    runtime.lifecycle.revise_mandate(OWNER, "founder", 0, "New firm", request_id="revise")
    _set_staffing(runtime, staffing)
    calls = []
    runtime.levels.chooser = lambda _: calls.append(1) or {
        "scope_guidance": None,
        "functional_guidance": [],
        "reason": "Own results only.",
    }
    runtime.start()
    for _ in range(5):
        runtime.levels.tick(background=False)
        now[0] += 300
    assert len(calls) == 1
    assert guide_record(runtime, "founder")["state"] == "chosen"
    with runtime.store.read() as db:
        assert "founder" in [r["employee_id"] for r in runtime.admission_candidates(db)]


def test_background_chooser_drains_cleanly_on_stop_without_late_writes(system):
    runtime, _ = system
    employee = guide_hire(runtime)
    entered = threading.Event()
    release = threading.Event()

    def slow_chooser(_):
        entered.set()
        assert release.wait(5)
        return guidance_reply()

    runtime.levels.chooser = slow_chooser
    runtime.start()
    started = runtime.levels.tick(background=True)
    assert employee in started
    assert entered.wait(2)
    runtime.stop()
    release.set()
    runtime.levels.close(wait=True)
    assert record(runtime, employee)["state"] == "pending"
    assert record(runtime, employee)["attempts"] == 0
    assert guide_record(runtime, employee)["state"] == "pending"
    assert guide_record(runtime, employee)["attempts"] == 0
    runtime.levels.chooser = lambda _: guidance_reply(scope_guidance="specialist-adviser")
    runtime.start()
    assert employee in runtime.levels.tick(background=False)
    assert record(runtime, employee)["state"] == "chosen"
    assert guide_record(runtime, employee)["state"] == "chosen"


def test_single_transaction_settlement_rolls_back_both_decisions_on_second_write_error(
    system, monkeypatch
):
    runtime, _ = system
    employee = guide_hire(runtime)
    runtime.levels.chooser = lambda _: guidance_reply(scope_guidance="specialist-adviser")
    runtime.start()

    def broken_apply(*args, **kwargs):
        raise RuntimeError("disk failure")

    monkeypatch.setattr(guidance, "apply", broken_apply)
    with pytest.raises(RuntimeError, match="disk failure"):
        runtime.levels.tick(background=False)
    assert record(runtime, employee)["state"] == "pending"
    assert record(runtime, employee)["attempts"] == 0
    assert guide_record(runtime, employee)["state"] == "pending"
    assert guide_record(runtime, employee)["attempts"] == 0
