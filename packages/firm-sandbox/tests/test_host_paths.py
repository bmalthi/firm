import os
import subprocess
import sys
from pathlib import Path

import pytest

from firm_sandbox.host_paths import read_text_file


def test_read_text_file_reads_regular_and_symlinked_regular_files(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("hello\n")
    link = tmp_path / "link.txt"
    link.symlink_to(source)

    assert read_text_file(source) == "hello\n"
    assert read_text_file(link) == "hello\n"


def test_read_text_file_rejects_invalid_utf8(tmp_path: Path):
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="valid UTF-8"):
        read_text_file(path)


@pytest.mark.parametrize("operation", [
    "from firm_sandbox.host_paths import read_text_file; read_text_file(path)",
    "from firm_sandbox import Config; Config.load(path)",
])
def test_control_file_readers_reject_fifo_without_blocking(tmp_path: Path, operation):
    path = tmp_path / "pipe"
    os.mkfifo(path)

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; path = Path(sys.argv[1]); " + operation,
         str(path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "must be a regular file" in result.stderr


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="device file unavailable")
def test_read_text_file_rejects_device_files():
    with pytest.raises(ValueError, match="regular file"):
        read_text_file(Path("/dev/null"))


def test_read_text_file_rejects_oversized_regular_files(tmp_path: Path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 17)

    with pytest.raises(ValueError, match="exceeds 16 bytes"):
        read_text_file(path, limit=16)
