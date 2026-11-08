"""Display-only subscription readings; never writes account admission observations."""

import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .claude_provider import _read_auth as _read_claude_auth
from .codex_provider import _read_auth as _read_codex_auth
from .credential_store import open_without_redirects

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
POLL_SECONDS = 300
WINDOWS = {
    "five_hour": "Five-hour allowance",
    "seven_day": "Weekly allowance",
    "seven_day_opus": "Weekly Opus allowance",
    "seven_day_sonnet": "Weekly Sonnet allowance",
}


def _read_quota(url, headers):
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with open_without_redirects(request, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Quota response exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid quota response")
    return data


def claude_quota(auth_file):
    """Read the same subscription endpoint used by Claude Code, with bounded I/O.

    Returns None for a setup token: its inference-only scope is refused by this
    endpoint, so that account's allowance comes only from Claude role streams.
    """
    kind, raw = _read_claude_auth(Path(auth_file))
    if kind == "oauth-token":
        return None
    token = json.loads(raw).get("claudeAiOauth", {}).get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Claude OAuth token unavailable")
    data = _read_quota(
        CLAUDE_USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    windows = []
    for key, label in WINDOWS.items():
        value = data.get(key)
        if not isinstance(value, dict):
            continue
        used = value.get("utilization")
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        try:
            reset = datetime.fromisoformat(value["resets_at"].replace("Z", "+00:00"))
            if reset.tzinfo is None:
                continue
            resets_at = reset.timestamp()
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            continue
        windows.append(
            {"window": key, "label": label, "used_percent": used, "resets_at": resets_at}
        )
    if not windows:
        raise ValueError("No supported subscription readings")
    return windows


def codex_quota(auth_file):
    """Read Codex's subscription endpoint without refreshing or changing credentials."""
    auth = json.loads(_read_codex_auth(Path(auth_file)))
    if auth.get("auth_mode") == "apikey" or auth.get("OPENAI_API_KEY"):
        raise ValueError("Codex subscription token unavailable for API-key authentication")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Codex subscription token unavailable")
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Codex subscription token unavailable")
    headers = {"Authorization": f"Bearer {token}"}
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-Id"] = account_id
    data = _read_quota(CODEX_USAGE_URL, headers)
    # Sol/Astra use the standard allowance; separate product buckets do not apply.
    limits = data.get("rate_limit")
    if not isinstance(limits, dict):
        raise ValueError("No supported subscription readings")
    windows = []
    for key in ("primary_window", "secondary_window"):
        value = limits.get(key)
        if not isinstance(value, dict):
            continue
        used, reset = value.get("used_percent"), value.get("reset_at")
        duration = value.get("limit_window_seconds")
        if (
            type(used) not in (int, float)
            or not math.isfinite(used)
            or not 0 <= used <= 100
            or type(reset) not in (int, float)
            or not math.isfinite(reset)
            or not 0 < reset <= 253402300799
            or type(duration) is not int
            or duration <= 0
        ):
            continue
        duration_label = {18000: "Five-hour", 604800: "Weekly"}.get(duration)
        if duration_label is None:
            duration_label = (
                f"{duration // 60}-minute" if duration % 60 == 0 else f"{duration}-second"
            )
        windows.append(
            {
                "window": f"codex:{key}",
                "label": f"{duration_label} allowance",
                "used_percent": used,
                "resets_at": reset,
            }
        )
    if not windows:
        raise ValueError("No supported subscription readings")
    return windows


class QuotaObserver:
    """A console-owned background poller; HTTP reads only copy its in-memory cache."""

    def __init__(self, accounts, *, fetchers=None, clock=time.time):
        self.accounts, self.clock = accounts, clock
        self.fetchers = (
            fetchers
            if fetchers is not None
            else {
                "claude": claude_quota,
                "codex": codex_quota,
            }
        )
        self._cache = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="firm-quota-observer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                # Missing config/storage cannot affect console or firm execution.
                pass
            self._stop.wait(POLL_SECONDS)

    def refresh(self):
        accounts = self.accounts()
        for account_id, account in accounts.items():
            if self._stop.is_set():
                return
            provider = account.get("provider")
            if provider == "agy":
                continue  # No supported quota endpoint; never poll AGY.
            fetch = self.fetchers.get(provider)
            if fetch is None:
                continue
            checked_at = self.clock()
            windows = error = None
            try:
                windows = fetch(account["auth_file"])
            except urllib.error.HTTPError as exc:
                error = (
                    "rate_limited"
                    if exc.code == 429
                    else (
                        "authentication_unavailable"
                        if exc.code in (401, 403)
                        else "provider_unavailable"
                    )
                )
            except Exception:
                error = "reading_unavailable"
            with self._lock:
                previous = self._cache.get(account_id, {})
                if windows is None and error is None:
                    self._cache[account_id] = {"polled": False, "windows": []}
                    continue
                self._cache[account_id] = {
                    "polled": True,
                    "source": f"{provider.capitalize()} subscription usage",
                    "checked_at": checked_at,
                    "observed_at": previous.get("observed_at") if error else self.clock(),
                    "windows": previous.get("windows", []) if error else windows,
                    "error": error,
                }
        with self._lock:
            self._cache = {key: value for key, value in self._cache.items() if key in accounts}

    def snapshot(self, account_id, stream=None):
        """Show the newer of the polled reading and the latest role-stream reading."""
        if self.accounts().get(account_id, {}).get("provider") == "agy":
            return {"state": "unsupported", "windows": [], "polled": False}
        with self._lock:
            value = self._cache.get(account_id)
            if value is not None:
                value = {**value, "windows": [dict(window) for window in value["windows"]]}
        if stream is not None and (
            value is None or not value["windows"] or stream["observed_at"] >= value["observed_at"]
        ):
            result = {
                "polled": False,
                "source": "Claude role stream",
                "checked_at": stream["observed_at"],
                "observed_at": stream["observed_at"],
                "windows": [
                    {**window, "label": WINDOWS[window["window"]]}
                    for window in stream["windows"]
                    if window["window"] in WINDOWS
                ],
                "error": None,
            }
        elif value is None:
            return {"state": "loading", "windows": []}
        elif not value["polled"]:
            return {"state": "awaiting_stream", "windows": []}
        else:
            result = value
        now = self.clock()
        result["state"] = (
            "unavailable"
            if not result["windows"]
            else "stale"
            if result["error"] or now - result["observed_at"] > POLL_SECONDS * 2
            else "current"
        )
        for window in result["windows"]:
            window["expired"] = window["resets_at"] <= now
        return result
