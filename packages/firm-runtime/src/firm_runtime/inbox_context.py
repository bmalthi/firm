"""Read-only context, appointment and Mac-disk notices for self and selected direct reports."""

import math

from .analysis import disk_state
from .store import encoded

# A quiet provider does not make its last measurement a fresh observation.
STALE_AFTER_SECONDS = 5 * 60


def overview(db, actor_id: str, now: float, *, byte_limit: int) -> dict | None:
    actor = db.execute("SELECT * FROM roles WHERE employee_id=?", (actor_id,)).fetchone()
    if actor is None:  # Owner/Operations are inbox actors, not employees.
        return None
    reports = db.execute(
        "SELECT * FROM roles WHERE manager_id=? AND employment!='retired' ORDER BY employee_id",
        (actor_id,),
    ).fetchall()
    relevant = []
    for role in reports:
        row = _facts(db, role, now)
        temporary = row.get("temporary_periods")
        near_limit = temporary is not None and temporary["periods_after_current"] <= 1
        level = row["context"]["level"]
        if near_limit or level in {"advisory", "handoff_required"} or row["activity"] != "idle":
            priority = (not near_limit, level != "handoff_required", level != "advisory")
            relevant.append((priority, role["employee_id"], row))
    relevant.sort(key=lambda item: (item[0], item[1]))
    result = {
        "self": _facts(db, actor, now),
        "direct_reports": [],
        "relevant_reports": len(relevant),
        "omitted_relevant_reports": len(relevant),
        "other_direct_reports": len(reports) - len(relevant),
        "notices": _mac_disk_notice(db, actor_id, now),
    }
    for _, _, row in relevant:
        result["direct_reports"].append(row)
        result["omitted_relevant_reports"] -= 1
        if len(encoded(result).encode("utf-8")) > byte_limit:
            result["direct_reports"].pop()
            result["omitted_relevant_reports"] += 1
            break
    return result


def _mac_disk_notice(db, actor_id, now):
    """A notice while the Mac the actor holds is low on disk, from its current sample.

    The first producer for the ``notices`` hook (``role_output._awareness``, rendered
    on inbox peek). It reads the machine's latest resource sample through the one
    disk-state calculation, not the lease's own acquire reading: acquire refuses a full
    Mac, so that figure is never low. Work in any station account can fill their
    shared disk. A stale or unknown reading says nothing.
    """
    lease = db.execute(
        "SELECT 1 FROM mac_leases WHERE employee_id=? AND released_at IS NULL",
        (actor_id,),
    ).fetchone()
    if lease is None:
        return []
    sample = db.execute(
        "SELECT disk_used_bytes,disk_total_bytes,observed_at FROM resource_samples "
        "WHERE machine=? ORDER BY sequence DESC LIMIT 1",
        ("mac",),
    ).fetchone()
    state = disk_state(dict(sample), now) if sample is not None else None
    if state is None or not state["low_disk"]:
        return []
    return [
        f"The Mac you hold is low on disk: about {state['free_bytes'] / (1024**3):.1f} GiB "
        "free. Delete what you no longer need in your scratch folder."
    ]


def _facts(db, role, now: float) -> dict:
    employee_id = role["employee_id"]
    active = db.execute(
        "SELECT * FROM activations WHERE employee_id=? AND state IN ('admitted','running')",
        (employee_id,),
    ).fetchone()
    observation = None
    if active is not None:
        observation = db.execute(
            "SELECT * FROM activation_usage WHERE activation_id=?", (active["activation_id"],)
        ).fetchone()
    session = db.execute(
        "SELECT * FROM context_sessions WHERE employee_id=? "
        "ORDER BY observed_at DESC, session_id LIMIT 1",
        (employee_id,),
    ).fetchone()
    current = observation is not None and observation["current_context_tokens"] is not None
    if current:
        session = db.execute(
            "SELECT * FROM context_sessions WHERE session_id=?", (observation["session_id"],)
        ).fetchone()
    else:
        observation = session
    tokens = observation["current_context_tokens"] if observation else None
    measured_at = observation["observed_at"] if observation and tokens is not None else None
    age = max(0.0, now - measured_at) if measured_at is not None else None
    window = (
        session["soft_context_window"] or session["provider_context_window"] if session else None
    )
    level = "unknown"
    if tokens is not None and window is not None:
        if tokens >= max(0, window - session["handoff_reserve"]):
            level = "handoff_required"
        elif tokens >= math.ceil(window * session["advisory_fraction"]):
            level = "advisory"
        else:
            level = "normal"
    context = {
        "state": (
            "unknown"
            if tokens is None
            else "stale"
            if not current or age > STALE_AFTER_SECONDS
            else "measured"
        ),
        "level": level,
        "observed_tokens": tokens,
        "window_tokens": window,
        "window_source": (
            "configured_soft_window"
            if session and session["soft_context_window"]
            else "provider_window"
            if window
            else "unknown"
        ),
        "measured_at": measured_at,
        "age_seconds": age,
    }
    if tokens is not None and not current:
        context["stale_reason"] = "no_current_work_period_measurement"
    elif tokens is not None and age > STALE_AFTER_SECONDS:
        context["stale_reason"] = "measurement_age"
    result = {
        "employee_id": employee_id,
        "address": role["address"][:96],
        "activity": active["state"] if active else "idle",
        "context": context,
    }
    if len(role["address"]) > 96:
        result["address_truncated"] = True
    terms = db.execute(
        "SELECT * FROM appointments WHERE employee_id=? AND kind='temporary'", (employee_id,)
    ).fetchone()
    if terms is not None:
        result["temporary_periods"] = {
            "unspent_periods": terms["remaining_periods"],
            "periods_after_current": max(
                0,
                terms["remaining_periods"]
                - int(active is not None and active["state"] == "admitted"),
            ),
            "final_work_period_running": bool(
                active is not None
                and active["state"] == "running"
                and terms["remaining_periods"] == 0
            ),
        }
    return result
