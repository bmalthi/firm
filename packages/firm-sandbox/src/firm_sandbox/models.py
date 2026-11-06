from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .host_paths import read_text_file
from .linux_vm_guest import role_account


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ValueError(
            "IDs must start with a-z and contain at most 40 lowercase letters/digits/-"
        )
    return value


def role_address(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or len(value.split("/")) > 16
        or any(
            part == "work" or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", part)
            for part in value.split("/")
        )
    ):
        raise ValueError("Invalid organization address")
    return value


@dataclass(frozen=True)
class Organization:
    """Resolved organization subtree grant; the runtime owns membership decisions."""

    address: str

    def __post_init__(self):
        role_address(self.address)

    @property
    def ancestors(self) -> tuple[str, ...]:
        parts = self.address.split("/")
        return tuple("/".join(parts[:index]) for index in range(1, len(parts)))


def firm_identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", value)
    ):
        raise ValueError(
            "Firm IDs must use at most 64 lowercase letters/digits with single - or _ separators"
        )
    return value


EGRESS_MODES = ("enforce", "observe")
MAX_HOSTS = 256
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")


def hostname(value) -> str:
    """A lowercase DNS name of two or more labels whose last label is not numeric.

    The last-label rule keeps every IPv4 spelling (``1.2.3.4``, ``127.1``) out: the
    proxy reaches hosts by name, never by address.
    """
    if (
        not isinstance(value, str)
        or len(value) > 253
        or not _HOSTNAME.fullmatch(value)
        or value.rsplit(".", 1)[1].isdigit()
    ):
        raise ValueError(f"Invalid host name: {value!r}")
    return value


def host_entry(value) -> str:
    """One allowlist entry: an exact host name, or ``*.suffix`` for any subdomain of it.

    A suffix entry never matches the bare suffix, and the suffix itself needs two labels
    so that no entry can stand for a whole top-level domain.
    """
    try:
        if isinstance(value, str) and value.startswith("*."):
            hostname(value[2:])
            return value
        return hostname(value)
    except ValueError:
        raise ValueError(
            f"Allowed hosts must be lowercase names or *.suffix entries, not {value!r}"
        ) from None


def host_entries(value) -> tuple[str, ...]:
    """A sorted, duplicate-free allowlist."""
    if not isinstance(value, (tuple, list)):
        raise ValueError("allow_hosts must be a list of host names")
    entries = tuple(sorted({host_entry(entry) for entry in value}))
    if len(entries) > MAX_HOSTS:
        raise ValueError(f"At most {MAX_HOSTS} allowed hosts")
    return entries


def egress_mode(value) -> str:
    if value not in EGRESS_MODES:
        raise ValueError(f"[network] egress must be one of: {', '.join(EGRESS_MODES)}")
    return value


def _absolute_image(value, label):
    if value is None:
        return
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ValueError(f"{label} must be an absolute path")
    if not Path(value).is_absolute():
        raise ValueError(f"{label} must be an absolute path to a sealed Mac station image")


@dataclass(frozen=True)
class Config:
    """The Linux VM, optional shared Mac VM and network policy.

    The Mac image determines its station accounts; CPU and memory are shared by them.
    """

    root: Path
    firm_id: str = "firm"
    cpus: int = 8
    memory_gib: int = 16
    disk_gib: int = 60
    mac_image: str | None = None
    mac_cpus: int = 8
    mac_memory_gib: int = 24
    egress_mode: str = "enforce"
    allow_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "root", self.root.resolve())
        firm_identifier(self.firm_id)
        if os.getuid() == 0:
            raise ValueError("Run the sandbox as an ordinary host user")
        resources = (self.cpus, self.memory_gib, self.disk_gib, self.mac_cpus, self.mac_memory_gib)
        if any(type(v) is not int or v <= 0 for v in resources):
            raise ValueError("VM resource values must be positive")
        _absolute_image(self.mac_image, "mac.image")
        egress_mode(self.egress_mode)
        object.__setattr__(self, "allow_hosts", host_entries(self.allow_hosts))

    @classmethod
    def load(cls, path: Path) -> Config:
        path = path.resolve()
        raw = tomllib.loads(read_text_file(path))
        sections = {
            "firm": {"id", "cpus", "memory_gib", "disk_gib"},
            "mac": {"image", "cpus", "memory_gib"},
            "network": {"egress", "allow_hosts"},
        }
        if set(raw) - set(sections):
            raise ValueError("Unknown configuration section")
        for section, keys in sections.items():
            values = raw.get(section, {})
            if not isinstance(values, dict):
                raise ValueError(f"{section} must be a table")
            unknown = set(values) - keys
            if unknown:
                raise ValueError(f"Unknown {section} setting: {', '.join(sorted(unknown))}")
        firm = dict(raw.get("firm", {}))
        mac = dict(raw.get("mac", {}))
        network = dict(raw.get("network", {}))
        if "mac" in raw and mac.get("image") is None:
            raise ValueError("[mac] must name the station image")
        return cls(
            root=path.parent,
            firm_id=firm.get("id", "firm"),
            cpus=firm.get("cpus", 8),
            memory_gib=firm.get("memory_gib", 16),
            disk_gib=firm.get("disk_gib", 60),
            mac_image=mac.get("image"),
            mac_cpus=mac.get("cpus", 8),
            mac_memory_gib=mac.get("memory_gib", 24),
            egress_mode=network.get("egress", "enforce"),
            allow_hosts=network.get("allow_hosts", ()),
        )

    @property
    def durable(self) -> Path:
        """Persistent state: the identity file, events, the file server ledger and one directory
        per VM, beside the runtime's database."""
        return self.root / "durable"

    @property
    def locks(self) -> Path:
        """Every host lock of the firm, kept separate from persistent state: the lifecycle
        lock, the station lock, and the file server's, events' and each tunnel's lock."""
        return self.root / "locks"

    @property
    def identity_file(self) -> Path:
        """The firm's runtime ID, created once with the sandbox."""
        return self.durable / "firm.json"

    @property
    def assets(self) -> Path:
        return Path(__file__).parent / "assets"


@dataclass(frozen=True)
class VMSettings:
    """What one VM driver needs from the firm; ``Sandbox`` takes it from ``Config``.

    ``root`` is the VM's own directory, ``durable/linux`` or ``durable/mac``: its
    record, its Lima disk, the saved egress decision, the granted hosts, the proxy key
    and its host network's reconstructible files. ``firm_locks`` is the firm's lock
    directory, where every lifecycle operation of the firm locks; a VM keeps no lock
    of its own. ``egress_mode`` and ``allow_hosts`` are the firm's ``[network]``.
    """

    root: Path
    firm_locks: Path
    firm_id: str
    cpus: int
    memory_gib: int
    disk_gib: int = 60
    egress_mode: str = "enforce"
    allow_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).absolute())
        object.__setattr__(self, "firm_locks", Path(self.firm_locks).absolute())
        firm_identifier(self.firm_id)
        if os.getuid() == 0:
            raise ValueError("Run the sandbox as an ordinary host user")
        if any(type(v) is not int or v <= 0 for v in (self.cpus, self.memory_gib, self.disk_gib)):
            raise ValueError("VM resource values must be positive")
        egress_mode(self.egress_mode)
        object.__setattr__(self, "allow_hosts", host_entries(self.allow_hosts))


@dataclass(frozen=True)
class Layout:
    """Paths and addresses as a role sees them inside the firm's Linux VM.

    The firm's shared trees are one SMB share mounted per role at ``drive``. The
    defaults are the shape; ``for_role`` gives the paths of one role.
    """

    private: str = "/home/firmrole"
    private_work: str = "/home/firmrole/work"
    scratch: str = "/home/firmrole/scratch"
    drive: str = "/home/firmrole/drive"
    organization: str = "/home/firmrole/drive/org"
    shared: str = "/home/firmrole/drive/shared"
    state: str = "/run/firm/role/state"
    credentials: str = "/run/firm/role/credentials"
    python: str = "/usr/bin/python3"
    tools: str = "/usr/local/bin"
    host: str = "127.0.0.1"

    @classmethod
    def for_role(cls, role_id: str) -> Layout:
        """The one place a role's guest paths come from; pure, no VM required."""
        home = f"/home/{role_account(identifier(role_id))}"
        run = f"/run/firm/{role_id}"
        return cls(
            private=home,
            private_work=f"{home}/work",
            scratch=f"{home}/scratch",
            drive=f"{home}/drive",
            organization=f"{home}/drive/org",
            shared=f"{home}/drive/shared",
            state=f"{run}/state",
            credentials=f"{run}/credentials",
        )

    def workspace(self, address: str) -> str:
        return f"{self.organization}/{address}/work"

    def credential(self, name: str) -> str:
        return f"{self.credentials}/{name}"


@dataclass(frozen=True)
class RunSpec:
    role_id: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict, repr=False)
    secrets: dict[str, bytes] = field(default_factory=dict, repr=False)
    secret_env: dict[str, str] = field(default_factory=dict, repr=False)
    stdin: bytes = field(default=b"", repr=False)
    run_id: str | None = None
    organization: Organization | None = None

    def __post_init__(self):
        identifier(self.role_id)
        if self.organization is not None:
            if not isinstance(self.organization, Organization):
                raise ValueError("organization must be an Organization grant")
        if self.run_id:
            identifier(self.run_id)
        if not self.argv or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv):
            raise ValueError("argv must contain a command and valid string arguments")
        for key, value in self.env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
                raise ValueError("Invalid environment entry")
        for key in self.secrets:
            identifier(key)
        for variable, name in self.secret_env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) or name not in self.secrets:
                raise ValueError("Secret environment mappings must reference a supplied secret")
            if "\0" in self.secrets[name].decode():
                raise ValueError("Secret environment values must be text without NUL bytes")


@dataclass(frozen=True)
class Run:
    role_id: str
    run_id: str
    vm_generation: str
