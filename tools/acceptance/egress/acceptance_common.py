"""What `network.py` and `requests.py` share: the report, the owner's CLI and proxy records.

Not itself a check, as `mac/acceptance_common.py` is not. Nothing here needs a VM: the
helpers parse text, and `firm` runs the owner's command in a subprocess exactly as the
owner types it.
"""

import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"


def describe(error):
    """An exception as evidence: its type and message, bounded."""
    return f"{type(error).__name__}: {error}"[:2000]


class Report:
    """PASS, FAIL or SKIPPED per declared check, saved to `result.json` on every change.

    A declared check never reached fails at `finish`, so a run that stopped early never
    reads as a pass.
    """

    def __init__(self, evidence: Path, names, **fields):
        self.path = Path(evidence) / "result.json"
        self.names = tuple(names)
        self.value = {**fields, "checks": {}, "observations": {}}

    def save(self):
        self.path.write_text(json.dumps(self.value, indent=2) + "\n")

    def _record(self, name, status, evidence):
        if name not in self.names:
            raise KeyError(f"Undeclared check: {name}")
        self.value["checks"][name] = {"status": status, **(evidence or {})}
        self.save()
        print(f"{status} {name}", flush=True)

    def check(self, name, passed, evidence=None):
        self._record(name, PASS if passed else FAIL, evidence)
        return bool(passed)

    def skip(self, name, reason):
        self._record(name, SKIPPED, {"reason": reason})

    def attempt(self, name, step):
        """Run `step`, which returns `(passed, evidence)`; an exception is this check's FAIL."""
        try:
            passed, evidence = step()
        except Exception as error:
            return self.check(name, False, {"error": describe(error)})
        return self.check(name, passed, evidence)

    def observe(self, name, value):
        self.value["observations"][name] = value
        self.save()

    def finish(self, error=None, cleanup_errors=()):
        """Fail what was never reached; the run passes only with no FAIL, error or cleanup error."""
        for name in self.names:
            if name not in self.value["checks"]:
                self.check(name, False, {"reason": "Not reached"})
        if error is not None:
            self.value["error"] = describe(error)
        if cleanup_errors:
            self.value["cleanup_errors"] = list(cleanup_errors)
        self.value["passed"] = (
            error is None
            and not cleanup_errors
            and all(row["status"] != FAIL for row in self.value["checks"].values())
        )
        self.save()
        return self.value["passed"]


def firm(*arguments, timeout=180):
    """Run `firm ARGUMENTS` as the owner would; the command, its exit and its JSON result.

    The CLI prints its result as JSON on success and one `firm: ...` line on failure.
    """
    arguments = [str(argument) for argument in arguments]
    process = subprocess.run(
        [sys.executable, "-m", "firm_runtime.cli", *arguments],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        result = json.loads(process.stdout) if process.returncode == 0 else None
    except ValueError:
        result = None
    return {
        "command": shlex.join(["firm", *arguments]),
        "returncode": process.returncode,
        "result": result,
        "error": process.stderr.strip()[-2000:] or None,
    }


def fields(text):
    """`key=value` words of a line, as a dict; other words are ignored."""
    return dict(
        word.split("=", 1) for word in text.split() if "=" in word and not word.startswith("=")
    )


def code(value):
    """An HTTP status written by curl's `-w`, or None for curl's `000` and anything else."""
    return int(value) if isinstance(value, str) and value.isdigit() and int(value) else None


def parse_probes(output):
    """The `PROBE NAME exit=N start=S end=S [http=N connect=N]` lines a role probe prints.

    `start` and `end` are bash `$EPOCHREALTIME` readings; a decimal comma is accepted.
    """
    results = {}
    for line in output.splitlines():
        words = line.split(maxsplit=2)
        if len(words) < 3 or words[0] != "PROBE":
            continue
        values = fields(words[2])
        if not values.get("exit", "").isdigit():
            continue
        result = {"exit": int(values["exit"])}
        try:
            result["seconds"] = round(
                float(values["end"].replace(",", ".")) - float(values["start"].replace(",", ".")),
                3,
            )
        except (KeyError, ValueError):
            pass
        for key in ("http", "connect"):
            if key in values:
                result[key] = code(values[key])
        results[words[1]] = result
    return results


def proxy_log(text):
    """The egress proxy's JSON lines, each with `at`, its time as epoch seconds.

    A line that is not a whole record (cut by a rotation or a crash) is skipped.
    """
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
            at = datetime.fromisoformat(record["time"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        records.append({**record, "at": at})
    return records


def proxy_records(path, *, since=None, role=None, host=None, allowed=None):
    """Records from the proxy's log and its rotated copy, oldest first, filtered.

    `path` is `HostNetwork.paths["requests"]`, the VM's `scratch/egress.jsonl`.
    """
    path = Path(path)
    records = []
    for file in (path.with_name(path.name + ".1"), path):
        try:
            records.extend(proxy_log(file.read_text()))
        except FileNotFoundError:
            continue
    return [
        record for record in sorted(records, key=lambda record: record["at"])
        if (since is None or record["at"] >= since)
        and (role is None or record.get("role") == role)
        and (host is None or record.get("host") == host)
        and (allowed is None or record.get("allowed") is allowed)
    ]


def clock_drift(host_before, guest, host_after):
    """Guest clock minus host clock, against the midpoint of the host readings around it."""
    return {
        "seconds": round(guest - (host_before + host_after) / 2, 3),
        "uncertainty_seconds": round((host_after - host_before) / 2, 3),
    }


def destination_bytes(traffic, address, port, protocol="tcp"):
    """The packet filter's allowed and blocked bytes to one destination."""
    rows = [
        row for row in traffic.get("destinations", ())
        if (row.get("address"), row.get("port"), row.get("protocol")) == (address, port, protocol)
    ]
    return {
        "address": address,
        "port": port,
        "protocol": protocol,
        "allowed_bytes": sum(row.get("allowed_bytes", 0) for row in rows),
        "blocked_bytes": sum(row.get("blocked_bytes", 0) for row in rows),
    }


def scutil_proxy(text):
    """The top-level scalar settings of macOS `scutil --proxy`, such as `HTTPSProxy`."""
    values, depth = {}, 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("{"):
            depth += 1
        elif stripped == "}":
            depth -= 1
        elif depth == 1 and " : " in stripped:
            key, _, value = stripped.partition(" : ")
            values[key] = value
    return values
