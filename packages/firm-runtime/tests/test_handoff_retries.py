"""Handoff retries save the current work period, including after retained recovery."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from firm_runtime.errors import FirmError
from firm_runtime.prompt_resources import render
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "handoff-test",
        "Build",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "test",
            "profiles": {"test": {"context_window": 10000, "handoff_reserve": 1000}},
        },
    )
    runtime.start()
    return runtime


def start(runtime):
    invocation = runtime.admit()
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    return invocation


def fill_memory(runtime, invocation):
    runtime.record_usage(
        invocation.activation_id,
        ProviderUsage(current_context_tokens=9500, native_compactions=0, source="test"),
    )


def test_retained_recovery_retry_saves_current_period_and_restores_notes(runtime):
    first = start(runtime)
    notes = "Await the designer's reply; deliver /work/game.md to the owner."
    runtime.tools(first.token).handoff(notes, "continue", "save")
    runtime.finish(first.activation_id, outcome="interrupted", category="owner_stop")
    recovered = start(runtime)
    assert recovered.context["recovery"]["context_retained"] is True
    fill_memory(runtime, recovered)

    result = runtime.tools(recovered.token).handoff(notes, "continue", "save")

    with runtime.store.read() as db:
        activation = runtime._activation(db, recovered.activation_id)
        assert activation["next_intent"] == "continue"
        assert activation["handoff_notes"] == notes
    assert render("controls", "saved") in render_tool_result("handoff", result)
    runtime.finish(recovered.activation_id)
    following = start(runtime)
    assert following.context["fresh_session"] is True
    assert following.context["handoff_notes"] == notes


def test_same_label_can_save_new_notes_in_a_new_period_but_not_overwrite(runtime):
    first = start(runtime)
    runtime.tools(first.token).handoff("First notes", "continue", "save")
    runtime.finish(first.activation_id)
    second = start(runtime)
    tools = runtime.tools(second.token)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tools.handoff("Second notes", "wait", "save"), range(4)))
    assert all(result["status"] == "saved" for result in results)
    with pytest.raises(FirmError, match="different operation or payload"):
        tools.handoff("Changed notes", "wait", "save")
    with pytest.raises(FirmError, match="already has a handoff"):
        tools.handoff("Changed notes", "wait", "another-save")
    with runtime.store.read() as db:
        assert runtime._activation(db, second.activation_id)["handoff_notes"] == "Second notes"
        assert (
            db.execute("SELECT COUNT(*) FROM requests WHERE operation='handoff'").fetchone()[0] == 2
        )


def test_handoff_notes_are_capped_at_128_kib(runtime):
    invocation = start(runtime)
    tools = runtime.tools(invocation.token)
    with pytest.raises(FirmError, match="128 KiB"):
        tools.handoff("x" * (128 * 1024 + 1), "wait", "too-big")
    result = tools.handoff("x" * (128 * 1024), "wait", "exact-fit")
    assert result["status"] == "saved"


def test_saved_handoff_peek_reports_pressure_without_requesting_another_save(runtime):
    invocation = start(runtime)
    tools = runtime.tools(invocation.token)
    fill_memory(runtime, invocation)
    before = tools.peek()
    assert before["context_pressure"]["level"] == "handoff_required"
    tools.handoff("Review /work/game.md next", "wait", "save")

    after = tools.peek()
    rendered = render_tool_result("peek", after)

    assert after["context_pressure"]["level"] == "handoff_required"
    assert after["context_pressure"]["instruction"] == render("controls", "saved")
    assert render("controls", "saved") in rendered
    assert render("controls", "handoff_required") not in rendered
    assert "handoff_checklist" not in after["context_pressure"]
    assert runtime.context.gate_claim(invocation.activation_id) is True
