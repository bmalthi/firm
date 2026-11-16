"""Display-only subscription readings; never writes account admission observations."""

import threading
import time

from .provider_subscription import subscription_support
from .subscription_http import unavailable

POLL_SECONDS = 300


class QuotaObserver:
    """A console-owned background poller; HTTP reads only copy its in-memory cache."""

    def __init__(self, accounts, *, fetchers=None, clock=time.time):
        self.accounts, self.clock = accounts, clock
        self.fetchers = fetchers
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
            support = subscription_support(provider)
            if support.poll is None:
                continue
            fetch = support.poll if self.fetchers is None else self.fetchers.get(provider)
            if fetch is None:
                continue
            checked_at = self.clock()
            try:
                reading = fetch(account["auth_file"])
            except Exception as exc:
                reading = unavailable(exc)
            with self._lock:
                previous = self._cache.get(account_id, {})
                if reading.outcome in ("stream_only", "unsupported_auth"):
                    self._cache[account_id] = {
                        "polled": False,
                        "windows": [],
                        "poll_outcome": reading.outcome,
                    }
                    continue
                error = reading.reason
                self._cache[account_id] = {
                    "polled": True,
                    "source": support.poll_source,
                    "checked_at": checked_at,
                    "observed_at": previous.get("observed_at") if error else self.clock(),
                    "windows": previous.get("windows", [])
                    if error
                    else [dict(window) for window in reading.windows],
                    "error": error,
                    "poll_outcome": reading.outcome,
                }
        with self._lock:
            self._cache = {key: value for key, value in self._cache.items() if key in accounts}

    def snapshot(self, account_id, stream=None):
        """Select display evidence without changing the cache or durable stream."""
        provider = self.accounts().get(account_id, {}).get("provider")
        support = subscription_support(provider)
        if not support.stream and support.poll is None:
            return {
                "state": "unsupported",
                "windows": [],
                "polled": False,
                "capability": {"stream": False, "poll": "unsupported"},
            }
        with self._lock:
            value = self._cache.get(account_id)
            if value is not None:
                value = {**value, "windows": [dict(window) for window in value["windows"]]}
        capability = {
            "stream": support.stream,
            "poll": value.pop("poll_outcome", "pending") if value is not None else "pending",
        }
        if stream is not None and (
            value is None or not value["windows"] or stream["observed_at"] >= value["observed_at"]
        ):
            windows = support.project_windows(stream["windows"])
            if not windows and value is not None and value["windows"]:
                # Only the existing poll cache can supply a last recorded reading.
                # Keep its source, observation and refresh evidence unchanged.
                result = value
                result["unavailable_stream"] = {
                    "source": support.stream_source,
                    "observed_at": stream["observed_at"],
                }
            else:
                result = {
                    "polled": False,
                    "source": support.stream_source,
                    "checked_at": stream["observed_at"],
                    "observed_at": stream["observed_at"],
                    "windows": windows,
                    "error": None,
                }
        elif value is None:
            return {"state": "loading", "windows": [], "capability": capability}
        elif not value["polled"]:
            return {
                "state": "awaiting_stream"
                if capability["poll"] == "stream_only"
                else "unavailable",
                "windows": [],
                "capability": capability,
            }
        else:
            result = value
        result["capability"] = capability
        now = self.clock()
        result["state"] = (
            "unavailable"
            if not result["windows"]
            else "stale"
            if result.get("unavailable_stream")
            or result["error"]
            or now - result["observed_at"] > POLL_SECONDS * 2
            else "current"
        )
        for window in result["windows"]:
            window["expired"] = window["resets_at"] <= now
        return result
