# Firm runtime

The runtime package owns durable roles, scheduling, messaging, organization
discovery, provider sessions and the local owner console. It depends on the
sibling `firm-sandbox` package for execution on the firm's Linux VM and the optional
Mac lease. The packages are built and released together as one Firm release.

For first use, install Firm and run `firm start`. The guided flow creates or resumes
a firm, handles provider sign-in, and starts the selected first task. The default host
runs detached and keeps the console available after execution stops; owners can send
and reply to messages, decide access requests, stop or resume execution, and browse
or download files there. See the [installation guide](../../docs/installation.md) and
[runtime operations](../../docs/runtime.md).

From the repository root:

```sh
uv sync
uv run pytest -q -m "not integration" packages/firm-runtime/tests
uv build --package firm-runtime
```

The package includes role prompts, the weekly-planner starter mandate and web console
assets. Firm data belongs in the owner data directory, outside the package source.

See the [runtime guide](../../docs/runtime.md) and
[Linux VM guide](../../docs/linux-vm.md) for usage.

## Analysis

`firm analyze` prints execution, usage, messaging, readiness and Mac lease evidence
from a stopped or running firm, or from its `firm export`, without a host, VM or
provider; the console's **System → Diagnostics** view reads the same data. Options, routes and how
to interpret the evidence are in the [runtime guide](../../docs/runtime.md#analysis).
Focused checks:

```sh
uv run pytest -q packages/firm-runtime/tests/test_analysis.py
```
