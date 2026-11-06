"""Resolve employee workspace authority without exposing storage identities."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from firm_sandbox import Config, Layout, Organization
from firm_sandbox.models import identifier

from .errors import FirmError
from .store import OWNER, Store


def sandbox_role_id(employee_id: str) -> str:
    candidate = "r-" + employee_id
    try:
        return identifier(candidate)
    except ValueError:
        return "r-" + hashlib.sha256(employee_id.encode()).hexdigest()[:32]


def sandbox_run_id(activation_id: str) -> str:
    return "run-" + hashlib.sha256(activation_id.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Grants:
    references: dict
    organization: Organization


def organization_for(store: Store, employee_id: str) -> Organization:
    """The employee's organization address, checked against its reporting chain."""
    with store.read() as db:
        actor = store.require_actor(db, employee_id)
        role = actor
        seen = set()
        while role["employee_id"] != OWNER:
            if role["employee_id"] in seen:
                raise FirmError("cyclic reporting structure")
            seen.add(role["employee_id"])
            if role["manager_id"] == OWNER:
                if "/" in role["address"]:
                    raise FirmError("reporting relationship does not match workspace hierarchy")
                break
            manager = store.role(db, role["manager_id"])
            if role["address"].rpartition("/")[0] != manager["address"]:
                raise FirmError("reporting relationship does not match workspace hierarchy")
            role = manager
        return Organization(actor["address"])


def grants_for(store: Store, employee_id: str, layout: Layout) -> Grants:
    """Storage grants plus the paths the role will see, in the sandbox's layout.

    The reporting line reaches saved work on the drive. Scratch holds local builds;
    the rest of the private home keeps the role's own tools and state.
    """
    organization = organization_for(store, employee_id)
    return Grants(
        {
            "private": layout.private,
            "scratch": layout.scratch,
            "workspace": layout.workspace(organization.address),
            "drive": layout.drive,
            "organization": layout.organization,
            "shared": layout.shared,
        },
        organization,
    )


def sandbox_config(sandbox_root: str | Path) -> Config:
    """Load the bound firm's sandbox configuration."""
    root = Path(sandbox_root)
    if not root.is_absolute():
        raise FirmError("sandbox root must be absolute")
    path = root / "sandbox.toml"
    try:
        return Config.load(path)
    except (OSError, ValueError) as exc:
        raise FirmError(f"unreadable sandbox configuration {path}: {exc}") from exc


def provision_runtime_role(
    store: Store, sandbox_root: str | Path, employee_id: str, *, files
) -> str:
    """Make a hired employee's workspace and file server login usable.

    Call inside the hiring transaction, or before admitting an existing employee.
    Returns the drive-relative work path. Private homes stay separate; a replaced
    employee's work is archived for the address's reporting line.
    """
    if files.config.root != Path(sandbox_root).resolve():
        raise FirmError("the file server must belong to this sandbox")
    with store.transaction() as db:
        actor = store.require_actor(db, employee_id)
        grant = organization_for(store, employee_id)
        namespace = db.execute(
            "SELECT value FROM runtime_secrets WHERE name='discovery_cursor'"
        ).fetchone()["value"]
        try:
            files.bind_identity(hashlib.sha256(namespace.encode()).hexdigest())
        except (ValueError, OSError) as exc:
            raise FirmError(f"workspace provisioning failed: {exc}") from exc
        role_id = sandbox_role_id(employee_id)
        previous = files.role_at(grant.address)
        if previous is not None and previous != role_id:
            records = [dict(row) for row in db.execute("SELECT * FROM roles")]
            old = next(
                (row for row in records if sandbox_role_id(row["employee_id"]) == previous), None
            )
            # Committed employee rows are never deleted. An immutable storage
            # owner absent from this bound firm's DB is a rolled-back hire;
            # preserve its files in the archive instead of adopting them.
            if old is not None and old["employment"] != "retired":
                raise FirmError("workspace address is owned by another employee")
            subtree = [
                row
                for row in records
                if row["employee_id"] != employee_id
                and (
                    row["address"] == actor["address"]
                    or row["address"].startswith(actor["address"] + "/")
                )
            ]
            if any(row["employment"] != "retired" for row in subtree):
                raise FirmError("cannot replace an organization workspace with live descendants")
            for row in records:
                overlaps = (
                    row["address"] == actor["address"]
                    or row["address"].startswith(actor["address"] + "/")
                    or actor["address"].startswith(row["address"] + "/")
                )
                if row["employment"] != "retired" or not overlaps:
                    continue
                active = db.execute(
                    "SELECT 1 FROM activations WHERE employee_id=? "
                    "AND state IN ('admitted','running') LIMIT 1",
                    (row["employee_id"],),
                ).fetchone()
                if active:
                    raise FirmError(
                        "workspace replacement is waiting for retired subtree roles to finish"
                    )
        try:
            return files.provision(role_id, grant, replace_role_id=previous)
        except (ValueError, OSError, RuntimeError) as exc:
            raise FirmError(f"workspace provisioning failed: {exc}") from exc


def workspace_path(files, employee_id: str) -> str | None:
    """The drive-relative current or archived workspace; never a private home."""
    return files.workspace_path(sandbox_role_id(employee_id))
