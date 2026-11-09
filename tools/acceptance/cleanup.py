"""Live issue #5 cleanup through the real dispatcher and deterministic MCP probes.

No model provider or LLM cost. Use a fresh disposable firm with an unbound file
server. Checks handoff cleanup, temporary-appointment expiry and fired-role
retirement, then restarts the VM to inspect the retained state.
"""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, Organization, RunSpec, Sandbox

PROGRAM = r"""
import json, os, subprocess, urllib.request
from pathlib import Path
base=os.environ['PROBE_MCP']
def post(path,value):
    request=urllib.request.Request(base+path,data=json.dumps(value).encode(),headers={
        'Authorization':'Bearer '+os.environ['FIRM_ACTIVATION_TOKEN'],
        'Content-Type':'application/json','Accept':'application/json, text/event-stream',
        'Origin':base})
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)
def tool(name,args):
    result=post('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call',
                      'params':{'name':name,'arguments':args}})
    assert not result.get('error') and not result['result'].get('isError'), result
post('/activation/start',{})
role=os.environ['PROBE_ROLE']
assert role in {'founder','temp','fireme'}, role
scratch=Path(os.environ['PROBE_SCRATCH'])
(scratch/'cleanup-seed').write_text(role)
subprocess.run(['docker','run','--name','cleanup-stopped','alpine:3.20','true'],
               check=True,timeout=180)
if role=='founder':
    (scratch/'Dockerfile').write_text('FROM alpine:3.20\nRUN echo kept > /kept\n')
    subprocess.run(['docker','build','-t','cleanup-kept:latest',str(scratch)],
                   check=True,timeout=180)
    tool('hire',{'slug':'temp','mandate':'Seed scratch and a stopped container, then finish.',
                 'temporary_periods':1,'request_id':'cleanup-hire-temp'})
    tool('hire',{'slug':'fireme','mandate':'Seed scratch and a stopped container, then finish.',
                 'request_id':'cleanup-hire-fireme'})
    tool('handoff',{'notes':'Cleanup probe finished.','next':'wait','clean_up':True,
                    'request_id':'cleanup-handoff'})
print(json.dumps({'probe':'complete','role':role}),flush=True)
"""

VERIFY = r"""set -eu
test -d "$PROBE_SCRATCH"
test -z "$(ls -A "$PROBE_SCRATCH")"
containers=$(docker ps -a --format '{{.Names}}')
if printf '%s\n' "$containers" | grep -Fx cleanup-stopped; then exit 1; fi
if [ "$PROBE_ROLE" = founder ]; then docker image inspect cleanup-kept:latest; fi
printf '\nCLEANUP_VERIFY_PASS\n'
"""


class ProbeProvider:
    def build_spec(
        self, invocation, role_id, run_id, mcp_url, provider_session_id=None, *, layout=None,
    ):
        return RunSpec(
            role_id, (layout.python, layout.credential("probe-program")), run_id=run_id,
            env={"PROBE_MCP": mcp_url.removesuffix("/mcp"),
                 "PROBE_ROLE": invocation.context["address"].rsplit("/", 1)[-1],
                 "PROBE_SCRATCH": layout.scratch},
            secrets={"activation-token": invocation.token.encode(),
                     "probe-program": PROGRAM.encode()},
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    def parse_result(self, logs, exit_code):
        complete = any(
            isinstance(row, dict) and row.get("probe") == "complete"
            for line in logs.splitlines() if line.startswith(b"{")
            for row in [json.loads(line)]
        )
        return SimpleNamespace(
            provider_session_id=None, success=exit_code == 0 and complete,
            error=None if complete else SimpleNamespace(kind="cleanup_probe", category="unknown"),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    if box.file_server.database_identity() is not None:
        raise SystemExit(
            f"{args.root} has already served a runtime database; "
            "this check needs a fresh disposable firm with an unbound file server"
        )
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "vm-cleanup", "Validate runtime-driven scratch and Docker cleanup.",
        {"max_active": 2, "staffing": False, "founder_profile": "probe", "profiles": {"probe": {}}},
    )["founder"]["employee_id"]
    server = LocalServer(lambda port: build_http_app(
        runtime, allowed_hosts=[f"127.0.0.1:{port}"],
        allowed_origins=[f"http://127.0.0.1:{port}"],
    ))
    record = {"passed": False, "scope": "runtime handoff, expiry and fired-role cleanup"}
    try:
        with server:
            box.configure_runtime(server.port)
            with SandboxDispatcher(
                runtime, box, {"probe": ProbeProvider()},
                mcp_url=f"http://127.0.0.1:{server.port}/mcp",
            ) as dispatcher:
                dispatcher.start()
                deadline = time.monotonic() + 900
                pending = ["founder handoff cleanup", "temp expiry cleanup", "fireme retirement"]
                while time.monotonic() < deadline:
                    dispatcher.tick()
                    with runtime.store.read() as db:
                        roles = {row["address"].rsplit("/", 1)[-1]: dict(row) for row in db.execute(
                            "SELECT employee_id,address,employment FROM roles "
                            "WHERE employee_id!='owner'"
                        )}
                        rows = [dict(row) for row in db.execute(
                            "SELECT a.activation_id,a.employee_id,a.state,"
                            "a.failure_category,r.address "
                            "FROM activations AS a JOIN roles AS r USING(employee_id)"
                        )]
                    record.update(roles=roles, activations=rows)
                    failed = [row for row in rows if row["state"] in {"failed", "interrupted"}]
                    assert not failed, f"Probe periods failed: {failed}"
                    assert len({row["employee_id"] for row in rows}) == len(rows), rows
                    completed = {row["address"].rsplit("/", 1)[-1]: row for row in rows
                                 if row["state"] == "completed"}
                    if "fireme" in completed and roles["fireme"]["employment"] == "active":
                        record["fire"] = runtime.lifecycle.fire(
                            founder, roles["fireme"]["employee_id"],
                            reason="Cleanup acceptance probe finished.", request_id="cleanup-fire",
                        )
                    pending = [f"{name} completion/cleanup" for name in ("founder", "temp")
                               if name not in completed
                               or completed[name]["activation_id"] not in dispatcher._cleaned]
                    if ("fireme" not in roles or sandbox_role_id(roles["fireme"]["employee_id"])
                            not in box.file_server.retired_roles()):
                        pending.append("fireme guest/file-server retirement")
                    if not pending:
                        assert len(rows) == 3 and len(completed) == 3, rows
                        record["cleaned"] = sorted(dispatcher._cleaned)
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Cleanup did not finish: " + ", ".join(pending))
                dispatcher.stop()
                while runtime.status()["state"] != "stopped":
                    dispatcher.tick()
                    if time.monotonic() > deadline + 30:
                        raise TimeoutError("Runtime drain did not finish")
                    time.sleep(0.2)
            # Keep the local server open while restoring the sandbox's configured tunnel.
            box.up()
            record["verification"] = {}
            for name in ("founder", "temp"):
                role = roles[name]
                role_id = sandbox_role_id(role["employee_id"])
                run = box.start(RunSpec(
                    role_id, ("/bin/bash", "-lc", VERIFY),
                    organization=Organization(role["address"]),
                    env={"PROBE_ROLE": name, "PROBE_SCRATCH": box.layout(role_id).scratch},
                    clean_up=False,
                ))
                result = box.wait(run, timeout=300)
                logs = box.logs(run)
                (args.evidence / f"verify-{name}.log").write_bytes(logs)
                record["verification"][name] = result
                assert result.get("exit_code") == 0 and not result.get("unresolved"), result
                assert b"CLEANUP_VERIFY_PASS" in logs, name
            fireme = sandbox_role_id(roles["fireme"]["employee_id"])
            guest = box.linux.info()["guest"]["roles"][fireme]
            assert guest["retired"] is True, guest
            assert fireme in box.file_server.retired_roles()
            scratch = box.linux._shell(
                "sudo", "-n", "/bin/sh", "-ec",
                'test -d "$1"; test ! -L "$1"; test -z "$(ls -A "$1")"; echo FIREME_SCRATCH_EMPTY',
                "scratch-check", box.layout(fireme).scratch, timeout=30,
            )
            (args.evidence / "fireme-scratch.log").write_bytes(scratch.stdout + scratch.stderr)
            assert scratch.returncode == 0
            record["verification"]["fireme"] = {"guest_retired": True, "scratch_empty": True,
                                                 "file_server_retired": True}
            temp = roles["temp"]["employee_id"]
            with runtime.store.read() as db:
                assert runtime.store.role(db, founder)["employment"] == "active"
                assert runtime.store.role(db, temp)["employment"] == "active"
                fired = runtime.store.role(db, roles["fireme"]["employee_id"])
                assert fired["employment"] == "retired"
                terms = runtime.appointments.terms(db, temp)
                assert terms["kind"] == "temporary" and terms["remaining_periods"] == 0, terms
                record["appointment_before_renewal"] = terms
            record["renewal"] = runtime.appointments.renew(founder, temp, 1, "cleanup-renew")
            assert record["renewal"]["remaining_periods"] == 1, record["renewal"]
            record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        try:
            record["runs"] = []
            for run in box.runs():
                (args.evidence / f"{run.run_id}.log").write_bytes(box.logs(run))
                record["runs"].append({"role_id": run.role_id, "run_id": run.run_id,
                                       "status": box.status(run)})
        except Exception as error:
            record.update(passed=False, log_error=str(error))
        try:
            (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
        finally:
            if not args.keep_running:
                box.down(force=True)
    assert record["passed"], record
    print(json.dumps({"passed": True, "evidence": str(args.evidence)}))


if __name__ == "__main__":
    main()
