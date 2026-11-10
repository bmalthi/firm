"""Host cache and builder lifecycle, with all Lima calls faked."""

import json
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
import yaml

from firm_sandbox import linux_base
from firm_sandbox.lima_paths import LimaPaths
from firm_sandbox.linux_vm import GUEST_DEPENDENCY_CHECK
from firm_sandbox.locking import locked


@pytest.fixture
def cache():
    directory = linux_base.cache_directory()
    directory.mkdir(mode=0o700, parents=True)
    return directory


@pytest.fixture
def image(cache):
    path = cache / f"{linux_base.image_key()}.img"
    path.write_bytes(b"old image")
    return path


def expire(path):
    old = time.time() - (linux_base.MAX_AGE_DAYS + 1) * 86400
    os.utime(path, (old, old))


def test_key_tracks_both_assets(tmp_path):
    recipe, generalize = tmp_path / "recipe", tmp_path / "generalize"
    recipe.write_bytes(linux_base.RECIPE.read_bytes())
    generalize.write_bytes(linux_base.GENERALIZE.read_bytes())
    original = linux_base.image_key(recipe, generalize)
    assert original == linux_base.image_key()
    recipe.write_bytes(recipe.read_bytes() + b"\n# change\n")
    changed = linux_base.image_key(recipe, generalize)
    assert changed != original
    generalize.write_bytes(generalize.read_bytes() + b"\nsync\n")
    assert linux_base.image_key(recipe, generalize) not in (original, changed)


def test_fresh_image_reused(image, monkeypatch):
    monkeypatch.setattr(linux_base, "_build", lambda *a: pytest.fail("built fresh image"))
    assert linux_base.base_image("fake-limactl") == image


def test_old_image_still_used_not_rebuilt(image, monkeypatch):
    expire(image)
    monkeypatch.setattr(linux_base, "_build", lambda *a: pytest.fail("rebuilt an old image"))
    assert linux_base.base_image("fake-limactl") == image


def test_new_image_built_and_earlier_recipes_deleted(cache, monkeypatch):
    image = cache / f"{linux_base.image_key()}.img"
    earlier = cache / "earlier-key.img"
    earlier.write_bytes(b"earlier recipe")
    calls = []

    def build(lima, directory, destination, checkpoint):
        calls.append((lima, directory))
        destination.write_bytes(b"new image")

    monkeypatch.setattr(linux_base, "_build", build)
    assert linux_base.base_image("configured-limactl") == image
    assert calls == [("configured-limactl", cache)]
    assert image.read_bytes() == b"new image"
    assert not earlier.exists()


def test_failed_build_names_logs_and_keeps_other_images(cache, monkeypatch):
    earlier = cache / "earlier-key.img"
    earlier.write_bytes(b"earlier recipe")
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("download failed")

    monkeypatch.setattr(linux_base, "_build", fail)
    with pytest.raises(RuntimeError, match=r"download failed.*build-logs"):
        linux_base.base_image("fake-limactl")
    assert len(calls) == 1
    assert earlier.exists()


def test_lock_wait_checkpoints(cache):
    class Cancelled(Exception):
        pass

    calls = []

    def checkpoint():
        calls.append(True)
        if len(calls) == 3:
            raise Cancelled

    with locked(cache.parent, "linux-base.lock"):
        with pytest.raises(Cancelled):
            linux_base.base_image("fake-limactl", checkpoint)
    assert len(calls) == 3


@pytest.fixture
def lima(monkeypatch):
    state = SimpleNamespace(calls=[], running=False, statuses=[(2, b"status: done\n")],
                            fail=None, stop_fails=False, delete_fails=False,
                            created=0, starts=0, generalized=False)
    monkeypatch.setattr(LimaPaths, "mark_running", lambda self: None)
    monkeypatch.setattr(linux_base.time, "sleep", lambda _: None)

    def run(self, argv, **kwargs):
        assert argv[0] == "fake-limactl"
        assert all("proxy" not in key.lower() for key in self.env)
        state.calls.append((argv[1:], kwargs))
        command = argv[1]
        instance = linux_base.Builder(linux_base.cache_directory() / "builder",
                                      "fake-limactl", lambda: None).paths.instance
        if command == "create":
            assert not instance.exists()
            state.created += 1
            state.starts = 0
            instance.mkdir(mode=0o700, parents=True)
            (instance / "disk").write_bytes(b"new disk")
            (instance / "lima.yaml").write_text("provision: [{mode: system, script: install}]\n")
        if command == "edit":
            assert argv[2:] == ["--tty=false", "--set=.provision = []", "linux"]
            (instance / "lima.yaml").write_text("provision: []\n")
        if command == "start":
            (instance / "ha.stderr.log").write_text("Lima diagnostic")
            state.starts += 1
            if state.starts == 2:
                assert yaml.safe_load((instance / "lima.yaml").read_text())["provision"] == []
        if state.fail and state.fail(argv):
            raise RuntimeError("original build error")
        if command == "start":
            state.running = True
        if command == "stop":
            if state.stop_fails:
                raise RuntimeError("stop error")
            state.running = False
        if command == "delete":
            if state.delete_fails:
                raise RuntimeError("delete error")
            shutil.rmtree(instance)
        if command == "list":
            status = "Running" if state.running else "Stopped"
            return SimpleNamespace(stdout=json.dumps({"name": "linux", "status": status}).encode())
        if command == "shell":
            if argv[-1].startswith("/var/log/"):
                return SimpleNamespace(returncode=0, stdout=b"guest diagnostic\n")
            if argv[-1] == "status":
                # The last status repeats: each boot polls until done.
                statuses = state.statuses
                code, output = statuses.pop(0) if len(statuses) > 1 else statuses[0]
                return SimpleNamespace(returncode=code, stdout=output)
            if kwargs.get("input") == linux_base.GENERALIZE.read_bytes():
                state.generalized = True
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(linux_base.Commands, "run", run)
    return state


def test_build_reboots_generalizes_publishes_then_checkpoints(cache, lima):
    image = cache / f"{linux_base.image_key()}.img"
    published = []

    def checkpoint():
        # The list after the final stop has a checkpoint before confirming stopped.
        if lima.generalized and not lima.running:
            if not (cache / "builder").exists():
                assert image.read_bytes() == b"new disk"
                published.append(True)

    assert linux_base.base_image("fake-limactl", checkpoint) == image
    assert published == [True]
    assert image.read_bytes() == b"new disk"
    calls = [args for args, _ in lima.calls]
    assert sum(args[0] == "start" for args in calls) == 2
    create = next(args for args in calls if args[0] == "create")
    assert all(arg in create for arg in ("--cpus=4", "--memory=4", "--disk=20"))
    checks = [args for args in calls if args[-1] == GUEST_DEPENDENCY_CHECK]
    assert len(checks) == 1
    assert not (cache / "builder").exists()


def test_leftover_removed_without_booting(cache, lima):
    builder = linux_base.Builder(cache / "builder", "fake-limactl", lambda: None)
    builder.prepare()
    lima.calls.clear()
    lima.running = True
    linux_base._build("fake-limactl", cache, cache / "image.img", lambda: None)
    assert [args[0] for args, _ in lima.calls][:3] == ["stop", "delete", "create"]


@pytest.mark.parametrize("delete_fails", [False, True])
def test_build_error_survives_stop_failure_and_removes_builder(cache, lima, delete_fails):
    lima.fail = lambda args: args[1] == "start"
    lima.stop_fails = True
    lima.delete_fails = delete_fails
    with pytest.raises(RuntimeError, match="^original build error$"):
        linux_base._build("fake-limactl", cache, cache / "image.img", lambda: None)
    assert not (cache / "builder").exists()
    assert any(args[0] == "delete" for args, _ in lima.calls)
    logs = cache / "build-logs"
    assert (logs / "cloud-init.log").read_bytes() == b"guest diagnostic\n"
    assert (logs / "cloud-init-output.log").is_file()
    assert (logs / "ha.stderr.log").read_text() == "Lima diagnostic"


def test_cancellation_after_publish_keeps_image_without_retry(cache, lima):
    image = cache / f"{linux_base.image_key()}.img"

    def checkpoint():
        if image.exists():
            raise RuntimeError("creation cancelled")

    with pytest.raises(RuntimeError, match="creation cancelled"):
        linux_base.base_image("fake-limactl", checkpoint)
    assert image.read_bytes() == b"new disk"
    assert lima.created == 1 and not (cache / "builder").exists()
    assert linux_base.base_image("fake-limactl") == image


def test_poll_cloud_init_checkpoints_and_accepts_recoverable_warning(cache, lima):
    lima.statuses = [(0, b"status: running\n"), (2, b"status: done\n")]
    calls = []
    builder = linux_base.Builder(cache / "builder", "fake-limactl", lambda: calls.append(True))
    builder.wait_cloud_init()
    assert len(calls) >= 3
    status_calls = [kw for args, kw in lima.calls if args[-1] == "status"]
    assert len(status_calls) == 2 and all(kw["timeout"] <= 10 for kw in status_calls)


def test_cloud_init_error_and_overall_deadline_fail(cache, lima):
    builder = linux_base.Builder(cache / "builder", "fake-limactl", lambda: None)
    lima.statuses = [(2, b"status: error\n")]
    with pytest.raises(RuntimeError, match="cloud-init failed"):
        builder.wait_cloud_init()
    builder.deadline = time.monotonic() - 1
    with pytest.raises(RuntimeError, match="exceeded twenty minutes"):
        builder.run("start")


def test_base_recipe_installs_software_on_nat():
    config = yaml.safe_load(linux_base.RECIPE.read_text())
    assert config["networks"] == [{"vzNAT": True}]
    script = config["provision"][0]["script"]
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    subprocess.run(["bash", "-n", str(linux_base.GENERALIZE)], check=True)
