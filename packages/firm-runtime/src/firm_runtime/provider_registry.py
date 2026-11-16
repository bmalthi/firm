"""Lazy implementation lookup; metadata consumers use provider_definitions only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import FirmError
from .provider_contract import (
    ExecutionProvider,
    ProviderResult,
    ProviderUsage,
    SubscriptionObservation,
)

if TYPE_CHECKING:
    from .summaries import SummaryOutput


@dataclass(frozen=True)
class ProviderImplementation:
    constructor: Callable[..., ExecutionProvider]
    parse_result: Callable[[bytes | str, int], ProviderResult]
    parse_usage: Callable[[bytes | str], ProviderUsage | None]
    parse_summary: Callable[[bytes | str, int], SummaryOutput]
    parse_subscription: Callable[[bytes | str], SubscriptionObservation | None]


def provider_implementation(name: str) -> ProviderImplementation:
    """Select one built-in without importing unselected native implementations."""
    if name == "claude":
        from . import claude_provider as module

        constructor = module.ClaudeProvider
    elif name == "codex":
        from . import codex_provider as module

        constructor = module.CodexProvider
    elif name == "agy":
        from . import agy_provider as module

        constructor = module.AgyProvider
    else:
        raise FirmError("unsupported provider in retained binding")
    return ProviderImplementation(
        constructor=constructor,
        parse_result=module.parse_result,
        parse_usage=module.parse_usage,
        parse_summary=constructor.parse_summary,
        parse_subscription=module.parse_subscription,
    )
