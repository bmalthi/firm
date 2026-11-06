"""The shared Mac VM: clone, boot, shutdown and guest transport.

Station accounts and lease records share this machine; lease ownership belongs to
MacStation and lifecycle coordination to Sandbox.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import yaml

from .commands import (
    CommandError,
    Commands,
    VMStateError,
    lima_status,
    mac_sudo_command,
    parse_lima_list,
    resize_instance,
)
from .compatibility import validate_image_manifest, validate_retained_vm
from .guest_control import GuestControl, lifecycle, private_directory, read_record, save
from .host_network import HostNetwork
from .models import VMSettings

INSTANCE = "mac"
CONTROL_USER = "firmvm"
CONTROLLER = "/usr/local/libexec/firm-controller.py"
PYTHON = "/usr/local/firm/bin/python3"
CONTROLLER_COMMAND = (PYTHON, "-I", CONTROLLER)
BOOT_SCRIPT = "/usr/local/sbin/lima-macos-init.sh"
BOOT_AGENT = "/Volumes/cidata/lima-guestagent"
BOOT_DONE = "/var/run/lima-boot-done"
BOOT_POLL = 2
BOOT_GRACE = 10
logger = logging.getLogger(__name__)


class MacVM(GuestControl):
    """One retained macOS VM holding the firm's station accounts."""

    guest_instance = INSTANCE
    guest_controller_command = CONTROLLER_COMMAND

    def __init__(
        self,
        settings: VMSettings,
        image: Path | None = None,
        *,
        subnet="192.168.5.0/24",
    ):
        if settings.root.is_symlink():
            raise ValueError("VM root must not be a symlink")
        self.settings = settings
        self.root = settings.root
        self.firm_locks = settings.firm_locks
        from .lima_paths import lima_paths

        self.paths = lima_paths(self.root, instance=INSTANCE)
        self.home = self.root / "lima"
        self.instance = self.home / INSTANCE
        self.ssh_config = self.paths.ssh_config
        self.socket_root = self.paths.runtime
        self.record = self.root / "vm.json"
        self.image = image.resolve() if image is not None else None
        self.lima = shutil.which("limactl")
        if not self.lima:
            raise RuntimeError("Lima is not installed")
        env = {**os.environ, "LIMA_HOME": str(self.paths.home), "FIRM_REAL_LIMACTL": self.lima}
        for key in tuple(env):
            if "proxy" in key.lower():
                env.pop(key)
        self.commands = Commands(env)
        # Station sessions share one machine and one system proxy setting, so the proxy
        # attributes every Mac request to the Mac rather than asking for credentials.
        self.network = HostNetwork(
            settings,
            self.commands,
            socket_root=self.socket_root,
            subnet=subnet,
            verify=self.paths.verify_runtime,
            proxy_role="mac",
        )

    def _lima(self, *args, **kwargs):
        # Every Lima command acts on whatever VM LIMA_HOME, the runtime, holds.
        self.paths.verify_runtime()
        return self.commands.run([self.lima, *args], **kwargs)

    def _state(self):
        return read_record(self.record)

    def _running(self):
        if not self.instance.exists():
            return False
        if self.paths.projected and not self.paths.instance.exists():
            if self.paths.has_live_processes():
                raise RuntimeError("VM runtime projection is missing while a host process is alive")
            return False
        result = self._lima("list", "--json", INSTANCE)
        rows = parse_lima_list(result.stdout)
        if len(rows) != 1 or rows[0].get("name") != INSTANCE:
            raise RuntimeError("Could not determine VM state")
        status = lima_status(rows[0])
        if status not in ("running", "stopped"):
            raise VMStateError(status)
        return status == "running"

    def ssh_port(self):
        """The one guest sshd endpoint targeted by every station lease tunnel."""
        return self.paths.ssh_port()

    def _apply_resources(self, state):
        """Give the existing guest the configured CPU and memory before it boots.

        The disk, its accounts and the rest of the boot policy are untouched, so a
        firm grows or shrinks its Mac by editing `[mac]` and restarting, rather than
        replacing a VM whose contents are the work itself.
        """
        if self._running():
            raise VMStateError("Running")
        resize_instance(self.instance, self.settings.cpus, self.settings.memory_gib)
        state = {
            **state,
            "cpus": self.settings.cpus,
            "memory_gib": self.settings.memory_gib,
        }
        save(self.record, state)
        return state

    @staticmethod
    def _image_stations(image):
        enrollment = read_record(image / "image.json").get("enrollment")
        if enrollment is None:
            return 0
        count = enrollment.get("stations")
        if type(count) is not int or not 1 <= count <= 8:
            raise ValueError("Image enrollment.stations must be between 1 and 8")
        return count

    @property
    def stations(self):
        """Account numbers recorded at clone, or the image inventory before cloning."""
        count = (
            self._state()["stations"]
            if self.record.exists()
            else self._image_stations(self.image)
            if self.image is not None
            else 0
        )
        if type(count) is not int or not 0 <= count <= 8:
            raise ValueError("VM stations must be between 0 and 8")
        return tuple(range(1, count + 1))

    def enrolled(self):
        """Whether this VM was cloned from an image with sealed account enrollment."""
        if self.record.exists():
            return self._state()["enrolled"]
        return self.image is not None and bool(self._image_stations(self.image))

    def _rpc(self, operation, *, timeout=120, **payload):
        """Retry guest lock contention within this call's original transport budget."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Mac guest operation deadline exceeded")
            try:
                return super()._rpc(operation, timeout=remaining, **payload)
            except RuntimeError as exc:
                if str(exc) != "The Mac is busy with another operation; try again":
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Mac guest operation deadline exceeded") from exc
                time.sleep(min(0.25, remaining))

    def _validate_config(self):
        content = (self.instance / "lima.yaml").read_bytes()
        cfg = yaml.safe_load(content)
        if (
            cfg.get("os") != "Darwin"
            or cfg.get("vmType") != "vz"
            or cfg.get("networks") != [{"lima": "user-v2"}]
            or cfg.get("mounts")
            or cfg.get("additionalDisks")
            or cfg.get("plain") is not True
            or cfg.get("propagateProxyEnv") is not False
        ):
            raise ValueError(
                "VM configuration does not match its isolated network/storage contract"
            )
        ssh = cfg.get("ssh", {})
        if any(
            ssh.get(k) is not False
            for k in ("loadDotSSHPubKeys", "forwardAgent", "forwardX11", "forwardX11Trusted")
        ):
            raise ValueError("VM SSH forwarding or owner-key loading is not disabled")

    def _image_base(self, image):
        private_directory(image)
        metadata = validate_image_manifest(read_record(image / "image.json"))
        self._image_stations(image)
        if metadata.get("control_user") != CONTROL_USER:
            raise ValueError("Unsupported macOS VM image")
        base = image / "lima" / "base"
        private_directory(image / "lima")
        private_directory(base)
        source_commands = Commands({**self.commands.env, "LIMA_HOME": str(image / "lima")})
        listing = source_commands.run([self.lima, "list", "--json", "base"])
        rows = parse_lima_list(listing.stdout)
        if len(rows) != 1 or rows[0].get("status") != "Stopped":
            raise ValueError("Base image must be stopped before cloning")
        return base

    def _clone(self, image):
        metadata = validate_image_manifest(read_record(image / "image.json"))
        count = self._image_stations(image)
        base = self._image_base(image)
        private_directory(self.home)
        self.instance.mkdir(mode=0o700)
        for name in ("disk", "disk.patched", "vz-aux", "vz-hwmodel", "lima-version"):
            source = base / name
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError(f"Image asset is not a regular file: {name}")
            self.commands.run(
                ["/bin/cp", "-c", str(source), str(self.instance / name)], timeout=600
            )
        # Empty identity triggers Lima's supported fresh VZMacMachineIdentifier.
        (self.instance / "vz-identifier").touch()
        key_dir = self.home / "_config"
        private_directory(key_dir)
        source_key_dir = image / "lima" / "_config"
        key_info = source_key_dir.lstat()
        if (
            not stat.S_ISDIR(key_info.st_mode)
            or key_info.st_uid != os.getuid()
            or key_info.st_mode & 0o022
        ):
            raise ValueError("Image control key directory is not owner-controlled")
        for name in ("user", "user.pub"):
            source = image / "lima" / "_config" / name
            info = source.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or (key_dir / name).is_symlink()
            ):
                raise ValueError("Image control keys must be owner-controlled regular files")
            shutil.copyfile(source, key_dir / name)
            (key_dir / name).chmod(0o600)
        cfg = yaml.safe_load((base / "lima.yaml").read_text())
        # Construct the complete boot policy; do not inherit provisioning hooks,
        # exports, arbitrary sockets, or user-provided NICs from the template.
        cfg = {k: cfg[k] for k in ("os", "arch", "vmType", "images", "disk", "user")}
        cfg.update(
            cpus=self.settings.cpus,
            memory=f"{self.settings.memory_gib}GiB",
            mounts=[],
            plain=True,
            propagateProxyEnv=False,
            networks=[{"lima": "user-v2"}],
            hostResolver={"enabled": False},
            video={"display": "default"},
            audio={"device": "none"},
            ssh={
                "loadDotSSHPubKeys": False,
                "forwardAgent": False,
                "forwardX11": False,
                "forwardX11Trusted": False,
            },
        )
        (self.instance / "lima.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        save(
            self.record,
            {
                "firm_id": self.settings.firm_id,
                "image": str(image),
                "created_with_release": metadata["created_with_release"],
                "compatibility_generation": metadata["compatibility_generation"],
                "image_id": metadata["image_id"],
                "stations": count,
                "enrolled": bool(count),
                "generation": uuid.uuid4().hex,
                "cpus": self.settings.cpus,
                "memory_gib": self.settings.memory_gib,
                "ready": False,
            },
        )

    def _shell(self, *args, **kwargs):
        return self._lima("shell", "--tty=false", "--workdir=/", INSTANCE, *args, **kwargs)

    def boot_script_installed(self):
        """Whether up can leave the machine's boot script untouched."""
        return self._boot_script_intact(self._boot_script_source())

    @staticmethod
    def _boot_script_source():
        return Path(__file__).with_name("assets").joinpath("lima-macos-init.sh").read_bytes()

    def _boot_script_intact(self, content):
        digest = hashlib.sha256(content).hexdigest()
        probe = self._shell(
            "/bin/sh",
            "-c",
            f'/usr/bin/stat -f "%Su:%Sg:%Lp" {BOOT_SCRIPT} && /usr/bin/shasum -a 256 {BOOT_SCRIPT}',
            check=False,
            timeout=30,
        )
        installed = probe.stdout.decode(errors="replace").split()
        return probe.returncode == 0 and installed[:2] == ["root:wheel:755", digest]

    def _ensure_boot_script(self):
        """Repair the init script's bytes and metadata on every successful up."""
        content = self._boot_script_source()
        if self._boot_script_intact(content):
            return
        # Publish complete bytes and metadata together; sync before up can fail and
        # its caller power off the guest. The control account's only passwordless sudo
        # is the controller itself, so root comes from its password file, as the
        # controller install's does.
        script = f"""set -eu
temporary=$(/usr/bin/mktemp /usr/local/sbin/.lima-macos-init.XXXXXX)
trap '/bin/rm -f "$temporary"' EXIT
/bin/cat > "$temporary" <<'FIRM_BOOT_SCRIPT'
{content.decode()}FIRM_BOOT_SCRIPT
/usr/sbin/chown root:wheel "$temporary"
/bin/chmod 755 "$temporary"
/bin/mv -f "$temporary" {BOOT_SCRIPT}
/bin/sync
"""
        self._shell("/bin/zsh", "-c", mac_sudo_command("/bin/sh", "-c", script), timeout=60)

    def _watch_boot(self, finished):
        """One best-effort rescue while Lima waits; never retry an uncertain exec."""
        ready_since = None
        condition = f"[ -x {BOOT_AGENT} ] && [ ! -e {BOOT_DONE} ]"
        while not finished.wait(BOOT_POLL):
            try:
                probe = self._shell(
                    "/bin/sh",
                    "-c",
                    condition,
                    check=False,
                    timeout=5,
                )
            except (CommandError, OSError, subprocess.TimeoutExpired):
                ready_since = None
                continue
            if finished.is_set():
                return
            if probe.returncode:
                ready_since = None
                continue
            now = time.monotonic()
            if ready_since is None:
                ready_since = now
            if now - ready_since < BOOT_GRACE:
                continue
            # Recheck in the guest immediately before exec in case normal init
            # finished during the grace period or between SSH calls.
            logger.warning("Mac boot marker is absent; attempting fake-cloud-init once")
            try:
                self._shell(
                    "/bin/zsh",
                    "-c",
                    f"{condition} && {mac_sudo_command(BOOT_AGENT, 'fake-cloud-init')}",
                    timeout=30,
                )
            except (CommandError, OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Mac boot recovery attempt failed: %s", exc)
            return

    def _start(self):
        finished = threading.Event()
        watcher = threading.Thread(target=self._watch_boot, args=(finished,), name="mac-boot")
        watcher.start()
        try:
            self._lima("start", "--tty=false", INSTANCE, timeout=180)
        finally:
            finished.set()
            # The watcher must stop issuing calls before up's failure/shutdown path.
            watcher.join()

    def _controller_intact(self, content: bytes) -> bool:
        """Whether the guest still holds the controller the host recorded installing.

        A boot that fails after the install can power the VM off before the file reaches
        its disk; the recorded digest must not keep a later boot from repairing that.
        """
        result = self._shell("/usr/bin/shasum", "-a", "256", CONTROLLER, check=False, timeout=30)
        installed = result.stdout.decode(errors="replace").split()
        return result.returncode == 0 and installed[:1] == [hashlib.sha256(content).hexdigest()]

    def _controller_source(self):
        """The controller to install, its bytes, and the digest an install records."""
        source = Path(__file__).with_name("mac_vm_guest.py")
        content = source.read_bytes()
        return source, content, hashlib.sha256(content + Path(__file__).read_bytes()).hexdigest()

    def controller_installed(self):
        """Whether the guest already holds this host's controller, so ``up`` installs nothing.

        The install writes the whole machine, so a caller that may not meet a lease asks
        this before deciding how much of the Mac it has to lock.
        """
        _, content, digest = self._controller_source()
        return self._state().get("controller_digest") == digest and self._controller_intact(content)

    def _install_controller(self):
        source, _, digest = self._controller_source()
        if self.controller_installed():
            return
        self._lima("copy", str(source), f"{INSTANCE}:/Users/firmvm/firm-controller.py")
        script = f"""set -eu
trap '/bin/rm -f /Users/firmvm/install-controller.sh' EXIT
/usr/bin/install -d -m 755 -o root -g wheel /usr/local/libexec /etc/sudoers.d
/usr/bin/install -m 755 -o root -g wheel /Users/firmvm/firm-controller.py {CONTROLLER}
/usr/bin/install -d -m 700 -o root -g wheel /var/db/firm
printf '%s\\n' 'firmvm ALL=(root) NOPASSWD: {" ".join(CONTROLLER_COMMAND)}' > /etc/sudoers.d/firm
/bin/chmod 440 /etc/sudoers.d/firm
/usr/sbin/visudo -cf /etc/sudoers.d/firm
/bin/rm /Users/firmvm/firm-controller.py
/bin/sync
"""
        # The sync: the host records this install as done below, so it must be on the
        # guest's disk before a failed boot's power-off can follow. The install is the
        # last thing that needs the password file as it is: a fresh clone's own control
        # password is made afterwards, through the controller's passwordless sudo, so an
        # interrupted rotation is finished the same way and never blocks a reinstall.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh") as file:
            file.write(script)
            file.flush()
            self._lima("copy", file.name, f"{INSTANCE}:/Users/firmvm/install-controller.sh")
        self._shell(
            "/bin/zsh",
            "-c",
            mac_sudo_command("/bin/zsh", "/Users/firmvm/install-controller.sh"),
            timeout=240,
        )
        state = self._state()
        state["controller_digest"] = digest
        save(self.record, state)

    @lifecycle(exclusive=True)
    def up(self, *, checkpoint=lambda: None):
        """Clone the image on first use, boot behind the host filter and initialize the guest.

        ``checkpoint`` runs before the boot and on each side of Lima's start. An
        exception from it abandons the boot; once the start was attempted, the VM and
        its filter are powered off first.
        """
        checkpoint()
        # Check compatibility before projecting Lima files or creating guest state.
        if self.record.exists():
            state = validate_retained_vm(self._state())
            if self.image is not None:
                metadata = validate_image_manifest(read_record(self.image / "image.json"))
                if state.get("image_id") != metadata["image_id"]:
                    raise ValueError("Retained VM image identity changed; use mac-replace")
        elif self.image is not None:
            validate_image_manifest(read_record(self.image / "image.json"))
        private_directory(self.root)
        private_directory(self.network.paths["scratch"])
        self.paths.prepare()
        if self.record.exists():
            state = validate_retained_vm(self._state())
            if state.get("firm_id") != self.settings.firm_id:
                raise ValueError("VM record belongs to another firm identity")
            if self.image is not None and self.image != Path(state["image"]):
                raise ValueError("Use mac-replace, or replace, to change the VM image")
            if (state["cpus"], state["memory_gib"]) != (
                self.settings.cpus,
                self.settings.memory_gib,
            ):
                state = self._apply_resources(state)
        else:
            if self.instance.exists():
                raise RuntimeError("Incomplete VM clone exists; inspect it before recovery")
            try:
                self._clone(self.image)
                self.paths.synchronize()
            except BaseException:
                # A clone has never booted before its control record is published.
                if not self.record.exists() and self.instance.is_dir():
                    shutil.rmtree(self.instance)
                raise
        self._validate_config()
        if self._running():
            if not self.network.ready():
                raise RuntimeError("VM network boundary is unavailable; stop the VM")
        else:
            self.network.write_configuration()
            self.network.stop()
            self.network.start()
            # Lima must reuse our filtered user-v2 endpoint. Its marker names the
            # network process, never this runtime, and exists only at boot.
            try:
                self.network.publish_lima_pid()
                checkpoint()
                self._start()
                checkpoint()
                if not self.network.ready():
                    raise RuntimeError("Filtered guest NIC did not become ready")
                self.paths.synchronize()
                self.paths.mark_running()
            except BaseException:
                try:
                    self._lima("stop", "--force", "--tty=false", INSTANCE, check=False)
                finally:
                    self.paths.preserve_logs()
                    self.network.stop()
                raise
            finally:
                self.network.paths["pid"].unlink(missing_ok=True)
        self._install_controller()
        self._ensure_boot_script()
        # Restated on every boot: the machine's system proxy and the SSH sessions' proxy
        # environment both name this VM's proxy, the guest's only way out.
        self._rpc(
            "initialize",
            firm_id=self.settings.firm_id,
            proxy={"host": self.network.gateway, "port": self.network.proxy_port},
        )
        # A password change an earlier call left half done is finished here, through the
        # controller's passwordless sudo, before anything asks for a recorded password.
        self._rpc("settle-credentials", timeout=400)
        if not self._state().get("ready"):
            # A fresh clone gets its own control password; SSH password login stays
            # disabled and no owner key enters the VM. Journaled: a retry finishes it.
            self._rpc("rotate-control-password", timeout=400)
        state = self._state()
        state["ready"] = True
        save(self.record, state)
        return self.info()

    @lifecycle(exclusive=True)
    def down(self):
        """Stop the guest gracefully while its filter answers; power off a stopped or Broken VM."""
        self.paths.verify_runtime()
        try:
            running = self._running()
        except VMStateError:
            running = False
        if not running:
            return self.force_down()
        self.network.paths["pid"].unlink(missing_ok=True)
        if self.network.ready():
            try:
                self._lima("stop", "--tty=false", INSTANCE, timeout=30)
            except (RuntimeError, subprocess.TimeoutExpired):
                self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=30)
        else:
            self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=30)
        self._settle_stopped()
        return {"running": False}

    def force_down(self):
        """Power off first, without waiting for the ordinary lifecycle lock."""
        # No stop is sent into a runtime that may hold another firm's VM.
        self.paths.verify_runtime()
        # A removal interrupted after the instance went leaves only the record; there is
        # no configuration left to check and nothing to stop, and remove() must repeat.
        if self.record.exists() and self.instance.exists():
            self._validate_config()
        if self.instance.exists() or self.paths.has_live_processes():
            self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=30)
        self._settle_stopped()
        return {"running": False}

    def _settle_stopped(self):
        self.paths.verify_stopped(self._running)
        # Before the filter is stopped: a filter that refuses to stop must not cost the
        # logs of a VM that did. The running record goes only once the filter has.
        self.paths.preserve_logs()
        self.network.paths["pid"].unlink(missing_ok=True)
        self.network.stop()
        self.paths.mark_stopped()

    @lifecycle(exclusive=True)
    def remove(self):
        """Discard the shared disk and its VM record."""
        self.down()
        self.paths.verify_stopped(self._running)
        self.paths.prepare()
        if self.instance.exists():
            self._lima("delete", "--force", "--tty=false", INSTANCE, timeout=120)
        if self.paths.instance.exists():
            raise RuntimeError("Mac VM was not removed")
        self.paths.remove_instance()
        self.record.unlink(missing_ok=True)
        return {"removed": True}
