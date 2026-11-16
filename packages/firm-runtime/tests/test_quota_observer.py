import io
import json
import urllib.error
from copy import deepcopy

import pytest

from firm_runtime import claude_subscription, codex_subscription, quota_observer, subscription_http
from firm_runtime.claude_subscription import poll_subscription as claude_quota
from firm_runtime.codex_provider import parse_subscription
from firm_runtime.codex_subscription import poll_subscription as codex_quota
from firm_runtime.provider_contract import SubscriptionPollResult
from firm_runtime.quota_observer import QuotaObserver


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
            assert request.full_url == claude_subscription.USAGE_URL
            assert request.get_header("Authorization") == "Bearer private-token"
            assert timeout == 5
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(subscription_http.urllib.request, "build_opener", lambda *_: Opener())
    windows = claude_quota(auth).windows
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

    monkeypatch.setattr(subscription_http.urllib.request, "build_opener", lambda *_: Opener())
    assert claude_quota(auth) == SubscriptionPollResult("unavailable", reason="reading_unavailable")


def test_claude_setup_token_never_polls_the_usage_endpoint(tmp_path, monkeypatch):
    auth = tmp_path / "token"
    auth.write_text("sk-ant-oat01-private")
    auth.chmod(0o600)

    def unexpected_request(*_):
        pytest.fail("a setup token cannot read the subscription usage endpoint")

    monkeypatch.setattr(claude_subscription, "read_quota", unexpected_request)
    assert claude_quota(auth) == SubscriptionPollResult("stream_only")


def test_stream_only_account_waits_for_a_role_stream_reading():
    now = [100.0]
    observer = QuotaObserver(
        lambda: {"claude": {"provider": "claude", "auth_file": "private"}},
        fetchers={"claude": lambda _: SubscriptionPollResult("stream_only")},
        clock=lambda: now[0],
    )
    observer.refresh()
    assert observer.snapshot("claude") == {
        "state": "awaiting_stream",
        "windows": [],
        "capability": {"stream": True, "poll": "stream_only"},
    }
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
    assert reading["source"] == "Claude role stream"
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
            "claude": lambda _: SubscriptionPollResult(
                "reading",
                (
                    {
                        "window": "five_hour",
                        "label": "Five-hour allowance",
                        "used_percent": 5,
                        "resets_at": 1000,
                    },
                ),
            )
        },
        clock=lambda: 100,
    )
    observer.refresh()
    window = {"window": "five_hour", "used_percent": 9.0, "resets_at": 1000}
    older = observer.snapshot("claude", {"observed_at": 50, "windows": [window]})
    assert older["polled"] is True and older["windows"][0]["used_percent"] == 5
    newer = observer.snapshot("claude", {"observed_at": 100, "windows": [window]})
    assert newer["polled"] is False and newer["windows"][0]["used_percent"] == 9.0


@pytest.mark.parametrize(
    "limit_id,bucket", [("codex", "codex"), ("codex_other", "codex_other"), (None, "unknown")]
)
@pytest.mark.parametrize("observed_at", [100, 110])
def test_codex_stream_preserves_allowances_when_as_new_as_poll(limit_id, bucket, observed_at):
    now = [100]
    fetch_calls = []

    def fetch(path):
        fetch_calls.append(path)
        return SubscriptionPollResult(
            "reading",
            (
                {
                    "window": "codex:primary_window",
                    "label": "Weekly allowance",
                    "used_percent": 5,
                    "resets_at": 1000,
                },
            ),
        )

    observer = QuotaObserver(
        lambda: {"account": {"provider": "codex", "auth_file": "private"}},
        fetchers={"codex": fetch},
        clock=lambda: now[0],
    )
    observer.refresh()
    limits = {
        "primary": {"used_percent": 37, "resets_at": 105},
        "secondary": {"used_percent": 82, "resets_at": 1000},
    }
    if limit_id is not None:
        limits["limit_id"] = limit_id
    parsed = parse_subscription(
        json.dumps({"type": "firm.telemetry", "provider": "codex", "rate_limits": limits})
    )
    stream = {"windows": list(parsed.windows), "observed_at": observed_at}
    original_stream = json.dumps(stream, sort_keys=True)
    now[0] = 110
    reading = observer.snapshot("account", stream)
    assert reading["state"] == "current"
    assert reading["polled"] is False
    assert reading["source"] == "Codex role stream"
    assert reading["checked_at"] == reading["observed_at"] == observed_at
    assert reading["error"] is None
    assert reading["windows"] == [
        {
            "window": f"{bucket}_primary",
            "limit_id": limit_id,
            "used_percent": 37.0,
            "resets_at": 105.0,
            "label": f"{bucket} primary allowance",
            "expired": True,
        },
        {
            "window": f"{bucket}_secondary",
            "limit_id": limit_id,
            "used_percent": 82.0,
            "resets_at": 1000.0,
            "label": f"{bucket} secondary allowance",
            "expired": False,
        },
    ]
    assert json.dumps(stream, sort_keys=True) == original_stream
    reading["windows"][0]["used_percent"] = 0
    assert observer.snapshot("account", stream)["windows"][0]["used_percent"] == 37
    assert observer.snapshot("account")["windows"][0]["used_percent"] == 5
    older = observer.snapshot("account", {**stream, "observed_at": 99})
    assert older["polled"] is True
    assert older["source"] == "Codex subscription usage"
    assert older["windows"][0]["used_percent"] == 5
    now[0] = observed_at + quota_observer.POLL_SECONDS * 2 + 1
    assert observer.snapshot("account", stream)["state"] == "stale"
    assert fetch_calls == ["private"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("observed_at", [100, 101])
@pytest.mark.parametrize(
    "windows", [[], [{"window": "unlisted", "used_percent": 9, "resets_at": 200}]]
)
def test_unusable_stream_keeps_existing_poll_as_stale_without_mutation(
    provider, observed_at, windows
):
    now = [100]
    calls = []

    def fetch(_):
        calls.append("fetch")
        return SubscriptionPollResult(
            "reading",
            (
                {
                    "window": "polled",
                    "label": "Polled allowance",
                    "used_percent": 5,
                    "resets_at": 105,
                },
            ),
        )

    observer = QuotaObserver(
        lambda: {"account": {"provider": provider, "auth_file": "private"}},
        fetchers={provider: fetch},
        clock=lambda: now[0],
    )
    observer.refresh()
    cache = deepcopy(observer._cache)
    stream = {"windows": windows, "observed_at": observed_at}
    original_stream = deepcopy(stream)
    now[0] = 110
    reading = observer.snapshot("account", stream)
    assert reading["state"] == "stale"
    assert reading["polled"] is True
    assert reading["source"] == f"{provider.capitalize()} subscription usage"
    assert reading["checked_at"] == reading["observed_at"] == 100
    assert reading["error"] is None
    assert reading["windows"] == [{**cache["account"]["windows"][0], "expired": True}]
    assert reading["unavailable_stream"] == {
        "source": f"{provider.capitalize()} role stream",
        "observed_at": observed_at,
    }
    assert stream == original_stream
    assert observer._cache == cache
    reading["windows"][0]["used_percent"] = 99
    reading["unavailable_stream"]["observed_at"] = 999
    assert observer.snapshot("account", stream)["windows"][0]["used_percent"] == 5
    assert observer.snapshot("account", stream)["unavailable_stream"]["observed_at"] == observed_at
    older = observer.snapshot("account", {**stream, "observed_at": 99})
    assert older["state"] == "current"
    assert "unavailable_stream" not in older
    now[0] = 120
    observer.refresh()
    newer = observer.snapshot("account", stream)
    assert newer["state"] == "current"
    assert newer["observed_at"] == newer["checked_at"] == 120
    assert "unavailable_stream" not in newer
    assert calls == ["fetch", "fetch"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("poll_outcome", [None, "unavailable", "stream_only", "unsupported_auth"])
def test_empty_stream_without_usable_poll_stays_unavailable(provider, poll_outcome):
    observer = QuotaObserver(
        lambda: {"account": {"provider": provider, "auth_file": "private"}},
        fetchers={
            provider: lambda _: SubscriptionPollResult(poll_outcome, reason="reading_unavailable")
        },
        clock=lambda: 100,
    )
    if poll_outcome is not None:
        observer.refresh()
    reading = observer.snapshot("account", {"windows": [], "observed_at": 101})
    assert reading["state"] == "unavailable"
    assert reading["windows"] == []
    assert reading["polled"] is False
    assert reading["source"] == f"{provider.capitalize()} role stream"
    assert reading["observed_at"] == reading["checked_at"] == 101
    assert "unavailable_stream" not in reading


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_empty_stream_fallback_preserves_failed_poll_evidence(provider):
    now = [100]

    def fetch(_):
        if now[0] > 100:
            return SubscriptionPollResult("unavailable", reason="rate_limited")
        return SubscriptionPollResult(
            "reading",
            ({"window": "polled", "used_percent": 5, "resets_at": 1000},),
        )

    observer = QuotaObserver(
        lambda: {"account": {"provider": provider, "auth_file": "private"}},
        fetchers={provider: fetch},
        clock=lambda: now[0],
    )
    observer.refresh()
    now[0] = 150
    observer.refresh()
    cache = deepcopy(observer._cache)
    reading = observer.snapshot("account", {"windows": [], "observed_at": 125})
    assert reading["state"] == "stale"
    assert reading["polled"] is True
    assert reading["source"] == f"{provider.capitalize()} subscription usage"
    assert reading["error"] == "rate_limited"
    assert reading["checked_at"] == 150
    assert reading["observed_at"] == 100
    assert reading["unavailable_stream"]["observed_at"] == 125
    assert reading["capability"]["poll"] == "unavailable"
    assert observer._cache == cache


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_observer_keeps_last_reading_on_failure_and_expires_windows(provider):
    now = [100.0]
    calls = []

    def fetch(path):
        calls.append(path)
        if len(calls) > 1:
            raise urllib.error.HTTPError("private", 429, "private error", {}, None)
        return SubscriptionPollResult(
            "reading", ({"window": "five_hour", "used_percent": 5, "resets_at": 200},)
        )

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
            "claude": lambda _: SubscriptionPollResult(
                "reading", ({"window": "five_hour", "used_percent": 100, "resets_at": 200},)
            )
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
        subscription_http.read_quota(
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
            assert request.full_url == codex_subscription.USAGE_URL
            assert request.get_header("Authorization") == "Bearer private-token"
            assert request.get_header("Chatgpt-account-id") == "private-account"
            assert timeout == 5
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(subscription_http.urllib.request, "build_opener", lambda *_: Opener())
    windows = codex_quota(auth).windows
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
        codex_subscription, "read_quota", lambda *_: {"rate_limit": {"primary_window": value}}
    )
    assert codex_quota(_codex_auth(tmp_path)) == SubscriptionPollResult(
        "unavailable", reason="reading_unavailable"
    )


def test_codex_api_key_is_not_sent_to_subscription_endpoint(tmp_path, monkeypatch):
    def unexpected_request(*_):
        pytest.fail("API key must not be used for a subscription request")

    monkeypatch.setattr(codex_subscription, "read_quota", unexpected_request)
    assert codex_quota(_codex_auth(tmp_path, {"OPENAI_API_KEY": "private-api-key"})) == (
        SubscriptionPollResult("unsupported_auth")
    )


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
            "codex": lambda _: SubscriptionPollResult(
                "reading", ({"window": "primary", "used_percent": 100, "resets_at": 200},)
            ),
        },
        clock=lambda: 100,
    )
    observer.refresh()
    assert observer.snapshot("a")["error"] == "authentication_unavailable"
    assert observer.snapshot("b")["state"] == "current"
    assert observer.snapshot("b")["windows"][0]["used_percent"] == 100
    with runtime.store.read() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("status,reason", [
    (401, "authentication_unavailable"), (403, "authentication_unavailable"),
    (429, "rate_limited"), (500, "provider_unavailable"),
])
def test_provider_poll_failures_are_bounded(tmp_path, monkeypatch, provider, status, reason):
    module = claude_subscription if provider == "claude" else codex_subscription
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "private-token"}}
            if provider == "claude"
            else {"tokens": {"access_token": "private-token"}}
        )
    )
    auth.chmod(0o600)

    def fail(*_):
        raise urllib.error.HTTPError("private-url", status, "private response", {}, None)

    monkeypatch.setattr(module, "read_quota", fail)
    result = module.poll_subscription(auth)
    assert result == SubscriptionPollResult("unavailable", reason=reason)
    assert "private" not in repr(result)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("content", ['{"bad"', "{}"])
def test_invalid_credentials_are_unavailable_without_http(tmp_path, monkeypatch, provider, content):
    module = claude_subscription if provider == "claude" else codex_subscription
    auth = tmp_path / "auth.json"
    auth.write_text(content)
    auth.chmod(0o600)
    calls = []
    monkeypatch.setattr(module, "read_quota", lambda *args: calls.append(args))
    assert module.poll_subscription(auth) == SubscriptionPollResult(
        "unavailable", reason="reading_unavailable"
    )
    assert calls == []


def test_auth_mode_is_reevaluated_on_each_refresh(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("sk-ant-oat01-private")
    auth.chmod(0o600)
    observer = QuotaObserver(
        lambda: {"a": {"provider": "claude", "auth_file": str(auth)}}, clock=lambda: 100,
    )
    observer.refresh()
    assert observer.snapshot("a")["capability"]["poll"] == "stream_only"
    auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "private-token"}}))
    monkeypatch.setattr(claude_subscription, "read_quota", lambda *_: {
        "five_hour": {"utilization": 25, "resets_at": "2030-01-01T00:00:00Z"},
    })
    observer.refresh()
    assert observer.snapshot("a")["capability"]["poll"] == "reading"
    auth.write_text("sk-ant-oat01-private")
    observer.refresh()
    assert observer.snapshot("a")["state"] == "awaiting_stream"
    assert observer.snapshot("a")["windows"] == []


def test_codex_api_key_capability_is_distinct_from_failed_refresh(tmp_path):
    auth = _codex_auth(tmp_path, {"OPENAI_API_KEY": "private-api-key"})
    observer = QuotaObserver(lambda: {"a": {"provider": "codex", "auth_file": str(auth)}})
    observer.refresh()
    value = observer.snapshot("a")
    assert value == {
        "state": "unavailable", "windows": [],
        "capability": {"stream": True, "poll": "unsupported_auth"},
    }


def test_agy_has_no_poll_callable_or_credential_read_even_with_injected_fetcher():
    from firm_runtime.provider_subscription import subscription_support

    assert subscription_support("agy").poll is None
    calls = []
    observer = QuotaObserver(
        lambda: {"a": {"provider": "agy"}},
        fetchers={"agy": lambda *_: calls.append("called")},
    )
    observer.refresh()
    assert observer.snapshot("a") == {
        "state": "unsupported", "windows": [], "polled": False,
        "capability": {"stream": False, "poll": "unsupported"},
    }
    assert calls == []


@pytest.mark.parametrize("age,state", [(599.999, "current"), (600, "current"), (600.001, "stale")])
def test_display_freshness_exact_boundary_and_snapshot_has_no_io(age, state):
    now = [1000]
    calls = []
    original = {"window": "five_hour", "used_percent": 5, "resets_at": 3000}

    def fetch(_):
        calls.append("fetch")
        return SubscriptionPollResult("reading", (original,))

    observer = QuotaObserver(
        lambda: {"a": {"provider": "claude", "auth_file": "/unused"}},
        fetchers={"claude": fetch},
        clock=lambda: now[0],
    )
    observer.refresh()
    original["used_percent"] = 99
    now[0] += age
    assert observer.snapshot("a")["state"] == state
    assert observer.snapshot("a")["windows"][0]["used_percent"] == 5
    assert calls == ["fetch"]
    assert "expired" not in original
