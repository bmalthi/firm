#!/usr/bin/env python3
"""Verify a tagged release's recorded source and exact distribution hashes before publishing."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

REQUIRED_CHECKS = {
    "package_version_equality",
    "runtime_sandbox_pin",
    "wheel_contents",
    "sdist_contents",
    "sdist_rebuild",
    "isolated_install",
    "cli_help",
    "packaged_assets",
    "offline_workflow",
}


def expected_generation(root: Path) -> int:
    path = root / "packages/firm-sandbox/src/firm_sandbox/compatibility.py"
    tree = ast.parse(path.read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "COMPATIBILITY_GENERATION"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if type(value) is int and value > 0:
                return value
    raise ValueError("The source compatibility generation is missing or invalid")


def verify(root: Path, dist: Path, tag: str) -> None:
    projects = [
        tomllib.loads((root / "packages" / package / "pyproject.toml").read_text())["project"]
        for package in ("firm-runtime", "firm-sandbox")
    ]
    version = projects[0]["version"]
    if projects[1]["version"] != version or tag != f"v{version}":
        raise ValueError("Tag must match the single release version of both packages")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tagged = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], cwd=root, text=True
    ).strip()
    if tagged != revision:
        raise ValueError("The release tag must point to the current source revision")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise ValueError("Release verification requires a clean checkout")
    release = json.loads((dist / "release-evidence.json").read_text())
    expected = {
        f"firm_{package}-{version}{suffix}"
        for package in ("runtime", "sandbox")
        for suffix in ("-py3-none-any.whl", ".tar.gz")
    }
    actual = {path.name for pattern in ("*.whl", "*.tar.gz") for path in dist.glob(pattern)}
    if actual != expected:
        raise ValueError("The candidate must contain exactly this release's two wheels and sdists")
    hashes = {name: hashlib.sha256((dist / name).read_bytes()).hexdigest() for name in expected}
    if release.get("source_revision") != revision:
        raise ValueError("The release evidence was produced for a different source revision")
    if release.get("artifact_sha256") != hashes:
        raise ValueError("The release artifact hashes differ from the files to publish")
    checks = release.get("checks")
    if not isinstance(checks, dict) or not checks or set(checks.values()) != {"passed"}:
        raise ValueError("The release evidence has missing or unsuccessful checks")
    if release.get("release") != version:
        raise ValueError("The release evidence names a different release")
    if release.get("source_dirty") is not False:
        raise ValueError("Publishing requires evidence built from a clean checkout")
    if not REQUIRED_CHECKS <= release["checks"].keys():
        raise ValueError("The release evidence omits required checks")
    if release.get("compatibility_generation") != expected_generation(root):
        raise ValueError("The release evidence names a different compatibility generation")
    print(f"Verified {tag}: {revision}, {len(hashes)} matching artifacts and passing evidence")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        verify(Path(__file__).resolve().parents[1], args.dist, args.tag)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release verification failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
