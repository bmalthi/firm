"""Read-only, account-level Claude subscription capability."""

import json
import math
from datetime import datetime
from pathlib import Path

from .claude_auth import read_auth
from .provider_contract import SubscriptionPollResult, SubscriptionWindow
from .subscription_http import read_quota, unavailable

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = {
    "five_hour": "Five-hour allowance",
    "seven_day": "Weekly allowance",
    "seven_day_opus": "Weekly Opus allowance",
    "seven_day_sonnet": "Weekly Sonnet allowance",
}


def stream_windows(windows: list[SubscriptionWindow]) -> list[SubscriptionWindow]:
    """Add display labels without changing the native stored observation."""
    return [
        {**window, "label": WINDOWS[window["window"]]}
        for window in windows
        if window["window"] in WINDOWS
    ]


def _read_subscription(auth_file) -> SubscriptionPollResult:
    """Read the same subscription endpoint used by Claude Code, with bounded I/O.

    Returns stream_only for a setup token: its inference-only scope is refused by this
    endpoint, so that account's allowance comes only from Claude role streams.
    """
    kind, raw = read_auth(Path(auth_file))
    if kind == "oauth-token":
        return SubscriptionPollResult("stream_only")
    token = json.loads(raw).get("claudeAiOauth", {}).get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Claude OAuth token unavailable")
    data = read_quota(
        USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    windows: list[SubscriptionWindow] = []
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
    return SubscriptionPollResult("reading", tuple(windows))


def poll_subscription(auth_file) -> SubscriptionPollResult:
    """Read current auth mode on every refresh; never renew credentials."""
    try:
        return _read_subscription(auth_file)
    except Exception as exc:
        return unavailable(exc)
