# Little City

A small browser city builder that exposes the gap between **a correct simulation
and a game worth playing**. Independent QA found real allocation bugs; owner
feedback then changed what the team was trying to build.

Homes initially got jobs without roads. Fixes introduced double-counting and a
case where adding a dead-end road could destroy employment. Concrete QA
counterexamples pushed the implementation toward flow-based job allocation. That
is useful verification in the sense discussed in the [root README](../../README.md):
tests changed the product rather than merely producing a delivery checklist.

But correctness was only half the problem. After the team added costs and a
12-month challenge, the owner could win one preset with three road tiles and one
step—and winning stopped the simulation. The founder responded by turning it into
a continuing sandbox. The 97-message record shows how an owner can steer a firm
through mail while leaving the team to work out the implementation.

The [prompting design](../../design/prompting.md) leaves workflow and product
judgment to the roles; this run makes both the workplace mechanics and the limits
of that autonomy tangible. The founder (`Rowan Vale`) built the app directly and
hired one report (`e1`, self-named `Mira Vale — Simulation QA Specialist`), who
refused to touch implementation files, requested firewall domain approvals from
Operations (`c3`, `c4`) to pull a rootless Docker Playwright image, and wrote a
reusable playbook (`playbooks/snapshot-browser-qa.md`) for testing read-only HTML
snapshots in `~/scratch`. Independent tests improved the rules, while actual play
exposed the weak goal. The final city can still plateau, and the broad regression
suite covered the penultimate substantive version; final QA was narrower.

Start with the [run summary](run_summary.md) for the preview, iteration history,
and verification scope. In the archive's `messages.md`, read m1e for the first
product critique, m1v for the sandbox rethink, and m2m–m2p for delivery and final QA.

- [Original mandate](mandate.md)
- [Full run details](run_details.zip): app, complete messages, tests, screenshots,
  and QA reports.
- [Sandbox template](sandbox.toml) for a new run.

To try it, extract the ZIP and open `output/shared/city/index.html` in a browser.
No server is needed.
