# Prompting Design

> **TL;DR — How Prompting Works**
> * **Python Enforces, Prose Orients:** Permissions, reporting lines, and budgets are enforced in Python and the OS kernel—never by asking the LLM to "please follow the rules."
> * **Zero-LLM Prompt Assembly:** Wake prompts are rendered deterministically from templates (`prompts/`) and SQLite state using single-pass `${name}` substitution.
> * **Workplace Vocabulary + Accurate Environment:** Prompts use workplace terms for org concepts (*role, mandate, colleague, work period*) while describing Linux accounts, Docker containers, and Mac stations accurately.
> * **Byte-Stable System Prompt:** `standing.md` stays byte-identical across turns so provider prompt caches stay warm; dynamic state (mail, mandate deltas, context pressure) goes in `stdin`.

---

## Core Posture & Role Conventions

Three principles govern every prompt in Firm:
1. **Keep instructions light:** Explain the environment and tool contracts without prescribing how an employee should think, plan, or structure its code.
2. **Sustain the workplace metaphor:** Roles are employees in a company with a manager, direct reports, a mandate, and a shared drive—while keeping every statement about tool behavior and boundaries 100% factual.
3. **Work with model strengths:** Never ask models to memorize or type long hashes/UUIDs, and never copy the Founder's entire mandate into every employee's system prompt (employees receive a `get_role(role="founder", section="mandate")` pointer instead).

Every role is asked to maintain three lightweight workspace files (with no rigid template):
* **`profile.md`** — Public name and current responsibilities so colleagues can find them via `find_roles`.
* **`track-record.md`** — Append-only record of completed work and deliverable paths.
* **`playbooks/index.md`** — Reusable procedures and lessons learned in this firm.

---

## Prompt Layers & Assembly by Occasion

Codex receives standing instructions via `model_instructions_file`; Claude Code receives them via `--system-prompt-file`. Both receive the wake/transition update on `stdin`.

1. **Layer 1 — Standing Instructions (Byte-Stable System Prompt):**
   * Role identity, manager, verbatim own mandate, accessible workspace/drive paths (`workplace.json`), Linux environment summary (`~/scratch` vs durable `<drive>`), and only the optional capabilities (`[github]`, `[google_cloud]`, `[mac]`) actually enabled for this firm.
   * Contains no timestamps, inbox counts, or token counters so provider prompt caches stay warm.
2. **Layer 2 — Transition Context (`stdin`):**
   * First-day birth orientation (`founder_birth.md` or `birth.md`—injected once on the birth activation, never via claimable mail), post-handoff continuation notes, or a crash recovery notice.
3. **Layer 3 — Wake Update (`stdin`):**
   * Any newly revised mandate, changed org neighborhood delta, remaining temporary work periods, and the current inbox `peek` overview (plus any context-pressure notice).

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
| `standing.md`, `workplace.json` | Common role system prompt and conditionally rendered capability blocks (Linux, GitHub, Gemini, Mac). |
| `founder_birth.md`, `birth.md`, `wake.json` | First-day onboarding for the Founder vs. hired employees, plus wake/resume wrappers. |
| `context-handoff.md`, `controls.json` | Handoff checklist and Operations control banners (soft drain, context pressure). |
| `mac-acquired.md` | Detailed macOS/Xcode/Device Hub workflow notes delivered when `mac_acquire` succeeds. |
| `tools.json`, `results.json`, `references.json` | MCP tool schemas, return-state explanations, and actionable error messages. |
| `notices.json` | Templates for attributed Operations messages (bounces, request grants/denials, mandate updates). |
| `runtime_summary.md`, `runtime_level-choice.md` | Isolated infrastructure prompts for correspondence summarization (`context`) and hire sizing (`[staffing]`). Never shown to employees. |
| `tracking.md` | Maintainer reference mapping how standing and activation prompt layers are ordered across the four activation occasions (not injected at runtime). |
