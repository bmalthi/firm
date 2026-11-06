# Station helper

The station helper is a signed app, installed as `Firm Station Helper.app`, that
runs as each station account in the firm's Mac VM. It accepts one authenticated Unix
socket connection at a time, binds itself to a firm, role and lease, and launches applications,
tracking each one. `launch_app` defaults to `launch_mode:"launch_services"`, so each app
owns its protected data access. `launch_mode:"direct"` deliberately spawns a child that
inherits the helper's TCC grants and fixed environment, for tools opening the shared
drive. Direct mode is refused for Apple platform apps, which macOS kills when spawned
that way. The lease
binding survives connections: a connection that ends closes nothing, and the next
connection presenting the same binding adopts the lease's applications, ledger and
element handles. A different binding is refused while that lease still has applications
open. A release clears the binding and closes its applications; ledger and process
identity checks must confirm cleanup before another lease can bind. What a release cannot verify
gone stays in the ledger, across connections and helper restarts: the helper keeps
accepting connections, reports not ready and refuses new bindings until a later release
verifies those applications have stopped. It never terminates an application an
earlier helper process left. The helper is signed
with the hardened runtime; how it and the station relay authenticate each other is
in the [sandbox design](../design/sandbox.md#the-mac-lease).

`bind_lease` accepts `firm_id`, `role_id` and `lease_id` and replies with
`lease_bound:true` and `adopted` (whether the binding already existed). Status reports
`lease_bound` beside `session`, which describes the macOS login session. In Swift,
`LeaseState` holds the `LeaseBinding` and launch policy; `LeaseLedger` records the
applications that must be verified gone on release.

Launch verifies the returned PID, station UID, kernel start time and executable authority,
then waits for an activatable AppKit registration.
It does not require `isFinishedLaunching`, which some applications never report.
An executable directly inside the requested bundle's `Contents/MacOS`, including a
sibling reached by in-place exec, retains tool access. A relocation outside that
directory requires dynamic signature validation against the original
executable's exact CodeDirectory hash, as production Chrome makes a temporary signed
clone. An unrelated executable or reused PID cannot gain control. The pending launch's
kernel identity is persisted before GUI registration; release and failed-launch cleanup
still find the original process after relocation or an unauthorized exec.

Besides launching, it serves the desktop operations: `list_windows`, `capture_window`,
`inspect_ax`, `action`, `focus`, `type_text`, `press_key`, `click`, `drag`, `scroll` and
`tap_sequence`. The pointer operations take `modifiers`, held as real key presses for the
length of the call and released however it ends; `click` and `drag` take `hold_ms`, `drag`
takes `duration_ms` to pace its steps, and `tap_sequence` posts up to 64 taps on a
requested cadence and reports the offsets it achieved. One request may intend at most
20 seconds of input, inside the relay's 30-second request deadline.
Input requests use a scoped macOS interactive activity so a windowless helper's
timers and WindowServer calls are not treated as discretionary background work.
Posting offsets remain observations, not guarantees of application receipt timing.

`inspect_ax` accepts an optional `handle` from an earlier inspection of the same PID
and lease. Its depth (0–8) is relative to that element; omitted handles start at the
application. Inspections batch attribute reads, cap elements at 500 and time at three
seconds, and return the useful partial tree with `truncated` and `truncation_reasons`.
Continue from a returned branch handle to reach deeper controls or avoid a slow subtree.

Build the VM helper with Xcode and a signing identity:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
FIRM_HELPER_SIGN_IDENTITY='Developer ID Application: Example' \
sh station-helper/build.sh "$PWD/firms/.build/station-helper" 501 502
```

Use `-` as the identity for disposable compile checks. Install and enroll the
signed helper through the
[station enrollment guide](../docs/mac-vm-images.md#station-enrollment).

On macOS with Xcode selected, run the focused Swift checks from the repository root:

```sh
bash station-helper/tests/test-executable-code.sh
bash station-helper/tests/test-launch-policy.sh
bash station-helper/tests/test-held-input.sh
bash station-helper/tests/test-inspection-budget.sh
bash station-helper/tests/test-ledger.sh
bash station-helper/tests/test-request-values.sh
bash station-helper/tests/test-process-identity.sh
bash station-helper/tests/test-unix-socket.sh
bash station-helper/tests/test-peer-authentication.sh
bash station-helper/tests/test-request-framing.sh
```
