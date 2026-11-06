"""Ingest role-authored public profiles from the firm's file server."""

from __future__ import annotations

from .discovery import identity
from .errors import FirmError
from .store import Store
from .workspaces import sandbox_role_id


def ingest_public_profiles(store: Store, *, files) -> dict:
    """Refresh public profile state only; this intentionally creates no wake requests."""
    with store.read() as db:
        employee_ids = [
            row["employee_id"]
            for row in db.execute("SELECT employee_id FROM roles WHERE employment!='retired'")
        ]

    profiles = files.profiles()
    if not isinstance(profiles, dict):
        raise FirmError("profile scan returned invalid results")
    observed = {}
    for employee_id in employee_ids:
        result = profiles.get(sandbox_role_id(employee_id), {"status": "missing"})
        if not isinstance(result, dict) or result.get("status") not in (
            "available",
            "missing",
            "unreadable",
        ):
            raise FirmError("profile scan returned an invalid status")
        status = result["status"]
        profile = result.get("text", "")
        digest = result.get("sha256")
        if not isinstance(profile, str) or (digest is not None and not isinstance(digest, str)):
            raise FirmError("profile scan returned invalid content")
        if status == "available" and digest is None:
            raise FirmError("available profile scan is missing its digest")
        if status != "available":
            profile, digest = "", None
        observed[employee_id] = status, profile, digest
    changed = 0
    with store.transaction() as db:
        for employee_id, (status, profile, digest) in observed.items():
            role = store.role(db, employee_id)
            if role["employment"] == "retired":
                continue
            display_identity = identity(
                {
                    **role,
                    "public_profile": profile if status == "available" else "",
                }
            )
            result = db.execute(
                "UPDATE roles SET public_profile_status=?,public_profile=?,"
                "public_profile_digest=?,identity=? "
                "WHERE employee_id=? AND employment!='retired' "
                "AND (public_profile_status!=? OR public_profile!=? "
                "OR public_profile_digest IS NOT ? OR identity!=?)",
                (
                    status,
                    profile,
                    digest,
                    display_identity,
                    employee_id,
                    status,
                    profile,
                    digest,
                    display_identity,
                ),
            )
            changed += result.rowcount
    counts = {"available": 0, "missing": 0, "unreadable": 0}
    for status, _, _ in observed.values():
        counts[status] += 1
    return {"scanned": len(observed), "changed": changed, "statuses": counts}
