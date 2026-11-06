"""Ten Mac leases and cleanup failures, through a real Linux role's SSH endpoint.

Requires an already running disposable firm with an enrolled, unlocked Mac.
Leaves the VMs running. The only injected owner fault is a system-immutable guest
LaunchAgent file, which root sets: release clears a user-immutable one itself, as the
station account that could set it. A finally block removes the flag and the file before
recovery. Every clean release must also have completed its reclaim and all three
simulator steps.
"""

import base64
import json
import plistlib
import shlex
from uuid import uuid4

from acceptance_common import (
    Probe,
    acceptance_parser,
    cleanup_probe,
    select_station,
    station_home,
    station_status,
    station_uid,
    write_json,
)
from firm_sandbox import Config, Sandbox
from firm_sandbox.mac_station import VERIFY_RPC_TIMEOUT

ROLE = "lease-cleanup"
CONTENDER = "lease-contender"


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(firm, args.station)
    if not firm.linux._running() or not firm.mac._running():
        raise SystemExit("Start a disposable firm with an enrolled Mac before this check")
    before = station_status(firm, args.station)
    if before["held"] or before["dirty"]:
        raise SystemExit("The station must be free and clean before this check")
    probe = Probe(firm, args.evidence, args.station, role=f"{ROLE}-{args.station}")
    contender = None
    result = {"passed": False, "leases": probe.records}
    immutable = None
    injected_dirty = False
    failure = None
    try:
        probe.start()
        for index in range(10):
            item = probe.acquire(f"cycle-{index + 1:02d}")
            item["baseline_verification"] = firm.mac._rpc(
                "station-verify", station=args.station,
                baseline=probe.records[0]["baseline"], timeout=VERIFY_RPC_TIMEOUT,
            )
            assert item["baseline_verification"]["ok"], item["baseline_verification"]
            item["ssh"] = probe.command(
                f"printf 'lease-probe-cycle-{index + 1:02d}\\n'; id -u"
            )
            expected_uid = str(station_uid(probe.lease["station"]))
            assert item["ssh"]["stdout"].splitlines()[-1] == expected_uid, item["ssh"]
            probe.release()
        first = probe.records[0]["baseline"]
        result["strict_baselines_equal"] = all(
            item["baseline"] == first for item in probe.records
        )
        # The cleanup contract allows known processes to exit and trusted,
        # launchd-managed processes to respawn. Keep exact PID equality as
        # evidence, while requiring no unexplained work against cycle one's
        # baseline (including every extra service, LaunchAgent and crontab).
        result["baseline_contract_passed"] = all(
            item["baseline_verification"]["ok"] for item in probe.records
        )

        item = probe.acquire("background")
        item["ssh"] = probe.command(
            "nohup /bin/sleep 600 >/dev/null 2>&1 & "
            "printf 'lease-probe-background %s\\n' \"$!\""
        )
        probe.release()

        item = probe.acquire("launch-agent")
        label = "dev.firm.lease-probe-" + uuid4().hex
        agent = {
            "Label": label, "ProgramArguments": ["/bin/sleep", "600"],
            "RunAtLoad": True, "KeepAlive": True,
        }
        encoded = base64.b64encode(plistlib.dumps(agent)).decode()
        path = f"$HOME/Library/LaunchAgents/{label}.plist"
        item["ssh"] = probe.command(
            'mkdir -p "$HOME/Library/LaunchAgents"; '
            f"printf %s {shlex.quote(encoded)} | /usr/bin/base64 -D > \"{path}\"; "
            f'/bin/launchctl bootstrap gui/{station_uid(probe.lease["station"])} "{path}" && '
            f'/bin/launchctl print gui/{station_uid(probe.lease["station"])}/{label} && '
            "printf lease-probe-launch-agent"
        )
        released = probe.release()
        assert released["checks"]["work_stopped"]["launch_agents"]["extra"] == [], released
        assert released["checks"]["work_stopped"]["services"]["extra"] == [], released

        # A crontab the holder installs must be read, removed and verified gone.
        item = probe.acquire("crontab")
        item["ssh"] = probe.command(
            "printf '* * * * * /usr/bin/true\\n' | /usr/bin/crontab - && "
            "/usr/bin/crontab -l && printf lease-probe-crontab"
        )
        assert "lease-probe-crontab" in item["ssh"]["stdout"], item["ssh"]
        released = probe.release()
        assert released["checks"]["work_stopped"]["crontab"]["empty"] is True, released

        item = probe.acquire("control-master")
        probe.command(mode="master")
        item["before"] = probe.command("printf lease-probe-control-before", mode="channel")
        lease = probe.lease
        probe.release()
        # Only to attempt the revoked endpoint from the same Linux process.
        probe.lease = lease
        try:
            item["after"] = probe.command(
                "printf lease-probe-control-after", mode="channel", check=False,
            )
        finally:
            probe.lease = None
        assert item["after"]["returncode"] not in (0, None), item["after"]
        assert item["release"]["checks"]["access_revoked"]["sessions"] == [], item

        item = probe.acquire("immutable-agent")
        immutable = str(station_home(probe.lease) / "Library/LaunchAgents"
                        / f"dev.firm.lease-probe-{uuid4().hex}.plist")
        injected_dirty = True
        item["ssh"] = probe.command(
            f"printf lease-probe-immutable > {shlex.quote(immutable)}; "
            "printf lease-probe-dirty"
        )
        # Release clears uchg as the station account, which can set it; only root sets schg.
        probe.owner(f"/usr/bin/chflags schg {shlex.quote(immutable)}")
        released = probe.release(clean=False)
        assert released["checks"]["work_stopped"]["ok"] is False, released
        item["dirty_status"] = station_status(firm, args.station)
        assert item["dirty_status"]["dirty"] is True, item
        # The contender is a role with its own run: on a Mac with another free station it
        # is served there, and the lease's key is written into that run's credentials.
        contender_role = f"{CONTENDER}-{args.station}"
        contender = Probe(firm, args.evidence, args.station, role=contender_role)
        contender.start()
        try:
            unexpected = firm.mac_acquire(contender_role, "cleanup-refused")
        except (RuntimeError, OSError) as exc:
            item["refused"] = {"type": type(exc).__name__, "message": str(exc)}
            # With another station leased the refusal is busy and names a leased station,
            # never the dirty one; otherwise it asks for recovery.
            another_busy = (
                len(firm.mac.stations) > 1
                and "already leased" in str(exc)
                and f"Mac station {args.station} " not in str(exc)
            )
            assert "verified release or owner recovery" in str(exc) or another_busy, (
                item["refused"]
            )
        else:
            # A firm with another free station serves the contender there; the dirty
            # station itself must never grant a lease.
            served = unexpected["station"]
            firm.mac_release(unexpected["lease_id"])
            assert served != args.station, "A second role acquired the dirty station"
            item["served_elsewhere"] = served
        probe.remove_immutable(immutable)
        immutable = None
        item["recovery"] = firm.mac_station(args.station).recover(confirm_cleanup=True)
        assert item["recovery"]["clean"] and item["recovery"]["recovered"], item
        assert station_status(firm, args.station)["dirty"] is False
        probe.acquire("after-recovery")
        probe.command("printf lease-probe-after-recovery")
        probe.release()
        result["passed"] = True
    except BaseException as exc:
        failure = exc
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        cleanup_errors = []
        if immutable is not None:
            try:
                probe.remove_immutable(immutable)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if probe.lease is not None:
            try:
                result["final_release"] = firm.mac_release(probe.lease["lease_id"])
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if injected_dirty and station_status(firm, args.station)["dirty"]:
            try:
                result["final_recovery"] = firm.mac_station(args.station).recover(
                    confirm_cleanup=True
                )
                if not result["final_recovery"]["clean"]:
                    cleanup_errors.append("The station remains dirty after owner cleanup")
            except Exception as exc:
                cleanup_errors.append(str(exc))
        cleanup_errors += cleanup_probe(firm, probe, "holder.log")
        cleanup_errors += cleanup_probe(firm, contender)
        if cleanup_errors:
            result["passed"] = False
            result["cleanup_errors"] = cleanup_errors
        write_json(args.evidence / "result.json", result)
    print(json.dumps({"passed": result["passed"], "evidence": str(args.evidence)}))
    if failure is not None:
        raise failure
    if not result["passed"]:
        raise SystemExit("Acceptance cleanup failed; inspect result.json")


if __name__ == "__main__":
    main()
