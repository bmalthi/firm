"""Host-brokered admission, credential lease, and billing settlement for guest LLM CLIs."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from uuid import uuid4

from . import agy_launch, analysis_history, llm_cli
from .accounts import llm_settings
from .credential_store import DEFAULT_MARGIN, CredentialStore
from .errors import FirmError
from .execution_activity import record_activity
from .prompt_resources import prompt
from .provider_contract import ProviderSetupError, incident_code
from .provider_definitions import (
    PROVIDER_DEFINITIONS,
    PROVIDER_NAMES,
    resolve_model,
)
from .provider_registry import provider_implementation
from .role_output import _controls, _mail_waiting
from .summaries import SummaryService

LEASE_TTL_SECONDS = 90.0
MAX_LLM_LOG_BYTES = 512 * 1024
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
VALID_FINISH_REASONS = frozenset({"timeout", "cancelled", "launcher_setup", "provider_failure"})


class LlmService:
    """Manage admission, heartbeats, and usage accounting for guest CLI invocations."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    @staticmethod
    def enabled_clis(config: dict) -> list[str]:
        if "accounts" not in config or not config.get("llm", {}).get("enabled", True):
            return []
        accounts = config["accounts"]
        return [
            provider
            for provider in PROVIDER_NAMES
            if any(
                isinstance(account, dict) and account.get("provider") == provider
                for account in accounts.values()
            )
        ]

    @staticmethod
    def launch_settings(
        llm_url: str,
    ) -> tuple[dict[str, str], dict[str, bytes], dict[str, str]]:
        return (
            {"FIRM_LLM_URL": llm_url},
            {
                "llm-cli": Path(llm_cli.__file__).read_bytes(),
                "llm-instructions": (prompt("runtime_llm.md") + "\n").encode("utf-8"),
            },
            {},
        )

    def _sweep_expired(self, db, now: float) -> None:
        db.execute(
            "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
            "reason=COALESCE(reason,'timeout') "
            "WHERE kind='llm' AND state='running' "
            "AND ((deadline IS NOT NULL AND deadline<=?) "
            "OR (lease_until IS NOT NULL AND lease_until<=?))",
            (now, now, now),
        )
        db.execute(
            "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
            "reason=COALESCE(reason,'cancelled') "
            "WHERE kind='llm' AND state='running' AND caller_activation_id IN ("
            "SELECT a.activation_id FROM activations a "
            "LEFT JOIN sandbox_runs s USING(activation_id) "
            "WHERE a.state!='running' OR s.stop_cause IS NOT NULL "
            "OR (s.deadline IS NOT NULL AND s.deadline<=?)"
            ")",
            (now, now),
        )

    def _authenticate_parent(self, db, token: str, now: float) -> dict:
        activation = self.runtime._authenticate(db, token)
        sandbox_run = db.execute(
            "SELECT stop_cause,deadline FROM sandbox_runs WHERE activation_id=?",
            (activation["activation_id"],),
        ).fetchone()
        if sandbox_run is not None and (
            sandbox_run["stop_cause"] is not None or sandbox_run["deadline"] <= now
        ):
            raise FirmError("invalid or expired activation token")
        return activation

    def start(
        self,
        token: str,
        provider: str,
        *,
        effort: str | None = None,
        resumed: bool = False,
    ) -> dict:
        if not isinstance(provider, str) or provider not in PROVIDER_DEFINITIONS:
            raise FirmError("unsupported provider")
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise FirmError("effort must be a non-empty string")
        if type(resumed) is not bool:
            raise FirmError("resumed must be a boolean")

        reservation = None
        with self.store.transaction() as db:
            now = self.store.clock()
            activation = self._authenticate_parent(db, token, now)
            self._sweep_expired(db, now)

            if self.store.firm(db)["state"] != "running":
                return {"status": "unavailable", "reason": "firm_stopping"}

            config = self.runtime.accounts.config(db)
            settings = llm_settings(config.get("llm", {}))
            if not settings["enabled"]:
                return {"status": "unavailable", "reason": "llm_disabled"}

            accounts_cfg = config.get("accounts")
            if not isinstance(accounts_cfg, dict):
                return {
                    "status": "unavailable",
                    "reason": "provider_not_configured",
                    "configured": [],
                }
            matching = [
                (index, account_id, account)
                for index, (account_id, account) in enumerate(accounts_cfg.items())
                if isinstance(account, dict) and account.get("provider") == provider
            ]
            if not matching:
                return {
                    "status": "unavailable",
                    "reason": "provider_not_configured",
                    "configured": self.enabled_clis(config),
                }

            provider_settings = settings[provider]
            model = resolve_model(provider, provider_settings["model"])
            selected_effort = effort if effort is not None else provider_settings["effort"]
            supported = [
                item
                for item in EFFORT_ORDER
                if item in PROVIDER_DEFINITIONS[provider].supported_efforts
            ]
            if selected_effort not in PROVIDER_DEFINITIONS[provider].supported_efforts:
                return {
                    "status": "unavailable",
                    "reason": "invalid_effort",
                    "supported": supported,
                }

            if provider == "agy":
                from . import agy_provider

                try:
                    agy_provider.native_model_id(model, selected_effort)
                except ProviderSetupError as exc:
                    self.runtime.accounts.report_llm_setup(db, provider, model, exc.code)
                    return {"status": "unavailable", "reason": "configuration"}

            active_role_calls = db.execute(
                "SELECT COUNT(*) FROM summary_runs t "
                "JOIN activations a ON a.activation_id=t.caller_activation_id "
                "WHERE t.kind='llm' AND t.state='running' AND a.employee_id=?",
                (activation["employee_id"],),
            ).fetchone()[0]
            if active_role_calls >= settings["max_concurrent"]:
                return {
                    "status": "unavailable",
                    "reason": "busy",
                    "max_concurrent": settings["max_concurrent"],
                }

            unheld = []
            for index, account_id, account in matching:
                self.runtime.accounts._refresh_quota_threshold(db, account_id, account)
                if self.runtime.accounts._incident(db, "account", account_id) is None:
                    unheld.append((index, account_id, account))
            if not unheld:
                return {"status": "unavailable", "reason": "hold"}

            binding = self.runtime.accounts.binding(db, activation["session_id"])
            caller_bound_account = binding["account_id"] if binding else None
            candidates = []
            for index, account_id, account in unheld:
                can_borrow = (
                    caller_bound_account == account_id
                    and db.execute(
                        "SELECT 1 FROM summary_runs WHERE kind='llm' AND state='running' "
                        "AND reason IS NULL AND caller_activation_id=? AND account_id=?",
                        (activation["activation_id"], account_id),
                    ).fetchone()
                    is None
                )
                projected = self.runtime.accounts._usage(db, account_id) + (0 if can_borrow else 1)
                if projected <= account["max_active"]:
                    candidates.append(
                        (
                            not can_borrow,
                            self.runtime.accounts._low_quota(db, account_id, account),
                            projected / account["max_active"],
                            index,
                            account_id,
                            account,
                        )
                    )
            if not candidates:
                return {"status": "unavailable", "reason": "capacity"}

            *_, selected_account_id, selected_account = min(candidates)
            run_id = "llm-" + uuid4().hex
            run_token = secrets.token_urlsafe(32)
            timeout = settings["timeout_seconds"]
            deadline = now + timeout
            lease_until = min(deadline, now + LEASE_TTL_SECONDS)
            profile_name = (
                binding["profile"]
                if binding and binding["account_id"] == selected_account_id
                else None
            )
            db.execute(
                "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,profile,"
                "provider,model,deadline,lease_until,token_hash,provider_started_at,kind,"
                "caller_activation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    now,
                    "running",
                    0,
                    selected_account_id,
                    profile_name,
                    provider,
                    model,
                    deadline,
                    lease_until,
                    hashlib.sha256(run_token.encode("utf-8")).hexdigest(),
                    now,
                    "llm",
                    activation["activation_id"],
                ),
            )
            analysis_history.summary_bound(
                db,
                now,
                run_id,
                activation["session_id"],
                {
                    "account_id": selected_account_id,
                    "profile": profile_name,
                    "provider": provider,
                    "model": model,
                    "effort": selected_effort,
                },
                activation_id=activation["activation_id"],
            )
            reservation = (
                run_id,
                run_token,
                selected_account_id,
                selected_account,
                model,
                selected_effort,
                timeout,
                activation["activation_id"],
            )

        run_id, run_token, account_id, account, model, selected_effort, timeout, aid = reservation
        margin = agy_launch.STARTUP_MARGIN + 1 if provider == "agy" else DEFAULT_MARGIN
        try:
            snapshot = CredentialStore(provider, account["auth_file"]).snapshot(margin=margin)
        except ProviderSetupError as exc:
            refusal_reason = "configuration" if exc.category == "config" else "provider_failure"
            with self.store.transaction() as db:
                now = self.store.clock()
                db.execute(
                    "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
                    "reason=? WHERE run_id=? AND kind='llm' AND state='running'",
                    (now, refusal_reason, run_id),
                )
                if exc.category == "auth":
                    self.runtime.accounts.hold(db, "account", account_id, "auth", code=exc.code)
                elif exc.category == "config":
                    self.runtime.accounts.report_llm_setup(db, provider, model, exc.code)
            return {"status": "unavailable", "reason": refusal_reason}

        record_activity(self.store, aid)
        return {
            "status": "granted",
            "run_id": run_id,
            "run_token": run_token,
            "provider": provider,
            "model": model,
            "effort": selected_effort,
            "timeout_seconds": timeout,
            "credentials": snapshot,
        }

    def heartbeat(self, run_token: str, *, progress: bool = False) -> dict:
        if not isinstance(run_token, str) or not 1 <= len(run_token) <= 256:
            raise FirmError("invalid llm run token")
        if type(progress) is not bool:
            raise FirmError("progress must be a boolean")
        digest = hashlib.sha256(run_token.encode("utf-8")).hexdigest()
        caller_activation_id = None
        result = None

        with self.store.transaction() as db:
            now = self.store.clock()
            row = db.execute(
                "SELECT run_id,state,deadline,caller_activation_id FROM summary_runs "
                "WHERE token_hash=? AND kind='llm'",
                (digest,),
            ).fetchone()
            if row is None:
                raise FirmError("invalid llm run token")
            if row["state"] != "running":
                return {"status": "stop", "reason": "cancelled"}

            firm_state = self.store.firm(db)["state"]
            parent = db.execute(
                "SELECT a.state,s.stop_cause,s.deadline FROM activations a "
                "LEFT JOIN sandbox_runs s USING(activation_id) WHERE a.activation_id=?",
                (row["caller_activation_id"],),
            ).fetchone()
            parent_live = (
                parent is not None
                and parent["state"] == "running"
                and parent["stop_cause"] is None
                and (parent["deadline"] is None or parent["deadline"] > now)
            )
            if firm_state not in {"running", "draining"} or not parent_live:
                db.execute(
                    "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
                    "reason='cancelled' WHERE run_id=? AND kind='llm' AND state='running'",
                    (now, row["run_id"]),
                )
                return {"status": "stop", "reason": "cancelled"}
            if row["deadline"] <= now:
                db.execute(
                    "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
                    "reason='timeout' WHERE run_id=? AND kind='llm' AND state='running'",
                    (now, row["run_id"]),
                )
                return {"status": "stop", "reason": "timeout"}

            new_lease = min(row["deadline"], now + LEASE_TTL_SECONDS)
            db.execute(
                "UPDATE summary_runs SET lease_until=? WHERE run_id=? AND kind='llm'",
                (new_lease, row["run_id"]),
            )
            caller_activation_id = row["caller_activation_id"]
            result = {"status": "ok", "remaining_seconds": max(0.0, row["deadline"] - now)}

        if progress and caller_activation_id is not None:
            record_activity(self.store, caller_activation_id)
        assert result is not None
        return result

    def finish(
        self,
        run_token: str,
        *,
        exit_code: int,
        logs: str | bytes,
        reason: str | None = None,
        resumed: bool = False,
    ) -> dict:
        if not isinstance(run_token, str) or not 1 <= len(run_token) <= 256:
            raise FirmError("invalid llm run token")
        if type(exit_code) is not int:
            raise FirmError("exit_code must be an integer")
        if isinstance(logs, str):
            encoded_len = len(logs.encode("utf-8", errors="replace"))
        elif isinstance(logs, bytes):
            encoded_len = len(logs)
        else:
            raise FirmError("logs must be str or bytes")
        if encoded_len > MAX_LLM_LOG_BYTES:
            raise FirmError("logs exceed 512 KiB limit")
        if reason is not None and reason not in VALID_FINISH_REASONS:
            raise FirmError("invalid finish reason")
        if type(resumed) is not bool:
            raise FirmError("resumed must be a boolean")

        digest = hashlib.sha256(run_token.encode("utf-8")).hexdigest()
        with self.store.read() as db:
            row = db.execute(
                "SELECT * FROM summary_runs WHERE token_hash=? AND kind='llm'", (digest,)
            ).fetchone()
            if row is None:
                raise FirmError("invalid llm run token")
            if row["state"] != "running":
                return {"status": "already_settled"}
            row = dict(row)

        parser = provider_implementation(row["provider"])
        parsed = parser.parse_result(logs, exit_code)
        observed = parser.parse_usage(logs)
        reading = parser.parse_subscription(logs)
        if reason is None and reading is not None and row["account_id"]:
            self.runtime.accounts.record_subscription(
                row["account_id"],
                list(reading.windows),
                status=reading.status,
                warning=reading.warning,
            )

        counters = dict(observed.__dict__) if observed else {}
        if not resumed:
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                if counters.get(name) is None:
                    counters[name] = counters.get("session_" + name)
        usage = SummaryService._safe_usage(counters) or {}

        caller_running = False
        with self.store.transaction() as db:
            current = db.execute(
                "SELECT state,started_at,caller_activation_id FROM summary_runs "
                "WHERE run_id=? AND kind='llm'",
                (row["run_id"],),
            ).fetchone()
            if current is None or current["state"] != "running":
                return {"status": "already_settled"}
            now = self.store.clock()
            latency_ms = max(0, round((now - current["started_at"]) * 1000))

            if reason is not None:
                state, final_reason = "unavailable", reason
            elif parsed.error is not None:
                error = parsed.error
                rejected_setup = error.category == "config" or error.code == "model_quota_reached"
                if rejected_setup:
                    self.runtime.accounts.report_llm_setup(
                        db, row["provider"], row["model"], incident_code(error.code)
                    )
                    final_reason = "configuration"
                else:
                    if error.category in {"auth", "quota"} and row["account_id"]:
                        limits = []
                        if error.reset_at is not None:
                            limits.append(error.reset_at)
                        if error.retry_after_seconds is not None:
                            limits.append(now + error.retry_after_seconds)
                        until = max(limits) if limits else None
                        self.runtime.accounts.hold(
                            db,
                            "account",
                            row["account_id"],
                            error.category,
                            until if error.category == "quota" else None,
                            code=error.code,
                        )
                    final_reason = "provider_failure"
                state = "unavailable"
            else:
                state, final_reason = "summarized", None

            db.execute(
                "UPDATE summary_runs SET finished_at=?,state=?,output_tokens=?,"
                "latency_ms=?,reason=?,lease_until=NULL,"
                "input_tokens=COALESCE(?,input_tokens),"
                "cached_input_tokens=COALESCE(?,cached_input_tokens),"
                "output_token_usage=COALESCE(?,output_token_usage),"
                "reasoning_output_tokens=COALESCE(?,reasoning_output_tokens),"
                "provider_context_window=COALESCE(?,provider_context_window),"
                "native_compactions=COALESCE(?,native_compactions),"
                "resolved_model=COALESCE(?,resolved_model) "
                "WHERE run_id=? AND kind='llm' AND state='running'",
                (
                    now,
                    state,
                    usage.get("output_tokens"),
                    latency_ms,
                    final_reason,
                    usage.get("input_tokens"),
                    usage.get("cached_input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("reasoning_output_tokens"),
                    usage.get("provider_context_window"),
                    usage.get("native_compactions"),
                    usage.get("resolved_model"),
                    row["run_id"],
                ),
            )
            parent = db.execute(
                "SELECT state FROM activations WHERE activation_id=?",
                (current["caller_activation_id"],),
            ).fetchone()
            caller_running = parent is not None and parent["state"] == "running"

        controls_notice = ""
        if caller_running:
            record_activity(self.store, row["caller_activation_id"])
            try:
                presented = self.runtime.present_controls(row["caller_activation_id"], {})
                controls_notice = "\n".join(_controls(presented) + _mail_waiting(presented))
            except FirmError:
                controls_notice = ""

        return {
            "status": "completed" if state == "summarized" else "unavailable",
            "reason": final_reason,
            "session_id": parsed.provider_session_id,
            "controls_notice": controls_notice,
        }

    def settle_activation(self, db, activation_id: str, *, reason: str = "cancelled") -> int:
        now = self.store.clock()
        return db.execute(
            "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
            "reason=COALESCE(reason,?) "
            "WHERE caller_activation_id=? AND kind='llm' AND state='running'",
            (now, reason, activation_id),
        ).rowcount

    def settle_all(self, db, *, reason: str = "cancelled") -> int:
        now = self.store.clock()
        return db.execute(
            "UPDATE summary_runs SET state='unavailable',finished_at=?,lease_until=NULL,"
            "reason=COALESCE(reason,?) WHERE kind='llm' AND state='running'",
            (now, reason),
        ).rowcount
