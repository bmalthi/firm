# Messaging Design

> **TL;DR — How Messaging Works**
> * **Threads, Addressed by Message ID:** Each set of people shares one conversation with no subject. A `send` with a subject starts a separate named thread. Roles never handle a thread ID: any message ID (`m4a`) selects its thread.
> * **Action or FYI, Stated on Every Message:** `send` takes `to_action` and `to_fyi`; `reply` and `introduce` take `action`. Only recipients asked to act get a work period for that message.
> * **Unread Changes Only by `read`, `dismiss` or `clear`:** Sending, replying and introducing never touch the sender's inbox, and reading notifies no one.
> * **Boundaries Are Messages, Not Clocks:** `read`, `dismiss` and `clear` act on unread mail up to and including the message named, so mail that arrives afterwards is never read or set aside unseen.
> * **Mid-Turn Mail Hints:** New mail does not interrupt a running turn; tool responses append a `mail_waiting` summary of senders and counts so the role can `peek` before finishing.
> * **Isolated Summaries:** `read(summarize=true)` and `introduce(summarize=true)` summarize through an isolated, tool-free LLM call that uses neither the role's context window nor its slot.

---

## Threads

Communication is asynchronous and stored in the host's SQLite database outside role sandboxes. A thread is one row of `conversations`, and there are two kinds:

* **The no-subject conversation of a participant set:** exactly one per set of people (author plus delivered recipients), created on first use. A `send` without a subject always lands here, so everyday mail between the same people stays in one place. The Owner and Operations are participants like any role.
* **A named thread:** created by every `send` that carries a subject. The subject is fixed for the life of the thread. Nothing is ever merged by subject: two sends with the same subject to the same people are two threads.

Each message is stored once with a short base-36 ID (`m1`, `m2`, …). Threads have IDs too (`c1`, `c2`, …), but those appear only in the Owner Console and `firm analyze`. On the role surface a thread is always named by one of its messages.

* **`reply`** continues the thread of the message it names: same people, same subject or none.
* **`introduce`** brings people in without changing the source thread. For a named thread it starts a new thread with the same subject and the larger group; for a no-subject conversation the introduction goes into the larger group's own no-subject conversation. A thread created this way records `continues = <source thread>`. The original members keep their thread untouched, and newcomers cannot read it.

### Key Design Trade-offs: Threads

* **Why keep a shared no-subject conversation?** When every message needs a thread, agents reply on stale threads and fork duplicates with the same colleague. A default place keyed by participant set means "talking to Alice and Bob" needs no decision and no ID.
* **Why named threads as well?** A piece of work that takes many exchanges is easier to follow, read and set aside on its own than as part of everything a group has said. A subject is the one deliberate step that separates it.
* **Why never merge by subject?** Matching on text would make a typo or a reused title decide where mail lands. A thread is continued only by replying to a message in it.
* **Why message IDs instead of thread IDs?** Roles already hold message IDs from `peek` and `read`. One kind of ID is one fewer thing to copy wrongly, and a message ID also gives `read`, `dismiss` and `clear` their boundary.

---

## Action and FYI

Every message records, per recipient, whether it asks that recipient to act.

* **`send(to_action, to_fyi, body, subject=None, attachments=None)`:** both lists are given every time, may not overlap (compared by resolved person), and may not both be empty. The rule is the same for one recipient and for many.
* **`reply(message_id, action, body, attachments=None)`:** `action` names who is asked to act, chosen from the thread's other participants. Everyone else receives the reply as FYI; `[]` makes it FYI for all.
* **`introduce(message_id, add, action, body, summarize=False)`:** `action` may name anyone in the larger group.

Only recipients asked to act get an immediate work request. FYI mail still arrives in the inbox, shows up in the mid-turn mail hint, and is eligible for idle review when the firm has no ordinary work. The designation belongs to one message. It is never inherited from the thread or from an earlier message, and body `@mentions` designate no one.

---

## Inbox Operations

Each delivery has its own disposition: `pending` (unread), `read`, `dismissed`, or `undeliverable`.

| Tool | Effect on the Caller's Inbox |
| :--- | :--- |
| **`peek()`** | Lists one entry per thread with unread mail. Changes nothing. |
| **`read(message_id, summarize=False)`** | Returns the thread's unread messages up to and including `message_id`, oldest first, and marks them `read`. With `summarize=true`, returns a generated summary instead and marks only the messages it covers. |
| **`dismiss(message_id)`** | Sets aside the thread's unread mail up to and including `message_id` without returning it. |
| **`clear(message_id)`** | Sets aside all unread mail in every thread up to and including `message_id`. |

All four require the caller to be a participant of the selected message's thread.

### `peek`

`peek` takes no arguments. Each entry shows the latest unread message ID, the subject (or `No subject`), `Action` if any unread message in the thread asks the reader to act and `FYI` otherwise, the unread count, the other participants, and who wrote the last message. A newer FYI never hides an older unread action request. No-subject entries add a one-line preview of the last message, which is not stored as a subject.

```text
Inbox: 2 threads · 5 unread messages · newest m57.

m53 · Stage 1 direction · Action · 3 unread
      With: founder, designer, producer
      Last message: designer

m57 · No subject · FYI · 2 unread
      With: founder
      Last message: founder: I am reviewing the deck now…
```

Threads are ordered with Operations first, then the Owner, then by oldest unread message. There is no paging: entries fill the response budget in that order, and if threads remain the result says how many are not shown; they appear as the listed ones are read or dismissed. The header counts cover the whole inbox, and `newest` is the ID `clear` takes.

A `peek` also carries a compact read-only status block (under 4 KiB): the role's context-window advisory or handoff state, direct reports nearing their context or temporary-appointment limits, and, when they apply, notices for low shared Linux disk (a fresh sample at or above 85% used), housekeeping on every twentieth work period, and low disk on a Mac station the role holds.

### `read`

`message_id` selects the thread and the last message to include. Later messages in that thread and all other threads are untouched.

```text
Stage 1 direction
With: founder, designer, producer
Reading unread messages through m53.

m41 · founder · Action · 2026-11-16T14:03:00+00:00
Use the second route as the basis. Keep the operator benefit concrete.

m48 · designer · FYI · 2026-11-16T14:20:00+00:00
Asked to act: founder
Updated the opening layouts in shared/projects/stage1/layouts.html.

Marked read: m41, m48
Unread remaining through m53: 1
Continue: read(message_id="m53")
```

* **Bounded batches:** a call returns as many complete messages as fit, marks exactly those read, and says how many remain. Repeating the same call continues toward the same boundary and never sweeps in later arrivals. Every accepted message fits alone, so a call with unread mail always returns at least one.
* **Summaries:** `summarize=true` selects unread messages oldest first until the summary input limits are reached, summarizes them outside any transaction, and on success marks only the covered messages read. If the summary is unavailable nothing is marked read and the result says so. Messages left out by the input limit stay unread.
* **Reading again:** when nothing is unread up to `message_id`, `read` shows that message again and changes nothing. With `summarize=true` it returns a summary of the thread up to that message. This is how a role recovers a brief after a handoff, and how it catches up on a thread it has already read.
* **Handoff pressure:** when working memory is full, `read` is refused so no new bodies enter context. `peek`, `reply`, `send`, `dismiss` and `clear` stay available.

Reading records receipt. It tells the sender nothing and says nothing about whether the work is done; unfinished commitments belong in working memory and handoff notes.

### `dismiss` and `clear`

Both set mail aside without returning it and tell the senders nothing. `dismiss` covers one thread; `clear` covers the whole inbox and is the inbox-zero call. Because each names a message as its boundary, neither can set aside mail that arrived after the caller last looked.

### Presentation

* **Waiting-mail notices:** when new mail arrives while a role is already running, the next runtime tool response attaches a compact `mail_waiting` notice (ordered by Owner, Operations, manager, then oldest arrival) without interrupting the turn. A `peek` records what existed at that moment, so only later arrivals are announced.
* **Wake and idle review:** every work period starts with an inbox snapshot in the `peek` format. An idle review, which runs only when the firm has no ordinary work, lists the threads holding mail the role has not been offered before. Mail counted in a shown entry is recorded as offered, so the same unread mail does not trigger review after review.

### Key Design Trade-offs: Inbox

* **Why does nothing but `read`, `dismiss` and `clear` change what is unread?** A reply that also acknowledged mail made the inbox depend on what the role had happened to look at earlier. With one rule, the inbox always shows exactly what has not been read or set aside.
* **Why a message as the boundary?** A role acts on what it has seen. Naming the last message it saw means mail arriving mid-turn stays unread, with no watermark for the runtime to track and no requirement to `peek` first.

---

## Send, Reply, Introduce, Attachments, and Bounces

* **Subjects:** an optional `1–80` character string on `send`. Replies and introductions inherit the thread's subject; a no-subject conversation never acquires one.
* **Deliverable attachments:** `send` and `reply` accept up to 10 file paths under the role's drive (`shared/...` or `org/<address>/work/...`). Paths are validated at send time and stored as drive-relative `{path, name}` references for the Owner Console's **Results** reader. `introduce` carries no attachments.
* **Retries:** role tools take no `request_id`. For `send`, `reply` and `introduce` the runtime derives the retry key from the work period and the exact call, so an identical call repeated in the same work period returns the first receipt, says the message was already sent, and sends nothing. `read` is not replayed (repeating it continues), and `dismiss` and `clear` are idempotent by nature. The Owner Console and `firm send` supply their own operation keys.
* **Deterministic bounces:** each recipient is evaluated independently. Sending to an unknown or retired address reports the failed recipient inline in the tool result (or queues a single deterministic Operations bounce if a recipient retires before delivery) while delivering normally to valid recipients.
* **Operations notices:** every notice from Operations goes into the no-subject conversation of Operations and its recipients, with the notice title as the first line of the body. A role therefore sees one Operations entry in `peek` and reads all its notices with one call.
* **Owner Console:** the owner composes with the same thread rules. **Mark done** dismisses exactly the messages selected, and replying as the Owner dismisses the Owner's copy of the message replied to.

---

## Generated Context and Summaries

Two operations produce derived text, and both label it as generated:

* **`read(message_id, summarize=true)`** summarizes the selected unread messages, or the thread up to `message_id` when nothing is unread.
* **`introduce(..., summarize=true)`** prepends a summary of the source thread to the introduction. Wherever the message is read, the summary appears before the sender's body under a label saying it was generated by Operations at the sender's request and is neither an Operations instruction nor the sender's words, followed by a line marking where the sender's introduction begins.

How a summary is produced:

1. **Selection:** for unread mail, complete messages oldest first until the limits are reached. For an introduction, the newest messages of the source thread first, then up to 5 ancestors along `continues`, within the same limits (if the newest message alone exceeds the byte limit, it is excerpted rather than skipped in favor of older mail). For a no-subject source this is the recent mail of that group's whole conversation, not a reply chain.
2. **Isolated summarizer:** an isolated, tool-free turn on the caller's bound provider account, without consuming an active role slot (capped at 4 concurrent isolated calls firm-wide, 30s timeout, 16 KiB of summary text). A longer summary is cut to fit, ends with a marker line saying so, and still counts as available.
3. **Failure has no side effects:** if the summary is unavailable, `read` marks nothing read, and `introduce` sends nothing. Both say so, and the caller can retry, read the full messages, or introduce with `summarize=false` and write the context into the body.

Original messages remain stored whichever way they were read.

---

## Messaging Limits

| Boundary | Limit |
| :--- | :--- |
| **Message body** | Max `16 KiB` UTF-8 (validated before acceptance so every message can be read individually). |
| **Thread participants** | Max `50` (including the sender). |
| **Attachments** | Max `10` per `send` or `reply`. |
| **`peek` result** | As many thread entries as fit a `16 KiB` response ceiling (`10 KiB` payload + `6 KiB` control reserve); the rest are counted. |
| **`read` result** | Max `100` messages within a `64 KiB` response ceiling (`58 KiB` payload + `6 KiB` control reserve); the rest stay unread. |
| **Summary input** | Up to `50` messages within a `64 KiB` source budget; an introduction's context may also reach back through up to `5` `continues` ancestors. |
| **Summary output** | Max `16 KiB` of text. |
