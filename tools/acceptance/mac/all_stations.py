"""Hold every account on the one Mac concurrently through the real dispatcher.

Use a fresh disposable firm cloned from an enrolled image; its file server must
never have served another runtime database. --station selects an account that
must participate, but every recorded station is checked. Each role reaches the
same guest sshd through its own lease port and drives its desktop. An extra role
sees busy with every holder named. Retains role logs, lease tables, tunnel and VM
evidence, then releases all accounts and stops both VMs. No model account needed.
"""

import base64
import json
import time
from types import SimpleNamespace

from acceptance_common import (
    acceptance_parser,
    capture_logs,
    lease_tables,
    rows,
    select_station,
    write_json,
)
from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, RunSpec, Sandbox
from firm_sandbox.commands import parse_lima_list
from firm_sandbox.mac_station import lease_ssh_port

PROGRAM = r"""
import json, os, re, subprocess, time, urllib.error, urllib.request
from pathlib import Path

base = os.environ['PROBE_MCP']
token = os.environ['FIRM_ACTIVATION_TOKEN']
kind = os.environ['PROBE_KIND']
holders = json.loads(os.environ['PROBE_HOLDERS'])

def post(path, value, auth=token):
    request = urllib.request.Request(
        base + path, data=json.dumps(value).encode(),
        headers={'Authorization': 'Bearer ' + auth, 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream', 'Origin': base})
    with urllib.request.urlopen(request, timeout=360) as response:
        return json.load(response)

def call(name, arguments):
    result = post('/mcp', {'jsonrpc': '2.0', 'id': name, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}})
    assert not result.get('error') and not result['result'].get('isError'), result
    body = result['result']['content'][0]['text']
    return body, dict(re.findall(r'^([A-Z][a-z_ ]*): (.*)$', body, re.M))

def tool(name, arguments):
    return call(name, arguments)[1]

def report(**value):
    print(json.dumps({'probe': kind, **value}), flush=True)

def wait_for_all():
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if call('mac_status', {})[0].count('Held: True') == len(holders):
            return
        time.sleep(1)
    raise AssertionError('the firm never held all stations at once')

try:
    post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, auth='wrong-token')
    raise AssertionError('Unauthenticated MCP access succeeded')
except urllib.error.HTTPError as error:
    assert error.code == 401

post('/activation/start', {})
if kind == 'holder':
    lease = tool('mac_acquire', {})
    assert lease['Status'] == 'granted', lease
    report(event='granted', station=lease['Station'], port=lease['Ssh port'])
    wait_for_all()
    mine = tool('mac_status', {})
    assert mine.get('Held by you') == 'True', mine
    assert mine.get('Your station') == lease['Station'], mine
    ssh = ['/usr/bin/ssh', '-i', lease['Key path'], '-p', lease['Ssh port'],
           '-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
           lease['User'] + '@' + lease['Ssh host']]
    who = subprocess.run(ssh + ['id -u'], capture_output=True, text=True, timeout=30)
    assert who.returncode == 0 and who.stdout.strip() == str(501 + int(lease['Station'])), who
    scan = subprocess.run(['/usr/bin/ssh-keyscan', '-T', '10', '-t', 'ed25519',
                           '-p', lease['Ssh port'], lease['Ssh host']],
                          capture_output=True, text=True, timeout=30, check=True)
    keys = {line.split()[2] for line in scan.stdout.splitlines() if not line.startswith('#')}
    assert len(keys) == 1, scan
    report(event='sshd', station=lease['Station'], host_key=keys.pop(), uid=who.stdout.strip())
    desktop = tool('desktop_status', {})
    assert desktop.get('Ready') == 'True', desktop
    report(event='all_held', station=lease['Station'], port=lease['Ssh port'],
           desktop=desktop)
    # The host releases this barrier only after every desktop and the busy refusal
    # have been observed and the live topology has been checked.
    deadline = time.monotonic() + 600
    while not (Path(os.environ['FIRM_CREDENTIALS_DIR']) / 'acceptance-release').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Host did not finish the concurrent checks')
        time.sleep(0.2)
    released = tool('mac_release', {})
    assert released.get('Clean') == 'True', released
    report(event='released', result=released)
elif kind == 'contender':
    wait_for_all()
    body, busy = call('mac_acquire', {})
    report(event='refused', result=busy)
    assert busy['Status'] == 'busy', busy
    missing = [address for address in holders if address not in body]
    assert not missing, (missing, body)
    report(event='holders_named', holders=holders)
tool('handoff', {'notes': kind + ' all-station probe finished.', 'next': 'wait',
                 'request_id': 'mac-all-stations-' + kind})
report(event='complete')
"""


class ProbeProvider:
    def __init__(self, kinds, holders):
        self.kinds = kinds
        self.holders = holders

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
                "PROBE_KIND": self.kinds[invocation.employee_id],
                "PROBE_HOLDERS": json.dumps(self.holders),
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
                None
                if complete
                else SimpleNamespace(kind="mac_all_stations_probe", category="unknown")
            ),
        )


def probe_events(box, role_ids):
    """Every JSON line the named roles' runs printed, in run order."""
    events = []
    for run in box.runs():
        if run.role_id not in role_ids:
            continue
        for line in box.logs(run).decode(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and "probe" in event:
                events.append(event)
    return events


def topology_evidence(box, held):
    """Inspect live forwards, recorded machines and the guest's public SSH identity."""
    assert set(box._vms()) == {"linux", "mac"}, box._vms()
    records = sorted(box.config.durable.glob("mac/**/vm.json"))
    assert records == [box.mac.root / "vm.json"], records
    listing = box.mac._lima("list", "--json")
    instances = parse_lima_list(listing.stdout)
    assert len(instances) == 1 and instances[0]["name"] == "mac", instances
    forwards = list(box.config.durable.rglob("files-mac*.json"))
    assert forwards == [box.mac.root / "files-mac.json"], forwards
    files = json.loads(forwards[0].read_text())
    port = int(files["forward"][1].split(":")[1])
    live_forwards = list(box.file_server.forwards(port))
    assert [t.name for t in live_forwards] == ["files-owner", "files-mac"], live_forwards
    for forward in live_forwards:
        forward.require_ready()
    tunnels = []
    for row in held:
        tunnel = box._lease_tunnel(row["lease_id"], row["station"])
        record = json.loads(tunnel.record.read_text())
        expected = ["-R", f"127.0.0.1:{lease_ssh_port(row['station'])}:"
                          f"127.0.0.1:{box.mac.ssh_port()}"]
        assert record["forward"] == expected, record
        tunnel.require_ready()
        tunnels.append(record)
    public_key = box.mac._shell("/bin/cat", "/etc/ssh/ssh_host_ed25519_key.pub").stdout
    if isinstance(public_key, bytes):
        public_key = public_key.decode()
    return {"vm_records": [str(p) for p in records], "instances": instances,
            "files_forward": files, "lease_tunnels": tunnels,
            "sshd_host_key": public_key.split()[1]}


def main():
    parser = acceptance_parser(
        __doc__, station_help="Station that must participate (all are checked)"
    )
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(box, args.station)
    stations = list(box.mac.stations)
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
        "vm-mac-all-stations",
        "Validate every Mac station leased at the same time.",
        {"max_active": len(stations) + 2, "staffing": False,
         "founder_profile": "probe", "profiles": {"probe": {}}},
    )["founder"]["employee_id"]
    holders = [runtime.lifecycle.hire(
        founder, f"holder-{number}", "Lease a Mac station and hold it.", "probe",
        request_id=f"holder-{number}-role", temporary_periods=1,
    ) for number in stations]
    contender = runtime.lifecycle.hire(
        founder, "contender", "See busy while every station is held.", "probe",
        request_id="contender-role", temporary_periods=1,
    )
    kinds = {founder: "founder", contender["employee_id"]: "contender",
             **{holder["employee_id"]: "holder" for holder in holders}}
    holder_addresses = [holder["address"] for holder in holders]
    holder_roles = {sandbox_role_id(holder["employee_id"]) for holder in holders}
    provider = ProbeProvider(kinds, holder_addresses)
    server = LocalServer(
        lambda port: build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
    )
    result = None
    try:
        with server:
            box.configure_runtime(server.port)
            with SandboxDispatcher(
                runtime,
                box,
                {"probe": provider},
                mcp_url=f"http://127.0.0.1:{server.port}/mcp",
            ) as dispatcher:
                dispatcher.start()
                held = []
                all_deadline = time.monotonic() + 420
                while time.monotonic() < all_deadline:
                    dispatcher.tick()
                    held = box.mac_status()["stations"]
                    if len(held) == len(stations) and all(row["held"] for row in held):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("The roles never held all stations at once")
                assert sorted(row["station"] for row in held) == stations, held
                # The sandbox holds a station before the runtime has recorded whose lease
                # it is: an acquire is granted there first. Wait for the names.
                named_deadline = time.monotonic() + 60
                while True:
                    dispatcher.tick()
                    summary = runtime.mac.summary()["stations"]
                    named = [row["holder"] for row in summary]
                    if all(named) or time.monotonic() >= named_deadline:
                        break
                    time.sleep(0.2)
                assert sorted(str(name) for name in named) == sorted(holder_addresses), summary

                topology = topology_evidence(box, held)
                barrier_deadline = time.monotonic() + 600
                while time.monotonic() < barrier_deadline:
                    dispatcher.tick()
                    observed = probe_events(box, holder_roles | {
                        sandbox_role_id(contender["employee_id"])
                    })
                    ready = {int(e["station"]) for e in observed if e.get("event") == "all_held"}
                    if ready == set(stations) and any(
                        e.get("event") == "holders_named" for e in observed
                    ):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Every desktop and busy refusal did not reach the barrier")
                sshd = [e for e in observed if e.get("event") == "sshd"]
                assert {int(e["station"]) for e in sshd} == set(stations), sshd
                assert {e["host_key"] for e in sshd} == {topology["sshd_host_key"]}, sshd
                topology["lease_sshd"] = sshd
                for role in holder_roles:
                    box.linux._rpc("write-credential", role_id=role, name="acceptance-release",
                                   data=base64.b64encode(b"release").decode())
                completion_deadline = time.monotonic() + 420
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
                    raise TimeoutError("The holders and the contender did not settle")

                logs = capture_logs(box, args.evidence)
                # A probe that reached its own assertion failure still settles;
                # the lease tables alone would not reveal it.
                failed = [row for row in current
                          if row["employee_id"] != founder and row["state"] != "completed"]
                assert not failed, (failed, logs)
                tables = lease_tables(runtime)
                assert sorted(row["station"] for row in tables["leases"]) == stations, tables
                outcomes = [row["outcome"] for row in tables["leases"]]
                assert outcomes == ["released"] * len(stations), tables
                assert [e["event"] for e in tables["events"]].count("busy") >= 1, tables
                events = probe_events(box, holder_roles)
                endpoints = sorted(
                    (int(event["station"]), int(event["port"]))
                    for event in events
                    if event.get("event") == "all_held"
                )
                assert endpoints == [
                    (station, lease_ssh_port(station)) for station in stations
                ], endpoints
                after = box.mac_status()["stations"]
                assert all(
                    not row["held"] and not row["dirty"] for row in after
                ), after
                result = {
                    "passed": False,
                    "scope": "every station held on one Mac, shared sshd, one files-mac forward",
                    "selected_station": args.station,
                    "topology": topology,
                    "roles": {"holders": [h["employee_id"] for h in holders],
                              "contender": contender["employee_id"]},
                    "activations": current,
                    "lease_tables": tables,
                    "held_together": held,
                    "lease_endpoints": endpoints,
                    "stations_after": after,
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
        write_json(args.evidence / "result.json", failure)
        raise
    finally:
        box.down(force=True)
    if box.state() != "stopped":
        raise RuntimeError("All-station acceptance cleanup left a VM running")
    result["vms_stopped"] = True
    result["passed"] = True
    write_json(args.evidence / "result.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
