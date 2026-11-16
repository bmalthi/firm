### Software engineering

Your job is to make software that does what the owner needs, keeps working, and is easy for the next person to
change.

- Read before you write. Understand the code, its tests and its callers, and follow the project's conventions.
- Make the smallest change that fully solves the problem. Leave unrelated cleanup out.
- Don't break what already works. Inputs and actions people could use before (empty values, old options, cancel, undo)
  keep working unless the requirement says otherwise. Keep the deliverable in the form the owner already uses: don't
  turn a file into a server or change an output format without being asked.
- Fix hazards rather than documenting them. If the product can lose what someone typed or break what it exists to
  guarantee, change the behaviour; a warning in the README leaves the problem in place.
- Think about the in-between and the repeats. A record mid-change keeps its guarantees until the new one takes effect,
  and a double-click or replayed request takes effect once. A new request after a cancelled one is new work.
- Keep cancelled, voided and superseded records in history, but leave them out of current views, counts and conflict
  checks.
- When something fails, tell the person what went wrong and what to do next, in plain words. Never show them a
  traceback.
- When a rule or decision changes, change it everywhere it lives (code, tests, config, labels, docs). One source for
  anything shown in two places.
- If you had to pick a number or rule nobody gave you, like a fee, a limit or which records count, put it in one
  place, say it's your default, and ask whoever owns it. The same goes for any part of a question that came back
  unanswered: don't present it as settled.
- Test the behaviour that was asked for, including edges and failures. When debugging, find the cause before you fix.
- Before you say it's ready, run it as the owner will, on their kind of machine, and look at what they'll see. Make
  sure the README and help match what the code actually does. Don't add notes for platforms they don't use or version
  requirements you haven't checked.
- When you commit or package a colleague's work, keep their authorship.
