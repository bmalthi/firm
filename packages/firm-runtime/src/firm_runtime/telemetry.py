"""Structured provider usage and role context pressure."""

from __future__ import annotations

import json
import math
from dataclasses import asdict

from . import analysis_history
from .errors import FirmError
from .prompt_resources import prompt, render
from .provider_contract import ProviderUsage
from .store import OWNER, encoded

DEFAULT_ADVISORY_FRACTION = 0.60
DEFAULT_HANDOFF_RESERVE = 10_000
DEFAULT_CONTEXT_WINDOWS = {"claude": 500_000, "agy": 500_000, "codex": 256_000}
CONTEXT_POLICY_KEYS = frozenset({"context_window", "context_advisory_fraction", "handoff_reserve"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_sessions (
    session_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    execution_profile TEXT NOT NULL,
    provider TEXT,
    soft_context_window INTEGER,
    advisory_fraction REAL NOT NULL,
    handoff_reserve INTEGER NOT NULL,
    current_context_tokens INTEGER,
    high_water_context_tokens INTEGER,
    provider_context_window INTEGER,
    cumulative_input_tokens INTEGER,
    cumulative_cached_input_tokens INTEGER,
    cumulative_output_tokens INTEGER,
    cumulative_reasoning_output_tokens INTEGER,
    native_compactions INTEGER,
    observed_at REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS context_sessions_by_employee
    ON context_sessions(employee_id, observed_at);
CREATE TABLE IF NOT EXISTS activation_usage (
    activation_id TEXT PRIMARY KEY REFERENCES activations(activation_id),
    session_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    current_context_tokens INTEGER,
    high_water_context_tokens INTEGER,
    provider_context_window INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    native_compactions INTEGER,
    observed_at REAL NOT NULL,
    source TEXT NOT NULL,
    resolved_model TEXT,
    last_presented_context_tokens INTEGER,
    last_presented_at REAL
);
CREATE TABLE IF NOT EXISTS activation_tool_calls (
    activation_id TEXT NOT NULL REFERENCES activations(activation_id),
    tool TEXT NOT NULL,
    calls INTEGER NOT NULL,
    PRIMARY KEY(activation_id, tool)
);
"""


def validate_context_policy(profile: dict, name: str) -> None:
    window = profile.get("context_window", DEFAULT_CONTEXT_WINDOWS.get(profile.get("provider")))
    fraction = profile.get("context_advisory_fraction", DEFAULT_ADVISORY_FRACTION)
    reserve = profile.get("handoff_reserve", DEFAULT_HANDOFF_RESERVE)
    if window is not None and (type(window) is not int or window <= 0):
        raise FirmError(f"profile {name} context_window must be a positive integer")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction)
        or not 0 < fraction < 1
    ):
        raise FirmError(f"profile {name} context_advisory_fraction must be between zero and one")
    if type(reserve) is not int or reserve <= 0:
        raise FirmError(f"profile {name} handoff_reserve must be a positive integer")
    if window is not None and reserve >= window:
        raise FirmError(f"profile {name} handoff_reserve must be smaller than context_window")


def context_policy(profile: dict) -> tuple[int | None, float, int]:
    return (
        profile.get("context_window", DEFAULT_CONTEXT_WINDOWS.get(profile.get("provider"))),
        float(profile.get("context_advisory_fraction", DEFAULT_ADVISORY_FRACTION)),
        profile.get("handoff_reserve", DEFAULT_HANDOFF_RESERVE),
    )


def handoff_checklist() -> str:
    return prompt("context-handoff.md")


def _maximum(old: int | None, new: int | None) -> int | None:
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


def _delta(old: int | None, new: int | None) -> int:
    if new is None:
        return 0
    return max(0, new - (old or 0))


class ContextTelemetry:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def ensure_session(self, db, role: dict, session_id: str) -> None:
        binding = self.runtime.accounts.binding(db, session_id)
        if binding:
            profile_name = binding["profile"]
            profile = json.loads(binding["settings_json"])
            provider = binding["provider"]
        else:
            profile_name = role["execution_profile"]
            profile = self.runtime.accounts.config(db)["profiles"][profile_name]
            provider = profile.get("provider")
        window, fraction, reserve = context_policy(profile)
        db.execute(
            "INSERT OR IGNORE INTO context_sessions(session_id,employee_id,execution_profile,"
            "provider,soft_context_window,advisory_fraction,handoff_reserve) VALUES(?,?,?,?,?,?,?)",
            (session_id, role["employee_id"], profile_name, provider, window, fraction, reserve),
        )

    def refresh_policies(self, db, config: dict) -> int:
        """Apply policy-only corrections to matching retained profile bindings."""
        changed = 0
        rows = list(db.execute("SELECT * FROM execution_bindings"))
        for row in rows:
            current = config["profiles"].get(row["profile"])
            if current is None:
                continue
            captured = json.loads(row["settings_json"])
            identity_keys = ("provider", "model", "effort")
            if any(captured.get(key) != current.get(key) for key in identity_keys):
                continue
            revised = dict(captured)
            for key in CONTEXT_POLICY_KEYS:
                if key in current:
                    revised[key] = current[key]
                else:
                    revised.pop(key, None)
            if revised != captured:
                db.execute(
                    "UPDATE execution_bindings SET settings_json=? WHERE session_id=?",
                    (encoded(revised), row["session_id"]),
                )
            window, fraction, reserve = context_policy(revised)
            result = db.execute(
                "UPDATE context_sessions SET soft_context_window=?,advisory_fraction=?,"
                "handoff_reserve=? WHERE session_id=? AND (soft_context_window IS NOT ? "
                "OR advisory_fraction!=? OR handoff_reserve!=?)",
                (window, fraction, reserve, row["session_id"], window, fraction, reserve),
            )
            changed += result.rowcount
            if result.rowcount:
                analysis_history.context_observed(
                    db, self.store.clock(), row["session_id"], "policy"
                )
        return changed

    def record(
        self, activation_id: str, usage: ProviderUsage | None, *, historical: bool = False,
    ) -> bool:
        """Historical collection updates only the activation, never the live session."""
        if usage is not None and not isinstance(usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage")
        with self.store.transaction() as db:
            activation = self.runtime._activation(db, activation_id)
            if not historical and activation["state"] not in {"admitted", "running"}:
                return False
            if usage is None:
                return False
            if not historical:
                role = self.store.role(db, activation["employee_id"])
                self.ensure_session(db, role, activation["session_id"])
                session = db.execute(
                    "SELECT * FROM context_sessions WHERE session_id=?", (activation["session_id"],)
                ).fetchone()
                assert session is not None
            old = db.execute(
                "SELECT * FROM activation_usage WHERE activation_id=?", (activation_id,)
            ).fetchone()
            values = asdict(usage)
            if values["resolved_model"] is None and old is not None:
                values["resolved_model"] = old["resolved_model"]
            comparable = (
                "current_context_tokens",
                "high_water_context_tokens",
                "provider_context_window",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "native_compactions",
                "source",
                "resolved_model",
            )
            session_unchanged = historical or all(
                provided is None or session[column] is not None and provided <= session[column]
                for column, provided in (
                    ("cumulative_input_tokens", usage.session_input_tokens),
                    ("cumulative_cached_input_tokens", usage.session_cached_input_tokens),
                    ("cumulative_output_tokens", usage.session_output_tokens),
                    ("cumulative_reasoning_output_tokens", usage.session_reasoning_output_tokens),
                )
            )
            if (
                old is not None
                and all(old[key] == values[key] for key in comparable)
                and session_unchanged
            ):
                return False
            now = (activation["finished_at"] if historical else None) or self.store.clock()
            db.execute(
                "INSERT INTO activation_usage(activation_id,session_id,employee_id,"
                "current_context_tokens,high_water_context_tokens,provider_context_window,"
                "input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,"
                "native_compactions,observed_at,source,resolved_model) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(activation_id) DO UPDATE SET "
                "current_context_tokens=excluded.current_context_tokens,"
                "high_water_context_tokens=excluded.high_water_context_tokens,"
                "provider_context_window=excluded.provider_context_window,"
                "input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,"
                "output_tokens=excluded.output_tokens,"
                "reasoning_output_tokens=excluded.reasoning_output_tokens,"
                "native_compactions=excluded.native_compactions,observed_at=excluded.observed_at,"
                "source=excluded.source,resolved_model=excluded.resolved_model",
                (
                    activation_id,
                    activation["session_id"],
                    activation["employee_id"],
                    usage.current_context_tokens,
                    usage.high_water_context_tokens,
                    usage.provider_context_window,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    usage.native_compactions,
                    now,
                    usage.source,
                    values["resolved_model"],
                ),
            )
            if historical:
                return True
            explicit = (
                usage.session_input_tokens,
                usage.session_cached_input_tokens,
                usage.session_output_tokens,
                usage.session_reasoning_output_tokens,
            )
            activation_fields = (
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
                usage.reasoning_output_tokens,
            )
            cumulative = []
            for column, provided, activation_value in zip(
                (
                    "cumulative_input_tokens",
                    "cumulative_cached_input_tokens",
                    "cumulative_output_tokens",
                    "cumulative_reasoning_output_tokens",
                ),
                explicit,
                activation_fields,
                strict=True,
            ):
                if provided is not None:
                    cumulative.append(_maximum(session[column], provided))
                elif activation_value is None:
                    cumulative.append(session[column])
                else:
                    old_value = old[column.removeprefix("cumulative_")] if old else None
                    cumulative.append((session[column] or 0) + _delta(old_value, activation_value))
            compactions = session["native_compactions"]
            if usage.native_compactions is not None:
                compactions = (compactions or 0) + _delta(
                    old["native_compactions"] if old else None,
                    usage.native_compactions,
                )
            db.execute(
                "UPDATE context_sessions SET current_context_tokens=?,"
                "high_water_context_tokens=?,provider_context_window=COALESCE(?,provider_context_window),"
                "cumulative_input_tokens=?,cumulative_cached_input_tokens=?,"
                "cumulative_output_tokens=?,cumulative_reasoning_output_tokens=?,"
                "native_compactions=?,observed_at=?,source=? WHERE session_id=?",
                (
                    usage.current_context_tokens,
                    _maximum(session["high_water_context_tokens"], usage.high_water_context_tokens),
                    usage.provider_context_window,
                    *cumulative,
                    compactions,
                    now,
                    usage.source,
                    activation["session_id"],
                ),
            )
            analysis_history.context_observed(db, now, activation["session_id"], "usage")
            return True

    def record_tool_calls(self, activation_id: str, tally: dict) -> None:
        """Names and counts of what a finished work period called, from its provider log.

        Part of terminal collection rather than an observation of runtime state, so it
        shares that failure behavior: a tally that silently rolled back is worse than none.
        """
        if not tally:
            return
        with self.store.transaction() as db:
            db.executemany(
                "INSERT INTO activation_tool_calls(activation_id,tool,calls) VALUES(?,?,?) "
                "ON CONFLICT(activation_id,tool) DO UPDATE SET calls=excluded.calls",
                [
                    (activation_id, tool, calls)
                    for tool, calls in sorted(tally.items())
                    if type(calls) is int and calls > 0
                ],
            )

    def gate_claim(self, activation_id: str) -> bool:
        with self.store.read() as db:
            activation = self.runtime._activation(db, activation_id)
            pressure = self._pressure(db, activation)
            return pressure is not None and pressure["level"] == "handoff_required"

    def guidance(self, db, activation: dict) -> dict | None:
        pressure = self._pressure(db, activation)
        if pressure is None:
            return None
        usage = db.execute(
            "SELECT * FROM activation_usage WHERE activation_id=?",
            (activation["activation_id"],),
        ).fetchone()
        prior = usage["last_presented_context_tokens"]
        pressure["change_since_previous_guidance"] = (
            pressure["current_context_tokens"] - prior if prior is not None else None
        )
        if pressure["level"] in {"advisory", "handoff_required"}:
            pressure["instruction"] = render(
                "controls", "saved" if activation["next_intent"] is not None else pressure["level"]
            )
        db.execute(
            "UPDATE activation_usage SET last_presented_context_tokens=?,last_presented_at=? "
            "WHERE activation_id=?",
            (
                pressure["current_context_tokens"],
                self.store.clock(),
                activation["activation_id"],
            ),
        )
        return pressure

    def _pressure(self, db, activation: dict) -> dict | None:
        usage = db.execute(
            "SELECT * FROM activation_usage WHERE activation_id=?",
            (activation["activation_id"],),
        ).fetchone()
        session = db.execute(
            "SELECT * FROM context_sessions WHERE session_id=?", (activation["session_id"],)
        ).fetchone()
        if usage is None or session is None or usage["current_context_tokens"] is None:
            return None
        window = session["soft_context_window"] or session["provider_context_window"]
        if window is None:
            return None
        current = usage["current_context_tokens"]
        gate = max(0, window - session["handoff_reserve"])
        advisory = math.ceil(window * session["advisory_fraction"])
        level = (
            "handoff_required"
            if current >= gate
            else "advisory"
            if current >= advisory
            else "normal"
        )
        waiting = db.execute(
            "SELECT COUNT(*) FROM message_deliveries WHERE employee_id=? AND disposition='pending'",
            (activation["employee_id"],),
        ).fetchone()[0]
        return {
            "level": level,
            "current_context_tokens": current,
            "soft_context_window": window,
            "window_source": (
                "configured_soft_window"
                if session["soft_context_window"] is not None
                else "provider_reported_native_window"
            ),
            "consumed_fraction": min(1.0, current / window),
            "remaining_soft_tokens": max(0, window - current),
            "handoff_at_tokens": gate,
            "session_high_water_tokens": session["high_water_context_tokens"],
            "native_compactions": session["native_compactions"],
            "provider_context_window": session["provider_context_window"],
            "measured_at": usage["observed_at"],
            "waiting_messages": waiting,
        }

    def diagnostics(
        self, actor_id: str, *, employee_id: str | None = None, limit: int = 100
    ) -> dict:
        if actor_id != OWNER:
            raise FirmError("only the owner may inspect provider usage diagnostics")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FirmError("usage diagnostics limit must be between 1 and 100")
        with self.store.read() as db:
            parameters: tuple = ()
            condition = ""
            if employee_id is not None:
                self.store.role(db, employee_id)
                condition = " WHERE employee_id=?"
                parameters = (employee_id,)
            session_count = db.execute(
                "SELECT COUNT(*) FROM context_sessions" + condition, parameters
            ).fetchone()[0]
            activation_count = db.execute(
                "SELECT COUNT(*) FROM activation_usage" + condition, parameters
            ).fetchone()[0]
            sessions = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM context_sessions"
                    + condition
                    + " ORDER BY COALESCE(observed_at,0) DESC,session_id LIMIT ?",
                    (*parameters, limit),
                )
            ]
            activations = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM activation_usage"
                    + condition
                    + " ORDER BY observed_at DESC,activation_id LIMIT ?",
                    (*parameters, limit),
                )
            ]
            return {
                "sessions": sessions,
                "activations": activations,
                "sessions_omitted": max(0, session_count - len(sessions)),
                "activations_omitted": max(0, activation_count - len(activations)),
            }
