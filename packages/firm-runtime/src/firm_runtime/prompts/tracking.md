# Prompt delivery tracking

Each activation receives standing instructions plus an activation prompt.

This file documents their assembly; it is not an injected prompt.
Implementation: [provider_support.py](../provider_support.py).

## 1. Founder — first activation

Standing instructions, in order:

- `workplace.json`: identity, manager, and complete owner mandate.
- `workplace.json`: workspace paths and access rules.
- `workplace.json`: configured service and Mac capabilities, or their explicit absence.
- `standing.md`: common employee instructions.

Activation prompt, in order:

- `founder_birth.md`: first-day instructions plus organization-building and delegation guidance.
- Organization orientation, where available.
- Inbox snapshot, where available.

## 2. Founder — subsequent activations

Standing instructions, in order:

- `workplace.json`: current identity, manager, and complete owner mandate.
- `workplace.json`: workspace paths and access rules.
- `workplace.json`: configured service and Mac capabilities, or their explicit absence.
- `standing.md`.

Activation prompt, in order:

- One opening from `wake.json`, selected by precedence:
  1. Recovery notice, if recovering from interruption.
  2. Fresh-context introduction with saved handoff notes.
  3. Fresh-context introduction without notes.
  4. Ordinary work update explaining the wake reason.
- Mandate-change notice when applicable to a resumed provider session.
- Organization orientation, where available.
- Inbox snapshot, or “No pending mail.”

## 3. Non-founder — first activation

Standing instructions, in order:

- `workplace.json`: identity, manager, and complete delegated mandate.
- `workplace.json`: workspace paths, access rules, and instructions for looking
  up the founder's mandate.
- `workplace.json`: configured service and Mac capabilities, or their explicit absence.
- `standing.md`.

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
- `workplace.json`: configured service and Mac capabilities, or their explicit absence.
- `standing.md`.

Activation prompt, in order:

- One `wake.json` opening using the same recovery → handoff → fresh → ordinary
  precedence as the founder.
- Mandate-change notice when applicable to a resumed provider session.
- Organization orientation, where available.
- Remaining work-period allowance, if temporarily employed.
- Inbox snapshot, or “No pending mail.”

## Shared behavior

- Standing instructions are assembled for every activation.
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
- `runtime_level-choice.md`: staffing assessment after a hire request.
