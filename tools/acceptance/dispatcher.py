"""Live dispatcher/MCP/storage acceptance using deterministic probe programs.

Uses no model-provider account. This checks real runtime admission through the
dispatcher, guest jobs, MCP authentication/hiring, sharing and cleanup; it is not
an LLM capability test. Run only against a fresh disposable firm.
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
from firm_sandbox import Config, RunSpec, Sandbox

PROGRAM = r"""
import json, os, time, urllib.request, urllib.error
from pathlib import Path
base=os.environ['PROBE_MCP']
token=os.environ['FIRM_ACTIVATION_TOKEN']
def post(path,value,auth=token):
    request=urllib.request.Request(base+path, data=json.dumps(value).encode(),headers={
      'Authorization':'Bearer '+auth,'Content-Type':'application/json',
      'Accept':'application/json, text/event-stream','Origin':base})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)
def tool(name,args):
    result=post('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call',
                      'params':{'name':name,'arguments':args}})
    assert not result.get('error') and not result['result'].get('isError'), result
    return result
try:
    post('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list'},auth='wrong-test-token')
    raise AssertionError('Unauthenticated MCP access succeeded')
except urllib.error.HTTPError as error:
    assert error.code==401
post('/activation/start',{})
home=Path(os.environ['PROBE_HOME']); shared=Path(os.environ['PROBE_SHARED'])
(home/'private-sentinel').write_text('private')
Path('public-sentinel').write_text('public')
tool('peek',{})
if os.environ['PROBE_FOUNDER']=='1':
    (shared/'dispatcher-shared').write_text('shared-content')
    tool('hire',{'slug':'child','mandate':'Verify collaboration through the dispatcher.',
                 'request_id':'dispatcher-probe-hire'})
    time.sleep(12)
else:
    assert (shared/'dispatcher-shared').read_text()=='shared-content'
    try:
        Path(os.environ['PROBE_FOUNDER_HOME'],'private-sentinel').read_text()
        raise AssertionError("Another role's private home was readable")
    except PermissionError:
        pass
    try:
        Path(os.environ['PROBE_FOUNDER_WORK'],'public-sentinel').write_text('bad')
        raise AssertionError("The manager's workspace was writable")
    except PermissionError:
        pass
    (shared/'dispatcher-child-result').write_text('child-shared-write')
tool('handoff',{'notes':'Dispatcher probe finished.','next':'wait',
                'request_id':'dispatcher-probe-done'})
print(json.dumps({'probe':'complete','uid':os.getuid()}),flush=True)
"""


class ProbeProvider:
    def __init__(self, founder, box):
        self.founder, self.box = founder, box

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
        founder_layout = self.box.layout(sandbox_role_id(self.founder))
        return RunSpec(
            role_id,
            (layout.python, layout.credential("probe-program")),
            run_id=run_id,
            env={
                "PROBE_MCP": mcp_url.removesuffix("/mcp"),
                "PROBE_HOME": layout.private,
                "PROBE_SHARED": layout.shared,
                "PROBE_FOUNDER": str(int(invocation.employee_id == self.founder)),
                "PROBE_FOUNDER_HOME": founder_layout.private,
                "PROBE_FOUNDER_WORK": founder_layout.workspace("founder"),
            },
            secrets={
                "activation-token": invocation.token.encode(),
                "probe-program": PROGRAM.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    def parse_result(self, logs, exit_code):
        complete = any(
            isinstance(row, dict) and row.get("probe") == "complete"
            for line in logs.splitlines()
            if line.startswith(b"{")
            for row in [json.loads(line)]
        )
        return SimpleNamespace(
            provider_session_id=None,
            success=exit_code == 0 and complete,
            error=None if complete else SimpleNamespace(kind="dispatcher_probe", category="config"),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
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
        "vm-dispatcher",
        "Validate the guest path through the dispatcher.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "probe",
            "profiles": {"probe": {}},
        },
    )["founder"]["employee_id"]
    provider = ProbeProvider(founder, box)
    server = LocalServer(
        lambda port: build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
    )
    try:
        with server:
            box.configure_runtime(server.port)
            with SandboxDispatcher(
                runtime, box, {"probe": provider}, mcp_url=f"http://127.0.0.1:{server.port}/mcp"
            ) as dispatcher:
                dispatcher.start()
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    dispatcher.tick()
                    with runtime.store.read() as db:
                        rows = [
                            dict(row)
                            for row in db.execute(
                                "SELECT activation_id,state,failure_category FROM activations"
                            )
                        ]
                        roles = [
                            dict(row)
                            for row in db.execute(
                                "SELECT employee_id,address FROM roles WHERE employee_id!='owner'"
                            )
                        ]
                    terminal = [row for row in rows if row["state"] not in {"admitted", "running"}]
                    if len(terminal) >= 2:
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Two dispatched jobs did not settle")
                inventory = box.runs()
                results = []
                for run in inventory:
                    logs = box.logs(run)
                    (args.evidence / f"{run.run_id}.log").write_bytes(logs)
                    status = box.status(run)
                    results.append(
                        {
                            "role_id": run.role_id,
                            "exit_code": status.get("exit_code"),
                            "log_truncated": status.get("log_truncated"),
                        }
                    )
                assert len(results) == 2 and all(row["exit_code"] == 0 for row in results), results
                assert len(roles) == 2, roles
                (args.evidence / "result.json").write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "roles": roles,
                            "activations": rows,
                            "runs": results,
                            "scope": (
                                "deterministic programs through real dispatcher "
                                "and authenticated MCP"
                            ),
                        },
                        indent=2,
                    )
                )
                dispatcher.stop()
                while runtime.status()["state"] != "stopped":
                    dispatcher.tick()
                    if time.monotonic() > deadline + 30:
                        raise TimeoutError("Runtime drain did not finish")
                    time.sleep(0.2)
        print(json.dumps({"passed": True, "evidence": str(args.evidence)}))
    finally:
        if not args.keep_running:
            box.down(force=True)


if __name__ == "__main__":
    main()
