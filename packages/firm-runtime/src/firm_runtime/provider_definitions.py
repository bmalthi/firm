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
    llm: ModelSettings
    staffing_low: ModelSettings
    staffing_medium: ModelSettings
    staffing_high: ModelSettings
    staffing_chooser: ModelSettings
    soft_context_window: int | None = None


PROVIDER_DEFINITIONS: Mapping[str, ProviderDefinition] = MappingProxyType({
    "claude": ProviderDefinition(
        name="claude",
        supported_efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
        onboarding=ModelSettings("sonnet", "medium"),
        summary=ModelSettings("sonnet", "medium"),
        llm=ModelSettings("opus", "medium"),
        staffing_low=ModelSettings("sonnet", "high"),
        staffing_medium=ModelSettings("opus", "high"),
        staffing_high=ModelSettings("fable", "high"),
        staffing_chooser=ModelSettings("sonnet", "medium"),
        soft_context_window=500_000,
    ),
    "codex": ProviderDefinition(
        name="codex",
        supported_efforts=frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
        onboarding=ModelSettings("gpt-6.1-terra", "low"),
        summary=ModelSettings("gpt-6.1-terra", "medium"),
        llm=ModelSettings("gpt-6.2-sol", "medium"),
        staffing_low=ModelSettings("gpt-6.1-terra", "high"),
        staffing_medium=ModelSettings("gpt-6.2-sol", "high"),
        staffing_high=ModelSettings("gpt-6.2-astra", "medium"),
        staffing_chooser=ModelSettings("gpt-6.1-terra", "medium"),
        soft_context_window=256_000,
    ),
    "agy": ProviderDefinition(
        name="agy",
        # Firm xhigh selects native Carbon high, never native xhigh.
        supported_efforts=frozenset({"low", "medium", "high", "xhigh"}),
        onboarding=ModelSettings("gemini-4-neon", "medium"),
        summary=ModelSettings("gemini-4-hydrogen", "medium"),
        llm=ModelSettings("gemini-4-neon", "medium"),
        staffing_low=ModelSettings("gemini-4-hydrogen", "medium"),
        staffing_medium=ModelSettings("gemini-4-neon", "medium"),
        staffing_high=ModelSettings("gemini-4-neon", "high"),
        staffing_chooser=ModelSettings("gemini-4-hydrogen", "low"),
        soft_context_window=500_000,
    ),
})

MODEL_ALIASES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "claude": MappingProxyType({
        "low": "sonnet",
        "medium": "opus",
        "high": "fable",
    }),
    "codex": MappingProxyType({
        "terra": "gpt-6.1-terra",
        "sol": "gpt-6.2-sol",
        "astra": "gpt-6.2-astra",
        "low": "gpt-6.1-terra",
        "medium": "gpt-6.2-sol",
        "high": "gpt-6.2-astra",
    }),
    "agy": MappingProxyType({
        "hydrogen": "gemini-4-hydrogen",
        "neon": "gemini-4-neon",
        "carbon": "gemini-4-carbon",
        "argon": "gemini-4-argon",
        "low": "gemini-4-hydrogen",
        "medium": "gemini-4-neon",
        "high": "gemini-4-neon",
    }),
})

# Preserve the established command-line choice order.
PROVIDER_NAMES = tuple(PROVIDER_DEFINITIONS)


def resolve_model(provider: str | None, model: str | None) -> str | None:
    """Map a Firm family or class alias to the provider's model identifier."""
    if not isinstance(provider, str) or not isinstance(model, str):
        return model
    return MODEL_ALIASES.get(provider, {}).get(model, model)


def effective_model(provider: str | None, model: str | None, effort: str | None) -> str | None:
    """The model a run really uses, and so whose allowance it draws on.

    Effort normally leaves the model alone. AGY's xhigh is the exception: it always
    selects Carbon, whatever model the profile names.
    """
    base = resolve_model(provider, model)
    return "gemini-4-carbon" if provider == "agy" and effort == "xhigh" else base
