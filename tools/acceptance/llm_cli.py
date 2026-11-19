"""Live acceptance check for host-brokered guest LLM CLIs (`claude`, `codex`, `agy`).

Runs a deterministic probe role in a fresh disposable Linux-only firm whose PATH
includes the runtime-installed LLM CLI shims, invoking real guest CLIs against the
host `/llm/{start,heartbeat,finish}` and `/credentials` broker endpoints.
Evidence holds booleans, states, and token/timing totals, never credentials or nonces.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

from firm_sandbox import Config, RunSpec, Sandbox

from firm_runtime import credential_mirror
from firm_runtime.hosting import LocalServer
from firm_runtime.llm_service import LlmService
from firm_runtime.mcp_http import build_http_app
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_checks.acceptance_common import PROVIDERS, selected_profile  # noqa: E402

PROGRAM = r"""
import os, re, runpy, subprocess, urllib.request, json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

base = os.environ["PROBE_MCP"]
provider = os.environ["PROBE_PROVIDER"]
nonce = os.environ["PROBE_NONCE"]
creds_dir = os.environ["FIRM_CREDENTIALS_DIR"]
state_dir = os.environ["PROBE_STATE"]
private_home = os.environ["PROBE_PRIVATE"]
llm_url = os.environ["FIRM_LLM_URL"]

def post(path, value):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(value).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["FIRM_ACTIVATION_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Origin": base,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)

post("/activation/start", {})

ns = runpy.run_path(os.path.join(creds_dir, "llm-cli"), run_name="firm_llm_cli_probe")
ns["install_shims"](state_dir, creds_dir, private_home, llm_url)

def cli_args(prompt_text, *, resume_id=None, tools=True):
    if provider == "codex":
        base_args = ["codex", "exec"]
        if not tools:
            base_args.append("--no-tools")
        if resume_id:
            return [*base_args, "resume", resume_id, prompt_text]
        return [*base_args, prompt_text]
    base_args = [provider, "-p"]
    if not tools:
        base_args.extend(["--tools", ""])
    if resume_id:
        base_args.extend(["--resume", resume_id])
    base_args.append(prompt_text)
    return base_args

def extract_session_id(stderr_text):
    m = re.search(r"^firm:\s+session\s+(\S+)\s*$", stderr_text, re.MULTILINE)
    return m.group(1) if m else None

def run_one(prompt_text, *, resume_id=None, tools=False):
    return subprocess.run(
        cli_args(prompt_text, resume_id=resume_id, tools=tools),
        capture_output=True,
        text=True,
        timeout=int(os.environ["PROBE_TIMEOUT"]) + 30,
    )

# 1) Fresh tool-free call seeding a one-time recall nonce.
fresh = run_one(
    f"Remember this private nonce for a follow-up question: {nonce}. Reply with exactly FRESH_OK.",
    tools=False,
)
session_id = extract_session_id(fresh.stderr)
fresh_ok = fresh.returncode == 0 and fresh.stdout.strip() == "FRESH_OK" and bool(session_id)

# 2) Resumed call verifying persisted conversation recall.
recalled = False
if session_id:
    resumed = run_one(
        "Repeat ONLY the exact format RECALLED:<nonce> using the nonce from our previous turn.",
        resume_id=session_id,
        tools=False,
    )
    expected = f"RECALLED:{nonce}"
    recalled = resumed.returncode == 0 and resumed.stdout.strip() == expected

# 3) Concurrent calls triggering broker capacity / busy refusal on a 3rd overlapping call.
with ThreadPoolExecutor(max_workers=3) as pool:
    futs = [
        pool.submit(
            run_one,
            f"Reply briefly with CONCURRENT_{idx}. Wait no tools.",
            tools=False,
        )
        for idx in range(3)
    ]
    results = [fut.result() for fut in futs]

concurrent_succeeded = any(r.returncode == 0 for r in results)
concurrent_refused = any(
    r.returncode in (2, 75)
    and ("at most" in r.stderr or "no free account capacity" in r.stderr)
    for r in results
)

# 4) Verify no ephemeral staged auth directories remain and persist dir has session state.
llm_dir = Path(state_dir) / "llm"
cleanup_clean = not llm_dir.exists() or list(llm_dir.iterdir()) == []
persist_root = Path(private_home) / ".firm" / "cli" / provider
session_persisted = persist_root.exists() and any(persist_root.rglob("*"))

print(
    json.dumps(
        {
            "probe": "llm_cli",
            "fresh_ok": fresh_ok,
            "recalled": recalled,
            "session_present": bool(session_id),
            "concurrent_succeeded": concurrent_succeeded,
            "concurrent_refused": concurrent_refused,
            "cleanup_clean": cleanup_clean,
            "session_persisted": session_persisted,
        }
    ),
    flush=True,
)
"""


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, help="Require this profile provider")
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-CLI call timeout in seconds (minimum 30)",
    )
    return parser


def build_firm_config(source, profile_name, provider_override=None, *, timeout=120):
    if timeout < 30:
        raise ValueError("--timeout must be at least 30 seconds")
    profile, accounts = selected_profile(source, profile_name, provider_override)
    cli_provider = profile["provider"]
    llm_source = source.get("llm") or {}
    if isinstance(llm_source, dict) and isinstance(llm_source.get(cli_provider), dict):
        cli_settings = dict(llm_source[cli_provider])
    else:
        cli_settings = {"model": profile["model"], "effort": profile["effort"]}
    return {
        "max_active": 2,
        "staffing": False,
        "founder_profile": profile_name,
        "profiles": {profile_name: profile},
        "accounts": accounts,
        "llm": {
            "enabled": True,
            "max_concurrent": 2,
            "timeout_seconds": timeout,
            cli_provider: cli_settings,
        },
    }


def sanitize_evidence(provider, runs, probe):
    """Extract only safe structural fields and booleans for saved acceptance evidence."""
    llm_runs = [
        {
            "kind": r["kind"],
            "provider": r["provider"],
            "state": r["state"],
            "reason": r["reason"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "latency_ms": r["latency_ms"],
            "run_id_sha256": hashlib.sha256(str(r["run_id"]).encode()).hexdigest()[:16],
        }
        for r in runs
        if r.get("kind") == "llm"
    ]
    checks = {
        "fresh_ok": bool(probe and probe.get("fresh_ok")),
        "recalled": bool(probe and probe.get("recalled")),
        "session_present": bool(probe and probe.get("session_present")),
        "concurrent_succeeded": bool(probe and probe.get("concurrent_succeeded")),
        "concurrent_refused": bool(probe and probe.get("concurrent_refused")),
        "cleanup_clean": bool(probe and probe.get("cleanup_clean")),
        "session_persisted": bool(probe and probe.get("session_persisted")),
        "completed_runs": sum(1 for r in llm_runs if r["state"] == "summarized"),
    }
    passed = all(
        checks[k]
        for k in (
            "fresh_ok",
            "recalled",
            "session_present",
            "concurrent_succeeded",
            "concurrent_refused",
            "cleanup_clean",
            "session_persisted",
        )
    ) and checks["completed_runs"] >= 2
    return {
        "passed": passed,
        "provider": provider,
        "checks": checks,
        "llm_runs": llm_runs,
    }


class ProbeProvider:
    name = "probe"
    model = "probe"

    def __init__(self, cli_provider: str, nonce: str, timeout: int):
        self.cli_provider = cli_provider
        self.nonce = nonce
        self.timeout = timeout

    def validate_credentials(self):
        return None

    def build_summary_spec(self, request, layout):
        raise NotImplementedError("probe only executes roles")

    def parse_summary(self, logs, exit_code):
        raise NotImplementedError("probe only executes roles")

    def parse_usage(self, logs):
        return None

    def parse_tool_calls(self, logs):
        return {}

    def parse_subscription(self, logs):
        return None

    def build_spec(self, request, *, layout):
        llm_url = request.mcp_url.removesuffix("/mcp") + "/llm"
        llm_env, llm_secrets, _ = LlmService.launch_settings(llm_url)
        return RunSpec(
            request.role_id,
            (layout.python, layout.credential("probe-program")),
            run_id=request.run_id,
            env={
                **llm_env,
                "PROBE_MCP": request.mcp_url.removesuffix("/mcp"),
                "PROBE_STATE": layout.state,
                "PROBE_PRIVATE": layout.private,
                "PROBE_PROVIDER": self.cli_provider,
                "PROBE_NONCE": self.nonce,
                "PROBE_TIMEOUT": str(self.timeout),
            },
            secrets={
                **llm_secrets,
                "credential-mirror": Path(credential_mirror.__file__).read_bytes(),
                "activation-token": request.invocation.token.encode(),
                "probe-program": PROGRAM.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    def parse_result(self, logs, exit_code):
        complete = False
        for line in logs.splitlines():
            if not line.startswith(b"{"):
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict) and row.get("probe") == "llm_cli":
                complete = True
        return SimpleNamespace(
            provider_session_id=None,
            success=exit_code == 0 and complete,
            error=None if complete else SimpleNamespace(kind="llm_cli_probe", category="unknown"),
        )


def _extract_probe_line(logs: bytes) -> dict | None:
    for line in logs.splitlines():
        if not line.startswith(b"{"):
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and row.get("probe") == "llm_cli":
            return row
    return None


def main():
    parser = argument_parser()
    args = parser.parse_args()
    source = tomllib.loads(args.provider_config.read_text())
    try:
        config = build_firm_config(source, args.profile, args.provider, timeout=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))

    cli_provider = source["profiles"][args.profile]["provider"]
    os.umask(0o077)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    if box.file_server.database_identity() is not None:
        raise SystemExit(
            f"{args.root} has already served a runtime database; this check needs a firm "
            "directory whose file server is unbound (a fresh disposable firm)"
        )

    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "vm-llm-cli-live",
        "Validate host-brokered guest LLM CLI execution.",
        config,
    )
    probe_logs: dict[str, bytearray] = {}
    follow = box.follow

    def capture_follow(run):
        buf = probe_logs.setdefault(run.run_id, bytearray())
        for chunk in follow(run):
            buf.extend(chunk)
            yield chunk

    box.follow = capture_follow
    server = LocalServer(
        lambda port: build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
    )
    nonce = secrets.token_hex(16)
    result = {"passed": False, "provider": cli_provider}
    try:
        with server:
            box.configure_runtime(server.port)
            probe = ProbeProvider(cli_provider, nonce, args.timeout)
            with SandboxDispatcher(
                runtime,
                box,
                {cli_provider: probe},
                mcp_url=f"http://127.0.0.1:{server.port}/mcp",
            ) as dispatcher:
                dispatcher.start()
                deadline = time.monotonic() + max(240, args.timeout * 4)
                while time.monotonic() < deadline:
                    dispatcher.tick()
                    with runtime.store.read() as db:
                        rows = [
                            dict(r)
                            for r in db.execute(
                                "SELECT activation_id,state,failure_reason FROM activations"
                            )
                        ]
                        runs = [
                            dict(r)
                            for r in db.execute(
                                "SELECT run_id,kind,provider,state,reason,input_tokens,"
                                "output_tokens,latency_ms FROM summary_runs ORDER BY started_at"
                            )
                        ]
                    if rows and all(
                        r["state"] in {"completed", "failed", "interrupted"} for r in rows
                    ):
                        raw = b"".join(bytes(b) for b in probe_logs.values())
                        result = sanitize_evidence(cli_provider, runs, _extract_probe_line(raw))
                        if any(r["state"] != "completed" for r in rows):
                            result["passed"] = False
                        break
                    time.sleep(0.25)
    finally:
        (args.evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
