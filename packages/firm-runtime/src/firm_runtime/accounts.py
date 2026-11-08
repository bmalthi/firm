"""Account admission, retained-session bindings and scoped operational holds."""

import hashlib
import json
import math
import re
from pathlib import Path
from uuid import uuid4

from .errors import FirmError
from .prompt_resources import render
from .provider_contract import SUPPORTED_EFFORTS
from .store import CONTROL_PRIORITY, OPERATIONS, ORDINARY_PRIORITY, OWNER, encoded
from .telemetry import CONTEXT_POLICY_KEYS

# Owner-private home for firm account credentials; an account without auth_file uses
# <CREDENTIALS_DIR>/<account>.json, written by `firm save-credentials`.
CREDENTIALS_DIR = Path("~/.config/firm-runtime/credentials")
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9_-]+")

SUMMARY_DEFAULTS = {
    "agy": {"model": "gemini-3.8-flash", "effort": "medium"},
    "codex": {"model": "gpt-6-luna", "effort": "medium"},
    "claude": {"model": "sonnet", "effort": "medium"},
}

SUBSCRIPTION_READING_FRESH_SECONDS = 5 * 60

TASK_DEFAULTS = {
    "agy": {"model": "gemini-3.8-flash", "effort": "high"},
    "max_concurrent": 2,
    "timeout_seconds": 600,
    "max_instruction_bytes": 16384,
    "max_answer_bytes": 16384,
    "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
    "claude": {"model": "opus", "effort": "medium"},
}


def task_settings(value):
    """Validate and fill task defaults without changing the supplied mapping."""
    if not isinstance(value, dict) or set(value) - set(TASK_DEFAULTS):
        raise FirmError("task has unsupported settings")
    result = {}
    for key, default in TASK_DEFAULTS.items():
        setting = value.get(key, default)
        if isinstance(default, dict):
            if not isinstance(setting, dict) or set(setting) - set(default):
                raise FirmError(f"task.{key} has unsupported settings")
            setting = {**default, **setting}
            if not isinstance(setting["model"], str) or not setting["model"].strip():
                raise FirmError(f"task.{key}.model must be non-empty")
            if (
                not isinstance(setting["effort"], str)
                or setting["effort"] not in SUPPORTED_EFFORTS[key]
            ):
                raise FirmError(f"unsupported task effort for {key}")
        elif type(setting) is not int or setting <= 0:
            raise FirmError(f"task.{key} must be a positive integer")
        result[key] = setting
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_bindings (
    session_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    primary_profile TEXT NOT NULL,
    profile TEXT NOT NULL,
    account_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    account_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_incidents (
    incident_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    dependency TEXT NOT NULL,
    kind TEXT NOT NULL,
    until_at REAL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT CHECK(resolved_by IN ('owner','expired','superseded'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_dependency_incident
    ON execution_incidents(scope,dependency) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS incident_roles (
    incident_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    PRIMARY KEY(incident_id,employee_id,manager_id)
);
CREATE TABLE IF NOT EXISTS account_observations (
    account_id TEXT PRIMARY KEY,
    remaining_fraction REAL,
    observed_at REAL NOT NULL,
    valid_for REAL NOT NULL,
    source TEXT NOT NULL
);
-- Latest allowance windows copied from provider streams; admission uses only fresh windows.
CREATE TABLE IF NOT EXISTS subscription_readings (
    account_id TEXT PRIMARY KEY,
    windows_json TEXT NOT NULL,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS role_dependencies (
    employee_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL
);
"""


def incident_resolution(row, now):
    """Effective first resolution, including expiry not yet materialized by admission."""
    if row["resolved_at"] is not None:
        return {"resolved_at": row["resolved_at"], "resolved_by": row["resolved_by"]}
    if row["until_at"] is not None and row["until_at"] <= now:
        return {"resolved_at": row["until_at"], "resolved_by": "expired"}
    return {"resolved_at": None, "resolved_by": None}


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FirmError(f"{label} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise FirmError(f"{label} must be a positive finite number")


def credentials_path(account, provider=None):
    if not isinstance(account, str) or ACCOUNT_NAME.fullmatch(account) is None:
        raise FirmError("account names must use letters, digits, '-' and '_'")
    if provider == "agy":
        from .agy_auth import TOKEN_PATH

        return CREDENTIALS_DIR.expanduser() / f"{account}-agy" / "home" / TOKEN_PATH
    return CREDENTIALS_DIR.expanduser() / f"{account}.json"


def validate_accounts(config):
    """Controlled execution need not configure external providers or accounts."""
    config["task"] = task_settings(config.get("task", {}))
    summary = config.get("summary")
    if summary is not None:
        if not isinstance(summary, dict) or set(summary) - set(SUMMARY_DEFAULTS):
            raise FirmError("summary must contain only codex, claude and agy settings")
        for provider, defaults in SUMMARY_DEFAULTS.items():
            setting = summary.get(provider, defaults)
            if not isinstance(setting, dict) or set(setting) - set(defaults):
                raise FirmError(f"summary.{provider} has unsupported settings")
            if not isinstance(setting.get("model"), str) or not setting["model"].strip():
                raise FirmError(f"summary.{provider}.model must be non-empty")
            effort = setting.get("effort")
            if not isinstance(effort, str) or effort not in SUPPORTED_EFFORTS[provider]:
                raise FirmError(f"unsupported summary effort for {provider}")
    if "accounts" not in config:
        return
    accounts, profiles = config["accounts"], config["profiles"]
    if config.get("google_cloud") and any(
        isinstance(item, dict) and item.get("provider") == "agy"
        for collection in (accounts, profiles) if isinstance(collection, dict)
        for item in collection.values()
    ):
        raise FirmError("AGY subscription inference cannot coexist with [google_cloud]")
    if not isinstance(accounts, dict) or not accounts:
        raise FirmError("accounts must be a nonempty mapping")
    for name, account in accounts.items():
        if not isinstance(name, str) or not name or not isinstance(account, dict):
            raise FirmError("each account needs a name and settings")
        if ACCOUNT_NAME.fullmatch(name) is None:
            raise FirmError("account names must use letters, digits, '-' and '_'")
        if set(account) - {
            "provider", "auth_file", "max_active", "min_fresh_fraction", "stop_admitting_at"
        }:
            raise FirmError(f"unknown account setting for {name}")
        provider = account.get("provider")
        if not isinstance(provider, str) or provider not in ("codex", "claude", "agy"):
            raise FirmError(f"invalid account provider for {name}")
        if "auth_file" not in account:
            account["auth_file"] = str(credentials_path(name, provider))
        auth = account["auth_file"]
        try:
            auth_path = Path(auth).expanduser() if isinstance(auth, str) else None
        except RuntimeError as exc:
            raise FirmError(f"account {name} requires an absolute auth_file") from exc
        if auth_path is None or not auth_path.is_absolute():
            raise FirmError(f"account {name} requires an absolute auth_file")
        if type(account.get("max_active")) is not int or account["max_active"] <= 0:
            raise FirmError(f"account {name} requires positive integer max_active")
        threshold = account.get("min_fresh_fraction", 0.1)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise FirmError("min_fresh_fraction must be between zero and one")
        stop_at = account.get("stop_admitting_at")
        if stop_at is not None and (
            isinstance(stop_at, bool)
            or not isinstance(stop_at, (int, float))
            or not math.isfinite(stop_at)
            or not 0 <= stop_at <= 1
        ):
            raise FirmError("stop_admitting_at must be between zero and one")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise FirmError(f"invalid execution profile {name}")
        if set(profile) - {
            "provider",
            "model",
            "effort",
            "accounts",
            "alternates",
            *CONTEXT_POLICY_KEYS,
        }:
            raise FirmError(f"unsupported execution setting in {name}")
        provider = profile.get("provider")
        if not isinstance(provider, str) or provider not in ("codex", "claude", "agy"):
            raise FirmError(f"invalid profile provider for {name}")
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise FirmError(f"profile {name} requires a model")
        effort = profile.get("effort")
        if not isinstance(effort, str) or effort not in SUPPORTED_EFFORTS[provider]:
            raise FirmError(f"unsupported effort for {name}")
        names = profile.get("accounts")
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
            raise FirmError(f"profile {name} requires named accounts")
        if len(set(names)) != len(names) or any(
            n not in accounts or accounts[n]["provider"] != provider for n in names
        ):
            raise FirmError(f"profile {name} references invalid accounts")
        alternates = profile.get("alternates", [])
        if (
            not isinstance(alternates, list)
            or any(not isinstance(n, str) for n in alternates)
            or len(set(alternates)) != len(alternates)
            or any(n not in profiles or n == name for n in alternates)
        ):
            raise FirmError(f"invalid alternate profiles for {name}")


class Accounts:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def config(self, db):
        return json.loads(self.store.firm(db)["config_json"])

    def enabled(self, db):
        return "accounts" in self.config(db)

    def binding(self, db, session_id):
        row = db.execute(
            "SELECT * FROM execution_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def _incident(self, db, scope, dependency):
        row = db.execute(
            "SELECT * FROM execution_incidents WHERE scope=? AND dependency=? "
            "AND resolved_at IS NULL",
            (scope, dependency),
        ).fetchone()
        if row and row["until_at"] is not None and row["until_at"] <= self.store.clock():
            db.execute(
                "UPDATE execution_incidents SET resolved_at=?,resolved_by='expired' "
                "WHERE incident_id=?",
                (row["until_at"], row["incident_id"]),
            )
            return None
        return dict(row) if row else None

    def _notify_block(self, db, role, incident):
        created = db.execute(
            "INSERT OR IGNORE INTO incident_roles VALUES(?,?,?)",
            (
                incident["incident_id"],
                role["employee_id"],
                role["manager_id"],
            ),
        ).rowcount
        if created:
            timed = incident["until_at"] is not None
            instruction = render(
                "notices",
                "shared_capacity_wait_instruction_timed"
                if timed
                else "shared_capacity_wait_instruction_owner",
            )
            self.runtime.messaging.notice(
                db,
                [role["manager_id"]],
                render(
                    "notices",
                    "shared_capacity_wait_body",
                    address=role["address"],
                    instruction=instruction,
                ),
                subject=render("notices", "shared_capacity_wait_subject"),
                # A shared incident may require owner repair, but this manager copy is
                # informational and must not inherit control priority from Operations.
                priority=ORDINARY_PRIORITY,
                regarding={"kind": "incident", "id": incident["incident_id"]},
            )

    def _pin_role(self, db, role, incident_id):
        db.execute(
            "INSERT INTO role_dependencies VALUES(?,?) ON CONFLICT(employee_id) "
            "DO UPDATE SET incident_id=excluded.incident_id",
            (role["employee_id"], incident_id),
        )
        incident = dict(
            db.execute(
                "SELECT * FROM execution_incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
        )
        self._notify_block(db, role, incident)

    def _usage(self, db, account_id):
        """Live roles plus tasks, borrowing one slot per live caller activation.

        After caller exit every unresolved task holds a slot. Summary and staffing
        retain their deliberate exemption from configured slots.
        A running task with a reason has already returned an unconfirmed refusal;
        only a still-waiting call (NULL reason) can borrow the caller's slot.
        """
        roles = db.execute(
            "SELECT COUNT(*) FROM activations a JOIN execution_bindings b USING(session_id) "
            "WHERE b.account_id=? AND a.state IN ('admitted','running')",
            (account_id,),
        ).fetchone()[0]
        tasks = db.execute(
            "SELECT COALESCE(SUM(n-borrowed),0) FROM ("
            "SELECT COUNT(*) AS n, CASE WHEN a.state IN ('admitted','running') "
            "AND COUNT(CASE WHEN t.reason IS NULL THEN 1 END)>0 "
            "THEN 1 ELSE 0 END AS borrowed FROM summary_runs t "
            "JOIN activations a ON a.activation_id=t.caller_activation_id "
            "WHERE t.kind='task' AND t.state='running' AND t.account_id=? "
            "GROUP BY t.caller_activation_id)",
            (account_id,),
        ).fetchone()[0]
        return roles + tasks

    def _conversations(self, db, account_id):
        """Roles whose current conversation is bound to the account, running or waiting."""
        return db.execute(
            "SELECT COUNT(*) FROM roles r JOIN execution_bindings b USING(session_id) "
            "WHERE b.account_id=? AND r.employment!='retired'",
            (account_id,),
        ).fetchone()[0]

    def _isolated(self, db, account_id):
        """Isolated summary and staffing runs on the account, outside its slots."""
        return db.execute(
            "SELECT COUNT(*) FROM summary_runs WHERE account_id=? AND state='running' "
            "AND kind!='task'",
            (account_id,),
        ).fetchone()[0]

    def reserve_summary(
        self, db, run_id: str, *, caller_session_id: str | None = None
    ) -> dict | None:
        """Bind an isolated summary turn to the caller's account.

        The run takes no configured slot, so a full account does not hold back a
        role's correspondence context; SummaryService bounds isolated runs on its
        own.  Account holds still apply.  A firm
        without named provider accounts uses an injected provider-free test
        runner and needs no account binding.
        """
        if not isinstance(run_id, str) or not run_id:
            raise FirmError("summary run_id must be a non-empty string")
        config = self.config(db)
        if "accounts" not in config:
            return {}
        if self.store.firm(db)["state"] != "running":
            return None
        binding = self.binding(db, caller_session_id) if caller_session_id else None
        if binding is None:
            return None
        profile_name = binding["profile"]
        profile = config["profiles"].get(profile_name)
        if profile is None or profile.get("provider") != binding["provider"]:
            return None
        provider = binding["provider"]
        account_ids = [binding["account_id"]]
        setting = config.get("summary", {}).get(provider, SUMMARY_DEFAULTS[provider])
        candidates = []
        for account_id in account_ids:
            account = config["accounts"].get(account_id)
            if account is None or account.get("provider") != provider:
                continue
            if not self._eligible(db, profile_name, account_id, account, capacity=False):
                continue
            candidates.append((self._isolated(db, account_id), account_id))
        if not candidates:
            return None
        _, account_id = min(candidates)
        return {
            "account_id": account_id,
            "profile": profile_name,
            "provider": provider,
            "model": setting["model"],
            "effort": setting["effort"],
        }

    def reserve_staffing(self, db, run_id: str) -> dict | None:
        """Select a fresh staffing route; the caller records its reservation in this transaction.

        Like a summary, the assessment takes no configured slot, so hiring is not
        held up by a full roster.
        """
        if not isinstance(run_id, str) or not run_id:
            raise FirmError("staffing run_id must be a non-empty string")
        config = self.config(db)
        staffing = config.get("staffing")
        if not staffing:
            return None
        if "accounts" not in config:
            return {}
        if self.store.firm(db)["state"] != "running":
            return None
        selected = self._select_fresh(db, config, staffing["profile"], capacity=False)
        if selected is None:
            return None
        settings = json.loads(selected["settings_json"])
        return {
            "account_id": selected["account_id"],
            "profile": selected["profile"],
            "provider": selected["provider"],
            "model": settings["model"],
            "effort": settings["effort"],
        }

    def _eligible(self, db, profile_name, account_id, account, *, capacity=True):
        self._refresh_quota_threshold(db, account_id, account)
        for scope, key in (("profile", profile_name), ("account", account_id)):
            incident = self._incident(db, scope, key)
            if incident:
                return False
        return not (capacity and self._usage(db, account_id) >= account["max_active"])

    def _refresh_quota_threshold(self, db, account_id, account):
        """Materialize a fresh crossed subscription threshold as a timed account hold."""
        threshold = account.get("stop_admitting_at")
        if threshold is None:
            return None
        current = self._incident(db, "account", account_id)
        if current and (current["kind"] != "quota" or current["until_at"] is None):
            # Unknown provider refusals and non-quota holds require owner intervention.
            return current
        row = db.execute(
            "SELECT windows_json,observed_at FROM subscription_readings WHERE account_id=?",
            (account_id,),
        ).fetchone()
        now = self.store.clock()
        if row is None or row["observed_at"] + SUBSCRIPTION_READING_FRESH_SECONDS <= now:
            return current
        try:
            windows = json.loads(row["windows_json"])
        except (TypeError, json.JSONDecodeError):
            return current
        if not isinstance(windows, list):
            return current
        crossed = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            used, reset = window.get("used_percent"), window.get("resets_at")
            if type(used) not in (int, float) or type(reset) not in (int, float):
                continue
            try:
                used, reset = float(used), float(reset)
            except OverflowError:
                continue
            if (
                math.isfinite(used)
                and used / 100 >= threshold
                and math.isfinite(reset)
                and reset > now
            ):
                crossed.append(reset)
        if not crossed:
            return current
        return self.hold(db, "account", account_id, "quota", max(crossed))

    def _low_quota(self, db, account_id, account) -> bool:
        """A recent reading below the account's threshold. Fresh work takes such an
        account only when it has nowhere else to go, so a sole option runs to empty."""
        reading = db.execute(
            "SELECT * FROM account_observations WHERE account_id=?", (account_id,)
        ).fetchone()
        return bool(
            reading
            and reading["remaining_fraction"] is not None
            and reading["observed_at"] + reading["valid_for"] > self.store.clock()
            and reading["remaining_fraction"] < account.get("min_fresh_fraction", 0.1)
        )

    def select(self, db, role):
        config = self.config(db)
        accounts, profiles = config["accounts"], config["profiles"]
        dependency = db.execute(
            "SELECT i.* FROM role_dependencies d JOIN execution_incidents i "
            "USING(incident_id) WHERE d.employee_id=? AND i.resolved_at IS NULL",
            (role["employee_id"],),
        ).fetchone()
        if dependency:
            incident = self._incident(db, dependency["scope"], dependency["dependency"])
            if incident and incident["incident_id"] == dependency["incident_id"]:
                self._notify_block(db, role, incident)
                return None
        retained = self.binding(db, role["session_id"]) if role["session_id"] else None
        if retained:
            account = accounts.get(retained["account_id"])
            if not account or account["provider"] != retained["provider"]:
                incident_id = self.hold(db, "account", retained["account_id"], "config")
                self._pin_role(db, role, incident_id)
                return None
            # A retained conversation keeps its account whatever that account's slot
            # limit now says; only holds stop it. Limits steer fresh conversations.
            if self._eligible(
                db, retained["profile"], retained["account_id"], account, capacity=False
            ):
                return retained
            for scope, key in (
                ("profile", retained["profile"]),
                ("account", retained["account_id"]),
            ):
                incident = self._incident(db, scope, key)
                if incident:
                    self._notify_block(db, role, incident)
            return None
        primary = role["execution_profile"]
        if primary not in profiles:
            incident_id = self.hold(db, "profile", primary, "config")
            self._pin_role(db, role, incident_id)
            return None
        selected = self._select_fresh(db, config, primary, effort=role["effort"])
        if selected is not None:
            return selected
        for name in [primary, *profiles[primary].get("alternates", [])]:
            for scope, key in [
                ("profile", name),
                *(("account", a) for a in profiles[name]["accounts"]),
            ]:
                incident = self._incident(db, scope, key)
                if incident:
                    self._notify_block(db, role, incident)
        return None

    def _select_fresh(self, db, config, primary, *, effort=None, capacity=True):
        """Send a fresh conversation where there is most room.

        Only explicitly configured routes participate. An account whose recent quota
        reading is below its threshold ranks after every other candidate, so it is
        chosen only when nothing else can take the work. Candidates then rank by their
        provider's load across its accounts that are not held, then the account's
        own load, then route order, so the primary wins ties. Load is the conversations
        bound to the account, running or waiting, plus isolated runs, against configured
        slots: retained conversations run whatever the limit, so an account the owner
        lowered keeps drawing no new work until its own conversations end. It is read
        inside the reserving transaction so concurrent admissions see each other.
        Eligibility still needs a free slot now.
        `capacity=False` selects for an isolated run, which takes no slot.
        """
        accounts, profiles = config["accounts"], config["profiles"]
        for account_id, account in accounts.items():
            self._refresh_quota_threshold(db, account_id, account)
        load = {
            account_id: self._conversations(db, account_id) + self._isolated(db, account_id)
            for account_id in accounts
        }
        provider_load, provider_slots = {}, {}
        for account_id, account in accounts.items():
            if self._incident(db, "account", account_id):
                continue  # a held account offers its provider no room
            provider = account["provider"]
            provider_load[provider] = provider_load.get(provider, 0) + load[account_id]
            provider_slots[provider] = provider_slots.get(provider, 0) + account["max_active"]
        candidates = []
        for index, name in enumerate([primary, *profiles[primary].get("alternates", [])]):
            profile = profiles[name]
            for account_id in profile["accounts"]:
                account = accounts[account_id]
                if self._eligible(db, name, account_id, account, capacity=capacity):
                    provider = profile["provider"]
                    candidates.append(
                        (
                            self._low_quota(db, account_id, account),
                            provider_load[provider] / provider_slots[provider],
                            load[account_id] / account["max_active"],
                            index,
                            account_id,
                            name,
                        )
                    )
        if not candidates:
            return None
        *_, account_id, name = min(candidates)
        profile = profiles[name]
        return {
            "primary_profile": primary,
            "profile": name,
            "account_id": account_id,
            "provider": profile["provider"],
            "settings_json": encoded({**profile, "effort": effort or profile["effort"]}),
            "account_json": encoded(accounts[account_id]),
        }

    def bind(self, db, role, session_id, selected):
        if self.binding(db, session_id):
            return
        db.execute(
            "INSERT INTO execution_bindings VALUES(?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                role["employee_id"],
                selected["primary_profile"],
                selected["profile"],
                selected["account_id"],
                selected["provider"],
                selected["settings_json"],
                selected["account_json"],
                self.store.clock(),
            ),
        )

    def hold(self, db, scope, dependency, kind, until=None):
        if scope not in {"account", "profile"} or kind not in {"auth", "config", "quota", "outage"}:
            raise FirmError("invalid shared hold")
        if kind in {"auth", "quota"} and scope != "account":
            raise FirmError(f"{kind} holds must be scoped to an account")
        if kind in {"auth", "config"} and until is not None:
            raise FirmError(f"{kind} holds require explicit clearance")
        if until is not None:
            _positive(until, "hold time")
        old = self._incident(db, scope, dependency)
        if old and old["kind"] == kind:
            if old["until_at"] is None or until is None:
                until = None
            else:
                until = max(old["until_at"], until)
            db.execute(
                "UPDATE execution_incidents SET until_at=? WHERE incident_id=?",
                (until, old["incident_id"]),
            )
            return old["incident_id"]
        if old:
            db.execute(
                "UPDATE execution_incidents SET resolved_at=?,resolved_by='superseded' "
                "WHERE incident_id=?",
                (self.store.clock(), old["incident_id"]),
            )
        incident_id = str(uuid4())
        db.execute(
            "INSERT INTO execution_incidents"
            "(incident_id,scope,dependency,kind,until_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (incident_id, scope, dependency, kind, until, self.store.clock()),
        )
        self.runtime.messaging.notice(
            db,
            [OWNER],
            render(
                "notices",
                "shared_dependency_paused_body_timed"
                if until
                else "shared_dependency_paused_body_owner",
                scope=scope,
                dependency=dependency,
                kind=kind,
                incident_id=incident_id,
                until=until,
            ),
            subject=render("notices", "shared_dependency_paused_subject"),
            priority=0 if until else CONTROL_PRIORITY,
            regarding={"kind": "incident", "id": incident_id},
        )
        return incident_id

    def record_failure(self, db, session_id, category, until=None):
        binding = self.binding(db, session_id)
        if not binding or category not in {"auth", "config", "quota"}:
            return None
        scope = "profile" if category == "config" else "account"
        key = binding["profile"] if scope == "profile" else binding["account_id"]
        incident_id = self.hold(db, scope, key, category, until if category == "quota" else None)
        role = self.store.role(db, binding["employee_id"])
        if category in {"auth", "config"}:
            self._pin_role(db, role, incident_id)
        else:
            incident = dict(
                db.execute(
                    "SELECT * FROM execution_incidents WHERE incident_id=?", (incident_id,)
                ).fetchone()
            )
            self._notify_block(db, role, incident)
        return incident_id

    def observe(self, actor_id, account_id, remaining_fraction, *, observed_at, valid_for, source):
        if actor_id != OWNER:
            raise FirmError("only the owner may supply quota observations")
        _positive(valid_for, "observation lifetime")
        _positive(observed_at, "observation time")
        if observed_at > self.store.clock() + 5:
            raise FirmError("quota observation is in the future")
        if remaining_fraction is not None and (
            isinstance(remaining_fraction, bool)
            or not isinstance(remaining_fraction, (int, float))
            or not math.isfinite(remaining_fraction)
            or not 0 <= remaining_fraction <= 1
        ):
            raise FirmError("remaining fraction must be between zero and one, or unknown")
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise FirmError("quota observation needs a bounded source description")
        with self.store.transaction() as db:
            if account_id not in self.config(db).get("accounts", {}):
                raise FirmError("unknown account")
            db.execute(
                "INSERT INTO account_observations VALUES(?,?,?,?,?) ON CONFLICT(account_id) "
                "DO UPDATE SET remaining_fraction=excluded.remaining_fraction,"
                "observed_at=excluded.observed_at,"
                "valid_for=excluded.valid_for,source=excluded.source "
                "WHERE excluded.observed_at>=account_observations.observed_at",
                (account_id, remaining_fraction, observed_at, valid_for, source),
            )
        return {"status": "recorded", "account_id": account_id}

    def record_subscription(self, account_id, windows, *, status=None, warning=None):
        """Keep the newest streamed allowance and notify once per warning window."""
        with self.store.transaction() as db:
            config = self.config(db)
            account = config.get("accounts", {}).get(account_id)
            if account is None:
                return
            now = self.store.clock()
            if isinstance(windows, list):
                db.execute(
                    "INSERT INTO subscription_readings VALUES(?,?,?) ON CONFLICT(account_id) "
                    "DO UPDATE SET windows_json=excluded.windows_json,"
                    "observed_at=excluded.observed_at",
                    (account_id, encoded(windows), now),
                )
                self._refresh_quota_threshold(db, account_id, account)
            if not isinstance(warning, dict):
                return
            name, resets_at = warning.get("window"), warning.get("resets_at")
            warning_status = warning.get("status")
            if type(resets_at) not in (int, float):
                return
            try:
                resets_at = float(resets_at)
            except OverflowError:
                return
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9_]{1,40}", name)
                or status not in ("allowed", "allowed_warning")
                or warning_status != "allowed_warning"
                or not math.isfinite(resets_at)
                or resets_at <= now
            ):
                return
            payload = {
                "account_id": account_id,
                "window": name,
                "resets_at": float(resets_at),
            }
            request_id = "subscription-limit-warning:" + hashlib.sha256(
                encoded(payload).encode("utf-8")
            ).hexdigest()

            def notify():
                return self.runtime.messaging.notice(
                    db,
                    [OWNER],
                    render(
                        "notices",
                        "subscription_limit_warning_body",
                        account_id=account_id,
                        window=name,
                        status=warning_status,
                        resets_at=resets_at,
                    ),
                    subject=render(
                        "notices", "subscription_limit_warning_subject", account_id=account_id
                    ),
                    priority=CONTROL_PRIORITY,
                )

            self.store.request(
                db,
                OPERATIONS,
                request_id,
                "subscription_limit_warning",
                payload,
                notify,
            )

    def subscription_reading(self, db, account_id):
        row = db.execute(
            "SELECT * FROM subscription_readings WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        return {"observed_at": row["observed_at"], "windows": json.loads(row["windows_json"])}

    def pause(self, actor_id, scope, dependency, kind, until=None):
        if actor_id != OWNER:
            raise FirmError("only the owner may pause shared execution dependencies")
        with self.store.transaction() as db:
            config = self.config(db)
            key = {"account": "accounts", "profile": "profiles"}.get(scope)
            if key is None or dependency not in config.get(key, {}):
                raise FirmError("unknown execution dependency")
            incident_id = self.hold(db, scope, dependency, kind, until)
        return {"status": "paused", "incident_id": incident_id}

    def clear(self, actor_id, incident_id):
        if actor_id != OWNER:
            raise FirmError("only the owner may clear shared execution holds")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM execution_incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if not row:
                raise FirmError("unknown incident")
            if row["resolved_at"] is None:
                now = self.store.clock()
                resolution = incident_resolution(row, now)
                db.execute(
                    "UPDATE execution_incidents SET resolved_at=?,resolved_by=? "
                    "WHERE incident_id=? AND resolved_at IS NULL",
                    (
                        resolution["resolved_at"] if resolution["resolved_at"] is not None else now,
                        resolution["resolved_by"] or "owner",
                        incident_id,
                    ),
                )
        return {"status": "cleared", "incident_id": incident_id}

    def status(self):
        with self.store.read() as db:
            config = self.config(db)
            accounts = []
            for name, account in config.get("accounts", {}).items():
                observation = db.execute(
                    "SELECT * FROM account_observations WHERE account_id=?", (name,)
                ).fetchone()
                current = (
                    observation
                    and observation["observed_at"] + observation["valid_for"] > self.store.clock()
                )
                accounts.append(
                    {
                        "account_id": name,
                        "provider": account["provider"],
                        "active": self._usage(db, name),
                        "max_active": account["max_active"],
                        "isolated": self._isolated(db, name),
                        "remaining_fraction": observation["remaining_fraction"]
                        if current
                        else None,
                        "headroom": "known"
                        if current and observation["remaining_fraction"] is not None
                        else "unknown",
                    }
                )
            incidents = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM execution_incidents WHERE resolved_at IS NULL "
                    "AND (until_at IS NULL OR until_at>?) ORDER BY created_at",
                    (self.store.clock(),),
                )
            ]
        from .credential_store import setup_token_warning

        warnings = [
            warning
            for name, account in config.get("accounts", {}).items()
            if (warning := setup_token_warning(name, account)) is not None
        ]
        return {"accounts": accounts, "incidents": incidents, "warnings": warnings}
