"""Owner-only records of exact runtime-supplied launch inputs, without credentials."""

import hashlib
import json
import os
from pathlib import Path


def record_inputs(root: Path, identifier: str, spec, *, kind: str, metadata: dict) -> Path:
    if not identifier or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in identifier):
        raise ValueError("invalid input record identifier")
    directory = root / kind / identifier
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    key = "role-instructions" if kind == "activations" else "summary-instructions"
    if "agy-instructions" in spec.secrets:
        key = "agy-instructions"
    input_name = {
        "activations": "activation.txt", "tasks": "task.txt",
    }.get(kind, "sources.txt")
    payloads = {input_name: spec.stdin or b""}
    if key in spec.secrets:
        payloads["instructions.txt"] = spec.secrets[key]
    files = {}
    for name, payload in payloads.items():
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        path = directory / name
        with path.open("wb") as handle:
            os.chmod(path, 0o600)
            handle.write(data)
        files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    provenance = {
        **metadata,
        "files": files,
        "scope": "Exact runtime-supplied inputs; excludes CLI defaults and provider-added context.",
        "launch_state": "Prepared; consult the runtime record for confirmed start.",
    }
    path = directory / "provenance.json"
    with path.open("w") as handle:
        os.chmod(path, 0o600)
        json.dump(provenance, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return directory


def role_tool_catalog(runtime) -> list[dict]:
    """Record the offered catalog, not a claim about a provider's deferred tool loading."""
    from .mcp_server import build_server

    server = build_server(runtime, "")
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.parameters}
        for tool in server._tool_manager.list_tools()
    ]
