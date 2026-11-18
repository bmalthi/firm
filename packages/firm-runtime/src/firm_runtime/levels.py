"""Runtime-chosen hiring levels: a model class and an effort for each hire.

The hiring manager supplies only a mandate. A chooser, one isolated no-authority
provider turn configured under ``[staffing]``, reads the hire request and the
organization around the manager and picks both values. The owner maps classes to
execution profiles and can override any choice by routing the role.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable
from typing import Any

from . import analysis_history, guidance
from .errors import FirmError
from .prompt_resources import render
from .provider_definitions import PROVIDER_DEFINITIONS
from .store import OWNER, encoded
from .summaries import SummaryInputTooLarge, check_prompt_size

CLASSES = ("low", "medium", "high")
EFFORTS = ("low", "medium", "high", "xhigh")
FOUNDER_LEVEL = ("high", "high")
FALLBACK_LEVEL = ("medium", "high")
MAX_ATTEMPTS = 3
MAX_CHOICE_REASON_CHARS = 1000
RETRY_DELAY_SECONDS = 60.0
CHOOSER_TIMEOUT_SECONDS = 120.0
MAX_STAFFING_PROMPT_BYTES = 256 * 1024
# The hire's mandate is authoritative input and is passed through unchanged.
# Only surrounding mandates are excerpts, so a verbose manager cannot crowd out
# the work being sized.
MAX_CONTEXT_CHARS = 8000
MAX_EXCERPT_CHARS = 400
MAX_REPORTS = 20
TRUNCATION_MARKER = " [truncated]"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hire_levels (
    employee_id TEXT PRIMARY KEY REFERENCES roles(employee_id),
    state TEXT NOT NULL CHECK(state IN ('pending','chosen','defaulted','routed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL,
    model_class TEXT,
    effort TEXT,
    reason TEXT,
    updated_at REAL NOT NULL
);
"""


def with_staffing_defaults(config: dict) -> dict:
    """Resolve omitted staffing using the owner's provider and account selection."""
    if "staffing" in config:
        return config
    profiles = config["profiles"]
    source_name = config.get("founder_profile", config.get("default_profile"))
    if source_name is None and len(profiles) == 1:
        source_name = next(iter(profiles))
    if not isinstance(source_name, str):
        raise FirmError(
            "staffing defaults need a founder_profile or default_profile naming a configured "
            "profile; configure [staffing] explicitly, or staffing = false for fixed-profile "
            "hiring"
        )
    source = profiles.get(source_name)
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("provider"), str)
        or source["provider"] not in PROVIDER_DEFINITIONS
    ):
        raise FirmError(
            "staffing defaults need a founder_profile with provider codex, claude or agy "
            "(or one configured profile); configure [staffing] to choose explicitly, "
            "or staffing = false for fixed-profile hiring"
        )
    config = copy.deepcopy(config)
    profiles = config["profiles"]
    alternates = source.get("alternates", [])
    if not isinstance(alternates, list) or any(
        not isinstance(name, str) for name in alternates
    ):
        raise FirmError("staffing default alternates must be configured profile names")
    source_names = [source_name, *alternates]
    routes = [profiles.get(name) for name in source_names]
    if any(
        not isinstance(route, dict)
        or not isinstance(route.get("provider"), str)
        or route["provider"] not in PROVIDER_DEFINITIONS
        for route in routes
    ):
        raise FirmError(
            "staffing default routes must name configured codex, claude or agy profiles"
        )
    classes = {}
    for level in (*CLASSES, "chooser"):
        names = [f"staffing-{level}" + (f"-{i}" if i else "") for i in range(len(routes))]
        for name, route in zip(names, routes, strict=True):
            if name in profiles:
                raise FirmError(
                    f"profile {name} is reserved for staffing defaults; configure [staffing]"
                )
            profile = {k: copy.deepcopy(v) for k, v in route.items() if k != "alternates"}
            definition = PROVIDER_DEFINITIONS[route["provider"]]
            settings = getattr(definition, f"staffing_{level}")
            profile.update(settings.as_dict())
            profiles[name] = profile
        if len(names) > 1:
            profiles[names[0]]["alternates"] = names[1:]
        if level != "chooser":
            classes[level] = names[0]
    config["staffing"] = {"profile": "staffing-chooser", "classes": classes}
    return config


def validate_staffing(config: dict) -> None:
    """Validate resolved staffing or an explicit fixed-profile opt-out."""
    staffing = config.get("staffing")
    if staffing is False:
        return
    if staffing is None:
        raise FirmError(
            "omit staffing to enable defaults, or configure [staffing] and [staffing.classes]; "
            "set staffing = false explicitly to use fixed-profile hiring"
        )
    if not isinstance(staffing, dict) or set(staffing) - {"profile", "classes"}:
        raise FirmError("staffing accepts profile and classes")
    profiles = config["profiles"]
    chooser_name = staffing.get("profile")
    if not isinstance(chooser_name, str) or chooser_name not in profiles:
        raise FirmError("staffing.profile must name a configured profile")
    chooser = profiles[chooser_name]
    if (
        not isinstance(chooser, dict)
        or not isinstance(chooser.get("provider"), str)
        or chooser["provider"] not in PROVIDER_DEFINITIONS
    ):
        raise FirmError("staffing profile provider must be codex, claude or agy")
    if not isinstance(chooser.get("model"), str) or not chooser["model"].strip():
        raise FirmError("staffing profile model must be non-empty")
    effort = chooser.get("effort")
    if (
        not isinstance(effort, str)
        or effort not in PROVIDER_DEFINITIONS[chooser["provider"]].supported_efforts
    ):
        raise FirmError("unsupported staffing profile effort")
    classes = staffing.get("classes")
    if not isinstance(classes, dict) or set(classes) != set(CLASSES):
        raise FirmError("staffing.classes must map low, medium and high to profiles")
    profiles = config["profiles"]
    for name, profile in classes.items():
        if not isinstance(profile, str) or profile not in profiles:
            raise FirmError(f"staffing.classes.{name} must name a configured profile")


def founder_execution(config: dict) -> tuple[str, str | None]:
    """The founder's profile and effort override: explicit, or high class at high effort."""
    if config.get("founder_profile"):
        return config["founder_profile"], None
    return config["staffing"]["classes"][FOUNDER_LEVEL[0]], FOUNDER_LEVEL[1]


def hire_execution(config: dict) -> tuple[str, str | None]:
    """The profile and effort a hire holds before, or without, a chooser decision."""
    staffing = config.get("staffing")
    if staffing is False:
        return config.get("default_profile", config["founder_profile"]), None
    return staffing["classes"][FALLBACK_LEVEL[0]], FALLBACK_LEVEL[1]


def level_of(role: dict, config: dict) -> dict:
    """Describe a current role's level from its profile and effort override."""
    classes = (config.get("staffing") or {}).get("classes", {})
    by_profile = {profile: name for name, profile in classes.items()}
    profile = config["profiles"].get(role["execution_profile"], {})
    return {
        "model_class": by_profile.get(role["execution_profile"]),
        "effort": role["effort"] or profile.get("effort"),
    }


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER


def _mandate_body(db, employee_id: str) -> str:
    row = db.execute(
        "SELECT body FROM mandates WHERE employee_id=? ORDER BY revision DESC LIMIT 1",
        (employee_id,),
    ).fetchone()
    return row["body"] if row else ""


def build_prompt(
    db, store, role: dict, config: dict, *, level=True, guides=True
) -> tuple[str, int]:
    """Render the complete hire mandate with bounded surrounding context as JSON."""
    # A promoted appointment keeps its row with kind 'durable'.
    terms = db.execute(
        "SELECT * FROM appointments WHERE employee_id=? AND kind='temporary'",
        (role["employee_id"],),
    ).fetchone()
    manager = store.role(db, role["manager_id"]) if role["manager_id"] != OWNER else None

    def described(person: dict, *, excerpt: bool = False) -> dict:
        body = _mandate_body(db, person["employee_id"])
        item = {"address": person["address"], "level": level_of(person, config)}
        if excerpt:
            item["mandate_excerpt"] = _clip(body, MAX_EXCERPT_CHARS)
            item["mandate_excerpt_truncated"] = len(body) > MAX_EXCERPT_CHARS
        else:
            item["mandate"] = _clip(body, MAX_CONTEXT_CHARS)
            item["mandate_truncated"] = len(body) > MAX_CONTEXT_CHARS
        return item

    above = None
    reports = []
    total_reports = 0
    if manager is not None:
        above = (
            {"address": "owner", "note": "The firm's owner."}
            if manager["manager_id"] == OWNER
            else described(store.role(db, manager["manager_id"]))
        )
        report_filter = "manager_id=? AND employment!='retired' AND employee_id!=?"
        report_parameters = (manager["employee_id"], role["employee_id"])
        total_reports = db.execute(
            f"SELECT COUNT(*) AS count FROM roles WHERE {report_filter}", report_parameters
        ).fetchone()["count"]
        reports = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM roles WHERE "
                + report_filter
                + " ORDER BY created_at, address, employee_id LIMIT ?",
                (*report_parameters, MAX_REPORTS),
            )
        ]
    hire = {
        "address": role["address"],
        "employment": "temporary" if terms else "durable",
        # This is the authoritative work request. Context bounds apply to
        # surrounding roles only and must never rewrite the hire's mandate.
        "mandate": _mandate_body(db, role["employee_id"]),
    }
    if terms:
        hire["temporary_work_periods"] = terms["remaining_periods"] + terms["used_periods"]
    value = {
        "hire": hire,
        "hiring_manager": described(manager)
        if manager
        else {"address": "owner", "note": "The firm's owner."},
        "managers_manager": above,
        "existing_reports_of_hiring_manager": [described(r, excerpt=True) for r in reports],
        "existing_reports_total": total_reports,
        "existing_reports_included": len(reports),
        "existing_reports_omitted": total_reports - len(reports),
    }
    if level:
        value["levels_available"] = {"model_class": list(CLASSES), "effort": list(EFFORTS)}
    if guides:
        value["guidance_catalog"] = guidance.chooser_entries()
    revision = db.execute(
        "SELECT MAX(revision) FROM mandates WHERE employee_id=?", (role["employee_id"],)
    ).fetchone()[0]
    return encoded(value), revision


_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_choice(text: str) -> tuple[str, str, str]:
    """Extract (model_class, effort, reason) from the chooser's reply."""
    match = _OBJECT.search(text if isinstance(text, str) else "")
    if match is None:
        raise ValueError("chooser reply contains no JSON object")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("chooser reply is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("chooser reply must be a JSON object")
    return parse_level(value)


def parse_level(value: dict) -> tuple[str, str, str]:
    model_class, effort = value.get("model_class"), value.get("effort")
    if model_class not in CLASSES:
        raise ValueError("chooser reply has an unknown model_class")
    if effort not in EFFORTS:
        raise ValueError("chooser reply has an unknown effort")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("chooser reason must be a string")
    return model_class, effort, _clip(reason.strip(), MAX_CHOICE_REASON_CHARS)


def register(db, employee_id: str, now: float) -> None:
    """Record that a hire awaits the chooser before its first work period."""
    db.execute(
        "INSERT INTO hire_levels(employee_id,state,updated_at) VALUES(?,'pending',?)",
        (employee_id, now),
    )


class Levels:
    """Run pending hire assessments and apply their decisions durably."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        # A provider-free chooser for controlled execution and tests.
        self.chooser: Callable[[str], dict[str, Any]] | None = None
        self._inflight: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            self._closed = False

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()

    def record(self, db, employee_id: str) -> dict | None:
        row = db.execute("SELECT * FROM hire_levels WHERE employee_id=?", (employee_id,)).fetchone()
        return dict(row) if row else None

    def mark_routed(self, db, employee_id: str) -> None:
        db.execute(
            "UPDATE hire_levels SET state='routed',updated_at=? WHERE employee_id=?",
            (self.store.clock(), employee_id),
        )

    def can_select(self) -> bool:
        return self.chooser is not None or self.runtime.summary_service.provider is not None

    @staticmethod
    def _due(record, now):
        return (
            record is not None
            and record["state"] == "pending"
            and (record["retry_at"] is None or record["retry_at"] <= now)
        )

    def tick(self, *, background: bool = True) -> list[str]:
        """Start each employee's due decisions together, preserving independent retries."""
        with self.store.transaction() as db:
            if self.store.firm(db)["state"] != "running":
                return []
            staffing = self.runtime.accounts.config(db).get("staffing") is not False
            if not staffing:
                now = self.store.clock()
                model_class, effort = FALLBACK_LEVEL
                pending_levels = db.execute(
                    "SELECT h.employee_id,h.attempts,r.execution_profile FROM hire_levels h "
                    "JOIN roles r USING(employee_id) "
                    "WHERE h.state='pending' AND r.employment='active'"
                ).fetchall()
                db.execute(
                    "UPDATE hire_levels SET state='defaulted',retry_at=NULL,model_class=?,"
                    "effort=?,reason='staffing disabled',updated_at=? "
                    "WHERE state='pending' AND employee_id IN ("
                    "SELECT employee_id FROM roles WHERE employment='active')",
                    (model_class, effort, now),
                )
                for row in pending_levels:
                    analysis_history.record(
                        db,
                        now,
                        "level.defaulted",
                        row["employee_id"],
                        model_class=model_class,
                        effort=effort,
                        profile=row["execution_profile"],
                        reason="staffing disabled",
                        attempts=row["attempts"],
                    )
            if not staffing or not self.can_select():
                reason = "staffing disabled" if not staffing else "no isolated runner"
                pending = db.execute(
                    "SELECT g.employee_id,g.attempts FROM role_guidance g "
                    "JOIN roles r USING(employee_id) "
                    "WHERE g.state='pending' AND r.employment='active'"
                ).fetchall()
                db.execute(
                    "UPDATE role_guidance SET state='defaulted',retry_at=NULL,reason=? "
                    "WHERE state='pending' AND employee_id IN ("
                    "SELECT employee_id FROM roles WHERE employment='active')",
                    (reason,),
                )
                for row in pending:
                    analysis_history.record(
                        db,
                        self.store.clock(),
                        "guidance.defaulted",
                        row["employee_id"],
                        reason=reason,
                        attempts=row["attempts"],
                    )
            now = self.store.clock()
            due = [
                r["employee_id"]
                for r in db.execute(
                    "SELECT r.employee_id FROM roles r "
                    "LEFT JOIN hire_levels h USING(employee_id) "
                    "LEFT JOIN role_guidance g USING(employee_id) "
                    "WHERE r.employment='active' AND ((h.state='pending' AND "
                    "(h.retry_at IS NULL OR h.retry_at<=?)) OR (? AND g.state='pending' AND "
                    "(g.retry_at IS NULL OR g.retry_at<=?))) "
                    "ORDER BY COALESCE(h.updated_at,r.created_at),r.address",
                    (now, staffing, now),
                )
            ]
        with self._lock:
            if self._closed:
                return []
            due = [e for e in due if e not in self._inflight]
            self._inflight.update(due)
            if background:
                started = []
                for employee_id in due:
                    thread = threading.Thread(
                        target=self._assess, args=(employee_id,), daemon=True, name="firm-level"
                    )
                    self._threads.add(thread)
                    try:
                        thread.start()
                    except BaseException:
                        self._threads.discard(thread)
                        self._inflight.discard(employee_id)
                        raise
                    started.append(employee_id)
                return started
        for employee_id in due:
            self._assess(employee_id)
        return due

    def _assess(self, employee_id: str) -> None:
        level_request = guide_request = None
        try:
            try:
                with self.store.read() as db:
                    if self.store.firm(db)["state"] != "running":
                        return
                    role = self.store.role(db, employee_id)
                    config = self.runtime.accounts.config(db)
                    if role["employment"] != "active":
                        return
                    now = self.store.clock()
                    record = self.record(db, employee_id)
                    level_request = record if self._due(record, now) else None
                    # Staffing off stops guidance selection only; a pending level still fails
                    # and defaults through the usual bounded retries rather than staying pending.
                    staffing = config.get("staffing") is not False
                    guide_record = guidance.record(db, employee_id)
                    guide_request = (
                        guide_record if (staffing and self._due(guide_record, now)) else None
                    )
                    if level_request is None and guide_request is None:
                        return
                    prompt, _revision = build_prompt(
                        db,
                        self.store,
                        role,
                        config,
                        level=level_request is not None,
                        guides=guide_request is not None,
                    )
                value = self._choose(prompt)
            except (FirmError, ValueError) as exc:
                self._fail_decisions(
                    employee_id,
                    level_request,
                    guide_request,
                    str(exc),
                    retry=not isinstance(exc, SummaryInputTooLarge),
                )
                return
            parsed_level = parsed_guide = None
            level_error = guide_error = None
            if level_request is not None:
                try:
                    parsed_level = parse_level(value)
                except (FirmError, ValueError) as exc:
                    level_error = str(exc)
            if guide_request is not None:
                try:
                    selection = guidance.parse_selection(value, guidance.chooser_entries())
                    reason = value.get("reason", "")
                    if not isinstance(reason, str):
                        raise ValueError("chooser reason must be a string")
                    parsed_guide = (selection, _clip(reason.strip(), MAX_CHOICE_REASON_CHARS))
                except (FirmError, ValueError) as exc:
                    guide_error = str(exc)
            with self.store.transaction() as db:
                if self.store.firm(db)["state"] != "running":
                    return
                staffing = self.runtime.accounts.config(db).get("staffing") is not False
                if level_request is not None and level_error is None:
                    try:
                        self._apply(employee_id, *parsed_level, request=level_request, db=db)
                    except (FirmError, ValueError) as exc:
                        level_error = str(exc)
                if guide_request is not None:
                    if not staffing:
                        guidance.failed(
                            self.runtime,
                            employee_id,
                            guide_request,
                            "staffing disabled",
                            retry=False,
                            notice=False,
                            db=db,
                        )
                        guide_error = None
                    elif guide_error is None:
                        guidance.apply(
                            self.runtime, employee_id, guide_request, *parsed_guide, db=db
                        )
                if level_error or guide_error:
                    self._fail_decisions(
                        employee_id,
                        level_request if level_error else None,
                        guide_request if guide_error else None,
                        level_error or guide_error or "",
                        guide_reason=guide_error,
                        db=db,
                    )
        finally:
            with self._lock:
                self._inflight.discard(employee_id)
                self._threads.discard(threading.current_thread())

    def _fail_decisions(
        self,
        employee_id,
        level_request,
        guide_request,
        reason,
        *,
        retry=True,
        guide_reason=None,
        db=None,
    ):
        # Defer notices so a call that defaults both decisions sends one message.
        if db is None:
            with self.store.transaction() as tx:
                return self._fail_decisions(
                    employee_id,
                    level_request,
                    guide_request,
                    reason,
                    retry=retry,
                    guide_reason=guide_reason,
                    db=tx,
                )
        if self.store.firm(db)["state"] != "running":
            return
        level = (
            self._failed(
                employee_id, reason, retry=retry, request=level_request, notice=False, db=db
            )
            if level_request is not None
            else None
        )
        guide = (
            guidance.failed(
                self.runtime,
                employee_id,
                guide_request,
                guide_reason or reason,
                retry=retry,
                notice=False,
                db=db,
            )
            if guide_request is not None
            else None
        )
        if guide:
            guidance.default_notice(self.runtime, db, guide, level)
        elif level:
            role = self.store.role(db, employee_id)
            self.runtime.messaging.notice(
                db,
                [OWNER],
                render("notices", "level_default", address=role["address"], **level),
                subject=render("notices", "level_default_subject"),
            )

    def _choose(self, prompt: str) -> dict:
        check_prompt_size(prompt, MAX_STAFFING_PROMPT_BYTES)
        if self.chooser is not None:
            value = self.chooser(prompt)
            text = encoded(value)
        else:
            from .provider_support import level_instructions

            outcome = self.runtime.summary_service.run_isolated(
                prompt,
                kind="staffing",
                instructions=level_instructions(),
                reserve=self.runtime.accounts.reserve_staffing,
                max_input_bytes=MAX_STAFFING_PROMPT_BYTES,
                timeout=CHOOSER_TIMEOUT_SECONDS,
            )
            if outcome.get("reason") == "input_limit":
                raise SummaryInputTooLarge(
                    "staffing prompt exceeds the isolated runner's input limit"
                )
            if outcome["status"] != "completed":
                raise FirmError(outcome["reason"])
            text = outcome["text"]
        match = _OBJECT.search(text)
        if match is None:
            raise ValueError("chooser reply contains no JSON object")
        value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("chooser reply must be a JSON object")
        return value

    def _apply(self, employee_id, model_class, effort, reason, *, request, db=None) -> None:
        if db is None:
            with self.store.transaction() as tx:
                return self._apply(employee_id, model_class, effort, reason, request=request, db=tx)
        now = self.store.clock()
        record = self.record(db, employee_id)
        if (
            record is None
            or record["state"] != "pending"
            or self.store.role(db, employee_id)["employment"] != "active"
            or record["attempts"] != request["attempts"]
        ):
            return
        # The owner may have edited the configuration while the chooser ran.
        config = self.runtime.accounts.config(db)
        profile = (config.get("staffing") or {}).get("classes", {}).get(model_class)
        if profile is None or profile not in config.get("profiles", {}):
            raise FirmError(
                f"hiring level {model_class!r} no longer maps to a configured profile"
            )
        db.execute(
            "UPDATE roles SET execution_profile=?,effort=? WHERE employee_id=?",
            (profile, effort, employee_id),
        )
        db.execute(
            "UPDATE hire_levels SET state='chosen',attempts=attempts+1,retry_at=NULL,"
            "model_class=?,effort=?,reason=?,updated_at=? WHERE employee_id=?",
            (model_class, effort, reason, now, employee_id),
        )
        analysis_history.record(
            db,
            now,
            "level.chosen",
            employee_id,
            model_class=model_class,
            effort=effort,
            profile=profile,
            reason=_clip(reason, 500),
            attempts=record["attempts"] + 1,
        )

    def _failed(
        self, employee_id: str, reason: str, *, request, retry=True, notice=True, db=None
    ) -> dict | None:
        if db is None:
            with self.store.transaction() as tx:
                return self._failed(
                    employee_id, reason, request=request, retry=retry, notice=notice, db=tx
                )
        now = self.store.clock()
        record = self.record(db, employee_id)
        if (
            record is None
            or record["state"] != "pending"
            or self.store.role(db, employee_id)["employment"] != "active"
            or record["attempts"] != request["attempts"]
        ):
            return
        attempts = record["attempts"] + 1
        if retry and attempts < MAX_ATTEMPTS:
            db.execute(
                "UPDATE hire_levels SET attempts=?,retry_at=?,reason=?,updated_at=? "
                "WHERE employee_id=?",
                (attempts, now + RETRY_DELAY_SECONDS * attempts, reason, now, employee_id),
            )
            return
        role = self.store.role(db, employee_id)
        model_class, effort = FALLBACK_LEVEL
        db.execute(
            "UPDATE hire_levels SET state='defaulted',attempts=?,retry_at=NULL,"
            "model_class=?,effort=?,reason=?,updated_at=? WHERE employee_id=?",
            (attempts, model_class, effort, reason, now, employee_id),
        )
        analysis_history.record(
            db,
            now,
            "level.defaulted",
            employee_id,
            model_class=model_class,
            effort=effort,
            profile=role["execution_profile"],
            reason=_clip(reason, 500),
            attempts=attempts,
        )
        result = {
            "model_class": model_class,
            "effort": effort,
            "reason": reason,
            "attempts": attempts,
        }
        if notice:
            self.runtime.messaging.notice(
                db,
                [OWNER],
                render("notices", "level_default", address=role["address"], **result),
                subject=render("notices", "level_default_subject"),
            )
        return result
