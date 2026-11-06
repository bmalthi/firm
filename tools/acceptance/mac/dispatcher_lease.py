"""Live Mac lease acceptance through the real dispatcher with deterministic programs.

Run against a fresh disposable firm cloned from an enrolled image.  Its
organizational storage must never have been bound to another runtime database;
discard the sandbox after each attempt.  An ordinary role acquires the Mac,
reaches the station over the lease's SSH endpoint, writes to its workspace on
the drive through the station and reads it back from its own mount, drives the
helper, and releases; a second ordinary role sees busy while the lease is held
and granted after. Dispatcher-managed reservation roles hold the other accounts.
It does not require a model-provider account.
"""

import base64
import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

from acceptance_common import (
    acceptance_parser,
    capture_logs,
    lease_tables,
    rows,
    select_station,
    station_status,
    write_json,
)
from firm_runtime.console import build_console_app, console_key
from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, RunSpec, Sandbox

PROGRAM = r"""
import json, os, re, subprocess, time, urllib.error, urllib.request
from pathlib import Path

base = os.environ['PROBE_MCP']
token = os.environ['FIRM_ACTIVATION_TOKEN']
kind = os.environ['PROBE_KIND']

def post(path, value, auth=token):
    request = urllib.request.Request(
        base + path, data=json.dumps(value).encode(),
        headers={'Authorization': 'Bearer ' + auth, 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream', 'Origin': base})
    with urllib.request.urlopen(request, timeout=360) as response:
        return json.load(response)

def tool(name, arguments):
    result = post('/mcp', {'jsonrpc': '2.0', 'id': name, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}})
    assert not result.get('error') and not result['result'].get('isError'), result
    text = result['result']['content'][0]['text']
    return dict(re.findall(r'^([A-Z][a-z_ ]*): (.*)$', text, re.M))

def report(**value):
    print(json.dumps({'probe': kind, **value}), flush=True)

try:
    post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, auth='wrong-token')
    raise AssertionError('Unauthenticated MCP access succeeded')
except urllib.error.HTTPError as error:
    assert error.code == 401

def gate(name):
    deadline = time.monotonic() + 1200
    while not (Path(os.environ['FIRM_CREDENTIALS_DIR']) / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Host did not open ' + name)
        time.sleep(0.2)

post('/activation/start', {})
if kind != 'founder':
    report(event='ready')
    gate('acceptance-acquire')
if kind == 'holder':
    lease = tool('mac_acquire', {})
    assert lease['Status'] == 'granted', lease
    assert int(lease['Station']) == int(os.environ['PROBE_STATION']), lease
    assert tool('mac_acquire', {}) == lease, 'a second acquire must return the same lease'
    report(event='granted', lease=lease)
    ssh = ['/usr/bin/ssh', '-i', lease['Key path'], '-p', lease['Ssh port'],
           '-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
           lease['User'] + '@' + lease['Ssh host']]
    who = subprocess.run(ssh + ['id -u; hostname'], capture_output=True, text=True, timeout=60)
    assert who.returncode == 0 and who.stdout.split()[0] == str(501 + int(lease['Station'])), who
    marker = lease['Project dir'] + '/mac-lease-probe.txt'
    written = subprocess.run(ssh + ['printf lease-ok > ' + marker + ' && ls -l ' + marker],
                             capture_output=True, text=True, timeout=60)
    assert written.returncode == 0, written
    mine = os.path.join(os.environ['PROBE_DRIVE'], 'org', os.environ['PROBE_ADDRESS'],
                        'work', 'mac-lease-probe.txt')
    for _ in range(50):
        if os.path.exists(mine) and open(mine).read() == 'lease-ok':
            break
        time.sleep(0.2)
    else:
        raise AssertionError('station write did not appear on the role drive: ' + mine)
    background = subprocess.run(ssh + ['nohup /bin/sleep 600 >/dev/null 2>&1 & echo started'],
                                capture_output=True, text=True, timeout=60)
    assert 'started' in background.stdout, background
    status = tool('desktop_status', {})
    assert status.get('Ready') == 'True', status
    report(event='worked', ssh=who.stdout.split(), desktop=status)
    gate('acceptance-release')
    released = tool('mac_release', {})
    report(event='released', result=released)
    assert released.get('Clean') == 'True', released
    # The contender may already hold the Mac; only this lease must be gone.
    after = tool('mac_status', {})
    assert after.get('Lease id') != lease['Lease id'], after
    assert after.get('Dirty') == 'False', after
elif kind == 'contender':
    busy = tool('mac_acquire', {})
    report(event='first', result=busy)
    assert busy['Status'] == 'busy', busy
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        time.sleep(1)
        later = tool('mac_acquire', {})
        if later['Status'] == 'granted':
            break
    else:
        raise AssertionError('the Mac was never granted after the holder released')
    assert int(later['Station']) == int(os.environ['PROBE_STATION']), later
    report(event='granted', lease=later)
    released = tool('mac_release', {})
    assert released.get('Clean') == 'True', released
    report(event='released', result=released)
elif kind == 'reservation':
    lease = tool('mac_acquire', {})
    assert lease['Status'] == 'granted', lease
    assert int(lease['Station']) == int(os.environ['PROBE_STATION']), lease
    report(event='granted', lease=lease)
    gate('acceptance-release')
    released = tool('mac_release', {})
    assert released.get('Clean') == 'True', released
    report(event='released', result=released)
tool('handoff', {'notes': kind + ' lease probe finished.', 'next': 'wait',
                 'request_id': 'mac-lease-' + kind})
report(event='complete')
"""


class ProbeProvider:
    def __init__(self, kinds, addresses, stations):
        self.stations = stations
        self.kinds = kinds
        self.addresses = addresses

    def build_spec(
        self,
        invocation,
        role_id,
        run_id,
        mcp_url,
        provider_session_id=None,
        *,
        layout=None,
    ):
        return RunSpec(
            role_id,
            (layout.python, layout.credential("probe-program")),
            run_id=run_id,
            env={
                "PROBE_MCP": mcp_url.removesuffix("/mcp"),
                "PROBE_STATION": str(self.stations.get(invocation.employee_id, 0)),
                "PROBE_KIND": self.kinds[invocation.employee_id],
                "PROBE_DRIVE": layout.drive,
                "PROBE_ADDRESS": self.addresses[invocation.employee_id],
            },
            secrets={
                "activation-token": invocation.token.encode(),
                "probe-program": PROGRAM.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    def parse_result(self, logs, exit_code):
        complete = b'"event": "complete"' in logs
        return SimpleNamespace(
            provider_session_id=None,
            success=exit_code == 0 and complete,
            error=(
                None if complete else SimpleNamespace(kind="mac_lease_probe", category="unknown")
            ),
        )


@contextmanager
def console_probe(runtime, browser_path):
    if browser_path is None:
        yield None
        return
    from playwright.sync_api import sync_playwright

    key = console_key(runtime.store.path.parent)
    console = LocalServer(lambda port: build_console_app(
        runtime, allowed_host=f"127.0.0.1:{port}",
    ))
    with console, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(browser_path), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://127.0.0.1:{console.port}/unlock/{key}")
            page.get_by_role("link", name="System", exact=True).click()
            yield page
        finally:
            browser.close()


def main():
    parser = acceptance_parser(__doc__, station_help="Mac station the holder leases")
    parser.add_argument("--browser", type=Path, help="Chromium executable for live console checks")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    # This check builds its own runtime database under --evidence, and a file
    # server may only ever serve one. A firm that has already run rejects it.
    if box.file_server.database_identity() is not None:
        raise SystemExit(
            f"{args.root} has already served a runtime database; this check needs a firm "
            "directory whose file server is unbound (a fresh disposable firm, or a new "
            "--evidence run against a firm created for it)"
        )
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "vm-mac-lease",
        "Validate the Mac lease from ordinary roles.",
        {"max_active": len(box.mac.stations) + 2, "staffing": False,
         "founder_profile": "probe", "profiles": {"probe": {}}},
    )["founder"]["employee_id"]
    holder = runtime.lifecycle.hire(
        founder, "holder", "Lease the Mac and work through it.", "probe",
        request_id="holder-role", temporary_periods=1,
    )
    contender = runtime.lifecycle.hire(
        founder, "contender", "See busy, then take the Mac.", "probe",
        request_id="contender-role", temporary_periods=1,
    )
    reservations = {number: runtime.lifecycle.hire(
        founder, f"reservation-{number}", "Hold another station during the selected lease check.",
        "probe", request_id=f"reservation-{number}", temporary_periods=1,
    ) for number in box.mac.stations if number != args.station}
    kinds = {founder: "founder", holder["employee_id"]: "holder",
             contender["employee_id"]: "contender"}
    addresses = {founder: "founder", holder["employee_id"]: holder["address"],
                 contender["employee_id"]: contender["address"]}
    kinds.update({r["employee_id"]: "reservation" for r in reservations.values()})
    addresses.update({r["employee_id"]: r["address"] for r in reservations.values()})
    station_by_role = {r["employee_id"]: number for number, r in reservations.items()}
    station_by_role.update({holder["employee_id"]: args.station,
                           contender["employee_id"]: args.station})
    provider = ProbeProvider(kinds, addresses, station_by_role)
    server = LocalServer(
        lambda port: build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
    )
    result = None
    try:
        with server, console_probe(runtime, args.browser) as page:
            box.configure_runtime(server.port)
            with SandboxDispatcher(
                runtime,
                box,
                {"probe": provider},
                mcp_url=f"http://127.0.0.1:{server.port}/mcp",
            ) as dispatcher:
                dispatcher.start()

                def wait_event(role, event):
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline:
                        dispatcher.tick()
                        for run in box.runs():
                            if run.role_id == sandbox_role_id(role["employee_id"]):
                                if f'"event": "{event}"'.encode() in box.logs(run):
                                    return run
                        time.sleep(0.2)
                    raise TimeoutError(f"{role['address']} did not report {event}")

                def open_gate(role, name):
                    box.linux._rpc("write-credential",
                                   role_id=sandbox_role_id(role["employee_id"]),
                                   name=name, data=base64.b64encode(b"go").decode())

                # Acquisition chooses the first free station, so admit holders in
                # account order before starting the contender.
                for number in box.mac.stations:
                    role = holder if number == args.station else reservations[number]
                    wait_event(role, "ready")
                    open_gate(role, "acceptance-acquire")
                    wait_event(role, "granted")
                holder_run = None
                startup_deadline = time.monotonic() + 240
                while time.monotonic() < startup_deadline:
                    dispatcher.tick()
                    inventory = box.runs()
                    holder_run = next(
                        (r for r in inventory
                         if r.role_id == sandbox_role_id(holder["employee_id"])), None
                    )
                    if holder_run and b'"event": "worked"' in box.logs(holder_run):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("The holder did not work through its lease")
                held = station_status(box, args.station)
                assert held["held"] and held["holder"] == sandbox_role_id(
                    holder["employee_id"]
                ), held
                summary = runtime.mac.summary()["stations"]
                assert next(row for row in summary if row["station"] == args.station)["holder"] == (
                    holder["address"]
                ), summary
                if page is not None:
                    page.reload()
                    snapshot = page.request.get(urljoin(page.url, "/api/snapshot")).json()
                    identity = next(
                        row["identity"] or row["address"]
                        for row in snapshot["readiness"]["rows"]
                        if row["employee_id"] == holder["employee_id"]
                    )
                    page.wait_for_function(
                        "([station, identity]) => [...document.querySelectorAll('#health-rows tr')]"
                        ".find(row => row.querySelector('th').textContent === "
                        "'Mac station · ' + station)?.querySelector('td').textContent"
                        ".startsWith('In use by ' + identity + ' since ')",
                        arg=[args.station, identity],
                    )
                    page.screenshot(path=str(args.evidence / "console-held.png"), full_page=True)

                wait_event(contender, "ready")
                open_gate(contender, "acceptance-acquire")
                wait_event(contender, "first")
                open_gate(holder, "acceptance-release")
                wait_event(contender, "released")
                for role in reservations.values():
                    open_gate(role, "acceptance-release")
                completion_deadline = time.monotonic() + 300
                while time.monotonic() < completion_deadline:
                    dispatcher.tick()
                    current = rows(runtime)
                    if all(
                        row["state"] not in {"admitted", "running"}
                        for row in current
                        if row["employee_id"] != founder
                    ):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Holder and contender activations did not settle")

                logs = capture_logs(box, args.evidence)
                # A probe that reached its own assertion failure still settles;
                # the lease tables alone would not reveal it.
                failed = [row for row in current
                          if row["employee_id"] != founder and row["state"] != "completed"]
                assert not failed, (failed, logs)
                tables = lease_tables(runtime)
                outcomes = [row["outcome"] for row in tables["leases"]]
                assert outcomes == ["released"] * (len(box.mac.stations) + 1), tables
                assert [e["event"] for e in tables["events"]].count("busy") >= 1, tables
                if page is not None:
                    page.reload()
                    page.wait_for_function(
                        "count => { const rows = [...document.querySelectorAll('#health-rows tr')]"
                        ".filter(row => row.querySelector('th').textContent"
                        ".startsWith('Mac station · '))"
                        "; return rows.length === count && "
                        "rows.every(row => row.querySelector('td').textContent === 'Free') }",
                        arg=len(box.mac.stations),
                    )
                    page.locator("#analysis > summary").click()
                    page.get_by_role("button", name="Mac", exact=True).click()
                    analysis = page.request.get(urljoin(page.url, "/api/analysis/mac")).json()
                    assert analysis["total_rows"] >= 2, analysis
                    (args.evidence / "console-mac-analysis.json").write_text(
                        json.dumps(analysis, indent=2)
                    )
                    page.screenshot(path=str(args.evidence / "console-free.png"), full_page=True)
                result = {
                    "passed": False,
                    "scope": "real dispatcher, ordinary roles, SSH and drive through the lease, "
                    "busy then granted, verified release",
                    "roles": {"holder": holder["employee_id"],
                              "contender": contender["employee_id"]},
                    "activations": current,
                    "lease_tables": tables,
                    "station_after": station_status(box, args.station),
                    "runs": logs,
                }
                dispatcher.stop()
                stop_deadline = time.monotonic() + 60
                while runtime.status()["state"] != "stopped":
                    dispatcher.tick()
                    if time.monotonic() >= stop_deadline:
                        raise TimeoutError("Runtime drain did not settle")
                    time.sleep(0.2)
    except BaseException as exc:
        failure = {
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "activations": rows(runtime),
            "lease_tables": lease_tables(runtime),
        }
        try:
            failure["runs"] = capture_logs(box, args.evidence)
        except Exception as capture_error:
            failure["log_capture_error"] = str(capture_error)
        write_json(args.evidence / "failure.json", failure)
        raise
    finally:
        box.down(force=True)
    if box.state() != "stopped":
        raise RuntimeError("Mac lease acceptance cleanup left a VM running")
    result["vms_stopped"] = True
    result["passed"] = True
    write_json(args.evidence / "result.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
