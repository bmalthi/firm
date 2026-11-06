import Foundation

enum ProbeError: Error { case failed }

@main enum HeldInputTests {
  @MainActor static func main() async throws {
    var events: [String] = []
    try await withPressedInput {
      events.append("down")
    } release: {
      events.append("up")
    } body: {
      events.append("hold")
    }
    precondition(events == ["down", "hold", "up"])

    events = []
    let cancelled = Task { @MainActor in
      try await withPressedInput {
        events.append("down")
      } release: {
        precondition(Task.isCancelled)
        events.append("up")
      } body: {
        withUnsafeCurrentTask { $0?.cancel() }
        try await Task.sleep(for: .seconds(10))
      }
    }
    do { try await cancelled.value; preconditionFailure("expected cancellation") }
    catch is CancellationError {}
    precondition(events == ["down", "up"])

    events = []
    do {
      try await withPressedInput {
        events.append("down")
      } release: {
        events.append("up")
      } body: {
        throw ProbeError.failed
      }
      preconditionFailure("expected hold failure")
    } catch ProbeError.failed {}
    precondition(events == ["down", "up"])

    events = []
    do {
      try await withPressedInput {
        throw ProbeError.failed
      } release: {
        events.append("up")
      } body: {
        events.append("hold")
      }
      preconditionFailure("expected press failure")
    } catch ProbeError.failed {}
    precondition(events.isEmpty)

    let alreadyCancelled = Task { @MainActor in
      withUnsafeCurrentTask { $0?.cancel() }
      try await withPressedInput {
        events.append("down")
      } release: {
        events.append("up")
      } body: {}
    }
    do { try await alreadyCancelled.value; preconditionFailure("expected cancellation") }
    catch is CancellationError {}
    precondition(events.isEmpty)
    print("Held input cancellation tests passed")
  }
}
