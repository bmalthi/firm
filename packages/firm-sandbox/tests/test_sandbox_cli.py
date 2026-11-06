import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from firm_sandbox import cli


@pytest.fixture(autouse=True)
def synthetic_firm_marker(monkeypatch):
    monkeypatch.setattr(cli, "validate_firm_record", lambda _directory: None)


@pytest.mark.parametrize("option", ["--env", "--secret", "--secret-env"])
@pytest.mark.parametrize("value", ["NAME", "=value"])
def test_assignment_options_explain_the_required_shape(option, value):
    with pytest.raises(ValueError, match=rf"{option} values must use NAME=VALUE"):
        cli._assignments([value], option)


def test_assignment_options_preserve_empty_and_embedded_equals_values():
    assert cli._assignments(["EMPTY=", "TOKEN=one=two"], "--env") == {
        "EMPTY": "",
        "TOKEN": "one=two",
    }


def test_sandbox_only_init_creates_marker_without_database_or_vm(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sandbox.toml"
    config.write_text('[firm]\nid = "enrollment"\n')
    monkeypatch.setattr(cli, "Sandbox", lambda _: pytest.fail("constructed a sandbox"))
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "--config", str(config), "init"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["compatibility_generation"] > 0
    assert (tmp_path / "firm-release.json").is_file()
    assert not (tmp_path / "durable").exists()


def test_sandbox_only_init_refuses_to_stamp_an_existing_vm(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sandbox.toml"
    config.write_text('[firm]\nid = "enrollment"\n')
    (tmp_path / "durable" / "linux").mkdir(parents=True)
    monkeypatch.setattr(cli, "Sandbox", lambda _: pytest.fail("constructed a sandbox"))
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "--config", str(config), "init"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "does not stamp or migrate" in capsys.readouterr().err
    assert not (tmp_path / "firm-release.json").exists()


def test_cli_loads_one_sandbox_configuration(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sandbox.toml"
    config.write_text(
        '[firm]\nid = "test-firm"\ncpus = 4\nmemory_gib = 8\n[mac]\nimage = "/private/tmp/image"\n'
    )
    seen = {}

    class Box:
        def __init__(self, value):
            seen["config"] = value

        def up(self):
            return {"running": True}

    monkeypatch.setattr(cli, "Sandbox", Box)
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "--config", str(config), "up"])
    cli.main()
    assert seen["config"].firm_id == "test-firm"
    assert seen["config"].cpus == 4
    assert seen["config"].memory_gib == 8
    assert json.loads(capsys.readouterr().out)["running"] is True


def test_cli_checks_compatibility_before_constructing_sandbox(tmp_path, monkeypatch):
    config = tmp_path / "sandbox.toml"
    config.write_text("[firm]\nid = 'test-firm'\n")
    calls = []
    monkeypatch.setattr(cli, "validate_firm_record", lambda root: calls.append(("guard", root)))

    class Box:
        def __init__(self, _config):
            calls.append(("sandbox", None))

        def up(self):
            return {"running": True}

    monkeypatch.setattr(cli, "Sandbox", Box)
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "--config", str(config), "up"])
    cli.main()
    assert calls == [("guard", tmp_path), ("sandbox", None)]


def test_emergency_down_bypasses_compatibility_marker(tmp_path, monkeypatch):
    config = tmp_path / "sandbox.toml"
    config.write_text("[firm]\nid = 'test-firm'\n")
    monkeypatch.setattr(
        cli,
        "validate_firm_record",
        lambda _root: pytest.fail("shutdown checked compatibility"),
    )

    class Box:
        def __init__(self, _config):
            pass

        def down(self):
            return {"stopped": True}

    monkeypatch.setattr(cli, "Sandbox", Box)
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "--config", str(config), "down"])
    cli.main()


class NetworkBox:
    """The sandbox's network surface: granted hosts kept in memory, every change recorded."""

    def __init__(self, granted=()):
        self.granted, self.changes = list(granted), []

    def granted_hosts(self):
        return list(self.granted)

    def set_allowed_hosts(self, hosts):
        self.changes.append(list(hosts))
        self.granted = sorted(set(hosts))
        return {"hosts": self.granted}

    def allowed_hosts(self):
        return [{"host": host, "source": "granted"} for host in self.granted]

    def network_status(self):
        return {"linux": {"egress": True}}

    def set_network_enabled(self, enabled):
        return {"linux": {"egress": enabled}}


@pytest.mark.parametrize(
    ("arguments", "output"),
    [
        (["hosts"], [{"host": "a.example", "source": "granted"}]),
        ([], {"linux": {"egress": True}}),
        (["off"], {"linux": {"egress": False}}),
    ],
)
def test_network_cli_reads_and_cuts_off_but_never_writes_hosts(
    monkeypatch, capsys, arguments, output
):
    box = NetworkBox(["a.example"])
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "network", *arguments])
    cli.main()
    assert box.changes == []
    assert json.loads(capsys.readouterr().out) == output


@pytest.mark.parametrize("arguments", [["allow", "docs.example"], ["revoke", "a.example"]])
def test_granted_hosts_have_one_writer_and_it_is_not_this_cli(monkeypatch, capsys, arguments):
    box = NetworkBox(["a.example"])
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "network", *arguments])
    with pytest.raises(SystemExit):
        cli.main()
    assert "invalid choice" in capsys.readouterr().err and box.changes == []


def test_mac_image_build_cli_loads_and_passes_the_recipe(tmp_path, monkeypatch, capsys):
    recipe_path = tmp_path / "ios.toml"
    recipe_path.write_text(
        'disk_gib = 64\nstations = 3\n[[application]]\npath = "/Applications/Xcode.app"\n'
        '[[simulator]]\nplatform = "iOS"\n'
    )
    seen = {}

    def build(destination, recipe):
        seen["destination"] = destination
        seen["recipe"] = recipe
        return {"built": True}

    monkeypatch.setattr(cli.mac_image, "build_base_image", build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["firm-sandbox", "mac-image-build", str(tmp_path), "--recipe", str(recipe_path)],
    )
    cli.main()
    assert seen["destination"] == tmp_path
    assert seen["recipe"].disk_gib == 64
    assert seen["recipe"].applications == (Path("/Applications/Xcode.app"),)
    assert seen["recipe"].simulators == (cli.mac_image.Simulator("iOS"),)
    assert json.loads(capsys.readouterr().out)["built"] is True


def test_mac_image_build_cli_requires_a_recipe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-image-build", str(tmp_path)])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert "--recipe" in capsys.readouterr().err


def test_mac_image_apply_cli_loads_the_recipe_and_applies_to_the_shared_mac(
    tmp_path, monkeypatch, capsys
):
    recipe_path = tmp_path / "ios.toml"
    recipe_path.write_text(
        'disk_gib = 64\nstations = 3\n[[application]]\npath = "/Applications/Xcode.app"\n'
        '[[simulator]]\nplatform = "iOS"\nversion = "26.0"\n'
    )
    seen = {}

    def apply_recipe(vm, recipe):
        seen["vm"] = vm
        seen["recipe"] = recipe
        return {"installed": []}

    monkeypatch.setattr(cli.mac_image, "apply_recipe", apply_recipe)
    monkeypatch.setattr(cli, "Sandbox", lambda config: SimpleNamespace(mac="the-only-mac"))
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-image-apply", str(recipe_path)])
    cli.main()
    assert seen["vm"] == "the-only-mac"
    assert seen["recipe"].simulators == (cli.mac_image.Simulator("iOS", "26.0"),)
    assert json.loads(capsys.readouterr().out) == {"installed": []}


def test_mac_replace_cli_uses_loaded_configuration(monkeypatch):
    calls = []
    box = SimpleNamespace(
        mac=SimpleNamespace(stations=(1,)),
        mac_replace=lambda: calls.append("mac-replace") or {"replaced": True},
    )
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-replace"])
    cli.main()
    assert calls == ["mac-replace"]


def test_mac_image_build_cli_unwinds_sigterm_like_an_interrupt(tmp_path, monkeypatch, capsys):
    before = signal.getsignal(signal.SIGTERM)
    unwound = []
    recipe_path = tmp_path / "ios.toml"
    recipe_path.write_text("disk_gib = 64\nstations = 3\n")

    def build(destination, recipe):
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        except KeyboardInterrupt:
            unwound.append(True)
            raise

    monkeypatch.setattr(cli.mac_image, "build_base_image", build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["firm-sandbox", "mac-image-build", str(tmp_path), "--recipe", str(recipe_path)],
    )
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 130 and unwound == [True]
    assert "interrupted" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) == before


def test_replace_cli_uses_loaded_configuration(monkeypatch):
    calls = []
    box = SimpleNamespace(replace=lambda: calls.append("replace"))
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "replace"])
    cli.main()
    assert calls == ["replace"]


@pytest.mark.parametrize("action", ["logs", "run"])
def test_cli_emits_complete_sandbox_logs(tmp_path, monkeypatch, capsysbinary, action):
    content = b"x" * (1024 * 1024) + b"last page\n"
    calls = []

    class Box:
        def __init__(self, config):
            pass

        def logs(self, run):
            calls.append("logs")
            return content

        def follow(self, run):
            calls.append("follow")
            yield content[: 1024 * 1024]
            yield content[1024 * 1024 :]

        def current(self, role):
            assert role == "analyst"
            return object()

        def start(self, spec):
            return object()

        def wait(self, run):
            return {"exit_code": 0}

    monkeypatch.setattr(cli, "Sandbox", Box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    arguments = ["logs", "analyst"] if action == "logs" else ["run", "analyst", "true"]
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", *arguments])
    if action == "run":
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 0
    else:
        cli.main()
    assert capsysbinary.readouterr().out == content
    assert calls == ["logs" if action == "logs" else "follow"]


def test_mac_recovery_requires_explicit_owner_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm-sandbox",
            "mac-recover",
        ],
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


@pytest.mark.parametrize("clean", [True, False])
def test_mac_recovery_cli_reports_verified_result(tmp_path, monkeypatch, capsys, clean):
    def recover(*, confirm_cleanup):
        assert confirm_cleanup is True
        return {"status": "ok", "clean": clean, "recovered": clean}

    monkeypatch.setattr(
        cli,
        "Sandbox",
        lambda config: SimpleNamespace(
            mac=SimpleNamespace(stations=(1,)),
            mac_station=lambda station: SimpleNamespace(recover=recover),
        ),
    )
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "firm-sandbox",
            "mac-recover",
            "--station",
            "1",
            "--confirm-cleanup",
        ],
    )
    if clean:
        cli.main()
    else:
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 2
    assert json.loads(capsys.readouterr().out)["recovered"] is clean


@pytest.mark.parametrize("named", [None, 2])
def test_mac_session_reset_cli_requires_and_routes_the_account(monkeypatch, capsys, named):
    chosen = []
    box = SimpleNamespace(
        mac=SimpleNamespace(stations=(1, 2)),
        mac_session_reset=lambda number: chosen.append(number) or {"station": number},
    )
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    arguments = ["mac-session-reset"] + ([] if named is None else ["--station", "2"])
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", *arguments])
    if named is None:
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 2
        assert "--station" in capsys.readouterr().err
        assert chosen == []
    else:
        cli.main()
        assert chosen == [2]
        assert json.loads(capsys.readouterr().out) == {"station": 2}


def test_mac_restart_cli_is_machine_wide(monkeypatch):
    calls = []
    box = SimpleNamespace(mac=object(), mac_restart=lambda: calls.append("restart"))
    monkeypatch.setattr(cli, "Sandbox", lambda config: box)
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-restart"])
    cli.main()
    assert calls == ["restart"]


def test_mac_prepare_cli_requires_a_recipe(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-prepare", "/helper.app"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert "--recipe" in capsys.readouterr().err


def test_mac_status_reports_every_station_with_its_own_probes(monkeypatch, capsys):
    def station(number):
        return SimpleNamespace(
            helper_status=lambda: {"ready": number == 1},
            awake_status=lambda: (_ for _ in ()).throw(
                OSError(f"Mac station {number} is already leased")
            ),
        )

    monkeypatch.setattr(
        cli,
        "Sandbox",
        lambda config: SimpleNamespace(
            mac=SimpleNamespace(stations=(1, 2)),
            mac_station=station,
            mac_status=lambda: {
                "configured": True,
                "enrolled": True,
                "stations": [{"station": 1, "held": True}, {"station": 2, "held": False}],
            },
        ),
    )
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-status"])
    cli.main()
    rows = json.loads(capsys.readouterr().out)["stations"]
    assert [row["station"] for row in rows] == [1, 2]
    assert [row["helper"] for row in rows] == [{"ready": True}, {"ready": False}]
    assert rows[1]["awake"] == {"error": "Mac station 2 is already leased"}
    # With --station only that station is reported.
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", "mac-status", "--station", "2"])
    cli.main()
    rows = json.loads(capsys.readouterr().out)["stations"]
    assert [row["station"] for row in rows] == [2]


def test_prepare_accepts_recipe_accounts_on_one_mac(tmp_path, monkeypatch, capsys):
    recipe = tmp_path / "recipe.toml"
    recipe.write_text("disk_gib = 64\nstations = 3\n")
    vm = SimpleNamespace(stations=(1, 2, 3))
    calls = []
    monkeypatch.setattr(
        cli,
        "Sandbox",
        lambda config: SimpleNamespace(
            mac=vm, file_server=SimpleNamespace(reconnect=lambda: calls.append("reconnect"))
        ),
    )
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(
        cli.mac_enrollment,
        "prepare",
        lambda machine, app, recipe: calls.append((machine, app, recipe.stations)) or {"turn": 1},
    )
    monkeypatch.setattr(
        sys, "argv", ["firm-sandbox", "mac-prepare", "/helper.app", "--recipe", str(recipe)]
    )
    cli.main()
    assert calls == [(vm, Path("/helper.app"), 3), "reconnect"]
    assert json.loads(capsys.readouterr().out) == {"turn": 1}


@pytest.mark.parametrize("running", [True, False])
def test_a_failed_prepare_turn_still_restores_the_drive_forward(
    tmp_path,
    monkeypatch,
    capsys,
    running,
):
    # A turn reboots the Mac, then can fail on a station that does not come back ready;
    # the owner's next step is the drive handoff, which needs the forward.
    recipe = tmp_path / "recipe.toml"
    recipe.write_text("disk_gib = 64\nstations = 2\n")
    calls = []

    def reconnect():
        calls.append("reconnect")
        raise RuntimeError("reconnect also failed")

    monkeypatch.setattr(
        cli,
        "Sandbox",
        lambda config: SimpleNamespace(
            mac=SimpleNamespace(stations=(1, 2), _running=lambda: running),
            file_server=SimpleNamespace(reconnect=reconnect),
        ),
    )
    monkeypatch.setattr(cli.Config, "load", lambda path: object())

    def prepare(machine, app, recipe):
        raise RuntimeError("Station 1's helper session is not ready")

    monkeypatch.setattr(cli.mac_enrollment, "prepare", prepare)
    monkeypatch.setattr(
        sys, "argv", ["firm-sandbox", "mac-prepare", "/helper.app", "--recipe", str(recipe)]
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == (["reconnect"] if running else [])
    # The turn's own failure is what the owner reads, not the reconnect's.
    assert "Station 1's helper session is not ready" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["mac-status"],
        ["mac-recover", "--station", "1", "--confirm-cleanup"],
        ["mac-install-helper", "/x/Firm Station Helper.app"],
        ["mac-prepare", "/x/Firm Station Helper.app", "--recipe", "/x/r.toml"],
        ["mac-image-seal", "/x/sealed", "--recipe", "/x/r.toml"],
        ["mac-image-apply", "/x/r.toml"],
        ["mac-session-reset", "--station", "1"],
        ["mac-replace"],
    ],
)
def test_mac_verbs_refuse_without_a_mac(monkeypatch, capsys, arguments):
    monkeypatch.setattr(cli, "Sandbox", lambda config: SimpleNamespace(mac=None))
    monkeypatch.setattr(cli.Config, "load", lambda path: object())
    monkeypatch.setattr(sys, "argv", ["firm-sandbox", *arguments])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert "add [mac] to sandbox.toml" in capsys.readouterr().err
