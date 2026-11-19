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
| **Handoff** | Saving continuation notes (`handoff(notes=..., next="continue" \| "wait", clean_up=false)`) so the next activation starts with a clean context window while keeping drive files and mail intact; with `clean_up`, scratch and scoped Docker state are disposed of. |

---

## Hiring, Appointments, and Automatic Model Tiering

A manager hires direct reports with `hire(slug, mandate, request_id, temporary_periods=...)`, creating the direct report at `<manager_address>/<slug>`.
* **Durable vs. Temporary Appointments:** Hires are either durable or temporary for `1–1,000` work periods. A temporary work period is deducted at the execution launch gate, not at slot reservation, so pre-launch or provisioning failures do not burn an employee's turns. Managers can extend temporary roles (`renew_temporary`) or make them permanent (`promote`) in place. When a temporary appointment's final work period ends in completion, or in a refusal or unclassified failure (not a runtime stop — inactivity timeout, owner stop or host restart — nor a transient, quota, auth or configuration failure), the dispatcher's idle-role loop also disposes of the role's scratch and scoped Docker state, once per such period. The role stays employed with identity, drive workspace and working memory unchanged, and can still be renewed or promoted; a renewed role starts with an empty scratch.
* **Permanent Identities:** The founder is always `founder`; hires receive monotonic IDs (`e1`, `e2`, …) backed by an `fsync`'d high-water file beside SQLite so a rolled-back transaction cannot reuse an ID that already touched the filesystem.

### Hiring Levels

Neither the hiring manager nor the new employee chooses or sees model names (`[staffing]`). When a role is hired, an isolated, no-authority **staffing chooser** reads the hire's full mandate (plus bounded excerpts of surrounding mandates) and outputs `{model_class, effort, reason}`:

| Axis | Values | Default Provider Mapping |
| :--- | :--- | :--- |
| **Model Class** | `low`, `medium`, `high` | **Codex:** `gpt-6.1-terra` / `gpt-6.2-sol` / `gpt-6.2-astra`<br>**Claude:** `sonnet` / `opus` / `fable`<br>**AGY:** `gemini-4-hydrogen` / `gemini-4-neon` / `gemini-4-neon` (`xhigh` → `gemini-4-carbon-high`) |
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

## Credential backend boundaries

`credential_store.py` is the public account credential facade. Its explicit backend
selection depends only on the provider name. `claude_auth.py` and `codex_auth.py` own
native parsing, validation, access-only projections, expiry scheduling, renewal eligibility,
and OAuth field mapping; `oauth_auth.py` shares HTTP transport and renewal race handling.
`agy_auth.py` keeps its dedicated retained profile and native snapshot/recovery state machine.
`credential_files.py` owns owner-private bounded reads, the existing account lock, and
same-directory atomic saves with compare-before-replace. Provider launch modules do
not own credential readers, and read-only quota polling never invokes renewal.

`CredentialStore.snapshot(force=False, *, margin=DEFAULT_MARGIN)` uses a 600-second
HTTP OAuth trigger or a 3,000-second AGY trigger by default. Explicit `margin=None`
projects without age-triggered renewal while retaining AGY interrupted-write and expiry
backup recovery. A numeric margin must be finite and non-negative. HTTP renews at
equality; AGY accepts equality. The trigger does not guarantee a minimum lifetime:
AGY preserves its transient-failure fallback when more than 300 seconds remain.
AGY validation uses `margin=None`, launch staging uses 301 seconds, and credential
requests use the default. Claude/Codex spec construction remains read/project only.

The standalone guest mirror and launchers do not import host backends. The credential
wire shape stays `{kind, data, expires_at}` with Unix-second or unknown expiry and
access-only allowlists; native refresh authority stays on the host. External writers
that ignore the lock retain the existing compare/read/replace guarantee, rather than
an atomic coordination promise. No persisted credential formats change.

---

## Storage & Durable Memory

Every run mounts the firm's internal SMB file server at `<home>/drive`:

| Space | Guest Path | Access Policy |
| :--- | :--- | :--- |
| **Private Home** | `/home/firmrole-<hash>` | **Role only (`0700`).** Persistent tools, language caches, and personal notes. `~/scratch` persists between activations. |
| **Role Workspace** | `<drive>/org/<address>/work` | **Reporting line only.** The role and its ancestor managers read/write; descendants read-only; siblings and cousins have zero access. |
| **Shared Space** | `<drive>/shared` | **Firm-wide.** Every active role can read and write. |

### Disk Pressure
* **Owner mail from samples:** The latest Linux reading outside the 80–85% band holds the alert state (at or above 85% sets it, below 80% clears it), so sampling gaps and host restarts do not reset it. The mail commits with the sample that flips the state; an unknown sample sends none.
* **Role notices:** Every role's inbox carries a low-disk notice while the latest fresh Linux sample is at or above 85%, and a housekeeping notice on each twentieth work period, counted over finished activations of any terminal state.
* **Owner cleanup:** Machine-wide disk views (`firm analyze`, **System → Health**) and `firm-sandbox --config PATH storage [ROLE]` guide a request to the role or `dispose ROLE`.

### Role-Authored Memory
Runtime telemetry is never dumped into agent workspaces. Instead, roles maintain three lightweight conventions in their workspace:
1. **`profile.md`** — Public identity and responsibilities (ingested host-side for `find_roles` search).
2. **`track-record.md`** — Append-only log of completed deliverables and where evidence lives.
3. **`playbooks/`** (with `playbooks/index.md`) — Reusable procedures and lessons learned.

### Firing and Retirement
Firing (`fire`) is direct-report only and refuses if the employee still has live descendants. It stops new admissions, cancels pending reminders, withdraws pending owner requests, and marks pending inbound mail undeliverable—while keeping all workspace files intact (downgraded to read-only `rx` for the reporting line).

After sandbox reconciliation while the firm is running, the dispatcher disposes of scratch and scoped Docker state for idle fired roles, then retires their guest and file-server identities; a failure is retried after fifteen minutes. Drive workspaces, tagged images, volumes and the rest of the home remain.

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
* **Per-message action targets:** Every message states, for each recipient, whether it asks them to act (`to_action`, `action`) or is FYI (`to_fyi`). Queued recipients persist `action_required`; only designated role deliveries get `scheduling_ready_at`. Operations notices retain explicit internal wake control.
* **No recurring wake from ignored mail:** Admission clears `scheduling_ready_at` on due deliveries when the work period starts. If a role leaves a message unread in its inbox without reading or dismissing it, that old message will **not** endlessly re-wake the role after completion.
* **Reminders:** Roles can schedule future wakes with `set_reminder(due_at, note, request_id)` (up to 100 pending reminders per role). When due, a reminder becomes an ordinary Operations message and wake.

When ordinary selection finds no admissible work and there are no admitted/running
activations, a free global slot may admit one eligible role for `idle_inbox_review`.
This conditional selection is outside `scheduling_opportunities` and reuses ordinary
employment, hold, retry, guidance, appointment and account gates. Ordinary work wins,
including actionable notices created by account selection. Future timers are unchanged.

`roles.last_offered_message_sequence` is a role-lifetime conservative frontier. Admission
and activation-authorized peek advance it only through the unread messages counted in
thread entries actually returned after bounded rendering, stopping at the first unseen gap.
SQLite `BEGIN IMMEDIATE` serializes allocation/insertion and frontier writes; deferred
lower-sequence deliveries cannot appear after a frontier commit. No owner/analysis read,
count, sender notice or derived summary advances it. An ordinary peek that cannot show
every thread may leave holes; idle snapshots guarantee a nonempty chronological prefix,
stopping before a future-scheduled delivery or the rendering budget.
Finite fixed mail therefore permits finitely many reviews without requiring disposition.
Previously shown mail above a hole may appear again. Admission stores the prior frontier
and restores it on pre-start failure;
`mark_started` establishes the committed scheduling offer, using existing recovery after
that point. No per-delivery review registry or receipt protocol is needed.

### Handoff and the Next Work Period

The handoff request records `clean_up` (default `false`) both on `activations` and in its request receipt. When a period whose handoff set `clean_up` completes, the dispatcher's idle-role loop disposes of scratch and scoped Docker state (containers, dangling images and ordinary build cache; tagged images and volumes stay) best-effort, one role per tick, shared with retirement and expired temporary appointments; if the role's next launch is admitted first, that launch performs the cleanup instead. A returned receipt, even partial, records `cleaned_up_at` on `activations` in SQLite so cleanup does not repeat across host restarts; an exception backs off fifteen minutes.

| How the role stops | Context on next activation | Wake behavior |
| :--- | :--- | :--- |
| **Normal completion** | Retained provider session | Waits for new mail or a due reminder. |
| **`handoff(notes, next="wait")`** | Fresh session + saved `notes` | Waits for new mail or a due reminder. |
| **`handoff(notes, next="continue")`** | Fresh session + saved `notes` | Immediately queues an ordinary wake opportunity (even with an empty inbox). |

---

## Provider Execution, Routing, and Context Policy

Named `[profiles.<name>]` configure provider (`codex`, `claude`, or experimental `agy`), model, effort, context limits, and `[accounts.<id>]`.

### Provider Boundaries

`provider_definitions.py` owns immutable configured defaults, supported efforts, soft context windows, and service constraints. It imports no runtime implementations. `provider_registry.py` lazily selects the built-in constructor and stateless parsers; metadata consumers use definitions directly.

The `ExecutionProvider` protocol separates role launches from tool-free summary/staffing turns. Every role adapter receives a `RoleLaunchRequest` with its invocation, sandbox identities, MCP URL, retained native session ID, and offered Firm tool catalog. Codex restricts Firm discovery with `enabled_tools`; Claude excludes the complement of the authoritative Firm catalog through `--disallowedTools`, preserving ordinary native tools and existing delegation restrictions. AGY 1.5.3 uses [`enabledTools`](https://antigravity.google/docs/mcp#mcp-configuration-properties) for exposure and eagerly registers each offered schema; empty catalogs are explicitly unsupported because native empty-allowlist behavior has not been verified. Each adapter rejects malformed or unknown tool names. Server authorization remains enforced independently of client discovery.

Native Firm MCP calls use the fixed 120-second transport timeout (`MCP_TIMEOUT_SECONDS` in `provider_support.py`). Codex writes `tool_timeout_sec`, AGY writes integer `timeoutSeconds`, and Claude writes per-server integer milliseconds in `timeout`, which also raises its HTTP idle timeout for a silent call. Claude role launches disable automatic MCP backgrounding so MCP tool results remain synchronous. Claude and Codex MCP startup settings are unchanged; AGY's native `timeoutSeconds` also controls connection and discovery.

Active roles parse results through their model-bound adapter instance, preserving configured/native model attribution on errors. Summary and guest LLM CLI runs use stateless parsers selected from the provider captured when the work launched, without constructing an adapter or reading current credentials. Builders still reject an account that has changed provider. Unknown telemetry remains unknown, historical logs do not create fresh subscription readings, and AGY subscription parsing returns no observation.

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
* **Handoff Gate (`context_window - handoff_reserve`, default reserve `10,000` tokens):** Blocks `read` so no new message bodies enter context and returns the handoff checklist, while leaving `peek`, `reply`, `send`, `dismiss`, `clear`, file saving, and `handoff()` open so the role can finish cleanly without being killed mid-thought.

### Key Design Trade-offs: Execution & Context
* **Why wait on a quota-blocked warm session instead of migrating it to another account?** Neither Codex nor Claude supports transferring a server-side session across accounts or providers. Automatic migration would silently wipe the role's conversation memory; waiting preserves it (and the owner can explicitly `stop-role` -> `route --fresh` if they prefer a reset).
* **Why does reconfiguration only update context policy on live sessions?** Changing `context_window` or `handoff_reserve` is safe mid-conversation, whereas changing model or provider is an identity change that waits for a fresh session.

---

## Guest LLM CLIs

Roles can invoke host-brokered non-interactive `claude` (`-p`), `codex` (`exec`), and `agy` (`-p`) CLIs directly from Linux shell scripts via shims installed on `PATH` (`<state_dir>/bin/{claude,codex,agy}` -> `llm_cli.py`) backed by `LlmService` (`llm_service.py`) over `/llm/start`, `/llm/heartbeat`, `/llm/finish`, and lease-gated `/credentials`:
* **No Firm authority or secret bleed:** Guest CLI shims scrub `FIRM_*`, `GH_TOKEN`, `GITHUB_TOKEN`, and raw API key env vars, inject `FIRM_LLM_CHILD=1` (preventing nested CLI recursion), pass no Firm MCP configuration, and stage short-lived credentials transiently under `<state_dir>/llm/<run_id>/<provider>` on `/run` tmpfs (deleted in `finally:`).
* **Cross-provider routing & slot borrowing:** Roles may invoke any configured CLI provider regardless of the role's own provider. At most one active CLI call on the caller's own bound account borrows the waiting caller's slot; additional concurrent calls (`max_concurrent = 2`) or calls to other accounts require a free account slot.
* **Session persistence & output:** Conversations persist across calls and work periods under `<private_home>/.firm/cli/<provider>`. Shims write only the final answer to `stdout` and emit session IDs (`firm: session <id>`) and waiting-mail/control cues on `stderr`.
* **Defaults:** Uses `[llm.codex]` (`gpt-6.2-sol`, `medium`), `[llm.claude]` (`opus`, `medium`), or `[llm.agy]` (`gemini-4-neon`, `medium`), as defined in `provider_definitions.py`.
* **Why host-brokered CLIs instead of provider-native subagents?** Provider-native subagents can inherit parent instructions and Firm MCP authority. Brokering CLI runs through the host runtime lets Firm enforce slot accounting, ephemeral credential leases, recursion prevention, model/MCP pinning, and clean stdout/stderr separation. Native tool behavior remains provider-specific.

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
When a role restarts after an abnormal exit that actually ran, it receives a minimal recovery notice: the UTC detection time, whether session context was retained, the **last 5 read message IDs** (with subject, or a body preview when there is none), and a reminder to check workspace files before repeating side effects.

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

### Generative Images
* Codex (`features.image_generation`) and AGY (`generate_image`) provide native subscription-backed image generation and editing for roles and guest CLI runs without external API keys; Claude-only firms have no raster image-generation provider, while Claude roles can call `codex exec` or `agy -p` when those providers are configured.

### The Mac Lease (`mac_acquire` / `mac_release` / `mac_status`)
* A firm with `[mac]` in `sandbox.toml` exposes `1 to 8` macOS desktop stations (`firmstation1..8`). Any Linux role can lease one free station at a time during an activation.
* The runtime brokers the `desktop_*` tool suite (`desktop_launch`, `desktop_inspect`, `desktop_action`, `desktop_capture`, pointer/key input, and `desktop_close_apps`) over an authenticated relay to the station's signed Swift helper, saving screenshots automatically to `org/<address>/work/captures/<timestamp>-<app>.png`. (See [Sandbox Design](sandbox.md#the-mac-lease) for the 4-check release sweep.)

---

## Owner Interface, Console, and Lifecycle

### CLI & Lifecycle Commands

| Command | Behavior |
| :--- | :--- |
| `firm init <dir>` | Validate mandate and `firm.toml`, and create the stopped firm with its `founder`. |
| `firm start [<dir>]` | Without `<dir>`: interactive setup wizard. With `<dir>`: launch or reconnect to the detached local host and open the Owner Console (`--foreground` attaches to terminal). |
| `firm stop <dir> [--force]` | **Soft drain** (default): close admission, ask active roles to save a `handoff`, then shut down VMs. **`--force`**: terminate active runs immediately and shut down VMs. |
| `firm status` / `firm analyze` | Read-only state summary or windowed JSON telemetry (`overview`, `inbox`, `readiness`, `timeline`, `usage`, `conversations`, `mac`, `resources`, `network`; omit `--section` for all sections). Works offline without booting VMs. |
| `firm export` / `firm discard-vms` | Stream the database, host logs, and Linux `/srv/firm` drive into `<dir>/export/` (verified by SHA-256 `manifest.json`). `discard-vms` exports with role homes and deletes the VM disk images while keeping the firm inspectable via `firm analyze`. |

### Soft Drain and Hard Stop
* **Soft drain (`firm stop <dir>`):** Closes new admission, asks running roles to finish their current step and save continuation notes via `handoff()`, releases any active Mac station leases cleanly, and shuts down VMs once all slots drain.
* **Hard stop (`firm stop <dir> --force`):** Closes admission, immediately terminates active guest runs, and shuts down VMs; interrupted work is re-queued for the next start without charging the role's retry budget.

### Analysis
* `firm analyze` computes read-only telemetry over a time window (`--from` / `--until`) from SQLite records and guest run journals (`overview`, `inbox`, `readiness`, `conversations`, `timeline`, `usage`, `mac`, `resources`, `network`, or omit `--section` for the full report).
* Telemetry reports objective execution facts (wake triggers, tool-call counts by name, token usage, message read delays, and cgroup peak memory) without using an LLM to judge whether a deliverable meets the mandate.

### The Local Owner Console (`http://127.0.0.1:8766/`)
* **Bookmarkable, owner-only auth:** Loopback is shared by local processes, so the console requires an `HttpOnly`, `SameSite=strict` session cookie initialized via `/unlock/<key>`. The key is generated once in `durable/console-key` (`0600`), keeping the console URL stable across restarts.
* **Sandboxed file previews:** Untrusted message text renders via `textContent`. HTML deliverables render inside an opaque-origin `<iframe sandbox>` with no network, forms, or parent access; Markdown and text render in a script-free frame (`frame-ancestors` locked to the console).
* **Development state:** Breaking changes are allowed without compatibility gates or migrations. Use fresh disposable state when schema or guest-contract changes require it.
