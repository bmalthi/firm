# Prompting Design

> **TL;DR — How Prompting Works**
> * **Python Enforces, Prose Orients:** Permissions, reporting lines, and budgets are enforced in Python and the OS kernel—never by asking the LLM to "please follow the rules."
> * **Zero-LLM Prompt Assembly:** Wake prompts are rendered deterministically from templates (`prompts/`) and SQLite state using single-pass `${name}` substitution.
> * **Workplace Vocabulary + Accurate Environment:** Prompts use workplace terms for org concepts (*role, mandate, colleague, work period*) while describing Linux accounts, Docker containers, and Mac stations accurately.
> * **Byte-Stable System Prompt:** Standing instructions stay byte-identical across turns while the role's identity, mandate, paths, capabilities and effective guidance are unchanged, so provider prompt caches stay warm; dynamic state (mail, mandate deltas, context pressure) goes in `stdin`.
> * **Physics, Guidance and Limits:** After the role's facts come workplace physics, common good-worker guidance, selected role guidance, and standing limits.

---

## Core Posture & Role Conventions

Three principles govern every prompt in Firm:
1. **Keep instructions light:** Explain the environment and tool contracts, then give short working expectations focused on observable conduct (settle ambiguity that changes success, follow through, separate evidence from assumption, deliver usable results). Scope and function guides recommend working methods, and founder orientation encourages delegation. Roles choose how to apply that guidance to their mandate; there is no fixed org chart or reporting cadence.
2. **Sustain the workplace metaphor:** Roles are employees in a company with a manager, direct reports, a mandate, and a shared drive—while keeping every statement about tool behavior and boundaries 100% factual.
3. **Work with model strengths:** Never ask models to memorize or type long hashes/UUIDs, and never copy the Founder's entire mandate into every employee's system prompt (employees receive a `get_role(role="founder", section="mandate")` pointer instead).

Every role is asked to maintain three lightweight workspace files (with no rigid template):
* **`profile.md`** — Public name and current responsibilities so colleagues can find them via `find_roles`.
* **`track-record.md`** — Append-only record of completed work and deliverable paths.
* **`playbooks/index.md`** — Reusable procedures and lessons learned in this firm.

---

## Prompt Layers & Assembly by Occasion

Every provider receives the same assembled standing text. Codex receives it via `model_instructions_file`; Claude Code via `--system-prompt-file`; AGY as the body of its `firm-role` agent definition. Each receives the wake/transition update separately as input.

1. **Layer 1 — Standing Instructions (Byte-Stable System Prompt):**
   * Role identity, manager, verbatim own mandate, accessible workspace/drive paths (`workplace.json`), and only the optional capabilities (`[github]`, `[google_cloud]`, `[mac]`) actually enabled for this firm.
   * First, two common resources in fixed order: `standing-physics.md` (mail, working memory and handoff, tools, Linux environment summary with `~/scratch` vs durable `<drive>`, outside services and access) and `standing-good-worker.md` (outcomes, follow-through, investigation and escalation, communication, evidence, usable results, shared time and resources).
   * Then `guidance-introduction.md` and selected bodies from `guidance/`, scope first and functions in catalog order; omit the introduction when no guides apply. `standing-limits.md` comes last for every role. Physics describes the environment; good-worker and role guides describe craft and yield to the mandate. Limits hold firm-wide rules that no mandate, profile, mail, guide or tool result changes.
   * Contains no timestamps, inbox counts, or token counters so provider prompt caches stay warm. The common resources are fixed for a running host; the assembled text changes only when the role's identity, mandate, paths, capabilities or effective guidance change. Prompt resource edits load only after restarting the host.
2. **Layer 2 — Transition Context (`stdin`):**
   * First-day birth orientation (`founder_birth.md` or `birth.md`—injected once on the birth activation, never via claimable mail), post-handoff continuation notes, or a crash recovery notice.
3. **Layer 3 — Wake Update (`stdin`):**
   * Any newly revised mandate, a short guidance-change notice on a retained native session, changed org neighborhood delta, remaining temporary work periods, and the current inbox `peek` overview (plus any context-pressure notice).

| Occasion | System Prompt (Standing) | Wake & Transition Input (`stdin`) |
| :--- | :--- | :--- |
| **Founder birth** | Owner mandate, workplace rules, enabled capabilities | `founder_birth.md` (org-building guidance), initial orientation, and inbox |
| **Employee birth** | Delegated mandate, pointer to Founder mandate, workplace rules | `birth.md` (first-day setup), manager/neighborhood facts, and inbox |
| **Normal resume** | Retained session context + current standing instructions | New mandate revision (if any), org delta (if changed), and current inbox |
| **Wake after `handoff`** | Fresh session + rebuilt standing instructions | Saved handoff continuation notes + current inbox |
| **Fresh start after crash** | Fresh session + rebuilt standing instructions | Minimal recovery notice + last 5 claimed message IDs/subjects + current inbox |

---

## Resources

Prompt resource files live in `packages/firm-runtime/src/firm_runtime/prompts/`:

| Resource File | Purpose |
| :--- | :--- |
| `workplace.json` | Role identity, mandate, workspace paths, and conditionally rendered capability blocks (GitHub, Gemini, Mac). |
| `standing-physics.md` | Common workplace physics: mail, working memory and handoff, tools, Linux environment, outside services and access. |
| `standing-good-worker.md` | Common good-worker guidance: how to do useful work with others. |
| `guidance/catalog.json`, `guidance/*.md`, `guidance-introduction.md` | Validated scope and function guides, composed only when selected. |
| `standing-limits.md` | Firm-wide authority, spending, credential, secret and publication limits; always last. |
| `founder_birth.md`, `birth.md`, `wake.json` | First-day onboarding for the Founder vs. hired employees, plus wake/resume wrappers. |
| `context-handoff.md`, `controls.json` | Handoff checklist and Operations control banners (soft drain, context pressure). |
| `mac-acquired.md` | Detailed macOS/Xcode/Device Hub workflow notes delivered when `mac_acquire` succeeds. |
| `tools.json`, `results.json`, `references.json` | MCP tool schemas, return-state explanations, and actionable error messages. |
| `notices.json` | Templates for attributed Operations messages (bounces, request grants/denials, mandate updates). |
| `runtime_summary.md`, `runtime_level-choice.md` | Isolated infrastructure prompts for correspondence summarization (`context`) and level and guidance selection (`[staffing]`). Never shown to employees. |
| `tracking.md` | Maintainer reference mapping how standing and activation prompt layers are ordered across the four activation occasions (not injected at runtime). |

The founder automatically receives `firm-leader`; every other role with non-retired direct reports receives
`team-leader`, including when reports are paused or temporary appointments are exhausted. With staffing on,
one isolated chooser call selects functions and, where appropriate, `specialist-adviser` or `cross-team-lead`.
The org-chart scope takes precedence over a chooser scope. Selection runs at initialization, hire and mandate
revision; ordinary wakes reuse it. Level and guidance validation and retries are independent. A role's first
period waits for pending guidance when a selector is available, bounded by three attempts. With `staffing = false`,
no chooser runs and only the org-chart scope applies unless the owner sets an override.

`firm guidance DIR EMPLOYEE_ID --set [--scope ID] [--function ID]... --request-id R` sets exactly that selection,
including an empty selection or an org-chart scope. `--automatic` clears the stored selection and requests a
new assessment when staffing is on; only the org-chart scope applies until it succeeds. Guidance changes preserve
sessions and model routing. The console and `firm analyze` readiness show guidance and its scope source.
Function guides ship as provisional hypotheses; evidence of useful behavior determines which stay.
