import CoreGraphics
import Foundation

let maximumInputBytes = 4096

/// The longest a single request may intend to spend posting input. The station relay
/// gives a request 30 seconds, which also has to cover activation and the reply.
let maximumIntentMilliseconds = 20_000

/// The modifiers a request may hold, with the virtual key and event flag each one posts,
/// in the order the helper presses them.
let modifierKeys: [(name: String, key: CGKeyCode, flag: CGEventFlags)] = [
  ("command", 55, .maskCommand), ("shift", 56, .maskShift),
  ("option", 58, .maskAlternate), ("control", 59, .maskControl),
]

struct Tap {
  let point: CGPoint
  let afterMilliseconds: Int
}

func integer(_ request: [String: Any], _ key: String) throws -> Int {
  guard let value = request[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
    value.doubleValue.isFinite, floor(value.doubleValue) == value.doubleValue,
    let exact = Int(exactly: value.doubleValue)
  else {
    throw HelperError.invalid("\(key) must be an integer")
  }
  return exact
}

/// A whole-number argument that may be absent or JSON null, meaning the caller gave none.
func optionalInteger(_ r: [String: Any], _ key: String) throws -> Int? {
  guard let raw = r[key], !(raw is NSNull) else { return nil }
  return try integer(r, key)
}

func bounded(_ value: Any?, _ fallback: Int, _ range: ClosedRange<Int>) -> Int {
  guard let value, let number = value as? NSNumber,
    CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite,
    floor(number.doubleValue) == number.doubleValue
  else { return fallback }
  if number.doubleValue <= Double(range.lowerBound) { return range.lowerBound }
  if number.doubleValue >= Double(range.upperBound) { return range.upperBound }
  return Int(number.doubleValue)
}

func number(_ r: [String: Any], _ key: String) throws -> CGFloat {
  guard let value = r[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
    value.doubleValue.isFinite,
    abs(value.doubleValue) <= 1_000_000
  else { throw HelperError.invalid("\(key) must be a finite coordinate") }
  return CGFloat(value.doubleValue)
}

func point(_ r: [String: Any], _ x: String, _ y: String) throws -> CGPoint {
  CGPoint(x: try number(r, x), y: try number(r, y))
}

/// A millisecond argument. Absent or null means the operation's own default (the runtime
/// always sends the key); present must be a whole number the operation can honour.
func milliseconds(_ r: [String: Any], _ key: String, _ range: ClosedRange<Int>) throws -> Int? {
  guard let raw = r[key], !(raw is NSNull) else { return nil }
  let value = try integer(r, key)
  guard range.contains(value) else {
    throw HelperError.invalid(
      "\(key) must be \(range.lowerBound)–\(range.upperBound) milliseconds")
  }
  return value
}

/// Whole milliseconds of a measured interval, for reporting cadence back to the caller.
func milliseconds(_ elapsed: Duration) -> Int {
  let parts = elapsed.components
  return Int(parts.seconds * 1000 + parts.attoseconds / 1_000_000_000_000_000)
}

func modifierNames(_ r: [String: Any]) throws -> [String] {
  guard let raw = r["modifiers"], !(raw is NSNull) else { return [] }
  guard let names = raw as? [String] else {
    throw HelperError.invalid("modifiers must be an array of strings")
  }
  _ = try heldModifiers(names)
  return names
}

/// The keys to hold for `names`, in press order. The one place a modifier name is resolved,
/// so an unknown one is refused wherever it arrives.
func heldModifiers(_ names: [String]) throws -> [(key: CGKeyCode, flag: CGEventFlags)] {
  guard names.count <= 4 else {
    throw HelperError.invalid("at most four modifiers may be held")
  }
  for name in names where !modifierKeys.contains(where: { $0.name == name }) {
    throw HelperError.invalid("unknown key modifier")
  }
  return modifierKeys.filter { names.contains($0.name) }.map { ($0.key, $0.flag) }
}

func taps(_ r: [String: Any]) throws -> [Tap] {
  guard let items = r["taps"] as? [[String: Any]], (1...64).contains(items.count) else {
    throw HelperError.invalid("taps must be an array of 1–64 objects")
  }
  let taps = try items.map {
    Tap(
      point: try point($0, "x", "y"),
      afterMilliseconds: try milliseconds($0, "after_ms", 0...5_000) ?? 0)
  }
  try requireIntentBudget(taps.reduce(0) { $0 + $1.afterMilliseconds })
  return taps
}

/// Refuses a request that would hold the connection past what the relay waits for.
func requireIntentBudget(_ total: Int) throws {
  guard total <= maximumIntentMilliseconds else {
    throw HelperError.invalid(
      "a request may intend at most \(maximumIntentMilliseconds) milliseconds of input")
  }
}
