"""The station helper and the Python side name the same helper.

The helper is Swift, built and signed by hand, and nothing imports across the two;
they meet only through baked-in names. This reads the helper's sources and the
guest assets as text, so it needs neither Swift nor macOS.
"""

import re
from pathlib import Path

import pytest

from firm_sandbox import mac_enrollment, mac_image, mac_station, mac_vm_guest

REPOSITORY = Path(__file__).resolve().parents[3]
HELPER = REPOSITORY / "station-helper"
ASSETS = mac_station.ASSETS
BUNDLE_ID = "dev.firm.station-helper"
APP = "Firm Station Helper.app"
EXECUTABLE = "FirmStationHelper"
PLIST_KEYS = {"FirmStationEnvironment", "FirmStationControlUID", "FirmStationFirstUID"}
SOCKET_ROOT = "/Users/Shared/firm-station-helper"
LEDGER = ".firm-station-helper-ledger.json"


@pytest.fixture
def helper():
    if not HELPER.is_dir():
        pytest.skip(f"the station helper sources are not at {HELPER}")
    return HELPER


def found(pattern, text, *, group=1):
    """Every match of ``pattern``; a pattern that matches nothing is itself stale."""
    matches = {match.group(group) for match in re.finditer(pattern, text)}
    assert matches, f"no match for {pattern!r}"
    return matches


def read(path):
    return Path(path).read_text()


def test_bundle_identity_matches_the_build_installer_seal_and_guest(helper):
    build = read(helper / "build.sh")
    install = read(ASSETS / "mac-station-install.sh")
    seal = read(ASSETS / "mac-station-seal.py")
    assert found(r"(?m)^helper_identifier=(\S+)$", build) == {BUNDLE_ID}
    assert found(r"'Print CFBundleIdentifier' \"\$plist\"\)\" = (\S+) \]", install) == {BUNDLE_ID}
    assert found(r"<key>Label</key><string>([^<]+)</string>", install) == {BUNDLE_ID}
    assert found(r"/Library/LaunchAgents/([^'\"\s]+)\.plist", install + seal) == {BUNDLE_ID}
    assert Path(f"/Library/LaunchAgents/{BUNDLE_ID}.plist") in mac_vm_guest.SYSTEM_ROOTS


def test_app_and_executable_names_match_everywhere(helper):
    build = read(helper / "build.sh")
    install = read(ASSETS / "mac-station-install.sh")
    relay = read(ASSETS / "mac-station-relay.py")
    seal = read(ASSETS / "mac-station-seal.py")
    assert found(r"helper_app=\"\$helper_build/([^\"]+)\"", build) == {APP}
    assert found(r"<key>CFBundleName</key><string>([^<]+)</string>", build) == {
        APP.removesuffix(".app")
    }
    assert found(r"<key>CFBundleExecutable</key><string>([^<]+)</string>", build) == {EXECUTABLE}
    assert found(r"-o \"\$helper_app/Contents/MacOS/(\w+)\"", build) == {EXECUTABLE}
    assert found(r"/Applications/([^.'\"/][^'\"/]*\.app)", install + relay + seal) == {APP}
    assert found(r"\.app/Contents/MacOS/(\w+)", install + relay) == {EXECUTABLE}
    assert found(r"app\.name != \"([^\"]+)\"", read(mac_enrollment.__file__)) == {APP}
    assert Path("/Applications") / APP in mac_vm_guest.SYSTEM_ROOTS


def test_info_plist_keys_match_the_build_helper_and_installer(helper):
    build = read(helper / "build.sh")
    swift = read(helper / "StationHelper.swift") + read(helper / "LaunchPolicy.swift")
    install = read(ASSETS / "mac-station-install.sh")
    assert found(r"<key>(FirmStation\w+)</key>", build) == PLIST_KEYS
    assert found(r"forInfoDictionaryKey: \"(FirmStation\w+)\"", swift) == PLIST_KEYS
    assert found(r"'Print (FirmStation\w+)'", install) == PLIST_KEYS
    environment = found(r"(?m)^helper_environment=(\S+)$", build)
    assert found(r"'Print FirmStationEnvironment' \"\$plist\"\)\" = (\S+) \]", install) == (
        environment
    )
    assert f'!= "{environment.pop()}"' in read(mac_station.__file__)


def test_role_reports_redact_the_helper_configured_uids(helper):
    mac_lease = pytest.importorskip("firm_runtime.mac_lease")
    configured = found(r"\"(configured_\w+_uid)\"", read(helper / "StationHelper.swift"))
    assert configured == {"configured_control_uid", "configured_station_uid"}
    report = mac_lease.public_result({name: 501 for name in configured} | {"ready": True})
    assert report == {"ready": True}


def test_socket_and_ledger_names_match_the_installer_relay_and_guest(helper):
    install = read(ASSETS / "mac-station-install.sh")
    relay = read(ASSETS / "mac-station-relay.py")
    assert f'--socket "{SOCKET_ROOT}/$user/station.sock"' in install
    assert f'connection.connect(f"{SOCKET_ROOT}/firmstation{{station}}/station.sock")' in relay
    assert f'"socket": f"{SOCKET_ROOT}/{{user}}/station.sock"' in read(mac_vm_guest.__file__)
    assert found(r"(?m)^socket_root=(\S+)$", install) == {SOCKET_ROOT}
    assert found(r"\"(\.firm-[\w-]+\.json)\"", read(helper / "Ledger.swift")) == {LEDGER}
    assert found(r"\"(\.firm-[\w-]+)\.lock\"", read(helper / "UnixSocketListener.swift")) == {
        LEDGER.removesuffix("-ledger.json")
    }
    assert LEDGER in install
    assert '"--verify-peer", str(connection.fileno()), str(501 + station)' in relay
    assert 'let station = uid_t(args[2]), accounts.contains(station)' in read(helper / "Main.swift")


def test_helper_accepts_each_station_uid_without_requiring_the_console(helper):
    policy = read(helper / "LaunchPolicy.swift")
    assert "uid >= first && uid - first < 8" in policy
    issue = policy.split("func sessionIssue(", 1)[1].split("\n}", 1)[0]
    assert "screenLocked" in issue and "setupAssistantRunning" in issue
    assert "onConsole" not in issue and "consoleUID" not in issue


def test_helper_operations_are_the_ones_the_sandbox_relays(helper):
    operations = found(r"(?m)^\s+case \"(\w+)\":", read(helper / "Main.swift"))
    assert operations == mac_station.OPERATIONS
    assert mac_station.RECOVERY_OPERATIONS <= operations


def test_role_facing_operations_are_the_sandbox_operations_but_lease_binding():
    mac_lease = pytest.importorskip("firm_runtime.mac_lease")
    # The sandbox binds the helper itself when it connects a lease; roles get
    # every other operation.
    assert mac_lease.OPERATIONS == mac_station.OPERATIONS - {"bind_lease"}


def test_helper_lease_binding_reply_matches_the_host_consumers(helper):
    mac_lease = pytest.importorskip("firm_runtime.mac_lease")
    swift = read(helper / "StationHelper.swift")
    status = swift.split("func status()", 1)[1].split("private var heldLeaseIssue", 1)[0]
    bind = swift.split("func bindLease(", 1)[1].split("func launch(", 1)[0]
    assert found(r'"(\w+_bound)":', status) == {"lease_bound"}
    assert found(r'"(\w+_bound)":', bind) == {"lease_bound"}
    for module in (mac_station, mac_image, mac_lease):
        assert found(r'\.get\("(\w+_bound)"\)', read(module.__file__)) == {"lease_bound"}


def test_helper_input_budget_fits_the_relay_request_deadline(helper):
    # The helper sleeps through a held click, a paced drag and a tap burst; the relay is
    # waiting on the same request meanwhile.
    values = read(helper / "RequestValues.swift")
    budget = found(r"let maximumIntentMilliseconds = ([\d_]+)", values)
    assert int(budget.pop().replace("_", "")) < mac_station.REQUEST_SECONDS * 1000


def test_role_facing_input_limit_is_the_helper_limit(helper):
    mac_lease = pytest.importorskip("firm_runtime.mac_lease")
    limit = found(r"let maximumInputBytes = (\d+)", read(helper / "RequestValues.swift"))
    assert limit == {str(mac_lease.MAX_DESKTOP_INPUT_BYTES)}
