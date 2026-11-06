"""Firm export and VM discarding without VMs: the export contract, its swap and its guards."""

import fcntl
import json
import sqlite3
import sys

import pytest

from firm_runtime import export as exports
from firm_runtime import fleet
from firm_runtime.cli import main
from firm_runtime.errors import FirmError


class FakeSandbox:
    """Unpacks a small drive; a marker file under the firm directory records discarding."""

    calls = []
    failure = None

    def __init__(self, config):
        self.config = config

    def export(self, destination, *, homes=False, boot=False):
        FakeSandbox.calls.append(("export", homes, boot))
        if FakeSandbox.failure:
            raise RuntimeError(FakeSandbox.failure)
        work = destination / "drive" / "org" / "founder" / "work"
        work.mkdir(parents=True)
        (work / "hello.md").write_text("hello")
        (work / "latest").symlink_to("hello.md")
        if homes:
            (destination / "homes" / "founder" / "work").mkdir(parents=True)
            (destination / "homes" / "founder" / "work" / "draft.md").write_text("draft")
        return []

    def state(self):
        return "discarded" if (self.config.root / "discarded").exists() else "stopped"

    def discard(self):
        FakeSandbox.calls.append(("discard", (self.config.root / "export").is_dir()))
        (self.config.root / "discarded").touch()
        return {"discarded": True}


@pytest.fixture
def firm(tmp_path, monkeypatch, capsys):
    FakeSandbox.calls, FakeSandbox.failure = [], None
    monkeypatch.setattr(exports, "Sandbox", FakeSandbox)
    monkeypatch.setattr(fleet, "Sandbox", FakeSandbox)
    mandate, config = tmp_path / "mandate.md", tmp_path / "firm.toml"
    mandate.write_text("Build a useful firm.")
    config.write_text('max_active = 2\nstaffing = false\nfounder_profile = "p"\n[profiles.p]\n')
    directory = tmp_path / "demo"
    invoke(
        monkeypatch,
        capsys,
        "init",
        directory,
        "--firm-id",
        "demo",
        "--mandate",
        mandate,
        "--config",
        config,
    )
    (directory / "sandbox.toml").write_text('[firm]\nid = "demo"\n')
    durable = directory / "durable"
    (durable / "inputs" / "activations" / "a1").mkdir(parents=True)
    (durable / "inputs" / "activations" / "a1" / "activation.txt").write_text("wake")
    (durable / "events.jsonl").write_text('{"kind": "firm.started"}\n')
    for station in (1, 2):
        leases = durable / "mac" / "stations" / str(station) / "leases"
        leases.mkdir(parents=True)
        (leases / "lease.log").write_text("$ xcodebuild\n")
    return directory


def invoke(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["firm", *map(str, args)])
    main()
    return json.loads(capsys.readouterr().out)


def refused(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["firm", *map(str, args)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    return capsys.readouterr().err


def refused_usage(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["firm", *map(str, args)])
    with pytest.raises(SystemExit):
        main()
    return capsys.readouterr().err


def test_export_writes_database_host_records_drive_and_a_verifiable_manifest(
    firm, monkeypatch, capsys
):
    result = invoke(monkeypatch, capsys, "export", firm)
    export = firm / "export"
    assert result["directory"] == str(export)
    assert FakeSandbox.calls == [("export", False, True)]
    manifest = exports.verify(export)
    assert set(manifest["entries"]) == {
        "firm-release.json",
        "firm.sqlite3",
        "events.jsonl",
        "inputs/activations/a1/activation.txt",
        "mac/stations/1/leases/lease.log",
        "mac/stations/2/leases/lease.log",
        "drive/org/founder/work/hello.md",
        "drive/org/founder/work/latest",
    }
    assert manifest["entries"]["drive/org/founder/work/latest"] == {"link": "hello.md"}
    assert manifest["firm_id"] == result["firm_id"] == "demo"
    assert (result["files"], manifest["homes"], manifest["refused"]) == (8, False, [])
    assert (result["created_with_release"], result["compatibility_generation"]) == (
        "0.1.0",
        1,
    )
    assert result["totals"]["drive"]["files"] == 2 and "entries" not in result
    db = sqlite3.connect(export / "firm.sqlite3")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        db.close()
    analysis = invoke(monkeypatch, capsys, "analyze", export, "--section", "overview")
    assert analysis["firm_id"] == "demo"
    assert sorted(path.name for path in export.iterdir()) == [
        "drive",
        "events.jsonl",
        "firm-release.json",
        "firm.sqlite3",
        "inputs",
        "mac",
        "manifest.json",
    ]


def test_export_carries_work_period_tallies_and_leaves_ledger_payloads_behind(
    firm, monkeypatch, capsys
):
    db = sqlite3.connect(firm / "durable" / "firm.sqlite3")
    try:
        db.execute(
            "INSERT INTO activations(activation_id,employee_id,session_id,state,wake_snapshot,"
            "context_json,created_at,started_at,finished_at) VALUES('a1','founder','s1',"
            "'completed',?,'{}',1000,1001,1005)",
            (json.dumps([{"reason": "message", "priority": 0, "ready_at": 1000}]),),
        )
        db.execute("INSERT INTO activation_tool_calls VALUES('a1','Bash',7)")
        db.execute(
            "INSERT INTO requests VALUES('founder','r1','send',?,'{}','a1',1002)",
            (json.dumps({"body": "confidential draft"}),),
        )
        db.commit()
    finally:
        db.close()
    invoke(monkeypatch, capsys, "export", firm)
    data = invoke(
        monkeypatch,
        capsys,
        "analyze",
        firm / "export",
        "--section",
        "timeline",
        "--row-type",
        "activation",
        "--from",
        "1970-01-01T00:00:00Z",
        "--until",
        "2100-01-01T00:00:00Z",
    )
    row = next(r for r in data["rows"] if r["activation_id"] == "a1")
    assert row["tool_calls"] == {"Bash": 7}
    assert row["operations"] == {"send": 1}
    assert [w["reason"] for w in row["wake_reasons"]] == ["message"]
    assert "confidential draft" not in json.dumps(data)


def test_reexport_replaces_the_previous_export_whole(firm, monkeypatch, capsys):
    invoke(monkeypatch, capsys, "export", firm)
    (firm / "export" / "drive" / "stale.md").write_text("from an earlier export")
    with pytest.raises(FirmError, match="does not match its manifest at drive/stale.md"):
        exports.verify(firm / "export")
    result = invoke(monkeypatch, capsys, "export", firm, "--homes")
    assert not (firm / "export" / "drive" / "stale.md").exists()
    assert (firm / "export" / "homes" / "founder" / "work" / "draft.md").read_text() == "draft"
    assert result["homes"] is True and exports.verify(firm / "export")["files"] == 9
    assert not list(firm.glob(".export.new-*"))


def test_failed_export_keeps_the_previous_export(firm, monkeypatch, capsys):
    invoke(monkeypatch, capsys, "export", firm)
    before = exports.verify(firm / "export")
    FakeSandbox.failure = "Guest export failed: No space left on device"
    assert "No space left" in refused(monkeypatch, capsys, "export", firm)
    assert exports.verify(firm / "export") == before
    assert not list(firm.glob(".export.new-*"))


def test_export_preserves_neighbors_and_uses_its_own_staging(firm, monkeypatch, capsys):
    neighbor = firm / ".export.new"
    neighbor.mkdir()
    (neighbor / "precious").write_text("owner data")
    original = FakeSandbox.export
    destinations = []

    def overlapping(self, destination, **kwargs):
        destinations.append(destination)
        if len(destinations) == 1:
            exports.write(firm, "demo")
        return original(self, destination, **kwargs)

    monkeypatch.setattr(FakeSandbox, "export", overlapping)
    invoke(monkeypatch, capsys, "export", firm)
    assert len(set(destinations)) == 2
    assert exports.verify(firm / "export")["firm_id"] == "demo"
    assert (neighbor / "precious").read_text() == "owner data"
    assert not any(path.exists() for path in destinations)


def test_export_refuses_a_symlink_destination(firm, monkeypatch, capsys, tmp_path):
    invoke(monkeypatch, capsys, "export", firm)
    alias = tmp_path / "alias"
    alias.symlink_to(firm / "export", target_is_directory=True)
    assert "refusing to replace" in refused(monkeypatch, capsys, "export", firm, alias)
    assert alias.is_symlink()
    assert exports.verify(firm / "export")["firm_id"] == "demo"


def test_export_to_a_named_destination_refuses_to_replace_other_data(
    firm, monkeypatch, capsys, tmp_path
):
    elsewhere = tmp_path / "kept"
    elsewhere.mkdir()
    (elsewhere / "photo.jpg").write_text("mine")
    assert "not a firm export" in refused(monkeypatch, capsys, "export", firm, elsewhere)
    assert (elsewhere / "photo.jpg").exists() and FakeSandbox.calls == []
    result = invoke(monkeypatch, capsys, "export", firm, tmp_path / "archive")
    assert result["directory"] == str(tmp_path / "archive")
    assert not (firm / "export").exists()


def test_export_under_a_running_execution_host_does_not_boot(firm, monkeypatch, capsys):
    (firm / "locks").mkdir(exist_ok=True)
    with (firm / "locks" / "runtime-owner.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        invoke(monkeypatch, capsys, "export", firm)
    assert FakeSandbox.calls == [("export", False, False)]


def test_discard_vms_exports_first_and_keeps_the_firm_directory(firm, monkeypatch, capsys):
    assert "does not match" in refused(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "other"
    )
    (firm / "locks").mkdir(exist_ok=True)
    with (firm / "locks" / "runtime-owner.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "execution host owns" in refused(
            monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo"
        )
    assert FakeSandbox.calls == []
    # An earlier export without homes never stands in for the discard's own.
    invoke(monkeypatch, capsys, "export", firm)
    FakeSandbox.calls = []
    result = invoke(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo")
    assert FakeSandbox.calls == [("export", True, True), ("discard", True)]
    assert result["status"] == "discarded" and result["export"]["homes"] is True
    assert (firm / "export" / "homes" / "founder" / "work" / "draft.md").exists()
    assert (firm / "durable" / "firm.sqlite3").exists()
    assert invoke(monkeypatch, capsys, "status", firm)["sandbox_state"] == "discarded"
    # A repeated discard verifies the kept export instead of exporting an empty drive.
    FakeSandbox.calls = []
    invoke(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo")
    assert FakeSandbox.calls == [("discard", True)]
    (firm / "export" / "firm.sqlite3").write_bytes(b"damaged")
    assert "does not match" in refused(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo"
    )
    assert FakeSandbox.calls == [("discard", True)]


def test_discard_vms_does_not_discard_when_the_export_fails(firm, monkeypatch, capsys):
    FakeSandbox.failure = "Linux VM startup was not confirmed"
    assert "startup was not confirmed" in refused(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo"
    )
    assert FakeSandbox.calls == [("export", True, True)]
    assert not (firm / "export").exists()


def test_discard_rerun_refuses_an_export_from_another_firm(firm, monkeypatch, capsys):
    invoke(monkeypatch, capsys, "export", firm, "--homes")
    (firm / "discarded").touch()
    export = firm / "export"
    with sqlite3.connect(export / "firm.sqlite3") as db:
        db.execute("UPDATE firm SET firm_id='other'")
    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["firm_id"] = "other"
    manifest["entries"] = exports._entries(export)
    manifest_path.write_text(json.dumps(manifest))
    assert exports.verify(export)["firm_id"] == "other"
    FakeSandbox.calls = []
    assert "different firm" in refused(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo"
    )
    assert FakeSandbox.calls == []


@pytest.mark.parametrize("entries", [None, [], "not an index"])
def test_export_verify_reports_malformed_manifest(firm, monkeypatch, capsys, entries):
    invoke(monkeypatch, capsys, "export", firm)
    path = firm / "export" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["entries"] = entries
    path.write_text(json.dumps(manifest))
    with pytest.raises(FirmError, match="manifest"):
        exports.verify(path.parent)


def test_rerun_discard_refuses_a_kept_export_without_the_homes_it_asks_for(
    firm, monkeypatch, capsys
):
    result = invoke(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo", "--no-homes")
    assert FakeSandbox.calls == [("export", False, True), ("discard", True)]
    assert result["export"]["homes"] is False
    FakeSandbox.calls = []
    assert "finish with --no-homes" in refused(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo"
    )
    assert FakeSandbox.calls == []
    invoke(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo", "--no-homes")
    assert FakeSandbox.calls == [("discard", True)]
    assert "unrecognized arguments: --homes" in refused_usage(
        monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo", "--homes"
    )


def test_discard_vms_and_delete_require_a_stopped_firm(firm, monkeypatch, capsys):
    db = sqlite3.connect(firm / "durable" / "firm.sqlite3")
    db.execute("UPDATE firm SET state='running'")
    db.commit()
    assert "stop the firm" in refused(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo")
    db.execute("UPDATE firm SET state='stopped'")
    db.commit()
    db.close()
    invoke(monkeypatch, capsys, "discard-vms", firm, "--confirm", "demo")
    assert invoke(monkeypatch, capsys, "delete", firm, "--confirm", "demo")["status"] == "deleted"
    assert not firm.exists()
