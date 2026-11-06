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
