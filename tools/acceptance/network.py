"""Closed egress on every VM: only the proxy reaches out, only to allowed hosts.

Run only against a disposable, already provisioned development firm that has a runtime
database (`firm init`) and is not started: the check drives the VMs itself and changes
the allowlist and cutoff with the owner's `firm network` command. A role on the Linux VM
probes from its shell and from a container under its rootless Docker; with [mac] and a
station, a leased station is probed over the lease's SSH. Writes `result.json` with PASS,
FAIL or SKIPPED per check, and each role run's log; never credentials.
"""

import argparse
import base64
import json
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# `python -I` leaves this directory off sys.path; the shared helpers live beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from egress.acceptance_common import (  # noqa: E402
    Report,
    clock_drift,
    code,
    describe,
    destination_bytes,
    fields,
    firm,
    parse_probes,
    proxy_records,
    scutil_proxy,
)

ROLE = "network-acceptance"
MAC_ROLE = "network-acceptance-mac"
# In the shipped baseline (`*.github.com`); `example.com` is in no firm's list until granted.
ALLOWED_HOST = "api.github.com"
ALLOWED_URL = f"https://{ALLOWED_HOST}/zen"
UNLISTED = "example.com"
UNLISTED_URL = f"https://{UNLISTED}"
# Public addresses on 443, reachable only around the proxy.
DIRECT = ("1.1.1.1", "9.9.9.9")
CURL_IMAGE = "curlimages/curl:8.10.1"
VOLUME_IMAGE = "alpine:3.20"
VOLUME = "network-acceptance"
# A refused resolver answers at once; anything slower means DNS is leaving the guest.
DNS_SECONDS = 5
# One A query for example.com, sent straight to a public resolver over UDP.
DNS_QUERY = "123401000001000000000000076578616d706c6503636f6d0000010001"
DNS_PROGRAM = """import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(bytes.fromhex(sys.argv[1]), (sys.argv[2], 53))
try:
    s.recv(512)
except OSError:
    sys.exit(1)
"""
WRITE_CODES = "http=%{http_code} connect=%{http_connect}"
# Each probe prints one line that `parse_probes` reads; stderr goes to the run's log.
# The guest's bash 5 has $EPOCHREALTIME; an older bash falls back to whole seconds.
PROBE = r"""set -u
now() { echo "${EPOCHREALTIME:-$(date +%s)}"; }
probe() {
  local name=$1 start out code
  start=$(now)
  shift
  out=$("$@")
  code=$?
  case $out in http=*) ;; *) out= ;; esac
  echo "PROBE $name exit=$code start=$start end=$(now) $out"
}
"""

# A revoke must close the revoked host's open tunnels within this many seconds, and leave
# every other tunnel open at least as long.
REVOKE_SECONDS = 5
TUNNEL_DONE = "tunnel-done"
# Held TLS tunnels through the role's proxy, one per host argument. It prints JSON events
# (`held`, `closed`, `usable`, `done`) and never the proxy URL or its credential. It reads
# each tunnel until the proxy or the server closes it, or until the check publishes the
# `tunnel-done` credential; then it sends one request on each tunnel still open.
TUNNEL_PROGRAM = r"""import base64, json, os, socket, ssl, sys, time
from pathlib import Path
from urllib.parse import urlsplit

proxy = urlsplit(os.environ["HTTPS_PROXY"])
secret = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
done = Path(os.environ["FIRM_CREDENTIALS_DIR"]) / "tunnel-done"


def say(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def tunnel(host):
    raw = socket.create_connection((proxy.hostname, proxy.port), timeout=15)
    raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
                f"Proxy-Authorization: Basic {secret}\r\n\r\n".encode())
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        byte = raw.recv(1)  # One byte at a time, so no TLS byte is consumed here.
        if not byte:
            raise ConnectionError(f"The proxy closed the CONNECT to {host}")
        head += byte
    status = head.split()[1].decode()
    if status != "200":
        raise ConnectionError(f"The proxy answered {status} to the CONNECT to {host}")
    held = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    held.settimeout(0.2)
    return held


tunnels = {host: tunnel(host) for host in sys.argv[1:]}
say("held", hosts=list(tunnels))
deadline = time.monotonic() + 180
while tunnels and not done.exists() and time.monotonic() < deadline:
    for host, held in list(tunnels.items()):
        try:
            data = held.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            data = b""
        if not data:
            say("closed", host=host)
            tunnels.pop(host).close()
for host, held in tunnels.items():
    try:
        held.settimeout(15)
        held.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        line = held.recv(4096).split(b"\r\n", 1)[0].decode(errors="replace")
        say("usable", host=host, status_line=line[:100])
    except OSError as error:
        say("unusable", host=host, error=type(error).__name__)
say("done")
"""

CHECKS = (
    "docker_pull_via_proxy",
    "role_direct_address_refused",
    "role_http_without_proxy_refused",
    "role_dns_fails_fast",
    "role_allowed_host_via_proxy",
    "role_unlisted_host_refused",
    "container_allowed_host_via_proxy",
    "container_unlisted_host_refused",
    "container_direct_address_refused",
    "filter_counts_direct_attempts",
    "host_listener_unreachable",
    "live_allow",
    "live_revoke",
    "revoke_closes_open_tunnel",
    "revoke_keeps_other_tunnels",
    "mac_station_proxy",
    "cutoff_blocks_proxy",
    "cutoff_retained_after_restart",
    "named_volume_retained",
    "on_restores_proxy",
    "vms_stopped",
)


def curl(url, *options, timeout=20):
    return ("curl", "-sS", "-o", "/dev/null", "-w", WRITE_CODES, "--max-time", str(timeout),
            *options, url)


def container(command):
    """The same curl command in a container; the image's entrypoint is curl."""
    return ("docker", "run", "--rm", CURL_IMAGE, *command[1:])


def unproxied(command):
    return ("env", "-u", "HTTPS_PROXY", "-u", "https_proxy", "-u", "HTTP_PROXY", "-u",
            "http_proxy", *command)


def script(probes):
    return PROBE + "".join(f"probe {shlex.join([name, *argv])}\n" for name, argv in probes)


def reached(result):
    return result.get("exit") == 0 and result.get("http") == 200


def refused(result):
    return result.get("exit") not in (0, None)


def proxy_refused(result):
    """curl's CONNECT got the proxy's 403."""
    return refused(result) and result.get("connect") == 403


def covered(host, entries):
    return any(
        host == entry or entry.startswith("*.") and host.endswith(entry[1:]) for entry in entries
    )


def event_time(event):
    return datetime.fromisoformat(event["time"]).timestamp()


def tunnel_events(output):
    """The JSON events `TUNNEL_PROGRAM` printed, in order."""
    events = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "event" in row:
            events.append(row)
    return events


def tunnel_verdicts(events, closed_seen, open_at_revoke, revoke_started, revoke_done,
                    revoked, kept, bound=REVOKE_SECONDS):
    """Whether the revoke closed `revoked`'s held tunnel within `bound` and left `kept`'s.

    `closed_seen` maps a host to the host time its `closed` event was first seen; seconds
    count from the end of the revoke command, and a close seen while the command ran counts
    as zero. Both tunnels must have been open when the revoke began, or an upstream timeout
    could pass for the proxy's doing. `kept` must outlive `bound` past the revoke and still
    answer a request.
    """
    seen = closed_seen.get(revoked)
    seconds = None if seen is None else round(max(0.0, seen - revoke_done), 3)
    closes = (
        revoked in open_at_revoke and seen is not None and seen >= revoke_started
        and seconds <= bound
    )
    usable = next((row for row in events if row["event"] == "usable" and row["host"] == kept),
                  None)
    keeps = kept in open_at_revoke and kept not in closed_seen and usable is not None
    return (
        (closes, {"host": revoked, "open_at_revoke": revoked in open_at_revoke,
                  "seconds_after_revoke": seconds,
                  "revoke_command_seconds": round(revoke_done - revoke_started, 3),
                  "bound_seconds": bound}),
        (keeps, {"host": kept, "open_at_revoke": kept in open_at_revoke,
                 "closed": kept in closed_seen,
                 "status_line_after": usable and usable.get("status_line"),
                 "observed_seconds_after_revoke": bound}),
    )


@contextmanager
def synthetic_host_listener():
    """Serve only synthetic data on a new host loopback port, then remove it."""
    hits = []
    payload = b"firm synthetic host isolation probe"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    listener = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", listener.server_port, timeout=5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200 and response.read() == payload
        finally:
            connection.close()
        hits.clear()
        yield listener.server_port, hits
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=5)


class NetworkCheck:
    def __init__(self, args, config, report):
        from firm_sandbox import Organization, Sandbox

        self.args, self.config, self.report = args, config, report
        self.box = Sandbox(config)
        self.organization = Organization(ROLE)
        self.runs = 0

    # ---- probes ----------------------------------------------------------------------

    def role_probe(self, label, probes, timeout=300):
        """Run the probes in one role run; each probe's exit, seconds and HTTP codes."""
        from firm_sandbox import RunSpec

        run = self.box.start(RunSpec(
            ROLE, ("/bin/bash", "-c", script(probes)), organization=self.organization,
        ))
        state = self.box.wait(run, timeout=timeout)
        output = self.box.logs(run).decode(errors="replace")
        self.runs += 1
        (self.args.evidence / f"{self.runs:02d}-{label}.log").write_text(output)
        if state.get("unresolved"):
            raise RuntimeError(f"The {label} run did not settle: {state}")
        results = parse_probes(output)
        missing = [name for name, _ in probes if name not in results]
        if missing:
            raise RuntimeError(f"The {label} run printed no result for {missing}")
        return results

    def mac_control(self, url, timeout=15):
        """curl from the Mac's control account through the Mac's proxy; its exit code."""
        mac = self.box.mac
        proxy = f"http://{mac.network.gateway}:{mac.network.proxy_port}"
        return mac._shell(
            "/usr/bin/curl", "-sS", "-o", "/dev/null", "--max-time", str(timeout),
            "--proxy", proxy, url, check=False, timeout=timeout + 30,
        ).returncode

    def network(self, *arguments):
        """`firm network ROOT ...`, the owner's command; raises when it fails."""
        result = firm("network", self.args.root, *arguments)
        if result["returncode"]:
            raise RuntimeError(f"{result['command']} failed: {result['error']}")
        return result

    def denials(self, vm, since, role, host=UNLISTED):
        return [
            {key: record.get(key) for key in ("time", "role", "host", "port", "allowed", "reason")}
            for record in proxy_records(
                vm.network.paths["requests"], since=since, role=role, host=host, allowed=False,
            )
        ]

    def admitted(self, vm, since, role, host):
        return proxy_records(
            vm.network.paths["requests"], since=since, role=role, host=host, allowed=True,
        )

    def drift(self):
        """Guest minus host clock on each VM; informational, as the guests have no NTP."""
        from firm_sandbox.mac_vm import PYTHON

        rows = {}
        for name, vm, argv in (
            ("linux", self.box.linux, ("/bin/date", "+%s.%N")),
            ("mac", self.box.mac, (PYTHON, "-I", "-c", "import time; print(time.time())")),
        ):
            if vm is None:
                continue
            try:
                before = time.time()
                guest = float(vm._shell(*argv, timeout=60).stdout.decode().strip())
                rows[name] = clock_drift(before, guest, time.time())
            except Exception as error:
                rows[name] = {"error": f"{type(error).__name__}: {error}"[:500]}
        return rows

    # ---- checks ----------------------------------------------------------------------

    def run(self):
        box, report, linux = self.box, self.report, self.box.linux
        box.up()
        self.network("on")
        allowed = [row["host"] for row in box.network_status()["allowed_hosts"]]
        if covered(UNLISTED, allowed):
            raise RuntimeError(f"{UNLISTED} is already allowed here; use a disposable firm")
        report.observe("clock_drift_start", self.drift())
        box.file_server.provision(ROLE, self.organization)

        def pull():
            since = time.time()
            results = self.role_probe("pull", [
                ("pull_curl", ("docker", "pull", CURL_IMAGE)),
                ("pull_alpine", ("docker", "pull", VOLUME_IMAGE)),
                ("volume", ("docker", "run", "--rm", "-v", f"{VOLUME}:/retained", VOLUME_IMAGE,
                            "sh", "-ec", "printf retained-volume >/retained/result")),
            ])
            hosts = sorted({
                record["host"] for record in proxy_records(
                    linux.network.paths["requests"], since=since, role=ROLE, allowed=True,
                )
            })
            passed = all(row["exit"] == 0 for row in results.values()) and bool(hosts)
            return passed, {"probes": results, "registry_hosts_via_proxy": hosts}

        report.attempt("docker_pull_via_proxy", pull)

        role_since = time.time()
        role = self.role_probe("role-boundary", [
            *((f"direct_{address}", curl(f"https://{address}", "--noproxy", "*", timeout=10))
              for address in DIRECT),
            ("http_unproxied", unproxied(curl("http://example.com", timeout=10))),
            ("dns_getent", ("getent", "hosts", "example.com")),
            ("dns_direct", ("python3", "-I", "-c", DNS_PROGRAM, DNS_QUERY, DIRECT[0])),
            ("allowed", curl(ALLOWED_URL)),
            ("unlisted", curl(UNLISTED_URL)),
        ])
        direct = {key: value for key, value in role.items() if key.startswith("direct_")}
        report.check("role_direct_address_refused", all(map(refused, direct.values())), direct)
        report.check("role_http_without_proxy_refused", refused(role["http_unproxied"]),
                     role["http_unproxied"])
        getent, dns = role["dns_getent"], role["dns_direct"]
        report.check(
            "role_dns_fails_fast",
            refused(getent) and getent.get("seconds", DNS_SECONDS) < DNS_SECONDS and refused(dns),
            {"getent": getent, "udp_to_public_resolver": dns, "bound_seconds": DNS_SECONDS},
        )
        admitted = self.admitted(linux, role_since, ROLE, ALLOWED_HOST)
        report.check("role_allowed_host_via_proxy", reached(role["allowed"]) and bool(admitted),
                     {"probe": role["allowed"], "proxy_records": len(admitted)})
        denied = self.denials(linux, role_since, ROLE)
        report.check(
            "role_unlisted_host_refused",
            proxy_refused(role["unlisted"])
            and any(row["reason"] == "not_allowed" for row in denied),
            {"probe": role["unlisted"], "proxy_denials": denied},
        )

        container_since = time.time()
        inside = self.role_probe("container-boundary", [
            ("allowed", container(curl(ALLOWED_URL))),
            ("unlisted", container(curl(UNLISTED_URL))),
            ("direct", container(curl(f"https://{DIRECT[0]}", "--noproxy", "*", timeout=10))),
        ])
        admitted = self.admitted(linux, container_since, ROLE, ALLOWED_HOST)
        report.check("container_allowed_host_via_proxy",
                     reached(inside["allowed"]) and bool(admitted),
                     {"probe": inside["allowed"], "proxy_records": len(admitted)})
        denied = self.denials(linux, container_since, ROLE)
        report.check(
            "container_unlisted_host_refused",
            proxy_refused(inside["unlisted"])
            and any(row["reason"] == "not_allowed" for row in denied),
            {"probe": inside["unlisted"], "proxy_denials": denied},
        )
        report.check("container_direct_address_refused", refused(inside["direct"]),
                     inside["direct"])

        def counters():
            traffic = box.network_status()["vms"]["linux"]["traffic"]
            rows = [destination_bytes(traffic, address, 443) for address in DIRECT]
            rows.append(destination_bytes(traffic, DIRECT[0], 53, "udp"))
            passed = bool(traffic.get("live")) and all(
                row["blocked_bytes"] > 0 and row["allowed_bytes"] == 0 for row in rows
            )
            return passed, {"destinations": rows, "blocked_bytes": traffic.get("blocked_bytes")}

        report.attempt("filter_counts_direct_attempts", counters)
        report.attempt("host_listener_unreachable", self.host_listener)
        report.attempt("live_allow", self.live_allow)
        report.attempt("live_revoke", self.live_revoke)
        try:
            closes, keeps = self.revoke_held_tunnels()
        except Exception as error:
            closes = keeps = (False, {"error": describe(error)})
        report.check("revoke_closes_open_tunnel", *closes)
        report.check("revoke_keeps_other_tunnels", *keeps)
        if box.mac is None:
            report.skip("mac_station_proxy", "No [mac] is configured")
        else:
            report.attempt("mac_station_proxy", self.mac_station)
        report.attempt("cutoff_blocks_proxy", self.cutoff)
        report.attempt("cutoff_retained_after_restart", self.restart)
        report.attempt("named_volume_retained", self.volume)
        report.attempt("on_restores_proxy", self.restore)
        report.observe("clock_drift_end", self.drift())
        report.observe("live_observation", self.box.network_status())

        def stop():
            self.box.down(force=True)
            state = self.box.state()
            return state == "stopped", {"state": state}

        report.attempt("vms_stopped", stop)
        report.observe("historical_observation", self.box.network_status())

    def host_listener(self):
        """Nothing on the host but the proxy port is reachable at the gateway."""
        box = self.box
        gateways = {"linux": box.linux.network.gateway}
        if box.mac is not None:
            gateways["mac"] = box.mac.network.gateway
        with synthetic_host_listener() as (port, hits):
            target = f"http://{gateways['linux']}:{port}"
            results = self.role_probe("host-listener", [
                ("role", curl(f"{target}/role", "--noproxy", "*", timeout=5)),
                ("container", container(curl(f"{target}/container", "--noproxy", "*",
                                             timeout=5))),
            ])
            exits = {name: row["exit"] for name, row in results.items()}
            if box.mac is not None:
                exits["mac"] = box.mac._shell(
                    "/usr/bin/curl", "--noproxy", "*", "-fsS", "--max-time", "5",
                    f"http://{gateways['mac']}:{port}/mac", check=False, timeout=30,
                ).returncode
            status = box.network_status()
            blocked = {
                side: destination_bytes(status["vms"][side]["traffic"], gateway, port)
                for side, gateway in gateways.items()
            }
            passed = (
                all(exit not in (0, None) for exit in exits.values())
                and not hits
                and all(row["blocked_bytes"] > 0 and not row["allowed_bytes"]
                        for row in blocked.values())
            )
            return passed, {"port": port, "gateways": gateways, "exits": exits,
                            "guest_requests_received": len(hits), "filter_denials": blocked}

    def _hosts_change(self, verb, reach):
        since = time.time()
        command = self.network(verb, UNLISTED)
        results = self.role_probe(f"{verb}-{UNLISTED}", [
            ("role", curl(UNLISTED_URL)),
            ("container", container(curl(UNLISTED_URL))),
        ])
        granted = {"host": UNLISTED, "source": "granted"} in self.box.network_status()[
            "allowed_hosts"]
        events = [
            {"time": event["time"], "hosts": event.get("hosts"), "failures": event.get("failures")}
            for event in self.box.events()
            if event.get("kind") == "network.hosts_changed" and event_time(event) >= since - 1
        ]
        changed = any(
            (UNLISTED in (event["hosts"] or ())) is reach and not event["failures"]
            for event in events
        )
        probes_ok = all((reached if reach else proxy_refused)(row) for row in results.values())
        evidence = {"command": command["command"], "probes": results,
                    "allowed_hosts_lists_grant": granted, "hosts_changed_events": events}
        return probes_ok and granted is reach and changed, evidence

    def live_allow(self):
        """`firm network ROOT allow` takes effect for a running role with no restart."""
        return self._hosts_change("allow", True)

    def live_revoke(self):
        return self._hosts_change("revoke", False)

    def revoke_held_tunnels(self):
        """Hold TLS tunnels to the granted host and a baseline host, then revoke the first.

        The role's program reports each tunnel's close; the check notes when it first sees
        each report, polling the run's log, so the seconds are on the host's clock.
        """
        from firm_sandbox import RunSpec

        box = self.box
        self.network("allow", UNLISTED)
        run = box.start(RunSpec(
            ROLE, (box.layout(ROLE).python, "-I", "-c", TUNNEL_PROGRAM, UNLISTED, ALLOWED_HOST),
            organization=self.organization,
        ))
        self.runs += 1
        log = self.args.evidence / f"{self.runs:02d}-held-tunnels.log"
        closed_seen = {}

        def read():
            output = box.logs(run).decode(errors="replace")
            log.write_text(output)
            events = tunnel_events(output)
            for row in events:
                if row["event"] == "closed":
                    closed_seen.setdefault(row["host"], time.time())
            return events

        try:
            deadline = time.monotonic() + 90
            while not any(row["event"] == "held" for row in read()):
                if not box.status(run).get("running") or time.monotonic() > deadline:
                    raise RuntimeError(f"The tunnels were never held; inspect {log.name}")
                time.sleep(0.2)
            open_at_revoke = {UNLISTED, ALLOWED_HOST} - set(closed_seen)
            revoke_started = time.time()
            self.network("revoke", UNLISTED)
            revoke_done = time.time()
            while time.time() < revoke_done + REVOKE_SECONDS:
                read()
                time.sleep(0.2)
            box.linux._rpc(
                "write-credential", role_id=ROLE, name=TUNNEL_DONE,
                data=base64.b64encode(b"done").decode(),
            )
            state = box.wait(run, timeout=60)
            events = read()
            if state.get("unresolved") or not any(row["event"] == "done" for row in events):
                raise RuntimeError(f"The tunnel program did not finish; inspect {log.name}")
        finally:
            if box.status(run).get("running"):
                box.stop(run)
        return tunnel_verdicts(events, closed_seen, open_at_revoke, revoke_started,
                               revoke_done, UNLISTED, ALLOWED_HOST)

    def mac_station(self):
        """A leased station reaches an allowed host through the proxy and nothing else."""
        from mac.acceptance_common import Probe

        box = self.box
        directory = self.args.evidence / "mac-station"
        directory.mkdir(mode=0o700)
        holder = Probe(box, directory, self.args.station, role=MAC_ROLE)
        since = time.time()
        results, settings, session = {}, {}, None
        errors = []
        try:
            holder.start()
            holder.acquire("network")
            holder.command("printf lease-probe-network")
            for name, command in (
                ("allowed", curl(ALLOWED_URL)),
                ("unlisted", curl(UNLISTED_URL)),
                ("direct", curl(f"https://{DIRECT[0]}", "--noproxy", "*", timeout=10)),
            ):
                reply = holder.command(shlex.join(command), check=False)
                codes = fields(reply.get("stdout") or "")
                results[name] = {
                    "exit": reply.get("returncode"),
                    **{key: code(codes.get(key)) for key in ("http", "connect")},
                }
            settings = scutil_proxy(holder.command("/usr/sbin/scutil --proxy")["stdout"])
            session = holder.command("printenv HTTPS_PROXY", check=False).get("stdout", "").strip()
            holder.release()
        finally:
            if holder.lease is not None:
                try:
                    box.mac_release(holder.lease["lease_id"])
                except Exception as error:
                    errors.append(f"release: {error}")
            if holder.run is not None:
                try:
                    (directory / "role.log").write_bytes(box.logs(holder.run))
                    box.stop(holder.run)
                except Exception as error:
                    errors.append(f"stop: {error}")
        network = box.mac.network
        expected = {}
        for scheme in ("HTTP", "HTTPS"):
            expected.update({f"{scheme}Enable": "1", f"{scheme}Proxy": network.gateway,
                             f"{scheme}Port": str(network.proxy_port)})
        system_proxy = all(settings.get(key) == value for key, value in expected.items())
        denied = self.denials(box.mac, since, "mac")
        passed = (
            reached(results["allowed"]) and proxy_refused(results["unlisted"])
            and refused(results["direct"]) and system_proxy and bool(denied) and not errors
        )
        return passed, {
            "station": self.args.station, "probes": results, "scutil_proxy": settings,
            "system_proxy_set": system_proxy, "session_https_proxy": session,
            "proxy_denials": denied, "cleanup_errors": errors,
        }

    def blocked_everywhere(self, label):
        """The allowed host fails from the role, a container and the Mac."""
        results = self.role_probe(label, [
            ("role", curl(ALLOWED_URL, timeout=10)),
            ("container", container(curl(ALLOWED_URL, timeout=10))),
        ])
        if self.box.mac is not None:
            results["mac"] = {"exit": self.mac_control(ALLOWED_URL)}
        return all(map(refused, results.values())), results

    def cutoff(self):
        """`firm network ROOT off` stops the proxy path too."""
        command = self.network("off")
        blocked, results = self.blocked_everywhere("cutoff")
        enabled = self.box.network_status()["enabled"]
        return blocked and enabled is False, {"command": command["command"], "probes": results,
                                              "enabled": enabled}

    def restart(self):
        """The cutoff holds across a VM restart, which starts no role processes."""
        from firm_sandbox import Sandbox

        self.box.down()
        stopped = self.box.state()
        self.box = box = Sandbox(self.config)
        box.up()
        role_slice = box.linux.info()["guest"]["roles"][ROLE]["slice"]
        slice_state = box.linux._shell(
            "sudo", "-n", "systemctl", "is-active", role_slice, check=False,
        ).stdout.decode().strip()
        saved = {"linux": box.linux.network.saved_egress()}
        if box.mac is not None:
            saved["mac"] = box.mac.network.saved_egress()
        blocked, results = self.blocked_everywhere("after-restart")
        passed = (stopped == "stopped" and slice_state == "inactive"
                  and not any(saved.values()) and blocked)
        return passed, {"stopped_state": stopped, "role_slice_after_boot": slice_state,
                        "saved_egress": saved, "probes": results}

    def volume(self):
        results = self.role_probe("volume", [(
            "retained",
            ("sh", "-c", f"docker run --rm -v {VOLUME}:/retained {VOLUME_IMAGE} "
                         "cat /retained/result | grep -q retained-volume"),
        )])
        return results["retained"]["exit"] == 0, results

    def restore(self):
        """`firm network ROOT on` restores the proxy path."""
        command = self.network("on")
        results = self.role_probe("restored", [
            ("role", curl(ALLOWED_URL)),
            ("container", container(curl(ALLOWED_URL))),
        ])
        passed = all(map(reached, results.values()))
        if self.box.mac is not None:
            results["mac"] = {"exit": self.mac_control(ALLOWED_URL)}
            passed = passed and results["mac"]["exit"] == 0
        enabled = self.box.network_status()["enabled"]
        return passed and enabled is True, {"command": command["command"], "probes": results,
                                            "enabled": enabled}

    def cleanup(self):
        """Leave the firm as found: the host revoked, egress on, both VMs stopped."""
        errors = []
        try:
            if UNLISTED in self.box.granted_hosts():
                self.network("revoke", UNLISTED)
        except Exception as error:
            errors.append(f"revoke: {error}")
        for name, step in (
            ("egress on", lambda: self.box.set_network_enabled(True)),
            ("stop", lambda: self.box.down(force=True)),
        ):
            try:
                step()
            except Exception as error:
                errors.append(f"{name}: {error}")
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--station", type=int, help="Mac station to probe")
    args = parser.parse_args()
    if not (args.root / "durable" / "firm.sqlite3").is_file():
        parser.error(f"{args.root} has no runtime database; run `firm init` on it first, "
                     "because the check changes the network with `firm network`")
    from firm_runtime.compatibility import validate_firm

    validate_firm(args.root)
    from firm_sandbox import Config

    config = Config.load(args.root / "sandbox.toml")
    report = Report(args.evidence, CHECKS, root=str(args.root))
    check = NetworkCheck(args, config, report)
    if check.box.mac is not None:
        from mac.acceptance_common import select_station

        args.station = select_station(check.box, args.station)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    failure = None
    try:
        check.run()
    except Exception as error:
        failure = error
    finally:
        errors = check.cleanup()
    passed = report.finish(failure, errors)
    print(f"{'PASS' if passed else 'FAIL'} network acceptance; see {report.path}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
