import Foundation

/// Tracks the limits that can stop an accessibility-tree inspection. The caller
/// records a reason only when it observes that a node or descendants were omitted.
struct InspectionBudget {
  enum TruncationReason: String, CaseIterable {
    case axError = "ax_error"
    case axTimeout = "ax_timeout"
    case deadline
    case maxElements = "max_elements"
    case maxDepth = "max_depth"
  }

  let maximumElements: Int
  private let expired: () -> Bool
  private(set) var count = 0
  private(set) var reasons: Set<TruncationReason> = []

  init(maximumElements: Int, expired: @escaping () -> Bool) {
    self.maximumElements = maximumElements
    self.expired = expired
  }

  mutating func beginElement() -> Bool {
    guard hasTime() else { return false }
    guard count < maximumElements else {
      reasons.insert(.maxElements)
      return false
    }
    count += 1
    return true
  }

  mutating func hasTime() -> Bool {
    guard !expired() else {
      reasons.insert(.deadline)
      return false
    }
    return true
  }

  var remainingElements: Int { maximumElements - count }

  mutating func visitableChildCount(observed: Int) -> Int {
    if observed > remainingElements { reasons.insert(.maxElements) }
    return min(observed, remainingElements)
  }

  mutating func observedChildrenAtDepthLimit(_ count: Int) {
    if count > 0 { reasons.insert(.maxDepth) }
  }

  mutating func observedAXFailure(timedOut: Bool) {
    reasons.insert(timedOut ? .axTimeout : .axError)
  }

  /// Even an inspection whose deadline expires before its first AX call returns a
  /// usable application root. Account for that root in the response's node count.
  mutating func includeRootSkeleton() {
    precondition(count == 0 && maximumElements > 0)
    count = 1
    reasons.insert(.deadline)
  }

  var truncated: Bool { !reasons.isEmpty }
  var reasonNames: [String] { reasons.map(\.rawValue).sorted() }
}
