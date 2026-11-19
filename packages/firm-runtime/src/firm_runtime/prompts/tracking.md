# Prompt delivery tracking

Each activation receives standing instructions plus an activation prompt.

This file documents their assembly; it is not an injected prompt.
Implementation: [provider_support.py](../provider_support.py).

## 1. Founder — first activation

Standing instructions, in order:

- `workplace.json`: identity, manager, and complete owner mandate.
- `workplace.json`: workspace paths and access rules.
- `workplace.json`: configured LLM CLIs (when enabled) and service/Mac capabilities, or their explicit absence.
- `standing-physics.md`: common workplace physics (how the workplace operates).
- `standing-good-worker.md`: common good-worker guidance (how to do useful work
  with others).
- `guidance-introduction.md` and selected scope/function bodies, when any apply.
- `standing-limits.md`, last for every role.

Activation prompt, in order:

- `founder_birth.md`: first-day instructions plus organization-building and delegation guidance.
- Organization orientation, where available.
- Inbox snapshot, where available.

## 2. Founder — subsequent activations

Standing instructions, in order:

- `workplace.json`: current identity, manager, and complete owner mandate.
- `workplace.json`: workspace paths and access rules.
- `workplace.json`: configured LLM CLIs (when enabled) and service/Mac capabilities, or their explicit absence.
- `standing-physics.md`.
- `standing-good-worker.md`.
- `guidance-introduction.md` and selected scope/function bodies, when any apply.
- `standing-limits.md`, last for every role.

Activation prompt, in order:

- One opening from `wake.json`, selected by precedence:
  1. Recovery notice, if recovering from interruption.
  2. Fresh-context introduction with saved handoff notes.
  3. Fresh-context introduction without notes.
  4. Ordinary work update explaining the wake reason.
- Mandate-change notice when applicable to a resumed provider session.
- Guidance-change notice when a retained native session receives a different selection.
- Organization orientation, where available.
- Inbox snapshot, or “No unread mail.”

## 3. Non-founder — first activation

Standing instructions, in order:

- `workplace.json`: identity, manager, and complete delegated mandate.
- `workplace.json`: workspace paths, access rules, and instructions for looking
  up the founder's mandate.
- `workplace.json`: configured LLM CLIs (when enabled) and service/Mac capabilities, or their explicit absence.
- `standing-physics.md`.
- `standing-good-worker.md`.
- `guidance-introduction.md` and selected scope/function bodies, when any apply.
- `standing-limits.md`, last for every role.

Activation prompt, in order:

- `birth.md`.
- Organization orientation, where available.
- Remaining work-period allowance, if temporarily employed.
- Inbox snapshot, where available.

## 4. Non-founder — subsequent activations

Standing instructions, in order:

- `workplace.json`: current identity, manager, and complete delegated mandate.
- `workplace.json`: workspace paths, access rules, and founder-mandate lookup
  instructions.
- `workplace.json`: configured LLM CLIs (when enabled) and service/Mac capabilities, or their explicit absence.
- `standing-physics.md`.
- `standing-good-worker.md`.
- `guidance-introduction.md` and selected scope/function bodies, when any apply.
- `standing-limits.md`, last for every role.

Activation prompt, in order:

- One `wake.json` opening using the same recovery → handoff → fresh → ordinary
  precedence as the founder.
- Mandate-change notice when applicable to a resumed provider session.
- Guidance-change notice when a retained native session receives a different selection.
- Organization orientation, where available.
- Remaining work-period allowance, if temporarily employed.
- Inbox snapshot, or “No unread mail.”

## Shared behavior

- Standing instructions are assembled for every activation. Codex, Claude and
  AGY receive the same assembled text.
- Common resources appear once, after the role's identity, mandate,
  workplace and capability facts: physics first, then good-worker guidance, the optional
  guidance introduction, scope and
  function bodies in catalog order, and limits last for every role.
  The role's mandate takes precedence over good-worker and role guidance; neither can
  change limits, workplace rules or tool permissions.
- While `birth_delivered` is false, a role reporting to `owner` receives
  `founder_birth.md`; other roles receive `birth.md`.
  The runtime marks birth delivered when execution starts; a later fresh session does not repeat birth.
- Non-founders receive a lookup instruction for the founder's mandate, not its
  full text in their standing instructions.
- Tool descriptions are supplied separately in all four cases.
- Context warnings, drain instructions, and `context-handoff.md` are conditional
  controls, not unconditional startup prompts.
- Supplying standing instructions on every activation does not necessarily mean
  appending a new copy to a retained provider conversation; provider handling of
  standing instructions and session resumption is separate from prompt assembly.

## Runtime-only prompts

These are used by isolated runtime calls, not injected into employee activations:

- `runtime_summary.md`: correspondence summarization. Employees receive the
  derived summary, not these instructions.
- `runtime_level-choice.md`: one isolated staffing assessment for pending level and/or guidance decisions.
- `runtime_llm.md`: host-brokered guest LLM CLI instructions supplied by the CLI shim.

## Role guidance and limits

- `guidance/catalog.json` and `guidance/*.md`: validated catalog and the scope and function bodies.
  Org-chart scopes are recomputed at admission; the chooser sees descriptions only for the other scopes
  and functions. Staffing off makes no chooser call. Owner overrides specify the exact selection.
- `guidance-introduction.md`: rendered only with a nonempty selection, outside the catalog directory.
- `standing-limits.md`: rules for every role, last even when no guidance is selected.
- `wake.json:guidance_changed`: a short notice when retained native context receives a changed selection.

Prompt resource edits load on host restart only. `firm guidance` changes stored selection without resetting
sessions; `--automatic` restores org-chart and staffing rules.
