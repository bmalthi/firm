"""Account-owned OAuth renewal; guests receive access credentials, never refresh tokens."""

from __future__ import annotations

import base64
import copy
import fcntl
import http.client
import json
import math
import os
import stat
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import agy_auth
from .claude_provider import _read_auth as read_claude_auth
from .codex_provider import _read_auth as read_codex_auth
from .provider_contract import ProviderSetupError
from .provider_support import read_private_credential

REFRESH_MARGIN = 600
MAX_RESPONSE_BYTES = 1024 * 1024
OAUTH_ENDPOINTS = {
    "claude": (
        "https://platform.claude.com/v1/oauth/token",
        "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    ),
    "codex": ("https://auth.openai.com/oauth/token", "app_EMoamEEZ73f0CkXaXp7hrann"),
}


def _failure(code, *, transient=False):
    return ProviderSetupError(
        category="transient" if transient else "auth",
        code=code,
        message="Account credential renewal is temporarily unavailable"
        if transient
        else "Account credentials require renewal or login",
    )


def _expiry(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _jwt_expiry(token):
    # JWT claims only schedule refresh; they never establish identity or authorization.
    try:
        encoded = token.split(".")[1]
        return _expiry(
            json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))).get("exp")
        )
    except (AttributeError, IndexError, ValueError, TypeError):
        return None


def _decode(provider, raw):
    if provider not in (*OAUTH_ENDPOINTS, "agy"):
        raise _failure("credential_provider_invalid")
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        if provider == "claude" and not text.lstrip().startswith(("{", "[")):
            if not text.strip():
                raise ValueError
            return "oauth-token", text.strip()
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        kind = {"claude": "credentials-json", "codex": "codex-json", "agy": "agy-access"}
        return kind[provider], value
    except (UnicodeError, ValueError, AttributeError, TypeError):
        raise _failure("credential_invalid") from None


def guest_credentials(provider, raw):
    """Project an already-read credential bundle without network or filesystem mutation."""
    kind, document = _decode(provider, raw)
    if provider == "agy":
        return agy_auth.access_projection(document)
    if kind == "oauth-token":
        return {"kind": kind, "data": document, "expires_at": None}
    if provider == "claude":
        fields = document.get("claudeAiOauth")
        allowed = ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier")
        access_key = "accessToken"
    else:
        api_key = document.get("OPENAI_API_KEY")
        if isinstance(api_key, str) and api_key.strip():
            return {
                "kind": kind,
                "data": {"auth_mode": "apikey", "OPENAI_API_KEY": api_key},
                "expires_at": None,
            }
        if document.get("auth_mode") == "apikey":
            raise _failure("credential_missing_token")
        fields = document.get("tokens")
        allowed = ("access_token", "id_token", "account_id")
        access_key = "access_token"
    if (
        not isinstance(fields, dict)
        or not isinstance(fields.get(access_key), str)
        or not fields[access_key].strip()
    ):
        raise _failure("credential_missing_token")
    projected = {key: copy.deepcopy(fields[key]) for key in allowed if key in fields}
    if provider == "claude":
        expiry = _expiry(fields.get("expiresAt"))
        expiry = expiry / 1000 if expiry is not None else None
        data = {"claudeAiOauth": projected}
    else:
        expiry = _jwt_expiry(fields[access_key])
        # Codex requires the field structurally, but guests must not own renewal.
        projected["refresh_token"] = ""
        data = {"tokens": projected}
        for key in ("auth_mode", "last_refresh"):
            if key in document:
                data[key] = copy.deepcopy(document[key])
    return {"kind": kind, "data": data, "expires_at": expiry}


def setup_token_warning(account_id, account):
    """A Claude setup-token lasts a year and every guest run holds it; OAuth JSON does not."""
    if account.get("provider") != "claude":
        return None
    try:
        kind, _ = read_claude_auth(Path(account["auth_file"]).expanduser())
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_without_redirects(request, *, timeout):
    """Open ``request`` refusing every redirect, so a bearer token is never forwarded."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def refresh_credentials(provider, document, now):
    """Exchange one refresh token, returning a complete renewed host-only bundle."""
    if provider == "agy":
        raise _failure("credential_native_renewal_required")
    fields = document.get("claudeAiOauth" if provider == "claude" else "tokens", {})
    key = "refreshToken" if provider == "claude" else "refresh_token"
    refresh = fields.get(key)
    if not isinstance(refresh, str) or not refresh.strip():
        raise _failure("credential_refresh_missing")
    endpoint, client = OAUTH_ENDPOINTS[provider]
    body = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client}
    if provider == "claude":
        scopes = fields.get("scopes")
        if isinstance(scopes, list) and scopes and all(isinstance(scope, str) for scope in scopes):
            body["scope"] = " ".join(scopes)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
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
        rotated = result.get("refresh_token", refresh)
        if not isinstance(rotated, str) or not rotated.strip():
            raise ValueError
        renewed = copy.deepcopy(document)
        target = renewed["claudeAiOauth" if provider == "claude" else "tokens"]
        target["accessToken" if provider == "claude" else "access_token"] = access
        target[key] = rotated
        if provider == "claude":
            lifetime = _expiry(result.get("expires_in"))
            if lifetime is None:
                raise ValueError
            target["expiresAt"] = (now + lifetime) * 1000
            if isinstance(result.get("scope"), str):
                target["scopes"] = result["scope"].split()
        else:
            if isinstance(result.get("id_token"), str) and result["id_token"]:
                target["id_token"] = result["id_token"]
            renewed["last_refresh"] = datetime.fromtimestamp(now, UTC).isoformat()
        return renewed
    except urllib.error.HTTPError as exc:
        raise _failure(
            "credential_refresh_rejected"
            if exc.code in (400, 401, 403)
            else "credential_refresh_unavailable",
            transient=exc.code not in (400, 401, 403),
        ) from None
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        raise _failure("credential_refresh_unavailable", transient=True) from None


class CredentialStore:
    def __init__(self, provider, auth_file, *, clock=time.time, refresh=refresh_credentials):
        self.provider = provider
        self.path = Path(auth_file).expanduser()
        self.clock, self.refresh = clock, refresh

    def _read(self):
        if self.provider == "agy":
            agy_auth.profile_home(self.path)
            return read_private_credential(self.path, "AGY")
        if self.provider == "claude":
            return read_claude_auth(self.path)[1]
        if self.provider == "codex":
            return read_codex_auth(self.path)
        raise _failure("credential_provider_invalid")

    @contextmanager
    def _lock(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.path) + ".lock", flags, 0o600)
        except OSError:
            raise _failure("credential_lock_unavailable", transient=True) from None
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
            ):
                raise _failure("credential_lock_invalid")
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise _failure("credential_lock_timeout", transient=True) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(descriptor)

    def _replace(self, expected, document):
        """Check the current source immediately before atomic replacement.

        Firm participants share the lock. Uncooperative external writers cannot
        be made atomic with this compare/read/replace protocol; changed sources
        detected here are reread, never overwritten with the stale candidate.
        """
        raw = json.dumps(document, separators=(",", ":")).encode()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _failure("credential_refresh_invalid", transient=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".firm-auth-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if self._read() != expected:
                return False
            os.replace(temporary, self.path)
            return True
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def save(self, raw):
        """Install owner-supplied credentials privately, under the renewal lock."""
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _failure("credential_too_large")
        guest_credentials(self.provider, raw)
        if self.provider == "agy":
            agy_auth.prepare_profile(self.path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock():
            descriptor, temporary = tempfile.mkstemp(prefix=".firm-auth-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _agy_snapshot(self, force, *, margin=agy_auth.RENEWAL_MARGIN):
        agy_auth.profile_home(self.path)
        try:
            raw, document = agy_auth.read_native(self.path)
            snapshot = agy_auth.access_projection(document)
        except ProviderSetupError:
            pass  # A native mid-write gets a second read under the renewal lock.
        else:
            backup = agy_auth._expiry_backup(self.path)
            if (not force and not backup.exists() and not backup.is_symlink()
                    and (margin is None or snapshot["expires_at"] >= self.clock() + margin)):
                return snapshot
        with self._lock():
            agy_auth.restore_expiry(self.path, replace=self._replace)
            for _ in range(3):
                raw, document = agy_auth.read_native(self.path)
                current = agy_auth.access_projection(document)
                if not force and (margin is None or current["expires_at"] >= self.clock() + margin):
                    return current
                try:
                    renewed = agy_auth.force_renew(self.path, replace=self._replace)
                except (ProviderSetupError, OSError) as exc:
                    if isinstance(exc, OSError):
                        exc = _failure("credential_persist_unavailable", transient=True)
                    if agy_auth.read_native(self.path)[1] != document:
                        force = False
                        continue
                    # Native CLI has no structured HTTP rejection; do not infer from prose.
                    if exc.category == "transient":
                        if current["expires_at"] > self.clock() + 300:
                            return current
                        if current["expires_at"] <= self.clock():
                            raise _failure("credential_refresh_rejected") from None
                    raise exc from None
                native, native_document = agy_auth.read_native(self.path)
                if native_document != renewed:
                    force = False
                    continue
                result = guest_credentials(self.provider, native)
                if result["expires_at"] <= self.clock():
                    raise _failure("credential_refresh_invalid", transient=True)
                try:
                    if self._replace(native, renewed):
                        return result
                except OSError:
                    raise _failure("credential_persist_unavailable", transient=True) from None
                force = False
        raise _failure("credential_refresh_conflict", transient=True)

    def snapshot(self, force=False):
        if self.provider == "agy":
            return self._agy_snapshot(force)
        original = self._read()
        snapshot = guest_credentials(self.provider, original)
        if snapshot["expires_at"] is None and snapshot["kind"] == "oauth-token":
            return snapshot
        if self.provider == "codex" and snapshot["data"].get("OPENAI_API_KEY"):
            return snapshot
        expires = snapshot["expires_at"]
        if not force and (expires is None or expires > self.clock() + REFRESH_MARGIN):
            return snapshot
        with self._lock():
            for _ in range(3):
                raw = self._read()
                current = guest_credentials(self.provider, raw)
                expires = current["expires_at"]
                # Another Firm/local CLI renewal won while we waited for the lock.
                if raw != original:
                    force = False
                if not force and (expires is None or expires > self.clock() + REFRESH_MARGIN):
                    return current
                kind, document = _decode(self.provider, raw)
                if kind == "oauth-token" or (
                    self.provider == "codex" and document.get("OPENAI_API_KEY")
                ):
                    return current
                try:
                    renewed = self.refresh(self.provider, document, self.clock())
                    encoded = json.dumps(renewed).encode()
                    result = guest_credentials(self.provider, encoded)
                    if result["expires_at"] is not None and result["expires_at"] <= self.clock():
                        raise _failure("credential_refresh_invalid", transient=True)
                except ProviderSetupError:
                    if self._read() != raw:
                        original, force = raw, False
                        continue
                    raise
                try:
                    if self._replace(raw, renewed):
                        return result
                except OSError:
                    raise _failure("credential_persist_unavailable", transient=True) from None
                original, force = raw, False
        raise _failure("credential_refresh_conflict", transient=True)
