"""Browser setup evidence and offline reuse must keep failures visible and restore egress."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "acceptance/browser.py"
SPEC = importlib.util.spec_from_file_location("browser_acceptance", PATH)
browser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser)


def test_setup_evidence_distinguishes_sdk_and_browser_failure():
    assert browser.setup_steps([
        "installer output", "BROWSER_STEP sdk 0 120", "BROWSER_STEP browser_install 1 4500",
    ]) == {
        "sdk": {"exit_code": 0, "milliseconds": 120},
        "browser_install": {"exit_code": 1, "milliseconds": 4500},
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_offline_reuse_restores_egress_and_stops_vm_when_workload_fails(
    tmp_path, monkeypatch, enabled,
):
    calls = []

    class Network:
        def traffic(self):
            return {"live": True}

        def saved_egress(self):
            return enabled

        def set_egress(self, value):
            calls.append(("egress", value))
            return {"egress": value}

    class Sandbox:
        def __init__(self, config):
            self.config = config
            self.mac = None
            self.linux = SimpleNamespace(network=Network())
            self.file_server = SimpleNamespace(provision=lambda *args: None)

        def up(self):
            calls.append("up")

        def down(self):
            calls.append("down")

        def start(self, spec):
            assert spec.argv[:2] == ("/usr/bin/env", "FIRM_BROWSER_OFFLINE=1")
            calls.append("start")
            return SimpleNamespace(run_id="run")

        def wait(self, run, timeout):
            raise RuntimeError("workload failed")

    monkeypatch.setattr(browser, "Sandbox", Sandbox)
    monkeypatch.setattr(browser.Config, "load", lambda path: SimpleNamespace(egress_mode="enforce"))
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", [
        str(PATH), "--root", str(tmp_path), "--evidence", str(evidence), "--offline-reuse",
    ])
    with pytest.raises(RuntimeError, match="workload failed"):
        browser.main()
    assert calls == ["up", ("egress", False), "start", ("egress", enabled), "down"]
    result = json.loads((evidence / "result.json").read_text())
    assert result["passed"] is False
    assert result["offline_egress"] is False
    assert result["egress_mode"] == "enforce"
