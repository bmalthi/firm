"""Snapshot-based, read-only analysis of one existing firm's recorded behavior."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from .accounts import incident_resolution
from .analysis_history import RESOURCE_INTERVAL, public_event
from .discovery import identity
from .errors import FirmError
from .hosts import granted_hosts
from .requests import pending_requests
from .store import OWNER
from .workspaces import sandbox_role_id

COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
SECTIONS = (
    "overview",
    "timeline",
    "usage",
    "conversations",
    "inbox",
    "readiness",
    "mac",
    "resources",
    "network",
)
TABLES = (
    "activations",
    "messages",
    "summary_runs",
    "analysis_events",
    "roles",
    "execution_incidents",
    "mac_leases",
    "mac_lease_events",
    "mac_operations",
    "resource_samples",
    "sandbox_runs",
    "sandbox_run_settlements",
)
COUNTER_FIELDS = (
    "memory_stall_usec",
    "memory_full_stall_usec",
    "cpu_stall_usec",
    "oom_kills",
    "swapouts",
)
LOW_DISK_FRACTION = 0.85  # used/total at or above this is low_disk.
# The host is the owner's own large disk, where a fraction fires with hundreds of GB left.
HOST_LOW_DISK_FREE_BYTES = 100 * 1000**3
ACTIVATION_FIELDS = (
    "activation_id,employee_id,session_id,state,created_at,launch_attempted_at,started_at,"
    "finished_at,next_intent,drain_requested_at,drain_presented_at,failure_category,failure,"
    "wake_snapshot"
)


def wake_reasons(snapshot):
    """The scheduling opportunities admission saw, decoded from its stored snapshot."""
    try:
        items = json.loads(snapshot)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [
        {
            "reason": item.get("reason"),
            "priority": item.get("priority"),
            "ready_at": item.get("ready_at"),
            # Message opportunities carry the message id, which `conversations` already shows.
            "source_id": item.get("source_id") or None,
        }
        for item in items
        if isinstance(item, dict)
    ]


def tool_group(tool):
    """Runtime tools carry their MCP server; everything else ran inside the guest."""
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" and parts[1] else "guest"


class AnalysisQueryError(ValueError):
    pass


class AnalysisNotFound(FirmError):
    pass


def measured(rows):
    result = {}
    for key in COUNTERS:
        values = [r[key] for r in rows if r.get(key) is not None]
        result[key] = {
            "value": sum(values) if values else None,
            "measured": len(values),
            "eligible": len(rows),
            "complete": len(values) == len(rows),
        }
    return result


def model_resolution(rows):
    """One last reported model per execution, including explicit unknowns."""
    known = sorted({r["resolved_model"] for r in rows if r.get("resolved_model") is not None})
    missing = sum(r.get("resolved_model") is None for r in rows)
    return {
        "resolved_models": known + ([None] if missing else []),
        "missing_resolved_model_identity": missing,
    }


def latency(values):
    valid = sorted(v for v in values if v is not None and math.isfinite(v) and v >= 0)
    return {
        "samples": len(valid),
        "invalid": len(values) - len(valid),
        "median_seconds": statistics.median(valid) if valid else None,
        "p95_seconds": valid[math.ceil(len(valid) * 0.95) - 1] if valid else None,
    }


def duration(start, finish):
    if start is None or finish is None or finish < start:
        return None
    return finish - start


def percentile(values, fraction=0.95):
    valid = sorted(value for value in values if value is not None)
    return valid[math.ceil(len(valid) * fraction) - 1] if valid else None


def present(rows, field):
    """The values a field was actually measured with."""
    return [row[field] for row in rows if row[field] is not None]


def counter_delta(before, after):
    """The increase of a cumulative counter; a decrease means it was reset, not measured."""
    if before is None or after is None or after < before:
        return None
    return after - before


def disk_state(sample, now=None):
    """One resource sample's disk state: free bytes, `low_disk` and staleness.

    The one calculation every disk consumer shares. With ``now`` it answers for the
    present: a reading older than three sampling intervals is stale, and a stale or
    unknown one (a missing used or total figure) never asserts low_disk; it reports that
    instead. Without ``now`` it answers for the moment of the sample, which is what a
    historical window wants: a firm that ended low must still read as low next week.
    """
    used, total = sample["disk_used_bytes"], sample["disk_total_bytes"]
    unknown = used is None or total is None or total <= 0
    stale = now is not None and now - sample["observed_at"] > 3 * RESOURCE_INTERVAL
    host = "machine" in sample.keys() and sample["machine"] == "host"
    low = not unknown and not stale and (
        total - used < HOST_LOW_DISK_FREE_BYTES if host else used / total >= LOW_DISK_FRACTION
    )
    return {
        "used_bytes": used,
        "total_bytes": total,
        "free_bytes": None if unknown else total - used,
        "low_disk": low,
        "observed_at": sample["observed_at"],
        "stale": stale,
        "unknown": unknown,
    }


class AnalysisReader:
    def __init__(self, path, *, clock=time.time, network_status=None):
        self.path = Path(path).resolve()
        self.clock = clock
        # The sandbox's live network view, for the network section; None for an export.
        self.network_status = network_status

    @contextmanager
    def snapshot(self):
        db = None
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            # Establish the SQLite snapshot before capturing its observation time.
            if db.execute("SELECT firm_id FROM firm").fetchone() is None:
                raise FirmError("Firm database is not initialized")
            yield db, self.clock()
        except sqlite3.Error as exc:
            raise FirmError(
                "Cannot read firm analysis; check that firm.sqlite3 exists, is "
                "readable, and contains an initialized firm: " + str(exc)
            ) from exc
        finally:
            if db is not None:
                db.close()

    def read(
        self,
        section=None,
        *,
        from_at=None,
        until=None,
        employee_id=None,
        limit=100,
        cursor=None,
        conversation_id=None,
        row_type=None,
    ):
        if section is not None and section not in SECTIONS:
            raise AnalysisQueryError("Unknown analysis section")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise AnalysisQueryError("limit must be an integer from 1 to 500")
        allowed_types = {
            "timeline": {"activation", "task"},
            "usage": {"role", "model", "session", "workload", "tool", "task"},
            "mac": {"lease", "busy", "disk_full", "operation"},
            "resources": {"run", "role", "summary_run"},
        }
        if row_type is not None and row_type not in allowed_types.get(section, set()):
            raise AnalysisQueryError("Invalid row type for this section")
        token = None
        if cursor:
            try:
                if len(cursor) > 8192:
                    raise ValueError
                token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(token, dict) or token["section"] != section:
                    raise ValueError
                if (
                    token["employee_id"] != employee_id
                    or token["conversation_id"] != conversation_id
                ):
                    raise ValueError
                if token.get("row_type") != row_type:
                    raise ValueError
                if not isinstance(token["after"], str):
                    raise ValueError
                if set(token["cutoffs"]) != set(TABLES):
                    raise ValueError
                if any(type(v) is not int or v < 0 for v in token["cutoffs"].values()):
                    raise ValueError
                for supplied, saved in ((from_at, token["from"]), (until, token["until"])):
                    if supplied is not None and float(supplied) != saved:
                        raise ValueError
                from_at, until = token["from"], token["until"]
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise AnalysisQueryError("Invalid cursor or cursor filters do not match") from exc
        with self.snapshot() as (db, now):
            firm = dict(db.execute("SELECT firm_id,state,created_at FROM firm").fetchone())
            try:
                end = now if until is None else float(until)
                start = end - 86400 if from_at is None else float(from_at)
                if not math.isfinite(start) or not math.isfinite(end) or start >= end:
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise AnalysisQueryError(
                    "from and until must be finite UTC epochs with from < until"
                ) from exc
            if (
                employee_id
                and not db.execute(
                    "SELECT 1 FROM roles WHERE employee_id=?", (employee_id,)
                ).fetchone()
            ):
                raise AnalysisNotFound("Unknown employee identity")
            if (
                conversation_id
                and not db.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
            ):
                raise AnalysisNotFound("Unknown conversation")
            cutoffs = {
                table: db.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
                for table in TABLES
            }
            if token:
                cutoffs = token["cutoffs"]
            meta = {
                "firm_id": firm["firm_id"],
                "sampled_at": now,
                "window": {"from": start, "until": end},
                "coverage": self.coverage(db, now),
                "firm_state": firm["state"],
            }
            context = (db, now, start, end, employee_id, cutoffs)
            sections = SECTIONS if section is None else (section,)
            result = dict(meta)
            for name in sections:
                if name == "overview":
                    data = self.overview(*context)
                elif name == "timeline":
                    data = self.timeline(*context)
                elif name == "usage":
                    data = self.usage(*context)
                elif name == "conversations":
                    data = self.conversations(*context, conversation_id=conversation_id)
                elif name == "inbox":
                    data = self.inbox(db, now, employee_id, cutoffs["messages"])
                elif name == "mac":
                    data = self.mac(*context)
                elif name == "resources":
                    data = self.resources(*context)
                elif name == "network":
                    data = self.network(db, now)
                    result["coverage"]["limits"].extend(data.pop("coverage_limits"))
                else:
                    data = {"rows": self.readiness(db, now, employee_id, cutoffs["roles"])}
                rows = sorted(data.pop("rows", []), key=lambda r: r["id"])
                if row_type is not None:
                    rows = [r for r in rows if r["type"] == row_type]
                if name == "timeline" and row_type == "activation":
                    rows.sort(key=lambda r: (-r["created_at"], r["id"]))
                total_rows = len(rows)
                if token:
                    if name == "timeline" and row_type == "activation":
                        after = next(
                            (i for i, row in enumerate(rows) if row["id"] == token["after"]), None
                        )
                        if after is None:
                            raise AnalysisQueryError("Cursor work period no longer exists")
                        rows = rows[after + 1 :]
                    else:
                        rows = [r for r in rows if r["id"] > token["after"]]
                shown = rows[:limit]
                has_more = len(rows) > limit
                next_cursor = None
                if has_more:
                    value = {
                        "section": name,
                        "employee_id": employee_id,
                        "conversation_id": conversation_id,
                        "row_type": row_type,
                        "from": start,
                        "until": end,
                        "cutoffs": cutoffs,
                        "after": shown[-1]["id"],
                    }
                    next_cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
                data.update(
                    rows=shown,
                    total_rows=total_rows,
                    has_more=has_more,
                    next_cursor=next_cursor,
                    role_labels=self.role_labels(db, shown),
                )
                if section is None:
                    result[name] = data
                else:
                    result.update(data)
            return result

    @staticmethod
    def role_labels(db, rows):
        """Current display labels for employees on this page, not historical identity."""
        ids = {
            value
            for row in rows
            for value in (
                row.get("employee_id") or (row.get("label") if row.get("type") == "role" else None),
                row.get("author_id"),
            )
        } - {None}
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        return {
            row["employee_id"]: {"address": row["address"], "identity": identity(dict(row))}
            for row in db.execute(
                "SELECT employee_id,address,public_profile FROM roles "
                f"WHERE employee_id IN ({placeholders})",
                tuple(ids),
            )
        }

    def coverage(self, db, now):
        from .analysis_history import recording_failed

        boundary = db.execute("SELECT MIN(observed_at) FROM analysis_events").fetchone()[0]
        row = db.execute(
            "SELECT kind,observed_at FROM analysis_events WHERE kind IN "
            "('analysis.started','analysis.stopped','readiness.observed') "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        last_host = dict(row) if row else None
        gaps = db.execute(
            "SELECT COUNT(*) FROM analysis_events WHERE kind='analysis.gap'"
        ).fetchone()[0]
        limits = [
            "Recording is best effort; host gaps are not measured waiting time.",
            "Tool tallies are read from the provider's own log when a work period is "
            "collected, so an empty tally is a period that was not measured — a lost log "
            "or a provider without an extractor — as much as one that called nothing.",
            "Unknown counters and identity are not inferred from current profiles.",
            "Resolved models are the last non-empty provider-reported identity per execution, "
            "not a per-request accounting or a guarantee of the next execution's model. "
            "Missing reports are unknown, never inferred from configured aliases.",
            "Linux reclaim counts describe named categories only; timed-out operations may "
            "have removed unmeasured data. Missing reclaim evidence is unknown, not zero.",
            "Current dispositions, usage and readiness are sampled now, not at window end.",
        ]
        stale_disk = sorted(
            machine
            for machine, state in self.disk_states(db, now).items()
            if state["stale"] or state["unknown"]
        )
        if stale_disk:
            limits.append(
                "Disk readings for " + ", ".join(stale_disk) + " are stale or unknown; "
                "low_disk is not asserted for them."
            )
        return {
            "journal_recording_from": boundary,
            "recorded_gaps": gaps,
            "recording_failure_pending": recording_failed(self.path),
            "last_host_observation": last_host,
            "host_liveness": "unverified",
            "historical_identity": "captured bindings only",
            "limits": limits,
        }

    def activations(self, db, start, end, employee_id, cutoffs, *, overlap=False):
        where = "created_at>=? AND created_at<?"
        args = [start, end]
        if overlap:
            where = "created_at<? AND (finished_at IS NULL OR finished_at>? OR created_at>=?)"
            args = [end, start, start]
        if employee_id:
            where += " AND employee_id=?"
            args.append(employee_id)
        args.append(cutoffs["activations"])
        return [
            dict(r)
            for r in db.execute(
                f"SELECT {ACTIVATION_FIELDS} FROM activations WHERE {where} AND rowid<=?", args
            )
        ]

    @staticmethod
    def tool_calls(db, activation_ids):
        """What each of these work periods called, by tool name and count.

        Selected by activation rather than by its own cutoff, so a page's tally can
        never include a work period that page cannot show.
        """
        tallies = defaultdict(dict)
        if activation_ids:
            for row in db.execute("SELECT activation_id,tool,calls FROM activation_tool_calls"):
                if row["activation_id"] in activation_ids:
                    tallies[row["activation_id"]][row["tool"]] = row["calls"]
        return tallies

    @staticmethod
    def operations(db, activation_ids):
        """Mutating operations each work period asked for, by name only.

        The ledger's payloads and results hold message bodies and handoff notes and are
        never read here; it also sees only operations that carry a request id.
        """
        counts = defaultdict(Counter)
        if activation_ids:
            for row in db.execute(
                "SELECT activation_id,operation FROM requests WHERE activation_id IS NOT NULL"
            ):
                if row["activation_id"] in activation_ids:
                    counts[row["activation_id"]][row["operation"]] += 1
        return {aid: dict(tally) for aid, tally in counts.items()}

    def events(self, db, start, end, employee_id, cutoffs):
        if not cutoffs["analysis_events"]:
            return []
        query = (
            "SELECT * FROM analysis_events WHERE observed_at>=? AND observed_at<? AND sequence<=?"
        )
        args = [start, end, cutoffs["analysis_events"]]
        if employee_id:
            query += " AND (employee_id=? OR employee_id IS NULL)"
            args.append(employee_id)
        result = []
        for r in db.execute(query, args):
            result.append(
                {
                    "id": f"journal:{r['sequence']:020d}",
                    "type": "event",
                    "kind": r["kind"],
                    "employee_id": r["employee_id"],
                    "activation_id": r["activation_id"],
                    "summary_run_id": r["summary_run_id"],
                    "observed_at": r["observed_at"],
                    "evidence": public_event(json.loads(r["payload_json"])),
                }
            )
        return result

    def timeline(self, db, now, start, end, employee_id, cutoffs):
        rows = self.activations(db, start, end, employee_id, cutoffs, overlap=True)
        rows = [r for r in rows if r["finished_at"] is not None or now >= start]
        endpoints = Counter()
        delays = []
        selected = {row["activation_id"] for row in rows}
        tallies = self.tool_calls(db, selected)
        operations = self.operations(db, selected)
        measured_tallies = 0
        for row in rows:
            a, b = row["started_at"], row["finished_at"]
            tally = tallies.get(row["activation_id"], {})
            measured_tallies += bool(tally)
            row.update(
                wake_reasons=wake_reasons(row.pop("wake_snapshot")),
                tool_calls=tally,
                operations=operations.get(row["activation_id"], {}),
            )
            uncertain = row["failure_category"] == "host_restart" or b is None
            elapsed = duration(a, b if b is not None else now)
            row.update(
                id="activation:" + row["activation_id"],
                type="activation",
                open=b is None,
                time_quality="observed" if uncertain else "confirmed",
                duration_seconds=elapsed,
                launch_delay_seconds=duration(row["created_at"], a),
                clipped_from=max(a, start) if a is not None else None,
                clipped_until=min(b if b is not None else now, end),
                markers=[
                    {
                        "id": f"activation:{row['activation_id']}:{field}",
                        "kind": field,
                        "observed_at": row[field],
                    }
                    for field in (
                        "created_at",
                        "launch_attempted_at",
                        "started_at",
                        "drain_requested_at",
                        "drain_presented_at",
                        "finished_at",
                    )
                    if row[field] is not None and start <= row[field] < end
                ],
            )
            if a is not None and elapsed is not None:
                left, right = max(a, start), min(b if b is not None else now, end)
                if right > left:
                    endpoints[left] += 1
                    endpoints[right] -= 1
            if not uncertain:
                delays.append(row["launch_delay_seconds"])
        concurrent = peak = 0
        for at in sorted(endpoints):
            concurrent += endpoints[at]
            peak = max(peak, concurrent)
        events = self.events(db, start, end, employee_id, cutoffs)
        # Incident rows retain opening and clearance, though extensions overwrite their deadline.
        for incident in db.execute(
            "SELECT incident_id,scope,dependency,kind,created_at,until_at,resolved_at,resolved_by "
            "FROM execution_incidents WHERE rowid<=?",
            (cutoffs["execution_incidents"],),
        ):
            if (
                employee_id
                and not db.execute(
                    "SELECT 1 FROM incident_roles WHERE incident_id=? AND employee_id=?",
                    (incident[0], employee_id),
                ).fetchone()
            ):
                continue
            resolution = incident_resolution(incident, now)
            for phase, at in (
                ("opened", incident["created_at"]),
                ("cleared", resolution["resolved_at"]),
            ):
                if at is not None and start <= at < end:
                    events.append(
                        {
                            "id": f"incident:{incident[0]}:{phase}",
                            "type": "event",
                            "kind": f"incident.{phase}",
                            "observed_at": at,
                            "employee_id": employee_id,
                            "evidence": {**dict(incident), **resolution},
                        }
                    )
        tasks = []
        for result in db.execute(
            "SELECT t.run_id,t.caller_activation_id,a.employee_id,t.started_at,"
            "t.provider_started_at,t.finished_at,t.state,t.reason,t.provider,t.account_id "
            "FROM summary_runs t JOIN activations a ON a.activation_id=t.caller_activation_id "
            "WHERE t.kind='task' AND t.rowid<=? AND a.rowid<=? AND t.started_at<? "
            "AND (t.finished_at IS NULL OR t.finished_at>? OR t.started_at>=?) "
            "AND (? IS NULL OR a.employee_id=?)",
            (cutoffs["summary_runs"], cutoffs["activations"], end, start, start,
             employee_id, employee_id),
        ):
            task = dict(result)
            task.update(
                id="task:" + task["run_id"],
                type="task",
                activation_id=task["caller_activation_id"],
                outcome=(
                    "unavailable" if task["reason"] else
                    "completed" if task["state"] == "summarized" else task["state"]
                ),
                open=task["finished_at"] is None,
                duration_seconds=duration(
                    task["started_at"],
                    task["finished_at"] if task["finished_at"] is not None else now,
                ),
            )
            tasks.append(task)
        return {
            "rows": rows + tasks + events,
            "activation_count": len(rows),
            "task_count": len(tasks),
            "peak_observed_concurrency": peak,
            "launch_delay": latency(delays),
            "work_period_contents": {
                "tallied_work_periods": measured_tallies,
                "eligible_work_periods": len(rows),
                "semantics": "wake_reasons is the snapshot admission saw, not the role's own "
                "reason for what it then did. tool_calls is what the provider's log shows the "
                "period called, by name and count, with no arguments or results; an empty "
                "tally is unmeasured as much as it is idle. operations counts the mutating "
                "runtime operations the period asked for by name, which is exact but covers "
                "only operations carrying a request id: peeks, discovery, desktop work and "
                "everything inside the guest are not in it.",
            },
            "concurrency_limit": (
                "Overlap of observed intervals; uncertain exits may overstate overlap."
            ),
        }

    def usage(self, db, now, start, end, employee_id, cutoffs):
        executions = self.activations(db, start, end, employee_id, cutoffs)
        usage = {
            r["activation_id"]: dict(r)
            for r in db.execute(
                "SELECT activation_id,resolved_model," + ",".join(COUNTERS)
                + " FROM activation_usage"
            )
        }
        bindings = {
            r["session_id"]: dict(r)
            for r in db.execute(
                "SELECT session_id,provider,account_id,settings_json FROM execution_bindings"
            )
        }
        for row in executions:
            row.update(usage.get(row["activation_id"], {}))
            binding = bindings.get(row["session_id"], {})
            row.update(
                provider=binding.get("provider"),
                account_id=binding.get("account_id"),
                model=json.loads(binding.get("settings_json", "{}")).get("model"),
                workload="role",
            )
        identity = {}
        if cutoffs["analysis_events"]:
            for r in db.execute(
                "SELECT summary_run_id,employee_id,payload_json FROM analysis_events "
                "WHERE kind='summary.bound' AND sequence<=?",
                (cutoffs["analysis_events"],),
            ):
                identity[r[0]] = {**public_event(json.loads(r[2])), "employee_id": r[1]}
        summaries, tasks = [], []
        if cutoffs["summary_runs"]:
            for r in db.execute(
                "SELECT t.run_id,t.started_at,t.provider,t.account_id,t.input_tokens,"
                "t.cached_input_tokens,t.output_token_usage AS output_tokens,"
                "t.reasoning_output_tokens,t.resolved_model,t.kind,t.caller_activation_id,"
                "a.employee_id AS caller_employee_id FROM summary_runs t "
                "LEFT JOIN activations a ON a.activation_id=t.caller_activation_id AND a.rowid<=? "
                "WHERE t.started_at>=? AND t.started_at<? AND t.rowid<=?",
                (cutoffs["activations"], start, end, cutoffs["summary_runs"]),
            ):
                row = {**dict(r), **identity.get(r["run_id"], {}), "workload": r["kind"]}
                del row["kind"]
                if r["kind"] == "task":
                    row["employee_id"] = row["caller_employee_id"]
                if employee_id is None or row.get("employee_id") == employee_id:
                    (tasks if r["kind"] == "task" else summaries).append(row)
        all_usage = executions + summaries + tasks
        groups = defaultdict(list)
        for row in all_usage:
            for dimension, value in (
                ("role", row.get("employee_id")),
                ("model", (row.get("provider"), row.get("model"))),
                ("workload", row["workload"]),
            ):
                groups[(dimension, json.dumps(value))].append(row)
        rows = [
            {
                "id": f"{dimension}:{value}",
                "type": dimension,
                "label": json.loads(value),
                "usage": measured(group),
                "executions": len(group),
                **model_resolution(group),
            }
            for (dimension, value), group in groups.items()
        ]
        rows += [
            {
                "id": "task:" + task["run_id"],
                "type": "task",
                "run_id": task["run_id"],
                "employee_id": task.get("employee_id"),
                "activation_id": task["caller_activation_id"],
                "caller_activation_id": task["caller_activation_id"],
                "provider": task["provider"],
                "account_id": task["account_id"],
                "model": task.get("model"),
                "resolved_model": task["resolved_model"],
                "usage": measured([task]),
            }
            for task in tasks
        ]
        tallies = self.tool_calls(db, {r["activation_id"] for r in executions})
        by_tool = defaultdict(lambda: {"calls": 0, "executions": 0})
        for tally in tallies.values():
            for tool, calls in tally.items():
                by_tool[tool]["calls"] += calls
                by_tool[tool]["executions"] += 1
        rows += [
            {
                "id": "tool:" + tool,
                "type": "tool",
                "label": tool,
                "group": tool_group(tool),
                "calls": totals["calls"],
                "executions": totals["executions"],
            }
            for tool, totals in by_tool.items()
        ]
        by_group = Counter()
        for tool, totals in by_tool.items():
            by_group[tool_group(tool)] += totals["calls"]
        firsts = {
            r[0]: (r[1], r[2])
            for r in db.execute(
                "SELECT session_id,MIN(created_at),MIN(started_at) FROM activations "
                "WHERE rowid<=? AND (? IS NULL OR employee_id=?) GROUP BY session_id",
                (cutoffs["activations"], employee_id, employee_id),
            )
        }
        session_ids = {r["session_id"] for r in executions}
        session_executions = defaultdict(list)
        for execution in executions:
            session_executions[execution["session_id"]].append(execution)
        for r in db.execute(
            "SELECT session_id,employee_id,execution_profile,provider,soft_context_window,"
            "advisory_fraction,handoff_reserve,current_context_tokens,high_water_context_tokens,"
            "provider_context_window,cumulative_input_tokens,cumulative_cached_input_tokens,"
            "cumulative_output_tokens,cumulative_reasoning_output_tokens,native_compactions,"
            "observed_at,source FROM context_sessions"
        ):
            if r["session_id"] not in session_ids:
                continue
            row = dict(r)
            selected = session_executions[r["session_id"]]
            row.update(
                id="session:" + r["session_id"],
                type="session",
                label=r["session_id"],
                usage=measured(selected),
                executions=len(selected),
                model=selected[0]["model"],
                **model_resolution(selected),
                first_admitted_at=firsts[r["session_id"]][0],
                first_started_at=firsts[r["session_id"]][1],
            )
            rows.append(row)
        events = self.events(db, start, end, employee_id, cutoffs)
        threshold_entries = Counter(
            e["evidence"].get("session_id")
            for e in events
            if e["kind"] == "context.pressure_changed"
            and e["evidence"].get("cause") == "usage"
            and not e["evidence"].get("baseline")
            and e["evidence"].get("value") in ("advisory", "handoff_required")
        )
        for row in rows:
            if row["type"] == "session":
                row["observed_threshold_entries"] = threshold_entries[row["session_id"]]
        appointments = {
            row["employee_id"]: dict(row)
            for row in db.execute(
                "SELECT employee_id,kind,remaining_periods,used_periods FROM appointments"
            )
        }
        periods_in_window = Counter(
            e["employee_id"] for e in events if e["kind"] == "appointment.started"
        )
        for row in rows:
            if row["type"] == "role":
                terms = appointments.get(row["label"])
                if terms and (terms["kind"] == "temporary" or terms["used_periods"]):
                    row["appointment"] = {
                        **terms,
                        "periods_in_window": periods_in_window[row["label"]],
                    }
        return {
            "rows": rows,
            "totals": measured(all_usage),
            "role_totals": measured(executions),
            "summary_totals": measured(summaries),
            "task_totals": measured(tasks),
            "missing_model_identity": sum(r.get("model") is None for r in all_usage),
            **model_resolution(all_usage),
            "sessions_used": len(session_ids),
            "sessions_first_admitted": sum(start <= firsts[s][0] < end for s in session_ids),
            "sessions_first_started": sum(
                firsts[s][1] is not None and start <= firsts[s][1] < end for s in firsts
            ),
            "temporary_periods_observed": sum(e["kind"] == "appointment.started" for e in events),
            "tool_calls": {
                "calls": sum(totals["calls"] for totals in by_tool.values()),
                "by_group": dict(by_group),
                "tallied_executions": len(tallies),
                "eligible_executions": len(executions),
                "semantics": "Tool names and counts from each period's provider log, grouped "
                "by MCP server so runtime work separates from building in the guest. Names "
                "without arguments cannot tell a product build from a doc build. Periods with "
                "no tally are unmeasured, not idle.",
            },
            "semantics": "Usage of executions admitted in this window, "
            "measured through sampled_at. "
            "Role, summary and task totals are disjoint; totals count each execution once. "
            "Role/model/workload rows regroup those same executions; task rows give caller "
            "attribution and are not additional usage. "
            "Session counters are lifetime totals; cache/reasoning are overlapping breakdowns.",
        }

    def conversations(
        self, db, now, start, end, employee_id, cutoffs, *, conversation_id=None
    ):
        query = "SELECT message_id,sequence,conversation_id,author_id,subject,created_at,reply_to"
        if conversation_id:
            query += ",body"
        query += " FROM messages WHERE rowid<=?"
        args = [cutoffs["messages"]]
        if conversation_id:
            query += " AND conversation_id=?"
            args.append(conversation_id)
        messages = {r["message_id"]: dict(r) for r in db.execute(query, args)}
        deliveries = defaultdict(list)
        for r in db.execute(
            "SELECT message_id,employee_id,disposition,disposition_at,failure_reason,"
            "claimed_activation_id FROM message_deliveries"
        ):
            if r["message_id"] in messages:
                deliveries[r["message_id"]].append(dict(r))
        started_at = {
            r["activation_id"]: r["started_at"]
            for r in db.execute(
                "SELECT activation_id,started_at FROM activations WHERE rowid<=?",
                (cutoffs["activations"],),
            )
        }
        notices = defaultdict(list)
        for r in db.execute(
            "SELECT employee_id,observed_at,payload_json FROM analysis_events "
            "WHERE kind='mail.presented' AND rowid<=?",
            (cutoffs["analysis_events"],),
        ):
            payload = json.loads(r["payload_json"])
            if payload.get("value") is not None:
                notices[r["employee_id"]].append(
                    (payload.get("previous") or 0, payload["value"], r["observed_at"])
                )

        def announced_at(employee_id, sequence):
            """When a waiting-mail notice first named this message to this role."""
            covering = [
                at for low, high, at in notices[employee_id] if low < sequence <= high
            ]
            return min(covering) if covering else None

        selected = []
        backlog = []
        edges = Counter()
        claims = []
        # Claimed mail split by whether its claiming work period had already begun when it
        # arrived: mail claimed while the role was already running was noticed mid-period.
        while_running = []
        before_work_period = []
        unattributed = []
        after_notice = []
        never_announced = 0
        for mid, message in messages.items():
            recipients = deliveries[mid]
            if (
                employee_id
                and message["author_id"] != employee_id
                and not any(d["employee_id"] == employee_id for d in recipients)
            ):
                continue
            message["deliveries"] = recipients
            if start <= message["created_at"] < end:
                selected.append(message)
                for d in recipients:
                    if d["disposition"] == "claimed":
                        delay = duration(message["created_at"], d["disposition_at"])
                        claims.append(delay)
                        began = started_at.get(d["claimed_activation_id"])
                        if began is None:
                            unattributed.append(delay)
                        elif began <= message["created_at"]:
                            while_running.append(delay)
                        else:
                            before_work_period.append(delay)
                        announced = announced_at(d["employee_id"], message["sequence"])
                        if announced is None:
                            never_announced += 1
                        else:
                            after_notice.append(duration(announced, d["disposition_at"]))
                    if employee_id and employee_id not in (message["author_id"], d["employee_id"]):
                        continue
                    edges[(message["author_id"], d["employee_id"])] += 1
            elif message["created_at"] < start:
                for d in recipients:
                    if d["disposition"] == "pending" and (
                        not employee_id or d["employee_id"] == employee_id
                    ):
                        backlog.append(
                            {
                                "id": f"backlog:{mid}:{d['employee_id']}",
                                "type": "backlog",
                                "message_id": mid,
                                "conversation_id": message["conversation_id"],
                                "author_id": message["author_id"],
                                "employee_id": d["employee_id"],
                                "pending_age_seconds": duration(message["created_at"], now),
                            }
                        )
        groups = defaultdict(list)
        for message in selected:
            groups[message["conversation_id"]].append(message)
        rows = []
        for cid, group in groups.items():
            ds = [d for m in group for d in m["deliveries"]]
            delays = [
                duration(m["created_at"], d["disposition_at"])
                for m in group
                for d in m["deliveries"]
                if d["disposition"] == "claimed"
            ]
            ages = [
                duration(m["created_at"], now)
                for m in group
                for d in m["deliveries"]
                if d["disposition"] == "pending"
            ]
            rows.append(
                {
                    "id": "conversation:" + cid,
                    "type": "conversation",
                    "conversation_id": cid,
                    "subject": min(group, key=lambda m: m["sequence"])["subject"],
                    "messages": len(group),
                    "replies": sum(m["reply_to"] is not None for m in group),
                    "participants": sorted(
                        {m["author_id"] for m in group} | {d["employee_id"] for d in ds}
                    ),
                    "last_activity": max(m["created_at"] for m in group),
                    "dispositions": dict(Counter(d["disposition"] for d in ds)),
                    "claim_delay": latency(delays),
                    "pending_age": latency(ages),
                }
            )
        if conversation_id:
            rows = [
                {**m, "id": f"message:{m['sequence']:020d}", "type": "message"} for m in selected
            ]
        else:
            rows += [
                {
                    "id": f"traffic:{a}:{b}",
                    "type": "traffic",
                    "author_id": a,
                    "employee_id": b,
                    "deliveries": count,
                    "owner_or_operations": a.startswith("@") or b.startswith("@"),
                }
                for (a, b), count in edges.items()
            ]
            rows += backlog
        return {
            "rows": rows,
            "message_count": len(selected),
            "delivery_count": sum(len(m["deliveries"]) for m in selected),
            "claim_delay": latency(claims),
            "claims_by_arrival": {
                "while_running": latency(while_running),
                "before_work_period": latency(before_work_period),
                "unattributed": latency(unattributed),
                "after_notice": latency(after_notice),
                "claimed_without_notice": never_announced,
                "semantics": "Claimed mail, by whether the claiming work period had already "
                "started when the message arrived. while_running means the role was mid-period "
                "and claimed it there; before_work_period means it waited for a wake. Those two "
                "measure from arrival; after_notice measures from the waiting-mail notice that "
                "first named the message, so it is the reaction to being told. Mail claimed "
                "without a notice was seen at wake or through the role's own peek. A peek that "
                "claimed nothing is still invisible here.",
            },
            "pending_deliveries_from_before_window": len(backlog),
            "dispositions": dict(
                Counter(d["disposition"] for m in selected for d in m["deliveries"])
            ),
            "undeliverable_reasons": dict(
                Counter(
                    d["failure_reason"] or "unrecorded"
                    for m in selected
                    for d in m["deliveries"]
                    if d["disposition"] == "undeliverable"
                )
            ),
        }

    def inbox(self, db, now, employee_id, cutoff):
        """The mail still waiting in the owner's inbox, with its bodies, sampled now.

        Dispositions are current, so claimed and dismissed mail is gone from here and the
        analysis window does not bound it; `conversations` is where a window's traffic is
        counted.
        """
        query = (
            "SELECT m.message_id,m.sequence,m.conversation_id,m.author_id,m.subject,m.created_at,"
            "m.reply_to,m.body FROM message_deliveries d JOIN messages m USING(message_id) "
            "WHERE d.employee_id=? AND d.disposition='pending' AND m.rowid<=?"
        )
        args = [OWNER, cutoff]
        if employee_id:
            query += " AND m.author_id=?"
            args.append(employee_id)
        rows = [
            {
                **dict(r),
                "id": f"pending:{r['sequence']:020d}",
                "type": "pending_mail",
                "pending_age_seconds": duration(r["created_at"], now),
            }
            for r in db.execute(query, args)
        ]
        requests = [
            request
            for request in pending_requests(db, now)
            if not employee_id or request["employee_id"] == employee_id
        ]
        return {
            "rows": rows,
            "pending_count": len(rows),
            "pending_age": latency([r["pending_age_seconds"] for r in rows]),
            "pending_requests": requests,
            "pending_request_count": len(requests),
            "needs_owner_count": len(rows) + len(requests),
            "semantics": "Mail pending for the owner right now, oldest first, with full bodies; "
            "use limit and the cursor to page through it. pending_requests are what roles asked "
            "the owner for and are waiting on: decide them with firm requests.",
        }

    def network(self, db, now):
        """The firm's allowlist and outbound traffic by role, read live from the sandbox.

        Grants and requests are the runtime's record; the allowlist's other sources and the
        traffic exist only in the sandbox, so without it they are reported unavailable.
        """
        status, error = None, None
        if self.network_status is not None:
            try:
                status = self.network_status()
            except Exception as exc:  # A stopped or absent sandbox is a coverage fact.
                error = str(exc) or type(exc).__name__
        else:
            error = "no sandbox for this firm directory"
        granted = granted_hosts(db)
        egress = (status or {}).get("egress") or {"live": False, "entries": [], "overflow": 0}
        roles = {
            sandbox_role_id(row["employee_id"]): row["employee_id"]
            for row in db.execute("SELECT employee_id FROM roles")
        }
        by_role = {}
        for entry in egress.get("entries", []):
            employee_id = roles.get(entry.get("role"))
            key = employee_id or entry.get("role")
            row = by_role.setdefault(
                key,
                {
                    "id": f"egress:{key}",
                    "type": "role_egress",
                    "employee_id": employee_id,
                    "sandbox_role": entry.get("role"),
                    "requests": 0,
                    "denied": 0,
                    "bytes_up": 0,
                    "bytes_down": 0,
                    "hosts": [],
                },
            )
            for field in ("requests", "denied", "bytes_up", "bytes_down"):
                row[field] += entry.get(field) or 0
            row["hosts"].append(
                {
                    field: entry.get(field)
                    for field in (
                        "host", "requests", "denied", "bytes_up", "bytes_down", "last_seen"
                    )
                }
            )
        limits = []
        if status is None:
            limits.append(
                f"The sandbox could not be read ({error}); the allowlist shows the owner's "
                "grants only and traffic is unavailable."
            )
        elif not egress.get("live"):
            limits.append("The proxy is not running; traffic is the last recorded tally.")
        if egress.get("overflow"):
            limits.append(
                f"{egress['overflow']} role and host pairs exceeded the proxy's tally and are "
                "not shown."
            )
        return {
            "rows": list(by_role.values()),
            "live": status is not None,
            "enabled": status.get("enabled") if status else None,
            "mode": status.get("mode") if status else None,
            "allowed_hosts": status.get("allowed_hosts")
            if status
            else [{"host": row["host"], "source": "granted"} for row in granted],
            "granted_hosts": granted,
            "pending_requests": pending_requests(db, now),
            "egress": {"live": bool(egress.get("live")), "overflow": egress.get("overflow", 0)},
            "coverage_limits": limits,
            "semantics": "Hosts the firm may reach and where each role's outbound requests went, "
            "with denials; a denial is a request the proxy refused.",
        }

    @staticmethod
    def readiness(db, now, employee_id=None, cutoff=None):
        firm = db.execute("SELECT state,config_json FROM firm").fetchone()
        config = json.loads(firm["config_json"])
        host = db.execute("SELECT infrastructure_state FROM execution_host").fetchone()
        active = {
            r["employee_id"]: dict(r)
            for r in db.execute(
                "SELECT employee_id,activation_id,state,drain_requested_at FROM activations "
                "WHERE state IN ('admitted','running')"
            )
        }
        terms = {r["employee_id"]: dict(r) for r in db.execute("SELECT * FROM appointments")}
        wakes = defaultdict(list)
        for r in db.execute("SELECT employee_id,ready_at FROM scheduling_opportunities"):
            wakes[r["employee_id"]].append(dict(r))
        reminders = defaultdict(list)
        for r in db.execute(
            "SELECT employee_id,reminder_id,due_at FROM reminders WHERE state='pending'"
        ):
            reminders[r["employee_id"]].append(dict(r))
        pending = {
            r["employee_id"]: {
                "deliveries": r["deliveries"],
                "oldest_age_seconds": duration(r["oldest"], now),
            }
            for r in db.execute(
                "SELECT d.employee_id,COUNT(*) AS deliveries,MIN(m.created_at) AS oldest "
                "FROM message_deliveries d JOIN messages m USING(message_id) "
                "WHERE d.disposition='pending' GROUP BY d.employee_id"
            )
        }
        incidents = [
            dict(r)
            for r in db.execute(
                "SELECT incident_id,scope,dependency,kind,until_at "
                "FROM execution_incidents WHERE resolved_at IS NULL"
            )
            if r["until_at"] is None or r["until_at"] > now
        ]
        dependencies = dict(db.execute("SELECT employee_id,incident_id FROM role_dependencies"))
        asked = defaultdict(list)
        for request in pending_requests(db, now):
            asked[request["employee_id"]].append(
                {
                    key: request.get(key)
                    for key in ("request_id", "kind", "resource", "cause", "age_seconds")
                }
            )
        pending_levels = {
            r["employee_id"]: dict(r)
            for r in db.execute(
                "SELECT employee_id,attempts,retry_at FROM hire_levels WHERE state='pending'"
            )
        }
        bindings = {
            r["session_id"]: dict(r)
            for r in db.execute(
                "SELECT session_id,account_id,profile,provider FROM execution_bindings"
            )
        }
        occupancy = Counter(
            dict(
                db.execute(
                    "SELECT b.account_id,COUNT(*) FROM activations a "
                    "JOIN execution_bindings b USING(session_id) "
                    "WHERE a.state IN ('admitted','running') GROUP BY b.account_id"
                )
            )
        )
        query = (
            "SELECT rowid,employee_id,address,public_profile,employment,session_id,"
            "execution_profile,"
            "hold_reason,retry_at,attempt_count,recovery_episode FROM roles"
        )
        rows = []
        for role in db.execute(query):
            eid = role["employee_id"]
            if employee_id and eid != employee_id or cutoff is not None and role["rowid"] > cutoff:
                continue
            reasons, evidence, stable, deadlines = [], [], [], []

            def add(code, fact=None, reasons=reasons, evidence=evidence):
                if code not in reasons:
                    reasons.append(code)
                if fact is not None:
                    evidence.append(fact)

            execution = active.get(eid)
            if role["employment"] == "retired":
                add("retired")
            if execution:
                add(execution["state"], execution)
                stable.append(execution["activation_id"])
                if (
                    execution["drain_requested_at"] is not None
                    or role["employment"] != "active"
                    or firm["state"] == "stopping"
                ):
                    add("exit_pending")
            if role["employment"] == "paused":
                add("owner_paused")
            if firm["state"] != "running":
                add("firm_" + firm["state"])
            if host and host[0] != "running":
                add(
                    "infrastructure_not_ready",
                    {"recorded_infrastructure_state": host[0], "liveness": "unverified"},
                )
            if role["hold_reason"]:
                add(
                    role["hold_reason"],
                    {
                        "hold_reason": role["hold_reason"],
                        "recovery_episode": role["recovery_episode"],
                        "attempt_count": role["attempt_count"],
                    },
                )
                stable.append(role["recovery_episode"])
            pinned = next((i for i in incidents if i["incident_id"] == dependencies.get(eid)), None)
            if pinned:
                add("pinned_dependency", pinned)
                stable.append(pinned["incident_id"])
            if eid in pending_levels:
                add("level_pending", pending_levels[eid])
            appointment = terms.get(eid, {"kind": "durable"})
            if appointment["kind"] == "temporary" and appointment["remaining_periods"] == 0:
                add("temporary_exhausted", appointment)
            if role["retry_at"] is not None and role["retry_at"] > now:
                add("retry_wait", {"retry_at": role["retry_at"]})
                deadlines.append(role["retry_at"])
            due = any(w["ready_at"] <= now for w in wakes[eid])
            future = [w["ready_at"] for w in wakes[eid] if w["ready_at"] > now]
            future += [r["due_at"] for r in reminders[eid] if r["due_at"] > now]
            waiting_on_owner = bool(asked[eid]) and not execution and not due
            if waiting_on_owner:
                # The owner's answer is the wake; a reminder may still come first.
                add("owner_request_pending", {"requests": asked[eid]})
                stable.extend(request["request_id"] for request in asked[eid])
            if not due:
                if any(r["due_at"] <= now for r in reminders[eid]):
                    add(
                        "reminder_due",
                        {
                            "due_reminders": [
                                r["reminder_id"] for r in reminders[eid] if r["due_at"] <= now
                            ]
                        },
                    )
                elif future:
                    add("scheduled_wake", {"next_due_at": min(future)})
                    deadlines.append(min(future))
                elif pending.get(eid):
                    add("pending_mail_no_wake", pending[eid])
                elif not waiting_on_owner:
                    add("idle_no_wake")
            if len(active) >= config["max_active"] and not execution:
                add("firm_capacity", {"occupied": len(active), "limit": config["max_active"]})
            options = []
            if "accounts" in config:
                retained = bindings.get(role["session_id"])
                primary = role["execution_profile"]
                profiles = config["profiles"]
                names = (
                    [retained["profile"]]
                    if retained
                    else [primary, *profiles.get(primary, {}).get("alternates", [])]
                )
                if not retained and primary not in profiles:
                    add("profile_config_hold", {"profile": primary})
                for name in names:
                    accounts = (
                        [retained["account_id"]]
                        if retained
                        else profiles.get(name, {}).get("accounts", [])
                    )
                    for account_id in accounts:
                        account = config["accounts"].get(account_id)
                        blockers = []
                        option_incidents = [
                            i
                            for i in incidents
                            if (i["scope"], i["dependency"])
                            in (("account", account_id), ("profile", name))
                        ]
                        if (
                            account is None
                            or retained
                            and account["provider"] != retained["provider"]
                        ):
                            blockers.append("account_config_hold")
                        if option_incidents:
                            blockers.extend(
                                "account_quota_hold"
                                if i["kind"] == "quota"
                                else i["scope"] + "_config_hold"
                                if i["kind"] == "config"
                                else i["scope"] + "_hold"
                                for i in option_incidents
                            )
                        # A retained conversation runs whatever its account's limit.
                        if (
                            not retained
                            and account
                            and occupancy[account_id] >= account["max_active"]
                        ):
                            blockers.append("account_capacity")
                        options.append(
                            {
                                "profile": name,
                                "account_id": account_id,
                                "blockers": sorted(set(blockers)),
                                "occupied": occupancy[account_id],
                                "limit": account.get("max_active") if account else None,
                                "incidents": option_incidents,
                                "next_check_at": min(
                                    [
                                        i["until_at"]
                                        for i in option_incidents
                                        if i["until_at"] is not None
                                    ],
                                    default=None,
                                ),
                            }
                        )
                if options and all(o["blockers"] for o in options):
                    for code in sorted({b for o in options for b in o["blockers"]}):
                        add(code)
                    deadlines.extend(
                        o["next_check_at"] for o in options if o["next_check_at"] is not None
                    )
                if not options:
                    add("unknown")
                stable += [
                    {
                        "account_id": o["account_id"],
                        "profile": o["profile"],
                        "blockers": o["blockers"],
                        "incidents": [i["incident_id"] for i in o["incidents"]],
                    }
                    for o in options
                    if o["blockers"]
                ]
            if not reasons:
                add("ready" if due else "unknown")
            activity = (
                "retired"
                if role["employment"] == "retired"
                else execution["state"]
                if execution
                else "unknown"
                if reasons == ["unknown"]
                else "ready"
                if reasons == ["ready"]
                else "idle"
                if all(
                    r in ("idle_no_wake", "pending_mail_no_wake", "scheduled_wake") for r in reasons
                )
                else "blocked"
            )
            rows.append(
                {
                    "id": "role:" + eid,
                    "employee_id": eid,
                    "address": role["address"],
                    "identity": identity(dict(role)),
                    "activity": activity,
                    "primary_reason": reasons[0],
                    "reasons": reasons,
                    "evidence": evidence,
                    "pending_mail": pending.get(eid),
                    "account_options": options,
                    "appointment": appointment,
                    "stable_evidence": stable,
                    "next_check_at": min(deadlines) if deadlines else None,
                    "sampled_at": now,
                    "actionable_by": "owner_or_manager"
                    if any(
                        r in reasons
                        for r in (
                            "owner_paused",
                            "pinned_dependency",
                            "temporary_exhausted",
                            "refusal",
                            "attempts_exhausted",
                            "profile_config_hold",
                            "account_config_hold",
                            "account_hold",
                            "profile_hold",
                            "firm_stopped",
                            "owner_request_pending",
                        )
                    )
                    else "runtime_or_time",
                    "evidence_limits": [
                        "Stored evidence only; no account selected or live host verified."
                    ],
                }
            )
        return rows

    @classmethod
    def console_readiness(cls, db, now):
        """Compact current readiness on the caller's connection, without reader pagination."""
        rows = []
        for row in cls.readiness(db, now):
            blockers = []
            pinned = {fact["incident_id"] for fact in row["evidence"] if "incident_id" in fact}
            incidents = set(pinned)
            for option in row["account_options"]:
                incidents.update(i["incident_id"] for i in option["incidents"])
            alternative = any(not option["blockers"] for option in row["account_options"])
            for incident_id in sorted(incidents):
                blockers.append({
                    "incident_id": incident_id,
                    "alternative_account_usable": alternative and incident_id not in pinned,
                })
            for fact in row["evidence"]:
                if "requests" in fact:
                    blockers.append({"request_ids": [q["request_id"] for q in fact["requests"]]})
                for key in ("retry_at", "hold_reason"):
                    if fact.get(key) is not None:
                        blockers.append({key: fact[key]})
            rows.append({
                **{key: row[key] for key in (
                    "employee_id", "address", "identity", "activity", "primary_reason",
                    "reasons", "next_check_at", "actionable_by",
                )},
                "blockers": blockers,
            })
        return {"sampled_at": now, "rows": rows}

    def _lease_disk(self, db, leases, cutoff):
        """Each lease's disk reading at acquire and release, from its `mac.disk` events.

        Read by role, which the journal indexes, not by time: a lease that began before
        the window still has its acquire reading. A lease without events is unknown.
        """
        unknown = {
            "free_bytes": None, "total_bytes": None, "reclaimed_bytes": None,
            "reclaim": None, "simulators": None,
        }
        readings = {
            row["lease_id"]: {"acquired": unknown, "released": unknown} for row in leases
        }
        for employee_id in sorted({row["employee_id"] for row in leases}):
            for event in db.execute(
                "SELECT payload_json FROM analysis_events WHERE employee_id=? "
                "AND kind='mac.disk' AND sequence<=? ORDER BY sequence",
                (employee_id, cutoff),
            ):
                payload = json.loads(event["payload_json"])
                phase = {"acquire": "acquired", "release": "released"}.get(payload.get("phase"))
                if phase and payload.get("lease_id") in readings:
                    # A lease released twice keeps the later reading.
                    readings[payload["lease_id"]][phase] = {
                        key: payload.get(key) for key in unknown
                    }
        return readings

    def _disk_refusals(self, db, start, end, employee_id, cutoff):
        """Admission refusals below the disk floor: the same evidence, with no lease.

        From `mac.disk` events recorded without a lease_id (phase `refused`), windowed
        by observation time like a busy event, since a refusal has no acquire to anchor it.
        """
        query = (
            "SELECT sequence,observed_at,employee_id,activation_id,payload_json "
            "FROM analysis_events WHERE sequence<=? AND kind='mac.disk' "
            "AND observed_at>=? AND observed_at<?"
        )
        parameters = [cutoff, start, end]
        if employee_id:
            query += " AND employee_id=?"
            parameters.append(employee_id)
        rows = []
        for row in db.execute(query, parameters):
            payload = json.loads(row["payload_json"])
            if payload.get("phase") != "refused":
                continue
            rows.append(
                {
                    "id": f"disk_full:{row['sequence']:012d}",
                    "type": "disk_full",
                    "employee_id": row["employee_id"],
                    "activation_id": row["activation_id"],
                    "at": row["observed_at"],
                    # A refused station granted no lease, so there is none to name.
                    "lease_id": None,
                    "station": payload.get("station"),
                    "free_bytes": payload.get("free_bytes"),
                    "total_bytes": payload.get("total_bytes"),
                    "reclaimed_bytes": payload.get("reclaimed_bytes"),
                    # What the reclaim before this refusal removed, and what it left.
                    "reclaim": payload.get("reclaim"),
                }
            )
        return rows

    def mac(self, db, now, start, end, employee_id, cutoffs):
        """Leases, refusals (busy or too full) and brokered desktop calls over the window."""
        rows, by_role = [], defaultdict(
            lambda: {"leases": 0, "busy": 0, "disk_full": 0, "held_seconds": 0.0}
        )
        query = "SELECT rowid,* FROM mac_leases WHERE rowid<=? AND acquired_at<? " \
                "AND (released_at IS NULL OR released_at>=?)"
        parameters = [cutoffs["mac_leases"], end, start]
        if employee_id:
            query += " AND employee_id=?"
            parameters.append(employee_id)
        for row in db.execute(query, parameters):
            finish = row["released_at"] if row["released_at"] is not None else now
            held = duration(max(row["acquired_at"], start), min(finish, end))
            by_role[row["employee_id"]]["leases"] += 1
            by_role[row["employee_id"]]["held_seconds"] += held or 0
            rows.append(
                {
                    "id": "lease:" + row["lease_id"],
                    "type": "lease",
                    "lease_id": row["lease_id"],
                    "station": row["station"],
                    "employee_id": row["employee_id"],
                    "activation_id": row["activation_id"],
                    "acquired_at": row["acquired_at"],
                    "released_at": row["released_at"],
                    "open": row["released_at"] is None,
                    "duration_seconds": duration(row["acquired_at"], finish),
                    "outcome": row["outcome"],
                    "checks": json.loads(row["checks_json"] or "{}"),
                }
            )
        disk = self._lease_disk(db, rows, cutoffs["analysis_events"])
        for row in rows:
            row["disk"] = disk[row["lease_id"]]
        query = "SELECT rowid,* FROM mac_lease_events WHERE rowid<=? AND at>=? AND at<? " \
                "AND event='busy'"
        parameters = [cutoffs["mac_lease_events"], start, end]
        if employee_id:
            query += " AND employee_id=?"
            parameters.append(employee_id)
        for row in db.execute(query, parameters):
            by_role[row["employee_id"]]["busy"] += 1
            rows.append(
                {
                    "id": f"busy:{row['rowid']:012d}",
                    "type": "busy",
                    "employee_id": row["employee_id"],
                    "activation_id": row["activation_id"],
                    "at": row["at"],
                    # A busy refusal is about every station at once, so both are null.
                    "lease_id": row["lease_id"],
                    "station": row["station"],
                }
            )
        for row in self._disk_refusals(db, start, end, employee_id, cutoffs["analysis_events"]):
            by_role[row["employee_id"]]["disk_full"] += 1
            rows.append(row)
        query = "SELECT rowid,* FROM mac_operations WHERE rowid<=? AND at>=? AND at<?"
        parameters = [cutoffs["mac_operations"], start, end]
        if employee_id:
            query += " AND employee_id=?"
            parameters.append(employee_id)
        durations, by_operation = defaultdict(list), {}
        for row in db.execute(query, parameters):
            durations[row["op"]].append((row["duration_ms"], row["outcome"]))
            rows.append(
                {
                    "id": f"operation:{row['rowid']:012d}",
                    "type": "operation",
                    "employee_id": row["employee_id"],
                    "activation_id": row["activation_id"],
                    "at": row["at"],
                    "lease_id": row["lease_id"],
                    "station": row["station"],
                    "op": row["op"],
                    "duration_ms": row["duration_ms"],
                    "outcome": row["outcome"],
                }
            )
        for op, measured_calls in durations.items():
            values = [value for value, _ in measured_calls]
            by_operation[op] = {
                "count": len(values),
                "median_duration_ms": statistics.median(values),
                "max_duration_ms": max(values),
                "failures": sum(1 for _, outcome in measured_calls if outcome != "ok"),
            }
        return {
            "rows": rows,
            "by_role": dict(by_role),
            "by_operation": by_operation,
            "semantics": "A lease row's disk is the Mac VM's machine-wide free space at "
            "that moment (acquire, release), not what the lease itself used; a lease "
            "recorded before this figure existed reads as unknown. `reclaimed_bytes` is "
            "the station's own figure, from that reclaim alone; `reclaim` says per category "
            "what it removed and what it skipped or ran out of time for, and `simulators` "
            "which simulator step completed and, for one that failed without timing out, "
            "its `returncode` and the end of what it `said`, since reclaiming less than "
            "hoped still releases clean. A `disk_full` row is the same evidence for a station that "
            "refused admission, one row for each full station, so it names no lease.",
        }

    def disk_states(self, db, now):
        """Current machines' disk state, from each one's latest resource sample.

        Unbounded by any window: what a machine looks like right now, for the overview and
        the console, not a historical figure. The sandbox samples only host, linux and
        mac (one VM carrying all stations); other names belong only to history.
        A machine never sampled is absent.
        """
        rows = db.execute(
            "SELECT r.machine,r.disk_used_bytes,r.disk_total_bytes,r.observed_at "
            "FROM resource_samples r JOIN (SELECT machine,MAX(sequence) AS sequence "
            "FROM resource_samples WHERE machine IN ('host','linux','mac') "
            "GROUP BY machine) latest "
            "ON latest.machine=r.machine AND latest.sequence=r.sequence"
        )
        return {row["machine"]: disk_state(dict(row), now) for row in rows}

    def machines(self, db, start, end, cutoffs):
        """Each machine's counters over the window, from consecutive same-boot samples.

        Cumulative counters are summed as deltas, so a one-minute interval loses nothing
        but short dips in available memory. A reboot resets them, so a pair that crosses
        one contributes nothing. Disk is also reported as of the window's last sample,
        named accordingly so it is never mistaken for `disk_states`' current reading.
        """
        samples = defaultdict(list)
        for row in db.execute(
            "SELECT * FROM resource_samples WHERE observed_at>=? AND observed_at<? "
            "AND sequence<=? ORDER BY machine,observed_at,sequence",
            (start, end, cutoffs["resource_samples"]),
        ):
            samples[row["machine"]].append(dict(row))
        result = {}
        for machine, rows in samples.items():
            # A cumulative counter contributes only across a valid same-boot pair.
            # Missing and reset-only counters remain unknown rather than becoming zero.
            totals = dict.fromkeys(COUNTER_FIELDS)
            utilization, gap = [], None
            for before, after in zip(rows, rows[1:], strict=False):
                elapsed = after["observed_at"] - before["observed_at"]
                gap = elapsed if gap is None else max(gap, elapsed)
                if before["boot"] is None or before["boot"] != after["boot"] or elapsed <= 0:
                    continue
                for field in COUNTER_FIELDS:
                    delta = counter_delta(before[field], after[field])
                    if delta is not None:
                        totals[field] = (totals[field] or 0) + delta
                # The Mac guest has no cheap CPU counter; the host measures its process.
                busy = counter_delta(before["cpu_usec"], after["cpu_usec"])
                if busy is None:
                    busy = counter_delta(before["host_cpu_usec"], after["host_cpu_usec"])
                if busy is not None and after["cpus"]:
                    utilization.append(busy / (elapsed * 1e6 * after["cpus"]))
            used = [
                r["memory_total_bytes"] - r["memory_available_bytes"]
                for r in rows
                if r["memory_total_bytes"] is not None and r["memory_available_bytes"] is not None
            ]
            last_disk = disk_state(rows[-1])
            result[machine] = {
                "samples": len(rows),
                "first_observed_at": rows[0]["observed_at"],
                "last_observed_at": rows[-1]["observed_at"],
                "largest_gap_seconds": gap,
                "boots": len({r["boot"] for r in rows if r["boot"] is not None}),
                "cpus": rows[-1]["cpus"],
                "memory_total_bytes": rows[-1]["memory_total_bytes"],
                "memory_available_low_bytes": min(
                    present(rows, "memory_available_bytes"), default=None
                ),
                "memory_used_p95_bytes": percentile(used),
                "memory_stall_seconds": (
                    totals["memory_stall_usec"] / 1e6
                    if totals["memory_stall_usec"] is not None
                    else None
                ),
                "memory_full_stall_seconds": (
                    totals["memory_full_stall_usec"] / 1e6
                    if totals["memory_full_stall_usec"] is not None
                    else None
                ),
                "cpu_stall_seconds": (
                    totals["cpu_stall_usec"] / 1e6
                    if totals["cpu_stall_usec"] is not None
                    else None
                ),
                "cpu_utilization_mean": statistics.fmean(utilization) if utilization else None,
                "cpu_utilization_p95": percentile(utilization),
                "oom_kills": totals["oom_kills"],
                "swapouts": totals["swapouts"],
                "compressed_max_bytes": max(present(rows, "compressed_bytes"), default=None),
                "pressure_level_max": max(present(rows, "pressure_level"), default=None),
                "disk_used_max_bytes": max(present(rows, "disk_used_bytes"), default=None),
                "disk_total_bytes": rows[-1]["disk_total_bytes"],
                "disk_used_last_bytes": last_disk["used_bytes"],
                "disk_free_last_bytes": last_disk["free_bytes"],
                "low_disk_last": last_disk["low_disk"],
                "host_footprint_max_bytes": max(
                    present(rows, "host_footprint_bytes"), default=None
                ),
            }
        return result

    def resources(self, db, now, start, end, employee_id, cutoffs):
        """Machine-wide samples over the window and what each role's runs used."""
        activations = {
            row["activation_id"]: row
            for row in self.activations(db, start, end, employee_id, cutoffs)
        }
        runs, by_role = [], defaultdict(list)
        for row in db.execute(
            "SELECT s.activation_id,s.role_id,s.memory_peak_bytes,s.cpu_usec,s.oom_kills,"
            "r.reclaim_json FROM sandbox_runs s LEFT JOIN sandbox_run_settlements r "
            "ON r.run_id=s.run_id AND r.rowid<=? WHERE s.rowid<=?",
            (cutoffs["sandbox_run_settlements"], cutoffs["sandbox_runs"]),
        ):
            activation = activations.get(row["activation_id"])
            if activation is None:
                continue
            finished = activation["finished_at"]
            elapsed = duration(activation["started_at"], now if finished is None else finished)
            seconds = None if row["cpu_usec"] is None else row["cpu_usec"] / 1e6
            run = {
                "id": "run:" + row["activation_id"],
                "type": "run",
                "activation_id": row["activation_id"],
                "employee_id": activation["employee_id"],
                "role_id": row["role_id"],
                "started_at": activation["started_at"],
                "finished_at": finished,
                "duration_seconds": elapsed,
                "memory_peak_bytes": row["memory_peak_bytes"],
                "cpu_seconds": seconds,
                "mean_vcpus": seconds / elapsed if seconds is not None and elapsed else None,
                "oom_kills": row["oom_kills"],
                "reclaim": json.loads(row["reclaim_json"]) if row["reclaim_json"] else None,
            }
            runs.append(run)
            by_role[activation["employee_id"]].append(run)
        rows = list(runs)
        for employee, group in by_role.items():
            peaks = [r["memory_peak_bytes"] for r in group if r["memory_peak_bytes"] is not None]
            seconds = [r["cpu_seconds"] for r in group if r["cpu_seconds"] is not None]
            kills = [r["oom_kills"] for r in group if r["oom_kills"] is not None]
            rows.append(
                {
                    "id": "role:" + employee,
                    "type": "role",
                    "label": employee,
                    "employee_id": employee,
                    "runs": len(group),
                    "measured": len(peaks),
                    "memory_peak_max_bytes": max(peaks, default=None),
                    "memory_peak_p95_bytes": percentile(peaks),
                    "cpu_seconds": sum(seconds) if seconds else None,
                    "oom_kills": sum(kills) if kills else None,
                }
            )
        endpoints = []
        for run in runs:
            if run["memory_peak_bytes"] is None or run["started_at"] is None:
                continue
            left = max(run["started_at"], start)
            right = min(now if run["finished_at"] is None else run["finished_at"], end)
            if right > left:
                endpoints += [(left, run["memory_peak_bytes"]), (right, -run["memory_peak_bytes"])]
        concurrent = bound = 0
        for _, value in sorted(endpoints):
            concurrent += value
            bound = max(bound, concurrent)
        config = json.loads(db.execute("SELECT config_json FROM firm").fetchone()[0])
        if employee_id is None:
            for row in db.execute(
                "SELECT q.run_id,q.started_at,q.finished_at,r.role_id,r.usage_json,r.reclaim_json "
                "FROM summary_runs q JOIN sandbox_run_settlements r USING(run_id) "
                "WHERE q.kind!='task' AND q.started_at>=? AND q.started_at<? "
                "AND q.rowid<=? AND r.rowid<=?",
                (start, end, cutoffs["summary_runs"], cutoffs["sandbox_run_settlements"]),
            ):
                usage = json.loads(row["usage_json"]) or {}
                rows.append({
                    "id": "summary:" + row["run_id"], "type": "summary_run",
                    "run_id": row["run_id"], "role_id": row["role_id"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"],
                    "usage": usage,
                    "reclaim": json.loads(row["reclaim_json"]),
                })
        return {
            "rows": rows,
            "machines": self.machines(db, start, end, cutoffs),
            "disk": self.disk_states(db, now),
            "run_memory_peak_concurrent_bound_bytes": bound,
            "max_active": config["max_active"],
            "semantics": "Machine-wide samples are cumulative counters, sampled once a "
            "minute while a VM runs; short dips between samples are not seen. Run peaks "
            "and CPU usage include child tasks; task resources are not added separately. Peaks "
            "include page cache, so they bound demand from above, and their concurrent "
            "bound assumes every peak at once. Host footprints never shrink: a VM keeps "
            "the memory it has touched. `disk` is each machine's current state, from its "
            "own latest sample regardless of this window; `machines` also reports the "
            "window's last sample, named accordingly.",
        }

    def overview(self, db, now, start, end, employee_id, cutoffs):
        readiness = self.readiness(db, now, employee_id, cutoffs["roles"])
        activations = self.activations(db, start, end, employee_id, cutoffs)
        disks = self.disk_states(db, now)
        return {
            "rows": readiness,
            "readiness_counts": dict(Counter(r["activity"] for r in readiness)),
            # A fact about the firm, not a reason on a role: a low disk blocks nobody
            # until it is full. The host is the owner's own Mac and is left out.
            "low_disk": {
                machine: state
                for machine, state in sorted(disks.items())
                if machine != "host" and state["low_disk"]
            },
            "admitted_executions": len(activations),
            "outcomes": dict(Counter(a["state"] for a in activations)),
        }
