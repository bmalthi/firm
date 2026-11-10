# Sandbox Design

> **TL;DR — How the Sandbox Works**
> * **Linux-First + Optional Mac Leases:** Every role is a dedicated Linux user (`firmrole-<hash>`) with its own `systemd` slice and **rootless Docker daemon**. Roles lease Mac desktop stations (`firmstation1..8`) on demand for Xcode and GUI work.
> * **Kernel-Verified Settlement (`populated 0`):** After each work period, the sandbox stops the role's slice, verifies `cgroup.events` reads `populated 0` (all processes dead, running containers included), and preserves scratch and on-disk Docker state.
> * **Reporting-Line SMB File Server (`/srv/firm`):** No host folders are mounted into guests. Internal Samba (`smbd`) + POSIX ACLs keep `org/<address>/work` private to that reporting line while `shared/` is firm-wide.
> * **Fail-Closed Packet Filter & Proxy:** Guest NICs drop all UDP, raw DNS, and direct IP traffic. Outbound HTTP/HTTPS goes through the host egress proxy (`egress = "observe"` by default for open public web, or `"enforce"` as the stricter opt-in).
> * **4-Check Mac Station Sweep:** Releasing a Mac station verifies `access_revoked`, `work_stopped`, `drive_detached`, and `desktop_usable` against the pre-lease snapshot before another role can acquire it.

---

## Package Boundary

* **`firm-sandbox`** owns VM lifecycles (`LinuxVM`, `MacVM`), guest controllers, the SMB file server (`FileServer`), the userspace packet filter and egress proxy, SSH reverse forwards, and Mac station leases (`MacStation`). It has zero dependency on providers, prompts, or SQLite scheduling.
* **`firm-runtime`** calls `Sandbox` to provision role accounts, launch uniquely identified runs, broker Mac desktop actions, update allowed network hosts, and sample VM resource counters.

---

## Linux Base Image

The shared Linux base builder runs the trusted installation recipe on ordinary
Apple Virtualization NAT, outside the firm boundary. It has no roles, firm data,
credentials or host mounts. This permits software downloads without exposing
untrusted work to that network. Its generalized disk supplies new firm VMs, which
boot behind the packet filter and egress proxy before any role runs.

## Roles on the Linux VM

Each hired role is provisioned on the Linux VM in two phases (recording the identity in the guest ledger before creating the user so interrupted provisioning is idempotent):
* **Dedicated Account & Slice:** `firmrole-<hash>` with a `0700` home directory, a reserved 65,536-ID subordinate UID/GID range, and `user-<uid>.slice`.
* **Rootless Docker:** The user manager (`user@<uid>.service`) runs in its default slice (required by `systemd` `OwnerUID` checks so rootless `runc` can allocate container scopes) while all role commands run inside `user-<uid>.slice`.
* **Task Cgroups:** Short-lived [`task()`](runtime.md#tasks) workers run as child processes inside dedicated root-owned leaf cgroups under the role's slice, killed via `cgroup.kill` on timeout or cancellation.

### 20-Second Settlement & Explicit Disposal

Nothing of a role runs between activations (`logind` lingering is off). When an activation ends, settlement runs under a 20-second deadline:
1. **Kill task children** and read the slice's `memory.peak`, CPU usage, and OOM counters before removing the cgroup.
2. **Stop the daemon & `user-<uid>.slice`**, then verify `cgroup.events` reads `populated 0` (or the cgroup is absent).
3. **Unmount `<home>/drive`** and remove temporary run credentials. Settlement does not walk retained storage.

Ordinary settlement removes nothing in either column.

| Category | Removed by Disposal (`dispose`, fired-role cleanup, a temporary appointment's finished final period, handoff `clean_up`) | Kept by Disposal |
| :--- | :--- | :--- |
| **Filesystem** | `<home>/scratch` (recreated empty `0700`, role-owned) | `<home>` tools, configs, provider transcripts, and `<drive>` (`org/` & `shared/`) |
| **Language Caches** | *None* | `uv`, `pip`, `npm`, `Cargo`, and `Go` caches in `<home>` (avoids re-downloading) |
| **Docker** | All containers (running ones are stopped first), dangling images (`dangling=true`), and ordinary build cache | Tagged images and all named/anonymous volumes |

* **Explicit disposal:** `firm-sandbox --config PATH dispose ROLE` starts rootless Docker, stops running containers, then prunes stopped containers, dangling images and ordinary build cache, stops and verifies the slice, then recreates scratch empty. The runtime disposes before retiring idle fired roles and when a temporary appointment's final period finishes rather than being cut short, the role staying employed; a completed `handoff` with `clean_up` requests best-effort disposal shortly after completion (or at the role's next launch, if it is woken first). Tagged images, volumes and drive files stay.
* **On-demand measurement:** `firm-sandbox --config PATH storage [ROLE]` uses `du` for read-only scratch and Docker estimates, including retired accounts, without execution locks. Both forms share a 60-second request budget; missing totals report unknown.
* **Disk pressure:** A fresh Linux sample at or above 85% used puts a notice in every role's inbox, and every twentieth work period carries a housekeeping notice; owner mail is described in the [runtime design](runtime.md#disk-pressure). Nothing is disposed of by age or quota.
* **Scratch at launch:** A symlinked or file scratch is replaced without following it, and a foreign-owned scratch directory is re-owned to the role.
* **Guest journal retention:** Finished guest run journals (`/var/lib/firm/runs/<id>`, which hold up to 64 MiB of raw provider stdout) are kept for **7 days** for postmortem debugging, then swept hourly once the host has committed token, tool, and resource tallies to SQLite.

### Key Design Trade-offs: Linux Execution
* **Why stop a `systemd` slice instead of `pkill -u <uid>`?** Killing processes by UID misses rootless Docker containers running under the role's 65,536 subordinate UIDs. Waiting on the slice's recursive `cgroup.events` (`populated 0`) guarantees every container and background subprocess is dead before the work period is released.
* **Why tombstone retired Linux UIDs forever instead of deleting the user?** If a retired role's UID or subordinate UID range were recycled for a new hire, the new hire could inherit leftover container layers or files written under those subordinate IDs.

---

## The File Server

`smbd` serves `/srv/firm` on the Linux VM's loopback interface:
* **Separate execution vs. file-server identities:** Every role gets a Samba login (`firmfile-<hash>`) in a shared traversal group (`firmfiles`) with a UID **separate** from its Linux execution account (`firmrole-<hash>`). During a run, root mounts the share at `<home>/drive` via `mount.cifs` mapped to the execution UID. Because the execution UID has no direct access to `/srv/firm`, neither the role nor its rootless containers can bypass Samba's root-owned file creation rules or strip a manager's ACLs.
* **Reporting-Line POSIX ACLs:**
  * **`shared/`** — Read/write for all active roles.
  * **`org/<address>/work`** — Read/write for the role and its ancestor managers; read-only (`rx`) for its descendants; **no ACL entry** for siblings, cousins, or other branches (they cannot read or list sibling directories).
  * **Retirement & Archiving:** Retiring a role deletes its Samba login and downgrades its reporting line's access on its workspace (or `org/.retired/<role>`) to read-only (`rx`).

### Key Design Trade-offs: File Server
* **Why internal Samba (`smbd`) instead of `virtiofs` or `NFS`?** `virtiofs` maps all guest file operations to the single macOS host user, making per-role reporting-line permissions impossible. `NFS` trusts whichever UID the client claims. Running `smbd` inside the Linux VM gives kernel-enforced per-role POSIX ACLs, works identically over SSH port-forwards to leased Mac stations, and lets the owner browse files from the console's **Results** view (or mount the share on the host via `firm-sandbox data-status`) without mounting host folders into guests.

---

## Network Filter & Egress Proxy (`egress_proxy.py`)

Each VM's network interface is spliced through a host-owned userspace packet filter and HTTP/HTTPS proxy running in the same host process:

```text
[ Guest Role / Container ]
        |  HTTP_PROXY / HTTPS_PROXY (with per-role proxy credential on Linux)
        v
[ Host Userspace Packet Filter ]  <-- Drops all UDP, raw DNS, LAN/loopback, and direct IPs;
        |                             only passes DHCP, management SSH, and TCP to proxy.
        v
[ Host Egress Proxy ]             <-- Resolves DNS on host, checks TLS SNI in both modes;
        |                             enforces allowlist in enforce mode, blocks private IPs,
        |                             logs {role, host, port, bytes}.
        v
[ Public Internet ]
```

1. **Fail-closed packet filter:** Rather than relying on macOS host `pf` firewall rules (which fail *open* if flushed or reloaded), the filter process holds the upstream socket directly; if the filter dies, the guest's network link is severed.
2. **Hostname validation without TLS MITM:** For HTTPS (`CONNECT :443`), the proxy resolves the hostname on the host, dials only public IPs, and inspects the initial TLS `ClientHello` SNI to verify it matches the `CONNECT` hostname (preventing domain fronting) without terminating TLS or installing a root CA in the guest. For plain HTTP (`:80`), it forwards one request per connection with a rewritten `Host` header and fixed `Content-Length`.
3. **Two Egress Modes (`[network] egress`):** Observe is the default because the baseline already includes GitHub, PyPI, npm and Docker Hub, which can receive data, so hostname allowlisting offers little protection while blocking legitimate sites.
   * **`"observe"` (Default):** Allows all public HTTP/HTTPS hosts while still blocking private/loopback/LAN IP ranges and recording per-role traffic in the proxy log.
   * **`"enforce"` (Stricter opt-in):** Allows `BASELINE_HOSTS` (Anthropic/Claude, OpenAI/ChatGPT, including provider-native **Web Search**, plus GitHub, GHCR, Docker Hub, PyPI, Astral/uv, npm, Node, Ubuntu/Debian apt, Homebrew, and Apple CDNs), `[network] allow_hosts`, and owner-granted domains in `hosts.json` (reloaded live on disk change; revoking a host immediately closes open tunnels).
4. **Live Egress Cutoff (`firm network DIR off`):** Closes the proxy exception across all VMs while keeping the host's management SSH channels open so the owner can still inspect or shut down the firm.

---

## The Mac Lease

Adding `[mac]` in `sandbox.toml` attaches one shared macOS VM (default 8 CPUs, 24 GiB RAM) carrying `1 to 8` enrolled station accounts (`firmstation1..8`, UIDs `502..509`).

### How Off-Console Station Sessions Work
* **`firmvm` (UID 501) parks on the console:** No station uses the main VM window. Instead, the host creates each station's GUI desktop session on demand via a brief loopback Screen Sharing login, verified by the station's signed Swift **Station Helper**.
* **Per-lease SSH tunnel & ephemeral key:** When a Linux role calls `mac_acquire`, the host snapshots the station's baseline processes/services, mounts the role's SMB drive, installs a one-time `ed25519` SSH key (with a command-logging wrapper), and opens a reverse SSH forward on the Linux guest (`127.0.0.1:2221 + n`).
* **Signed Swift Station Helper:** Runs inside each station's GUI session and exposes bounded desktop operations (`launch_app`, `inspect_ax`, `action`, `focus`, `capture_window`, pointer/key input, and `release`) over a Unix socket. The host relay verifies the helper's **kernel audit token and designated code signature** (not just UID) so untrusted apps running as the station user cannot spoof or talk to the helper.

### The 4-Check Verified Release Sweep
A leased station cannot be acquired by another role until `mac_release` (or owner `mac-recover`) passes all four checks against the pre-lease snapshot:

| Check | What Must Hold |
| :--- | :--- |
| **1. `access_revoked`** | The per-lease SSH reverse tunnel is closed, the temporary `authorized_keys` entry is deleted, and no `sshd` session for the station UID remains. |
| **2. `work_stopped`** | All processes, `launchd` services, `LaunchAgents`, and `crontab` entries for the station UID match the pre-lease baseline (except processes dynamically spawned by `launchd` from root-owned `/System`, sealed cryptex, or Xcode plists, plus Apple's `XProtectRemediator*`). |
| **3. `drive_detached`** | The role's SMB drive mount under the station home is unmounted. |
| **4. `desktop_usable`** | The signed Station Helper closes all tracked apps from the lease, verifies none remain running, and reports an unlocked desktop session with no system TCC/consent alert modal blocking the screen. |

### What Persists vs. What Is Reclaimed on the Mac
* **Reclaimed on every release:** `~/scratch`, iOS Simulator devices (`simctl`), Darwin `$TMPDIR` (`.../T`), top-level home additions, `~/.Trash`, and `~/Library` build/cache trees (`DerivedData`, `Archives`, `Caches`, `Logs`, `SwiftPM`).
* **Persisted across leases:** `/opt/homebrew` (writable by the `firmstations` group, on `PATH` for lease shells only, never root or `firmvm`), system-wide Xcode Simulator runtimes (roles can add missing Apple platforms via one fixed, argument-validated `sudo /usr/local/libexec/firm-station-add-platform <Platform>` rule), and standard station home folders (`~/Applications`, `~/Documents`, `~/Downloads`, `~/Desktop`, keychains, TCC consents).

### Key Design Trade-offs: Mac Stations
* **Why Linux-first + leased Mac stations instead of giving every agent a Mac account?** macOS lacks Linux's per-role `systemd` slices, `cgroup.events` emptiness guarantees, and native rootless containers. Making Linux the permanent home and the Mac a lease whose processes, mounts, simulators, and temp/scratch state are swept on release (while keeping shared `/opt/homebrew` and station home folders) preserves one clean execution model.
* **Why one Mac VM with multiple station accounts instead of one Mac VM per station?** Sharing one VM avoids duplicating guest memory allocations and Xcode Simulator runtimes. The trade-off is shared persistent tooling and a weaker cross-lease boundary; size the VM using measured resource pressure during the intended workload.

---

## Resource Observation & Durability

* **Lock-free 1-minute resource sampling:** `Sandbox.resources()` samples Linux `/proc` (available RAM, PSI stalls, CPU busy time, OOMs), Mac `vm_stat`/`sysctl`, and host `proc_pid_rusage` (`phys_footprint` per VM disk) once a minute without waiting on the guest controller's provisioning lock.
* **Durable disks & short socket paths:** Guest disks and SQLite live in `<firm_dir>/durable/`, while Lima Unix sockets live in a short `/private/tmp` runtime directory so long firm paths never exceed macOS's 104-byte `AF_UNIX` socket path limit.

## Security Boundaries

* **Host isolation:** Guest VMs have no host filesystem mounts, no access to host credentials (`~/.config/firm-runtime/credentials/` stays on the host), and no route to the host's loopback or LAN. If the host-side packet filter process exits, the guest's network link fails closed.
* **Credential staging:** On the Linux VM, per-run credentials (short-lived OAuth access tokens, GitHub installation tokens, and Mac lease SSH keys) are written to a memory-backed `tmpfs` (`/run/firm/<role>/credentials`, mode `0600`) and removed at settlement.
* **Shared-kernel and allowlist limits:** Employees share the Linux guest kernel and (when leasing a Mac) share `/opt/homebrew` and `/tmp` on the Mac guest. The sandbox enforces per-role Linux slices, separate Samba vs. execution UIDs, and reporting-line POSIX ACLs on `/srv/firm`, but it does not claim protection against guest kernel/hypervisor 0-days or data sent over hosts the owner has allowed through the egress proxy.

* **Persistent writable Mac tooling:** `/opt/homebrew` is writable by every member
  of `firmstations` and survives lease release. A role can replace a shared binary
  or library that a later role executes, so the release sweep does not provide a
  fresh software environment or prevent this form of cross-lease persistence.
  The shared prefix is excluded from root and `firmvm` command paths, but later
  station holders still trust its contents. Use Mac stations only when this shared
  trust is acceptable; a fresh VM per workload provides a stronger boundary.
