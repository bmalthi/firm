from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).resolve().parents[1] / "release_check.py"
)
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


@pytest.fixture
def synthetic_release(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    package = root / "packages/firm-runtime"
    source = package / "src/firm_runtime"
    (source / "resources").mkdir(parents=True)
    (source / "__init__.py").write_bytes(b"current module\n")
    (source / "resources/starter_mandate.md").write_bytes(b"current starter\n")
    package.mkdir(exist_ok=True)
    (package / "README.md").write_bytes(b"package README\n")
    (package / "pyproject.toml").write_bytes(b"[project]\nversion='0.1.0'\n")
    (root / "LICENSE").write_bytes(b"license\n")
    (root / "NOTICE").write_bytes(b"notice\n")
    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setattr(
        release_check,
        "ASSETS",
        {
            "firm-runtime": ("firm_runtime/resources/starter_mandate.md",),
            "firm-sandbox": (),
        },
    )
    return root, package, source


def _wheel(path: Path, source: Path, *, fault: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "firm_runtime-0.1.0.dist-info/METADATA",
            "\n".join(
                (
                    "Name: firm-runtime",
                    "Version: 0.1.0",
                    "License-Expression: Apache-2.0",
                    "Requires-Python: >=3.12",
                    "Project-URL: Source, https://github.com/bmalthi/firm",
                    "Requires-Dist: firm-sandbox==0.1.0",
                    "",
                )
            ),
        )
        archive.writestr("firm_runtime-0.1.0.dist-info/licenses/LICENSE", b"license\n")
        archive.writestr("firm_runtime-0.1.0.dist-info/licenses/NOTICE", b"notice\n")
        for file in source.rglob("*"):
            if not file.is_file():
                continue
            relative = file.relative_to(source).as_posix()
            if fault == "missing" and relative.endswith("starter_mandate.md"):
                continue
            contents = file.read_bytes()
            if fault == "stale" and relative == "__init__.py":
                contents = b"stale module\n"
            archive.writestr(f"firm_runtime/{relative}", contents)


def _sdist(path: Path, package: Path, source: Path, *, fault: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        files = {
            "LICENSE": b"license\n",
            "NOTICE": b"notice\n",
            "README.md": (package / "README.md").read_bytes(),
            "pyproject.toml": (package / "pyproject.toml").read_bytes(),
        }
        for file in source.rglob("*"):
            if file.is_file():
                relative = f"src/firm_runtime/{file.relative_to(source).as_posix()}"
                if fault == "missing" and relative.endswith("starter_mandate.md"):
                    continue
                contents = file.read_bytes()
                if fault == "stale" and relative.endswith("__init__.py"):
                    contents = b"stale module\n"
                files[relative] = contents
        for name, contents in files.items():
            member = tarfile.TarInfo(f"firm-runtime-0.1.0/{name}")
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("fault", [None, "stale", "missing"])
def test_release_archive_source_bytes_match_current_checkout(
    tmp_path, synthetic_release, artifact_kind, fault
):
    root, package, source = synthetic_release
    distribution = root / "dist"
    distribution.mkdir()
    if artifact_kind == "wheel":
        path = distribution / "firm_runtime-0.1.0-py3-none-any.whl"
        _wheel(path, source, fault=fault)
    else:
        path = distribution / "firm-runtime-0.1.0.tar.gz"
        _sdist(path, package, source, fault=fault)

    if fault is None:
        if artifact_kind == "wheel":
            release_check.verify_wheel(path, "firm-runtime", "0.1.0")
        else:
            release_check.verify_sdist(path, "firm-runtime", "0.1.0", rebuild=False)
    else:
        with pytest.raises(RuntimeError, match="current checkout|missing packaged files"):
            if artifact_kind == "wheel":
                release_check.verify_wheel(path, "firm-runtime", "0.1.0")
            else:
                release_check.verify_sdist(path, "firm-runtime", "0.1.0", rebuild=False)


def test_isolated_install_resolves_repository_wheel_paths_before_changing_cwd(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    wheel = root / "dist/firm_runtime-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"synthetic wheel")
    temporary = tmp_path / "install"
    temporary.mkdir()
    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setenv("OPENAI_API_KEY", "owner-openai-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "owner-claude-secret")
    monkeypatch.setenv("FIRM_CREDENTIALS_DIR", str(tmp_path / "owner-credentials"))
    commands = []

    def record(command, *, cwd, env=None):
        commands.append((command, cwd, env))
        if len(command) >= 2 and command[1] == "init":
            return json.dumps({"firm": {"firm_id": Path(command[2]).name}})
        if len(command) >= 2 and command[1] == "analyze":
            return json.dumps({"coverage": {"complete": True}})
        return ""

    monkeypatch.setattr(release_check, "run", record)

    def check(command, *, cwd, env, check, capture_output, text):
        assert capture_output
        assert command[1:3] == ["check", str(temporary / "2026-10-28_release-smoke")]
        assert env["HOME"] == str(temporary / "home")
        assert env["XDG_CONFIG_HOME"] == str(temporary / "home/.config")
        assert env["XDG_DATA_HOME"] == str(temporary / "home/.local/share")
        assert "OPENAI_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "FIRM_CREDENTIALS_DIR" not in env
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "initialized": True,
                    "ok": True,
                    "checks": [
                        {"check": name, "status": "good"}
                        for name in (
                            "directory",
                            "database",
                            "mandate",
                            "firm_config",
                            "firm_config_consistency",
                            "mandate_consistency",
                            "sandbox_config",
                        )
                    ]
                    + [{"check": "credentials", "status": "skipped"}],
                }
            ),
            "",
        )

    monkeypatch.setattr(release_check.subprocess, "run", check)
    release_check.smoke_install(
        [Path("dist/firm_runtime-0.1.0-py3-none-any.whl")],
        {"firm-runtime": "0.1.0", "firm-sandbox": "0.1.0"},
        temporary,
    )

    install = next(
        command for command, _cwd, _env in commands if command[1:3] == ["pip", "install"]
    )
    assert install[-1] == str(wheel.resolve())
    assert Path(install[-1]).is_file()
    assert commands[1][1] == temporary
    firm_commands = {
        command[1]
        for command, _cwd, _env in commands
        if len(command) > 1 and Path(command[0]).name == "firm"
    }
    assert {"init", "analyze"} <= firm_commands
    assert "starter_mandate.md" in "\n".join(
        command[-1] for command, _cwd, _env in commands if "-c" in command
    )
