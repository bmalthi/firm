# Provider acceptance

Use fresh disposable firms and private evidence directories. These checks consume
provider quota. Run commands from the repository root.

Run `llm_cli.py` once per provider with a fresh, unbound **Linux-only** firm and a new
evidence directory each time. The selected profile must match `--provider`; its
accounts need `max_active >= 2` for full 2-way concurrency checks. It uses
`[llm.PROVIDER]` model/effort from the provider config, or runtime `[llm]` defaults,
and runs fresh, resumed, concurrent/refused, and cleanup checks via the installed guest
CLI shims. `--timeout` sets each CLI call's deadline (minimum 30 seconds). It prints
sanitized JSON evidence (`passed: true/false`) and exits nonzero on failure.

`result.json` records sanitized boolean checks (`fresh_ok`, `recalled`,
`session_present`, `concurrent_succeeded`, `concurrent_refused`, `cleanup_clean`,
`session_persisted`, `completed_runs`) and hashed `summary_runs` (`kind = "llm"`)
telemetry, never raw answers, nonces, or credentials. It verifies that ephemeral `/run`
auth directories under `$FIRM_STATE_DIR/llm` are removed after CLI exit while
persistent session files remain under `$FIRM_PRIVATE_HOME/.firm/cli/<provider>`.


## Experimental AGY integrated acceptance

This recipe uses the registered adapter, consumes subscription quota
and starts/stops disposable Linux VMs. Offline parser/evidence tests do not establish
integrated acceptance.

Prepare fresh directories, then save the following TOML as
`$agy_acceptance/firm.toml`:

```sh
umask 077
agy_acceptance=$(mktemp -d /tmp/firm-agy-acceptance-XXXXXX)
mkdir "$agy_acceptance/provider" "$agy_acceptance/summary" "$agy_acceptance/llm-cli"
```

```toml
max_active = 2
staffing = false
founder_profile = "agy"

[profiles.agy]
provider = "agy"
model = "gemini-4-neon"
effort = "medium"
accounts = ["agy-dedicated"]

[accounts.agy-dedicated]
provider = "agy"
max_active = 2
auth_file = "/PRIVATE/AGY-PROFILE/home/.gemini/antigravity-cli/antigravity-oauth-token"

[summary.agy]
model = "gemini-4-hydrogen"
effort = "medium"

[llm.agy]
model = "gemini-4-neon"
effort = "high"
```

Create a dedicated profile using the supported login command in your terminal:

```sh
uv run firm login agy-dedicated --provider agy --auth-file /PRIVATE/AGY-PROFILE/home/.gemini/antigravity-cli/antigravity-oauth-token
```

Replace the example absolute path in both the command and TOML. Reuse that
authenticated profile directly; do not copy credentials. See the
[AGY setup requirements](../../docs/installation.md#experimental-agy-integration).
Acceptance never initiates login and rejects ordinary HOME profiles.

Save this `sandbox.toml` in each subdirectory, changing `id` to a unique lowercase
name for each fixture:

```toml
[firm]
id = "agy-provider-unique"
cpus = 4
memory_gib = 8
disk_gib = 60

[network]
egress = "observe"
allow_hosts = []
```

Keep files private and omit `[mac]`. The guest's pinned AGY and subscription egress
are described in the [sandbox README](../../packages/firm-sandbox/README.md);
no Google wildcard or guest renewal host is needed. Initialize only sandbox markers;
these checks create their own runtime databases:

```sh
for case in provider summary llm-cli; do
  uv run firm-sandbox --config "$agy_acceptance/$case/sandbox.toml" init
done
uv run tools/acceptance/provider.py --root "$agy_acceptance/provider" \
  --evidence "$agy_acceptance/provider-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy
uv run tools/acceptance/provider.py --root "$agy_acceptance/provider" \
  --evidence "$agy_acceptance/provider-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy --resume
uv run tools/acceptance/summary.py --root "$agy_acceptance/summary" \
  --evidence "$agy_acceptance/summary-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy
uv run tools/acceptance/llm_cli.py --root "$agy_acceptance/llm-cli" \
  --evidence "$agy_acceptance/llm-cli-evidence" --provider agy \
  --provider-config "$agy_acceptance/firm.toml" --profile agy --timeout 120
```

Resume requires the first provider pass, its VM disk/evidence database and host-only
`recall-nonce.txt`. The mandate finishes without handoff (which clears the session)
and requests graceful shutdown after drain. Resume checks retained runtime/native
identity, returned init/result identity and exact recall of a nonce supplied only
in the first turn, without fresh routing.

Acceptance requires server-side peek receipts in `server-tools.json`, native Firm
MCP events and the workspace artifact; admission inbox reads do not count. Evidence
checks the actual `agy-auth` projection before sandbox launch and saves allowlisted
`init.model` and cumulative usage, excluding prose, arguments, native session IDs
and credentials. Quota/context are `unavailable`. Summary/chooser checks fail on
any native tool step, including implicit `manage_task`.

To validate eager MCP discovery independently of model tool use, run the provider
commands with `--catalog-only` for both the fresh and resumed activation, using a
separate disposable fixture. This mode asks for no tools, requires a successful
authenticated `tools/list` receipt for each activation in `server-catalog.json`,
and rejects any reported tool step. It skips the workspace artifact and peek
requirements. The ordinary provider check also records catalog receipts.

See [model/effort mapping](../../docs/runtime.md#experimental-agy-configuration)
for additional fresh cases. Revoked auth/MCP, VM
restart, two simultaneous real roles and other adversarial cases need separate live
evidence. Keep generated state outside Git.
