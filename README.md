# Firm

Firm ([`bmalthi/firm`](https://github.com/bmalthi/firm)) is an experimental multi-agent runtime and VM sandbox for **Apple Silicon macOS**. You give one agent—the **Founder**—a plain-English mandate, and it decides how to shape the rest of the organization: hiring a team, delegating sub-mandates, messaging colleagues over durable mail, building inside rootless Docker containers, and shipping deliverables back to a local web console. 

It runs with one or more Claude or Codex subscriptions.

Experimental Antigravity CLI (AGY) provider support aims to add another subscription-backed option. See [the tracking issue](https://github.com/bmalthi/firm/issues/1) and [AGY setup and limitations](docs/installation.md#experimental-agy-integration).

I had a lot of fun building this. I wanted to explore what happens when a multi-agent system tries to do real software work without risking my local machine—so I built heavy OS, network, and credential safeguards around a dedicated Linux VM (and an optional macOS VM for Xcode and iOS Simulator work) where you can safely let a dozen or more agents loose and go burn some tokens. See **[what we gave / what we learned](examples/README.md)** for recorded runs, deliverables, and transcripts.

---

## Notes from Building and Running Firm

### Keeping up the mirage: how the runtime hides itself

I put a lot of thought into the boundaries between the runtime and the agents, and I'm particularly proud of how the **[prompting](design/prompting.md)** and **[messaging](design/messaging.md)** systems came together to sustain the illusion of a real workplace.

As much as possible, Firm tries to pretend the "agent harness" isn't there. If you read through **[the prompt templates](packages/firm-runtime/src/firm_runtime/prompts/)**, you won't find words like *"agent"*, *"LLM"*, *"prompt"*, *"token"*, *"context window"*, or *"subagent"*. From the moment a role is created, the runtime treats it as a person starting a job, provides the company plumbing behind the scenes, and otherwise gets out of the way:

* **"Welcome to the firm" (`founder_birth.md` & `birth.md`):** A newly created role's first-day note is literally ten lines ([`birth.md`](packages/firm-runtime/src/firm_runtime/prompts/birth.md)): *"Welcome to the firm… Choose your public name, title, identity and responsibilities in your workspace's `profile.md`. Then begin pursuing your mandate."* The runtime never prescribes personas, org structures, or workflows—employees name themselves and figure out how to divide the work.
* **Breaking the "human at the terminal" reflex:** Coding CLIs like Claude Code and Codex are trained to stop and ask a human operator at a terminal for confirmation. [`standing.md`](packages/firm-runtime/src/firm_runtime/prompts/standing.md) explicitly dismantles that assumption: *"Files and printed conclusions do not notify anyone; message the requested result to the requester… There is no terminal operator waiting to answer."* If an employee is blocked, they email their manager or colleague, switch to other work, or end their work period and go to sleep until a reply wakes them.
* **Workplace concepts instead of framework mechanics:**
  * Context limits aren't exposed as token counts; they appear as **working memory** ([`controls.json`](packages/firm-runtime/src/firm_runtime/prompts/controls.json): *"Working memory is filling. Consider saving progress and handing off before taking more work."*).
  * Turns and sessions are **work periods**. Ending a work period normally keeps working memory intact; calling `handoff` starts the next work period with fresh working memory and your saved continuation notes.
  * Spawning an agent is **hiring** (`hire`)—either as a permanent employee or a temporary appointment for *N* work periods that a manager can later `renew` or `promote`. Stopping an agent is **retiring**, and any mail sent to them afterward bounces back with *"recipient `e3` retired."*
  * Managers never pick model IDs (`opus`, `sonnet`, `gpt-6-luna`) when hiring—they just write a job mandate, and an invisible background step ([`runtime_level-choice.md`](packages/firm-runtime/src/firm_runtime/prompts/runtime_level-choice.md)) privately sizes the hire's model tier (`low`, `medium`, `high`).
* **Infrastructure speaks as "Operations":** Whenever the outside world or the host runtime *has* to intervene—a subscription quota window pauses execution, the owner approves or denies a firewall domain (`request_access`), a direct report crashes repeatedly and is held for manager review, or the owner pauses the firm—it never surfaces as a raw harness exception. It arrives as an attributed notice or email from **Operations** ([`notices.json`](packages/firm-runtime/src/firm_runtime/prompts/notices.json)), like an internal IT/ops team, while remaining 100% factual about what the Linux account, SMB drive, or Mac station will actually do.
* **The runtime handles the lifecycle invisibly between work periods:** Employees never poll in sleep loops waiting for replies. When a role finishes a work period, the runtime freezes its warm provider session, terminates stray background processes in its Linux cgroup slice, wipes its ephemeral `~/scratch` build directory, reclaims dangling Docker state, and only wakes the role when new mail arrives, an owner decision comes back, or a self-scheduled `reminder` comes due.

#### Prompting architecture ([`prompts/`](packages/firm-runtime/src/firm_runtime/prompts/) & [`design/prompting.md`](design/prompting.md))
Small, subtle phrasing changes in **[the prompt templates](packages/firm-runtime/src/firm_runtime/prompts/)** make a surprisingly large difference in how eagerly agents hire teams, how much they delegate versus doing the work themselves, and whether they spin up bureaucracy or actually ship. A few structural rules keep the prompts clean:
* **Python enforces, prose only orients:** Permissions, reporting lines, network boundaries, and budgets are enforced in Python and the OS kernel—never by pleading with the model to *"please follow the rules."* That keeps [`standing.md`](packages/firm-runtime/src/firm_runtime/prompts/standing.md) calm, brief, and factual.
* **Three-layer assembly with a byte-stable system prompt:**
  1. **Standing instructions (`standing.md` + `workplace.json`):** Passed as the system prompt. It includes the role's mandate, workspace paths, and *only* the optional capabilities (`[github]`, `[google_cloud]`, `[mac]`) actually enabled for that firm. It deliberately excludes timestamps, inbox counts, and token counters so it stays **byte-identical across turns** and keeps provider prompt caches warm.
  2. **Transition context (`stdin`):** First-day orientation ([`founder_birth.md`](packages/firm-runtime/src/firm_runtime/prompts/founder_birth.md) or [`birth.md`](packages/firm-runtime/src/firm_runtime/prompts/birth.md), injected once at birth rather than cluttering the inbox), post-`handoff` continuation notes, or crash-recovery context.
  3. **Wake delta (`stdin`):** Only what changed while the role was asleep—mandate revisions, changes to its immediate org neighborhood, context-pressure notices, and its current inbox `peek`.
* **Pointers instead of prompt bloat:** Rather than stuffing the Founder's entire mandate and the company org chart into every employee's prompt, hires get their own delegated mandate plus a pointer (`get_role(role="founder", section="mandate")`) and maintain three lightweight files in their workspace: `profile.md` (for colleague discovery via `find_roles`), an append-only `track-record.md` (completed deliveries), and `playbooks/index.md` (reusable lessons across fresh sessions).

#### Messaging ([`design/messaging.md`](design/messaging.md))
Multi-agent mail falls apart quickly if agents have to juggle UUID thread IDs or if new messages interrupt a running turn. Firm's mail system is built around how LLMs actually behave in long tool chains:
* **Conversations are participant sets, not threads:** When agents manage traditional thread IDs, they constantly reply on stale threads or fork duplicate threads with the same colleague. In Firm, a conversation is uniquely keyed by the **exact set of people in it**: every message between `{founder, e1}` lands in `c1`. If `e1` replies and loops in `e2` (`reply(m1, ..., add=["e2"])`), the discussion automatically moves to `{founder, e1, e2}` (`c2`, with `continues = c1`). The original 1:1 stays untouched, and `e2` cannot read `c1` unless they call `context()` or the sender passes `summarize=true` on the introduction.
* **Short base-36 IDs everywhere (`c1`, `m4a`, `e2`, `q1`):** Models never have to copy or type UUIDs. Roles, conversations, messages, and access requests all use compact, monotonic base-36 tokens.
* **Watermarked `clear()` so mid-turn mail is never lost:** `peek` lists waiting conversation headers (ordered by Operations, Owner, then oldest waiting mail) plus a compact health footer showing the role's context pressure and direct reports' status. If a role peeks at 10:00 (`m1..m5`), works for ten minutes while `m6` arrives at 10:05, and calls `clear()` at 10:10, `clear()` only dismisses up to the sequence watermark of the last complete `peek` (`m1..m5`) and leaves `m6` untouched.
* **Non-interrupting `New mail` cues:** Mail arriving while a role is already working never interrupts its turn. Instead, the next Firm tool result appends a one-line `New mail` hint (senders and counts) so the agent knows to `peek` before wrapping up or sending a stale finding.
* **Isolated correspondence summaries (`context`):** Catching up on a long conversation chain (`continues`) runs an isolated, tool-free LLM turn ([`runtime_summary.md`](packages/firm-runtime/src/firm_runtime/prompts/runtime_summary.md)) outside the employee's session—sparing the employee's context window and never blocking message delivery if the summarizer times out.

### Inspiration: from Gas Town to self-shaping orgs

This project was partly inspired by Steve Yegge's writing on anthropological multi-agent systems—both *Gas Town* and his later move toward *Wyvern* / *Wheelhouse*, where the agent harness and the game itself start to fuse into the same thing. I wanted to try something in that spirit, but push the organizational flexibility even further: instead of hardcoding fixed workflow stages, you hand a single Founder a mandate and let it invent the org chart from scratch.

In practice, total freedom might not be prescriptive enough. You may actually need—as Yegge built into Gas Town—specific structural roles whose job is to audit, verify, and push work forward, rather than trusting a Founder to invent and enforce good process on its own. Or maybe it just needs better-tuned prompts, sharper mandates, and more tokens.

### Is it actually useful? (The 5B token question)

Maybe..Its well built and it is genuinely fun to watch agents hire each other, debate architecture, spin up Docker containers, build websites, and hand off work safely inside a VM.

Whether it beats driving a frontier model directly is a harder question:
* **On small or short tasks**, Firm is noticeably less efficient than just running Claude Code or Codex directly on the problem. But it gets it done (see the 1-role [Flashcards CLI run](examples/flashcards-cli/README.md)).
* **On larger builds**, it is tricky to do an apples-to-apples comparison. When I build something substantial with an LLM directly, I am in the loop the whole time—writing plans, checking intermediate steps, and course-correcting turn by turn. With Firm, I have mostly thrown ambitious, fairly loose mandates over the wall and nudged the Founder with messages when it drifted (compare [Little City](examples/miniature-city/README.md) and [Kestrel](examples/space-station/README.md)).
* **The main failure mode I've seen so far is testing theatre / hash check versioning hell on work produced.** Left to their own devices, agents love creating elaborate checklists, QA rubrics, and versioned handoff rituals that look productive without moving the actual product forward. (Watching them get tangled in bureaucracy—and then telling them off from the owner inbox—is half the fun.)

At one point I ran a firm for a couple of days across **two Claude subscriptions and one Codex subscription**, with around **15 agents running concurrently** (see the archived [iOS game studio run](examples/ios-game-studio/README.md) for a week-long 8-role studio with the same pattern). After burning roughly **5 billion tokens**, real product output had ground to a crawl. So I messaged the Founder and told it to hire an internal consultant to investigate what had gone wrong.

Watching those messages flow through the system was fascinating. The Founder gave the consultant a formal remit and wrote to every member of staff saying, *"please don't hide anything—give the consultant everything they ask for."* The consultant interviewed the team, inspected the workspaces, and wrote up a detailed report of findings—which the Founder then half-ignored. Thinking about real companies, people spend an awful lot of time chatting and writing memos rather than doing real work too. I genuinely don't know yet whether that is the natural equilibrium of multi-agent LLMs, a structural limit of unprescribed org trees, or something that disappears if I can write better prompts and heavily specified mandates. Testing this properly on real-world projects takes ambitious, well-structured mandates and a lot of tokens.

### What's under the hood (and the Mac sandbox trade-off)

Because the whole point was to let swarms of agents run real tools without babysitting every command, the sandbox is built to be genuinely protective and flexible:
* **Real isolation per employee:** Every hired role is a separate Linux OS user inside a dedicated Lima VM, with its own systemd cgroup slice, its own **rootless Docker daemon**, and reporting-line ACLs enforced by an internal SMB server (`/srv/firm`).
* **Multi-provider, multi-account pooling:** You can pool multiple **Codex** and **Claude Code** subscriptions in a single firm. Long-lived OAuth refresh tokens and GitHub App private keys never enter the VM; the host rotates them and stages short-lived tokens over an in-memory `tmpfs` mount.
* **Blind model tiering (`[staffing]`):** When a manager hires a report, the manager only writes a mandate; an isolated chooser ([`runtime_level-choice.md`](packages/firm-runtime/src/firm_runtime/prompts/runtime_level-choice.md)) assigns a `low`, `medium`, or `high` model tier from the mandate alone so managers don't just pick the most expensive model for every sub-task.
* **Mac & iOS Simulator stations (working, with a known wart):** Getting Mac and iOS GUI testing working inside a sandbox is a pain in the ass. Today it works by running a second shared macOS VM alongside the Linux VM: a Linux role calls `mac_acquire` to lease a clean macOS desktop user (`firmstation1..8`), syncs code over SSH, runs `xcodebuild` and iOS Simulator / Device Hub, and drives the GUI through a signed Swift app ([`station-helper`](station-helper/README.md)). Honestly, having an agent in the Linux VM remotely puppet the Mac through a helper app is a bit ugly. What I really want to explore next is injecting the running agent directly into the Mac VM—pausing the agent's session in the Linux VM and resuming that exact session context inside macOS (or a cleaner equivalent).

If I made the agents mac first in a macos vm then giving them docker is hard since nested virtualization on mac doesn't work well. I had one version where each macos first agent had a ssh tunnel to a shadow linux user_id to provide docker but...thats silly

### Recorded runs:

See **[`examples/`](examples/README.md)** for five recorded runs—a [miniature city game](examples/miniature-city/README.md), a [space-station dashboard](examples/space-station/README.md), a [multi-day iOS game studio](examples/ios-game-studio/README.md), a [solo CLI tool](examples/flashcards-cli/README.md), and a [native iOS arcade game](examples/ios-tap-to-fly/README.md). The apps themselves are simple builds; the interesting part is inspecting their exact mandates, recorded team and owner messages, role workspaces (`profile.md`, `track-record.md`, `playbooks/`), and how the roles organized, debated QA, and responded to feedback.

---

## Architecture at a glance

```text
+-----------------------------------------------------------------------------------+
| macOS Host (Apple Silicon)                                                        |
|                                                                                   |
|  +---------------------------+   +---------------------------------------------+  |
|  | Local Owner Console       |   | Firm Runtime (SQLite + Dispatcher + MCP)    |  |
|  | http://127.0.0.1:8766     |<->| - Roles, Mandates, Org Tree & Hiring Levels |  |
|  | - Overview & Decisions    |   | - Durable Mail, Summaries & Wake Scheduler  |  |
|  | - Inbox & File Reader     |   | - Host-Side OAuth Renewal & GitHub App      |  |
|  | - Team, Health & History  |   | - Userspace Packet Filter & Allowlist Proxy |  |
|  +---------------------------+   +----------------------+----------------------+  |
|                                                         |                         |
|                                   Loopback SSH Tunnels & Egress Proxy             |
|                                    (No host home mounts, no raw DNS/UDP)          |
|                                                         |                         |
|         +-----------------------------------------------+---------------+         |
|         |                                                               |         |
|         v                                                               v         |
|  +-------------------------------------------+   +-----------------------------+  |
|  | Persistent Linux VM (Lima)                |   | Shared Mac VM (Optional)    |  |
|  |                                           |   |                             |  |
|  |  +-------------------------------------+  |   |  +-----------------------+  |  |
|  |  | Role: founder (isolated slice)      |  |   |  | Station 1..N Leases   |  |  |
|  |  | - Private ~, ~/scratch, Dockerd     |  |   |  | - Off-console desktop |  |  |
|  |  +-------------------------------------+  |   |  | - Signed Swift Helper |  |  |
|  |  | Role: e1, e2... (isolated slice)    |  |   |  | - Xcode & Device Hub  |  |  |
|  |  | - Private ~, ~/scratch, Dockerd     |  |   |  | - 4-Check Clean Sweep |  |  |
|  |  +-------------------------------------+  |   |  +-----------------------+  |  |
|  |  | Internal SMB File Server (/srv/firm)|  |   +-----------------------------+  |
|  |  | - Reporting-line ACLs on org/       |  |                                    |
|  |  | - Firm-wide collaboration on shared/|  |                                    |
|  |  +-------------------------------------+  |                                    |
|  +-------------------------------------------+                                    |
+-----------------------------------------------------------------------------------+
```

---

## Setup & Quick Start

**Requirements:** An **Apple Silicon Mac (macOS 13+)**, **16 GiB+ RAM**, **30 GiB+ free disk space**, and a **Claude** or **Codex** subscription. See **[`docs/installation.md`](docs/installation.md)** for full setup details, hardware sizing, and optional [GitHub](docs/installation.md#optional-github), [Gemini image generation](docs/installation.md#optional-generative-images), and [Mac station](docs/installation.md#advanced-a-mac-with-stations) configuration.

### Path (A): For Agents (Recommended — Let Your Coding Agent Set It Up)

The easiest way to run Firm is to let your local coding agent (Claude Code, Codex, Cursor, etc. on a medium-tier model) install it and act as your **Chief of Staff**—polling `firm status` and your inbox every 5 minutes, summarizing progress, flagging access requests, and messaging the Founder when you want to steer direction.

Paste this prompt into your coding agent:

```text
Install and set up Firm from https://github.com/bmalthi/firm on my Mac following "Path (A): For Coding Agents" in docs/installation.md. Ask me what mandate I want the founder to tackle (or use examples/miniature-city/mandate.md for a demo) and whether to use my Codex or Claude subscription. Once running, give me the Owner Console unlock_url, poll status and inbox every 5 minutes, summarize progress, and help me message the founder when needed.
```

What the standard setup gives you by default:
* **Dedicated Linux VM:** 4 vCPUs, 8 GiB RAM, and a thin-provisioned 60 GiB disk (configurable in `sandbox.toml`), with per-employee OS accounts, cgroup slices, rootless Docker, and an internal `/srv/firm` SMB share.
* **Automatic model tiering (`[staffing]`):** Up to 4 concurrent active roles (`max_active = 4`), isolated browser OAuth sign-in (`~/.config/firm-runtime/credentials/`), and blind `low` / `medium` / `high` model assignment per hire.
* **Safe outbound firewall (`egress = "enforce"`):** Provider web search (`web_search` / `WebSearch`), GitHub, Docker Hub, PyPI, npm, and OS package mirrors work out of the box; unlisted domains trigger one-click owner approval requests (or set `egress = "observe"` to allow all public web).
* **Local Owner Console (`http://127.0.0.1:8766/`):** Live org tree, inbox, deliverable file reader, network approvals, and VM health controls.

<details>
<summary><strong>Path (B): Manual Terminal Setup (`firm start` wizard)</strong> — expand for commands</summary>

Prefer to install and run the interactive setup wizard yourself in Terminal?

```sh
# 1. Install prerequisites (uv, Lima 2.2.0+, Node.js)
brew install uv lima node

# 2. Clone bmalthi/firm, build the wheels, and install the CLI
git clone https://github.com/bmalthi/firm.git
cd firm
uv build --all-packages
uv tool install --force --python 3.12 \
  --with "$PWD/dist/firm_sandbox-0.1.0-py3-none-any.whl" \
  "$PWD/dist/firm_runtime-0.1.0-py3-none-any.whl"

# 3. Launch the interactive setup wizard (picks provider, starter mandate, and boots VM)
firm start
```

*(If `firm` is not on your `PATH`, run `uv tool update-shell` and reopen Terminal. For non-interactive CLI commands (`firm check`, `firm init`, `firm start <dir>`, `firm analyze`), firewall modes, and troubleshooting, see **[`docs/installation.md`](docs/installation.md)** and **[`docs/runtime.md`](docs/runtime.md)**.)*

</details>

<details>
<summary><strong>Tips for Writing a Good Mandate (`mandate.md`)</strong> — expand</summary>

A mandate is a plain Markdown file handed directly to the Founder (see [`examples/miniature-city/mandate.md`](examples/miniature-city/mandate.md)). A clear mandate covers four things:
1. **Goal & deliverable path:** What to build and where to put finished files (e.g. in `shared/` or `work/`, attached to the completion message to `owner`).
2. **Scope & constraints:** Stack preferences, offline vs. external API usage, and whether to keep the team small or hire dedicated specialists (e.g. implementation vs. independent QA).
3. **Verification:** How the team must verify the work before claiming completion (running tests, exercising the UI/CLI locally, and reporting what was actually tested).
4. **When to check in:** Whether to message `owner` with a plan first or ship a working prototype before asking for feedback.

</details>

---

## Documentation & Design Reference

### Operator Guides (`docs/`)
* **[Installing Firm](docs/installation.md)** — Hardware requirements, agent & human setup paths, firewall config, troubleshooting, and optional [GitHub](docs/installation.md#optional-github), [Gemini image generation](docs/installation.md#optional-generative-images), and [Mac station](docs/installation.md#advanced-a-mac-with-stations) setup.
* **[Runtime Operations](docs/runtime.md)** — `firm.toml` & `sandbox.toml` reference, staffing classes, credential renewal, quota holds, owner requests, console guide, and `firm analyze`.
* **[Linux VM Guide](docs/linux-vm.md)** — Per-role slices, rootless Docker, the SMB file server, scratch & cache settlement, and the network filter/proxy.
* **[Mac Station Guide](docs/mac-station.md)** & **[Mac VM Images](docs/mac-vm-images.md)** — Building, enrolling, signing, and leasing multi-station macOS VMs for Xcode and desktop automation.
* **[Development Checks](docs/development.md)** & **[Release Procedures](docs/releases.md)** — Running the test suites, [live acceptance tools](tools/acceptance/README.md), and release verification.

### Architecture & Rationale (`design/`)
* **[Design Overview](design/README.md)** — High-level index of the architecture and design rationale.
* **[Runtime Design](design/runtime.md)** — Durable roles, automatic model tiering, mandate revisions, scheduling, OAuth renewal, and recovery.
* **[Sandbox Design](design/sandbox.md)** — Linux slices, kernel cgroup settlement, SMB ACLs, the packet filter & proxy, and Mac station leases.
* **[Messaging Design](design/messaging.md)** — Participant-set conversations, atomic claims, watermarks, and isolated correspondence summaries.
* **[Prompting Design](design/prompting.md)** — How Firm sustains the workplace metaphor and keeps enforcement in Python rather than prose.

---

## Repository Layout

* [`packages/firm-runtime`](packages/firm-runtime/README.md) — Durable roles, scheduling, messaging, provider adapters, CLI, and web console.
* [`packages/firm-sandbox`](packages/firm-sandbox/README.md) — Linux VM lifecycle, guest controller, SMB file server, network proxy, and optional Mac station leases.
* [`station-helper`](station-helper/README.md) — Signed Swift macOS application inside each Mac station that executes bounded desktop inspection, input, and window capture.
* [`examples`](examples/README.md) — What we gave / what we learned: five recorded runs ([Little City](examples/miniature-city/README.md), [Kestrel](examples/space-station/README.md), [Painting Game](examples/ios-game-studio/README.md), [Flashcards](examples/flashcards-cli/README.md), and [Lumen Drift](examples/ios-tap-to-fly/README.md)), including runnable output, full messages, and QA work.
* [`tools`](tools/README.md) — Local test runner (`check.py`), release verifiers, line-of-code analyzer (`loc.py`), and [live VM acceptance suites](tools/acceptance/README.md).
* [`design`](design/) — Architecture, security boundaries, and design rationale.
* [`docs`](docs/) — Operator, setup, development, and release guides.
* [`images`](images/) — Tracked Mac VM image recipes; built VM images live in the Git-ignored `images/.images/` directory.
* `firms/` — Local firm runs, including configuration and durable state; ignored by Git.


## License

Firm lives at [`https://github.com/bmalthi/firm`](https://github.com/bmalthi/firm) and is licensed under [Apache-2.0](LICENSE) (see also [NOTICE](NOTICE) and [provenance](docs/provenance.md)).
