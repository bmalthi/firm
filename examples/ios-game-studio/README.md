# Example Game Studio — Painting Game

A week-long, 8-role iOS studio run (~8.8B measured input/output tokens across
1,974 work periods) that brings the [root README's](../../README.md) questions
about **organizational overhead, testing theatre, and product judgment** into
focus. The founder (`Mara Ellison`) hired seven reports across engineering,
design, picture pipeline, art, canvas, QA, and consulting (`e1..e7`). While `e1`
(`Elliot Mace`, iOS Engineer) spent a third of the firm's total active minutes
building and hitting repeated `mac_disk_full` station admission refusals (`m19w`–`m1a5`),
the founder ran 822 work periods at a 0.6-minute median—turning into a mail-triage
router across 2,243 messages and 75 conversations while the coloring game remained
an unfinished preview.

The most revealing episode is the consultant intervention described in the root
README. In the historical owner/founder conversation, the owner asks for a review (m17y)
and the founder hires `e6` (`Vera Holt — Organizational Consultant`, m180), who
opens separate 1:1 interview conversations (`c1r`–`c1x`) with every colleague.
Her report (`c1y`, m18j) notes that the waste was not in testing itself, but in
attaching *"124 KB of checksums"* and a *"202 MB capture matrix"* to a preview build.
In `c1z` (m18m), designer `Iris Bellamy` (`e2`) refuses the consultant's suggestion
to message the owner directly because her role mandate reserves owner contact to
`founder`, and warns that calling a quick launch check "play" will quietly erode
the release bar over time. The owner turns the consultant's findings into specific
operating changes (m18s): ship cheaper previews, give implementers authority to
land fixes, stop acknowledgement loops, and drop release packets and checksum
manifests from the preview path. The founder reports adopting those changes in
m18v; that message still says the preview is pending.

This is an unusually concrete view of Firm's workplace metaphor in action—and of
the bureaucracy it can permit. Roles can organize, consult, and change their own
process through [durable correspondence](../../design/messaging.md). Whether those
changes produce a better product still has to be judged from what ships.

A separate problem survived extensive testing: the team had substituted manually
guided extra regions for the requested general picture-complexity pipeline. Owner
feedback narrowed work to one picture, then clarified that subdivision should cover
the whole image. Later changes improved the pipeline, but responsiveness remained
unsatisfactory to the owner. Hundreds of reported test passes on an earlier build
could not establish that the intended experience had been understood.

Read this as evidence of the tensions described in the root README, rather than a
settled explanation of why large firms stall. The saved causal post-mortem was
unfinished; the [run details](run_summary.md) distinguish historical test reports,
the build-110 preview, and incomplete usage coverage. They also explain the
pipeline illustration. Source and transcripts are not distributed with this example.

- [Historical studio mandate](mandate.md); later owner requests shaped the game.
- [Run details](run_summary.md): product development, organizational changes,
  reported results, and usage coverage.
- [Sandbox template](sandbox.toml) for a new run, with a locally supplied Mac image.

This is a written historical account, not a downloadable app, source archive,
or resumable firm. Branding, game titles, and owner identifiers use generic
placeholders. The underlying source, correspondence, and evidence are omitted.
