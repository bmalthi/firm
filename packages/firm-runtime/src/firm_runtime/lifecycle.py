"""Durable role registration, employment, and mandate authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from firm_sandbox import Layout

from . import analysis_history
from .appointments import Appointments, validate_periods
from .discovery import identity
from .errors import ConfigurationError, FirmError
from .levels import founder_execution, hire_execution, validate_staffing, with_staffing_defaults
from .levels import register as register_level
from .prompt_resources import render
from .requests import withdraw_role
from .store import CONTROL_PRIORITY, OPERATIONS, OWNER, Store, encoded
from .workspaces import sandbox_role_id

FOUNDER_ID = "founder"
MAX_REASON_BYTES = 8 * 1024
MAX_ROLE_DEPTH = 16
MAX_ADDRESS_CHARS = 1024
ROLE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FIRM_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


class Lifecycle:
    """Own the small, trusted lifecycle domain API for one firm's store."""

    def __init__(
        self,
        store: Store,
        messaging: Any,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.appointments = Appointments(store, messaging)
        self.messaging = messaging
        self._id_factory = id_factory
        self.provision_role: Callable[[str], None] | None = None
        # The execution host replaces this with its sandbox's per-role layout on entry;
        # offline previews use the same pure function.
        self.layout_for: Callable[[str], Layout] = lambda employee_id: Layout.for_role(
            sandbox_role_id(employee_id)
        )

    def initialize(self, firm_id: str, mandate: str, config: dict) -> dict:
        """Create a stopped firm and its one founder birth opportunity."""
        if not isinstance(firm_id, str) or len(firm_id) > 64 or FIRM_ID.fullmatch(firm_id) is None:
            raise FirmError(f"invalid firm_id: {firm_id!r}")
        _require_mandate(mandate)
        normalized_config = validate_config(config)
        config_json = encoded(normalized_config)
        app = None
        if "github" in normalized_config:
            with self.store.read() as db:
                initialized = db.execute("SELECT 1 FROM firm").fetchone() is not None
            if not initialized:
                from . import github

                app = github.connect(normalized_config["github"])

        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM firm WHERE singleton=1").fetchone()
            if existing is not None:
                if (
                    existing["firm_id"] != firm_id
                    or existing["initial_mandate"] != mandate
                    or existing["config_json"] != config_json
                ):
                    raise FirmError("firm is already initialized with different inputs")
                founder = self.store.role(db, FOUNDER_ID)
                return {
                    "firm": _firm_view(dict(existing)),
                    "founder": self._role_view(db, founder, include_execution=True),
                }

            now = self.store.clock()
            db.execute(
                "INSERT INTO firm(singleton,firm_id,config_json,initial_mandate,state,created_at) "
                "VALUES(1,?,?,?,'stopped',?)",
                (firm_id, config_json, mandate, now),
            )
            founder_profile, founder_effort = founder_execution(normalized_config)
            db.execute(
                """INSERT INTO roles(
                       employee_id,address,manager_id,identity,public_profile,employment,
                       execution_profile,effort,recovery_episode,created_at
                   ) VALUES(?,?,?,?,?,'active',?,?,?,?)""",
                (
                    FOUNDER_ID,
                    "founder",
                    OWNER,
                    "founder",
                    "",
                    founder_profile,
                    founder_effort,
                    f"{FOUNDER_ID}:1",
                    now,
                ),
            )
            db.execute(
                "INSERT INTO mandates(employee_id,revision,author_id,body,reason,created_at) "
                "VALUES(?,0,?,?,?,?)",
                (FOUNDER_ID, OWNER, mandate, "Initial owner mandate", now),
            )
            if app is not None:
                from . import github

                github.record_app(db, app)
            self.store.request_execution(
                db,
                FOUNDER_ID,
                "birth",
                CONTROL_PRIORITY,
                source_id=FOUNDER_ID,
            )
            firm = self.store.firm(db)
            founder = self.store.role(db, FOUNDER_ID)
            analysis_history.role_changed(db, now, None, FOUNDER_ID)
            return {
                "firm": _firm_view(firm),
                "founder": self._role_view(db, founder, include_execution=True),
            }

    def hire(
        self,
        actor_id: str,
        slug: str,
        mandate: str,
        execution_profile: str | None = None,
        *,
        request_id: str,
        temporary_periods: int | None = None,
    ) -> dict:
        """Durably hire one direct report and queue its first work period."""
        if temporary_periods is not None:
            validate_periods(temporary_periods)
        _require_slug(slug, "role slug")
        if slug == "work":
            raise FirmError("role slug is reserved for workspace structure")
        _require_mandate(mandate)
        if execution_profile is not None and (
            not isinstance(execution_profile, str) or not execution_profile
        ):
            raise FirmError("execution_profile must be a non-empty string")

        payload = {
            "slug": slug,
            "mandate": mandate,
            "execution_profile": execution_profile,
            "temporary_periods": temporary_periods,
        }
        with self.store.transaction() as db:
            if actor_id in {OWNER, OPERATIONS}:
                raise FirmError("only an active employee may hire a direct report")
            actor = self.store.require_actor(db, actor_id)
            config = json.loads(self.store.firm(db)["config_json"])
            address = f"{actor['address']}/{slug}"
            _require_address(address)

            def create() -> dict:
                if execution_profile is None:
                    selected_profile, effort = hire_execution(config)
                else:
                    selected_profile, effort = execution_profile, None
                if selected_profile not in config["profiles"]:
                    raise ConfigurationError("execution_profile is not configured for this firm")
                occupied = db.execute(
                    "SELECT 1 FROM roles WHERE address=? AND employment!='retired'",
                    (address,),
                ).fetchone()
                if occupied is not None:
                    raise FirmError("role address is already in use")
                employee_id = (
                    self._id_factory() if self._id_factory else self.store.allocate_employee_id(db)
                )
                if not isinstance(employee_id, str) or not employee_id:
                    raise RuntimeError("employee ID factory returned an invalid ID")
                now = self.store.clock()
                try:
                    db.execute(
                        """INSERT INTO roles(
                               employee_id,address,manager_id,identity,public_profile,employment,
                               execution_profile,effort,recovery_episode,created_at
                           ) VALUES(?,?,?,?,?,'active',?,?,?,?)""",
                        (
                            employee_id,
                            address,
                            actor_id,
                            address,
                            "",
                            selected_profile,
                            effort,
                            f"{employee_id}:1",
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise FirmError("employee identity or role address is already in use") from exc
                db.execute(
                    "INSERT INTO mandates(employee_id,revision,author_id,body,reason,created_at) "
                    "VALUES(?,0,?,?,?,?)",
                    (employee_id, actor_id, mandate, "Initial delegated mandate", now),
                )
                if self.provision_role is not None:
                    self.provision_role(employee_id)
                self.store.request_execution(
                    db,
                    employee_id,
                    "birth",
                    CONTROL_PRIORITY,
                    source_id=employee_id,
                )
                if temporary_periods is not None:
                    db.execute(
                        "INSERT INTO appointments VALUES(?, 'temporary', ?, 0)",
                        (employee_id, temporary_periods),
                    )
                if execution_profile is None and config.get("staffing"):
                    # The chooser decides the level before the first work period.
                    register_level(db, employee_id, now)
                role = self.store.role(db, employee_id)
                from .workspaces import grants_for

                analysis_history.role_changed(db, now, None, employee_id)

                access = grants_for(
                    self.store, employee_id, self.layout_for(employee_id)
                ).references
                return {
                    **self._role_view(db, role, include_execution=True, include_public_text=False),
                    "status": "pending",
                    "appointment": self.appointments.terms(db, employee_id),
                    "access_on_activation": access,
                    "related_actions": ["send", "revise_mandate", "fire"]
                    + (["renew_temporary", "promote"] if temporary_periods is not None else []),
                }

            return self.store.request(db, actor_id, request_id, "hire", payload, create)

    def revise_mandate(
        self,
        actor_id: str,
        employee_id: str,
        expected_revision: int,
        mandate: str,
        *,
        reason: str = "",
        request_id: str,
    ) -> dict:
        """Append an authorized mandate revision with optimistic concurrency."""
        _require_revision(expected_revision)
        _require_mandate(mandate)
        _require_reason(reason, required=False)
        payload = {
            "employee_id": employee_id,
            "expected_revision": expected_revision,
            "mandate": mandate,
            "reason": reason,
        }
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            target = self.store.role(db, employee_id)
            if target["employment"] == "retired":
                raise FirmError("cannot revise a retired employee's mandate")
            owner_revision = actor_id == OWNER and employee_id == FOUNDER_ID
            manager_revision = target["manager_id"] == actor_id and actor_id != OWNER
            if not owner_revision and not manager_revision:
                raise FirmError("only the direct manager may revise this mandate")

            def revise() -> dict:
                current = _mandate(db, employee_id)
                if current["revision"] != expected_revision:
                    raise FirmError(
                        "mandate revision conflict: "
                        f"expected {expected_revision}, current is {current['revision']}"
                    )
                revision = expected_revision + 1
                db.execute(
                    "INSERT INTO mandates(employee_id,revision,author_id,body,reason,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (employee_id, revision, actor_id, mandate, reason, self.store.clock()),
                )
                role = self.store.role(db, employee_id)
                result = {
                    **self._role_view(db, role, include_public_text=False),
                    "status": "updated",
                }
                if owner_revision:
                    explanation = reason or render(
                        "notices", "owner_mandate_revision_default_explanation"
                    )
                    self.messaging.notice(
                        db,
                        [employee_id],
                        render(
                            "notices",
                            "owner_mandate_revision_body",
                            revision=revision,
                            explanation=explanation,
                        ),
                        subject=render(
                            "notices", "owner_mandate_revision_subject", revision=revision
                        ),
                        priority=CONTROL_PRIORITY,
                        wake=True,
                    )
                return result

            return self.store.request(db, actor_id, request_id, "revise_mandate", payload, revise)

    def get_role(self, actor_id: str, employee_id: str) -> dict:
        """Return public role facts and the current authoritative mandate."""
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            return self._role_view(db, self.store.role(db, employee_id))

    def fire(
        self,
        actor_id: str,
        employee_id: str,
        *,
        reason: str,
        request_id: str,
    ) -> dict:
        """Retire one direct report while preserving its execution and files."""
        _require_reason(reason, required=True)
        payload = {"employee_id": employee_id, "reason": reason}
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)
            target = self.store.role(db, employee_id)
            if employee_id == FOUNDER_ID:
                raise FirmError("the founder cannot be fired")
            if target["manager_id"] != actor_id:
                raise FirmError("only the direct manager may fire this employee")

            def retire() -> dict:
                if target["employment"] != "retired":
                    descendant = db.execute(
                        """WITH RECURSIVE descendants(employee_id,employment) AS (
                               SELECT employee_id,employment FROM roles WHERE manager_id=?
                               UNION ALL
                               SELECT r.employee_id,r.employment FROM roles AS r
                               JOIN descendants AS d ON r.manager_id=d.employee_id
                           )
                           SELECT employee_id FROM descendants
                           WHERE employment!='retired' LIMIT 1""",
                        (employee_id,),
                    ).fetchone()
                    if descendant is not None:
                        raise FirmError("cannot fire an employee with live descendants")
                    db.execute(
                        "UPDATE roles SET employment='retired',hold_reason=NULL,retry_at=NULL "
                        "WHERE employee_id=?",
                        (employee_id,),
                    )
                    db.execute("DELETE FROM execution_requests WHERE employee_id=?", (employee_id,))
                    self.messaging.retire(db, employee_id)
                    withdraw_role(db, self.store.clock(), employee_id)
                    analysis_history.role_changed(db, self.store.clock(), target, employee_id)
                role = self.store.role(db, employee_id)
                return {
                    "employee_id": role["employee_id"],
                    "address": role["address"],
                    "employment": role["employment"],
                    "status": "retired",
                    "files": "preserved",
                    "reason": reason,
                }

            return self.store.request(db, actor_id, request_id, "fire", payload, retire)

    def _role_view(
        self,
        db: sqlite3.Connection,
        role: dict,
        *,
        include_execution: bool = False,
        include_public_text: bool = True,
    ) -> dict:
        profile = role["public_profile"]
        mandate = _mandate(db, role["employee_id"])
        result = {
            "employee_id": role["employee_id"],
            "address": role["address"],
            "identity": identity(role),
            "manager_id": role["manager_id"],
            "employment": role["employment"],
            "accepts_mail": role["employment"] != "retired",
            "profile": profile if include_public_text else None,
            "mandate": mandate,
        }
        if not include_public_text:
            result["mandate"] = {key: value for key, value in mandate.items() if key != "body"}
            result["mandate"]["digest"] = hashlib.sha256(
                mandate["body"].encode("utf-8")
            ).hexdigest()
        if include_execution:
            result["execution_profile"] = role["execution_profile"]
        return result


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise FirmError("config must be a mapping")
    # Normalization adds generated staffing profiles and default credential paths.
    # Keep validation free of surprising mutations to the caller's parsed config.
    config = deepcopy(config)
    max_active = config.get("max_active")
    if type(max_active) is not int or max_active <= 0:
        raise FirmError("config.max_active must be a positive integer")
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        raise FirmError("config.profiles must be a mapping")
    if any(not isinstance(name, str) or not name for name in profiles):
        raise FirmError("config profile names must be non-empty strings")
    # With a staffing chooser, the founder defaults to the high class and hires
    # take the chooser's decision; explicit profiles remain valid when given.
    config = with_staffing_defaults(config)
    profiles = config["profiles"]
    validate_staffing(config)
    chooser = config["staffing"] is not False
    founder_profile = config.get("founder_profile")
    if founder_profile is None and chooser:
        pass
    elif not isinstance(founder_profile, str) or not founder_profile:
        raise FirmError("config.founder_profile must be a non-empty string")
    elif founder_profile not in profiles:
        raise FirmError("config.founder_profile must name a configured profile")
    default_profile = config.get("default_profile", founder_profile)
    if (default_profile is not None or not chooser) and (
        not isinstance(default_profile, str) or default_profile not in profiles
    ):
        raise FirmError("config.default_profile must name a configured profile")
    retry_delays = config.get("retry_delays", [60, 300, 1800])
    if (
        not isinstance(retry_delays, list)
        or len(retry_delays) != 3
        or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay <= 0
            for delay in retry_delays
        )
    ):
        raise FirmError("config.retry_delays must contain exactly three positive finite delays")
    from .telemetry import validate_context_policy

    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise FirmError(f"invalid execution profile {name}")
        provider = profile.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise FirmError(f"invalid execution profile provider for {name}")
        validate_context_policy(profile, name)
    from .accounts import validate_accounts
    from .github import validate_github
    from .google_cloud import validate_google_cloud
    from .notify import validate_owner

    validate_accounts(config)
    validate_github(config)
    validate_google_cloud(config)
    validate_owner(config)
    try:
        encoded(config)
    except (TypeError, ValueError) as exc:
        raise FirmError("config must be JSON serializable") from exc
    return config


def _firm_view(firm: dict) -> dict:
    return {
        "firm_id": firm["firm_id"],
        "state": firm["state"],
        "config": json.loads(firm["config_json"]),
    }


def _mandate(db: sqlite3.Connection, employee_id: str) -> dict:
    row = db.execute(
        "SELECT revision,author_id,body,reason FROM mandates "
        "WHERE employee_id=? ORDER BY revision DESC LIMIT 1",
        (employee_id,),
    ).fetchone()
    if row is None:
        raise FirmError("employee has no authoritative mandate")
    return dict(row)


def _require_slug(value: str, label: str) -> None:
    if not isinstance(value, str) or ROLE_SLUG.fullmatch(value) is None:
        raise FirmError(f"invalid {label}: {value!r}")


def _require_address(address: str) -> None:
    parts = address.split("/")
    if (
        len(address) > MAX_ADDRESS_CHARS
        or len(parts) > MAX_ROLE_DEPTH
        or any(ROLE_SLUG.fullmatch(part) is None for part in parts)
    ):
        raise FirmError(f"invalid role address: {address!r}")


def _require_mandate(mandate: str) -> None:
    if not isinstance(mandate, str) or not mandate.strip():
        raise FirmError("mandate must be a non-empty string")


def _require_revision(revision: int) -> None:
    if type(revision) is not int or revision < 0:
        raise FirmError("expected_revision must be a non-negative integer")


def _require_reason(reason: str, *, required: bool) -> None:
    if not isinstance(reason, str) or (required and not reason.strip()):
        requirement = "non-empty " if required else ""
        raise FirmError(f"reason must be a {requirement}string")
    if len(reason.encode("utf-8")) > MAX_REASON_BYTES:
        raise FirmError(f"reason exceeds {MAX_REASON_BYTES} bytes")
