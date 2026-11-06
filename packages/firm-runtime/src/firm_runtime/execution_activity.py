"""Durable inactivity deadlines, refreshed only by observed role activity."""

import hashlib

DEFAULT_IDLE_TIMEOUT = 30 * 60


def record_activity(store, activation_id: str, *, output: bytes | None = None) -> None:
    # Empty/repeated log snapshots are polling, not progress. Hash content rather
    # than length so rotated logs and equal-sized output changes are observable.
    if output == b"":
        return
    digest = hashlib.sha256(output).hexdigest() if output is not None else None
    with store.transaction() as db:
        activity = db.execute(
            "SELECT e.* FROM execution_activity e "
            "JOIN sandbox_runs s USING(activation_id) JOIN activations a USING(activation_id) "
            "WHERE activation_id=? AND s.stop_cause IS NULL "
            "AND a.state IN ('admitted','running')",
            (activation_id,),
        ).fetchone()
        if activity is None or (digest is not None and digest == activity["output_digest"]):
            return
        db.execute(
            "UPDATE sandbox_runs SET deadline=MAX(deadline,?) WHERE activation_id=?",
            (store.clock() + activity["idle_timeout"], activation_id),
        )
        if digest is not None:
            db.execute(
                "UPDATE execution_activity SET output_digest=? WHERE activation_id=?",
                (digest, activation_id),
            )
