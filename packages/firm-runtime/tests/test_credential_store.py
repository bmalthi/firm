import base64
import http.client
import io
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from firm_runtime import credential_files, oauth_auth
from firm_runtime.credential_store import CredentialStore, guest_credentials
from firm_runtime.oauth_auth import refresh_credentials
from firm_runtime.provider_contract import ProviderSetupError


def jwt(expires):
    return (
        "header."
        + base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
        + ".signature"
    )


def bundle(provider, expires=1100, refresh="refresh-secret"):
    if provider == "claude":
        return {
            "claudeAiOauth": {
                "accessToken": "access-secret",
                "refreshToken": refresh,
                "expiresAt": expires * 1000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": jwt(expires),
            "refresh_token": refresh,
            "id_token": "identity-token",
            "account_id": "account",
        },
    }


def save(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_access_only_projection_and_fresh_snapshot_do_not_write(tmp_path, provider):
    path = tmp_path / "auth.json"
    value = bundle(provider, expires=10000)
    save(path, value)
    original = path.read_bytes()
    result = CredentialStore(provider, path, clock=lambda: 1000).snapshot()
    assert result["expires_at"] == 10000
    assert "refresh-secret" not in json.dumps(result)
    assert "refreshToken" not in json.dumps(result)
    if provider == "codex":
        assert result["data"]["tokens"]["refresh_token"] == ""
    assert path.read_bytes() == original
    assert not path.with_name("auth.json.lock").exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_refresh_once_for_concurrent_workers_and_persist_rotation(tmp_path, provider):
    path = tmp_path / "auth.json"
    save(path, bundle(provider))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def refresh(selected, document, now):
        assert selected == provider and now == 1000
        calls.append(document)
        entered.set()
        assert release.wait(5)
        return bundle(provider, expires=10000, refresh="rotated-secret")

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(
            CredentialStore(provider, path, clock=lambda: 1000, refresh=refresh).snapshot
        )
        assert entered.wait(5)
        others = [
            pool.submit(
                CredentialStore(provider, path, clock=lambda: 1000, refresh=refresh).snapshot
            )
            for _ in range(3)
        ]
        release.set()
        results = [first.result(), *(result.result() for result in others)]
    assert len(calls) == 1
    assert all(result["expires_at"] == 10000 for result in results)
    assert all("rotated-secret" not in json.dumps(result) for result in results)
    assert "rotated-secret" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("fails", [False, True])
def test_external_change_during_refresh_wins(tmp_path, provider, fails):
    path = tmp_path / "auth.json"
    save(path, bundle(provider))
    replacement = bundle(provider, expires=20000, refresh="external-secret")

    def refresh(*_):
        save(path, replacement)
        if fails:
            raise ProviderSetupError(
                category="auth", code="credential_refresh_rejected", message="safe"
            )
        return bundle(provider, expires=10000, refresh="stale-candidate")

    result = CredentialStore(provider, path, clock=lambda: 1000, refresh=refresh).snapshot()
    assert result["expires_at"] == 20000
    assert json.loads(path.read_text()) == replacement


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_external_change_just_before_commit_is_not_overwritten(tmp_path, provider, monkeypatch):
    path = tmp_path / "auth.json"
    save(path, bundle(provider))
    store = CredentialStore(
        provider, path, clock=lambda: 1000, refresh=lambda *_: bundle(provider, expires=10000)
    )
    original = store.files.replace
    replacement = bundle(provider, expires=20000, refresh="external-secret")

    def replace(expected, document):
        save(path, replacement)
        return original(expected, document)

    monkeypatch.setattr(store.files, "replace", replace)
    assert store.snapshot()["expires_at"] == 20000
    assert json.loads(path.read_text()) == replacement


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_refresh_failure_preserves_original_and_hides_secrets(tmp_path, provider, monkeypatch):
    path = tmp_path / "auth.json"
    save(path, bundle(provider))
    original = path.read_bytes()

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError("private-token", 401, "refresh-secret", {}, None)

    monkeypatch.setattr(oauth_auth.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore(provider, path, clock=lambda: 1000).snapshot()
    assert error.value.category == "auth"
    assert "secret" not in str(error.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_missing_refresh_does_not_launch_renewal(tmp_path, provider):
    path = tmp_path / "auth.json"
    save(path, guest_credentials(provider, json.dumps(bundle(provider)).encode())["data"])
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore(provider, path, clock=lambda: 1000).snapshot()
    assert error.value.code == "credential_refresh_missing"


def test_static_credentials_need_no_lock_or_refresh(tmp_path):
    raw = tmp_path / "token"
    raw.write_text("plain-token\n")
    raw.chmod(0o600)
    assert CredentialStore("claude", raw).snapshot(force=True) == {
        "kind": "oauth-token",
        "data": "plain-token",
        "expires_at": None,
    }
    auth = tmp_path / "auth.json"
    save(
        auth,
        {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": "api-key",
            "tokens": {"refresh_token": "unused-secret"},
        },
    )
    assert CredentialStore("codex", auth).snapshot(force=True) == {
        "kind": "codex-json",
        "data": {"auth_mode": "apikey", "OPENAI_API_KEY": "api-key"},
        "expires_at": None,
    }
    assert not list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("margin", [None, 0, 1000000])
def test_static_modes_never_lock_or_renew_for_any_snapshot_policy(
    tmp_path, monkeypatch, provider, force, margin
):
    path = tmp_path / "auth"
    if provider == "claude":
        path.write_text("  plain-token\n")
        path.chmod(0o600)
        expected = {"kind": "oauth-token", "data": "plain-token", "expires_at": None}
    else:
        save(path, {"OPENAI_API_KEY": "api-key", "tokens": {"refresh_token": "unused"}})
        expected = {
            "kind": "codex-json", "data": {"auth_mode": "apikey", "OPENAI_API_KEY": "api-key"},
            "expires_at": None,
        }
    original = path.read_bytes()
    store = CredentialStore(
        provider, path, clock=lambda: 1000,
        refresh=lambda *args: pytest.fail("static credentials must never renew"),
    )
    monkeypatch.setattr(store.files, "locked", lambda: pytest.fail("static credentials locked"))
    assert store.snapshot(force=force, margin=margin) == expected
    assert path.read_bytes() == original


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("api_key,renew", [
    (" ", False), (123, False), (True, False), (["unexpected"], False),
    ({"unexpected": "value"}, False),
    (None, True), ("", True), (0, True), (False, True), ([], True), ({}, True),
])
def test_codex_mixed_bundle_preserves_raw_api_key_renewal_decision(
    tmp_path, force, api_key, renew
):
    path = tmp_path / "auth.json"
    source = {**bundle("codex"), "OPENAI_API_KEY": api_key}
    save(path, source)
    original = path.read_bytes()
    calls = []

    def refresh(provider, document, now):
        assert provider == "codex" and document == source and now == 1000
        calls.append(document)
        return bundle("codex", expires=10000)

    result = CredentialStore("codex", path, clock=lambda: 1000, refresh=refresh).snapshot(force)
    # Projection accepts valid subscription tokens and discards the malformed API key.
    # The raw bundle's existing truthy-key rule still suppresses renewal under the lock.
    assert "OPENAI_API_KEY" not in result["data"]
    assert len(calls) == int(renew)
    assert result["expires_at"] == (10000 if renew else 1100)
    assert path.with_name("auth.json.lock").exists()
    if not renew:
        assert path.read_bytes() == original


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_refresh_endpoint_contract_and_preserved_identity(provider, monkeypatch):
    captured = []

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == {"claude": "https://platform.claude.com/v1/oauth/token",
                                        "codex": "https://auth.openai.com/oauth/token"}[provider]
            assert timeout == 15
            captured.append(json.loads(request.data))
            return io.BytesIO(
                json.dumps(
                    {
                        "access_token": jwt(10000),
                        "refresh_token": "rotated",
                        "expires_in": 9000,
                        "id_token": "new-id",
                    }
                ).encode()
            )

    monkeypatch.setattr(oauth_auth.urllib.request, "build_opener", lambda *_: Opener())
    result = refresh_credentials(provider, bundle(provider), 1000)
    assert captured[0]["grant_type"] == "refresh_token"
    assert captured[0]["refresh_token"] == "refresh-secret"
    assert guest_credentials(provider, json.dumps(result))["expires_at"] == 10000
    if provider == "codex":
        assert result["tokens"]["account_id"] == "account"
        assert result["tokens"]["id_token"] == "new-id"
    else:
        assert result["claudeAiOauth"]["subscriptionType"] == "max"


@pytest.mark.parametrize(
    "status,category",
    [(400, "auth"), (401, "auth"), (403, "auth"), (429, "transient"), (500, "transient")],
)
def test_refresh_errors_are_typed_and_sanitized(status, category, monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError("secret-url", status, "secret-message", {}, None)

    monkeypatch.setattr(oauth_auth.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(ProviderSetupError) as error:
        refresh_credentials("codex", bundle("codex"), 1000)
    assert error.value.category == category
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "failure", [http.client.IncompleteRead(b"secret"), http.client.BadStatusLine("secret")]
)
def test_broken_refresh_http_response_is_a_sanitized_transient_failure(monkeypatch, failure):
    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(oauth_auth, "open_without_redirects", broken)
    with pytest.raises(ProviderSetupError) as error:
        refresh_credentials("codex", bundle("codex"), 1000)
    assert error.value.category == "transient"
    assert error.value.code == "credential_refresh_unavailable"
    assert "secret" not in str(error.value)


def test_lock_symlink_is_rejected(tmp_path):
    auth = tmp_path / "auth.json"
    save(auth, bundle("claude"))
    target = tmp_path / "target"
    target.write_text("untouched")
    auth.with_name("auth.json.lock").symlink_to(target)
    with pytest.raises(ProviderSetupError):
        CredentialStore("claude", auth, clock=lambda: 1000).snapshot()
    assert target.read_text() == "untouched"


def test_refresh_transport_refuses_redirects():
    assert (
        oauth_auth._NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://other.test"
        )
        is None
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_force_renews_an_unexpired_bundle(tmp_path, provider):
    path = tmp_path / "auth.json"
    save(path, bundle(provider, expires=10000))
    calls = []

    def refresh(*args):
        calls.append(args)
        return bundle(provider, expires=20000)

    store = CredentialStore(provider, path, clock=lambda: 1000, refresh=refresh)
    assert store.snapshot(force=True)["expires_at"] == 20000
    assert len(calls) == 1


def test_failed_persistence_preserves_source_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    save(path, bundle("claude"))
    original = path.read_bytes()

    def fail(*_):
        raise OSError("private diagnostic")

    monkeypatch.setattr(credential_files.os, "replace", fail)
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore(
            "claude", path, clock=lambda: 1000, refresh=lambda *_: bundle("claude", expires=10000)
        ).snapshot()
    assert error.value.category == "transient"
    assert "private" not in str(error.value)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".firm-auth-*"))


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("margin,remaining,renew", [
    (None, -10, False), (None, 0, False), (None, 600, False),
    (0, 0, True), (0, 1, False), (300, 300, True), (300, 301, False),
    (600, 599, True), (600, 600, True), (600, 601, False),
    (0.5, 0.5, True), (0.5, 1, False),
])
def test_public_snapshot_margin_policy(tmp_path, provider, margin, remaining, renew):
    path = tmp_path / "auth.json"
    save(path, bundle(provider, expires=1000 + remaining))
    original = path.read_bytes()
    calls = []

    def refresh(*args):
        calls.append(args)
        return bundle(provider, expires=10000)

    result = CredentialStore(provider, path, clock=lambda: 1000, refresh=refresh).snapshot(
        margin=margin
    )
    assert bool(calls) is renew
    assert result["expires_at"] == (10000 if renew else 1000 + remaining)
    assert path.with_name("auth.json.lock").exists() is renew
    if not renew:
        assert path.read_bytes() == original


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_default_snapshot_renews_http_at_margin_equality(tmp_path, provider):
    path = tmp_path / "auth.json"
    save(path, bundle(provider, expires=1600))
    store = CredentialStore(provider, path, clock=lambda: 1000,
                            refresh=lambda *_: bundle(provider, expires=10000))
    assert store.snapshot()["expires_at"] == 10000


@pytest.mark.parametrize("margin", [None, 0, 600])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_force_overrides_explicit_snapshot_margin(tmp_path, provider, margin):
    path = tmp_path / "auth.json"
    save(path, bundle(provider, expires=10000))
    store = CredentialStore(provider, path, clock=lambda: 1000,
                            refresh=lambda *_: bundle(provider, expires=20000))
    assert store.snapshot(force=True, margin=margin)["expires_at"] == 20000


@pytest.mark.parametrize("margin", [-1, True, "600", float("nan"), float("inf"),
                                   float("-inf"), object(), 10**1000])
def test_invalid_margin_is_rejected_before_reading(tmp_path, margin):
    store = CredentialStore("claude", tmp_path / "missing")
    with pytest.raises(ValueError, match="finite non-negative number or None"):
        store.snapshot(margin=margin)
