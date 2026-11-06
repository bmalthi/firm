# The Mac stations

> **Quick Reference — Optional Mac Desktop Leases**
> * **Optional Capability (`[mac]` in `sandbox.toml`):** Firms without `[mac]` run entirely on the Linux VM and do not need this guide. Adding `[mac] image = "/path/to/enrolled-image"` attaches one shared macOS VM (default 8 vCPUs, 24 GiB RAM) carrying `1 to 8` desktop accounts (`firmstation1..8`).
> * **On-Demand Leases (`mac_acquire` / `mac_release`):** Any Linux role can lease one free station at a time over an ephemeral SSH tunnel (`127.0.0.1:2221 + n`) to run `xcodebuild`, Device Hub / Simulator, or GUI automation via the signed Swift **Station Helper**.
> * **4-Check Verified Release:** A released station cannot be leased to another role until the sandbox verifies `access_revoked`, `work_stopped`, `drive_detached`, and `desktop_usable` against the pre-lease baseline.

To build, enroll, sign, seal, or update the reusable Mac image, see the [Mac VM image guide](mac-vm-images.md). The [sandbox design](../design/sandbox.md#the-mac-lease) states the lease contract and rationale. (When installed via `uv tool install`, invoke `firm` or `firm-sandbox` directly; inside a repository checkout, prefix with `uv run`.)

## Contents

- [Add the Mac to a firm](#add-the-mac-to-a-firm)
- [Prepare the Mac image](#prepare-the-mac-image)
- [The Mac lease](#the-mac-lease) ([The host's path into the Mac](#the-hosts-path-into-the-mac), [What a lease may install](#what-a-lease-may-install), [Recovering a blocked station](#recovering-a-blocked-station), [Sessions and the screen lock](#sessions-and-the-screen-lock))
- [Network](#network)
- [What the stations are not](#what-the-stations-are-not)
- [Development limits](#development-limits)

## Add the Mac to a firm

Name an enrolled image in the firm's `sandbox.toml`; `firm start` or `firm-sandbox up`
then boots its Mac VM alongside the Linux VM, and the roles' `mac_acquire` starts
answering granted or busy instead of `unavailable` with reason `no_mac`:

```toml
[mac]
image = "/absolute/path/to/enrolled-image"
```

That is one Mac of 8 CPUs and 24 GiB, carrying every station the image records; `cpus`
and `memory_gib` may be set, `stations` may not (the count is the image's, from its
recipe, and `Config.load` refuses the key). Stations are numbered from 1. All of them
share the machine: its CPUs, memory, disk, Homebrew, `/tmp` and loopback ports. What
they do not share is a desktop, a home, a drive mount and a helper. `firm-sandbox
mac-status` and the other `mac-*` verbs refuse without `[mac]`; `mac-image-build` needs
no firm at all.

`up` refuses a changed `mac.image`. To move an existing firm onto a different enrolled
image, edit `mac.image` and, with the firm stopped, run `firm-sandbox mac-replace`: it
refuses while the firm is running, checks the newly configured image, closes any lease
tunnel a station's records still name, and removes the Mac VM with its disk, its lease
records and its cleanup markers. The Linux VM and its disk are untouched, and the next
`up` clones the new image as a first `up` does. Every step repeats, so an interrupted
`mac-replace` is finished by running it again. `replace` also adopts a new image, but
discards Linux with it; `mac-replace` is the narrower adoption.

A clone's first `up` installs the guest controller, finishes any password change an
earlier boot left half done, and then gives `firmvm` its own random password through
the controller, so no two clones of one image share a control password. Every boot
restates the machine's sleep settings and each station's screen lock, screen saver and
relock-at-login, because a clone has been seen to lose them and the lock is kept per
host identity.

macOS can expose `/Volumes/cidata` before the ISO's guest agent is executable, and
Lima's own init script then fails and `up` times out waiting for boot completion; with
12 CPUs it lost that race on every boot. Each `up` keeps a root:wheel 0755 init script
that waits up to 60 seconds for the agent, and during start a host watcher allows 10
seconds after SSH and agent readiness, then runs `fake-cloud-init` once as root if Lima's
boot marker is still absent, so a clone of an older image boots too.

## Prepare the Mac image

Find and reuse a suitable enrolled image before building a new one. The image guide
covers [image discovery and base builds](mac-vm-images.md#find-and-reuse-an-image),
[station enrollment](mac-vm-images.md#station-enrollment), runtime installation,
sealing and clone verification. Once enrolled, add its absolute path under `[mac]` as
described above. Keep the image directory outside each firm's directory.

## The Mac lease

Any role may call `mac_acquire`; it tries each station in order and grants the first free one (so a station that is dirty, awaiting recovery, or too full does not block the others):

| `mac_acquire` Outcome | Cause / Meaning | What Happens Next |
| :--- | :--- | :--- |
| **`granted`** | A free station passed preflight and mounted the role's drive. | Role receives SSH tunnel (`127.0.0.1:2221 + n`), key path, and desktop tools. |
| **`busy`** | All healthy stations are currently leased by other roles. | Returns current holders and lease start times; role can wait or do Linux work. |
| **`unavailable` (`mac_disk_full`)** | Every tried station reclaimed caches and still measured `< 5 GiB` free on the shared disk. | Recorded as `mac.refused` and a `disk_full` row in `firm analyze --section mac`. |
| **`pending` (`mac_not_enrolled` / `mac_needs_recovery` / `mac_session_not_ready`)** | Only the owner can ready the Mac (enroll the image, run `mac-recover`, or reset the session). | Opens a unified [owner request](runtime.md#owner-requests) in **Overview → Needs your decision**; granting wakes all waiting roles. |

When more than one station refuses for
a different reason, busy is reported first (it may free itself), then needing recovery
(the owner already has to look), then unready, then a full station (it has just
reclaimed and measured). An activation holds at most one station at a time,
and a later lease may land on another one, so a role keeps state it needs again on the
drive rather than in a station's home. What a lease leaves where:

- The lease record and baseline: `durable/mac/stations/<n>/leases/<lease_id>.json`, and
  the dirty marker `durable/mac/stations/<n>/mac-station-dirty`, from acquire until a
  clean release or recovery.
- The drive: mounted at `/Users/firmstation<n>/drive` with the holder's own file server
  login, as the station's UID through a pseudo-terminal `mount_smbfs`. macOS's SMB
  client performs `rename(2)` as a lookup, a delete and then a rename with the
  protocol's replace-if-exists flag off, so the call is not atomic: concurrent writers
  renaming onto one destination — a compiler's content-addressed index store, any
  shared cache — fail with `EEXIST` on a different file each time, and an interrupted
  rename can leave neither file in place. The Linux mount does not share the fault.
  Roles are told to check out into their Mac home and build there
  ([workplace rules](../design/prompting.md#resources)). The share reaches the Mac
  through one tunnel from the host; when that path stalls (below) the mount reconnects
  on its own, and a mount whose connection timed out at acquire is tried once more.
- The lease key: public half at `/etc/ssh/firm-lease-keys/firmstation<n>` on the Mac,
  private half written as the role, create-only and `0600`, at
  `/run/firm/<role>/credentials/mac-<lease_id>` on the Linux VM; the runtime never sees
  it.
- The lease tunnel: an SSH reverse forward named `mac-lease-<lease_id>` on the Linux
  VM, to the Mac's one sshd from the station's own port in the Linux guest,
  `127.0.0.1:2221 + n`.
- The role receives `station`, `ssh_host` `127.0.0.1`, `ssh_port` (`2222` for
  station 1, `2223` for station 2, and so on), `user`, `key_path`, `drive`,
  `project_dir` (`/Users/firmstation<n>/drive/org/<address>/work`) and `scratch`
  (`/Users/firmstation<n>/scratch`, recreated empty at every release). Acquire and
  release both also carry `disk` (the Mac's data volume, free of total, at that moment;
  machine-wide, shared by every station) and `disk_reclaimed` (bytes that call's own
  `station-reclaim` removed, logical, not guest or host space regained; null when
  reclaim did not run).
- Desktop captures: each successful `desktop_capture` is saved by the runtime, through
  the Linux VM's file server, at `org/<address>/work/captures/<UTC time>-<app>.png` on
  the drive; it outlives the lease like any other work there. The helper writes no file.

The image's sshd block for the `firmstations` group allows public-key authentication
only, no forwarding of any kind, a TTY, `AuthorizedKeysFile /etc/ssh/firm-lease-keys/%u`
and `ForceCommand /usr/local/libexec/firm-lease-shell`. The wrapper sets `PATH` to
`/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/homebrew/sbin` (a login shell's
`path_helper` then puts the system's `/etc/paths` entries first and keeps Homebrew after
them). It runs a login shell for an empty `SSH_ORIGINAL_COMMAND`; otherwise it appends
the command with a timestamp to the station's root-owned
`/var/log/firm-station/firmstation<n>/commands.log` and runs it through `/bin/zsh -lc`.
The block also sets `PermitUserRC no`, and lease shells never read zsh startup files
from a station home: the installer puts a block at the top of `/etc/zshenv` that, for
any member of `firmstations`, points `ZDOTDIR` at the root-owned
`/usr/local/libexec/firm-lease-zsh`, whose `.zprofile` keeps Homebrew after the system
directories and whose `.zshrc` keeps history in `~/.zsh_history`. It has to be
`/etc/zshenv` because sshd starts even the `ForceCommand` through the account's
`zsh -c`, which reads `.zshenv` before the wrapper runs. The system's own `/etc/zprofile`
and `/etc/zshrc` still run. A `~/.zshenv`, `~/.zprofile`, `~/.zshrc` or `~/.ssh/rc` a
lease leaves behind therefore never runs in a later lease's session; programs it leaves
in the home or in Homebrew still can, when a later lease runs them
([why that remains](../design/sandbox.md#the-mac-lease)). At release the sandbox copies
that log to `durable/mac/stations/<n>/leases/<lease_id>.log` and empties it.

### Release Sequence & 4-Check Verification

When a role calls `mac_release` (or its activation ends), the sandbox runs a strict per-UID cleanup sequence (leaving other stations untouched):
1. **Revoke SSH access:** Close the lease tunnel, verify its control socket is gone, remove the temporary SSH key, and kill all `sshd` sessions for the station UID.
2. **Stop apps, simulators, and background work:** Run helper `release`, shut down and delete the station's Simulator device sets (`simctl`), boot out non-baseline `launchd` services/LaunchAgents/crontabs, and kill any remaining station processes not in the pre-lease baseline.
3. **Unmount & reclaim:** Unmount `~/drive` and run `station-reclaim` (which refuses to run if `~/drive` is still mounted) to wipe `~/scratch`, `$TMPDIR`, and Xcode/Library caches.
4. **Verify all 4 checks (`access_revoked`, `work_stopped`, `drive_detached`, `desktop_usable`):** Run a fresh helper preflight and final assessment, copy the command log to `durable/mac/stations/<n>/leases/<lease_id>.log`, and clear `mac-station-dirty` only if every check is verified clean.

For `work_stopped`, the cleanup
report records every tolerated process with its service and definition path (an
XProtect remediator, which launchd does not manage, with its executable and its parent
plugin service instead), the trusted registrations in `services.tolerated`, and the pid,
start time and error of each process that refused termination in `processes.kill_errors`.
It also reports `disk` and `disk_reclaimed`: reclaiming less than hoped is only ever
evidence, never a failed check, but a reclaim the host could not confirm at all — a lost reply,
an unreaped worker — counts the same as any other unverified step. Both `reclaim` and `simulators` outcomes are recorded in the `mac.released` sandbox event and `firm analyze --section mac`.

Any failed check, or a reclaim the host could not confirm, leaves the dirty marker and the
lease record in place, and no new lease is granted on that
station until release or [recovery](#recovering-a-blocked-station) succeeds. The runtime automatically releases any lease whose activation is no longer admitted or running.

`firm-sandbox mac-status` reports `configured` (the firm has `[mac]`) and `enrolled`
(the image records a sealed enrollment), plus a `stations` list with one row per
station: `held`, `lease_id`, `holder`, `since` and `dirty`, a fresh helper readiness
probe and that station's `awake` settings (`screen_lock`, `power`, `relock_at_login`,
`screen_saver_idle`) when it is not leased. Both probes take the station lock, so during
a lease each reports `error` instead; on a stopped Mac each reports `stopped`.
`mac-status --station N` reports just that station; omitted, every station is reported.
`mac-recover` and `mac-session-reset` act on one station and require `--station N`;
`mac-restart` restarts the whole Mac and restores every station's session, keeping
Linux running.

### The host's path into the Mac

Everything the host does in the guest — the controller's RPCs, the helper relays, the
file-server tunnel and the lease tunnels — goes through Lima's forward of the guest's
ssh port on the host's loopback. Under two stations' load that forward has been seen to
stop accepting new connections for fifteen to twenty-five seconds after a burst of
short ones, with the guest itself running normally and connections already established
unaffected. The firm rides that out rather than failing a release for it: helper relays
are channels on one persistent connection, the tunnels tolerate thirty seconds of
silence, a drive mount that timed out is tried again, and a release's listings and
sweep give each read and cleanup opportunity forty-five seconds. The sweep has one
360-second deadline: four parts times a forty-five-second cleanup and read allowance,
retaining tolerance for stalls while removing stacked passes. The host allows 480
seconds: 360 for the sweep, 75 for simulator cleanup, 20 for the station lock and 25
for transport and reporting. While the file server's tunnel is stalled, the drive's SMB
session reconnects and anything touching the drive blocks until it does;
`ps` blocks behind such a process, which is why those budgets are as long as they are.
The final verification RPC allows 285 seconds: four 45-second work reads, a 45-second
session listing, a 5-second mount read and 20 seconds for locks total 250 seconds,
leaving 35 for transport and reporting. Reclaim allows 110 seconds: its 60-second
worker and 20 seconds for locks leave 30 for transport and reporting.
Session reset allows 210 seconds: 45 for
sessions, 20 for unmount and its checks, 80 for ending and recreating the desktop and
20 for locks total 165 seconds, leaving 45 for transport and reporting.

### What a lease may install

A station account is an ordinary account, not an administrator, and owns its home; the
`firmstations` group owns the image's Homebrew. A holder installs formulae with `brew
install` or tools into the home without sudo. Homebrew installs and simulator runtimes
persist for the whole Mac. A station keeps its preferences, keychain, grants and
standard home folders, such as `~/Applications` and `~/Documents`; release reclaims
simulator devices, DerivedData and the other Library caches, top-level home additions,
scratch, and the contents of the station user's Darwin temp directory (`.../T`, its
`$TMPDIR`), resolved as the station UID. That is where `simctl delete` actually leaves
deleted simulators, as `Deleting-*` trees whose background removal release stops; the
directory itself, the Darwin cache directory (`.../C`, still used by the logged-in
session) and other users' directories stay. Temp bytes appear under `temp` and count in
`disk_reclaimed`.
What stays in the home is that station's, and a later lease may be granted
another station; what lands in Homebrew is every station's, and every role in the firm
shares it ([why](../design/sandbox.md#the-mac-lease)). A service started with `brew services`
is lease work: its `homebrew.mxcl.*` LaunchAgent and program are booted out, removed
and killed at release like any other.

Simulator platforms are system-wide, so a station account adds one through the single
sudo rule it has:

```sh
sudo -n /usr/local/libexec/firm-station-add-platform iOS
```

Simulator has moved to Device Hub in Xcode
([Apple's guide](https://developer.apple.com/documentation/xcode/managing-your-simulated-and-physical-devices-in-device-hub)).
The image carries no `Simulator.app`; `DeviceHub.app` ships inside Xcode at
`/Applications/Xcode.app/Contents/Applications/DeviceHub.app`, and its window is what
the desktop tools launch, click and capture. It opens on a placeholder: the device's
screen appears once the device is selected, which `desktop_inspect` finds as the entry
named for the device and `desktop_action` presses, and it then sits centred in the
window's content area beside the sidebar. Launch Device Hub with `launch_mode:"direct"`.
Inspection returns partial trees when its three-second, element or depth limit is
reached, with `truncation_reasons`. Pass a returned element `handle` to inspect that
branch; Device Hub's sidebar `AXOutline` exposes its device rows this way. Home,
rotation, orientation, appearance, a
screenshot, screen recording and locking the screen are menu items the same two tools
reach; a shake is not among them and stays `XCUIDevice`'s. The pointer tools send one
pointer: a click or drag can hold a modifier, hold its press and pace its movement, and
`desktop_tap_sequence` posts a short planned run of taps and returns its posting times;
these do not guarantee when the simulated app receives the input. The
[Device Hub acceptance check](../tools/acceptance/mac/README.md) measures both ends
with monotonic app receipt times and requires the app's touch cadence to match within
150 ms. Device Hub can still deliver taps in bursts even when the helper posts near
the requested cadence. The desktop tools cannot make a two-finger gesture or
guarantee app receipt timing.
Run XCTest UI tests through the lease shell for app timing and multi-touch assertions.
The host acceptance driver `ui_test_play.py` demonstrates this: its XCTest target drives
a pinch, long press and velocity swipe through the simulator's touch pipeline, reads
the app's gesture-recognizer readout, covers idle and continuously rendering phases,
and times each gesture call. What a
role opens through them stays open for the rest of its lease: the helper is bound
to the firm, role and lease through `bind_lease`, and the relay connection, which
the runtime reopens when its 30-minute deadline passes, presents that binding and
adopts the binding. Helper status reports this as `lease_bound`; `session` describes
the macOS login session. `desktop_close_apps` and `mac_release` close the applications. The
helper only ever drives applications it launched itself: it refuses to adopt one that
was already running in the station's session.

The installer, `/usr/local/libexec/firm-station-add-platform`, is root-owned and
installed with the helper. From a lease it accepts exactly one argument, `iOS`,
`watchOS`, `tvOS` or `visionOS`, requires root, changes to `/`, finds the selected Xcode
in `/Applications` through `xcode-select` under a fixed environment, checks that
`xcodebuild` and every directory above it up to the `.app` bundle is no symlink, is
owned by root or `firmvm` (the build copies Xcode as `firmvm`) and is writable by no
group or other, and runs `xcodebuild -downloadPlatform <platform>`; it passes nothing
else through. One install runs at a time in a Mac, whichever station asked: it holds a
lock while the download lives, a second caller is told one is still running, and it
stops a download that passes 110 minutes. The drop-in `/etc/sudoers.d/firm-station`
(mode 440) grants each station account `NOPASSWD` for the installer with each platform
spelled out, states `env_reset` (sudo's default, so the caller's environment is
dropped) and needs no TTY, so it works over a lease's SSH commands. The installer runs
as root and the runtime mounts through the system's `simdiskimaged`, so neither is a
station process the release checks count; the runtime stays installed afterwards, for
every station. Any other command or argument, including `sudo -n true`, is refused. It
is the route to a system-wide install of the image's current platform, not the only
route to a runtime: a lease shell can run the selected Xcode's own `xcodebuild` with no
privilege, and `-downloadPlatform iOS -buildVersion 26.0` resolves an older runtime and
downloads it without an Apple ID.

### Recovering a blocked station

When a release left a check failed, `dirty:true` blocks new leases on that station
only; the Mac's other stations are unaffected. Inspect the Mac, end the activity the
release reported, then run:

```sh
uv run firm-sandbox --config /absolute/firm/sandbox.toml mac-recover --station N --confirm-cleanup
```

The flag confirms that inspection and cleanup; it does not perform them. Recovery
requires the running Mac VM. It takes the station lock, verifies the same four checks
against the last lease's baseline, closing a leftover lease tunnel as part of
`access_revoked`, terminates no station process, reclaims once the four checks hold
(never before, so a station not yet verified stopped is not touched), and clears the
marker and the record only when all four hold and the reclaim itself was confirmed. A
failed check, or an unconfirmed reclaim, leaves everything in place. A failed read
appears in the structured checks with its error, including a lost verification RPC;
it does not hide the checks that could be read. A lease that was
never released — a host process killed mid-lease — still holds its key and its mount,
so recovery refuses it; release the lease by its ID first (the acceptance tools do this
through `Sandbox.mac_release`), then recover if the station is still dirty. Replace the
firm when cleanup cannot be verified. The runtime records the station recovered on its
next tick and answers every role whose `mac_acquire` was waiting on recovery, so a
`firm requests grant` is not needed for those; it is still enough on its own.

A system consent or authorization alert left in a station's session, such as a Network
Volumes prompt raised by an app a lease launched, outlives that app and blocks the
access it guards for the next holder of that station. While one is on screen the helper
reports `ready:false`, so `desktop_usable` fails and the station stays dirty. Answer it
in the VM window — switch to that station's session with fast user switching — then
recover; nothing answers it automatically.

### Sessions and the screen lock

A station's desktop is an off-console session. The host makes it with a seconds-long
Screen Sharing login over the guest's own loopback, as that station with its recorded
password, then drops the connection; capture, input and accessibility stay in the
helper, which reports the session complete and unlocked. Screen Sharing admits the
`firmstations` group only, never `firmvm`; Remote Management stays off, because with it
on every local user is admitted whatever the group says. The console stays with
`firmvm`, which auto-logs in.

`mac-session-reset --station N` ends that station's session and makes it again; it
refuses during a lease and is the answer to a session the helper reports unready. It
ends only `gui/<uid>`, leaving `user/<uid>` services to launchd, and gives its whole
deadline to the new session rather than to reading the station: what it still runs is
the sweep's to report. A retained lease then gets the same cleanup and final assessment
as release; a later complete, clean read supersedes an earlier error, but current
unreadable state keeps its record and dirty marker. The screen lock is turned off for
every station at every boot; a station that still asks
for a password at its lock screen is answered by `mac-restart`, not by typing. A
station's password is 48 random hexadecimal characters, root-private in the guest and
never typed; the account rejects a wrong one, macOS locks the account out after four
wrong ones, and the controller stops trying after two, so a session that will not log
in is reported (`login_blocked`) rather than driving the account into a lockout.
Passwords are short and typeable only during an enrollment console turn (see the
[Mac VM image guide](mac-vm-images.md#console-turns)).

`desktop_launch` defaults to `launch_mode:"launch_services"`, the standard macOS launch
path. Each app is responsible for access to its own protected data: Chrome needs this
to open its profile. Use `launch_mode:"direct"` when a tool must inherit the helper's
Network Volumes grant to open the shared drive. Direct mode makes the helper responsible
for the child's data access, so it is unsuitable for apps with protected profiles.
Prefer working in lease scratch and copying deliverables back through the lease shell.
Apple platform apps such as TextEdit require LaunchServices; direct mode is refused
based on the code signature's platform identifier. Xcode's Device Hub is not platform
constrained: use direct mode to retain its process identity through its trampoline.
The helper allows 20 seconds for
the launch and AppKit registration within
the station relay's 30-second request deadline. A failed launch stops what it started
and drops its ledger entry. An app the helper cannot verify gone stays in its ledger:
the helper keeps answering, reports `ready:false` and refuses new bindings until a
release, or `mac-recover` once the app has stopped, verifies it gone. A lease whose
applications are still open refuses another lease's binding the same way; its own
release closes them.

## Network

The Mac reaches the internet the way the Linux VM does: by name, through the host's
proxy, with the same allowlist and cutoff (see the
[Linux VM guide](linux-vm.md#network-access-and-cutoff)). Each boot sets the system
web and secure web proxy on every network service, points DNS at nothing, and gives
SSH sessions the proxy variables, so browsers, `brew`, `xcodebuild` downloads and shell
tools go through it without the holder doing anything. The Mac's requests are recorded
as the Mac's, not per role: stations share one proxy identity as they share one
machine. A tool that ignores the system proxy cannot reach anything.

## What the stations are not

Separate accounts give attribution, hygiene and per-station recovery. They are not a
confidentiality boundary between roles: Homebrew is shared and writable, so one role
can replace a tool another later runs; processes, `/tmp` and loopback ports are visible
across stations; and nothing meters one station's CPU, memory or disk against
another's — two busy stations run out of the Mac's CPUs first, and a holder can fill the
shared disk past the admission floor. One Mac is one failure domain: a guest reboot,
panic or full disk takes every lease with it. Roles are told this in their prompts. Keep
owner credentials and unrelated host files outside the guest.

## Development limits

The image can include Xcode, Blender and browsers. The Mac VM supplies a virtual Metal
device; it does not expose every physical-GPU capability. A station's app state and
home are shared by every role that leases that station, and a later lease may land on
another station, so a role keeps what it needs again on the drive. Homebrew installs
and the simulator runtimes are the Mac's and reach every station. Another firm cloned
from the same image starts from the image's own set.

Verify a configured Mac with the [Mac lease acceptance tools](../tools/acceptance/mac/README.md)
on disposable firms.
