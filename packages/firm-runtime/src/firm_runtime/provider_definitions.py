"""Immutable configured defaults and constraints for Firm's built-in providers.

These defaults are not a model catalog or provider-reported context limits.
This module deliberately depends only on the standard library.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelSettings:
    model: str
    effort: str

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    supported_efforts: frozenset[str]
    onboarding: ModelSettings
    summary: ModelSettings
    task: ModelSettings
    staffing_low: ModelSettings
    staffing_medium: ModelSettings
    staffing_high: ModelSettings
    staffing_chooser: ModelSettings
    soft_context_window: int | None = None
    incompatible_services: tuple[str, ...] = ()


PROVIDER_DEFINITIONS: Mapping[str, ProviderDefinition] = MappingProxyType({
    "claude": ProviderDefinition(
        name="claude",
        supported_efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
        onboarding=ModelSettings("sonnet", "medium"),
        summary=ModelSettings("sonnet", "medium"),
        task=ModelSettings("opus", "medium"),
        staffing_low=ModelSettings("sonnet", "high"),
        staffing_medium=ModelSettings("opus", "high"),
        staffing_high=ModelSettings("claude-fable-5-1", "high"),
        staffing_chooser=ModelSettings("sonnet", "medium"),
        soft_context_window=500_000,
    ),
    "codex": ProviderDefinition(
        name="codex",
        supported_efforts=frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
        onboarding=ModelSettings("gpt-6-luna", "low"),
        summary=ModelSettings("gpt-6-luna", "medium"),
        task=ModelSettings("gpt-6.1-sol", "medium"),
        staffing_low=ModelSettings("gpt-6-luna", "high"),
        staffing_medium=ModelSettings("gpt-6.1-sol", "high"),
        staffing_high=ModelSettings("gpt-6-astra", "medium"),
        staffing_chooser=ModelSettings("gpt-6-luna", "medium"),
        soft_context_window=256_000,
    ),
    "agy": ProviderDefinition(
        name="agy",
        # Firm xhigh selects native Argon high, never native xhigh.
        supported_efforts=frozenset({"low", "medium", "high", "xhigh"}),
        onboarding=ModelSettings("gemini-4-hydrogen", "medium"),
        summary=ModelSettings("gemini-4-hydrogen", "medium"),
        task=ModelSettings("gemini-4-hydrogen", "high"),
        staffing_low=ModelSettings("gemini-4-hydrogen", "medium"),
        staffing_medium=ModelSettings("gemini-4-hydrogen", "high"),
        staffing_high=ModelSettings("gemini-4-argon", "medium"),
        staffing_chooser=ModelSettings("gemini-4-hydrogen", "low"),
        incompatible_services=("google_cloud",),
    ),
})

# Preserve the established command-line choice order.
PROVIDER_NAMES = tuple(PROVIDER_DEFINITIONS)


def effective_model(provider: str | None, model: str | None, effort: str | None) -> str | None:
    """The model a run really uses, and so whose allowance it draws on.

    Effort normally leaves the model alone. AGY's xhigh is the exception: it always
    selects Argon, whatever model the profile names.
    """
    return "gemini-4-argon" if provider == "agy" and effort == "xhigh" else model


def incompatible_service(provider: str | None, config: Mapping[str, object]) -> str | None:
    """Return a configured service forbidden by the provider's declared policy."""
    definition = PROVIDER_DEFINITIONS.get(provider)
    if definition is None:
        return None
    return next((name for name in definition.incompatible_services if config.get(name)), None)
