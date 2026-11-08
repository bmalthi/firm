"""One real isolated summary and one hiring-level assessment in a disposable firm.

Both turns go through the runtime's own path: the account reservation, the
one-use summary gate, the provider's summary launch spec, the guest launcher and
the output parser. Neither turn admits a role activation. Credentials come only
from the explicitly selected provider configuration; evidence holds run records
and the guest's provider output, never credentials.
"""

import argparse
import json
import os
import sys
import time
import tomllib
import uuid
from pathlib import Path

from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.providers import Providers
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import OWNER, Store
from firm_sandbox import Config, Sandbox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_checks.acceptance_common import (  # noqa: E402
    PROVIDERS,
    agy_output_evidence,
    capture_agy_launches,
    selected_profile,
)

MANDATE = "Validate isolated summary and hiring-level turns. Do no other work."
HIRE_MANDATE = (
    "Reconcile the firm's monthly supplier invoices against purchase orders and report "
    "discrepancies to your manager."
)
MESSAGE = (
    "Please confirm the supplier invoice reconciliation is on track for Friday. "
    "Two purchase orders were still missing receipts yesterday."
)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, help="Require this profile provider")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    source = tomllib.loads(args.provider_config.read_text())
    try:
        profile, accounts = selected_profile(source, args.profile, args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    config = {
        "max_active": 2,
        "founder_profile": args.profile,
        # Every class maps to the selected profile, so the chooser's pick is
        # always applicable; this check is about the turn, not the choice.
        "staffing": {
            "profile": args.profile,
            "classes": {level: args.profile for level in ("low", "medium", "high")},
        },
        "profiles": {args.profile: profile},
        "accounts": accounts,
        "summary": {profile["provider"]: source.get("summary", {}).get(profile["provider"], {})},
    }
    sandbox_config = args.root / "sandbox.toml"
    os.umask(0o077)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(sandbox_config))
    launches = capture_agy_launches(box, args.evidence)
    outputs = {}
    # This check builds its own runtime database under --evidence, and a file
    # server may only ever serve one. A firm that has already run rejects it.
    if box.file_server.database_identity() is not None:
        raise SystemExit(
            f"{args.root} has already served a runtime database; this check needs a firm "
            "directory whose file server is unbound (a fresh disposable firm)"
        )
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize("vm-summary-live", MANDATE, config)["founder"]
    providers = Providers(runtime)
    preflight = providers.preflight()
    if preflight["failures"]:
        raise RuntimeError(f"Provider preflight failed: {preflight['failures']}")

    # Keep each isolated turn's guest output: the runner retires the summary
    # account once the turn exits.
    follow = box.follow

    def capture_follow(run):
        if profile["provider"] == "agy":
            logs = bytearray()
            try:
                for chunk in follow(run):
                    logs.extend(chunk)
                    yield chunk
            finally:
                receipt = agy_output_evidence(
                    bytes(logs), expected_model=launches.get(run.run_id, {}).get("native_model"))
                outputs[run.run_id] = receipt
                (args.evidence / f"{run.run_id}.output.json").write_text(
                    json.dumps(receipt, indent=2))
            return
        with (args.evidence / f"{run.run_id}.log").open("ab") as output:
            for chunk in follow(run):
                output.write(chunk)
                output.flush()
                yield chunk

    box.follow = capture_follow
    server = LocalServer(
        lambda port: build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
    )
    result = {"passed": False}
    try:
        with server:
            box.configure_runtime(server.port)
            with SandboxDispatcher(
                runtime, box, providers, mcp_url=f"http://127.0.0.1:{server.port}/mcp"
            ) as dispatcher:
                # The dispatcher is never ticked, so no role activation is admitted.
                dispatcher.start()
                deadline = time.monotonic() + 240

                # A context summary uses the caller session's account binding.
                # Bind a session for the founder exactly as admission would,
                # without launching the founder's turn.
                session_id = str(uuid.uuid4())
                with runtime.store.transaction() as db:
                    role = runtime.store.role(db, founder["employee_id"])
                    selected = runtime.accounts._select_fresh(
                        db, runtime.accounts.config(db), args.profile
                    )
                    if selected is None:
                        raise RuntimeError(f"No fresh account is eligible for {args.profile}")
                    runtime.accounts.bind(db, role, session_id, selected)
                sent = runtime.messaging.send(
                    OWNER, [founder["address"]], MESSAGE, request_id="summary-probe-message"
                )
                summary = runtime.messaging.context(
                    founder["employee_id"], sent["conversation_id"], session_id=session_id
                )

                hired = runtime.lifecycle.hire(
                    founder["employee_id"],
                    "invoices",
                    HIRE_MANDATE,
                    request_id="summary-probe-hire",
                )
                assert runtime.levels.tick(background=False) == [hired["employee_id"]]
                with runtime.store.read() as db:
                    level = runtime.levels.record(db, hired["employee_id"])
                    runs = [
                        dict(row)
                        for row in db.execute(
                            "SELECT run_id,kind,provider,state,reason,latency_ms,output_tokens "
                            "FROM summary_runs ORDER BY started_at"
                        )
                    ]

                result = {
                    "passed": summary["status"] == "summarized" and level["state"] == "chosen",
                    "provider": profile["provider"],
                    "summary": {
                        "status": summary["status"],
                        "reason": summary.get("reason"),
                        "text": summary.get("summary"),
                    },
                    "level": {
                        key: level[key] for key in ("state", "model_class", "effort", "reason")
                    },
                    "runs": runs,
                }
                if profile["provider"] == "agy":
                    result["summary"].pop("text", None)
                    result["launches"], result["outputs"] = launches, outputs
                    result["passed"] = result["passed"] and len(launches) == 2 and all(
                        launch["passed"] and key in outputs and outputs[key]["success"]
                        and outputs[key]["model_matches"] and outputs[key]["tool_steps"] == 0
                        for key, launch in launches.items()
                    )
                (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
                dispatcher.stop()
                while runtime.status()["state"] != "stopped":
                    dispatcher.tick()
                    if time.monotonic() > deadline + 30:
                        raise TimeoutError("Runtime drain did not settle")
                    time.sleep(0.2)
        print(json.dumps(result))
        if not result["passed"]:
            for run in result["runs"]:
                if run["state"] != "summarized":
                    print(
                        f"{run['kind']} run {run['run_id']} was {run['state']} "
                        f"({run['reason']}); evidence is in {args.evidence}"
                    )
            raise SystemExit(1)
    finally:
        box.down(force=True)


if __name__ == "__main__":
    main()
