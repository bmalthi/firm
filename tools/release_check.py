#!/usr/bin/env python3
"""Build and smoke-test the two release distributions outside the checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("firm-runtime", "firm-sandbox")


def packaged_assets() -> dict[str, tuple[str, ...]]:
    runtime = ROOT / "packages/firm-runtime/src/firm_runtime"
    sandbox = ROOT / "packages/firm-sandbox/src/firm_sandbox"
    return {
        "firm-runtime": tuple(
            path.relative_to(runtime.parent).as_posix()
            for folder in (runtime / "prompts", runtime / "web_console", runtime / "resources")
            for path in sorted(folder.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ),
        "firm-sandbox": tuple(
            path.relative_to(sandbox.parent).as_posix()
            for path in sorted((sandbox / "assets").rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ),
    }


ASSETS = packaged_assets()


def package_source_files(name: str, *, root: Path | None = None) -> dict[str, bytes]:
    root = ROOT if root is None else root
    module = name.replace("-", "_")
    source = root / "packages" / name / "src" / module
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def verify_source_files(
    artifact: Path, expected: dict[str, bytes], actual: dict[str, bytes]
) -> None:
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    stale = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )
    differences = []
    if missing:
        differences.append(f"missing: {', '.join(missing)}")
    if extra:
        differences.append(f"unexpected: {', '.join(extra)}")
    if stale:
        differences.append(f"stale: {', '.join(stale)}")
    if differences:
        raise RuntimeError(
            f"{artifact} package source differs from the current checkout "
            f"({'; '.join(differences)})"
        )


def verify_project_files(path: Path, archive_files: dict[str, bytes], project: Path) -> None:
    for filename in ("README.md", "pyproject.toml"):
        actual = archive_files.get(filename)
        expected = (project / filename).read_bytes()
        if actual != expected:
            raise RuntimeError(f"{path} has stale or missing {filename}")


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({result.returncode}):\n{result.stdout}")
    return result.stdout


def metadata_from_wheel(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode())
        return metadata, archive.read(metadata_name), names


def verify_wheel(path: Path, name: str, expected_version: str) -> dict:
    metadata, raw_metadata, members = metadata_from_wheel(path)
    if any("__pycache__" in member.split("/") or member.endswith(".pyc") for member in members):
        raise RuntimeError(f"{path} contains generated Python bytecode")
    if metadata.get("Name", "").lower().replace("_", "-") != name:
        raise RuntimeError(f"{path} has unexpected package name {metadata.get('Name')!r}")
    if metadata.get("Version") != expected_version:
        raise RuntimeError(
            f"{path} has version {metadata.get('Version')}, expected {expected_version}"
        )
    if metadata.get("License-Expression") != "Apache-2.0":
        raise RuntimeError(f"{path} is missing the Apache-2.0 SPDX license expression")
    if metadata.get("Requires-Python") != ">=3.12":
        raise RuntimeError(f"{path} has unexpected Python compatibility metadata")
    project_urls = metadata.get_all("Project-URL") or []
    if not any(
        value.startswith("Source, https://github.com/bmalthi/firm") for value in project_urls
    ):
        raise RuntimeError(f"{path} is missing the canonical repository URL")
    if not {"LICENSE", "NOTICE"} <= {
        Path(member).name for member in members if ".dist-info/licenses/" in member
    }:
        raise RuntimeError(f"{path} is missing package license/NOTICE files")
    with zipfile.ZipFile(path) as archive:
        for filename in ("LICENSE", "NOTICE"):
            member = next(
                item
                for item in members
                if ".dist-info/licenses/" in item and Path(item).name == filename
            )
            if archive.read(member) != (ROOT / filename).read_bytes():
                raise RuntimeError(f"{path} has a stale or different {filename}")
    module = name.replace("-", "_")
    prefix = f"{module}/"
    with zipfile.ZipFile(path) as archive:
        package_files = {
            member.removeprefix(prefix): archive.read(member)
            for member in members
            if member.startswith(prefix) and not member.endswith("/")
        }
    verify_source_files(path, package_source_files(name), package_files)
    if name == "firm-runtime":
        dependencies = metadata.get_all("Requires-Dist") or []
        if not any(
            dependency.replace(" ", "")
            in {
                f"firm-sandbox=={expected_version}",
                f"firm-sandbox(=={expected_version})",
            }
            for dependency in dependencies
        ):
            raise RuntimeError(
                f"Runtime does not select its matching sandbox build: {dependencies}"
            )
    try:
        display_path = str(path.relative_to(ROOT))
    except ValueError:
        display_path = str(path)
    return {
        "file": display_path,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(raw_metadata).hexdigest(),
        "version": expected_version,
        "members": sorted(members),
    }


def verify_sdist(path: Path, name: str, expected_version: str, *, rebuild: bool) -> dict:
    with tarfile.open(path, "r:gz") as archive:
        members = {member.name.split("/", 1)[-1] for member in archive.getmembers()}
        source_assets = {f"src/{path}" for path in ASSETS[name]}
        required = {"LICENSE", "NOTICE", "README.md", "pyproject.toml"} | source_assets
        if not required <= members:
            missing = sorted(required - members)
            raise RuntimeError(f"{path} is missing packaged files: {', '.join(missing)}")
        for filename in ("LICENSE", "NOTICE"):
            member = next(name for name in archive.getnames() if name.split("/")[-1] == filename)
            if archive.extractfile(member).read() != (ROOT / filename).read_bytes():
                raise RuntimeError(f"{path} has a stale or different {filename}")
        package = ROOT / "packages" / name
        project_files = {
            member.name.split("/", 1)[-1]: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }
        verify_project_files(path, project_files, package)
        module = name.replace("-", "_")
        source_prefix = f"src/{module}/"
        source_files = {
            relative.removeprefix(source_prefix): archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
            and (relative := member.name.split("/", 1)[-1]).startswith(source_prefix)
        }
        verify_source_files(path, package_source_files(name), source_files)
        if name == "firm-sandbox" and "README.md" not in members:
            raise RuntimeError(f"{path} is missing the sandbox README")
        report = {
            "file": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "version": expected_version,
            "members": sorted(member for member in members if member),
        }
        if rebuild:
            with tempfile.TemporaryDirectory(prefix="firm-sdist-build-") as temporary:
                extracted = Path(temporary) / "source"
                archive.extractall(extracted, filter="data")
                project = next(extracted.iterdir())
                output = Path(temporary) / "wheel"
                output.mkdir()
                run(["uv", "build", "--wheel", "--out-dir", str(output)], cwd=project)
                wheels = list(output.glob("*.whl"))
                if len(wheels) != 1:
                    raise RuntimeError(f"Building {path} produced {len(wheels)} wheels")
                rebuilt = verify_wheel(wheels[0], name, expected_version)
                report["rebuilt_wheel_sha256"] = rebuilt["sha256"]
    return report


def smoke_install(wheels: list[Path], versions: dict[str, str], temporary: Path) -> dict:
    wheels = [(path if path.is_absolute() else ROOT / path).resolve() for path in map(Path, wheels)]
    missing = [path for path in wheels if not path.is_file()]
    if missing:
        raise RuntimeError(f"Isolated install wheel does not exist: {missing}")
    env_root = temporary / "venv"
    python = sys.executable
    run(["uv", "venv", "--python", python, str(env_root)], cwd=ROOT)
    python_path = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    scripts = env_root / ("Scripts" if os.name == "nt" else "bin")
    firm = scripts / ("firm.exe" if os.name == "nt" else "firm")
    firm_sandbox = scripts / ("firm-sandbox.exe" if os.name == "nt" else "firm-sandbox")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "UV_",
                "PYTHON",
                "OPENAI_",
                "CODEX_",
                "ANTHROPIC_",
                "CLAUDE_",
                "FIRM_GITHUB_",
            )
        )
        and key
        not in {
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "FIRM_CREDENTIALS_DIR",
            "FIRM_PRIVATE_HOME",
        }
    }
    env["PYTHONPATH"] = ""
    env["UV_NO_PROJECT"] = "1"
    home = temporary / "home"
    for name, directory in (
        ("HOME", home),
        ("XDG_CONFIG_HOME", home / ".config"),
        ("XDG_DATA_HOME", home / ".local" / "share"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    run(
        ["uv", "pip", "install", "--python", str(python_path), *(str(p) for p in wheels)],
        cwd=temporary,
        env=env,
    )
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(python_path),
            str(firm),
            "--help",
        ],
        cwd=temporary,
        env=env,
    )
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(python_path),
            str(firm_sandbox),
            "--help",
        ],
        cwd=temporary,
        env=env,
    )
    resource_checks = []
    for package, paths in (
        ("firm_runtime", ASSETS["firm-runtime"]),
        ("firm_sandbox", ASSETS["firm-sandbox"]),
    ):
        resource_checks.extend(
            f"assert resources.files({package!r}).joinpath("
            f"{path.removeprefix(package + '/')!r}).is_file()"
            for path in paths
        )
    code = "\n".join(
        [
            "import importlib.util",
            "from importlib import metadata, resources",
            "from pathlib import Path",
            "import sys",
            f"prefix = Path({str(env_root.resolve())!r})",
            "assert Path(sys.prefix).resolve() == prefix",
            f"assert metadata.version('firm-runtime') == {versions['firm-runtime']!r}",
            f"assert metadata.version('firm-sandbox') == {versions['firm-sandbox']!r}",
            "assert Path(importlib.util.find_spec('firm_runtime').origin).resolve()"
            ".is_relative_to(prefix)",
            "assert Path(importlib.util.find_spec('firm_sandbox').origin).resolve()"
            ".is_relative_to(prefix)",
            *resource_checks,
        ]
    )
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(python_path),
            str(python_path),
            "-I",
            "-c",
            code,
        ],
        cwd=temporary,
        env=env,
    )

    # Build a provider-free firm using the installed starter resource, then exercise
    # the installed product commands against it from outside the source checkout.
    firm_directory = temporary / "2026-10-28_release-smoke"
    prepare = "\n".join(
        [
            "from importlib import resources",
            "from pathlib import Path",
            f"directory = Path({str(firm_directory)!r})",
            "directory.mkdir(mode=0o700)",
            "mandate = resources.files('firm_runtime').joinpath("
            "'resources/starter_mandate.md').read_text(encoding='utf-8')",
            "(directory / 'mandate.md').write_text(mandate, encoding='utf-8')",
            "(directory / 'firm.toml').write_text("
            "'max_active = 1\\nstaffing = false\\nfounder_profile = \\\"controlled\\\"\\n'"
            "'[profiles.controlled]\\n', encoding='utf-8')",
            "(directory / 'sandbox.toml').write_text("
            "f'[firm]\\nid = \\\"{directory.name}\\\"\\ncpus = 2\\n'"
            "'memory_gib = 4\\ndisk_gib = 20\\n', encoding='utf-8')",
        ]
    )
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(python_path),
            str(python_path),
            "-I",
            "-c",
            prepare,
        ],
        cwd=temporary,
        env=env,
    )
    initialized = json.loads(run([str(firm), "init", str(firm_directory)], cwd=temporary, env=env))
    if initialized.get("firm", {}).get("firm_id") != firm_directory.name:
        raise RuntimeError("Installed firm init did not initialize the isolated smoke firm")

    checked_process = subprocess.run(
        [str(firm), "check", str(firm_directory)],
        cwd=temporary,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        checked = json.loads(checked_process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Installed firm check returned invalid JSON:\n{checked_process.stdout}\n"
            f"{checked_process.stderr}"
        ) from exc
    if checked_process.returncode != (0 if checked.get("ok") else 2):
        raise RuntimeError(
            f"Installed firm check returned an unexpected exit code "
            f"{checked_process.returncode}: {checked_process.stdout}\n{checked_process.stderr}"
        )
    statuses = {item["check"]: item["status"] for item in checked.get("checks", [])}
    required_good = {
        "directory",
        "database",
        "mandate",
        "firm_config",
        "firm_config_consistency",
        "mandate_consistency",
        "sandbox_config",
    }
    if not checked.get("initialized") or any(
        statuses.get(name) != "good" for name in required_good
    ):
        raise RuntimeError(
            f"Installed firm check did not validate initialized local state: {checked}"
        )
    failures = {name for name, status in statuses.items() if status == "failure"}
    if failures - {"host_platform", "lima", "host_memory", "host_disk"}:
        raise RuntimeError(f"Installed firm check found a non-host-environment failure: {checked}")
    if statuses.get("credentials") != "skipped":
        raise RuntimeError("Provider-free release smoke unexpectedly inspected credentials")

    analyzed = json.loads(run([str(firm), "analyze", str(firm_directory)], cwd=temporary, env=env))
    if not isinstance(analyzed, dict) or "coverage" not in analyzed:
        raise RuntimeError("Installed firm analyze did not return its read-only analysis report")
    return {
        "installed": versions,
        "cli_help": ["firm", "firm-sandbox"],
        "resources": list(ASSETS["firm-runtime"] + ASSETS["firm-sandbox"]),
        "offline_workflow": {
            "commands": ["firm init", "firm check", "firm analyze"],
            "firm_id": firm_directory.name,
            "check_ok": checked["ok"],
            "check_host_failures": sorted(failures),
            "analysis_coverage": analyzed["coverage"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="require the optional v-prefixed tag to match both versions")
    parser.add_argument(
        "--no-build", action="store_true", help="inspect existing package dist artifacts"
    )
    parser.add_argument(
        "--no-install", action="store_true", help="skip the clean uv install and CLI smoke"
    )
    args = parser.parse_args(argv)
    runtime = __import__("tomllib").loads(
        (ROOT / "packages/firm-runtime/pyproject.toml").read_text()
    )
    sandbox = __import__("tomllib").loads(
        (ROOT / "packages/firm-sandbox/pyproject.toml").read_text()
    )
    versions = {
        "firm-runtime": runtime["project"]["version"],
        "firm-sandbox": sandbox["project"]["version"],
    }
    if versions["firm-runtime"] != versions["firm-sandbox"]:
        raise RuntimeError(f"Package versions differ: {versions}")
    if f"firm-sandbox=={versions['firm-sandbox']}" not in runtime["project"]["dependencies"]:
        raise RuntimeError("Runtime dependency must pin the matching sandbox release exactly")
    if args.tag and args.tag.removeprefix("v") != versions["firm-runtime"]:
        raise RuntimeError(f"Tag {args.tag!r} does not match release {versions['firm-runtime']}")
    if not args.no_build:
        run(["uv", "build", "--all-packages"], cwd=ROOT)
    artifacts = {}
    artifact_hashes = {}
    canonical_names = set()
    checks = {
        "package_version_equality": "passed",
        "runtime_sandbox_pin": "passed",
    }
    if args.tag:
        checks["tag_matches_release"] = "passed"
    distribution = ROOT / "dist"
    distribution.mkdir(exist_ok=True)
    for name, version_value in versions.items():
        distribution_name = name.replace("-", "_")
        wheels = sorted(distribution.glob(f"{distribution_name}-{version_value}-*.whl"))
        sdists = sorted(distribution.glob(f"{distribution_name}-{version_value}.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError(
                f"Expected one wheel and sdist for {name} {version_value}; "
                f"found {wheels} and {sdists}"
            )
        artifacts[name] = {
            "wheel": verify_wheel(wheels[0], name, version_value),
            "sdist": verify_sdist(sdists[0], name, version_value, rebuild=True),
        }
        canonical_names.update({wheels[0].name, sdists[0].name})
        artifact_hashes[wheels[0].name] = artifacts[name]["wheel"]["sha256"]
        artifact_hashes[sdists[0].name] = artifacts[name]["sdist"]["sha256"]
    extra_archives = sorted(
        path.name
        for path in distribution.iterdir()
        if path.suffix in {".whl", ".gz"} and path.name not in canonical_names
    )
    if extra_archives:
        raise RuntimeError(f"Remove stale or unexpected release archives: {extra_archives}")
    checks.update(
        wheel_contents="passed",
        sdist_contents="passed",
        sdist_rebuild="passed",
    )
    if args.no_install:
        install = {"skipped": True}
    else:
        with tempfile.TemporaryDirectory(prefix="firm-release-install-") as temporary:
            install = smoke_install(
                [Path(artifacts[name]["wheel"]["file"]) for name in PACKAGES],
                versions,
                Path(temporary),
            )
    checks["isolated_install"] = "skipped" if args.no_install else "passed"
    checks["cli_help"] = "skipped" if args.no_install else "passed"
    checks["packaged_assets"] = "skipped" if args.no_install else "passed"
    checks["offline_workflow"] = "skipped" if args.no_install else "passed"
    evidence = {
        "source_revision": run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(),
        "source_dirty": bool(
            run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT).strip()
        ),
        "release": versions["firm-runtime"],
        "compatibility_generation": __import__(
            "firm_sandbox.compatibility", fromlist=["COMPATIBILITY_GENERATION"]
        ).COMPATIBILITY_GENERATION,
        "artifact_sha256": dict(sorted(artifact_hashes.items())),
        "checks": checks,
        "tag": args.tag,
        "artifact_details": artifacts,
        "isolated_install_details": install,
    }
    destination = ROOT / "dist" / "release-evidence.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "release": evidence["release"],
                "artifact_sha256": evidence["artifact_sha256"],
                "checks": checks,
                "isolated_install": install,
            },
            indent=2,
        )
    )
    print(f"Release evidence: {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
