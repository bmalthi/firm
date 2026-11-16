"""Read-only, account-level Codex subscription capability."""

import json
import math
from pathlib import Path

from .codex_auth import read_auth
from .provider_contract import SubscriptionPollResult, SubscriptionWindow
from .subscription_http import read_quota, unavailable

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def stream_windows(windows: list[SubscriptionWindow]) -> list[SubscriptionWindow]:
    """Label opaque stream buckets by their reported slot, never guessed duration."""
    projected = []
    for window in windows:
        bucket, _, slot = window["window"].rpartition("_")
        if bucket and slot in ("primary", "secondary"):
            projected.append({**window, "label": f"{bucket} {slot} allowance"})
    return projected


def _read_subscription(auth_file) -> SubscriptionPollResult:
    """Read Codex's subscription endpoint without refreshing or changing credentials."""
    auth = json.loads(read_auth(Path(auth_file)))
    if auth.get("auth_mode") == "apikey" or auth.get("OPENAI_API_KEY"):
        return SubscriptionPollResult("unsupported_auth")
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
    data = read_quota(USAGE_URL, headers)
    # Sol/Astra use the standard allowance; separate product buckets do not apply.
    limits = data.get("rate_limit")
    if not isinstance(limits, dict):
        raise ValueError("No supported subscription readings")
    windows: list[SubscriptionWindow] = []
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
    return SubscriptionPollResult("reading", tuple(windows))


def poll_subscription(auth_file) -> SubscriptionPollResult:
    """Read current auth mode on every refresh; never renew credentials."""
    try:
        return _read_subscription(auth_file)
    except Exception as exc:
        return unavailable(exc)
