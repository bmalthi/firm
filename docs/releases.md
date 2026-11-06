# Building and validating a Firm release

Firm has one release identifier across `firm-runtime` and `firm-sandbox`. Both
package versions must match; the runtime pins that exact sandbox release. Package
metadata checks enforce equality. The sandbox package is an implementation boundary,
not a separately supported version choice for owners.

## Existing firms and images

`firm-release.json` records the creating release and compatibility generation.
The single generation constant lives in
`packages/firm-sandbox/src/firm_sandbox/compatibility.py`. It is shared by runtime
state and VM/image contracts. Change it only for a breaking persisted-state or
guest-contract change, not every patch release.

Equal generations can reuse an existing firm. Its original creating release and
image provenance remain intact. New Mac images and retained Linux/Mac VM records
carry compatibility and image identity too. An image recipe/digest identifies what
created a disk; it is not a second package version.

The ordinary Store constructor checks the marker before any directory, database,
WAL, schema or secret writes. CLI and host entry points also check before other
mutating work. VM drivers check their records before booting or installing guest
controllers. Missing, malformed, older
and newer generations refuse changes. There is no force override or migration.
Use the original runtime to stop and export an incompatible firm, retain that
runtime with its data, and explicitly create a new firm and image for the new
generation. Never add a marker to an old firm to bypass a refusal.

`firm analyze` remains a read-only diagnostic, subject to its schema/coverage limits.
The running original host can stop its firm; sandbox emergency power-off remains
available without upgrading its database. See [runtime operations](runtime.md).

## Local candidate checks

On supported macOS with uv, Node, Xcode tools and Lima:

```sh
uv sync --locked --all-packages
uv run tools/check.py --required
uv run tools/release_check.py --no-build
```

Run focused tests while changing a component, then this full batch after integration.
`--required` fails when Node or the Swift toolchain is missing instead of skipping.
The package check verifies one version and dependency pin, the four exact archives,
packaged assets/license/notice, wheel metadata, wheels rebuilt from extracted source
archives, and clean installed CLI/resource checks outside the checkout. The installed
smoke also runs `firm init`, `firm check` and `firm analyze` on a disposable,
provider-free firm with isolated home/configuration directories. It requires valid
local state and skipped credential checks, records host-prerequisite failures
separately, and does not launch a VM or contact a provider. Release verification
requires this `offline_workflow` receipt check.

`dist/release-evidence.json` records the source revision, dirty-checkout status,
compatibility generation, package SHA256 hashes and checks. Generated reports and
firm data remain ignored. A dirty local checkout produces development evidence;
publication requires evidence from a clean, tagged checkout.

## Live first-run rehearsal

Record evidence outside Git, with secrets and private task content removed. Record
the source revision and artifact hashes; macOS/architecture, RAM and free disk;
Lima/provider CLI versions; each user action; installation, provisioning and first
result times; and every undocumented repair. Mark each check passed, failed or not
run. Do not turn unmeasured timing into an installation promise.

Use a clean supported Mac/account with no saved Firm login or VM. Install the
candidate wheels as described in [installation](installation.md), then:

1. Run `firm start`, choose a provider and the weekly planner. Complete dedicated
   sign-in; confirm there is no handwritten configuration, IDs or credential-file
   copying. Repeat separately with Codex and Claude before advertising both paths.
2. Confirm the founder sends a completion message with the file attached and the file exists.
   Open it in **Results** and actually add, edit, complete and remove a task. A process
   exit, activation completion or plausible filename is not sufficient evidence.
3. Use **Write a follow-up** in the reader and verify the changed deliverable. Check that
   the reply stays in its conversation with the file attached. Re-submit an interrupted
   send and verify one durable message.
4. Use the disposable [request acceptance scenario](../tools/acceptance/requests.py)
   to request an otherwise blocked host. Grant in the console and verify access
   and wakeup; test denial and revocation. Do not use a real private service.
5. Stop, verify confirmed VM shutdown, close the terminal/browser, reopen the
   console, resume, and verify identities, messages and outputs survive. Repeat
   after a host-process interruption; check the visible recovery state.

Also exercise canceled sign-in, rejected credentials/model, insufficient resources,
interrupted provisioning, occupied ports/second start, incompatible state/image,
oversized/traversing artifact paths and provider limits. Every failure must preserve
work and give an actionable next step. The automated fake-boundary and browser
checks cover deterministic cases; they do not substitute for real provisioning and
model-generated output.

## Optional Mac images

Follow [Mac VM creation](mac-vm-images.md) through host signing, attended guest
enrollment, sealing and a **fresh clone**. Run all-stations, isolation, session and
project-handoff checks from the [Mac acceptance catalog](../tools/acceptance/mac/README.md).
Record OS/image identity, station count and signing continuity. Verify a real build
or desktop operation returns a file to the drive. Repair the source image and repeat
an affected clone check; extra grants on a failing clone do not prove the sealed
image works. Linux-only first use does not require this optional image process.

## Tag verification

Firm has no GitHub CI or hosted publishing workflow. Run the checks above locally
on supported macOS. Before tagging a `v<version>` release:

- Finish the live rehearsal above on the candidate and record supported versions
  and known limitations in release notes.
- Use **bmalthi** as the public owner identity and `https://github.com/bmalthi/firm`
  as the public home. Preserve other contributors' applicable notices.
- Enable and verify
  [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).
- Review [provenance](provenance.md), license/notice copies. Keep
  firm exports, account logins, signing keys and private acceptance logs out of
  distributions and repository commits.

From a clean checkout of the intended tag, rerun local checks, then verify
the exact candidate archives:

```sh
uv run tools/verify_release.py --tag v0.1.0 --dist dist
```

Substitute the actual tag for a later release. The verifier checks the source
revision, clean-checkout receipt and artifact hashes. Pushing a commit or tag
does not run checks or publish anything automatically.
