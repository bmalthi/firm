"""One bounded real provider activation in a dedicated disposable firm.

Credentials come only from the explicitly selected provider configuration.
Evidence contains outcome metadata and a public test artifact, never credentials.
"""

import argparse
import json
import os
import sys
import time
import tomllib
import uuid
from dataclasses import replace
from functools import wraps
from pathlib import Path

from firm_runtime.agy_provider import parse_result as agy_result
from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.providers import Providers
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store
from firm_runtime.workspaces import sandbox_role_id
from firm_sandbox import Config, Organization, RunSpec, Sandbox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_checks.acceptance_common import (  # noqa: E402
    PROVIDERS,
    agy_output_evidence,
    capture_agy_launches,
    native_id_from_output,
    selected_profile,
)

MANDATE = """Perform this bounded runtime validation and then wait. Call the peek role
MCP tool. Use your shell to write provider-validation.txt in your workspace
directory containing exactly VM_PROVIDER_OK followed by a newline. Then call the
handoff role MCP tool with next='wait' and notes describing the successful check.
Do not hire colleagues or perform other work. This is a single activation test.
"""
AGY_MANDATE = """Perform this bounded runtime validation. Call the peek role MCP
tool once. Use your shell to write provider-validation.txt in your workspace
directory containing exactly VM_PROVIDER_OK followed by a newline. Then return
a final answer of DONE. Preserve this conversation for the next owner message:
do not call handoff or wait. Do not hire colleagues or perform other work.
If the peek MCP tool is unavailable, report that and finish without investigating
or using shell commands to emulate MCP. This is a single activation test.
"""
AGY_RECALL = """\nFor this resumed activation, replace the final DONE answer with
exactly RECALLED: followed by the synthetic recall nonce from your first activation.
Use only your retained conversation memory; do not inspect files or native state
to recover it. Still perform the peek and workspace artifact checks above.
"""


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, help="Require this profile provider")
    parser.add_argument("--resume", action="store_true")
    return parser


def wake_retained_session(runtime, founder, profile):
    with runtime.store.read() as db:
        role = runtime.store.role(db, founder["employee_id"])
        session = db.execute(
            "SELECT * FROM provider_sessions WHERE session_id=?", (role["session_id"],)
        ).fetchone()
        if not session or role["execution_profile"] != profile:
            raise ValueError("--resume requires the original profile and a retained native session")
        session_id, native_id = role["session_id"], session["native_session_id"]
    runtime.messaging.send(
        "owner", [founder["address"]], AGY_MANDATE + AGY_RECALL,
        request_id=f"provider-probe-{uuid.uuid4()}"
    )
    return session_id, native_id


def prepare_resume(runtime, founder, profile, provider):
    if provider == "agy":
        return wake_retained_session(runtime, founder, profile)
    # Preserve the existing Codex/Claude acceptance fixture's fresh-route retry.
    runtime.owner.route(
        "owner", founder["employee_id"], profile, f"provider-probe-{uuid.uuid4()}"
    )
    return None, None


def capture_server_peek(runtime, directory):
    tools, receipts = runtime.tools, []

    def capture_tools(token):
        facade = tools(token)
        peek = facade.peek

        @wraps(peek)
        def capture(*args, **kwargs):
            result = peek(*args, **kwargs)
            # Observe the authenticated tool facade, not internal admission's
            # inbox read. Never retain tokens, inbox contents, or response text.
            receipts.append({"tool": "peek", "activation_validated": True})
            (directory / "server-tools.json").write_text(json.dumps(receipts, indent=2))
            return result

        facade.peek = capture
        return facade

    runtime.tools = capture_tools
    return receipts


def seed_recall_nonce(box, nonce):
    start = box.start

    def capture(spec):
        if spec.env.get("FIRM_AGY_MODE") == "role":
            event = json.loads(spec.stdin)
            event["message"]["content"] += (
                f"\nRemember this synthetic recall nonce for the next activation: {nonce}. "
                "Keep it only in conversation memory; do not write it to workspace files. "
                "Perform the mandate and finish with DONE."
            )
            spec = replace(spec, stdin=(json.dumps(event) + "\n").encode())
        return start(spec)

    box.start = capture


def capture_provider_logs(box, directory, provider, launches, expected_native=None,
                          recall_nonce=None):
    read_logs = box.logs
    outputs, buffers = {}, {}

    def capture_logs(run, *, offset=0, until=None):
        value = read_logs(run, offset=offset, until=until)
        buffer = buffers.setdefault(run.run_id, bytearray())
        if offset > len(buffer):
            buffer[:] = read_logs(run, until=offset)
        buffer[offset:offset + len(value)] = value
        full = bytes(buffer)
        if provider == "agy":
            receipt = agy_output_evidence(
                full, expected_model=launches.get(run.run_id, {}).get("native_model"),
                expected_id=expected_native,
            )
            if recall_nonce is not None:
                response = (agy_result(full, 0).final_text or "").strip()
                receipt["recall_matches"] = (
                    receipt["success"] and response.startswith("RECALLED:")
                    and response.removeprefix("RECALLED:").strip() == recall_nonce
                )
            outputs[run.run_id] = (receipt, native_id_from_output(full))
            path = directory / f"{run.run_id}.output.json"
            path.write_text(json.dumps(receipt, indent=2))
        else:
            path = directory / f"{run.run_id}.log"
            path.write_bytes(full)
        path.chmod(0o600)
        return value

    box.logs = capture_logs
    return outputs


def main():
    parser = argument_parser()
    args = parser.parse_args()
    source = tomllib.loads(args.provider_config.read_text())
    try:
        profile, accounts = selected_profile(source, args.profile, args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    config = {
        "max_active": 1,
        "staffing": False,
        "founder_profile": args.profile,
        "profiles": {args.profile: profile},
        "accounts": accounts,
    }
    os.umask(0o077)
    if args.resume and not (args.evidence / "firm.sqlite3").is_file():
        parser.error("--resume requires the original evidence database")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    nonce = None
    if profile["provider"] == "agy":
        nonce_path = args.evidence / "recall-nonce.txt"
        if args.resume:
            if not nonce_path.is_file():
                parser.error("--resume requires the original synthetic recall nonce")
            nonce = nonce_path.read_text().strip()
        else:
            nonce = uuid.uuid4().hex
            nonce_path.write_text(nonce)
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    server_tools = (capture_server_peek(runtime, args.evidence)
                    if profile["provider"] == "agy" else [])
    mandate = AGY_MANDATE if profile["provider"] == "agy" else MANDATE
    founder = runtime.lifecycle.initialize("vm-provider-live", mandate, config)["founder"]
    with runtime.store.read() as db:
        previous = {row[0] for row in db.execute("SELECT activation_id FROM activations")}
    expected_session = expected_native = None
    if args.resume:
        expected_session, expected_native = prepare_resume(
            runtime, founder, args.profile, profile["provider"])
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
    if nonce is not None and not args.resume:
        seed_recall_nonce(box, nonce)
    launches = capture_agy_launches(box, args.evidence)
    outputs = capture_provider_logs(
        box, args.evidence, profile["provider"], launches, expected_native,
        nonce if args.resume else None)
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
                with runtime.store.read() as db:
                    current = runtime.store.role(db, founder["employee_id"])
                    native = db.execute(
                        "SELECT * FROM provider_sessions WHERE session_id=?",
                        (current["session_id"],),
                    ).fetchone()
                if args.resume and profile["provider"] == "agy":
                    result["resume"] = {
                        "runtime_session_retained": current["session_id"] == expected_session,
                        "native_session_retained": bool(native) and
                        native["native_session_id"] == expected_native,
                        "state_recalled": bool(launches) and all(
                            outputs.get(key, ({},))[0].get("recall_matches") for key in launches),
                    }
                    result["passed"] = all(result["resume"].values())
                if profile["provider"] == "agy":
                    result["launches"] = launches
                    result["server_tools"] = server_tools
                    result["outputs"] = {key: value[0] for key, value in outputs.items()
                                         if key in launches}
                    result["passed"] = result["passed"] and bool(server_tools) and bool(launches)
                    result["passed"] = result["passed"] and all(
                        launch["passed"] and key in outputs and outputs[key][0]["success"]
                        and outputs[key][0]["model_matches"]
                        and outputs[key][0]["firm_tool_seen"]
                        and bool(native) and outputs[key][1] == native["native_session_id"]
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
            raise SystemExit(1)
    finally:
        try:
            # Request graceful shutdown if dispatcher drain has not already
            # powered off the VM. Resume relies on durable guest state.
            box.down(force=profile["provider"] != "agy")
        except Exception:
            box.down(force=True)
            raise


if __name__ == "__main__":
    main()
