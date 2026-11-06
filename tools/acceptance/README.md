# Acceptance tools

These manual checks exercise the packaged Linux VM, the Mac lease, the file
server and the runtime. Run them from the repository root as
`uv run tools/acceptance/...`, which uses the root workspace environment and its
Firm packages; from inside `tools/`, uv selects a different project and the
imports fail. Use dedicated test firms and new evidence directories; these tools
create roles, interrupt processes and stop VMs. The dispatcher, provider and
summary checks require a firm never bound to another runtime database; provider
`--resume` reuses its original firm and evidence.

Every check takes `--root FIRM_DIR`, the disposable firm directory containing
`sandbox.toml`, and `--evidence DIR`, where it writes its results and logs.

The owner console has a synthetic real-browser acceptance that needs no firm
configuration, provider, or VM. It creates a temporary firm and fake file server,
then checks the five destinations, messaging and Mark done, owner decisions and recovery,
the Firm menu, attachments and reader follow-up, downloads, Markdown rendering and the
HTML preview boundary in Chromium. Run it from the repository root:

```sh
uv run --with playwright tools/acceptance/console_browser.py
```

If Chromium is not installed in Playwright's cache, install its official browser
once with `uv run --with playwright playwright install chromium`. To use an
already-installed Chromium binary, set `FIRM_ACCEPTANCE_CHROMIUM` to its path.
The script prints a JSON summary and exits nonzero on an acceptance failure.

Set up a firm using the [Linux VM guide](../../docs/linux-vm.md). None of the
checks below needs `[mac]`; when the firm configures Mac stations, `network.py`
also probes the Mac VM's egress and host denial and reports them as skipped
otherwise. The [Mac lease checks](mac/README.md) need an enrolled Mac image from the
[Mac station guide](../../docs/mac-station.md). Roles run on the Linux VM;
Docker is each role's own rootless daemon there, and bind mounts from the role's
home and drive work as on any Linux host.

The request check creates the runtime database itself: prepare only a fresh
`sandbox.toml`, then run `uv run firm-sandbox --config FIRM_DIR/sandbox.toml init`
to create its compatibility marker before `requests.py`. Do not run `firm init`
on that fixture. The network check instead requires an already initialized,
stopped firm. Both validate the marker before touching sandbox state. Other
dispatcher/provider checks keep their disposable runtime database in the evidence
directory and require a sandbox that has not been bound to a different database.

| Tool | Coverage | Arguments |
| --- | --- | --- |
| `execution.py` | Two concurrent Linux roles with separate homes and daemons, account/host isolation, independent stop with an empty slice and its handed-over resource usage, filter loss, restart retention and whole-firm replacement | `--root FIRM_DIR --evidence DIR --replace` |
| `dispatcher.py` | Deterministic programs through the real dispatcher: authenticated guest MCP, in-guest hiring, shared files, and private-home and manager-workspace write denial | `--root FIRM_DIR --evidence DIR [--keep-running]` |
| `provider.py` | Real configured model provider, guest credentials and runtime tools | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE [--resume]` |
| `summary.py` | One real isolated correspondence summary and one hiring-level assessment through the runtime's summary gate, launch spec, guest launcher and parser, with no role activation; a failure names each run's recorded reason and how to read its guest output | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE` |
| `task.py` | Real provider tasks under a token-free placeholder parent: file answer and usage/model, worker-reported isolation, two concurrent tasks and busy refusal, detached-process timeout, a user-manager scope that leaves task and parent running, child-before-parent stop, and separate task analysis totals | `--root FIRM_DIR --evidence DIR --provider codex\|claude --provider-config PROVIDERS_TOML --profile PROFILE [--timeout 120]` |
| `docker.py` | Build/Compose, root and uid-1000 containers with bind mounts from the home and the drive and their host-visible owners, a published port reached from the role, the Docker Python SDK's ping, exec and log streaming from inside the role, dockerd killed mid-run still settling with an empty slice, scratch and stopped-container reclaim with tagged images, named volumes, work and language caches retained, top-level scratch symlink settlement preserving its private target, and the resource readings: a container holding 300 MiB in that run's memory peak, a later small run's lower peak, and machine-wide samples answering while the Docker install holds the guest lock | `--root FIRM_DIR --evidence DIR [--role ROLE]` |
| `browser.py` | A Compose project in the role's home, with only rootless Docker: a Python HTTP backend and the pinned official Playwright image, whose headless Chromium waits for a title set by JavaScript from backend data, reads an element's text, captures the console and takes a full-page screenshot checked as a PNG of the expected size; Compose exits 0 and cleans up, the run settles with an empty slice and the VMs stop | `--root FIRM_DIR --evidence DIR [--role ROLE]` |
| `network.py` | Closed egress, on a firm after `firm init` that is not started: from a role shell and a container, direct addresses, unproxied HTTP and DNS refused (DNS within 5 s), a baseline host reached through the proxy and `example.com` refused with a 403 the proxy log attributes to the role; the filter's blocked bytes for the direct attempts, host sentinel denial, `firm network allow` and `revoke` live with `network.hosts_changed`, a revoke closing the role's held tunnel to that host within 5 s while a held baseline tunnel survives, a leased station's proxy and `scutil --proxy` (skipped without `[mac]`), `firm network off` blocking the proxy path and retained across restart, `on` restoring it, named-volume retention and host–guest clock drift | `--root FIRM_DIR --evidence DIR [--station N]` |
| `requests.py` | Owner requests through the real dispatcher, on a fresh firm whose runtime database it creates: a deterministic role's pending `request_access` in `firm analyze` inbox and readiness (`owner_request_pending`), the console's `needs_you` and `firm requests`; `grant` waking the role with Operations mail and the host reachable with no restart, shown in the `network` section and the proxy log; `deny --reason` answering `access_denied` with the note, for a host under the denied wildcard too; `firm network revoke` withdrawing the request | `--root FIRM_DIR --evidence DIR` |
| `recovery.py` | Runtime loss, run adoption, settlement with an empty slice and retained container output | `--root FIRM_DIR --evidence DIR` |

The resource history has not yet run live. `docker.py` records the facts its readings
assume (`uname -r`, `user.slice/cgroup.subtree_control`, `/proc/pressure/memory` and
whether `systemd-oomd` is active) in `result.json`, and `mac/xcode_build.py` records a
sample taken while the lease is held. Confirm on the first live run that PSI and memory
accounting are enabled under `user.slice`, that no `systemd-oomd` kill is missing from
`memory.events`, and that each virtual-machine process is attributed to the right VM: the
Mac report has `mac_attributed` and `linux_attributed` beside the total over every VZ
process. If attribution fails, read each VM's `host_footprint_bytes` as unknown and use
that total instead. `host_cpu_usec` comes from Mach time units converted with
`mach_timebase_info`; check one reading against `ps -o time=` for the same process before
trusting Apple Silicon CPU numbers.

GitHub access has no script: it is exercised manually on a disposable firm with
`[github]` configured (registering the App from a manifest, creating a
private repository on request, cloning, pushing a branch and opening a pull request as the
App's bot with the role as commit author, plus testing configured `firm.toml` grants,
refusal for an ungranted role, the repository cap, a token refreshed past its hour, and
`firm github revoke`). Confirm that GitHub accepts the loopback redirect and the
manifest's `administration` permission, that an Administration-only token can create
a repository reachable at once, and that `gh` and the credential helper work in the
Linux VM. Record results for the release candidate being tested.

For example, the execution check requires a new disposable firm. It deliberately
replaces the firm and verifies that the previous generation's files are gone:

```sh
uv run tools/acceptance/execution.py \
  --root /PRIVATE/NEW-TEST-FIRM --evidence /PRIVATE/NEW-EVIDENCE --replace
```

Checks stop their VMs on completion; `dispatcher.py --keep-running` explicitly
leaves its test firm running for inspection. `provider.py --resume` continues
against its original evidence database. Use each tool's `--help` for options.

Run `task.py` once per provider with a fresh, unbound **Linux-only** firm and a new
evidence directory each time. The selected profile must match `--provider`; its
accounts need `max_active >= 2`. It uses `[task.PROVIDER]` model/effort from the
provider config, or runtime task defaults, and admits six real workers. `--timeout`
sets each task's deadline, including provider startup (minimum 30 seconds). It
prints PASS/FAIL for all eight checks and exits nonzero on failure, including an
unreached check or failed cleanup. Refusing the requested fixture or returning
early fails the check; no placeholder replaces a task provider.

`result.json` records the random file answer, worker-output checks, overlapping
barriers, cgroup/PID observations and the parent's SIGTERM receipt. Analysis
snapshots retain `coverage` and `role_labels`; `provider-output.json` keeps output
hashes/counts and isolation flags, never raw provider text, tool arguments or
credentials. Busy refusal is replayed after capacity frees; it creates no task
row. Parent token usage remains unknown in analysis because the placeholder emits
no provider telemetry. Isolation is based on the worker's own answer, instruction
sentinels and visible provider tool events, not inspection of hidden prompts.
After the answer worker exits, before its home is removed, `auth_files` verifies
Codex `auth.json` or Claude `.credentials.json` is still a symlink resolving into
`/run`. Claude token-only auth instead requires that no home auth file exists.
This evidence contains only the auth-file path and booleans, never credentials.
This check does not cover Docker lifetime, tmux integration, host-loss watchdogs,
restart recovery, adversarial same-UID escapes or independent cancellation while
another task continues. Resource snapshots preserve the inclusive-task semantics;
they do not measure a separate task CPU/memory total.

[Mac lease acceptance](mac/README.md) covers the enrolled Mac station, a role's lease
with an app and the Metal compute probe built on the Mac into the drive,
browser input/capture and the lease through the real dispatcher. The Metal probe
and the iOS build fixture live in [fixtures](fixtures/), and what more than one of
those checks needs lives in [`mac/acceptance_common.py`](mac/acceptance_common.py),
which is not itself a check.

The file server's per-role mount, ACL enforcement and archiving run as an
integration test on a disposable firm:

```sh
FIRM_LIVE=1 FIRM_SANDBOX_CONFIG=/PRIVATE/CLONE/sandbox.toml \
  uv run pytest -q packages/firm-sandbox/tests/test_file_server_integration.py -m integration
```

These manual checks complement the package regression suite:

```sh
uv run pytest -q -m "not integration"
```

Keep only checks that exercise the supported execution model. Capture live
results outside source control; update this catalog when an entry point changes.
