"""Acceptance entry points import and expose their documented command line."""

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
ACCEPTANCE = REPOSITORY / "tools" / "acceptance"
ENTRYPOINTS = sorted(
    path for path in ACCEPTANCE.rglob("*.py") if path.name != "acceptance_common.py"
)


def test_acceptance_entrypoints_are_found():
    if not ACCEPTANCE.is_dir():
        pytest.skip("tools/acceptance is not present")
    assert ENTRYPOINTS


@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda path: str(path.relative_to(ACCEPTANCE)))
def test_acceptance_entrypoint_help(path):
    """Run the safe public entry point, covering syntax, imports and argument setup."""
    result = subprocess.run(
        [sys.executable, str(path), "--help"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")
