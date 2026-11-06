"""Validate Linux execution, isolation and retention; --replace destroys this test firm.

Two roles run concurrently on the Linux VM with separate homes, slices and
Docker daemons; one checks account and host isolation, independent stop, the
filter-loss launch refusal, restart retention and whole-firm replacement. It
needs no Mac; a configured Mac VM only boots and stops with the firm.
"""

import argparse
import json
import socket
import time
from pathlib import Path

from firm_sandbox import Config, Organization, RunSpec, Sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--replace", action="store_true", required=True)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    results = []

    def record(name, result):
        results.append({"check": name, "result": result})
        (args.evidence / "results.json").write_text(json.dumps(results, indent=2))
        print(name, json.dumps(result), flush=True)

    def run(role, script, *, timeout=180, organization=None, secrets=None):
        job = firm.start(RunSpec(
            role, ("/bin/bash", "-eu", "-c", script), organization=organization,
            secrets=secrets or {},
        ))
        state = firm.wait(job, timeout=timeout)
        output = firm.logs(job).decode(errors="replace")
        (args.evidence / f"{job.run_id}.log").write_text(output)
        assert state["exit_code"] == 0 and not state.get("unresolved"), (state, output)
        return output

    a_org, b_org = Organization("role-a"), Organization("role-b")
    try:
        started = time.monotonic()
        firm.up()
        state = firm.linux.info()
        record("boot", {"seconds": time.monotonic() - started, **state})
        assert state["guest"]["roles"] == {}, "Use a new dedicated test firm"
        for role, organization in (("role-a", a_org), ("role-b", b_org)):
            firm.file_server.provision(role, organization)
        a, b = firm.layout("role-a"), firm.layout("role-b")
        first = firm.start(RunSpec(
            "role-a",
            ("/bin/bash", "-eu", "-c",
             "sleep 10; id; cat /proc/self/cgroup; grep \"^$(id -un):\" /etc/subuid; "
             "uv venv .venv; uv run --with numpy python -c "
             "'import numpy; print(numpy.arange(10).sum())'; docker info --format '{{.Name}}'"),
            organization=a_org,
        ))
        second = firm.start(RunSpec(
            "role-b",
            ("/bin/bash", "-eu", "-c",
             "sleep 10; id; docker run --rm alpine:3.20 echo container-b; "
             "printf private > \"$HOME/private-note\"; printf work > work-note"),
            organization=b_org,
        ))
        assert firm.status(first)["running"] and firm.status(second)["running"]
        record("concurrent", [first.run_id, second.run_id])
        for job in (first, second):
            status = firm.wait(job, timeout=300)
            output = firm.logs(job).decode(errors="replace")
            (args.evidence / f"{job.role_id}.log").write_text(output)
            assert status["exit_code"] == 0 and not status.get("unresolved"), (status, output)
            record(job.role_id, status)
        record(
            "role_isolation",
            run(
                "role-a",
                f"test ! -r {b.private}/private-note; test ! -r /var/lib/firm/firm.json; "
                "test ! -w /usr/local/libexec/firm-controller.py; test ! -w /usr/local/bin; "
                "test ! -r /srv/firm/org; ! sudo -n true; "
                f"if cat {a.drive}/org/role-b/work/work-note; then exit 30; fi; "
                f"if touch {a.drive}/org/role-b/work/denied; then exit 31; fi; id",
                organization=a_org,
            ),
        )
        # A loopback-only host service must be unreachable through the usernet gateway.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            gateway = firm.linux.subnet.replace("0/24", "2")
            network_script = (
                "import socket,urllib.request; "
                "print(urllib.request.urlopen('https://example.com',timeout=20).status); "
                f"s=socket.socket(); s.settimeout(2); result=s.connect_ex(('{gateway}',{port})); "
                "assert result != 0, 'Host loopback accessible'; "
                "print('host access blocked',result)"
            )
            job = firm.start(RunSpec("role-a", ("/usr/bin/python3", "-c", network_script)))
            status = firm.wait(job)
            output = firm.logs(job).decode(errors="replace")
            assert status["exit_code"] == 0, (status, output)
            record("network", output)
        lingering = firm.start(RunSpec("role-a", ("/bin/bash", "-c", "sleep 300 & wait")))
        other = firm.start(RunSpec("role-b", ("/bin/sleep", "300")))
        stopped = firm.stop(lingering)
        assert not stopped["running"] and not stopped.get("unresolved")
        assert firm.status(other)["running"]
        firm.stop(other)
        role_slice = firm.linux.info()["guest"]["roles"]["role-a"]["slice"]
        slice_state = firm.linux._shell(
            "sudo", "-n", "systemctl", "is-active", role_slice, check=False
        ).stdout.decode().strip()
        assert slice_state == "inactive", slice_state
        # The stop settles the live slice before its supervisor does, so its reading
        # is the only one the run has.
        usage = stopped.get("usage")
        assert usage and usage["cpu_usec"] is not None, stopped
        record("independent_stop", {**stopped, "slice_after_stop": slice_state})
        firm.linux.network.stop()
        assert firm.linux._running() and not firm.linux.network.ready()
        try:
            firm.start(RunSpec("role-a", ("/usr/bin/true",)))
        except RuntimeError as exc:
            assert "network boundary is unavailable" in str(exc)
            record("filter_loss_blocks_launch", str(exc))
        else:
            raise AssertionError("Launch proceeded without its network filter")
        firm.down()
        firm.up()
        resumed = firm.linux.info()
        assert resumed["generation"] == state["generation"]
        record("restart_retention", run(
            "role-b", "test \"$(cat \"$HOME/private-note\")\" = private; echo retained",
        ))
        started = time.monotonic()
        firm.replace()
        replacement = firm.linux.info()
        assert replacement["generation"] != state["generation"]
        assert replacement["guest"]["roles"] == {}
        record("replacement", {"seconds": time.monotonic() - started, **replacement})
        try:
            firm.status(first)
        except ValueError:
            pass
        else:
            raise AssertionError("Discarded generation accepted an old run")
        firm.file_server.provision("role-b", b_org)
        record("replacement_clean", run(
            "role-b", "test ! -e \"$HOME/private-note\"; test ! -e work-note; echo clean",
            organization=b_org,
        ))
    finally:
        firm.down()
        record("stopped", {"linux": firm.linux.status(), "state": firm.state()})


if __name__ == "__main__":
    main()
