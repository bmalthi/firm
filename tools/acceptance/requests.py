"""A role asks the owner for a host, the owner decides, and the role resumes, live.

Deterministic programs through the real dispatcher and the authenticated guest MCP, as in
`dispatcher.py`; no model-provider account. The check creates the firm's runtime database
in FIRM_DIR, so the owner's own commands (`firm analyze`, `firm requests`, `firm network`)
read and decide the same record while the firm runs. Run only against a fresh disposable
firm whose file server is unbound and which has no runtime database. Writes `result.json`
with PASS or FAIL per step and each work period's log: mail subjects only, never bodies,
and never credentials.
"""

import argparse
import json
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

# `python -I` leaves this directory off sys.path; the shared helpers live beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from egress.acceptance_common import Report, code, fields, firm, proxy_records  # noqa: E402

HOST = "example.com"
WILDCARD = "*.example.org"
UNDER_WILDCARD = "www.example.org"
REASON = "Acceptance check: this host is declined on purpose."
PERIOD_SECONDS = 300

# Pure parsing the guest program uses on rendered tool results; tested on the host.
PARSERS = r'''
import re


def result_fields(text):
    """A rendered tool result's top-level `Title: value` lines, keyed as the result's fields."""
    values = {}
    for line in text.splitlines():
        title, separator, value = line.partition(": ")
        if separator and title and not title[0].isspace():
            values.setdefault(title.strip().lower().replace(" ", "_"), value.strip())
    return values


def waiting_mail(text):
    """The waiting mail a rendered peek lists: message id, sender and subject, no body."""
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s+(\S+) · (.+?): (.+)$", line)
        if match:
            rows.append({"message_id": match[1], "sender": match[2], "subject": match[3]})
    return rows
'''

# One phase per work period, kept in the role's private home: ask, read the grant and
# ask again, read the denial and ask under it. Every period hands off with next=wait,
# so only the owner's answer wakes the role.
PROGRAM = PARSERS + r'''
import json, os, subprocess, urllib.request
from pathlib import Path

base = os.environ["PROBE_MCP"]
token = os.environ["FIRM_ACTIVATION_TOKEN"]
state = Path(os.environ["PROBE_HOME"]) / "requests-probe-phase"
phase = int(state.read_text()) if state.exists() else 0


def post(path, value):
    request = urllib.request.Request(base + path, data=json.dumps(value).encode(), headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "Origin": base})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def tool(name, arguments):
    reply = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})
    if reply.get("error"):
        return False, json.dumps(reply["error"])[:500]
    result = reply["result"]
    text = "\n".join(item.get("text", "") for item in result.get("content", []))
    return not result.get("isError"), text


def curl(url):
    process = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "http=%{http_code} connect=%{http_connect}",
         "--max-time", "20", url], capture_output=True, text=True)
    return {"exit": process.returncode, "codes": process.stdout.strip()}


def access(name, host):
    ok, text = tool("request_access", {
        "kind": "network_host", "resource": host, "request_id": f"requests-probe-{phase}-{name}",
        "purpose": "Acceptance check of owner requests."})
    return {"ok": True, **result_fields(text)} if ok else {"ok": False, "error": text[:500]}


def inbox():
    ok, text = tool("peek", {})
    rows = waiting_mail(text) if ok else []
    if rows:
        tool("dismiss", {"request_id": f"requests-probe-{phase}-dismiss",
                         "message_ids": [row["message_id"] for row in rows]})
    return rows


post("/activation/start", {})
report = {}
if phase == 0:
    report["host"] = curl("https://example.com")
    report["request"] = access("host", "example.com")
elif phase == 1:
    report["mail"] = inbox()
    report["host"] = curl("https://example.com")
    report["request"] = access("wildcard", "*.example.org")
elif phase == 2:
    report["mail"] = inbox()
    report["under"] = access("under", "www.example.org")
    report["same"] = access("same", "*.example.org")
state.write_text(str(phase + 1))
tool("handoff", {"notes": f"Requests probe phase {phase} finished.", "next": "wait",
                 "request_id": f"requests-probe-handoff-{phase}"})
print(json.dumps({"probe": "complete", "phase": phase, "report": report}), flush=True)
'''

CHECKS = (
    "unlisted_refused_before_grant",
    "request_opened_pending",
    "inbox_lists_request",
    "readiness_owner_request_pending",
    "console_needs_you",
    "requests_lists_pending",
    "grant_command",
    "grant_wakes_role",
    "reachable_after_grant",
    "network_section_shows_grant",
    "second_request_pending",
    "deny_command",
    "deny_wakes_role",
    "denied_host_under_wildcard",
    "denied_same_host",
    "revoke_withdraws_request",
    "runtime_drained",
)


def program_reports(logs):
    """Each work period's report, by phase, from the program's `complete` lines."""
    reports = {}
    for line in logs.splitlines():
        if not line.startswith(b"{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("probe") == "complete":
            reports[row["phase"]] = row["report"]
    return reports


def curl_result(value):
    """The program's curl record as exit and HTTP codes."""
    value = value or {}
    codes = fields(value.get("codes", ""))
    return {"exit": value.get("exit"), "http": code(codes.get("http")),
            "connect": code(codes.get("connect"))}


def has_subject(mail, subject):
    return any(row["subject"] == subject and "Operations" in row["sender"] for row in mail or ())


def refused_with_note(answer):
    return (answer or {}).get("status") == "refused" and answer.get("reason") == "access_denied" \
        and answer.get("owner_note") == REASON


class ProbeProvider:
    def build_spec(self, invocation, role_id, run_id, mcp_url, provider_session_id=None, *,
                   layout=None):
        from firm_sandbox import RunSpec

        return RunSpec(
            role_id,
            (layout.python, layout.credential("probe-program")),
            run_id=run_id,
            env={"PROBE_MCP": mcp_url.removesuffix("/mcp"), "PROBE_HOME": layout.private},
            secrets={
                "activation-token": invocation.token.encode(),
                "probe-program": PROGRAM.encode(),
            },
            secret_env={"FIRM_ACTIVATION_TOKEN": "activation-token"},
        )

    def parse_result(self, logs, exit_code):
        complete = bool(program_reports(logs))
        return SimpleNamespace(
            provider_session_id=None,
            success=exit_code == 0 and complete,
            error=None if complete else SimpleNamespace(kind="requests_probe", category="config"),
        )


class RequestsCheck:
    def __init__(self, args, box, runtime, founder, report):
        from firm_runtime.workspaces import sandbox_role_id

        self.args, self.box, self.runtime, self.report = args, box, runtime, report
        self.founder, self.role_id = founder, sandbox_role_id(founder)
        self.dispatcher = self.console = self.console_app = None

    def firm(self, *arguments):
        return firm(*arguments[:1], self.args.root, *arguments[1:])

    def analyze(self, section):
        """`firm analyze ROOT --section SECTION`; its result, which must have succeeded."""
        command = self.firm("analyze", "--section", section)
        if command["returncode"]:
            raise RuntimeError(f"{command['command']} failed: {command['error']}")
        return command["command"], command["result"]

    def activations(self):
        with self.runtime.store.read() as db:
            return [
                dict(row) for row in db.execute(
                    "SELECT activation_id,state,failure_category,created_at FROM activations "
                    "WHERE employee_id=? ORDER BY created_at,rowid", (self.founder,),
                )
            ]

    def period(self, phase):
        """Tick the dispatcher until work period `phase` settles; its activation and report."""
        deadline = time.monotonic() + PERIOD_SECONDS
        while True:
            self.dispatcher.tick()
            rows = self.activations()
            if len(rows) > phase and all(
                row["state"] not in {"admitted", "running"} for row in rows
            ):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Work period {phase + 1} did not settle: {rows}")
            time.sleep(0.2)
        reports = {}
        for run in self.box.runs():
            if run.role_id == self.role_id:
                logs = self.box.logs(run)
                (self.args.evidence / f"{run.run_id}.log").write_bytes(logs)
                reports.update(program_reports(logs))
        return rows[phase], reports.get(phase)

    def snapshot(self):
        """The owner console's snapshot, read as the browser does after unlocking."""
        port = self.console.port
        headers = {"Host": f"127.0.0.1:{port}"}
        connection = HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request("GET", f"/unlock/{self.console_app.state.console_key}",
                               headers=headers)
            response = connection.getresponse()
            response.read()
            cookie = (response.getheader("set-cookie") or "").split(";", 1)[0]
            connection.request("GET", "/api/snapshot", headers={**headers, "Cookie": cookie})
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"Console snapshot answered {response.status}")
        return json.loads(body)

    def run(self):
        from firm_runtime.console import build_console_app
        from firm_runtime.hosting import LocalServer
        from firm_runtime.mcp_http import build_http_app
        from firm_runtime.sandbox_dispatcher import SandboxDispatcher

        def console_app(port):
            self.console_app = build_console_app(self.runtime, allowed_host=f"127.0.0.1:{port}")
            return self.console_app

        mcp = LocalServer(lambda port: build_http_app(
            self.runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        ))
        with mcp, LocalServer(console_app) as self.console:
            self.box.configure_runtime(mcp.port)
            with SandboxDispatcher(
                self.runtime, self.box, {"probe": ProbeProvider()},
                mcp_url=f"http://127.0.0.1:{mcp.port}/mcp",
            ) as self.dispatcher:
                self.dispatcher.start()
                self.steps()
                self.report.attempt("runtime_drained", self.drain)

    def steps(self):
        report = self.report
        _, first = self.period(0)
        first = first or {}
        before = curl_result(first.get("host"))
        report.check("unlisted_refused_before_grant",
                     before["exit"] not in (0, None) and before["connect"] == 403, before)
        asked = first.get("request") or {}
        request_id = asked.get("request_id")
        report.check("request_opened_pending",
                     asked.get("status") == "pending" and bool(request_id), {"answer": asked})

        def inbox():
            command, result = self.analyze("inbox")
            rows = [row for row in result["pending_requests"] if row["request_id"] == request_id]
            passed = bool(rows) and rows[0]["resource"] == HOST and rows[0]["employee_id"] == (
                self.founder)
            return passed, {"command": command, "pending_requests": result["pending_requests"],
                            "coverage": result["coverage"], "role_labels": result["role_labels"]}

        def readiness():
            command, result = self.analyze("readiness")
            row = next((row for row in result["rows"] if row["employee_id"] == self.founder), {})
            passed = ("owner_request_pending" in row.get("reasons", ())
                      and row.get("actionable_by") == "owner_or_manager")
            return passed, {
                "command": command,
                "role": {key: row.get(key) for key in (
                    "employee_id", "primary_reason", "reasons", "evidence", "actionable_by")},
                "coverage": result["coverage"], "role_labels": result["role_labels"],
            }

        def console():
            needs_you = self.snapshot()["needs_you"]
            ids = [row["request_id"] for row in needs_you["requests"]]
            return request_id in ids, {"needs_you": needs_you}

        def listing():
            command = self.firm("requests")
            pending = (command["result"] or {}).get("pending", [])
            return request_id in [row["request_id"] for row in pending], command

        report.attempt("inbox_lists_request", inbox)
        report.attempt("readiness_owner_request_pending", readiness)
        report.attempt("console_needs_you", console)
        report.attempt("requests_lists_pending", listing)

        granted_at = time.time()
        grant = self.firm("requests", "grant", request_id or "")
        result = grant["result"] or {}
        report.check("grant_command",
                     result.get("status") == "granted" and request_id in result.get("answered", ()),
                     grant)

        activation, second = self.period(1)
        second = second or {}
        report.check(
            "grant_wakes_role",
            activation["created_at"] >= granted_at - 1 and has_subject(
                second.get("mail"), self.subject("access_granted_subject", HOST)),
            {"activation": activation, "mail": second.get("mail")},
        )
        after = curl_result(second.get("host"))
        report.check("reachable_after_grant", after["exit"] == 0 and after["http"] == 200, after)

        def network():
            command, result = self.analyze("network")
            role = next((row for row in result["rows"] if row["employee_id"] == self.founder), {})
            host = next((row for row in role.get("hosts", ()) if row["host"] == HOST), {})
            admitted = proxy_records(
                self.box.linux.network.paths["requests"], since=granted_at, role=self.role_id,
                host=HOST, allowed=True,
            )
            passed = (
                any(row["host"] == HOST for row in result["granted_hosts"])
                and {"host": HOST, "source": "granted"} in result["allowed_hosts"]
                and host.get("requests", 0) >= 2 and host.get("denied", 0) >= 1
                and bool(admitted)
            )
            return passed, {
                "command": command, "granted_hosts": result["granted_hosts"],
                "role_egress": role, "proxy_records_after_grant": len(admitted),
                "coverage": result["coverage"], "role_labels": result["role_labels"],
            }

        report.attempt("network_section_shows_grant", network)
        asked = second.get("request") or {}
        wildcard_id = asked.get("request_id")
        report.check("second_request_pending",
                     asked.get("status") == "pending" and bool(wildcard_id), {"answer": asked})
        deny = self.firm("requests", "deny", wildcard_id or "", "--reason", REASON)
        report.check("deny_command", (deny["result"] or {}).get("status") == "denied", deny)

        activation, third = self.period(2)
        third = third or {}
        report.check(
            "deny_wakes_role",
            has_subject(third.get("mail"), self.subject("access_denied_subject", WILDCARD)),
            {"activation": activation, "mail": third.get("mail")},
        )
        report.check("denied_host_under_wildcard", refused_with_note(third.get("under")),
                     {"host": UNDER_WILDCARD, "answer": third.get("under")})
        report.check("denied_same_host", refused_with_note(third.get("same")),
                     {"host": WILDCARD, "answer": third.get("same")})

        revoke = self.firm("network", "revoke", HOST)
        listing = self.firm("requests")
        recent = {row["request_id"]: row["status"]
                  for row in (listing["result"] or {}).get("recent", ())}
        report.check(
            "revoke_withdraws_request",
            request_id in (revoke["result"] or {}).get("withdrawn", ())
            and recent.get(request_id) == "withdrawn",
            {"revoke": revoke, "recent": recent},
        )

    @staticmethod
    def subject(name, host):
        from firm_runtime.prompt_resources import render

        return render("notices", name, resource=host)

    def drain(self):
        self.dispatcher.stop()
        deadline = time.monotonic() + 60
        while (state := self.runtime.status()["state"]) != "stopped":
            if time.monotonic() > deadline:
                return False, {"state": state}
            self.dispatcher.tick()
            time.sleep(0.2)
        return True, {"state": state}

    def cleanup(self):
        try:
            self.box.down(force=True)
        except Exception as error:
            return [f"stop: {error}"]
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    database = args.root / "durable" / "firm.sqlite3"
    if database.exists():
        parser.error(f"{database} exists; this check creates the runtime database itself and "
                     "needs a fresh disposable firm")
    from firm_runtime.compatibility import validate_firm
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store
    from firm_sandbox import Config, Sandbox

    validate_firm(args.root)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    if box.file_server.database_identity() is not None:
        parser.error(f"{args.root} has already served a runtime database; use a fresh firm")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = Report(args.evidence, CHECKS, root=str(args.root))
    runtime = Runtime(Store(database))
    founder = runtime.lifecycle.initialize(
        "vm-requests",
        "Validate owner requests through the dispatcher.",
        {"max_active": 1, "staffing": False, "founder_profile": "probe",
         "profiles": {"probe": {}}},
    )["founder"]["employee_id"]
    check = RequestsCheck(args, box, runtime, founder, report)
    failure = None
    try:
        check.run()
    except Exception as error:
        failure = error
    finally:
        errors = check.cleanup()
    passed = report.finish(failure, errors)
    print(f"{'PASS' if passed else 'FAIL'} requests acceptance; see {report.path}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
