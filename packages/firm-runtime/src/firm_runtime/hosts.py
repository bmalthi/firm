"""Network hosts the owner granted: the record, and the sandbox that enforces it.

The runtime keeps the record of granted hosts; the sandbox enforces them. Every owner
decision writes the whole recorded set to the sandbox inside the write transaction that
records it. The transaction is IMMEDIATE, so a second owner command or the host waits its
turn instead of pushing a stale set, and a failed write rolls the decision back. A write
that half applied before failing is undone by the next decision's write, which is always
the record.
"""

import json
import logging
import re

from .errors import FirmError
from .requests import AskOwner, require_owner

LOGGER = logging.getLogger(__name__)
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# Lower-case DNS names of two or more labels whose last label is not numeric, so an IP
# literal, a port or a bare top-level wildcard never passes.
HOST = re.compile(rf"(?:\*\.)?(?:{LABEL}\.)+(?!\d+$){LABEL}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS network_hosts (
    host TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('request','owner')),
    granted_at REAL NOT NULL
);
"""


def valid_host(host):
    """The host as the allowlist stores it, or None: lower-case, no port, no IP literal."""
    if not isinstance(host, str):
        return None
    host = host.strip().lower()
    return host if len(host) <= 253 and HOST.fullmatch(host) else None


def covered(host, allowed):
    """Whether an allowlist (exact names and ``*.suffix`` entries) already admits ``host``."""
    return any(
        host == entry or entry.startswith("*.") and host.endswith(entry[1:]) for entry in allowed
    )


def granted_hosts(db):
    return [
        dict(row)
        for row in db.execute("SELECT host,source,granted_at FROM network_hosts ORDER BY host")
    ]


class NetworkHosts:
    """The owner request kind for hosts; ``sandbox`` enforces what is granted."""

    name = "network_host"

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        # The execution host attaches its sandbox; an owner command attaches its own.
        self.sandbox = None

    def network_status(self):
        """The sandbox's live network view; raises when no sandbox is attached."""
        if self.sandbox is None:
            raise RuntimeError("no sandbox is attached to this runtime")
        return self.sandbox.network_status()

    def configured_policy(self):
        """The sandbox's mode and own entries, never its granted entries.

        A granted host the sandbox reports but the record lacks is a write that failed
        part way; only the record says what the owner granted.
        """
        if self.sandbox is None:
            return None, set()
        try:
            status = self.network_status()
            allowed = {
                entry["host"]
                for entry in status["allowed_hosts"]
                if entry["source"] != "granted"
            }
            return status.get("mode"), allowed
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            LOGGER.warning("The sandbox's allowed hosts are unavailable; using grants only")
            return None, set()

    def _apply(self, db, hosts):
        """Write a granted set inside the caller's write transaction; a failure aborts it."""
        if not db.in_transaction:
            raise RuntimeError("granted hosts are written only under the write lock")
        if self.sandbox is not None:
            self.sandbox.set_allowed_hosts(sorted(hosts))

    def push(self):
        """Write the recorded granted set, holding the write lock so no decision interleaves."""
        if self.sandbox is None:
            return
        with self.store.transaction() as db:
            self._apply(db, {row["host"] for row in granted_hosts(db)})

    # ------------------------------------------------------------------ roles

    def ask(self, actor, host, purpose):
        """Grant what the network policy permits; otherwise honor denials or ask the owner."""
        normalized = valid_host(host)
        if normalized is None:
            return {"status": "refused", "reason": "access_resource_invalid"}
        mode, allowed = self.configured_policy()
        if mode == "observe":
            return {"status": "granted", "resource": normalized}
        with self.store.read() as db:
            if covered(normalized, allowed | {row["host"] for row in granted_hosts(db)}):
                return {"status": "granted", "resource": normalized}
            # A denied *.suffix covers every host under it, as a grant would.
            denied = next(
                (
                    row
                    for row in db.execute(
                        "SELECT payload_json,decision_note FROM owner_requests "
                        "WHERE kind=? AND status='denied' ORDER BY rowid DESC",
                        (self.name,),
                    )
                    if covered(normalized, {json.loads(row["payload_json"])["resource"]})
                ),
                None,
            )
        if denied is not None:
            return {
                "status": "refused",
                "reason": "access_denied",
                "resource": normalized,
                "owner_note": denied["decision_note"],
            }
        return AskOwner(normalized, "network_host_not_allowed")

    # ------------------------------------------------------------------ owner

    def grant(self, row):
        """Grant the host a request asked for, and answer every request it covers."""
        with self.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            return self._allow(db, json.loads(row["payload_json"])["resource"], "request")

    def allow(self, actor, host):
        """Grant a host firm-wide and answer every role waiting on it."""
        require_owner(actor)
        normalized = valid_host(host)
        if normalized is None:
            raise FirmError(
                "host must be a lower-case DNS name such as api.example.com or *.example.com, "
                "with no port or IP address"
            )
        with self.store.transaction() as db:
            return self._allow(db, normalized, "owner")

    def _allow(self, db, host, source):
        hosts = {row["host"] for row in granted_hosts(db)} | {host}
        self._apply(db, hosts)
        db.execute(
            "INSERT OR IGNORE INTO network_hosts VALUES(?,?,?)", (host, source, self.store.clock())
        )
        answered = self._answer(db, host, "granted")
        return {"resource": host, "status": "granted", "answered": answered,
                "granted_hosts": sorted(hosts)}

    def deny(self, row, note):
        """Decline a host for the firm: every role waiting on a host it covers is told why."""
        with self.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            host = json.loads(row["payload_json"])["resource"]
            # Removes the host from any machine where an earlier failed grant left it.
            self._apply(db, {item["host"] for item in granted_hosts(db)})
            answered = self._answer(db, host, "denied", note)
        return {"request_id": row["request_id"], "resource": host, "status": "denied",
                "answered": answered}

    def _answer(self, db, host, status, note=None):
        """Decide every pending request ``host`` covers."""
        rows = [
            row
            for row in db.execute(
                "SELECT * FROM owner_requests WHERE kind=? AND status='pending' ORDER BY rowid",
                (self.name,),
            )
            if covered(json.loads(row["payload_json"])["resource"], {host})
        ]
        return self.runtime.requests.answer(db, rows, status, note, decided_by="owner")

    def revoke(self, actor, host):
        """Take a granted host away; requests that asked for it are withdrawn."""
        require_owner(actor)
        normalized = valid_host(host)
        with self.store.transaction() as db:
            hosts = {row["host"] for row in granted_hosts(db)}
            if normalized not in hosts:
                raise FirmError(
                    f"{host} is not a granted host; the baseline and sandbox.toml hosts are "
                    "changed in sandbox.toml"
                )
            self._apply(db, hosts - {normalized})
            db.execute("DELETE FROM network_hosts WHERE host=?", (normalized,))
            withdrawn = self.runtime.requests.answer(
                db,
                db.execute(
                    "SELECT * FROM owner_requests WHERE kind=? "
                    "AND status IN ('pending','granted') "
                    "AND json_extract(payload_json,'$.resource')=?",
                    (self.name, normalized),
                ).fetchall(),
                "withdrawn",
                decided_by="owner",
            )
        return {"resource": normalized, "status": "revoked", "withdrawn": withdrawn,
                "granted_hosts": sorted(hosts - {normalized})}
