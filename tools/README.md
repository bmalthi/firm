# Firm developer tools

Run from the repository root using the shared uv environment:

```sh
uv sync --locked --all-packages
uv run tools/check.py
```

The check runner runs Ruff, package and tools tests, package builds, console
JavaScript tests, and available Swift checks. Missing Node/Swift tools are
reported as skipped; use `--required` to require them. Live acceptance is separate.

| Task | Command or guide |
| --- | --- |
| Tools regression tests | `uv run pytest -q tools/tests` |
| Release candidate checks | `uv run tools/release_check.py` |
| Tagged release verification | `uv run tools/verify_release.py --tag TAG --dist dist` |
| Live VM and provider checks | [Acceptance catalog](acceptance/README.md) |
| Mac lease checks | [Mac catalog](acceptance/mac/README.md) |

Follow the [release guide](../docs/releases.md) for build and publication order.
These utilities are not shipped with the product.

## Python line counts

```sh
uv run tools/loc.py
uv run tools/loc.py packages/firm-sandbox --by-file
uv run tools/loc.py --daily --since '30 days ago'
uv run tools/loc.py --save tools/.loc.json
uv run tools/loc.py --compare tools/.loc.json
```

Counts Python source and tests separately, honoring Git ignores and excluding
caches, generated output and symlinks. Docstrings count as comments. Use `--json`
for structured output, `--exclude` for exclusions and `--help` for all options.
Daily totals use the last reachable commit of each day. The optional local
`.loc.json` baseline is ignored by Git; keep other generated results outside Git.
