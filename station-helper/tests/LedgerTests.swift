import Darwin
import Foundation

// Ledger.swift normally receives this error type from StationHelper.swift.
enum HelperError: Error {
  case unavailable(String)
  case operation(String)
}

func expectThrows(_ work: () throws -> Void, _ message: String) {
  do {
    try work()
    fatalError("expected error: \(message)")
  } catch {}
}

func marker(_ socket: String) -> URL {
  URL(fileURLWithPath: socket).deletingLastPathComponent().appendingPathComponent(
    ".firm-station-helper-ledger.json")
}

func app(_ pid: pid_t, _ executable: String, started: UInt64) -> ProcessIdentity {
  ProcessIdentity(pid: pid, uid: 502, executable: executable, startSeconds: started,
    startMicroseconds: 7)
}

@main enum LedgerTests {
  static func main() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(
      "firm-ledger-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: root) }
    let socket = root.appendingPathComponent("control.sock").path
    // A launch is named by the bundle's executable directory: which of its executables
    // the process ends up running is not known until it runs.
    let one = "/Applications/One.app/Contents/MacOS"
    let two = "/Applications/Two.app/Contents/MacOS"

    // A crash between marker creation and registration leaves a pending launch that
    // blocks a restarted helper until no station process runs one of that bundle's.
    let prelaunch = try LeaseLedger(socketPath: socket)
    try prelaunch.beginLaunch(directory: one)
    let restarted = try LeaseLedger(socketPath: socket)
    precondition(!restarted.isClean && restarted.launching == one)
    expectThrows({ try restarted.beginLaunch(directory: two) }, "pending launch must block launch")
    let running = [app(900, one + "/One", started: 50)]
    precondition(restarted.remaining(in: running) == ["unverified launch of \(one)"])
    expectThrows({ try restarted.clearAfterVerifiedCleanup(processes: running) },
      "a running pending launch must not clear")
    precondition(FileManager.default.fileExists(atPath: marker(socket).path))
    try restarted.clearAfterVerifiedCleanup(processes: [app(901, two + "/Two", started: 50)])
    precondition(restarted.isClean && !FileManager.default.fileExists(atPath: marker(socket).path))

    // Once the launched pid is captured, it remains attributable after macOS moves a
    // signed app into Chromium's code-sign clone directory. The pending record survives
    // a helper restart, refuses both clear paths while that kernel process is live, and
    // clears when the pid has been reused by a different process.
    let chromeDir = "/Applications/Google Chrome.app/Contents/MacOS"
    let cloneExecutable =
      "/private/var/folders/test/com.google.Chrome.code_sign_clone/Google Chrome.app.bundle/Contents/MacOS/Google Chrome"
    let pendingChrome = try LeaseLedger(socketPath: socket)
    try pendingChrome.beginLaunch(directory: chromeDir)
    let clone = app(1700, cloneExecutable, started: 80)
    try pendingChrome.identifyLaunch(clone)
    let restartedChrome = try LeaseLedger(socketPath: socket)
    precondition(restartedChrome.launching == chromeDir)
    precondition(restartedChrome.launchingIdentity == clone)
    precondition(restartedChrome.remaining(in: [clone]) == ["unverified launch of \(chromeDir)"])
    expectThrows({ try restartedChrome.abandonLaunch(processes: [clone]) },
      "a live cloned launch must not be abandoned")
    expectThrows({ try restartedChrome.clearAfterVerifiedCleanup(processes: [clone]) },
      "a live cloned launch must not clear")
    try restartedChrome.clearAfterVerifiedCleanup(processes: [app(clone.pid, two + "/Two", started: 81)])
    precondition(restartedChrome.isClean)

    // A trampoline hands off to a sibling in the same directory: that process is the
    // launch, and it is what the ledger records and later verifies gone.
    let trampolined = try LeaseLedger(socketPath: socket)
    try trampolined.beginLaunch(directory: one)
    let sibling = app(950, one + "/OneHelper", started: 60)
    expectThrows({ try trampolined.record(sibling) },
      "a launch cannot be recorded before its identity is captured")
    try trampolined.identifyLaunch(sibling)
    expectThrows({ try trampolined.record(app(951, two + "/Two", started: 60)) },
      "another bundle's executable is not this launch")
    try trampolined.record(sibling)
    precondition(trampolined.launching == nil && trampolined.apps == [sibling])
    precondition(trampolined.remaining(in: [sibling]) == ["pid 950 \(one)/OneHelper"])
    try trampolined.clearAfterVerifiedCleanup(processes: [app(950, one + "/One", started: 61)])
    precondition(trampolined.isClean)

    // A failed launch drops only its own entry, once its bundle's executables are gone.
    let ledger = try LeaseLedger(socketPath: socket)
    try ledger.beginLaunch(directory: one)
    let first = app(101, one + "/One", started: 1)
    try ledger.identifyLaunch(first)
    try ledger.record(first)
    try ledger.beginLaunch(directory: two)
    expectThrows({ try ledger.abandonLaunch(processes: [app(102, two + "/Two", started: 2)]) },
      "a surviving failed launch must stay recorded")
    try ledger.abandonLaunch(processes: [first])
    precondition(ledger.launching == nil && ledger.apps == [first])
    let data = try Data(contentsOf: marker(socket))
    let object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    precondition((object["apps"] as? [[String: Any]])?.count == 1 && object["launching"] == nil)
    try ledger.beginLaunch(directory: one)
    let pending = app(104, one + "/One", started: 4)
    try ledger.identifyLaunch(pending)
    expectThrows({ try ledger.record(app(103, two + "/Two", started: 3)) },
      "a launch cannot record a different process")
    try ledger.abandonLaunch(processes: [])

    // Tracked apps survive a restart and clear only once gone by pid and start time;
    // a reused pid with another start time is a different process.
    let reloaded = try LeaseLedger(socketPath: socket)
    precondition(reloaded.apps == [first] && !reloaded.isClean)
    precondition(reloaded.entries(excluding: [first]).isEmpty && reloaded.entries().count == 1)
    expectThrows({ try reloaded.clearAfterVerifiedCleanup(processes: [first]) },
      "a live tracked app must not clear")
    try reloaded.clearAfterVerifiedCleanup(processes: [app(101, one + "/One", started: 99)])
    precondition(reloaded.isClean && !FileManager.default.fileExists(atPath: marker(socket).path))

    // A ledger the helper cannot read never clears.
    try Data("{\"state\":\"launching\"}".utf8).write(to: marker(socket))
    let unreadable = try LeaseLedger(socketPath: socket)
    precondition(!unreadable.isClean && unreadable.entries().count == 1)
    expectThrows({ try unreadable.beginLaunch(directory: one) }, "unreadable blocks launch")
    expectThrows({ try unreadable.clearAfterVerifiedCleanup(processes: []) },
      "unreadable never clears")

    // An invalid pending-launch value is corruption, not the absence of a launch.
    // Treating it as nil would let cleanup erase evidence the helper cannot interpret.
    for value in [17, "relative/path"] as [Any] {
      let invalid = try JSONSerialization.data(withJSONObject: ["apps": [], "launching": value])
      try invalid.write(to: marker(socket))
      let invalidLaunch = try LeaseLedger(socketPath: socket)
      precondition(!invalidLaunch.isClean && invalidLaunch.entries().count == 1)
      expectThrows({ try invalidLaunch.clearAfterVerifiedCleanup(processes: []) },
        "an invalid launch marker never clears")
    }
    print("Ledger tests passed")
  }
}
