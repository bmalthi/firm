# Build and maintain Mac VM images

> **Quick Reference — Mac Image Pipeline**
> * **When you need this:** Only if you want roles to lease macOS desktop stations (`[mac]` in `sandbox.toml`) for Xcode, iOS Simulator, or GUI apps. Once you build and seal one enrolled image, every firm clones and reuses it.
> * **5-Step Image Lifecycle:**
>   1. **Reuse if available:** Check `images/.images/` for an existing sealed `image.json`.
>   2. **Build base (`mac-image-build`):** Restores macOS from IPSW, copies Xcode/apps from your recipe (`images/ios.toml`), and installs Homebrew in `/opt/homebrew`.
>   3. **Sign helper & enroll (`mac-prepare`):** Creates `firmstation1..N` accounts and walks you through one brief VM-window console turn per station to grant TCC permissions inside the guest.
>   4. **Install simulator runtimes (`mac-image-apply`):** Downloads the recipe's `[[simulator]]` platforms idempotently.
>   5. **Seal & verify (`mac-image-seal`):** Verifies all station sessions, permissions, and root-owned boundaries, stops the VM, and writes the reusable base directory.

Day-to-day station leases and desktop tools are covered in the [Mac station guide](mac-station.md).

## Contents

- [Prerequisites and image path](#prerequisites-and-image-path)
- [Find and reuse an image](#find-and-reuse-an-image)
- [Build an image](#build-an-image)
- [Station enrollment](#station-enrollment) ([Console turns](#console-turns), [Runtimes, sealing and the clone check](#runtimes-sealing-and-the-clone-check))

## Prerequisites and image path

Use an Apple silicon host with uv, Lima and Xcode. Run the `firm-sandbox` commands
below from the repository root. Building a base needs no firm; enrollment, simulator
runtime installation, sealing and helper updates use a dedicated enrollment firm's
Mac VM. macOS setup and privacy consent happen in that VM's window, never on the host.

Keep prepared images outside individual firm directories; the recommended location and
search commands are in [Find and reuse an image](#find-and-reuse-an-image). Reuse a
suitable enrolled image rather than building another. `sandbox.toml` names the enrolled
image by absolute path; regular firms clone it. Keep the helper signing identity and
private key in a separate, owner-only host directory, outside images, roles, exports
and version control.

The image lifecycle is covered in the sections below:

## Find and reuse an image

Keep reusable prepared images under `images/.images/<image-name>/`, outside any
individual firm directory. This is a recommended location, not an automatic lookup;
`mac.image` in `sandbox.toml` must name the image directory by its absolute path.
Built images in `images/.images/` are ignored by Git; recipes in `images/` remain
tracked. Local firm runs belong in the separate, Git-ignored `firms/` tree.

Before building, search from the repository root for image manifests and references in
existing firm configurations, including ignored and hidden directories:

```sh
rg --files --hidden --no-ignore images/.images -g image.json
rg -n --hidden --no-ignore '^\s*image\s*=' firms -g sandbox.toml
```

Inspect a candidate's `image.json`: a base image records `control_user: "firmvm"` and an
`os_build`; an enrolled one also records `enrollment` with its `stations` count, the
runtimes and applications it carries, and the recipe it was sealed with. Its
`lima/base/` instance must be stopped. Reuse a suitable image across firms; build
another only when none is available or its applications, runtimes or station count do
not meet the new firm's needs.

## Build an image

Build an image once, from a recipe. [`images/ios.toml`](../images/ios.toml) at the
repository root is the checked-in recipe: `disk_gib` sets the base disk size,
`stations` the number of station accounts enrollment creates (1 to 8), and each
`[[application]]` names an absolute host `.app` bundle to copy in.

```toml
disk_gib = 192
stations = 2

[[application]]
path = "/Applications/Xcode.app"

[[simulator]]
platform = "iOS"

[[simulator]]
platform = "iOS"
version = "26.0"

[[simulator]]
platform = "watchOS"
```

`[[simulator]]` entries name the runtimes a firm expects; build ignores them entirely —
[`mac-image-apply` and `mac-image-seal`](#runtimes-sealing-and-the-clone-check) read
them instead. An entry without a `version` means the runtime the image's Xcode
downloads by default, which is the one for that platform's SDK
(`xcrun --sdk iphonesimulator --show-sdk-version`), so it follows a new Xcode without
an edit and no older runtime stands in for it. A `version` pins an older one and is
written as `simctl list runtimes` reports it, `26.0` and not `26`; it is met by that
version or a patch of it, `26.0.1`, never by `26.4`. Parsing is strict, like
`Config.load`: an unknown section or key refuses, and so does an unrecognized
platform, a malformed version, a relative or non-`.app` application path, a duplicate
application or simulator, a station count outside 1 to 8, or simulators without Xcode
among the applications.

```sh
uv run firm-sandbox mac-image-build "$PWD/images/.images/xcode-macos" --recipe images/ios.toml
```

The pinned base is macOS 27.0 (26A428), restored from Apple's
`UniversalMac_27.0_26A428_Restore.ipsw` with its SHA-256 checked; only `disk_gib` is
recipe-controlled at build. The builder supplies uv and the guest controller's Python
and copies the recipe's applications; it copies no owner settings, Keychains, accounts,
conversations or simulator runtime. Xcode first-launch setup happens while building the
image, and so does Homebrew, below. A different Xcode, or a different disk size, means a
new base.

The destination must be new or empty. The builder finds `uv` with `shutil.which`; pass
`uv_binary=` when calling the Python API if it is outside `PATH`. The build works in
`/private/tmp/firm-<uid>/image-*`. A failed build, or one interrupted with Ctrl-C or
`SIGTERM`, stops its VM (forcing it if needed) and removes that directory and a
destination it created; only when Lima cannot confirm the VM stopped does it keep the
directory and name it in the error. `mac-image-seal` likewise removes its destination
when it fails or is interrupted.

An image with Xcode also gets Homebrew in its Apple silicon prefix, `/opt/homebrew`.
The build clones one pinned `github.com/Homebrew/brew` release there as `firmvm`
(`git clone --branch <tag>`, with the tag in `mac_image.HOMEBREW_TAG`, currently
`7.0.3`). The clone is full, not shallow, and checked out detached at the tag: it tracks
every branch and tag and sets `origin/HEAD`, as Homebrew's own git installation does,
so `brew update` and auto-update find origin's default branch (a `--depth 1` clone
tracks only the tag, and Homebrew fails with `Not a valid ref: refs/remotes/origin/main`).
The build requires `origin/HEAD` to resolve, runs `brew analytics off` (analytics stay
off in the repository's own settings), requires `brew --version` to print a `Homebrew `
line, and records what it reported as `homebrew: {prefix, tag, version}` in
`image.json` (`version` is what `brew --version` printed; `tag` is the cloned tag) and
leaves the prefix owned by `root:wheel`. It never runs `brew update`, so rebuilding gives
the same Homebrew; a role updates it during a lease if it wants a newer one, and to
move the image to a newer release, change the tag and rebuild.
[`mac-prepare`](#station-enrollment) hands the prefix to the `firmstations` group,
writable by every station; after that only station accounts run `brew`. Because a
station account can rewrite anything in the prefix, nothing system-wide puts it on
`PATH`: no `/etc/paths.d` entry, so neither the control account's shells nor root
scripts run under sudo can pick a Homebrew program. Only a lease's SSH session finds
it, through the root-owned lease wrapper's `PATH`, after the system directories. Apps
the helper launches keep the helper's minimal `PATH` and do not see Homebrew. An image
without Xcode has no Homebrew, and `mac-image-apply` has no Xcode there to install a
runtime with.

A built image has no station accounts yet: complete [enrollment](#station-enrollment),
then seal and verify a fresh clone before creating normal firms.

## Station enrollment

Enrollment is a one-time owner action per base image: it creates the recipe's station
accounts, walks each of them through its first console login and the consents only a
console login can grant, and leaves the Mac with `firmvm` on the console and every
station's session working off it. Start with a new enrollment firm whose `sandbox.toml`
names a built base image.

Build the helper on the trusted host. If no signing identity exists, create a
dedicated one:

```sh
uv run --no-project station-helper/signing.py /PRIVATE/HOST-ONLY/firm-signing
```

The parent directory must exist. Keep the resulting owner-only directory outside VM
images, role paths, exports and version control. Its private key lives only in a
dedicated keychain. The tool restores the previous keychain search list and does not
modify host trust or privacy settings; required privacy consents are granted only
inside the guest image. Local keychain passwords are compatibility values; protection
comes from owner-only filesystem permissions (0700 directory, 0600 files).
Both the dedicated keychain password and the temporary PKCS#12 import password
are public compatibility values. A process running as the owner can unlock the
keychain and sign a replacement helper with the identity trusted by enrolled
guests. This local identity does not grant Apple Developer ID or notarization
privileges. Back up this directory securely if
enrollment must survive loss of the host; losing the key means a new identity and new
guest consent.

Use the returned certificate fingerprint and keychain path:

```sh
uv run --no-project station-helper/signing.py --unlock /PRIVATE/HOST-ONLY/firm-signing
FIRM_HELPER_SIGN_IDENTITY=FINGERPRINT \
FIRM_HELPER_SIGN_KEYCHAIN=/PRIVATE/HOST-ONLY/firm-signing/helper.keychain-db \
sh station-helper/build.sh /PRIVATE/BUILD 501 502
```

The two numbers are the control account's UID and the first station's; the helper
accepts its own UID in the eight that follow, one per possible station. Unlock only
this dedicated keychain after its automatic timeout. For an existing Developer ID
identity, use its normal keychain management. VM builds require an explicit signing
identity and hardened-runtime signing. `FIRM_HELPER_SIGN_IDENTITY=-` is an explicit
disposable development option: it does not preserve the enrolled designated
requirement, so it cannot be installed in enrolled mode. `FIRM_HELPER_BUILD_VERSION=2`
changes the bundle version for a later build. Before replacing an enrolled helper,
record its designated requirement (`codesign -dr -`, the text after `designated =>`)
and verify the new build carries exactly that; the consents are tied to it.

Create `/PRIVATE/ENROLLMENT/sandbox.toml`:

```toml
[firm]
id = "enrollment"

[mac]
image = "/PRIVATE/BASE"
```

Like every firm, the enrollment firm boots its own Linux VM on `up`, including a
first-boot install on a new firm. Then the same command, repeated, drives the whole
enrollment; each call does one turn and says what the next is:

```sh
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml init
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml up
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml \
  mac-prepare '/PRIVATE/BUILD/Firm Station Helper.app' --recipe images/ios.toml
```

The first call refuses unless the VM holds only the control account, creates the
recipe's `stations` accounts (`firmstation1` … `firmstation<N>`, each with a SecureToken,
because without one the account's lock screen cannot be turned off before its first
login), hands the image's root-owned Homebrew to the `firmstations` group, installs the
helper and the lease access as `mac-install-helper --mode enroll` does, gives every
account a 48-character password, and reboots with station 1 selected for automatic
login. Its reply carries `prepared`, the guest's summary of the enrollment, and
`next`, the first turn's step. It refuses a firm cloned from an image that is already
enrolled: those accounts have been through this; prepare a firm cloned from the base
image instead.

Each turn is one account on the console, and each `mac-prepare` moves to the next. The
reply names the turn, the account on the console and what to do at the VM window, and
writes `durable/mac/enrollment-credentials.txt` (mode 600) with the administrator's
password and the console station's, both eight digits for this turn only, because the
VM window takes no pasted text. Open the file locally when the guest asks for a
password; the values are never printed. When the turn ends, that station's password is
made 48 characters again.

### Console turns

The steps alternate between the VM window and the host. Some macOS 27 builds name
**Privacy & Security** **Device Control and Data Access**; its grants live there.

For **each station**, in turn:

1. **VM window:** the station is logged in automatically. Complete the macOS welcome
   screens; leave FileVault off. Its screen lock is already off.
2. **On the first station's turn only, VM window:** grant `/Applications/Firm Station
   Helper.app` **Accessibility** and **Screen Recording** under **Privacy & Security**;
   under **General > Sharing > Remote Login**, enable Remote Login and, behind the (i),
   **Allow full disk access for remote users** (the same grant as adding
   `sshd-keygen-wrapper` under **Full Disk Access**). Without it, the drive mounts but
   reads and writes over SSH fail with `Operation not permitted`. These are machine-wide
   and are asked once. Do not enable password logins; the installer's sshd block admits
   the station group by key only.
3. **Host, then VM window:** raise the **Files and Folders > Network Volumes** prompt.
   macOS shows it only the first time an app the helper spawned touches the drive, and
   only while that account owns the console; off console the access is refused without
   a prompt. Run the [`project_handoff.py`](../tools/acceptance/mac/README.md) check with
   `--station N` against the enrollment firm; it stops with `AXError -25204` while the
   app waits on the prompt. Click **Allow** in the VM window. The grant is per account,
   and it holds off console afterwards.
4. **Host:** run `mac-prepare` again with the same recipe. It makes the station's
   password long again and reboots with the next account on the console; the new
   reply's first instruction is to run `project_handoff.py --station N` once more, now
   that station N is *off* the console, which proves its grant holds there.

After the last station, the reply moves the console back to `firmvm`:

5. **VM window:** `firmvm` completes its own first login (it shows a Setup Assistant
   flow; FileVault stays off). The administrator's password is in the credentials file.
6. **Host:** `project_handoff.py --station N` for the last station, off console, then
   `mac-prepare` once more. This final call makes every station's session off console,
   verifies each helper reports an unlocked session with its grants, closes `su` to the
   station group (it needs the Remote Login grant, which is why it waits for this turn),
   and answers `complete: true` with `su_closed: true`.

All consent is **inside the guest**, not on the host Mac. Guest administrator prompts
use `firmvm`, not the personal host account. Readiness must report `ready:true`,
`clean:true`, `environment:vm-session` and an unlocked session for every station. There
is no TCC database editing, configuration profile, trust override or scripted
permission grant. Why these grants and not others, and what they do not open, are in
the [sandbox design](../design/sandbox.md#the-mac-lease).

Check every enrolled helper through its relay; this reports missing permissions without
leasing a station:

```sh
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml mac-status
```

If a station's session will not log in, `mac-status` says so and the controller stops
after two attempts rather than driving the account toward macOS's lockout. A locked-out
account (four wrong passwords in the guest) is unlocked from the console as `firmvm`
with `sysadminctl` or System Settings; nothing in the firm does that.

### Runtimes, sealing and the clone check

Install the recipe's simulator runtimes onto this same running Mac VM. Idempotent, so
a failed download is a plain rerun: it lists the guest's runtimes as the control
account, installs each one the recipe names that the guest lacks, and does nothing when
the guest already has them all. The install is the station's own platform installer,
copied in and run as root the way `mac-install-helper` runs its script, with the pinned
version added as `-buildVersion`; a lease's sudo rule still takes a platform and no
version. It lists the runtimes again afterwards and fails if one it installed is still
missing, rather than leaving that for `mac-image-seal` to find.

One install runs at a time in a Mac, whoever asked. The installer holds a lock for as
long as its download lives and stops the download itself after 110 minutes; this
command waits two hours. So after a lost connection or an interrupted command, a rerun
says an install is still running instead of starting a second download beside it, and
`mac-image-seal` refuses for the same reason. Wait and run it again.

```sh
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml mac-image-apply images/ios.toml
```

After enrollment is complete, every helper reports ready and the recipe's runtimes are
installed, publish the image, naming the same recipe:

```sh
uv run firm-sandbox --config /PRIVATE/ENROLLMENT/sandbox.toml \
  mac-image-seal /PRIVATE/ENROLLED-BASE --recipe images/ios.toml
```

Before touching the guest, `mac-image-seal` checks two prerequisites:
- **Disk size match:** Refuses if the Mac's disk size differs from the recipe (every clone's Lima config carries its base image's disk size; a different size requires a new base build, not a reseal).
- **Simulator runtimes:** Refuses while a platform install is still running or if any simulator runtime named in the recipe is missing (run `mac-image-apply` first).

It then verifies every station and system boundary before sealing:
- **Station login & desktop state:** Every station has completed first login, holds an unlocked desktop session that will not sleep, start a screen saver, or relock at login (if refused, run `mac-restart` to restate these settings), has a 48-character password with no half-finished rotation, and has an empty helper ledger.
- **Enrollment & console lockdown:** `su` is closed to the `firmstations` group, Screen Sharing admits only that group, Remote Management is off, and `firmvm` is configured for automatic console login.
- **SSH & shell startup isolation:** The `sshd` lease block, command wrapper, empty key directory, and command logs are in place; `/etc/zshenv` and the root-owned `/usr/local/libexec/firm-lease-zsh` startup files match the expected bytes.
- **Homebrew & service hygiene:** `firmstations` owns `/opt/homebrew`, neither `/etc/paths` nor `/etc/paths.d` puts it on the system path, no Homebrew `LaunchAgents` or `LaunchDaemons` are installed or loaded, and no running process executes from `/opt/homebrew`.
- **Root-owned system paths:** The platform installer, sudoers drop-in (allowing station accounts no other `sudo` rule), `/Applications/Xcode*.app` trees, and `/usr/local/firm` guest controller are root-owned and not group/world-writable.

Finally, `mac-image-seal` reads the live guest to record `image.json` (installed simulator runtimes, recipe app bundle versions, selected Xcode version/build, Homebrew version, station count, and recipe SHA-256), clears the stale resume flag, disables app restoration, stops the source VM, and copies only the stopped VM assets and bootstrap keys into the sealed directory. Create normal firms from this enrolled base.

Before accepting the sealed image, create a **new disposable firm from it** and run the
[Mac lease acceptance checks](../tools/acceptance/mac/README.md) there: at the least
`all_stations.py`, `station_isolation.py` and `station_sessions.py`, and
`project_handoff.py --station N` for each station, which proves a file written through
the Mac's mount appears on the role's drive and back and that an app the helper
launches from the drive can write there. Repeat `project_handoff.py` after
`firm-sandbox down` followed by `up`. Record the enrollment guest's `sw_vers` version
and build with these results. Helper readiness alone does not prove Remote Login Full
Disk Access or its retention. If any clone check fails, stop and report the exact
failure and OS; do not grant extra consent on the clone to make acceptance pass.

`mac-install-helper --mode` takes `update` (the default), `enroll` or `development`.
For subsequent builds use `--mode update`. It rejects a changed designated requirement;
use `--mode enroll` when intentionally changing identity. Only `--mode development`
accepts an ad-hoc signed build, for disposable development VMs. Apple documents
self-signed identity continuity without trust-store enrollment, but actual TCC
retention must be tested on the enrolled OS and image:
[Apple signing procedures](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/Procedures/Procedures.html).

To refresh an already enrolled image, create a new enrollment firm pointing to that
image, start it, and run `mac-install-helper '/PRIVATE/BUILD/Firm Station Helper.app'
--mode update` with the same signing identity. This also installs the current lease
wrapper, sshd configuration, platform installer and sudoers drop-in for every station.
It does not add Homebrew to an image built without it; build a new base for that.
`mac-image-apply` does add a simulator runtime to any image that already has Xcode,
without a new base: point a recipe naming the new runtime at this same running
enrollment firm, `mac-image-apply` it, then seal. `mac-prepare` is only for an image
without station accounts; a different station count is a new enrollment of the base.
Check readiness, seal to a new image directory with `--recipe` naming what the image
should carry, then verify a fresh clone as above.
Sealing accepts this enrolled clone without enrollment progress of its own; a clone
of a base image still needs every console turn finished.
