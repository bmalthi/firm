#!/usr/bin/env python3
"""Run Firm's local, non-integration developer checks."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[object]]
Availability = Callable[[str], str | None]


def _print(message: str) -> None:
    print(message, flush=True)


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    optional_tool: str | None = None
    optional_probe: tuple[str, ...] | None = None


def _commands() -> tuple[Check, ...]:
    checks = [
        Check("Ruff", ("uv", "run", "ruff", "check", ".")),
        Check(
            "Python package tests",
            ("uv", "run", "pytest", "-q", "-m", "not integration"),
        ),
        Check(
            "Developer tools tests",
            ("uv", "run", "pytest", "-q", "tools/tests", "-m", "not integration"),
        ),
        Check("Package builds", ("uv", "build", "--all-packages")),
        Check(
            "Node console tests",
            ("node", "--test", "packages/firm-runtime/tests/web_console.test.mjs"),
            optional_tool="node",
        ),
    ]

    swift_scripts = (
        "test-executable-code.sh",
        "test-launch-policy.sh",
        "test-held-input.sh",
        "test-inspection-budget.sh",
        "test-ledger.sh",
        "test-peer-authentication.sh",
        "test-process-identity.sh",
        "test-request-framing.sh",
        "test-request-values.sh",
        "test-unix-socket.sh",
    )
    checks.extend(
        Check(
            f"Swift {script.removeprefix('test-').removesuffix('.sh')}",
            ("sh", f"station-helper/tests/{script}"),
            optional_tool="xcrun",
            optional_probe=("xcrun", "--find", "swiftc"),
        )
        for script in swift_scripts
    )
    checks.append(
        Check(
            "Swift station-helper build",
            (
                "sh",
                "-eu",
                "-c",
                "build_dir=$(mktemp -d \"${TMPDIR:-/tmp}/firm-station-helper-check.XXXXXX\"); "
                "trap 'rm -rf \"$build_dir\"' EXIT; "
                "FIRM_HELPER_SIGN_IDENTITY=- FIRM_HELPER_SIGN_KEYCHAIN= "
                "FIRM_HELPER_BUILD_VERSION=1 "
                "sh station-helper/build.sh \"$build_dir\" 501 502",
            ),
            optional_tool="xcrun",
            optional_probe=("xcrun", "--find", "swiftc"),
        )
    )
    return tuple(checks)


def _missing_requirement(
    check: Check,
    *,
    available: Availability,
    probe: Runner,
    root: Path,
) -> str | None:
    if check.optional_tool is not None and available(check.optional_tool) is None:
        return f"{check.optional_tool} is unavailable"
    if check.optional_probe is not None:
        try:
            result = probe(
                check.optional_probe,
                cwd=root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return f"{' '.join(check.optional_probe)} is unavailable ({error})"
        if result.returncode != 0:
            return f"{' '.join(check.optional_probe)} is unavailable"
    return None


def run_checks(
    checks: Iterable[Check],
    *,
    root: Path,
    runner: Runner = subprocess.run,
    available: Availability = shutil.which,
    output: Callable[[str], None] = _print,
    required: bool = False,
) -> int:
    """Run every check, continuing after failures, and return a process status."""

    failures = 0
    passed = 0
    skipped = 0
    for check in checks:
        reason = _missing_requirement(check, available=available, probe=runner, root=root)
        if reason:
            if required:
                output(f"FAIL  {check.name}: {reason}")
                failures += 1
            else:
                output(f"SKIP  {check.name}: {reason}")
                skipped += 1
            continue

        output(f"RUN   {check.name}: {' '.join(check.command)}")
        try:
            result = runner(check.command, cwd=root, check=False)
            returncode = result.returncode
        except OSError as error:
            output(f"FAIL  {check.name}: {error}")
            failures += 1
            continue
        if returncode == 0:
            output(f"PASS  {check.name}")
            passed += 1
        else:
            output(f"FAIL  {check.name} (exit {returncode})")
            failures += 1

    output(f"Results: {passed} passed, {skipped} skipped, {failures} failed")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--required", action="store_true",
        help="Fail instead of skipping checks when Node or the Swift toolchain is unavailable",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    return run_checks(_commands(), root=root, required=args.required)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
