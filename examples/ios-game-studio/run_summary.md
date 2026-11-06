# Example Game Studio: a week building Painting Game

A studio built an iPad coloring game, its picture-generation tools, and supporting
studio and website material over September 18–25, 2026. The useful story is the
interaction between working software, extensive verification, and owner feedback:
a substantial amount of work preceded a shared understanding of the requested
picture-generation behavior. The resulting app remained a preview.

![Archived butterfly artwork with generated regions](preview.png)

This is an archived pipeline illustration (`butterfly-mosaic-medium-colour.png`),
not an app screenshot or proof of its final behavior. It illustrates
an intermediate approach whose limitations became part of the feedback.

## What the owner asked for

The [standing mandate](mandate.md) commissioned a clean iPhone/iPad game studio:
no tracking, advertising, manipulative retention mechanics, or routine network
use, with reproducible production and testing. Individual games arrived as
commissions. The owner subsequently asked for a coloring game and a pipeline that
could turn a picture or SVG into substantially different levels of complexity.
The studio mandate alone therefore does not describe the whole game request;
the progression below summarizes the later owner/founder exchanges.

The mandate here is a historical contract. Its text matches the
saved founder mandate excerpt through the point where that excerpt is truncated.
Later repository revisions added instructions that were not in that excerpt.

## How the run developed

1. **Studio and initial product.** The team established Example Game Studio,
   assembled the app and studio repositories, and worked on pictures, controls,
   saving, accessibility, packaging, and a website. The saved records include
   detailed exchanges about both implementation and release requirements.
2. **The central mismatch.** The owner wanted a general way to vary picture
   complexity. The founder later acknowledged substituting a small set
   of manually guided extra regions. More pictures and verification did not
   resolve that product misunderstanding.
3. **A narrower iteration.** The owner brought the scope back to one picture
   designed end to end. The team produced Butterfly variants using generated
   geometry, then explained that only selected parts of the artwork were being
   divided. The owner clarified that the background and whole image should be
   included without a manually specified boundary map.
4. **Visible changes and trade-offs.** The founder's build 106 report describes
   whole-image tessellation from SVG, density and seed, with 89/186/396 cells.
   It also discloses lost thin details and a large proportion of background
   cells. Later reports describe source-color boundaries, more piece sizes,
   zoom changes, seams, and rendering-cost investigations.
5. **Remaining product feedback.** The owner requested direct entry into a
   picture and a piece-size control within it, then reported that fills arrived
   “50ms too late.” That is an owner observation, not a measured latency result.
   The saved source snapshot was build 110; experimental branches and later work
   did not establish an integrated, finished release.

This account draws on the saved owner/founder exchanges, including the build
105–107 reports and the final responsiveness exchange. The underlying transcripts
and source are not distributed with this example.

## Scale and organization

The historical conversation index recorded **2,243 messages across
75 conversation files**. Historical quantitative reporting records **1,974 work
periods**, a span of roughly **173 hours**, and **214.3 combined role-hours**.
The same report estimates that at least one role was active for **86.6 hours**;
the seven-day span includes pauses and must not be read as continuous execution.
Those duration calculations are historical reports, not newly measured runtime.

The recorded role labels identify founder Mara Ellison; Elliot Mace (iOS), Iris
Bellamy (design), Nadia Okonkwo (picture pipeline), Wren Calloway (pictures), Juno
Farrow (canvas), Vera Holt (organizational consulting), and Rowan Vale (independent
play/testing). These are the firm's recorded agent identities, not human testers.

## Model and token usage

The saved usage report records **8,846,434,268 measured tokens**:
8,814,936,909 input tokens (including 8,686,746,143 cached) and 31,497,359 output
tokens (including 8,956,057 reasoning). Cache and reasoning are overlapping
breakdowns, not additional tokens. These are cumulative request counters, not
unique context size or a dollar cost.

**Coverage is incomplete:** input/output counters were measured for 1,957 of
2,053 eligible executions; reasoning was measured for 1,955. This is the recorded
subtotal, not a verified complete-run total. The analyzer window is September
18–27, 2026, sampled on September 26 UTC, and includes role, summary, and task work.

Reported resolved models include `claude-opus-5`, `claude-opus-5-5`,
`gpt-5.6-sol`, and `gpt-6-astra`. Resolved model identity is missing for 1,218
executions, so this list cannot establish every model used throughout the run.

The configured model breakdown was:

| Configured model | Executions | Measured input + output tokens |
| --- | ---: | ---: |
| Claude `opus` | 899 | 5,549,934,691 |
| `gpt-6-astra` | 788 | 2,332,882,335 |
| `gpt-5.6-sol` | 289 | 963,565,975 |
| `gpt-5.6-luna` | 57 | 7,531 |
| Claude `sonnet` | 20 | 43,736 |

These counts include role, summary, and task executions. Configured aliases do
not prove the exact model resolved for every execution. Token coverage is especially
sparse for Luna (1 of 57 executions measured) and Sonnet (6 of 20), so the token
subtotals cannot establish their full usage.

## What the evidence supports

The saved delivery included Swift app source, an Xcode project, automated tests,
regenerable art/pipeline material, and substantial studio and release documentation.
The founder reported 259 headless and 38 UI test passes for build 106, with one
listing-capture skip, plus non-author agent play. These are historical reports
about that build, not independently rerun results for this written account. The same message
explicitly leaves release verification and physical-device experience incomplete.

The run demonstrates an ability to implement changes and investigate defects.
It also shows why passing checks cannot establish that the requested experience
has been understood: the owner had to correct the scope of picture generation,
and responsiveness remained unsatisfactory to them.

Coverage is best effort. Saved `firm analyze` output reports unverified host
liveness, identities limited to captured bindings, and potentially missing
provider tool logs and token counters. A zero gap count does not prove complete
recording. The Markdown communication export does not retain delivery metadata;
the historical reconstructed index did not supply that missing information.
The backup's causal post-mortem was unfinished, so its conclusions are not treated
as verified findings here.

## What is included here

This example retains the historical mandate, this written run account, an
illustrative preview, and a sandbox template for a new run. The original source,
transcripts, detailed evidence, and run archive are not distributed. There is no
downloadable app or source build to try from this example.

Studio branding, game titles, and owner identifiers use generic placeholders.
The reported outcomes and usage figures summarize historical records; readers
cannot independently reproduce or audit them from the retained files alone.
