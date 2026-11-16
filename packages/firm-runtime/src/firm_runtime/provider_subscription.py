"""Explicit, lazy account subscription capabilities, separate from execution."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .provider_contract import SubscriptionPollResult, SubscriptionWindow


@dataclass(frozen=True)
class SubscriptionSupport:
    stream: bool
    poll: Callable[[str | Path], SubscriptionPollResult] | None = None
    stream_source: str | None = None
    poll_source: str | None = None
    project_windows: Callable[[list[SubscriptionWindow]], list[SubscriptionWindow]] | None = None


def subscription_support(provider: str) -> SubscriptionSupport:
    if provider == "claude":
        from .claude_subscription import poll_subscription, stream_windows

        return SubscriptionSupport(
            True,
            poll_subscription,
            "Claude role stream",
            "Claude subscription usage",
            stream_windows,
        )
    if provider == "codex":
        from .codex_subscription import poll_subscription, stream_windows

        return SubscriptionSupport(
            True, poll_subscription, "Codex role stream", "Codex subscription usage", stream_windows
        )
    if provider == "agy":
        return SubscriptionSupport(False)
    raise ValueError("Unknown subscription provider")
