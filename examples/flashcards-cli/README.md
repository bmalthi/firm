# Flashcards CLI

A standard-library Python study tool whose most interesting organizational
choice was **not hiring anyone**. The owner allowed up to six roles; one Codex
founder completed the task in a single execution with 12 tool calls.

Firm's [runtime design](../../design/runtime.md) lets the founder decide how to
staff a mandate. This is the smallest end of that experiment: the founder
self-named `Alex Rowan — Founder and CLI Product Engineer` in `profile.md`, built
JSON decks, interactive or scripted quizzes, persistent scores, and most-missed
ordering directly, and recorded the delivery in `track-record.md` with just two
messages—execution constraints and delivery. There was no delegation or recorded
owner feedback cycle.

It makes a useful companion to the [root README's](../../README.md) question
about whether Firm helps on small tasks. The archive exposes the work and its
usage: a reported `gpt-6-luna` execution consumed 374,735 cumulative input/output
tokens, including 319,104 cached input tokens. Those counters are not unique
context size or a cost estimate, and there is no direct-agent comparison from
which to claim an efficiency gain.

Six tests passed during the run and again during packaging, covering persistence,
quiz interaction, ordering, and invalid inputs. That supports a small working
delivery; it was not independently tested by another role. Read this alongside
[the game studio](../ios-game-studio/README.md) to see how widely organizational
scale and communication volume can vary within Firm.

- [Run summary](run_summary.md), including the complete usage breakdown and test scope.
- [Original mandate](mandate.md).
- [Full run details](run_details.zip): source, tests, usage examples, both messages,
  and recorded token usage.
- [Execution settings](firm.toml) and [sandbox template](sandbox.toml).

To try it, extract the ZIP, change to
`output/org/founder/work/artifacts/flashcards/`, and run
`uv run flashcards.py --help`. Run the tests with `uv run -m unittest discover`.
