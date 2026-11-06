# Firm tools

Developer-run utilities and live acceptance checks. Nothing here is shipped or
trusted by the product.

## Local checks

From the repository root, run the complete offline developer check set:

```sh
uv run tools/check.py
```

It runs Ruff, both Python test suites without integration tests,
package builds, Node's console tests, the Swift checks, and
a full ad-hoc signed station-helper build when their platform tools are
available. Missing Node or Swift requirements are reported as skipped. Live VM,
provider, and desktop checks are not included.

## Acceptance

The [acceptance tools](acceptance/) exercise execution on the Linux VM, the
dispatcher, Docker inside a role, network cutoff and recovery, and the Mac lease.
Run them from the Firm repo root as `uv run tools/acceptance/...`, not from inside
`tools/`. Each takes `--root FIRM_DIR --evidence DIR`; only the
[Mac lease checks](acceptance/mac/README.md) need `[mac]`. Their fixtures, the
Metal probe and the iOS build app, live with them. Use `firm-sandbox --config PATH`
for VM operation and maintenance.

## Python line counts

Run from the Firm repo root. No third-party dependencies are needed.

```sh
uv run --project tools tools/loc.py
uv run --project tools tools/loc.py packages/firm-sandbox --by-file
uv run --project tools tools/loc.py --json
```

The default scan covers the entire Firm repo, including these tools. The table
separates source code from tests and reports files, non-comment code lines,
comment lines, blank lines, and total physical lines.

- Counts `.py` and `.pyi` files. Other languages, config, and documentation are excluded.
- Uses Python syntax and tokens: `#` inside a string is code. Module, class, and
  function docstrings count as comments; other multiline strings count as code.
- Inline comments count toward comments; the same line also counts as code if it
  contains code. Thus code and comment columns can overlap. Whitespace-only lines
  count as blank, even inside multiline strings and docstrings.
- Tests include files under `test/` or `tests/`, `test_*.py`, `*_test.py`, and
  `conftest.py`. Test helpers and fixtures count as test code. These are line
  counts, not collected test cases or test coverage.
- Skips virtual environments, caches, build output, vendored dependencies, and
  symlinks. In Git repos, honors Git ignore rules, including for tracked files;
  untracked files are included unless ignored. Outside Git, only the built-in
  exclusions and explicit globs apply.
- Fails on unreadable or invalid Python files instead of reporting partial totals.

Add exclusions or test naming conventions with repeatable globs, relative to the
scan directory. Quote globs to prevent shell expansion; `*` can match `/`.

```sh
uv run --project tools tools/loc.py --exclude 'tools/*' --test-pattern 'specs/*'
```

### Daily totals

`--daily` counts the tree at the last commit of each day in Git history, so you
can see how the line counts move over time. The table adds the day's commit and
the change in code lines from the previous day; `--json` also carries the
source/test split for each day.

```sh
uv run --project tools tools/loc.py --daily
uv run --project tools tools/loc.py --daily --since '30 days ago'
uv run --project tools tools/loc.py packages/firm-sandbox --daily --json
```

- Days with no commits are omitted; the counts on the days around them show the
  flat stretch. A day is the committer date in that commit's own time zone.
- Only commits reachable from the current `HEAD` count, so the series follows
  the checked-out branch.
- `--since` takes any Git date, such as `'30 days ago'` or `2026-01-01T00:00:00+0000`.
  A bare date is read as that day at the current time of day.
- History counts the files tracked in each commit, applying the built-in
  exclusions and `--exclude` globs. Git ignore rules are not replayed, so the
  last day can differ slightly from the working-tree table above, which also
  includes untracked files and drops tracked-but-ignored ones.
- Blobs are counted once and reused across days, so a full history scan of this
  repo takes a couple of seconds.

### Snapshots

Save a baseline, then compare later (use the same scan path and options):

```sh
uv run --project tools tools/loc.py --save tools/.loc.json
uv run --project tools tools/loc.py --compare tools/.loc.json
```

Use both `--compare` and `--save` to show the delta and replace the baseline in
one run. Snapshots contain totals, per-file counts, scan options, and a UTC
timestamp; `--json` also includes deltas when comparing. Use distinct filenames
to retain historical snapshots. Snapshot parent directories must already exist.

Run the tool's focused checks:

```sh
uv run --project tools -m unittest discover -s tools/tests -v
```
