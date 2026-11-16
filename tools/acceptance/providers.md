# Provider acceptance

Use fresh disposable firms and private evidence directories. These checks consume
provider quota. Run commands from the repository root.

Run `task.py` once per provider with a fresh, unbound **Linux-only** firm and a new
evidence directory each time. The selected profile must match `--provider`; its
accounts need `max_active >= 2`. It uses `[task.PROVIDER]` model/effort from the
provider config, or runtime task defaults, and admits six real workers. `--timeout`
sets each task's deadline, including provider startup (minimum 30 seconds). It
prints PASS/FAIL for all eight checks and exits nonzero on failure, including an
unreached check or failed cleanup. Refusing the requested fixture or returning
early fails the check; no placeholder replaces a task provider.

`result.json` records the random file answer, worker-output checks, overlapping
barriers, cgroup/PID observations and the parent's SIGTERM receipt. Analysis
snapshots retain `coverage` and `role_labels`; `provider-output.json` keeps output
hashes/counts and isolation flags, never raw provider text, tool arguments or
credentials. Busy refusal is replayed after capacity frees; it creates no task
row. Parent token usage remains unknown in analysis because the placeholder emits
no provider telemetry. Isolation is based on the worker's own answer, instruction
sentinels and visible provider tool events, not inspection of hidden prompts.
After the answer worker exits, before its home is removed, `auth_files` verifies
Codex `auth.json` or Claude `.credentials.json` is still a symlink resolving into
`/run`. Claude token-only auth instead requires that no home auth file exists.
AGY instead checks that its native auth symlink was removed by launcher cleanup
and that the remaining ephemeral `/run` credential is private, kind `agy-access`,
access-only, with no refresh token value, email, or unknown fields. This evidence
contains only the auth-file path, fixed labels, and booleans, never credentials.
This check does not cover Docker lifetime, tmux integration, host-loss watchdogs,
restart recovery, adversarial same-UID escapes or independent cancellation while
another task continues. Resource snapshots preserve the inclusive-task semantics;
they do not measure a separate task CPU/memory total.


## Experimental AGY integrated acceptance

This recipe uses the registered adapter, consumes subscription quota
and starts/stops disposable Linux VMs. Offline parser/evidence tests do not establish
integrated acceptance.

Prepare fresh directories, then save the following TOML as
`$agy_acceptance/firm.toml`:

```sh
umask 077
agy_acceptance=$(mktemp -d /tmp/firm-agy-acceptance-XXXXXX)
mkdir "$agy_acceptance/provider" "$agy_acceptance/summary" "$agy_acceptance/task"
```

```toml
max_active = 2
staffing = false
founder_profile = "agy"

[profiles.agy]
provider = "agy"
model = "gemini-4-hydrogen"
effort = "medium"
accounts = ["agy-dedicated"]

[accounts.agy-dedicated]
provider = "agy"
max_active = 2
auth_file = "/PRIVATE/AGY-PROFILE/home/.gemini/antigravity-cli/antigravity-oauth-token"

[summary.agy]
model = "gemini-4-hydrogen"
effort = "medium"

[task.agy]
model = "gemini-4-hydrogen"
effort = "high"
```

Create a dedicated profile using the supported login command in your terminal:

```sh
uv run firm login agy-dedicated --provider agy --auth-file /PRIVATE/AGY-PROFILE/home/.gemini/antigravity-cli/antigravity-oauth-token
```

Replace the example absolute path in both the command and TOML. Reuse that
authenticated profile directly; do not copy credentials. See the
[AGY setup requirements](../../docs/installation.md#experimental-agy-integration).
Acceptance never initiates login and rejects ordinary HOME profiles and
`[google_cloud]`.

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
for case in provider summary task; do
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
uv run tools/acceptance/task.py --root "$agy_acceptance/task" \
  --evidence "$agy_acceptance/task-evidence" --provider agy \
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

See [model/effort mapping](../../docs/runtime.md#experimental-agy-configuration)
for additional fresh cases. Revoked auth/MCP, VM
restart, two simultaneous real roles and other adversarial cases need separate live
evidence. Keep generated state outside Git.
