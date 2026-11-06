import Foundation

@main enum InspectionBudgetTests {
  static func main() {
    var expired = false
    var budget = InspectionBudget(maximumElements: 3) { expired }

    precondition(budget.beginElement())
    precondition(budget.beginElement())
    precondition(budget.remainingElements == 1)
    // A known child count distinguishes a full result from omitted siblings while
    // allowing the AX child read itself to request only what can be visited.
    precondition(budget.visitableChildCount(observed: 2) == 1)
    precondition(budget.reasonNames == ["max_elements"])
    precondition(budget.beginElement())
    precondition(!budget.beginElement())
    precondition(budget.count == 3)
    precondition(budget.reasonNames == ["max_elements"])

    budget.observedChildrenAtDepthLimit(0)
    precondition(budget.reasonNames == ["max_elements"])
    budget.observedChildrenAtDepthLimit(1)
    precondition(budget.reasonNames == ["max_depth", "max_elements"])

    expired = true
    precondition(!budget.hasTime())
    precondition(budget.reasonNames == ["deadline", "max_depth", "max_elements"])

    budget.observedAXFailure(timedOut: true)
    budget.observedAXFailure(timedOut: false)
    precondition(
      budget.reasonNames
        == ["ax_error", "ax_timeout", "deadline", "max_depth", "max_elements"])

    var complete = InspectionBudget(maximumElements: 1) { false }
    precondition(complete.beginElement())
    precondition(complete.count == 1)
    precondition(!complete.truncated && complete.reasonNames.isEmpty)

    var immediateDeadline = InspectionBudget(maximumElements: 1) { true }
    precondition(!immediateDeadline.beginElement())
    immediateDeadline.includeRootSkeleton()
    precondition(immediateDeadline.count == 1)
    precondition(immediateDeadline.reasonNames == ["deadline"])

    print("Inspection budget tests passed")
  }
}
