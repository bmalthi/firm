"""Shared HTTP OAuth transport and renewal races for Claude and Codex."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from . import claude_auth, codex_auth
from .credential_files import MAX_AUTH_BYTES, failure
from .provider_contract import ProviderSetupError

REFRESH_MARGIN = 600
MAX_RESPONSE_BYTES = MAX_AUTH_BYTES


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_without_redirects(request, *, timeout):
    """Open ``request`` refusing every redirect, so a bearer token is never forwarded."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def refresh_credentials(provider, document, now):
    """Exchange one refresh token into a complete host-only bundle."""
    if provider == "agy":
        raise failure("credential_native_renewal_required")
    codecs = {"claude": claude_auth, "codex": codex_auth}
    try:
        codec = codecs[provider]
    except KeyError:
        raise failure("credential_provider_invalid") from None
    endpoint, body = codec.refresh_request(document)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with open_without_redirects(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        access = result.get("access_token")
        if not isinstance(access, str) or not access.strip():
            raise ValueError
        rotated = result.get("refresh_token", body["refresh_token"])
        if not isinstance(rotated, str) or not rotated.strip():
            raise ValueError
        return codec.apply_refresh(document, result, now, rotated)
    except urllib.error.HTTPError as exc:
        raise failure(
            "credential_refresh_rejected"
            if exc.code in (400, 401, 403)
            else "credential_refresh_unavailable",
            transient=exc.code not in (400, 401, 403),
        ) from None
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        raise failure("credential_refresh_unavailable", transient=True) from None


def snapshot(provider, codec, files, force, *, margin, clock, refresh):
    """Read/project first; serialize renewal and preserve external writer precedence."""
    original = files.read()
    snapshot = codec.project(original)
    if not codec.renewable(snapshot["kind"], snapshot["data"]):
        return snapshot
    expires = snapshot["expires_at"]
    if not force and (margin is None or expires is None or expires > clock() + margin):
        return snapshot
    with files.locked():
        for _ in range(3):
            raw = files.read()
            current = codec.project(raw)
            expires = current["expires_at"]
            # Another Firm/local CLI renewal won while we waited for the lock.
            if raw != original:
                force = False
            if not force and (margin is None or expires is None or expires > clock() + margin):
                return current
            kind, document = codec.decode(raw)
            if not codec.renewable(kind, document):
                return current
            try:
                renewed = refresh(provider, document, clock())
                encoded = json.dumps(renewed).encode()
                result = codec.project(encoded)
                if result["expires_at"] is not None and result["expires_at"] <= clock():
                    raise failure("credential_refresh_invalid", transient=True)
            except ProviderSetupError:
                if files.read() != raw:
                    original, force = raw, False
                    continue
                raise
            try:
                if files.replace(raw, renewed):
                    return result
            except OSError:
                raise failure("credential_persist_unavailable", transient=True) from None
            original, force = raw, False
    raise failure("credential_refresh_conflict", transient=True)
