# AGENTS.md

Instructions for coding agents (Codex, Claude Code, Cursor, etc.) working in `bmalthi/firm`.

## Repository Layout

* [`packages/firm-runtime`](packages/firm-runtime/README.md) — Durable roles, scheduling, messaging, provider adapters, CLI, and web console.
* [`packages/firm-sandbox`](packages/firm-sandbox/README.md) — Linux VM lifecycle, guest controller, SMB file server, network proxy, and optional Mac station leases.
* [`station-helper`](station-helper/README.md) — Signed Swift macOS application inside each Mac station that executes bounded desktop inspection, input, and window capture.
* [`examples`](examples/README.md) — Mandates, runnable results, complete messages, and QA work from recorded runs.
* [`tools`](tools/README.md) — Local test runner (`check.py`), release verifiers, line-of-code analyzer (`loc.py`), and [live VM acceptance suites](tools/acceptance/README.md).
* [`design`](design/) — Architecture, security boundaries, and design rationale.
* [`docs`](docs/) — Operator, setup, development, and release guides.
* [`images`](images/) — Mac VM image recipes.

## Development & Verification

* Use `uv` for all Python work (`uv sync --locked --all-packages`); never install into system or user Python.
* Run the local verification suite before finishing changes:
  ```sh
  uv run tools/check.py
  ```
  Or run targeted checks:
  * Lint: `uv run ruff check .`
  * Package tests: `uv run pytest -q -m "not integration"`
  * Console JS tests: `node --test packages/firm-runtime/tests/web_console.test.mjs`
* Keep `NOTICE` and `LICENSE` byte-identical across the repo root, `packages/firm-runtime/`, and `packages/firm-sandbox/`.

## Architectural Invariants

* **Compatibility generation (`firm-release.json`):** `firm-runtime` and `firm-sandbox` share a single version and compatibility generation (`COMPATIBILITY_GENERATION` in `firm_sandbox/compatibility.py`). Do not add database migrations or fallback shims for old firm state; incompatible state is refused before mutation.
* **Inspection via `firm analyze`:** When inspecting a firm directory, use `firm analyze <dir> --section <name> --json` rather than hand-querying `durable/firm.sqlite3`.
* **Non-interactive CLI usage:** Bare `firm start` (with no directory argument) requires an interactive TTY. When starting or operating a firm from an agent, follow **Path (A): For Coding Agents** in [`docs/installation.md`](docs/installation.md#path-a-for-coding-agents-recommended--non-interactive-cli-setup) (`firm check <dir>`, `firm init <dir>`, `firm start <dir>`).
* **Public identity:** Use `bmalthi` (`4552227+bmalthi@users.noreply.github.com`) for commits and attribution. Never include personal legal names, emails, or credentials in code, docs, or commits.
