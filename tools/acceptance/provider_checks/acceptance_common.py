"""Sanitized evidence shared by the integrated provider acceptance checks."""

import json
import re

from agy_protocol import analyze

PROVIDERS = ("codex", "claude", "agy")
AGY_AUTH_FILE = (
    "/tmp/firm-agy-dedicated-profile/home/.gemini/antigravity-cli/antigravity-oauth-token"
)
AGY_MODELS = {f"{model}-{effort}" for model in ("gemini-3.8-flash", "gemini-4-argon")
              for effort in ("low", "medium", "high")}


def selected_profile(source, name, provider=None):
    profile = dict(source["profiles"][name])
    profile.pop("alternates", None)
    if profile["provider"] not in PROVIDERS:
        raise ValueError("Unsupported acceptance provider")
    if provider is not None and profile["provider"] != provider:
        raise ValueError("--profile must select --provider")
    accounts = {key: source["accounts"][key] for key in profile["accounts"]}
    if profile["provider"] == "agy":
        if source.get("google_cloud"):
            raise ValueError("AGY subscription acceptance cannot use [google_cloud]")
        if any(a.get("provider") != "agy" or a.get("auth_file") != AGY_AUTH_FILE
               for a in accounts.values()):
            raise ValueError("AGY acceptance requires the dedicated profile auth_file reference")
    return profile, accounts


# This exact validator also runs in the task guest. It reads the access projection,
# never the host profile, and returns only fixed labels and booleans.
AGY_PROJECTION_CHECK = r'''
def agy_projection_evidence(raw):
    result = {'kind': 'agy-access', 'access_only': False,
              'no_refresh_token': False, 'no_email': False, 'passed': False}
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate key')
                value[key] = item
            return value
        document = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(document, dict) or not isinstance(document.get('token'), dict):
            return result
        token = document['token']
        result['no_refresh_token'] = token.get('refresh_token', '') == ''
        result['no_email'] = 'email' not in document and 'email' not in token
        result['access_only'] = (
            set(document) == {'auth_method', 'token'}
            and isinstance(document['auth_method'], str) and bool(document['auth_method'])
            and {'access_token', 'token_type', 'expiry'} <= set(token)
            and set(token) <= {'access_token', 'token_type', 'expiry',
                               'refresh_token', 'expires_in'}
            and isinstance(token['access_token'], str) and bool(token['access_token'].strip())
            and token['token_type'] == 'Bearer'
            and isinstance(token['expiry'], str) and bool(token['expiry'])
            and ('expires_in' not in token or
                 (type(token['expires_in']) is int and token['expires_in'] >= 0))
            and result['no_refresh_token'] and result['no_email'])
        result['passed'] = result['access_only']
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return result
'''
_projection_namespace = {"json": json}
exec(AGY_PROJECTION_CHECK, _projection_namespace)
agy_projection_evidence = _projection_namespace["agy_projection_evidence"]


def agy_launch_evidence(spec):
    """Inspect the actual staged bytes, without re-projecting away a leak."""
    projection = agy_projection_evidence(spec.secrets.get("agy-auth", b""))
    try:
        argv = json.loads(spec.env.get("FIRM_AGY_ARGV", "null"))
        model = argv[argv.index("--model") + 1]
        model = model if model in AGY_MODELS else None
    except (ValueError, TypeError, AttributeError, IndexError):
        model = None
    mode = spec.env.get("FIRM_AGY_MODE")
    mode = mode if mode in {"role", "task", "tool_free"} else None
    return {"provider": "agy", "route": "native-subscription", "mode": mode,
            "native_model": model, "credential_projection": projection,
            "quota": "unavailable", "context": "unavailable",
            "passed": projection["passed"] and model is not None and mode is not None}


def capture_agy_launches(box, directory):
    """Observe role/summary RunSpecs at the real sandbox boundary."""
    start, launches = box.start, {}

    def capture(spec):
        if "FIRM_AGY_MODE" in spec.env:
            receipt = agy_launch_evidence(spec)
            launches[spec.run_id] = receipt
            (directory / f"{spec.run_id}.launch.json").write_text(json.dumps(receipt, indent=2))
            if not receipt["passed"]:
                raise ValueError("AGY launch route or access projection failed acceptance")
        return start(spec)

    box.start = capture
    return launches


def provider_output(logs):
    """Remove only the task wrapper's final receipt before native protocol parsing."""
    lines = logs.splitlines(keepends=True)
    if lines:
        try:
            event = json.loads(lines[-1])
        except (ValueError, UnicodeError):
            event = None
        if isinstance(event, dict) and set(event) == {"task_auth_files"}:
            return b"".join(lines[:-1])
    return logs


def agy_output_evidence(logs, *, expected_model=None, expected_id=None, exit_code=0):
    stream = provider_output(logs)
    lines = stream.splitlines(keepends=True)
    if lines and expected_id is not None:
        try:
            first = json.loads(lines[0])
        except (ValueError, UnicodeError):
            first = None
        # The launcher emits this before native output. Check its exact identity
        # before passing the native stream to the A0 protocol parser.
        if first == {"type": "firm.agy_expected_session", "conversation_id": expected_id}:
            stream = b"".join(lines[1:])
    evidence = analyze(stream, exit_code, expected_conversation_id=expected_id)
    model = evidence["resolved_model"]
    model = model if model in AGY_MODELS else None
    return {
        "protocol_complete": evidence["protocol_complete"], "success": evidence["success"],
        "native_model": model, "model_matches": model is not None and model == expected_model,
        "resume_requested": expected_id is not None,
        "conversation_matches": evidence["protocol_complete"] if expected_id is not None else None,
        "tool_steps": sum(evidence["tool_steps"].values()),
        "failed_tool_steps": sum(evidence["failed_tool_steps"].values()),
        "firm_tool_seen": any(name.startswith("mcp_firm_") for name in evidence["tool_steps"]),
        "native_cumulative_usage": evidence["native_cumulative_usage"],
    }


def native_id_from_output(logs):
    """Keep a native ID only in memory for equality checks, never in JSON evidence."""
    for line in provider_output(logs).splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and event.get("event") == "init":
            value = event.get("conversation_id")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                return value
    return None
