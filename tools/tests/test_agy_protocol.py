"""Offline acceptance protocol regressions; no host provider processes."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "acceptance/agy_protocol.py"
spec = importlib.util.spec_from_file_location("agy_protocol", path)
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)

@pytest.fixture
def events():
    return [
        {"event": "init", "conversation_id": "synthetic-conversation", "init": {}},
        {"event": "result", "result": {
            "conversation_id": "synthetic-conversation", "status": "SUCCESS", "response": "ok",
        }},
    ]

def analyze(events, exit_code=0):
    return protocol.analyze(b"\n".join(json.dumps(event).encode() for event in events), exit_code)

@pytest.mark.parametrize("mode", ["role", "task"])
def test_captured_subscription_search_without_page_fetch(mode):
    fixture = json.loads((Path(__file__).parent / "fixtures/agy" /
                          f"captured_search_{mode}.json").read_text())
    result = analyze(fixture["events"], fixture["exit_code"])
    assert result["success"]
    assert result["tool_steps"]["search_web"] > 0
    assert "read_url_content" not in result["tool_steps"]
    assert not result["failed_tool_steps"]

@pytest.mark.parametrize("status", sorted(protocol.STATUSES))
@pytest.mark.parametrize("code", [0, 1, 2, -9])
def test_only_success_and_zero_exit_complete_work(events, status, code):
    events[-1]["result"]["status"] = status
    result = analyze(events, code)
    assert result["protocol_complete"]
    assert result["success"] is (status == "SUCCESS" and code == 0)

@pytest.mark.parametrize("raw,code", [
    (b'{"event":"result","event":"init"}', "duplicate_json_key"),
    (b'{"value":NaN}', "nonfinite_json"),
    (b'{"value":Infinity}', "nonfinite_json"),
    (b'\xff', "invalid_json"),
    (b'[]', "event_not_object"),
    (b'{}', "unsupported_event"),
    (b'{' * 2000, "invalid_json"),
    (b'a' * (protocol.MAX_BYTES + 1), "output_limit"),
])
def test_malformed_protocol_is_bounded_static_failure(raw, code):
    result = protocol.analyze(raw, 0)
    assert not result["success"] and result["error"] == code

@pytest.mark.parametrize("value", [-1, 0.5, True, "123", None])
def test_invalid_usage_not_accepted(events, value):
    events[-1]["result"]["usage"] = {"input_tokens": value}
    assert analyze(events)["error"] == "invalid_usage"

def test_usage_not_promoted_to_activation_counters(events):
    events[-1]["result"]["usage"] = {"input_tokens": 160, "output_tokens": 30}
    result = analyze(events)
    assert result["native_cumulative_usage"]["input_tokens"] == 160
    assert result["native_cumulative_usage"]["cache_read_tokens"] is None
    assert result["activation_usage"] is None

def test_no_result_changed_id_and_trailing_events_fail(events):
    assert analyze(events[:1])["error"] == "missing_result"
    events[-1]["result"]["conversation_id"] = "another-conversation"
    assert analyze(events)["error"] == "conversation_changed"
    events[-1]["result"]["conversation_id"] = "synthetic-conversation"
    assert analyze([*events, events[-1]])["error"] == "event_after_result"

def test_tool_updates_deduplicated_and_payloads_omitted(events):
    event = {"event": "step_update", "step_update": {
        "conversation_id": "synthetic-conversation", "step_index": 1,
        "step_type": "tool", "state": "DONE", "tool_name": "run_command",
        "tool_info": {"parameters": "secret-command", "output": "private-output"},
    }}
    result = analyze([events[0], event, event, events[1]])
    assert result["tool_steps"] == {"run_command": 1}
    assert not any(value in json.dumps(result) for value in (
        "secret-command", "private-output", "synthetic-conversation",
    ))

def test_result_only_success_never_completes(events):
    events[-1]["result"]["conversation_id"] = ""
    result = analyze(events[-1:])
    assert not result["success"] and result["error"] == "invalid_conversation_id"

def test_error_prose_does_not_classify_auth_failure(events):
    events[-1]["result"].update(status="ERROR", error="authentication required")
    result = analyze(events)
    assert result["error"] == "unsuccessful_status"
    assert "authentication" not in json.dumps(result)

def native_credential():
    return {"auth_method": "consumer", "id_token_email": "private@example.invalid", "token": {
        "access_token": "synthetic-access", "refresh_token": "synthetic-renewal",
        "token_type": "Bearer", "expiry": "2030-01-01T00:00:00Z", "expires_in": 3600,
        "unknown": "never-project",
    }}

def test_captured_timeout_success_is_not_completion(events):
    events[-1]['result']['finished_reason'] = 'print_timeout'
    result = analyze(events)
    assert result['protocol_complete'] and result['status'] == 'SUCCESS'
    assert not result['success'] and result['error'] == 'print_timeout'

def test_resume_requires_exact_native_id(events):
    stream = b'\n'.join(json.dumps(event).encode() for event in events)
    result = protocol.analyze(stream, 0, expected_conversation_id='requested-other-id')
    assert not result['success'] and result['error'] == 'resume_id_mismatch'
    assert 'requested-other-id' not in json.dumps(result)

def test_failed_tool_is_structured_even_when_native_result_succeeds(events):
    update = {'event': 'step_update', 'step_update': {
        'conversation_id': 'synthetic-conversation', 'step_index': 2,
        'step_type': 'tool', 'tool_name': 'mcp_firm_firm_echo', 'state': 'ERROR',
    }}
    result = analyze([events[0], update, events[1]])
    assert result['success']
    assert result['failed_tool_steps'] == {'mcp_firm_firm_echo': 1}
    assert result['tool_steps'] == {'mcp_firm_firm_echo': 1}

def test_multiple_turns_require_explicit_count_and_keep_latest_cumulative_usage(events):
    events[-1]['result']['usage'] = {'input_tokens': 10, 'output_tokens': 5}
    second = json.loads(json.dumps(events[-1]))
    second['result']['usage'] = {'input_tokens': 30, 'output_tokens': 12}
    step = {'event': 'step_update', 'step_update': {
        'conversation_id': 'synthetic-conversation', 'step_index': 2,
        'step_type': 'user_input', 'state': 'DONE',
    }}
    stream = b'\n'.join(json.dumps(e).encode() for e in [*events, step, second])
    assert protocol.analyze(stream, 0)['error'] == 'event_after_result'
    result = protocol.analyze(stream, 0, expected_turns=2)
    assert result['success'] and result['native_cumulative_usage']['input_tokens'] == 30
    assert result['activation_usage'] is None
    assert protocol.analyze(stream, 0, expected_turns=3)['error'] == 'missing_turn_result'

@pytest.mark.parametrize('name', [
    'captured_expiry_no_replacement', 'captured_expiry_replacement',
    'captured_expiry_resume_stale_error',
])
def test_real_expiry_capture_exit_zero_does_not_mean_success(name):
    fixture = json.loads((Path(__file__).parent / 'fixtures/agy' / (name + '.json')).read_text())
    assert fixture['exit_code'] == 0
    result = analyze(fixture['events'], fixture['exit_code'])
    assert result['protocol_complete'] and not result['success']
    assert result['status'] == 'ERROR' and result['finished_reason'] == 'error'
    assert result['error'] == 'unsuccessful_status'
    assert result['native_cumulative_usage']['input_tokens'] > 0

@pytest.mark.parametrize('cycle', [1, 2])
def test_captured_print_yield_is_not_completion_and_fresh_resume_is_normal(cycle):
    fixtures = Path(__file__).parent / 'fixtures/agy'
    yielded = json.loads((fixtures / f'captured_print_cycle_{cycle}_yield.json').read_text())
    resumed = json.loads((fixtures / f'captured_print_cycle_{cycle}_resume.json').read_text())
    result = analyze(yielded['events'], yielded['exit_code'])
    assert result['protocol_complete'] and not result['success']
    assert result['status'] == 'SUCCESS' and result['finished_reason'] == 'print_timeout'
    assert yielded['markers_at_native_exit'] == [1, 2]
    assert yielded['markers_45s_after_native_exit'] == [1, 2, 3]
    result = analyze(resumed['events'], resumed['exit_code'])
    assert result['success'] and result['finished_reason'] is None
    assert resumed['nonce_recalled']

def test_captured_mcp_inventory_and_success_do_not_prove_tool_availability():
    fixtures = Path(__file__).parent / 'fixtures/agy'
    for label, expected in [('float_ignored', {}),
                            ('integer_authenticated', {'mcp_firm_org_tree': 1})]:
        captured = json.loads((fixtures / f'captured_mcp_{label}.json').read_text())
        result = analyze(captured['events'], captured['exit_code'])
        inventory = captured['events'][0]['init']['tools']
        assert result['success'] and result['tool_steps'] == expected
        assert len(inventory) == 60 and not any(t.startswith('mcp_firm_') for t in inventory)
        assert captured['mcp_symlink'] and captured['catalog_size'] == 42
        calls = [r for r in captured['receipts'] if r.get('method') == 'tools/call']
        assert calls == ([{'authorized': True, 'method': 'tools/call', 'tool': 'org_tree'}]
                         if expected else [])

@pytest.mark.parametrize("path", sorted((Path(__file__).parent / "fixtures/agy").glob("*.json")),
                         ids=lambda p: p.stem)
def test_protocol_fixture(path):
    fixture = json.loads(path.read_text())
    assert fixture["fixture_kind"] in {"synthetic", "captured_sanitized"}
    stream = b"\n".join(json.dumps(e).encode() for e in fixture["events"])
    actual = protocol.analyze(stream, fixture["exit_code"], **fixture.get("parser_options", {}))
    assert fixture["expected"]
    for key, value in fixture["expected"].items():
        assert actual[key] == value, key
