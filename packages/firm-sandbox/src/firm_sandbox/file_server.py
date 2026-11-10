"""Per-firm SMB file server inside the firm's Linux VM.

The shared trees ``org/`` and ``shared/`` live on the Linux VM's disk
under ``/srv/firm``, root-owned. Each role is a Samba login there, separate from its
execution account; the kernel enforces POSIX ACLs on every SMB operation. smbd
listens on the guest's loopback only: roles mount it there directly, the sandbox
forwards it into the shared Mac's loopback and to host loopback over its own SSH
channels. The owner account is a Samba admin user, so Finder sees the whole tree.

The ledger lives on the host and is written before each guest side effect; every
remote step is repeatable, so an interrupted operation converges on the next call.
The steps themselves are the ``files`` operation of the Linux guest controller.
"""

from __future__ import annotations

import base64
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from .file_grants import (
    GROUP,
    TREE,
    Grant,
    firm_grants,
    in_line,
    role_grants,
    strip,
    vacated_grants,
)
from .guest_control import private_directory, read_record, save
from .linux_vm_guest import allocate_identity
from .locking import locked
from .models import Config, Organization, identifier, role_address

SHARE = "firm"
OWNER = "firmowner"
SERVER_PORT = 445
LEDGER = "file-server.json"
PROFILE_LIMIT = 1024 * 1024
# Base64 in one guest request (at most 2 * MAX_FILE); covers the station helper's
# largest window capture (11.5 MB).
WORK_FILE_LIMIT = 11 * 1024 * 1024
BROWSE_LIST_LIMIT = 200
BROWSE_FILE_LIMIT = 6 * 1024 * 1024

SMB_CONF = f"""[global]
  server role = standalone server
  passdb backend = tdbsam
  interfaces = 127.0.0.1
  bind interfaces only = yes
  smb ports = {SERVER_PORT}
  disable netbios = yes
  load printers = no
  printing = bsd
  printcap name = /dev/null
  disable spoolss = yes
  server min protocol = SMB3
  server multi channel support = no
  admin users = {OWNER}
  inherit acls = yes
  inherit owner = yes
  create mask = 0777
  directory mask = 0777
  nt acl support = no
  map readonly = no
  map archive = no
  map hidden = no
  map system = no
  store dos attributes = no
  ea support = yes
  vfs objects = fruit streams_xattr
  fruit:nfs_aces = no
[{SHARE}]
  path = {TREE}
  read only = no
  browseable = no
"""


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str
    port: int
    share: str = SHARE

    def request(self) -> dict:
        return {
            "user": self.user,
            "password": self.password,
            "port": self.port,
            "share": self.share,
        }


def local_runner(root: Path):
    """Run the guest controller's file steps against a local tree, recording commands."""
    from . import linux_vm_guest as guest

    recorder = guest.Privileged(simulate=True)  # accounts persist across calls, as in a guest

    def run(request: dict) -> list[dict]:
        with guest.simulation(recorder):
            samba, guest.SMB_CONF = guest.SMB_CONF, Path(root) / "smb.conf"
            try:
                return guest.files({**request, "root": str(root)})
            finally:
                guest.SMB_CONF = samba

    return run


class FileServer:
    """``runner({"root", "steps"}) -> [reply, ...]`` runs the guest's file steps as root."""

    def __init__(self, config: Config, runner, *, forwards=lambda port: ()):
        self.config = config
        self.run = runner
        self.forwards = forwards
        self.record = config.durable / LEDGER

    # ---- ledger -------------------------------------------------------------

    def _lock(self):
        return locked(self.config.locks, "file-server")

    def _load(self) -> dict:
        if self.record.exists():
            return read_record(self.record)
        return {
            "port": None,
            "database_identity": None,
            "owner_password": secrets.token_urlsafe(24),
            "accounts": {},
            "roles": {},
            "addresses": {},
            "pending_archive": None,
        }

    def _save(self, ledger: dict) -> None:
        private_directory(self.config.durable)
        save(self.record, ledger)

    def _remote(self, *steps: dict) -> list[dict]:
        return self.run({"root": TREE, "steps": list(steps)})

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> dict:
        with self._lock():
            ledger = self._load()
            if ledger["port"] is None:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    ledger["port"] = probe.getsockname()[1]
            self._save(ledger)
            # Replay every current role's directories and account before the
            # grants, so a provisioning step lost to the guest converges here.
            steps = [
                {
                    "action": "setup",
                    "group": GROUP,
                    "owner": OWNER,
                    "owner_password": ledger["owner_password"],
                    "smb_conf": SMB_CONF,
                },
            ]
            # A file moved between areas keeps the entries of where it came from;
            # nobody runs at boot, so strip every extended entry and recompute.
            grants = [strip(f"{TREE}/org"), strip(f"{TREE}/shared"), *firm_grants()]
            vacated = self._vacated(ledger)
            if vacated:
                steps.append(
                    {
                        "action": "provision",
                        "directories": [directory + "/work" for _, directory in vacated],
                    }
                )
            for role_id, record in ledger["roles"].items():
                entry = ledger["accounts"].get(role_id)
                if record["archived"] or not entry or entry.get("retired"):
                    continue
                steps.append({"action": "provision", "directories": self._directories(record)})
                steps.append(self._account_step(entry))
                grants.extend(self._role_grants(ledger, role_id))
            grants = list(dict.fromkeys(grants))
            steps.append({"action": "grants", "grants": [grant.argv for grant in grants]})
            self._remote(*steps)
            for tunnel in self.forwards(ledger["port"]):
                tunnel.start()
            return self.owner_access()

    def stop(self) -> None:
        if not self.record.exists():
            return
        port = read_record(self.record)["port"]
        if port is not None:
            for tunnel in self.forwards(port):
                tunnel.close()

    def reconnect(self) -> None:
        """Restore forwards after a Mac reboot without rewriting live Linux grants."""
        with self._lock():
            port = self._load()["port"]
            if port is None:
                raise RuntimeError("File server is not started")
            for tunnel in self.forwards(port):
                tunnel.start()

    def require_ready(self) -> None:
        port = self._load()["port"]
        if port is None:
            raise RuntimeError("File server is not started")
        for tunnel in self.forwards(port):
            tunnel.require_ready()

    def discard(self) -> None:
        """Forget every account and address; the guest disk is gone with them."""
        self.record.unlink(missing_ok=True)

    def owner_access(self) -> dict:
        ledger = self._load()
        if ledger["port"] is None:
            raise RuntimeError("File server is not started")
        # macOS keeps one SMB session per server name and user, not per port: a second
        # firm's drive at 127.0.0.1 would be refused while the first is mounted. Every
        # *.localhost name is loopback, so a name per firm's port keeps them apart.
        return {
            "url": f"smb://{OWNER}@firm-{ledger['port']}.localhost:{ledger['port']}/{SHARE}",
            "user": OWNER,
            "password": ledger["owner_password"],
            "path": TREE,
        }

    # ---- accounts -----------------------------------------------------------

    def credentials(self, role_id: str) -> Credentials:
        """The credentials a run mounts with; provisioning created them."""
        identifier(role_id)
        ledger = self._load()
        entry = ledger["accounts"].get(role_id)
        if entry is None or entry.get("retired"):
            raise RuntimeError(f"Role {role_id} has no file server login")
        return Credentials(entry["account"], entry["password"], ledger["port"])

    def _account(self, ledger, role_id) -> dict:
        if ledger["port"] is None:
            raise RuntimeError("Start the file server before provisioning roles")
        entry = ledger["accounts"].get(role_id)
        if entry is not None and entry.get("retired"):
            raise RuntimeError("File server role has been retired")
        if entry is None:
            used = {item.get("uid") for item in ledger["accounts"].values()}
            account, uid = allocate_identity(
                f"{self.config.firm_id}:{role_id}",
                account_prefix="firmfile-",
                uid_start=50000,
                uid_count=10000,
                used_uids=used,
            )
            entry = {
                "account": account,
                "uid": uid,
                "password": secrets.token_urlsafe(24),
                "retired": False,
            }
            ledger["accounts"][role_id] = entry
            self._save(ledger)
        return entry

    @staticmethod
    def _account_step(entry) -> dict:
        return {
            "action": "account",
            "account": entry["account"],
            "uid": entry["uid"],
            "password": entry["password"],
            "group": GROUP,
        }

    def retired_roles(self) -> set[str]:
        """Atomic local ledger read; no guest call is needed."""
        return {role for role, account in self._load()["accounts"].items()
                if account.get("retired")}

    def retire_role(self, role_id: str) -> dict:
        identifier(role_id)
        with self._lock():
            ledger = self._load()
            entry = ledger["accounts"].get(role_id)
            if entry is None:
                ledger["accounts"][role_id] = {"account": None, "uid": None, "retired": True}
            elif not entry.get("retired"):
                steps = [{"action": "retire", "account": entry["account"], "group": GROUP}]
                record = ledger["roles"].get(role_id)
                if record is not None and not record["archived"]:
                    # Its reporting line keeps reading the work it leaves and stops writing it.
                    members = self._members(ledger)
                    members.pop(record["address"], None)
                    grants = vacated_grants(record["address"], record["directory"], members)
                    steps += [
                        {"action": "provision", "directories": [record["directory"] + "/work"]},
                        {"action": "grants", "grants": [grant.argv for grant in grants]},
                    ]
                self._remote(*steps)
                entry.update({"retired": True, "password": None})
            self._save(ledger)
            return {"role_id": role_id, "retired": True}

    # ---- organization -------------------------------------------------------

    def bind_identity(self, identity: str) -> None:
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or set(identity) - set("0123456789abcdef")
        ):
            raise ValueError("Invalid runtime database identity")
        with self._lock():
            ledger = self._load()
            bound = ledger["database_identity"]
            if bound is not None and bound != identity:
                raise ValueError("File server belongs to a different runtime database")
            ledger["database_identity"] = identity
            self._save(ledger)

    def database_identity(self) -> str | None:
        """The runtime database this file server is bound to, if it has served one."""
        return self._load()["database_identity"]

    def role_at(self, address: str) -> str | None:
        """The role currently holding an organization address, if any."""
        role_address(address)
        return self._load()["addresses"].get(address)

    def address(self, role_id: str) -> str:
        identifier(role_id)
        record = self._load()["roles"].get(role_id)
        if record is None:
            raise RuntimeError(f"Role {role_id} has no organization address")
        return record["address"]

    def workspace_path(self, role_id: str) -> str | None:
        """Drive-relative workspace, current or archived."""
        identifier(role_id)
        record = self._load()["roles"].get(role_id)
        return None if record is None else record["directory"] + "/work"

    def browse_roots(self) -> list[dict]:
        """Current owner-visible workspaces plus shared deliverables, from the file ledger."""
        ledger = self._load()
        roots = [{"path": "shared", "label": "Shared deliverables", "address": None}]
        for _role_id, record in sorted(
            ledger["roles"].items(), key=lambda item: item[1]["address"]
        ):
            roots.append(
                {
                    "path": record["directory"] + "/work",
                    "label": record["address"],
                    "address": record["address"],
                }
            )
        return roots

    def _browse_path(self, path: str) -> str:
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError("Invalid file browser path")
        parts = path.split("/")
        if any(not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in parts):
            raise ValueError("Invalid file browser path")
        roots = {root["path"] for root in self.browse_roots()}
        if not any(path == root or path.startswith(root + "/") for root in roots):
            raise ValueError("File browser path is outside role workspaces and shared files")
        return path

    def validate_browse_file(self, path: str, name: str) -> tuple[str, str]:
        """Validate a browser file reference without contacting the guest."""
        relative = self._browse_path(path)
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise ValueError("Invalid file browser name")
        return relative, name

    def validate_role_file(self, role_id: str, path: str, name: str) -> tuple[str, str]:
        """Validate a reference against the role's ledger grants, without guest I/O."""
        relative, name = self.validate_browse_file(path, name)
        ledger = self._load()
        record = ledger["roles"].get(role_id)
        account = ledger["accounts"].get(role_id) or {}
        if (record is None or record["archived"] or account.get("retired")
                or not account.get("account")):
            raise ValueError("Attachment sender has no file access")
        roots = ["shared"]
        workspaces = [(address, "org/" + address) for address in self._members(ledger)]
        workspaces.extend(self._vacated(ledger))
        roots.extend(
            directory + "/work" for address, directory in workspaces
            if in_line(record["address"], address)
        )
        if not any(relative == root or relative.startswith(root + "/") for root in roots):
            raise ValueError("Attachment is outside the sender's readable workspaces")
        return relative, name

    def browse_list(self, path: str) -> dict:
        """List one bounded directory through the live guest file-server boundary."""
        relative = self._browse_path(path)
        self.require_ready()
        result = self._remote(
            {"action": "browse-list", "path": relative, "limit": BROWSE_LIST_LIMIT}
        )[0]
        return {"entries": result.get("entries", []), "truncated": result.get("truncated", False)}

    def browse_read(self, path: str, name: str, *, limit: int = BROWSE_FILE_LIMIT) -> dict:
        """Read one bounded regular file without following any guest-side links."""
        relative, name = self.validate_browse_file(path, name)
        if type(limit) is not int or not 1 <= limit <= BROWSE_FILE_LIMIT:
            raise ValueError("Invalid file browser size limit")
        self.require_ready()
        result = self._remote(
            {"action": "browse-read", "path": relative, "name": name, "limit": limit}
        )[0]
        return {key: result[key] for key in ("status", "data", "size") if key in result}

    def members(self) -> dict[str, str]:
        """Current address -> account for every provisioned role."""
        return self._members(self._load())

    @staticmethod
    def _members(ledger) -> dict[str, str]:
        members = {}
        for role_id, record in ledger["roles"].items():
            entry = ledger["accounts"].get(role_id) or {}
            if not record["archived"] and entry.get("account") and not entry.get("retired"):
                members[record["address"]] = entry["account"]
        return members

    @staticmethod
    def _vacated(ledger) -> list[tuple[str, str]]:
        """Address and directory of the work of every archived or retired role.

        A role retired before its login existed never had a directory on the server.
        """
        vacated = []
        for role_id, record in ledger["roles"].items():
            entry = ledger["accounts"].get(role_id) or {}
            if record["archived"] or (entry.get("retired") and entry.get("account")):
                vacated.append((record["address"], record["directory"]))
        return vacated

    @staticmethod
    def _directories(record) -> list[str]:
        return [record["directory"] + "/work", "shared"]

    def provision(self, role_id: str, grant: Organization, *, replace_role_id=None) -> str:
        """Bind a role to its address; a replacement archives the previous subtree."""
        identifier(role_id)
        if replace_role_id is not None:
            identifier(replace_role_id)
        with self._lock():
            ledger = self._load()
            pending = ledger["pending_archive"]
            if pending:
                if pending["replacement"] != role_id or pending["role"] != replace_role_id:
                    raise ValueError("An organization replacement must finish before provisioning")
                self._finish_archive(ledger)
            current = ledger["addresses"].get(grant.address)
            if current is not None and current != role_id:
                if current != replace_role_id:
                    raise ValueError("Organization address belongs to another role")
                ledger["pending_archive"] = {
                    "role": current,
                    "replacement": role_id,
                    "source": ledger["roles"][current]["directory"],
                }
                self._save(ledger)
                self._finish_archive(ledger)
            record = ledger["roles"].get(role_id)
            if record is not None:
                if record["address"] != grant.address or record["archived"]:
                    raise ValueError("Role storage cannot change organization identity")
            else:
                record = {
                    "address": grant.address,
                    "directory": "org/" + grant.address,
                    "archived": False,
                }
                ledger["roles"][role_id] = record
                ledger["addresses"][grant.address] = role_id
                self._save(ledger)
            entry = self._account(ledger, role_id)
            self._remote(
                {"action": "provision", "directories": self._directories(record)},
                self._account_step(entry),
                {
                    "action": "grants",
                    "grants": [g.argv for g in self._role_grants(ledger, role_id)],
                },
            )
            return record["directory"] + "/work"

    def _finish_archive(self, ledger) -> None:
        pending = ledger["pending_archive"]
        source = pending["source"]
        destination = "org/.retired/" + pending["role"]
        moved = []
        for role_id, record in ledger["roles"].items():
            if record["directory"] == source or record["directory"].startswith(source + "/"):
                record["directory"] = destination + record["directory"][len(source) :]
                record["archived"] = True
                moved.append(record)
                if ledger["addresses"].get(record["address"]) == role_id:
                    del ledger["addresses"][record["address"]]
        # Only the address's current reporting line reads the archive, and nobody writes it.
        members = self._members(ledger)
        grants = [strip(f"{TREE}/{destination}")]
        for record in moved:
            grants += vacated_grants(record["address"], record["directory"], members)
        self._remote(
            {"action": "archive", "source": source, "destination": destination},
            {"action": "provision", "directories": [item["directory"] + "/work" for item in moved]},
            {"action": "grants", "grants": [grant.argv for grant in dict.fromkeys(grants)]},
        )
        ledger["pending_archive"] = None
        self._save(ledger)

    # ---- grants and profiles ------------------------------------------------

    def _role_grants(self, ledger, role_id) -> list[Grant]:
        record = ledger["roles"][role_id]
        entry = ledger["accounts"].get(role_id) or {}
        if record["archived"] or entry.get("retired") or not entry.get("account"):
            return []
        return role_grants(
            entry["account"],
            Organization(record["address"]),
            self._members(ledger),
            self._vacated(ledger),
        )

    def profiles(self) -> dict[str, dict]:
        """Every current role's bounded ``profile.md``, keyed by role ID."""
        paths = {
            role_id: record["directory"] + "/work"
            for role_id, record in self._load()["roles"].items()
            if not record["archived"]
        }
        if not paths:
            return {}
        (reply,) = self._remote({"action": "profiles", "paths": paths, "limit": PROFILE_LIMIT})
        return {role_id: reply.get(role_id, {"status": "missing"}) for role_id in paths}

    # ---- files written for a role ------------------------------------------

    def write_work_file(self, role_id: str, directory: str, name: str, data: bytes) -> str:
        """Create a new file in a directory of the role's current workspace.

        A taken name gets a numeric suffix; nothing is replaced. Returns the file's
        drive-relative path.
        """
        identifier(role_id)
        identifier(directory)
        if not isinstance(data, bytes) or len(data) > WORK_FILE_LIMIT:
            raise ValueError("Work file exceeds its size limit")
        record = self._load()["roles"].get(role_id)
        if record is None or record["archived"]:
            raise RuntimeError(f"Role {role_id} has no current workspace")
        path = f"{record['directory']}/work/{directory}"
        (reply,) = self._remote(
            {
                "action": "write",
                "directory": path,
                "name": name,
                "data": base64.b64encode(data).decode(),
            }
        )
        return f"{path}/{reply['name']}"
