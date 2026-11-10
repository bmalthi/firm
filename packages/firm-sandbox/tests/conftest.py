import json
import shutil
from pathlib import Path

import pytest

from firm_sandbox import lima_paths, linux_base


@pytest.fixture(autouse=True)
def disposable_host_runtime(request, tmp_path, tmp_path_factory, monkeypatch):
    """Unit firms keep their short Lima runtime directories out of the host's real parent.

    The real parent, /private/tmp/firm-<uid>/lima, holds running firms' runtimes. Each
    unit test gets its own parent under pytest's temporary directory instead; a test
    that needs a particular parent sets one itself. Integration tests boot real VMs,
    whose sockets need the real, short parent, so they use it and remove afterwards
    only the runtimes their own firms own.
    """
    if not request.node.get_closest_marker("integration"):
        monkeypatch.setattr(
            linux_base, "cache_directory", lambda: tmp_path / "cache" / "linux-base"
        )
        monkeypatch.setenv("LIMA_HOME", str(tmp_path / "lima-home"))
        monkeypatch.setattr(
            lima_paths, "RUNTIME_PARENT", tmp_path_factory.mktemp("runtime") / "lima"
        )
        yield
        return
    parent = lima_paths.RUNTIME_PARENT
    before = set(parent.iterdir()) if parent.exists() else set()
    yield
    if parent.exists():
        for runtime in set(parent.iterdir()) - before:
            # Concurrent test runners and real fixtures share this parent.
            # A newly observed directory is not evidence that we own it.
            try:
                owner = json.loads((runtime / "owner.json").read_text())
                owned = Path(owner["root"]).is_relative_to(tmp_path.resolve())
            except (OSError, ValueError, KeyError, TypeError):
                owned = False
            if owned:
                shutil.rmtree(runtime)
