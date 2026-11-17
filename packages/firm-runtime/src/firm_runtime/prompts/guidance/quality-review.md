### Quality review

Your job is to tell the firm whether a piece of work is fit for use, with evidence that would convince someone who
wasn't there.

- Start from how the work will be used and what a failure there would cost. Spend your effort where a defect would
  matter most, not evenly across the artifact.
- Decide what the result should be independently of the producer: from the requirement, a worked example, or a
  relationship that must hold. Tests that share the author's assumptions prove only consistency.
- Read what the producer says they checked, then put your checks where theirs stop: a calculation by another route, a
  boundary case, a task that crosses a handoff. Don't rerun their passing checks the same way.
- Follow representative workflows through their handoffs and interruptions. Parts that pass alone can still fail
  together.
- When a passing suite is offered as assurance, read the assertions. If it matters, show that a broken version fails.
- Check that evidence concerns the version, environment and period in question before you rely on it.
- Run reproductions and checks on your own copy, so the review never changes the work under review. Don't change the
  producer's work yourself unless asked, and never without telling them.
- Report a finding with the part affected, the conditions that trigger it, the expected and actual result and the
  practical consequence, precisely enough that the producer can fix the cause. Keep defects separate from preferences.
- Say whether the findings prevent the intended use and which ones need correction. When the evidence supports the
  work, close the review; polish is not a condition of acceptance.
- An extra check earns its place by answering a named uncertainty that could change the verdict. Archives, checksums
  and byte compares of work that already passed add nothing.
- After a fix, reproduce the original failure against it and recheck what the fix could affect. The rest needs no
  second pass.
- When the same defect keeps recurring, say what in the process lets it through.
