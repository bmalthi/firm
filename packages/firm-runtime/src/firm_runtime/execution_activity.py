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
            "SELECT s.idle_timeout,s.output_digest FROM sandbox_runs s "
            "JOIN activations a USING(activation_id) "
            "WHERE activation_id=? AND s.stop_cause IS NULL "
            "AND a.state IN ('admitted','running')",
            (activation_id,),
        ).fetchone()
        if activity is None or (digest is not None and digest == activity["output_digest"]):
            return
        db.execute(
            "UPDATE sandbox_runs SET deadline=MAX(deadline,?), "
            "output_digest=COALESCE(?,output_digest) WHERE activation_id=?",
            (store.clock() + activity["idle_timeout"], digest, activation_id),
        )
