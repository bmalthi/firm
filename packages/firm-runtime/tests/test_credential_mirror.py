import io
import json
import urllib.error

import pytest

from firm_runtime.credential_mirror import CredentialMirror, MirrorUnavailable


def test_mirror_updates_atomically_and_retains_access_on_transport_failure(tmp_path, monkeypatch):
    from firm_runtime import credential_mirror

    path = tmp_path / "auth.json"
    value = {"kind": "codex-json", "data": {"tokens": {"access_token": "first"}}}

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer credential-token"
            return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(credential_mirror.urllib.request, "build_opener", lambda *_: Opener())
    mirror = CredentialMirror(path, url="http://firm.test/credentials", token="credential-token")
    mirror.refresh()
    assert json.loads(path.read_bytes())["tokens"]["access_token"] == "first"
    assert path.stat().st_mode & 0o777 == 0o600
    value["data"]["tokens"]["access_token"] = "second"
    mirror.refresh()
    assert json.loads(path.read_bytes())["tokens"]["access_token"] == "second"
    before = path.read_bytes()
    monkeypatch.setattr(
        Opener, "open", lambda *_: (_ for _ in ()).throw(OSError("private response"))
    )
    with pytest.raises(MirrorUnavailable):
        mirror.__enter__()
    assert path.read_bytes() == before


def test_mirror_auth_failure_emits_only_safe_code(tmp_path, monkeypatch, capsys):
    mirror = CredentialMirror(tmp_path / "auth", url="http://firm.test/credentials", token="secret")

    def fail():
        raise urllib.error.HTTPError("private-url", 403, "private response", {}, None)

    monkeypatch.setattr(mirror, "refresh", fail)
    with pytest.raises(MirrorUnavailable):
        mirror.__enter__()
    output = capsys.readouterr().out
    assert "authentication_failed" in output
    assert "private" not in output and "secret" not in output


@pytest.mark.parametrize("provider,document", [
    ("claude", "setup-token"),
    ("claude", {"claudeAiOauth": {"accessToken": "access", "refreshToken": "host-secret",
                                 "expiresAt": 4102444800000, "scopes": ["user:inference"]}}),
    ("codex", {"tokens": {"access_token": "access", "refresh_token": "host-secret",
                          "account_id": "account"}, "auth_mode": "chatgpt"}),
    ("codex", {"OPENAI_API_KEY": "api-key", "tokens": {"refresh_token": "host-secret"}}),
    ("agy", {"auth_method": "oauth-personal", "email": "host-identity", "token": {
        "access_token": "access", "refresh_token": "host-secret", "token_type": "Bearer",
        "expiry": "2100-01-01T00:00:00Z", "expires_in": 3600}}),
])
def test_real_backend_projection_delivered_by_copied_standalone_assets(
    tmp_path, provider, document
):
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from firm_runtime import agy_auth, credential_mirror
    from firm_runtime.credential_store import CredentialStore

    source = Path(credential_mirror.__file__).parent
    guest = tmp_path / "guest"
    guest.mkdir()
    for name in ("credential_mirror", "claude_launch", "codex_launch", "agy_launch"):
        shutil.copyfile(source / f"{name}.py", guest / f"{name}.py")
    auth = (tmp_path / "profile/home" / agy_auth.TOKEN_PATH if provider == "agy"
            else tmp_path / "host-auth")
    store = CredentialStore(provider, auth)
    raw = document.encode() if isinstance(document, str) else json.dumps(document).encode()
    store.save(raw)
    snapshot = store.snapshot(margin=None)
    assert set(snapshot) == {"kind", "data", "expires_at"}
    assert "host-secret" not in json.dumps(snapshot)
    assert "host-identity" not in json.dumps(snapshot)
    script = '''import importlib.abc, io, json, os, runpy, sys
from pathlib import Path
class RefuseHost(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "firm_runtime" or fullname.startswith("firm_runtime."):
            raise AssertionError("guest imported host package")
sys.meta_path.insert(0, RefuseHost())
root = Path(sys.argv[1])
for name in ("claude_launch", "codex_launch", "agy_launch"):
    runpy.run_path(str(root / (name + ".py")))
mirror = runpy.run_path(str(root / "credential_mirror.py"))
snapshot = json.load(sys.stdin)
class Opener:
    def open(self, request, timeout):
        assert request.get_header("Authorization") == "Bearer guest-token"
        return io.BytesIO(json.dumps(snapshot).encode())
mirror["urllib"].request.build_opener = lambda *_: Opener()
path = root / "auth"
mirror["CredentialMirror"](path, url="http://firm.test/credentials", token="guest-token").refresh()
if snapshot["kind"] == "oauth-token":
    print(json.dumps(os.environ["CLAUDE_CODE_OAUTH_TOKEN"]))
else:
    assert path.stat().st_mode & 0o777 == 0o600
    print(path.read_text())
assert not any(name == "firm_runtime" or name.startswith("firm_runtime.") for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(guest)],
                            input=json.dumps(snapshot), text=True, capture_output=True,
                            cwd=guest, check=True, timeout=10)
    assert json.loads(result.stdout) == snapshot["data"]
