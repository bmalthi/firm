"""Bounded, redirect-refusing transport for display-only allowance reads."""

import json
import urllib.error
import urllib.request

from .oauth_auth import open_without_redirects
from .provider_contract import SubscriptionPollResult


def read_quota(url, headers):
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with open_without_redirects(request, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Quota response exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid quota response")
    return data


def unavailable(exc: Exception) -> SubscriptionPollResult:
    """Retain only a fixed reason; provider responses and secrets never escape."""
    reason = "reading_unavailable"
    if isinstance(exc, urllib.error.HTTPError):
        reason = (
            "rate_limited"
            if exc.code == 429
            else "authentication_unavailable"
            if exc.code in (401, 403)
            else "provider_unavailable"
        )
    return SubscriptionPollResult("unavailable", reason=reason)
