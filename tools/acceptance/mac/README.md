# Mac lease acceptance

These host-side checks use one Mac VM with the station accounts recorded by its
image. Enroll an image as the [Mac station guide](../../../docs/mac-station.md)
describes, then run the checks on a disposable firm cloned from it. The station count
comes from the image recipe and `Sandbox.mac.stations`; there is no station count in
`sandbox.toml`. Ordinary application checks on different stations can run side by
side; a role has one run. The Device Hub tap check is timing-sensitive, so keep its
result and timing evidence with the run that produced it. Known live-validation gaps
include mixed Device Hub downstream timing, virtual display resizing, Apple Events
entitlement, consent prompt order and macOS session/reboot timing. Run checks on each
enrolled account before claiming parity; scripts alone do not establish that these pass.

Run from the repository root with `uv run tools/acceptance/mac/SCRIPT.py`.
Every executable check accepts `--root FIRM_DIR`, `--evidence DIR` (a new private
directory per run), and `--station N`. The station option is required on a Mac
with multiple accounts; it defaults to the sole account only on a one-account Mac.
Application checks need both VMs running and the selected account unlocked,
ready and clean. Console ownership is not required. Inspect all accounts with
`firm-sandbox mac-status`, or one with `--station N`.

A check asks the firm for the account it names (`mac_acquire(..., station=N)`),
so ordinary checks on different accounts run side by side from separate host
processes; a busy or dirty account refuses rather than passing that check on to
another. Run `lease_cleanup.py` and `release_faults.py` alone: their contender roles
may acquire any free account while the selected station is busy or dirty. Roles
never choose: the runtime's lease tool asks for any free account. Workers receive
the checked lease reply, including SSH user/host/port, drive, project directory
and scratch; expected uids are `501 + lease.station`.

`dispatcher_lease.py` starts dispatcher-managed holders for all other accounts.
`all_stations.py`, `station_isolation.py` and `station_sessions.py` require all
accounts free and clean and manage their own holders, so they run alone. For
`all_stations.py`, `--station N` names a required participant; coverage always
includes every account. The isolation and session checks need at least two.

Evidence keeps the existing role logs, captures, release reports and `result.json`
conventions. The isolation and session checks also keep per-account subdirectories
with baselines, lease replies and copied command logs, including on failure.
`all_stations.py` retains dispatcher tables and role logs and writes both
`failure.json` and `result.json` on failure. Evidence contains private paths and role
output: keep it private. Run the checks on a fresh clone for each release candidate,
including ordinary application checks on both stations while another account works.

`acceptance_common.py` is not a check: it holds what more than one of them needs — the
shared argument parser and station lookups, the Linux lease probe, the worker a check
starts and the close-out that releases and stops it, the timed helper caller, and the
shared text of the programs a role and a station run.

| Tool | Coverage | Arguments |
| --- | --- | --- |
| `project_handoff.py` | Shared-drive app handoff, Metal execution and clean release | `--root FIRM_DIR --evidence DIR --station N` |
| `helper_smoke.py` | Browser input and capture through the station helper | `--root FIRM_DIR --evidence DIR --browser GUEST_APP --station N [--page URL] [--apple-app GUEST_APP]` |
| `dispatcher_lease.py` | Mac leases through the real dispatcher | `--root FIRM_DIR --evidence DIR --station N [--browser HOST_CHROMIUM]` |
| `lease_cleanup.py` | Dirty cleanup refusal and recovery | `--root FIRM_DIR --evidence DIR --station N` |
| `release_faults.py` | Lease cleanup under injected faults | `--root FIRM_DIR --evidence DIR --station N` |
| `release_during_tests.py` | Lease release during active simulator tests | `--root FIRM_DIR --evidence DIR --station N` |
| `station_self_service.py` | Station package and simulator self-service | `--root FIRM_DIR --evidence DIR --station N` |
| `xcode_build.py` | Xcode builds and simulator execution from a leased station | `--root FIRM_DIR --evidence DIR --station N [--ios VERSION]` |
| `ui_test_play.py` | Gesture delivery, UI test counts and captured artifacts | `--root FIRM_DIR --evidence DIR --station N [--gesture-seconds S] [--video-seconds S]` |
| `pointer_tools.py` | Held clicks, drags, scrolls and tap cadence | `--root FIRM_DIR --evidence DIR --station N` |
| `device_hub_taps.py` | Tap arrival and cadence measured inside the simulated app | `--root FIRM_DIR --evidence DIR --station N [--cadence-ms MS] [--taps N]` |
| `all_stations.py` | Concurrent leases across all enrolled accounts | `--root FIRM_DIR --evidence DIR --station N` |
| `station_isolation.py` | Cross-account file, process and helper isolation | `--root FIRM_DIR --evidence DIR --station N` |
| `station_sessions.py` | Session restoration, reset contention and recovery | `--root FIRM_DIR --evidence DIR --station N` |

```sh
uv run tools/acceptance/mac/project_handoff.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-EVIDENCE

uv run tools/acceptance/mac/helper_smoke.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-BROWSER-EVIDENCE \
  --browser '/Applications/Google Chrome.app'

uv run tools/acceptance/mac/lease_cleanup.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-CLEANUP-EVIDENCE

uv run tools/acceptance/mac/xcode_build.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-XCODE-EVIDENCE

uv run tools/acceptance/mac/station_self_service.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-SELF-SERVICE-EVIDENCE

uv run tools/acceptance/mac/ui_test_play.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/NEW-UI-TEST-EVIDENCE \
  --gesture-seconds 30
```

The self-service check needs an image whose recipe names Xcode and at least one
`[[simulator]]`, enrolled with `mac-prepare` from this checkout, and public
egress on the Mac for `brew install`. Its `hello` install persists in the shared
Homebrew prefix and is visible to every account.

The Xcode build check needs an image whose recipe names `/Applications/Xcode.app` and
an iOS `[[simulator]]`, applied with `mac-image-apply` before sealing (see
[Build an image](../../../docs/mac-vm-images.md#build-an-image)); the sealed
`image.json` then lists the iOS runtime under `simulators`. A role can also add a
missing iOS runtime during a lease with
`sudo -n /usr/local/libexec/firm-station-add-platform iOS`, but that is a long
download inside the check.

The play check has the same image requirement as the Xcode build check. It uses the
UI test as a repeatable play-through.
It takes two `xcodebuild test` runs and is the longest of these checks. `simctl` hands
screenshots and recordings to the simulator service, a separate process that cannot
write the drive's mount, so the station writes them in its home and copies them to the
drive. `--gesture-seconds` is the bound on a single
gesture call; it exists because a UI test's wait-for-idle gate may stall against a
continuously rendering game, and measuring that is the point of the rendering phase.
`silent_skip.reproduced` in `result.json` says honestly whether this Xcode still ignores
a scheme's reference to a local package's tests; the check fails only if it does ignore
them and the test-count comparison misses it. The whole station-side program runs in
one SSH call bounded at 90 minutes, with its own bounds on each `xcodebuild` and
`simctl` step inside; on a slow host the outer bound can fire first, and the cause is
then at the end of `role.log` rather than in the result.

The Device Hub tap check measures whether the simulated app receives the helper's tap
cadence. It has the same image requirement as the Xcode build check. It finds Device Hub
inside the selected Xcode and fails naming the applications it found if there is none.
Unlike ordinary application launches, it requests the helper's explicit direct mode so
Device Hub's trampoline handoff stays on the tracked PID; ordinary launches continue to
use LaunchServices. It polls a bounded accessibility tree before selecting the device,
records each attempt's count and truncation reasons, and can inspect the device sidebar
from the outline handle when the first tree is partial. It presses the device's entry,
re-lists and validates the usable selected window, then targets the centre of the content
area beyond the sidebar. `result.json` keeps the AX evidence, chosen and post-selection
windows, device entry, sidebar width, captures, helper offsets, wall-clock timestamps
and monotonic app timestamps. The pass condition requires every requested tap; a
multi-tap run also requires monotonic app spacing within 150 ms of the requested
cadence. Delivery can still bunch after the helper posts near the requested cadence,
so receiving every tap and releasing cleanly are not enough for this check to pass.
`--taps 1` is allowed, with no cadence interval to evaluate.

The handoff check needs Xcode and the helper's Files and Folders > Network Volumes
consent. Its helper-launched app reads a role-written drive marker and writes a
result back. It records `helper-status.json` and `result.json.mode`: `console-consent`
when `session.on_console` is true, otherwise `off-console-verification`. On console
it announces the Allow prompt and gives the save/read phase ten minutes; off console
the read must succeed with the existing grant. The app keeps its main thread available
while consent is pending. A ready helper alone is not evidence of this consent.

Enrollment is N+1 console turns, driven by repeated `mac-prepare APP --recipe R`:

1. The recipe names `stations = N`. On each station's automatic-login console turn,
   the owner finishes Setup Assistant; enrollment turns the screen lock off. Accessibility,
   Screen Recording and Remote Login full disk access are machine-wide grants made once.
2. After the file forwards are reconnected (the prepare CLI does this), run
   `project_handoff.py --station N` for that turn's account, using a fresh evidence
   directory. Allow Network Volumes in the VM window. Require a successful result
   with `mode: console-consent` before advancing `mac-prepare`.
3. Finish the final prepare turn on `firmvm`. Run the handoff again for **every**
   station with new evidence directories. Require `mode: off-console-verification`
   and a successful drive read/write for each; macOS will not raise this prompt off console.
4. Seal only after those owner turns and checks. On a disposable sealed clone,
   `station_sessions.py` checks control-account automatic login after restart,
   including the clone's rotated password, plus session restoration after cold boot.
   No consent, setup-completion flag or account password is synthesized by these checks.

For the browser check, install a full Chromium browser in the disposable guest and
complete its first-run prompts for each station account; sign-in is not needed.
The role stages the checked-in `browser_page.html` fixture into
the lease's scratch directory automatically; pass `--page` to use an explicit guest URL
instead. The `--browser` value is the full guest `.app` path, for example
`/Applications/Google Chrome.app`. Keep Chrome's sandbox enabled. The smoke opens a
fresh window so restored tabs
cannot satisfy the form assertions; input replies acknowledge posting, so it
waits for the expected window title before proceeding.

The cleanup check uses a real, long-lived Linux holder without a model provider.
Its deterministic dirty case sets the macOS `schg` flag as guest root on a unique extra
LaunchAgent file inside the disposable guest. Release must report failed work
cleanup, refuse another role, and recover after the owner clears the flag and
removes the file. A `finally` block also clears this injected fault on failure; no host privilege is used.
This proves dirty cleanup refusal and recovery; it does not claim an unkillable
or continuously respawning process. `result.json` retains every baseline,
release check, refusal, and recovery, alongside copied command logs and the
Linux holder log. Baseline verification uses the release contract against the
first cycle's baseline: known processes may exit and trusted processes managed
by launchd may respawn, but extra services, LaunchAgents, crontab entries and
unexplained processes fail. Exact baseline equality is recorded separately as
`strict_baselines_equal`; it is not substituted for the cleanup contract.
No privileged commands run on the host.

The application and cleanup checks release their leases but leave the VMs running. Stop them
when finished:

```sh
uv run firm-sandbox --config /PRIVATE/CLONED-FIRM/sandbox.toml down
```

The dispatcher lease check needs a fresh firm directory whose `sandbox.toml`
names the enrolled image. That firm must never have been bound to another runtime
database:

```sh
uv run tools/acceptance/mac/dispatcher_lease.py \
  --root /PRIVATE/FRESH-FIRM --station 1 --evidence /PRIVATE/NEW-DISPATCHER-EVIDENCE
```

With `--browser`, it also checks the live console in a host Chromium through
Playwright. Playwright is not a project dependency, so add it for that run with
`uv run --with playwright`:

```sh
uv run --with playwright tools/acceptance/mac/dispatcher_lease.py \
  --root /PRIVATE/FRESH-FIRM --station 1 --evidence /PRIVATE/NEW-DISPATCHER-EVIDENCE \
  --browser '/HOST/CHROMIUM/Contents/MacOS/Google Chrome for Testing'
```

It stops its VMs in its cleanup path; after a failure that is a forced stop rather than
a release, so its evidence database then shows the leases as still held. The
application and dispatcher lease checks use separate test firms because the runtime
owns the firm's organization binding.

`all_stations.py` has the same fresh-firm requirement as the dispatcher lease
check. All accounts participate regardless of the selected station:

```sh
uv run tools/acceptance/mac/all_stations.py \
  --root /PRIVATE/FRESH-FIRM --station 1 --evidence /PRIVATE/ALL-STATIONS-EVIDENCE

uv run tools/acceptance/mac/station_isolation.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/ISOLATION-EVIDENCE

uv run tools/acceptance/mac/station_sessions.py \
  --root /PRIVATE/CLONED-FIRM --station 1 --evidence /PRIVATE/SESSIONS-EVIDENCE
```

The dispatcher checks stop their VMs, forcing shutdown on failure. The isolation
check and session check leave the VMs running. Run the session check only after
sealing, with `firmvm` configured for automatic login. It rotates the control
password before its restart and verifies the control console after both boots.
It temporarily disables the
selected account's `dev.firm.station-helper` launchd job, restores it in `finally`,
and resets that account again. Its guest lock holders exit on their own bounded
deadlines. Ordinary failures retain restoration evidence in `result.json`; killing
the host process can interrupt cleanup, so use disposable clones.

Isolation covers the stated account boundaries. CPU, memory, loopback ports and
Homebrew remain shared. This check deliberately proves writable shared Homebrew;
it does not claim resource or package isolation.

`../network.py --station N` validates the station selection but probes the shared
Mac's machine-wide network policy through the control channel; its evidence uses
`mac`, as do resource samples. `../browser.py` uses Linux and merely reports whether
the shared Mac is configured.

These scripts must be run on the target image and accounts to establish live
acceptance. They do not establish virtual display resizing, Apple Events
entitlement, or all consent and reboot ordering behavior.
