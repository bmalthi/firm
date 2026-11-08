# Installing Firm

Firm ([`bmalthi/firm`](https://github.com/bmalthi/firm)) runs on **Apple Silicon Macs (macOS 13 or newer)**. Each firm provisions one persistent Linux VM (via Lima) where roles run in isolated accounts with rootless Docker; shared Mac desktop stations for Xcode and GUI work are optional.

Firm is distributed as source at `https://github.com/bmalthi/firm` and installed locally by building the `firm-runtime` and `firm-sandbox` wheels with `uv` (nothing is published to PyPI).

## 1. Prerequisites & Hardware Sizing

* **Apple Silicon Mac** running **macOS 13+**
* **Memory & Disk Recommendations:**
  * **Linux-only firm (standard):** **16 GiB+ host RAM** and **30 GiB+ free disk space** recommended (the guided `firm start` starter allocates 4 vCPUs, 8 GiB RAM, and a thin-provisioned 60 GiB disk; omitting `cpus`/`memory_gib` in a manual `sandbox.toml` defaults to 8 vCPUs and 16 GiB RAM).
  * **With optional Mac stations (`[mac]`):** **32–64 GiB+ host RAM** and **100 GiB+ free disk space** recommended (the shared Mac VM allocates 8 vCPUs and 24 GiB RAM from first boot).
* **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (Python package and tool manager)
* **[Lima 2.2.0+](https://lima-vm.io/docs/installation/)** (2.2.0 is the tested baseline)
* **Node.js / npm** (needed if setup must fetch a provider's login CLI via `npx` when `codex` or `claude` is not already installed)

With Homebrew installed, install all prerequisites in one step:

```sh
brew install uv lima node
```

## 2. Install the `firm` CLI

Clone the repository and install both wheels together as a unit:

```sh
git clone https://github.com/bmalthi/firm.git
cd firm
uv build --all-packages
uv tool install --force --python 3.12 \
  --with "$PWD/dist/firm_sandbox-0.1.0-py3-none-any.whl" \
  "$PWD/dist/firm_runtime-0.1.0-py3-none-any.whl"
```

If `firm` is not on your shell's `PATH` after installation, run `uv tool update-shell` and reopen Terminal (or invoke `~/.local/bin/firm` directly). This updates the `PATH` for installed `uv` tools without moving or recreating your firms.

---

## 3. Setup Paths: (A) For Agents vs. (B) For Humans

### Path (A): For Coding Agents (Recommended — Non-Interactive CLI Setup)

> **Suggested Way to Run Firm:** Run your host coding agent on a medium-effort model (e.g. `opus` at `medium` effort, or `gpt-6.1-sol` at `medium` effort) and ask it to start the firm, poll `firm status` / `firm analyze --section inbox` every 5 minutes, summarize progress for you, and use `firm send` to message the Founder on your behalf—while you occasionally open the Owner Console (`http://127.0.0.1:8766/`) to preview deliverables.

Because bare `firm start` requires an interactive TTY (`sys.stdin.isatty()`), a coding agent (Claude Code, Codex, Cursor, etc.) setting up Firm on behalf of a user should use the explicit file-based and JSON-emitting CLI workflow below.

#### Step 1 — Check & install host prerequisites (`uv`, `limactl` >= 2.2.0, `node`)

```sh
brew install uv lima node
limactl --version
```

#### Step 2 — Clone `bmalthi/firm`, build the wheels, and install the `firm` CLI

Firm is installed directly from local wheel builds of [`bmalthi/firm`](https://github.com/bmalthi/firm) (nothing is published to PyPI):

```sh
git clone https://github.com/bmalthi/firm.git ~/firm-src
cd ~/firm-src
uv build --all-packages
uv tool install --force --python 3.12 \
  --with "$PWD/dist/firm_sandbox-0.1.0-py3-none-any.whl" \
  "$PWD/dist/firm_runtime-0.1.0-py3-none-any.whl"
```

*(Ensure `~/.local/bin` is on `PATH`, or invoke `~/.local/bin/firm`.)*

#### Step 3 — Ensure a dedicated provider login exists

Firm stores Codex and Claude credentials in `~/.config/firm-runtime/credentials/<account>.json` (`0600`).
* **Recommended (isolated browser login for Codex or Claude):** Check if `~/.config/firm-runtime/credentials/codex-personal.json` (or `claude-personal.json`) exists. If not, run the isolated sign-in helper, which opens the user's browser in a temporary profile so Firm gets its own refresh token without signing out the user's everyday host CLI:
  ```sh
  # For Codex:
  uv run --directory ~/firm-src python -c 'from firm_runtime.onboarding import login_provider; print(login_provider("codex", "codex-personal"))'

  # For Claude:
  uv run --directory ~/firm-src python -c 'from firm_runtime.onboarding import login_provider; print(login_provider("claude", "claude-personal"))'
  ```
* **AGY (experimental subscription):** Install the native CLI on the host, then create a dedicated login as described in [Experimental AGY integration](#experimental-agy-integration):
  ```sh
  firm login ACCOUNT --provider agy
  ```
* *(Optional shortcut for Codex:* `~/.local/bin/firm save-credentials codex-personal --provider codex < ~/.codex/auth.json` imports the user's existing host login without a browser step, **but** because OAuth refresh tokens rotate on renewal, sharing one login between the host CLI and a running firm can sign the other out. Prefer the isolated login above.)

#### Step 4 — Create the firm directory (`mandate.md`, `firm.toml`, `sandbox.toml`)

From the repository root, create an owner-only directory (`chmod 700`) under `firms/<FIRM_ID>` (where `<FIRM_ID>` matches `[a-z0-9]+([-_][a-z0-9]+)*`, e.g., `2026-10-31_demo-firm`). The `firms/` tree is ignored by Git:

```sh
export FIRM_ID="2026-10-31_demo-firm"
export FIRM_DIR="$PWD/firms/$FIRM_ID"
mkdir -p -m 700 "$FIRM_DIR"
```

Write three files inside `"$FIRM_DIR"`:

1. **`mandate.md`** — The plain-English goal for the Founder (or copy `~/firm-src/examples/miniature-city/mandate.md` for a quick demo; see [recorded runs and mandates](../examples/README.md)).
2. **`firm.toml`** — Execution and staffing settings.
   * **Multi-role firm with automatic model tiering (`[staffing]` enabled by default):**
     ```toml
     max_active = 4
     founder_profile = "codex"          # or "claude"

     [profiles.codex]                   # or [profiles.claude] with provider = "claude", model = "sonnet"
     provider = "codex"
     model = "gpt-6-luna"
     effort = "medium"
     accounts = ["codex-personal"]      # or ["claude-personal"]

     [accounts.codex-personal]
     provider = "codex"
     max_active = 4
     ```
   * **Single-model account override:** Automatic staffing (`gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra` for Codex; `sonnet` / `opus` / `claude-fable-5-1` for Claude), correspondence `[summary]`, and `[task]` workers each have their own default models. If the user's account only has access to a single model (`<MODEL>`), pin all three in `firm.toml`:
     ```toml
     max_active = 2
     staffing = false
     founder_profile = "main"

     [profiles.main]
     provider = "codex"                 # or "claude"
     model = "<MODEL>"
     effort = "medium"
     accounts = ["codex-personal"]

     [accounts.codex-personal]
     provider = "codex"
     max_active = 2

     [summary.codex]                    # or [summary.claude]
     model = "<MODEL>"
     effort = "medium"

     [task.codex]                       # or [task.claude]
     model = "<MODEL>"
     effort = "medium"
     ```
3. **`sandbox.toml`** — Linux VM sizing and firewall policy:
   ```toml
   [firm]
   id = "2026-10-31_demo-firm"
   cpus = 4
   memory_gib = 8
   disk_gib = 60

   [network]
   egress = "enforce"   # "enforce" (default baseline + approved hosts) or "observe" (allow all public web)
   allow_hosts = []     # e.g. ["api.example.com", "*.example.org"]
   ```

#### Step 5 — Validate, initialize, and start the firm (all emit JSON on `stdout`)

```sh
# 1. Verify config, credentials, and local ports (read-only)
~/.local/bin/firm check "$FIRM_DIR"

# 2. Initialize the SQLite database and founder role
~/.local/bin/firm init "$FIRM_DIR" --firm-id "$FIRM_ID" \
  --mandate "$FIRM_DIR/mandate.md" \
  --config "$FIRM_DIR/firm.toml"

# 3. Launch the detached local host + Linux VM (prints JSON with console_url and unlock_url)
~/.local/bin/firm start "$FIRM_DIR" --sandbox-config "$FIRM_DIR/sandbox.toml"
```

When `firm start "$FIRM_DIR"` returns JSON, give the user the `unlock_url` so they can watch the **Owner Console** in their browser (`http://127.0.0.1:8766/`). The first boot provisions the Ubuntu Linux VM in the background, which takes a few minutes before the Founder's first work period starts.

#### Step 6 — Monitor, message, and operate the firm via JSON CLI

```sh
# Check company state, active/waiting roles, and VM state
~/.local/bin/firm status "$FIRM_DIR"

# Deep read-only telemetry (omit --section for all sections, or choose: overview, inbox, readiness, conversations, timeline, usage, mac, resources, network)
~/.local/bin/firm analyze "$FIRM_DIR" --section overview --json
~/.local/bin/firm analyze "$FIRM_DIR" --section inbox --json
~/.local/bin/firm analyze "$FIRM_DIR" --section readiness --json

# Send a message or follow-up instruction to the founder (use a unique --request-id per message)
~/.local/bin/firm send "$FIRM_DIR" founder \
  --subject "Additional context" \
  --body "Please make sure the deliverables are saved in shared/ and attached to your reply." \
  --request-id "owner-msg-1"

# List and grant/deny pending agent requests (e.g. unlisted network hosts)
~/.local/bin/firm requests "$FIRM_DIR"
~/.local/bin/firm requests "$FIRM_DIR" grant q1
~/.local/bin/firm network "$FIRM_DIR" allow docs.example.com

# Gracefully drain active work and shut down the VM
~/.local/bin/firm stop "$FIRM_DIR"
```

### Path (B): For Humans (Interactive Wizard)

Run `firm start` with no arguments in an interactive terminal:

```sh
firm start
```

The wizard walks you through three steps:
1. **Choose your provider:** Pick **Codex**, **Claude**, or **AGY (experimental subscription)**.
   See [Experimental AGY integration](#experimental-agy-integration).
2. **Choose your first mandate:** Pick the built-in **weekly planner** starter (which produces a single interactive HTML weekly planner you can preview in the console) or describe your own task.
3. **Complete isolated sign-in:** Firm checks local prerequisites, opens an isolated provider sign-in in your browser, saves the login owner-private (`0600`) under `~/.config/firm-runtime/credentials/`, and starts the firm.

Firm stores owner data under `firms/` in the current directory by default. Run the wizard from the repository root to keep local runs in its Git-ignored `firms/` tree. Use `firm start --data-dir PATH` to choose another root. Starting with no directory opens the wizard (**Create a new firm** or **Resume**); `firm start DIRECTORY` starts an existing firm directly.

---

## 4. Default Firewall, Web Search, and Network Egress

Every guest VM's network interfaces are locked down at the host boundary: there is no direct internet route, no UDP/DNS, and no access to your Mac's loopback or private LAN. All outbound HTTP/HTTPS traffic from roles goes through the host's egress proxy (`packages/firm-sandbox/src/firm_sandbox/egress_proxy.py`).

* **Web Search works out of the box:** Provider-native web search (`web_search` in Codex, `WebSearch` in Claude Code, and `search_web` in AGY roles and tasks) is enabled by default and executes on the provider's servers over the pre-allowed provider API hosts. Firm denies AGY's native page fetch (`read_url`); AGY tool-free runs do not enable search.
* **Default mode (`egress = "enforce"`):** Allows a built-in baseline of essential hosts out of the box:
  * **Providers & Web Search:** `*.anthropic.com`, `*.claude.com`, `*.openai.com`, `chatgpt.com`, `*.chatgpt.com`
  * **GitHub & Containers:** `github.com`, `*.github.com`, `*.githubusercontent.com`, `ghcr.io`, `*.docker.com`, `*.docker.io`
  * **Package Registries & OS Mirrors:** `pypi.org`, `files.pythonhosted.org`, `astral.sh`, `releases.astral.sh`, `registry.npmjs.org`, `nodejs.org`, `*.ubuntu.com`, `*.debian.org`, `formulae.brew.sh`, `*.apple.com`, `*.cdn-apple.com`
  * Unlisted third-party domains return HTTP `403`. When a role needs an unlisted host for `curl`, `WebFetch`, or an SDK call, it calls `request_access(kind="network_host", resource="...", purpose="...")`, which creates a pending **Owner Request** you can grant in the console or via `firm requests DIRECTORY grant <id>` / `firm network DIRECTORY allow <host>`.
* **Open public web mode (`egress = "observe"`):** If you prefer agents to reach any public HTTP/HTTPS domain directly without prompting for host approvals (while still blocking private/loopback IP ranges and recording every hostname in the proxy log), configure `sandbox.toml` (changes to `egress` take effect on the next VM start):
  ```toml
  [network]
  egress = "observe"
  allow_hosts = []
  ```

---

## 5. First Boot, Provider Sign-In & Daily Use

The first start downloads the Ubuntu cloud image and provisions the Linux guest, which can take several minutes. Keep the Mac connected to power and ensure enough free disk space for the VM. The owner console opens in your browser (`http://127.0.0.1:8766/` by default) and the local host continues running in the background after `firm start` returns and after its terminal closes. Bookmark the console URL to reconnect to the same firm.

The console can be ready before the Linux VM finishes starting. If startup fails, the console stays available and shows the cause. Firm retries brief package-download problems and leaves incomplete setup safe to resume. Before clicking **Resume**, wait until the console confirms the VM has stopped; then retry from the console. If shutdown is still in progress, wait and check again.

### Provider sign-in and model defaults

See [Provider differences](runtime.md#provider-differences) for a comparison of Codex, Claude and AGY.

The wizard isolates login from your everyday provider CLI. If Codex or Claude Code
is installed, Firm uses that CLI in a dedicated temporary configuration. If the CLI
is missing, Firm can fetch it with `npx` (Node.js/npm). Codex uses the CLI's browser
login and file-backed credentials. Claude uses `setup-token`; Firm captures the
token without printing it and saves it in its owner-only credential store. If setup
is interrupted, rerun `firm start` and choose the incomplete firm to resume.

The first Codex firm defaults to `gpt-6-luna`; Claude defaults to Sonnet. These are
starting choices, not a promise that every account or workspace exposes every model.
If a model is unavailable, edit `firm.toml` and apply it with
`firm configure DIRECTORY --config DIRECTORY/firm.toml`. Advanced credential and
model routing options are in [runtime operations](runtime.md#provider-credentials)
and [firm configuration](runtime.md#firm-configuration).

### Daily use

Use **Overview** for instructions and decisions, **Inbox** to reply or mark mail done,
**Results** to read attached files and follow up, **Team** for roles and mandates, and
**System** for health, diagnostics and history. The [console](runtime.md#console) itself
remains available when execution is stopped, but file browsing does not start a stopped
VM; choose **Firm → Resume Firm** when you need its guest files again. Stop
the firm before closing its host: choose **Firm → Stop after current work**, wait for both
execution and VM state to report stopped, then **Firm → Close Firm**. Start it again
with `firm start DIRECTORY` or choose it under **Resume** in `firm start`.

For diagnosis, `firm start DIRECTORY --foreground` keeps the host attached to the
terminal. The normal detached mode survives terminal closure. `firm check DIRECTORY`
is a read-only local diagnostic; it does not start a VM or contact a provider, and a
passing check does not prove remote model access.

Use `firm status DIRECTORY` for a command-line state summary and
`firm analyze DIRECTORY --section inbox` to inspect pending owner mail and requests.
Neither a process exit nor a plausible output filename proves that the task is
complete; open and inspect the artifact, then send follow-up instructions through
the console when it needs changes.

See [development checks](development.md),
[Linux VM operations](linux-vm.md), [Mac VM image creation](mac-vm-images.md), and
[Mac station operation](mac-station.md).

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| `limactl` is missing or older than 2.2.0 | Install or upgrade Lima (`brew upgrade lima` when installed with Homebrew), then rerun `firm start`. 2.2.0 is the tested baseline. |
| Provider CLI is missing and `npx` is unavailable | Install Node.js/npm, then rerun `firm start` to resume sign-in. |
| Provider login was interrupted | Choose the incomplete firm under **Resume**; its isolated login state is retained until credentials save successfully. |
| The provider rejected saved credentials | Choose the firm under **Resume** and complete sign-in again. Firm validates the replacement before clearing that account's authentication hold. |
| A configured model is rejected | Change its configured model and apply it with `firm configure DIRECTORY --config DIRECTORY/firm.toml`. |
| Setup reports a local prerequisite failure | Follow the action shown by `firm check DIRECTORY`, then rerun `firm start`. |
| A port is already in use | Stop the other host, or choose different `--console-port` and `--mcp-port` values. |
| A saved firm needs attention | Do not initialize over it. Run `firm check DIRECTORY` and use its original compatible runtime to inspect or export it. |

For configuration, requests, recovery, backups and deletion see
[runtime operations](runtime.md). Firm does not migrate incompatible firm databases
or VM images; compatible releases retain their existing state.

## Optional: GitHub

GitHub is off unless `firm.toml` has a `[github]` table; without it roles get no GitHub
credentials. To let roles push to and open pull requests in repositories of a GitHub
organization you own, and optionally create private repositories there:

Use the firm directory printed by guided setup for `firm_dir` in the commands below.
The default is under `firms/` in the directory where you launched guided setup.

1. Check the organization allows repository creation, with the [GitHub CLI](https://cli.github.com/)
   logged in as an organization owner (add the scope with
   `gh auth refresh -h github.com -s admin:org` if fields read `null`):

   ```sh
   gh api /orgs/<organization> --jq '{members_can_create_repositories, two_factor_requirement_enabled}'
   ```

2. Add to `firm.toml`:

   ```toml
   [github]
   organization = "<organization>"
   # auto_create = false   # withhold repositories on request; default true
   ```

3. Apply it to the firm created above, from a terminal:

   ```sh
   firm configure "$firm_dir" --config "$firm_dir/firm.toml"
   ```

   The first firm for an organization opens the browser twice: choose **Create GitHub
   App**, then install it on the organization with **All repositories**. Later firms
   for the same organization reuse that App.

[Runtime operations](runtime.md#github) cover the recommended organization settings,
grants for existing repositories, what roles receive and revoking access.

## Optional: generative images

Image generation is off unless `firm.toml` has a `[google_cloud]` table. To give roles
one Google Gemini API key, in a Google Cloud project you own:

1. Enable the API, and create a key that can reach it and nothing else:

   ```sh
   gcloud services enable generativelanguage.googleapis.com apikeys.googleapis.com --project <project>
   gcloud services api-keys create --project <project> --display-name=firm-images \
     --api-target=service=generativelanguage.googleapis.com
   ```

2. Write the key string, alone on one line, to
   `~/.config/firm-runtime/credentials/google-cloud-<project>.key`, mode `0600`.

3. Add to `firm.toml`:

   ```toml
   [google_cloud]
   project = "<project>"
   ```

4. Put a monthly spend cap on the project in [AI Studio](https://aistudio.google.com/),
   under **Spend → Monthly spend cap**. Nothing in the firm counts spending, and the key
   works until Google stops it.

[Runtime operations](runtime.md#generative-images) cover what roles receive, applying it
to a running firm, and why a Cloud Billing budget is a backstop rather than a cap.

## Advanced: a Mac with stations

A firm may add one Mac VM carrying several stations, each an account any role can
lease for an Xcode build, a simulator or a desktop app. It needs a prepared Mac image
with the applications you want on it, Xcode on the host to build the station helper,
and one-time enrollment inside the image. Enrollment creates station accounts, installs
the helper and grants its consents, with one short console turn at the VM window per
account; every firm clones that same enrolled image. The
[Mac VM image guide](mac-vm-images.md) covers image creation and enrollment, while the
[Mac station guide](mac-station.md) covers leases and desktop use. Once an enrolled
image exists, a firm uses it by naming it in `sandbox.toml`:

```toml
[mac]
image = "/absolute/path/to/enrolled-image"
```

That gives the firm one Mac of 8 CPUs and 24 GiB, the defaults, with as many stations
as the image was enrolled with; `cpus` and `memory_gib` may be set, the station count
may not. The [acceptance tools](../tools/acceptance/README.md) verify a configured
environment with disposable firms; only the Mac lease checks need `[mac]`.

## Project and release information

See [release procedures](releases.md) and [source and attribution](provenance.md).

## Experimental AGY integration

AGY is an **experimental subscription provider** in the first-run chooser. Install
the native host CLI with the [vendor's installer](https://antigravity.google/cli/install.sh);
the `agy` executable must be on the PATH of the process that runs `firm start`.
Firm does not fetch it through npm. The guest package pins CLI 1.5.3.

`firm login ACCOUNT --provider agy` opens the interactive native
`agy acp --login --include-default-customizations=false` flow in a dedicated
host-owned profile. The default credential is
`~/.config/firm-runtime/credentials/ACCOUNT-agy/home/.gemini/antigravity-cli/antigravity-oauth-token`.
An explicit `--auth-file PATH` must be absolute and end in
`home/.gemini/antigravity-cli/antigravity-oauth-token`; that profile's `home`
must not be the owner's own HOME. Firm does not import ordinary AGY HOME,
`.gemini`, API keys, or keyring credentials. On macOS, host login and renewal run
under a macOS sandbox that blocks the `security` tool, so the CLI keeps its
credentials in the profile's token file and shows no Keychain dialog.
These launches require `/usr/bin/sandbox-exec`.

Both `useG1Credits: false` in CLI settings and `userSettings.useAiCredits: false`
in native config disable credit overages. Refresh credentials stay on the host;
guests receive an access-only projection with a blank refresh token and no email
or unknown fields. Repeating login retries sign-in in the same profile; login
does not prove model entitlement. Quota/context telemetry are unavailable.
`[google_cloud]` cannot coexist with AGY because image API-key injection could
change inference billing. `gemini` is not a provider alias.

Long turns yield after roughly 45–55 minutes and resume the same conversation
with fresh credentials. AGY reads its token once; replacing it mid-turn cannot
extend its lifetime. Expiry can leave a conversation reporting persistent ERROR.
The VM clock must track the host; skew over roughly 300 seconds can defeat the
pre-expiry yield bound. See [runtime policy](runtime.md#experimental-agy-configuration)
for configuration, retry limits and experimental tool isolation.

If a retained conversation keeps failing, route it fresh on its current profile,
then authorize retry if its role budget is exhausted:
`firm route DIR EMPLOYEE --profile PROFILE --fresh --request-id ID`, then
`firm retry DIR EMPLOYEE --episode EPISODE --request-id ANOTHER_ID`.
Fresh routing discards the conversation but preserves holds and the attempt
budget; an ordinary retry alone retains the conversation.
