#!/bin/zsh
# Guest-only installation of an enrolled station's signed helper and its lease
# access: key-only SSH as the station account through a logging wrapper. No TCC writes.
set -eu
# Every program is named by its absolute path; the caller's PATH chooses nothing.
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
[ "$(/usr/sbin/sysctl -n kern.hv_vmm_present)" = 1 ]
[ "$(/usr/bin/id -u)" = 0 ]
if [ "${1:-}" != --machine-locked ]; then
  exec /usr/local/firm/bin/python3 -I - "$0" "$@" <<'PY'
import fcntl, os, signal, subprocess, sys
os.makedirs('/var/db/firm', mode=0o700, exist_ok=True)
fd = os.open('/var/db/firm/lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
process = subprocess.Popen(['/bin/zsh', sys.argv[1], '--machine-locked', *sys.argv[2:]],
                           start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=600))
finally:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
PY
fi
shift
[ "$(/usr/bin/id -u firmvm)" = 501 ]
station_count=$(/usr/local/firm/bin/python3 -I - <<'PY'
import json, os, pwd, stat
from pathlib import Path
path = Path('/var/db/firm-station/stations.json')
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
    raise SystemExit('Unsafe station inventory')
count = json.loads(path.read_text())['stations']
if type(count) is not int or not 1 <= count <= 8:
    raise SystemExit('Invalid station count')
for number in range(1, count + 1):
    account = pwd.getpwnam(f'firmstation{number}')
    if account.pw_uid != 501 + number or account.pw_dir != f'/Users/{account.pw_name}':
        raise SystemExit('Unexpected station identity')
print(count)
PY
)
for number in {1..$station_count}; do
  user=firmstation$number
  [ ! -e "/etc/ssh/firm-lease-keys/$user" ] && [ ! -L "/etc/ssh/firm-lease-keys/$user" ] || {
    echo 'Remove every station lease key before installing the helper.' >&2; exit 2
  }
  /usr/sbin/dseditgroup -o checkmember -m "$user" firmstations | /usr/bin/grep -q '^yes '
  if /usr/sbin/dseditgroup -o checkmember -m "$user" admin | /usr/bin/grep -q '^yes '; then
    echo 'Station accounts must be ordinary accounts.' >&2; exit 2
  fi
done
[ -x /usr/local/firm/session/.venv/bin/python3 ]
source_app=${1:?signed app path required}
# The platform installer is copied beside this script.
assets=${0:A:h}
add_platform="$assets/mac-station-add-platform.py"
relay="$assets/mac-station-relay.py"
[ ! -L "$add_platform" ]
[ -f "$add_platform" ]
[ ! -L "$relay" ]
[ -f "$relay" ]
mode=${2:?install mode required: update, enroll or development}
case "$mode" in update|enroll|development) ;; *) exit 2;; esac
app='/Applications/Firm Station Helper.app'
pending='/Applications/.Firm Station Helper.pending.app'
backup='/Applications/.Firm Station Helper.previous.app'
socket_root=/Users/Shared/firm-station-helper
agent=/Library/LaunchAgents/dev.firm.station-helper.plist
plist="$source_app/Contents/Info.plist"
[ ! -L "$source_app" ]
[ -d "$source_app" ]
/usr/bin/codesign --verify --strict "$source_app"
[ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$plist")" = dev.firm.station-helper ]
[ "$(/usr/libexec/PlistBuddy -c 'Print FirmStationEnvironment' "$plist")" = vm-session ]
[ "$(/usr/libexec/PlistBuddy -c 'Print FirmStationControlUID' "$plist")" = 501 ]
[ "$(/usr/libexec/PlistBuddy -c 'Print FirmStationFirstUID' "$plist")" = 502 ]
signature=$(/usr/bin/codesign -dv "$source_app" 2>&1)
if printf '%s' "$signature" | /usr/bin/grep -q 'Signature=adhoc'; then
  [ "$mode" = development ] || { echo 'Enrolled images require persistent signing.' >&2; exit 2; }
fi
if [ "$mode" = update ] && [ -d "$app" ]; then
  old_requirement=$(/usr/bin/codesign -d -r- "$app" 2>&1 | /usr/bin/sed -n '/^designated => /p')
  new_requirement=$(/usr/bin/codesign -d -r- "$source_app" 2>&1 | /usr/bin/sed -n '/^designated => /p')
  [ -n "$old_requirement" ] && [ "$old_requirement" = "$new_requirement" ] || {
    echo 'Signing identity changed; explicit enroll mode and new consent are required.' >&2; exit 2
  }
fi
# Never erase a dirty marker during an update or quietly kill tracked apps.
for number in {1..$station_count}; do
  /usr/bin/sudo -n -u "#$(($number + 501))" /bin/test ! -e \
    "$socket_root/firmstation$number/.firm-station-helper-ledger.json" || {
    echo 'A station helper has an unfinished application ledger; release it or replace the firm.' >&2; exit 2
  }
done
[ ! -e "$pending" ]
[ ! -L "$pending" ]
[ ! -e "$backup" ]
[ ! -L "$backup" ]
[ ! -L "$app" ]
[ ! -L "$socket_root" ]
[ ! -L "$agent" ]
/usr/bin/ditto "$source_app" "$pending"
/usr/sbin/chown -R root:wheel "$pending"
/bin/chmod -R go-w "$pending"
/usr/bin/codesign --verify --strict "$pending"
# The guest machine lock excludes station operations during this update.
for number in {1..$station_count}; do
  /bin/launchctl bootout gui/$((501 + number))/dev.firm.station-helper 2>/dev/null || true
done
[ ! -e "$app" ] || /bin/mv "$app" "$backup"
/bin/mv "$pending" "$app"
/usr/bin/install -d -o root -g wheel -m 755 /usr/local/libexec
[ ! -L /usr/local/libexec/firm-station-relay.py ]
/usr/bin/install -o root -g wheel -m 644 "$relay" /usr/local/libexec/firm-station-relay.py
# No privileged writes through the station account's home. The system LaunchAgent
# starts once and exits normally in every other account's Aqua login.
/bin/cat > /usr/local/libexec/firm-station-launch <<'LAUNCH'
#!/bin/sh
user=$(/usr/bin/id -un)
/usr/sbin/dseditgroup -o checkmember -m "$user" firmstations | /usr/bin/grep -q '^yes ' || exit 0
exec '/Applications/Firm Station Helper.app/Contents/MacOS/FirmStationHelper' \
  --socket "/Users/Shared/firm-station-helper/$user/station.sock"
LAUNCH
/usr/sbin/chown root:wheel /usr/local/libexec/firm-station-launch
/bin/chmod 755 /usr/local/libexec/firm-station-launch
/usr/local/firm/bin/python3 -I - "$station_count" <<'PY'
import grp, os, stat, sys
parent = os.open('/Users/Shared', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    try:
        os.mkdir('firm-station-helper', 0o755, dir_fd=parent)
    except FileExistsError:
        pass
    root = os.open('firm-station-helper', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                   dir_fd=parent)
    try:
        info = os.fstat(root)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('Unsafe helper socket root')
        for number in range(1, int(sys.argv[1]) + 1):
            name = f'firmstation{number}'
            try:
                os.mkdir(name, 0o755, dir_fd=root)
                created = True
            except FileExistsError:
                created = False
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                if created:
                    os.fchown(child, 501 + number, grp.getgrnam('firmstations').gr_gid)
                    os.fchmod(child, 0o755)
                elif os.fstat(child).st_uid != 501 + number:
                    raise SystemExit('Unexpected helper socket directory owner')
            finally:
                os.close(child)
    finally:
        os.close(root)
finally:
    os.close(parent)
PY
/usr/bin/install -d -o root -g wheel -m 755 /Library/LaunchAgents
/bin/cat > "$agent" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.firm.station-helper</string>
<key>ProgramArguments</key><array>
<string>/usr/local/libexec/firm-station-launch</string>
</array>
<key>LimitLoadToSessionType</key><string>Aqua</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
PLIST
/usr/sbin/chown root:wheel "$agent"
/bin/chmod 644 "$agent"
/usr/bin/plutil -lint "$agent" >/dev/null
for number in {1..$station_count}; do
  if /bin/launchctl print gui/$((501 + number)) >/dev/null 2>&1; then
    /bin/launchctl bootstrap gui/$((501 + number)) "$agent"
  fi
done
# Lease access: key-only SSH as the station account, every command logged, and the
# key installed per lease by the guest controller. Nothing is authorized until then.
/usr/bin/install -d -o root -g wheel -m 755 /etc/ssh/firm-lease-keys
/bin/cat > /usr/local/libexec/firm-lease-shell <<'SHELL'
#!/bin/sh
# ForceCommand for the leased station account: log the command, then run it.
# Only lease sessions find the station's Homebrew, after the system directories.
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/homebrew/sbin
export PATH
user=$(/usr/bin/id -un)
/usr/sbin/dseditgroup -o checkmember -m "$user" firmstations | /usr/bin/grep -q '^yes ' || exit 2
cd "$HOME/scratch" || exit 2
free_kib=$(/bin/df -k . | /usr/bin/awk 'END {print $4}')
if [ "$free_kib" -lt 20971520 ]; then
  printf 'Mac disk space is low: %s KiB free; save work to the drive.\n' "$free_kib" >&2
fi
if [ -z "${SSH_ORIGINAL_COMMAND:-}" ]; then
  exec /bin/zsh -l
fi
printf '%s %s\n' "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$SSH_ORIGINAL_COMMAND" \
  >> "/var/log/firm-station/$user/commands.log" || exit 2
exec /bin/zsh -lc "$SSH_ORIGINAL_COMMAND"
SHELL
/usr/sbin/chown root:wheel /usr/local/libexec/firm-lease-shell
/bin/chmod 755 /usr/local/libexec/firm-lease-shell
# The station account's zsh never reads startup files from its home, which every
# lease shares and can write, so one lease cannot plant code that runs in the next.
# sshd starts even the ForceCommand through the account's zsh (zsh -c), so the
# redirect is made in /etc/zshenv, the only file zsh reads before the home's: it
# points ZDOTDIR at a root-owned directory whose files set PATH and history.
[ ! -L /usr/local/libexec/firm-lease-zsh ]
/usr/bin/install -d -o root -g wheel -m 755 /usr/local/libexec/firm-lease-zsh
/bin/rm -f /usr/local/libexec/firm-lease-zsh/.zprofile /usr/local/libexec/firm-lease-zsh/.zshrc
/bin/cat > /usr/local/libexec/firm-lease-zsh/.zprofile <<'ZPROFILE'
# Lease login shells: after /etc/zprofile's path_helper, Homebrew follows the system directories.
path=(${path:#/opt/homebrew/*} /opt/homebrew/bin /opt/homebrew/sbin)
ZPROFILE
/bin/cat > /usr/local/libexec/firm-lease-zsh/.zshrc <<'ZSHRC'
# Interactive lease shells keep their history in the station home, as before.
HISTFILE=$HOME/.zsh_history
ZSHRC
/usr/sbin/chown root:wheel /usr/local/libexec/firm-lease-zsh/.zprofile /usr/local/libexec/firm-lease-zsh/.zshrc
/bin/chmod 644 /usr/local/libexec/firm-lease-zsh/.zprofile /usr/local/libexec/firm-lease-zsh/.zshrc
/usr/local/firm/bin/python3 -I - <<'PY'
import os, re
from pathlib import Path
path = Path('/etc/zshenv')
block = """# firm-lease begin
# The station account's zsh reads startup files from a root-owned directory, never its home.
if /usr/sbin/dseditgroup -o checkmember -m "$(/usr/bin/id -un)" firmstations |
    /usr/bin/grep -q '^yes '; then
  ZDOTDIR=/usr/local/libexec/firm-lease-zsh
fi
# firm-lease end
"""
if path.is_symlink():
    raise SystemExit('/etc/zshenv must not be a symlink')
text = path.read_text() if path.exists() else ''
text = re.sub(r'# firm-lease begin\n.*?# firm-lease end\n', '', text, flags=re.S)
path.write_text(block + text)
os.chown(path, 0, 0)
os.chmod(path, 0o644)
PY
# The station account's only sudo: one simulator platform at a time, with the image's
# Xcode. The drop-in is validated before it takes effect and must then be the rule
# sudo reports for the account.
[ ! -L /usr/local/libexec/firm-station-add-platform ]
/usr/bin/install -o root -g wheel -m 755 "$add_platform" /usr/local/libexec/firm-station-add-platform
/usr/local/firm/bin/python3 -I - "$add_platform" "$station_count" <<'PY'
import importlib.util, os, subprocess, sys
spec = importlib.util.spec_from_file_location('add_platform', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
directory = os.path.dirname(module.SUDOERS_FILE)
if os.path.islink(directory):
    raise SystemExit('The sudoers drop-in directory must not be a symlink')
os.makedirs(directory, mode=0o755, exist_ok=True)
if os.stat(directory).st_uid != 0 or os.stat(directory).st_mode & 0o022:
    raise SystemExit('The sudoers drop-in directory is not root-owned')
# A name with a dot is never read by sudo's includedir, so the draft cannot take effect.
draft = os.path.join(directory, '.firm-station.new')
fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o440)
with os.fdopen(fd, 'w') as stream:
    stream.write(module.SUDOERS)
    stream.flush()
    os.fsync(stream.fileno())
os.chown(draft, 0, 0)
os.chmod(draft, 0o440)
checked = subprocess.run(['/usr/sbin/visudo', '-c', '-f', draft], capture_output=True, text=True)
if checked.returncode:
    os.unlink(draft)
    raise SystemExit('The station sudoers drop-in does not validate: ' + checked.stderr.strip())
os.replace(draft, module.SUDOERS_FILE)
for number in range(1, int(sys.argv[2]) + 1):
    rules = subprocess.run(['/usr/bin/sudo', '-n', '-l', '-U', f'firmstation{number}'],
                           capture_output=True, text=True, check=True).stdout
    # Without a terminal sudo wraps the listing on spaces.
    rules = ' '.join(rules.split())
    if not all(f'{module.WRAPPER} {name}' in rules for name in module.PLATFORMS):
        raise SystemExit('sudo does not read the station drop-in; check includedir in /etc/sudoers')
PY
/usr/local/firm/bin/python3 -I - "$station_count" <<'PY'
import os, stat, subprocess, sys
root = '/var/log/firm-station'
if os.path.islink(root):
    raise SystemExit('Unsafe command log root')
os.makedirs(root, mode=0o755, exist_ok=True)
if os.stat(root).st_uid != 0 or os.stat(root).st_mode & 0o022:
    raise SystemExit('Unsafe command log root')
for number in range(1, int(sys.argv[1]) + 1):
    directory = f'{root}/firmstation{number}'
    if os.path.islink(directory):
        raise SystemExit('Unsafe station command log directory')
    os.makedirs(directory, mode=0o755, exist_ok=True)
    info = os.stat(directory)
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Unsafe station command log directory')
    fd = os.open(f'{directory}/commands.log', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit('Unsafe station command log')
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    log = f'{directory}/commands.log'
    subprocess.run(['/bin/chmod', '-N', log], check=True, capture_output=True, timeout=10)
    subprocess.run(['/bin/chmod', '+a', f'user:firmstation{number} allow append', log],
                   check=True, capture_output=True, timeout=10)
PY
# The block goes at the end of sshd_config: a Match block in an included file
# would swallow the main file's later global directives.
/usr/local/firm/bin/python3 - <<'PY'
import re
from pathlib import Path
path = Path('/etc/ssh/sshd_config')
block = """# firm-lease begin
Match Group firmstations
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY yes
    PermitUserRC no
    AuthorizedKeysFile /etc/ssh/firm-lease-keys/%u
    ForceCommand /usr/local/libexec/firm-lease-shell
# firm-lease end
"""
text = re.sub(r'# firm-lease begin\n.*?# firm-lease end\n', '', path.read_text(), flags=re.S)
if not text.endswith('\n'):
    text += '\n'
path.write_text(text + block)
PY
/usr/sbin/sshd -t
if /usr/sbin/dseditgroup -o read com.apple.access_ssh >/dev/null 2>&1; then
  /usr/sbin/dseditgroup -o edit -a firmstations -t group com.apple.access_ssh
fi
# macOS answers membership from a cache that a nested group edit does not refresh: a
# station stayed "not a member" of Remote Login's list, and its lease session was closed.
/usr/bin/dsmemberutil flushcache
# Backup contains only the prior helper application, never station account data.
[ ! -e "$backup" ] || /bin/rm -rf "$backup"
echo 'Installed the station helper and lease access. Check readiness; first signing identity requires guest consent.'
