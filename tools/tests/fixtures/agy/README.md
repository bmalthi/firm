# AGY protocol fixtures

Synthetic and sanitized captured streams exercise completion, malformed output,
tool failures, session identity, cumulative usage, search, timeout/resume and
credential-expiry errors. Both runtime provider tests and tools protocol tests
consume these fixtures.

Each fixture declares its kind, events, exit code and expected analysis. Optional
parser options describe multi-turn streams or an expected conversation ID.
Captures omit response prose, tool arguments, real native identifiers and credentials.
Synthetic fixtures do not establish live provider acceptance. Captured streams retain
the original model IDs and CLI version/date; they are historical protocol evidence,
not evidence that the current Hydrogen default has passed live acceptance.

`captured_search_role.json` and `captured_search_task.json` record AGY 1.5.3
subscription search on 2026-11-08, with `search_web` added to the declared tools.
Both completed with successful search steps, no page-fetch step and no permission
allow entry; URL-reading tools remained denied. Native identifiers and response prose
were sanitized. `startup_error.json` preserves the zero-counter error shape from
AGY 1.5.3 on 2026-11-07 without injected credentials, with error prose replaced.
The MCP timeout captures used harmless echoes of the Firm catalog, not a production
Firm server; only the integer-timeout capture had an authenticated server receipt.

The MCP timeout fixtures preserve the regression where integer and floating-point
timeout settings produced different tool availability despite terminal SUCCESS.
Expiry and yield fixtures distinguish a successful process exit from completed work.
