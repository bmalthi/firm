"""Evidence reuse must fail before sandbox construction."""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["execution", "docker", "browser"])
@pytest.mark.parametrize("populated", [False, True])
def test_existing_evidence_refused_before_sandbox(tmp_path, monkeypatch, name, populated):
    path = Path(__file__).resolve().parents[1] / "acceptance" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"output_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    if populated:
        (evidence / "result.json").write_text("previous result")
    def forbidden(*args, **kwargs):
        pytest.fail("sandbox constructed before evidence refusal")
    monkeypatch.setattr(module, "Sandbox", forbidden)
    monkeypatch.setattr(module.Config, "load", forbidden)
    argv = [str(path), "--root", str(tmp_path / "firm"), "--evidence", str(evidence)]
    if name == "execution":
        argv.append("--replace")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(FileExistsError):
        module.main()
    if populated:
        assert (evidence / "result.json").read_text() == "previous result"
    else:
        assert not list(evidence.iterdir())


@pytest.mark.parametrize("invalid", [False, True])
def test_agy_acceptance_validates_dedicated_profile(tmp_path, invalid, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "acceptance"))
    from provider_checks.acceptance_common import selected_profile

    from firm_runtime.agy_auth import prepare_profile

    auth = tmp_path / "profile/home/.gemini/antigravity-cli/antigravity-oauth-token"
    prepare_profile(auth)
    if invalid:
        auth = tmp_path / "ordinary/token"
    source = {"profiles": {"agy": {"provider": "agy", "accounts": ["test"]}},
              "accounts": {"test": {"provider": "agy", "auth_file": str(auth)}}}
    if invalid:
        with pytest.raises(ValueError, match="dedicated profile"):
            selected_profile(source, "agy")
    else:
        _, accounts = selected_profile(source, "agy")
        assert accounts["test"]["auth_file"] == str(auth)
        assert not auth.exists()
