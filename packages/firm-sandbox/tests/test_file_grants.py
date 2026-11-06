from firm_sandbox import Organization
from firm_sandbox.file_grants import (
    GROUP,
    TREE,
    firm_grants,
    in_line,
    role_grants,
    vacated_grants,
)

MEMBERS = {
    "founder": "f-founder",
    "founder/eng": "f-eng",
    "founder/eng/api": "f-api",
    "founder/eng/web": "f-web",
    "founder/ops": "f-ops",
    "founder/ops/audit": "f-audit",
}


def without(removed):
    return {address: account for address, account in MEMBERS.items() if address != removed}


def commands(grants):
    return [" ".join(grant.argv) for grant in grants]


def firm(members, vacated=()):
    grants = firm_grants()
    for address, account in members.items():
        grants += role_grants(account, Organization(address), members, vacated)
    return grants


def effective(grants):
    """Access entries by path and principal, as the last ``setfacl -m`` left them."""
    table = {}
    for grant in grants:
        for entry in grant.entries:
            if not entry.startswith("d:"):
                kind, name, rights = entry.split(":")
                table[grant.path, f"{kind}:{name}"] = rights
    return table


def rights(table, account, path):
    return table.get((path, f"u:{account}"), table.get((path, f"g:{GROUP}"), ""))


def can(table, account, path, right):
    """Whether a member of the group reaches ``path`` with ``right``, traversing from the root."""
    parts = path.split("/")
    directories = [TREE] + [f"{TREE}/" + "/".join(parts[:depth]) for depth in range(1, len(parts))]
    if any("x" not in rights(table, account, directory) for directory in directories):
        return False
    return right in rights(table, account, f"{TREE}/{path}")


def test_firm_grants_traverse_to_org_and_share_shared():
    assert commands(firm_grants()) == [
        f"setfacl -m g:{GROUP}:x /srv/firm",
        f"setfacl -m g:{GROUP}:x /srv/firm/org",
        f"setfacl -R -m g:{GROUP}:rwx,d:g:{GROUP}:rwx /srv/firm/shared",
    ]


def test_work_is_readable_by_the_reporting_line_and_writable_from_above():
    table = effective(firm(MEMBERS))
    for viewer, account in MEMBERS.items():
        for target in MEMBERS:
            work = f"org/{target}/work"
            writer = target == viewer or target.startswith(viewer + "/")
            assert can(table, account, work, "r") == in_line(viewer, target), (viewer, target)
            assert can(table, account, work, "w") == writer, (viewer, target)
            assert can(table, account, f"org/{target}", "r") == writer, (viewer, target)
    assert can(table, "f-api", "shared", "w")


def test_siblings_and_cousins_neither_read_nor_list_each_others_work():
    table = effective(firm(MEMBERS))
    for account, other in (("f-api", "founder/eng/web"), ("f-eng", "founder/ops"),
                           ("f-api", "founder/ops/audit"), ("f-audit", "founder/eng/api")):
        assert not can(table, account, f"org/{other}/work", "r")
        assert not can(table, account, f"org/{other}/work", "x")
        assert not can(table, account, f"org/{other}", "r")
    assert not can(table, "f-api", "org/founder", "r")
    assert not can(table, "f-api", "org", "r")


def test_a_hire_grants_its_line_both_ways_without_recomputing_the_firm():
    before = without("founder/eng/web")
    hired = role_grants("f-web", Organization("founder/eng/web"), MEMBERS)
    assert effective(firm(before) + hired) == effective(firm(MEMBERS))
    assert commands(hired)[:4] == [
        "setfacl -m u:f-web:x /srv/firm/org/founder",
        "setfacl -m u:f-web:x /srv/firm/org/founder/eng",
        "setfacl -m u:f-web:rx /srv/firm/org/founder/eng/web",
        "setfacl -R -m u:f-web:rwx,d:u:f-web:rwx /srv/firm/org/founder/eng/web/work",
    ]
    assert "setfacl -R -m u:f-founder:rwx,d:u:f-founder:rwx /srv/firm/org/founder/eng/web/work" in (
        commands(hired))
    assert "setfacl -R -m u:f-eng:rwx,d:u:f-eng:rwx /srv/firm/org/founder/eng/web/work" in (
        commands(hired))
    assert "setfacl -R -m u:f-web:rx,d:u:f-web:rx /srv/firm/org/founder/eng/work" in commands(hired)
    assert not any("api" in command or "ops" in command for command in commands(hired))


def test_missing_ancestor_account_is_skipped_until_it_exists():
    grants = role_grants("f-api", Organization("founder/eng/api"), {"founder/eng/api": "f-api"})
    assert all("f-eng" not in command for command in commands(grants))


def test_vacated_work_is_read_only_to_the_current_line_of_its_address():
    archive = ("founder/eng/api", "org/.retired/r-api")
    members = without("founder/eng/api")
    grants = firm(members) + vacated_grants(*archive, members)
    table = effective(grants)
    work = "org/.retired/r-api/work"
    for address, account in members.items():
        assert can(table, account, work, "r") == in_line("founder/eng/api", address), address
        assert not can(table, account, work, "w")
    assert not can(table, "f-eng", "org/.retired", "r")
    # A replacement at the address reads its predecessor's archive through its own grants.
    replaced = {**members, "founder/eng/api": "f-api2"}
    hired = role_grants("f-api2", Organization("founder/eng/api"), replaced, [archive])
    assert can(effective(grants + hired), "f-api2", work, "r")
    assert effective(firm(replaced, [archive])) == effective(firm(members) + vacated_grants(
        *archive, members) + hired)
