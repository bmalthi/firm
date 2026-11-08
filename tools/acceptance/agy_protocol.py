"""Conservative offline AGY CLI protocol probe, not a registered Firm provider.

Native event shape: https://antigravity.google/docs/cli/headless/
Fixtures identify synthetic versus sanitized captures from the pinned runtime.
No response, tool arguments, native IDs, or provider error prose leave this module.
"""

from __future__ import annotations

import json
import re
from collections import Counter

MAX_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
COUNTERS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
STATUSES = frozenset({
    "SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING",
})


class InvalidProtocol(ValueError):
    """A static diagnostic code, never provider output."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidProtocol("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise InvalidProtocol("nonfinite_json")


def decode(raw: bytes):
    """Bounded strict JSON decoding shared by the probe and fixture reader."""
    if len(raw) > MAX_BYTES:
        raise InvalidProtocol("output_limit")
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, InvalidProtocol):
            raise
        raise InvalidProtocol("invalid_json") from None


def analyze(stream: bytes, exit_code: int, *, expected_turns: int = 1,
            expected_conversation_id: str | None = None) -> dict:
    """Inspect one completed print invocation; not an incremental runtime parser.

    Result usage is reported only as native cumulative usage: it must not be
    added directly to activation totals on resume. Exit-zero
    SUCCESS does not establish tool execution, auth isolation, or Firm readiness.
    """
    result = {
        "protocol_complete": False,
        "success": False,
        "status": None,
        "startup_failure": False,
        "finished_reason": None,
        "resolved_model": None,
        "error": None,
        "events": 0,
        "tool_steps": {},
        "failed_tool_steps": {},
        "native_cumulative_usage": None,
        "activation_usage": None,
    }
    try:
        if type(exit_code) is not int:
            raise InvalidProtocol("invalid_exit_code")
        if type(expected_turns) is not int or expected_turns < 1:
            raise InvalidProtocol("invalid_expected_turns")
        if len(stream) > MAX_BYTES:
            raise InvalidProtocol("output_limit")
        native_id = None
        terminal = None
        tools = {}
        failed_tools = {}
        completed_turns = 0
        for line in stream.splitlines():
            if not line.strip():
                continue
            event = decode(line)
            if not isinstance(event, dict):
                raise InvalidProtocol("event_not_object")
            result["events"] += 1
            if terminal is not None:
                if completed_turns >= expected_turns or terminal["status"] != "SUCCESS":
                    raise InvalidProtocol("event_after_result")
                if event.get("event") != "step_update":
                    raise InvalidProtocol("missing_turn_steps")
                terminal = None
            kind = event.get("event")
            if kind not in {"init", "step_update", "result"}:
                raise InvalidProtocol("unsupported_event")
            payload = event.get(kind)
            if not isinstance(payload, dict):
                raise InvalidProtocol("invalid_payload")
            current_id = event.get("conversation_id") if kind == "init" else payload.get(
                "conversation_id"
            )
            # Captured 1.5.3 startup errors have only a result, an empty ID and
            # ERROR status. They never establish a resumable conversation.
            startup_failure = (
                kind == "result" and result["events"] == 1
                and current_id == "" and payload.get("status") == "ERROR"
            )
            if not startup_failure and (
                not isinstance(current_id, str) or not IDENTIFIER.fullmatch(current_id)
            ):
                raise InvalidProtocol("invalid_conversation_id")
            if kind == "init":
                if native_id is not None or result["events"] != 1:
                    raise InvalidProtocol("duplicate_init")
                native_id = current_id
                if expected_conversation_id is not None and native_id != expected_conversation_id:
                    raise InvalidProtocol("resume_id_mismatch")
                model = payload.get("model")
                if isinstance(model, str) and TOOL_NAME.fullmatch(model):
                    result["resolved_model"] = model
            elif native_id is None and not startup_failure:
                raise InvalidProtocol("missing_init")
            elif native_id is not None and native_id != current_id:
                raise InvalidProtocol("conversation_changed")
            if kind == "step_update":
                index = payload.get("step_index")
                if type(index) is not int or index < 0:
                    raise InvalidProtocol("invalid_step_index")
                if payload.get("state") not in {"ACTIVE", "DONE", "ERROR"}:
                    raise InvalidProtocol("invalid_step_state")
                if payload.get("step_type") == "tool":
                    name = payload.get("tool_name")
                    name = name if isinstance(name, str) and TOOL_NAME.fullmatch(name) else "other"
                    if index in tools and tools[index] != name:
                        raise InvalidProtocol("tool_identity_changed")
                    tools[index] = name
                    if payload.get("state") == "ERROR":
                        failed_tools[index] = name
            if kind == "result":
                status = payload.get("status")
                if not isinstance(status, str) or status not in STATUSES:
                    raise InvalidProtocol("unknown_status")
                if not isinstance(payload.get("response"), str):
                    raise InvalidProtocol("invalid_response")
                usage = payload.get("usage")
                if usage is not None:
                    if not isinstance(usage, dict):
                        raise InvalidProtocol("invalid_usage")
                    for key in COUNTERS:
                        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                            raise InvalidProtocol("invalid_usage")
                    result["native_cumulative_usage"] = {key: usage.get(key) for key in COUNTERS}
                terminal = payload
                completed_turns += 1
                result["startup_failure"] = startup_failure
        if terminal is None:
            raise InvalidProtocol("missing_result")
        if completed_turns != expected_turns:
            raise InvalidProtocol("missing_turn_result")
        result["protocol_complete"] = True
        result["status"] = terminal["status"]
        reason = terminal.get("finished_reason")
        if isinstance(reason, str) and IDENTIFIER.fullmatch(reason):
            result["finished_reason"] = reason
        result["tool_steps"] = dict(sorted(Counter(tools.values()).items()))
        result["failed_tool_steps"] = dict(sorted(Counter(failed_tools.values()).items()))
        if exit_code != 0:
            result["error"] = "process_exit"
        elif reason == "print_timeout":
            result["error"] = "print_timeout"
        elif terminal["status"] != "SUCCESS":
            result["error"] = "unsuccessful_status"
        else:
            result["success"] = True
    except InvalidProtocol as exc:
        result["error"] = str(exc)
    return result
