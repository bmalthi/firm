import hashlib
import json

import pytest
from firm_sandbox import Config, Layout
from firm_sandbox.file_server import FileServer, local_runner

from firm_runtime.discovery import (
    MAX_DISCOVERY_BYTES,
    MAX_LIST_BYTES,
    Discovery,
)
from firm_runtime.errors import FirmError
from firm_runtime.lifecycle import FOUNDER_ID
from firm_runtime.role_output import render_tool_result
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store, encoded
from firm_runtime.workspaces import grants_for, provision_runtime_role

CONFIG = {
    "max_active": 4,
    "staffing": False,
    "founder_profile": "default",
    "profiles": {"default": {}},
}


def _runtime(tmp_path):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize("test", "Build the company.", CONFIG)
    runtime.discovery = Discovery(runtime.store)
    return runtime


def _hire(runtime, manager, slug, mandate=None):
    return runtime.lifecycle.hire(
        manager,
        slug,
        mandate or f"Own {slug} responsibilities.",
        "default",
        request_id=f"hire-{manager}-{slug}",
    )


def _set_profile(runtime, employee_id, profile):
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE roles SET public_profile=?,public_profile_status='available',"
            "public_profile_digest=? "
            "WHERE employee_id=?",
            (profile, hashlib.sha256(profile.encode()).hexdigest(), employee_id),
        )


def _activate(runtime, employee_id=FOUNDER_ID, colleagues=()):
    sandbox_root = runtime.store.path.parent / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox_root.chmod(0o700)
    tree = sandbox_root / "tree"
    tree.mkdir(exist_ok=True)
    files = FileServer(Config(sandbox_root), local_runner(tree))
    files.start()
    # Discovery exercises workspace grants, but these unit tests do not launch
    # a guest. The file server's guest steps run against the temporary tree.
    runtime.discovery.files = files
    for person in (employee_id, *colleagues):
        provision_runtime_role(runtime.store, sandbox_root, person, files=files)
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO execution_host(singleton,sandbox_root,sandbox_firm_id,"
            "sandbox_identity,infrastructure_state) VALUES(1,?,'test','test','running')",
            (str(sandbox_root),),
        )
        db.execute("DELETE FROM execution_requests")
        runtime.store.request_execution(db, employee_id, "test")
    runtime.start()
    invocation = runtime.admit()
    assert invocation and invocation.employee_id == employee_id
    references = grants_for(runtime.store, employee_id, Layout()).references
    invocation.context["workspace_references"] = references
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE activations SET context_json=? WHERE activation_id=?",
            (json.dumps(invocation.context), invocation.activation_id),
        )
    runtime.mark_started(invocation.activation_id)
    return invocation


def test_get_role_address_vs_exact_retired_id_and_actual_invocation_grants(tmp_path):
    runtime = _runtime(tmp_path)
    old = _hire(runtime, FOUNDER_ID, "finance")
    _set_profile(
        runtime,
        old["employee_id"],
        "# Mira — Finance Lead\nSee [track record](track-record.md).",
    )
    runtime.lifecycle.fire(FOUNDER_ID, old["employee_id"], reason="Replace", request_id="fire-old")
    new = runtime.lifecycle.hire(
        FOUNDER_ID,
        "finance",
        "Own new finance responsibilities.",
        "default",
        request_id="hire-new-finance",
    )
    _set_profile(runtime, new["employee_id"], "Noor — Finance Lead")
    invocation = _activate(runtime, colleagues=(new["employee_id"],))
    tools = runtime.tools(invocation.token)

    current = tools.get_role(role="founder/finance")
    assert current["role"]["employee_id"] == new["employee_id"]
    assert current["workspace"] == {
        "path": "/home/firmrole/drive/org/founder/finance/work",
        "access": "read_write",
    }
    historical = tools.get_role(role=old["employee_id"])
    assert historical["role"]["employment"] == "retired"
    assert historical["role"]["accepts_mail"] is False
    assert historical["documents"]["profile"]["label"] == "last_recorded"
    assert historical["workspace"]["path"] is None
    with pytest.raises(FirmError, match="unknown employee or current role address"):
        tools.get_role(role="founder/missing")


def test_paused_employee_accepts_mail_without_running_but_stays_visible(tmp_path):
    runtime = _runtime(tmp_path)
    analyst = _hire(runtime, FOUNDER_ID, "ops")["employee_id"]
    runtime.owner.stop_role(OWNER, analyst, request_id="pause-ops")
    sent = runtime.messaging.send(OWNER, [analyst], "Still reachable", request_id="mail-paused")
    assert sent["message_id"]
    with runtime.store.read() as db:
        opportunities = db.execute(
            "SELECT COUNT(*) FROM scheduling_opportunities WHERE employee_id=?", (analyst,)
        ).fetchone()[0]
        candidates = {row["employee_id"] for row in runtime.admission_candidates(db)}
    assert opportunities >= 1
    assert analyst not in candidates
    tree = runtime.discovery.org_tree(FOUNDER_ID)
    node = next(n for n in tree["nodes"] if n["employee_id"] == analyst)
    assert node["employment"] == "paused"
    assert node["accepts_mail"] is True
    search = runtime.discovery.find_roles(FOUNDER_ID, query="ops")
    assert any(r["employee_id"] == analyst and r["accepts_mail"] is True for r in search["roles"])


def test_get_role_shows_workspaces_only_within_the_callers_reporting_line(tmp_path):
    runtime = _runtime(tmp_path)
    manager = _hire(runtime, FOUNDER_ID, "engineering")
    peer = _hire(runtime, FOUNDER_ID, "finance")
    active = _activate(
        runtime, manager["employee_id"], colleagues=(FOUNDER_ID, peer["employee_id"])
    )
    child = _hire(runtime, manager["employee_id"], "api")
    cousin = _hire(runtime, peer["employee_id"], "audit")
    root = runtime.store.path.parent / "sandbox"
    for person in (child, cousin):
        provision_runtime_role(
            runtime.store, root, person["employee_id"], files=runtime.discovery.files
        )
    _set_profile(runtime, child["employee_id"], "API lead\n[record](track-record.md)")
    _set_profile(runtime, cousin["employee_id"], "Audit\n[record](track-record.md)")
    tools = runtime.tools(active.token)
    report = tools.get_role(child["employee_id"])
    assert report["workspace"] == {
        "path": "/home/firmrole/drive/org/founder/engineering/api/work",
        "access": "read_write",
    }
    assert "Workspace: /home/firmrole/drive/org/founder/engineering/api/work (read/write)" in (
        render_tool_result("get_role", report)
    )
    assert tools.get_role(FOUNDER_ID)["workspace"] == {
        "path": "/home/firmrole/drive/org/founder/work",
        "access": "read_only",
    }
    for outside in (peer, cousin):
        result = tools.get_role(outside["employee_id"])
        assert result["workspace"] == {
            "path": None,
            "access": None,
            "visibility": "reporting_line",
        }
        rendered = render_tool_result("get_role", result)
        assert outside["address"] + "/work" not in rendered
        assert "/drive/shared" not in rendered and "track-record" not in rendered.split("\n", 1)[0]
        assert "only by its reporting line" in rendered


def test_get_role_pages_unicode_exactly_and_authenticates_changed_continuation(tmp_path):
    runtime = _runtime(tmp_path)
    profile = "Profile 🧭\n" + "研究🙂" * 30_000
    mandate = "Mandate 🌍\n" + "delegationé" * 12_000
    _set_profile(runtime, FOUNDER_ID, profile)
    with runtime.store.transaction() as db:
        db.execute("UPDATE mandates SET body=? WHERE employee_id=?", (mandate, FOUNDER_ID))
    invocation = _activate(runtime)
    tools = runtime.tools(invocation.token)

    cursor = None
    collected = {"profile": "", "mandate": ""}
    pages = 0
    while True:
        page = tools.get_role(role=FOUNDER_ID, cursor=cursor)
        assert len(encoded(page).encode()) <= 64 * 1024
        # Discovery reserves the rest for any activation control added by RoleTools.
        without_controls = {
            key: value for key, value in page.items() if key != "operations_control"
        }
        assert len(encoded(without_controls).encode()) <= MAX_DISCOVERY_BYTES
        for source, document in page["documents"].items():
            collected[source] += document["text"]
        pages += 1
        cursor = page["continuation"]
        if cursor is None:
            break
    assert pages >= 3
    assert collected == {"profile": profile, "mandate": mandate}

    first = tools.get_role(role=FOUNDER_ID)
    tampered = first["continuation"][:-1] + ("A" if first["continuation"][-1] != "A" else "B")
    with pytest.raises(FirmError, match="invalid discovery continuation"):
        tools.get_role(role=FOUNDER_ID, cursor=tampered)
    with runtime.store.transaction() as db:
        db.execute("UPDATE mandates SET body=body || ' changed' WHERE employee_id=?", (FOUNDER_ID,))
    with pytest.raises(FirmError, match="no longer matches"):
        tools.get_role(role=FOUNDER_ID, cursor=first["continuation"])
    with pytest.raises(FirmError, match="invalid discovery continuation"):
        tools.get_role(role=FOUNDER_ID, cursor="A" * 8193)


def test_get_role_can_advance_after_a_complete_document_fills_the_page(tmp_path):
    runtime = _runtime(tmp_path)
    profile = "P" * (MAX_DISCOVERY_BYTES - 1_300)
    mandate = "M" * 1_000
    _set_profile(runtime, FOUNDER_ID, profile)
    with runtime.store.transaction() as db:
        db.execute("UPDATE mandates SET body=? WHERE employee_id=?", (mandate, FOUNDER_ID))
    tools = runtime.tools(_activate(runtime).token)

    first = tools.get_role(role=FOUNDER_ID)
    assert list(first["documents"]) == ["profile"]
    assert first["documents"]["profile"]["complete"] is True
    assert first["documents"]["profile"]["text"] == profile
    assert first["continuation"] is not None
    assert len(encoded(first).encode()) <= 64 * 1024

    second = tools.get_role(role=FOUNDER_ID, cursor=first["continuation"])
    assert second["documents"]["mandate"]["text"] == mandate
    assert second["complete"] is True
    assert second["continuation"] is None


def test_find_roles_keeps_accepted_lexical_ranking_and_invalidates_on_public_change(tmp_path):
    runtime = _runtime(tmp_path)
    exact = _hire(runtime, FOUNDER_ID, "refunds", "Own exception approvals")
    support = _hire(runtime, FOUNDER_ID, "support", "Handle customer refunds communication")
    finance = _hire(runtime, FOUNDER_ID, "finance", "Execute customer refunds and payments")
    _set_profile(runtime, exact["employee_id"], "Refunds")
    _set_profile(runtime, support["employee_id"], "Customer Support")
    _set_profile(runtime, finance["employee_id"], "Finance")
    invocation = _activate(runtime)
    tools = runtime.tools(invocation.token)

    result = tools.find_roles("refunds", limit=2)
    assert result["roles"][0]["employee_id"] == exact["employee_id"]
    assert result["total_matches"] == 3
    assert result["remaining"] == 1
    assert len(encoded(result).encode()) <= MAX_LIST_BYTES
    assert all(
        evidence["source"] in {"address", "identity", "profile", "mandate"}
        for role in result["roles"]
        for evidence in role["evidence"]
    )
    second = tools.find_roles("refunds", limit=2, cursor=result["continuation"])
    assert len(second["roles"]) == 1
    assert second["continuation"] is None

    cursor = tools.find_roles("", limit=1)["continuation"]
    _set_profile(runtime, finance["employee_id"], "Treasury")
    with pytest.raises(FirmError, match="no longer matches"):
        tools.find_roles("", limit=1, cursor=cursor)


def test_org_tree_depth_order_counts_paging_and_current_membership(tmp_path):
    runtime = _runtime(tmp_path)
    alpha = _hire(runtime, FOUNDER_ID, "alpha")
    beta = _hire(runtime, FOUNDER_ID, "beta")
    deep = _hire(runtime, alpha["employee_id"], "deep")
    retired = _hire(runtime, beta["employee_id"], "retired")
    runtime.lifecycle.fire(
        beta["employee_id"], retired["employee_id"], reason="Done", request_id="retire"
    )
    invocation = _activate(runtime)
    tools = runtime.tools(invocation.token)

    first = tools.org_tree(depth=1, max_nodes=1)
    assert first["root"]["employee_id"] == FOUNDER_ID
    assert first["total_descendants"] == 3
    assert first["depth_eligible_descendants"] == 2
    assert first["omitted_by_depth"] == 1
    assert first["nodes"][0]["employee_id"] == alpha["employee_id"]
    assert first["nodes"][0]["direct_report_count"] == 1
    assert first["remaining"] == 1
    second = tools.org_tree(depth=1, max_nodes=1, cursor=first["continuation"])
    assert second["nodes"][0]["employee_id"] == beta["employee_id"]
    assert second["continuation"] is None
    assert len(encoded(second).encode()) <= MAX_LIST_BYTES

    scoped = tools.org_tree(root=alpha["employee_id"], depth=1)
    assert [item["employee_id"] for item in scoped["breadcrumb"]] == [FOUNDER_ID]
    assert [item["employee_id"] for item in scoped["nodes"]] == [deep["employee_id"]]
    with pytest.raises(FirmError, match="unknown or retired"):
        tools.org_tree(root=retired["employee_id"])


def test_short_continuations_are_scoped_to_work_period_and_validate_original_view(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    runtime = _runtime(tmp_path)
    child = _hire(runtime, FOUNDER_ID, "child")
    _hire(runtime, FOUNDER_ID, "other")
    first = _activate(runtime, colleagues=(child["employee_id"],))
    tools = runtime.tools(first.token)
    with ThreadPoolExecutor(max_workers=4) as workers:
        pages = list(workers.map(lambda _: tools.find_roles("", limit=1), range(4)))
    reference = pages[0]["continuation"]
    assert all(p["continuation"] == reference for p in pages)
    assert reference.startswith("d") and reference[1:].isdigit() and len(reference) < 20
    with runtime.store.read() as db:
        rows = db.execute("SELECT * FROM discovery_continuations").fetchall()
        assert len(rows) == 1
        assert len(rows[0]["cursor"]) > 100
    # A reconstructed runtime retains a live work period's handles.
    reopened = Runtime(Store.scratch(runtime.store.path))
    assert reopened.tools(first.token).find_roles("", limit=1, cursor=reference)["roles"]
    with pytest.raises(FirmError, match="no longer matches"):
        tools.find_roles("other", limit=1, cursor=reference)
    with pytest.raises(FirmError, match="no longer matches"):
        tools.org_tree(cursor=reference)
    second = _activate(runtime, child["employee_id"])
    with pytest.raises(FirmError, match="invalid discovery continuation"):
        runtime.tools(second.token).find_roles("", cursor=reference)
    runtime.finish(first.activation_id)
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM discovery_continuations").fetchone()[0] == 0
    runtime.messaging.send("owner", [first.employee_id], "Continue work", request_id="wake-again")
    resumed = runtime.admit()
    assert resumed.employee_id == first.employee_id
    runtime.mark_started(resumed.activation_id)
    with pytest.raises(FirmError, match="invalid discovery continuation"):
        runtime.tools(resumed.token).find_roles("", cursor=reference)
    new_reference = runtime.tools(resumed.token).find_roles("", limit=1)["continuation"]
    assert new_reference != reference


def test_short_document_continuation_cannot_retarget_reused_role_address(tmp_path):
    runtime = _runtime(tmp_path)
    old = _hire(runtime, FOUNDER_ID, "delivery")
    _set_profile(runtime, old["employee_id"], "Old profile " * 10000)
    first = _activate(runtime, colleagues=(old["employee_id"],))
    tools = runtime.tools(first.token)
    page = tools.get_role("founder/delivery")
    assert page["continuation"]
    tools.fire(old["employee_id"], "Replace", "retire")
    new = tools.hire("delivery", "New work", "replace")
    assert new["employee_id"] != old["employee_id"]
    with pytest.raises(FirmError, match="no longer matches"):
        tools.get_role("founder/delivery", cursor=page["continuation"])
    # Exact retired identity still retrieves the original document, never the replacement.
    continuation = tools.get_role(old["employee_id"], cursor=page["continuation"])
    assert continuation["role"]["employee_id"] == old["employee_id"]
