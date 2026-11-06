import asyncio
import json
import os
import sys

from firm_sandbox.file_server import FileServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from firm_runtime import mcp_server
from firm_runtime.compatibility import initialize_firm
from firm_runtime.mcp_server import build_server
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store


def test_stdio_server_checks_release_marker_before_opening_store(tmp_path, monkeypatch, capsys):
    database = tmp_path / "firm" / "durable" / "firm.sqlite3"
    database.parent.parent.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["firm-role-tools", str(database)])
    monkeypatch.setenv("FIRM_ACTIVATION_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        mcp_server,
        "Store",
        lambda _path: (_ for _ in ()).throw(AssertionError("Store opened before guard")),
    )
    try:
        mcp_server.main()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unmarked firm was accepted")
    assert "no release compatibility record" in capsys.readouterr().err
    assert not database.exists()


def test_mcp_stdio_authentication_and_real_tool_call(tmp_path, monkeypatch):
    initialize_firm(tmp_path)
    runtime = Runtime(Store(tmp_path / "durable" / "firm.sqlite3"))
    founder = runtime.lifecycle.initialize(
        "test",
        "Build",
        {
            "max_active": 1,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
        },
    )["founder"]["employee_id"]
    files = FileServer.__new__(FileServer)
    monkeypatch.setattr(files, "browse_roots", lambda: [{"path": "shared"}])
    runtime.messaging.files = files
    mail = runtime.messaging.send(
        OWNER, [founder], "Real MCP envelope", request_id="mail",
        attachments=[{"path": "shared/reports", "name": "findings.md"}],
    )
    runtime.start()
    invocation = runtime.admit()
    runtime.mark_started(invocation.activation_id)

    async def check():
        server = build_server(runtime, invocation.token)
        definitions = await server.list_tools()
        assert {
            "send",
            "claim",
            "context",
            "handoff",
            "hire",
            "retry",
            "get_role",
            "find_roles",
            "org_tree",
        } <= {t.name for t in definitions}
        for tool in definitions:
            assert not {"actor_id", "token", "sender", "owner"} & set(
                tool.inputSchema.get("properties", {})
            )
        by_name = {tool.name: tool for tool in definitions}
        hiring_schema = by_name["hire"].inputSchema
        assert set(hiring_schema["properties"]) == {
            "slug",
            "mandate",
            "request_id",
            "temporary_periods",
        }
        assert hiring_schema["additionalProperties"] is False
        for name in ("send", "reply"):
            attachment_schema = by_name[name].inputSchema["properties"]["attachments"]
            variants = attachment_schema.get("anyOf", [attachment_schema])
            assert any(variant.get("maxItems") == 10 for variant in variants)
        assert by_name["find_roles"].annotations.readOnlyHint is True
        assert by_name["org_tree"].annotations.readOnlyHint is True
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                "from firm_runtime.mcp_server import main; main()",
                str(runtime.store.path),
            ],
            env={**os.environ, "FIRM_ACTIVATION_TOKEN": invocation.token},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            rejected = await session.call_tool(
                "hire",
                {
                    "slug": "tester",
                    "mandate": "Test",
                    "request_id": "hire-tester",
                    "execution_profile": "untrusted-choice",
                },
            )
            assert rejected.isError
            with runtime.store.read() as db:
                assert db.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 1
            hired = await session.call_tool(
                "hire",
                {
                    "slug": "tester",
                    "mandate": "Test",
                    "request_id": "hire-tester",
                },
            )
            assert not hired.isError
            assert "founder/tester (e1)" in hired.content[0].text
            assert "No kickoff message is needed" in hired.content[0].text
            assert "execution_profile" not in hired.content[0].text
            result = await session.call_tool(
                "claim", {"message_ids": [mail["message_id"]], "request_id": "mcp-claim"}
            )
            assert not result.isError
            assert result.structuredContent is None
            assert len(result.content) == 1
            assert result.content[0].text.startswith("Messages claimed.")
            assert "Real MCP envelope" in json.dumps(result.model_dump())
            assert "Attachments (relative to your drive; prepend its path to open):" in (
                result.content[0].text
            )
            assert "shared/reports/findings.md" in result.content[0].text
            directory = await session.call_tool("org_tree", {"depth": 0})
            assert not directory.isError
            assert founder in directory.content[0].text
            runtime.finish(invocation.activation_id)
            expired = await session.call_tool("peek", {})
            assert expired.isError
            assert "expired" in json.dumps(expired.model_dump())

    asyncio.run(asyncio.wait_for(check(), timeout=20))
