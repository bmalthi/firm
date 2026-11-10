"""Build and cache the trusted Linux software image on this Mac."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from .commands import Commands, lima_status, parse_lima_list
from .guest_control import private_directory
from .lima_paths import lima_paths
from .locking import LockBusy, locked
from .models import MIN_DISK_GIB

RECIPE = Path(__file__).with_name("assets") / "linux-base.yaml"
GENERALIZE = RECIPE.with_name("linux-generalize.sh")
# After this many days `firm check` suggests rebuilding before a new firm; it is optional.
MAX_AGE_DAYS = 30
BUILD_TIMEOUT = 20 * 60
INSTANCE = "linux"
LOGGER = logging.getLogger(__name__)


def cache_directory() -> Path:
    return Path.home() / "Library" / "Caches" / "firm" / "linux-base"


def image_key(recipe: Path = RECIPE, generalize: Path = GENERALIZE) -> str:
    return hashlib.sha256(recipe.read_bytes() + generalize.read_bytes()).hexdigest()


def image_age_days() -> float | None:
    """Age of the current recipe's image, or None when the next new firm must build it."""
    try:
        built = (cache_directory() / f"{image_key()}.img").stat().st_mtime
    except FileNotFoundError:
        return None
    return max(0.0, (time.time() - built) / 86400)


def base_image(limactl: str, checkpoint=lambda: None) -> Path:
    directory = cache_directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock outside the directory operators delete to force a rebuild.
    with ExitStack() as stack:
        while True:
            checkpoint()
            try:
                stack.enter_context(locked(directory.parent, "linux-base.lock", timeout=0.2))
                break
            except LockBusy:
                continue
        private_directory(directory)
        image = directory / f"{image_key()}.img"
        # An old image is still used: `firm check` tells the owner, who decides to rebuild.
        if not image.exists():
            try:
                _build(limactl, directory, image, checkpoint)
            except Exception as exc:
                checkpoint()  # A force-stop propagates as itself, not as a build failure.
                raise RuntimeError(
                    f"Linux base image build failed: {exc}; logs in {directory / 'build-logs'}"
                ) from exc
            # Images from earlier recipes are obsolete once this one exists.
            for old in directory.glob("*.img"):
                if old != image:
                    old.unlink()
        checkpoint()
        return image


def _build(limactl: str, directory: Path, image: Path, checkpoint) -> None:
    logs = directory / "build-logs"
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir(mode=0o700)
    builder = Builder(directory / "builder", limactl, checkpoint)
    try:
        # A killed process's builder is discarded without ever booting it.
        if builder.root.exists():
            builder.paths.prepare()
            builder.remove()
        builder.prepare()
        builder.boot()
        builder.wait_cloud_init()
        builder.keep_guest_logs(logs)
        builder.stop()
        # Lima runs system provisioning on every boot. The second boot tests the
        # upgraded kernel without installing the software a second time.
        builder.run("edit", "--tty=false", "--set=.provision = []", INSTANCE)
        builder.boot()
        # cloud-init still runs its per-boot stages; let them finish before generalizing.
        builder.wait_cloud_init()
        from .linux_vm import GUEST_DEPENDENCY_CHECK

        builder.shell("bash", "-c", GUEST_DEPENDENCY_CHECK)
        builder.shell("sudo", "-n", "bash", "-s", input=GENERALIZE.read_bytes(), timeout=120)
        builder.stop()
        # Publish the stopped disk before cancellation can discard a finished build.
        os.replace(builder.paths.durable_instance / "disk", image)
    except BaseException:
        builder.keep_guest_logs(logs)
        raise
    finally:
        builder.remove(logs=logs)


class Builder:
    def __init__(self, root: Path, limactl: str, checkpoint):
        self.root = root
        self.lima = limactl
        self.checkpoint = checkpoint
        self.deadline = time.monotonic() + BUILD_TIMEOUT
        self.paths = lima_paths(root, instance=INSTANCE)
        env = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
        self.commands = Commands({**env, "LIMA_HOME": str(self.paths.home)})

    def run(self, *args, timeout=120, **kwargs):
        self.checkpoint()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Linux base build exceeded twenty minutes")
        self.paths.verify_runtime()
        return self.commands.run([self.lima, *args], timeout=min(timeout, remaining), **kwargs)

    def shell(self, *args, **kwargs):
        return self.run("shell", "--tty=false", "--workdir=/", INSTANCE, *args, **kwargs)

    def running(self):
        rows = parse_lima_list(self.run("list", "--json", INSTANCE).stdout)
        if len(rows) != 1 or rows[0].get("name") != INSTANCE:
            raise RuntimeError("Could not determine Linux base builder state")
        status = lima_status(rows[0])
        if status not in ("running", "stopped"):
            raise RuntimeError(f"Linux base builder state: {status}")
        return status == "running"

    def prepare(self):
        private_directory(self.root)
        private_directory(self.paths.durable_home)
        self.paths.prepare()
        self.run("create", f"--name={INSTANCE}", "--cpus=4", "--memory=4",
                 f"--disk={MIN_DISK_GIB}",
                 "--mount-none", "--plain", "--tty=false", str(RECIPE))
        self.paths.adopt_stopped_instance()

    def boot(self):
        remaining = max(1, int(self.deadline - time.monotonic()))
        self.run("start", "--tty=false", f"--timeout={remaining}s", INSTANCE,
                 timeout=remaining, check=False)
        if not self.running():
            raise RuntimeError("Linux base builder startup was not confirmed")
        self.paths.mark_running()
        self.paths.synchronize()

    def wait_cloud_init(self):
        while True:
            result = self.shell("sudo", "-n", "cloud-init", "status", check=False, timeout=10)
            status = result.stdout.decode(errors="replace").lower()
            # Exit 2 means recoverable warnings (Lima's ssh-authorized-keys).
            if "status: error" in status or result.returncode not in (0, 2):
                raise RuntimeError(f"Linux base cloud-init failed: {status.strip()}")
            if "status: done" in status:
                return
            self.checkpoint()
            time.sleep(1)

    def stop(self):
        self.run("stop", "--tty=false", INSTANCE)
        if self.running():
            raise RuntimeError("Linux base builder shutdown was not confirmed")
        self.paths.mark_stopped()

    def keep_guest_logs(self, logs):
        # Bypass cancellation/deadline only for bounded evidence and cleanup calls.
        for name in ("cloud-init-output.log", "cloud-init.log"):
            try:
                self.paths.verify_runtime()
                result = self.commands.run(
                    [self.lima, "shell", "--tty=false", "--workdir=/", INSTANCE,
                     "sudo", "-n", "cat", f"/var/log/{name}"], check=False, timeout=10,
                )
                if result.stdout:
                    (logs / name).write_bytes(result.stdout)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass

    def remove(self, *, logs=None):
        errors = []
        commands = ("stop", "delete") if self.paths.instance.exists() else ()
        for command in commands:
            try:
                self.paths.verify_runtime()
                self.commands.run([self.lima, command, "--force", "--tty=false", INSTANCE],
                                  timeout=30)
            except BaseException as exc:
                errors.append(exc)
            if command == "stop" and logs is not None:
                try:
                    for path in self.paths.instance.glob("*.log"):
                        shutil.copy2(path, logs / path.name)
                except OSError as exc:
                    errors.append(exc)
        for path in dict.fromkeys((self.paths.runtime, self.root)):
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(exc)
        if errors:
            if logs is None:
                raise RuntimeError("Could not remove leftover Linux base builder") from errors[0]
            LOGGER.warning("Linux base builder cleanup failed: %s", errors[0])
