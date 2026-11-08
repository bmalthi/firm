"""Offline native-profile tests. All credential strings are synthetic sentinels."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firm_runtime import agy_auth, onboarding
from firm_runtime.credential_store import CredentialStore, guest_credentials
from firm_runtime.provider_contract import ProviderSetupError


def bundle(expiry=10000):
    return {
        "auth_method": "oauth-personal",
        "email": "removed",
        "unknown": "discard",
        "token": {
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-host-refresh",
            "token_type": "Bearer",
            "expiry": datetime.fromtimestamp(expiry, UTC).isoformat(),
            "expires_in": 3600,
            "unknown": "discard",
        },
    }


def account(tmp_path, expiry=10000):
    path = tmp_path / "profile/home" / agy_auth.TOKEN_PATH
    CredentialStore("agy", path).save(json.dumps(bundle(expiry)).encode())
    return path


def fake_agy(tmp_path, body):
    executable = tmp_path / "agy"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("arguments", [
    ["models"], ["acp", "--login", "--include-default-customizations=false"],
])
def test_host_argv_blocks_keychain_on_macos(monkeypatch, arguments):
    monkeypatch.setattr(agy_auth.sys, "platform", "darwin")
    checks = []
    def available(path, mode):
        checks.append((path, mode))
        return True
    monkeypatch.setattr(agy_auth.os, "access", available)
    monkeypatch.setattr(agy_auth, "native_command", lambda: "/native/agy")
    assert agy_auth.host_argv(arguments) == [
        "/usr/bin/sandbox-exec", "-p",
        '(version 1)(allow default)(deny process-exec (literal "/usr/bin/security"))',
        "/native/agy", *arguments,
    ]
    assert checks == [("/usr/bin/sandbox-exec", os.X_OK)]


def test_host_argv_non_macos(monkeypatch):
    monkeypatch.setattr(agy_auth.sys, "platform", "linux")
    monkeypatch.setattr(agy_auth.os, "access", lambda *_: pytest.fail("macOS binary check"))
    assert agy_auth.host_argv(["models"], executable="/native/agy") == ["/native/agy", "models"]


@pytest.mark.parametrize("launch", ["argv", "renew", "login", "snapshot"])
def test_missing_host_sandbox_fails_clearly_without_launch(tmp_path, monkeypatch, launch):
    monkeypatch.setattr(agy_auth.sys, "platform", "darwin")
    monkeypatch.setattr(agy_auth.os, "access", lambda *_: False)
    monkeypatch.setattr(agy_auth, "native_command", lambda: "/native/agy")
    monkeypatch.setattr(agy_auth.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    path = tmp_path / "profile/home" / agy_auth.TOKEN_PATH
    with pytest.raises(ProviderSetupError, match="require /usr/bin/sandbox-exec") as error:
        if launch == "argv":
            agy_auth.host_argv(["models"])
        elif launch == "renew":
            agy_auth.renew(path)
        elif launch == "snapshot":
            path = account(tmp_path, 1100)
            CredentialStore("agy", path, clock=lambda: 1000).snapshot()
        else:
            onboarding.login_provider("agy", "example", target_credentials_path=path)
    assert error.value.code == "credential_sandbox_missing"


def test_access_only_native_projection_and_fresh_snapshot(tmp_path):
    path = account(tmp_path)
    before = path.read_bytes()
    result = CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert result["kind"] == "agy-access"
    assert result["expires_at"] == 10000
    assert set(result["data"]) == {"auth_method", "token"}
    assert set(result["data"]["token"]) == {
        "access_token",
        "token_type",
        "expiry",
        "expires_in",
        "refresh_token",
    }
    assert result["data"]["token"]["refresh_token"] == ""
    assert "synthetic-host-refresh" not in json.dumps(result)
    assert "removed" not in json.dumps(result) and "discard" not in json.dumps(result)
    assert json.loads(path.read_bytes()) == json.loads(before)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_token", ""),
        ("token_type", "Basic"),
        ("expiry", "2026-01-01"),
        ("expiry", "invalid"),
        ("expires_in", True),
        ("expires_in", -1),
    ],
)
def test_reject_invalid_native_shape(field, value):
    document = bundle()
    document["token"][field] = value
    with pytest.raises(ProviderSetupError):
        guest_credentials("agy", json.dumps(document))


def test_native_renewal_is_serialized_clean_and_never_calls_oauth(tmp_path, monkeypatch):
    path = account(tmp_path, 3700)
    executable = fake_agy(
        tmp_path,
        """import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['models']
assert 'GEMINI_API_KEY' not in os.environ
assert 'NODE_OPTIONS' not in os.environ
assert os.environ['AGY_CLI_DISABLE_AUTO_UPDATE'] == 'true'
home = Path(os.environ['HOME'])
assert Path.cwd() == home
path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
value = json.loads(path.read_text())
assert value['token']['expiry'] == '2000-01-01T00:00:00Z'
assert value['token']['expires_in'] == 3600
assert value['token']['unknown'] == 'discard'
assert value['unknown'] == 'discard'
value['token']['access_token'] = 'synthetic-renewed'
value['token']['expiry'] = '2100-01-01T00:00:00Z'
path.write_text(json.dumps(value))
with (home / 'renewal-count').open('a') as output:
    output.write('one\\n')
""",
    )
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(executable))
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("NODE_OPTIONS", "must-not-inherit")
    store = CredentialStore(
        "agy", path, clock=lambda: 1000, refresh=lambda *_: pytest.fail("custom OAuth called")
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.snapshot(), range(4)))
    assert (path.parents[2] / "renewal-count").read_text() == "one\n"
    assert all(item["data"]["token"]["access_token"] == "synthetic-renewed" for item in results)
    assert all(item["data"]["token"]["refresh_token"] == "" for item in results)
    assert json.loads(path.read_bytes())["token"]["refresh_token"] == "synthetic-host-refresh"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "body",
    [
        "import sys\nprint('synthetic-sensitive-output')\nsys.exit(7)\n",
        "import time\ntime.sleep(10)\n",
    ],
)
def test_native_renewal_failures_are_bounded_and_safe(tmp_path, monkeypatch, capsys, body):
    path = account(tmp_path, 1100)
    before = path.read_bytes()
    executable = fake_agy(tmp_path, body)
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(executable))
    monkeypatch.setattr(agy_auth, "RENEWAL_TIMEOUT", 0.1)
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert error.value.code == "credential_refresh_unavailable"
    assert "synthetic-sensitive-output" not in str(error.value)
    assert json.loads(path.read_bytes()) == json.loads(before)
    assert capsys.readouterr().out == ""


def test_profile_links_and_normal_home_layout_are_rejected(tmp_path):
    with pytest.raises(ProviderSetupError):
        agy_auth.profile_home(tmp_path / "ordinary-user" / agy_auth.TOKEN_PATH)
    path = account(tmp_path)
    native_home = path.parents[2]
    native_home.rename(native_home.with_name("real-home"))
    native_home.symlink_to(native_home.with_name("real-home"), target_is_directory=True)
    with pytest.raises(ProviderSetupError):
        CredentialStore("agy", path).snapshot()


def test_isolated_native_login_retains_profile_and_overages_disabled(tmp_path, monkeypatch):
    path = tmp_path / "profile/home" / agy_auth.TOKEN_PATH
    executable = fake_agy(
        tmp_path,
        """import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['acp', '--login', '--include-default-customizations=false']
assert 'GEMINI_API_KEY' not in os.environ
home = Path(os.environ['HOME'])
settings = home / '.gemini/antigravity-cli/settings.json'
assert json.loads(settings.read_text()) == {'useG1Credits': False}
config = json.loads((home / '.gemini/config/config.json').read_text())
assert config == {'userSettings': {'useAiCredits': False}}
path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
"""
        + f"path.write_text({json.dumps(json.dumps(bundle()))})\npath.chmod(0o600)\n",
    )
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(executable))
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-inherit")
    assert onboarding.login_provider("agy", "example", target_credentials_path=path) == path
    assert path.exists()
    assert json.loads(path.read_bytes())["token"]["refresh_token"] == "synthetic-host-refresh"
    assert (path.parents[2] / ".gemini/config/config.json").exists()


def test_environment_contains_only_explicit_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    assert set(agy_auth.environment(tmp_path)) == {
        "HOME",
        "PATH",
        "LANG",
        "USER",
        "LOGNAME",
        "AGY_CLI_DISABLE_AUTO_UPDATE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
    }
    assert os.getuid() == Path(tmp_path).stat().st_uid


@pytest.mark.parametrize(
    "path",
    ["/", "/missing", "/missing/profile/home/.gemini/antigravity-cli/antigravity-oauth-token"],
)
def test_bad_profile_paths_raise_safe_setup_errors(path):
    with pytest.raises(ProviderSetupError):
        CredentialStore("agy", path).snapshot()


def test_guest_mirror_delivers_only_native_access_file(tmp_path, monkeypatch):
    import io

    from firm_runtime import credential_mirror

    snapshot = guest_credentials("agy", json.dumps(bundle()))

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer synthetic-admission"
            return io.BytesIO(json.dumps(snapshot).encode())

    monkeypatch.setattr(credential_mirror.urllib.request, "build_opener", lambda *_: Opener())
    path = tmp_path / "native-token"
    mirror = credential_mirror.CredentialMirror(
        path,
        url="http://synthetic/credentials",
        token="synthetic-admission",
    )
    mirror.refresh()
    assert json.loads(path.read_bytes()) == snapshot["data"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert mirror.thread is None
    assert "synthetic-host-refresh" not in path.read_text()
    snapshot["data"]["token"]["refresh_token"] = "must-reject"
    with pytest.raises(ValueError, match="AGY access projection"):
        mirror.refresh()


@pytest.mark.parametrize("expired", [False, True])
def test_native_renewal_failure_is_auth_only_after_expiry(tmp_path, monkeypatch, expired):
    path = account(tmp_path, 900 if expired else 1100)
    def unavailable(*_):
        raise agy_auth.failure("credential_refresh_unavailable", transient=True)
    monkeypatch.setattr(agy_auth, "renew", unavailable)
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert error.value.category == ("auth" if expired else "transient")


def test_fresh_snapshot_and_build_do_not_take_renewal_lock(tmp_path, monkeypatch):
    from firm_runtime.agy_provider import AgyProvider
    path = account(tmp_path, 4102444800)
    monkeypatch.setattr(CredentialStore, "_lock", lambda *_: pytest.fail("lock acquired"))
    assert CredentialStore("agy", path).snapshot()["kind"] == "agy-access"
    adapter = AgyProvider(model="gemini-3.8-flash", effort="medium", auth_file=path)
    assert json.loads(adapter._access_snapshot())["token"]["refresh_token"] == ""


def test_native_directories_are_tightened_and_billing_restored(tmp_path, monkeypatch):
    path = account(tmp_path, 1100)
    home = path.parents[2]
    (home / ".gemini").chmod(0o755)
    settings = home / ".gemini/antigravity-cli/settings.json"
    settings.write_text('{"useG1Credits":false,"nativeSetting":"preserve"}')
    executable = fake_agy(tmp_path, """import json, os
from pathlib import Path
home = Path(os.environ['HOME'])
settings = home / '.gemini/antigravity-cli/settings.json'
assert json.loads(settings.read_text())['useG1Credits'] is False
assert os.umask(0o077) == 0o077
settings.write_text('{"nativeSetting":"preserve"}')
path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
data = json.loads(path.read_text())
data['token']['expiry'] = '2100-01-01T00:00:00Z'
path.write_text(json.dumps(data))
""")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(executable))
    CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert (home / ".gemini").stat().st_mode & 0o777 == 0o700
    assert json.loads(settings.read_text()) == {"useG1Credits": False, "nativeSetting": "preserve"}


def test_failed_login_names_exit_and_retained_state_without_lock(tmp_path, monkeypatch):
    executable = fake_agy(tmp_path, "import sys\nsys.exit(7)\n")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(executable))
    monkeypatch.setattr(CredentialStore, "_lock", lambda *_: pytest.fail("interactive lock"))
    from firm_runtime.errors import FirmError
    path = tmp_path / "profile/home" / agy_auth.TOKEN_PATH
    with pytest.raises(FirmError, match="status 7; profile retained at") as error:
        onboarding.login_provider("agy", "example", target_credentials_path=path)
    assert str(tmp_path) in str(error.value)


@pytest.mark.parametrize("remaining", [301, 1800, 2999])
def test_failed_headroom_renewal_restores_and_stages_still_valid_token(
    tmp_path, monkeypatch, remaining
):
    path = account(tmp_path, 1000 + remaining)
    before = json.loads(path.read_bytes())
    native = fake_agy(tmp_path, "import sys\nsys.exit(7)\n")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(native))
    result = CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert result == guest_credentials("agy", json.dumps(before))
    assert json.loads(path.read_bytes()) == before
    assert path.stat().st_mode & 0o777 == 0o600


def test_fifty_minute_boundary_skips_forcing(tmp_path, monkeypatch):
    path = account(tmp_path, 4000)
    monkeypatch.setattr(agy_auth, "force_renew", lambda *a, **k: pytest.fail("renewal"))
    assert agy_auth.RENEWAL_MARGIN == 50 * 60
    assert CredentialStore("agy", path, clock=lambda: 1000).snapshot()["expires_at"] == 4000


def test_failed_native_process_does_not_restore_over_a_rotated_token(tmp_path, monkeypatch):
    path = account(tmp_path, 2800)
    native = fake_agy(tmp_path, """import json, os, sys
from pathlib import Path
path = Path(os.environ['HOME']) / '.gemini/antigravity-cli/antigravity-oauth-token'
data = json.loads(path.read_text())
data['token']['access_token'] = 'synthetic-rotated'
data['token']['expiry'] = '2100-01-01T00:00:00Z'
path.write_text(json.dumps(data))
sys.exit(7)
""")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(native))
    result = CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert result["data"]["token"]["access_token"] == "synthetic-rotated"
    assert json.loads(path.read_bytes())["token"]["access_token"] == "synthetic-rotated"


@pytest.mark.parametrize("build", [False, True])
def test_native_midwrite_is_retried_under_lock(tmp_path, monkeypatch, build):
    from contextlib import contextmanager

    from firm_runtime.agy_provider import AgyProvider

    path = account(tmp_path, 4102444800)
    original = agy_auth.read_private_credential
    locked, reads = [], []
    @contextmanager
    def lock(_store):
        locked.append(True)
        yield
    def read(target, provider):
        reads.append(bool(locked))
        return b"{" if len(reads) == 1 else original(target, provider)
    monkeypatch.setattr(CredentialStore, "_lock", lock)
    monkeypatch.setattr(agy_auth, "read_private_credential", read)
    if build:
        result = AgyProvider(model="gemini-3.8-flash", effort="medium",
                             auth_file=path)._access_snapshot()
        assert json.loads(result)["token"]["refresh_token"] == ""
    else:
        assert CredentialStore("agy", path).snapshot()["kind"] == "agy-access"
    assert reads == [False, True]


@pytest.mark.parametrize("raises", [False, True])
def test_post_renewal_malformed_json_is_transient(tmp_path, monkeypatch, raises):
    path = account(tmp_path, 2000)
    def renew(*_, **kwargs):
        path.write_bytes(b"{")
        if raises:
            raise agy_auth.failure("credential_refresh_unavailable", transient=True)
        return bundle(10000)
    monkeypatch.setattr(agy_auth, "force_renew", renew)
    with pytest.raises(ProviderSetupError) as error:
        CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert error.value.category == "transient"
    assert error.value.code == "credential_read_unavailable"


def test_force_renew_crash_restores_private_backup_on_next_locked_pass(tmp_path, monkeypatch):
    import subprocess

    path = account(tmp_path, 2000)
    original = json.loads(path.read_bytes())
    script = """import os, sys
from firm_runtime import agy_auth
from firm_runtime.credential_store import CredentialStore
store = CredentialStore('agy', sys.argv[1])
agy_auth.renew = lambda *_: os._exit(9)
with store._lock():
    agy_auth.force_renew(store.path, replace=store._replace)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=10)
    assert result.returncode == 9
    backup = agy_auth._expiry_backup(path)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(backup.read_bytes())) == {"expiry", "access_token"}
    assert json.loads(path.read_bytes())["token"]["expiry"] == agy_auth.FORCED_EXPIRY
    def unavailable(*_):
        raise agy_auth.failure("credential_refresh_unavailable", transient=True)
    monkeypatch.setattr(agy_auth, "renew", unavailable)
    snapshot = CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert snapshot["expires_at"] == 2000
    assert json.loads(path.read_bytes()) == original
    assert not backup.exists()


def test_native_wait_kills_helpers_before_reaping_leader(monkeypatch):
    from types import SimpleNamespace

    actions = []
    process = SimpleNamespace(pid=12345, returncode=None)
    def waitid(kind, pid, flags):
        assert kind == os.P_PID and pid == process.pid
        assert flags & os.WNOWAIT and flags & os.WEXITED
        actions.append("observe")
        return object()
    def kill(pid, _signal):
        assert process.returncode is None and pid == process.pid
        actions.append("kill-group")
    def reap():
        actions.append("reap")
        process.returncode = 0
        return 0
    process.wait = reap
    monkeypatch.setattr(agy_auth.os, "waitid", waitid)
    monkeypatch.setattr(agy_auth.os, "killpg", kill)
    assert agy_auth._wait_native(process) == 0
    assert actions == ["observe", "kill-group", "reap"]


@pytest.mark.parametrize("platform,states,accepted", [
    ("darwin", "Z\nZ+\n", True),
    ("darwin", "Z\nS\n", False),
    ("darwin", "", False),
    ("linux", None, False),
])
def test_native_wait_accepts_only_confirmed_darwin_zombie_group(
    monkeypatch, platform, states, accepted
):
    from types import SimpleNamespace

    process = SimpleNamespace(pid=12345, returncode=None)
    monkeypatch.setattr(agy_auth.sys, "platform", platform)
    monkeypatch.setattr(agy_auth.os, "waitid", lambda *_: object())
    def denied(*_):
        raise PermissionError
    def inspect(command, **kwargs):
        assert states is not None
        assert process.returncode is None
        assert command == ["/bin/ps", "-g", "12345", "-o", "stat="]
        assert kwargs["timeout"] == 5
        return states
    def reap():
        process.returncode = 0
    process.wait = reap
    monkeypatch.setattr(agy_auth.os, "killpg", denied)
    monkeypatch.setattr(agy_auth.subprocess, "check_output", inspect)
    if accepted:
        assert agy_auth._wait_native(process) == 0
    else:
        with pytest.raises(PermissionError):
            agy_auth._wait_native(process)
        assert process.returncode is None


def test_native_login_keeps_full_profile_for_renewal(tmp_path, monkeypatch):
    path = tmp_path / "profile/home" / agy_auth.TOKEN_PATH
    native = fake_agy(tmp_path, """import json, os, sys
from pathlib import Path
home = Path(os.environ['HOME'])
state = home / 'native-login-state'
path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
if sys.argv[1] == 'acp':
    state.write_text('synthetic-native-state')
    path.write_text(""" + repr(json.dumps(bundle(2000))) + """)
else:
    assert state.read_text() == 'synthetic-native-state'
    data = json.loads(path.read_text())
    data['token']['expiry'] = '2100-01-01T00:00:00Z'
    path.write_text(json.dumps(data))
""")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(native))
    onboarding.login_provider("agy", "test", target_credentials_path=path)
    assert (path.parents[2] / "native-login-state").exists()
    assert not list(path.parents[3].glob(".agy-login-*"))
    assert CredentialStore("agy", path, clock=lambda: 1000).snapshot()["expires_at"] > 10000


def test_successful_native_renewal_leaves_no_helper_effects(tmp_path, monkeypatch):
    import time

    path = account(tmp_path, 2000)
    native = fake_agy(tmp_path, """import json, os, time
from pathlib import Path
home = Path(os.environ['HOME'])
path = home / '.gemini/antigravity-cli/antigravity-oauth-token'
data = json.loads(path.read_text())
data['token']['expiry'] = '2100-01-01T00:00:00Z'
path.write_text(json.dumps(data))
if os.fork() == 0:
    (home / 'helper-ready').touch()
    time.sleep(0.3)
    (home / 'late-helper-effect').touch()
    os._exit(0)
while not (home / 'helper-ready').exists():
    time.sleep(0.01)
os._exit(0)
""")
    monkeypatch.setattr(agy_auth, "native_command", lambda: str(native))
    CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert (path.parents[2] / "helper-ready").exists()
    time.sleep(0.4)
    assert not (path.parents[2] / "late-helper-effect").exists()


def test_expiry_backup_io_failure_falls_back_without_config_hold(tmp_path, monkeypatch):
    path = account(tmp_path, 2000)
    def unavailable(*_, **kwargs):
        raise OSError("synthetic storage outage")
    monkeypatch.setattr(agy_auth, "force_renew", unavailable)
    result = CredentialStore("agy", path, clock=lambda: 1000).snapshot()
    assert result["expires_at"] == 2000
