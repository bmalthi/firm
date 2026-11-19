"""Opt-in CLI request checks using dummy keys and loopback fake provider APIs.

Set FIRM_CODEX_TEST_EXECUTABLE / FIRM_CLAUDE_TEST_EXECUTABLE to pinned executables.
No personal configuration, credentials, or external model endpoint is used.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest


@pytest.fixture
def capture_api():
    requests = []
    scenario = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if "messages" in body:
                message = {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }
                events = [
                    {"type": "message_start", "message": message},
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "done"},
                    },
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 1},
                    },
                    {"type": "message_stop"},
                ]
                if scenario.get("read_path") and len(requests) == 1:
                    events = [
                        {"type": "message_start", "message": message},
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "tool_use",
                                "id": "read_fixture",
                                "name": "Read",
                                "input": {},
                            },
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps({"file_path": scenario["read_path"]}),
                            },
                        },
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
            else:
                message = {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "done", "annotations": []}],
                }
                response = {
                    "id": "resp_fixture",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": body["model"],
                    "output": [message],
                    "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
                }
                events = [
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {"type": "response.completed", "response": response},
                ]
            data = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, scenario
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run(command, work, env):
    result = subprocess.run(
        command,
        input="Say done.",
        text=True,
        capture_output=True,
        cwd=work,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("provider,compact", [("codex", False), ("codex", True), ("claude", False)])
def test_pinned_cli_replaces_base_on_resume_and_excludes_project_instructions(
    tmp_path,
    provider,
    compact,
    request,
):
    executable = os.environ.get(f"FIRM_{provider.upper()}_TEST_EXECUTABLE")
    if not executable:
        pytest.skip(f"set FIRM_{provider.upper()}_TEST_EXECUTABLE for loopback CLI validation")
    base_url, requests, scenario = request.getfixturevalue("capture_api")
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    cli_home = tmp_path / "cli-home"
    cli_home.mkdir()
    instructions = tmp_path / "employee-instructions.txt"
    instructions.write_text("FIRST_EMPLOYEE_MARKER. Answer done.")
    instruction_file = "AGENTS.md" if provider == "codex" else "CLAUDE.md"
    (work / instruction_file).write_text("FORBIDDEN_PROJECT_INSTRUCTION")
    (tmp_path / instruction_file).write_text("FORBIDDEN_ANCESTOR_INSTRUCTION")
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if provider == "codex":
        env.update(CODEX_HOME=str(cli_home), DUMMY_API_KEY="loopback-only-dummy-key")
        config = {
            "model": "gpt-6-astra",
            "model_provider": "capture",
            "model_providers.capture.name": "Loopback capture",
            "model_providers.capture.base_url": base_url + "/v1",
            "model_providers.capture.wire_api": "responses",
            "model_providers.capture.env_key": "DUMMY_API_KEY",
            "model_instructions_file": str(instructions),
            "include_collaboration_mode_instructions": False,
            "project_doc_max_bytes": 0,
            "skills.include_instructions": False,
            "project_root_markers": [],
            "features.multi_agent": False,
            "agents.enabled": False,
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
        }
        if compact:
            config["model_auto_compact_token_limit"] = 1
        command = [executable]
        for key, value in config.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        command += ["--cd", str(work), "exec"]
        options = [
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        events = _run(command + options + ["-"], work, env)
        session = next(event["thread_id"] for event in events if event["type"] == "thread.started")
        resumed = command + ["resume"] + options + [session, "-"]
    else:
        nested = work / "child"
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("FORBIDDEN_NESTED_INSTRUCTION")
        (nested / "note.txt").write_text("Harmless work file.")
        scenario["read_path"] = str(nested / "note.txt")
        env.update(
            CLAUDE_CONFIG_DIR=str(cli_home),
            ANTHROPIC_API_KEY="loopback-only-dummy-key",
            ANTHROPIC_BASE_URL=base_url,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1",
            CLAUDE_CODE_MAX_RETRIES="0",
        )
        command = [
            executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "sonnet",
            "--setting-sources",
            "",
            "--settings",
            json.dumps(
                {
                    "autoMemoryEnabled": False,
                    "claudeMdExcludes": ["**"],
                    "crossSessionInbound": "refuse",
                }
            ),
            "--system-prompt-file",
            str(instructions),
            "--system-prompt-snapshot",
            "off",
            "--disable-slash-commands",
            "--tools",
            "Read",
            "--strict-mcp-config",
        ]
        session = str(uuid4())
        _run(command + ["--session-id", session], work, env)
        resumed = command + ["--resume", session]
    initial_requests = 2 if provider == "claude" else 1
    assert len(requests) == initial_requests
    if provider == "claude":
        assert "Harmless work file." in json.dumps(requests[1]["messages"])
    instructions.write_text("SECOND_EMPLOYEE_MARKER. Answer done.")
    _run(resumed, work, env)
    assert len(requests) == initial_requests + (2 if compact else 1)
    if compact:
        records = [
            json.loads(line)
            for path in (cli_home / "sessions").rglob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert any(record.get("type") == "compacted" for record in records)
    for index, payload in enumerate(requests):
        rendered = json.dumps(payload)
        assert ("FIRST_EMPLOYEE_MARKER" in rendered) is (index < initial_requests)
        assert ("SECOND_EMPLOYEE_MARKER" in rendered) is (index >= initial_requests)
        assert "FORBIDDEN_PROJECT_INSTRUCTION" not in rendered
        assert "FORBIDDEN_ANCESTOR_INSTRUCTION" not in rendered
        assert "FORBIDDEN_NESTED_INSTRUCTION" not in rendered
        assert "<collaboration_mode>" not in rendered
        assert "You are Codex" not in rendered
        if provider == "claude":
            # The pinned CLI retains an SDK identity and billing header outside
            # its replaceable body. Do not claim those native layers disappeared.
            expected = "FIRST" if index < initial_requests else "SECOND"
            assert payload["system"][2]["text"] == f"{expected}_EMPLOYEE_MARKER. Answer done."
            assert len(payload["system"]) == 3
            assert "Claude Agent SDK" in payload["system"][1]["text"]
