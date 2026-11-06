"""Mac images: build a base image, apply a recipe's runtimes, and seal an enrolled image.

A Mac image is a directory that firms clone: ``image.json``, the stopped Lima
instance ``lima/base`` and the control account's key pair in ``lima/_config``. A
checked-in ``Recipe`` (``images/ios.toml`` at the repository root) states a base
image's disk size, station count, applications and the simulator runtimes a firm
expects; seal refuses a guest missing one. The IPSW pin, Homebrew tag and controller
Python stay code constants.

``build_base_image`` uses its own Lima home, separate from any firm, and copies only
the recipe's applications into it; it downloads no simulator runtime. It owns a short
trusted provisioning window (the guest has Lima's NAT network then) and publishes only
the stopped Lima instance plus its ``image.json`` manifest. A dedicated firm cloned
from it is enrolled with ``mac_enrollment``. ``apply_recipe`` then installs the
recipe's runtimes a running firm's Mac VM lacks, and ``seal_enrolled_image`` inspects
that guest directly — runtimes, applications, Xcode, Homebrew — rather than carrying
forward what the base image recorded, and publishes a stopped copy of its Mac VM.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .commands import Commands, lima_status, mac_sudo_command, parse_lima_list
from .compatibility import image_identity
from .guest_control import lifecycle, private_directory, save
from .host_paths import TEMP_PARENT, _move_durably, _owned_directory, remove_directory
from .mac_station import ASSETS, DIRTY_MARKER, station_locks
from .mac_vm import CONTROL_USER, PYTHON

INSTANCE = "base"
PYTHON_VERSION = "3.12"
MAC_VM_BASE_CONFIG = Path(__file__).with_name("assets") / "mac-vm-base.yaml"
# A build works in a new directory here, short enough for Lima's sockets, and moves it
# to the destination when it succeeds. A failed or interrupted build stops its VM and
# removes the directory; it leaves it for recovery only when the stop is unconfirmed.
WORKSPACE_PARENT = TEMP_PARENT
# uv is copied into the image from the invoking owner's installation.  Keep a
# deterministic fallback for callers that provide it later through uv_binary.
DEFAULT_UV = Path(shutil.which("uv") or (Path.home() / ".local" / "bin" / "uv"))
# The simulator platforms a recipe may name, as xcodebuild -downloadPlatform names
# them, each with the SDK whose version is the runtime Xcode downloads by default. A
# version is written as simctl reports it. The station's platform installer accepts the
# same platforms and version form.
SIMULATOR_SDKS = {
    "iOS": "iphonesimulator",
    "watchOS": "watchsimulator",
    "tvOS": "appletvsimulator",
    "visionOS": "xrsimulator",
}
SIMULATOR_PLATFORMS = tuple(SIMULATOR_SDKS)
SIMULATOR_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
# mac-image-apply's per-runtime download can be several gigabytes over the firm's network.
SIMULATOR_TIMEOUT = 7200
# Homebrew's own Apple silicon layout: prefix and repository are one directory.
# The build clones it as the control account, publishes it root-owned, and
# mac-prepare gives every station write access through an inheritable group ACL.
# Nothing system-wide puts it on PATH: only station lease shells, whose root-owned
# wrapper sets PATH, look there.
HOMEBREW_PREFIX = "/opt/homebrew"
HOMEBREW_REMOTE = "https://github.com/Homebrew/brew"
# The Homebrew/brew release the image carries, so a rebuild gets the same brew. The
# build never runs brew update; the station account updates it when a role wants.
HOMEBREW_TAG = "7.0.3"
HOMEBREW_ENVIRONMENT = (
    "HOMEBREW_NO_ANALYTICS=1",
    "HOMEBREW_NO_ENV_HINTS=1",
)


def _progress(message: str, *, command: str = "mac-image-build") -> None:
    print(f"{command}: {message}", file=sys.stderr, flush=True)


def _empty_destination(destination: Path) -> Path:
    """Create or validate the owner-only destination used for the image."""

    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("image destination must not be a symlink")
    if destination.exists() and not destination.is_dir():
        raise ValueError("image destination must be a directory")
    if not destination.exists():
        destination.mkdir(mode=0o700)
    else:
        info = destination.stat()
        if info.st_uid != os.getuid():
            raise ValueError("image destination is not owner-controlled")
        if any(destination.iterdir()):
            raise ValueError("image destination must be empty")
        destination.chmod(stat.S_IRWXU)
    return destination


def _source_file(path: Path, *, label: str) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is not a file") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"{label} is not an executable file")
    return resolved


def _application(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("application paths must be absolute")
    if path.is_symlink():
        raise ValueError("application paths must not be top-level symlinks")
    if path.suffix != ".app" or not path.is_dir():
        raise ValueError(f"application is not an .app bundle: {path}")
    return path


def _xcode(applications: tuple[Path, ...]) -> Path | None:
    return next(
        (
            app
            for app in applications
            if app.name.lower().startswith("xcode")
            or (app / "Contents" / "Developer" / "usr" / "bin" / "xcodebuild").is_file()
        ),
        None,
    )


# ---- the image recipe -----------------------------------------------------------------


@dataclass(frozen=True)
class Simulator:
    """One ``[[simulator]]`` entry: a platform, and optionally the version to pin.

    Without a version it means the runtime the image's Xcode downloads by default,
    which is the one for that platform's SDK.
    """

    platform: str
    version: str | None = None

    def __post_init__(self):
        if self.platform not in SIMULATOR_PLATFORMS:
            raise ValueError(
                f"Unknown simulator platform {self.platform!r}; choose from "
                + ", ".join(SIMULATOR_PLATFORMS)
            )
        if self.version is not None and (
            not isinstance(self.version, str) or not SIMULATOR_VERSION.fullmatch(self.version)
        ):
            raise ValueError(f"Invalid simulator version {self.version!r}")


@dataclass(frozen=True)
class Recipe:
    """A checked-in image recipe: disk size, station count, apps and runtimes.

    Strict like ``Config.load``: an unknown section or key refuses, and every
    ``[[application]]`` and ``[[simulator]]`` is validated before build, apply or
    seal uses it. ``build_base_image`` uses ``disk_gib`` and ``applications``;
    ``apply_recipe`` and ``seal_enrolled_image`` use ``simulators``; seal also uses
    ``applications`` to know which bundles to inspect. ``sha256`` is the recipe
    file's own digest, set by ``load`` and recorded by seal; a ``Recipe`` built
    directly (in tests, for instance) leaves it ``None``.
    """

    disk_gib: int
    stations: int
    applications: tuple[Path, ...] = ()
    simulators: tuple[Simulator, ...] = ()
    sha256: str | None = None

    def __post_init__(self):
        if type(self.disk_gib) is not int or not 1 <= self.disk_gib <= 2048:
            raise ValueError("recipe disk_gib must be a positive number of gibibytes")
        if type(self.stations) is not int or not 1 <= self.stations <= 8:
            raise ValueError("recipe stations must be an integer from 1 to 8")
        applications = tuple(Path(app) for app in self.applications)
        for app in applications:
            if not app.is_absolute() or app.suffix != ".app":
                raise ValueError(f"recipe application path is not an absolute .app bundle: {app}")
        names = tuple(app.name for app in applications)
        if len(set(names)) != len(names):
            raise ValueError("recipe application names must be unique")
        object.__setattr__(self, "applications", applications)
        simulators = tuple(self.simulators)
        if len(set(simulators)) != len(simulators):
            raise ValueError("recipe simulators must be unique")
        if simulators and _xcode(applications) is None:
            raise ValueError("recipe simulators need Xcode among its applications")
        object.__setattr__(self, "simulators", simulators)

    @classmethod
    def load(cls, path: Path) -> Recipe:
        data = Path(path).read_bytes()
        raw = tomllib.loads(data.decode())
        sections = {"disk_gib", "stations", "application", "simulator"}
        unknown = set(raw) - sections
        if unknown:
            raise ValueError(f"Unknown recipe setting: {', '.join(sorted(unknown))}")
        if "disk_gib" not in raw:
            raise ValueError("recipe must set disk_gib")
        if "stations" not in raw:
            raise ValueError("recipe must set stations")
        applications, simulators = raw.get("application", []), raw.get("simulator", [])
        if not isinstance(applications, list) or not isinstance(simulators, list):
            raise ValueError("recipe application and simulator must be [[tables]]")
        application_paths = []
        for entry in applications:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path"}
                or not isinstance(entry["path"], str)
            ):
                raise ValueError("[[application]] takes exactly one string key: path")
            application_paths.append(Path(entry["path"]))
        simulator_specs = []
        for entry in simulators:
            if (
                not isinstance(entry, dict)
                or not set(entry) <= {"platform", "version"}
                or not isinstance(entry.get("platform"), str)
                or not (entry.get("version") is None or isinstance(entry["version"], str))
            ):
                raise ValueError("[[simulator]] takes platform and an optional string version")
            simulator_specs.append(Simulator(entry["platform"], entry.get("version")))
        return cls(
            disk_gib=raw["disk_gib"],
            stations=raw["stations"],
            applications=tuple(application_paths),
            simulators=tuple(simulator_specs),
            sha256=hashlib.sha256(data).hexdigest(),
        )


# ---- simulator runtime inspection ------------------------------------------------------


def _runtime_listing(listing: bytes | str) -> list:
    """Parse ``simctl list runtimes -j``'s raw text into its runtime dicts."""

    try:
        runtimes = json.loads(listing)["runtimes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("simctl did not return a runtime listing") from exc
    if not isinstance(runtimes, list):
        raise RuntimeError("simctl did not return a runtime listing")
    return runtimes


def _runtime_names(simulators: list[Simulator], defaults: dict[str, str]) -> str:
    return ", ".join(
        f"{entry.platform} {entry.version or defaults[entry.platform]}" for entry in simulators
    )


def _guest_runtimes(vm, recipe: Recipe) -> list:
    """The guest's own runtime listing, read as the control account; nothing runs as root.

    simctl is Xcode's, so an image whose recipe names no Xcode has no runtimes to list.
    """

    if _xcode(recipe.applications) is None:
        return []
    listing = vm._shell("/usr/bin/xcrun", "simctl", "list", "runtimes", "-j", timeout=300)
    return _runtime_listing(listing.stdout)


def _default_versions(vm, simulators: tuple[Simulator, ...]) -> dict[str, str]:
    """The selected Xcode's SDK version for each platform the recipe leaves unpinned.

    ``xcodebuild -downloadPlatform`` without a version installs the runtime for that
    SDK, so this is what an entry without a version stands for in this guest.
    """

    versions = {}
    for platform in sorted({entry.platform for entry in simulators if entry.version is None}):
        result = vm._shell(
            "/usr/bin/xcrun", "--sdk", SIMULATOR_SDKS[platform], "--show-sdk-version", timeout=120
        )
        version = result.stdout.decode(errors="replace").strip()
        if not SIMULATOR_VERSION.fullmatch(version):
            raise RuntimeError(f"Xcode did not report its {platform} SDK version")
        versions[platform] = version
    return versions


def missing_simulators(
    simulators: tuple[Simulator, ...], runtimes: list, defaults: dict[str, str]
) -> list[Simulator]:
    """The recipe entries ``runtimes`` (as ``simctl list runtimes -j`` reports them) lacks.

    An entry needs an available runtime of its platform at its version, or a patch of
    it: 26.0 is met by 26.0 and 26.0.1, never by 26.4. An entry without a version stands
    for ``defaults[platform]``, so an older pinned runtime never stands in for the
    current one. ``apply_recipe`` installs what this returns; ``seal_enrolled_image``
    refuses a guest while it returns anything.
    """

    available = [
        (runtime["platform"], runtime["version"]) for runtime in every_available_runtime(runtimes)
    ]

    def present(entry):
        wanted = entry.version or defaults[entry.platform]
        return any(
            platform == entry.platform and (version == wanted or version.startswith(wanted + "."))
            for platform, version in available
        )

    return [entry for entry in simulators if not present(entry)]


def every_available_runtime(runtimes: list) -> list[dict[str, str]]:
    """Every available runtime in ``runtimes``, reduced to what an image manifest keeps."""

    return [
        {
            "platform": runtime["platform"],
            "version": runtime["version"],
            "build": str(runtime.get("buildversion", "")),
            "identifier": str(runtime.get("identifier", "")),
        }
        for runtime in runtimes
        if isinstance(runtime, dict)
        and runtime.get("isAvailable") is True
        and isinstance(runtime.get("platform"), str)
        and isinstance(runtime.get("version"), str)
    ]


def _environment(lima_home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["LIMA_HOME"] = str(lima_home)
    # A host proxy is an implicit host channel.  Provisioning uses Lima's
    # trusted NAT directly, and the generated config also disables propagation.
    for key in tuple(environment):
        if "proxy" in key.lower():
            environment.pop(key)
    return environment


def _run(
    argv: list[str],
    environment: dict[str, str],
    *,
    timeout: float = 1200,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        argv,
        input=input,
        capture_output=True,
        env=environment,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result


def _load_config(disk_gib: int) -> dict[str, Any]:
    with MAC_VM_BASE_CONFIG.open() as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("pinned macOS VM config must be a mapping")

    # Keep the pinned restore URL, digest, and resource settings, while making
    # the test config's safety properties explicit for image creation.
    config = dict(config)
    config["disk"] = f"{disk_gib}GiB"
    config["mounts"] = []
    config["propagateProxyEnv"] = False
    config["networks"] = [{"vzNAT": True}]
    config["plain"] = True
    ssh = dict(config.get("ssh") or {})
    ssh.update(
        loadDotSSHPubKeys=False,
        forwardAgent=False,
        forwardX11=False,
        forwardX11Trusted=False,
    )
    config["ssh"] = ssh
    user = dict(config.get("user") or {})
    user.update(name=CONTROL_USER, home="/Users/firmvm", shell="/bin/zsh")
    config["user"] = user
    return config


def _write_setup(destination: Path, applications: tuple[Path, ...]) -> Path:
    """Write the only host-side provisioning payload, with quoted guest paths."""

    xcode = _xcode(applications)
    xcode_lines: list[str] = []
    application_lines: list[str] = []
    for app in applications:
        guest_app = f"/Applications/{app.name}"
        quoted_app = shlex.quote(guest_app)
        application_lines.extend(
            [
                f"app_path={quoted_app}",
                'bad_permissions=$(/usr/bin/find "$app_path" ! -type l -perm +022 -print -quit)',
                '[ -z "$bad_permissions" ] || { '
                'print -u2 "Group- or world-writable app entry: $bad_permissions"; exit 1; }',
            ]
        )
    if xcode is not None:
        guest_developer = f"/Applications/{xcode.name}/Contents/Developer"
        guest_xcode = f"{guest_developer}/usr/bin/xcodebuild"
        quoted_developer = shlex.quote(guest_developer)
        quoted_xcode = shlex.quote(guest_xcode)
        xcode_lines = [
            f"/usr/bin/xcode-select --switch {quoted_developer}",
            f"xcodebuild={quoted_xcode}",
            'test -x "$xcodebuild"',
            '"$xcodebuild" -license accept',
            '"$xcodebuild" -runFirstLaunch',
            # Homebrew needs Xcode's developer tools, so it comes with Xcode. The
            # control account clones into an empty prefix it owns only for the build.
            "/usr/bin/install -d -o root -g wheel -m 755 /opt",
            f"[ ! -e {HOMEBREW_PREFIX} ]",
            f"[ ! -L {HOMEBREW_PREFIX} ]",
            f"/usr/bin/install -d -o {CONTROL_USER} -g admin -m 755 {HOMEBREW_PREFIX}",
        ]

    lines = [
        "#!/bin/zsh",
        "set -euo pipefail",
        '[ "$(/usr/sbin/sysctl -n kern.hv_vmm_present)" = 1 ]',
        '[ "$(/usr/bin/id -u)" = 0 ]',
        "/bin/chmod 700 /Users/firmvm",
        "/bin/chmod 600 /Users/firmvm/password",
        *application_lines,
        *xcode_lines,
        "/usr/bin/install -d -o root -g wheel -m 755 /usr/local/firm",
        "/usr/bin/install -d -o root -g wheel -m 755 /usr/local/firm/bin",
        "/usr/bin/install -d -o root -g wheel -m 755 /usr/local/firm/python",
        "/usr/bin/install -o root -g wheel -m 755 /Users/firmvm/uv /usr/local/firm/bin/uv",
        "export UV_PYTHON_INSTALL_DIR=/usr/local/firm/python",
        "export UV_PYTHON_DOWNLOADS=automatic",
        "/usr/local/firm/bin/uv python install " + shlex.quote(PYTHON_VERSION),
        "python_path=$(/usr/bin/find /usr/local/firm/python -type f -name python3.12 "
        "-perm -111 -print -quit)",
        '[ -n "$python_path" ]',
        f'/bin/ln -sfn "$python_path" {PYTHON}',
        # Root runs this interpreter: nobody else may change any part of it.
        "/usr/sbin/chown -R root:wheel /usr/local/firm",
        "/bin/chmod -R go-w /usr/local/firm",
        "/usr/bin/install -d -o root -g wheel -m 755 /etc/ssh/sshd_config.d",
        "/bin/cat > /etc/ssh/sshd_config.d/firm.conf <<'EOF'",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "ChallengeResponseAuthentication no",
        "PermitRootLogin no",
        "EOF",
        "/usr/sbin/sshd -t",
        # The Lima-generated key remains the sole control path.  These files
        # are only needed to cross the initial sudo boundary.
        "/bin/rm -f /Users/firmvm/setup.sh /Users/firmvm/uv",
    ]
    setup = destination / "setup.sh"
    setup.write_text("\n".join(lines) + "\n")
    setup.chmod(0o700)
    return setup


def _shell(
    environment: dict[str, str], *args: str, timeout: float = 1200
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        ["limactl", "shell", "--tty=false", "--workdir=/", INSTANCE, *args],
        environment,
        timeout=timeout,
    )


def _install_homebrew(environment: dict[str, str]) -> dict[str, str]:
    """Clone the pinned Homebrew release as the control account, then publish it root-owned.

    This is Homebrew's documented git installation into its Apple silicon prefix,
    without the installer's interactive sudo, at the release ``HOMEBREW_TAG`` names
    and without ``brew update``, so the image is reproducible. Analytics are turned
    off in the repository's own configuration, so they stay off for the station
    control account. ``mac-prepare`` shares the prefix with every station account.
    """

    _progress(f"installing Homebrew {HOMEBREW_TAG}")
    brew = f"{HOMEBREW_PREFIX}/bin/brew"
    as_control = ("/usr/bin/env", *HOMEBREW_ENVIRONMENT)
    # A full clone checked out at the tag, not a shallow one: `--depth 1` implies a
    # single-branch clone whose only refspec is the tag, so `brew update` cannot find
    # origin's default branch ("Not a valid ref: refs/remotes/origin/main") and its
    # first `git fetch --tags` would pull the history anyway. This is the layout
    # Homebrew's own git installation leaves: every branch, every tag, origin/HEAD set.
    _shell(
        environment,
        *as_control,
        "/usr/bin/git",
        "clone",
        "--quiet",
        "--branch",
        HOMEBREW_TAG,
        HOMEBREW_REMOTE,
        HOMEBREW_PREFIX,
        timeout=1800,
    )
    # brew update follows the branch origin/HEAD names; fail the build if it is unset.
    _shell(
        environment,
        *as_control,
        "/usr/bin/git",
        "-C",
        HOMEBREW_PREFIX,
        "symbolic-ref",
        "--quiet",
        "refs/remotes/origin/HEAD",
        timeout=300,
    )
    _shell(environment, *as_control, brew, "analytics", "off", timeout=300)
    reported = _shell(environment, *as_control, brew, "--version", timeout=300)
    first = reported.stdout.decode(errors="replace").strip().splitlines()
    if not first or not first[0].startswith("Homebrew "):
        raise RuntimeError("Homebrew did not report its version")
    version = first[0].removeprefix("Homebrew ").strip()
    _shell(
        environment,
        "/bin/zsh",
        "-c",
        mac_sudo_command("/usr/sbin/chown", "-R", "-h", "root:wheel", HOMEBREW_PREFIX),
        timeout=600,
    )
    return {"prefix": HOMEBREW_PREFIX, "tag": HOMEBREW_TAG, "version": version}


def _confirm_stopped(environment: dict[str, str]) -> None:
    result = _run(["limactl", "list", "--json", INSTANCE], environment, timeout=120)
    try:
        records = parse_lima_list(result.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("limactl did not return valid JSON for the base VM") from exc
    record = next(
        (item for item in records if isinstance(item, dict) and item.get("name") == INSTANCE),
        None,
    )
    if record is None or lima_status(record) != "stopped":
        raise RuntimeError("base VM was not confirmed stopped before publishing the image")


def build_base_image(
    destination: Path,
    recipe: Recipe,
    *,
    uv_binary: Path = DEFAULT_UV,
) -> dict[str, Any]:
    """Build using short Lima socket paths, then publish the stopped image.

    Any failure, including KeyboardInterrupt, stops and removes the build VM and
    removes a destination directory this call created, so nothing partial remains.
    """
    destination = Path(destination).absolute()
    created_destination = not destination.exists() and not destination.is_symlink()
    destination = _empty_destination(destination)
    # Validated before the try block: a bad input is a refusal, not a build failure.
    for app in recipe.applications:
        _application(app)
    uv_binary = _source_file(Path(uv_binary), label="uv binary")
    # /private/tmp is world-writable and WORKSPACE_PARENT a fixed name beneath it, so
    # it is validated before anything is created inside it, same as the Lima runtime.
    _owned_directory(WORKSPACE_PARENT, private=True)
    workspace = Path(tempfile.mkdtemp(prefix="image-", dir=WORKSPACE_PARENT))
    try:
        manifest = _build_base_image(workspace, recipe, uv_binary=uv_binary)
        _move_durably(workspace, destination)
    except BaseException as exc:
        if _discard_workspace(workspace):
            outcome = "its build VM was stopped and removed"
        else:
            # Never delete a disk whose stop could have failed. This exact path is
            # sufficient to inspect/recover the build without a global VM search.
            outcome = f"recovery files remain at {workspace}"
        if created_destination and destination.is_dir() and not any(destination.iterdir()):
            destination.rmdir()
        if not isinstance(exc, Exception):
            _progress(f"interrupted; {outcome}")
            raise
        raise RuntimeError(f"Image build failed; {outcome}: {exc}") from exc
    _progress(f"published {destination / 'image.json'}")
    return manifest


def _discard_workspace(workspace: Path) -> bool:
    """Stop a failed build's VM and remove its workspace; False keeps it for recovery.

    The workspace's Lima home is private to this build, so its instance is this
    build's VM. A graceful stop gets one forced retry, and the workspace is removed
    only once Lima confirms the instance stopped.
    """

    home = workspace / "lima"
    if (home / INSTANCE).exists():
        environment = _environment(home)
        _progress(f"stopping the failed build's {INSTANCE} instance")
        for flag, timeout in (("--tty=false", 300), ("--force", 120)):
            try:
                _run(["limactl", "stop", flag, INSTANCE], environment, timeout=timeout)
            except Exception:
                pass
            try:
                _confirm_stopped(environment)
                break
            except Exception:
                continue
        else:
            return False
    try:
        remove_directory(workspace.parent, workspace.name)
    except OSError:
        return False
    return True


def _build_base_image(
    destination: Path,
    recipe: Recipe,
    *,
    uv_binary: Path = DEFAULT_UV,
) -> dict[str, Any]:
    """Create and stop a clean macOS base image under ``destination``.

    Only the recipe's application bundles are copied into the guest, at the disk
    size it names. After Xcode's first-launch setup the build installs Homebrew if
    Xcode is among them; it installs no simulator runtime (``mac-image-apply`` does
    that against a running firm's Mac VM instead). The function raises on any failed
    provisioning step and never writes ``image.json`` in that case; ``build_base_image``
    stops and removes the VM.
    """

    destination = _empty_destination(Path(destination))
    apps = tuple(_application(app) for app in recipe.applications)
    app_names = tuple(app.name for app in apps)
    if len(set(app_names)) != len(app_names):
        raise ValueError("application bundle names must be unique")
    xcode = _xcode(apps)
    uv = _source_file(Path(uv_binary), label="uv binary")
    lima_home = destination / "lima"
    environment = _environment(lima_home)
    lima_version = (
        _run(["limactl", "--version"], environment, timeout=30)
        .stdout.decode(errors="replace")
        .strip()
    )
    _progress(f"creating fresh {INSTANCE} instance")
    config_path = destination / "base.yaml"
    config_path.write_text(yaml.safe_dump(_load_config(recipe.disk_gib), sort_keys=False))
    setup = _write_setup(destination, apps)
    _run(
        [
            "limactl",
            "create",
            "--name=base",
            "--mount-none",
            "--plain",
            "--tty=false",
            str(config_path),
        ],
        environment,
    )
    _progress("starting base instance")
    _run(["limactl", "start", "--tty=false", INSTANCE], environment)

    _progress("copying uv")
    _run(["limactl", "copy", str(uv), f"{INSTANCE}:/Users/firmvm/uv"], environment)
    for app in apps:
        _progress(f"copying {app.name}")
        _run(
            ["limactl", "copy", "-r", str(app), f"{INSTANCE}:/Applications/{app.name}"],
            environment,
        )
        # Some vendor bundles contain group-writable generated metadata.
        # Seal copies as their existing owner: macOS app-management policy
        # can reject a root ownership rewrite of an already installed app.
        _shell(
            environment,
            "/usr/bin/find",
            f"/Applications/{app.name}",
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
        )
    _run(
        ["limactl", "copy", str(setup), f"{INSTANCE}:/Users/firmvm/setup.sh"],
        environment,
    )
    _progress("configuring guest")
    bootstrap = mac_sudo_command("/bin/zsh", "/Users/firmvm/setup.sh")
    _shell(environment, "/bin/zsh", "-c", bootstrap)
    homebrew = _install_homebrew(environment) if xcode is not None else None
    os_build_result = _shell(environment, "/usr/bin/sw_vers", "-buildVersion")
    os_build = os_build_result.stdout.decode(errors="replace").strip()
    if not os_build:
        raise RuntimeError("guest did not report a macOS build")
    _progress("stopping base instance")
    _run(["limactl", "stop", "--tty=false", INSTANCE], environment)
    _progress("confirming stopped base instance")
    _confirm_stopped(environment)
    manifest = {
        **image_identity(),
        "name": INSTANCE,
        "stations": recipe.stations,
        "os_build": os_build,
        "control_user": CONTROL_USER,
        "python": PYTHON,
        "lima_version": lima_version,
        "applications": list(app_names),
        "homebrew": homebrew,
    }
    (destination / "image.json").write_text(json.dumps(manifest, indent=2) + "\n")
    _progress(f"published {destination / 'image.json'}")
    return manifest


@lifecycle(exclusive=True)
def apply_recipe(vm, recipe: Recipe) -> dict[str, Any]:
    """Install every simulator runtime ``recipe`` names that ``vm``'s guest lacks.

    Idempotent: ``missing_simulators`` decides from the guest's own listing, so a rerun
    after a failed download costs nothing for what already succeeded, and a satisfied
    recipe changes nothing in the guest. Reuses ``install_helper``'s copy-and-run shape
    (``mac_enrollment.py``) with the station's own platform installer, the one install
    implementation: it reaches the guest once, and root runs it once per missing
    runtime. The installer bounds itself and holds the guest's install lock, so after
    a lost connection a rerun is refused until that install is over, never raced; this
    host waits longer than its bound. An install that leaves its runtime missing is an
    error here, not at seal. Not a package manager: this only adds.
    """

    if not vm.network.ready() or not vm._running():
        raise RuntimeError("Start the isolated Mac VM before applying a recipe to it")
    if vm.stations and len(vm.stations) != recipe.stations:
        raise ValueError(
            f"The Mac has {len(vm.stations)} stations and the recipe names {recipe.stations}"
        )
    with station_locks(vm.firm_locks, vm.stations):
        if any(
            os.path.lexists(vm.root / "stations" / str(number) / DIRTY_MARKER)
            for number in vm.stations
        ):
            raise RuntimeError("A Mac station has unverified cleanup")
        if not recipe.simulators:
            return {"installed": []}
        defaults = _default_versions(vm, recipe.simulators)
        missing = missing_simulators(recipe.simulators, _guest_runtimes(vm, recipe), defaults)
        if not missing:
            return {"installed": []}
        remote = f"/Users/firmvm/mac-station-add-platform-{uuid4().hex}.py"
        vm._lima("copy", str(ASSETS / "mac-station-add-platform.py"), f"mac:{remote}")
        try:
            for simulator in missing:
                _progress(
                    f"installing the {_runtime_names([simulator], defaults)} runtime",
                    command="mac-image-apply",
                )
                arguments = [simulator.platform, *filter(None, [simulator.version])]
                vm._shell(
                    "/bin/zsh",
                    "-c",
                    mac_sudo_command(PYTHON, remote, *arguments),
                    timeout=SIMULATOR_TIMEOUT,
                )
        finally:
            vm._shell("/bin/rm", "-f", remote, check=False)
        still = missing_simulators(recipe.simulators, _guest_runtimes(vm, recipe), defaults)
        if still:
            raise RuntimeError(
                f"Installed, but the Mac still lacks the {_runtime_names(still, defaults)} "
                "simulator runtime"
            )
        return {
            "installed": [
                {"platform": entry.platform, "version": entry.version or defaults[entry.platform]}
                for entry in missing
            ]
        }


@lifecycle(exclusive=True)
def seal_enrolled_image(sandbox, destination: Path, recipe: Recipe):
    """Validate a running enrollment VM against ``recipe``, stop it, publish a stopped copy.

    Refuses any station with unverified cleanup, a held or bound lease, a
    lease key or unfinished helper ledger; any ordinary account besides the
    control account and the recipe's stations; a disk of another size than the recipe names (that
    is a new base), or a guest missing a simulator runtime the recipe
    names (run ``mac-image-apply`` first). Everything recorded — every available
    runtime, each recipe application's bundle identifier and version, the selected
    Xcode, Homebrew — is read from the guest itself, never carried forward from the
    base image's own manifest. It preserves the station's completed setup/TCC state
    and does not sanitize its home. A failed or interrupted seal never publishes
    image.json, removes the destination and never restarts the source.
    """
    vm = sandbox.mac
    if vm is None:
        raise ValueError("The enrollment firm has no Mac")
    if len(vm.stations) != recipe.stations:
        raise ValueError(
            f"The Mac has {len(vm.stations)} stations and the recipe names {recipe.stations}"
        )
    # A clone of an enrolled image is a refresh: its accounts took their console turns
    # before that image was sealed, and it carries no progress of its own. A clone of a
    # base image has only its own progress to show for them. Either way the checks below
    # and the guest's seal script verify the accounts as they are now.
    if not vm.enrolled():
        progress = vm._state().get("enrollment_progress")
        if not isinstance(progress, dict) or progress.get("phase") != "complete":
            raise RuntimeError("Finish every mac-prepare console turn before sealing")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new image directory")
    if destination == vm.root or vm.root in destination.parents:
        raise ValueError("The image must be outside the source VM directory")
    if not vm._running() or not vm.network.ready():
        raise RuntimeError("Start the isolated enrollment VM for readiness checks before sealing")
    # Every clone's Lima configuration carries the size its base was built with.
    disk = yaml.safe_load((vm.instance / "lima.yaml").read_text()).get("disk")
    if disk != f"{recipe.disk_gib}GiB":
        raise RuntimeError(
            f"The Mac's disk is {disk} and the recipe names {recipe.disk_gib}GiB; "
            "build a new base for a different disk size"
        )
    remote = f"/Users/firmvm/mac-station-seal-{uuid4().hex}.py"
    # Hold every station lock through readiness, guest validation and shutdown so a
    # lease cannot race publication of any account in the shared machine.
    with sandbox._station_locks():
        for number in vm.stations:
            station = sandbox.mac_station(number)
            if os.path.lexists(station.root / DIRTY_MARKER):
                raise RuntimeError(f"Mac station {number} has unverified cleanup")
            client = station.probe()
            try:
                state = client.request("status")
                if (
                    state.get("ready") is not True
                    or state.get("clean") is not True
                    or state.get("lease_bound") is not False
                    or state.get("environment") != "vm-session"
                ):
                    raise RuntimeError(
                        f"Enroll and release Mac station {number} before sealing its image"
                    )
            finally:
                client.close()
        vm._lima("copy", str(ASSETS / "mac-station-seal.py"), f"mac:{remote}")
        try:
            # An install can outlive a mac-image-apply whose connection was lost. Asked
            # first, it is either still running and refused here, or over and in the
            # listing below. Nothing starts one afterwards: apply takes the firm's
            # exclusive lifecycle lock as this does, the station lock above keeps leases
            # out, and the seal script holds the guest's install lock while it runs the
            # station's own Homebrew.
            vm._shell(
                "/bin/zsh",
                "-c",
                mac_sudo_command(PYTHON, remote, "--no-install-running"),
                timeout=60,
            )
            # Read as the control account, and before the seal script changes anything.
            runtimes = _guest_runtimes(vm, recipe)
            defaults = _default_versions(vm, recipe.simulators)
            missing = missing_simulators(recipe.simulators, runtimes, defaults)
            if missing:
                raise RuntimeError(
                    f"The Mac lacks the {_runtime_names(missing, defaults)} simulator "
                    "runtime; run mac-image-apply first"
                )
            seal_recipe = json.dumps(
                {
                    "applications": [app.name for app in recipe.applications],
                    "stations": recipe.stations,
                }
            )
            command = mac_sudo_command(PYTHON, remote, seal_recipe)
            result = vm._shell("/bin/zsh", "-c", command, timeout=660)
            checked = json.loads(result.stdout)
        finally:
            vm._shell("/bin/rm", "-f", remote, check=False)
        sandbox.down()
        # The destination did not exist; a failed or interrupted copy removes it, so
        # no directory without image.json is left to look like an image.
        destination.mkdir(mode=0o700, parents=True)
        try:
            private_directory(destination)
            home = destination / "lima"
            base = home / INSTANCE
            keys = home / "_config"
            for path in (home, base, keys):
                private_directory(path)
            for name in (
                "disk",
                "disk.patched",
                "vz-aux",
                "vz-hwmodel",
                "vz-identifier",
                "lima-version",
                "lima.yaml",
            ):
                source = vm.instance / name
                _regular_source(source)
                vm.commands.run(["/bin/cp", "-c", str(source), str(base / name)], timeout=600)
                (base / name).chmod(0o600)
            for name in ("user", "user.pub"):
                source = vm.home / "_config" / name
                _regular_source(source)
                shutil.copyfile(source, keys / name)
                (keys / name).chmod(0o600)
            # Confirm Lima can recognize the result as a stopped base before making
            # it eligible for MacVM._image_base and normal runtime cloning.
            commands = Commands({**vm.commands.env, "LIMA_HOME": str(home)})
            listing = commands.run([vm.lima, "list", "--json", INSTANCE])
            rows = parse_lima_list(listing.stdout)
            if len(rows) != 1 or lima_status(rows[0]) != "stopped":
                raise RuntimeError("Sealed base did not report stopped")
            manifest = {
                **image_identity(),
                "name": INSTANCE,
                "control_user": CONTROL_USER,
                "python": PYTHON,
                "os_build": checked["os_build"],
                "enrollment": checked["enrollment"],
                "lima_version": vm._lima("--version").stdout.decode().strip(),
                "applications": checked["applications"],
                "xcode": checked["xcode"],
                "simulators": every_available_runtime(runtimes),
                "homebrew": checked["homebrew"],
                "recipe": {
                    "disk_gib": recipe.disk_gib,
                    "stations": recipe.stations,
                    "applications": [app.name for app in recipe.applications],
                    "simulators": [
                        {"platform": s.platform, "version": s.version} for s in recipe.simulators
                    ],
                    "sha256": recipe.sha256,
                },
            }
            save(destination / "image.json", manifest)
            return manifest
        except BaseException:
            remove_directory(destination.parent, destination.name)
            raise


def _regular_source(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Image source assets must be owner-controlled regular files")
