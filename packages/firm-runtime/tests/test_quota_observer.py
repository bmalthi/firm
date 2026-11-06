import io
import json
import urllib.error

import pytest

from firm_runtime import quota_observer
from firm_runtime.quota_observer import QuotaObserver, claude_quota, codex_quota


def test_claude_quota_only_returns_allowlisted_subscription_fields(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "private-token"}}))
    auth.chmod(0o600)
    payload = {
        "five_hour": {"utilization": 5, "resets_at": "2030-01-01T00:00:00Z"},
        "seven_day": {"utilization": 85, "resets_at": "2030-01-02T00:00:00Z"},
        "seven_day_opus": {"utilization": True, "resets_at": "2030-01-02T00:00:00Z"},
        "secret": "must not escape",
    }

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == quota_observer.CLAUDE_USAGE_URL
            assert request.get_header("Authorization") == "Bearer private-token"
            assert timeout == 5
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(quota_observer.urllib.request, "build_opener", lambda *_: Opener())
    windows = claude_quota(auth)
    assert [w["used_percent"] for w in windows] == [5, 85]
    assert [w["window"] for w in windows] == ["five_hour", "seven_day"]
    assert "private-token" not in json.dumps(windows)
    assert "must not escape" not in json.dumps(windows)


@pytest.mark.parametrize("payload", [b"x" * 65537, b"[]", b"{}", b'{"five_hour": null}'])
def test_invalid_quota_is_unknown_not_zero(tmp_path, monkeypatch, payload):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "private-token"}}))
    auth.chmod(0o600)

    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(payload)

    monkeypatch.setattr(quota_observer.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(ValueError):
        claude_quota(auth)


def test_claude_setup_token_never_polls_the_usage_endpoint(tmp_path, monkeypatch):
    auth = tmp_path / "token"
    auth.write_text("sk-ant-oat01-private")
    auth.chmod(0o600)

    def unexpected_request(*_):
        pytest.fail("a setup token cannot read the subscription usage endpoint")

    monkeypatch.setattr(quota_observer, "_read_quota", unexpected_request)
    assert claude_quota(auth) is None


def test_stream_only_account_waits_for_a_role_stream_reading():
    now = [100.0]
    observer = QuotaObserver(
        lambda: {"claude": {"provider": "claude", "auth_file": "private"}},
        fetchers={"claude": lambda _: None},
        clock=lambda: now[0],
    )
    observer.refresh()
    assert observer.snapshot("claude") == {"state": "awaiting_stream", "windows": []}
    stream = {
        "observed_at": 90.0,
        "windows": [
            {"window": "five_hour", "used_percent": 4.0, "resets_at": 1000},
            {"window": "unlisted", "used_percent": 1.0, "resets_at": 1000},
        ],
    }
    reading = observer.snapshot("claude", stream)
    assert reading["state"] == "current"
    assert reading["polled"] is False
    assert reading["observed_at"] == 90.0
    assert reading["windows"] == [
        {
            "window": "five_hour",
            "used_percent": 4.0,
            "resets_at": 1000,
            "label": "Five-hour allowance",
            "expired": False,
        }
    ]
    now[0] = 90.0 + quota_observer.POLL_SECONDS * 2 + 1
    assert observer.snapshot("claude", stream)["state"] == "stale"


def test_newer_of_polled_and_stream_readings_is_shown():
    observer = QuotaObserver(
        lambda: {"claude": {"provider": "claude", "auth_file": "private"}},
        fetchers={
            "claude": lambda _: [
                {
                    "window": "five_hour",
                    "label": "Five-hour allowance",
                    "used_percent": 5,
                    "resets_at": 1000,
                }
            ]
        },
        clock=lambda: 100,
    )
    observer.refresh()
    window = {"window": "five_hour", "used_percent": 9.0, "resets_at": 1000}
    older = observer.snapshot("claude", {"observed_at": 50, "windows": [window]})
    assert older["polled"] is True and older["windows"][0]["used_percent"] == 5
    newer = observer.snapshot("claude", {"observed_at": 100, "windows": [window]})
    assert newer["polled"] is False and newer["windows"][0]["used_percent"] == 9.0


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_observer_keeps_last_reading_on_failure_and_expires_windows(provider):
    now = [100.0]
    calls = []

    def fetch(path):
        calls.append(path)
        if len(calls) > 1:
            raise urllib.error.HTTPError("private", 429, "private error", {}, None)
        return [{"window": "five_hour", "used_percent": 5, "resets_at": 200}]

    observer = QuotaObserver(
        lambda: {"claude": {"provider": provider, "auth_file": "private"}},
        fetchers={provider: fetch},
        clock=lambda: now[0],
    )
    assert observer.snapshot("claude")["state"] == "loading"
    observer.refresh()
    assert observer.snapshot("claude")["state"] == "current"
    now[0] = 250
    observer.refresh()
    reading = observer.snapshot("claude")
    assert reading["state"] == "stale"
    assert reading["observed_at"] == 100
    assert reading["checked_at"] == 250
    assert reading["windows"][0]["expired"]
    assert reading["error"] == "rate_limited"
    assert "private" not in json.dumps(reading)
    observer.snapshot("claude")["windows"].clear()
    assert len(observer.snapshot("claude")["windows"]) == 1
    assert len(calls) == 2  # Cached console reads never poll the provider.


def test_observer_cannot_change_admission_observations(tmp_path):
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store

    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "quota-test",
        "Work",
        {"max_active": 1, "staffing": False, "founder_profile": "p", "profiles": {"p": {}}},
    )
    with runtime.store.read() as db:
        before = list(db.iterdump())
    observer = QuotaObserver(
        lambda: {"claude": {"provider": "claude", "auth_file": "private"}},
        fetchers={
            "claude": lambda _: [{"window": "five_hour", "used_percent": 100, "resets_at": 200}]
        },
    )
    observer.refresh()
    assert observer.snapshot("claude")["windows"][0]["used_percent"] == 100
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


def test_quota_requests_do_not_forward_credentials_on_redirect(monkeypatch):
    # A quota endpoint that redirects must not carry the bearer token onward: the
    # request fails at the redirect and the target never sees it. Responses come
    # from urllib's own handler chain, so no socket is opened. The system proxy
    # lookup is skipped too: on macOS it starts native threads that outlive the
    # test, and a later test that forks a pseudo-terminal would inherit them.
    import http.client
    import urllib.request
    import urllib.response

    reached = []

    def response(body, headers, url, code, reason):
        reply = urllib.response.addinfourl(io.BytesIO(body), headers, url, code)
        reply.msg = reason
        return reply

    def http_open(self, request):
        headers = http.client.HTTPMessage()
        if request.host == "target.test":
            reached.append(request.get_header("Authorization"))
            return response(b"{}", headers, request.full_url, 200, "OK")
        headers["Location"] = "http://target.test/quota"
        return response(b"", headers, request.full_url, 302, "Found")

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    with pytest.raises(urllib.error.HTTPError) as refused:
        quota_observer._read_quota(
            "http://source.test/quota", {"Authorization": "Bearer private-token"}
        )
    assert refused.value.code == 302
    assert reached == []


def _codex_auth(tmp_path, content=None):
    auth = tmp_path / "codex-auth.json"
    auth.write_text(
        json.dumps(
            content
            or {"tokens": {"access_token": "private-token", "account_id": "private-account"}}
        )
    )
    auth.chmod(0o600)
    return auth


def test_codex_quota_uses_reported_duration_and_ignores_unrelated_buckets(tmp_path, monkeypatch):
    auth = _codex_auth(tmp_path)
    payload = {
        "email": "private-email",
        "rate_limit": {
            "primary_window": {
                "used_percent": 96,
                "limit_window_seconds": 604800,
                "reset_at": 2000000000,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": [
            {
                "metered_feature": "codex_spark",
                "limit_name": "Spark",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 0,
                        "limit_window_seconds": 18000,
                        "reset_at": 2000000001,
                    }
                },
            }
        ],
    }

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == quota_observer.CODEX_USAGE_URL
            assert request.get_header("Authorization") == "Bearer private-token"
            assert request.get_header("Chatgpt-account-id") == "private-account"
            assert timeout == 5
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(quota_observer.urllib.request, "build_opener", lambda *_: Opener())
    windows = codex_quota(auth)
    assert [w["label"] for w in windows] == ["Weekly allowance"]
    assert [w["used_percent"] for w in windows] == [96]
    assert [w["window"] for w in windows] == ["codex:primary_window"]
    assert "private" not in json.dumps(windows)


@pytest.mark.parametrize(
    "override",
    [
        {"used_percent": True},
        {"used_percent": -1},
        {"used_percent": 101},
        {"used_percent": float("nan")},
        {"reset_at": None},
        {"reset_at": float("inf")},
        {"reset_at": 253402300800},
        {"limit_window_seconds": True},
        {"limit_window_seconds": 0},
    ],
)
def test_codex_invalid_readings_are_unknown_not_zero(tmp_path, monkeypatch, override):
    value = {"used_percent": 10, "reset_at": 2000000000, "limit_window_seconds": 18000}
    value.update(override)
    monkeypatch.setattr(
        quota_observer, "_read_quota", lambda *_: {"rate_limit": {"primary_window": value}}
    )
    with pytest.raises(ValueError, match="No supported"):
        codex_quota(_codex_auth(tmp_path))


def test_codex_api_key_is_not_sent_to_subscription_endpoint(tmp_path, monkeypatch):
    def unexpected_request(*_):
        pytest.fail("API key must not be used for a subscription request")

    monkeypatch.setattr(quota_observer, "_read_quota", unexpected_request)
    with pytest.raises(ValueError, match="subscription token unavailable"):
        codex_quota(_codex_auth(tmp_path, {"OPENAI_API_KEY": "private-api-key"}))


def test_provider_polling_failure_is_isolated_and_never_changes_admission(tmp_path):
    from firm_runtime.runtime import Runtime
    from firm_runtime.store import Store

    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3"))
    runtime.lifecycle.initialize(
        "quota-test",
        "Work",
        {"max_active": 1, "staffing": False, "founder_profile": "p", "profiles": {"p": {}}},
    )
    with runtime.store.read() as db:
        before = list(db.iterdump())

    def unavailable(_):
        raise urllib.error.HTTPError("private", 401, "private error", {}, None)

    observer = QuotaObserver(
        lambda: {
            "a": {"provider": "claude", "auth_file": "private"},
            "b": {"provider": "codex", "auth_file": "private"},
        },
        fetchers={
            "claude": unavailable,
            "codex": lambda _: [{"window": "primary", "used_percent": 100, "resets_at": 200}],
        },
        clock=lambda: 100,
    )
    observer.refresh()
    assert observer.snapshot("a")["error"] == "authentication_unavailable"
    assert observer.snapshot("b")["state"] == "current"
    assert observer.snapshot("b")["windows"][0]["used_percent"] == 100
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before
