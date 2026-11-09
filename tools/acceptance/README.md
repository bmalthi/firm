# Acceptance tools

These manual checks exercise the packaged Linux VM, the Mac lease, the file
server and the runtime. Run them from the repository root as
`uv run tools/acceptance/...`, which uses the root workspace environment and its
Firm packages; from inside `tools/`, uv selects a different project and the
imports fail. Use dedicated test firms and new evidence directories; these tools
create roles, interrupt processes and stop VMs. The dispatcher, cleanup, provider and
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

`agy_probe.py` investigates the native CLI independently of the integrated provider.
Replay and inspection request no inference and read no credentials. CLI inspection
runs help/version with a temporary HOME; SDK inspection reads installed metadata
without importing the SDK. Fixtures distinguish synthetic and sanitized captures.

```sh
uv run tools/acceptance/agy_probe.py replay --route cli --fixtures tools/tests/fixtures/agy
uv run tools/acceptance/agy_probe.py inspect --route cli \
  --executable /ABSOLUTE/PATH/agy --work-dir /tmp/FRESH-AGY-INSPECTION
uv run tools/acceptance/agy_probe.py inspect --route sdk
```

Host exercises require explicit authorization and can consume subscription quota.
Use a trusted CLI **1.5.3** and fresh disposable work directories outside Git.
The harness clears API/ADC credentials, disables credit overages and sets the
[auto-update opt-out](https://antigravity.google/docs/cli/troubleshooting/).
The default copies no credentials; a separate HOME is neither an OS sandbox nor a
keyring boundary. These host checks do not establish Linux/proxy acceptance.

```sh
uv run tools/acceptance/agy_probe.py exercise --route cli \
  --auth-kind subscription --executable /ABSOLUTE/PATH/agy \
  --work-dir /tmp/FRESH-AGY-EXERCISE --case isolated-home
```

`--case workspace-hook` seeds a SessionStart marker hook; execution fails the case.
An absent marker does not prove discovery isolation, especially after auth failure.
Each exercise has a 25-second outer deadline, bounded output and process-group
cleanup, including surviving descendants. Unconfirmed cleanup fails; deliberately
detached same-UID processes are outside this guarantee. `decision.json` is small;
vendor state/logs stay in the disposable directory. These cases do not cover MCP,
continuation, Linux workers, proxy access or all execution modes.

Prepare an isolated persistent profile to test sign-in:

```sh
uv run tools/acceptance/agy_probe.py prepare-login \
  --executable /ABSOLUTE/PATH/agy --profile-dir /tmp/FRESH-AGY-PROFILE
/tmp/FRESH-AGY-PROFILE/login.sh
```

Run the script in your terminal: its native
[`agy acp --login`](https://antigravity.google/docs/ide/extensions/acp#authentication)
flow opens Google's authorization URL; paste the returned code into that terminal,
never chat. Use the same subscription account as your everyday login: a separate
HOME does not prove a separate keyring, and switching accounts/logging out could
affect another session. The script clears credential/settings environment variables,
preserves OS identity and private paths, and does not export/copy/inspect credentials.

```sh
uv run tools/acceptance/agy_probe.py exercise --route cli \
  --auth-kind subscription --executable /ABSOLUTE/PATH/agy \
  --profile-dir /tmp/FRESH-AGY-PROFILE --work-dir /tmp/FRESH-AGY-AFTER-LOGIN \
  --case isolated-home
```

The profile disables overages and selects `firm-probe` with `tools: []` and
`inheritCustomizations: false`. A text-only pass requires valid success, the marker,
empty advertised inventory, zero tool calls and cleanup. CLI 1.5.3 advertises 60
tools for this agent, so inference success alone fails the conservative check.
Repeat `workspace-hook` with a fresh directory to check customization exclusion;
neither case proves all authority gates or transferable Linux authentication.

Add `--credential-mode access-only` to stage the dedicated profile's
`.gemini/antigravity-cli/antigravity-oauth-token` through Firm's private reader.
It copies access token/type/expiry, optional lifetime and auth method, blanks the
refresh token, omits email/unknown fields, and removes the staged file on exit,
including failures. Use a fresh work directory; never target ordinary HOME or copy
the profile into a guest. `missing` and `expired-access-only` are negative controls:
expected auth failures exit nonzero; inspect `analysis`. The exercise does not
modify source credentials or implement production renewal.

The `gates` matrix accepts only `/tmp/firm-agy-dedicated-profile` and stages
access-only credentials in fresh `/tmp/firm-agy-*` directories:

```sh
uv run tools/acceptance/agy_probe.py gates \
  --executable "$HOME/.local/bin/agy" \
  --profile-dir /tmp/firm-agy-dedicated-profile \
  --work-dir /tmp/firm-agy-models-UNIQUE --case models
```

Cases cover `models`, `authority`, `discovery`, `discovery-lifecycle`, `mcp`,
`durability`, `usage`, `lifetime`, `framing`, `cancellation`, `search`,
`launcher-mcp`, `expiry` and `renewal-yield`. Select independent rows with `--only`
(e.g. `--case models --only gemini-4-argon-high-effort-xhigh`, `--case mcp --only good`,
or `--case lifetime --only 301`). Durability, usage, discovery-lifecycle, expiry and
renewal-yield require dependent rows together. Ordinary rows have 35-second native
and 42-second outer deadlines; cancellation observes 15 seconds after SIGTERM and
group cleanup. Zero exit means evidence was collected, **not that gates passed**.
Full matrices consume quota. Keep settings, vendor state and progress/decision JSON
outside Git; receipts save authorization booleans, never bearer values.

* `models`: check Flash/Argon low/medium/high and `init.model` identity (results
  have no model field); native xhigh and conflicting model/effort combinations fail.
* `authority`/`discovery`: strict denies constrain file/shell effects, but empty
  tool declarations permit implicit `manage_task`. Hook controls and workspace
  instruction effects require separate checks; broader delegation exclusion is unproven.
* `search`: test role/task `search_web` with launcher settings and a public release
  date, saving sanitized events/status and answer checks without response prose.
  Search needs no allow entry; `read_url(*)` / `execute_url(*)` stay denied.
* `mcp`: use a loopback Streamable HTTP echo server to check authenticated eager
  tools, missing/wrong bearer, unavailable server, timeout and lazy controls.
  Missing calls and tool ERROR can coexist with SUCCESS; lazy MCP is unverified.
* `launcher-mcp`: use runtime helpers, symlinked credentials and all 42 Firm names
  as harmless echo tools. `--only exact`, `float-timeout` and `integer-timeout`
  compare settings: JSON `timeoutSeconds: 2.0` disables MCP; integer `2` works.
  The 60-tool init inventory and terminal SUCCESS do not prove MCP execution.
  `http-proxy` / `https-bypass` test integer settings against a rejecting proxy
  that records no headers/URLs; loopback MCP bypasses it, including with NO_PROXY.
* `durability`/`usage`: check recall and returned conversation ID (unknown IDs can
  start fresh). Usage is cumulative across turns/processes; reasoning is in output.
* `lifetime`/`framing`: startup requires more than 300 seconds of life. One NDJSON
  user event plus EOF works with or without a final newline; a partial trailing
  line can produce SUCCESS then ERROR/exit 1. `print_timeout` is not completion.
  Replay defaults to one turn; `expected_turns`, `expected_conversation_id` and
  `failed_tool_steps` expose multi-turn, identity and structured failure evidence.

`expiry` is an explicitly authorized real-token case: wait up to 75 minutes,
probe native renewal at 20/10/6½ minutes, then compare two tasks across the original
expiry with nine 60-second waits. One staged file is unchanged; the other gets a
fresh projection after two minutes. Both use `--print-timeout 0` and a 15-minute
outer deadline; resume/confirmation controls check recall and persistent ERROR
(at most five turns). Native renewal occurs inside five minutes; tokens last about
one hour. Replacement cannot extend a running turn, and expiry can leave persistent
ERROR. The startup margin does not bound running turns. Progress records contain
no tokens; staged files are removed after cleanup.

`renewal-yield` checks three native renewals three minutes apart and two 90-second
timeout/resume cycles on one conversation, plus a retry of an expiry-damaged
conversation if present. Only this case rewrites host cached expiry: it atomically
sets `token.expiry` to `2000-01-01T00:00:00Z` (0600), preserving other fields, then
runs `agy models` in the clean profile environment and restores billing settings.
Failure restores the original document only if the stale document is unchanged;
offline tests cover this. Native timeout can leave shell commands running: a
45-second observation before forced cleanup checks post-exit markers. Resumes
check identity and nonce recall; successful recall can coexist with terminal ERROR.
Captured fixtures omit responses, arguments, native IDs and diagnostics.

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
| `cleanup.py` | Deterministic programs through the real dispatcher: end-of-period handoff cleanup removing the founder's scratch and stopped container while its tagged image stays, expired temporary-role disposal that keeps the role employed and renewable, and fired-role disposal followed by guest and file-server identity retirement | `--root FIRM_DIR --evidence DIR [--keep-running]` |
| `provider.py` | Real configured model provider, guest credentials and runtime tools; AGY resume retains and checks the native session | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE [--provider codex\|claude\|agy] [--resume]` |
| `summary.py` | One real isolated correspondence summary and one hiring-level assessment through the runtime's summary gate, launch spec, guest launcher and parser, with no role activation; AGY additionally requires zero native tool steps | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE [--provider codex\|claude\|agy]` |
| `task.py` | Real provider tasks under a token-free placeholder parent: file answer and usage/model, worker-reported isolation, two concurrent tasks and busy refusal, detached-process timeout, a user-manager scope that leaves task and parent running, child-before-parent stop, and separate task analysis totals | `--root FIRM_DIR --evidence DIR --provider codex\|claude\|agy --provider-config PROVIDERS_TOML --profile PROFILE [--timeout 120]` |
| `docker.py` | Build/Compose, root and uid-1000 containers with bind mounts from the home and the drive and their host-visible owners, a published port reached from the role, the Docker Python SDK's ping, exec and log streaming from inside the role, dockerd killed mid-run still settling with an empty slice, scratch, stopped containers, dangling and tagged images and build cache surviving pause/resume, explicit disposal clearing scratch, stopped containers, dangling images and build cache while tagged images, named volumes, work and language caches stay, a top-level scratch symlink kept by settlement and replaced by the next launch without touching its target, and the resource readings: a container holding 300 MiB in that run's memory peak, a later small run's lower peak, and machine-wide samples answering while the Docker install holds the guest lock | `--root FIRM_DIR --evidence DIR [--role ROLE]` |
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

Checks stop their VMs on completion; `dispatcher.py` and `cleanup.py` `--keep-running`
explicitly leave their test firms running for inspection. `provider.py --resume` continues
against its original evidence database. Use each tool's `--help` for options.
AGY retains the native conversation on resume; Codex and Claude use
a fresh route for retries in this acceptance fixture.

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
AGY instead checks that its native auth symlink was removed by launcher cleanup
and that the remaining ephemeral `/run` credential is private, kind `agy-access`,
access-only, with no refresh token value, email, or unknown fields. This evidence
contains only the auth-file path, fixed labels, and booleans, never credentials.
This check does not cover Docker lifetime, tmux integration, host-loss watchdogs,
restart recovery, adversarial same-UID escapes or independent cancellation while
another task continues. Resource snapshots preserve the inclusive-task semantics;
they do not measure a separate task CPU/memory total.

## Experimental AGY integrated acceptance

This **live-phase recipe** uses the registered adapter, consumes subscription quota
and starts/stops disposable Linux VMs. Offline parser/evidence tests do not establish
integrated acceptance.

Prepare fresh directories, then save the following TOML as
`$agy_acceptance/firm.toml`:

```sh
umask 077
agy_acceptance=$(mktemp -d /tmp/firm-agy-acceptance-XXXXXX)
mkdir "$agy_acceptance/provider" "$agy_acceptance/summary" "$agy_acceptance/task"
```

```toml
max_active = 2
staffing = false
founder_profile = "agy"

[profiles.agy]
provider = "agy"
model = "gemini-3.8-flash"
effort = "medium"
accounts = ["agy-dedicated"]

[accounts.agy-dedicated]
provider = "agy"
max_active = 2
auth_file = "/tmp/firm-agy-dedicated-profile/home/.gemini/antigravity-cli/antigravity-oauth-token"

[summary.agy]
model = "gemini-3.8-flash"
effort = "medium"

[task.agy]
model = "gemini-3.8-flash"
effort = "high"
```

Reference the authenticated host profile directly; do not copy it or run login for
this recipe. The scripts reject other auth paths and `[google_cloud]`. Follow the
[host PATH and billing requirements](../../docs/installation.md#experimental-agy-integration),
using no API key/billing project or ordinary HOME/keyring state. Native `agy models`
renews under the dedicated HOME with a cleared environment. `firm login ... --auth-file`
forces native sign-in and does not edit `firm.toml`.

Save this `sandbox.toml` in each subdirectory, changing `id` to a unique lowercase
name for each fixture:

```toml
[firm]
id = "agy-provider-unique"
cpus = 4
memory_gib = 8
disk_gib = 60

[network]
egress = "enforce"
allow_hosts = []
```

Keep files private and omit `[mac]`. The guest's pinned AGY and subscription egress
are described in the [sandbox README](../../packages/firm-sandbox/README.md);
no Google wildcard or guest renewal host is needed. Initialize only sandbox markers;
these checks create their own runtime databases:

```sh
for case in provider summary task; do
  uv run firm-sandbox --config "$agy_acceptance/$case/sandbox.toml" init
done
uv run tools/acceptance/provider.py --root "$agy_acceptance/provider" \
  --evidence "$agy_acceptance/provider-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy
uv run tools/acceptance/provider.py --root "$agy_acceptance/provider" \
  --evidence "$agy_acceptance/provider-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy --resume
uv run tools/acceptance/summary.py --root "$agy_acceptance/summary" \
  --evidence "$agy_acceptance/summary-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy
uv run tools/acceptance/task.py --root "$agy_acceptance/task" \
  --evidence "$agy_acceptance/task-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy --timeout 120
```

Resume requires the first provider pass, its VM disk/evidence database and host-only
`recall-nonce.txt`. The mandate finishes without handoff (which clears the session)
and requests graceful shutdown after drain. Resume checks retained runtime/native
identity, returned init/result identity and exact recall of a nonce supplied only
in the first turn, without fresh routing.

Acceptance requires server-side peek receipts in `server-tools.json`, native Firm
MCP events and the workspace artifact; admission inbox reads do not count. Evidence
checks the actual `agy-auth` projection before sandbox launch and saves allowlisted
`init.model` and cumulative usage, excluding prose, arguments, native session IDs
and credentials. Quota/context are `unavailable`. Summary/chooser checks fail on
any native tool step, including implicit `manage_task`.

See [model/effort mapping](../../docs/runtime.md#experimental-agy-configuration)
for additional fresh cases. These scripts cover part of A5; revoked auth/MCP, VM
restart, two simultaneous real roles and other adversarial cases need separate live
evidence. Keep generated state outside Git.

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
