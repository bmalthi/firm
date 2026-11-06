# Examples: what we gave, what we learned

Five actual Firm runs that put the [root README's](../README.md) design goals
and open questions into practice: how much structure a founder needs, when
independent QA improves the product, how owner feedback changes direction, and
how a working organization can accumulate process without shipping a satisfying
result. Each example's README and run summary describe its particular story.
Four examples include a ZIP with the product and recorded correspondence; the
iOS game studio retains a written account without its source or transcripts.

| Example | Read the run | Why it is interesting |
| --- | --- | --- |
| Miniature city | [Little City](miniature-city/README.md) | QA fixes real simulation bugs; owner play reveals that passing rules still make a weak game. |
| Space-station dashboard | [Kestrel](space-station/README.md) | A specific mandate and a small team turn interface agreement and independent QA into visible improvements. |
| iOS game studio | [Painting Game](ios-game-studio/README.md) | A consultant, extensive verification, and thousands of messages expose the gap between organizational activity and a finished product. |
| Flashcards CLI | [Flashcards](flashcards-cli/README.md) | A founder allowed to hire chooses to work alone, providing a small-task case with recorded usage. |
| iOS tap-to-fly | [Lumen Drift](ios-tap-to-fly/README.md) | Roles coordinate real Mac stations, then have to reconcile observed gameplay with conflicting UI test results. |

Each folder is mostly flat:

- `run_summary.md` — the product, the run's progression, what worked, and what
  remains limited, with instructions for trying the result and a screenshot where applicable.
- `mandate.md` — the original request or identified historical contract; later owner messages can refine it.
- `sandbox.toml` — a template for starting a new firm with that mandate.
- `run_details.zip` (all except the iOS game studio) — the delivered app/project, full transcripts, tests, reports,
  screenshots, and original archive README with build/run pointers.
- `README.md` and, for visual apps, `preview.png` — the entry point and summary illustration.

Feedback and iteration are part of these examples: the summaries explain what
observations led to changes and how those changes affected the product. They
separate product feedback, implementation defects, and incomplete verification.

## Explore the full details

Where provided, extract an example's `run_details.zip` into its own directory to inspect the
deliverable, correspondence (`messages.md` and `messages.json`), and role
workspaces (`profile.md`, `track-record.md`, and `playbooks/`). See each
example's README and `run_summary.md` for its exact layout, verification scope,
and archive limits:

- **[Little City](miniature-city/README.md)** ([summary](miniature-city/run_summary.md)) —
  `output/shared/city/` delivery, `output/org/` role workspaces, and 97 messages
  including firewall access requests (`request_access`) and owner playtest steering.
- **[Kestrel](space-station/README.md)** ([summary](space-station/run_summary.md)) —
  `output/shared/station/` delivery, upfront `INTERFACE.md` contract between
  builder and tester, and 52 messages across two iterations.
- **[Painting Game](ios-game-studio/README.md)** ([summary](ios-game-studio/run_summary.md)) —
  written account of the build-110 preview, 2,243 historical messages across 75
  conversations, 8-role activity, ~8.8B measured tokens, and the consultant review.
  The underlying source, transcripts, and evidence archive are not distributed.
- **[Flashcards](flashcards-cli/README.md)** ([summary](flashcards-cli/run_summary.md)) —
  single-founder delivery in `output/org/founder/work/artifacts/flashcards/`,
  both messages, test outputs, and complete single-execution token counters.
- **[Lumen Drift](ios-tap-to-fly/README.md)** ([summary](ios-tap-to-fly/run_summary.md)) —
  `output/shared/tap-to-fly/` Xcode project, 36 messages, Mac station coordination,
  and raw UI test logs and failure reports (the incomplete ~65 MB `.xcresult`
  bundle remains omitted).

These are deliverable archives, not resumable Firm installations: they exclude
VM images, runtime databases, credentials, private role homes, and provider
sessions. Browser apps run locally by opening their `index.html` after extraction;
iOS projects require macOS, Xcode, and an installed simulator runtime.

To start a new firm, use a folder's `mandate.md` and `sandbox.toml` with the setup
instructions in the [repository README](../README.md).
