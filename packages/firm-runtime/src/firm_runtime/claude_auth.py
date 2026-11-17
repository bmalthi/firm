"""Claude credential parsing, projection, and OAuth field mapping."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from .credential_files import failure, read_private_credential
from .provider_contract import ProviderSetupError


def expiry(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


MAX_TOKEN_BYTES = 4096


def _auth_error(code: str, message: str) -> ProviderSetupError:
    return ProviderSetupError(category="auth", code=code, message=message)


def read_auth(path: Path) -> tuple[str, bytes]:
    return parse_auth(read_private_credential(path, "Claude"))


def parse_auth(raw: bytes) -> tuple[str, bytes]:
    """Classify credential bytes by the one rule used for both saving and reading."""
    if b"\0" in raw:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication contains a NUL byte"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication is not valid UTF-8"
        ) from None
    if text.lstrip().startswith(("{", "[")):
        try:
            credentials = json.loads(text)
        except json.JSONDecodeError:
            raise _auth_error(
                "credential_invalid", "configured Claude authentication is not valid JSON"
            ) from None
        if not isinstance(credentials, dict) or not credentials:
            raise _auth_error(
                "credential_invalid",
                "configured Claude authentication must contain a JSON object",
            )
        return "credentials-json", raw
    if len(raw) > MAX_TOKEN_BYTES:
        raise _auth_error(
            "credential_too_large", "configured Claude OAuth token exceeds 4096 bytes"
        )
    if text.endswith("\r\n"):
        body = text[:-2]
    elif text.endswith(("\n", "\r")):
        body = text[:-1]
    else:
        body = text
    if "\n" in body or "\r" in body:
        raise _auth_error(
            "credential_invalid", "configured Claude authentication must contain one line"
        )
    token = body.strip()
    if not token:
        raise _auth_error("credential_invalid", "configured Claude authentication is empty")
    return "oauth-token", token.encode()


def validate_credentials(path):
    kind, raw = read_auth(path)
    if kind == "credentials-json":
        account = json.loads(raw).get("claudeAiOauth")
        if not (
            isinstance(account, dict)
            and isinstance(account.get("accessToken"), str)
            and account["accessToken"].strip()
        ):
            raise _auth_error(
                "credential_missing_token",
                "configured Claude authentication has no OAuth token",
            )


def read(path):
    # Setup-token normalization is also the value used for conflict comparisons.
    return read_auth(path)[1]


def decode(raw):
    try:
        if isinstance(raw, bytes):
            # What is saved must be readable later: a pasted token with a stray line
            # is refused here instead of being installed and rejected at the next read.
            raw = parse_auth(raw)[1]
        text = raw.decode() if isinstance(raw, bytes) else raw
        if not text.lstrip().startswith(("{", "[")):
            if not text.strip():
                raise ValueError
            return "oauth-token", text.strip()
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError
        return "credentials-json", document
    except (UnicodeError, ValueError, AttributeError, TypeError):
        raise failure("credential_invalid") from None


def project(raw):
    kind, document = decode(raw)
    if kind == "oauth-token":
        return {"kind": kind, "data": document, "expires_at": None}
    fields = document.get("claudeAiOauth")
    if (
        not isinstance(fields, dict)
        or not isinstance(fields.get("accessToken"), str)
        or not fields["accessToken"].strip()
    ):
        raise failure("credential_missing_token")
    allowed = ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier")
    projected = {key: copy.deepcopy(fields[key]) for key in allowed if key in fields}
    expires = expiry(fields.get("expiresAt"))
    return {
        "kind": kind,
        "data": {"claudeAiOauth": projected},
        "expires_at": expires / 1000 if expires is not None else None,
    }


def renewable(kind, document):
    """Claude setup tokens have no refresh exchange."""
    return kind != "oauth-token"


def refresh_request(document):
    fields = document.get("claudeAiOauth", {})
    refresh = fields.get("refreshToken")
    if not isinstance(refresh, str) or not refresh.strip():
        raise failure("credential_refresh_missing")
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    }
    scopes = fields.get("scopes")
    if isinstance(scopes, list) and scopes and all(isinstance(scope, str) for scope in scopes):
        body["scope"] = " ".join(scopes)
    return "https://platform.claude.com/v1/oauth/token", body


def apply_refresh(document, result, now, refresh):
    renewed = copy.deepcopy(document)
    target = renewed["claudeAiOauth"]
    target["accessToken"] = result["access_token"]
    target["refreshToken"] = refresh
    lifetime = expiry(result.get("expires_in"))
    if lifetime is None:
        raise ValueError
    target["expiresAt"] = (now + lifetime) * 1000
    if isinstance(result.get("scope"), str):
        target["scopes"] = result["scope"].split()
    return renewed


def setup_token_warning(account_id, account):
    """Warn when every guest receives a long-lived Claude setup-token."""
    if account.get("provider") != "claude":
        return None
    try:
        kind, _ = read_auth(Path(account["auth_file"]).expanduser())
    except (ProviderSetupError, OSError, KeyError):
        return None
    if kind != "oauth-token":
        return None
    return (
        f"Claude account {account_id} holds a long-lived setup-token, which every role run "
        "receives as is. Save Claude Code's OAuth credentials JSON instead (the "
        "'Claude Code-credentials' Keychain item) with firm save-credentials "
        f"{account_id} --provider claude; guests then hold only short-lived access tokens."
    )
