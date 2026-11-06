"""Sandbox contract: one Linux VM where every role lives, plus optional leased Mac stations."""

from __future__ import annotations

import base64
import logging
import subprocess
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from .commands import VMStateError
from .egress_proxy import BASELINE_HOSTS
from .events import Events
from .file_grants import TREE
from .guest_control import (
    MAX_FILE,
    ControlConnectionError,
    lifecycle,
    private_directory,
    read_record,
    save,
)
from .host_network import hosts_file, read_hosts, write_hosts
from .identity import ensure_runtime_id, load_runtime_id
from .linux_vm import LinuxVM
from .locking import lifecycle_locked, locked
from .log_pagination import PAGE_BYTES, pages
from .mac_vm import MacVM
from .models import Config, Layout, Run, RunSpec, VMSettings, host_entries, identifier
from .resources import host_sample, vm_process

STOP_TIMEOUT = 30
SAMPLE_TIMEOUT = 10
NO_MAC = "No Mac station is configured; add [mac] to sandbox.toml"
DISCARDED = "discarded.json"
LOGGER = logging.getLogger(__name__)
# Every run and task reaches the network through its role's proxy URL. The URL carries
# the role's proxy credential, so it travels as a secret, as the activation token does.
PROXY_SECRET = "egress-proxy"
# set_allowed_hosts runs inside its caller's database write: these bound it to ~6 s.
HOSTS_LOCK_WAIT = 1.0
HOSTS_TIMEOUT = 3.0
HOSTS_RESTORE_TIMEOUT = 1.0
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
NO_PROXY = "localhost,127.0.0.1,::1"


def proxied(url, env, secrets, secret_env):
    """``env``, ``secrets`` and ``secret_env`` with the egress proxy settings added.

    ``NO_PROXY`` keeps guest loopback, where the runtime's tunnel and the shared drive
    are, off the proxy. The proxy settings replace any the caller supplied.
    """
    if PROXY_SECRET in secrets:
        raise ValueError(f"The secret name {PROXY_SECRET} is reserved for the egress proxy")
    return (
        {**env, "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY},
        {**secrets, PROXY_SECRET: url.encode()},
        {**secret_env, **dict.fromkeys(PROXY_VARIABLES, PROXY_SECRET)},
    )


class Sandbox:
    """The firm's execution infrastructure, with one optional shared ``mac`` VM."""

    def __init__(self, config: Config):
        self.config = config
        self._events = Events(config)
        private_directory(config.root)
        ensure_runtime_id(config)
        self.linux = LinuxVM(
            VMSettings(
                config.durable / "linux",
                firm_locks=config.locks,
                firm_id=config.firm_id,
                cpus=config.cpus,
                memory_gib=config.memory_gib,
                disk_gib=config.disk_gib,
                egress_mode=config.egress_mode,
                allow_hosts=config.allow_hosts,
            )
        )
        self.mac = (
            MacVM(
                VMSettings(
                    config.durable / "mac",
                    firm_locks=config.locks,
                    firm_id=config.firm_id,
                    cpus=config.mac_cpus,
                    memory_gib=config.mac_memory_gib,
                    egress_mode=config.egress_mode,
                    allow_hosts=config.allow_hosts,
                ),
                Path(config.mac_image) if config.mac_image is not None else None,
            )
            if config.mac_image is not None or (config.durable / "mac" / "vm.json").exists()
            else None
        )
        self.commands = self.linux.commands
        self._file_server = None
        self._runtime_port = None
        self._runtime_tunnel = None
        self._mac_stations = {}
        # Which station holds a lease this process granted; the records answer after a crash.
        self._lease_stations = {}
        self._force_stopped = threading.Event()
        self._launch_condition = threading.Condition()
        self._pending_launches = 0
        self._pending_boots = 0
        self._emergency_shutdowns = 0

    @property
    def root(self):
        return self.linux.root

    @property
    def firm_locks(self):
        return self.config.locks

    @property
    def file_server(self):
        if self._file_server is None:
            from .file_server import SERVER_PORT, FileServer
            from .tunnel import Tunnel

            def mac_forward(port, vm):
                return Tunnel(
                    name="files-mac",
                    host="lima-mac",
                    forward=("-R", f"127.0.0.1:{port}:127.0.0.1:{port}"),
                    ssh_config=vm.ssh_config,
                    socket_root=vm.socket_root,
                    root=vm.root,
                    locks=self.firm_locks,
                    generation=lambda: vm._state()["generation"],
                    commands=vm.commands,
                    verify=vm.paths.verify_runtime,
                )

            def forwards(port):
                # One owner forward and one reverse forward into the shared Mac.
                owner = Tunnel(
                    name="files-owner",
                    host="lima-linux",
                    forward=("-L", f"127.0.0.1:{port}:127.0.0.1:{SERVER_PORT}"),
                    ssh_config=self.linux.ssh_config,
                    socket_root=self.linux.socket_root,
                    root=self.linux.root,
                    locks=self.firm_locks,
                    generation=lambda: self.linux._state()["generation"],
                    commands=self.linux.commands,
                    verify=self.linux.paths.verify_runtime,
                )
                return (owner, mac_forward(port, self.mac)) if self.mac else (owner,)

            self._file_server = FileServer(
                self.config, lambda request: self.linux.rpc("files", **request), forwards=forwards
            )
        return self._file_server

    def _check_admission(self):
        if self._force_stopped.is_set():
            raise RuntimeError("Firm was force-stopped; start a fresh lifecycle before admission")

    def mac_station(self, station):
        """The lease surface of one station, built on first use."""
        if self.mac is None:
            raise RuntimeError(NO_MAC)
        if type(station) is not int or station not in self.mac.stations:
            raise ValueError(f"This firm has no Mac station {station}")
        if station not in self._mac_stations:
            from .mac_station import MacStation

            self._mac_stations[station] = MacStation(
                self.mac,
                station,
                revoke=lambda lease_id: self._close_lease_tunnel(lease_id, station),
            )
        return self._mac_stations[station]

    def _station_locks(self, *, skip_busy=False):
        """Hold all account locks before a whole-machine operation touches the Mac."""
        from .mac_station import station_locks

        return station_locks(
            self.firm_locks, self.mac.stations if self.mac else (), skip_busy=skip_busy
        )

    def _restates_sessions_only(self):
        """Whether ``up`` would write nothing outside the station accounts themselves.

        Booting a VM or installing its controller or boot script writes the whole machine, so
        those keep the whole-machine lock and refuse under any lease. Restating an
        account's desktop needs only that account's lock, and a station another process
        holds is its holder's to keep.
        """
        if self.mac is None:
            return False
        try:
            return (
                self.linux._running()
                and self.mac._running()
                and self.mac.controller_installed()
                and self.mac.boot_script_installed()
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return False

    def identity(self):
        return load_runtime_id(self.config)

    def configure_runtime(self, port: int):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Runtime port must be between 1 and 65535")
        if self._runtime_port not in (None, port):
            raise RuntimeError("Stop the execution host before changing its runtime port")
        self._runtime_port = port

    @lifecycle(exclusive=True)
    def up(self):
        with self._station_locks(skip_busy=self._restates_sessions_only()) as leased:
            self._require_retained()
            with self._launch_condition:
                if self._emergency_shutdowns:
                    raise RuntimeError("Emergency shutdown is still in progress")
                if self._pending_launches:
                    raise RuntimeError("Wait for pending launches before restarting the firm")
                self._pending_boots += 1
                self._force_stopped.clear()
            try:
                self._check_admission()
                result = self.linux.up()
                self._check_admission()
                if self.mac is not None:
                    self.mac.up(checkpoint=self._check_admission)
                    self._check_admission()
                    result = {**result, "mac": self._ensure_mac_sessions(leased=leased)}
                # Every generation record and SSH endpoint must exist before the
                # file server opens its host and station forwards.
                self.file_server.start()
                if self._runtime_port is not None:
                    from .tunnel import runtime_tunnel

                    self._runtime_tunnel = runtime_tunnel(self.linux, self._runtime_port)
                    self._runtime_tunnel.start()
                self._check_admission()
                self._events.write("firm.started")
                return result
            except BaseException as exc:
                for error in self._power_off_resources():
                    exc.add_note(f"Startup cleanup failed: {error}")
                raise
            finally:
                with self._launch_condition:
                    self._pending_boots -= 1
                    self._launch_condition.notify_all()

    def down(self, *, expected_runs=None, force=False):
        if force:
            return self.force_down()
        return self._down(expected_runs=expected_runs)

    def force_down(self):
        # Emergency power-off must not wait for an in-flight guest RPC lock.
        with self._launch_condition:
            self._emergency_shutdowns += 1
            self._force_stopped.set()
        try:
            return self._force_down()
        finally:
            with self._launch_condition:
                self._emergency_shutdowns -= 1
                self._launch_condition.notify_all()

    def _force_down(self):
        with self._launch_condition:
            startup_possible = bool(self._pending_launches or self._pending_boots)
        errors = self._power_off_resources()
        with self._launch_condition:
            if not self._launch_condition.wait_for(
                lambda: not self._pending_launches and not self._pending_boots, 10
            ):
                errors.append(RuntimeError("Pending VM startup has not settled after shutdown"))
        if startup_possible:
            # A boot may have crossed its start boundary before cancellation.
            # Confirm shutdown again after every setup call settles.
            errors.extend(self._power_off_resources())
        if errors:
            raise RuntimeError("Firm emergency shutdown requires attention: " + str(errors[0]))
        self._events.write("firm.stopped", forced=True)
        return {"running": False}

    def _power_off_resources(self):
        errors = []
        actions = [
            *(vm.force_down for vm in ([self.mac] if self.mac else [])),
            self.file_server.stop,
            self.linux.force_down,
            self._close_runtime_tunnel,
        ]
        for action in actions:
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        return errors

    @lifecycle(exclusive=True)
    def _down(self, *, expected_runs=None):
        with self._station_locks():
            try:
                running = self.linux._running()
            except VMStateError:
                running = False
                self.linux.force_down()
            if running and not self.linux.network.ready():
                if expected_runs is not None:
                    raise RuntimeError(
                        "Cannot verify active execution; use force-down for shutdown"
                    )
                with self._launch_condition:
                    self._force_stopped.set()
                # Losing the boundary forfeits a graceful guest shutdown, not the roles'
                # work: commit the guest's writes before cutting power, bounded so a
                # wedged guest cannot delay it.
                flushed = self.linux.flush()
                # The exclusive lock keeps queued launches outside setup; they observe
                # cancellation when it is released. Do not wait for them here.
                errors = self._power_off_resources()
                if errors:
                    raise RuntimeError("Firm shutdown requires attention: " + str(errors[0]))
                self._events.write("firm.stopped", forced=True, flushed=flushed)
                return {"running": False}
            if running:
                if expected_runs is not None:
                    expected = set(expected_runs)
                    if any(
                        run not in expected and self.status(run)["running"] for run in self.runs()
                    ):
                        raise RuntimeError(
                            "Refusing VM shutdown with unrecognized active execution"
                        )
                for run in self.runs():
                    if self.status(run)["running"]:
                        self.stop(run)
            try:
                # Guest mounts are gone with their runs; close the forwards before the
                # VM that serves them powers off.
                self.file_server.stop()
                self._close_runtime_tunnel()
                if self.mac is not None:
                    self.mac.down()
            finally:
                self.linux.down()
            self._events.write("firm.stopped", forced=False)
            return {"running": False}

    def _close_runtime_tunnel(self):
        if self._runtime_tunnel is None:
            record = self.linux.root / "runtime-mcp.json"
            if not record.exists():
                return
            from .tunnel import runtime_tunnel

            port = int(read_record(record)["forward"][1].split(":")[1])
            self._runtime_tunnel = runtime_tunnel(self.linux, port)
        self._runtime_tunnel.close()
        self._runtime_tunnel = None

    def _vms(self):
        """The firm's VMs by machine name: ``linux`` and optionally ``mac``."""
        return {"linux": self.linux, **({"mac": self.mac} if self.mac else {})}

    def _ensure_mac_sessions(self, *, leased=()):
        """Restate machine and account awake policy; report unavailable desktops.

        A leased station is left as its holder is using it: restating a desktop under a
        live lease would disturb the work the lease exists for.
        """
        self.mac.rpc("keep-awake")
        return {
            "stations": [
                self.mac_station(station).ensure_session()
                for station in self.mac.stations
                if station not in leased
            ],
            "stations_skipped": [
                {"station": station, "skipped": "leased"} for station in leased
            ],
        }

    def infrastructure_status(self):
        errors = {}

        def observe(name, vm):
            try:
                return vm._running()
            except (OSError, ValueError, RuntimeError) as exc:
                errors[name] = str(exc)
                return None

        observed = {name: observe(name, vm) for name, vm in self._vms().items()}
        if errors:
            running, state = None, "recovery_required"
        elif all(observed.values()):
            running, state = True, "running"
        elif any(observed.values()):
            running, state = None, "partial"
        elif (self.config.durable / DISCARDED).exists():
            running, state = False, "discarded"
        else:
            running, state = False, "stopped"
        return {
            "firm_id": self.config.firm_id,
            "running": running,
            "state": state,
            "errors": errors,
            "network_ready": self.linux.network.ready() if observed["linux"] else False,
        }

    def state(self):
        return self.infrastructure_status()["state"]

    def data_status(self):
        """Where the shared trees live and how the owner mounts them."""
        return {"storage": "linux-vm", **self.file_server.owner_access()}

    def network_status(self):
        """The firm's egress: ``enabled``, the owner's cutoff (true only while every VM is
        on); ``mode``, enforce or observe; ``allowed_hosts``, the effective allowlist with
        each entry's source; ``egress``, every proxy's requests by role and host (``vm``
        names the machine; the Mac's are all role ``mac``), ``live`` only while every proxy
        answered, else from the last saved tallies. ``vms`` keeps each machine's own switch,
        proxy summary and packet-filter ``traffic``; a record neither live nor saved reads
        ``available: false``.
        """

        def observe(read):
            try:
                return read()
            except RuntimeError:
                return {"live": False, "available": False}

        vms = {}
        for name, vm in self._vms().items():
            traffic = observe(vm.network.traffic)
            enabled = traffic.get("egress") if traffic.get("live") else None
            vms[name] = {
                "enabled": vm.network.saved_egress() if enabled is None else enabled,
                "egress": observe(vm.network.egress_summary),
                "traffic": traffic,
            }
        return {
            "enabled": all(row["enabled"] for row in vms.values()),
            "mode": self.config.egress_mode,
            "allowed_hosts": self.allowed_hosts(),
            "egress": {
                "live": all(row["egress"]["live"] for row in vms.values()),
                "entries": [
                    {**entry, "vm": name}
                    for name, row in vms.items() for entry in row["egress"].get("entries", ())
                ],
                "overflow": sum(row["egress"].get("overflow", 0) for row in vms.values()),
            },
            "vms": vms,
        }

    def granted_hosts(self):
        """The owner's run-time grants, as the Linux VM's ``hosts.json`` holds them."""
        return list(read_hosts(hosts_file(self.linux.root)))

    def allowed_hosts(self):
        """The effective allowlist, each entry once with the first source that holds it:
        ``baseline`` (shipped), ``config`` (``[network] allow_hosts``) or ``granted``."""
        rows, seen = [], set()
        for source, entries in (
            ("baseline", BASELINE_HOSTS),
            ("config", self.config.allow_hosts),
            ("granted", self.granted_hosts()),
        ):
            for host in entries:
                if host not in seen:
                    seen.add(host)
                    rows.append({"host": host, "source": source})
        return rows

    @lifecycle(timeout=HOSTS_LOCK_WAIT)
    def set_allowed_hosts(self, hosts):
        """Replace the granted hosts on every VM; a running proxy reloads them at once.

        Entries use the allowlist syntax (``name.example`` or ``*.example.com``). A stopped
        VM's proxy reads its ``hosts.json`` when it starts. The change is all or nothing:
        if any VM fails to take it, every VM's previous file is put back and its proxy
        asked to reload (best effort), and the event records ``applied: false``.

        Callers hold a database write while this runs, so it is bounded to a few seconds.
        It takes the shared lifecycle lock: runs and observations go on beside it, while
        ``up``, ``down``, ``replace``, ``discard`` and the cutoff, which hold the lock
        exclusively, never overlap it. Behind one of those it waits ``HOSTS_LOCK_WAIT``
        seconds and is then refused (``LockBusy``) with nothing changed. A filter that does
        not confirm within ``HOSTS_TIMEOUT`` in all fails the call and triggers the restore.
        """
        hosts = host_entries(hosts)
        # Two callers (the runtime and the owner's CLI) share the lifecycle lock; this one
        # orders their read-write-restore of the files.
        with locked(self.firm_locks, "hosts", timeout=HOSTS_LOCK_WAIT):
            return self._set_allowed_hosts(hosts)

    def _set_allowed_hosts(self, hosts):
        vms = self._vms()
        previous = {}
        for name, vm in vms.items():
            try:
                previous[name] = hosts_file(vm.root).read_bytes()
            except FileNotFoundError:
                previous[name] = None
        deadline = time.monotonic() + HOSTS_TIMEOUT
        failures = []
        for name, vm in vms.items():
            try:
                write_hosts(hosts_file(vm.root), hosts)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("No time was left to confirm the change")
                vm.network.reload_hosts(timeout=remaining)
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(f"{name}: {exc}")
                break
        unrestored = [
            name for name, vm in vms.items()
            if failures and not self._restore_hosts(vm, previous[name])
        ]
        try:
            # A record of what happened, not part of it: the result reflects the files.
            self._events.write(
                "network.hosts_changed", hosts=list(hosts), failures=failures,
                applied=not failures,
            )
        except OSError as exc:
            LOGGER.warning("The hosts change was not recorded in the event log: %s", exc)
        if failures:
            outcome = (
                f"the previous hosts file could not be put back on {', '.join(unrestored)}"
                if unrestored else "the previous hosts files were restored"
            )
            raise RuntimeError(
                "Allowed hosts change incomplete: " + "; ".join(failures) + f"; {outcome}"
            )
        return {"hosts": list(hosts)}

    @staticmethod
    def _restore_hosts(vm, content) -> bool:
        """Put a VM's previous granted hosts file back; True once the file is back.

        The file is what the proxy enforces (it re-reads it on change), so a proxy that
        does not confirm the reload in time is logged, not a failed restore.
        """
        path = hosts_file(vm.root)
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                temporary = path.with_name(f".{path.name}.restore")
                temporary.write_bytes(content)
                temporary.chmod(0o600)
                temporary.replace(path)
        except OSError as exc:
            LOGGER.warning("Granted hosts file was not restored on a VM: %s", exc)
            return False
        try:
            vm.network.reload_hosts(timeout=HOSTS_RESTORE_TIMEOUT)
        except (OSError, ValueError, RuntimeError) as exc:
            LOGGER.warning("A restored hosts file was not confirmed by its proxy: %s", exc)
        return True

    @lifecycle(exclusive=True)
    def set_network_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Egress control requires a boolean")
        results, failures = {}, []

        def change(name, vm):
            try:
                results[name] = (
                    vm.network.set_egress(enabled)
                    if vm._running()
                    else vm.network.prepare_egress(enabled)
                )
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(f"{name}: {exc}")

        for name, vm in self._vms().items():
            change(name, vm)
        self._events.write("network.egress_changed", enabled=enabled, failures=failures)
        if failures:
            raise RuntimeError("Network change incomplete: " + "; ".join(failures))
        return results

    def host_address(self):
        # Reverse SSH forwards only this selected service from guest loopback.
        return "127.0.0.1"

    def layout(self, role_id):
        return Layout.for_role(role_id)

    def child_layout(self, role_id, run_id):
        role = self.layout(role_id)
        root = f"/var/lib/firm/task-homes/{identifier(run_id)}"
        return replace(role, private=f"{root}/home", state=f"{root}/state",
                       credentials=f"/run/firm/{identifier(role_id)}/tasks/{run_id}/credentials")

    # ---- the Mac lease --------------------------------------------------------------

    def _lease_tunnel(self, lease_id, station=None):
        """The holder reaches the station's sshd at ``127.0.0.1:<lease port>`` on Linux.

        Without a station the forward is the one the tunnel's own record holds, which
        is all that closing it needs once the Mac it led to may be gone.
        """
        from .mac_station import lease_identifier, lease_ssh_port
        from .tunnel import Tunnel

        name = f"mac-lease-{lease_identifier(lease_id)}"
        if station is None:
            forward = read_record(self.linux.root / f"{name}.json")["forward"]
        else:
            port = lease_ssh_port(station)
            forward = ("-R", f"127.0.0.1:{port}:127.0.0.1:{self.mac.ssh_port()}")
        return Tunnel(
            name=name,
            host="lima-linux",
            forward=forward,
            ssh_config=self.linux.ssh_config,
            socket_root=self.linux.socket_root,
            root=self.linux.root,
            locks=self.firm_locks,
            generation=lambda: self.linux._state()["generation"],
            commands=self.linux.commands,
            verify=self.linux.paths.verify_runtime,
        )

    def _close_lease_tunnel(self, lease_id, station):
        """Close the lease's tunnel and verify its control socket is gone."""
        return self._close_tunnel(self._lease_tunnel(lease_id, station))

    def _close_recorded_lease_tunnel(self, lease_id):
        """Close a lease's tunnel by its own record; none was started if it has none."""
        from .mac_station import lease_identifier

        if (self.linux.root / f"mac-lease-{lease_identifier(lease_id)}.json").exists():
            self._close_tunnel(self._lease_tunnel(lease_id))

    @staticmethod
    def _close_tunnel(tunnel):
        tunnel.close()
        closed = not tunnel.socket.exists()
        if closed:
            tunnel.record.unlink(missing_ok=True)
        return {"tunnel_closed": closed}

    @lifecycle()
    def mac_acquire(self, role_id, activation_id, *, station=None):
        """Lease one free station to an activation; the key reaches only the role's run.

        Stations are tried in order, and a busy, unrecovered or full one moves the
        caller on to the next; only when none grants does one refusal reach the
        caller. All stations share the one image and machine. With ``station``, only
        that one is tried: the owner's checks address a station by number, roles never
        do (the runtime's lease tool passes none).

        The runtime never sees key material: the private half is written through the
        Linux guest controller as the role, create-only and 0600, under the role's
        credentials directory, and the returned ``key_path`` names it.
        """
        from .mac_station import (
            HelperNotReady,
            StationBusy,
            StationDiskFull,
            StationNeedsRecovery,
            StationNotEnrolled,
            StationSessionNotReady,
            generate_key_pair,
        )

        identifier(role_id)
        if self.mac is None:
            return {"status": "unavailable", "reason": "no_mac"}
        if not self.mac.stations:
            raise StationNotEnrolled("The Mac has no station accounts; run mac-prepare")
        if station is not None and station not in self.mac.stations:
            raise ValueError(f"This Mac has stations {self.mac.stations}, not station {station}")
        stations = self.mac.stations if station is None else [station]
        self.file_server.require_ready()
        drive = self.file_server.credentials(role_id)
        address = self.file_server.address(role_id)
        # A refused station writes no record, so one identity and key pair serve
        # whichever station grants the lease.
        lease_id = uuid4().hex
        private_key, public_key = generate_key_pair()
        busy = needs_recovery = disk_full = not_ready = None
        # Every station found too full on the way, whatever this acquire comes to: the
        # reply or the refusal carries them, so none is lost to the one that was granted
        # or to the refusal that outranks it.
        disk_refusals = []
        try:
            for station in stations:
                try:
                    lease, disk, disk_reclaimed = self.mac_station(station).acquire(
                        lease_id,
                        role_id,
                        drive,
                        public_key,
                        address=address,
                        activation_id=activation_id,
                    )
                except (StationSessionNotReady, HelperNotReady) as exc:
                    not_ready = exc
                    continue
                except StationBusy as exc:
                    busy = exc
                    continue
                except StationNeedsRecovery as exc:
                    needs_recovery = exc
                    continue
                except StationDiskFull as exc:
                    disk_full = exc
                    disk_refusals.append(exc.evidence())
                    self._events.write("mac.refused", role_id=role_id, **exc.evidence())
                    continue
                try:
                    self._lease_tunnel(lease_id, station).start()
                    written = self.linux._rpc(
                        "write-credential",
                        role_id=role_id,
                        name=f"mac-{lease_id}",
                        data=base64.b64encode(private_key).decode(),
                    )
                except BaseException:
                    # The lease is unusable without its endpoint and key; a failed release
                    # keeps the marker.
                    self.mac_station(station).release(lease_id)
                    raise
                self._lease_stations[lease_id] = station
                self._events.write(
                    "mac.acquired", lease_id=lease_id, role_id=role_id, station=station
                )
                return {
                    **asdict(lease), "key_path": written["path"], "disk": disk,
                    "disk_reclaimed": disk_reclaimed, "disk_refusals": disk_refusals,
                }
            # Busy may free itself; needing recovery already asks the owner to look; a
            # full station already reclaimed and is freshest evidence of the three, so it
            # is reported only once neither of the other two explains the refusal.
            raise busy or needs_recovery or not_ready or disk_full
        except BaseException as exc:
            # Whatever ends this acquire, a refusal or a lost connection to a later
            # station, the full stations found on the way travel with it.
            exc.disk_refusals = disk_refusals
            raise

    @lifecycle()
    def mac_release(self, lease_id):
        station = self._lease_station(lease_id)
        if station is None:
            return {"status": "ok", "lease_id": lease_id, "gone": True}
        result = self.mac_station(station).release(lease_id)
        self._lease_stations.pop(lease_id, None)
        if not result.get("gone"):
            # A clean release forgets its record, so what reclaim left and which
            # simulator step timed out are kept here for the owner.
            self._events.write(
                "mac.released", lease_id=lease_id, clean=result["clean"], station=station,
                disk=result["disk"], reclaim=result["reclaim"], simulators=result["simulators"],
            )
        return result

    def _mac_stopped(self):
        """Whether the Mac is known to be powered off; an unreadable state is not an answer."""
        try:
            return not self.mac._running()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return False

    def mac_status(self):
        """``configured``: a Mac is retained or configured; ``enrolled``: its image is sealed.

        ``stations`` carries one status per station, in station order. On a stopped Mac
        each station reports its ``helper`` and ``awake`` as stopped: nothing is asked of
        a helper or a guest that is not running, so silence that only means the VM is off
        is never reported as a relay or network fault.
        """
        stations = (
            [self.mac_station(station).status() for station in self.mac.stations]
            if self.mac else []
        )
        if stations and self._mac_stopped():
            stations = [
                {**row, "helper": {"status": "stopped"}, "awake": {"status": "stopped"}}
                for row in stations
            ]
        return {
            "configured": self.mac is not None,
            "enrolled": bool(stations) and all(row["enrolled"] for row in stations),
            "stations": stations,
        }

    def mac_connect(self, lease_id):
        station = self._lease_station(lease_id)
        if station is None:
            raise OSError("The Mac lease is not held")
        return self.mac_station(station).connect(lease_id)

    def _lease_station(self, lease_id):
        """The station a lease belongs to, or ``None`` when no station knows it.

        This process remembers what it granted; after a crash the lease record left on
        a station answers instead.
        """
        if self.mac is None:
            raise RuntimeError(NO_MAC)
        if lease_id in self._lease_stations:
            return self._lease_stations[lease_id]
        return next(
            (
                station
                for station in self.mac.stations
                if self.mac_station(station).holds(lease_id)
            ),
            None,
        )

    # ---- runs ----------------------------------------------------------------------

    def start(self, spec: RunSpec):
        with self._launch_condition:
            self._check_admission()
            self._pending_launches += 1
        try:
            return self._start(spec)
        finally:
            with self._launch_condition:
                self._pending_launches -= 1
                self._launch_condition.notify_all()

    @lifecycle()
    def _start(self, spec: RunSpec):
        self._check_admission()
        if self._runtime_tunnel is not None:
            self._runtime_tunnel.require_ready()
        if spec.run_id is None:
            spec = replace(spec, run_id="run-" + uuid4().hex[:16])
        drive = None
        if spec.organization is not None:
            # The role mounts the shared trees with its own credentials; a run
            # without the drive is a launch failure, not a degraded run.
            self.file_server.require_ready()
            drive = self.file_server.credentials(spec.role_id).request()
        self._check_admission()
        run = self._admit(spec, drive)
        self._events.write("run.started", run)
        return run

    @lifecycle()
    def start_child(self, parent: Run, *, run_id: str, argv, seconds_remaining: float,
                    env=None, secrets=None, secret_env=None, stdin=b""):
        """Launch only inside this live parent; the guest derives all identity/state.

        A task gets its parent role's egress proxy settings, as every run does.
        """
        self.linux._check_run(parent)
        self._check_admission()
        env, secrets, secret_env = proxied(
            self.linux.network.proxy_url(parent.role_id), env or {}, secrets or {},
            secret_env or {},
        )
        return self.linux.rpc(
            "start-child", parent_run_id=parent.run_id, run_id=identifier(run_id),
            argv=list(argv), seconds_remaining=seconds_remaining, env=env,
            secrets={key: base64.b64encode(value).decode() for key, value in secrets.items()},
            secret_env=secret_env, stdin=base64.b64encode(stdin).decode(),
        )

    @lifecycle()
    def child_status(self, run_id: str):
        return self.linux.rpc("child-status", run_id=identifier(run_id))

    @lifecycle()
    def child_logs(self, run_id: str, *, offset=0, limit=1024 * 1024):
        result = self.linux.rpc(
            "child-logs", run_id=identifier(run_id), offset=offset, limit=limit,
        )
        return base64.b64decode(result["data"], validate=True)

    @lifecycle()
    def stop_child(self, run_id: str):
        # Also fences a launch whose reply was lost, or which has not arrived yet.
        return self.linux.rpc("stop-child", run_id=identifier(run_id), timeout=60)

    @lifecycle()
    def stop(self, run: Run, timeout=STOP_TIMEOUT):
        result = self._stop_run(run, timeout=timeout)
        self._events.write("run.stopped", run, exit_code=result.get("exit_code"))
        return result

    @lifecycle()
    def remove(self, run):
        self.linux._check_run(run)
        state = self.linux.rpc("status", run_id=run.run_id)
        if state.get("running") or state.get("unresolved"):
            raise ValueError("Stop and resolve a run before removing it")
        self.linux.rpc("remove", run_id=run.run_id)

    @lifecycle()
    def retire(self, role_id):
        role_id = identifier(role_id)
        if any(
            row["role_id"] == role_id and (row.get("running") or row.get("unresolved"))
            for row in self.states()
        ):
            raise ValueError("Stop and resolve a role's runs before retirement")
        self.linux.rpc("retire", role_id=role_id)
        self.file_server.retire_role(role_id)

    @lifecycle()
    def status(self, run, *, timeout=120):
        self.linux._check_run(run)
        result = self.linux.rpc("status", run_id=run.run_id, timeout=timeout)
        return {**result, "observation": "guest-controller"}

    @lifecycle()
    def runs(self):
        generation = self.linux._state()["generation"]
        return [Run(row["role_id"], row["run_id"], generation) for row in self.states()]

    @lifecycle()
    def inventory(self):
        """Every run's identity and status from the guest's one ``runs`` reply.

        ``status()`` tags a single guest status with ``"observation": "guest-controller"``;
        do the same here so a caller sees one shape regardless of which call produced it.
        """
        generation = self.linux._state()["generation"]
        return {
            row["run_id"]: (
                Run(row["role_id"], row["run_id"], generation),
                {**row, "observation": "guest-controller"},
            )
            for row in self.states()
        }

    @lifecycle()
    def states(self):
        return self.linux.rpc("runs")

    def current(self, role_id):
        identifier(role_id)
        runs = [run for run in self.runs() if run.role_id == role_id]
        if not runs:
            raise ValueError(f"No invocation exists for role {role_id}")
        states = [(run, self.status(run)) for run in runs]
        live = [run for run, state in states if state["running"]]
        if len(live) > 1:
            raise RuntimeError("Multiple live runs exist for one role")
        if live:
            return live[0]
        return max(
            states,
            key=lambda item: (
                item[1].get("ended") or item[1].get("started") or 0,
                item[0].run_id,
            ),
        )[0]

    def wait(self, run, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else 120
            state = self.status(run, timeout=remaining)
            if not state["running"]:
                return state
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Role is still running")
            time.sleep(
                min(0.2, max(0, deadline - time.monotonic())) if deadline is not None else 0.2
            )

    @lifecycle()
    def logs(self, run, *, offset=0, until=None):
        # The guest journal already bounds the log; read every stored chunk rather
        # than truncating structured CLI output at one MiB. A caller that already
        # knows how much it has read passes that offset to fetch only the rest, and
        # one that knows the log's size passes it to skip the empty page past the end.
        return b"".join(pages(self._log_chunk, run, offset, until))

    def follow(self, run):
        offset = 0
        while True:
            chunk = self._log_chunk(run, offset=offset)
            if chunk:
                offset += len(chunk)
                yield chunk
            elif not self.status(run)["running"]:
                # The process can write its last output and exit between the empty
                # read and the status reply (the guest publishes exit only after the
                # log is closed), so read the settled log to its end before returning.
                while chunk := self._log_chunk(run, offset=offset):
                    offset += len(chunk)
                    yield chunk
                return
            else:
                time.sleep(0.2)

    def resources(self):
        """One machine-wide sample per running VM, plus the host's own counters.

        The VM samples are read without the guest's global lock, so this never waits
        for a provisioning call; a stopped, unready or unreachable VM is reported as
        unknown instead of failing the sample. The host measures each VM process's
        footprint and CPU time, which the guest cannot see.
        """

        def observe(vm):
            try:
                if not vm._state().get("ready") or not vm._running():
                    return None
                return {**vm.rpc("sample", timeout=SAMPLE_TIMEOUT), **vm_process(vm)}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                LOGGER.warning("Resource sample unavailable (%s)", type(exc).__name__)
                return None

        return {"host": host_sample(), **{name: observe(vm) for name, vm in self._vms().items()}}

    def events(self):
        return self._events.read()

    # ---- export, replacement and discarding -----------------------------------------

    def export(self, destination: Path, *, homes=False, boot=False):
        """Unpack the shared trees under ``destination/drive``, and with ``homes`` each
        role's work and provider sessions under ``destination/homes/<role>``.

        With ``boot``, a stopped Linux VM boots for the copy, without the file server or
        the Mac VM, and stops again; the caller guarantees no execution host starts
        meanwhile. Without it the Linux VM must already be running. Returns the member
        names the host's extraction filter refused.
        """
        self._require_retained()
        if not boot:
            if not self.linux._running():
                raise RuntimeError("The Linux VM is stopped, and this export may not boot it")
            return self.linux.export(destination, root=TREE, homes=homes)
        with lifecycle_locked(self.firm_locks, exclusive=True):
            started = not self.linux._running()
            if started:
                self.linux.up()
            try:
                return self.linux.export(destination, root=TREE, homes=homes)
            finally:
                if started:
                    self.linux.down()

    def _require_retained(self):
        if (self.config.durable / DISCARDED).exists():
            raise RuntimeError(
                "This firm's VMs were discarded; its host records remain, but it cannot boot"
            )

    @lifecycle(exclusive=True)
    def mac_restart(self):
        """Restart the shared Mac and restore account sessions, keeping Linux running."""
        self._require_retained()
        if self.mac is None:
            raise ValueError("This firm has no Mac")
        with self._station_locks():
            self.mac.down()
            result = self.mac.up()
            self.file_server.reconnect()
            return {**result, **self._ensure_mac_sessions()}

    @lifecycle()
    def mac_session_reset(self, station):
        """Reset only the named account's desktop and attest retained lease cleanup."""
        self._require_retained()
        return self.mac_station(station).session_reset()

    @lifecycle(exclusive=True)
    def replace(self):
        """Discard both guest disks and account state, then clone the configured image."""
        self._require_retained()
        with self._station_locks():
            stations = [self.mac_station(n) for n in self.mac.stations] if self.mac else []
            if self.mac is not None:
                image = Path(self.config.mac_image).resolve()
                self.mac._image_base(image)
            self.down()
            if self.mac is not None:
                self.mac.remove()
                for station in stations:
                    station.discard()
                self.mac.image = image
                self._mac_stations.clear()
            self._discard_linux()
            return self.up()

    @lifecycle(exclusive=True)
    def mac_replace(self):
        """Remove the stopped Mac for a fresh clone of the configured image on next up.

        Linux and its disk are untouched. Account records are discarded only after
        the shared disk is gone; interrupted removal can be repeated.
        """
        self._require_retained()
        if self.mac is None:
            raise ValueError("This firm has no Mac")
        state = self.state()
        if state != "stopped":
            raise RuntimeError(f"Stop the firm before adopting a new Mac image; state is {state}")
        with self._station_locks():
            image = Path(self.config.mac_image).resolve()
            self.mac._image_base(image)
            stations = [self.mac_station(n) for n in self.mac.stations]
            for station in stations:
                for record_path in station._records():
                    self._close_recorded_lease_tunnel(read_record(record_path)["lease_id"])
            self.mac.remove()
            for station in stations:
                station.discard()
            self.mac.image = image
            self._mac_stations.clear()
            return {"replaced": True}

    @lifecycle(exclusive=True)
    def discard(self):
        """Discard every VM and guest disk for good, keeping the host records.

        Unlike replacement nothing boots again. The marker is written before any disk
        goes, so a repeated call finishes an interrupted discard, and from then on ``up``,
        ``replace`` and ``export`` refuse the firm. The Mac lease records stay with the
        firm's other host records.
        """
        state = self.state()
        if state not in {"stopped", "discarded"}:
            raise RuntimeError(f"Stop the firm's VMs before discarding them; state is {state}")
        with self._station_locks():
            save(self.config.durable / DISCARDED, {"discarded_at": time.time()})
            if self.mac is not None:
                self.mac.remove()
            self._discard_linux()
        self._events.write("firm.discarded")
        return {"discarded": True}

    def _discard_linux(self):
        # Role identities and the shared trees must not outlive the accounts they
        # were provisioned for; a fresh VM reuses role names and UIDs.
        self.linux.remove()
        self.file_server.discard()

    def _admit(self, spec: RunSpec, drive=None):
        run_id = spec.run_id or "run-" + uuid4().hex[:16]
        if not self.linux._state()["ready"]:
            raise RuntimeError("Call up() before starting roles")
        env, secrets, secret_env = proxied(
            self.linux.network.proxy_url(spec.role_id), spec.env, spec.secrets, spec.secret_env
        )
        payload = dict(
            role_id=spec.role_id,
            run_id=run_id,
            argv=list(spec.argv),
            env=env,
            stdin=base64.b64encode(spec.stdin).decode(),
            organization=spec.organization.address if spec.organization else None,
            drive=drive,
            secrets={key: base64.b64encode(value).decode() for key, value in secrets.items()},
            secret_env=secret_env,
        )
        try:
            self.linux._rpc("start", **payload)
        except (TimeoutError, OSError, ControlConnectionError):
            # A lost reply is not a failed launch. The guest reconciles the same
            # immutable request by run ID, without executing it twice.
            try:
                self.linux._rpc("start", **payload)
            except Exception as exc:
                raise ControlConnectionError(
                    f"Launch outcome unknown for {run_id}; recover with ps/stop or down"
                ) from exc
        return Run(spec.role_id, run_id, self.linux._state()["generation"])

    def _stop_run(self, run, timeout):
        """Terminate immediately; timeout bounds the guest acknowledgement and exit wait."""
        if not isinstance(timeout, (int, float)) or not 0 <= timeout < float("inf"):
            raise ValueError("Stop timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        self.linux._check_run(run)
        result = self.linux.rpc("stop", run_id=run.run_id, timeout=timeout)
        while result.get("running"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Role is still running")
            result = self.linux.rpc("status", run_id=run.run_id, timeout=remaining)
            if result.get("running"):
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if result.get("unresolved"):
            raise RuntimeError("Role cleanup remains unresolved; restart or replace the VM")
        return result

    @lifecycle()
    def _log_chunk(self, run, *, offset=0, limit=PAGE_BYTES):
        self.linux._check_run(run)
        if offset < 0 or not 0 <= limit <= MAX_FILE:
            raise ValueError("Invalid log range")
        result = self.linux.rpc("logs", run_id=run.run_id, offset=offset, limit=limit)
        return base64.b64decode(result["data"], validate=True)
