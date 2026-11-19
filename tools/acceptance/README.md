# Acceptance tools

These manual checks exercise the packaged Linux VM, the Mac lease, the file
server and the runtime. Run them from the repository root as
`uv run tools/acceptance/...`, using the shared workspace environment and its
Firm packages. Use dedicated test firms and new evidence directories; these tools
create roles, interrupt processes and stop VMs. The dispatcher, cleanup, provider and
summary checks require a firm never bound to another runtime database; provider
`--resume` reuses its original firm and evidence.

These scripts describe checks to run, not evidence that a release has passed them.

Resource history still needs live validation of PSI and memory accounting under
`user.slice`, OOM event reporting, per-VM attribution and Apple Silicon host CPU
readings. GitHub access has no automated acceptance script: validate App registration,
repository creation, role grants/refusals, clone/push, token renewal and revocation
manually on a disposable firm, and record results for the release candidate.

VM checks take `--root FIRM_DIR`, the disposable firm directory containing
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
to validate the fixture before `requests.py`. Do not run `firm init`
on that fixture. The network check instead requires an already initialized,
stopped firm. Both check owner-controlled directories before touching sandbox state. Other
dispatcher/provider checks keep their disposable runtime database in the evidence
directory and require a sandbox that has not been bound to a different database.

| Tool | Coverage | Arguments |
| --- | --- | --- |
| `linux_base.py` | Boot timing and independent VM identities | `--root NEW_PARENT --evidence DIR [--count N] [--parallel N]` |
| `execution.py` | Concurrent Linux roles, isolation, stop, retention and replacement | `--root FIRM_DIR --evidence DIR --replace` |
| `dispatcher.py` | Guest MCP, hiring, shared files and workspace permissions | `--root FIRM_DIR --evidence DIR [--keep-running]` |
| `cleanup.py` | Role disposal, scratch cleanup and identity retirement | `--root FIRM_DIR --evidence DIR [--keep-running]` |
| `provider.py` | Configured provider execution, credentials, tools and resume | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE [--provider codex\|claude\|agy] [--resume]` |
| `summary.py` | Isolated summarization and hiring assessment | `--root FIRM_DIR --evidence DIR --provider-config PROVIDERS_TOML --profile PROFILE [--provider codex\|claude\|agy]` |
| `stateless.py` | Reusable tool-free account isolation, cleanup, cancellation and restart without inference | `--root FIRM_DIR --evidence DIR` |
| `llm_cli.py` | Host-brokered guest LLM CLI execution, resume, concurrency and cleanup | `--root FIRM_DIR --evidence DIR --provider codex\|claude\|agy --provider-config PROVIDERS_TOML --profile PROFILE [--timeout 120]` |
| `docker.py` | Containers, mounts, resource accounting and cleanup | `--root FIRM_DIR --evidence DIR [--role ROLE]` |
| `browser.py` | Native or containerized browser rendering and exported artifacts | `--root FIRM_DIR --evidence DIR [--role ROLE] [--mode native\|docker] [--chromium-sandbox] [--offline-reuse]` |
| `network.py` | Egress enforcement, revocation, restart retention and Mac proxy policy | `--root FIRM_DIR --evidence DIR [--station N]` |
| `requests.py` | Owner access requests, grant, denial and revocation | `--root FIRM_DIR --evidence DIR` |
| `recovery.py` | Runtime loss, adoption and settlement | `--root FIRM_DIR --evidence DIR` |

For native browser setup, run `browser.py` with a new role and evidence directory,
repeat with the same role and a new evidence directory for warm setup, then repeat
with `--offline-reuse`. The offline check disables Linux egress for the run, skips
installers and uses `uv run --offline --no-sync`; cleanup restores the earlier egress
setting. It requires that role's successful prior installation. Results report
`setup_steps` with SDK/browser installer timings and exit codes, `setup_failure`,
`project_existed`, rendering time and traffic snapshots before/after the workload.
On a base with shared browsers, native mode adds the recorded Playwright version
(`shared_browser_version`) and fails `shared_browser_used` if the role downloaded a
Chromium of its own (`own_browser_downloads`); on an older base it installs both itself.
A new project alone does not prove empty package/browser caches: use a new role
for fresh setup. Run these checks sequentially and retain separate evidence.
The tool exercises trusted local HTML; it does not establish safety for arbitrary
untrusted pages or prove that browser CDNs are allowed in enforce mode.

To force a Linux base rebuild before `linux_base.py`, delete
`~/Library/Caches/firm/linux-base/`.

For `network.py` and `requests.py`, explicitly set `[network] egress = "enforce"`
in the fixture's `sandbox.toml` before starting: these suites test hostname refusals
and owner grants, while the default `"observe"` allows all public HTTP/HTTPS hosts.

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

See [provider acceptance](providers.md) for provider, summary and LLM CLI setup,
including AGY login and resume requirements.

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
