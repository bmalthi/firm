"""Bounded, read-only organization discovery over one firm's public registry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from firm_sandbox.file_grants import in_line

from .errors import FirmError
from .prompt_resources import render
from .response_limits import CONTROL_RESERVE_BYTES, DETAIL_RESULT_BYTES, LIST_RESULT_BYTES
from .store import OWNER, Store, encoded
from .workspaces import workspace_path

MAX_DISCOVERY_BYTES = DETAIL_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_LIST_BYTES = LIST_RESULT_BYTES - CONTROL_RESERVE_BYTES
MAX_QUERY_BYTES = 2048
MAX_CURSOR_CHARACTERS = 8192
STOP_WORDS = frozenset(
    "a an and are as at be by can could do does for from how i in is it me my of "
    "on or our should that the their this to us we what when where which who with "
    "would you your".split()
)
TOKEN = re.compile(r"\w+", re.UNICODE)
FIELDS = ("address", "identity", "profile", "mandate")
WEIGHTS = {"address": 2, "identity": 2, "profile": 1, "mandate": 1}


def _bytes(value: object) -> int:
    return len(encoded(value).encode("utf-8"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity(role: dict) -> str:
    if role.get("employee_id") == OWNER:
        return role.get("identity", "Owner")
    for line in role.get("public_profile", "").splitlines():
        # Profiles are free-form Markdown; an explicit name outranks the heading.
        label, separator, value = line.strip().partition(":")
        if separator and label.strip(" #*-_\t").casefold() == "public name":
            name = value.strip(" *_`\t")
            if name:
                return name[:200]
    return next(
        (
            line.strip().lstrip("#").strip()[:200]
            for line in role.get("public_profile", "").splitlines()
            if line.strip().lstrip("#").strip()
        ),
        role["address"],
    )


def _manager(store: Store, db: sqlite3.Connection, manager_id: str) -> dict | None:
    role = store.resolve(db, manager_id)
    if role is None:
        return None
    return {
        "employee_id": role["employee_id"],
        "address": role["address"],
        "identity": identity(role),
    }


def compact_role(store: Store, db: sqlite3.Connection, role: dict) -> dict:
    return {
        "employee_id": role["employee_id"],
        "address": role["address"],
        "identity": identity(role),
        "manager": (
            _manager(store, db, role["manager_id"]) if role["employee_id"] != OWNER else None
        ),
        "employment": role["employment"],
        "accepts_mail": role["employment"] != "retired",
    }


def _terms(text: str) -> set[str]:
    return {word for word in TOKEN.findall(text.casefold()) if word not in STOP_WORDS}


def _excerpt(text: str, query_terms: set[str], maximum: int = 240) -> dict:
    matches = [match for match in TOKEN.finditer(text) if match.group().casefold() in query_terms]
    if len(text) <= maximum or not matches:
        start = 0
    else:
        starts = {max(0, match.start() - 70) for match in matches}
        start = max(
            sorted(starts),
            key=lambda offset: len(_terms(text[offset : offset + maximum]) & query_terms),
        )
    end = min(len(text), start + maximum)
    return {
        "text": text[start:end],
        "start": start,
        "end": end,
        "omitted_before": start > 0,
        "omitted_after": end < len(text),
    }


@dataclass(frozen=True)
class _SearchRole:
    row: dict
    identity: str
    mandate: dict


class Discovery:
    """Organization reads. Caller authentication remains in Runtime.authorized."""

    def __init__(self, store: Store, *, files=None):
        self.store = store
        self.files = files

    def resolve_continuation(self, activation_id: str, reference: str | None) -> str | None:
        """Role tools expose short work-period handles; signed view validation stays below."""
        if reference is None:
            return None
        if not isinstance(reference, str) or re.fullmatch(r"d[1-9][0-9]{0,18}", reference) is None:
            raise FirmError(render("references", "invalid_continuation"))
        with self.store.read() as db:
            row = db.execute(
                "SELECT cursor FROM discovery_continuations WHERE activation_id=? AND number=?",
                (activation_id, reference[1:]),
            ).fetchone()
            if row is None:
                raise FirmError(render("references", "invalid_continuation"))
            return row["cursor"]

    def present_continuation(self, activation_id: str, result: dict) -> dict:
        cursor = result.get("continuation")
        if cursor is None:
            return result
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO discovery_continuations(activation_id,cursor) VALUES(?,?)",
                (activation_id, cursor),
            )
            number = db.execute(
                "SELECT number FROM discovery_continuations WHERE activation_id=? AND cursor=?",
                (activation_id, cursor),
            ).fetchone()[0]
            return {**result, "continuation": f"d{number}"}

    def _secret(self, db: sqlite3.Connection) -> bytes:
        row = db.execute(
            "SELECT value FROM runtime_secrets WHERE name='discovery_cursor'"
        ).fetchone()
        if row is None:
            raise RuntimeError("discovery cursor secret is unavailable")
        return bytes.fromhex(row["value"])

    def _firm_id(self, db: sqlite3.Connection) -> str:
        return self.store.firm(db)["firm_id"]

    def _cursor(self, db: sqlite3.Connection, payload: dict) -> str:
        raw = encoded(payload).encode("utf-8")
        signature = hmac.new(self._secret(db), raw, hashlib.sha256).digest()
        return ".".join(
            base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii") for part in (raw, signature)
        )

    def _open_cursor(self, db: sqlite3.Connection, cursor: str, expected: dict) -> dict:
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARACTERS:
            raise FirmError("invalid discovery continuation")
        try:
            left, right = cursor.split(".", 1)
            raw = base64.urlsafe_b64decode(left + "=" * (-len(left) % 4))
            supplied = base64.urlsafe_b64decode(right + "=" * (-len(right) % 4))
            if any(
                base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii") != value
                for part, value in ((raw, left), (supplied, right))
            ):
                raise ValueError
            valid = hmac.compare_digest(
                supplied, hmac.new(self._secret(db), raw, hashlib.sha256).digest()
            )
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise FirmError("invalid discovery continuation") from None
        if not valid or not isinstance(payload, dict):
            raise FirmError("invalid discovery continuation")
        if any(payload.get(key) != value for key, value in expected.items()):
            raise FirmError(render("references", "changed_view"))
        return payload

    @staticmethod
    def _mandate(db: sqlite3.Connection, employee_id: str) -> dict:
        row = db.execute(
            "SELECT revision,author_id,body FROM mandates WHERE employee_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (employee_id,),
        ).fetchone()
        if row is None:
            return {"status": "missing", "revision": None, "author_id": None, "body": ""}
        return {"status": "available", **dict(row)}

    def _source_snapshot(self, role: dict, mandate: dict) -> str:
        return _digest(
            encoded(
                {
                    "employee_id": role["employee_id"],
                    "public_profile_status": role["public_profile_status"],
                    "public_profile_digest": role["public_profile_digest"],
                    "public_profile_body": _digest(role["public_profile"]),
                    "mandate_status": mandate["status"],
                    "mandate_revision": mandate["revision"],
                    "mandate_body": _digest(mandate["body"]),
                }
            )
        )

    def _workspace(
        self,
        db: sqlite3.Connection,
        actor_id: str,
        target: dict,
        activation_id: str,
    ) -> dict:
        """The target's workspace path and the caller's access, only within its reporting line."""
        activation = db.execute(
            "SELECT employee_id,context_json FROM activations WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
        if activation is None or activation["employee_id"] != actor_id:
            raise FirmError("activation no longer exists")
        references = json.loads(activation["context_json"]).get("workspace_references", {})
        actor_address = self.store.role(db, actor_id)["address"]
        workspace = None
        access = None
        line = in_line(actor_address, target["address"])
        if target["employee_id"] == actor_id:
            workspace = references.get("workspace")
            access = "read_write" if workspace else None
        elif line and references.get("drive") and self.files is not None:
            try:
                path = workspace_path(self.files, target["employee_id"])
            except (ValueError, OSError):
                path = None
            if path is not None:
                workspace = f"{references['drive']}/{path}"
                report = target["address"].startswith(actor_address + "/")
                access = (
                    "read_write"
                    if report and target["employment"] != "retired"
                    else "read_only"
                )
        result = {"path": workspace, "access": access}
        if not line:
            result["visibility"] = "reporting_line"
        return result

    def get_role(
        self,
        actor_id: str,
        *,
        address: str | None = None,
        employee_id: str | None = None,
        section: str = "all",
        cursor: str | None = None,
        activation_id: str | None = None,
    ) -> dict:
        if (address is None) == (employee_id is None):
            raise FirmError("supply exactly one of address or employee_id")
        if section not in {"all", "profile", "mandate"}:
            raise FirmError("section must be all, profile, or mandate")
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            if address is not None:
                row = db.execute(
                    "SELECT * FROM roles WHERE address=? AND employment!='retired'", (address,)
                ).fetchone()
                if row is None:
                    raise FirmError("unknown current role address")
                role = dict(row)
            else:
                role = self.store.role(db, employee_id or "")
            mandate = self._mandate(db, role["employee_id"])
            snapshot = self._source_snapshot(role, mandate)
            expected = {
                "firm": self._firm_id(db),
                "actor": actor_id,
                "operation": "get_role",
                "employee_id": role["employee_id"],
                "section": section,
                "snapshot": snapshot,
            }
            source_index, offset = 0, 0
            if cursor is not None:
                payload = self._open_cursor(db, cursor, expected)
                source_index, offset = payload.get("source_index"), payload.get("offset")
                if type(source_index) is not int or type(offset) is not int:
                    raise FirmError("invalid discovery continuation position")

            selected = ["profile", "mandate"] if section == "all" else [section]
            if not 0 <= source_index < len(selected) or offset < 0:
                raise FirmError("invalid discovery continuation position")
            profile_status = role["public_profile_status"]
            sources = {
                "profile": {
                    "status": profile_status,
                    "author_id": role["employee_id"],
                    "digest": role["public_profile_digest"],
                    "body": role["public_profile"] if profile_status == "available" else "",
                    "label": "last_recorded" if role["employment"] == "retired" else "current",
                },
                "mandate": {
                    "status": mandate["status"],
                    "author_id": mandate["author_id"],
                    "revision": mandate["revision"],
                    "digest": _digest(mandate["body"])
                    if mandate["status"] == "available"
                    else None,
                    "body": mandate["body"],
                    "label": "last_recorded" if role["employment"] == "retired" else "current",
                },
            }
            if offset > len(sources[selected[source_index]]["body"]):
                raise FirmError("invalid discovery continuation position")

            direct_reports = db.execute(
                "SELECT COUNT(*) FROM roles WHERE manager_id=? AND employment!='retired'",
                (role["employee_id"],),
            ).fetchone()[0]
            base = {
                "role": compact_role(self.store, db, role),
                "direct_report_count": direct_reports,
                "tree": f'org_tree(root="{role["address"]}", depth=1)',
                "workspace": (
                    self._workspace(db, actor_id, role, activation_id)
                    if activation_id is not None
                    else {"status": "owner_console_inspection"}
                ),
                "selection": section,
            }

            shown: dict[str, dict] = {}

            def render(next_position: tuple[int, int] | None) -> dict:
                result = {**base, "documents": shown, "complete": next_position is None}
                if next_position is None:
                    result["continuation"] = None
                else:
                    result["continuation"] = self._cursor(
                        db,
                        {
                            **expected,
                            "source_index": next_position[0],
                            "offset": next_position[1],
                        },
                    )
                return result

            for index in range(source_index, len(selected)):
                name = selected[index]
                source = sources[name]
                start = offset if index == source_index else 0
                body = source.pop("body")
                metadata = {**source, "character_start": start}
                if source["status"] != "available":
                    shown[name] = {**metadata, "text": "", "character_end": 0, "complete": True}
                    if (
                        _bytes(render((index + 1, 0) if index + 1 < len(selected) else None))
                        > MAX_DISCOVERY_BYTES
                    ):
                        raise FirmError("role metadata exceeds the discovery response budget")
                    continue

                complete_doc = {
                    **metadata,
                    "text": body[start:],
                    "character_end": len(body),
                    "complete": True,
                }
                shown[name] = complete_doc
                next_position = (index + 1, 0) if index + 1 < len(selected) else None
                if _bytes(render(next_position)) <= MAX_DISCOVERY_BYTES:
                    continue

                low, high = 0, len(body) - start
                while low < high:
                    amount = (low + high + 1) // 2
                    end = start + amount
                    shown[name] = {
                        **metadata,
                        "text": body[start:end],
                        "character_end": end,
                        "complete": False,
                    }
                    if _bytes(render((index, end))) <= MAX_DISCOVERY_BYTES:
                        low = amount
                    else:
                        high = amount - 1
                if low == 0:
                    # A completed earlier document can legitimately consume the rest of
                    # this page. Advance to the next selected document without requiring
                    # its metadata and first character to fit on the prior page.
                    shown.pop(name)
                    if index > source_index:
                        return render((index, start))
                    raise FirmError("role metadata exceeds the discovery response budget")
                end = start + low
                shown[name] = {
                    **metadata,
                    "text": body[start:end],
                    "character_end": end,
                    "complete": False,
                }
                return render((index, end))
            result = render(None)
            if _bytes(result) > MAX_DISCOVERY_BYTES:
                raise FirmError("role metadata exceeds the discovery response budget")
            return result

    def _search_roles(self, db: sqlite3.Connection) -> list[_SearchRole]:
        return [
            _SearchRole(dict(row), identity(dict(row)), self._mandate(db, row["employee_id"]))
            for row in db.execute(
                "SELECT * FROM roles WHERE employment!='retired' ORDER BY employee_id"
            )
        ]

    @staticmethod
    def _search_snapshot(roles: list[_SearchRole]) -> str:
        return _digest(
            encoded(
                [
                    {
                        "employee_id": item.row["employee_id"],
                        "address": item.row["address"],
                        "manager_id": item.row["manager_id"],
                        "employment": item.row["employment"],
                        "public_profile_status": item.row["public_profile_status"],
                        "public_profile": item.row["public_profile"],
                        "mandate_revision": item.mandate["revision"],
                        "mandate": item.mandate["body"],
                    }
                    for item in roles
                ]
            )
        )

    @staticmethod
    def _rank(roles: list[_SearchRole], query: str) -> list[_SearchRole]:
        needle = query.strip().casefold()
        query_terms = _terms(query)
        role_tokens = {
            item.row["employee_id"]: {
                "address": _terms(item.row["address"]),
                "identity": _terms(item.identity),
                "profile": _terms(item.row["public_profile"]),
                "mandate": _terms(item.mandate["body"]),
            }
            for item in roles
        }
        frequency = Counter(
            word for fields in role_tokens.values() for word in set().union(*fields.values())
        )
        idf = {
            word: 1 + math.log((1 + len(roles)) / (1 + count)) for word, count in frequency.items()
        }
        ranked = []
        for item in roles:
            fields = role_tokens[item.row["employee_id"]]
            exact = needle != "" and needle in {
                item.row["address"].casefold(),
                item.identity.casefold(),
            }
            matched = query_terms & set().union(*fields.values())
            if needle and not exact and not matched:
                continue
            score = sum(
                idf[word] * max(WEIGHTS[field] for field in FIELDS if word in fields[field])
                for word in matched
            )
            if needle and any(
                needle in value.casefold()
                for value in (
                    item.row["address"],
                    item.identity,
                    item.row["public_profile"],
                    item.mandate["body"],
                )
            ):
                score += 2
            ranked.append((not exact, -score, item.row["employee_id"], item))
        return [entry[3] for entry in sorted(ranked)]

    def find_roles(
        self,
        actor_id: str,
        query: str = "",
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict:
        if not isinstance(query, str) or len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise FirmError("query exceeds 2048 bytes")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FirmError("limit must be between 1 and 100")
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            roles = self._search_roles(db)
            snapshot = self._search_snapshot(roles)
            expected = {
                "firm": self._firm_id(db),
                "actor": actor_id,
                "operation": "find_roles",
                "query": query,
                "snapshot": snapshot,
            }
            offset = 0
            if cursor is not None:
                payload = self._open_cursor(db, cursor, expected)
                offset = payload.get("offset")
            ranked = self._rank(roles, query)
            if type(offset) is not int or not 0 <= offset <= len(ranked):
                raise FirmError("invalid discovery continuation position")
            query_terms = _terms(query)

            def card(item: _SearchRole) -> dict:
                evidence = []
                values = {
                    "address": item.row["address"],
                    "identity": item.identity,
                    "profile": item.row["public_profile"],
                    "mandate": item.mandate["body"],
                }
                for field in FIELDS:
                    matched = _terms(values[field]) & query_terms
                    if matched:
                        evidence.append(
                            {
                                "source": field,
                                "matched_terms": sorted(matched),
                                **_excerpt(values[field], query_terms),
                            }
                        )
                return {
                    **compact_role(self.store, db, item.row),
                    "profile_present": item.row["public_profile_status"] == "available",
                    "mandate_revision": item.mandate["revision"],
                    "evidence": evidence,
                }

            cards: list[dict] = []

            def response(items: list[dict]) -> dict:
                next_offset = offset + len(items)
                remaining = len(ranked) - next_offset
                continuation = (
                    self._cursor(db, {**expected, "offset": next_offset}) if remaining else None
                )
                return {
                    "query": query,
                    "total_matches": len(ranked),
                    "offset": offset,
                    "roles": items,
                    "remaining": remaining,
                    "continuation": continuation,
                }

            for item in ranked[offset : offset + limit]:
                candidate = [*cards, card(item)]
                if _bytes(response(candidate)) > MAX_LIST_BYTES:
                    if not cards:
                        raise FirmError(
                            "first complete role match exceeds the discovery response budget"
                        )
                    break
                cards = candidate
            result = response(cards)
            if _bytes(result) > MAX_LIST_BYTES:
                raise FirmError("search metadata exceeds the discovery response budget")
            return result

    @staticmethod
    def _org_snapshot(rows: list[dict]) -> str:
        return _digest(
            encoded(
                [
                    {
                        "employee_id": row["employee_id"],
                        "address": row["address"],
                        "manager_id": row["manager_id"],
                        "employment": row["employment"],
                        "identity": identity(row),
                    }
                    for row in sorted(rows, key=lambda value: value["employee_id"])
                ]
            )
        )

    def org_tree(
        self,
        actor_id: str,
        root: str | None = None,
        depth: int = 2,
        max_nodes: int = 50,
        cursor: str | None = None,
    ) -> dict:
        if type(depth) is not int or not 0 <= depth <= 32:
            raise FirmError("depth must be between 0 and 32")
        if type(max_nodes) is not int or not 1 <= max_nodes <= 100:
            raise FirmError("max_nodes must be between 1 and 100")
        with self.store.read() as db:
            self.store.require_actor(db, actor_id)
            rows = [
                dict(row) for row in db.execute("SELECT * FROM roles WHERE employment!='retired'")
            ]
            by_id = {row["employee_id"]: row for row in rows}
            if root is None:
                root_role = next((row for row in rows if row["address"] == "founder"), None)
            else:
                root_role = next(
                    (row for row in rows if row["address"] == root or row["employee_id"] == root),
                    None,
                )
            if root_role is None:
                raise FirmError("unknown or retired organization root")
            children: dict[str, list[dict]] = {}
            for row in rows:
                children.setdefault(row["manager_id"], []).append(row)
            for items in children.values():
                items.sort(key=lambda item: (item["address"], identity(item), item["employee_id"]))

            all_descendants: list[tuple[dict, int]] = []

            def walk(employee: str, level: int, seen: set[str]) -> None:
                if employee in seen:
                    raise FirmError("cyclic reporting structure")
                branch = {*seen, employee}
                for child in children.get(employee, []):
                    all_descendants.append((child, level))
                    walk(child["employee_id"], level + 1, branch)

            walk(root_role["employee_id"], 1, set())
            eligible = [(row, level) for row, level in all_descendants if level <= depth]
            snapshot = self._org_snapshot(rows)
            expected = {
                "firm": self._firm_id(db),
                "actor": actor_id,
                "operation": "org_tree",
                "root_id": root_role["employee_id"],
                "depth": depth,
                "max_nodes": max_nodes,
                "snapshot": snapshot,
            }
            offset = 0
            if cursor is not None:
                payload = self._open_cursor(db, cursor, expected)
                offset = payload.get("offset")
            if type(offset) is not int or not 0 <= offset <= len(eligible):
                raise FirmError("invalid discovery continuation position")

            breadcrumb = []
            manager_id = root_role["manager_id"]
            seen = set()
            while manager_id != OWNER:
                if manager_id in seen or manager_id not in by_id:
                    raise FirmError("invalid reporting structure")
                seen.add(manager_id)
                manager = by_id[manager_id]
                breadcrumb.append(compact_role(self.store, db, manager))
                manager_id = manager["manager_id"]
            breadcrumb.reverse()

            def node(row: dict, level: int) -> dict:
                return {
                    **compact_role(self.store, db, row),
                    "depth": level,
                    "direct_report_count": len(children.get(row["employee_id"], [])),
                }

            nodes: list[dict] = []

            def response(items: list[dict]) -> dict:
                next_offset = offset + len(items)
                remaining = len(eligible) - next_offset
                return {
                    "root": {
                        **compact_role(self.store, db, root_role),
                        "direct_report_count": len(children.get(root_role["employee_id"], [])),
                    },
                    "breadcrumb": breadcrumb,
                    "depth": depth,
                    "total_descendants": len(all_descendants),
                    "depth_eligible_descendants": len(eligible),
                    "omitted_by_depth": len(all_descendants) - len(eligible),
                    "offset": offset,
                    "nodes": items,
                    "remaining": remaining,
                    "continuation": (
                        self._cursor(db, {**expected, "offset": next_offset}) if remaining else None
                    ),
                }

            for row, level in eligible[offset : offset + max_nodes]:
                candidate = [*nodes, node(row, level)]
                if _bytes(response(candidate)) > MAX_LIST_BYTES:
                    if not nodes:
                        raise FirmError("tree metadata leaves no room for an eligible node")
                    break
                nodes = candidate
            result = response(nodes)
            if _bytes(result) > MAX_LIST_BYTES:
                raise FirmError("tree metadata exceeds the discovery response budget")
            return result
