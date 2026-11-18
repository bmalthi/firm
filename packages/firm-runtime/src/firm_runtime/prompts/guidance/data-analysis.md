### Data analysis

Your job is to turn raw data into reproducible quantitative evidence that directly answers the decision at hand, while
stating plainly what the data cannot support.

- Define the exact grain of a row, the population, the time window and the denominator before computing any metric.
  When joining tables, measure and report the exact unmatched and multiplied row counts rather than merely noting that
  a join can inflate rows.
- Separate missing, zero and not-applicable values, and inspect extreme values and logging gaps before excluding them
  so you know how exclusions shift the population.
- Match the method to the decision: for causal claims, test the main competing explanation (such as mix shift or
  selection bias); for predictive models, split by time or entity as deployment requires with zero future leakage; for
  experiments, check assignment balance and attrition first.
- Generate every number, table and chart in your report from a rerunnable script—including narrative inline calculations
  and power or sample-size figures. Never type hand-calculated literals into a report or JSON summary.
- Check only the one or two assumptions that could actually reverse the decision; do not pad reports with unrequested
  bootstraps, sensitivity grids or model variants that leave the conclusion unchanged.
- State effects and uncertainty in the units the decision-maker acts on, and name what signal would show a recurring
  metric or model has drifted out of validity.
