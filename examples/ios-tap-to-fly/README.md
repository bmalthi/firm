# Lumen Drift

A native tap-to-fly game that puts Firm's **Mac station design and independent
verification under real pressure**. The team built and played the game in iOS
Simulator, but different roles obtained conflicting automated UI results.

The [root README](../../README.md) describes the awkwardness of Linux-based
roles remotely operating a shared Mac VM. Here that becomes a concrete workflow:
the founder (`Avery Lane`), engineer (`e1`, `Rowan Vale`), and tester (`e2`,
`Mira Chen`) coordinate station leases across a 1:1 thread (`c1`), a three-way
handoff thread (`c2`), and a scheduled self-reminder (`c5`) while waiting on
station availability. Both specialists recorded detailed `playbooks/` on the real
sandbox sharp edges they hit: building in local `~/scratch` because `simctl io screenshot`
atomic renames fail on the shared SMB mount (`Cocoa error 513`), driving Xcode 27's
Device Hub window via `AXPress`, and spacing pointer captures ~160ms apart so live
arcade runs don't die mid-query. The [Mac sandbox design](../../design/sandbox.md)
enables this work; access to the tools still leaves the team responsible for
interpreting their results.

The engineer reported two passing UI tests. The founder reproduced failures;
disabling parallel testing did not fix them. Independent QA then passed 22 core
checks, including nine additional edge cases, but flight/restart automation failed
eight assertions. Actual pointer interaction demonstrated upward flight, scoring,
collision, and repeated restart with the best score preserved.

That disagreement is the reason to read this example. Independent QA challenged
the builder's result and separated working game logic, observed gameplay, and
failing automation. A timing race was suggested but never established, and the
UI discrepancy remained open at delivery. It is a particularly useful example
of why a completion report needs more than a single “tests passed” verdict.

Start with the [run summary](run_summary.md) for the preview and evidence limits.
In the archive's `messages.md`, mq records the founder's repeated UI failures and
my records final independent QA.

- [Original mandate](mandate.md)
- [Full run details](run_details.zip): Xcode project, complete messages, tests,
  captures, and failure reports.
- [Sandbox template](sandbox.toml) for a new run.

To try it on a Mac with Xcode, extract the ZIP, open
`output/shared/tap-to-fly/LumenDrift.xcodeproj`, select the LumenDrift scheme and
an installed iPhone simulator, and Run.
