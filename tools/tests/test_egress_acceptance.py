"""Offline tests for the egress and owner-request acceptance helpers."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"


def load(name, filename):
    # The checks put their own directory on sys.path; keep the test process's path as it was.
    path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(name, ACCEPTANCE / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = path
    return module


common = load("egress_acceptance_common_under_test", "egress/acceptance_common.py")
network = load("network_acceptance", "network.py")
requests = load("requests_acceptance", "requests.py")


def test_report_fails_unreached_checks_and_keeps_skips(tmp_path, capsys):
    report = common.Report(tmp_path, ("one", "two", "three", "four"), root="firm")
    assert report.check("one", True, {"value": 1})
    report.skip("two", "No [mac] is configured")
    assert not report.attempt("three", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not report.finish()
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["root"] == "firm"
    assert saved["checks"]["one"] == {"status": "PASS", "value": 1}
    assert saved["checks"]["two"] == {"status": "SKIPPED", "reason": "No [mac] is configured"}
    assert saved["checks"]["three"] == {"status": "FAIL", "error": "RuntimeError: boom"}
    assert saved["checks"]["four"] == {"status": "FAIL", "reason": "Not reached"}
    assert saved["passed"] is False
    assert "PASS one" in capsys.readouterr().out
    with pytest.raises(KeyError):
        report.check("undeclared", True)


@pytest.mark.parametrize("error,cleanup,passed", [
    (None, (), True), (RuntimeError("stopped"), (), False), (None, ["stop: refused"], False),
])
def test_report_passes_only_without_error_or_cleanup_failure(tmp_path, error, cleanup, passed):
    report = common.Report(tmp_path, ("one", "two"))
    report.check("one", True)
    report.skip("two", "not configured")
    assert report.finish(error, cleanup) is passed
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["passed"] is passed
    assert ("error" in saved) is (error is not None)
    assert saved.get("cleanup_errors", []) == list(cleanup)


def test_parse_probes_reads_exit_seconds_and_codes():
    output = "\n".join([
        "Unable to find image 'curlimages/curl:8.10.1' locally",
        "PROBE allowed exit=0 start=100.250000 end=101.000000 http=200 connect=000",
        "PROBE unlisted exit=56 start=5,5 end=6,0 http=000 connect=403",
        "PROBE dns_getent exit=2 start=1.0 end=1.004 ",
        "PROBE broken exit=x start=1 end=2",
        "curl: (56) CONNECT tunnel failed, response 403",
    ])
    assert common.parse_probes(output) == {
        "allowed": {"exit": 0, "seconds": 0.75, "http": 200, "connect": None},
        "unlisted": {"exit": 56, "seconds": 0.5, "http": None, "connect": 403},
        "dns_getent": {"exit": 2, "seconds": 0.004},
    }


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_role_probe_script_prints_what_parse_probes_reads():
    script = network.script([
        ("ok", ("printf", "http=200 connect=000")),
        ("noise", ("printf", "203.0.113.1 example.com")),
        ("fails", ("sh", "-c", "exit 7")),
    ])
    output = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    results = common.parse_probes(output)
    assert results["ok"]["exit"] == 0 and results["ok"]["http"] == 200
    assert results["noise"]["exit"] == 0 and "http" not in results["noise"]
    assert results["fails"]["exit"] == 7
    assert all(row["seconds"] >= 0 for row in results.values())


def test_probe_commands_keep_the_proxy_out_only_where_asked():
    assert network.container(network.curl("https://example.com"))[:4] == (
        "docker", "run", "--rm", network.CURL_IMAGE,
    )
    assert network.container(network.curl("https://example.com"))[4] == "-sS"
    command = network.unproxied(network.curl("http://example.com"))
    for variable in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert variable in command
    assert network.covered("example.com", ["*.example.com", "example.com"])
    assert network.covered("api.example.com", ["*.example.com"])
    assert not network.covered("example.com", ["*.example.com", "github.com"])
    assert network.proxy_refused({"exit": 56, "connect": 403})
    assert not network.proxy_refused({"exit": 0, "connect": 403})
    assert network.reached({"exit": 0, "http": 200})


def test_tunnel_events_and_program():
    compile(network.TUNNEL_PROGRAM, "tunnel-program", "exec")
    assert "print(proxy" not in network.TUNNEL_PROGRAM and "secret)" not in network.TUNNEL_PROGRAM
    output = "\n".join([
        json.dumps({"event": "held", "hosts": ["example.com", "api.github.com"]}),
        "Traceback (most recent call last):",
        json.dumps({"event": "closed", "host": "example.com"}),
        json.dumps(["not", "an", "event"]),
    ])
    assert [row["event"] for row in network.tunnel_events(output)] == ["held", "closed"]


def verdicts(closed_seen, open_at_revoke=("example.com", "api.github.com"), usable=True):
    events = [{"event": "usable", "host": "api.github.com", "status_line": "HTTP/1.1 200 OK"}]
    return network.tunnel_verdicts(
        events if usable else [], closed_seen, set(open_at_revoke), 100.0, 101.0,
        "example.com", "api.github.com",
    )


def test_tunnel_verdicts_require_a_prompt_close_and_a_surviving_other_tunnel():
    (closes, closed), (keeps, kept) = verdicts({"example.com": 103.5})
    assert closes and closed["seconds_after_revoke"] == 2.5
    assert closed["revoke_command_seconds"] == 1.0
    assert keeps and kept["status_line_after"] == "HTTP/1.1 200 OK"
    # Closed while the revoke command still ran counts as zero seconds.
    (closes, closed), _ = verdicts({"example.com": 100.5})
    assert closes and closed["seconds_after_revoke"] == 0.0
    # Too slow, never closed, or closed before the revoke began (an upstream timeout).
    assert not verdicts({"example.com": 106.5})[0][0]
    assert not verdicts({})[0][0]
    assert not verdicts({"example.com": 99.0})[0][0]
    assert not verdicts({"example.com": 103.0}, open_at_revoke=("api.github.com",))[0][0]
    # The baseline tunnel must stay open and still answer.
    assert not verdicts({"example.com": 103.0, "api.github.com": 104.0})[1][0]
    assert not verdicts({"example.com": 103.0}, usable=False)[1][0]


def test_proxy_records_read_both_files_and_filter(tmp_path):
    log = tmp_path / "egress.jsonl"
    rows = [
        {"time": "2026-10-26T10:00:00+00:00", "role": "alpha", "host": "example.com",
         "port": 443, "allowed": False, "reason": "not_allowed"},
        {"time": "2026-10-26T10:00:05+00:00", "role": "alpha", "host": "api.github.com",
         "port": 443, "allowed": True, "reason": None},
        {"time": "2026-10-26T10:00:09+00:00", "role": "beta", "host": "example.com",
         "port": 443, "allowed": False, "reason": "not_allowed"},
    ]
    (tmp_path / "egress.jsonl.1").write_text(json.dumps(rows[0]) + "\n")
    log.write_text("\n".join([json.dumps(rows[2]), '{"time": "cut', json.dumps(rows[1])]) + "\n")
    every = common.proxy_records(log)
    assert [row["host"] for row in every] == ["example.com", "api.github.com", "example.com"]
    since = every[1]["at"]
    assert [row["role"] for row in common.proxy_records(log, since=since)] == ["alpha", "beta"]
    denied = common.proxy_records(log, role="alpha", host="example.com", allowed=False)
    assert len(denied) == 1 and denied[0]["reason"] == "not_allowed"
    assert common.proxy_records(tmp_path / "missing.jsonl") == []


def test_clock_drift_and_destination_bytes():
    assert common.clock_drift(100.0, 103.5, 101.0) == {
        "seconds": 3.0, "uncertainty_seconds": 0.5,
    }
    traffic = {"destinations": [
        {"address": "1.1.1.1", "port": 443, "protocol": "tcp", "allowed_bytes": 0,
         "blocked_bytes": 300},
        {"address": "1.1.1.1", "port": 53, "protocol": "udp", "allowed_bytes": 0,
         "blocked_bytes": 80},
        {"address": "9.9.9.9", "port": 443, "protocol": "tcp", "allowed_bytes": 10,
         "blocked_bytes": 0},
    ]}
    assert common.destination_bytes(traffic, "1.1.1.1", 443)["blocked_bytes"] == 300
    assert common.destination_bytes(traffic, "1.1.1.1", 53, "udp")["blocked_bytes"] == 80
    assert common.destination_bytes(traffic, "8.8.8.8", 443) == {
        "address": "8.8.8.8", "port": 443, "protocol": "tcp", "allowed_bytes": 0,
        "blocked_bytes": 0,
    }


def test_scutil_proxy_reads_top_level_settings_only():
    text = """<dictionary> {
  ExceptionsList : <array> {
    0 : localhost
    1 : 127.0.0.1
  }
  FTPPassive : 1
  HTTPEnable : 1
  HTTPPort : 23456
  HTTPProxy : 192.168.6.2
  HTTPSEnable : 1
  HTTPSPort : 23456
  HTTPSProxy : 192.168.6.2
}
"""
    settings = common.scutil_proxy(text)
    assert settings["HTTPSProxy"] == "192.168.6.2" and settings["HTTPSPort"] == "23456"
    assert settings["HTTPEnable"] == "1"
    assert "0" not in settings and "ExceptionsList" not in settings


def test_firm_runs_the_owner_cli_and_keeps_its_json_or_error(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "fail":
            return SimpleNamespace(returncode=2, stdout="", stderr="firm: unknown request\n")
        return SimpleNamespace(returncode=0, stdout='{"status": "granted"}', stderr="")

    monkeypatch.setattr(common.subprocess, "run", run)
    granted = common.firm("requests", Path("/firms/one"), "grant", "q1")
    assert calls[0] == [sys.executable, "-m", "firm_runtime.cli", "requests", "/firms/one",
                        "grant", "q1"]
    assert granted == {"command": "firm requests /firms/one grant q1", "returncode": 0,
                       "result": {"status": "granted"}, "error": None}
    failed = common.firm("requests", "/firms/one", "grant", "fail")
    assert failed["result"] is None and failed["error"] == "firm: unknown request"


def test_guest_parsers_read_rendered_tool_results():
    namespace = {}
    exec(requests.PARSERS, namespace)
    refused = ("Status: refused\n\nReason: access_denied\n\nResource: *.example.org\n\n"
               "Owner note: Acceptance check: this host is declined on purpose.")
    assert namespace["result_fields"](refused) == {
        "status": "refused", "reason": "access_denied", "resource": "*.example.org",
        "owner_note": "Acceptance check: this host is declined on purpose.",
    }
    assert namespace["result_fields"]("Status: pending\n\nRequest id: q1") == {
        "status": "pending", "request_id": "q1",
    }
    inbox = ("Inbox: 1 pending message.\n- c7 · with Operations · 1 waiting\n"
             "  m12 · Operations: Access granted: example.com")
    assert namespace["waiting_mail"](inbox) == [
        {"message_id": "m12", "sender": "Operations", "subject": "Access granted: example.com"},
    ]


def test_request_report_helpers():
    logs = b"\n".join([
        b"not json",
        json.dumps({"probe": "complete", "phase": 0, "report": {"request": {}}}).encode(),
        b"{broken",
        json.dumps({"probe": "complete", "phase": 1, "report": {"mail": []}}).encode(),
    ])
    assert requests.program_reports(logs) == {0: {"request": {}}, 1: {"mail": []}}
    assert requests.curl_result({"exit": 56, "codes": "http=000 connect=403"}) == {
        "exit": 56, "http": None, "connect": 403,
    }
    assert requests.curl_result(None) == {"exit": None, "http": None, "connect": None}
    mail = [{"message_id": "m1", "sender": "Operations", "subject": "Access granted: example.com"}]
    assert requests.has_subject(mail, "Access granted: example.com")
    assert not requests.has_subject(mail, "Access declined: example.com")
    assert requests.refused_with_note({"status": "refused", "reason": "access_denied",
                                       "owner_note": requests.REASON})
    assert not requests.refused_with_note({"status": "pending", "request_id": "q3"})
    assert not requests.refused_with_note(None)


def test_probe_provider_runs_the_program_with_the_activation_token_as_a_secret():
    layout = SimpleNamespace(python="/usr/bin/python3", private="/home/probe",
                             credential=lambda name: f"/run/credentials/{name}")
    spec = requests.ProbeProvider().build_spec(
        SimpleNamespace(token="activation-secret"), "founder", "run-1",
        "http://127.0.0.1:9000/mcp", layout=layout,
    )
    assert spec.argv == ("/usr/bin/python3", "/run/credentials/probe-program")
    assert spec.env == {"PROBE_MCP": "http://127.0.0.1:9000", "PROBE_HOME": "/home/probe"}
    assert "activation-secret" not in json.dumps(spec.env)
    assert spec.secrets["probe-program"] == requests.PROGRAM.encode()
    compile(requests.PROGRAM, "probe-program", "exec")
