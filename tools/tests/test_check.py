from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

check_path = Path(__file__).resolve().parents[1] / "check.py"
spec = importlib.util.spec_from_file_location("check", check_path)
check = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = check
spec.loader.exec_module(check)
Check = check.Check
run_checks = check.run_checks


def test_run_checks_continues_after_failure_and_reports_optional_skip(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    messages: list[str] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 7 if command[0] == "broken" else 0)

    checks = (
        Check("first", ("first",)),
        Check("broken", ("broken",)),
        Check("optional", ("optional",), optional_tool="optional-tool"),
        Check("last", ("last",)),
    )

    status = run_checks(
        checks,
        root=tmp_path,
        runner=runner,
        available=lambda tool: None if tool == "optional-tool" else "/bin/tool",
        output=messages.append,
    )

    assert status == 1
    assert calls == [("first",), ("broken",), ("last",)]
    assert any(message.startswith("SKIP  optional: ") for message in messages)
    assert messages[-1] == "Results: 2 passed, 1 skipped, 1 failed"


def test_run_checks_skips_when_optional_probe_fails(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    messages: list[str] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 1 if command[0] == "probe" else 0)

    status = run_checks(
        (Check("fixture", ("fixture",), optional_probe=("probe", "compiler")),),
        root=tmp_path,
        runner=runner,
        output=messages.append,
    )

    assert status == 0
    assert calls == [("probe", "compiler")]
    assert messages == [
        "SKIP  fixture: probe compiler is unavailable",
        "Results: 0 passed, 1 skipped, 0 failed",
    ]


def test_required_checks_fail_for_missing_tool_and_failed_probe(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    messages: list[str] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 1 if command[0] == "probe" else 0)

    status = run_checks(
        (
            Check("node", ("node", "test"), optional_tool="node"),
            Check("swift", ("compile",), optional_probe=("probe",)),
            Check("remaining", ("remaining",)),
        ),
        root=tmp_path, runner=runner, available=lambda _: None,
        output=messages.append, required=True,
    )

    assert status == 1
    assert calls == [("probe",), ("remaining",)]
    assert messages[0] == "FAIL  node: node is unavailable"
    assert messages[1] == "FAIL  swift: probe is unavailable"
    assert messages[-1] == "Results: 1 passed, 0 skipped, 2 failed"


def test_helper_build_isolates_signing_settings_and_removes_its_output(tmp_path, monkeypatch):
    helper = next(item for item in check._commands() if item.name == "Swift station-helper build")
    monkeypatch.setenv("FIRM_HELPER_SIGN_IDENTITY", "owner-identity")
    monkeypatch.setenv("FIRM_HELPER_SIGN_KEYCHAIN", "/owner/keychain")
    monkeypatch.setenv("FIRM_HELPER_BUILD_VERSION", "owner-version")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    script = tmp_path / "station-helper" / "build.sh"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\nset -eu\n"
        'printf "%s\\n" "$FIRM_HELPER_SIGN_IDENTITY" "$FIRM_HELPER_SIGN_KEYCHAIN" '
        '"$FIRM_HELPER_BUILD_VERSION" "$1" > receipt.txt\n'
        'touch "$1/output"\n'
    )

    def runner(command, **kwargs):
        if command == helper.optional_probe:
            return subprocess.CompletedProcess(command, 0)
        return subprocess.run(command, **kwargs)

    assert run_checks(
        [helper], root=tmp_path, runner=runner, available=lambda tool: "/bin/tool"
    ) == 0
    identity, keychain, version, build_path = (tmp_path / "receipt.txt").read_text().splitlines()
    assert (identity, keychain, version) == ("-", "", "1")
    assert Path(build_path).parent == tmp_path
    assert not Path(build_path).exists()
