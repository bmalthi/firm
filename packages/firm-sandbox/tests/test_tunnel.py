import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox.guest_control import save
from firm_sandbox.tunnel import Tunnel, runtime_tunnel

TRUSTED = SimpleNamespace(verify_runtime=lambda: None)


@pytest.fixture
def tunnel():
    with tempfile.TemporaryDirectory(prefix="fvrt-", dir="/private/tmp") as directory:
        root = Path(directory)
        vm = SimpleNamespace(
            root=root,
            firm_locks=root / "firm-locks",
            socket_root=root,
            ssh_config=root / "lima/ssh.config",
            commands=None,
            paths=TRUSTED,
            _state=lambda: {"generation": "g"},
        )
        yield runtime_tunnel(vm, 8765)


def test_dead_control_socket_recovered_but_live_listener_preserved(tunnel):
    save(tunnel.record, tunnel._identity())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tunnel.socket))
        listener.listen()
        with pytest.raises(RuntimeError, match="live unrecognized"):
            tunnel._remove_dead_socket()
        assert tunnel.socket.exists()
    tunnel._remove_dead_socket()
    assert not tunnel.socket.exists()


def test_unrecognized_socket_and_symlink_are_never_removed(tunnel):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tunnel.socket))
    with pytest.raises(RuntimeError, match="Unrecognized"):
        tunnel._remove_dead_socket()
    tunnel.socket.unlink()
    target = tunnel.root / "target"
    target.write_text("keep")
    tunnel.socket.symlink_to(target)
    with pytest.raises(RuntimeError, match="Unsafe"):
        tunnel._remove_dead_socket()
    assert target.read_text() == "keep"


def test_live_other_generation_cannot_be_reused_or_closed(tunnel):
    save(tunnel.record, {"generation": "other", "forward": list(tunnel.forward)})
    tunnel._control = lambda operation: True
    with pytest.raises(RuntimeError, match="different binding"):
        tunnel.start()
    with pytest.raises(RuntimeError, match="differently bound"):
        tunnel.close()
    # The tunnel's lock is a firm host lock, not VM state.
    assert (tunnel.root / "firm-locks" / "runtime-mcp").is_file()
    assert not (tunnel.root / "locks").exists()


def test_guest_port_does_not_accept_command_fragments(tunnel):
    vm = SimpleNamespace(
        root=tunnel.root, firm_locks=tunnel.locks, socket_root=tunnel.root,
        ssh_config=tunnel.ssh_config, commands=None, paths=TRUSTED,
        _state=lambda: {"generation": "g"},
    )
    for port in (True, 0, 65536, "8765:evil:22"):
        with pytest.raises(ValueError):
            runtime_tunnel(vm, port)


def test_forward_and_host_are_the_tunnel_identity(tmp_path):
    calls = []
    commands = SimpleNamespace(
        run=lambda argv, **options: calls.append(argv) or SimpleNamespace(returncode=1)
    )
    files = Tunnel(
        name="files-owner", host="lima-linux", forward=("-L", "127.0.0.1:4450:127.0.0.1:445"),
        ssh_config=tmp_path / "ssh", socket_root=tmp_path, root=tmp_path,
        locks=tmp_path / "locks", generation=lambda: "linux-g", commands=commands,
        verify=lambda: None,
    )
    assert files._identity() == {
        "generation": "linux-g", "forward": ["-L", "127.0.0.1:4450:127.0.0.1:445"]
    }
    assert files.socket == tmp_path / "files-owner.sock"
    save(files.record, files._identity())
    with pytest.raises(RuntimeError, match="unavailable"):
        files.require_ready()
    assert calls[-1][-3:] == ["-O", "check", "lima-linux"]


@pytest.mark.parametrize("operation", ["start", "close", "require_ready"])
def test_no_ssh_command_goes_through_a_runtime_the_vm_refuses(tmp_path, operation):
    """ssh.config and the control socket are in the VM's runtime, whoever's VM that holds."""
    calls = []

    def refuse():
        raise RuntimeError("Short VM runtime is owned by another firm")

    tunnel = Tunnel(
        name="files-owner", host="lima-linux", forward=("-L", "127.0.0.1:4450:127.0.0.1:445"),
        ssh_config=tmp_path / "ssh", socket_root=tmp_path, root=tmp_path,
        locks=tmp_path / "locks", generation=lambda: "linux-g",
        commands=SimpleNamespace(run=lambda argv, **options: calls.append(argv)),
        verify=refuse,
    )
    save(tunnel.record, tunnel._identity())
    with pytest.raises(RuntimeError, match="owned by another firm"):
        getattr(tunnel, operation)()
    assert calls == []


def test_lease_socket_leaves_room_for_openssh_temporary_suffix():
    # At least as long as a real runtime, /private/tmp/firm-<uid>/lima/<20-character digest>.
    runtime = tempfile.TemporaryDirectory(prefix="firm-501-lima-0123456789ab-", dir="/private/tmp")
    with runtime as root:
        def lease(identifier):
            return Tunnel(
                name="mac-lease-" + identifier, host="lima-linux", forward=(),
                ssh_config=Path(root) / "ssh", socket_root=root, root=root,
                locks=Path(root) / "locks", generation=lambda: "g", commands=None,
                verify=lambda: None,
            )

        first, second = lease("a" * 32), lease("b" * 32)
        assert first.socket != second.socket
        assert first.record.name == "mac-lease-" + "a" * 32 + ".json"
        temporary = str(first.socket) + ".1234567890123456"
        assert len(temporary.encode()) < 104
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(temporary)
