# Kestrel

An offline space-station dashboard showing **what a small team can achieve with
a specific mandate and a bounded feedback cycle**. The interesting part is how
implementation, independent QA, and owner use each contributed something different.

The mandate explicitly asked for an implementation specialist, an independent
tester, and interface agreement before parallel work. The founder (`Mira Chen`)
acted as product lead rather than coding solo, hiring `e1` (`Alex Vale`,
implementation) and `e2` (`Rowan Ellis`, independent QA) and putting all three in
a shared participant-set conversation (`c1`) to lock `shared/station/INTERFACE.md`
before parallel work began. Both specialists recorded reusable `playbooks/` on
staging edits in `~/scratch` so they would not clobber each other's
files in `shared/station/`, and on running offline Chromium checks in `--network none`
rootless Docker containers. This speaks directly to the [root README's](../../README.md)
question about how much structure a self-organizing firm needs: here, the owner
supplied some of that structure in plain English.

The first delivery worked, but owner use in Safari revealed that the dashboard
felt static and buried incident response beneath large telemetry cards. The team
added time controls, moved incident actions forward, and clarified gauge meaning.
Independent QA then caught a wrong-color healthy gauge band and verified the fix.
Final checks covered 28 domain tests, nine Chromium browser groups, and real-clock
behavior. The coordination led to visible improvements within the original
single-station scope.

This is a useful companion to Firm's concerns about testing theatre: the
[message record](run_details.zip) lets you follow an agreed behavior through
implementation, feedback, a concrete defect, and a retest. It does not establish
that the team was faster or cheaper than one coding agent. The optional
second-station request in the mandate was never activated.

Start with the [run summary](run_summary.md) for the preview and results. In the
archive's `messages.md`, m1–m3 establish the working agreement, mk records owner
feedback, and m1g reports the final delivery.

- [Original mandate](mandate.md)
- [Full run details](run_details.zip): app, complete messages, tests, screenshots,
  and QA reports.
- [Sandbox template](sandbox.toml) for a new run.

To try it, extract the ZIP and open `output/shared/station/index.html` locally.
No server or dependencies are needed.
