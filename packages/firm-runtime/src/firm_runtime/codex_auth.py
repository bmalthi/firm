"""Codex credential parsing, projection, and OAuth field mapping."""

from __future__ import annotations

import base64
import copy
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from .credential_files import failure, read_private_credential
from .provider_contract import ProviderSetupError


def expiry(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def jwt_expiry(token):
    # JWT claims only schedule refresh; they never establish identity or authorization.
    try:
        encoded = token.split(".")[1]
        return expiry(
            json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))).get("exp")
        )
    except (AttributeError, IndexError, ValueError, TypeError):
        return None


def read_auth(path: Path) -> bytes:
    data = read_private_credential(path, "Codex")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderSetupError(
            category="auth",
            code="credential_invalid",
            message="configured Codex authentication is not valid JSON",
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderSetupError(
            category="auth",
            code="credential_invalid",
            message="configured Codex authentication must contain a JSON object",
        )
    return data


def validate_credentials(path):
    parsed = json.loads(read_auth(path))
    tokens = parsed.get("tokens")
    if not (
        isinstance(parsed.get("OPENAI_API_KEY"), str) and parsed["OPENAI_API_KEY"].strip()
    ) and not (
        isinstance(tokens, dict)
        and isinstance(tokens.get("access_token"), str)
        and tokens["access_token"].strip()
    ):
        raise ProviderSetupError(
            category="auth",
            code="credential_missing_token",
            message="configured Codex authentication has no account token or API key",
        )


def decode(raw):
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError
        return "codex-json", document
    except (UnicodeError, ValueError, AttributeError, TypeError):
        raise failure("credential_invalid") from None


def project(raw):
    kind, document = decode(raw)
    api_key = document.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        return {
            "kind": kind,
            "data": {"auth_mode": "apikey", "OPENAI_API_KEY": api_key},
            "expires_at": None,
        }
    if document.get("auth_mode") == "apikey":
        raise failure("credential_missing_token")
    fields = document.get("tokens")
    if (
        not isinstance(fields, dict)
        or not isinstance(fields.get("access_token"), str)
        or not fields["access_token"].strip()
    ):
        raise failure("credential_missing_token")
    projected = {
        key: copy.deepcopy(fields[key])
        for key in ("access_token", "id_token", "account_id")
        if key in fields
    }
    # Codex requires the field structurally, but guests must not own renewal.
    projected["refresh_token"] = ""
    data = {"tokens": projected}
    for key in ("auth_mode", "last_refresh"):
        if key in document:
            data[key] = copy.deepcopy(document[key])
    return {"kind": kind, "data": data, "expires_at": jwt_expiry(fields["access_token"])}


def renewable(kind, document):
    """Preserve the API-key renewal rule for projected and raw bundles."""
    return not document.get("OPENAI_API_KEY")


def refresh_request(document):
    refresh = document.get("tokens", {}).get("refresh_token")
    if not isinstance(refresh, str) or not refresh.strip():
        raise failure("credential_refresh_missing")
    return "https://auth.openai.com/oauth/token", {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    }


def apply_refresh(document, result, now, refresh):
    renewed = copy.deepcopy(document)
    target = renewed["tokens"]
    target["access_token"] = result["access_token"]
    target["refresh_token"] = refresh
    if isinstance(result.get("id_token"), str) and result["id_token"]:
        target["id_token"] = result["id_token"]
    renewed["last_refresh"] = datetime.fromtimestamp(now, UTC).isoformat()
    return renewed
