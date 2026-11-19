"""Tests for host-brokered guest LLM CLI shims (`claude`, `codex`, `agy`)."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_agy_credentials import account as agy_account

from firm_runtime import credential_mirror, llm_cli
from firm_runtime.mcp_http import build_http_app
from firm_runtime.prompt_resources import prompt
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store


def _write_fake_exe(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


class _LoopbackBroker:
    """Thread-safe HTTP loopback frontend forwarding requests into build_http_app."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self._ready_q: queue.Queue[BaseException | None] = queue.Queue()
        self._stop_q: queue.Queue[None] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._loop_thread: threading.Thread | None = None
        self._http_thread: threading.Thread | None = None
        self.llm_url = ""

    def __enter__(self):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _forward(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() != "content-length"
                }

                async def call():
                    assert broker._client is not None
                    return await broker._client.request(
                        method, self.path, content=body, headers=headers
                    )

                assert broker._loop is not None
                future = asyncio.run_coroutine_threadsafe(call(), broker._loop)
                resp = future.result(timeout=20)
                self.send_response(resp.status_code)
                for key, val in resp.headers.items():
                    if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                        continue
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

        self._http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._http_server.server_address[1]
        app = build_http_app(
            self.runtime,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )

        def run_loop():
            async def runner():
                self._loop = asyncio.get_running_loop()
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url=f"http://127.0.0.1:{port}",
                    ) as client:
                        self._client = client
                        self._ready_q.put(None)
                        while True:
                            try:
                                self._stop_q.get_nowait()
                                break
                            except queue.Empty:
                                await asyncio.sleep(0.05)

            try:
                asyncio.run(runner())
            except BaseException as exc:
                self._ready_q.put(exc)

        self._loop_thread = threading.Thread(target=run_loop, daemon=True)
        self._loop_thread.start()
        err = self._ready_q.get(timeout=5)
        if err is not None:
            raise err

        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        self.llm_url = f"http://127.0.0.1:{port}/llm"
        return self

    def __exit__(self, *_args):
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        self._stop_q.put(None)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)


def _setup_runtime(tmp_path: Path, *, max_active: int = 2, llm_config: dict | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    claude_auth = tmp_path / "claude-auth"
    claude_auth.write_text("claude-oauth-token", encoding="utf-8")
    claude_auth.chmod(0o600)

    codex_auth = tmp_path / "codex-auth.json"
    codex_auth.write_text('{"tokens":{"access_token":"codex-access"}}', encoding="utf-8")
    codex_auth.chmod(0o600)

    agy_auth = agy_account(tmp_path, expiry=4102444800)

    config: dict = {
        "max_active": 4,
        "staffing": False,
        "founder_profile": "main",
        "profiles": {
            "main": {
                "provider": "claude",
                "model": "sonnet",
                "effort": "medium",
                "accounts": ["claude_main"],
            },
        },
        "accounts": {
            "claude_main": {
                "provider": "claude",
                "auth_file": str(claude_auth),
                "max_active": max_active,
            },
            "codex_main": {
                "provider": "codex",
                "auth_file": str(codex_auth),
                "max_active": max_active,
            },
            "agy_main": {
                "provider": "agy",
                "auth_file": str(agy_auth),
                "max_active": max_active,
            },
        },
    }
    if llm_config is not None:
        config["llm"] = llm_config

    store = Store.scratch(tmp_path / "firm.sqlite3")
    runtime = Runtime(store)
    runtime.lifecycle.initialize("test", "Build reliable software.", config)
    runtime.start()
    founder = runtime.admit()
    assert founder is not None
    runtime.mark_started(founder.activation_id)
    return runtime, founder


def _prepare_guest(
    tmp_path: Path,
    activation_token: str,
    llm_url: str,
    *,
    fake_bin: Path,
    base_env: dict[str, str] | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "guest-state"
    creds_dir = tmp_path / "guest-creds"
    private_home = tmp_path / "guest-private"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    creds_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_home.mkdir(mode=0o700, parents=True, exist_ok=True)

    shutil.copy(llm_cli.__file__, creds_dir / "llm-cli")
    shutil.copy(credential_mirror.__file__, creds_dir / "credential-mirror")
    (creds_dir / "llm-instructions").write_text(prompt("runtime_llm.md") + "\n", encoding="utf-8")
    (creds_dir / "activation-token").write_text(activation_token + "\n", encoding="utf-8")

    # Install shims while temporarily restoring caller PATH afterwards.
    original_path = os.environ.get("PATH")
    try:
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.path.dirname(sys.executable)}"
        bin_dir = llm_cli.install_shims(state_dir, creds_dir, private_home, llm_url)
        guest_path = os.environ["PATH"]
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path

    role_home = tmp_path / "role-home"
    role_home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": guest_path,
        "HOME": str(role_home),
        "USER": "role",
        "LOGNAME": "role",
        "LANG": "C.UTF-8",
        # Scrubbing targets that must NOT survive into real CLI child processes:
        "FIRM_ACTIVATION_TOKEN": activation_token,
        "FIRM_GITHUB_URL": "http://127.0.0.1:9999/github/token",
        "GH_TOKEN": "ghs_secret_should_be_scrubbed",
        "GITHUB_TOKEN": "ghs_secret_should_be_scrubbed_too",
        "ANTHROPIC_API_KEY": "sk-ant-should-be-scrubbed",
        "OPENAI_API_KEY": "sk-openai-should-be-scrubbed",
    }
    if base_env:
        env.update(base_env)
    return state_dir, creds_dir, private_home, bin_dir, env


def _run_shim(bin_dir: Path, provider: str, args: list[str], env: dict[str, str], **kwargs):
    return subprocess.run(
        [str(bin_dir / provider), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        **kwargs,
    )


def test_install_shims_layout_permissions_and_symlink_guards(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    creds_dir = tmp_path / "creds"
    private_home = tmp_path / "private"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    bin_dir = llm_cli.install_shims(state_dir, creds_dir, private_home, "http://127.0.0.1:8765/llm/")
    assert bin_dir == state_dir / "bin"
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o700

    cfg_path = state_dir / "llm-config.json"
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg == {
        "llm_url": "http://127.0.0.1:8765/llm",
        "credentials_url": "http://127.0.0.1:8765/credentials",
        "credentials_dir": str(creds_dir),
        "state_dir": str(state_dir),
        "private_home": str(private_home),
    }

    for provider in ("claude", "codex", "agy"):
        shim = bin_dir / provider
        assert stat.S_IMODE(shim.stat().st_mode) == 0o700
        text = shim.read_text(encoding="utf-8")
        assert f"--config '{cfg_path}' {provider} \"$@\"" in text or (
            f"--config {cfg_path} {provider} \"$@\"" in text
        )

    assert os.environ["PATH"] == f"{bin_dir}:/usr/bin:/bin"
    # Second install does not duplicate bin_dir on PATH.
    llm_cli.install_shims(state_dir, creds_dir, private_home, "http://127.0.0.1:8765/llm")
    assert os.environ["PATH"].split(os.pathsep).count(str(bin_dir)) == 1

    # Reject relative paths and symlinks.
    with pytest.raises(ValueError, match="absolute"):
        llm_cli.install_shims("rel/state", creds_dir, private_home, "http://127.0.0.1:8765/llm")
    with pytest.raises(ValueError, match="end in /llm"):
        llm_cli.install_shims(state_dir, creds_dir, private_home, "http://127.0.0.1:8765/other")

    symlink_target = tmp_path / "target-file"
    symlink_target.write_text("x", encoding="utf-8")
    (bin_dir / "claude").unlink()
    (bin_dir / "claude").symlink_to(symlink_target)
    with pytest.raises(ValueError, match="regular file"):
        llm_cli.install_shims(state_dir, creds_dir, private_home, "http://127.0.0.1:8765/llm")


def test_local_guards_help_version_and_validation(tmp_path):
    fake_bin = tmp_path / "real-bin"
    fake_bin.mkdir()
    for cli in ("claude", "codex", "agy"):
        _write_fake_exe(
            fake_bin,
            cli,
            (
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], "
                "'firm_vars': sorted(k for k in os.environ if k.startswith('FIRM_')), "
                "'gh': os.environ.get('GH_TOKEN')}))\n"
            ),
        )

    _state_dir, creds_dir, _private_home, bin_dir, env = _prepare_guest(
        tmp_path,
        "missing-token-ignored-for-help",
        "http://127.0.0.1:9/llm",
        fake_bin=fake_bin,
    )
    # Remove activation-token to verify --help / --version do not hit broker or credentials.
    (creds_dir / "activation-token").unlink()

    for provider, flag in (
        ("claude", "--help"),
        ("claude", "--version"),
        ("codex", "help"),
        ("agy", "-V"),
    ):
        res = _run_shim(bin_dir, provider, [flag], env)
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["argv"] == [flag]
        assert payload["firm_vars"] == []
        assert payload["gh"] is None

    # Nested child guard.
    nested = _run_shim(bin_dir, "claude", ["-p", "hello"], {**env, "FIRM_LLM_CHILD": "1"})
    assert nested.returncode == 2
    assert "nested LLM CLI calls are disabled" in nested.stderr

    # Model override rejection across providers.
    for provider, bad_args in (
        ("claude", ["-p", "--model", "haiku", "hi"]),
        ("claude", ["-p", "-m", "opus", "hi"]),
        ("claude", ["-p", "--model=sonnet", "hi"]),
        ("codex", ["exec", "--model", "gpt-5", "hi"]),
        ("codex", ["exec", '-c', 'model="o3"', "hi"]),
        ("agy", ["-p", "--model=gemini-4-hydrogen-low", "hi"]),
    ):
        res = _run_shim(bin_dir, provider, bad_args, env)
        assert res.returncode == 2, (provider, bad_args)
        assert "model selection is managed by the firm" in res.stderr

    # MCP override rejection.
    for provider, bad_args in (
        ("claude", ["-p", "--mcp-config", "mcp.json", "hi"]),
        ("claude", ["-p", "--strict-mcp-config", "hi"]),
        ("codex", ["exec", "-c", "mcp_servers={}", "hi"]),
        ("codex", ["exec", "-c", "mcp_servers.foo.command=true", "hi"]),
        ("agy", ["-p", "--mcp-config=foo.json", "hi"]),
    ):
        res = _run_shim(bin_dir, provider, bad_args, env)
        assert res.returncode == 2, (provider, bad_args)
        assert "MCP configuration is managed by the firm" in res.stderr

    # Interactive mode rejection.
    for provider, bad_args in (
        ("claude", ["hello"]),
        ("claude", []),
        ("codex", []),
        ("codex", ["chat", "hello"]),
        ("agy", ["hello"]),
    ):
        res = _run_shim(bin_dir, provider, bad_args, env)
        assert res.returncode == 2, (provider, bad_args)
        assert "interactive mode is unavailable" in res.stderr

    # Unknown agy session refused before hitting broker.
    unknown_agy = _run_shim(bin_dir, "agy", ["-p", "--resume", "missing-conv-1", "hi"], env)
    assert unknown_agy.returncode == 2
    assert "unknown agy session" in unknown_agy.stderr

    assert llm_cli._agy_native_model("neon", "high") == "gemini-4-neon-high"
    assert llm_cli._agy_native_model("gemini-4-hydrogen", "xhigh") == "gemini-4-carbon-high"
    assert llm_cli._agy_native_model("high", "high") == "gemini-4-neon-high"
    assert llm_cli._agy_native_model("carbon", "medium") == "gemini-4-carbon-medium"

    # Codex exec -C /dir resume SESSION_ID puts -C in pre_exec and detects resumed=True.
    parsed = llm_cli._validate_and_parse(
        "codex", ["exec", "-C", "/tmp/work", "resume", "sess-1", "continue"]
    )
    assert isinstance(parsed, dict)
    assert parsed["resumed"] is True
    assert parsed["pre_exec"] == ["-C", "/tmp/work"]
    assert parsed["post_exec"] == ["sess-1", "continue"]


def test_end_to_end_claude_codex_agy_execution_resume_and_cleanup(tmp_path):
    fake_bin = tmp_path / "real-bin"
    fake_bin.mkdir()

    claude_record = tmp_path / "claude-seen.json"
    _write_fake_exe(
        fake_bin,
        "claude",
        f"""
import json, os, sys
from pathlib import Path
rec_path = Path({json.dumps(str(claude_record))})
seen = json.loads(rec_path.read_text()) if rec_path.exists() else []
cfg_dir = Path(os.environ["CLAUDE_CONFIG_DIR"])
assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "claude-oauth-token"
assert not (cfg_dir / ".credentials.json").exists()
instr_idx = sys.argv.index("--system-prompt-file") + 1
instructions = Path(sys.argv[instr_idx]).read_text()
seen.append({{
    "argv": sys.argv[1:],
    "env_scrubbed": {{
        "FIRM_LLM_CHILD": os.environ.get("FIRM_LLM_CHILD"),
        "FIRM_ACTIVATION_TOKEN": os.environ.get("FIRM_ACTIVATION_TOKEN"),
        "FIRM_GITHUB_URL": os.environ.get("FIRM_GITHUB_URL"),
        "GH_TOKEN": os.environ.get("GH_TOKEN"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
    }},
    "instructions": instructions,
    "projects_is_symlink": (cfg_dir / "projects").is_symlink(),
}})
rec_path.write_text(json.dumps(seen))
(cfg_dir / "projects" / "turn.txt").write_text("turn-" + str(len(seen)))
sess = "11111111-1111-4111-8111-111111111111"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sess}}))
print(json.dumps({{
    "type": "assistant",
    "message": {{
        "id": "m1", "model": "claude-opus", "usage": {{"input_tokens": 11, "output_tokens": 5}}
    }},
}}))
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": sess,
    "result": f"claude answer {{len(seen)}}",
    "usage": {{"input_tokens": 11, "output_tokens": 5}},
}}))
""",
    )

    codex_record = tmp_path / "codex-seen.json"
    _write_fake_exe(
        fake_bin,
        "codex",
        f"""
import json, os, sys
from pathlib import Path
rec_path = Path({json.dumps(str(codex_record))})
seen = json.loads(rec_path.read_text()) if rec_path.exists() else []
codex_home = Path(os.environ["CODEX_HOME"])
auth_data = json.loads((codex_home / "auth.json").read_text())
assert auth_data["tokens"]["access_token"] == "codex-access"
instr_arg = [a for a in sys.argv if a.startswith("model_instructions_file=")][0]
instr_path = json.loads(instr_arg.split("=", 1)[1])
instructions = Path(instr_path).read_text()
seen.append({{
    "argv": sys.argv[1:],
    "env_scrubbed": {{
        "FIRM_LLM_CHILD": os.environ.get("FIRM_LLM_CHILD"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "GH_TOKEN": os.environ.get("GH_TOKEN"),
    }},
    "instructions": instructions,
    "sessions_is_symlink": (codex_home / "sessions").is_symlink(),
    "images_is_symlink": (codex_home / "generated_images").is_symlink(),
}})
rec_path.write_text(json.dumps(seen))
rollout = codex_home / "sessions" / "rollout-1.jsonl"
n = len(seen)
with rollout.open("a") as fh:
    fh.write(json.dumps({{"type": "turn_context", "payload": {{"model": "gpt-6.1-sol"}}}}) + "\\n")
    fh.write(json.dumps({{
        "type": "event_msg",
        "payload": {{
            "type": "token_count",
            "info": {{
                "last_token_usage": {{
                    "input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 9
                }},
                "total_token_usage": {{
                    "input_tokens": 20 * n, "cached_input_tokens": 4 * n, "output_tokens": 9 * n
                }},
            }},
        }},
    }}) + "\\n")
tid = "22222222-2222-4222-8222-222222222222"
print(json.dumps({{"type": "thread.started", "thread_id": tid}}))
print(json.dumps({{
    "type": "item.completed",
    "item": {{"id": "i1", "type": "agent_message", "text": f"codex answer {{n}}"}},
}}))
print(json.dumps({{
    "type": "turn.completed",
    "usage": {{"input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 9}},
}}))
""",
    )

    agy_record = tmp_path / "agy-seen.json"
    _write_fake_exe(
        fake_bin,
        "agy",
        f"""
import json, os, sys
from pathlib import Path
rec_path = Path({json.dumps(str(agy_record))})
seen = json.loads(rec_path.read_text()) if rec_path.exists() else []
home = Path(os.environ["HOME"])
auth_data = json.loads((home / ".gemini/antigravity-cli/antigravity-oauth-token").read_text())
assert auth_data["token"]["access_token"] == "synthetic-access"
settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
agent_idx = sys.argv.index("--agent") + 1
agent_name = sys.argv[agent_idx]
agent_md = (home / ".gemini/config/agents" / f"{{agent_name}}.md").read_text()
stdin_line = json.loads(sys.stdin.readline())
seen.append({{
    "argv": sys.argv[1:],
    "stdin": stdin_line,
    "settings": settings,
    "agent_md": agent_md,
    "env_keys": sorted(os.environ.keys()),
    "conversations_is_symlink": (home / ".gemini/antigravity-cli/conversations").is_symlink(),
    "brain_is_symlink": (home / ".gemini/antigravity-cli/brain").is_symlink(),
}})
rec_path.write_text(json.dumps(seen))
conv_id = "agy-conv-1"
(home / ".gemini/antigravity-cli/conversations" / f"{{conv_id}}.db").write_text("sqlite-bytes")
print(json.dumps({{
    "event": "init", "conversation_id": conv_id, "init": {{"model": "gemini-4-hydrogen-high"}}
}}))
print(json.dumps({{
    "event": "step_update",
    "step_update": {{"conversation_id": conv_id, "step_index": 0, "step_type": "agent_response"}},
}}))
print(json.dumps({{
    "event": "result",
    "result": {{
        "conversation_id": conv_id,
        "status": "SUCCESS",
        "response": f"agy answer {{len(seen)}}",
        "usage": {{"input_tokens": 14, "output_tokens": 6}},
    }},
}}))
""",
    )

    runtime, founder = _setup_runtime(tmp_path, max_active=2)
    with _LoopbackBroker(runtime) as broker:
        # Deliver owner mail so finished calls include a controls_notice on stderr.
        runtime.owner.send_mail(OWNER, [founder.employee_id], "Please check mail.", request_id="m1")
        state_dir, _creds_dir, private_home, bin_dir, env = _prepare_guest(
            tmp_path, founder.token, broker.llm_url, fake_bin=fake_bin
        )

        # 1) Claude fresh + resume + --tools "" + --system-prompt
        r1 = _run_shim(
            bin_dir,
            "claude",
            ["-p", "--effort", "high", "--system-prompt", "Be concise.", "Inspect repo"],
            env,
        )
        assert r1.returncode == 0, r1.stderr
        assert r1.stdout == "claude answer 1\n"
        assert "firm: session 11111111-1111-4111-8111-111111111111" in r1.stderr
        assert "New mail:" in r1.stderr
        assert list((state_dir / "llm").iterdir()) == []

        r2 = _run_shim(
            bin_dir,
            "claude",
            ["-p", "--tools", "", "--resume", "11111111-1111-4111-8111-111111111111", "More"],
            env,
        )
        assert r2.returncode == 0, r2.stderr
        assert r2.stdout == "claude answer 2\n"
        turn_file = private_home / ".firm" / "cli" / "claude" / "projects" / "turn.txt"
        assert turn_file.read_text() == "turn-2"

        claude_calls = json.loads(claude_record.read_text())
        assert claude_calls[0]["env_scrubbed"] == {
            "FIRM_LLM_CHILD": "1",
            "FIRM_ACTIVATION_TOKEN": None,
            "FIRM_GITHUB_URL": None,
            "GH_TOKEN": None,
            "ANTHROPIC_API_KEY": None,
        }
        assert "--effort" in claude_calls[0]["argv"]
        assert claude_calls[0]["argv"][claude_calls[0]["argv"].index("--effort") + 1] == "high"
        assert "Be concise." in claude_calls[0]["instructions"]
        assert claude_calls[0]["instructions"].startswith(prompt("runtime_llm.md"))
        tools_idx = claude_calls[1]["argv"].index("--tools")
        assert claude_calls[1]["argv"][tools_idx + 1] == ""
        assert "--resume" in claude_calls[1]["argv"]

        # 2) Codex fresh + --no-tools + resume (reads incremental rollout telemetry)
        c1 = _run_shim(bin_dir, "codex", ["exec", "--effort", "high", "Summarize git log"], env)
        assert c1.returncode == 0, c1.stderr
        assert c1.stdout == "codex answer 1\n"
        assert "firm: session 22222222-2222-4222-8222-222222222222" in c1.stderr
        assert list((state_dir / "llm").iterdir()) == []

        c2 = _run_shim(
            bin_dir,
            "codex",
            [
                "exec",
                "-C",
                str(tmp_path),
                "resume",
                "--no-tools",
                "22222222-2222-4222-8222-222222222222",
                "And tests",
            ],
            env,
        )
        assert c2.returncode == 0, c2.stderr
        assert c2.stdout == "codex answer 2\n"
        assert (private_home / ".firm" / "cli" / "codex" / "sessions" / "rollout-1.jsonl").exists()

        codex_calls = json.loads(codex_record.read_text())
        assert codex_calls[0]["env_scrubbed"] == {
            "FIRM_LLM_CHILD": "1",
            "OPENAI_API_KEY": None,
            "GH_TOKEN": None,
        }
        assert codex_calls[0]["sessions_is_symlink"] is True
        assert codex_calls[0]["images_is_symlink"] is True
        assert (private_home / ".firm" / "cli" / "codex" / "generated_images").is_dir()
        assert "features.image_generation=true" in codex_calls[0]["argv"]
        assert "--dangerously-bypass-approvals-and-sandbox" in codex_calls[0]["argv"]
        assert 'model_reasoning_effort="high"' in codex_calls[0]["argv"]
        assert "-C" in codex_calls[1]["argv"]
        assert codex_calls[1]["argv"].index("-C") < codex_calls[1]["argv"].index("exec")
        assert "resume" in codex_calls[1]["argv"]
        assert "features.shell_tool=false" in codex_calls[1]["argv"]
        assert "features.image_generation=false" in codex_calls[1]["argv"]
        assert "--dangerously-bypass-approvals-and-sandbox" not in codex_calls[1]["argv"]

        # Verify resumed codex run recorded last_token_usage (20, not cumulative 40).
        with runtime.store.read() as db:
            codex_rows = db.execute(
                "SELECT input_tokens,cached_input_tokens,output_tokens FROM summary_runs "
                "WHERE kind='llm' AND provider='codex' ORDER BY started_at"
            ).fetchall()
        assert [tuple(r) for r in codex_rows] == [(20, 4, 9), (20, 4, 9)]

        # 3) AGY fresh (--tools "" strict mode) + resume + custom tools list
        a1 = _run_shim(
            bin_dir,
            "agy",
            ["-p", "--tools", "", "--system-prompt", "Extra rule.", "Answer directly"],
            env,
        )
        assert a1.returncode == 0, a1.stderr
        assert a1.stdout == "agy answer 1\n"
        assert "firm: session agy-conv-1" in a1.stderr
        assert list((state_dir / "llm").iterdir()) == []
        assert (
            private_home / ".firm" / "cli" / "agy" / "conversations" / "agy-conv-1.db"
        ).is_file()
        assert (private_home / ".firm" / "cli" / "agy" / "brain").is_dir()
        assert "generate_image" in llm_cli.AGY_NATIVE_TOOLS

        a2 = _run_shim(
            bin_dir,
            "agy",
            ["-p", "--tools", "view_file,grep_search", "--resume", "agy-conv-1", "Follow up"],
            env,
        )
        assert a2.returncode == 0, a2.stderr
        assert a2.stdout == "agy answer 2\n"

        agy_calls = json.loads(agy_record.read_text())
        model_idx = agy_calls[0]["argv"].index("--model") + 1
        assert agy_calls[0]["argv"][model_idx] == "gemini-4-neon-medium"
        assert agy_calls[0]["settings"]["toolPermission"] == "strict"
        assert "run_command(*)" in agy_calls[0]["settings"]["permissions"]["deny"]
        assert "tools: []" in agy_calls[0]["agent_md"]
        assert "Extra rule." in agy_calls[0]["agent_md"]
        assert "FIRM_ACTIVATION_TOKEN" not in agy_calls[0]["env_keys"]
        assert "GH_TOKEN" not in agy_calls[0]["env_keys"]
        assert "FIRM_LLM_CHILD" in agy_calls[0]["env_keys"]
        assert "--conversation" in agy_calls[1]["argv"]
        assert agy_calls[1]["stdin"] == {"event": "user", "message": {"content": "Follow up"}}
        assert agy_calls[1]["settings"]["toolPermission"] == "always-proceed"
        assert agy_calls[1]["brain_is_symlink"] is True
        assert 'tools: ["view_file", "grep_search"]' in agy_calls[1]["agent_md"]


def test_shim_cleans_run_dir_on_failure_restores_oauth_and_handles_refusals(tmp_path):
    fake_bin = tmp_path / "real-bin"
    fake_bin.mkdir()
    _write_fake_exe(
        fake_bin,
        "claude",
        """
import json, sys
print(json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"
}))
sys.exit(1)
""",
    )

    runtime, founder = _setup_runtime(tmp_path, llm_config={"enabled": False})
    with _LoopbackBroker(runtime) as broker:
        _state_dir, _creds_dir, _private, bin_dir, env = _prepare_guest(
            tmp_path, founder.token, broker.llm_url, fake_bin=fake_bin
        )
        # With [llm] enabled = false -> exit 2
        disabled = _run_shim(bin_dir, "claude", ["-p", "hi"], env)
        assert disabled.returncode == 2
        assert "LLM CLI tools are disabled" in disabled.stderr

    # Re-enable with working config, test failed child exit cleans run_dir.
    runtime2, founder2 = _setup_runtime(tmp_path / "r2")
    with _LoopbackBroker(runtime2) as broker2:
        state_dir2, creds_dir2, _private_home2, bin_dir2, env2 = _prepare_guest(
            tmp_path / "r2", founder2.token, broker2.llm_url, fake_bin=fake_bin
        )
        # Also verify CLAUDE_CODE_OAUTH_TOKEN is restored when invoking main() in-process.
        prev = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "caller-prior-token"
        saved_path = os.environ.get("PATH")
        try:
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{saved_path or ''}"
            old_stdin = sys.stdin
            try:
                sys.stdin = open(os.devnull, encoding="utf-8")
                rc = llm_cli.main(
                    ["--config", str(state_dir2 / "llm-config.json"), "claude", "-p", "fail"]
                )
            finally:
                sys.stdin.close()
                sys.stdin = old_stdin
            assert rc != 0
            assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "caller-prior-token"
            assert list((state_dir2 / "llm").iterdir()) == []
        finally:
            if saved_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = saved_path
            if prev is None:
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            else:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = prev

        # Bad activation token -> exit 75
        (creds_dir2 / "activation-token").write_text("bad-token\n", encoding="utf-8")
        expired = _run_shim(bin_dir2, "claude", ["-p", "hi"], env2)
        assert expired.returncode == 75
        assert "exist only during your work period" in expired.stderr
        assert list((state_dir2 / "llm").iterdir()) == []


def test_cross_provider_env_scrubbing_and_safe_stdin(tmp_path):
    fake_bin = tmp_path / "real-bin"
    fake_bin.mkdir()
    _write_fake_exe(
        fake_bin,
        "claude",
        """
import json, sys
sess = "11111111-1111-4111-8111-111111111111"
stdin_data = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": sess}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": sess,
    "result": "claude:" + repr(stdin_data),
}))
""",
    )
    _write_fake_exe(
        fake_bin,
        "codex",
        """
import json, os, sys
if sys.argv[1:] == ["--help"]:
    print(json.dumps({
        "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
        "cfg": os.environ.get("CLAUDE_CONFIG_DIR"),
        "gemini": os.environ.get("GEMINI_API_KEY"),
    }))
    sys.exit(0)
stdin_data = sys.stdin.read()
env_seen = {
    "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
    "cfg": os.environ.get("CLAUDE_CONFIG_DIR"),
    "gemini": os.environ.get("GEMINI_API_KEY"),
    "stdin": stdin_data,
}
print(json.dumps({"type": "thread.started", "thread_id": "22222222-2222-4222-8222-222222222222"}))
print(json.dumps({
    "type": "item.completed",
    "item": {"id": "i1", "type": "agent_message", "text": json.dumps(env_seen)},
}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""",
    )
    _write_fake_exe(
        fake_bin,
        "agy",
        """
import json, os, sys
payload = json.loads(sys.stdin.readline())
print(json.dumps({"event": "init", "conversation_id": "a-1", "init": {"model": "m"}}))
print(json.dumps({
    "event": "result",
    "result": {
        "conversation_id": "a-1",
        "status": "SUCCESS",
        "response": "agy:" + payload["message"]["content"],
    },
}))
""",
    )

    runtime, founder = _setup_runtime(tmp_path, max_active=2)
    with _LoopbackBroker(runtime) as broker:
        _state_dir, _creds_dir, _private, bin_dir, env = _prepare_guest(
            tmp_path,
            founder.token,
            broker.llm_url,
            fake_bin=fake_bin,
            base_env={
                "CLAUDE_CODE_OAUTH_TOKEN": "parent-claude-token",
                "CLAUDE_CONFIG_DIR": "/tmp/parent-claude-dir",
                "GEMINI_API_KEY": "parent-gemini-key",
            },
        )

        # --help scrubs parent Claude env vars and cloud keys
        h = _run_shim(bin_dir, "codex", ["--help"], env)
        assert h.returncode == 0
        assert json.loads(h.stdout) == {"oauth": None, "cfg": None, "gemini": None}

        # Redirected shell while-read loop does not lose remaining lines to codex exec
        list_file = tmp_path / "items.txt"
        list_file.write_text("first\nsecond\n", encoding="utf-8")
        loop_cmd = (
            f'while IFS= read -r line; do {bin_dir}/codex exec "check $line"; '
            f"done < {list_file}"
        )
        loop_proc = subprocess.run(
            ["/bin/sh", "-c", loop_cmd],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert loop_proc.returncode == 0, loop_proc.stderr
        items = [json.loads(line) for line in loop_proc.stdout.strip().splitlines()]
        assert items == [
            {"oauth": None, "cfg": None, "gemini": None, "stdin": ""},
            {"oauth": None, "cfg": None, "gemini": None, "stdin": ""},
        ]

        # Open idle pipe passed to positional invocations must not hang
        for provider, args in (
            ("claude", ["-p", "hello"]),
            ("codex", ["exec", "hello"]),
            ("agy", ["-p", "hello"]),
        ):
            r_fd, w_fd = os.pipe()
            try:
                proc = subprocess.run(
                    [str(bin_dir / provider), *args],
                    env=env,
                    stdin=r_fd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert proc.returncode == 0, (provider, proc.stderr)
            finally:
                os.close(r_fd)
                os.close(w_fd)

