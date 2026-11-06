"""The organization chart as POSIX ACL operations on the file server's tree.

Every role's Samba login belongs to ``firmfiles``, which traverses the root and ``org``
and writes ``shared``. A role's work at ``org/<address>/work`` belongs to its reporting
line: the role and its ancestors write it, its descendants read it, nobody else reaches
it. Work no current role holds, a retired role's or an archive under ``org/.retired``,
is read-only to the current reporting line of its address. A role traverses the
directories above each work directory it reaches and lists the address directories of
its own subtree. Default entries make new files inherit the grant. Applying the
operations repeatedly converges; only ``strip`` removes entries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import Organization

GROUP = "firmfiles"
TREE = "/srv/firm"


@dataclass(frozen=True)
class Grant:
    path: str
    entries: tuple[str, ...]
    recursive: bool

    @property
    def argv(self) -> list[str]:
        recursive = ["-R"] if self.recursive else []
        if not self.entries:  # remove every extended entry
            return ["setfacl", *recursive, "-b", self.path]
        return ["setfacl", *recursive, "-m", ",".join(self.entries), self.path]


def strip(path: str) -> Grant:
    return Grant(path, (), True)


def _tree(principal: str, rights: str) -> tuple[str, str]:
    return (f"{principal}:{rights}", f"d:{principal}:{rights}")


def firm_grants(root: str = TREE) -> list[Grant]:
    return [
        Grant(root, (f"g:{GROUP}:x",), False),
        Grant(f"{root}/org", (f"g:{GROUP}:x",), False),
        Grant(f"{root}/shared", _tree(f"g:{GROUP}", "rwx"), True),
    ]


def in_line(address: str, other: str) -> bool:
    """Whether ``other`` is ``address``, one of its ancestors or one of its descendants."""
    return other == address or other.startswith(address + "/") or address.startswith(other + "/")


def _work(account: str, address: str, directory: str, rights: str, root: str) -> list[Grant]:
    """``rights`` on a drive-relative ``directory``'s work, reached from ``org`` down."""
    own = f"org/{address}"
    parts = directory.split("/")
    grants = []
    for depth in range(2, len(parts) + 1):
        path = "/".join(parts[:depth])
        listing = "rx" if path == own or path.startswith(own + "/") else "x"
        grants.append(Grant(f"{root}/{path}", (f"u:{account}:{listing}",), False))
    grants.append(Grant(f"{root}/{directory}/work", _tree(f"u:{account}", rights), True))
    return grants


def role_grants(
    account: str,
    organization: Organization,
    members: dict[str, str],
    vacated: Iterable[tuple[str, str]] = (),
    root: str = TREE,
) -> list[Grant]:
    """What a role and its current reporting line need on each other's work.

    ``members`` maps every current address to its account; ``vacated`` pairs the address
    and drive-relative directory of every work no current role holds.
    """
    address = organization.address
    own = f"org/{address}"
    grants = _work(account, address, own, "rwx", root)
    for other, holder in members.items():
        if other.startswith(address + "/"):
            grants += _work(account, address, f"org/{other}", "rwx", root)
            grants += _work(holder, other, own, "rx", root)
        elif address.startswith(other + "/"):
            grants += _work(account, address, f"org/{other}", "rx", root)
            grants += _work(holder, other, own, "rwx", root)
    for other, directory in vacated:
        if in_line(address, other):
            grants += _work(account, address, directory, "rx", root)
    return list(dict.fromkeys(grants))


def vacated_grants(
    address: str, directory: str, members: dict[str, str], root: str = TREE
) -> list[Grant]:
    """Read-only access to work no current role holds, for its address's reporting line."""
    grants = []
    for other, holder in members.items():
        if in_line(address, other):
            grants += _work(holder, other, directory, "rx", root)
    return list(dict.fromkeys(grants))
