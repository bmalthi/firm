from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_release", Path(__file__).resolve().parents[1] / "verify_release.py"
)
verify_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_release)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root, dist = tmp_path / "repo", tmp_path / "dist"
    dist.mkdir()
    for name in ("runtime", "sandbox"):
        package = root / "packages" / f"firm-{name}"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
        for suffix in ("-py3-none-any.whl", ".tar.gz"):
            (dist / f"firm_{name}-0.1.0{suffix}").write_bytes(name.encode())
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.iterdir()}
    compatibility = root / "packages/firm-sandbox/src/firm_sandbox/compatibility.py"
    compatibility.parent.mkdir(parents=True)
    compatibility.write_text("COMPATIBILITY_GENERATION = 1\n")
    receipt = {
        "source_revision": "revision",
        "source_dirty": False,
        "release": "0.1.0",
        "compatibility_generation": 1,
        "artifact_sha256": hashes,
        "checks": dict.fromkeys(verify_release.REQUIRED_CHECKS, "passed"),
    }
    (dist / "release-evidence.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(
        verify_release.subprocess, "check_output",
        lambda argv, **kwargs: "" if argv[1] == "status" else "revision\n",
    )
    return root, dist


def test_exact_candidate_verifies(candidate):
    root, dist = candidate
    verify_release.verify(root, dist, "v0.1.0")


@pytest.mark.parametrize(
    "fault",
    [
        "tag",
        "hash",
        "revision",
        "dirty",
        "failed",
        "missing",
        "extra",
        "checks",
        "missing_offline_workflow",
        "generation",
    ],
)
def test_publication_refuses_changed_or_incomplete_candidate(candidate, fault):
    root, dist = candidate
    tag = "v0.1.0"
    receipt_path = dist / "release-evidence.json"
    receipt = json.loads(receipt_path.read_text())
    if fault == "tag":
        tag = "v0.2.0"
    elif fault == "hash":
        (dist / "firm_runtime-0.1.0-py3-none-any.whl").write_bytes(b"changed")
    elif fault == "revision":
        receipt["source_revision"] = "other"
    elif fault == "dirty":
        receipt["source_dirty"] = True
    elif fault == "failed":
        receipt["checks"]["isolated_install"] = "failed"
    elif fault == "missing":
        (dist / "firm_sandbox-0.1.0.tar.gz").unlink()
    elif fault == "extra":
        (dist / "firm_sandbox-0.0.1.tar.gz").write_bytes(b"stale")
    elif fault == "checks":
        del receipt["checks"]["isolated_install"]
    elif fault == "missing_offline_workflow":
        del receipt["checks"]["offline_workflow"]
    elif fault == "generation":
        receipt["compatibility_generation"] = 2
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        verify_release.verify(root, dist, tag)


@pytest.mark.parametrize("fault", ["tag-target", "missing-tag", "dirty-checkout"])
def test_actual_git_tag_and_worktree_are_verified(candidate, monkeypatch, fault):
    root, dist = candidate

    def output(argv, **kwargs):
        if "--verify" in argv:
            assert argv[-1] == "refs/tags/v0.1.0^{commit}"
            if fault == "missing-tag":
                raise verify_release.subprocess.CalledProcessError(128, argv)
            return "other\n" if fault == "tag-target" else "revision\n"
        if argv[1] == "status":
            return " M source.py\n" if fault == "dirty-checkout" else ""
        return "revision\n"

    monkeypatch.setattr(verify_release.subprocess, "check_output", output)
    with pytest.raises((ValueError, verify_release.subprocess.CalledProcessError)):
        verify_release.verify(root, dist, "v0.1.0")
