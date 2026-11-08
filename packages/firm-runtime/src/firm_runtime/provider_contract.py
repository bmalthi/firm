"""Small shared contracts for sandbox provider adapters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

FailureCategory = Literal[
    "refusal",
    "auth",
    "config",
    "quota",
    "transient",
    "unknown",
]
ErrorKind = Literal[
    "activation_gate",
    "launcher_setup",
    "turn_failed",
    "result_error",
    "process_exit",
    "invalid_output",
]
SetupCategory = Literal["auth", "config", "transient"]

MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SUPPORTED_EFFORTS = {
    # Owner mapping: Firm xhigh selects native Argon high, never native xhigh.
    "agy": frozenset({"low", "medium", "high", "xhigh"}),
    "codex": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
}

_CATEGORIES: dict[str, FailureCategory] = {
    "rate_limit_exceeded": "quota",
    "rate_limit_error": "quota",
    "rate_limit": "quota",
    "usage_limit_reached": "quota",
    "insufficient_quota": "quota",
    "quota_exceeded": "quota",
    "credit_balance_too_low": "quota",
    "out_of_credits": "quota",
    "overloaded_error": "transient",
    "capacity_exceeded": "transient",
    "server_overloaded": "transient",
    "service_unavailable": "transient",
    "transport_error": "transient",
    "connection_error": "transient",
    "connection_timeout": "transient",
    "timeout": "transient",
    "request_timeout": "transient",
    "authentication_error": "auth",
    "authentication_failed": "auth",
    "invalid_api_key": "auth",
    "unauthenticated": "auth",
    "unauthorized": "auth",
    "permission_error": "auth",
    "permission_denied": "auth",
    "forbidden": "auth",
    "access_denied": "auth",
    "refusal": "refusal",
    "policy_refusal": "refusal",
    "content_filter": "refusal",
    "model_not_found": "config",
    "invalid_model": "config",
    # Anthropic's own wire error for a typo'd, renamed, retired, or org-inaccessible
    # model: HTTP 404, error.type "not_found_error" (message starts "model: <id>").
    "not_found_error": "config",
    "configuration_error": "config",
    "sandbox_error": "config",
}


@dataclass(frozen=True)
class ProviderError:
    """Bounded evidence safe to retain in host diagnostics."""

    kind: ErrorKind
    category: FailureCategory
    message: str
    exit_code: int
    code: str | None = None
    retry_after_seconds: float | int | None = None
    reset_at: float | None = None
    # The model the run was launched with. Set by the provider adapter instance
    # (which knows its own configured model), never guessed from provider text.
    model: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    provider_session_id: str | None
    success: bool
    final_text: str | None
    error: ProviderError | None
    usage: ProviderUsage | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class ProviderUsage:
    """Provider-reported facts; nullable fields remain unknown rather than estimated."""

    current_context_tokens: int | None = None
    high_water_context_tokens: int | None = None
    provider_context_window: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    session_input_tokens: int | None = None
    session_cached_input_tokens: int | None = None
    session_output_tokens: int | None = None
    session_reasoning_output_tokens: int | None = None
    native_compactions: int | None = None
    source: str = "unknown"
    resolved_model: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "current_context_tokens",
            "high_water_context_tokens",
            "provider_context_window",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "session_input_tokens",
            "session_cached_input_tokens",
            "session_output_tokens",
            "session_reasoning_output_tokens",
            "native_compactions",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or unknown")
            if name == "provider_context_window" and value == 0:
                raise ValueError("provider_context_window must be positive or unknown")
        if not isinstance(self.source, str) or not SAFE_CODE.fullmatch(self.source):
            raise ValueError("usage source must be a bounded safe token")
        if self.resolved_model is not None and (
            not isinstance(self.resolved_model, str) or not self.resolved_model.strip()
        ):
            raise ValueError("resolved_model must be a non-empty string or unknown")


class ProviderSetupError(ValueError):
    """A safe, scoped configuration failure detected before sandbox launch."""

    category: SetupCategory
    code: str

    def __init__(self, *, category: SetupCategory, code: str, message: str) -> None:
        if category not in {"auth", "config", "transient"}:
            raise ValueError("provider setup category must be auth, config or transient")
        if not isinstance(code, str) or not SAFE_CODE.fullmatch(code):
            raise ValueError("provider setup code must be a bounded safe token")
        super().__init__(message)
        self.category = category
        self.code = code


def summary_error(result: ProviderResult, exit_code: int) -> str | None:
    """The short reason an isolated summary or chooser turn failed, or None.

    A completed turn with no text is left to the summary service's own
    ``empty_summary`` check; ``provider_error`` remains only when the provider
    reported failure without a structured error kind. A process that exited
    non-zero without a valid turn (for example a CLI refusing to start before
    it prints any event) is ``process_exit`` rather than ``invalid_output``.
    """
    if result.success:
        return None
    if result.error is None:
        return "provider_error"
    if result.error.kind == "invalid_output" and exit_code != 0:
        return "process_exit"
    return result.error.kind


def safe_code(value: object) -> str | None:
    if isinstance(value, str) and SAFE_CODE.fullmatch(value):
        return value
    return None


def tool_name(value: object) -> str:
    """A tool's own name, bounded like an error code. Anything else is counted as ``other``."""
    return safe_code(value) or "other"


def token_count(mapping: Mapping[str, object] | None, key: str) -> int | None:
    """Accept only explicit, non-negative integral provider counters."""
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return value if type(value) is int and value >= 0 else None


def category_for_code(code: str | None) -> FailureCategory:
    return _CATEGORIES.get((code or "").lower(), "unknown")


def retry_after_seconds(*mappings: Mapping[str, object]) -> float | int | None:
    """Read only explicit structured relative retry fields."""
    for mapping in mappings:
        for key in ("retry_after", "retry-after", "retry_after_seconds"):
            parsed = _bounded_delay(mapping.get(key))
            if parsed is not None:
                return parsed
        milliseconds = _bounded_number(mapping.get("retry_after_ms"))
        if milliseconds is not None:
            parsed = milliseconds / 1000
            if 0 <= parsed <= MAX_RETRY_AFTER_SECONDS:
                return int(parsed) if parsed.is_integer() else parsed
    return None


def reset_at(*mappings: Mapping[str, object]) -> float | None:
    """Read only explicit structured absolute reset timestamps."""
    for mapping in mappings:
        for key in ("reset_at", "resets_at", "resetAt", "resetsAt"):
            value = mapping.get(key)
            number = _bounded_number(value)
            if number is not None and number > 0:
                return number
            if isinstance(value, str) and len(value) <= 64:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is not None:
                    try:
                        timestamp = parsed.timestamp()
                    except (OSError, OverflowError):
                        continue
                    if timestamp > 0:
                        return timestamp
    return None


def _bounded_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and 0 < len(value.strip()) <= 30:
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _bounded_delay(value: object) -> float | int | None:
    number = _bounded_number(value)
    if number is None or not 0 <= number <= MAX_RETRY_AFTER_SECONDS:
        return None
    return int(number) if number.is_integer() else number
