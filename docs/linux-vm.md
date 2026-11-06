# Running a firm on the Linux VM

> **Quick Reference — Linux VM & Sandbox**
> * **One Persistent Ubuntu VM per Firm:** Every role runs as a dedicated Linux user (`firmrole-<hash>`) with its own `0700` home, `systemd` slice (`user-<uid>.slice`), and **rootless Docker daemon**.
> * **Scratch vs. Durable Storage:** Build and check out code in `~/scratch` (wiped after every work period). Save durable deliverables on `~/drive` (`/srv/firm`, served via internal Samba with reporting-line POSIX ACLs). Installed tools and language caches (`uv`, `npm`, `Cargo`, `Go`) in `~` persist across runs.
> * **Two CLIs:** Use `firm` for everyday firm lifecycle and network grants (`firm start`, `firm stop`, `firm network`). Use `firm-sandbox --config PATH` for low-level guest diagnostics (`ps`, `logs`, `data-status`) and Mac image maintenance. (Invoke directly when installed via `uv tool install`, or prefix with `uv run` inside a repo checkout.)

The [sandbox design](../design/sandbox.md) describes what the model guarantees,
why, and what it does not.

## Contents

- [Create and start a firm](#create-and-start-a-firm)
- [Roles on the Linux VM](#roles-on-the-linux-vm) ([Lifecycle and diagnostics](#lifecycle-and-diagnostics))
- [Retained files](#retained-files)
- [File server](#file-server)
- [Docker inside a role](#docker-inside-a-role)
- [Network access and cutoff](#network-access-and-cutoff)
- [Development limits](#development-limits)

The first boot trusts the configured Ubuntu image and package repositories, Docker's
installation script, Astral's uv installer, and the pinned Node/provider CLI downloads
in `assets/linux-vm.yaml`. Docker and uv installers execute as guest root and are not
pinned by content digest. The egress allowlist limits destinations; it does not verify
publisher content. Review the recipe before provisioning where that supply-chain
trust is unacceptable.

## Create and start a firm

Use the ordinary `firm.toml` provider and account configuration from the
[runtime guide](runtime.md). Create a dated firm directory and its `sandbox.toml`:

```toml
[firm]
id = "2026-09-14_development"
cpus = 8
memory_gib = 16
disk_gib = 60

[network]
egress = "enforce"
allow_hosts = []
```

`[firm]` sizes the Linux VM; there is no image to build for it. Edit `cpus` or
`memory_gib` later and the stopped VM takes the new size on its next boot, keeping its
disk; size them from
[its resource history](runtime.md#sizing-a-firm-from-its-resource-history) after a week
of real use. `disk_gib` is a positive integer in GiB, default 60, used only when
creating the Linux VM. Editing it leaves an existing firm's disk unchanged.
Its first boot
downloads the Ubuntu cloud image and installs Docker, Samba, cifs-utils, uv, Node,
Claude Code and Codex, which takes several minutes. `[network]` names the hosts the
firm's VMs may reach beyond the built-in baseline, and `egress = "observe"` logs every
host without refusing any, for finding out what a new workload needs; see
[network access](#network-access-and-cutoff). An optional `[mac]` table names an enrolled
Mac image; that image records its one to eight station accounts (see
[the Mac station guide](mac-station.md)); without it the firm has one VM.

```sh
uv run firm init firms/2026-09-14_development \
  --firm-id 2026-09-14_development \
  --mandate firms/2026-09-14_development/mandate.md \
  --config firms/2026-09-14_development/firm.toml
uv run firm start firms/2026-09-14_development \
  --sandbox-config firms/2026-09-14_development/sandbox.toml
```

`up` boots the Linux VM and installs its guest controller, boots its Mac VM when
the firm has Mac stations, starts the file server, then forwards the runtime's MCP
port narrowly into the Linux guest's loopback over the host-owned management SSH
connection. Role tokens still authenticate every MCP request.

## Roles on the Linux VM

Every role is a Linux account `firmrole-<hash>` (UID 2000 to 49999, allocated
deterministically and never reused) with a private home `/home/<account>`, its
private `work/` directory, `scratch/` for checkouts and builds, and its `drive/` mount
point, all `0700`. Scratch is created on launch, including in an existing home.
Its path is supplied with the role's other paths through `Layout`. Provisioning also
reserves a subordinate UID and GID range of 65536 IDs at
`100000 + index * 65536`, written to `/etc/subuid` and `/etc/subgid`, and writes
the unit for the account's standard slice `user-<uid>.slice`. Rootless Docker is
installed for the account once. How the slice, user manager and daemon are started
and settled is in the [sandbox design](../design/sandbox.md#roles-on-the-linux-vm).

A run starts inside the slice: the guest controller starts the user manager and the
rootless daemon, then launches the command with
`systemd-run --scope --slice=user-<uid>.slice --uid=<uid> --gid=<gid>`. The
environment carries `HOME`, `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`,
`XDG_RUNTIME_DIR`, `FIRM_CREDENTIALS_DIR`, `FIRM_RUN_ID` and `FIRM_STATE_DIR`.
State and credentials live under `/run/firm/<role>/`, a tmpfs that vanishes on
reboot; secrets are written there as the account, create-only and `0600`.
Settlement writes `exit.json` last, including the slice's `usage`: peak memory, CPU
time and OOM kills for the whole run, read just before the slice stops. The runtime
stores them on the run and **System → Diagnostics → Resources** shows them; the peak
counts page cache, so it is an upper bound on what the run needed.

Save work needed again on the drive before finishing a period. When an activation ends, settlement runs under a 20-second deadline:
1. **Prune disposable Docker state (2s budget per category):** Prunes stopped containers, dangling images, and ordinary builder cache (a timed-out Docker prune does not block slice shutdown).
2. **Stop the slice & verify quiescence:** Stops the rootless daemon and `user-<uid>.slice`, then confirms the kernel's recursive `cgroup.events` reads `populated 0` (or the slice cgroup is gone).
3. **Wipe `~/scratch` & unmount `~/drive`:** Recreates `~/scratch` empty (`0700`) using the remaining 20-second budget without following symlinks or crossing mounts, while keeping `work/`, provider transcripts, installed tools, and language caches (`pip`, `uv`, `npm`, `Cargo`, `Go`) intact.

`exit.json` records `reclaim.categories`:
`docker_containers`, `docker_image_deletion_records`, `docker_build_cache` and
`scratch`, each with removed `count` and `bytes`, `skipped` reasons, `completed` and
`timed_out` (retained on the host in `firm analyze PATH --section resources --json`).
A failed scratch cleanup keeps execution reserved and
is retried by the dispatcher before the role can work again; if cleanup repeatedly
fails without progress (for example, a stuck bind mount inside `scratch`), keep execution stopped, repair the obstacle in the guest, and retry stop/settlement.

`stop(run, timeout)` reclaims the Docker categories, then terminates the slice and bounds the guest
acknowledgement and exit wait (default 30 seconds). `run` follows
output live while the command executes; `logs` pages through all retained output (up to 64 MiB per run).
Finished guest journals are retained for **7 days** and swept hourly once the host has committed tool-call tallies, provider usage, slice usage, and reclaim evidence to SQLite (after pruning, `firm analyze` keeps the recorded history while raw stdout is removed from `firm-sandbox logs`).

Retire a role's guest identity with `firm-sandbox retire ROLE` after the runtime has
retired the employee; it refuses while the role is running, and keeps the role's
home.

### Lifecycle and diagnostics

Status reports `recovery_required` when Lima cannot establish a normal VM state. An
unknown `running` value is not evidence of shutdown. Force stop can act on a broken
instance and verifies shutdown before releasing execution ownership; it preserves
every guest disk.

`down` on a Linux VM whose state Lima cannot establish powers that VM off and
shuts down the rest normally, whether or not the caller supplied expected runs.

A running Linux VM whose network boundary is not ready cannot be shut down
gracefully. An ordinary `down` flushes guest writes and powers off every resource.
When the caller supplied expected runs, as a runtime-managed shutdown that must
account for active execution does, this is an error instead, and `force-down` is
the explicit path. With a ready boundary, expected runs make `down` refuse while any
other run is active.

A runtime restart interrupts its owned active runs: the new runtime process
stops them before admitting fresh work. Existing owner-stop and timeout causes
stay intact.

The guest controller is installed root-owned at `/usr/local/libexec/firm-controller.py`
and reached through `sudo -n` from the Lima user; a changed guest controller is
reinstalled on the next `up`, which refuses while a role is running.

Every host call is one such sudo, so the install also writes an rsyslog rule,
`/etc/rsyslog.d/10-firm.conf`, that keeps two things out of `/var/log/auth.log`: the
Lima user's successful controller call, matched as the whole message, and the PAM
session lines around every sudo, which name no command. A denied call, extra arguments,
another command, a terminal or another user still logs its `COMMAND` line. The install
validates the rule with `rsyslogd -N1` and removes it if rsyslog refuses it, because one
bad rule stops `auth.log` altogether. The journal keeps every line and is capped at
500 MB.

Use `firm-sandbox` for normal lifecycle operations. A direct `limactl` restart can
attach Lima's own network instead of Firm's filter. On the next `up`, Firm stops
that VM, verifies shutdown, and boots it again with its filter.

## Retained files

VM disks live under `durable/linux/` and, with a Mac, `durable/mac/`, whose
`stations/<n>/` holds each station's lease records. On the
Linux disk, `/home/<account>` holds each role's private home and provider
conversations, `/srv/firm` the shared trees, and each role's rootless Docker
images and volumes stay in its home. Scratch, stopped containers, dangling images
and ordinary build cache have the settlement policy above; everything outside
those named categories stays. Stopping retains the disks.
Retiring a role keeps its home inaccessible to later roles until the firm is
replaced.

Sockets and Lima runtime state for a firm with a long directory path live under
`/private/tmp/firm-<uid>/lima/`. Stopping copies its `*.log` files into
`durable/.../lima/<instance>/logs/` first, best effort, keeping the previous stop's
copy in `logs.previous/`, so the runtime directory can then be removed; the next
start rebuilds it.

`firm-sandbox replace` discards every guest disk, homes, daemons, images, volumes
and the shared trees included, and starts a fresh Linux VM; with `[mac]` it also
clones `mac.image` again for the firm's shared Mac VM, so edit that setting first to select a
different image.
Do not use it on a runtime-managed firm: back up and replace a firm as a whole, as
[runtime operations](runtime.md#firm-inventory-export-and-deletion) describe.

To keep a finished run without its disks, `firm export` copies the shared trees, and
optionally each role's `work/` and provider sessions, out of the Linux VM into the
firm's `export/` directory, and `firm discard-vms` exports, homes included, and then
discards both VMs for good. The copy streams through the guest controller over the management channel,
not over the file server, and boots only the Linux VM when the firm is stopped.

## File server

The Linux VM serves `/srv/firm` as the SMB share `firm` from `smbd` bound to its own
loopback. Every role has a file server login there (`firmfile-<hash>`, in group
`firmfiles`) with a password the sandbox generates once and keeps in
`durable/file-server.json`. No account is created on the host. Who can read and
write what is set by ACLs described in the
[sandbox design](../design/sandbox.md#the-file-server); they are recomputed from the
org chart at every firm start.

Each organizational run mounts `//127.0.0.1/firm` at `<home>/drive` with
`mount.cifs`, run as root with the account's `uid` and `gid`, the password in the
mount helper's environment and never on a command line, works in
`<drive>/org/<address>/work`, and unmounts at settlement. A failed mount fails the
launch with the reason. The mount options are SMB 3.1.1 with NTLMSSP, `forceuid`,
`forcegid`, `file_mode=0700`, `dir_mode=0700`, `noperm`, `mfsymlinks` and
`actimeo=1`: the account owns what it sees locally and the server's ACLs decide
access. Samba disables multichannel for the loopback connection, and the `fruit`
and `streams_xattr` modules keep the Mac station's mount working.

The sandbox forwards port 445 to one per-firm loopback port on the host, and,
when the firm has Mac stations, on into the Mac VM's loopback, over its own SSH
channels, so the share never crosses a network interface or the packet filter. The
owner login `firmowner` is a Samba admin user. Use **Results** in the console to
browse and download deliverables directly, or inspect the SMB URL and password with
`firm-sandbox --config PATH data-status` and run
`mount_smbfs //firmowner@127.0.0.1:<port>/firm <directory>` (or open the URL in Finder)
when you want a host mount; see [runtime operations](runtime.md#start-and-stop).
The port and password are chosen once per firm and survive stop, start and host
restarts; only discarding the VMs replaces them.

## Docker inside a role

Docker is each role's own rootless daemon, started at run start under the role's
slice and stopped at settlement. `docker`, Compose and Buildx come from the image.
Bind mounts from the role's home and from its drive work as on any Linux host;
container users map into the role's subordinate range, so a file a root container
writes is owned by the role's account on the host and a uid-1000 container's file
by the corresponding subordinate ID. Named volumes live in the role's home across
restarts and are gone with the firm's replacement.

Reclaim never uses `-a`, prunes volumes or removes tagged images. Anonymous volumes
stay too. Containers still running when prune starts become candidates at a later
settlement; stopped-container writable layers are disposable, so keep container
data in a volume or on the drive. Separate Buildx builders are not swept. Expect
some rebuild cost after builder-cache reclaim. Tagged images and volumes still
provide a warm cache across work periods.

A published port is bound on the Linux VM's loopback and reachable from the role
itself; nothing forwards it to the host. **Guest loopback is shared by every role**:
other roles can reach published ports and compete to bind them.

## Network access and cutoff

A guest reaches the internet only by name, through a proxy the host runs beside each
VM's packet filter. The filter lets nothing else out: no direct connection to any
address, no UDP, no DNS. The proxy resolves names itself and admits a request when
its host is on the firm's allowlist: the built-in baseline (the provider APIs, GitHub,
the package registries, the image's package mirrors, `astral.sh` and its installer
redirect host `releases.astral.sh`, and, for a Mac, Apple's download hosts),
`[network] allow_hosts` in `sandbox.toml`, and the hosts the owner granted while the
firm ran. An entry is an exact lower-case name or `*.example.com` for every subdomain.
Anything else is refused with `403`, which a role reads as a host the firm has not
allowed yet and asks for with `request_access`; see
[owner requests](runtime.md#owner-requests).

Every role's run carries the proxy in its environment with a credential that names
the role, so `curl`, `git`, `uv`, `npm`, `apt`, Claude Code and Codex go through it
unchanged and each request is attributed. The role's rootless Docker daemon pulls
through it, and its Docker client configuration passes the same settings into every
container and build. A tool that resolves names itself fails fast, because the guest's
resolver points nowhere. Web search needs none of this: Claude Code's WebSearch and
Codex's web search run on the provider's side, so only the provider API has to be
reachable. WebFetch and `curl` reach allowed hosts only.

`egress = "observe"` in `sandbox.toml` lets every host through and logs it. Run a new
kind of workload under it once, read the hosts from `firm analyze --section network`,
and put the ones it needs in `allow_hosts`. The mode is recorded in the filter's policy
and changes on the next VM start. In observe mode, host-access requests return granted
without asking the owner; hostname, port and public-address checks still apply. This
mode permits outbound actions as well as reads.

The owner can inspect the record and cut every VM off:

```sh
uv run firm network /absolute/firm status
uv run firm network /absolute/firm off
uv run firm network /absolute/firm on
uv run firm network /absolute/firm allow api.example.com
uv run firm network /absolute/firm revoke api.example.com
```

`allow` and `revoke` change the granted list on the running VMs at once; nothing
restarts, and a stopped VM takes the list on its next boot. `revoke` acts on granted
hosts only; baseline and `allow_hosts` entries are changed in `sandbox.toml`. Status
reports the allowlist with each host's source, the proxy's summary of requests by role
and host (requests, refusals and bytes each way, bounded at 512 role-host pairs with an
overflow count), and each VM's filter counters. It marks current observations with
`live: true`; after stop, a saved observation is historical and includes its timestamp.
The proxy writes one JSON line per request to the VM's `scratch/egress.jsonl` with the
role, host, port, outcome and byte counts, never request bodies or URLs beyond the
host. The filter's own counters still record outbound bytes by destination address;
with egress closed, bytes blocked there are a role trying to go around the proxy.
What a cutoff keeps open and what the records do not prove are in the
[sandbox design](../design/sandbox.md#security-boundaries).

## Development limits

Ordinary Linux accounts, one slice per role and per-role rootless daemons provide
filesystem and process separation within one shared kernel. Anything that needs
macOS, Xcode, a simulator or a desktop app happens on a
[leased Mac station](mac-station.md). Keep owner credentials and unrelated host
files outside the guests.

Verify a configured environment with the [acceptance tools](../tools/acceptance/README.md)
on disposable firms.
