# Flashcards CLI: a small tool delivered by one founder

The run produced a Python command-line flashcards tool using only the standard
library. It creates named JSON decks, adds cards, quizzes through stdin or
scripted answers, and persists per-card correct/incorrect totals. Quiz order puts
the highest miss rate first; untested cards follow tested cards.

Run: `2026-11-05_flashcards-cli` · November 5, 2026 · 2 recorded messages ·
[Original mandate](mandate.md)

## How the run developed

The owner enabled open public web access, restricted execution to Codex, and
allowed up to six agents. The founder completed the work in one execution without
hiring other roles. The two preserved messages are the owner's execution
constraints and the founder's delivery report. There was no recorded feedback
or independent agent QA cycle.

## Model and token usage

The provider reported **gpt-6-luna** for the single founder execution.
`records/usage.json` in the archive records complete counters for that execution:

| Counter | Tokens |
| --- | ---: |
| Input, including cached input | 369,472 |
| Cached input (part of input) | 319,104 |
| Output, including reasoning | 5,263 |
| Reasoning (part of output) | 390 |
| Total input + output | **374,735** |

These are cumulative request counters, not unique context size or a dollar cost.
They exclude the owner's setup and packaging chat. The run used 12 tool calls.

## Verification and limits

The founder ran `python3 -m unittest discover -v`: **6 tests passed**. The original
captured output is preserved as `test-evidence.txt`. During packaging, the same
six tests passed again with `uv run -m unittest discover`; the separate output is
in `records/packaging-test-evidence.txt`.

The suite covers JSON creation, scripted scoring, stdin interaction, most-missed
ordering across sessions, invalid deck names, and missing scripted answers.
The delivered README includes a transcript of usage commands actually run by the
founder. This is a small local tool; these checks do not establish concurrent-writer
safety or exhaustive handling of damaged deck files. No independent agent tested it.

## Try it or read the evidence

Extract [run_details.zip](run_details.zip) and change to
`output/org/founder/work/artifacts/flashcards/`. Run `uv run flashcards.py --help`
and `uv run -m unittest discover`; the included README has deck and quiz examples.

The archive preserves source, tests, original usage documentation and test evidence,
both complete messages, and analyzer usage records. The firm was stopped and its
VM disks discarded after export. VM images, runtime databases, credentials, private
role homes, provider sessions, and caches are excluded from this public example.
