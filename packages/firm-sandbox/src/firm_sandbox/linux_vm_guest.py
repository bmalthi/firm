"""Guest controller of the firm's Linux VM, installed root-owned; JSON over the control channel.

One request on stdin, one ``ok``/``error`` reply on stdout, under a global lock, which
``sample``, ``export``, ``child-status`` and ``child-logs`` skip; only ``export``
replies with a tar stream instead of JSON. Every role is a Linux account whose work
runs inside one systemd slice; the file server's steps are the ``files`` operation of
the same controller. This module is self-contained so the guest needs nothing but its
Python.

Every privileged step (accounts, systemd, mounts, processes) goes through ``Privileged``.
Contract tests run the controller in simulation: those steps are recorded, not run, and
every pure step (journal, ledger, fingerprint, subordinate ranges, unit files, cgroup
check, the file server's trees) runs for real against a temporary tree.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import io
import json
import math
import os
import pwd
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

STATE = Path("/var/lib/firm")  # ledger, run journals
RUN_ROOT = Path("/run/firm")  # per-role state and credentials; tmpfs, gone on reboot
HOMES = Path("/home")
SUBID = Path("/etc")  # subuid and subgid
UNITS = Path("/etc/systemd/system")
LOGIND_CONF = Path("/etc/systemd/logind.conf.d/firm.conf")
CGROUP = Path("/sys/fs/cgroup")
PROC = Path("/proc")
ROOT_FILESYSTEM = "/"
SMB_CONF = Path("/etc/samba/smb.conf")
PYTHON = "/usr/bin/python3"
CONTROLLER = "/usr/local/libexec/firm-controller.py"
LIMIT = 8 * 1024 * 1024
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\.[a-z0-9]{1,8}")
MAX_LOG_BYTES = 64 * 1024 * 1024
CHILD_LOG_BYTES = 4 * 1024 * 1024
CHILD_STOP_TIMEOUT = 2.0
SETTLE_TIMEOUT = 20.0
DOCKER_RECLAIM_TIMEOUT = 2.0  # Per category; leave most of settlement for stopping the slice.
SETTLE_REPORT_MARGIN = 0.2
SCRATCH_MAX_DEPTH = 32  # Each level holds a directory fd and a scandir fd, plus the home fd.
ACCOUNT_PREFIX = "firmrole-"
UID_START, UID_COUNT = 2000, 48000
SUBID_START, SUBID_COUNT = 100000, 65536
# Firm assigns role ranges itself; Samba identities need none.
USERADD_NO_SUBIDS = ("-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0")
# The role's drive mount: files appear owned by the account, and the server's ACLs,
# not client-side mode checks, decide access. uid= and gid= are added per role.
CIFS_OPTIONS = (
    "vers=3.1.1,sec=ntlmssp,forceuid,forcegid,file_mode=0700,dir_mode=0700,"
    "noperm,mfsymlinks,actimeo=1"
)


# ---- identity -------------------------------------------------------------------------


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ValueError("Invalid identifier")
    return value


def firm_identifier(value):
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", value)
    ):
        raise ValueError("Invalid firm identifier")
    return value


def allocate_identity(seed, *, account_prefix, uid_start, uid_count, used_uids):
    """A deterministic account name and UID; callers reserve retired UIDs in used_uids."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    uid = uid_start + int(digest[:8], 16) % uid_count
    while uid in used_uids:
        uid = uid_start + (uid - uid_start + 1) % uid_count
    return account_prefix + digest[:12], uid


def role_account(role_id):
    """The execution account of a role; ``Layout.for_role`` derives the home from it."""
    return ACCOUNT_PREFIX + hashlib.sha256(identifier(role_id).encode()).hexdigest()[:12]


# ---- the privileged seam ----------------------------------------------------------------


class Privileged:
    """Every step that needs root: commands, account lookups, ownership, mounts, spawning.

    Under ``simulate`` commands are recorded and answered from ``replies`` (keyed by an
    argv prefix); accounts created by a recorded ``useradd`` become visible to later
    lookups; mounts recorded by ``mount.cifs`` are reported mounted until ``umount``;
    ownership set through ``chown`` is what ``owner`` reports.
    """

    def __init__(self, *, simulate=False):
        self.simulate = simulate
        self.commands = []
        self.environments = []
        self.replies = {}
        self.accounts = {}
        self.mounts = set()
        self.owned = {}
        self.spawned = []
        self.processes = []

    def run(self, argv, *, input=None, env=None, check=True, timeout=None):
        argv = [str(item) for item in argv]
        self.commands.append(argv)
        self.environments.append(env)
        if not self.simulate:
            return subprocess.run(
                argv,
                input=input,
                env=env,
                check=check,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        code, output = next(
            (
                reply
                for prefix, reply in self.replies.items()
                if tuple(argv[: len(prefix)]) == prefix
            ),
            (0, ""),
        )
        if argv[0] == "useradd" and not code:
            name = argv[-1]
            uid = int(argv[argv.index("-u") + 1]) if "-u" in argv else 65533
            gid = int(argv[argv.index("-g") + 1]) if "-g" in argv else uid
            home = argv[argv.index("-d") + 1] if "-d" in argv else ""
            self.accounts[name] = SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid, pw_dir=home)
        elif argv[0] == "mount.cifs" and not code:
            self.mounts.add(argv[2])
        elif argv[0] == "umount":
            self.mounts.discard(argv[-1])
        if check and code:
            raise subprocess.CalledProcessError(code, argv, output)
        return subprocess.CompletedProcess(argv, code, output, output)

    def lookup(self, *, name=None, uid=None):
        if self.simulate:
            for entry in self.accounts.values():
                if entry.pw_name == name or entry.pw_uid == uid:
                    return entry
            return None
        try:
            return pwd.getpwnam(name) if name is not None else pwd.getpwuid(uid)
        except KeyError:
            return None

    def chown(self, fd, uid, gid):
        self.owned[os.fstat(fd).st_ino] = (uid, gid)
        if not self.simulate:
            os.fchown(fd, uid, gid)

    def owner(self, fd):
        if self.simulate:
            return self.owned.get(os.fstat(fd).st_ino, (0, 0))[0]
        return os.fstat(fd).st_uid

    def mounted(self, point):
        return point in self.mounts if self.simulate else os.path.ismount(point)

    @contextmanager
    def as_user(self, info):
        if self.simulate:
            yield
            return
        os.setgroups([info["gid"]])
        os.setegid(info["gid"])
        os.seteuid(info["uid"])
        try:
            yield
        finally:
            os.seteuid(0)
            os.setegid(0)

    def create_child_cgroup(self, path):
        path.mkdir()  # The live parent's slice must already exist.
        if self.simulate:
            (path / "cgroup.procs").write_text("")
            (path / "cgroup.kill").write_text("")
            (path / "cgroup.events").write_text("populated 0\n")

    def kill_child_cgroup(self, path):
        (path / "cgroup.kill").write_text("1")
        if self.simulate:
            (path / "cgroup.procs").write_text("")
            (path / "cgroup.events").write_text("populated 0\n")

    def remove_child_cgroup(self, path):
        if self.simulate:
            for name in ("cgroup.procs", "cgroup.kill", "cgroup.events"):
                (path / name).unlink()
        path.rmdir()

    def spawn(self, argv, **options):
        argv = [str(item) for item in argv]
        self.spawned.append((argv, options))
        if not self.simulate:
            return subprocess.Popen(argv, **options)
        if self.processes:
            return self.processes.pop(0)
        return SimpleNamespace(pid=4242, wait=lambda: 0, stdout=io.BytesIO(b""))


privileged = Privileged()


@contextmanager
def simulation(recorder=None):
    """Run the controller against the module's paths with privileged steps recorded."""
    global privileged
    previous, privileged = privileged, recorder or Privileged(simulate=True)
    try:
        yield privileged
    finally:
        privileged = previous


# ---- ledger and journal ----------------------------------------------------------------


def save(path, value):
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def lock():
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def database():
    path = STATE / "firm.json"
    return json.loads(path.read_text()) if path.exists() else {"roles": {}, "next": 0}


def boot():
    return (PROC / "sys/kernel/random/boot_id").read_text().strip()


def role_record(role):
    identifier(role)
    return database()["roles"][role]


def initialize(request):
    firm_id = firm_identifier(request["firm_id"])
    data = database()
    if data.get("firm_id", firm_id) != firm_id:
        raise ValueError("This guest is already bound to another firm")
    # The guest controller owns manager lifetime. Do not let logind stop Docker after
    # its short administrative sessions end; lingering remains disabled.
    config = "[Login]\nUserStopDelaySec=infinity\n"
    if not LOGIND_CONF.exists() or LOGIND_CONF.read_text() != config:
        LOGIND_CONF.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        LOGIND_CONF.write_text(config)
        LOGIND_CONF.chmod(0o644)
    # Retry the reload even when the file survived an interrupted initialization.
    privileged.run(["systemctl", "reload", "systemd-logind"])
    data["firm_id"] = firm_id
    STATE.mkdir(mode=0o700, exist_ok=True)
    (STATE / "runs").mkdir(mode=0o700, exist_ok=True)
    save(STATE / "firm.json", data)
    return {"firm_id": firm_id, "state": str(STATE)}


def run_path(run_id):
    return STATE / "runs" / identifier(run_id)


@contextmanager
def run_lock(run_id):
    with (run_path(run_id) / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ---- accounts, ranges and units --------------------------------------------------------


def managed_directory(path, uid, gid, mode):
    """Create a trusted directory without following a substituted ancestor."""
    path = Path(path)
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            os.mkdir(path.name, mode=mode, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if privileged.owner(fd) not in (0, uid):
                raise ValueError(f"Managed directory has an unexpected owner: {path}")
            privileged.chown(fd, uid, gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def subordinate_range(account, index):
    """Reserve ``65536`` subordinate IDs per role, written to subuid and subgid."""
    start = SUBID_START + index * SUBID_COUNT
    line = f"{account}:{start}:{SUBID_COUNT}\n"
    missing = []
    for name in ("subuid", "subgid"):
        path = SUBID / name
        lines = path.read_text().splitlines(keepends=True) if path.exists() else []
        assigned = False
        for entry in lines:
            if not entry.strip():
                continue
            owner, first, count = entry.strip().split(":")
            first, count = int(first), int(count)
            if owner == account:
                if (first, count) != (start, SUBID_COUNT):
                    raise ValueError(f"{account} already has another subordinate range in {name}")
                assigned = True
            elif first < start + SUBID_COUNT and start < first + count:
                raise ValueError(f"Subordinate range for {account} overlaps {owner} in {name}")
        if not assigned:
            missing.append(path)
    # Validate both files before writing either; a partial write can be retried.
    for path in missing:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "a") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o644)
    return [start, SUBID_COUNT]


def slice_unit(uid):
    # systemd and runc derive OwnerUID from this exact slice name. A dedicated
    # account per role makes the standard user slice its complete boundary.
    return f"user-{int(uid)}.slice"


def slice_cgroup(uid):
    """The slice's cgroup; systemd nests ``a-b-c.slice`` under ``a-b.slice`` under ``a.slice``."""
    parts = slice_unit(uid).removesuffix(".slice").split("-")
    units = ("-".join(parts[:count]) + ".slice" for count in range(1, len(parts) + 1))
    return CGROUP.joinpath(*units)


def slice_empty(uid):
    """Trust only the kernel's recursive populated signal, or an absent slice.

    Enumerating descendant cgroups can miss processes moving between them. Missing
    or unreadable events on an existing cgroup never prove quiescence.
    """
    root = slice_cgroup(uid)
    try:
        events = (root / "cgroup.events").read_text()
    except FileNotFoundError:
        try:
            root.stat()
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False
    except OSError:
        return False
    return [line.split() for line in events.splitlines() if line.startswith("populated")] == [
        ["populated", "0"]
    ]


def read_number(path):
    """The first integer of a one-value kernel file; ``None`` when it cannot be read."""
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_field(path, key):
    """One ``<key> <integer>`` line of a kernel file; ``None`` when it cannot be read."""
    try:
        for line in path.read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value)
    except (OSError, ValueError):
        return None
    return None


def slice_usage(uid):
    """What the role's slice used, read while it still exists; ``None`` once it is gone.

    ``memory.peak`` counts page cache, so it is an upper bound on demand, not demand.
    The counters cover the whole run, including containers that already exited, because
    the slice is created at run start and removed when it stops.
    """
    root = slice_cgroup(uid)
    if not root.is_dir():
        return None
    return {
        "memory_peak_bytes": read_number(root / "memory.peak"),
        "cpu_usec": read_field(root / "cpu.stat", "usage_usec"),
        "oom_kills": read_field(root / "memory.events", "oom_kill"),
    }


def write_units(role, uid):
    """Name the dedicated user's standard slice without relocating its manager."""
    for path, text in ((UNITS / slice_unit(uid), f"[Unit]\nDescription=Firm role {role}\n"),):
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o644)


def role_scope(info, argv):
    """Enter the role's slice as its account: the one seam for the scope command.

    If systemd-run refuses ``--uid`` in scope mode, the fallback is
    ``setpriv --reuid=<uid> --regid=<gid> --init-groups`` inside the scope.
    """
    return [
        "systemd-run",
        "--quiet",
        "--collect",
        "--scope",
        f"--slice={slice_unit(info['uid'])}",
        f"--uid={info['uid']}",
        f"--gid={info['gid']}",
        "--",
        *argv,
    ]


def user_systemctl(info, *args):
    return ["systemctl", "--user", "--machine", f"{info['account']}@.host", *args]


def role_environment(info):
    return {
        "HOME": info["home"],
        "USER": info["account"],
        "LOGNAME": info["account"],
        "SHELL": "/bin/bash",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{info['uid']}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{info['uid']}/bus",
        "DOCKER_HOST": f"unix:///run/user/{info['uid']}/docker.sock",
        # Named so a task, whose HOME is its own, still uses the role's proxy settings.
        "DOCKER_CONFIG": f"{info['home']}/.docker",
    }


PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


def start_services(info, environment=None):
    """The user manager and the rootless daemon, started at run start under the slice.

    The daemon pulls images through the run's egress proxy: the user manager imports the
    proxy variables from this call's environment, never from argv, where every account
    could read the role's proxy credential.
    """
    privileged.run(["systemctl", "start", f"user@{info['uid']}.service"])
    proxy = {name: value for name, value in (environment or {}).items() if name in PROXY_VARIABLES}
    if proxy:
        privileged.run(
            user_systemctl(info, "import-environment", *sorted(proxy)),
            env={**os.environ, **proxy},
        )
        write_docker_proxies(info, proxy)
    privileged.run(user_systemctl(info, "start", "docker.service"))


# Nothing answers DNS here, inside a container as in the guest: a lookup is refused at
# once instead of timing out on Docker's public fallback, which the filter drops.
CONTAINER_RESOLVER = "127.0.0.1"


def write_docker_proxies(info, proxy):
    """Docker's own settings for the run's egress proxy, in the role's home.

    ``~/.docker/config.json`` ``proxies``: the Docker CLI injects them into every
    container it runs and every build. ``~/.config/docker/daemon.json`` ``dns``: the
    rootless daemon gives containers a resolver that refuses at once. Other settings in
    both files are kept.
    """
    url = proxy.get("HTTPS_PROXY") or proxy.get("https_proxy")
    if not url:
        return
    settings = {
        "httpProxy": proxy.get("HTTP_PROXY") or proxy.get("http_proxy") or url,
        "httpsProxy": url,
        "noProxy": proxy.get("NO_PROXY") or proxy.get("no_proxy") or "",
    }

    def proxies(config):
        current = config.get("proxies")
        config["proxies"] = {**(current if isinstance(current, dict) else {}), "default": settings}

    update_role_json(info, (".docker",), "config.json", proxies)
    update_role_json(
        info,
        (".config", "docker"),
        "daemon.json",
        lambda config: config.update(dns=[CONTAINER_RESOLVER]),
    )


def update_role_json(info, directories, name, update):
    """Apply ``update`` to a JSON object under the role's home, creating what is missing.

    Done as the account without following links, so the role's own home can never steer
    a root write; the file is 0600 because it may hold the role's proxy credential. An
    unreadable or non-object file starts from an empty object.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with privileged.as_user(info):
        directory = os.open(info["home"], flags)
        try:
            for part in directories:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd) as stream:
                    config = json.load(stream)
            except (OSError, ValueError):
                config = {}
            if not isinstance(config, dict):
                config = {}
            update(config)
            staged = f".{name}.{os.getpid()}"
            fd = os.open(
                staged,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(config, stream)
            os.replace(staged, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            os.close(directory)


def reclaim_counts():
    return {"count": 0, "bytes": 0, "skipped": {}, "completed": False, "timed_out": False}


def reclaim_skipped(reason, *, timed_out=False):
    # A lost reply may have removed something. Unknown must not read as zero reclaimed.
    return {
        "count": None,
        "bytes": None,
        "skipped": {reason: 1},
        "completed": False,
        "timed_out": timed_out,
    }


def reclaim_docker(info, deadline):
    """The daemon's prune API gives exact byte counts without parsing CLI prose.

    Only these endpoints are allowed: no volumes, networks or `all` image/build prune.
    A request that times out can still be in flight until the slice is stopped below.
    """
    categories = {}
    for category, endpoint, field in (
        ("docker_containers", "containers/prune", "ContainersDeleted"),
        (
            "docker_image_deletion_records",
            "images/prune?filters=" + quote('{"dangling":["true"]}'),
            "ImagesDeleted",
        ),
        ("docker_build_cache", "build/prune?all=false", "CachesDeleted"),
    ):
        timeout = min(DOCKER_RECLAIM_TIMEOUT, deadline - time.monotonic())
        if timeout <= 0:
            categories[category] = reclaim_skipped("deadline", timed_out=True)
            continue
        try:
            result = privileged.run(
                [
                    "/usr/bin/setpriv",
                    f"--reuid={info['uid']}",
                    f"--regid={info['gid']}",
                    "--clear-groups",
                    "--",
                    "/usr/bin/curl",
                    "--disable",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--globoff",
                    "--noproxy",
                    "*",
                    "--max-time",
                    str(timeout),
                    "--unix-socket",
                    f"/run/user/{info['uid']}/docker.sock",
                    "--request",
                    "POST",
                    "http://localhost/" + endpoint,
                ],
                env=role_environment(info),
                timeout=timeout,
            )
            report = json.loads(result.stdout)
            removed = report[field] or []
            size = report["SpaceReclaimed"]
            if not isinstance(removed, list) or type(size) is not int or size < 0:
                raise ValueError("Invalid prune counters")
            categories[category] = {
                **reclaim_counts(),
                "count": len(removed),
                "bytes": size,
                "completed": True,
            }
        except subprocess.TimeoutExpired:
            categories[category] = reclaim_skipped("deadline", timed_out=True)
        except subprocess.CalledProcessError as exc:
            categories[category] = reclaim_skipped(
                "deadline" if exc.returncode == 28 else "docker_unavailable",
                timed_out=exc.returncode == 28,
            )
        except (OSError, ValueError, KeyError, TypeError):
            categories[category] = reclaim_skipped("docker_unavailable")
    return categories


def reclaim_scratch(info, deadline):
    """Remove only home/scratch, without following links or traversing mounts.

    The slice is empty. Root can remove files a container wrote with subordinate IDs;
    descriptor-relative traversal keeps that authority inside the named category.
    """
    counts = reclaim_counts()
    home = Path(info["home"])
    scratch = home / "scratch"

    def check_deadline():
        if time.monotonic() >= deadline:
            counts["timed_out"] = True
            raise TimeoutError

    def unlink(parent, name, entry):
        os.unlink(name, dir_fd=parent)
        # Unlinking a hard link does not reclaim its other names' bytes.
        if entry.st_nlink == 1:
            counts["bytes"] += entry.st_size
        counts["count"] += 1

    def open_directory(parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            return name, fd, os.scandir(fd)
        except BaseException:
            os.close(fd)
            raise

    def clear(parent, name, device):
        check_deadline()
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if entry.st_dev != device:
            raise OSError("mount")
        if not stat.S_ISDIR(entry.st_mode):
            unlink(parent, name, entry)
            return
        stack = [open_directory(parent, name)]
        promoted = False
        try:
            while stack:
                check_deadline()
                current, fd, children = stack[-1]
                child = next(children, None)
                if child is None:
                    if len(stack) == 1 and promoted:
                        # Directory iteration need not see names added during this pass.
                        children.close()
                        stack[-1] = current, fd, os.scandir(fd)
                        promoted = False
                        continue
                    children.close()
                    os.close(fd)
                    stack.pop()
                    os.rmdir(current, dir_fd=stack[-1][1] if stack else parent)
                    counts["count"] += 1
                    continue
                entry = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if entry.st_dev != device:
                    raise OSError("mount")
                if not stat.S_ISDIR(entry.st_mode):
                    unlink(fd, child.name, entry)
                elif len(stack) < SCRATCH_MAX_DEPTH:
                    stack.append(open_directory(fd, child.name))
                else:
                    # No writers remain. Lift the subtree within scratch so even an
                    # interrupted attempt reduces depth, without opening more fds.
                    root = stack[0][1]
                    while True:
                        check_deadline()
                        fresh = ".firm-reclaim-" + os.urandom(16).hex()
                        try:
                            os.stat(fresh, dir_fd=root, follow_symlinks=False)
                        except FileNotFoundError:
                            break
                    os.rename(child.name, fresh, src_dir_fd=fd, dst_dir_fd=root)
                    promoted = True
        finally:
            for _, fd, children in reversed(stack):
                children.close()
                os.close(fd)

    try:
        # st_dev alone misses bind mounts on the same filesystem.
        mounts = (PROC / "self/mountinfo").read_text().splitlines()
        for line in mounts:
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
            path = Path(mount)
            if path == home or path == scratch or scratch in path.parents:
                counts["skipped"]["mount"] = 1
                return counts
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                clear(fd, "scratch", os.fstat(fd).st_dev)
            except FileNotFoundError:
                pass
            os.mkdir("scratch", mode=0o700, dir_fd=fd)
            child = os.open("scratch", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                privileged.chown(child, info["uid"], info["gid"])
            finally:
                os.close(child)
        finally:
            os.close(fd)
        counts["completed"] = True
    except TimeoutError:
        counts["skipped"]["deadline"] = 1
    except OSError as exc:
        counts["skipped"][f"errno:{exc.errno}"] = 1
    return counts


def scratch_worker(info, deadline):
    worker_deadline = deadline - SETTLE_REPORT_MARGIN
    timeout = worker_deadline - time.monotonic()
    if timeout <= 0:
        return reclaim_skipped("deadline", timed_out=True)
    try:
        if privileged.simulate:
            return reclaim_scratch(info, worker_deadline - 0.1)
        # An independent process also bounds a filesystem call that never returns.
        result = privileged.run(
            [PYTHON, "-I", CONTROLLER, "--reclaim-scratch"],
            input=json.dumps({"info": info, "deadline": worker_deadline - 0.1}),
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return reclaim_skipped("deadline", timed_out=True)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return reclaim_skipped("worker_failed")


def settle(info, *, reclaim=None):
    """Stop everything of the role and confirm its slice is empty.

    The daemon stop may fail; the slice stop is what kills the containers. A slice
    still holding processes after the timeout leaves the run unresolved.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    if reclaim is not None:
        # Leave shutdown and scratch half the budget even if a prune call stalls.
        reclaim["categories"] = reclaim_docker(info, deadline - 10)
        reclaim["categories"]["scratch"] = reclaim_skipped("slice_not_empty")
    try:
        privileged.run(
            user_systemctl(info, "stop", "docker.service"),
            check=False,
            timeout=max(0.001, min(2, deadline - time.monotonic())),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # A daemon failure never skips the slice stop or its verification.
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise RuntimeError("Role slice stop deadline expired")
    try:
        # Wait for the stop job itself; queued or failed stops do not authorize cleanup.
        privileged.run(["systemctl", "stop", slice_unit(info["uid"])], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Role slice stop did not complete") from exc
    while time.monotonic() < deadline:
        if slice_empty(info["uid"]):
            if reclaim is not None:
                reclaim["categories"]["scratch"] = scratch_worker(info, deadline)
                if not reclaim["categories"]["scratch"]["completed"]:
                    raise RuntimeError("Role scratch could not be recreated empty")
            return
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise RuntimeError("Role slice emptiness could not be confirmed")


def install_docker(info):
    """Rootless Docker for the account; the unit is never enabled and nothing lingers."""
    privileged.run(["systemctl", "start", f"user@{info['uid']}.service"])
    privileged.run(
        role_scope(info, ["dockerd-rootless-setuptool.sh", "install"]), env=role_environment(info)
    )
    privileged.run(user_systemctl(info, "disable", "docker.service"))
    settle(info)


def provision(role):
    identifier(role)
    data = database()
    existing = data["roles"].get(role)
    if existing is not None:
        if existing.get("retired"):
            raise ValueError("Retired role IDs cannot be reused; use a new role ID")
        if existing.get("retiring"):
            raise ValueError("Role retirement must finish before further use")
        return existing
    pending = data.get("pending")
    if pending is not None and pending["role_id"] != role:
        raise RuntimeError("Finish provisioning the pending role first")
    if pending is None:
        used = {item.get("uid") for item in data["roles"].values()}
        account, uid = allocate_identity(
            role,
            account_prefix=ACCOUNT_PREFIX,
            uid_start=UID_START,
            uid_count=UID_COUNT,
            used_uids=used,
        )
        # Persist intent first: an interrupted creation can only finish for this role,
        # and its UID and subordinate range stay reserved with the retired ones.
        pending = {"role_id": role, "account": account, "uid": uid, "index": data["next"]}
        data["pending"] = pending
        save(STATE / "firm.json", data)
    account, uid, index = pending["account"], pending["uid"], pending["index"]
    home = HOMES / account
    entry = privileged.lookup(name=account)
    if entry is None:
        if privileged.lookup(uid=uid) is not None:
            raise ValueError("Reserved role UID is occupied by an unrelated account")
        privileged.run(["groupadd", "-g", str(uid), account])
        privileged.run(
            [
                "useradd",
                *USERADD_NO_SUBIDS,
                "-M",
                "-d",
                str(home),
                "-s",
                "/bin/bash",
                "-u",
                str(uid),
                "-g",
                str(uid),
                account,
            ]
        )
        entry = privileged.lookup(name=account)
        if entry is None:
            raise RuntimeError(f"Created role account is not visible: {account}")
    if entry.pw_uid != uid or entry.pw_dir != str(home):
        raise ValueError("Reserved role account name is occupied by an unrelated account")
    gid = entry.pw_gid
    managed_directory(home, uid, gid, 0o700)
    for name in ("work", "drive"):
        managed_directory(home / name, uid, gid, 0o700)
    info = {
        "role_id": role,
        "account": account,
        "uid": uid,
        "gid": gid,
        "index": index,
        "slice": slice_unit(uid),
        "home": str(home),
        "drive": str(home / "drive"),
        "state": str(RUN_ROOT / role / "state"),
        "credentials": str(RUN_ROOT / role / "credentials"),
        "subordinate": subordinate_range(account, index),
    }
    write_units(role, uid)
    privileged.run(["systemctl", "daemon-reload"])
    install_docker(info)
    data = database()
    data["roles"][role] = info
    data["next"] = index + 1
    data.pop("pending", None)
    save(STATE / "firm.json", data)
    return info


# ---- runs ------------------------------------------------------------------------------


def request_fingerprint(request):
    value = {
        "role_id": request["role_id"],
        "argv": request["argv"],
        "env": request.get("env", {}),
        "stdin": request.get("stdin", ""),
        "organization": request.get("organization"),
        "drive": request.get("drive"),
        "secrets": request.get("secrets", {}),
        "secret_env": request.get("secret_env", {}),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def supervisor_identity(pid, run_id):
    try:
        result = privileged.run(["ps", "-p", str(pid), "-o", "args="], check=False)
    except OSError:
        return False
    if result.returncode != 0:
        return False
    expected = r"(?:^|\s)--supervise\s+" + re.escape(run_id) + r"(?:\s|$)"
    command = result.stdout.strip()
    return CONTROLLER in command and re.search(expected, command) is not None


def status(run_id):
    path = run_path(run_id)
    record = json.loads((path / "record.json").read_text())
    # A cheap stat, not a read: the dispatcher uses this to skip re-fetching a log
    # that has not grown, so it must be honest even before the log file exists.
    try:
        log_bytes = (path / "log").stat().st_size
    except FileNotFoundError:
        log_bytes = 0
    result = path / "exit.json"
    if result.exists():
        finished = {
            **record,
            **json.loads(result.read_text()),
            "running": False,
            "log_bytes": log_bytes,
        }
        if finished.get("cleanup_error"):
            finished.update({"status": "unresolved", "unresolved": True})
        return finished
    if record["boot"] != boot():
        # Nothing from an earlier boot survives, and /run/firm went with it.
        for name in ("request.json", "stdin"):
            (path / name).unlink(missing_ok=True)
        settlement = read_settlement(path)
        return {
            **record,
            "running": False,
            "exit_code": 125,
            "status": "interrupted",
            "interrupted": True,
            "unresolved": False,
            "log_bytes": log_bytes,
            "usage": settlement.get("usage"),
            "reclaim": settlement.get("reclaim"),
        }
    supervisor = record.get("supervisor")
    if supervisor and supervisor_identity(supervisor, run_id):
        try:
            os.kill(supervisor, 0)
        except (ProcessLookupError, PermissionError):
            return {
                **record,
                "running": False,
                "exit_code": 125,
                "status": "unresolved",
                "unresolved": True,
                "error": "Supervisor disappeared before exit journal was written",
                "log_bytes": log_bytes,
            }
        return {
            **record,
            "running": True,
            "status": "running",
            "unresolved": False,
            "log_bytes": log_bytes,
        }
    return {
        **record,
        "running": False,
        "exit_code": 125,
        "status": "unresolved",
        "unresolved": True,
        "error": "Supervisor identity could not be verified",
        "log_bytes": log_bytes,
    }


def runs():
    directory = STATE / "runs"
    return [status(p.name) for p in directory.iterdir()] if directory.exists() else []


def validate_secrets(request):
    secrets = request.get("secrets", {})
    mapping = request.get("secret_env", {})
    if not isinstance(secrets, dict) or not isinstance(mapping, dict):
        raise ValueError("Invalid secret transport")
    decoded = {}
    for name, value in secrets.items():
        identifier(name)
        try:
            decoded[name] = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid secret encoding") from exc
    for variable, name in mapping.items():
        if (
            not isinstance(variable, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable)
            or not isinstance(name, str)
            or name not in decoded
        ):
            raise ValueError("Invalid secret environment mapping")
        try:
            value = decoded[name].decode()
        except UnicodeDecodeError as exc:
            raise ValueError("Secret environment values must be text") from exc
        if "\0" in value:
            raise ValueError("Secret environment values must not contain NUL bytes")
    return decoded


def validate_drive(request):
    drive = request.get("drive")
    if drive is None:
        return None
    if (
        not isinstance(drive, dict)
        or set(drive) != {"user", "password", "port", "share"}
        or not all(
            isinstance(drive[key], str) and drive[key] and not set(drive[key]) & set("\0\n/@:,")
            for key in ("user", "password", "share")
        )
        or type(drive["port"]) is not int
        or not 1 <= drive["port"] <= 65535
    ):
        raise ValueError("Invalid drive credentials")
    return drive


def prepare_run_state(info):
    """Fresh tmpfs state and credentials directories owned by the account."""
    clean_run_state(info)
    # Scratch must exist with the role's ownership before launch.
    managed_directory(Path(info["home"]) / "scratch", info["uid"], info["gid"], 0o700)
    role_root = RUN_ROOT / info["role_id"]
    managed_directory(RUN_ROOT, 0, 0, 0o755)
    managed_directory(role_root, 0, 0, 0o711)
    for name in ("state", "credentials"):
        managed_directory(role_root / name, info["uid"], info["gid"], 0o700)


def clean_run_state(info):
    role_root = RUN_ROOT / info["role_id"]
    if role_root.is_symlink():
        raise RuntimeError("Run state root must not be a symlink")
    if role_root.is_dir():
        shutil.rmtree(role_root)
    children = STATE / "children"
    for path in children.iterdir() if children.exists() else ():
        record = json.loads((path / "record.json").read_text())
        if record.get("role_id") == info["role_id"]:
            shutil.rmtree(STATE / "task-homes" / path.name, ignore_errors=True)


def start(request):
    role, run_id = identifier(request["role_id"]), identifier(request["run_id"])
    argv = request["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) and "\0" not in a for a in argv)
    ):
        raise ValueError("Invalid command")
    environment = request.get("env", {})
    if not isinstance(environment, dict) or not all(
        isinstance(k, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        and isinstance(v, str)
        and "\0" not in v
        for k, v in environment.items()
    ):
        raise ValueError("Invalid environment")
    validate_secrets(request)
    if validate_drive(request) is not None and request.get("organization") is None:
        raise ValueError("A drive requires an organization address")
    try:
        base64.b64decode(request.get("stdin", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid stdin encoding") from exc
    fingerprint = request_fingerprint(request)
    directory = run_path(run_id)
    if directory.exists():
        try:
            record = json.loads((directory / "record.json").read_text())
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError(
                "Existing run journal is incomplete; reconcile it before reuse"
            ) from exc
        if record.get("fingerprint") != fingerprint or record.get("role_id") != role:
            raise ValueError(f"Run ID {run_id} already exists for a different request")
        return status(run_id)
    info = provision(role)
    for existing in runs():
        if existing["role_id"] != role:
            continue
        if existing.get("running") or existing.get("unresolved"):
            raise ValueError(f"Role already has an active or unresolved run: {existing['run_id']}")
    settle(info)
    prepare_run_state(info)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    # main() serializes controller requests. Keep unpublished journals outside
    # runs(), including after SIGKILL when Python cleanup cannot execute.
    staging = STATE / "launch-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    (staging / "lock").touch(mode=0o600)
    record = {
        "role_id": role,
        "run_id": run_id,
        "account": info["account"],
        "uid": info["uid"],
        "boot": boot(),
        "started": time.time(),
        "fingerprint": fingerprint,
        "phase": "launching",
    }
    try:
        save(staging / "record.json", record)
        save(staging / "request.json", request)
        descriptor = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Publish only after both journal files are durable. A crash before this
        # rename leaves private staging that can be swept without an incomplete
        # run visible to retry logic.
        os.rename(staging, directory)
        descriptor = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        # The supervisor uses the same lock before it publishes its PID.  The
        # parent therefore cannot overwrite its phase/PID journal update.
        with run_lock(run_id):
            process = privileged.spawn(
                [PYTHON, "-I", CONTROLLER, "--supervise", run_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            record["supervisor"] = process.pid
            save(directory / "record.json", record)
    except Exception as exc:
        record.update({"phase": "launch_failed", "error": type(exc).__name__})
        save(directory / "record.json", record)
        clean_run_state(info)
        (directory / "request.json").unlink(missing_ok=True)
        save(
            directory / "exit.json",
            {
                "exit_code": 125,
                "ended": time.time(),
                "status": "launch_failed",
                "unresolved": False,
            },
        )
        raise RuntimeError(f"Supervisor launch failed for run {run_id}") from exc
    return record


class LaunchError(RuntimeError):
    """A run that never started: the drive mount or the role's services failed."""


def mount_drive(info, drive):
    """Mount the shared trees at the role's drive, as root, owned by the account.

    The password travels in the mount helper's environment, never on a command line
    or the guest disk.
    """
    unmount_drive(info)
    options = f"uid={info['uid']},gid={info['gid']},{CIFS_OPTIONS}"
    argv = [
        "mount.cifs",
        f"//127.0.0.1/{drive['share']}",
        info["drive"],
        "-o",
        options,
    ]
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "USER": drive["user"],
        "PASSWD": drive["password"],
    }
    result = privileged.run(argv, env=environment, check=False, timeout=120)
    if result.returncode or not privileged.mounted(info["drive"]):
        detail = (result.stderr or result.stdout or "").replace(drive["password"], "***")
        detail = detail.strip().splitlines()
        raise LaunchError(
            f"Shared drive mount failed ({result.returncode}): {detail[-1] if detail else ''}"
        )


def unmount_drive(info):
    """Best effort as root; the slice is already empty."""
    if privileged.mounted(info["drive"]):
        privileged.run(["umount", "-f", info["drive"]], check=False)


def create_credentials(info, files):
    """Create-only 0600 files as the account, anchored under its credentials directory.

    Opening under the account's UID means even a substituted path can never trick the
    controller into writing a root file.
    """
    with privileged.as_user(info):
        parent = os.open(info["credentials"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, value in files.items():
                fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value)
        finally:
            os.close(parent)


def stage_secrets(info, request):
    decoded = validate_secrets(request)
    # No role process exists yet.
    create_credentials(info, decoded)
    return {
        variable: decoded[name].decode() for variable, name in request.get("secret_env", {}).items()
    }


def write_credential(info, request):
    """One more credential for the role's active run, such as a Mac lease key."""
    name = identifier(request["name"])
    try:
        data = base64.b64decode(request["data"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid credential encoding") from exc
    if len(data) > LIMIT:
        raise ValueError("Credential exceeds its size limit")
    try:
        create_credentials(info, {name: data})
    except FileNotFoundError as exc:
        raise RuntimeError("The role has no active run to hold the credential") from exc
    return {"path": info["credentials"] + "/" + name}


def supervise(run_id):
    directory = run_path(run_id)
    request = json.loads((directory / "request.json").read_text())
    info = role_record(request["role_id"])
    environment = {
        **role_environment(info),
        **request.get("env", {}),
        "FIRM_CREDENTIALS_DIR": info["credentials"],
        "FIRM_RUN_ID": run_id,
        "FIRM_STATE_DIR": info["state"],
    }
    # An organizational run works in its workspace on the drive; anything else
    # stays in its private work directory.
    cwd = f"{info['home']}/work"
    if request.get("drive") is not None:
        cwd = f"{info['drive']}/org/{request['organization']}/work"
    process = None
    code = 125
    error = None
    cleanup_error = None
    log_truncated = False
    launch_failed = False
    try:
        with run_lock(run_id):
            if (directory / "stop").exists():
                error = "Stop requested before the role's launch"
            else:
                environment.update(stage_secrets(info, request))
                try:
                    if request.get("drive") is not None:
                        mount_drive(info, request["drive"])
                    start_services(info, environment)
                except LaunchError:
                    launch_failed = True
                    raise
                except (OSError, subprocess.CalledProcessError) as exc:
                    launch_failed = True
                    raise LaunchError(f"Role services did not start: {exc}") from exc
                with (directory / "stdin").open("wb") as stream:
                    stream.write(base64.b64decode(request.get("stdin", ""), validate=True))
                record = json.loads((directory / "record.json").read_text())
                # Only ordinary mounted-role environment is inherited by tasks. Never
                # copy activation tokens, secret_env or provider settings from this run.
                save(
                    directory / "execution.json",
                    {
                        "info": info,
                        "cwd": cwd,
                        "env": role_environment(info),
                    },
                )
                record.update({"supervisor": os.getpid(), "phase": "running"})
                save(directory / "record.json", record)
                with (directory / "stdin").open("rb") as source:
                    # systemd-run drops to the account before it execs the command;
                    # the working directory is entered by this root supervisor, which
                    # only changes directory there and opens nothing.
                    process = privileged.spawn(
                        role_scope(info, request["argv"]),
                        env=environment,
                        cwd=cwd,
                        stdin=source,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
        if process is not None:
            saved = 0
            with (directory / "log").open("wb") as log:
                while chunk := process.stdout.read1(65536):
                    keep = chunk[: max(0, MAX_LOG_BYTES - saved)]
                    log.write(keep)
                    log.flush()
                    saved += len(keep)
                    log_truncated |= len(keep) != len(chunk)
            process.stdout.close()
            code = process.wait()
    except LaunchError as exc:
        error = str(exc)
    except Exception as exc:
        # Exec exceptions can include argv or environment values. Journals are
        # metadata only; the credential-bearing request is removed below.
        error = type(exc).__name__
    finally:
        # Stop takes global lock -> run lock in the same order.  Keep the
        # settlement and exit journal publication together so a concurrent stop
        # cannot observe a half-cleaned run.
        with lock():
            with run_lock(run_id):
                try:
                    settle_run(info, directory)
                    unmount_drive(info)
                    clean_run_state(info)
                except Exception as exc:
                    cleanup_error = str(exc)
                settlement = read_settlement(directory)
                for name in ("stdin", "request.json"):
                    (directory / name).unlink(missing_ok=True)
                save(
                    directory / "exit.json",
                    {
                        "exit_code": code,
                        "ended": time.time(),
                        "status": "unresolved"
                        if cleanup_error
                        else "launch_failed"
                        if launch_failed
                        else "exited",
                        "error": error,
                        "cleanup_error": cleanup_error,
                        "unresolved": bool(cleanup_error),
                        "log_truncated": log_truncated,
                        "usage": settlement.get("usage"),
                        "reclaim": settlement.get("reclaim"),
                    },
                )


# ---- task children: separate journals, no account lifecycle -----------------------------


def validate_seconds_remaining(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid child seconds remaining")
    return value


def child_path(child_id):
    return STATE / "children" / identifier(child_id)


@contextmanager
def child_lock(child_id):
    with (child_path(child_id) / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def child_cgroup(record):
    return slice_cgroup(record["uid"]) / ("task-" + record["run_id"])


def start_child(request):
    allowed = {
        "operation",
        "parent_run_id",
        "run_id",
        "argv",
        "seconds_remaining",
        "env",
        "secrets",
        "secret_env",
        "stdin",
    }
    if set(request) - allowed:
        raise ValueError("Children accept only a parent, command, ID, duration and launch settings")
    parent_id, child_id = identifier(request["parent_run_id"]), identifier(request["run_id"])
    deadline = time.time() + validate_seconds_remaining(request["seconds_remaining"])
    argv = request["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) and "\0" not in a for a in argv)
    ):
        raise ValueError("Invalid command")
    environment = request.get("env", {})
    if not isinstance(environment, dict) or not all(
        isinstance(k, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        and isinstance(v, str)
        and "\0" not in v
        for k, v in environment.items()
    ):
        raise ValueError("Invalid environment")
    decoded = validate_secrets(request)
    try:
        stdin = base64.b64decode(request.get("stdin", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid stdin encoding") from exc
    directory = child_path(child_id)
    if directory.exists():
        raise ValueError("Child ID already used or stopped")
    # A child journal never qualifies as a parent, even if its supervisor is alive.
    if child_path(parent_id).exists() or not run_path(parent_id).is_dir():
        raise ValueError("Parent must be a top-level run")
    with run_lock(parent_id):
        parent = status(parent_id)
        if (
            not parent.get("running")
            or parent.get("phase") != "running"
            or (run_path(parent_id) / "stop").exists()
        ):
            raise ValueError("Parent is not live or is stopping")
        if deadline <= time.time():
            raise ValueError("Child deadline has passed")
        execution = json.loads((run_path(parent_id) / "execution.json").read_text())
        info = execution["info"]
        record = {
            "run_id": child_id,
            "parent_run_id": parent_id,
            "role_id": parent["role_id"],
            "uid": info["uid"],
            "boot": boot(),
            "deadline": deadline,
            "started": time.time(),
            "phase": "launching",
        }
        directory.parent.mkdir(mode=0o700, exist_ok=True)
        staging = STATE / "child-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700)
        save(staging / "record.json", record)
        save(
            staging / "request.json",
            {
                **execution,
                "argv": argv,
                "env": {**execution["env"], **environment},
                "secret_env": request.get("secret_env", {}),
            },
        )
        (staging / "stdin").write_bytes(stdin)
        (staging / "lock").touch(mode=0o600)
        os.rename(staging, directory)  # Journal precedes cgroup creation and spawn.
        with child_lock(child_id):
            try:
                credentials = child_credentials(record)
                managed_directory(credentials.parent.parent, 0, 0, 0o711)
                managed_directory(credentials.parent, info["uid"], info["gid"], 0o700)
                managed_directory(credentials, info["uid"], info["gid"], 0o700)
                create_credentials({**info, "credentials": str(credentials)}, decoded)
                privileged.create_child_cgroup(child_cgroup(record))
                process = privileged.spawn(
                    [PYTHON, "-I", CONTROLLER, "--supervise-child", child_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                record["supervisor"] = process.pid
                save(directory / "record.json", record)
            except Exception:
                _stop_child(child_id, reason="launch_failed", exit_code=125)
                raise
    return record


def child_status(child_id):
    directory = child_path(child_id)
    record = json.loads((directory / "record.json").read_text())
    result = directory / "exit.json"
    log = directory / "log"
    size = log.stat().st_size if log.exists() else 0
    if result.exists():
        return {**record, **json.loads(result.read_text()), "log_bytes": size}
    if record["boot"] != boot():
        with child_lock(child_id):
            return {**_stop_child(child_id, reason="interrupted", exit_code=125), "log_bytes": size}
    return {
        **record,
        "running": True,
        "exit_code": None,
        "confirmed": False,
        "unresolved": False,
        "log_bytes": size,
    }


def _stop_child(child_id, *, reason="cancelled", exit_code=None, publish=True):
    """Caller holds child lock. Never use a PID or process group as kill evidence."""
    directory = child_path(child_id)
    record = json.loads((directory / "record.json").read_text())
    (directory / "stop").touch(mode=0o600, exist_ok=True)
    previous = (
        json.loads((directory / "exit.json").read_text())
        if (directory / "exit.json").exists()
        else {}
    )
    confirmed = False
    try:
        if "uid" not in record or record["boot"] != boot():
            confirmed = True  # Tombstone, or processes from a previous boot.
        else:
            group = child_cgroup(record)
            if not group.exists():
                confirmed = True
            else:
                privileged.kill_child_cgroup(group)
                end = time.monotonic() + CHILD_STOP_TIMEOUT
                while "populated 0" not in (group / "cgroup.events").read_text().splitlines():
                    if time.monotonic() >= end:
                        break
                    time.sleep(0.02)
                else:
                    # Root-owned, nondelegated leaf: children cannot create subgroups.
                    privileged.remove_child_cgroup(group)
                    confirmed = True
    except OSError:
        pass  # A failed read, kill or removal never proves cleanup.
    result = {
        "running": not confirmed,
        "confirmed": confirmed,
        "unresolved": not confirmed,
        "ended": time.time(),
        "exit_code": previous.get("exit_code", exit_code if exit_code is not None else 137),
        "reason": previous.get("reason", reason),
    }
    if publish:
        save(directory / "exit.json", result)
    if confirmed:
        for name in ("request.json", "stdin"):
            (directory / name).unlink(missing_ok=True)
        if record.get("parent_run_id"):
            shutil.rmtree(child_credentials(record).parent, ignore_errors=True)
            shutil.rmtree(STATE / "task-homes" / child_id, ignore_errors=True)
    return {**record, **result}


def stop_child(child_id):
    directory = child_path(child_id)
    if not directory.exists():
        # Fence a delayed start RPC, including admission before any guest journal.
        directory.parent.mkdir(mode=0o700, exist_ok=True)
        staging = STATE / "child-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700)
        save(staging / "record.json", {"run_id": child_id, "boot": boot()})
        os.rename(staging, directory)
    with child_lock(child_id):
        return _stop_child(child_id)


def stop_children(parent_id):
    directory = STATE / "children"
    failed = []
    for path in directory.iterdir() if directory.exists() else ():
        record = json.loads((path / "record.json").read_text())
        if record.get("parent_run_id") == parent_id:
            if not stop_child(path.name)["confirmed"]:
                failed.append(path.name)
    if failed:
        raise RuntimeError("Child cleanup unconfirmed: " + ", ".join(failed))


def child_logs(child_id, offset=0, limit=1024 * 1024):
    if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 0:
        raise ValueError("Invalid log range")
    try:
        with (child_path(child_id) / "log").open("rb") as stream:
            stream.seek(offset)
            data = stream.read(min(limit, CHILD_LOG_BYTES))
    except FileNotFoundError:
        data = b""
    return {"data": base64.b64encode(data).decode()}


def enter_child(record, info, cwd):
    # Popen runs this in its fork before exec, while the supervisor holds the
    # child lock. No user instruction can run or fork before cgroup membership.
    (child_cgroup(record) / "cgroup.procs").write_text(str(os.getpid()))
    os.setgroups([info["gid"]])
    os.setgid(info["gid"])
    os.setuid(info["uid"])
    os.chdir(cwd)


def child_credentials(record):
    return RUN_ROOT / record["role_id"] / "tasks" / record["run_id"] / "credentials"


def supervise_child(child_id):
    directory = child_path(child_id)
    code, reason = 125, "launch_failed"
    process = None
    try:
        record = json.loads((directory / "record.json").read_text())
        with run_lock(record["parent_run_id"]), child_lock(child_id):
            parent = status(record["parent_run_id"])
            if (
                (directory / "stop").exists()
                or (run_path(record["parent_run_id"]) / "stop").exists()
                or not parent.get("running")
                or record["deadline"] <= time.time()
            ):
                return
            request = json.loads((directory / "request.json").read_text())
            info = request["info"]
            # Journals remain root-only; tasks need traversal to their disk homes.
            managed_directory(STATE, 0, 0, 0o711)
            root = STATE / "task-homes"
            managed_directory(root, 0, 0, 0o711)
            root = root / child_id
            managed_directory(root, info["uid"], info["gid"], 0o700)
            for name in ("home", "state"):
                managed_directory(root / name, info["uid"], info["gid"], 0o700)
            credentials = child_credentials(record)
            env = {
                **request["env"],
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "state"),
                "FIRM_PRIVATE_HOME": str(root / "home"),
                "FIRM_STATE_DIR": str(root / "state"),
                "FIRM_CREDENTIALS_DIR": str(credentials),
                "FIRM_RUN_ID": child_id,
            }
            env.update(
                {
                    variable: (credentials / name).read_text()
                    for variable, name in request["secret_env"].items()
                }
            )
            with (directory / "stdin").open("rb") as source:
                process = privileged.spawn(
                    request["argv"],
                    env=env,
                    stdin=source,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: enter_child(record, info, request["cwd"]),
                )
            record.update(supervisor=os.getpid(), phase="running")
            save(directory / "record.json", record)
        code, reason = monitor_child(process, record, directory)
    except Exception:
        pass  # Command/env-bearing exceptions do not belong in the journal.
    finally:
        with child_lock(child_id):
            result = _stop_child(child_id, reason=reason, exit_code=code, publish=False)
        try:
            if process is not None and result["confirmed"]:
                if drain_child_output(process, directory):
                    result.update(reason="log_overflow", exit_code=137)
        finally:
            if process is not None:
                process.stdout.close()
                if result["confirmed"]:
                    process.wait()
            # Normal completion is visible only after all observed output is flushed.
            with child_lock(child_id):
                save(directory / "exit.json", result)


def drain_child_output(process, directory):
    """Retain bytes already in the pipe after cgroup death, without waiting on daemons."""
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    with (directory / "log").open("ab") as log:
        remaining = max(0, CHILD_LOG_BYTES - log.tell())
        while True:
            try:
                chunk = os.read(fd, min(65536, remaining + 1))
            except BlockingIOError:
                return False
            if not chunk:
                return False
            kept = chunk[:remaining]
            log.write(kept)
            remaining -= len(kept)
            if len(kept) != len(chunk):
                return True


def monitor_child(process, record, directory):
    """The guest watchdog also bounds logs when a child never closes stdout."""
    saved = 0
    with selectors.DefaultSelector() as selector, (directory / "log").open("wb") as log:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = record["deadline"] - time.time()
            if remaining <= 0:
                return 137, "timeout"
            ready = selector.select(min(0.1, remaining))
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                keep = chunk[: max(0, CHILD_LOG_BYTES - saved)]
                log.write(keep)
                log.flush()
                saved += len(keep)
                if len(keep) != len(chunk):
                    return 137, "log_overflow"
                if not chunk:
                    selector.unregister(process.stdout)
            code = process.poll()
            if code is not None and (not ready or not chunk):
                return code, "exited"


def read_settlement(directory):
    try:
        return json.loads((directory / "settlement.json").read_text())
    except (OSError, ValueError):
        return {}


def settle_run(info, directory):
    """Stop and supervisor share one receipt, so neither loses the other's evidence."""
    # Caller holds the parent run lock; no child can launch across settlement.
    (directory / "stop").touch(mode=0o600, exist_ok=True)
    child_error = None
    try:
        stop_children(directory.name)
    except Exception as exc:
        child_error = exc
    previous = read_settlement(directory)
    if previous and not previous["error"] and child_error is None:
        return previous
    # Stopping the slice removes the cgroup that holds its usage counters.
    receipt = {
        "usage": previous.get("usage") or slice_usage(info["uid"]),
        "reclaim": {},
        "error": None,
    }
    try:
        # Stopping member units cleans the role's Docker/services even when a
        # foreign task cgroup stays populated; slice stop alone cannot kill tasks.
        settle(info, reclaim=receipt["reclaim"])
        if child_error is not None:
            raise child_error
    except Exception as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        # Keep removal evidence from failed attempts, even if a retry succeeds.
        categories = receipt["reclaim"].get("categories", {})
        for name, before in previous.get("reclaim", {}).get("categories", {}).items():
            after = categories.setdefault(name, reclaim_counts())
            for key in ("count", "bytes"):
                after[key] = (
                    before[key] + after[key]
                    if before[key] is not None and after[key] is not None
                    else None
                )
            for reason, count in before["skipped"].items():
                after["skipped"][reason] = after["skipped"].get(reason, 0) + count
        save(directory / "settlement.json", receipt)
    return receipt


def stop_run(run_id):
    directory = run_path(run_id)
    # Publish intent before taking the per-run lock.  A supervisor that has
    # not spawned its child yet will observe this marker while holding the
    # same lock, and a stop racing a spawn will wait until the spawn is
    # complete before settling it.
    (directory / "stop").touch(mode=0o600, exist_ok=True)
    with run_lock(run_id):
        record = status(run_id)
        if record.get("running") or record.get("unresolved"):
            info = role_record(record["role_id"])
            settlement = settle_run(info, directory)
            if record.get("unresolved"):
                unmount_drive(info)
                clean_run_state(info)
                for name in ("request.json", "stdin"):
                    (directory / name).unlink(missing_ok=True)
                save(
                    directory / "exit.json",
                    {
                        "exit_code": record.get("exit_code", 125),
                        "ended": time.time(),
                        "status": "stopped",
                        "unresolved": False,
                        "cleanup_error": None,
                        "usage": settlement["usage"],
                        "reclaim": settlement["reclaim"],
                    },
                )
    return status(run_id)


def remove_run(run_id):
    result = status(run_id)
    if result.get("running") or result.get("unresolved"):
        raise ValueError("Stop and resolve a run before removing it")
    # Completed supervisors release both locks before another RPC may remove it.
    children = STATE / "children"
    for path in children.iterdir() if children.exists() else ():
        record = json.loads((path / "record.json").read_text())
        if record.get("parent_run_id") == run_id:
            with child_lock(path.name):
                shutil.rmtree(path)
    shutil.rmtree(run_path(run_id))
    return {"removed": run_id}


def retire_role(role_id):
    identifier(role_id)
    data = database()
    if data.get("pending", {}).get("role_id") == role_id:
        # Finish a partially created account so its UID and range stay reserved and
        # its home receives the same retirement treatment.
        provision(role_id)
        data = database()
    info = data["roles"].get(role_id)
    if info is None:
        # A role can be provisioned on the file server before its first run.
        # Reserve its identity without creating an account just to retire it.
        data["roles"][role_id] = {"role_id": role_id, "retired": True}
        save(STATE / "firm.json", data)
        return {"retired": role_id, "private_retained": False}
    if info.get("retired"):
        return {"retired": role_id, "private_retained": "uid" in info}
    if any(
        (run.get("running") or run.get("unresolved")) and run["role_id"] == role_id
        for run in runs()
    ):
        raise ValueError("Stop and resolve a role's runs before retirement")
    data["roles"][role_id]["retiring"] = True
    save(STATE / "firm.json", data)
    settle(info)
    unmount_drive(info)
    clean_run_state(info)
    # The home stays; the UID and subordinate range are tombstoned forever.
    data = database()
    data["roles"][role_id]["retired"] = True
    data["roles"][role_id].pop("retiring", None)
    save(STATE / "firm.json", data)
    return {"retired": role_id, "private_retained": True}


# ---- resource sampling -----------------------------------------------------------------


def meminfo():
    """``/proc/meminfo`` in bytes."""
    values = {}
    try:
        lines = (PROC / "meminfo").read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[name] = int(fields[0]) * (1024 if fields[1:2] == ["kB"] else 1)
    return values


def stall_usec(resource, kind):
    """The cumulative pressure stall of one PSI file, in microseconds."""
    try:
        for line in (PROC / "pressure" / resource).read_text().splitlines():
            fields = line.split()
            if fields[:1] == [kind]:
                return next(
                    int(field.partition("=")[2]) for field in fields if field.startswith("total=")
                )
    except (OSError, ValueError, StopIteration):
        return None
    return None


def cpu_usec():
    """Busy CPU time across every core, in microseconds."""
    try:
        fields = (PROC / "stat").read_text().split("\n", 1)[0].split()
        if fields[:1] != ["cpu"]:
            return None
        # user nice system idle iowait irq softirq steal; guest time is already in user.
        values = [int(value) for value in fields[1:9]]
    except (OSError, ValueError, IndexError):
        return None
    if len(values) < 5:
        return None
    return (sum(values) - values[3] - values[4]) * 10_000  # USER_HZ is 100


def disk_usage(path):
    try:
        info = os.statvfs(path)
    except OSError:
        return {"disk_used_bytes": None, "disk_total_bytes": None}
    return {
        "disk_used_bytes": (info.f_blocks - info.f_bfree) * info.f_frsize,
        "disk_total_bytes": info.f_blocks * info.f_frsize,
    }


def sample():
    """VM-wide counters for the host's resource history; cumulative where the kernel allows.

    Read without the global lock, so a sample never waits behind a long provisioning
    call, and never fails the request: an unreadable field is reported as unknown.
    """
    memory = meminfo()
    return {
        "boot": boot(),
        "cpus": os.cpu_count(),
        "memory_total_bytes": memory.get("MemTotal"),
        "memory_available_bytes": memory.get("MemAvailable"),
        "memory_stall_usec": stall_usec("memory", "some"),
        "memory_full_stall_usec": stall_usec("memory", "full"),
        "cpu_stall_usec": stall_usec("cpu", "some"),
        "cpu_usec": cpu_usec(),
        "oom_kills": read_field(PROC / "vmstat", "oom_kill"),
        "swapouts": read_field(PROC / "vmstat", "pswpout"),
        **disk_usage(ROOT_FILESYSTEM),
    }


# ---- the file server -------------------------------------------------------------------


def rename_exclusive(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":  # Simulation also runs on a Mac.
        rename, at, flag = libc.renameatx_np, -2, 0x4  # AT_FDCWD, RENAME_EXCL
    else:
        rename, at, flag = libc.renameat2, -100, 1  # AT_FDCWD, RENAME_NOREPLACE
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if rename(at, os.fsencode(source), at, os.fsencode(destination), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def ensure_account(name, uid, *, extra=()):
    """Create a service identity once, refusing a pre-existing UID mismatch."""
    existing = privileged.lookup(name=name)
    if existing is None:
        privileged.run(
            [
                "useradd",
                *USERADD_NO_SUBIDS,
                "-M",
                "-s",
                "/usr/sbin/nologin",
                *(["-u", str(uid)] if uid is not None else []),
                *extra,
                name,
            ]
        )
    elif uid is not None and existing.pw_uid != uid:
        raise RuntimeError(f"Account {name} exists with another UID")


def samba_account(name, uid, password, extra=()):
    ensure_account(name, uid, extra=extra)
    privileged.run(["smbpasswd", "-s", "-a", name], input=f"{password}\n{password}\n")


def write_new_file(root, directory, name, data):
    """Create ``name``, or ``<stem>-<n>.<suffix>`` beside it, in an ``org`` directory.

    Nothing is followed as a link and nothing is replaced; only the last directory
    component is created. The file inherits the directory's default ACL entries, as a
    file written over SMB does.
    """
    parts = directory.split("/") if isinstance(directory, str) else []
    if (
        not parts
        or parts[0] != "org"
        or any(part in {"", ".", ".."} for part in parts)
        or not isinstance(name, str)
        or not FILE_NAME.fullmatch(name)
    ):
        raise ValueError("Invalid file server write")
    stem, _, suffix = name.rpartition(".")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                try:
                    os.mkdir(part, 0o770, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        for attempt in range(1, 100):
            candidate = name if attempt == 1 else f"{stem}-{attempt}.{suffix}"
            try:
                out = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o660,
                    dir_fd=fd,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(out, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.unlink(candidate, dir_fd=fd)
                raise
            return candidate
        raise RuntimeError("Too many files with the same name")
    finally:
        os.close(fd)


def file_step(root, item):
    reply, action = {}, item["action"]
    if action == "setup":
        privileged.run(["groupadd", "-f", item["group"]])
        for path in (root, *(os.path.join(root, n) for n in ("org", "shared"))):
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o700)
        samba_account(item["owner"], None, item["owner_password"])
        try:
            changed = SMB_CONF.read_text() != item["smb_conf"]
        except OSError:
            changed = True
        if changed:
            SMB_CONF.parent.mkdir(parents=True, exist_ok=True)
            temporary = SMB_CONF.with_name(SMB_CONF.name + ".new")
            temporary.write_text(item["smb_conf"])
            os.chmod(temporary, 0o644)
            temporary.replace(SMB_CONF)
        privileged.run(["systemctl", "disable", "--now", "nmbd"], check=False)
        privileged.run(["systemctl", "enable", "--now", "smbd"])
        if changed:
            privileged.run(["systemctl", "restart", "smbd"])
        reply = {"configured": changed}
    elif action == "account":
        samba_account(item["account"], item["uid"], item["password"], ("-G", item["group"]))
    elif action == "retire":
        name = item["account"]
        if privileged.lookup(name=name) is None:
            reply = {"absent": True}  # Provisioning never reached this guest.
        else:
            privileged.run(["smbpasswd", "-x", name], check=False)
            privileged.run(["gpasswd", "-d", name, item["group"]], check=False)
            sessions = privileged.run(["smbstatus", "-p"], check=False).stdout
            for line in sessions.splitlines():
                fields = line.split()
                if len(fields) > 1 and fields[1] == name and fields[0].isdigit():
                    privileged.run(["kill", fields[0]], check=False)
            trees = [os.path.join(root, n) for n in ("org", "shared")]
            privileged.run(["setfacl", "-R", "-x", f"u:{name},d:u:{name}", *trees])
    elif action == "grants":
        for argv in item["grants"]:
            privileged.run(argv)
    elif action == "provision":
        for relative in item["directories"]:
            os.makedirs(os.path.join(root, relative), mode=0o770, exist_ok=True)
    elif action == "archive":
        source, destination = (os.path.join(root, item[k]) for k in ("source", "destination"))
        if os.path.isdir(destination) and not os.path.lexists(source):
            reply = {"archived": False}
        elif os.path.isdir(source) and not os.path.lexists(destination):
            os.makedirs(os.path.dirname(destination), mode=0o770, exist_ok=True)
            rename_exclusive(source, destination)
            for path in {os.path.dirname(source), os.path.dirname(destination)}:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                os.fsync(fd)
                os.close(fd)
            reply = {"archived": True}
        else:
            raise RuntimeError("Organization archive source and destination are inconsistent")
    elif action == "write":
        data = base64.b64decode(item["data"], validate=True)
        reply = {"name": write_new_file(root, item["directory"], item["name"], data)}
    elif action == "profiles":
        for role, relative in item["paths"].items():
            path = os.path.join(root, relative, "profile.md")
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                reply[role] = {"status": "missing"}
                continue
            except OSError:
                reply[role] = {"status": "unreadable"}
                continue
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                content = stream.read(item["limit"] + 1)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or len(content) > item["limit"]:
                reply[role] = {"status": "unreadable"}
            else:
                reply[role] = {
                    "status": "available",
                    "text": content.decode(errors="replace"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
    elif action in {"browse-list", "browse-read"}:
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or relative.startswith("/"):
            raise ValueError("Invalid browse path")
        parts = relative.split("/")
        if any(not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in parts):
            raise ValueError("Invalid browse path")
        if not (
            parts[0] == "shared" or (parts[0] == "org" and "work" in parts[2:])
        ):
            raise ValueError("Browse path is outside workspaces and shared files")
        # The host checks the workspace prefix against its ledger. Descendants
        # traverse the same descriptor-based, no-follow boundary as the root.

        def open_directory(parent, name):
            return os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )

        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        directory_fd = root_fd
        try:
            for component in parts:
                next_fd = open_directory(directory_fd, component)
                if directory_fd != root_fd:
                    os.close(directory_fd)
                directory_fd = next_fd
            if action == "browse-list":
                limit = item.get("limit", 200)
                if type(limit) is not int or not 1 <= limit <= 200:
                    raise ValueError("Invalid browse listing limit")
                entries = []
                truncated = False
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        if len(entries) >= limit:
                            truncated = True
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            kind = "file"
                        else:
                            kind = "unsupported"
                        entries.append(
                            {
                                "name": entry.name,
                                "type": kind,
                                "size": info.st_size if kind == "file" else None,
                            }
                        )
                reply = {"entries": entries, "truncated": truncated}
            else:
                name = item.get("name")
                limit = item.get("limit")
                if (
                    not isinstance(name, str)
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or "\x00" in name
                    or type(limit) is not int
                    or not 1 <= limit <= 6 * 1024 * 1024
                ):
                    raise ValueError("Invalid browse file request")
                try:
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    reply = {"status": "missing"}
                except OSError:
                    reply = {"status": "unsupported"}
                else:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        os.close(fd)
                        reply = {"status": "unsupported"}
                    else:
                        with os.fdopen(fd, "rb") as stream:
                            content = stream.read(limit + 1)
                        if len(content) > limit:
                            reply = {"status": "too_large"}
                        else:
                            reply = {
                                "status": "available",
                                "data": base64.b64encode(content).decode("ascii"),
                                "size": len(content),
                            }
        finally:
            if directory_fd != root_fd:
                os.close(directory_fd)
            os.close(root_fd)
    else:
        raise ValueError("Unknown file server action")
    return reply


def files(request):
    """The file server's steps; each reply carries the privileged commands it ran."""
    root = request["root"]
    if not isinstance(root, str) or not root.startswith("/"):
        raise ValueError("File server root must be an absolute path")
    previous = os.umask(0o007)  # the shared trees are group-traversable for the roles
    try:
        replies = []
        for item in request["steps"]:
            first = len(privileged.commands)
            reply = file_step(root, item)
            replies.append({**reply, "commands": privileged.commands[first:]})
        return replies
    finally:
        os.umask(previous)


# ---- export ----------------------------------------------------------------------------


def export(request, stream):
    """Write the shared trees, and with ``homes`` each role's work and provider sessions,
    to ``stream`` as one tar archive.

    Members are ``drive/...`` and ``homes/<role>/work/...`` or ``homes/<role>/.firm/...``;
    links are archived as links, never followed. The host unpacks the stream with its own
    extraction filter; nothing here trusts the files the roles wrote.
    """
    root = request["root"]
    if not isinstance(root, str) or not root.startswith("/"):
        raise ValueError("Export root must be an absolute path")
    if not isinstance(request.get("homes"), bool):
        raise ValueError("Export homes must be a boolean")
    with tarfile.open(fileobj=stream, mode="w|", copybufsize=1024 * 1024) as archive:
        archive.add(root, arcname="drive")
        if request["homes"]:
            for role, info in sorted(database()["roles"].items()):
                if "home" not in info:
                    continue  # retired before its account existed
                for name in ("work", ".firm"):
                    path = os.path.join(info["home"], name)
                    if os.path.lexists(path):
                        archive.add(path, arcname=f"homes/{identifier(role)}/{name}")


# ---- dispatch --------------------------------------------------------------------------


def dispatch(request):
    operation = request["operation"]
    if operation == "initialize":
        return initialize(request)
    if operation == "info":
        data = database()
        return {"roles": data["roles"], "boot": boot(), "firm_id": data.get("firm_id")}
    if operation == "provision":
        return provision(request["role_id"])
    if operation == "start-child":
        return start_child(request)
    if operation == "child-status":
        return child_status(request["run_id"])
    if operation == "child-logs":
        return child_logs(
            request["run_id"], request.get("offset", 0), request.get("limit", 1024 * 1024)
        )
    if operation == "stop-child":
        return stop_child(request["run_id"])
    if operation == "start":
        return start(request)
    if operation == "runs":
        return runs()
    if operation == "stop":
        return stop_run(request["run_id"])
    if operation == "remove":
        return remove_run(request["run_id"])
    if operation == "retire":
        return retire_role(request["role_id"])
    if operation == "files":
        return files(request)
    if operation in ("status", "logs"):
        record = status(request["run_id"])
        if operation == "logs":
            path = run_path(request["run_id"]) / "log"
            offset = request.get("offset", 0)
            limit = request.get("limit", 1024 * 1024)
            if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or limit < 0:
                raise ValueError("Invalid log range")
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(min(limit, LIMIT))
            except FileNotFoundError:
                # The launch journal is durable before the supervisor is spawned; the
                # log exists only after it takes the run lock, so an early poll sees none.
                data = b""
            return {"data": base64.b64encode(data).decode()}
        return record
    if operation == "write-credential":
        return write_credential(role_record(request["role_id"]), request)
    raise ValueError("Unknown operation")


def main():
    if sys.platform != "linux" or os.getuid() != 0:
        raise RuntimeError("The guest controller runs only as root inside the firm's Linux VM")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], check=False).returncode:
        raise RuntimeError("Refusing to run the guest controller outside a virtual machine")
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise":
        supervise(identifier(sys.argv[2]))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise-child":
        supervise_child(identifier(sys.argv[2]))
        return
    if sys.argv[1:] == ["--reclaim-scratch"]:
        request = json.load(sys.stdin)
        print(json.dumps(reclaim_scratch(request["info"], request["deadline"])))
        return
    streaming = False
    try:
        raw = sys.stdin.buffer.read(2 * LIMIT + 1)
        if len(raw) > 2 * LIMIT:
            raise ValueError("Request is too large")
        request = json.loads(raw)
        if request["operation"] == "export":
            # The one reply that is not JSON: the tar stream itself. It reads files
            # without the global lock, so a running firm's requests are not held for
            # the whole copy.
            streaming = True
            export(request, sys.stdout.buffer)
            return
        if request["operation"] == "sample":
            # Read-only counters must not wait for long provisioning calls.
            result = sample()
        elif request["operation"] in {"child-status", "child-logs"}:
            # Journal reads must not queue behind unrelated role operations.
            # Reboot reconciliation in child_status takes the child lock itself.
            result = dispatch(request)
        else:
            with lock():
                result = dispatch(request)
        reply = {"ok": True, "result": result}
    except Exception as exc:
        if streaming:
            raise  # A partial stream cannot carry a reply; the exit status and stderr do.
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
