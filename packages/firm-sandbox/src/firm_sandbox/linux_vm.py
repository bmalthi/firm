"""Linux VM driver: the retained VM holding every role's home, Docker daemon and file server.

Lima instance ``linux``. This module owns the disk, the packet filter, the guest
controller's installation and the one-request control channel to it. No host
directory is mounted into the guest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import yaml

from .commands import Commands, VMStateError, lima_status, parse_lima_list, resize_instance
from .compatibility import (
    COMPATIBILITY_GENERATION,
    release_identifier,
    validate_retained_vm,
)
from .egress_proxy import PROVISION
from .guest_control import GuestControl, lifecycle, private_directory, read_record, save
from .host_network import HostNetwork
from .lima_paths import lima_paths
from .models import VMSettings

INSTANCE = "linux"
IMAGE = Path(__file__).with_name("assets") / "linux-vm.yaml"
# The packaged provisioning recipe is the authority for the installed AGY version.
AGY_VERSION = re.search(r"^\s*AGY_VERSION=(\d+\.\d+\.\d+)$", IMAGE.read_text(), re.MULTILINE)[1]
PYTHON = "/usr/bin/python3"
CONTROLLER = "/usr/local/libexec/firm-controller.py"
PROVISION_MARKER = "/run/firm-provisioned"
GUEST_DEPENDENCIES = (
    "mount.cifs",
    "setfacl",
    "smbpasswd",
    "smbd",
    "newuidmap",
    "newgidmap",
    "dbus-run-session",
    "docker",
    "uv",
    "node",
    "npm",
    "claude",
    "codex",
    "agy",
    "gh",
    "git",
    "curl",
    "xz",
    "gcc",
    "g++",
    "make",
    "modprobe",
)
GUEST_DEPENDENCY_CHECK = "set -eu\n" + "\n".join(
    f"command -v {command} >/dev/null" for command in GUEST_DEPENDENCIES
) + (
    '\ntest -x /usr/local/lib/agy/agy'
    f'\ntest "$(cat /usr/local/lib/agy/version)" = "{AGY_VERSION}"'
)
CONTROLLER_COMMAND = (PYTHON, "-I", CONTROLLER)
# ${SUDO_USER} is the control user, known only inside the guest; _install_controller
# substitutes it for this placeholder without expanding $msg or $programname there.
CONTROL_USER_PLACEHOLDER = "@CONTROL_USER@"
# rsyslog's $msg for the control user's successful controller call, exactly: the leading
# space is the syslog separator's, the two after the colon are sudo-rs's empty TTY field.
CONTROLLER_SUDO_LINE = (
    f" {CONTROL_USER_PLACEHOLDER} :  PWD=/ ; USER=root ; COMMAND={' '.join(CONTROLLER_COMMAND)}"
)
# The guest's rsyslog rule, one line: drop sudo's line only when it equals that one, and
# the session lines around every sudo (they carry no command, so cannot be attributed).
# A denied call, extra arguments or any other command differs and is still logged.
# Equality, not re_match: RainerScript rejects a `\.` escape, which silences auth.log.
QUIET_CONTROLLER_SUDO = (
    f"if $programname == 'sudo' and ($msg == '{CONTROLLER_SUDO_LINE}' "
    "or $msg contains 'pam_unix(sudo:session)') then stop"
)


def _validate_image(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Linux VM image must be an owner-controlled regular file")
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("Unable to read the Linux VM image configuration") from exc
    if not isinstance(config, dict):
        raise ValueError("Linux VM image must contain a mapping")
    containerd = config.get("containerd")
    if not isinstance(containerd, dict):
        raise ValueError("Linux VM image must define containerd policy")
    if (
        config.get("os") != "Linux"
        or config.get("arch") != "aarch64"
        or config.get("vmType") != "vz"
        or config.get("networks") != [{"lima": "user-v2"}]
        or config.get("mounts")
        or config.get("propagateProxyEnv") is not False
        or containerd.get("system") is not False
        or containerd.get("user") is not False
    ):
        raise ValueError("Linux VM image violates its isolated VM policy")
    ssh = config.get("ssh", {})
    if any(
        ssh.get(key) is not False
        for key in ("loadDotSSHPubKeys", "forwardAgent", "forwardX11", "forwardX11Trusted")
    ):
        raise ValueError("Linux VM SSH forwarding must be disabled")
    return config


class LinuxVM(GuestControl):
    """One retained Linux VM per firm; roles, their daemons and the shared trees live here."""

    guest_instance = INSTANCE
    guest_controller_command = CONTROLLER_COMMAND

    def __init__(
        self,
        settings: VMSettings,
        *,
        limactl: str | None = None,
    ):
        if settings.root.is_symlink():
            raise ValueError("VM root must not be a symlink")
        self.settings = settings
        self.root = settings.root
        self.firm_locks = settings.firm_locks
        self.paths = lima_paths(self.root, instance=INSTANCE)
        self.home = self.paths.durable_home
        self.instance = self.paths.durable_instance
        self.ssh_config = self.paths.ssh_config
        self.socket_root = self.paths.runtime
        self.record = self.root / "vm.json"
        self.lima = limactl or shutil.which("limactl")
        if not self.lima:
            raise RuntimeError("Lima is not installed")
        # A stable private /24 per firm so Lima's network and the packet policy agree.
        subnet_octet = 16 + int(hashlib.sha256(str(self.root).encode()).hexdigest()[:4], 16) % 200
        self.subnet = f"192.168.{subnet_octet}.0/24"
        if self.record.exists():
            # A stopped firm can move; its retained guest keeps its original network.
            self.subnet = self._state()["subnet"]
        env = {**os.environ, "LIMA_HOME": str(self.paths.home), "FIRM_REAL_LIMACTL": self.lima}
        for key in tuple(env):
            if "proxy" in key.lower():
                env.pop(key)
        self.commands = Commands(env)
        self.network = HostNetwork(
            settings,
            self.commands,
            # Lima resolves user-v2 endpoints below LIMA_HOME; the filter must use the
            # same short runtime root or Lima boots with an unfiltered endpoint.
            socket_root=self.paths.runtime,
            subnet=self.subnet,
            verify=self.paths.verify_runtime,
        )

    def _lima(self, *args: str, **kwargs):
        # Every Lima command acts on whatever VM LIMA_HOME, the runtime, holds.
        self.paths.verify_runtime()
        return self.commands.run([self.lima, *args], **kwargs)

    def _shell(self, *args: str, **kwargs):
        return self._lima("shell", "--tty=false", "--workdir=/", INSTANCE, *args, **kwargs)

    def _running(self) -> bool:
        if not self.paths.instance.exists():
            if self.paths.has_live_processes():
                raise RuntimeError("VM runtime is missing while its host process is alive")
            return False
        rows = parse_lima_list(self._lima("list", "--json", INSTANCE).stdout)
        if len(rows) != 1 or rows[0].get("name") != INSTANCE:
            raise RuntimeError("Could not determine Linux VM state")
        status = lima_status(rows[0])
        if status not in ("running", "stopped"):
            raise VMStateError(status)
        return status == "running"

    def _state(self) -> dict:
        if not self.record.exists():
            return {"ready": False}
        return read_record(self.record)

    def _validate_existing(self) -> None:
        state = validate_retained_vm(self._state())
        # This digest records the template the retained VM was created from.
        # Compatible releases may change the template used to create new VMs.
        if state.get("firm_id") != self.settings.firm_id:
            raise ValueError("VM record belongs to another firm identity")
        if state.get("subnet") != self.subnet:
            raise ValueError("Use the recorded Linux VM subnet")
        if (
            state.get("cpus") != self.settings.cpus
            or state.get("memory_gib") != self.settings.memory_gib
        ):
            # The roles' homes are the VM's contents, so resizing edits the stopped
            # guest's definition rather than asking for a replacement.
            if self._running():
                raise VMStateError("Running")
            resize_instance(self.instance, self.settings.cpus, self.settings.memory_gib)
            save(
                self.record,
                {
                    **state,
                    "cpus": self.settings.cpus,
                    "memory_gib": self.settings.memory_gib,
                },
            )

    def _prepare_network_definition(self) -> None:
        """Make Lima's user-v2 gateway match this VM's filtered usernet."""
        path = self.paths.home / "_config" / "networks.yaml"
        if path.is_symlink():
            raise ValueError("Lima network definition must not be a symlink")
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("Unsafe Lima network definition")
            try:
                config = yaml.safe_load(path.read_text())
            except (OSError, yaml.YAMLError) as exc:
                raise ValueError("Invalid Lima network definition") from exc
        else:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("Invalid Lima network definition")
        networks = config.setdefault("networks", {})
        if not isinstance(networks, dict):
            raise ValueError("Invalid Lima network definitions")
        usernet = networks.setdefault("user-v2", {})
        if not isinstance(usernet, dict):
            raise ValueError("Invalid Lima user-v2 network definition")
        usernet.update(
            {
                "mode": "user-v2",
                "gateway": self.network.subnet.replace("0/24", "1"),
                "netmask": "255.255.255.0",
            }
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".new")
        temporary.write_text(yaml.safe_dump(config, sort_keys=False))
        temporary.chmod(0o600)
        temporary.replace(path)

    def _prepare(self) -> None:
        if self.record.exists():
            validate_retained_vm(self._state())
        private_directory(self.root)
        private_directory(self.network.paths["scratch"])
        _validate_image(IMAGE)
        private_directory(self.home)
        self.paths.prepare()
        if self.record.exists():
            self._validate_existing()
            if not self.instance.exists():
                raise RuntimeError("Linux VM record exists but its VM is missing")
            self._prepare_network_definition()
            return
        if self.instance.exists() or self.paths.instance.exists():
            raise RuntimeError("Incomplete Linux VM exists; inspect it before recovery")
        # The image's provisioning installs through the egress proxy like everything
        # else in the guest; Lima hands the setting to it as $PARAM_FIRM_PROXY. Its
        # credential holds until the guest provisioning script confirms its dependencies.
        self.network.paths["provisioning"].touch(mode=0o600)
        self.network.create_key()
        self._lima(
            "create",
            f"--name={INSTANCE}",
            f"--cpus={self.settings.cpus}",
            f"--memory={self.settings.memory_gib}",
            f"--disk={self.settings.disk_gib}",
            "--mount-none",
            "--plain",
            "--tty=false",
            f'--set=.param.FIRM_PROXY = "{self.network.proxy_url(PROVISION)}"',
            str(IMAGE),
            timeout=600,
        )
        if self._running():
            raise RuntimeError("New Linux VM unexpectedly started during creation")
        self.paths.adopt_stopped_instance()
        self._prepare_network_definition()
        save(
            self.record,
            {
                "firm_id": self.settings.firm_id,
                "generation": uuid.uuid4().hex,
                "created_with_release": release_identifier(),
                "compatibility_generation": COMPATIBILITY_GENERATION,
                "image_id": hashlib.sha256(IMAGE.read_bytes()).hexdigest(),
                "cpus": self.settings.cpus,
                "memory_gib": self.settings.memory_gib,
                "subnet": self.subnet,
                "ready": False,
            },
        )

    def _controller_intact(self, content: bytes) -> bool:
        """Whether the guest still holds the controller the host recorded installing.

        Power loss before the install reaches disk can leave an empty controller.
        Verify the guest file before trusting the recorded digest so the next boot
        repairs an incomplete install.
        """
        result = self._shell("/usr/bin/sha256sum", CONTROLLER, check=False, timeout=30)
        installed = result.stdout.decode(errors="replace").split()
        return result.returncode == 0 and installed[:1] == [hashlib.sha256(content).hexdigest()]

    def _install_controller(self) -> None:
        source = Path(__file__).with_name("linux_vm_guest.py")
        content = source.read_bytes()
        digest = hashlib.sha256(content + Path(__file__).read_bytes()).hexdigest()
        recorded = self._state().get("controller_digest") == digest
        if recorded and self._controller_intact(content):
            return
        # A schema/code change requires stopped roles; setup runs on a fresh VM or before
        # the next launch after guest controller updates. A controller that did not survive
        # its install has no runs to ask about and could not answer.
        if (
            not recorded
            and self._state().get("ready")
            and any(r["running"] for r in self._rpc("runs"))
        ):
            raise RuntimeError("Stop running roles before updating the guest controller")
        # The control user's private directory prevents another role from swapping
        # the controller between checksum verification and the privileged install.
        staging = self._shell(
            "/usr/bin/mktemp", "-d", "/tmp/firm-controller.XXXXXXXXXX", timeout=30,
        ).stdout.decode().strip()
        if not re.fullmatch(r"/tmp/firm-controller\.[A-Za-z0-9]{10}", staging):
            raise RuntimeError("Invalid controller staging directory")
        staged = f"{staging}/controller.py"
        try:
            with tempfile.NamedTemporaryFile(suffix=".py") as snapshot:
                snapshot.write(content)
                snapshot.flush()
                self._lima("copy", snapshot.name, f"{INSTANCE}:{staged}")
            script = f"""set -eu
printf '%s\\n' '{hashlib.sha256(content).hexdigest()}  {staged}' | sha256sum --check --status
install -d -m 755 -o root -g root /usr/local/libexec /etc/sudoers.d
install -m 755 -o root -g root {staged} {CONTROLLER}
install -d -m 700 -o root -g root /var/lib/firm
printf '%s\\n' "${{SUDO_USER:?}} ALL=(root) NOPASSWD: {" ".join(CONTROLLER_COMMAND)}" \\
    > /etc/sudoers.d/firm
chmod 440 /etc/sudoers.d/firm
visudo -cf /etc/sudoers.d/firm
rm -f {staged}
# QUIET_CONTROLLER_SUDO explains what this rule drops and why sudoers cannot.
cat > /etc/rsyslog.d/10-firm.conf <<'RSYSLOG'
{QUIET_CONTROLLER_SUDO}
RSYSLOG
sed -i "s/{CONTROL_USER_PLACEHOLDER}/${{SUDO_USER:?}}/" /etc/rsyslog.d/10-firm.conf
# As visudo does for sudoers: a rule rsyslog cannot parse stops auth.log altogether.
if ! rsyslogd -N1 >/dev/null 2>&1; then
    rm -f /etc/rsyslog.d/10-firm.conf
    echo 'rsyslog refused the firm rule' >&2
    exit 1
fi
install -d -m 755 -o root -g root /etc/systemd/journald.conf.d
printf '[Journal]\\nSystemMaxUse=500M\\n' > /etc/systemd/journald.conf.d/firm.conf
systemctl restart rsyslog systemd-journald
# The host records this install as done: it must be on disk before a power-off can follow.
sync
"""
            self._shell("/usr/bin/sudo", "-n", "bash", "-c", script, timeout=60)
        finally:
            self._shell("/bin/rm", "-rf", "--", staging, check=False, timeout=30)
        state = self._state()
        state["controller_digest"] = digest
        save(self.record, state)

    def _verify_guest_ready(self, *, require_provision_marker: bool) -> None:
        """Confirm first-boot provisioning and required runtime commands.

        New or previously unready guests must produce a marker from this boot's Lima
        provisioning script. Compatible guests created by older recipes have no marker,
        so their installed commands are checked directly instead.
        """
        if require_provision_marker:
            marker = self._shell(
                "/bin/bash",
                "-c",
                f"test -f {PROVISION_MARKER}",
                check=False,
                timeout=30,
            )
            if marker.returncode:
                raise RuntimeError(
                    "Linux guest provisioning did not complete; inspect its Lima cloud-init "
                    "log and retry firm start"
                )
        dependencies = self._shell(
            "/bin/bash",
            "-c",
            GUEST_DEPENDENCY_CHECK,
            check=False,
            timeout=30,
        )
        if dependencies.returncode:
            raise RuntimeError(
                "Linux VM is missing required runtime or file-server dependencies; "
                "inspect its Lima cloud-init log and retry firm start"
            )

    def _check_run(self, run) -> None:
        if run.vm_generation != self._state()["generation"]:
            raise ValueError("Run belongs to a discarded VM generation")

    @lifecycle(exclusive=True)
    def up(self) -> dict:
        self._prepare()
        state = self._state()
        provisioning_pending = (
            state.get("ready") is not True or self.network.paths["provisioning"].exists()
        )
        running = self._running()
        if running and provisioning_pending:
            # A previous first boot may have returned from Lima with an incomplete
            # provisioning script. Restart it so cloud-init gets a fresh attempt while
            # the host-side provisioning credential is still valid.
            self._lima("stop", "--force", "--tty=false", INSTANCE)
            if self._running():
                raise RuntimeError("Linux VM shutdown was not confirmed")
            self.paths.preserve_logs()
            running = False
        if running and not self.network.ready():
            # A direct Lima restart can attach its own usernet instead of our
            # filter. Stop that NIC before rebuilding the boundary.
            self._lima("stop", "--force", "--tty=false", INSTANCE)
            if self._running():
                raise RuntimeError("Linux VM shutdown was not confirmed")
            self.paths.preserve_logs()
            running = False
        if not running:
            self.network.write_configuration()
            self.network.stop()
            self.network.start()
            try:
                # Lima may signal this marker during stop. Publish the filter's
                # PID, never the controlling runtime's PID.
                self.network.publish_lima_pid()
                self._lima("start", "--tty=false", INSTANCE, timeout=1200)
                if not self._running():
                    raise RuntimeError("Linux VM startup was not confirmed")
                self.paths.mark_running()
                self.paths.synchronize()
                if not self.network.ready():
                    raise RuntimeError("Linux VM network filter is not attached")
                self._verify_guest_ready(require_provision_marker=provisioning_pending)
            except BaseException:
                try:
                    self._lima("stop", "--force", "--tty=false", INSTANCE, check=False)
                finally:
                    self.paths.preserve_logs()
                    self.network.stop()
                raise
            finally:
                self.network.paths["pid"].unlink(missing_ok=True)
        else:
            self.paths.mark_running()
            self._verify_guest_ready(require_provision_marker=provisioning_pending)
        self._install_controller()
        self._rpc("initialize", firm_id=self.settings.firm_id)
        state = self._state()
        state.update({"ready": True, "network_ready": self.network.ready()})
        save(self.record, state)
        # Provisioning is over: from now on its credential is refused like any wrong one.
        self.network.paths["provisioning"].unlink(missing_ok=True)
        return self.status()

    def status(self) -> dict:
        state = self._state()
        try:
            running = self._running()
        except VMStateError as exc:
            # A Broken Lima record does not prove that the VM is stopped. Do not
            # report a false safe state; expose the recovery requirement.
            return {
                **state,
                "running": None,
                "state": "recovery_required",
                "error": str(exc),
                "network_ready": False,
            }
        return {
            **state,
            "running": running,
            "network_ready": self.network.ready(attached=running) if running else False,
        }

    @lifecycle(exclusive=True)
    def down(self) -> dict:
        self.paths.verify_runtime()
        try:
            running = self._running()
            broken = False
        except VMStateError:
            # Lima's Broken state makes a graceful stop unreliable. Treat the VM as
            # requiring an emergency power-off and verify the result below.
            running = True
            broken = True
        try:
            if running:
                if broken:
                    self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=120, check=False)
                else:
                    try:
                        self._lima("stop", "--tty=false", INSTANCE, timeout=120)
                    except (RuntimeError, subprocess.TimeoutExpired):
                        self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=120)
            try:
                self.paths.verify_stopped(self._running)
            except (VMStateError, RuntimeError):
                if broken:
                    raise
                self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=120, check=False)
                self.paths.verify_stopped(self._running)
            self.paths.mark_stopped()
        finally:
            self.network.stop()
        return self.status()

    def flush(self, *, timeout=15) -> bool:
        """Best effort: commit the guest's dirty pages before it loses power.

        A power-off that keeps the boundary's guarantee must still not truncate
        the roles' work. This never blocks an emergency beyond its timeout, and a
        guest that cannot answer is powered off unflushed.
        """
        try:
            result = self._shell("/bin/sync", check=False, timeout=timeout)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def force_down(self) -> dict:
        """Power off first, without waiting for the ordinary lifecycle lock."""
        try:
            # Lima's stop and the filter's control socket both act on whatever the
            # runtime holds, so nothing is sent into one that may be another firm's.
            self.paths.verify_runtime()
        except RuntimeError as exc:
            raise RuntimeError("Linux VM emergency shutdown requires attention") from exc
        errors = []
        if self.paths.instance.exists() or self.paths.has_live_processes():
            try:
                # Do not let Lima consume a stale marker left by an interrupted launch.
                self.network.paths["pid"].unlink(missing_ok=True)
                self._lima("stop", "--force", "--tty=false", INSTANCE, timeout=60, check=False)
            except BaseException as exc:
                errors.append(exc)
            try:
                self.paths.verify_stopped(self._running)
                self.paths.mark_stopped()
            except BaseException as exc:
                errors.append(exc)
        try:
            self.network.stop()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError("Linux VM emergency shutdown requires attention") from errors[0]
        return self.status()

    @lifecycle()
    def export(self, destination: Path, *, root: str, homes: bool) -> list[str]:
        """Unpack the guest controller's ``export`` tar stream under ``destination``.

        The guest's files are role-written, so every member passes the ``data``
        extraction filter; a member it refuses (an absolute or escaping link, a device)
        is skipped and its name returned. A stream that ends before the guest controller
        exits successfully is an error, not a shorter export.
        """
        if not self.network.ready():
            raise RuntimeError("VM network boundary is unavailable; stop the VM")
        refused = []

        def data(member, path):
            try:
                return tarfile.data_filter(member, path)
            except tarfile.FilterError:
                refused.append(member.name)
                return None

        argv = [
            self.lima,
            "shell",
            "--tty=false",
            "--workdir=/",
            INSTANCE,
            "/usr/bin/sudo",
            "-n",
            *CONTROLLER_COMMAND,
        ]
        request = {"operation": "export", "root": root, "homes": homes}
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as diagnostics:
            stdin.write(json.dumps(request).encode())
            stdin.seek(0)
            process = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=diagnostics,
                env=self.commands.env,
                start_new_session=True,
            )
            unreadable = None
            try:
                try:
                    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                        archive.extractall(destination, filter=data)
                    while process.stdout.read(65536):
                        pass  # the archive's trailing record padding
                except tarfile.ReadError as exc:
                    unreadable = exc  # the guest's exit status explains a broken stream
                code = process.wait()
            finally:
                process.stdout.close()
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            if code or unreadable:
                diagnostics.seek(0)
                detail = diagnostics.read()[-16384:].decode(errors="replace").strip()
                raise RuntimeError(f"Guest export failed: {detail or unreadable}")
        return refused

    @lifecycle(exclusive=True)
    def remove(self) -> dict:
        """Discard the disk: homes, daemons, images, volumes and the shared trees."""
        self.down()
        self.paths.verify_stopped(self._running)
        self.paths.prepare()
        if self.instance.exists():
            self._lima("delete", "--force", "--tty=false", INSTANCE, timeout=120)
        if self.paths.instance.exists():
            raise RuntimeError("Linux VM was not removed")
        self.paths.remove_instance()
        self.record.unlink(missing_ok=True)
        self.network.paths["provisioning"].unlink(missing_ok=True)
        return {"removed": True}
