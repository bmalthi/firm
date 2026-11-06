"""Temporary appointments counted in work periods; renewal never replaces an employee's identity."""

from . import analysis_history
from .errors import FirmError

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    employee_id TEXT PRIMARY KEY REFERENCES roles(employee_id),
    kind TEXT NOT NULL CHECK(kind IN ('temporary','durable')),
    remaining_periods INTEGER NOT NULL CHECK(remaining_periods>=0),
    used_periods INTEGER NOT NULL DEFAULT 0
);
"""


def validate_periods(periods):
    if type(periods) is not int or not 1 <= periods <= 1000:
        raise FirmError("temporary work periods must be an explicit integer from 1 to 1000")


class Appointments:
    def __init__(self, store, messaging):
        self.store, self.messaging = store, messaging

    def terms(self, db, employee_id):
        row = db.execute(
            "SELECT * FROM appointments WHERE employee_id=?", (employee_id,)
        ).fetchone()
        return dict(row) if row else {"employee_id": employee_id, "kind": "durable"}

    def eligible(self, db, employee_id):
        terms = self.terms(db, employee_id)
        return terms["kind"] == "durable" or terms["remaining_periods"] > 0

    def consume(self, db, employee_id):
        terms = self.terms(db, employee_id)
        if terms["kind"] == "durable":
            return
        if terms["remaining_periods"] <= 0:
            raise FirmError("temporary employee needs renewal before another work period")
        db.execute(
            "UPDATE appointments SET remaining_periods=remaining_periods-1,"
            "used_periods=used_periods+1 WHERE employee_id=?",
            (employee_id,),
        )

        analysis_history.appointment_started(db, self.store.clock(), employee_id)

    def renew(self, actor_id, employee_id, periods, request_id):
        validate_periods(periods)
        return self._change(actor_id, employee_id, request_id, periods)

    def promote(self, actor_id, employee_id, request_id):
        return self._change(actor_id, employee_id, request_id, None)

    def _change(self, actor_id, employee_id, request_id, periods):
        operation = "promote" if periods is None else "renew_temporary"
        with self.store.transaction() as db:
            self.store.require_actor(db, actor_id)

            def change():
                role = self.store.role(db, employee_id)
                if role["manager_id"] != actor_id:
                    raise FirmError("only the direct manager may renew or promote this employee")
                if role["employment"] == "retired":
                    raise FirmError("retired employees cannot be renewed or promoted")
                terms = self.terms(db, employee_id)
                if terms["kind"] != "temporary":
                    raise FirmError("employee is already durable")
                if periods is None:
                    db.execute(
                        "UPDATE appointments SET kind='durable' WHERE employee_id=?",
                        (employee_id,),
                    )
                else:
                    if terms["remaining_periods"] + periods > 1000:
                        raise FirmError("temporary allocation cannot exceed 1000 remaining periods")
                    db.execute(
                        "UPDATE appointments SET remaining_periods=remaining_periods+? "
                        "WHERE employee_id=?",
                        (periods, employee_id),
                    )
                if terms["remaining_periods"] == 0:
                    self.store.request_execution(db, employee_id, operation)
                analysis_history.record(
                    db,
                    self.store.clock(),
                    "appointment.promoted" if periods is None else "appointment.renewed",
                    employee_id,
                    before=terms,
                    added_periods=periods,
                )
                return {
                    **self.terms(db, employee_id),
                    "status": "updated",
                    "identity_and_context": "preserved",
                    "other_holds": "preserved",
                }

            return self.store.request(
                db,
                actor_id,
                request_id,
                operation,
                {"employee_id": employee_id, "periods": periods},
                change,
            )
