"""Real provider tasks inside a token-free parent, on a fresh disposable Linux firm.

Evidence contains analysis snapshots and projections of provider output, not raw
logs, launch payloads, environment values or credentials. No Mac configuration.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from firm_runtime.analysis import AnalysisReader
from firm_runtime.hosting import LocalServer
from firm_runtime.mcp_http import build_http_app
from firm_runtime.provider_contract import ProviderResult
from firm_runtime.providers import Providers
from firm_runtime.runtime import Runtime
from firm_runtime.sandbox_dispatcher import SandboxDispatcher
from firm_runtime.store import OWNER, Store
from firm_runtime.task_runner import read_child_logs
from firm_sandbox import Config, RunSpec, Sandbox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_checks.acceptance_common import (  # noqa: E402
    AGY_PROJECTION_CHECK,
    PROVIDERS,
    agy_launch_evidence,
    agy_output_evidence,
    provider_output,
    selected_profile,
)

CHECKS = ("answer", "isolation", "auth_files", "concurrency", "timeout", "scope",
          "parent_stop", "analysis")

# Inspect after the provider exits but before the supervisor removes the task home.
# AGY additionally checks its access-only JSON shape; no values or link targets are emitted.
AUTH_PROBE = r'''
import json, os, stat, subprocess, sys
from pathlib import Path
''' + AGY_PROJECTION_CHECK + r'''

def auth_evidence(path, requires_symlink, run_root=Path('/run')):
    try:
        in_run = path.is_symlink() and path.resolve(strict=True).is_relative_to(run_root)
    except (OSError, RuntimeError):
        in_run = False
    absent = not path.is_symlink() and not path.exists()
    return {'path': str(path), 'requires_symlink': requires_symlink,
            'symlink_into_run': in_run, 'absent': absent,
            'passed': in_run if requires_symlink else absent}

def agy_auth_evidence(path, credential, run_root=Path('/run')):
    result = {'path': str(path), 'private_file': False, 'projection_into_run': False,
              'absent': not path.exists() and not path.is_symlink(),
              **agy_projection_evidence(b'')}
    try:
        result['projection_into_run'] = credential.resolve(strict=True).is_relative_to(run_root)
        details = credential.stat()
        result['private_file'] = (stat.S_ISREG(details.st_mode)
                                  and stat.S_IMODE(details.st_mode) == 0o600
                                  and details.st_uid == os.getuid())
        if result['projection_into_run'] and result['private_file'] and details.st_size <= 65536:
            result.update(agy_projection_evidence(credential.read_bytes()))
        result['passed'] = (result['absent'] and result['projection_into_run']
                            and result['private_file'] and result['passed'])
    except (OSError, ValueError):
        pass
    return result

if __name__ == '__main__':
    provider = sys.argv[1]
    code = subprocess.run(sys.argv[2:], check=False).returncode
    state = Path(os.environ['FIRM_STATE_DIR'])
    if provider == 'agy':
        home = (Path(os.environ['FIRM_PRIVATE_HOME']) / '.firm/agy-sessions'
                / os.environ['FIRM_RUNTIME_SESSION_ID'])
        receipt = agy_auth_evidence(
            home / '.gemini/antigravity-cli/antigravity-oauth-token',
            Path(os.environ['FIRM_CREDENTIALS_DIR']) / 'agy-auth')
    elif provider in ('codex', 'claude'):
        name = {'codex': 'auth.json', 'claude': '.credentials.json'}[provider]
        required = provider == 'codex' or os.environ['FIRM_CLAUDE_AUTH_KIND'] == 'credentials-json'
        receipt = auth_evidence(state / provider / name, required)
    else:
        raise ValueError('unsupported acceptance provider')
    print(json.dumps({'task_auth_files': receipt}), flush=True)
    sys.exit(code if code >= 0 else 128 - code)
'''

# Installed by the placeholder in its own cwd. Only the provider executes the
# barrier/detach/scope modes. Host inspection prints selected process facts only.
GUEST = r'''
import json, os, signal, subprocess, sys, time, urllib.request
from pathlib import Path

def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start': fields[19], 'state': fields[0]}
    except FileNotFoundError:
        return None

def mark(name, extra=None):
    value = {**identity(os.getpid()), 'time': time.time(),
             'cgroup': Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]}
    value.update(extra or {})
    temp = work / (name + '.tmp')
    temp.write_text(json.dumps(value))
    temp.replace(work / (name + '.json'))
    return value

mode = sys.argv[1]
if mode == 'parent':
    base = os.environ['PROBE_BASE']
    request = urllib.request.Request(base + '/activation/start', data=b'{}', headers={
        'Authorization': 'Bearer ' + os.environ.pop('FIRM_ACTIVATION_TOKEN'),
        'Content-Type': 'application/json', 'Origin': base})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
    work = Path.cwd() / 'task-acceptance'
    work.mkdir()
    (work / 'probe.py').write_text(Path(__file__).read_text())
    (work / 'input.json').write_text(os.environ['PROBE_INPUT'])
    for name in ('AGENTS.md', 'CLAUDE.md', 'GEMINI.md'):
        Path(name).write_text('Your role instruction marker is ' + os.environ['PROBE_ROLE_MARKER'])
    def stopped(signum, frame):
        target = work / 'stop-target.json'
        if target.exists():
            group = json.loads(target.read_text())['cgroup']
            print(json.dumps({'parent_stop': True, 'time': time.time(),
                              'child_cgroup_gone': not Path('/sys/fs/cgroup' + group).exists()}),
                  flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stopped)
    print(json.dumps({'parent_ready': True, 'workspace': str(work), 'pid': os.getpid()}),
          flush=True)
    while True:
        time.sleep(1)
else:
    work = Path(sys.argv[2])
    if mode == 'inspect':
        markers = {p.stem: json.loads(p.read_text()) for p in work.glob('*.json')
                   if p.stem not in ('input', 'stop-target')}
        groups = json.loads(sys.argv[3])
        tags = [('FIRM_RUN_ID=' + g.rsplit('/task-', 1)[1]).encode() for g in groups]
        processes = []
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():
                continue
            try:
                lines = (p / 'cgroup').read_text().splitlines()
                group = next((line[3:] for line in lines if line.startswith('0::')), '')
                tagged = any(tag in (p / 'environ').read_bytes().split(b'\0') for tag in tags)
                ident = identity(p.name)
                if ident and (tagged or any(group == g or group.startswith(g + '/')
                                            for g in groups)):
                    processes.append({**ident, 'cgroup': group})
            except (FileNotFoundError, ProcessLookupError):
                pass
        tracked = [identity(pid) for pid in json.loads(sys.argv[4])]
        print(json.dumps({'markers': markers, 'processes': processes, 'tracked': tracked,
                          'groups': {g: Path('/sys/fs/cgroup' + g).exists() for g in groups}}))
    elif mode == 'release':
        (work / 'release').touch()
    elif mode == 'stop-target':
        (work / 'stop-target.json').write_text(sys.argv[3])
    elif mode == 'barrier':
        mark(sys.argv[3])
        release = 'release' if sys.argv[3].startswith('parallel-') else 'stopping-release'
        while not (work / release).exists():
            time.sleep(.1)
        print('barrier released', flush=True)
    elif mode == 'detach':
        # setsid plus double fork; ordinary process-group killing cannot reach this.
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if os.fork():
                os._exit(0)
            with open('/dev/null', 'rb+', buffering=0) as stream:
                for fd in (0, 1, 2):
                    os.dup2(stream.fileno(), fd)
            mark('detached', {'session': os.getsid(0)})
            while True:
                time.sleep(1)
        os.waitpid(pid, 0)
        mark('hanging')
        while True:
            time.sleep(1)
    elif mode == 'scope':
        command = ['systemd-run', '--user', '--scope', '/usr/bin/touch', str(work / 'escaped')]
        result = subprocess.run(command, capture_output=True, timeout=20)
        mark('scope', {'returncode': result.returncode, 'escaped': (work / 'escaped').exists()})
        print(json.dumps({'scope_returncode': result.returncode}), flush=True)
'''


class Parent:
    def __init__(self, fixture, marker):
        self.fixture, self.marker, self.invocation = fixture, marker, None

    def build_spec(self, invocation, role_id, run_id, mcp_url, provider_session_id=None, *, layout):
        self.invocation = invocation
        return RunSpec(
            role_id, (layout.python, layout.credential("probe"), "parent"), run_id=run_id,
            env={"PROBE_BASE": mcp_url.removesuffix("/mcp"),
                 "PROBE_INPUT": json.dumps(self.fixture),
                 "PROBE_ROLE_MARKER": self.marker},
            secrets={"probe": GUEST.encode(),
                     "activation-token": invocation.token.encode()},
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    @staticmethod
    def parse_result(logs, exit_code):
        return ProviderResult(None, exit_code == 0, None, None)


class TaskProviders(Providers):
    """Replace only role execution; inherited task_runner builds real provider workers."""

    def __init__(self, runtime, parent):
        super().__init__(runtime)
        self.parent = parent
        self.launches = {}

    def for_session(self, session_id):
        return self.parent

    def task_runner(self, sandbox, *, mcp_url):
        runner = super().task_runner(sandbox, mcp_url=mcp_url)
        build = runner.build_spec

        def build_with_auth_probe(request, parent):
            spec = build(request, parent)
            if request.provider == "agy":
                receipt = agy_launch_evidence(spec)
                self.launches[request.run_id] = receipt
                if not receipt["passed"]:
                    raise ValueError("AGY launch route or access projection failed acceptance")
            return replace(spec, argv=(sandbox.child_layout(parent.role_id, request.run_id).python,
                                       "-c", AUTH_PROBE, request.provider, *spec.argv))

        runner.build_spec = build_with_auth_probe
        parse = getattr(runner, "parse_output", None)
        if parse is not None:
            def parse_with_probe(request, logs, *args, **kwargs):
                if request.provider == "agy":
                    logs = provider_output(logs)
                return parse(request, logs, *args, **kwargs)
            runner.parse_output = parse_with_probe
        return runner


def wait_for(probe, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = probe()
        if value:
            return value
        time.sleep(.2)
    raise TimeoutError("Acceptance observation did not arrive before its deadline")


def output_evidence(logs, adapter, marker):
    """Project provider output without copying arbitrary text or tool arguments."""
    events = []
    for line in logs.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    tools = adapter.parse_tool_calls(provider_output(logs))
    inventories = [e.get("init", {}) if e.get("event") == "init" else e for e in events]
    advertised = [tool for e in inventories if isinstance(e, dict)
                  and isinstance(e.get("tools"), list)
                  for tool in e["tools"] if isinstance(tool, str)]
    return {
        "sha256": hashlib.sha256(logs).hexdigest(), "bytes": len(logs),
        "json_events": len(events), "tool_calls": sum(tools.values()),
        "firm_tools_seen": any("firm" in name.lower() for name in [*tools, *advertised]),
        "role_marker_seen": marker.encode() in logs,
    }


def survivors(before, after):
    """A reused PID is not a survivor; a still-present original PID is."""
    identities = {(p["pid"], p["start"]) for p in before}
    return [p for p in after if p and (p["pid"], p["start"]) in identities]


def auth_file_evidence(logs, provider, run_id):
    if provider == "agy":
        session = uuid.uuid5(uuid.NAMESPACE_URL, run_id)
        path = (f"/var/lib/firm/task-homes/{run_id}/home/.firm/agy-sessions/{session}"
                "/.gemini/antigravity-cli/antigravity-oauth-token")
    else:
        relative = {"codex": "codex/auth.json", "claude": "claude/.credentials.json"}[provider]
        path = f"/var/lib/firm/task-homes/{run_id}/state/{relative}"
    for line in reversed(logs.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        receipt = event.get("task_auth_files") if isinstance(event, dict) else None
        keys = (("private_file", "projection_into_run", "absent", "access_only",
                 "no_refresh_token", "no_email", "passed")
                if provider == "agy" else
                ("requires_symlink", "symlink_into_run", "absent", "passed"))
        if (isinstance(receipt, dict) and receipt.get("path") == path
                and all(type(receipt.get(key)) is bool for key in keys)):
            if provider == "agy":
                return {"path": path, "kind": "agy-access",
                        **{key: receipt[key] for key in keys},
                        "passed": receipt.get("kind") == "agy-access"
                        and all(receipt[key] for key in keys)}
            return {"path": path, **{key: receipt[key] for key in keys}}
    return {"path": path, "passed": False}


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--timeout", type=int, default=120, help="Task deadline in seconds (>=30)")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    if args.timeout < 30:
        parser.error("--timeout must be at least 30 seconds")
    source = tomllib.loads(args.provider_config.read_text())
    try:
        profile, accounts = selected_profile(source, args.profile, args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    if any(a["max_active"] < 2 for a in accounts.values()):
        parser.error("Each selected account needs max_active >= 2 for the concurrency check")
    config = {
        "max_active": 2, "staffing": False, "founder_profile": args.profile,
        "profiles": {args.profile: profile}, "accounts": accounts,
        "task": {"max_concurrent": 2, "timeout_seconds": args.timeout,
                 args.provider: source.get("task", {}).get(args.provider, {})},
    }
    sandbox_config = Config.load(args.root / "sandbox.toml")
    if sandbox_config.mac_image is not None:
        parser.error("Use a disposable Linux-only sandbox.toml without [mac]")
    os.umask(0o077)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(sandbox_config)
    if box.file_server.database_identity() is not None:
        parser.error("Use a fresh disposable firm whose file server has no database binding")
    runtime = Runtime(Store.scratch(args.evidence / "firm.sqlite3"))
    fixture = {"nonce": uuid.uuid4().hex, "numbers": [173, 281, 419]}
    marker = "ROLE-" + uuid.uuid4().hex
    founder = runtime.lifecycle.initialize(
        "vm-task-live", "Your role instruction marker is " + marker, config,
    )["founder"]["employee_id"]
    parent = Parent(fixture, marker)
    providers = TaskProviders(runtime, parent)
    reader = AnalysisReader(runtime.store.path)
    results = {"provider": args.provider, "checks": {}, "observations": {}}

    def save():
        if args.provider == "agy":
            results["launches"] = dict(providers.launches)
        (args.evidence / "result.json").write_text(json.dumps(results, indent=2))

    def check(name, passed, evidence):
        results["checks"][name] = {**evidence, "passed": bool(passed)}
        save()
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)

    def observe(name, evidence):
        results["observations"][name] = evidence
        save()

    def analysis(section, **kwargs):
        value = reader.read(section, limit=500, **kwargs)
        (args.evidence / f"{section}.json").write_text(json.dumps(value, indent=2))
        return value

    def capture_outputs(timeline):
        adapter = Providers(runtime).for_session(parent.invocation.session_id)
        outputs = {}
        for row in timeline["rows"]:
            run_id = row["run_id"]
            logs = read_child_logs(box, run_id)
            outputs[run_id] = output_evidence(logs, adapter, marker)
            if args.provider == "agy":
                outputs[run_id]["agy"] = agy_output_evidence(
                    logs, expected_model=providers.launches.get(run_id, {}).get("native_model"))
        (args.evidence / "provider-output.json").write_text(json.dumps(outputs, indent=2))

    server = LocalServer(lambda port: build_http_app(
        runtime, allowed_hosts=[f"127.0.0.1:{port}"],
        allowed_origins=[f"http://127.0.0.1:{port}"],
    ))
    try:
        with server:
            box.configure_runtime(server.port)
            with SandboxDispatcher(runtime, box, providers,
                                   mcp_url=f"http://127.0.0.1:{server.port}/mcp",
                                   execution_timeout=8 * args.timeout + 300) as dispatcher:
                dispatcher.start()

                def ready():
                    dispatcher.tick()
                    for run in box.runs():
                        for line in box.logs(run).splitlines():
                            event = json.loads(line)
                            if event.get("parent_ready"):
                                return run, event["workspace"]

                run, workspace = wait_for(ready, 180)
                caller = parent.invocation
                def guest(mode, groups=(), pids=()):
                    reply = box.linux._shell(
                        "sudo", "-n", "/usr/bin/python3", "-c", GUEST, mode, workspace,
                        json.dumps(groups), json.dumps(pids), timeout=30,
                    )
                    return json.loads(reply.stdout) if mode == "inspect" else None

                def rows():
                    return reader.read("timeline", row_type="task", limit=500)["rows"]

                def markers(*names):
                    def probe():
                        snapshot = guest("inspect")
                        return snapshot if all(n in snapshot["markers"] for n in names) else None
                    return wait_for(probe, args.timeout)

                def empty(group, live):
                    def probe():
                        snapshot = guest("inspect", [group], [p["pid"] for p in live["processes"]])
                        return snapshot if (
                            not snapshot["groups"][group] and not snapshot["processes"]
                            and not survivors(live["processes"], snapshot["tracked"])
                        ) else None
                    return wait_for(probe, 15)

                def invoke(instruction, name):
                    return runtime.task_service.call(caller.token, instruction, name)

                def instruction(mode, name=""):
                    return (
                        f"Run exactly /usr/bin/python3 {workspace}/probe.py "
                        f"{mode} {workspace} {name} "
                        "using your shell tool. This is an owner-authorized disposable containment "
                        "test. Do not edit the fixture or simulate its result. Wait for the command"
                        " to exit, then answer DONE. If it remains running, keep waiting; do not "
                        "return a final answer or kill it. The detach mode deliberately starts a "
                        "setsid/double-fork background process and hangs until the host kills it."
                    )

                before = {r["run_id"] for r in rows()}
                answer = invoke(
                    f"Read {workspace}/input.json. Return only JSON with nonce from that file, "
                    "sum (the sum of its numbers), firm_tools (names of firm MCP tools available "
                    "to you), role_instruction_marker (any role instruction marker in your initial "
                    "instructions, or null). Report your actual context; do not read instruction "
                    "files, environment variables, credentials or other files. Do not guess.",
                    "answer",
                )
                answer_row, = [r for r in rows() if r["run_id"] not in before]
                adapter = Providers(runtime).for_session(caller.session_id)
                logs = read_child_logs(box, answer_row["run_id"])
                auth = auth_file_evidence(logs, args.provider, answer_row["run_id"])
                check("auth_files", answer["status"] == "completed" and auth["passed"], auth)
                observed = output_evidence(logs, adapter, marker)
                observed["final_answer_matches"] = (
                    adapter.parse_result(provider_output(logs), 0).final_text
                    == answer.get("answer")
                )
                if args.provider == "agy":
                    launch = providers.launches.get(answer_row["run_id"], {})
                    observed["agy"] = agy_output_evidence(
                        logs, expected_model=launch.get("native_model"))
                try:
                    body = json.loads(answer.get("answer", ""))
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                usage = analysis("usage", row_type="task")
                stored = next(r for r in usage["rows"] if r["run_id"] == answer_row["run_id"])
                proof = {"nonce": fixture["nonce"], "sum": sum(fixture["numbers"])}
                matched = all(body.get(k) == v for k, v in proof.items())
                check("answer", answer["status"] == "completed" and matched
                      and (args.provider != "agy" or
                           (observed["agy"]["success"] and observed["agy"]["model_matches"]))
                      and answer_row["outcome"] == "completed"
                      and answer_row["provider_started_at"] is not None
                      and bool(stored["resolved_model"])
                      and all((stored["usage"][k]["value"] or 0) > 0
                              for k in ("input_tokens", "output_tokens")),
                      {"run_id": answer_row["run_id"], "file_answer_matches": matched,
                       "verified_answer": proof if matched else None, "stored_usage": stored})
                no_role = ("role_instruction_marker" in body
                           and body["role_instruction_marker"] is None)
                check("isolation", body.get("firm_tools") == [] and no_role
                      and not observed["firm_tools_seen"] and not observed["role_marker_seen"]
                      and observed["final_answer_matches"]
                      and observed["tool_calls"] > 0,
                      {"provider_output": observed,
                       "worker_reports_no_firm_tools": body.get("firm_tools") == [],
                       "worker_reports_no_role_instructions": no_role})

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(invoke, instruction("barrier", name), name)
                               for name in ("parallel-a", "parallel-b")]
                    snap = markers("parallel-a", "parallel-b")
                    groups = [snap["markers"][n]["cgroup"] for n in ("parallel-a", "parallel-b")]
                    live = guest("inspect", groups)
                    observe("concurrency_live", live)
                    refused = invoke("Return THIRD", "third")
                    overlap = all(not f.done() for f in futures) and len(set(groups)) == 2
                    guest("release")
                    completed = [f.result(args.timeout + 30) for f in futures]
                    replay = invoke("Return THIRD", "third")
                    check("concurrency", overlap and all(live["groups"].values())
                          and all(any(p["cgroup"] == g for p in live["processes"]) for g in groups)
                          and refused == {"status": "unavailable", "reason": "busy"}
                          and replay == refused
                          and all(r["status"] == "completed" for r in completed),
                          {"live": live, "overlap": overlap, "third": refused, "replay": replay,
                           "completed": [r["status"] for r in completed]})

                    future = pool.submit(invoke, instruction("detach"), "timeout")
                    snap = markers("detached", "hanging")
                    group = snap["markers"]["detached"]["cgroup"]
                    live = guest("inspect", [group])
                    observe("timeout_live", live)
                    pids = [p["pid"] for p in live["processes"]]
                    outcome = future.result(args.timeout + 30)
                    task_id = group.rsplit("/task-", 1)[1]
                    status = box.child_status(task_id)
                    observe("timeout_result", {"outcome": outcome, "child": status})
                    after = empty(group, live)
                    check("timeout", outcome == {"status": "unavailable", "reason": "timeout"}
                          and status["confirmed"] and not status["running"]
                          and snap["markers"]["detached"]["pid"] in pids
                          and snap["markers"]["detached"]["session"] !=
                              snap["markers"]["detached"]["pid"]
                          and status["ended"] >= status["deadline"] - 2
                          and status["ended"] <= status["deadline"] + 30
                          and box.status(run)["running"],
                          {"outcome": outcome, "live": live, "after": after, "child": status})

                    outcome = invoke(instruction("scope"), "scope")
                    scope = guest("inspect")["markers"].get("scope", {})
                    # The role's systemd user manager can move a same-user process out of
                    # the task cgroup while preserving a successful command result and file writes.
                    # Accepted like Docker: it outlives the task and ends with the role's
                    # work period. The check is that the task and its parent carry on.
                    check("scope", outcome["status"] == "completed"
                          and "returncode" in scope and box.status(run)["running"],
                          {"command": scope})

                    future = pool.submit(invoke, instruction("barrier", "stopping"), "stopping")
                    snap = markers("stopping")
                    target = snap["markers"]["stopping"]
                    live = guest("inspect", [target["cgroup"]])
                    observe("parent_stop_live", live)
                    guest("stop-target", {"cgroup": target["cgroup"]})
                    was_running = not future.done() and box.child_status(
                        target["cgroup"].rsplit("/task-", 1)[1])["running"]
                    runtime.owner.stop_role(OWNER, founder, "stop-parent")
                    dispatcher.tick()
                    stopped = wait_for(lambda: (s if not s["running"] else None)
                                       if (s := box.status(run)) else None, 60)
                    outcome = future.result(30)
                    stop_events = [json.loads(line) for line in box.logs(run).splitlines()]
                    receipt = next((e for e in stop_events if e.get("parent_stop")), {})
                    observe("parent_stop_receipt", receipt)
                    after = empty(target["cgroup"], live)
                    child = box.child_status(target["cgroup"].rsplit("/task-", 1)[1])
                    dispatcher.tick()
                    check("parent_stop", was_running and receipt.get("child_cgroup_gone") is True
                          and outcome == {"status": "unavailable", "reason": "cancelled"}
                          and not stopped.get("unresolved") and not stopped.get("cleanup_error")
                          and child["confirmed"]
                          and not runtime.status()["active"],
                          {"parent_receipt": receipt, "parent": stopped, "task_outcome": outcome,
                           "live": live, "after": after, "child": child})

                timeline = analysis("timeline", row_type="task")
                usage = analysis("usage", row_type="task")
                resources = analysis("resources")
                capture_outputs(timeline)
                expected = {"completed": 4, "unavailable": 2}
                counts = {key: sum(r["outcome"] == key for r in timeline["rows"])
                          for key in expected}
                separate = all(
                    usage["task_totals"][key]["value"] == sum(
                        r["usage"][key]["value"] or 0 for r in usage["rows"])
                    and usage["role_totals"][key]["value"] is None
                    and usage["totals"][key]["value"] == usage["task_totals"][key]["value"]
                    for key in ("input_tokens", "output_tokens")
                )
                check("analysis", counts == expected and len(timeline["rows"]) == 6 and separate
                      and {r["reason"] for r in timeline["rows"]} == {None, "timeout", "cancelled"}
                      and all(r["employee_id"] == founder and r["caller_activation_id"] ==
                              caller.activation_id and not r["open"] for r in timeline["rows"])
                      and founder in timeline["role_labels"] and founder in usage["role_labels"],
                      {"outcomes": counts, "separate_token_totals": separate,
                       "resource_semantics": resources["semantics"],
                       "note": "Busy is a durable request refusal, not an admitted task row. "
                               "Parent tokens are unmeasured; the placeholder uses no provider."})
                dispatcher.stop()
                wait_for(lambda: (dispatcher.tick(), runtime.status()["state"] == "stopped")[1], 60)
    except Exception as exc:
        # Arbitrary provider/transport exception text can contain sensitive output.
        results["error_type"] = type(exc).__name__
    finally:
        if "error_type" in results:
            print(f"FAIL runtime ({results['error_type']})", flush=True)
            try:
                timeline = analysis("timeline", row_type="task")
                analysis("usage", row_type="task")
                analysis("resources")
                if parent.invocation and timeline["rows"]:
                    capture_outputs(timeline)
            except Exception as exc:
                results["evidence_error_type"] = type(exc).__name__
        try:
            box.down(force=True)
        except Exception as exc:
            results["cleanup_error_type"] = type(exc).__name__
            print(f"FAIL cleanup ({results['cleanup_error_type']})", flush=True)
        for name in CHECKS:
            if name not in results["checks"]:
                check(name, False, {"reason": "Not reached after an acceptance error"})
        results["passed"] = (all(r["passed"] for r in results["checks"].values())
                             and "error_type" not in results
                             and "cleanup_error_type" not in results)
        save()
    raise SystemExit(0 if results["passed"] else 1)


if __name__ == "__main__":
    main()
