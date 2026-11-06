"""File server checks against a disposable firm on a Mac; opt in with FIRM_LIVE=1.

Requires FIRM_SANDBOX_CONFIG naming the dedicated test sandbox. Covers reporting-line
enforcement, inheritance, each role's cifs mount on the Linux VM, unmount at
settlement, revocation and replacement archiving.
"""

import os
import uuid
from pathlib import Path

import pytest

from firm_sandbox import Config, Organization, RunSpec, Sandbox
from firm_sandbox.linux_vm_guest import PYTHON

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("FIRM_LIVE") != "1", reason="live VM opt-in"),
]


@pytest.fixture
def firm():
    config_path = os.environ.get("FIRM_SANDBOX_CONFIG")
    if not config_path:
        pytest.fail("FIRM_SANDBOX_CONFIG must select the dedicated test sandbox")
    box = Sandbox(Config.load(Path(config_path)))
    box.up()
    if any(box.status(run)["running"] for run in box.runs()):
        pytest.fail("File server validation requires an idle disposable sandbox")
    owned = set()
    try:
        yield box, owned
    finally:
        for run in box.runs():
            if run.role_id in owned and box.status(run)["running"]:
                box.stop(run)
        for role_id in owned:
            box.retire(role_id)


def exec_role(box, run, script, *, expect=0):
    """Probe a running account through the owner control channel, without restarting it."""
    account = box.linux.rpc("info")["roles"][run.role_id]["account"]
    result = box.linux._shell(
        "sudo", "-n", "-u", account, "/bin/sh", "-exc", script, check=False, timeout=60,
    )
    assert result.returncode == expect, (result.stdout + result.stderr).decode(errors="replace")


def server_permissions(box, *relative_paths):
    """Read authoritative ownership/modes/ACLs on the server side of the share."""
    paths = ["/srv/firm/" + path for path in relative_paths]
    return (
        box.linux._shell("sudo", "-n", "stat", "-c", "%n %u %g %a", "--", *paths).stdout,
        box.linux._shell("sudo", "-n", "getfacl", "-pn", "--", *paths).stdout,
    )


def test_live_drive_enforces_the_organization_and_settles_cleanly(firm):
    box, owned = firm
    files = box.file_server
    prefix = "fstest-" + uuid.uuid4().hex[:8]
    leader_address, peer_address = prefix + "/engineering", prefix + "/finance"
    child_address = leader_address + "/api"
    leader_id, peer_id, child_id, next_id = [
        prefix + suffix for suffix in ("-eng", "-fin", "-api", "-new")
    ]
    files.provision(prefix, Organization(prefix))
    owned.add(prefix)

    def start(role_id, address, script):
        owned.add(role_id)
        files.provision(role_id, Organization(address))
        return box.start(RunSpec(role_id, ("/bin/sh", "-ec", script),
                                 organization=Organization(address)))

    def completed(run):
        assert box.wait(run, timeout=60)["exit_code"] == 0, box.logs(run).decode()

    def drive(role_id):
        return box.layout(role_id).drive

    leader = start(leader_id, leader_address, "exec sleep 600")
    peer = start(peer_id, peer_address, "exec sleep 600")
    # Each role sees its own mount and nobody else's.
    exec_role(box, leader, f"test -d {drive(leader_id)}/org; test ! -r {drive(peer_id)}/org")
    exec_role(box, leader, f"mountpoint -q {drive(leader_id)}")
    child = start(
        child_id, child_address,
        f'test "$PWD" = {drive(child_id)}/org/{child_address}/work; '
        f"{PYTHON} -c 'import os; assert \"work\" in os.listdir(\"..\")'; "
        'echo child-result > result.txt; echo role-private > "$HOME/private-note"; '
        "mkdir sub; echo deep > sub/deep.txt",
    )
    completed(child)
    # Settlement leaves the mount point behind as an empty directory. Assert that
    # condition rather than mountpoint's exit code, which reports the same "not a
    # mountpoint" verdict as 1 for root and 32 for the unprivileged account.
    exec_role(
        box,
        child,
        f"test -d {drive(child_id)}; test -z \"$(ls -A {drive(child_id)})\"; "
        f"! mountpoint -q {drive(child_id)}",
    )
    child_work = f"org/{child_address}/work"
    leader_work = f"org/{leader_address}/work"
    exec_role(box, leader,
        f'test "$(cat {drive(leader_id)}/{child_work}/result.txt)" = child-result; '
        f"echo manager-edit > {drive(leader_id)}/{child_work}/manager.txt; "
        f"echo manager-deep > {drive(leader_id)}/{child_work}/sub/manager.txt; "
        f"ls {drive(leader_id)}/org/{child_address}; "
        f"echo leader-policy > {drive(leader_id)}/{leader_work}/policy; "
        f"echo firm-collaboration > {drive(leader_id)}/shared/{prefix}")
    # A sibling and a sibling's report are outside the reporting line: no read, no listing.
    sibling_denied = (
        f"if cat {drive(peer_id)}/{child_work}/result.txt; then exit 30; fi; "
        f"if touch {drive(peer_id)}/{child_work}/peer-must-not-write; then exit 31; fi; "
        f"if ls {drive(peer_id)}/{child_work}; then exit 32; fi; "
        f"if ls {drive(peer_id)}/org/{child_address}; then exit 33; fi; "
        f"if cat {drive(peer_id)}/{leader_work}/policy; then exit 34; fi; "
        f"if ls {drive(peer_id)}/org/{prefix}; then exit 35; fi; "
    )
    exec_role(box, peer,
        sibling_denied
        + f'test "$(cat {drive(peer_id)}/shared/{prefix})" = firm-collaboration')
    protected = (child_work, child_work + "/result.txt", child_work + "/sub")
    permissions = server_permissions(box, *protected)
    completed(start(child_id, child_address,
        f'test "$(cat {drive(child_id)}/{leader_work}/policy)" = leader-policy; '
        f"if touch {drive(child_id)}/{leader_work}/no-write; then exit 34; fi; "
        f"if ls {drive(child_id)}/org/{prefix}; then exit 35; fi; "
        # A cifs chmod can succeed as a no-op. Test the actual security boundary:
        # neither restrictive nor permissive modes, nor ACL removal, may change
        # server permissions or strip the manager's access.
        'for target in result.txt sub; do '
        'for mode in 000 700 777; do chmod "$mode" "$target" || true; done; '
        'setfacl -b "$target" 2>/dev/null || true; done; '
        'test "$(cat "$HOME/private-note")" = role-private'))
    assert server_permissions(box, *protected) == permissions
    exec_role(box, leader,
        f'test "$(cat {drive(leader_id)}/{child_work}/result.txt)" = child-result; '
        f"echo retained-access > {drive(leader_id)}/{child_work}/sub/after-chmod.txt")
    exec_role(box, peer, sibling_denied + "true")

    box.retire(child_id)
    files.provision(next_id, Organization(child_address), replace_role_id=child_id)
    owned.add(next_id)
    archive = files.workspace_path(child_id)
    assert archive.startswith("org/.retired/")
    exec_role(box, leader,
        # The leader's mount has the retired path cached; `actimeo=1` expires it.
        f"for _ in $(seq 20); do test -e {drive(leader_id)}/{child_work}/result.txt || break; "
        "sleep 0.5; done; "
        f"test ! -e {drive(leader_id)}/{child_work}/result.txt; "
        f"echo replacement > {drive(leader_id)}/{child_work}/new.txt; "
        f'test "$(cat {drive(leader_id)}/{archive}/result.txt)" = child-result; '
        f"test ! -e {drive(leader_id)}/{archive}/peer-must-not-write; "
        f"if touch {drive(leader_id)}/{archive}/archive-must-not-write; then exit 36; fi")
    exec_role(box, peer, f"if cat {drive(peer_id)}/{archive}/result.txt; then exit 37; fi")
    completed(start(next_id, child_address,
        'test ! -e "$HOME/private-note"; test "$(cat new.txt)" = replacement; '
        f'test "$(cat {drive(next_id)}/{archive}/result.txt)" = child-result'))
    with pytest.raises(RuntimeError, match="no file server login"):
        files.credentials(child_id)
    assert box.status(leader)["running"] and box.status(peer)["running"]
    assert files.profiles()[next_id] == {"status": "missing"}
