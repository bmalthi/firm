"""Dry checks for building a base Mac image, applying a recipe and sealing an enrolled one."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import mac_image as image
from firm_sandbox.compatibility import COMPATIBILITY_GENERATION, release_identifier
from firm_sandbox.mac_station import ASSETS, DIRTY_MARKER

# ---- building a base image -----------------------------------------------------------


RUNTIMES = {
    "runtimes": [
        {
            "platform": "iOS",
            "version": "26.4",
            "buildversion": "23E244",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-4",
            "isAvailable": True,
        },
        {
            "platform": "iOS",
            "version": "27.0",
            "buildversion": "24A335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
            "isAvailable": True,
        },
        {
            "platform": "iOS",
            "version": "27.1",
            "buildversion": "24B5",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-1",
            "isAvailable": False,
        },
        {
            "platform": "watchOS",
            "version": "27.0",
            "buildversion": "24R335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-27-0",
            "isAvailable": True,
        },
    ]
}


class FakeLima:
    """Records limactl calls and models the build instance's existence and status.

    ``stops`` is how many stop calls it takes to stop the running instance;
    ``None`` never stops it.
    """

    def __init__(self, *, fail_at: str | None = None, stops: int | None = 1):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail_at = fail_at
        self.stops = stops
        self.status = None

    def __call__(
        self,
        argv,
        *,
        input=None,
        stdout=None,
        stderr=None,
        capture_output=False,
        env=None,
        timeout=None,
        check=False,
    ):
        argv = list(argv)
        self.calls.append((argv, dict(env or {})))
        if self.fail_at and self.fail_at in argv:
            raise AssertionError(f"fake failure at {self.fail_at}")
        if argv[:2] == ["limactl", "create"]:
            (Path(env["LIMA_HOME"]) / "base").mkdir(parents=True)
            self.status = "Stopped"
        elif argv[:2] == ["limactl", "start"]:
            self.status = "Running"
        elif argv[:2] == ["limactl", "stop"] and self.stops is not None:
            self.stops -= 1
            if self.stops <= 0:
                self.status = "Stopped"
        if argv[:2] == ["limactl", "--version"]:
            output = b"limactl version 2.2.0\n"
        elif argv[-2:] == ["/usr/bin/sw_vers", "-buildVersion"]:
            output = b"25F84\n"
        elif argv[:3] == ["limactl", "list", "--json"]:
            output = json.dumps({"name": "base", "status": self.status}).encode()
        elif argv[-2:] == ["/opt/homebrew/bin/brew", "--version"]:
            output = f"Homebrew {image.HOMEBREW_TAG}\n".encode()
        else:
            output = b""
        return subprocess.CompletedProcess(argv, 0, output, b"")


@pytest.fixture(autouse=True)
def workspace_parent(tmp_path, monkeypatch):
    """Builds work under the test's directory, never the host's /private/tmp."""
    parent = tmp_path / "workspaces"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(image, "WORKSPACE_PARENT", parent)
    return parent


def test_builds_work_under_the_host_temporary_directory_by_default(monkeypatch):
    monkeypatch.undo()  # This one test checks the module default, before the fixture override.
    assert image.WORKSPACE_PARENT == image.TEMP_PARENT


def test_workspace_parent_is_validated_before_any_vm_work(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    """/private/tmp is world-writable, so WORKSPACE_PARENT must be real and owner-only."""
    uv, _ = inputs
    workspace_parent.chmod(0o777)
    fake = FakeLima()
    monkeypatch.setattr(image.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="Unsafe host state"):
        image.build_base_image(
            tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
        )
    assert fake.calls == []


@pytest.fixture
def inputs(tmp_path):
    uv = tmp_path / "uv"
    uv.write_bytes(b"uv")
    uv.chmod(0o755)
    app = tmp_path / "Xcode-beta.app"
    (app / "Contents" / "Developer" / "usr" / "bin").mkdir(parents=True)
    (app / "Contents" / "Developer" / "usr" / "bin" / "xcodebuild").write_bytes(b"xcodebuild")
    return uv, app


def test_build_creates_fresh_stopped_control_account_only_image(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    uv, app = inputs
    fake = FakeLima()
    monkeypatch.setattr(image.subprocess, "run", fake)

    recipe = image.Recipe(disk_gib=111, stations=2, applications=(app,))
    manifest = image.build_base_image(tmp_path / "image", recipe, uv_binary=uv)

    assert manifest == {
        "created_with_release": release_identifier(),
        "compatibility_generation": COMPATIBILITY_GENERATION,
        "image_id": manifest["image_id"],
        "name": "base",
        "stations": 2,
        "os_build": "25F84",
        "control_user": "firmvm",
        "python": "/usr/local/firm/bin/python3",
        "lima_version": "limactl version 2.2.0",
        "applications": ["Xcode-beta.app"],
        "homebrew": {"prefix": "/opt/homebrew", "tag": "7.0.3", "version": "7.0.3"},
    }
    calls = [argv for argv, _ in fake.calls]
    assert not any("-downloadPlatform" in " ".join(argv) for argv in calls)
    assert not any("simctl" in " ".join(argv) for argv in calls)
    assert calls[0] == ["limactl", "--version"]
    assert calls[1][:2] == ["limactl", "create"]
    assert "--mount-none" in calls[1] and "--plain" in calls[1]
    assert calls[2] == ["limactl", "start", "--tty=false", "base"]
    assert ["limactl", "copy", str(uv), "base:/Users/firmvm/uv"] in calls
    assert ["limactl", "copy", "-r", str(app), "base:/Applications/Xcode-beta.app"] in calls
    assert [
        "limactl",
        "shell",
        "--tty=false",
        "--workdir=/",
        "base",
        "/usr/bin/find",
        "/Applications/Xcode-beta.app",
        "!",
        "-type",
        "l",
        "-perm",
        "+022",
        "-exec",
        "/bin/chmod",
        "go-w",
        "{}",
        "+",
    ] in calls
    assert calls[-2] == ["limactl", "stop", "--tty=false", "base"]
    assert calls[-1] == ["limactl", "list", "--json", "base"]
    config = (tmp_path / "image" / "base.yaml").read_text()
    assert "disk: 111GiB" in config
    assert "mounts: []" in config
    assert "propagateProxyEnv: false" in config
    assert "loadDotSSHPubKeys: false" in config
    setup = (tmp_path / "image" / "setup.sh").read_text()
    assert "uv python install 3.12" in setup
    assert "UV_PYTHON_INSTALL_DIR=/usr/local/firm/python" in setup
    assert "PasswordAuthentication no" in setup
    assert "KbdInteractiveAuthentication no" in setup
    assert '[ "$(/usr/sbin/sysctl -n kern.hv_vmm_present)" = 1 ]' in setup
    assert "/bin/chmod 700 /Users/firmvm" in setup
    assert "/bin/chmod 600 /Users/firmvm/password" in setup
    assert 'bad_permissions=$(/usr/bin/find "$app_path" ! -type l -perm +022' in setup
    assert "/Users/firmvm/password" not in setup.splitlines()[-1]
    assert '"$xcodebuild" -license accept' in setup
    assert '"$xcodebuild" -runFirstLaunch' in setup
    assert json.loads((tmp_path / "image" / "image.json").read_text()) == manifest
    homes = {env["LIMA_HOME"] for _, env in fake.calls}
    assert len(homes) == 1
    assert homes.pop().startswith(f"{workspace_parent}/image-")
    # A published build leaves nothing behind in its workspace parent.
    assert list(workspace_parent.iterdir()) == []
    assert all(not any("proxy" in key.lower() for key in env) for _, env in fake.calls)


def test_build_installs_homebrew_as_the_control_account_and_publishes_it_root_owned(
    monkeypatch, tmp_path, inputs
):
    uv, app = inputs
    fake = FakeLima()
    monkeypatch.setattr(image.subprocess, "run", fake)
    image.build_base_image(
        tmp_path / "image",
        image.Recipe(disk_gib=64, stations=2, applications=(app,)),
        uv_binary=uv,
    )

    setup = (tmp_path / "image" / "setup.sh").read_text().splitlines()
    first_launch = setup.index('"$xcodebuild" -runFirstLaunch')
    prefix = setup.index("/usr/bin/install -d -o firmvm -g admin -m 755 /opt/homebrew")
    assert first_launch < prefix
    assert setup.index("[ ! -e /opt/homebrew ]") < prefix
    assert setup.index("[ ! -L /opt/homebrew ]") < prefix
    # Nothing system-wide puts the station-owned prefix on PATH.
    assert not any("paths.d" in line or line.startswith("/opt/homebrew") for line in setup)

    shell = ["limactl", "shell", "--tty=false", "--workdir=/", "base"]
    environment = ["/usr/bin/env", "HOMEBREW_NO_ANALYTICS=1", "HOMEBREW_NO_ENV_HINTS=1"]
    calls = [argv for argv, _ in fake.calls]
    brew = "/opt/homebrew/bin/brew"
    assert image.HOMEBREW_TAG == "7.0.3"
    # The pinned release, never updated at build, so a rebuild carries the same brew.
    assert not any("update" in argv for argv in calls)
    # A full clone at the tag: a shallow one tracks only the tag, so brew update cannot
    # resolve origin's default branch. The build confirms origin/HEAD is set.
    assert not any("--depth" in argv or "--single-branch" in argv for argv in calls)
    steps = [
        shell
        + environment
        + [
            "/usr/bin/git",
            "clone",
            "--quiet",
            "--branch",
            "7.0.3",
            "https://github.com/Homebrew/brew",
            "/opt/homebrew",
        ],
        shell
        + environment
        + [
            "/usr/bin/git",
            "-C",
            "/opt/homebrew",
            "symbolic-ref",
            "--quiet",
            "refs/remotes/origin/HEAD",
        ],
        shell + environment + [brew, "analytics", "off"],
        shell + environment + [brew, "--version"],
        shell
        + [
            "/bin/zsh",
            "-c",
            image.mac_sudo_command("/usr/sbin/chown", "-R", "-h", "root:wheel", "/opt/homebrew"),
        ],
    ]
    # In order, as the control account (no sudo) until the root-owned handoff, and
    # all after the root setup that created the empty prefix.
    indices = [calls.index(step) for step in steps]
    assert indices == sorted(indices)
    bootstrap = next(i for i, argv in enumerate(calls) if "/Users/firmvm/setup.sh" in argv[-1])
    assert bootstrap < indices[0]
    assert indices[-1] < calls.index(["limactl", "stop", "--tty=false", "base"])


def test_image_without_xcode_has_no_homebrew(monkeypatch, tmp_path, inputs):
    uv, _ = inputs
    fake = FakeLima()
    monkeypatch.setattr(image.subprocess, "run", fake)
    manifest = image.build_base_image(
        tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
    )
    assert manifest["homebrew"] is None
    assert "homebrew" not in (tmp_path / "image" / "setup.sh").read_text()
    assert not any("/opt/homebrew" in " ".join(argv) for argv, _ in fake.calls)


def test_build_refuses_when_homebrew_does_not_report_a_version(monkeypatch, tmp_path, inputs):
    uv, app = inputs
    fake = FakeLima()

    def run(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1] == "--version" and "brew" in argv[-2]:
            result.stdout = b"brew: command failed\n"
        return result

    monkeypatch.setattr(image.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Homebrew did not report its version"):
        image.build_base_image(
            tmp_path / "image",
            image.Recipe(disk_gib=64, stations=2, applications=(app,)),
            uv_binary=uv,
        )
    assert not (tmp_path / "image" / "image.json").exists()


@pytest.mark.parametrize(
    "reported",
    [
        "Homebrew 7.0.3",
        "Homebrew 7.0.4",
        "Homebrew 7.0.3-dirty",
        "Homebrew 7.0.3-12-gabcdef0",
    ],
)
def test_build_records_whatever_version_homebrew_reports_at_the_cloned_tag(
    monkeypatch, tmp_path, inputs, reported
):
    """The clone at HOMEBREW_TAG is the pin; brew --version need not echo it back
    verbatim (it can carry local commits or a differently formatted release), so the
    build records whatever version brew reported rather than requiring an exact match.
    """

    uv, app = inputs
    fake = FakeLima()

    def run(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1] == "--version" and "brew" in argv[-2]:
            result.stdout = f"{reported}\n".encode()
        return result

    monkeypatch.setattr(image.subprocess, "run", run)
    manifest = image.build_base_image(
        tmp_path / "image",
        image.Recipe(disk_gib=64, stations=2, applications=(app,)),
        uv_binary=uv,
    )
    assert manifest["homebrew"] == {
        "prefix": "/opt/homebrew",
        "tag": image.HOMEBREW_TAG,
        "version": reported.removeprefix("Homebrew ").strip(),
    }
    assert json.loads((tmp_path / "image" / "image.json").read_text()) == manifest


def test_build_ignores_recipe_simulators_and_downloads_no_runtime(monkeypatch, tmp_path, inputs):
    """Runtimes belong to mac-image-apply now; build only ever reads disk_gib and applications."""
    uv, app = inputs
    fake = FakeLima()
    monkeypatch.setattr(image.subprocess, "run", fake)
    recipe = image.Recipe(
        disk_gib=64,
        stations=2,
        applications=(app,),
        simulators=(image.Simulator("iOS"), image.Simulator("watchOS")),
    )

    manifest = image.build_base_image(tmp_path / "image", recipe, uv_binary=uv)

    assert "simulators" not in manifest
    calls = [argv for argv, _ in fake.calls]
    assert not any("-downloadPlatform" in " ".join(argv) for argv in calls)
    assert not any("simctl" in " ".join(argv) for argv in calls)


STOP = ["limactl", "stop", "--tty=false", "base"]
FORCE_STOP = ["limactl", "stop", "--force", "base"]


def test_failed_build_stops_and_removes_its_vm_and_the_destination_it_created(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    uv, _ = inputs
    fake = FakeLima(fail_at="/usr/bin/sw_vers")
    monkeypatch.setattr(image.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="build VM was stopped and removed: fake failure"):
        image.build_base_image(
            tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
        )

    calls = [argv for argv, _ in fake.calls]
    assert calls[-2:] == [STOP, ["limactl", "list", "--json", "base"]]
    assert FORCE_STOP not in calls
    assert list(workspace_parent.iterdir()) == []
    assert not (tmp_path / "image").exists()


def test_failure_before_the_vm_exists_removes_the_workspace_without_stopping(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    uv, _ = inputs
    fake = FakeLima(fail_at="create")
    monkeypatch.setattr(image.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="stopped and removed"):
        image.build_base_image(
            tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
        )
    assert not any(argv[:2] == ["limactl", "stop"] for argv, _ in fake.calls)
    assert list(workspace_parent.iterdir()) == []
    assert not (tmp_path / "image").exists()


def test_interrupted_build_stops_and_removes_its_vm(
    monkeypatch, tmp_path, inputs, workspace_parent, capsys
):
    uv, app = inputs
    fake = FakeLima()

    def run(argv, **kwargs):
        if "/Users/firmvm/setup.sh" in argv[-1]:
            fake.calls.append((list(argv), dict(kwargs["env"])))
            raise KeyboardInterrupt
        return fake(argv, **kwargs)

    monkeypatch.setattr(image.subprocess, "run", run)
    with pytest.raises(KeyboardInterrupt):
        image.build_base_image(
            tmp_path / "image",
            image.Recipe(disk_gib=64, stations=2, applications=(app,)),
            uv_binary=uv,
        )

    calls = [argv for argv, _ in fake.calls]
    interrupted = next(i for i, argv in enumerate(calls) if "/Users/firmvm/setup.sh" in argv[-1])
    assert calls[interrupted + 1] == STOP
    assert fake.status == "Stopped"
    assert list(workspace_parent.iterdir()) == []
    assert not (tmp_path / "image").exists()
    assert "interrupted; its build VM was stopped and removed" in capsys.readouterr().err


def test_failed_build_forces_a_stuck_vm_and_keeps_an_owner_created_destination(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    uv, _ = inputs
    destination = tmp_path / "image"
    destination.mkdir()
    fake = FakeLima(fail_at="/usr/bin/sw_vers", stops=2)
    monkeypatch.setattr(image.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="stopped and removed"):
        image.build_base_image(destination, image.Recipe(disk_gib=64, stations=2), uv_binary=uv)

    calls = [argv for argv, _ in fake.calls]
    assert calls.index(STOP) < calls.index(FORCE_STOP)
    assert list(workspace_parent.iterdir()) == []
    assert destination.is_dir() and list(destination.iterdir()) == []


def test_failed_build_keeps_a_vm_it_cannot_confirm_stopped(
    monkeypatch, tmp_path, inputs, workspace_parent
):
    uv, _ = inputs
    fake = FakeLima(fail_at="/usr/bin/sw_vers", stops=None)
    monkeypatch.setattr(image.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match=f"recovery files remain at {workspace_parent}/image-"):
        image.build_base_image(
            tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
        )

    assert FORCE_STOP in [argv for argv, _ in fake.calls]
    [workspace] = workspace_parent.iterdir()
    assert (workspace / "lima" / "base").is_dir()
    assert not (tmp_path / "image").exists()


def test_refuses_nonempty_or_top_level_symlink_inputs(tmp_path, inputs):
    uv, app = inputs
    destination = tmp_path / "image"
    destination.mkdir()
    (destination / "existing").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        image.build_base_image(destination, image.Recipe(disk_gib=64, stations=2), uv_binary=uv)

    symlink = tmp_path / "app-link.app"
    symlink.symlink_to(app, target_is_directory=True)
    with pytest.raises(ValueError, match="top-level symlink"):
        image.build_base_image(
            tmp_path / "new-image",
            image.Recipe(disk_gib=64, stations=2, applications=(symlink,)),
            uv_binary=uv,
        )


def test_lima_release_is_recorded_without_an_exact_pin(monkeypatch, tmp_path, inputs):
    uv, _ = inputs
    fake = FakeLima()

    def run(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[:2] == ["limactl", "--version"]:
            result.stdout = b"limactl version 9.0.0\n"
        return result

    monkeypatch.setattr(image.subprocess, "run", run)
    manifest = image.build_base_image(
        tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv
    )
    assert manifest["lima_version"] == "limactl version 9.0.0"


# ---- the image recipe ------------------------------------------------------------------


def test_recipe_parses_disk_applications_and_simulators(tmp_path):
    recipe_path = tmp_path / "ios.toml"
    recipe_path.write_text(
        "disk_gib = 192\n"
        "stations = 2\n"
        "[[application]]\n"
        'path = "/Applications/Xcode.app"\n'
        "[[simulator]]\n"
        'platform = "iOS"\n'
        "[[simulator]]\n"
        'platform = "iOS"\n'
        'version = "26.0"\n'
        "[[simulator]]\n"
        'platform = "watchOS"\n'
    )

    recipe = image.Recipe.load(recipe_path)

    assert recipe.disk_gib == 192
    assert recipe.stations == 2
    assert recipe.applications == (Path("/Applications/Xcode.app"),)
    assert recipe.simulators == (
        image.Simulator("iOS"),
        image.Simulator("iOS", "26.0"),
        image.Simulator("watchOS"),
    )
    assert recipe.sha256 == hashlib.sha256(recipe_path.read_bytes()).hexdigest()


def test_repository_recipe_parses_and_names_xcode_and_three_runtimes():
    """The checked-in images/ios.toml this stage adds."""
    path = Path(image.__file__).resolve().parents[4] / "images" / "ios.toml"
    recipe = image.Recipe.load(path)
    assert recipe.disk_gib == 192
    assert recipe.stations == 2
    assert recipe.applications == (Path("/Applications/Xcode.app"),)
    assert recipe.simulators == (
        image.Simulator("iOS"),
        image.Simulator("iOS", "26.0"),
        image.Simulator("watchOS"),
    )


def test_recipe_refuses_an_unknown_top_level_section(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text("disk_gib = 64\nstations = 2\nextra = true\n")
    with pytest.raises(ValueError, match="Unknown recipe setting: extra"):
        image.Recipe.load(path)


def test_recipe_requires_disk_gib(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text('stations = 2\n[[application]]\npath = "/Applications/Xcode.app"\n')
    with pytest.raises(ValueError, match="must set disk_gib"):
        image.Recipe.load(path)


def test_recipe_requires_stations(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text("disk_gib = 64\n")
    with pytest.raises(ValueError, match="must set stations"):
        image.Recipe.load(path)


@pytest.mark.parametrize("stations", [1, 8])
def test_recipe_accepts_station_count_boundaries(tmp_path, stations):
    path = tmp_path / "r.toml"
    path.write_text(f"disk_gib = 64\nstations = {stations}\n")
    assert image.Recipe.load(path).stations == stations


@pytest.mark.parametrize("bad", ["0", "9", "true", "2.0", '"2"'])
def test_recipe_refuses_a_station_count_outside_one_through_eight(tmp_path, bad):
    path = tmp_path / "r.toml"
    path.write_text(f"disk_gib = 64\nstations = {bad}\n")
    with pytest.raises(ValueError, match="stations"):
        image.Recipe.load(path)


@pytest.mark.parametrize("bad", ["64.0", "true", "0", "-1", "4096"])
def test_recipe_refuses_a_non_positive_or_non_integer_disk_size(tmp_path, bad):
    path = tmp_path / "r.toml"
    path.write_text(f"disk_gib = {bad}\nstations = 2\n")
    with pytest.raises(ValueError, match="disk_gib"):
        image.Recipe.load(path)


def test_recipe_refuses_an_application_entry_with_the_wrong_shape(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text(
        "disk_gib = 64\nstations = 2\n"
        '[[application]]\npath = "/Applications/Xcode.app"\nextra = 1\n'
    )
    with pytest.raises(ValueError, match=r"\[\[application\]\]"):
        image.Recipe.load(path)


def test_recipe_refuses_a_relative_or_non_app_application_path():
    with pytest.raises(ValueError, match="absolute .app bundle"):
        image.Recipe(disk_gib=64, stations=2, applications=(Path("Xcode.app"),))
    with pytest.raises(ValueError, match="absolute .app bundle"):
        image.Recipe(disk_gib=64, stations=2, applications=(Path("/Applications/Xcode"),))


def test_recipe_refuses_duplicate_application_names():
    with pytest.raises(ValueError, match="unique"):
        image.Recipe(
            disk_gib=64,
            stations=2,
            applications=(Path("/Applications/Xcode.app"), Path("/other/Xcode.app")),
        )


def test_recipe_refuses_an_unknown_simulator_platform(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text('disk_gib = 64\nstations = 2\n[[simulator]]\nplatform = "macOS"\n')
    with pytest.raises(ValueError, match="Unknown simulator platform 'macOS'"):
        image.Recipe.load(path)


def test_recipe_refuses_a_malformed_simulator_version():
    with pytest.raises(ValueError, match="Invalid simulator version"):
        image.Simulator("iOS", "current")


def test_recipe_refuses_a_simulator_entry_missing_platform(tmp_path):
    path = tmp_path / "r.toml"
    path.write_text('disk_gib = 64\nstations = 2\n[[simulator]]\nversion = "26.0"\n')
    with pytest.raises(ValueError, match=r"\[\[simulator\]\]"):
        image.Recipe.load(path)


def test_recipe_refuses_duplicate_simulators():
    with pytest.raises(ValueError, match="unique"):
        image.Recipe(
            disk_gib=64,
            stations=2,
            simulators=(image.Simulator("iOS"), image.Simulator("iOS")),
        )


def test_recipe_built_directly_leaves_sha256_none():
    assert image.Recipe(disk_gib=64, stations=2).sha256 is None


# ---- simulator runtime inspection -------------------------------------------------------


def _available(platform, version):
    return {
        "platform": platform,
        "version": version,
        "buildversion": "1",
        "identifier": "x",
        "isAvailable": True,
    }


def test_a_pinned_simulator_needs_exactly_that_available_version():
    runtimes = image._runtime_listing(json.dumps(RUNTIMES))
    present, unavailable, absent, other = (
        image.Simulator("iOS", "26.4"),
        image.Simulator("iOS", "27.1"),
        image.Simulator("iOS", "9.9"),
        image.Simulator("tvOS", "27.0"),
    )
    assert image.missing_simulators((present, unavailable, absent, other), runtimes, {}) == [
        unavailable,
        absent,
        other,
    ]


def test_a_version_is_met_by_itself_or_a_patch_of_it_and_by_nothing_else():
    pinned = (image.Simulator("iOS", "26.0"),)
    for version, met in (
        ("26.0", True),
        ("26.0.1", True),
        ("26.4", False),
        ("26.01", False),
        ("126.0", False),
        ("2.60", False),
    ):
        assert (image.missing_simulators(pinned, [_available("iOS", version)], {}) == []) is met
    assert image.missing_simulators(pinned, [_available("watchOS", "26.0")], {}) == list(pinned)


def test_an_entry_without_a_version_stands_for_this_xcodes_sdk_runtime():
    """An older pinned runtime, or any other, never stands in for the current one."""
    current, older = image.Simulator("iOS"), image.Simulator("iOS", "26.0")
    defaults = {"iOS": "27.0"}
    assert image.missing_simulators((current, older), [_available("iOS", "26.0")], defaults) == [
        current
    ]
    # Not by exclusion either: 26.4 is no pinned entry's runtime, and still not 27.0.
    others = [_available("iOS", "26.0"), _available("iOS", "26.4")]
    assert image.missing_simulators((current, older), others, defaults) == [current]
    both = [_available("iOS", "26.0"), _available("iOS", "27.0")]
    assert image.missing_simulators((current, older), both, defaults) == []
    # Pinning the current version beside the entry without one is met by the one runtime.
    same = (current, image.Simulator("iOS", "27.0"))
    assert image.missing_simulators(same, [_available("iOS", "27.0")], defaults) == []


@pytest.mark.parametrize("version", ["26", "current", "26.", "26.0.1.2", "23A343", " 26.0", ""])
def test_a_pinned_version_is_written_as_simctl_reports_it(version):
    """ "26" would never equal simctl's "26.0", so apply would reinstall and seal refuse."""
    with pytest.raises(ValueError, match="Invalid simulator version"):
        image.Simulator("iOS", version)


def test_runtime_listing_refuses_invalid_simctl_output():
    with pytest.raises(RuntimeError, match="runtime listing"):
        image._runtime_listing("not json")
    with pytest.raises(RuntimeError, match="runtime listing"):
        image._runtime_listing(json.dumps({"runtimes": "not a list"}))


def test_every_available_runtime_records_every_platform_and_version():
    runtimes = image._runtime_listing(json.dumps(RUNTIMES))
    assert image.every_available_runtime(runtimes) == [
        {
            "platform": "iOS",
            "version": "26.4",
            "build": "23E244",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-4",
        },
        {
            "platform": "iOS",
            "version": "27.0",
            "build": "24A335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
        },
        {
            "platform": "watchOS",
            "version": "27.0",
            "build": "24R335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-27-0",
        },
    ]


# ---- applying a recipe's runtimes to a running Mac VM -----------------------------------


LISTING = ("_shell", ("/usr/bin/xcrun", "simctl", "list", "runtimes", "-j"))
XCODE_APP = (Path("/Applications/Xcode.app"),)


def _sdk_query(platform):
    sdk = image.SIMULATOR_SDKS[platform]
    return ("_shell", ("/usr/bin/xcrun", "--sdk", sdk, "--show-sdk-version"))


def _guest_reply(vm, args):
    """What the control account's two read-only xcrun calls answer, or None for any other."""
    if args[:4] == ("/usr/bin/xcrun", "simctl", "list", "runtimes"):
        return SimpleNamespace(stdout=json.dumps({"runtimes": vm.runtimes}).encode())
    if args[:2] == ("/usr/bin/xcrun", "--sdk"):
        return SimpleNamespace(stdout=vm.sdk_version.encode() + b"\n")
    return None


def recipe_with(*simulators):
    return image.Recipe(disk_gib=64, stations=2, applications=XCODE_APP, simulators=simulators)


class FakeApplyVM:
    """A running Mac VM: unprivileged xcrun reads, then root installs via copy-and-run."""

    def __init__(self, root, *, runtimes=(), installs=True, sdk_version="27.0"):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stations = (1, 2)
        self.firm_locks = root.parent / "locks"
        self.network = SimpleNamespace(ready=lambda: True)
        self.running = True
        self.runtimes = list(runtimes)
        self.installs = installs
        self.sdk_version = sdk_version
        self.calls = []

    def _running(self):
        return self.running

    def _lima(self, *args, **kwargs):
        self.calls.append(("_lima", args))

    def _shell(self, *args, **kwargs):
        self.calls.append(("_shell", args))
        reply = _guest_reply(self, args)
        if reply is not None:
            return reply
        if args[:2] == ("/bin/zsh", "-c") and self.installs:
            # "... <script> PLATFORM [VERSION] < /Users/firmvm/password"
            platform, *version = args[2].split(" < ")[0].split(".py ")[1].split()
            self.runtimes.append(_available(platform, version[0] if version else self.sdk_version))
        return SimpleNamespace(stdout=b"")


def _install_commands(vm):
    return [
        call[1][2] for call in vm.calls if call[0] == "_shell" and call[1][:2] == ("/bin/zsh", "-c")
    ]


def test_apply_installs_only_the_runtimes_the_guest_lacks(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac", runtimes=[_available("iOS", "27.0")])
    recipe = recipe_with(image.Simulator("iOS"), image.Simulator("watchOS"))

    result = image.apply_recipe(vm, recipe)

    # What an entry without a version stood for in this guest is reported.
    assert result == {"installed": [{"platform": "watchOS", "version": "27.0"}]}
    copies = [call for call in vm.calls if call[0] == "_lima"]
    assert len(copies) == 1
    # The station's own platform installer: one install implementation.
    assert copies[0][1][:2] == ("copy", str(ASSETS / "mac-station-add-platform.py"))
    installs = _install_commands(vm)
    assert len(installs) == 1
    # The installer never inherits the password file sudo read: its stdin is /dev/null.
    assert installs[0].startswith(
        "/usr/bin/sudo -k -S -p '' /bin/sh -c 'exec \"$@\" 0</dev/null' sh "
        "/usr/local/firm/bin/python3 "
    )
    # Xcode chooses its default itself: no version is passed for an entry without one.
    assert installs[0].endswith(" watchOS < /Users/firmvm/password")
    removals = [call for call in vm.calls if call[0] == "_shell" and call[1][0] == "/bin/rm"]
    assert len(removals) == 1
    # Each SDK asked once, the runtimes listed before deciding and again after installing.
    assert vm.calls[:3] == [_sdk_query("iOS"), _sdk_query("watchOS"), LISTING]
    assert vm.calls[-1] == LISTING


def test_apply_adds_the_current_runtime_beside_older_ones(tmp_path):
    vm = FakeApplyVM(
        tmp_path / "mac", runtimes=[_available("iOS", "26.0"), _available("iOS", "26.4")]
    )
    recipe = recipe_with(image.Simulator("iOS"), image.Simulator("iOS", "26.0"))

    assert image.apply_recipe(vm, recipe) == {"installed": [{"platform": "iOS", "version": "27.0"}]}


def test_apply_fails_when_an_install_leaves_its_runtime_missing(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac", installs=False)

    with pytest.raises(RuntimeError, match="still lacks the iOS 26.0 simulator runtime"):
        image.apply_recipe(vm, recipe_with(image.Simulator("iOS", "26.0")))
    # The copied installer is removed whatever happened.
    assert [call for call in vm.calls if call[1][0] == "/bin/rm"]


def test_apply_refuses_when_xcode_does_not_report_its_sdk(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac", sdk_version="error: SDK not found")

    with pytest.raises(RuntimeError, match="did not report its iOS SDK version"):
        image.apply_recipe(vm, recipe_with(image.Simulator("iOS")))
    assert _install_commands(vm) == []


def test_apply_is_a_no_op_when_every_runtime_is_already_present(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac", runtimes=[_available("iOS", "27.0")])

    result = image.apply_recipe(vm, recipe_with(image.Simulator("iOS")))

    assert result == {"installed": []}
    # Only the two reads; nothing copied, installed or cleaned up.
    assert vm.calls == [_sdk_query("iOS"), LISTING]


def test_apply_with_no_recipe_simulators_never_reaches_the_guest(tmp_path):
    """An image without Xcode has no simctl to ask, and nothing to install."""
    vm = FakeApplyVM(tmp_path / "mac")

    assert image.apply_recipe(vm, image.Recipe(disk_gib=64, stations=2)) == {"installed": []}
    assert vm.calls == []


def test_a_recipe_cannot_name_simulators_without_xcode():
    with pytest.raises(ValueError, match="need Xcode"):
        image.Recipe(disk_gib=64, stations=2, simulators=(image.Simulator("iOS"),))
    with pytest.raises(ValueError, match="need Xcode"):
        image.Recipe(
            disk_gib=64,
            stations=2,
            applications=(Path("/Applications/Safari.app"),),
            simulators=(image.Simulator("iOS"),),
        )


def test_apply_requests_a_pinned_version(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac", runtimes=[])

    result = image.apply_recipe(vm, recipe_with(image.Simulator("iOS", "26.0")))

    assert result == {"installed": [{"platform": "iOS", "version": "26.0"}]}
    installs = _install_commands(vm)
    assert installs[0].endswith(" iOS 26.0 < /Users/firmvm/password")
    # Every entry is pinned, so no SDK is asked about.
    assert vm.calls[0] == LISTING


def test_apply_refuses_when_the_vm_is_not_running(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac")
    vm.running = False
    with pytest.raises(RuntimeError, match="Start the isolated Mac VM"):
        image.apply_recipe(vm, recipe_with(image.Simulator("iOS")))
    assert vm.calls == []


def test_apply_refuses_unverified_cleanup(tmp_path):
    vm = FakeApplyVM(tmp_path / "mac")
    dirty = vm.root / "stations/2" / DIRTY_MARKER
    dirty.parent.mkdir(parents=True)
    dirty.write_text("unverified")
    with pytest.raises(RuntimeError, match="unverified cleanup"):
        image.apply_recipe(vm, recipe_with(image.Simulator("iOS")))
    assert vm.calls == []


# ---- sealing an enrolled image ---------------------------------------------------------


class FakeSandbox:
    def __init__(self, vm):
        self.mac = vm
        self.root = vm.root
        self.firm_locks = vm.firm_locks

    def mac_station(self, station):
        raise AssertionError("the station fixture replaces this")

    def down(self):
        self.mac.calls.append("down")
        self.mac.running = False

    def _station_locks(self):
        return nullcontext()


class FakeVM:
    def __init__(self, root):
        self.root = root
        self.stations = (1, 2)
        self.firm_locks = root.parent / "locks"
        root.mkdir(mode=0o700)
        self.home = root / "lima"
        self.instance = self.home / "mac"
        self.instance.mkdir(parents=True)
        keys = self.home / "_config"
        keys.mkdir()
        for name in (
            "disk",
            "disk.patched",
            "vz-aux",
            "vz-hwmodel",
            "vz-identifier",
            "lima-version",
            "lima.yaml",
        ):
            (self.instance / name).write_text("synthetic-" + name)
        # The size the base was built with, as MacVM._clone carries it into every clone.
        (self.instance / "lima.yaml").write_text("os: Darwin\ndisk: 64GiB\n")
        for name in ("user", "user.pub"):
            (keys / name).write_text("synthetic-control-key")
            (keys / name).chmod(0o600)
        self.network = SimpleNamespace(ready=lambda: True)
        self.commands = SimpleNamespace(env={}, run=self.copy)
        self.lima = "/fake/limactl"
        self.running = True
        self.rejection = None
        self.calls = []
        # What the guest inspection reports back; tests override pieces of it.
        self.checked = {
            "os_build": "test-build",
            "enrollment": {
                "stations": 2,
                "control_uid": 501,
                "environment": "vm-session",
                "designated_requirement": "certificate leaf = H'123'",
            },
            "applications": [
                {"name": "Xcode-beta.app", "bundle_id": "com.apple.dt.Xcode", "version": "16.0"},
            ],
            "xcode": {"version": "16.0", "build": "16A242d"},
            "homebrew": {"prefix": "/opt/homebrew", "version": "4.9.2"},
        }
        # What xcrun answers the control account; the root script never reports either.
        self.runtimes = [
            {
                "platform": "iOS",
                "version": "27.0",
                "buildversion": "24A335",
                "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                "isAvailable": True,
            },
        ]
        self.sdk_version = "27.0"
        # A clone of the base image whose mac-prepare turns are all done.
        self.state = {"enrolled": False, "enrollment_progress": {"phase": "complete"}}

    def _state(self):
        return self.state

    def enrolled(self):
        return self.state["enrolled"]

    def _running(self):
        return self.running

    def _lima(self, *args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(stdout=b"limactl version 2.2.0\n")

    def _shell(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "/bin/rm":
            return SimpleNamespace(stdout=b"")
        reply = _guest_reply(self, args)
        if reply is not None:
            return reply
        if self.rejection:
            raise RuntimeError(self.rejection)
        return SimpleNamespace(stdout=json.dumps(self.checked).encode())

    def copy(self, argv, **kwargs):
        assert self.running is False, "Never copy disks while the source VM is running"
        assert argv[:2] == ["/bin/cp", "-c"]
        shutil.copyfile(argv[2], argv[3])


@pytest.fixture
def station(monkeypatch):
    states = {
        number: {
            "ready": True,
            "clean": True,
            "lease_bound": False,
            "environment": "vm-session",
        }
        for number in (1, 2)
    }

    def mac_station(self, station):
        client = SimpleNamespace(request=lambda op: states[station], close=lambda: None)
        root = self.mac.root / "stations" / str(station)
        root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(vm=self.mac, root=root, probe=lambda: client)

    monkeypatch.setattr(FakeSandbox, "mac_station", mac_station)
    monkeypatch.setattr(
        image,
        "Commands",
        lambda env: SimpleNamespace(
            run=lambda argv: SimpleNamespace(stdout=b'{"name":"base","status":"Stopped"}\n')
        ),
    )
    return states


RECIPE = image.Recipe(disk_gib=64, stations=2, applications=(Path("/Applications/Xcode-beta.app"),))


def test_sealed_image_is_stopped_cloneable_and_excludes_host_state(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    (vm.root / "never-export-this-secret").write_text("synthetic private owner state")
    destination = tmp_path / "image"
    result = image.seal_enrolled_image(FakeSandbox(vm), destination, RECIPE)
    assert result["enrollment"]["stations"] == 2
    assert json.loads((destination / "image.json").read_text()) == result
    assert vm.running is False
    assert not (destination / "never-export-this-secret").exists()
    assert sorted(path.name for path in destination.iterdir()) == ["image.json", "lima"]
    assert (destination / "lima" / "_config" / "user").stat().st_mode & 0o777 == 0o600
    # The recipe's application names reach the guest script as a JSON argument.
    asked, sealed = [call for call in vm.calls if call[0] == "/bin/zsh"]
    # First only whether an install is running, before the runtimes are listed.
    assert asked[2].split(" < ")[0].endswith(" " + seal_module().NO_INSTALL_RUNNING)
    assert (
        vm.calls.index(asked)
        < vm.calls.index(("/usr/bin/xcrun", "simctl", "list", "runtimes", "-j"))
        < vm.calls.index(sealed)
    )
    assert '"applications": ["Xcode-beta.app"]' in sealed[2]
    assert '"stations": 2' in sealed[2]


def test_sealed_image_carries_the_recipe_applications_xcode_and_homebrew(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    result = image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert result["applications"] == [
        {"name": "Xcode-beta.app", "bundle_id": "com.apple.dt.Xcode", "version": "16.0"},
    ]
    assert result["xcode"] == {"version": "16.0", "build": "16A242d"}
    assert result["homebrew"] == {"prefix": "/opt/homebrew", "version": "4.9.2"}
    assert result.keys() >= {"name", "control_user", "python", "os_build", "lima_version"}


def test_seal_records_every_available_runtime_and_the_recipe_with_its_hash(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    vm.runtimes = [
        {
            "platform": "iOS",
            "version": "26.0",
            "buildversion": "23A343",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-0",
            "isAvailable": True,
        },
        {
            "platform": "iOS",
            "version": "27.0",
            "buildversion": "24A335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
            "isAvailable": True,
        },
        {
            "platform": "watchOS",
            "version": "27.1",
            "buildversion": "24R5",
            "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-27-1",
            "isAvailable": False,
        },
    ]
    recipe = image.Recipe(
        disk_gib=64,
        stations=2,
        applications=(Path("/Applications/Xcode-beta.app"),),
        simulators=(image.Simulator("iOS"), image.Simulator("iOS", "26.0")),
        sha256="deadbeef",
    )

    result = image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", recipe)

    # Both iOS versions are recorded; the unavailable watchOS runtime is not.
    assert result["simulators"] == [
        {
            "platform": "iOS",
            "version": "26.0",
            "build": "23A343",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-0",
        },
        {
            "platform": "iOS",
            "version": "27.0",
            "build": "24A335",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
        },
    ]
    assert result["recipe"] == {
        "disk_gib": 64,
        "stations": 2,
        "applications": ["Xcode-beta.app"],
        "simulators": [
            {"platform": "iOS", "version": None},
            {"platform": "iOS", "version": "26.0"},
        ],
        "sha256": "deadbeef",
    }


@pytest.mark.parametrize("progress", [None, {"phase": "console"}, {"phase": "selecting"}])
def test_a_base_image_clone_must_finish_its_console_turns_before_sealing(
    tmp_path, station, progress
):
    vm = FakeVM(tmp_path / "source")
    vm.state = {"enrolled": False, "enrollment_progress": progress}
    with pytest.raises(RuntimeError, match="Finish every mac-prepare console turn"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.calls == [] and vm.running is True
    assert not (tmp_path / "image").exists()


def test_an_enrolled_image_clone_seals_without_progress_of_its_own(tmp_path, station):
    """Refreshing an image: its accounts were enrolled before it was sealed."""
    vm = FakeVM(tmp_path / "source")
    vm.state = {"enrolled": True}
    result = image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert result["enrollment"]["stations"] == 2
    # The guest's seal script still ran and verified the accounts as they are now.
    sealed = [call for call in vm.calls if call[0] == "/bin/zsh"][-1]
    assert '"stations": 2' in sealed[2]


def test_an_enrolled_image_clone_still_needs_every_station_released(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    vm.state = {"enrolled": True}
    station[1]["lease_bound"] = True
    with pytest.raises(RuntimeError, match="station 1"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is True
    assert not (tmp_path / "image").exists()


def test_seal_refuses_a_guest_missing_a_named_runtime(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    recipe = image.Recipe(
        disk_gib=64,
        stations=2,
        applications=(Path("/Applications/Xcode-beta.app"),),
        simulators=(image.Simulator("iOS", "26.0"),),
    )
    with pytest.raises(RuntimeError, match="lacks the iOS 26.0 simulator runtime"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", recipe)
    assert vm.running is True
    assert not (tmp_path / "image").exists()
    # Refused before the seal script ran for real: only its question about installs did,
    # which changes nothing, and the copied script was removed.
    asked = [call for call in vm.calls if call[0] == "/bin/zsh"]
    assert len(asked) == 1 and "--no-install-running" in asked[0][2]
    assert vm.calls[-1][0] == "/bin/rm"


def test_seal_refuses_a_disk_of_another_size_than_the_recipe_names(tmp_path, station):
    """A clone of a 64 GiB base cannot be published under a recipe that names 192."""
    vm = FakeVM(tmp_path / "source")
    recipe = image.Recipe(
        disk_gib=192, stations=2, applications=(Path("/Applications/Xcode-beta.app"),)
    )
    with pytest.raises(RuntimeError, match="disk is 64GiB and the recipe names 192GiB"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", recipe)
    # Refused on the host's own record, before anything reached the guest.
    assert vm.calls == [] and vm.running is True
    assert not (tmp_path / "image").exists()


def test_seal_refuses_while_an_install_outlives_its_apply(tmp_path, station):
    """A lost mac-image-apply leaves its bounded install running; seal asks before it lists."""
    vm = FakeVM(tmp_path / "source")
    vm.rejection = "A simulator platform install is still running; wait for it before sealing"
    with pytest.raises(RuntimeError, match="install is still running"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is True and not (tmp_path / "image").exists()
    assert not [call for call in vm.calls if call[0] == "/usr/bin/xcrun"]
    assert vm.calls[-1][0] == "/bin/rm"


def test_seal_refuses_older_runtimes_standing_in_for_this_xcodes_own(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    vm.runtimes = [_available("iOS", "26.0"), _available("iOS", "26.4")]
    recipe = image.Recipe(
        disk_gib=64,
        stations=2,
        applications=(Path("/Applications/Xcode-beta.app"),),
        simulators=(image.Simulator("iOS"), image.Simulator("iOS", "26.0")),
    )
    with pytest.raises(RuntimeError, match="lacks the iOS 27.0 simulator runtime"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", recipe)


def test_an_image_without_xcode_seals_without_asking_for_runtimes(tmp_path, station):
    """simctl is Xcode's; an image built without it is still a valid image."""
    vm = FakeVM(tmp_path / "source")
    vm.checked.update(applications=[], xcode=None, homebrew=None)
    result = image.seal_enrolled_image(
        FakeSandbox(vm), tmp_path / "image", image.Recipe(disk_gib=64, stations=2)
    )
    assert result["simulators"] == [] and result["xcode"] is None
    assert not [call for call in vm.calls if call[0] == "/usr/bin/xcrun"]


def test_missing_consent_does_not_stop_or_publish(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    station[2]["ready"] = False
    with pytest.raises(RuntimeError, match="station 2"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is True
    assert not (tmp_path / "image").exists()


def test_guest_refusal_keeps_the_source_running_and_publishes_nothing(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    vm.rejection = "Image has additional ordinary accounts; create a fresh enrollment VM"
    with pytest.raises(RuntimeError, match="additional ordinary accounts"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is True
    assert not (tmp_path / "image").exists()
    assert any(call[0] == "/bin/rm" for call in vm.calls if isinstance(call, tuple))


def test_unsafe_disk_asset_never_publishes_manifest(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    disk = vm.instance / "disk"
    disk.unlink()
    disk.symlink_to(vm.instance / "disk.patched")
    with pytest.raises(ValueError, match="owner-controlled regular"):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is False
    assert not (tmp_path / "image").exists()


def test_interrupted_seal_removes_the_partial_image(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    copy = vm.copy

    def interrupt(argv, **kwargs):
        if argv[2].endswith("vz-aux"):
            raise KeyboardInterrupt
        return copy(argv, **kwargs)

    vm.commands.run = interrupt
    with pytest.raises(KeyboardInterrupt):
        image.seal_enrolled_image(FakeSandbox(vm), tmp_path / "image", RECIPE)
    assert vm.running is False
    assert not (tmp_path / "image").exists()


def test_existing_destination_is_not_modified(tmp_path, station):
    vm = FakeVM(tmp_path / "source")
    destination = tmp_path / "image"
    destination.mkdir()
    keep = destination / "keep"
    keep.write_text("unchanged")
    with pytest.raises(ValueError, match="new image directory"):
        image.seal_enrolled_image(FakeSandbox(vm), destination, RECIPE)
    assert keep.read_text() == "unchanged"
    assert vm.running is True


def test_seal_requires_only_paths_the_installer_creates():
    # Sealing must not demand guest files that the machine-wide installer never creates.
    seal = (ASSETS / "mac-station-seal.py").read_text()
    install = (ASSETS / "mac-station-install.sh").read_text()
    required = re.findall(r'Path\("(/(?:usr/local/libexec|Library/LaunchAgents)/[^"]+)"\)', seal)
    assert required, "seal script no longer lists installed paths"
    for path in required:
        assert path in install, f"seal requires {path} but the installer never creates it"


def heredoc(text, marker):
    start = text.index(f"<<'{marker}'\n") + len(f"<<'{marker}'\n")
    return text[start : text.index(f"\n{marker}\n", start) + 1]


def test_only_lease_shells_put_homebrew_on_path_after_the_system_directories():
    install = (ASSETS / "mac-station-install.sh").read_text()
    wrapper = heredoc(install, "SHELL").splitlines()
    seal = seal_module()
    assert seal.LEASE_PATH in wrapper
    assert seal.LEASE_PATH == (
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/homebrew/sbin"
    )
    assert wrapper.index(seal.LEASE_PATH) < wrapper.index("export PATH")
    assert wrapper.index("export PATH") < min(
        index for index, line in enumerate(wrapper) if "exec " in line
    )
    assert str(seal.LEASE_SHELL) in install
    assert "paths.d" not in install
    assert "paths.d" not in (ASSETS / "mac-station-prepare.py").read_text()


def test_installer_writes_the_lease_shell_startup_files_the_seal_pins():
    install = (ASSETS / "mac-station-install.sh").read_text()
    seal = seal_module()
    assert heredoc(install, "ZPROFILE") == seal.LEASE_ZDOTDIR_FILES[".zprofile"]
    assert heredoc(install, "ZSHRC") == seal.LEASE_ZDOTDIR_FILES[".zshrc"]
    assert f'block = """{seal.ZSHENV_BLOCK}"""' in install
    assert "path.write_text(block + text)" in install  # first, before anything else it holds
    assert f"ZDOTDIR={seal.LEASE_ZDOTDIR}\n" in seal.ZSHENV_BLOCK
    assert "firmstations | /usr/bin/grep" in " ".join(seal.ZSHENV_BLOCK.split())
    assert "    PermitUserRC no\n" in install and "PermitUserRC no" in seal.LEASE_DIRECTIVES
    assert "verify_lease_shell_startup()" in (ASSETS / "mac-station-seal.py").read_text()


@pytest.mark.skipif(
    not os.path.exists("/bin/zsh") or not os.path.exists("/etc/zprofile"), reason="needs macOS zsh"
)
def test_lease_shells_ignore_the_station_homes_startup_files(tmp_path):
    seal = seal_module()
    home, zdotdir = tmp_path / "home", tmp_path / "zdot"
    home.mkdir()
    zdotdir.mkdir()
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
        (home / name).write_text(f"print -r planted-{name} >> {tmp_path}/ran\n")
    for name, content in seal.LEASE_ZDOTDIR_FILES.items():
        (zdotdir / name).write_text(content)
    # /etc/zshenv's block, for this test's account: it sets ZDOTDIR before zsh looks
    # for the home's .zshenv.
    zshenv = tmp_path / "zshenv"
    zshenv.write_text(
        seal.ZSHENV_BLOCK.replace("firmstations", "staff").replace(
            str(seal.LEASE_ZDOTDIR), str(zdotdir)
        )
    )
    probe = subprocess.run(
        ["/bin/zsh", "-f", "-c", f"source {zshenv}; print -r -- $ZDOTDIR"],
        capture_output=True,
        text=True,
        check=True,
        env={"HOME": str(home)},
    )
    assert probe.stdout.strip() == str(zdotdir)
    wrapper_path = seal.LEASE_PATH.removeprefix("PATH=")
    for argv in (
        ["-lc", "print -r -- $PATH"],
        ["-ilc", "print -r -- $PATH; print -r -- $HISTFILE"],
    ):
        shell = subprocess.run(
            ["/bin/zsh", *argv],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env={"HOME": str(home), "ZDOTDIR": str(zdotdir), "PATH": wrapper_path},
        )
        entries = shell.stdout.splitlines()[0].split(":")
        assert entries[-2:] == ["/opt/homebrew/bin", "/opt/homebrew/sbin"]
        assert entries.index("/usr/bin") < entries.index("/opt/homebrew/bin")
        if argv[0] == "-ilc":
            assert shell.stdout.splitlines()[1] == f"{home}/.zsh_history"
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "zshenv-missing",
        "zshenv-block",
        "zshenv-later",
        "zshenv-writable",
        "zdotdir-link",
        "zdotdir-extra",
        "zdotdir-content",
        "zdotdir-owner",
    ],
)
def test_seal_requires_the_root_owned_lease_shell_startup(tmp_path, monkeypatch, fault):
    seal = seal_module()
    zshenv, zdotdir = tmp_path / "zshenv", tmp_path / "firm-lease-zsh"
    zshenv.write_text(seal.ZSHENV_BLOCK + "# the system's own\n")
    zdotdir.mkdir()
    for name, content in seal.LEASE_ZDOTDIR_FILES.items():
        (zdotdir / name).write_text(content)
    monkeypatch.setattr(seal, "ZSHENV", zshenv)
    monkeypatch.setattr(seal, "LEASE_ZDOTDIR", zdotdir)
    monkeypatch.setattr(seal, "ROOT_UID", os.getuid())
    if fault == "zshenv-missing":
        zshenv.unlink()
    elif fault == "zshenv-block":
        zshenv.write_text(seal.ZSHENV_BLOCK.replace("firmstations", "wrong-group"))
    elif fault == "zshenv-later":
        zshenv.write_text("ZDOTDIR=$HOME\n" + seal.ZSHENV_BLOCK)
    elif fault == "zshenv-writable":
        zshenv.chmod(0o666)
    elif fault == "zdotdir-link":
        real = tmp_path / "real"
        zdotdir.rename(real)
        zdotdir.symlink_to(real)
    elif fault == "zdotdir-extra":
        (zdotdir / ".zshenv").write_text("")
    elif fault == "zdotdir-content":
        (zdotdir / ".zprofile").write_text("source $HOME/.zprofile\n")
    elif fault == "zdotdir-owner":
        monkeypatch.setattr(seal, "ROOT_UID", os.getuid() + 1)
    if fault is None:
        seal.verify_lease_shell_startup()
    else:
        with pytest.raises(SystemExit):
            seal.verify_lease_shell_startup()


SHELL_BUILTINS = {
    "set",
    "export",
    "if",
    "fi",
    "then",
    "else",
    "case",
    "esac",
    "exec",
    "printf",
    "echo",
    "trap",
    "umask",
    "exit",
    "shift",
    "for",
    "do",
    "done",
}


def commands_found_through_path(script):
    """Shell command lines of a root script whose program is not an absolute path.

    Heredoc bodies (file contents and inline Python) are skipped.
    """
    found, ending = [], None
    for line in script.splitlines():
        text = line.strip()
        if ending:
            ending = None if text == ending else ending
            continue
        match = re.search(r"<<'(\w+)'", text)
        if match:
            ending = match.group(1)
        word = text.split(" ", 1)[0]
        if re.fullmatch(r"[a-z][a-z0-9_-]*", word) and word not in SHELL_BUILTINS:
            found.append(text)
    return found


def test_root_scripts_name_their_programs_by_absolute_path():
    install = (ASSETS / "mac-station-install.sh").read_text()
    assert commands_found_through_path(install) == []
    assert commands_found_through_path("set -eu\nchmod 440 /x\n/bin/cat <<'PY'\nrm\nPY\n") == [
        "chmod 440 /x"
    ]


def seal_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mac_station_seal", ASSETS / "mac-station-seal.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "absent",
        "foreign-root",
        "missing-acl",
        "unwritable",
        "paths-d-file",
        "paths-d-entry",
        "etc-paths-entry",
        "agent",
        "daemon",
        "loaded",
        "running",
    ],
)
def test_seal_requires_group_shared_idle_homebrew(tmp_path, monkeypatch, fault):
    module = seal_module()
    homebrew = tmp_path / "opt" / "homebrew"
    (homebrew / "bin").mkdir(parents=True)
    (homebrew / "bin" / "brew").write_text("#!/bin/bash\n")
    (homebrew / "bin" / "jq").symlink_to("../Cellar/jq/1.8/bin/jq")
    etc_paths, paths_d = tmp_path / "paths", tmp_path / "paths.d"
    etc_paths.write_text("/usr/local/bin\n/usr/bin\n/bin\n/usr/sbin\n/sbin\n")
    paths_d.mkdir()
    (paths_d / "100-rvictl").write_text("/Library/Apple/usr/bin\n")
    agents, daemons = tmp_path / "LaunchAgents", tmp_path / "LaunchDaemons"
    agents.mkdir()
    daemons.mkdir()
    (agents / "dev.firm.station-helper.plist").write_text("x")
    monkeypatch.setattr(module, "HOMEBREW", homebrew)
    monkeypatch.setattr(module, "SYSTEM_PATHS", (etc_paths, paths_d))
    monkeypatch.setattr(module, "SYSTEM_SERVICE_DIRECTORIES", (agents, daemons))
    monkeypatch.setattr(module, "ROOT_UID", os.getuid())
    stations = [module.station_info(1), module.station_info(2)]
    services = "gui/501 = {\n\tservices = {\n\t\t0 - \tcom.apple.Finder\n\t}\n}\n"
    acl = " 0: group:firmstations allow read,write,file_inherit,directory_inherit\n"
    running = "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n"
    if fault == "absent":
        shutil.rmtree(homebrew)
    elif fault == "foreign-root":
        monkeypatch.setattr(module, "ROOT_UID", os.getuid() + 1)
    elif fault == "missing-acl":
        acl = ""
    elif fault == "paths-d-file":
        (paths_d / "homebrew").write_text("/usr/local/bin\n")
    elif fault == "paths-d-entry":
        (paths_d / "tools").write_text(f"{homebrew}/bin\n")
    elif fault == "etc-paths-entry":
        etc_paths.write_text(etc_paths.read_text() + f"{homebrew}/sbin\n")
    elif fault == "agent":
        (agents / "homebrew.mxcl.redis.plist").write_text("x")
    elif fault == "daemon":
        (daemons / "homebrew.mxcl.postgresql@17.plist").write_text("x")
    elif fault == "loaded":
        services = services.replace("com.apple.Finder", "homebrew.mxcl.redis")
    elif fault == "running":
        running += "/opt/homebrew/Cellar/redis/8.0/bin/redis-server\n"

    def run(argv, **kwargs):
        if argv[:2] == ["/bin/ls", "-lde"]:
            return SimpleNamespace(returncode=0, stdout=acl, stderr="")
        if argv[-3:-1] == ["/bin/test", "-w"]:
            refused = fault == "unwritable" and "firmstation2" in argv
            return SimpleNamespace(returncode=int(refused), stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=services, stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module,
        "running_executables",
        lambda: running.replace("/opt/homebrew/", f"{homebrew}/").splitlines(),
    )
    if fault is None:
        assert module.verify_homebrew(stations) == str(homebrew)
    elif fault == "absent":
        assert module.verify_homebrew(stations) is None
    else:
        expected = {
            "foreign-root": "not the root-owned shared prefix",
            "missing-acl": "lacks the inheritable firmstations ACL",
            "unwritable": "not writable by firmstation2",
            "paths-d-file": "system PATH entry names Homebrew: .*paths.d/homebrew",
            "paths-d-entry": "system PATH entry names Homebrew: .*paths.d/tools",
            "etc-paths-entry": "system PATH entry names Homebrew: .*/paths$",
            "agent": "Homebrew service is installed",
            "daemon": "Homebrew service is installed",
            "loaded": "Homebrew service is loaded",
            "running": "Homebrew program is running",
        }[fault]
        with pytest.raises(SystemExit, match=expected):
            module.verify_homebrew(stations)
    assert "verify_homebrew(stations)" in (ASSETS / "mac-station-seal.py").read_text()


def test_build_leaves_the_interpreter_tree_writable_by_root_alone(monkeypatch, tmp_path, inputs):
    uv, _ = inputs
    monkeypatch.setattr(image.subprocess, "run", FakeLima())
    image.build_base_image(tmp_path / "image", image.Recipe(disk_gib=64, stations=2), uv_binary=uv)
    setup = (tmp_path / "image" / "setup.sh").read_text().splitlines()
    link = setup.index('/bin/ln -sfn "$python_path" /usr/local/firm/bin/python3')
    owner = setup.index("/usr/sbin/chown -R root:wheel /usr/local/firm")
    assert link < owner < setup.index("/bin/chmod -R go-w /usr/local/firm")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "outside",
        "python-owner",
        "python-writable",
        "directory-writable",
        "file-writable",
        "file-owner",
    ],
)
def test_seal_requires_a_root_only_interpreter_tree(tmp_path, monkeypatch, fault):
    seal = seal_module()
    tree = tmp_path / "firm"
    python = tree / "python" / "cpython-3.12.13-macos-aarch64-none" / "bin" / "python3.12"
    python.parent.mkdir(parents=True)
    python.write_text("python")
    python.chmod(0o755)
    (python.parent / "python3").symlink_to("python3.12")
    library = tree / "python" / "cpython-3.12.13-macos-aarch64-none" / "lib" / "os.py"
    library.parent.mkdir(parents=True)
    library.write_text("")
    (tree / "bin").mkdir()
    (tree / "bin" / "python3").symlink_to(python)
    monkeypatch.setattr(seal, "FIRM_TREE", tree)
    monkeypatch.setattr(seal, "FIRM_PYTHON", tree / "bin" / "python3")
    monkeypatch.setattr(seal, "ROOT_UID", os.getuid())
    real_lstat = os.lstat
    foreign = {}
    monkeypatch.setattr(
        seal.os,
        "lstat",
        lambda path, *a, **k: (
            SimpleNamespace(st_uid=os.getuid() + 1, st_mode=real_lstat(path, *a, **k).st_mode)
            if str(path) in foreign
            else real_lstat(path, *a, **k)
        ),
    )
    monkeypatch.setattr(seal.Path, "lstat", lambda path: seal.os.lstat(path))
    if fault == "missing":
        python.unlink()
    elif fault == "outside":
        elsewhere = tmp_path / "python3.12"
        elsewhere.write_text("python")
        (tree / "bin" / "python3").unlink()
        (tree / "bin" / "python3").symlink_to(elsewhere)
    elif fault == "python-owner":
        foreign[str(python)] = True
    elif fault == "python-writable":
        python.chmod(0o775)
    elif fault == "directory-writable":
        library.parent.chmod(0o777)
    elif fault == "file-writable":
        library.chmod(0o666)
    elif fault == "file-owner":
        foreign[str(library)] = True
    if fault is None:
        seal.verify_interpreter_tree()
    else:
        with pytest.raises(SystemExit):
            seal.verify_interpreter_tree()
    assert "verify_interpreter_tree()" in (ASSETS / "mac-station-seal.py").read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS's")
def test_seal_reads_running_executables_from_the_kernel():
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4 * 1024)
    assert libproc.proc_pidpath(os.getpid(), buffer, ctypes.sizeof(buffer)) > 0
    kernel_path = os.fsdecode(buffer.value)

    # The interpreter launcher can differ from the executable path the kernel reports.
    assert kernel_path in seal_module().running_executables()


@pytest.mark.parametrize("fault", [None, "locks", "sleeps", "relocks", "screensaver", "unset"])
def test_seal_refuses_a_station_that_locks_or_sleeps(monkeypatch, fault):
    module = seal_module()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-2:] == ["-screenLock", "status"]:
            # sysadminctl answers on stderr, after a timestamp and its process tag.
            answer = "screenLock delay is 300 seconds" if fault == "locks" else "screenLock is off"
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=f"2026-09-21 14:16:48.159 sysadminctl[1:2] {answer}\n",
            )
        if argv[:2] == ["/usr/bin/defaults", "read"]:
            relock = fault == "relocks"
            return SimpleNamespace(returncode=0 if relock else 1, stdout="1\n" if relock else "")
        if argv[-4:] == ["-currentHost", "read", "com.apple.screensaver", "idleTime"]:
            if fault == "unset":
                return SimpleNamespace(returncode=1, stdout="", stderr="does not exist")
            idle = "1200\n" if fault == "screensaver" else "0\n"
            return SimpleNamespace(returncode=0, stdout=idle)
        raise AssertionError(argv)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda argv, **kwargs: (
            "System-wide power settings:\n sleep                0\n disksleep            0\n"
            f" displaysleep         {10 if fault == 'sleeps' else 0}\n"
        ),
    )
    station = module.station_info(2)
    if fault is None:
        module.verify_station_awake(station)
    else:
        expected = (
            "locks its screen .screenLock delay" if fault == "locks" else "run mac-restart first"
        )
        with pytest.raises(SystemExit, match=expected):
            module.verify_station_awake(station)
    assert calls[0][:7] == [
        "/bin/launchctl",
        "asuser",
        "503",
        "/usr/bin/sudo",
        "-H",
        "-u",
        "firmstation2",
    ]
    assert "verify_station_awake(station)" in (ASSETS / "mac-station-seal.py").read_text()


@pytest.mark.parametrize("fault", [None, "remote", "scope", "auto-login", "launcher", "sudoers"])
def test_seal_requires_plain_screen_sharing_scoped_to_only_station_accounts(
    tmp_path, monkeypatch, fault
):
    import plistlib

    module = seal_module()
    marker = tmp_path / "RemoteManagement.launchd"
    launcher = tmp_path / "firm-station-launch"
    sudoers = tmp_path / "firm-station-sudoers"
    launcher.write_text('dseditgroup -o checkmember -m "$user" firmstations\n')
    sudoers.write_text("%firmstations ALL = (root) NOPASSWD: /usr/local/libexec/tool\n")
    monkeypatch.setattr(module, "REMOTE_MANAGEMENT_MARKER", marker)
    monkeypatch.setattr(module, "HELPER_LAUNCHER", launcher)
    monkeypatch.setattr(module, "STATION_SUDOERS", sudoers)
    if fault == "remote":
        marker.write_text("")
    if fault == "launcher":
        launcher.write_text("firmstation1 only\n")
    if fault == "sudoers":
        sudoers.write_text("firmstation1 ALL=(root) ALL\n")
    nested = ["WRONG"] if fault == "scope" else ["STATION-GROUP-GUID"]
    group = plistlib.dumps({"dsAttrTypeStandard:NestedGroups": nested})

    def run(argv, **kwargs):
        value = "1\n" if fault == "remote" else "0\n"
        return SimpleNamespace(returncode=0, stdout=value, stderr="")

    def check_output(argv, **kwargs):
        if "print-disabled" in argv:
            return '"com.apple.screensharing" => enabled\n'
        if "-plist" in argv:
            return group
        if "GeneratedUID" in argv:
            return "GeneratedUID: STATION-GROUP-GUID\n"
        if "autoLoginUser" in argv:
            return "someone-else\n" if fault == "auto-login" else "firmvm\n"
        raise AssertionError(argv)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "check_output", check_output)
    monkeypatch.setattr(
        module.grp,
        "getgrnam",
        lambda name: SimpleNamespace(
            gr_gid=900,
            gr_mem=["firmstation1", "firmstation2"],
        ),
    )
    accounts = [
        SimpleNamespace(pw_name="firmvm", pw_gid=20),
        SimpleNamespace(pw_name="firmstation1", pw_gid=20),
        SimpleNamespace(pw_name="firmstation2", pw_gid=20),
        # macOS's nobody: a gid os.getgrouplist cannot pass on, met on the first live seal.
        SimpleNamespace(pw_name="nobody", pw_gid=4294967294),
    ]
    monkeypatch.setattr(module.pwd, "getpwall", lambda: accounts)
    monkeypatch.setattr(
        module.os, "getgrouplist", lambda user, gid: pytest.fail("asked for every account's groups")
    )
    stations = [module.station_info(1), module.station_info(2)]
    if fault is None:
        module.verify_access_scope(stations)
    else:
        with pytest.raises(SystemExit):
            module.verify_access_scope(stations)


@pytest.mark.parametrize("fault", [None, "extra", "missing", "uid", "inventory"])
def test_seal_requires_exact_numbered_accounts_and_matching_private_inventory(
    tmp_path, monkeypatch, fault
):
    module = seal_module()
    inventory = tmp_path / "stations.json"
    inventory.write_text(json.dumps({"stations": 3 if fault == "inventory" else 2}))
    inventory.chmod(0o600)
    monkeypatch.setattr(module, "STATION_INVENTORY", inventory)
    monkeypatch.setattr(module, "ROOT_UID", os.getuid())
    accounts = {
        "firmvm": SimpleNamespace(pw_name="firmvm", pw_uid=501, pw_dir="/Users/firmvm"),
        "firmstation1": SimpleNamespace(
            pw_name="firmstation1", pw_uid=502, pw_dir="/Users/firmstation1"
        ),
        "firmstation2": SimpleNamespace(
            pw_name="firmstation2", pw_uid=503, pw_dir="/Users/firmstation2"
        ),
    }
    if fault == "extra":
        accounts["someone"] = SimpleNamespace(
            pw_name="someone", pw_uid=504, pw_dir="/Users/someone"
        )
    elif fault == "missing":
        accounts.pop("firmstation2")
    elif fault == "uid":
        accounts["firmstation2"] = SimpleNamespace(
            pw_name="firmstation2", pw_uid=504, pw_dir="/Users/firmstation2"
        )
    monkeypatch.setattr(module.pwd, "getpwall", lambda: list(accounts.values()))
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: accounts[name])
    if fault is None:
        assert module.verify_accounts(2) == [module.station_info(1), module.station_info(2)]
    else:
        with pytest.raises((SystemExit, KeyError)):
            module.verify_accounts(2)


# ---- what the seal script reads for the manifest ----------------------------------------


def test_seal_reads_an_applications_identity_from_its_plist(tmp_path, monkeypatch):
    import plistlib

    module = seal_module()
    monkeypatch.setattr(module, "APPLICATIONS", tmp_path)
    contents = tmp_path / "Xcode.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {"CFBundleIdentifier": "com.apple.dt.Xcode", "CFBundleShortVersionString": "27.0"}
        )
    )
    assert module.read_application("Xcode.app") == {
        "name": "Xcode.app",
        "bundle_id": "com.apple.dt.Xcode",
        "version": "27.0",
    }
    with pytest.raises(SystemExit, match="no readable Info.plist"):
        module.read_application("Missing.app")


def test_seal_reads_xcodes_version_from_its_plist_and_runs_nothing_of_xcodes(tmp_path, monkeypatch):
    import plistlib

    module = seal_module()
    contents = tmp_path / "Xcode.app" / "Contents"
    (contents / "Developer").mkdir(parents=True)
    (contents / "version.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "27.0", "ProductBuildVersion": "18A123"})
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=f"{contents}/Developer\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "SELECTED_XCODE", re.compile(".*/Xcode.app/Contents/Developer"))
    assert module.read_xcode() == {"version": "27.0", "build": "18A123"}
    assert calls == [["/usr/bin/xcode-select", "-p"]]


@pytest.mark.parametrize(
    "selected",
    [
        (1, ""),
        (0, "/Library/Developer/CommandLineTools\n"),
        (0, "/Users/firmstation1/Xcode.app/Contents/Developer\n"),
    ],
)
def test_seal_reports_no_xcode_unless_one_in_applications_is_selected(monkeypatch, selected):
    module = seal_module()
    returncode, stdout = selected
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    assert module.read_xcode() is None


def test_seal_asks_homebrew_its_version_as_the_station_account_with_a_bound(monkeypatch):
    module = seal_module()
    station = module.station_info(2)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="Homebrew 4.6.11\nHomebrew/homebrew-core x\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.read_homebrew(None, station) is None and calls == []
    assert module.read_homebrew("/opt/homebrew", station) == {
        "prefix": "/opt/homebrew",
        "version": "4.6.11",
    }
    argv, kwargs = calls[0]
    assert argv == [
        "/bin/launchctl",
        "asuser",
        "503",
        "/usr/bin/sudo",
        "-H",
        "-u",
        "firmstation2",
        "/opt/homebrew/bin/brew",
        "--version",
    ]
    assert kwargs["timeout"] == 60


@pytest.mark.parametrize(
    "output",
    [
        "",
        "brew 4.6.11\n",
        "Homebrew\n",
        "Homebrew 4.6.11 $(id)\n",
        "Homebrew <script>\n",
        "  Homebrew 4.6.11\n",
    ],
)
def test_seal_refuses_a_homebrew_version_it_cannot_recognise(monkeypatch, output):
    module = seal_module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    with pytest.raises(SystemExit, match="did not report its version"):
        module.read_homebrew("/opt/homebrew", module.station_info(1))


def test_seal_refuses_a_homebrew_that_hangs_or_fails(monkeypatch):
    module = seal_module()

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(SystemExit, match="did not report its version"):
        module.read_homebrew("/opt/homebrew", module.station_info(1))


def test_seal_script_lists_no_runtimes_and_runs_no_xcode_tool_as_root():
    script = (ASSETS / "mac-station-seal.py").read_text()
    assert "simctl" not in script and 'xcodebuild", "-version' not in script


def _installer_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mac_station_add_platform", ASSETS / "mac-station-add-platform.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


def test_seal_refuses_while_a_platform_install_holds_its_lock(tmp_path, monkeypatch):
    module, installer = seal_module(), _installer_module()
    lock = tmp_path / "install.lock"
    monkeypatch.setattr(module, "PLATFORM_INSTALL_LOCK", str(lock))
    running = installer.install_lock(str(lock))
    try:
        with pytest.raises(SystemExit, match="install is still running"):
            module.exclude_platform_installs()
    finally:
        os.close(running)


def test_no_install_starts_while_seal_holds_the_lock(tmp_path, monkeypatch):
    """Seal runs the station account's own brew; whatever that starts finds the lock held."""
    module, installer = seal_module(), _installer_module()
    lock = tmp_path / "install.lock"
    monkeypatch.setattr(module, "PLATFORM_INSTALL_LOCK", str(lock))
    held = module.exclude_platform_installs()  # no lock file yet: a Mac that never installed
    try:
        with pytest.raises(SystemExit, match="still running"):
            installer.install_lock(str(lock))
    finally:
        os.close(held)
    os.close(installer.install_lock(str(lock)))


def test_seal_and_the_installer_name_the_same_lock():
    assert seal_module().PLATFORM_INSTALL_LOCK == _installer_module().LOCK


def test_asking_whether_an_install_is_running_changes_nothing(monkeypatch):
    """The host's probe only takes and releases the install lock."""
    module = seal_module()
    events = []

    monkeypatch.setattr(
        module.sys, "argv", [str(ASSETS / "mac-station-seal.py"), module.NO_INSTALL_RUNNING]
    )
    monkeypatch.setattr(module.os, "getuid", lambda: 0)

    def check_output(argv, *, text):
        events.append(("check_output", argv, text))
        return "1\n"

    def exclude():
        events.append(("exclude",))
        return "install-lock"

    monkeypatch.setattr(module.subprocess, "check_output", check_output)
    monkeypatch.setattr(module, "exclude_platform_installs", exclude)
    monkeypatch.setattr(module.os, "close", lambda descriptor: events.append(("close", descriptor)))
    monkeypatch.setattr(
        module,
        "verify_accounts",
        lambda count: pytest.fail(f"probe unexpectedly verified accounts: {count}"),
    )

    module.main()

    assert events == [
        ("check_output", ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"], True),
        ("exclude",),
        ("close", "install-lock"),
    ]


@pytest.mark.parametrize("outcome", ["success", "timeout", "output-error"])
def test_seal_supervisor_holds_machine_and_install_locks_through_worker_reaping(
    monkeypatch, capsys, outcome
):
    """Both guest-wide locks stay held until the bounded worker is fully reaped."""
    module = seal_module()
    events = []
    held = set()

    class Worker:
        pid = 901
        returncode = None

        def communicate(self, *, timeout=None):
            assert held == {"machine-lock", "install-lock"}
            events.append(("communicate", timeout))
            if outcome == "output-error":
                raise OSError("worker output failed")
            if outcome == "timeout" and timeout is not None:
                raise module.subprocess.TimeoutExpired("seal-worker", timeout)
            self.returncode = 0 if outcome == "success" else -9
            return "sealed output\n", "worker warning\n"

        def wait(self):
            assert held == {"machine-lock", "install-lock"}
            events.append(("wait",))
            self.returncode = -9

    worker = Worker()

    def machine_lock():
        held.add("machine-lock")
        events.append(("machine_lock",))
        return "machine-lock"

    def install_lock():
        held.add("install-lock")
        events.append(("install_lock",))
        return "install-lock"

    def popen(argv, **kwargs):
        assert held == {"machine-lock", "install-lock"}
        events.append(("popen", argv, kwargs))
        return worker

    def killpg(pid, signal):
        assert held == {"machine-lock", "install-lock"}
        events.append(("killpg", pid, signal))

    def close(descriptor):
        assert worker.returncode is not None, "released a lock before reaping the worker"
        held.remove(descriptor)
        events.append(("close", descriptor))

    monkeypatch.setattr(module, "machine_lock", machine_lock)
    monkeypatch.setattr(module, "exclude_platform_installs", install_lock)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.os, "killpg", killpg)
    monkeypatch.setattr(module.os, "close", close)

    if outcome == "timeout":
        with pytest.raises(SystemExit, match="deadline"):
            module.supervise('{"stations": 2}')
    elif outcome == "output-error":
        with pytest.raises(OSError, match="worker output failed"):
            module.supervise('{"stations": 2}')
        assert ("wait",) in events
    else:
        module.supervise('{"stations": 2}')

    output = capsys.readouterr()
    assert output.out == ("sealed output\n" if outcome == "success" else "")
    assert output.err == ("worker warning\n" if outcome == "success" else "")
    assert not held
    assert ("killpg", worker.pid, module.signal.SIGKILL) in events
    assert events[-2:] == [("close", "install-lock"), ("close", "machine-lock")]
    popen_event = events[2]
    assert popen_event[1] == [
        sys.executable,
        "-I",
        module.__file__,
        module.WORKER,
        '{"stations": 2}',
    ]
    assert popen_event[2]["start_new_session"] is True


def test_seal_reads_platform_metadata_before_clearing_image_identity():
    # The full worker requires an enrolled guest. Keep this narrow ordering guard
    # until that path has an executable fixture; the lock checks above run real code.
    script = (ASSETS / "mac-station-seal.py").read_text()
    main = script[script.index("def main():") :]
    cleared = main.index("temporary.replace(record)")
    for step in (
        "read_application(name)",
        "read_xcode()",
        "read_homebrew(homebrew_prefix, stations[0])",
    ):
        assert main.index(step) < cleared, step
