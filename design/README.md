# Design Reference

This directory explains **how Firm works under the hood and why it was built this way**.

While [docs/](../docs/) covers day-to-day setup and CLI flags, the four documents here cover the core architecture, system invariants, and the rejected alternatives behind each subsystem:

| Document | What it covers |
| :--- | :--- |
| **[Runtime (`runtime.md`)](runtime.md)** | Roles, hiring & automatic model tiering (`low`/`medium`/`high`), mandate authority, SQLite scheduling & priority queues, OAuth token renewal, failure recovery, search/discovery, owner requests, and the web console. |
| **[Sandbox (`sandbox.md`)](sandbox.md)** | The persistent Linux VM, per-role `systemd` slices & rootless Docker, kernel `cgroup.events` settlement, the internal SMB file server (`smbd`) with reporting-line POSIX ACLs, the host packet filter & allowlist proxy, and leased Mac stations (`firmstation1..8`). |
| **[Messaging (`messaging.md`)](messaging.md)** | Participant-set conversations (`c1`, `c2`), non-destructive peeks, atomic claims, sequence-watermarked `clear`, mid-turn mail hints, and isolated correspondence summaries. |
| **[Prompting (`prompting.md`)](prompting.md)** | How prompts are assembled without runtime LLM calls, the strict boundary between Python enforcement and prose, the workplace metaphor, and role memory (`profile.md`, `track-record.md`, `playbooks/`). |

### Where to find operator & package guides
* **Operator Guides (`docs/`):** [Installation](../docs/installation.md) · [Runtime Operations](../docs/runtime.md) · [Linux VM Guide](../docs/linux-vm.md) · [Mac Station Guide](../docs/mac-station.md) · [Mac VM Images](../docs/mac-vm-images.md)
* **Package & Helper Overviews:** [`packages/firm-runtime`](../packages/firm-runtime/README.md) · [`packages/firm-sandbox`](../packages/firm-sandbox/README.md) · [`station-helper`](../station-helper/README.md)
* **Verification & Acceptance:** Unit/contract tests live in `packages/*/tests`; live VM, Docker, network, and Mac lease verification suites live in [`tools/acceptance/`](../tools/acceptance/README.md).
