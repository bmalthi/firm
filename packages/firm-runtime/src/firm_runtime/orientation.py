"""Bounded organization facts supplied when a role begins or its neighborhood changes."""

import json
import sqlite3

from .discovery import compact_role
from .errors import FirmError
from .lifecycle import FOUNDER_ID
from .store import Store, encoded

MAX_NEIGHBORS = 20
MAX_ORIENTATION_BYTES = 16 * 1024


def _compact(store: Store, db: sqlite3.Connection, role: dict) -> dict:
    result = compact_role(store, db, role)
    result["inspect"] = f'get_role(role="{role["employee_id"]}")'
    return result


def _current_roles(db: sqlite3.Connection, clause: str, parameters: tuple) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM roles WHERE employment!='retired' AND "
        + clause
        + " ORDER BY address,identity,employee_id",
        parameters,
    )
    return [dict(row) for row in rows]


def build_orientation(
    store: Store,
    db: sqlite3.Connection,
    employee_id: str,
    *,
    include_top_level: bool,
) -> dict:
    """Build one stable, bounded view from the current role registry."""
    role = store.role(db, employee_id)
    manager = store.resolve(db, role["manager_id"])
    direct = _current_roles(db, "manager_id=?", (employee_id,))
    direct_ids = {item["employee_id"] for item in direct}
    top_level = (
        [
            item
            for item in _current_roles(db, "manager_id=?", (FOUNDER_ID,))
            if item["employee_id"] not in {employee_id, role["manager_id"]}
            and item["employee_id"] not in direct_ids
        ]
        if include_top_level
        else []
    )
    block = {
        "manager": _compact(store, db, manager) if manager is not None else None,
        "direct_reports": [],
        "top_level_roles": [],
        "top_level_included": include_top_level,
        "omitted": {
            "direct_reports": len(direct),
            "top_level_roles": len(top_level),
        },
        "references": {
            "inspect_known_role": 'get_role(role="<employee_id>")',
            "organization_browse": 'org_tree(root="<current-address>", depth=2)',
            "responsibility_search": 'find_roles(query="<responsibility>")',
        },
    }
    if len(encoded(block).encode()) > MAX_ORIENTATION_BYTES:
        raise FirmError("organization context metadata exceeds 16 KiB")

    shown = 0
    for section, candidates in (("direct_reports", direct), ("top_level_roles", top_level)):
        for candidate in candidates:
            if shown == MAX_NEIGHBORS:
                break
            item = _compact(store, db, candidate)
            block[section].append(item)
            block["omitted"][section] -= 1
            if len(encoded(block).encode()) > MAX_ORIENTATION_BYTES:
                block[section].pop()
                block["omitted"][section] += 1
                break
            shown += 1
    return block


def _last_supplied_orientation(db: sqlite3.Connection, employee_id: str) -> dict | None:
    rows = db.execute(
        "SELECT context_json FROM activations WHERE employee_id=? AND started_at IS NOT NULL "
        "ORDER BY started_at DESC,created_at DESC",
        (employee_id,),
    )
    for row in rows:
        orientation = json.loads(row["context_json"]).get("orientation")
        if orientation is not None:
            return orientation
    return None


def orientation_for_admission(
    store: Store,
    db: sqlite3.Connection,
    employee_id: str,
    *,
    birth: bool,
    fresh_session: bool,
) -> dict | None:
    """Return initial/current facts only when this invocation must receive them."""
    previous = None if fresh_session else _last_supplied_orientation(db, employee_id)
    include_top_level = (
        birth or fresh_session or bool(previous and previous.get("top_level_included"))
    )
    current = build_orientation(
        store,
        db,
        employee_id,
        include_top_level=include_top_level,
    )
    if fresh_session or previous != current:
        return current
    return None
