"""Role guidance catalog, effective selections and guarded chooser state."""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files

from . import analysis_history
from .errors import FirmError
from .prompt_resources import prompt, render
from .store import OWNER
from .store import encoded as encode_json

ORG_CHART_SCOPES = ("firm-leader", "team-leader")
_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


@cache
def catalog() -> list[dict]:
    """Validate every packaged entry and body before returning the ordered catalog."""
    directory = files("firm_runtime").joinpath("prompts", "guidance")
    try:
        entries = json.loads(directory.joinpath("catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FirmError(f"guidance catalog: {exc}") from exc
    if not isinstance(entries, list):
        raise FirmError("guidance catalog must be a list")
    seen = set()
    for entry in entries:
        label = entry.get("id", repr(entry)) if isinstance(entry, dict) else repr(entry)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "dimension", "title", "applies"}
            or not isinstance(entry["id"], str)
            or not _ID.fullmatch(entry["id"])
            or entry["id"] in seen
            or entry["dimension"] not in ("scope", "function")
            or any(
                not isinstance(entry[k], str) or not entry[k].strip() for k in ("title", "applies")
            )
        ):
            raise FirmError(f"invalid guidance entry: {label}")
        seen.add(entry["id"])
        try:
            text = directory.joinpath(entry["id"] + ".md").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FirmError(f"guidance {label}: cannot read body") from exc
        if not text.strip() or not text.startswith("### "):
            raise FirmError(f"guidance {label}: body must start with ### ")
    for guide_id in ORG_CHART_SCOPES:
        if not any(e["id"] == guide_id and e["dimension"] == "scope" for e in entries):
            raise FirmError(f"guidance {guide_id}: missing org-chart scope")
    expected = {"catalog.json", *(guide_id + ".md" for guide_id in seen)}
    for resource in directory.iterdir():
        if resource.name not in expected or not resource.is_file():
            raise FirmError(f"guidance {resource.name}: orphan body or unexpected resource")
    return entries


def chooser_entries() -> list[dict]:
    return [entry for entry in catalog() if entry["id"] not in ORG_CHART_SCOPES]


def body(guide_id: str) -> str:
    if guide_id not in {e["id"] for e in catalog()}:
        raise FirmError(f"unknown guidance ID: {guide_id}")
    return prompt(f"guidance/{guide_id}.md")


def parse_selection(value: dict, entries: list[dict]) -> dict:
    """Validate dimensions and normalize function IDs to catalog order."""
    if not isinstance(value, dict) or not {"scope_guidance", "functional_guidance"} <= value.keys():
        raise ValueError("guidance reply requires scope_guidance and functional_guidance")
    scope, functions = value["scope_guidance"], value["functional_guidance"]
    scopes = {e["id"] for e in entries if e["dimension"] == "scope"}
    ordered = [e["id"] for e in entries if e["dimension"] == "function"]
    if scope is not None and (not isinstance(scope, str) or scope not in scopes):
        raise ValueError("guidance reply has an unknown or wrong-dimension scope_guidance")
    if not isinstance(functions, list) or any(
        not isinstance(f, str) or f not in ordered for f in functions
    ):
        raise ValueError("guidance reply has unknown or wrong-dimension functional_guidance")
    return {"scope": scope, "functions": [f for f in ordered if f in functions]}


def effective(db, role, config: dict) -> dict:
    """Compute delivery from the current org chart, staffing and owner override."""
    if role["guidance_state"] == "owner":
        return {
            "scope": role["scope_guidance"],
            "functions": json.loads(role["functional_guidance"]),
        }
    scope = None
    if role["manager_id"] == OWNER:
        scope = "firm-leader"
    elif db.execute(
        "SELECT 1 FROM roles WHERE manager_id=? AND employment!='retired' LIMIT 1",
        (role["employee_id"],),
    ).fetchone():
        scope = "team-leader"
    if config.get("staffing") is False:
        return {"scope": scope, "functions": []}
    stored = role["scope_guidance"]
    scopes = {e["id"] for e in chooser_entries() if e["dimension"] == "scope"}
    return {
        "scope": scope or (stored if stored in scopes else None),
        "functions": json.loads(role["functional_guidance"]),
    }


def encoded(selection: dict | None) -> str:
    return encode_json(selection or {"scope": None, "functions": []})


def view(db, role, config: dict) -> dict:
    selection = effective(db, role, config)
    scope = selection["scope"]
    return {
        "state": role["guidance_state"],
        **selection,
        "source": "owner"
        if role["guidance_state"] == "owner"
        else "org_chart"
        if scope in ORG_CHART_SCOPES
        else "chooser"
        if scope
        else None,
        "reason": role["guidance_reason"],
    }


def describe(selection: dict) -> str:
    ids = ([selection["scope"]] if selection["scope"] else []) + selection["functions"]
    titles = {e["id"]: e["title"] for e in catalog()}
    return ", ".join(titles[i] for i in ids) or "none"


def register(db, employee_id: str, revision: int = 0) -> None:
    db.execute(
        "UPDATE roles SET guidance_state='pending',guidance_revision=?,guidance_attempts=0,"
        "guidance_retry_at=NULL,guidance_reason=NULL WHERE employee_id=?",
        (revision, employee_id),
    )


def request_reassessment(db, employee_id: str, revision: int) -> None:
    role = db.execute(
        "SELECT guidance_state FROM roles WHERE employee_id=?", (employee_id,)
    ).fetchone()
    if role["guidance_state"] != "owner":
        register(db, employee_id, revision)


def _current(db, employee_id, request):
    row = db.execute("SELECT * FROM roles WHERE employee_id=?", (employee_id,)).fetchone()
    if (
        row
        and row["employment"] == "active"
        and row["guidance_state"] == "pending"
        and row["guidance_revision"] == request["guidance_revision"]
        and row["guidance_attempts"] == request["guidance_attempts"]
    ):
        return dict(row)
    return None


def apply(runtime, employee_id, request, selection, reason) -> bool:
    from .levels import _clip

    with runtime.store.transaction() as db:
        if not _current(db, employee_id, request):
            return False
        db.execute(
            "UPDATE roles SET scope_guidance=?,functional_guidance=?,guidance_state='chosen',"
            "guidance_attempts=guidance_attempts+1,guidance_retry_at=NULL,guidance_reason=? "
            "WHERE employee_id=?",
            (selection["scope"], encode_json(selection["functions"]), reason, employee_id),
        )
        analysis_history.record(
            db,
            runtime.store.clock(),
            "guidance.chosen",
            employee_id,
            **selection,
            reason=_clip(reason, 500),
            attempts=request["guidance_attempts"] + 1,
        )
        return True


def failed(runtime, employee_id, request, reason, *, retry=True, notice=True) -> dict | None:
    """Charge only the pending request captured before the call; return a defaulted role."""
    from .levels import MAX_ATTEMPTS, RETRY_DELAY_SECONDS, _clip

    with runtime.store.transaction() as db:
        role = _current(db, employee_id, request)
        if role is None:
            return None
        attempts = role["guidance_attempts"] + 1
        pending = retry and attempts < MAX_ATTEMPTS
        db.execute(
            "UPDATE roles SET guidance_state=?,guidance_attempts=?,guidance_retry_at=?,"
            "guidance_reason=? WHERE employee_id=?",
            (
                "pending" if pending else "defaulted",
                attempts,
                runtime.store.clock() + RETRY_DELAY_SECONDS * attempts if pending else None,
                reason,
                employee_id,
            ),
        )
        if pending:
            return None
        analysis_history.record(
            db,
            runtime.store.clock(),
            "guidance.defaulted",
            employee_id,
            attempts=attempts,
            reason=_clip(reason, 500),
        )
        role.update(guidance_state="defaulted", guidance_attempts=attempts, guidance_reason=reason)
        if notice:
            default_notice(runtime, db, role)
        return role


def default_notice(runtime, db, role, level=None):
    selection = describe(effective(db, role, runtime.accounts.config(db)))
    runtime.messaging.notice(
        db,
        [OWNER],
        render(
            "notices",
            "level_and_guidance_default" if level else "guidance_default",
            address=role["address"],
            attempts=role["guidance_attempts"],
            reason=role["guidance_reason"],
            selection=selection,
            model_class=level["model_class"] if level else "",
            effort=level["effort"] if level else "",
        ),
        subject=render("notices", "level_default_subject" if level else "guidance_default_subject"),
    )


def reconcile(db, runtime) -> None:
    known = {e["id"] for e in catalog()}
    config = runtime.accounts.config(db)
    for row in db.execute("SELECT * FROM roles WHERE employment!='retired'").fetchall():
        role = dict(row)
        functions = json.loads(role["functional_guidance"])
        removed = [i for i in [role["scope_guidance"], *functions] if i and i not in known]
        if not removed:
            continue
        db.execute(
            "UPDATE roles SET scope_guidance=?,functional_guidance=? WHERE employee_id=?",
            (
                role["scope_guidance"] if role["scope_guidance"] in known else None,
                encode_json([i for i in functions if i in known]),
                role["employee_id"],
            ),
        )
        owner = role["guidance_state"] == "owner"
        staffing = config.get("staffing") is not False
        if not owner and staffing:
            revision = db.execute(
                "SELECT MAX(revision) FROM mandates WHERE employee_id=?", (role["employee_id"],)
            ).fetchone()[0]
            register(db, role["employee_id"], revision)
        analysis_history.record(
            db, runtime.store.clock(), "guidance.removed", role["employee_id"], removed=removed
        )
        if not owner and not staffing:
            # Staffing off delivers no stored automatic selection, so nothing the role
            # receives changed and there is nothing to tell the owner.
            continue
        runtime.messaging.notice(
            db,
            [OWNER],
            render(
                "notices",
                "guidance_removed",
                removed=", ".join(removed),
                address=role["address"],
                next=render(
                    "notices", "guidance_removed_owner" if owner else "guidance_removed_automatic"
                ),
            ),
            subject=render("notices", "guidance_removed_subject"),
        )
