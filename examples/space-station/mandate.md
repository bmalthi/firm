# Space-station incident dashboard

Build an offline incident-management dashboard for a fictional space station.
Include simulated sensor readings, incidents with severity and ownership, an event
timeline, and controls to inject equipment failures. Use synthetic data and make
the simulation deterministic with a seed.

Hire an implementation specialist and an independent QA specialist. Agree on
interfaces before parallel implementation. Deliver a working first version before
expanding scope, with tests for incident creation, assignment, resolution, and
repeated events.

Save the runnable app and instructions in `shared/station/`. Attach the entry file
to your owner message. Report what was actually verified and any remaining defects.

## Optional follow-up for the owner to send later

This is a separate change request, not part of the initial implementation. The
owner can send it after the first delivery or midway through development. To
demonstrate continuity, the owner can stop and resume the firm between phases.
Wait for the owner's explicit request before implementing this change:

> New requirement: the dashboard must support two space stations simultaneously.
> Existing single-station behavior must continue to work. Have QA identify likely
> regressions, coordinate the change, and report the results.
