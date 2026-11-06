"""Guest installation contracts which cross shell and Python assets."""

import re

from firm_sandbox.mac_station import ASSETS


def section(text, marker):
    return text.split(f"<<'{marker}'\n", 1)[1].split(f"\n{marker}\n", 1)[0]


def test_installer_gates_helpers_and_shells_by_group_with_account_paths():
    install = (ASSETS / "mac-station-install.sh").read_text()
    launcher = section(install, "LAUNCH")
    shell = section(install, "SHELL")
    for script in (launcher, shell):
        assert 'user=$(/usr/bin/id -un)' in script
        assert '-m "$user" firmstations' in script
    assert '--socket "/Users/Shared/firm-station-helper/$user/station.sock"' in launcher
    assert 'cd "$HOME/scratch" || exit 2' in shell
    assert '/var/log/firm-station/$user/commands.log' in shell
    assert '"$free_kib" -lt 20971520' in shell
    assert 'Match Group firmstations\n' in install
    assert 'Remove every station lease key before installing the helper.' in install
    assert 'AuthorizedKeysFile /etc/ssh/firm-lease-keys/%u' in install
    assert 'FirmStationFirstUID' in install
    assert 'vm-session' in install
    assert not re.search(r'\bfirmstation\b(?![-/])', launcher + shell)


def test_installer_holds_machine_lock_and_installs_the_authenticated_relay():
    install = (ASSETS / "mac-station-install.sh").read_text()
    assert "os.open('/var/db/firm/lock'" in install
    assert 'fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)' in install
    assert 'process.wait(timeout=600)' in install
    assert 'os.killpg(process.pid, signal.SIGKILL)' in install
    assert '/usr/bin/install -o root -g wheel -m 644 "$relay" ' in install
    assert '/usr/local/libexec/firm-station-relay.py' in install
    assert '/usr/bin/sudo -n -u "#$(($number + 501))" /bin/test ! -e' in install


def test_command_log_files_are_private_in_root_owned_directories():
    install = (ASSETS / "mac-station-install.sh").read_text()
    assert "root = '/var/log/firm-station'" in install
    assert "directory = f'{root}/firmstation{number}'" in install
    assert 'info.st_uid != 0 or info.st_mode & 0o022' in install
    assert 'os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600' in install
    assert 'not stat.S_ISREG(info.st_mode) or info.st_nlink != 1' in install
    assert 'os.fchown(fd, 0, 0)' in install
    assert 'os.fchmod(fd, 0o600)' in install

    assert "f'user:firmstation{number} allow append'" in install
