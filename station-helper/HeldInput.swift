import Foundation

/// Balance a press even when its hold is cancelled. Release must bypass cancellation
/// while retaining the target identity and permission checks used for ordinary input.
@MainActor
func withPressedInput(
  press: () throws -> Void, release: () throws -> Void,
  body: () async throws -> Void
) async throws {
  try Task.checkCancellation()
  try press()
  var released = false
  defer { if !released { try? release() } }
  try await body()
  try Task.checkCancellation()
  try release()
  released = true
}
