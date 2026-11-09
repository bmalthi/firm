# Messaging Design

> **TL;DR — How Messaging Works**
> * **Conversations Are Participant Sets, Not Threads:** Every message between `{founder, e1}` lands in `c1`. Adding `e2` (`reply(m1, ..., add=[e2])`) moves the discussion to `{founder, e1, e2}` (`c2`, with `continues = c1`).
> * **Short Base-36 IDs (`c1`, `m4a`):** Compact, monotonic base-36 tokens prevent the UUID hallucinations common in long tool-call chains.
> * **Sequence-Watermarked `clear`:** `clear()` dismisses only pending mail up through the watermark of the last complete `peek`; new mail arriving mid-turn is not dismissed unseen.
> * **Mid-Turn Mail Hints:** New mail does not interrupt a running turn; tool responses append a `mail_waiting` summary of senders and counts so the role can `peek` before finishing.
> * **Isolated Summaries (`context`):** Summarizes a conversation and its `continues` chain via an isolated, tool-free LLM call without using the role's context window or slot.

---

## Conversations by Participant Set

Communication is asynchronous and stored in the host's SQLite database outside role sandboxes:
* **Identity = Participant Set:** A conversation is uniquely keyed by the set of people it reached (author + delivered recipients) and assigned a short base-36 ID (`c1`, `c2`, …) on first use. The Owner and Operations are participants like any role.
* **Bringing People In (`add=[...]`):** Replying with `add=[D]` resolves to the conversation of the current members plus `D`. If that larger conversation is new, it records `continues = <previous_conversation_id>`.
  * The original members keep their smaller conversation untouched, and `D` never sees the old conversation unless `D` calls `context()` (or the sender passes `summarize=true` on the introduction).

### Key Design Trade-offs: Conversations
* **Why participant sets instead of per-send threads?** When agents manage separate thread IDs, they constantly reply on stale threads, fork duplicate threads with the same colleague, or confuse display grouping with context lookup. Keying conversations by participant set means "talking to Alice and Bob" is always one place (`c4`), and adding Carol naturally transitions to `c9 (continues c4)` with zero "confirm audience change" round-trips.

---

## Inbox Operations

Each message is stored once (`m1`, `m2`, …), with independent per-recipient delivery dispositions (`pending`, `claimed`, `dismissed`, or `undeliverable`):

| Tool / Action | Effect on Recipient's Inbox |
| :--- | :--- |
| **`peek`** | Lists waiting conversations (Operations first, then Owner, then oldest waiting message), showing up to 5 message headers (`id`, `sender`, `subject`) per conversation without changing state. |
| **`claim`** | Returns full message bodies (by conversation ID or exact message IDs) and removes them from `pending`. |
| **`dismiss`** | Removes a conversation's waiting mail (or exact message IDs) from `pending` as read/set-aside without returning bodies. (Labeled **Mark done** in the Owner Console; replying as the Owner automatically dismisses the replied-to message.) |
| **`clear`** | Dismisses all pending messages up to the sequence watermark of the work period's last complete `peek`. |
| **`messages`** | Read-only lookup of exact stored messages by ID (`messages(message_ids=["m42"])`) regardless of disposition. |
| **`context`** | Read-only LLM summary of a conversation and up to 5 ancestors along its `continues` chain. |

### Watermarked `clear` & Atomic Claims
* **Why `clear` uses a watermark:** If a role peeks at its inbox at 10:00 (`m1..m5`), works for ten minutes while `m6` arrives at 10:05, and then calls `clear()` at 10:10, `clear()` only dismisses `m1..m5` and leaves `m6` untouched. If a role hasn't paged to the end of its `peek`, `clear()` refuses to dismiss unseen pages.
* **No silent truncation on claims:** Claiming by conversation returns as many complete messages (oldest first) as fit within the 64 KiB response ceiling and reports how many remain. Claiming by explicit message IDs is all-or-nothing: if the selection exceeds 64 KiB, it is rejected whole rather than removing mail from `pending` without returning its body.
* **Inbox peek health footer:** A role's `peek` also includes a compact, read-only status block (<4 KiB) showing context pressure and remaining temporary work periods for the role and its direct reports.

### Presentation
* **Waiting-mail notices:** When new mail arrives while a role is already running, the next runtime tool response attaches a compact `mail_waiting` notice (ordered by Owner, Operations, manager, then oldest arrival) without interrupting the current turn.
* **Context & report status in `peek`:** Every `peek` response presents waiting conversations alongside the role's current context-window advisory/handoff state, any direct reports nearing their context or temporary-appointment limits, and, when they apply, notices for low shared Linux disk (a fresh sample at or above 85% used), housekeeping on every twentieth work period, and low disk on a Mac station the role currently holds.

---

## Send, Reply, Attachments, and Bounces

* **Subjects:** Optional `1–80` character string; if omitted on `send`, defaults to the first 80 characters of the body (replies inherit the target message's subject).
* **Deliverable Attachments:** `send` and `reply` accept up to 10 file paths under the role's drive (`shared/...` or `org/<address>/work/...`). Paths are validated at send time and stored as drive-relative `{path, name}` references for the Owner Console's **Results** reader.
* **Idempotency:** Every mutating inbox/mail call takes a `request_id` scoped to the role; identical retries replay the stored receipt.
* **Deterministic Bounces:** Each recipient is evaluated independently. Sending to an unknown or retired address reports the failed recipient inline in the tool result (or queues a single deterministic Operations bounce if a recipient retires before delivery) while delivering normally to valid recipients.

---

## Context & Correspondence Summaries

When a role calls `context(conversation="c9")` (or introduces a colleague with `reply(..., add=[D], summarize=true)`):
1. **Selection:** The runtime gathers the newest messages in `c9` first, then walks backward through up to 5 ancestors along `continues` until reaching the 50-message or 64 KiB selection limit. (If the newest message alone exceeds 64 KiB, it is excerpted rather than skipped in favor of older mail.)
2. **Isolated Summarizer:** Runs an isolated, tool-free turn on the caller's bound provider account without consuming an active role slot (capped at 4 concurrent isolated calls firm-wide, 30s timeout, 16 KiB of summary text). A longer summary is cut to fit, ends with a marker line saying so, and is flagged `truncated: true`.
3. **Never blocks sending:** On `reply(..., summarize=true)`, the summary is computed before opening the send transaction and attached as derived text. If the summarizer times out or is busy, the introduction message is still sent immediately without the summary.

---

## Messaging Limits

| Boundary | Limit |
| :--- | :--- |
| **Message body** | Max `16 KiB` UTF-8 (validated before acceptance so every message can be claimed individually). |
| **Conversation members** | Max `50` participants (including sender). |
| **`peek` page** | Max `100` conversations per page, `5` listed headers per conversation, within a `16 KiB` response ceiling (`10 KiB` payload + `6 KiB` control reserve). |
| **`claim` / `messages` result** | Max `100` messages within a `64 KiB` response ceiling (`58 KiB` payload + `6 KiB` control reserve). |
| **`context` summary input** | Newest `50` messages across the conversation + up to `5` `continues` ancestors, within a `64 KiB` source-envelope budget. |
