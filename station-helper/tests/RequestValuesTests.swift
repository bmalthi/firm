import CoreGraphics
import Foundation

enum HelperError: Error { case invalid(String) }

func expectThrows(_ work: () throws -> Void, _ message: String) {
  do {
    try work()
    fatalError("expected error: \(message)")
  } catch {}
}

@main enum RequestValuesTests {
  static func main() throws {
    let answer = try integer(["value": 42], "value")
    precondition(answer == 42)
    expectThrows({ _ = try integer(["value": true], "value") }, "booleans are not integers")
    expectThrows({ _ = try integer(["value": 1.5], "value") }, "fractions are not integers")
    expectThrows(
      { _ = try integer(["value": Double.infinity], "value") }, "infinity is not an integer")
    expectThrows(
      { _ = try integer(["value": Double(Int.max)], "value") },
      "rounded Int.max double cannot convert")
    expectThrows({ _ = try integer(["value": pow(2, 63)], "value") }, "2^63 cannot convert")

    precondition(bounded(Double.greatestFiniteMagnitude, 3, 0...8) == 8)
    precondition(bounded(-Double.greatestFiniteMagnitude, 3, 0...8) == 0)
    precondition(bounded(4.0, 3, 0...8) == 4)
    precondition(bounded(4.5, 3, 0...8) == 3)
    precondition(bounded(true, 3, 0...8) == 3)

    // Modifiers are held in the table's order, whatever order they were asked for in.
    let absent = try modifierNames([:])
    // The runtime may send modifiers as JSON null instead of omitting the key; both mean none.
    let nullModifiers = try modifierNames(["modifiers": NSNull()])
    let asked = try modifierNames(["modifiers": ["shift", "command"]])
    let held = try heldModifiers(asked)
    let option = try heldModifiers(["option"])
    precondition(absent.isEmpty)
    precondition(nullModifiers.isEmpty)
    precondition(asked == ["shift", "command"])
    precondition(held.map(\.key) == [55, 56])
    precondition(option.map(\.flag) == [.maskAlternate])
    expectThrows({ _ = try modifierNames(["modifiers": "shift"]) }, "modifiers must be an array")
    expectThrows({ _ = try modifierNames(["modifiers": [1]]) }, "modifiers must be strings")
    expectThrows({ _ = try heldModifiers(["fn"]) }, "fn is not a held modifier")
    expectThrows(
      { _ = try heldModifiers(["shift", "shift", "shift", "shift", "shift"]) },
      "at most four modifiers")

    // Millisecond arguments: absent is the operation's own default, present is bounded.
    let defaulted = try milliseconds([:], "hold_ms", 0...10_000)
    let zero = try milliseconds(["hold_ms": 0], "hold_ms", 0...10_000)
    let shortest = try milliseconds(["duration_ms": 50], "duration_ms", 50...10_000)
    // The runtime always sends the key, as JSON null when the role gave no duration.
    let null = try milliseconds(["duration_ms": NSNull()], "duration_ms", 50...10_000)
    precondition(defaulted == nil && null == nil && zero == 0 && shortest == 50)
    expectThrows(
      { _ = try milliseconds(["duration_ms": 49], "duration_ms", 50...10_000) }, "below the range")
    expectThrows(
      { _ = try milliseconds(["hold_ms": 10_001], "hold_ms", 0...10_000) }, "above the range")
    expectThrows({ _ = try milliseconds(["hold_ms": 1.5], "hold_ms", 0...10_000) }, "not an integer")
    precondition(milliseconds(.milliseconds(1500)) == 1500)
    precondition(milliseconds(.microseconds(1999)) == 1)

    // Optional integers (delta_x, delta_y): absent or null means the caller gave none.
    let noDelta = try optionalInteger([:], "delta_x")
    let nullDelta = try optionalInteger(["delta_x": NSNull()], "delta_x")
    let givenDelta = try optionalInteger(["delta_x": -12], "delta_x")
    precondition(noDelta == nil && nullDelta == nil && givenDelta == -12)
    expectThrows({ _ = try optionalInteger(["delta_x": 1.5], "delta_x") }, "not an integer")

    // Tap sequences.
    let burst = try taps(["taps": [["x": 1, "y": 2], ["x": 3, "y": 4, "after_ms": 120]]])
    precondition(burst.count == 2)
    precondition(burst[0].point == CGPoint(x: 1, y: 2) && burst[0].afterMilliseconds == 0)
    precondition(burst[1].point == CGPoint(x: 3, y: 4) && burst[1].afterMilliseconds == 120)
    expectThrows({ _ = try taps(["taps": []]) }, "at least one tap")
    expectThrows(
      { _ = try taps(["taps": Array(repeating: ["x": 1, "y": 1], count: 65)]) }, "at most 64 taps")
    expectThrows({ _ = try taps(["taps": [["x": 1]]]) }, "a tap needs both coordinates")
    expectThrows(
      { _ = try taps(["taps": [["x": 1, "y": 1, "after_ms": 5_001]]]) }, "after_ms is bounded")
    expectThrows(
      {
        _ = try taps([
          "taps": Array(repeating: ["x": 1, "y": 1, "after_ms": 5_000], count: 5)
        ])
      }, "the burst exceeds the request's duration budget")
    _ = try taps(["taps": Array(repeating: ["x": 1, "y": 1, "after_ms": 5_000], count: 4)])

    print("Request value tests passed")
  }
}
