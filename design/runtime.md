# Runtime Design

> **TL;DR — How the Runtime Works**
> * **Emergent Org, Enforced Authority:** You give one **Founder** a mandate. Agents decide who to hire and how to coordinate; Python and the OS enforce reporting-line permissions and slot limits.
> * **Blind Model Tiering (`[staffing]`):** Managers only write a mandate when hiring—an isolated chooser assigns a `low`, `medium`, or `high` model tier and reasoning effort from the mandate alone.
> * **Single-Table Projection Scheduling:** Pending mail and execution requests project into one SQLite view (`scheduling_opportunities`), ordered by priority (Control > Owner Mail > Ordinary) and wait age.
> * **Host-Side Credentials:** OAuth refresh tokens and GitHub App private keys stay on the macOS host (`0600`); guests receive short-lived access tokens over a per-run `tmpfs` mount.
> * **Unified Owner Requests (`owner_requests`):** Hitting an owner-controlled gate (unlisted network domain, GitHub repo cap, or Mac station repair) returns `pending` and raises a decision card in the Owner Console.

---

## Vocabulary

| Term | Meaning |
| :--- | :--- |
| **Role** | Persistent employee identity (`founder`, `e1`, `e2`, …), organizational address (`founder/eng`), mandate, workspace, and mailbox. |
| **Provider session** | A retained conversation bound to a specific provider, model, and account. May span many activations. |
| **Activation / work period** | One admitted turn of execution, from delivering a wake prompt until normal completion, `handoff`, or failure. |
| **Wake** | Starting an activation, either by **resuming** a retained provider session or starting a **fresh session** with saved handoff/recovery notes. |
| **Handoff** | Saving continuation notes (`handoff(notes=..., next="continue" | "wait")`) so the next activation starts with a clean context window while keeping files and mail intact. |

---

## Hiring, Appointments, and Automatic Model Tiering

A manager hires direct reports with `hire(slug, mandate, request_id, temporary_periods=...)`, creating the direct report at `<manager_address>/<slug>`.
* **Durable vs. Temporary Appointments:** Hires are either durable or temporary for `1–1,000` work periods. A temporary work period is deducted at the execution launch gate, not at slot reservation, so pre-launch or provisioning failures do not burn an employee's turns. Managers can extend temporary roles (`renew_temporary`) or make them permanent (`promote`) in place.
* **Permanent Identities:** The founder is always `founder`; hires receive monotonic IDs (`e1`, `e2`, …) backed by an `fsync`'d high-water file beside SQLite so a rolled-back transaction cannot reuse an ID that already touched the filesystem.

### Hiring Levels

Neither the hiring manager nor the new employee chooses or sees model names (`[staffing]`). When a role is hired, an isolated, no-authority **staffing chooser** reads the hire's full mandate (plus bounded excerpts of surrounding mandates) and outputs `{model_class, effort, reason}`:

| Axis | Values | Default Provider Mapping |
| :--- | :--- | :--- |
| **Model Class** | `low`, `medium`, `high` | **Codex:** `gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra`<br>**Claude:** `sonnet` / `opus` / `claude-fable-5-1` |
| **Effort** | `low`, `medium`, `high`, `xhigh` | Passed to the provider CLI for reasoning depth. |

* **Non-blocking registration:** `hire` registers the role immediately on the fallback level (`medium` class, `high` effort) with state `pending`. Admission skips roles while `pending`, and the dispatcher runs the chooser turn before admitting work. After 3 failed assessments (or an oversized >256 KiB prompt on the first try), the role defaults to `medium`/`high` and Operations notifies the owner.
* **Owner override:** The owner can override any role's model/effort at any time via `firm route`.

### Key Design Trade-offs: Hiring & Staffing
* **Why an isolated chooser instead of letting managers pick models?** A manager agent has no cost signal or view of the owner's subscription budget; when asked to pick a model for a report, agents systematically pick the most expensive tier. The chooser runs on the cheapest tier (`low` class, `medium` effort) and assigns a starting tier from the mandate alone.
* **Why mutate roles in place on `promote` / `renew_temporary`?** Retiring a temporary employee and hiring a replacement would lose their workspace, mailbox history, and warm provider session.

---

## Mandates

The mandate is the single authoritative delegation document managed through MCP:
* **Strict reporting-line revision:** Managers can revise only their direct reports' mandates (`revise_mandate`, bound to expected revision number for compare-and-swap safety). Any role can read colleagues' mandates and public profiles (`get_role`), while no role can edit its own mandate.
* **Owner mandate revision:** When the owner revises the Founder's mandate, the runtime commits the revision alongside a top-priority (`priority = 2`) Operations change notice and wake for the Founder. Ordinary manager revisions do not interrupt or wake a direct report automatically; the updated mandate is injected on the report's next wake (or the manager can send a message if it needs immediate attention).

### Delivery to Context

| Situation | What the role receives |
| :--- | :--- |
| **Birth or fresh context** | Full current mandate, standing workplace instructions, and continuation notes (if post-handoff). |
| **Resume retained context** | New wake inputs plus any mandate revision not yet delivered to this session. |
| **Revision while running** | Running work is never interrupted; the revised mandate is delivered before the next work period. |

---

## Storage & Durable Memory

Every run mounts the firm's internal SMB file server at `<home>/drive`:

| Space | Guest Path | Access Policy |
| :--- | :--- | :--- |
| **Private Home** | `/home/firmrole-<hash>` | **Role only (`0700`).** Persistent tools, language caches, and personal notes. `~/scratch` is wiped after every activation. |
| **Role Workspace** | `<drive>/org/<address>/work` | **Reporting line only.** The role and its ancestor managers read/write; descendants read-only; siblings and cousins have zero access. |
| **Shared Space** | `<drive>/shared` | **Firm-wide.** Every active role can read and write. |

### Role-Authored Memory
Runtime telemetry is never dumped into agent workspaces. Instead, roles maintain three lightweight conventions in their workspace:
1. **`profile.md`** — Public identity and responsibilities (ingested host-side for `find_roles` search).
2. **`track-record.md`** — Append-only log of completed deliverables and where evidence lives.
3. **`playbooks/`** (with `playbooks/index.md`) — Reusable procedures and lessons learned.

### Firing and Retirement
Firing (`fire`) is direct-report only and refuses if the employee still has live descendants. It stops new admissions, cancels pending reminders, withdraws pending owner requests, and marks pending inbound mail undeliverable—while keeping all workspace files intact (downgraded to read-only `rx` for the reporting line).

---

## Scheduling

Scheduling is a deterministic SQLite projection (`scheduling_opportunities`) combining two sources:
1. **Pending message deliveries** (`message_deliveries`) with a non-null `scheduling_ready_at`.
2. **Explicit execution requests** (`execution_requests`) for birth, `handoff(next="continue")`, owner resume, and failure recovery.

### Priority & Fairness

| Priority Class | Rank | Examples |
| :--- | :--- | :--- |
| **Control** | `2` | Owner mandate revision to Founder; manager action required on a direct report's failure. |
| **Owner Mail** | `1` | Direct messages from the Owner. |
| **Ordinary** | `0` | Colleague mail, due reminders, and `handoff(next="continue")`. |

* **Oldest-ready wins within a priority class:** Multiple pending triggers for the same role coalesce into a single activation at the role's highest due priority.
* **No recurring wake from ignored mail:** Admission clears `scheduling_ready_at` on due deliveries when the work period starts. If a role leaves a message pending in its inbox without claiming or dismissing it, that old message will **not** endlessly re-wake the role after completion.
* **Reminders:** Roles can schedule future wakes with `set_reminder(due_at, note, request_id)` (up to 100 pending reminders per role). When due, a reminder becomes an ordinary Operations message and wake.

### Handoff and the Next Work Period

| How the role stops | Context on next activation | Wake behavior |
| :--- | :--- | :--- |
| **Normal completion** | Retained provider session | Waits for new mail or a due reminder. |
| **`handoff(notes, next="wait")`** | Fresh session + saved `notes` | Waits for new mail or a due reminder. |
| **`handoff(notes, next="continue")`** | Fresh session + saved `notes` | Immediately queues an ordinary wake opportunity (even with an empty inbox). |

---

## Provider Execution, Routing, and Context Policy

Named `[profiles.<name>]` configure provider (`codex` or `claude`), model, effort, context limits, and `[accounts.<id>]`.

### Session Binding & Account Selection
* **Retained sessions stay pinned:** A warm session is bound to its provider, account, and model settings; lowering an account's `max_active` steers *new* sessions away without stranding warm ones.
* **Fresh sessions pick the least-loaded route:** Fresh sessions rank the primary profile and its configured `alternates` by provider load, then account load (`live_sessions / max_active`), demoting any account whose observed remaining quota is below `min_fresh_fraction` (default `0.1`).

### Quota Observations & Holds
* **Display polling vs. admission:** The console polls subscription usage every 5 minutes for display only; poller failures or stale caches never block admission.
* **Stream-based threshold (`stop_admitting_at`):** If configured on an account, fresh structured rate-limit windows from provider streams pause new admissions when usage crosses the threshold until the window's reset timestamp.
* **Hard quota errors:** When a provider rejects a turn for quota exhaustion, the runtime refunds the role's retry attempt and places a timed (or owner-cleared) hold on the account.

### Context Policy

`context_window` is a soft runtime token allowance (default `500,000` for Claude, `256,000` for Codex):
* **Advisory (60% of window):** Surfaces a gentle context-usage note on tool responses.
* **Handoff Gate (`context_window - handoff_reserve`, default reserve `10,000` tokens):** Blocks claiming *new* pending mail and returns the handoff checklist, while leaving exact message lookup, file saving, and `handoff()` open so the role can finish cleanly without being killed mid-thought.

### Key Design Trade-offs: Execution & Context
* **Why wait on a quota-blocked warm session instead of migrating it to another account?** Neither Codex nor Claude supports transferring a server-side session across accounts or providers. Automatic migration would silently wipe the role's conversation memory; waiting preserves it (and the owner can explicitly `stop-role` -> `route --fresh` if they prefer a reset).
* **Why does reconfiguration only update context policy on live sessions?** Changing `context_window` or `handoff_reserve` is safe mid-conversation, whereas changing model or provider is an identity change that waits for a fresh session.

---

## Tasks

`task(instruction, request_id)` gives a role a bounded, single-turn text answer from a fresh worker session running in a child cgroup:
* **No Firm authority:** Task workers have standard file, shell, and search tools in the caller's Linux account, but **no** Firm MCP tools, no mail access, no conversation history, and no sub-delegation.
* **Slot borrowing:** A role's first active task borrows the waiting caller's own execution slot; a second concurrent task (`max_concurrent = 2`) requires a free slot on the same account.
* **Defaults:** Uses `[task.codex]` (`gpt-6.1-sol`, `medium`) or `[task.claude]` (`opus`, `medium`).
* **Why `task()` instead of provider-native subagents?** Provider-native subagents can inherit parent instructions, Firm MCP authority, and delegation capabilities. Launching an isolated worker via the guest controller guarantees identical, tool-restricted behavior across both Codex and Claude.

---

## Failure and Recovery

Every failure is classified from structured provider/sandbox evidence before charging the role's 4-attempt recovery budget (initial run + 3 retries at `1m`, `5m`, `30m` backoff):

| Failure Category | Runtime Action | Charges Role Attempt Budget? |
| :--- | :--- | :--- |
| **Invalid credentials / Auth** | Hold the account; notify Owner once. | **No** (refunded) |
| **Invalid config** | Hold the profile/account; notify Owner. | **No** (refunded) |
| **Quota exhaustion** | Hold the account (timed until reset, or indefinite); notify Owner. | **No** (refunded) |
| **Owner stop / Pre-launch exit** | Queue interrupted work for next start. | **No** (refunded) |
| **Explicit model refusal** | Hold the role; notify direct manager (or Owner for Founder). | Yes |
| **Transient / Unclassified crash** | Isolate to the role; retry on `1m / 5m / 30m` schedule. | Yes |
| **Transient provider setup (named account, before launch)** | Retry on the same schedule; hold `attempts_exhausted` after the fourth failure. | Yes |

### Recovery Notice
When a role restarts after an abnormal exit that actually ran, it receives a minimal recovery notice: the UTC detection time, whether session context was retained, the **last 5 claimed message IDs and subjects**, and a reminder to check workspace files before repeating side effects.

---

## Organization Discovery

Roles discover who owns what using three read-only, deterministic tools:
1. **`find_roles(query="", limit=10, cursor=...)`** — In-process IDF + phrase search across role `address`, display `identity`, full `profile.md`, and current `mandate`.
2. **`get_role(role, section="all" | "profile" | "mandate", cursor=...)`** — Returns exact role metadata, direct-report count, `profile.md`, current mandate, and workspace path (if in the caller's reporting line).
3. **`org_tree(root=None, depth=2, max_nodes=50, cursor=...)`** — Breadcrumb chain and depth-first hierarchy (defaults to the firm's root when `root` is omitted).

* **Signed stateless pagination:** Cursors are HMAC-signed digests of the underlying source text (`d1`, `d2` handles per activation). If a mandate or profile changes mid-browse, pagination restarts cleanly rather than serving a torn view.
* **Delta orientation:** Fresh sessions receive a bounded neighborhood snapshot (manager, direct reports, and top-level roles). On later wakes, the snapshot is re-sent only when the neighborhood changed.

---

## External Capabilities & Owner Requests

### Unified Owner Requests (`owner_requests`)
When a role calls a tool that hits an owner-controlled gate, the tool opens a pending row in `owner_requests` (`{request_id, kind, resource, purpose, cause}`), sends a macOS notification (rate-limited to once per 10 minutes per role/kind), and returns `{status: "pending", request_id, cause}`. When the owner grants or denies it in the console or CLI (`firm requests <dir> grant|deny <id>`), the runtime applies the change and wakes the role with an Operations notice:

| Request Kind | Triggered By | What Granting Does |
| :--- | :--- | :--- |
| **`network_host`** | `request_access(kind="network_host", resource="api.example.com", ...)` | Adds the domain (or `*.wildcard`) to `network_hosts` and live-reloads the VM egress proxy allowlist without restarting VMs. |
| **`github_repository`** | `create_repository` (when `auto_create = false` or cap reached) or `request_access` on a repo outside the firm's grants | Mints a short-lived admin token to create the private repo, or adds an `owner` write grant in `github_grants`. |
| **`mac_station`** | `mac_acquire` when all free stations need recovery (`mac_needs_recovery`), enrollment (`mac_not_enrolled`), or session repair (`mac_session_not_ready`) | Answers all pending Mac requests once the owner has repaired/recovered the Mac VM. |

### GitHub Access (`[github]`)
* **Host-owned GitHub App:** Registered once per GitHub organization via browser manifest flow (`~/.config/firm-runtime/credentials/github-<org>.pem`). The private key never enters any VM.
* **Per-activation 1-hour tokens:** Linux roles get a `github-credential` helper and `/usr/local/bin/gh` wrapper that call the host's `/github/token` endpoint using the run's activation token. Minted tokens are scoped to the role's granted repositories and restricted to `contents`, `pull_requests`, and `issues` (never `administration`).
* **Durable revocations:** `firm github revoke <dir> <repo>` records a standing refusal in `github_revocations` so a role whose push fails and immediately re-requests access gets a pending Owner Request (`github_owner_revoked`) instead of an automatic re-grant.

### Generative Images (`[google_cloud]`)
* When `[google_cloud]` names a project, the host stages `~/.config/firm-runtime/credentials/google-cloud-<project>.key` into the Linux role's per-run credentials directory as `GEMINI_API_KEY` on each launch. Spend caps are enforced in Google AI Studio.

### The Mac Lease (`mac_acquire` / `mac_release` / `mac_status`)
* A firm with `[mac]` in `sandbox.toml` exposes `1 to 8` macOS desktop stations (`firmstation1..8`). Any Linux role can lease one free station at a time during an activation.
* The runtime brokers the `desktop_*` tool suite (`desktop_launch`, `desktop_inspect`, `desktop_action`, `desktop_capture`, pointer/key input, and `desktop_close_apps`) over an authenticated relay to the station's signed Swift helper, saving screenshots automatically to `org/<address>/work/captures/<timestamp>-<app>.png`. (See [Sandbox Design](sandbox.md#the-mac-lease) for the 4-check release sweep.)

---

## Owner Interface, Console, and Lifecycle

### CLI & Lifecycle Commands

| Command | Behavior |
| :--- | :--- |
| `firm init <dir>` | Validate mandate and `firm.toml`, write `firm-release.json`, and create the stopped firm with its `founder`. |
| `firm start [<dir>]` | Without `<dir>`: interactive setup wizard. With `<dir>`: launch or reconnect to the detached local host and open the Owner Console (`--foreground` attaches to terminal). |
| `firm stop <dir> [--force]` | **Soft drain** (default): close admission, ask active roles to save a `handoff`, then shut down VMs. **`--force`**: terminate active runs immediately and shut down VMs. |
| `firm status` / `firm analyze` | Read-only state summary or windowed JSON telemetry (`overview`, `inbox`, `readiness`, `timeline`, `usage`, `conversations`, `mac`, `resources`, `network`; omit `--section` for all sections). Works offline without booting VMs. |
| `firm export` / `firm discard-vms` | Stream the database, host logs, and Linux `/srv/firm` drive into `<dir>/export/` (verified by SHA-256 `manifest.json`). `discard-vms` exports with role homes and deletes the VM disk images while keeping the firm inspectable via `firm analyze`. |

### Soft Drain and Hard Stop
* **Soft drain (`firm stop <dir>`):** Closes new admission, asks running roles to finish their current step and save continuation notes via `handoff()`, releases any active Mac station leases cleanly, and shuts down VMs once all slots drain.
* **Hard stop (`firm stop <dir> --force`):** Closes admission, immediately terminates active guest runs, and shuts down VMs; interrupted work is re-queued for the next start without charging the role's retry budget.

### Analysis
* `firm analyze` computes read-only telemetry over a time window (`--from` / `--until`) from SQLite records and guest run journals (`overview`, `inbox`, `readiness`, `conversations`, `timeline`, `usage`, `mac`, `resources`, `network`, or omit `--section` for the full report).
* Telemetry reports objective execution facts (wake triggers, tool-call counts by name, token usage, message claim delays, cgroup peak memory, and settlement reclaim counts) without using an LLM to judge whether a deliverable meets the mandate.

### The Local Owner Console (`http://127.0.0.1:8766/`)
* **Bookmarkable, owner-only auth:** Loopback is shared by local processes, so the console requires an `HttpOnly`, `SameSite=strict` session cookie initialized via `/unlock/<key>`. The key is generated once in `durable/console-key` (`0600`), keeping the console URL stable across restarts.
* **Sandboxed file previews:** Untrusted message text renders via `textContent`. HTML deliverables render inside an opaque-origin `<iframe sandbox>` with no network, forms, or parent access; Markdown and text render in a script-free frame (`frame-ancestors` locked to the console).
* **Refusal-only compatibility (`firm-release.json`):** Firms and VM images share a single compatibility generation integer. `Store(path)` validates the marker before opening SQLite and refuses mismatched generations rather than running schema migrations.
