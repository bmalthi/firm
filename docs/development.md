# Development checks

Run commands from the repository root on macOS, using the shared uv environment:

```sh
uv sync --locked --all-packages
uv run ruff check .
uv run pytest -q -m "not integration"
uv build --all-packages
```

Run the same local checks with one command:

```sh
uv run tools/check.py
```

This runs Ruff, the package and developer-tools Python tests without integration
tests, Node's console tests, all package builds, and a
full ad-hoc signed station-helper build. Local checks skip unavailable Node/Swift
tools; `uv run tools/check.py --required` makes missing tools fail and is required
for local release validation. Missing repository test files always fail. There is
no GitHub CI; run this batch locally before preparing a release.
The Swift portion runs the ten scripts in `station-helper/tests/` and the helper
build when `xcrun --find swiftc` succeeds. The build uses a temporary directory and
does not use a configured signing identity or keychain. Live acceptance checks are
excluded.

The Python suite needs no provider login, enrolled image or running VM. Some tests
bind loopback sockets or use macOS process/filesystem facilities: a restricted
execution sandbox can refuse these even when the code is correct. Run the suite
from an ordinary terminal with local socket access. Swift checks skip if their
compiler is unavailable; use the [helper's test scripts](../station-helper/tests/)
when changing Swift code.

The independent developer-tools suite is not included by the root pytest paths:

```sh
uv run pytest -q tools/tests
```

Console formatting and role-tree tests use Node's built-in runner (verified with Node 26), with
no npm dependencies. They execute the shared browser modules without a DOM:

```sh
node --test packages/firm-runtime/tests/web_console.test.mjs
```

For a targeted edit, run its test module and related boundary tests first. Run the
full suite before handing over changes spanning components. The
[acceptance tools](../tools/acceptance/README.md) use disposable real firms and
exercise actual VM, provider and optional Mac behavior. Unit tests cannot prove
provider login, VM provisioning, desktop consent, or guest cleanup on a live machine.

## macOS imports fail after a successful sync

If `uv sync` succeeds but Python cannot import `firm_runtime` or `firm_sandbox`,
check the editable-install path files with `ls -lO .venv/lib/python*/site-packages/*.pth`.
Python skips `.pth` files carrying the macOS `hidden` flag. This can recur in
synced Documents/Desktop folders; see [uv issue #16977](https://github.com/astral-sh/uv/issues/16977).
Clearing flags with `chflags -R nohidden .venv` may only fix it temporarily.

To keep uv's environment project-local while excluding it from iCloud storage,
create it in `venv.nosync` and link uv's default `.venv` path to it. First move an
existing `.venv` out of the repository for safekeeping, then run:

```sh
UV_PROJECT_ENVIRONMENT=venv.nosync uv sync --locked --all-packages
ln -s venv.nosync .venv
uv run tools/check.py --required
```

After this one-time setup, use the normal `uv sync` and `uv run` commands above.
Both environment paths are ignored by Git. The `.nosync` suffix is an
[Apple-documented exclusion](https://developer.apple.com/library/archive/documentation/General/Conceptual/iCloudDesignGuide/Chapters/iCloudFundametals.html).

## Test boundaries

- Test observable transitions, permissions, persistence, retries and failure cleanup.
  Keep exact assertions for structured contracts such as event fields, command
  arguments, credentials and protocol schemas.
- For prompts, test resource selection, one-pass substitution, literal user content,
  tool exposure and privacy boundaries. Avoid duplicating paragraphs or checking
  synonyms merely to freeze their wording. Review instruction quality as prose.
- Execute generated scripts with controlled dependencies where possible. Searching
  source code for a function call does not prove the call happens or is ordered safely.
- Give shared test doubles their own support module; test modules should not import
  each other's tests or private setup functions. Share stable domain setup, while
  keeping scenario-specific state visible in the test.
- Use temporary directories and synthetic credentials. Tests must not inspect the
  operator's real firms or contact a live provider by default.

## Repository map

| Location | Responsibility |
| --- | --- |
| `packages/firm-runtime/src/firm_runtime/` | Durable roles, mail, scheduling, provider adapters, CLI and console |
| `packages/firm-sandbox/src/firm_sandbox/` | VM lifecycle, role isolation, file access and optional Mac stations |
| `packages/*/tests/` | Automated package and boundary tests |
| `station-helper/` | Swift desktop helper and standalone tests |
| `examples/` | Mandates, runnable results, complete messages, and QA work |
| `firms/` | Local firm runs and durable state (ignored by Git) |
| `images/` | Tracked Mac VM image recipes |
| `images/.images/` | Built VM images (ignored by Git) |
| `tools/acceptance/` | Live checks requiring disposable infrastructure |
| `docs/` | Installation and operator instructions |
| `design/` | Implemented contracts and explicit deferred proposals |

There are no database migrations or compatibility shims. Stop and export a firm
before updating code that changes its schema; read that export using its original
code revision, and create a new firm for the changed schema.

Both packages ship as one version; the runtime pins its matching sandbox package.
Breaking persisted-state or guest-contract changes increment one shared compatibility
generation. The marker must be checked before opening a mutating Store or changing
a guest. Compatible releases retain original firm/image provenance. The
[release guide](releases.md) covers archive/install checks, browser acceptance and live first-run evidence.
