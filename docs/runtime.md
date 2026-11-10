# Runtime configuration and owner operations

> **Quick Reference — Operating a Firm**
> * **Configuration (`firm.toml` & `sandbox.toml`):** Put `mandate.md`, `firm.toml` (provider profiles, accounts, and optional `[staffing]` / `[github]` / `[google_cloud]`), and `sandbox.toml` (VM sizing and `[network]` policy) in the firm's directory.
> * **Dedicated Credentials (`~/.config/firm-runtime/credentials/`):** Use a dedicated login per account (`firm start` wizard or `firm save-credentials`) so OAuth refresh-token rotation never signs out your everyday host CLI.
> * **Console & CLI Parity:** Everything in the local Owner Console (`http://127.0.0.1:8766/`) has an equivalent JSON CLI command (`firm status`, `firm analyze`, `firm send`, `firm requests`, `firm network`, `firm stop`).
> * **CLI invocation:** Once installed via `uv tool install`, run commands directly as `firm <subcommand> ...`. Inside a source checkout of this repository, you can equivalently run `uv run firm ...`, which the examples below show.

Install first — see [installation](installation.md), the [Linux VM guide](linux-vm.md),
and, for an optional Mac station, the [Mac station guide](mac-station.md). The
[runtime design](../design/runtime.md) explains why the contracts below behave as they do.

## Contents

- [Firm configuration](#firm-configuration) ([Hiring levels](#hiring-levels))
- [Provider credentials](#provider-credentials) ([Claude](#claude), [Codex](#codex), [Renewal](#renewal), [Correspondence summaries](#correspondence-summaries), [Tasks](#tasks))
- [GitHub](#github) ([Organization settings](#organization-settings), [Configuration](#configuration), [Registering the App](#registering-the-app), [Repositories on request](#repositories-on-request), [What roles get](#what-roles-get), [Revoking access](#revoking-access))
- [Owner requests](#owner-requests) ([Network hosts](#network-hosts))
- [Generative images](#generative-images)
- [Start and stop](#start-and-stop) ([Execution boundaries](#execution-boundaries))
- [Accounts, quota and recovery](#accounts-quota-and-recovery) ([Moving a role to another provider](#moving-a-role-to-another-provider))
- [Handoff](#handoff)
- [Context pressure](#context-pressure)
- [Console](#console)
- [Analysis](#analysis) ([Sizing a firm from its resource history](#sizing-a-firm-from-its-resource-history))
- [Firm inventory, export and deletion](#firm-inventory-export-and-deletion) ([Moving a stopped firm](#moving-a-stopped-firm))
- [Inspecting a firm without providers](#inspecting-a-firm-without-providers)

## Firm configuration

From the repository root, create `firms/YYYY-MM-DD_name/mandate.md` and two
configuration files. The `firms/` tree holds local runs and is ignored by Git.
Each independent firm gets a new identity; restart its existing directory to resume.
These three inputs must be regular UTF-8 files, at most 1 MiB each.

`firm.toml`:

```toml
max_active = 6
founder_profile = "codex"

[profiles.codex]
provider = "codex"
model = "gpt-6-luna"
effort = "low"
accounts = ["codex-personal"]
context_window = 256000
context_advisory_fraction = 0.60
handoff_reserve = 10000

[accounts.codex-personal]
provider = "codex"
max_active = 6
min_fresh_fraction = 0.1
```

`sandbox.toml`, with an optional `[mac]` table naming an enrolled Mac image (see
the [Mac station guide](mac-station.md)):

```toml
[firm]
id = "2026-09-09_demo"
cpus = 8
memory_gib = 16

[network]
egress = "observe"
allow_hosts = []
```

Network egress defaults to `"observe"`: all public HTTP/HTTPS hosts are allowed,
private/loopback/LAN ranges stay blocked, and per-role traffic is logged. The stricter
opt-in `"enforce"` limits hosts to the baseline, `allow_hosts` and owner grants.
Observe is the default because the baseline already includes GitHub, PyPI, npm and
Docker Hub, which can receive data, so hostname allowlisting offers little protection
while blocking legitimate sites. See [network access](linux-vm.md#network-access-and-cutoff).

Each profile names its provider, model, effort and allowed accounts. Optional
`alternates = ["another_profile"]` join selection for fresh conversations, which go to the
provider, and then the account, holding the fewest conversations per slot; only the profile
and its direct alternates participate. Each account is one subscription login
(see [provider credentials](#provider-credentials)) with a positive `max_active` slot limit.
The firm's own `max_active` must also allow the combined capacity — for two providers with
six account slots each, set the firm to 12 and each account to 6. Slots count role
activations only: isolated hiring-level and summary runs take none, so a full roster never
holds up a role's correspondence context or a hire's assessment. At most four isolated runs
execute at once across the firm, and they still respect account holds.

Editing the configuration applies new execution settings to fresh conversations. A retained
conversation keeps its selected provider, account and captured settings, and runs whatever
its account's slot limit now says; only a hold stops it. Lowering a limit therefore moves new
conversations elsewhere while the ones already there finish. Only context-policy keys are
re-applied to a retained conversation, and only when provider, model and effort still match.

### Hiring levels

`[staffing]` configures the [hiring-level chooser](../design/runtime.md#hiring-levels).
Omitting `[staffing]` enables the chooser automatically. With the Codex configuration
above, Luna at medium effort assesses each hire and selects its effort and model class. The
generated profiles are `staffing-low`, `staffing-medium`, `staffing-high` and
`staffing-chooser`, using the founder's accounts and context policy; alternate routes take
suffixes `-1`, `-2` and so on. If the founder profile lists `alternates`, default staffing
generates matching class and chooser routes for those providers too.

Codex uses Luna (`gpt-6-luna`), Sol (`gpt-6.1-sol`) and Astra (`gpt-6-astra`) for low,
medium and high; Claude uses Sonnet, Opus and Fable (`claude-fable-5-1`). Initialization
saves the expanded settings in the firm's database. The generated Codex high-class
profile uses medium effort. A hire's chosen effort still overrides its profile default.

Codex also accepts `gpt-6.1-terra` in any explicit profile, including a custom staffing
class. It is not part of the default three-class mapping. New Linux VMs install Codex
CLI 0.165.0; Terra requires 0.159.0 or newer. Model availability still depends on the
configured account. See the [OpenAI model guide](https://learn.chatgpt.com/docs/models).

To choose explicitly instead:

```toml
[staffing]
profile = "chooser_codex"

[staffing.classes]
high = "game_high"
medium = "game_medium"
low = "game_low"

[profiles.chooser_codex]
provider = "codex"
model = "gpt-6-luna"
effort = "medium"
accounts = ["codex-personal"]
alternates = ["chooser_claude"]
```

The class profiles and accounts named here must also be configured, and every class can name
its own alternates. With staffing enabled, `founder_profile` is optional and defaults to the
high class at high effort; an explicit founder profile keeps its configured model and effort.
`default_profile` does not assign hires.

For fixed-profile hiring, set `staffing = false` at the top level, **before any TOML tables**.
Only this explicit opt-out disables assessment; hires then take `default_profile`, or
`founder_profile` when no default is given. Note that [correspondence summaries](#correspondence-summaries) (`[summary.<provider>]`) and [tasks](#tasks) (`[task.<provider>]`) have their own default model settings; if your account only exposes a single model, set `model` in those tables as well.

## Provider credentials

Firms run on your personal Claude and Codex subscriptions. Configure one named account per
subscription — several per provider is fine — and give each account its own login to that
subscription, separate from the one your everyday CLI uses. A shared login shares one
refresh token: whichever side renews first can sign the other out, so a firm would break
your CLI session or be broken by it.

Save each account's login with `firm save-credentials <account> --provider claude|codex`.
It validates credentials from stdin (or a paste ended with Ctrl-D) and installs them at
`~/.config/firm-runtime/credentials/<account>.json`, mode `0600` in a mode-`0700` directory,
outside the repository and firm directories. Accounts without `auth_file` use that path;
initialization and `firm configure` record it. When credentials stop working, repeat the
login and save, then clear the account's `auth` hold with `firm clear-hold`. For an
initialized firm, guided `firm start` detects an authentication hold on the selected
account, starts a fresh isolated sign-in, saves to that account's configured `auth_file`,
and clears only that matching hold after the replacement passes local validation.

An explicit absolute `auth_file` (`~` is expanded) overrides the default. Pointing it at a
CLI's own login, such as `~/.codex/auth.json` or an export of Claude Code's Keychain item,
works but shares that login's refresh token as described above.

### Provider differences

Host paths below are relative to `~/.config/firm-runtime/credentials/` unless
`auth_file` overrides them. Model defaults are written as model / effort;
class entries are ordered low, medium, high. Quota and context depend on provider reports.

| Difference | [Codex](#codex) | [Claude](#claude) | [AGY](#experimental-agy-configuration) |
| --- | --- | --- | --- |
| Sign-in | `firm login ACCOUNT --provider codex`: browser login | `firm login ACCOUNT --provider claude`: `setup-token` | `firm login ACCOUNT --provider agy`: native `agy acp --login` |
| Host credentials | `ACCOUNT.json` | `ACCOUNT.json` | `ACCOUNT-agy/home/.gemini/antigravity-cli/antigravity-oauth-token` |
| VM credentials | OAuth access bundle, blank refresh token; or static API key | Setup token as-is; or OAuth access bundle without refresh token | Access-only token file, blank refresh token |
| [Renewal](#renewal) | Host OAuth exchange within 10 min of expiry; API keys static | Repeat setup-token sign-in; OAuth JSON uses host exchange within 10 min | Host `agy models` below 50 min; macOS Keychain sandbox |
| [Model classes](#firm-configuration): low, medium, high | `gpt-6-luna` / high; `gpt-6.1-sol` / high; `gpt-6-astra` / medium | `sonnet` / high; `opus` / high; `claude-fable-5-1` / high | `gemini-3.8-flash` / medium; `gemini-3.8-flash` / high; `gemini-4-argon` / medium |
| Chooser / [summary](#correspondence-summaries) default | `gpt-6-luna` / medium | `sonnet` / medium | `gemini-3.8-flash` / medium |
| [Task](#tasks) default | `gpt-6.1-sol` / medium | `opus` / medium | `gemini-3.8-flash` / high |
| Accepted efforts | low, medium, high, xhigh, max, ultra | low, medium, high, xhigh, max | low, medium, high; Firm `xhigh` → `gemini-4-argon-high` |
| Resume / retained conversation | `exec resume`; private `sessions` tree | `--resume`; private `projects` tree | `--conversation`; private HOME and checked conversation DB |
| [Web search](installation.md#4-default-firewall-web-search-and-network-egress) | Provider-side `web_search` | Provider-side `WebSearch` | Provider-side `search_web` for roles/tasks |
| [Page fetch](linux-vm.md#network-access-and-cutoff) | Shell `curl` through firewall | `WebFetch` through firewall | Shell `curl` through firewall; native page fetch denied |
| [Quota](#accounts-quota-and-recovery) / [context](#context-pressure) | Quota polling/stream; context from rollout | Quota stream (OAuth JSON also polling); context from stream | Unavailable |
| [Provider network hosts](linux-vm.md#network-access-and-cutoff) | `*.openai.com`, `chatgpt.com`, `*.chatgpt.com` | `*.anthropic.com`, `*.claude.com` | `www.googleapis.com`, `daily-cloudcode-pa.googleapis.com` |
| Status | Supported | Supported | Experimental subscription provider |

### Claude

The guided first sign-in uses `claude setup-token` in an isolated configuration and
saves the captured token in Firm's owner-only credential store; see
[installation](installation.md#provider-sign-in-and-model-defaults). A manually
configured Claude Code OAuth JSON is an advanced alternative; do not point Firm at
your everyday CLI's Keychain item unless you intend to share that login and refresh
token. With the setup token, a role's run holds the token, so `firm accounts` and
`firm check` warn about every account saved this way.

Repeat under another account name for each additional subscription, and again when a token
expires. **System → Accounts and capacity** may report subscription readings unavailable for
these tokens; execution does not depend on it.

```toml
[accounts.claude-personal]
provider = "claude"
max_active = 1

[profiles.claude-heavy]
provider = "claude"
model = "opus"
effort = "high"
accounts = ["claude-personal"]
alternates = []
```

`opus` is a Claude Code model alias; the CLI resolves it to the current Opus model. Claude
profiles accept `low`, `medium`, `high`, `xhigh` and `max` effort; Codex profiles accept
those plus `ultra`. Effort is validated per provider, so a value valid for one is not
necessarily valid for the other. The hiring chooser assigns from a narrower set — `low`,
`medium`, `high`, `xhigh`.

At launch, depending on the saved credential's kind, either a one-line token is injected as
`CLAUDE_CODE_OAUTH_TOKEN` or exported credentials JSON is copied into the run's temporary
Claude configuration directory. Only the provider conversation's `projects` transcript tree
persists in the private role home.

References: [Claude Code authentication](https://code.claude.com/docs/en/authentication),
[CLI options](https://code.claude.com/docs/en/cli-reference),
[model configuration](https://code.claude.com/docs/en/model-config).

### Codex

Follow the [guided provider sign-in](installation.md#provider-sign-in-and-model-defaults) to save a dedicated login,
separate from `~/.codex`. Firm renews that saved login from then on.

In the browser, choose the subscription for this account; repeat under another account name
for each additional one. `codex login --with-api-key` in the same steps saves an API key
instead.

```toml
[accounts.codex-personal]
provider = "codex"
max_active = 6
```

### Renewal

Firm owns renewal for each configured OAuth credential file. Before a token is within ten
minutes of expiry, a role's credential request renews it under an account-file lock and
atomically saves the complete token bundle with private permissions. Other Firm hosts using
that file share the lock, and role execution stays concurrent.

Role and summary invocations on the Linux VM receive access credentials only, never the
refresh token. A guest
supervisor updates the private credential file every 30 seconds. Claude uses a JSON credential
file; Codex requires an empty `refresh_token` field in its JSON schema. Explicit raw Claude
tokens and Codex API keys are static credentials with no OAuth renewal.

The `/credentials` endpoint accepts only a live activation token or a started, unexpired
summary token. It resolves the account from the existing binding, never a caller-supplied
account or path, and rechecks authorization after renewal. Responses are not cached.

Expired or revoked refresh credentials create an account authentication hold; temporary
transport failures preserve the last access token. Repair the configured credential file, then
explicitly clear its authentication hold — replacing credentials does not implicitly clear the
incident. The guided sign-in flow clears the selected account's matching authentication hold
only after the replacement is saved and validated. The console displays other blockers and
the required owner action.

Only Firm participants honor the account-file lock. Changes made by another CLI are reread
before persistence or after a failed renewal, but unrelated programs using cloned logins
cannot be made mutually exclusive by Firm's lock. A dedicated login avoids that race. The
coordinator does not silently switch accounts or read arbitrary desktop logins.

### Correspondence summaries

Summaries have their own provider settings and do not reuse the calling role's model. Defaults
are Codex `gpt-6-luna` and Claude `sonnet`, both at medium effort:

```toml
[summary.codex]
model = "gpt-6-luna"
effort = "medium"

[summary.claude]
model = "sonnet"
effort = "medium"
```

A summary uses the authenticated caller's retained provider and account, and takes none of that
account's slots. With all four firm-wide isolated slots already busy, or the account held, it
returns an unavailable result rather than switching providers. Its
foreground process receives no MCP server, activation token, workspace or drive.

An unavailable summary or hiring-level assessment records a short reason in
`summary_runs.reason`, such as `busy`, `timeout`, `output_limit` or `empty_summary`.
`output_limit` means a chooser answer exceeded 16 KiB or the provider stream exceeded 256 KiB.
Conversation summaries are limited to 16 KiB of text. A longer summary is cut to fit and
ends with the line `[THIS SUMMARY WAS CUT OFF at 16 KiB; the rest was not delivered.]`;
the `context` result or `reply` receipt then carries `truncated: true`. Reported output
tokens are recorded as usage. When the
provider turn itself fails, the reason is the provider's failure kind — `process_exit`,
`result_error`, `turn_failed`, `invalid_output`, `activation_gate` or `launcher_setup` — and
`provider_error` only when there is none. For `process_exit`, read the guest log with
`firm-sandbox --config SANDBOX_TOML logs s-<last 32 characters of the run ID>`.

### Tasks

The `task` tool lets a role hand one bounded instruction to a short-lived worker and get one
text answer back: locating code, condensing files, diagnosing a command, without the role
spending its own context on every intermediate read. Tasks have their own model settings,
independent of the role's and of summaries. These are the defaults, which apply even when
the tables are absent:

```toml
[task]
max_concurrent = 2          # per role
timeout_seconds = 600
max_instruction_bytes = 16384
max_answer_bytes = 16384

[task.codex]
model = "gpt-6.1-sol"
effort = "medium"

[task.claude]
model = "opus"
effort = "medium"
```

A task runs on the calling role's own provider and account. While the role waits, its first
running task uses the role's slot; each further concurrent task needs a free slot on that
account or is refused at once (`unavailable: capacity`), and one beyond `max_concurrent`
is refused as `busy`. Account holds apply as they do to roles. The worker runs
as the role's Linux user inside the role's current work period, with the role's files and
shell but no firm tools and no conversation history; a timeout or the end of the work
period kills that task alone and the role keeps running. Anything the worker hands to the
role's Docker or systemd user manager outlives the task and ends with the work period.
`firm analyze` attributes tasks to the calling role, with task tokens counted apart from
the role's own. Changes applied with `firm configure` affect later tasks.
[`tools/acceptance/task.py`](../tools/acceptance/task.py) checks all of this live.

## GitHub

Roles can work in repositories of a GitHub organization through a GitHub App that
`firm init` or `firm configure` registers. The runtime keeps the App's private key on the
host and mints one-hour installation tokens, each limited to the repositories a role is
granted and to contents, pull requests and issues. A firm may also let its roles create
private repositories. Nothing a role holds can delete or reconfigure a repository, change
its visibility or edit workflows. The [runtime design](../design/runtime.md#github-access-github)
explains the boundary.

GitHub is off by default: a firm without `[github]` gives roles no credential helper, and
the repository tools answer `github_not_configured`. A firm that names an organization gets
repositories on request with it: `auto_create` defaults to true, and `auto_create = false`
withholds creation, so a role that wants a repository asks you for it.

### Organization settings

Once per organization, in its settings: require two-factor authentication; under Member
privileges set base permissions to *No permission*, allow repository creation for members
and GitHub Apps, restrict visibility changes to owners, turn off deletion and transfer, and
stop members inviting outside collaborators. None of these binds an organization owner, and
the runtime does not change them.

With the GitHub CLI logged in as an organization owner (add the scope with
`gh auth refresh -h github.com -s admin:org` if fields read `null`), check them:

```sh
gh api /orgs/<organization> --jq '{members_can_create_repositories, default_repository_permission, members_can_change_repo_visibility, members_can_delete_repositories, members_can_invite_outside_collaborators, two_factor_requirement_enabled}'
```

and set all but two-factor authentication, which only the web settings change:

```sh
gh api -X PATCH /orgs/<organization> -F members_can_create_repositories=true -f default_repository_permission=none -F members_can_change_repo_visibility=false -F members_can_delete_repositories=false -F members_can_invite_outside_collaborators=false
```

Whether the deletion setting also binds the App is unverified; if it does, a repository
GitHub wrongly created public cannot be deleted by the runtime, which logs an error.

Roles decide their own branching and review practice. On the Free plan, branch protection
and rulesets do not apply to private repositories anyway, so a role with write access can
push or force-push the default branch; constraining that would need GitHub Team and a
ruleset on the default branch.

### Configuration

```toml
[github]
organization = "example-org"
auto_create = false       # withhold repositories on request; default true
max_repositories = 20     # per firm; default 20

[[github.grants]]         # optional: existing repositories
repo = "example-org/website"
address = "founder"
access = "write"          # or "read"
```

A grant covers the address and every role under it; where grants overlap, the widest
access wins. Repositories must belong to `organization`. `firm init` and `firm configure`
look up the App's installation on the organization and its bot account, and mint a probe
token over every granted repository, so a missing installation or an ungranted repository
fails the command and changes nothing. `firm configure` applies grant changes to running
roles: the next request sees them, and the running host revokes tokens a role is no longer
granted on its next tick. A role whose work period started before `[github]` existed gets
its credential helper in its next work period.

### Registering the App

The App belongs to the host and the organization, not the firm: its key is
`~/.config/firm-runtime/credentials/github-<organization>.pem` and its ID and slug are in
`github-<organization>.json` beside it, both mode `0600`. When `firm init` or
`firm configure` meets `[github]` and no App is saved for the organization, it registers
one first, and needs a terminal:

1. It opens the browser on a local page that sends GitHub the App's manifest: name
   `<organization>-roles`, private, webhook inactive, and permissions Administration,
   Contents, Pull requests and Issues at write. Choose **Create GitHub App**.
2. GitHub returns to the local page; the runtime saves the key, ID and slug and discards
   the client and webhook secrets.
3. It opens the App's installation page. Install it on the organization with
   **All repositories**; the command waits up to ten minutes for the installation, then
   continues.

Later firms of the same organization reuse the App. After a lost key, delete both files,
run `firm configure` to register a new App, and delete the old App on GitHub
(organization settings → Developer settings → GitHub Apps).

### Repositories on request

Roles get repositories two ways. Both answer at once when the firm's own policy allows
it, and otherwise open a [request to you](#owner-requests).

A role's `create_repository` tool, with a name and purpose, creates the private repository
`<firm-id>-<name>` with a README and grants the role write access, when `auto_create = true`
and the firm has fewer than `max_repositories`. The runtime reads the repository back and
deletes it if it is not private. With creation off or the cap reached, the tool answers
`pending` with cause `github_auto_create_off` or `github_limit_reached`, and the request
names the repository the role wanted; granting it creates that repository for the role,
past the setting and the cap, since you decided. A name the firm already used, or that
GitHub refuses, is `github_name_taken`.

A role's `request_access` tool with kind `github_repository` and an
`organization/repository` gives it write access at once to a repository its firm created,
one you granted it, or one `firm.toml` grants its address write on. Any other repository
of the organization that the App's installation can reach opens a request with cause
`github_not_this_firm`; granting it gives that role write. A repository outside the
organization, or one the installation cannot reach, is refused `access_resource_invalid`:
add it under the installation's repository access first. Granted answers are immediate;
git and `gh` work in the repository at once. Operations mails you, for information, when
a repository is created. **System → History → GitHub** lists created repositories and
requested access.

```sh
uv run firm github revoke firms/2026-09-09_demo example-org/2026-09-09_demo-app --address founder/web
```

withdraws the repository from that role and the roles under it, or from every role without
`--address`, and withdraws their open requests for it. The revocation lasts until you lift
it: the running host revokes the tokens on its next tick, and a role asking again opens a
request with cause `github_owner_revoked` rather than retrying. Give the access back with

```sh
uv run firm github grant firms/2026-09-09_demo example-org/2026-09-09_demo-app --address founder/web
```

which clears that address's own revocation and grants it write access; without
`--address` it clears every revocation of the repository and the grants the roles already
had apply again. A wider revocation covering the address, the firm-wide one or one on a
parent address, is never lifted for one role, since that would give write back to every
other role it holds, asked or not: the command refuses and names it, and you lift that
one first with `firm github grant` naming its address, or none for every role. Granting a
`github_owner_revoked` request with `firm requests grant` acts on the asking role's
address by the same rule, and the request stays pending until you lift the wider
revocation, which answers it. `grant` takes any repository of the organization the
installation can reach, so it also hands a role a repository the firm did not create. Both
commands print the addresses they changed and the requests they answered, and the console
lists current revocations. `revoke` acts on repositories the firm created or you granted;
a `firm.toml` grant of the same repository is your own standing decision and keeps
working, so remove it there and run `firm configure` if you want it gone. The runtime
never deletes a repository apart from the public-repository check; `firm delete` and
`firm discard-vms` leave GitHub untouched. The App key holds Administration: anyone with
the key could delete the organization's repositories or change their visibility, so keep
it only on this host.

### What roles get

A firm with `[github]` gives each role's run a credential helper. On the Linux VM, plain
`git` over `https://github.com/...` and `gh` fetch tokens from the runtime's
`/github/token` endpoint with the activation token; nothing is stored in the role's home.
Git asks per repository. `gh` uses the repository named by `-R`/`--repo` or `GH_REPO`, else
the checkout's `origin`, and otherwise read access to all the role's repositories. A
refused repository stops git or `gh` with a message naming it. Rootless Docker containers
and the Mac get no GitHub credentials: roles clone and push in their Linux account.

Pushes, pull requests and comments appear as the App's bot. Commits name the role's
address as author and committer with the bot's noreply email, so GitHub links them to the
bot. They are unsigned.

The endpoint accepts only a running activation's token, never a summary token. Tokens are
reused until ten minutes before expiry and revoked when their activation ends. Every mint
and refusal is recorded as a `github.token_minted` or `github.token_refused` analysis event
with repositories, access and expiry, never the token; repository requests are the
`github.repository_created`, `github.access_granted` and `github.request_refused` events.

### Revoking access

| Action | Effect |
| --- | --- |
| Remove a grant, `firm configure` | Next request refused; cached tokens revoked |
| `firm github revoke` | Requested access stops; asking again is a request to you; cached tokens revoked |
| `firm github grant` | Lifts every revocation; with `--address`, lifts that role's own, grants it write access and answers its request; refuses under a wider one |
| Stop the role or the firm | Its tokens revoked when the activation ends |
| Network cutoff | Guests lose egress; tokens already issued stay valid elsewhere until revoked or expired |
| Remove a repository from the installation | No token works on it |
| Suspend the installation (organization settings → GitHub Apps → Configure) | Every token and new mint fails |
| Rotate the key (App settings → Private keys) | Generate the new key, replace `github-<organization>.pem` with it (mode `0600`), then delete the old one |

The running host also revokes every token it issued when it exits. Tokens live only in its
memory, so after a host crash those already issued stay valid until they expire, within an
hour. Deleting the private key may not invalidate tokens already issued; revoke or suspend
as well.

A Linux VM keeps the tools it was created with, so GitHub access needs a firm created
after GitHub support was added.

## Owner requests

A role that needs something only you can give asks through the tool that would have used
it, and the tool answers `pending` with the request's id and a cause instead of refusing:
a network host the firm has not allowed, a GitHub repository the firm did not create or
you revoked, a repository when creation is off or the cap is reached, or the Mac while it
is not enrolled, a desktop awaits your verified cleanup or no desktop account is ready. A
refusal the role can act on — bad input, a name in use, a service down, a Mac that is
busy or full — stays a refusal. The role is told to take up other work or hand off and
wait, because the answer arrives as mail that wakes it. Each role holds one open request
per resource; asking again returns it, and another role asking for the same thing gets
its own, so one decision answers everyone waiting. What you declined stays declined: a
later request for the same thing is refused `access_denied` with your reason, so decline
with the reason the role should read. The Mac is the exception, since its state changes:
a Mac denial answers the one role that asked, and its next attempt asks afresh.

You learn about a request three ways. **Overview → Needs your decision** shows every
pending request and unresolved account or profile incident with no end time. The Overview
badge and browser tab title count these decisions; pending owner mail has its own Inbox
count. `firm analyze --section inbox` lists `pending_requests` beside your waiting mail,
and `--section readiness` reports the asking role as blocked with `owner_request_pending`
and `actionable_by: owner_or_manager`. While the firm runs, the host also posts a macOS
notification when a request opens and when mail for you arrives, at most one per role
every ten minutes; `[owner] notifications = false` in `firm.toml` turns that off. Nothing
reaches you by email or outside this Mac.

Decide from the CLI:

```sh
uv run firm requests firms/2026-09-09_demo
uv run firm requests firms/2026-09-09_demo grant q3
uv run firm requests firms/2026-09-09_demo deny q3 --reason "Use the registry mirror instead"
```

The listing shows pending requests first, each with its kind, resource, cause and the
role's purpose, then recent decisions; repositories the firm created or granted on request
appear as granted, so `firm requests` is one history of what roles were given. A grant
does what the kind needs — allows the host, creates the repository or grants write on it,
tells the roles the Mac is ready — and mails each answered role with a wake, so it resumes
on the next admission cycle. Every decision, from `firm requests` or a kind's own command,
prints the same shape: the `resource`, the `status` it recorded, and the request ids it
`answered` or `withdrawn`, plus what the kind adds (the granted hosts; the addresses a
repository was cleared or granted for). GitHub requests are also answered by
`firm github grant` and `revoke` ([repositories on request](#repositories-on-request)).
A Mac request is answered by your grant once the Mac is ready
([Mac station guide](mac-station.md#the-mac-lease)); the runtime also answers the ones
waiting on recovery itself, when it notices the recovery of a lease it recorded as failed.
Requests are the `request.opened`, `request.granted`, `request.denied` and
`request.withdrawn` analysis events.

### Network hosts

A firm's VMs reach only named hosts: a built-in baseline, `[network] allow_hosts` in
`sandbox.toml`, and what you grant while the firm runs. How the proxy and filter work,
and `firm network status|on|off`, are in the
[Linux VM guide](linux-vm.md#network-access-and-cutoff). A role that finds a host refused
asks for it with `request_access`, kind `network_host`, naming the host and what it is
for; a host already allowed answers `granted` at once.

```sh
uv run firm network firms/2026-09-09_demo allow api.example.com
uv run firm network firms/2026-09-09_demo revoke api.example.com
```

A grant writes the whole granted set to the running VMs before it is recorded and answers
every pending request the host covers: a `*.example.com` grant answers requests for its
subdomains, and a `*.example.com` denial declines them. `allow` grants a host nobody asked
for yet; `revoke` takes a granted host away at once and withdraws the requests that asked
for it. Grants hold until revoked and are pushed to the sandbox again at every
`firm start`, so a restarted firm keeps them. `firm network status` lists the granted
hosts, and `--section network` shows the allowlist with each host's source, the pending
requests, and the proxy's record of requests by role and host.

## Generative images

Off by default. A firm with `[google_cloud]` gives every role's run one API key for
Google's Gemini API, in `GEMINI_API_KEY`, with the project in `GOOGLE_CLOUD_PROJECT`.

```toml
[google_cloud]
project = "example-project"
api_key_file = "/absolute/path/to/key"   # default: the owner-private directory, below
```

The key is a single line in a file the owner writes and only the owner can read. Without
`api_key_file` the runtime looks in
`~/.config/firm-runtime/credentials/google-cloud-<project>.key`, beside the provider
credentials. The path is what the configuration holds; the key itself never enters
`firm.toml`, the database or the input records under `durable/inputs/`. It is staged into
each run's credentials directory the way a provider token is, so it lives only as long as
the work period.

`firm configure` applies it to a running firm: the configuration is read again for every
work period, so the next one has the key and no restart is needed. A file that cannot be
read, holds nothing, or holds more than one line fails the launch and closes admission
rather than starting a role that silently cannot generate; the error names the file and
never its contents.

What the key may reach is not the runtime's to enforce, and the runtime counts no
spending. Restrict the key on the Google Cloud side — one project, one API — and give that
project a spend cap. A monthly spend cap in AI Studio is the only limit that acts in
minutes; Cloud Billing budgets notify on reported spend and lag by hours, so they are a
backstop rather than a brake.

## Start and stop

The shortest path is to install Firm and run `firm start`; the wizard creates or resumes
a firm and handles its first login. See [installation](installation.md). For a manually
prepared or existing firm, the explicit developer workflow remains:

```sh
uv sync --all-packages
uv run firm check firms/2026-09-09_demo
uv run firm init firms/2026-09-09_demo --firm-id 2026-09-09_demo \
  --mandate firms/2026-09-09_demo/mandate.md --config firms/2026-09-09_demo/firm.toml
uv run firm start firms/2026-09-09_demo --sandbox-config firms/2026-09-09_demo/sandbox.toml
```

`firm check DIRECTORY` is safe to run before `init` or against an existing firm. It reads
`mandate.md`, `firm.toml`, `sandbox.toml`, and an existing database through a query-only SQLite
connection. It checks that the file identities agree, `limactl` is on `PATH`, locally configured
provider credentials are private, readable, and well formed, and loopback ports 8765 and 8766 are
free. `--mcp-port` and `--console-port` check the values intended for `start`. The bind probes are
temporary, so another process can still claim a port after the command exits.
Opening a WAL database may create SQLite's normal `-wal` or `-shm` bookkeeping files; the
query-only connection does not alter logical firm state.

For an initialized firm, credential checks use the saved configuration that `start` uses. A local
`firm.toml` edit is reported as unapplied until it is passed to `firm configure`. Controlled
profiles have no provider credentials, so that capability is reported as `skipped`. The JSON
report uses `good`, `failure`, and `skipped`; any failure makes the command exit nonzero. No VM,
account hold, OAuth renewal, GitHub registration, or provider request is performed. Local
credential validation does not prove that a login or model is accepted remotely.

Use **Results** to read attached files, browse role workspaces and shared deliverables,
preview HTML, Markdown or text, and download files. This does not mount the guest drive
in Finder.
Advanced operators can inspect its connection details with
`firm-sandbox --config PATH data-status` when they need direct SMB access.

`firm start` opens the console and prints its local address, which defaults to
`http://127.0.0.1:8766/`; bookmark it to reconnect. In guided mode it prints the unlock
URL directly, while `firm start DIRECTORY` emits a JSON object containing `console_url`
(`http://127.0.0.1:8766/`) and `unlock_url` (`http://127.0.0.1:8766/unlock/<key>`):

```json
{
  "console_url": "http://127.0.0.1:8766/",
  "unlock_url": "http://127.0.0.1:8766/unlock/<key>"
}
```

Loopback is reachable by every account on the machine, so the console is not open to whoever
can connect to the port: the unlock URL sets an HttpOnly session cookie holding the firm's
console key, and every other route requires it. The key is `durable/console-key`, created on
first start with mode `0600` and reused afterwards. The console also checks Host and Origin,
refusing DNS-rebinding and cross-origin requests. Its owner actions use the same durable
services as the CLI and require explicit same-origin POSTs. Owners can send and reply to
messages, grant or deny requests, stop or resume the firm, close the stopped host, and
browse, preview or download files under firm workspaces and shared deliverables.

By default, `firm start DIRECTORY` starts a detached host, opens the browser, prints its
console details and returns. That host survives terminal closure and remains available
when the firm is stopped. Repeating the command reconnects to the authenticated console
for the same firm instead of creating a second host. Use `firm start DIRECTORY --foreground`
to keep the host in the terminal for diagnosis; Ctrl-C or SIGTERM requests a drain and
waits for confirmed VM shutdown, then closes the foreground host. Repeated Ctrl-C does not
silently escalate. Choose **Firm → Stop after current work** to drain while keeping its
host available. Use `firm stop DIRECTORY --force` when hard termination is required.

The console can finish opening while a new VM is still booting. If guest setup fails,
the console remains available and displays the startup cause. A short package-download
retry may recover transient network errors; if setup remains incomplete, Firm keeps the
firm resumable. Use **Firm → Resume Firm** after the console confirms VM shutdown. If shutdown
is still in progress, wait for confirmation before trying again.

To close the detached host, stop execution and wait until both firm and VM states are
stopped, then choose **Firm → Close Firm**. The host releases its MCP and console
ports. If a port is occupied during a new host start, choose different `--mcp-port` and
`--console-port` values. The `--no-browser` flag starts or reconnects without opening a
browser.

```sh
uv run firm stop firms/2026-09-09_demo          # drain
uv run firm stop firms/2026-09-09_demo --force  # hard stop
uv run firm status firms/2026-09-09_demo
```

These close admission durably; the running execution host performs and confirms VM shutdown,
then remains available for inspection and resume. A drain releases every Mac lease first and
refuses to finish while any station's release is not verified clean; recover a station with
`firm-sandbox --config PATH mac-recover --station N --confirm-cleanup` or use `--force`. If the host has
crashed, relaunch `firm start` with the same configuration to reconnect and finish a pending
stop before admitting work. Keep the MCP port unchanged across host restarts; existing
invocations retain their original endpoint.

### Execution boundaries

What a drain and a hard stop guarantee is in the
[runtime design](../design/runtime.md#soft-drain-and-hard-stop); how runs are bound to
VM generations, reattached after a host restart and settled is in the
[sandbox design](../design/sandbox.md#roles-on-the-linux-vm). A host unable to verify
engine or process state keeps slots reserved and reports the error instead of
declaring a successful stop.

The default execution inactivity timeout is 1800 seconds; `firm start --timeout` overrides it in
seconds. There is no total activation time limit. New provider output or an authenticated role
tool call resets that role's timer; repeated polling of unchanged logs, console reads and
incoming mail do not. The timer starts at launch and refreshes at the guest's start handshake. A
command producing no observable output or runtime calls for the full interval can time out even
while still computing. Deadlines and observed output fingerprints survive host restart. Owner
hard stop terminates active work regardless of activity.

## Accounts, quota and recovery

Before opening execution and before admitting new work, the host validates local provider
settings and credential readability. Invalid credentials create an account hold before any
activation is reserved; healthy accounts stay eligible. These local checks do not prove remote
login validity or model access — structured provider responses remain authoritative.

Unknown or expired quota readings permit admission unless an explicit shared hold exists. A
recent qualified reading below `min_fresh_fraction` puts that account last for fresh work: it
is used only when nothing else can take the work, so a role with one account runs it to empty.
Retained conversations wait on their account's quota hold instead of silently changing provider.
The owner-supplied observation interface accepts an explicit source and validity period; it does
not scrape provider prose. Structured provider errors supply shared holds and supported retry
times. A provider-reported reset on a refused Claude subscription window times the quota hold;
without an unambiguous reset, the hold remains for owner clearance. API billing and request-rate
limits are separate from subscription windows and are not assigned a guessed reset.

An account may set `stop_admitting_at` to a fraction from 0 through 1. A streamed subscription
reading at or above that fraction pauses fresh and retained role activations, tasks, summaries and
staffing on that account until the latest crossed window resets. The reading must be no older than
five minutes when the pause is created; once created, its hold remains through the reported reset
even after the reading ages. Roles already running finish their turns. The default is unset, which
leaves admission unchanged. Claude's `allowed_warning` status sends one Operations notice per
account, window and reset. Codex rollout windows retain the provider's limit ID; crossing a
reported model-specific Codex bucket pauses the whole account.

```toml
[accounts.claude-personal]
provider = "claude"
auth_file = "/path/to/claude-auth.json"
max_active = 6
stop_admitting_at = 0.9
```

```sh
uv run firm accounts firms/2026-09-09_demo
uv run firm quota firms/2026-09-09_demo codex-personal --remaining-fraction 0.25 \
  --valid-for 300 --source "provider account dashboard"
uv run firm hold firms/2026-09-09_demo account codex-personal --kind quota --until 1789000000
uv run firm configure firms/2026-09-09_demo --config firms/2026-09-09_demo/firm.toml
uv run firm clear-hold firms/2026-09-09_demo INCIDENT_ID
```

`quota` requires either `--remaining-fraction` or `--unknown`, plus `--valid-for` and
`--source`; `--observed-at` defaults to now. `hold` takes a scope of `account` or `profile`
and a `--kind` of `auth`, `config`, `quota` or `outage`, with `--until` omitted when the
incident needs owner intervention rather than expiry.

Shared incidents notify the owner and affected managers through Operations mail, which,
like any mail to you, also posts a macOS notification while the host runs. Repeated
observations do not generate duplicate notices. Clearing a specific incident does not clear a
newer incident or reset a role's attempt budget. Manager retry releases only an exact isolated
refusal or exhaustion episode and reports when execution remains blocked on a dependency.

On a named account, a transient provider-setup failure before launch consumes one role
attempt and uses the normal retry delay; the fourth failure holds the role with
`attempts_exhausted`. Setup `auth` and `config` failures create account holds without
spending attempts. Without a named account, any setup failure closes admission until
the owner repairs the configuration.

### Moving a role to another provider

Configure the named account and profile, stop the role, wait for confirmed exit, then route
fresh work:

```sh
uv run firm stop-role firms/2026-09-09_demo EMPLOYEE_ID --request-id stop-1
uv run firm status firms/2026-09-09_demo
uv run firm route firms/2026-09-09_demo EMPLOYEE_ID --profile alternate --fresh --request-id route-1
```

This preserves files, mail and previous failure accounting. It starts fresh context; it does not
transfer a provider conversation. `resume-role` reopens a paused role with its retained
conversation instead. Isolated role holds still require an authorized `retry` against the current
recovery episode, and a stopped firm additionally needs an explicit start.
The held-role notice, sent to the role's manager (the owner, for the founder), names
`firm route --fresh` on the current profile for when retained context is unusable. Routing
discards that conversation and resets the role's effort to the profile default.

## Handoff

`handoff` saves notes for fresh working memory. The first successful handoff in a period
wins; another distinct call is refused, and an identical retry must include the same
`clean_up` value. A completed period's `clean_up` requests best-effort disposal shortly
after it ends, while the firm is running (or at the role's next launch, if it is woken
first). The dispatcher attempts one idle role per tick, shared with retirement and
expired temporary appointments. Containers (running ones are stopped first), dangling
images, ordinary build cache and scratch are removed; tagged images and volumes stay.
Returned receipts, including partial cleanup, are logged and counted as done for this
host process; exceptions retry after fifteen minutes without preventing the next-launch
fallback. That fallback records `cleanup` in `record.json`. Failed or interrupted
periods do not apply their handoff. Leave cleanup unset when the next period still needs
its state. Design: [Handoff and the next work
period](../design/runtime.md#handoff-and-the-next-work-period).

## Context pressure

Each profile's `context_window`, `context_advisory_fraction` and `handoff_reserve` set its soft
allowance, advisory and handoff gate; none of them changes native model capacity. What each
does, its default and how usage is measured are in the
[runtime design](../design/runtime.md#context-policy); what roles see about themselves and
their reports in the inbox is in [messaging](../design/messaging.md#presentation). Owner
diagnostics are in **Team** role details and **System → History → Model sessions**.

## Console

The console has five destinations, in a left navigation on desktop and a bottom bar on
narrow screens. The browser's Back button returns to the previous destination or file.

| Destination | Contents |
| --- | --- |
| Overview | The founder's mandate, current status, Needs your decision, Latest results, From the firm, Team now, and Ask or instruct |
| Inbox | Owner mail by conversation, with New, Everything and Done views, replies and Mark done; an option includes mail between roles |
| Results | Files attached to messages sent to you, a reader and follow-up composer, and Browse folders for shared deliverables and role workspaces |
| Team | The role tree and search, each role's mandate and status, and details for model, effort, tokens, context and holds |
| System | Health and observation ages, Web access, Accounts and capacity, Diagnostics and History |

The status headline describes current work, not progress toward the mandate:

| Status Headline | Meaning |
| :--- | :--- |
| **Working** | At least one role work period is admitted or running. |
| **Starting work** | A role is ready, queued for firm capacity, or due a reminder. |
| **Waiting on you** | No work is running/starting and a pending owner decision blocks a role. |
| **Held** | No work is running/starting and an indefinite hold needs owner or manager action. |
| **Waiting until …** | Roles are waiting on a future scheduled reminder or timed hold reset. |
| **Idle, nothing scheduled** | No role has work scheduled (does not automatically mean the mandate is finished; use **Instruct the founder** to send follow-up work). |
| **Not working / Status unknown** | Unrecognized blocker or missing readiness report. |

A decision is a pending [owner request](#owner-requests) of any kind or an unresolved
account or profile incident with no end time. This includes authentication and configuration
holds and indefinite quota or outage holds. Timed holds and pending mail do not count.
The Overview badge and browser tab title count decisions; Inbox separately counts pending
mail to you. A request explains who asked, their stated purpose, why it is waiting and
what allowing or declining does. Declining requires a reason.

Incident cards give recovery steps. For a failed login, sign in again and save the
credentials with the displayed `firm save-credentials` command, or replace the configured
credential file when the account uses a custom path. Credentials never pass through the
browser. For a configuration hold, fix the named entry; for an indefinite quota or outage
hold, resume when ready. Clearing reports **Hold cleared**, then **Work started** once a
work period is admitted on that account; it does not claim sign-in succeeded.

Reading mail changes nothing. **Mark done** sets aside that pending delivery to you; it
does not complete the role's work or decide a request. Sending a reply also marks the
replied-to message done. **Ask or instruct** sends a new message to the selected role.
Failed sends retain the draft and check recorded operations before retrying. Runtime
notices have no Reply action: notices about the same incident are grouped together, and
request notices appear with the decision they refer to. Their status follows the current
incident or request record; the message text stays unchanged.

Roles send deliverables with `send` or `reply` attachments, using absolute paths under
their drive mount. Attached files appear in **Latest results**, **Results** and Inbox's
**Read** and **Download** links. The reader previews HTML, Markdown and text up to 2 MiB
in an isolated frame; other types can be downloaded. HTML can run its own scripts, with
no console access, cookies, local storage or network; Markdown and text run no scripts.
**About this file** shows its folder and delivering messages. **Write a follow-up** replies
in the delivering conversation with the file attached. Attachments refer to the current
file, not a saved version. Browse folders remains available below the reader, but reading
guest files requires the Linux VM to be running and never starts it automatically.

The **Firm** menu is on every page. **Stop after current work** closes admission, lets
current work finish and shuts down the VMs. **Force stop now** interrupts work.
**Resume Firm** starts the VMs and accepts work again after confirmed shutdown; idle roles
still need a message or scheduled wake. **Close Firm** closes the stopped local host and
releases its ports; reopen it with `firm start DIRECTORY`. Each action explains its effect
and asks for confirmation. Closing the browser does not stop the firm.

Live views refresh every three seconds. The header shows when the console last updated;
a reading that is stale or failed to refresh shows its own age. If the firm cannot be reached, the console keeps the last observations, labels them stale and
disables actions that change the firm until the connection returns.

**System → Diagnostics** holds the [analysis views](#analysis). **System → History** is
collapsed by default and holds decisions, incidents, stops and drains, failed work periods,
GitHub records and model sessions. Historical failures do not imply a current failure.
Session details keep last reported and peak context separate from cumulative token usage;
missing measurements say unavailable.

**System → Accounts and capacity** shows slots, holds and subscription allowance. OAuth
allowance readings are collected every five minutes; cards show window usage, reset times
and observation age, with failed refreshes marked stale. Token usage is not an account
quota reading. This collector is display-only; admission uses recorded provider-stream
readings, and the owner-entered `quota` command remains separate.

## Analysis

Open **System → Diagnostics** for execution timelines (each work period with what woke it and
what it called), usage by role, model, workload, session and tool, conversations, delivery delays,
readiness evidence, Mac leases (leases overlapping the
window with their durations, outcomes and stations, the roles turned away when no
station could be granted and at least one was held, and what each desktop operation
cost: calls, median and longest time, and refusals) and **Resources**: what each machine
and each work period used. Choose the latest 24 hours, all time, or a custom window;
filter by stable employee ID. Load
additional rows on demand. Live console views refresh automatically; **Apply / refresh**
reloads the selected historical view. The timeline also has a table with original timestamps
and observation details.

Read the same evidence as JSON without starting a host, VM or provider:

```sh
uv run firm analyze /path/to/firm --json
uv run firm analyze /path/to/firm --from 2026-09-10T00:00:00Z \
  --until 2026-09-11T00:00:00Z --employee-id founder --section usage --json
uv run firm analyze /path/to/firm --section timeline --cursor 'RETURNED_CURSOR' --json
uv run firm analyze /path/to/firm --section inbox --json
```

The default output includes the overview and initial pages of every section. Pages default to 100
rows, maximum 500 (`--limit`). Continue with the returned `next_cursor`, the same section and the
same employee filter; cursors retain the window and initial source-row cutoffs. Each page takes a
new short read transaction, so outcomes and counters may update between pages. Totals cover the
entire selection, not just the page. Immutable IDs order pages; usage and traffic sorting in the
console applies to loaded rows. CLI timestamps require a timezone; HTTP uses finite UTC epoch
seconds.

The same read-only reader serves authenticated GET routes at
`/api/analysis/{overview,timeline,usage,conversations,inbox,readiness,mac,resources,network}`,
`/api/analysis/conversations/{conversation_id}` and `/api/analysis/roles/{employee_id}/readiness`.
It uses
SQLite `mode=ro` and `query_only`, never initializes schema, and excludes credentials and
invocation contexts. A missing database is an error.

`firm analyze` is the diagnostic surface for "what has the firm done" and "what is waiting"
questions, including for an assistant answering them on your behalf. Querying
`durable/firm.sqlite3` by hand is not the way: it silently drops the `coverage` block and the
`role_labels` identities, which belong in any answer drawn from this evidence.

| `--section` | Scope | What It Reports |
| :--- | :--- | :--- |
| **`overview`** | Window + current | High-level firm status, active/waiting counts, and machine disk/health flags. |
| **`inbox`** | Current (`sampled_at`) | All waiting mail to `owner` (oldest first, full body) plus `pending_requests` (`needs_owner_count`). |
| **`readiness`** | Current (`sampled_at`) | Per-role scheduling readiness, blockers, retained session bindings, and unreleased reminders. |
| **`timeline`** | Window | Every work period in the window with `wake_reasons`, `tool_calls` (by name), `operations`, and `failure` classification. |
| **`usage`** | Admitted in window | Token usage by role, model, workload, session, and tool, plus provider-reported `resolved_models`. |
| **`conversations`** | Window (or `--conversation-id`) | Message traffic edges, delivery counts, and claim delays (`while_running`, `before_work_period`, `after_notice`). |
| **`mac`** | Window | Mac station leases, durations, release check outcomes, busy refusals, and desktop tool timings. |
| **`resources`** | Window | 1-minute host/Linux/Mac CPU, memory, and disk samples, plus per-run cgroup peak memory and CPU usage. |
| **`network`** | Current / last run | Egress mode (`enforce` vs `observe`), active allowlist by source, pending host requests, and per-role proxy byte/request tallies. |

Interpret the evidence by its scope:

- Usage selects executions **admitted in the window** and reports their observed counters through
  `sampled_at`; it is not tokens consumed during the window. Session counters are lifetime totals,
  separate from activation totals. Cached input is included in input, and reasoning output
  overlaps output. Incomplete counters show a known subtotal and measured and eligible counts.
  Summary usage uses measured output tokens, with the captured summary model and caller.
- Usage model labels remain `[provider, configured model]`. `resolved_models` lists the
  distinct provider-reported models for each model, role, workload and session row, including
  summaries in the aggregate rows. Session rows also identify the captured `model` alias and
  `execution_profile`. These identities cover executions admitted in the selected window,
  just like the token totals. Each execution records its last non-empty reported model:
  Claude's assistant `message.model` or result `model`, and Codex's rollout
  `turn_context.payload.model` forwarded by the launcher. This is provider-reported identity,
  not per-request token attribution or a promise about the next run. No alias is expanded or
  guessed. `null` in `resolved_models` means unknown; `missing_resolved_model_identity` counts
  executions without a report, including runs without usage. `missing_model_identity` still
  counts missing **configured** model identity. Old unmeasured runs remain unknown; analysis
  does not backfill logs. The console renders unknown identities as “Unknown”.
  This recording adds columns to `activation_usage` and `summary_runs`; there is no migration
  for databases created by older code. Follow the schema-change procedure in
  [development.md](development.md) before updating a firm.
- Timelines include overlapping executions. Open or interrupted intervals have uncertain ends, and
  observed concurrency may overstate overlap across host gaps. Clock anomalies produce unknown
  durations. Process exits do not prove task completion. Each work period includes its
  recorded `failure` reason alongside `failure_category`, so an `unknown` category can
  still distinguish an inactivity timeout from a provider process exit. The dispatcher
  records bounded provider classifications here, not raw provider error text.
- Each work period also carries `wake_reasons`, `tool_calls` and `operations`. `wake_reasons` is
  what admission saw, not the role's own reason for what it then did: a role woken by mail may
  spend the period on something else. `tool_calls` counts what the provider's log shows the period
  called, by name, with no arguments, results or file paths; it is the fact that separates a period
  that built something from one re-reading its own documents, but names alone cannot tell a product
  build from a doc build. An empty tally means the period was not measured — a lost log, or a
  provider whose shape changed — as much as it means a period that called nothing, and the
  `usage` section's `tool` breakdown reports how many periods were tallied out of how many were
  eligible. Names are provider-shaped: Claude reports `Bash` and `Edit`, Codex reports
  `command_execution` and `file_change`, and both report runtime tools as `mcp__firm__…`, which is
  how the `by_group` split separates runtime work from building in the guest. `operations` is
  exact rather than parsed, but covers only mutating runtime operations that carry a request ID:
  peeks, discovery, desktop calls and everything inside the guest are not in it.
- Messages count once; each recipient delivery counts separately. A role filter selects messages
  authored or received by that employee; delivery totals and claim delays include all deliveries
  of those messages, and traffic edges are limited to that employee. Claim delay excludes
  dismissals and undeliverable mail. `claims_by_arrival` splits those claims by whether the
  claiming work period had already started when the message arrived: `while_running` is mail a
  role noticed mid-period, `before_work_period` is mail that waited for a wake. Those measure from
  arrival; `after_notice` measures from the
  [waiting-mail notice](../design/messaging.md#presentation) that first named the message, and
  `claimed_without_notice` counts mail claimed with no such notice — seen at wake or through the
  role's own peek. A peek that claimed nothing leaves no trace beyond its count in the period's
  `tool_calls`. `undeliverable_reasons` groups the window's undeliverable deliveries by why they
  failed. Current older backlog shows pending deliveries to the selected role. A conversation
  drill-down (`--conversation-id`) reads stored messages across the conversation's lifetime
  without a summary call.
- The inbox section is your own waiting mail: every delivery to you still pending now, oldest
  first, with author, age and full body. It is sampled at `sampled_at`, so the window does not
  bound it and claimed or dismissed mail is not in it; a role filter selects mail from that
  author. Reading it changes no disposition. `pending_requests` lists what roles are waiting
  on you for, each with its kind, resource and cause, and `needs_owner_count` is mail plus
  requests.
- The network section is current, like the inbox: the mode, whether egress is on, the
  allowlist with each host's source, the granted hosts and pending requests, and the proxy's
  record by role and host. It reads the firm's sandbox, so an export has none and a stopped
  firm's record is historical; `coverage.limits` says when the proxy was not live or its
  bounded table overflowed.
- Readiness describes current persisted evidence, including unreleased reminders, retained
  bindings, primary and alternate constraints and summary account occupancy. "No observed
  blocker" does not promise admission, select an account or verify host liveness.
- Keep inbox residence age separate from time waiting for compute: old mail does not show that a
  role lacked capacity, and activation or claim counts alone do not establish useful work
  completed.
- Resources report the host Mac, the Linux VM and the firm's Mac VM from samples taken once a minute
  while it runs, and each work period from the counters its slice held when it stopped. The
  machine rows are deltas of cumulative counters, so nothing is lost between samples except
  short dips in available memory; a reboot resets them and is not counted across. A run's peak
  memory includes page cache, so it bounds demand from above, and the concurrent bound assumes
  every measured peak at once. Host footprints never shrink: a VM keeps the memory it has
  touched, whatever its guest has freed. Runs before this history existed, and runs whose slice
  was already gone, have no reading rather than a zero.
  Isolated summaries appear as `summary_run` resource rows without an employee and
  are omitted when filtering by employee.

Linux roles check out and build in their supplied `scratch` directory and keep shared
deliverables on the drive. Settlement stops background processes and containers, waits
for the slice stop and the kernel's recursive empty signal (or an absent slice), and a
failed slice inspection leaves execution unresolved; it removes nothing. Scratch and
Docker state go only when the role deletes them, when the owner runs `firm-sandbox
--config PATH dispose ROLE`, before the dispatcher retires an idle fired role, when a
temporary employee's last allocated work period finishes (completed, or failed by a
refusal or an unclassified failure, not by a runtime stop — inactivity timeout, owner stop
or host restart — or a transient, quota, auth or configuration failure), or shortly after
a completed [handoff](#handoff) with `clean_up` (or at the role's next launch, if it is
woken first). A temporary employee whose last period finished stays employed, with
identity, drive files and working memory unchanged; its manager can still renew or promote
it, and a renewed role
starts its next period with an empty scratch. `firm-sandbox --config PATH storage
[ROLE]` estimates usage with `du` on demand; its readings are not part of run receipts
or analysis history.

The dispatcher prunes finished guest run journals after seven days, checked hourly and at startup,
only once the host has recorded completion and committed tool-call tallies, available
provider usage and slice usage from a complete log snapshot.
Failed evidence collection keeps the journal. Logs missed at completion backfill
the old activation without changing live session or subscription state; restarting
the host also does not refresh old subscription windows. Host history and
captured prompts stay; exact provider stdout then disappears from `firm-sandbox
logs`. Read or save it within the week when investigating a provider exit. See the
[Linux guide](linux-vm.md#roles-on-the-linux-vm) for settlement, storage measurement and disposal.

How analysis facts are recorded, and what they cannot show, is in the
[runtime design](../design/runtime.md#analysis).

### Sizing a firm from its resource history

The CPU and memory sizes in `sandbox.toml` are judgement calls until the history says
otherwise. Edit them and the stopped guest is resized on its next boot, keeping its disk: for the Linux VM that is
the firm's next start, and for the Mac, `firm-sandbox --config PATH mac-restart` cycles it
while the roles on Linux keep working, refusing while any station's lease is held. These
thresholds
are starting points, to confirm after a week of normal use in which observed concurrency
actually reached `max_active`:

- **Linux memory:** shrink only with no OOM kills, full memory stall ≈ 0 and memory stall under
  about a second an hour; the new size should be at least
  (total − lowest available) × 1.25 + 2 GiB, cross-checked against the concurrent peak bound.
  Any OOM kill, or more than about ten seconds of full stall an hour, argues for keeping or
  growing it.
- **Linux CPU:** 6 vCPU if CPU p95 stays under 0.5 of capacity and CPU stall under about 5% in
  the busiest hour; keep or grow above about 20%.
- **Disk:** choose `[firm] disk_gib` for a new Linux VM (default 60 GiB); editing it
  never resizes an existing firm's disk. Act above 70% used: read the machine-wide view in
  `firm analyze PATH --section overview` or `--section resources` or **System → Health**,
  attribute usage with `firm-sandbox --config PATH storage [ROLE]`, then ask roles to
  remove work or caches they no longer need, dispose of an idle role's state with
  `firm-sandbox --config PATH dispose ROLE` (`firm stop-role` first if it might be running
  or woken), or move selected work to a larger new firm. At 85% used every role gets a
  low-disk notice and Operations mails you with those commands; a recovery mail follows
  below 80%, readings in between change nothing, and sampling continues while the VMs run
  even with admissions stopped (details in the
  [Linux guide](linux-vm.md#roles-on-the-linux-vm)).
  The runtime itself reports `low_disk` at 85% used, or for the host (the owner's own large
  disk) below 100 GB free: per machine in the overview and resources
  sections and in the console. It is a fact about the firm, not a reason on a role, since a
  low disk blocks nobody until it is full. A window also reports its own last sample, so a
  firm that ended low still reads as low afterwards. Current disk views include only
  `host`, `linux` and `mac` (one VM for all stations), when sampled; retired machine
  names remain available in historical resource windows.
- **Mac guest:** 24 GiB is the default for a Mac carrying several stations. Size memory
  and CPUs from resource pressure during the intended workload: reduce memory only if
  pressure stays low and swapouts barely move during leases. Leave host headroom for
  both VM allocations and the owner's applications; a 64 GiB host with 24 GiB assigned
  to the Mac and 16 GiB to Linux has 24 GiB left before other host overhead.

## Firm inventory, export and deletion

```sh
uv run firm list firms
uv run firm export firms/2026-09-09_demo
uv run firm discard-vms firms/2026-09-09_demo --confirm 2026-09-09_demo
uv run firm delete firms/2026-09-09_demo --confirm 2026-09-09_demo
```

`firm list` reports state and durable size for each immediate subdirectory with a
`durable/firm.json` identity file.

`firm export` saves what is worth keeping from a run in `firms/<id>/export/`: the runtime
database as `firm.sqlite3`, `events.jsonl`, the captured `inputs/`, the Mac lease records and
command logs under `mac/stations/<n>/leases/`, the shared trees under `drive/`, and `manifest.json` with
every file's size and SHA-256. `--homes` adds each role's private `work/` and provider
sessions under `homes/<role>/`; leave it off when the export may be shared, because they can
hold sensitive text. A second argument writes the export to another directory instead; Firm
refuses to replace a directory that is not an export. A stopped firm boots only its Linux VM
for the copy and stops it again; a firm running under `firm start` is copied from its running
VM. The detached host remains available while a firm is stopped; close it from the console
when you want to release its ports. Each export is
verified and then replaces the previous one whole. Links that would point outside the export
and special files are skipped and listed as `refused` in the manifest.

`firm discard-vms` keeps a finished run without its VMs. It requires what deletion requires,
exports afresh, and only after the export verifies discards both VMs and every guest disk. Its
export includes role homes by default, because they are lost with the disks; `--no-homes`
leaves them out. The firm directory keeps its runtime database, host records and `export/`;
`status` and `list` report sandbox state `discarded`, and the firm can no longer start, be
replaced or be exported again. If it is interrupted, run it again: it verifies the existing
export and finishes. A rerun that asks for homes the kept export lacks is refused, since
nothing can be exported any more; finish it with `--no-homes`.

`firm analyze firms/<id>/export` reads an export's database like a firm's. The console is not
served from an export.

Deletion requires a stopped database, no admitted or running activations, its VMs stopped or
discarded, a free execution host lock and the typed firm ID, and holds the firm's lifecycle
lock while it checks the VMs and removes the directory. It removes the complete firm
directory, including retained guest disks and the export; use `discard-vms` to keep the run.

Stopping retains the guest disks. Before backup or relocation, stop the runtime and VMs and copy
the complete firm directory with sparse-file support. Run only one copy of a firm identity. For a
clean replacement, move selected work through the shared trees into a new firm; do not replace
guest disks underneath an existing runtime database.

### Moving a stopped firm

Keep the complete firm directory together, including its runtime database and
`durable/firm.json`. The execution host records that stable sandbox identity and firm ID. It
accepts a changed directory path only after the old path is gone, the runtime is stopped with no
unresolved activations, and the host confirms the firm VMs are stopped. A copied firm cannot run alongside
its original, and replacing the sandbox identity — even at the same path — is rejected. After a
verified relocation the host updates the saved path; identity and runtime history remain intact.

## Inspecting a firm without providers

The offline simulation dispatcher exercises the durable contracts without providers or a VM;
it is for development and tests, not a sandbox. To inspect a persistent
stopped firm, create a mandate file and this minimal configuration — `staffing = false` is
required, since staffing defaults otherwise demand a provider-backed founder profile:

```toml
staffing = false
max_active = 2
founder_profile = "controlled"

[profiles.controlled]
```

```sh
uv run firm init firms/example --firm-id example \
  --mandate firms/example-mandate.md --config firms/example-firm.toml
uv run firm send firms/example founder --body "Prepare an initial plan." \
  --request-id first-owner-message
uv run firm status firms/example
```

Initialization is repeatable with identical inputs and creates only the founder, in a stopped firm.
Owner mail queues work without starting execution. `firm_id` and role slugs use lowercase letters,
digits, underscores and hyphens.

## Experimental AGY configuration

`agy` is an experimental subscription provider; see [host setup and login](installation.md#experimental-agy-integration).

```toml
staffing = false
founder_profile = "agy"

[profiles.agy]
provider = "agy"
model = "gemini-3.8-flash"
effort = "medium"
accounts = ["agy-personal"]

[accounts.agy-personal]
provider = "agy"
max_active = 1
```

Firm stores model and effort separately. Low/medium classes select
`gemini-3.8-flash`; high selects `gemini-4-argon`, with class default efforts
medium/high/medium respectively. The founder's high/high override selects Argon
high. Chooser/summary defaults are Flash medium; tasks default to Flash high.
Efforts low, medium and high append a suffix to the base model. **Firm xhigh
selects `gemini-4-argon-high` regardless of class**; native xhigh is unsupported.
The command passes only `--model`. Console diagnostics report native `init.model`;
the retained binding separately stores the configured base identity.

Role/task agents declare bounded file/shell tools plus provider-side `search_web`,
using `toolPermission: "always-proceed"` and `agentMode: "accept-edits"` with VM/UID
isolation. Firm denies `read_url(*)` and `execute_url(*)`, plus `mcp(*)` outside
roles. Search needs no extra permission namespace or page-fetch allowance.
Every agent sets `inheritCustomizations: false`; workspace instructions can reach
role/task runs. Tool-free runs declare `tools: []` and explicitly deny native
names/namespaces, including search, `manage_task` and `mcp(*)`, under strict
permissions. Empty declarations alone do not enforce isolation: implicit native
tools, delegation, browser and scheduler exclusion are unproven, and an implicit
`manage_task` step can occur. Summaries have no shares or role authority.

Only roles get eager authenticated Firm MCP, with an activation bearer and the
configured task timeout. Tasks and summaries get no MCP config. A tool ERROR
does not fail an otherwise successful activation: the protocol cannot distinguish
ordinary tool errors from transport/auth failures. Missing MCP without a structured
failure cannot be reliably inferred from prose.

Each Firm session retains a private native HOME at `.firm/agy-sessions/SESSION_UUID`
for conversations, summaries and cache. Settings/agents are rewritten per activation;
access credentials and role MCP headers link to ephemeral credentials, with stale
links removed before preparation and links removed at exit. Resume requires an
existing regular, non-symlink `conversations/ID.db`; a returned ID mismatch kills
the child and fails. Only a valid SUCCESS with exit zero completes a turn.

AGY reads credentials once. Firm requests native renewal below 50 minutes of
token lifetime and bounds print timeout by the staged lifetime minus 300 seconds
(with a one-second guard), independently of the activity-extended inactivity
deadline. Long turns yield after roughly 45–55 minutes and resume the same
conversation with fresh credentials, without a dependency hold or spending the
failure budget. The launcher forwards signals, kills process-group children and
reaps its child; sandbox cgroup cleanup provides the final execution boundary.
Terminal ERROR and transient pre-launch failures use the ordinary four-attempt
role budget/backoff and role-scoped hold; local resume/settings failures do not
create shared account/profile holds. See [expiry recovery and clock requirements](installation.md#experimental-agy-integration).

Usage is cumulative per conversation: input, output, cache-read and thinking map
to session counters, with thinking included in output. Activation deltas, context
occupancy/window, compactions and quota are unknown. Fresh summary/chooser/task
sessions also report session totals as run totals, without double counting.
Summary/chooser `output_tokens` use the raw reported total; reasoning is also
reported separately. Those turns use a 30-second timeout, configured effort,
brevity instructions, a 256 KiB stream cap and 16 KiB answer-text cap. Overlong
summaries are cut with a visible marker; overlong chooser answers are rejected.
These byte caps do not cap generation cost.
