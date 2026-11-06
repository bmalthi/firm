"""One bounded real provider activation in a dedicated disposable firm.

Credentials come only from the explicitly selected provider configuration.
Evidence contains outcome metadata and a public test artifact, never credentials.
"""

import argparse
import json
import time
import tomllib
import uuid
from pathlib import Path

from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.providers import Providers
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, Organization, RunSpec, Sandbox

MANDATE = """Perform this bounded runtime validation and then wait. Call the peek role
MCP tool. Use your shell to write provider-validation.txt in your workspace
directory containing exactly VM_PROVIDER_OK followed by a newline. Then call the
handoff role MCP tool with next='wait' and notes describing the successful check.
Do not hire colleagues or perform other work. This is a single activation test.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    source = tomllib.loads(args.provider_config.read_text())
    profile = dict(source["profiles"][args.profile])
    profile.pop("alternates", None)
    accounts = {name: source["accounts"][name] for name in profile["accounts"]}
    config = {
        "max_active": 1,
        "staffing": False,
        "founder_profile": args.profile,
        "profiles": {args.profile: profile},
        "accounts": accounts,
    }
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize("vm-provider-live", MANDATE, config)["founder"]
    with runtime.store.read() as db:
        previous = {row[0] for row in db.execute("SELECT activation_id FROM activations")}
    if args.resume:
        runtime.owner.route(
            "owner", founder["employee_id"], args.profile, f"provider-probe-{uuid.uuid4()}"
        )
        # This acceptance fixture may have accumulated retry backoff while
        # diagnosing a launcher failure. Advance its clock rather than alter
        # production recovery records or wait half an hour between probes.
        with runtime.store.read() as db:
            role = runtime.store.role(db, founder["employee_id"])
            offset = max(0, (role["retry_at"] or 0) - time.time()) + 1
        runtime.store.clock = lambda: time.time() + offset
    providers = Providers(runtime)
    preflight = providers.preflight()
    if preflight["failures"]:
        raise RuntimeError(f"Provider preflight failed: {preflight['failures']}")
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    read_logs = box.logs

    def capture_logs(run):
        value = read_logs(run)
        path = args.evidence / f"{run.run_id}.log"
        path.write_bytes(value)
        path.chmod(0o600)
        return value

    box.logs = capture_logs
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
                runtime, box, providers, mcp_url=f"http://127.0.0.1:{server.port}/mcp"
            ) as dispatcher:
                dispatcher.start()
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    dispatcher.tick()
                    with runtime.store.read() as db:
                        rows = [
                            dict(row)
                            for row in db.execute(
                                "SELECT activation_id,state,failure_category FROM activations"
                            )
                            if row["activation_id"] not in previous
                        ]
                    if any(row["state"] not in {"admitted", "running"} for row in rows):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Provider activation did not settle within four minutes")
                assert len(rows) == 1 and rows[0]["state"] == "completed", rows
                check = box.start(RunSpec(
                    sandbox_role_id(founder["employee_id"]),
                    ("/bin/cat", "provider-validation.txt"),
                    organization=Organization("founder"),
                ))
                assert box.wait(check, timeout=120)["exit_code"] == 0, box.logs(check)
                content = box.logs(check)
                assert content == b"VM_PROVIDER_OK\n", "Public provider artifact did not match"
                result = {
                    "passed": True,
                    "provider": profile["provider"],
                    "model": profile["model"],
                    "activations": rows,
                    "artifact": content.decode().strip(),
                }
                (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
                dispatcher.stop()
                while runtime.status()["state"] != "stopped":
                    dispatcher.tick()
                    if time.monotonic() > deadline + 30:
                        raise TimeoutError("Runtime drain did not settle")
                    time.sleep(0.2)
        print(json.dumps(result))
    finally:
        box.down(force=True)


if __name__ == "__main__":
    main()
