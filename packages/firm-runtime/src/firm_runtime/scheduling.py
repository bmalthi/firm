"""One projection of outstanding reasons to run; admission acknowledges them together."""

SCHEMA = """
CREATE VIEW IF NOT EXISTS scheduling_opportunities AS
    SELECT employee_id,reason,priority,ready_at,created_at,source_id,
           request_id AS execution_request_id
    FROM execution_requests
    UNION ALL
    SELECT d.employee_id,'message',d.scheduling_priority,d.scheduling_ready_at,
           m.created_at,d.message_id,NULL
    FROM message_deliveries d JOIN messages m USING(message_id)
    WHERE d.disposition='pending' AND d.scheduling_ready_at IS NOT NULL;
"""


def acknowledge_opportunities(db, employee_id: str, now: float) -> None:
    """Use inside the admission transaction, after saving its reason snapshot.

    NULL ready time means the delivery no longer asks for an opportunity. Its
    inbox disposition is independent: admission never claims or dismisses mail.
    New arrivals after this transaction retain their own scheduling ready time.
    """
    db.execute(
        "UPDATE message_deliveries SET scheduling_ready_at=NULL "
        "WHERE employee_id=? AND disposition='pending' AND scheduling_ready_at<=?",
        (employee_id, now),
    )
    db.execute(
        "DELETE FROM execution_requests WHERE employee_id=? AND ready_at<=?",
        (employee_id, now),
    )
