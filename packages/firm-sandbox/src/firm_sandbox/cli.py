from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from . import mac_enrollment, mac_image
from .commands import CommandError
from .compatibility import initialize_firm_record, release_identifier, validate_firm_record
from .models import Config, RunSpec
from .sandbox import NO_MAC, Sandbox

MAC_ACTIONS = {
    "mac-status",
    "mac-recover",
    "mac-install-helper",
    "mac-prepare",
    "mac-image-seal",
    "mac-image-apply",
    "mac-replace",
    "mac-restart",
    "mac-session-reset",
}


def _assignments(values, option):
    """Parse repeatable NAME=VALUE options with an actionable CLI error."""
    result = {}
    for raw in values:
        name, separator, value = raw.partition("=")
        if not separator or not name:
            raise ValueError(f"{option} values must use NAME=VALUE")
        result[name] = value
    return result


def _with_station(parser, *, required=False):
    parser.add_argument(
        "--station",
        type=int,
        metavar="N",
        required=required,
        help="the account number on the firm's Mac",
    )
    return parser


def _station(sandbox, requested):
    """Validate an explicitly selected account against the Mac's recorded inventory."""
    if type(requested) is not int or requested not in sandbox.mac.stations:
        raise ValueError(f"This firm has no Mac station {requested}")
    return requested


def _network(sandbox, mode):
    """``network`` verbs. Granted hosts are the runtime's to write (``firm network``): one
    writer, so no second path can race it."""
    if mode == "status":
        return sandbox.network_status()
    if mode == "hosts":
        return sandbox.allowed_hosts()
    return sandbox.set_network_enabled(mode == "on")


@contextmanager
def _terminate_interrupts():
    """Unwind SIGTERM like Ctrl-C, so an image verb's cleanup runs before the process exits."""

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Operate one firm's execution infrastructure: its Linux VM, where every "
        "role lives, and its optional Mac stations."
    )
    parser.add_argument("--config", type=Path, default=Path("sandbox.toml"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "init", help="Record this release for a new sandbox-only firm without starting VMs"
    )
    for name, description in (
        ("up", "boot the Linux VM, file server and optional Mac; disks are retained"),
        ("down", "stop the VMs, keeping every guest disk"),
        ("force-down", "power off the VMs without waiting for guest runs to exit"),
        ("status", "report whether the VMs are running; not company state"),
        ("data-status", "report where the shared trees live and how the owner mounts them"),
        ("ps", "list recorded guest runs"),
        ("resources", "report what each machine is using: memory, CPU and disk"),
        ("events", "print the sandbox event log"),
        ("state", "print the one-word infrastructure state"),
        ("network-status", "report the host packet filters, egress proxies and egress policy"),
    ):
        sub.add_parser(name, help=description)
    _with_station(
        sub.add_parser(
            "mac-status",
            help="report each Mac station: configured, enrolled, held, holder, since, dirty",
        )
    )
    for name, description in (
        ("stop", "terminate a role's current guest run"),
        ("logs", "print a role's retained run output"),
        ("wait", "wait for a role's current run to exit"),
        ("remove", "remove a role's settled run record"),
    ):
        sub.add_parser(name, help=description).add_argument("role")
    sub.add_parser(
        "retire", help="revoke a stopped role's access; its data is retained"
    ).add_argument("role")
    sub.add_parser(
        "dispose", help="empty a stopped role's scratch and remove its stopped containers, "
        "dangling images and build cache (tagged images and volumes stay)"
    ).add_argument("role")
    sub.add_parser(
        "storage", help="measure scratch and Docker usage per role, read-only: "
        "du estimates share a 60s request budget",
        description="Read-only du estimates. Both forms share 60s across sorted roles "
        "and categories. "
        "Rerun roles with unknown measurements individually.",
    ).add_argument("role", nargs="?")
    for name in ("run", "start"):
        cmd = sub.add_parser(
            name,
            help="launch a command on the Linux VM as ROLE's account"
            + (" and follow its output" if name == "run" else ""),
        )
        cmd.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
        cmd.add_argument("--secret", action="append", default=[], metavar="NAME=HOST_FILE")
        cmd.add_argument("--secret-env", action="append", default=[], metavar="ENV_VAR=SECRET_NAME")
        cmd.add_argument(
            "--stdin", action="store_true", help="read initial command input from stdin"
        )
        cmd.add_argument("role")
        cmd.add_argument("command", nargs=argparse.REMAINDER)
    build = sub.add_parser("mac-image-build", help="build a Mac base image at DESTINATION")
    build.add_argument("destination", type=Path)
    build.add_argument(
        "--recipe", type=Path, required=True, help="the image recipe (see images/ios.toml)"
    )
    sub.add_parser(
        "mac-restart",
        help="restart the shared Mac and restore station sessions, keeping Linux running",
    )
    _with_station(
        sub.add_parser("mac-session-reset", help="reset only the named station account's desktop"),
        required=True,
    )
    sub.add_parser(
        "replace",
        help="discard every guest disk and start a fresh Linux VM; with [mac], also clone "
        "mac.image again",
    )
    sub.add_parser(
        "mac-replace",
        help="adopt sandbox.toml's configured image onto the Mac VM; the firm must "
        "already be stopped, and the Linux VM and its disk are untouched",
    )
    network = sub.add_parser(
        "network",
        help="inspect or control egress: status, the on/off cutoff, or the allowed hosts",
    )
    network.add_argument(
        "mode",
        choices=("status", "on", "off", "hosts"),
        default="status",
        nargs="?",
    )
    recovery = _with_station(
        sub.add_parser(
            "mac-recover",
            help="clear a stale Mac lease marker after the owner has inspected the station",
        ),
        required=True,
    )
    recovery.add_argument(
        "--confirm-cleanup",
        action="store_true",
        required=True,
        help="confirm you inspected the station and ended lease activity; "
        "the four release checks must still hold against the last lease baseline",
    )
    install = sub.add_parser(
        "mac-install-helper", help="install or update the signed station helper on a Mac VM"
    )
    install.add_argument("app", type=Path)
    install.add_argument(
        "--mode",
        choices=("update", "enroll", "development"),
        default="update",
        help="update the installed helper, enroll prepared accounts, or use an unsigned build",
    )
    prepare = sub.add_parser(
        "mac-prepare", help="prepare recipe accounts and advance their enrollment console turns"
    )
    prepare.add_argument("app", type=Path)
    prepare.add_argument("--recipe", type=Path, required=True)

    seal = sub.add_parser(
        "mac-image-seal",
        help="seal the enrollment Mac VM into an enrolled Mac image at DESTINATION",
    )
    seal.add_argument("destination", type=Path)
    seal.add_argument(
        "--recipe", type=Path, required=True, help="the image recipe (see images/ios.toml)"
    )
    sub.add_parser(
        "mac-image-apply",
        help="install every simulator runtime RECIPE names that the enrollment firm's Mac VM lacks",
    ).add_argument("recipe", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            config = Config.load(args.config)
            result = initialize_firm_record(config.root, release_identifier())
            print(json.dumps(result, indent=2))
            return
        if args.action == "mac-image-build":
            with _terminate_interrupts():
                result = mac_image.build_base_image(
                    args.destination, mac_image.Recipe.load(args.recipe)
                )
            print(json.dumps(result, indent=2))
            return
        # Power-off remains available even when new code refuses the recorded state.
        # Every other command constructs Sandbox, which may create host records or
        # install/boot a guest, so compatibility is checked before that constructor.
        if args.action not in {"down", "force-down"}:
            validate_firm_record(args.config.expanduser().absolute().parent)
        sandbox = Sandbox(Config.load(args.config))
        if args.action in MAC_ACTIONS and sandbox.mac is None:
            raise RuntimeError(NO_MAC)
        result = None
        if args.action in {"up", "down", "resources", "events", "state", "network-status"}:
            method = "network_status" if args.action == "network-status" else args.action
            result = getattr(sandbox, method)()
        elif args.action == "force-down":
            result = sandbox.force_down()
        elif args.action == "mac-restart":
            result = sandbox.mac_restart()
        elif args.action == "mac-session-reset":
            result = sandbox.mac_session_reset(_station(sandbox, args.station))
        elif args.action == "replace":
            result = sandbox.replace()
        elif args.action == "mac-replace":
            result = sandbox.mac_replace()
        elif args.action == "network":
            result = _network(sandbox, args.mode)
        elif args.action == "mac-status":
            # Without --station every station is reported, each with its own probes.
            result = sandbox.mac_status()
            if args.station is not None:
                chosen = _station(sandbox, args.station)
                result["stations"] = [row for row in result["stations"] if row["station"] == chosen]
            for row in result["stations"]:
                station = sandbox.mac_station(row["station"])
                for name, probe in (
                    ("helper", station.helper_status),
                    ("awake", station.awake_status),
                ):
                    # A stopped Mac has already answered for both; nothing is asked of it.
                    if name in row:
                        continue
                    try:
                        row[name] = probe()
                    except (OSError, ValueError, RuntimeError) as exc:
                        row[name] = {"error": str(exc)}
        elif args.action == "mac-recover":
            result = sandbox.mac_station(_station(sandbox, args.station)).recover(
                confirm_cleanup=args.confirm_cleanup
            )
            if result.get("clean") is not True or result.get("status") != "ok":
                print(json.dumps(result, indent=2))
                raise SystemExit(2)
        elif args.action == "mac-install-helper":
            result = mac_enrollment.install_helper(sandbox.mac, args.app, mode=args.mode)
        elif args.action == "mac-prepare":
            try:
                result = mac_enrollment.prepare(
                    sandbox.mac, args.app, mac_image.Recipe.load(args.recipe)
                )
            except BaseException:
                # A turn reboots the Mac, which drops the file server's forward into it,
                # and can fail afterwards (a station that does not come back ready): the
                # next thing the owner runs is the drive handoff, which needs the forward.
                # The turn's own failure is what is reported.
                try:
                    if sandbox.mac._running():
                        sandbox.file_server.reconnect()
                except Exception:
                    pass
                raise
            sandbox.file_server.reconnect()
        elif args.action == "mac-image-seal":
            with _terminate_interrupts():
                result = mac_image.seal_enrolled_image(
                    sandbox, args.destination, mac_image.Recipe.load(args.recipe)
                )
        elif args.action == "mac-image-apply":
            with _terminate_interrupts():
                result = mac_image.apply_recipe(sandbox.mac, mac_image.Recipe.load(args.recipe))
        elif args.action == "storage":
            result = sandbox.storage(args.role)
        elif args.action == "dispose":
            result = sandbox.dispose(args.role)
        elif args.action == "retire":
            sandbox.retire(args.role)
        elif args.action == "status":
            result = sandbox.infrastructure_status()
        elif args.action == "data-status":
            result = sandbox.data_status()
        elif args.action == "ps":
            result = sandbox.states()
        elif args.action in {"stop", "logs", "wait", "remove"}:
            result = getattr(sandbox, args.action)(sandbox.current(args.role))
        else:
            command = args.command
            if command and command[0] == "--":
                command = command[1:]
            spec = RunSpec(
                role_id=args.role,
                argv=tuple(command),
                env=_assignments(args.env, "--env"),
                secrets={
                    name: Path(path).read_bytes()
                    for name, path in _assignments(args.secret, "--secret").items()
                },
                stdin=sys.stdin.buffer.read() if args.stdin else b"",
                secret_env=_assignments(args.secret_env, "--secret-env"),
            )
            run = sandbox.start(spec)
            if args.action == "start":
                result = sandbox.status(run)
            else:
                try:
                    for chunk in sandbox.follow(run):
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    result = sandbox.wait(run)
                except (KeyboardInterrupt, CommandError, OSError):
                    sandbox.stop(run)
                    raise
                sys.exit(result["exit_code"])
        if isinstance(result, bytes):
            sys.stdout.buffer.write(result)
        elif result is not None:
            print(json.dumps(result, indent=2))
    except subprocess.TimeoutExpired:
        parser.exit(1, "error: sandbox operation timed out\n")
    except KeyboardInterrupt:
        parser.exit(130, "interrupted\n")
    except (ValueError, RuntimeError, OSError, CommandError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
