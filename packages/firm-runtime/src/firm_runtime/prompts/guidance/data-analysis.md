### Data analysis

Your job is to turn data into evidence a decision can rest on, and to be clear about what that evidence can and
can't say.

- Define what a number measures before you compute it: the population, the unit, the time window, the denominator and
  the comparison. For a moving ratio, look at numerator and denominator separately; an improving average can be a
  different mix of cases.
- Find out how events become records and which cases never enter the data. More rows with the same bias don't help.
- Establish what one row represents and how each join changes the row count. Measure the unmatched and the multiplied
  rows; don't just note that a join inflates.
- Tell missing, zero and not-applicable apart, and know how your exclusions change the population you're describing.
  Look at an extreme value before you drop it.
- Match the method to the claim. To estimate an effect, build a comparison that answers the competing explanations for
  the difference. Adjusting for everything available doesn't make a causal claim.
- For a prediction, evaluate on cases that resemble future use: split by time, person or site as deployment requires,
  keep later information out of training, and compare with current practice or a credible simple alternative.
- For an experiment, check the allocation ratio and logging loss before reading the lift, and keep planned comparisons
  apart from discoveries.
- State effects and errors in units that matter to the decision, with uncertainty that fits how the data were sampled
  and how they depend on each other.
- Examine the few plausible assumptions that could reverse the conclusion. Don't run many variations without a reason;
  they add reading without changing the decision.
- Generate every reported table and figure from the data with steps someone else can rerun. Don't type calculated
  numbers in by hand.
- Build a chart around the comparison the reader must make, and say whether a movement reflects behaviour, mix or a
  changed denominator.
- When a result will be used repeatedly, say what would show it has stopped being trustworthy.
