# AGY replay fixtures

The stream envelope follows the [official headless protocol](https://antigravity.google/docs/cli/headless/).
Each JSON fixture declares `fixture_kind`, `events`, `exit_code`, and a partial
`expected` analysis checked by both pytest and `agy_probe.py replay`.

- `captured_search_role.json` and `captured_search_task.json` capture AGY 1.5.3
  subscription search on 2026-11-08 with the launcher's exact role/task settings
  and `search_web` added to the declared tools. Both finish SUCCESS with successful
  `search_web` steps, no `read_url_content` step, and no permission allow entry.
  `read_url(*)` and `execute_url(*)` remained denied. Response prose, arguments,
  and native identifiers are omitted; the gate separately checked the answer date.

- `success.json`, `process_failure.json`, and `missing_result.json` are synthetic
  protocol tests, not successful AGY executions or authentication evidence.
- `startup_error.json` preserves the structure and zero counters observed from
  CLI **1.5.3** on 2026-11-07 with an isolated HOME and no injected credentials.
  Startup returned exit 1 and one ERROR result with an empty conversation ID,
  without an init event. Error and finished-reason prose were replaced. No
  credential, account identifier, private path, or actual conversation ID remains.
- `captured_mcp_float_ignored.json` and `captured_mcp_integer_authenticated.json`
  capture the launcher's silent MCP configuration failure and its one-field fix:
  `timeoutSeconds` must serialize as an integer, not `2.0`. Both streams report
  SUCCESS and the same 60 native tools in init; only the corrected configuration
  has an authenticated server receipt and `mcp_firm_org_tree` event. The real Firm
  catalog names were served by harmless echoes, not a production Firm server.

The parser does not infer authorization categories from provider prose. Unknown
activation accounting, context occupancy, quota, and inner MCP tool identities
remain unproven. No fixture means the provider is ready for Firm registration.
