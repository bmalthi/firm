# Firm sandbox

The package implements one retained Linux VM per firm, where every role is an
ordinary account with its own home, systemd slice and rootless Docker daemon and
the firm's file server serves the shared trees, plus one optional Mac VM. Each
station is an account and desktop in that Mac, leased to one activation at a time. It owns execution mechanics
and has no scheduler or provider dependency.

`Sandbox` owns the public lifecycle API and orchestrates the VMs, the file server,
the tunnels and the Mac stations. `LinuxVM` and `MacVM` each boot, stop and remove
their VM and carry requests to its guest controller. `Sandbox.mac` is the shared
Mac; its image determines the account inventory. The Linux guest controller owns accounts, slices, run
journals, the per-run drive mount and the file server's steps. `MacStation` owns one
station's Mac lease: baseline, drive mount, per-lease SSH key and tunnel, and a
verified release. `Layout.for_role` gives a role's guest paths without a VM.

## Setup and operation

Create a firm using the [Linux VM guide](../../docs/linux-vm.md). `sandbox.toml`
sizes the Linux VM:

```toml
[firm]
id = "development"
cpus = 8
memory_gib = 16

[network]
egress = "enforce"
allow_hosts = []
```

`[network]` names hosts the VMs may reach beyond the built-in baseline; `observe` logs
every host without refusing any. See the Linux VM guide's
[network section](../../docs/linux-vm.md#network-access-and-cutoff).

To add a Mac, add a `[mac]` table naming an enrolled Mac image by absolute path. The
one Mac defaults to 8 CPUs and 24 GiB shared by all its station accounts; the station
count comes from the image, not this table. See the
[Mac station guide](../../docs/mac-station.md) for leasing and desktop use, and the
[Mac VM image guide](../../docs/mac-vm-images.md) to build and enroll the image.

```toml
[mac]
image = "/absolute/path/to/enrolled-image"
cpus = 8
memory_gib = 24
```

Run from the repository root:

```sh
uv run firm-sandbox --config /absolute/firm/sandbox.toml init  # new sandbox-only firm
uv run firm-sandbox --config /absolute/firm/sandbox.toml up
uv run firm-sandbox --config /absolute/firm/sandbox.toml status
uv run firm-sandbox --config /absolute/firm/sandbox.toml down
uv run pytest -q -m "not integration" packages/firm-sandbox/tests
```

The Linux VM guide says when to use `firm-sandbox` and when the runtime's `firm`
CLI; all VM lifecycle operations go through `Sandbox`.
Runtime-managed firms already receive the same release marker from `firm init`.
Sandbox-only `init` is for a new empty-state firm (such as image enrollment); it
refuses to mark existing unversioned VM state as compatible.

The guest recipe pins AGY CLI **1.5.3** (experimental, Linux ARM64 glibc) and
verifies its vendor SHA-512 before extraction. `src/firm_sandbox/assets/linux-vm.yaml`
records the URL/checksum from the
[official installer's](https://antigravity.google/cli/install.sh)
[ARM64 manifest](https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_arm64.json).
Boots reuse matching installations without a manifest lookup. `/usr/local/bin/agy`
wraps root-owned `/usr/local/lib/agy/agy`, enforcing `AGY_CLI_DISABLE_AUTO_UPDATE=true`, the
[documented opt-out](https://antigravity.google/docs/cli/troubleshooting/).
Readiness checks the executable and root-owned `/usr/local/lib/agy/version` stamp,
written after verified installation; metadata-only boot checks do not launch providers.

Subscription egress allows exact hosts `www.googleapis.com` and
`daily-cloudcode-pa.googleapis.com`. Archive host `storage.googleapis.com` is setup-only:
authenticated `_provision` requests before guest readiness; roles need an owner grant.
OAuth/login, updater manifests, avatars and telemetry are excluded from the baseline.
Under the launcher's HOME, `.gemini/antigravity-cli/` holds access-only
`antigravity-oauth-token` and CLI `settings.json`; `.gemini/config/` holds shared
`config.json` and `agents/`. Renewable login state stays on the host.
Keep [credit overages disabled](../../docs/installation.md#experimental-agy-integration).
Packaging alone does not establish provider acceptance.

### Operator verbs

All operations use `--config PATH` (default `sandbox.toml`), except
`mac-image-build`, which needs only its destination and `--recipe`.

- Lifecycle: `up`, `down`, `force-down`, `replace`, `mac-replace`, `mac-restart`, `run`, `start`,
  `stop`, `wait`, `logs`, `remove`, `retire`, `dispose`.
  `dispose ROLE` stops a role's containers, removes them with dangling images and ordinary
  build cache, and empties its scratch while the role has no running or unresolved run,
  for active and retired accounts alike; tagged images, volumes and the rest of the
  home stay. `retire` only tombstones the identity and preserves its files.
- Inspection: `status`, `ps`, `resources`, `storage [ROLE]`, `events`, `state`, `data-status`,
  `network-status`; `network [status|on|off|hosts]` also controls egress and lists the
  allowed hosts. `storage [ROLE]` estimates scratch and Docker usage with `du` for every account,
  retired ones included, under one 60-second budget and without blocking runs; see
  `storage --help` and the [Linux VM guide](../../docs/linux-vm.md#roles-on-the-linux-vm). Hosts are granted
  through the runtime's `firm network` and `firm requests`, which keep the record.
- Mac image and accounts: `mac-image-build DEST --recipe R`,
  `mac-prepare APP --recipe R`, `mac-install-helper APP`, `mac-image-apply R`,
  `mac-image-seal DEST --recipe R`, `mac-status [--station N]`,
  `mac-session-reset --station N`, `mac-recover --station N --confirm-cleanup`.
  Helper installation and `mac-restart` affect the shared Mac and refuse under any
  lease. Session reset affects only the selected account. Repeated `mac-prepare`
  calls guide the owner through each account's console turn, then return to `firmvm`.


`run` and `start` take `ROLE COMMAND...` plus optional `--env`, `--secret`,
`--secret-env` and `--stdin` flags before the role. ROLE is the runtime's sandbox
role ID, `r-` followed by the employee ID. `stop`, `wait`, `logs` and
`remove` take a role and select its current run. `logs` returns all retained
output; `run` follows output while the command executes. `replace` discards every
guest disk; see [retained files](../../docs/linux-vm.md#retained-files) before using
it.

## Persistence and boundaries

The Mac disk and machine record live at `durable/mac/`; account records live at
`durable/mac/stations/N/`. Guest disks survive shutdowns; what
each holds is in the [Linux VM guide](../../docs/linux-vm.md#retained-files). The
[sandbox design](../../design/sandbox.md) describes the model and its
[security boundaries](../../design/sandbox.md#security-boundaries).

The [acceptance catalog](../../tools/acceptance/README.md) covers live execution,
Docker inside a role, the Mac lease, the network boundary and recovery. Tests run
the guest controller in simulation and use fake transports to verify contracts;
actual daemons, mounts and application workflows require live acceptance on a
disposable firm.
