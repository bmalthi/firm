// Public-API-only station helper for the Mac station.
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import ImageIO
import IOKit
import ScreenCaptureKit
import SystemConfiguration

enum HelperError: Error, CustomStringConvertible {
  case invalid(String)
  case unavailable(String)
  case operation(String)
  var description: String {
    switch self {
    case .invalid(let text), .unavailable(let text), .operation(let text): return text
    }
  }
  var code: String {
    switch self {
    case .invalid: return "invalid_request"
    case .unavailable: return "not_ready"
    case .operation: return "operation_failed"
    }
  }
}

struct Configuration {
  let controlUID: uid_t
  let stationUID: uid_t
  let environment: StationEnvironment
  static func load() throws -> Configuration {
    let accounts = try StationAccounts.load()
    let stationUID = getuid()
    guard accounts.contains(stationUID) else {
      throw HelperError.unavailable("helper UID is outside the configured station range")
    }
    let environment = try StationEnvironment.parse(
      Bundle.main.object(forInfoDictionaryKey: "FirmStationEnvironment") as? String
        ?? StationEnvironment.virtualMachine.rawValue,
      isVirtualMachine: StationEnvironment.isVirtualMachine)
    return Configuration(controlUID: accounts.control, stationUID: stationUID, environment: environment)
  }
}

struct SessionState {
  let uid: uid_t
  let guiUID: Int?
  let consoleUID: Int?
  let onConsole: Bool
  let loginDone: Bool
  let accessibility: Bool
  let screenCapture: Bool
  let eventPosting: Bool
  let screenLocked: Bool?

  static func screenLocked(uid: uid_t) -> Bool? {
    let root = IORegistryGetRootEntry(kIOMainPortDefault)
    guard root != 0 else { return nil }
    defer { IOObjectRelease(root) }
    guard let users = IORegistryEntryCreateCFProperty(
      root, "IOConsoleUsers" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue()
        as? [[String: Any]],
      let user = users.first(where: { ($0["kCGSSessionUserIDKey"] as? Int) == Int(uid) })
    else { return nil }
    // The lock marker is absent for an unlocked session on the tested macOS.
    guard let locked = user["CGSSessionScreenIsLocked"] else { return false }
    return locked as? Bool
  }

  static func read() -> SessionState {
    let dictionary = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
    var console: uid_t = .max
    _ = SCDynamicStoreCopyConsoleUser(nil, &console, nil)
    return SessionState(
      uid: getuid(), guiUID: dictionary[kCGSessionUserIDKey as String] as? Int,
      consoleUID: console == .max ? nil : Int(console),
      onConsole: dictionary[kCGSessionOnConsoleKey as String] as? Bool ?? false,
      loginDone: dictionary[kCGSessionLoginDoneKey as String] as? Bool ?? false,
      accessibility: AXIsProcessTrusted(), screenCapture: CGPreflightScreenCaptureAccess(),
      eventPosting: CGPreflightPostEventAccess(), screenLocked: screenLocked(uid: getuid()))
  }

  func json() -> [String: Any] {
    [
      "uid": Int(uid), "gui_uid": guiUID as Any, "console_uid": consoleUID as Any,
      "on_console": onConsole, "login_done": loginDone, "accessibility": accessibility,
      "screen_capture": screenCapture, "event_posting": eventPosting,
      "screen_locked": screenLocked as Any,
    ]
  }
}

func requireStationSession(_ config: Configuration) throws {
  let uid = getuid()
  guard uid != 0, uid == config.stationUID else {
    throw HelperError.unavailable("helper must run as its configured non-root station UID")
  }
  // This guard runs for every input event. Read only the state it checks: status's
  // Accessibility, Screen Recording and console-user queries add unrelated IPC here.
  // Each operation separately checks the permission it actually uses.
  let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
  guard session[kCGSessionUserIDKey as String] as? Int == Int(config.stationUID),
    session[kCGSessionLoginDoneKey as String] as? Bool == true
  else {
    throw HelperError.unavailable("configured station has no logged-in Aqua GUI session")
  }
  if let issue = sessionEnvironmentIssue(config, screenLocked: SessionState.screenLocked(uid: uid)) {
    throw HelperError.unavailable(issue)
  }
}

func sessionEnvironmentIssue(_ config: Configuration, screenLocked: Bool?) -> String? {
  config.environment.sessionIssue(
    screenLocked: screenLocked,
    setupAssistantRunning:
      NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.SetupAssistant")
        .contains(where: { processUID($0.processIdentifier) == config.stationUID && !$0.isTerminated }))
}

func processUID(_ pid: pid_t) -> uid_t? {
  guard pid > 0 else { return nil }
  var info = proc_bsdinfo()
  let bytes = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
  return bytes == Int32(MemoryLayout<proc_bsdinfo>.size) ? info.pbi_uid : nil
}

func requireTarget(_ pid: pid_t, config: Configuration) throws -> NSRunningApplication {
  try requireStationSession(config)
  guard processUID(pid) == getuid(), let app = NSRunningApplication(processIdentifier: pid),
    !app.isTerminated
  else {
    throw HelperError.invalid("target PID is not a live application in this desktop session")
  }
  return app
}

func primitive(_ value: CFTypeRef?, limit: Int = 512) -> Any? {
  guard let value else { return nil }
  if let string = value as? String { return String(string.prefix(limit)) }
  if let number = value as? NSNumber, number.doubleValue.isFinite { return number }
  return nil
}

func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
  var value: CFTypeRef?
  return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success
    ? value : nil
}

func axRequired(_ result: AXError, _ operation: String) throws {
  switch result {
  case .success: return
  case .cannotComplete:
    // The request reached the application, which did not answer within the one-second
    // messaging timeout; it may still act on it.
    var message = "\(operation) was delivered, but the application did not finish handling it"
      + " within 1 second; it may be busy or blocked"
    if !systemPrompts().isEmpty { message += " by a system prompt waiting on the console" }
    throw HelperError.operation(message)
  default:
    throw HelperError.operation("\(operation) returned AXError \(result.rawValue)")
  }
}

/// Processes that show system consent and authorization alerts. Such an alert outlives
/// the app that raised it and blocks later access it guards until someone answers it.
let systemPromptHosts: Set<String> = ["com.apple.UserNotificationCenter", "com.apple.SecurityAgent"]

/// Names of the system prompt hosts with an onscreen window. Window owners are listed
/// without Screen Recording permission.
func systemPrompts() -> [String] {
  guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID)
    as? [[String: Any]]
  else { return [] }
  let owners = Set(windows.compactMap { $0[kCGWindowOwnerPID as String] as? Int32 })
  return owners.compactMap { pid in
    guard let app = NSRunningApplication(processIdentifier: pid),
      let bundle = app.bundleIdentifier, systemPromptHosts.contains(bundle)
    else { return nil }
    return app.localizedName ?? bundle
  }.sorted()
}

final class HandleStore {
  struct Entry {
    let pid: pid_t
    let element: AXUIElement
    let created: Date
  }
  private var values: [String: Entry] = [:]
  func put(_ element: AXUIElement, pid: pid_t) -> String {
    values = values.filter { Date().timeIntervalSince($0.value.created) < 300 }
    let handle = UUID().uuidString.lowercased()
    values[handle] = Entry(pid: pid, element: element, created: Date())
    return handle
  }
  func get(_ handle: String, pid: pid_t) throws -> AXUIElement {
    guard let entry = values[handle], entry.pid == pid,
      Date().timeIntervalSince(entry.created) < 300
    else {
      throw HelperError.invalid("unknown, expired, or PID-mismatched element handle")
    }
    return entry.element
  }
  func clear() { values.removeAll() }
}

@MainActor
final class StationHelper {
  struct TrackedApplication {
    let pid: pid_t
    let identity: ProcessIdentity
    let executableDirectory: String
    let executableCode: ExecutableCode?
  }
  /// The bound lease's state: what it may launch and what binds it.
  struct LeaseState {
    let binding: LeaseBinding
    let policy: LaunchPolicy
  }
  let config: Configuration
  let handles = HandleStore()
  let ledger: LeaseLedger
  private var tracked: [pid_t: TrackedApplication] = [:]
  /// Directly spawned apps, retained only so Foundation reaps them when they exit.
  private var children: [pid_t: Process] = [:]
  private var lease: LeaseState?
  /// The private source for typed text and key presses, delivered to one process only.
  private let eventSource = CGEventSource(stateID: .privateState)
  /// Identifies the lease binding across connections; changes on bind and release.
  private(set) var generation = UUID().uuidString.lowercased()
  let allowedActions: Set<String> = [
    kAXPressAction as String, kAXConfirmAction as String,
    kAXCancelAction as String, kAXShowMenuAction as String,
  ]
  init(config: Configuration, ledger: LeaseLedger) {
    self.config = config
    self.ledger = ledger
  }

  /// Prepares the helper for a new connection. A connection that ended left the lease's
  /// binding, its ledger, its applications and their
  /// element handles in place for the next connection to adopt or for a release to end.
  func beginConnection() {
    children = children.filter { $0.value.isRunning }
  }

  func status() -> [String: Any] {
    let state = SessionState.read()
    var missing: [String] = []
    if state.uid == 0 { missing.append("helper is running as root") }
    if state.uid != config.stationUID {
      missing.append("helper UID does not match configured station")
    }
    if state.guiUID != Int(config.stationUID) || !state.loginDone {
      missing.append("station Aqua GUI session is absent")
    }
    if let issue = sessionEnvironmentIssue(config, screenLocked: state.screenLocked) {
      missing.append(issue)
    }
    let prompts = systemPrompts()
    if !prompts.isEmpty {
      missing.append(
        "a system prompt is waiting on the console (\(prompts.joined(separator: ", "))); the owner"
          + " answers it in the VM viewer, then recovers the station")
    }
    if !state.accessibility { missing.append("Accessibility permission is missing") }
    if !state.screenCapture { missing.append("Screen Recording permission is missing") }
    if !state.eventPosting { missing.append("event-posting permission is missing") }
    if let issue = leftoverIssue { missing.append(issue) }
    let clean = tracked.isEmpty && ledger.isClean
    return [
      "configured_control_uid": Int(config.controlUID),
      "configured_station_uid": Int(config.stationUID),
      "generation": generation, "clean": clean,
      "environment": config.environment.rawValue,
      "lease_bound": lease != nil,
      "session": state.json(), "ready": missing.isEmpty, "missing": missing,
    ]
  }

  /// Applications the bound lease tracks, requested under a different binding. They
  /// prevent a new binding as leftovers do: the lease that opened them ends them.
  private var heldLeaseIssue: String? {
    guard !tracked.isEmpty else { return nil }
    return "another lease's applications are still open ("
      + tracked.values.map { "pid \($0.pid) \($0.identity.executable)" }.sorted()
        .joined(separator: ", ")
      + "); its release or an owner recovery ends them"
  }

  /// Ledger entries this helper process does not track: an earlier helper process left
  /// them unverified. A release verifies them gone; nothing new starts first.
  private var leftoverIssue: String? {
    let leftovers = ledger.entries(excluding: tracked.values.map(\.identity))
    guard !leftovers.isEmpty else { return nil }
    return "earlier helper lease left applications not verified gone ("
      + leftovers.joined(separator: ", ")
      + "); a release or owner recovery verifies them once they have stopped"
  }

  private func trackedTarget(_ pid: pid_t) throws -> NSRunningApplication {
    let app = try requireTarget(pid, config: config)
    guard let tracked = tracked[pid] else {
      throw HelperError.invalid("target PID \(pid) is not tracked by this station lease")
    }
    guard let current = ProcessIdentity.read(pid) else {
      throw HelperError.invalid("target PID \(pid) has an unreadable current process identity")
    }
    guard current.isSameProcess(as: tracked.identity) else {
      throw HelperError.invalid(
        "target PID \(pid) changed kernel identity; expected UID \(tracked.identity.uid), start "
          + "\(tracked.identity.startSeconds).\(tracked.identity.startMicroseconds), current UID "
          + "\(current.uid), start \(current.startSeconds).\(current.startMicroseconds)")
    }
    if !isBundleExecutable(current.executable, in: tracked.executableDirectory) {
      guard tracked.executableCode?.matches(pid: pid) == true,
        ProcessIdentity.read(pid)?.isSameProcess(as: tracked.identity) == true
      else {
        throw HelperError.invalid(
          "target PID \(pid) executable \(current.executable) is outside its launched bundle "
            + "directory \(tracked.executableDirectory) and does not match its original signed code")
      }
    }
    return app
  }

  private func activateTarget(_ pid: pid_t) async throws {
    try requireStationSession(config)
    let target = try trackedTarget(pid)
    if target.isActive { return }
    guard target.activationPolicy != .prohibited, target.activate(options: []) else {
      throw HelperError.operation("target application could not request activation")
    }
    let deadline = Date().addingTimeInterval(5)
    while true {
      try Task.checkCancellation()
      try requireStationSession(config)
      if (try trackedTarget(pid)).isActive { return }
      guard Date() < deadline else {
        throw HelperError.operation("target application did not become active in the desktop session")
      }
      try await Task.sleep(for: .milliseconds(50))
    }
  }

  /// Binds the helper to one lease. A connection presenting the current binding adopts
  /// it, keeping its applications, its ledger and its element handles;
  /// a different binding is refused while that lease still has applications open, and
  /// otherwise replaces it.
  func bindLease(_ requested: LeaseBinding) throws -> [String: Any] {
    try requireStationSession(config)
    if let current = lease, current.binding == requested {
      // Recheck the station account's directories, as each launch does.
      guard try LaunchPolicy.load(binding: requested) == current.policy else {
        throw HelperError.unavailable("lease directories changed")
      }
      return ["lease_bound": true, "adopted": true]
    }
    if let issue = heldLeaseIssue { throw HelperError.unavailable(issue) }
    if let issue = leftoverIssue { throw HelperError.unavailable(issue) }
    handles.clear()
    generation = UUID().uuidString.lowercased()
    lease = LeaseState(binding: requested, policy: try LaunchPolicy.load(binding: requested))
    return ["lease_bound": true, "adopted": false]
  }

  func launch(path: String, mode: AppLaunchMode) async throws -> [String: Any] {
    try requireStationSession(config)
    guard let policy = lease?.policy else {
      throw HelperError.unavailable("owner must bind a lease before launching apps")
    }
    // Recheck the station account's home and Darwin temporary directory before each launch.
    guard try LaunchPolicy.load(binding: policy.binding) == policy
    else { throw HelperError.unavailable("lease directories changed") }
    guard path.hasPrefix("/"), path.hasSuffix(".app") else {
      throw HelperError.invalid("path must be an absolute .app bundle")
    }
    let url = URL(fileURLWithPath: path).standardizedFileURL
    guard FileManager.default.isReadableFile(atPath: url.path),
      (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
    else {
      throw HelperError.invalid("path must be a readable absolute .app bundle")
    }
    guard let bundle = Bundle(url: url), let bundleExecutable = bundle.executableURL else {
      throw HelperError.invalid("app bundle has no executable")
    }
    try mode.validate(bundle: url)
    // A bundle's main executable may hand off to a sibling beside it: Xcode's DeviceHub
    // runs DevicesTrampoline, which becomes DeviceHub. The launch is therefore verified
    // and tracked against the bundle's own executable directory, and never wider.
    let executables = try LaunchPolicy.resolvedPath(
      url.appendingPathComponent("Contents/MacOS").path)
    let mainExecutable = try LaunchPolicy.resolvedPath(bundleExecutable.path)
    guard isBundleExecutable(mainExecutable, in: executables) else {
      throw HelperError.invalid("app bundle's executable is not its own Contents/MacOS")
    }
    // Production Chrome can relocate its code-signing backing file to a temporary
    // clone without changing process identity. Permit only the exact launched code,
    // never a temporary-path prefix or another application from the same publisher.
    let executableCode = ExecutableCode(path: mainExecutable)
    let baseline = Set(NSWorkspace.shared.runningApplications.map(\.processIdentifier))
    // Refusing a running app also makes every station process running one of this
    // bundle's executables, from here until verified release, part of this launch.
    if try ProcessIdentity.all(uid: getuid())
      .contains(where: { isBundleExecutable($0.executable, in: executables) })
      || bundle.bundleIdentifier.map({
        NSRunningApplication.runningApplications(withBundleIdentifier: $0).contains {
          !$0.isTerminated
        }
      }) == true
    {
      throw HelperError.invalid("application is already running; refusing to reuse it")
    }
    try ledger.beginLaunch(directory: executables)
    // First launch can initialize app caches before registering (Blender takes over five
    // seconds in a fresh VM). Leave time for failure cleanup within the station relay's
    // 30-second request deadline.
    let deadline = Date().addingTimeInterval(20)
    let app: NSRunningApplication
    let identity: ProcessIdentity
    let reply = LaunchReply()
    let throughLaunchServices = mode == .launchServices
    var launchedIdentity: ProcessIdentity?
    do {
      let pid: pid_t
      if throughLaunchServices {
        // Normal macOS launch: the application is responsible for its own protected
        // data. Inheriting the helper's identity makes Chrome's profile inaccessible.
        pid = try await reply.open(
          url, configuration: openConfiguration(policy), seconds: deadline.timeIntervalSinceNow
        )
      } else {
        // A direct child keeps the policy's environment, and TCC treats the helper as its
        // responsible process, so it inherits the helper's Network Volumes grant.
        let child = Process()
        child.executableURL = URL(fileURLWithPath: mainExecutable)
        child.arguments = []
        child.environment = policy.environment
        child.currentDirectoryURL = URL(fileURLWithPath: policy.home)
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        try child.run()
        children[child.processIdentifier] = child
        pid = child.processIdentifier
      }
      guard pid > 0 else {
        throw HelperError.operation("launch returned invalid PID \(pid)")
      }
      guard !baseline.contains(pid) else {
        throw HelperError.operation("launch returned pre-existing PID \(pid)")
      }
      let identityClock = ContinuousClock()
      let identityDeadline =
        identityClock.now
        + .milliseconds(min(1_000, max(1, Int(deadline.timeIntervalSinceNow * 1_000))))
      var launched: ProcessIdentity?
      repeat {
        try Task.checkCancellation()
        try requireStationSession(config)
        if let found = ProcessIdentity.read(pid) {
          launched = found
          break
        }
        guard processUID(pid) == getuid() else {
          throw HelperError.operation("launch PID \(pid) exited or left the station account")
        }
        guard identityClock.now < identityDeadline else { break }
        try await Task.sleep(for: .milliseconds(25))
      } while true
      guard let launched else {
        throw HelperError.operation("launch returned PID \(pid), but its identity could not be read")
      }
      guard launched.uid == getuid() else {
        throw HelperError.operation(
          "launch returned PID \(pid) for UID \(launched.uid), not station UID \(getuid())")
      }
      launchedIdentity = launched
      // Persist the returned kernel process before GUI registration: an early code
      // relocation must not let a failed launch or helper restart forget it.
      try ledger.identifyLaunch(launched)
      var verified: (NSRunningApplication, ProcessIdentity)?
      var lastRegistrationState = "identity unavailable"
      while Date() < deadline {
        try Task.checkCancellation()
        try requireStationSession(config)
        if let current = ProcessIdentity.read(pid) {
          guard current.isSameProcess(as: launched) else {
            throw HelperError.operation("launch PID \(pid) changed identity before GUI registration")
          }
          let authorized = current.isAuthorizedBundleProcess(
            as: launched, executableDirectory: executables)
            || executableCode?.matches(pid: pid) == true
          let candidate = NSRunningApplication(processIdentifier: pid)
          lastRegistrationState =
            "executable \(current.executable), code_authorized=\(authorized), "
            + "registered=\(candidate != nil), terminated="
            + (candidate.map { String($0.isTerminated) } ?? "unavailable")
            + ", activation_policy="
            + (candidate.map { String(describing: $0.activationPolicy) } ?? "unavailable")
            + ", finished="
            + (candidate.map { String($0.isFinishedLaunching) } ?? "unavailable")
          // Some valid applications never post NSApplicationDidFinishLaunchingNotification,
          // so `finished` is diagnostic only. Exact kernel identity, authorized code,
          // and activatable AppKit registration define a usable target.
          if authorized, let candidate, !candidate.isTerminated,
            candidate.activationPolicy != .prohibited,
            ProcessIdentity.read(pid)?.isSameProcess(as: launched) == true
          {
            verified = (candidate, current)
            break
          }
        } else {
          // proc_pidpath can be temporarily unreadable while a launch trampoline execs
          // its sibling. The same outer deadline still bounds this retry.
          guard processUID(pid) == getuid() else {
            throw HelperError.operation("launch PID \(pid) exited before GUI registration")
          }
          lastRegistrationState = "station process exists, but its executable is unreadable"
        }
        try await Task.sleep(for: .milliseconds(50))
      }
      guard let verified else {
        throw HelperError.operation(
          "launch PID \(pid) did not register a live application from the requested bundle; "
            + "last observed: \(lastRegistrationState)")
      }
      (app, identity) = verified
    } catch {
      // Without LaunchServices' answer the app may still start; its entry stays for
      // release to verify.
      await stopFailedLaunch(
        executables, identity: launchedIdentity,
        keepEntry: throughLaunchServices && !reply.answered)
      throw error
    }
    let pid = identity.pid
    tracked[pid] = TrackedApplication(
      pid: pid, identity: identity, executableDirectory: executables,
      executableCode: executableCode)
    try ledger.record(identity)
    // A direct launch bypasses LaunchServices activation. AppKit custom views can discard
    // left mouse events while their application is inactive.
    try await activateTarget(pid)
    try requireStationSession(config)
    return [
      "pid": Int(pid), "bundle_id": app.bundleIdentifier as Any, "path": url.path,
      "launch_mode": mode.rawValue,
    ]
  }

  /// Stops the returned process even after relocation, plus the bundle's own executables
  /// in case its main executable handed off. Drops the pending entry only once all are
  /// gone; a survivor keeps the entry for release to verify.
  private func stopFailedLaunch(
    _ executables: String, identity: ProcessIdentity?, keepEntry: Bool
  ) async {
    func survivors() throws -> [ProcessIdentity] {
      try ProcessIdentity.all(uid: getuid())
        .filter { process in
          isBundleExecutable(process.executable, in: executables)
            || identity.map { process.isSameProcess(as: $0) } == true
        }
    }
    for signal in [SIGTERM, SIGKILL] {
      guard let found = try? survivors() else { return }
      for survivor in found where ProcessIdentity.read(survivor.pid) == survivor {
        _ = kill(survivor.pid, signal)
      }
      // The request may already be cancelled; wait regardless.
      for _ in 0..<10 {
        guard let remaining = try? survivors() else { return }
        if remaining.isEmpty { break }
        await Task.detached { try? await Task.sleep(for: .milliseconds(100)) }.value
      }
    }
    if !keepEntry, let processes = try? ProcessIdentity.all(uid: getuid()) {
      try? ledger.abandonLaunch(processes: processes)
    }
  }

  /// A new instance with the policy's variables, no arguments and no user prompts. launchd
  /// starts the app from the session's environment, so it is not confined to them, and the
  /// app is its own TCC responsible process.
  private func openConfiguration(_ policy: LaunchPolicy) -> NSWorkspace.OpenConfiguration {
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.createsNewApplicationInstance = true
    configuration.allowsRunningApplicationSubstitution = false
    configuration.activates = true
    configuration.addsToRecentItems = false
    configuration.promptsUserIfNeeded = false
    configuration.arguments = []
    configuration.environment = policy.environment
    return configuration
  }

  func listApps() throws -> [String: Any] {
    try requireStationSession(config)
    let apps = tracked.values.compactMap { item -> [String: Any]? in
      guard let app = try? trackedTarget(item.pid) else { return nil }
      return [
        "pid": Int(app.processIdentifier), "bundle_id": app.bundleIdentifier as Any,
        "name": app.localizedName as Any, "active": app.isActive,
      ]
    }
    return ["apps": apps]
  }

  func listWindows(pid: pid_t?) async throws -> [String: Any] {
    try requireStationSession(config)
    if let pid { _ = try trackedTarget(pid) }
    guard CGPreflightScreenCaptureAccess() else {
      throw HelperError.unavailable("Screen Recording permission is missing")
    }
    let content = try await SCShareableContent.excludingDesktopWindows(
      false, onScreenWindowsOnly: false)
    let windows = content.windows.compactMap { window -> [String: Any]? in
      guard let app = window.owningApplication, pid == nil || app.processID == pid,
        (try? trackedTarget(app.processID)) != nil
      else { return nil }
      return [
        "window_id": Int(window.windowID), "pid": Int(app.processID),
        "title": String((window.title ?? "").prefix(512)),
        "bundle_id": app.bundleIdentifier, "x": Int(window.frame.origin.x),
        "y": Int(window.frame.origin.y),
        "width": Int(window.frame.width), "height": Int(window.frame.height),
      ]
    }
    return ["windows": windows]
  }

  func inspect(pid: pid_t, depth: Int, maximum: Int, handle: String? = nil) throws -> [String: Any] {
    _ = try trackedTarget(pid)
    guard AXIsProcessTrusted() else {
      throw HelperError.unavailable("Accessibility permission is missing")
    }
    let rootElement = try handle.map { try handles.get($0, pid: pid) }
      ?? AXUIElementCreateApplication(pid)
    let clock = ContinuousClock()
    let deadline = clock.now + .seconds(3)
    var budget = InspectionBudget(maximumElements: maximum) { clock.now >= deadline }
    func canInspect(_ element: AXUIElement) throws -> Bool {
      try Task.checkCancellation()
      guard budget.hasTime() else { return false }
      // Keep one unresponsive element from consuming the request's entire budget.
      let components = (deadline - clock.now).components
      let remaining = Double(components.seconds) + Double(components.attoseconds) / 1e18
      _ = AXUIElementSetMessagingTimeout(element, Float(max(0.001, min(0.25, remaining))))
      return true
    }

    func recordAXFailure(_ result: AXError, operation: String, root: Bool) throws {
      guard result != .success else { return }
      if root, result == .apiDisabled {
        throw HelperError.unavailable("Accessibility API became disabled while \(operation)")
      }
      if root, result == .invalidUIElement {
        throw HelperError.operation("target accessibility root became invalid while \(operation)")
      }
      budget.observedAXFailure(timedOut: result == .cannotComplete)
    }
    func embeddedAXError(_ value: Any) -> AXError? {
      let object = value as CFTypeRef
      guard CFGetTypeID(object) == AXValueGetTypeID() else { return nil }
      let encoded = unsafeBitCast(object, to: AXValue.self)
      guard AXValueGetType(encoded) == .axError else { return nil }
      var error = AXError.success
      return AXValueGetValue(encoded, .axError, &error) ? error : nil
    }

    func visit(_ element: AXUIElement, level: Int) throws -> [String: Any]? {
      try Task.checkCancellation()
      guard budget.beginElement() else { return nil }
      let handle = handles.put(element, pid: pid)
      var node: [String: Any] = [
        "handle": handle, "role": "", "title": NSNull(), "identifier": NSNull(),
      ]

      if try canInspect(element) {
        let attributes: [CFString] = [
          kAXRoleAttribute as CFString, kAXTitleAttribute as CFString,
          kAXIdentifierAttribute as CFString, kAXValueAttribute as CFString,
        ]
        var values: CFArray?
        let attributesResult = AXUIElementCopyMultipleAttributeValues(
          element, attributes as CFArray, [], &values)
        try Task.checkCancellation()
        try recordAXFailure(attributesResult, operation: "reading AX attributes", root: level == 0)
        if attributesResult == .success, let values = values as? [Any] {
          for (index, value) in values.enumerated() {
            guard let error = embeddedAXError(value) else { continue }
            if index > 0, error == .attributeUnsupported || error == .noValue
              || error == .notImplemented
            {
              continue
            }
            try recordAXFailure(error, operation: "reading AX attributes", root: level == 0)
          }
          if values.indices.contains(0), let role = primitive(values[0] as CFTypeRef) {
            node["role"] = role
          }
          if values.indices.contains(1), let title = primitive(values[1] as CFTypeRef) {
            node["title"] = title
          }
          if values.indices.contains(2), let identifier = primitive(values[2] as CFTypeRef) {
            node["identifier"] = identifier
          }
          if values.indices.contains(3), let value = primitive(values[3] as CFTypeRef) {
            node["value"] = value
          }
          if values.count != attributes.count {
            budget.observedAXFailure(timedOut: false)
          }
        } else if attributesResult == .success {
          budget.observedAXFailure(timedOut: false)
        }
      }

      var actions: CFArray?
      if try canInspect(element) {
        let actionsResult = AXUIElementCopyActionNames(element, &actions)
        try Task.checkCancellation()
        switch actionsResult {
        case .success: node["actions"] = actions as? [String] ?? []
        case .attributeUnsupported, .noValue, .notImplemented: break
        default:
          try recordAXFailure(actionsResult, operation: "reading AX actions", root: level == 0)
        }
      }

      guard try canInspect(element) else { return node }
      var childCount: CFIndex = 0
      let childCountResult = AXUIElementGetAttributeValueCount(
        element, kAXChildrenAttribute as CFString, &childCount)
      try Task.checkCancellation()
      if childCountResult != .success {
        if childCountResult != .attributeUnsupported, childCountResult != .noValue,
          childCountResult != .notImplemented
        {
          try recordAXFailure(
            childCountResult, operation: "counting AX children", root: level == 0)
        }
        _ = budget.hasTime()
        return node
      }
      guard budget.hasTime() else { return node }
      guard childCount > 0 else { return node }
      if level >= depth {
        budget.observedChildrenAtDepthLimit(Int(childCount))
        return node
      }

      let visitable = budget.visitableChildCount(observed: Int(childCount))
      guard visitable > 0, try canInspect(element) else { return node }
      var childValues: CFArray?
      let childrenResult = AXUIElementCopyAttributeValues(
        element, kAXChildrenAttribute as CFString, 0, visitable, &childValues)
      try Task.checkCancellation()
      guard childrenResult == .success else {
        if childrenResult != .attributeUnsupported, childrenResult != .noValue,
          childrenResult != .notImplemented
        {
          try recordAXFailure(
            childrenResult, operation: "reading AX children", root: level == 0)
        }
        _ = budget.hasTime()
        return node
      }
      guard budget.hasTime() else { return node }
      guard let children = childValues as? [AXUIElement] else {
        budget.observedAXFailure(timedOut: false)
        return node
      }
      if children.count != visitable { budget.observedAXFailure(timedOut: false) }
      var nodes: [[String: Any]] = []
      for child in children {
        try Task.checkCancellation()
        guard let childNode = try visit(child, level: level + 1) else { break }
        nodes.append(childNode)
      }
      if !nodes.isEmpty { node["children"] = nodes }
      return node
    }
    let root: [String: Any]
    if let visited = try visit(rootElement, level: 0) {
      root = visited
    } else {
      budget.includeRootSkeleton()
      root = [
        "handle": handles.put(rootElement, pid: pid), "role": "", "title": NSNull(),
        "identifier": NSNull(),
      ]
    }
    return [
      "pid": Int(pid), "count": budget.count, "truncated": budget.truncated,
      "truncation_reasons": budget.reasonNames, "tree": root,
    ]
  }

  func action(pid: pid_t, handle: String, action: String) throws -> [String: Any] {
    _ = try trackedTarget(pid)
    guard AXIsProcessTrusted() else {
      throw HelperError.unavailable("Accessibility permission is missing")
    }
    guard allowedActions.contains(action) else {
      throw HelperError.invalid("unsupported AX action")
    }
    let element = try handles.get(handle, pid: pid)
    _ = AXUIElementSetMessagingTimeout(element, 1)
    var available: CFArray?
    guard AXUIElementCopyActionNames(element, &available) == .success,
      (available as? [String] ?? []).contains(action)
    else { throw HelperError.invalid("action is unavailable on this element") }
    try axRequired(AXUIElementPerformAction(element, action as CFString), action)
    return ["pid": Int(pid), "handle": handle, "action": action]
  }

  func focus(pid: pid_t, handle: String) throws -> [String: Any] {
    _ = try trackedTarget(pid)
    guard AXIsProcessTrusted() else {
      throw HelperError.unavailable("Accessibility permission is missing")
    }
    let element = try handles.get(handle, pid: pid)
    _ = AXUIElementSetMessagingTimeout(element, 1)
    try axRequired(
      AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue),
      "AXFocused")
    return ["pid": Int(pid), "handle": handle]
  }

  func type(pid: pid_t, text: String, handle: String?) async throws -> [String: Any] {
    _ = try trackedTarget(pid)
    guard text.utf8.count <= maximumInputBytes, !text.isEmpty else {
      throw HelperError.invalid("text must contain 1–4096 UTF-8 bytes")
    }
    try await activateTarget(pid)
    if let handle { _ = try focus(pid: pid, handle: handle) }
    guard CGPreflightPostEventAccess() else {
      throw HelperError.unavailable("event-posting permission is missing")
    }
    for character in text {
      var units = Array(String(character).utf16)
      for down in [true, false] {
        _ = try trackedTarget(pid)
        guard let event = CGEvent(keyboardEventSource: eventSource, virtualKey: 0, keyDown: down)
        else {
          throw HelperError.operation("could not create keyboard event")
        }
        event.flags = []
        event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        event.postToPid(pid)
        try await Task.sleep(for: .milliseconds(1))
      }
    }
    return ["pid": Int(pid), "bytes": text.utf8.count]
  }

  private func targetWindow(pid: pid_t, windowID: CGWindowID) async throws -> SCWindow {
    _ = try trackedTarget(pid)
    guard CGPreflightScreenCaptureAccess() else {
      throw HelperError.unavailable("Screen Recording permission is missing")
    }
    let content = try await SCShareableContent.excludingDesktopWindows(
      false, onScreenWindowsOnly: false)
    guard
      let window = content.windows.first(where: {
        $0.windowID == windowID && $0.owningApplication?.processID == pid
      }),
      window.frame.width > 0, window.frame.height > 0,
      window.frame.width * window.frame.height <= 16_000_000
    else {
      throw HelperError.invalid(
        "window is absent, does not belong to the target, or exceeds 16 megapixels")
    }
    return window
  }

  /// Holds `modifiers` down for the duration of `body` and releases them however it ends,
  /// passing it the flags every event it posts must carry. Some applications (including
  /// Blender) track modifiers from flagsChanged events instead of reading only the
  /// following event's flags, so a held modifier has to be a real key press.
  private func withModifiers<T>(
    _ modifiers: [String], pid: pid_t, _ body: (CGEventFlags) async throws -> T
  ) async throws -> T {
    let keys = try heldModifiers(modifiers)
    var pressed: [(key: CGKeyCode, flag: CGEventFlags)] = []
    var flags: CGEventFlags = []
    // The modifier is pressed for the one process, like every key the helper sends, and
    // the defer below lifts it however the call ends. Whether an application reads it
    // from a pointer event is its own affair: TextEdit's shift-click has not, whether
    // the press went to the process, to the session or to the HID level.
    func postModifier(_ key: CGKeyCode, down: Bool, flags: CGEventFlags) throws {
      _ = try trackedTarget(pid)
      guard let event = CGEvent(keyboardEventSource: eventSource, virtualKey: key, keyDown: down)
      else {
        throw HelperError.operation("could not create modifier event")
      }
      event.type = .flagsChanged
      event.flags = flags
      event.postToPid(pid)
    }
    defer {
      for (key, flag) in pressed.reversed() {
        flags.subtract(flag)
        try? postModifier(key, down: false, flags: flags)
      }
    }
    for (key, flag) in keys {
      flags.formUnion(flag)
      try postModifier(key, down: true, flags: flags)
      pressed.append((key, flag))
    }
    return try await body(flags)
  }

  func pressKey(pid: pid_t, keyCode: Int, modifiers: [String]) async throws -> [String: Any] {
    guard (0...127).contains(keyCode) else {
      throw HelperError.invalid("key_code must be 0–127")
    }
    _ = try heldModifiers(modifiers)
    try await activateTarget(pid)
    guard CGPreflightPostEventAccess() else {
      throw HelperError.unavailable("event-posting permission is missing")
    }
    try await withModifiers(modifiers, pid: pid) { flags in
      for down in [true, false] {
        _ = try trackedTarget(pid)
        guard
          let event = CGEvent(
            keyboardEventSource: eventSource, virtualKey: CGKeyCode(keyCode), keyDown: down)
        else { throw HelperError.operation("could not create keyboard event") }
        event.flags = flags
        event.postToPid(pid)
      }
    }
    return ["pid": Int(pid), "key_code": keyCode, "modifiers": modifiers]
  }

  private func requirePoint(_ point: CGPoint, in window: SCWindow) throws {
    guard point.x.isFinite, point.y.isFinite, point.x >= 0, point.y >= 0,
      point.x < window.frame.width, point.y < window.frame.height
    else {
      throw HelperError.invalid("point must be inside the current top-left window bounds")
    }
  }

  /// Posts one pointer event, built as an AppKit event: an application accepts that as
  /// a click where a bare CGEvent, from any source, is not one. The request's point is
  /// in the window's own top-left coordinates, the ones its capture is measured in; the
  /// AppKit event takes it flipped against the screen with the window's number, and its
  /// conversion lands it on that window.
  private func postMouse(
    _ type: CGEventType, point: CGPoint, window: SCWindow, pid: pid_t,
    button: CGMouseButton = .left, clickCount: Int64 = 1, flags: CGEventFlags = [],
    releasing: Bool = false
  ) throws {
    if !releasing { try Task.checkCancellation() }
    _ = try trackedTarget(pid)
    guard CGPreflightPostEventAccess() else {
      throw HelperError.unavailable("event-posting permission is missing")
    }
    let kinds: [CGEventType: NSEvent.EventType] = [
      .mouseMoved: .mouseMoved, .leftMouseDown: .leftMouseDown, .leftMouseUp: .leftMouseUp,
      .leftMouseDragged: .leftMouseDragged, .rightMouseDown: .rightMouseDown,
      .rightMouseUp: .rightMouseUp,
    ]
    guard let kind = kinds[type], let screen = NSScreen.screens.first,
      let event = NSEvent.mouseEvent(
        with: kind,
        location: CGPoint(x: point.x, y: screen.frame.maxY - point.y),
        modifierFlags: NSEvent.ModifierFlags(rawValue: UInt(flags.rawValue))
          .intersection(.deviceIndependentFlagsMask),
        timestamp: ProcessInfo.processInfo.systemUptime,
        windowNumber: Int(window.windowID), context: nil,
        eventNumber: 0, clickCount: Int(clickCount), pressure: 1),
      let cgEvent = event.cgEvent
    else { throw HelperError.operation("could not create PID-targeted mouse event") }
    cgEvent.flags = flags
    cgEvent.postToPid(pid)
  }

  func click(
    pid: pid_t, windowID: CGWindowID, point: CGPoint, button: String, modifiers: [String],
    hold: Int
  ) async throws -> [String: Any] {
    let types: (down: CGEventType, up: CGEventType, button: CGMouseButton)
    switch button {
    case "left": types = (.leftMouseDown, .leftMouseUp, .left)
    case "right": types = (.rightMouseDown, .rightMouseUp, .right)
    default: throw HelperError.invalid("button must be left or right")
    }
    let window = try await targetWindow(pid: pid, windowID: windowID)
    try requirePoint(point, in: window)
    try await activateTarget(pid)
    try await withModifiers(modifiers, pid: pid) { flags in
      // Custom canvases such as Blender track position from movement events;
      // their button events do not carry coordinates into the application model.
      try postMouse(.mouseMoved, point: point, window: window, pid: pid, flags: flags)
      try await withPressedInput {
        try postMouse(
          types.down, point: point, window: window, pid: pid, button: types.button, flags: flags)
      } release: {
        try postMouse(
          types.up, point: point, window: window, pid: pid, button: types.button, flags: flags,
          releasing: true)
      } body: {
        if hold > 0 { try await Task.sleep(for: .milliseconds(hold)) }
      }
    }
    return [
      "pid": Int(pid), "window_id": Int(windowID), "x": point.x, "y": point.y, "button": button,
      "modifiers": modifiers, "hold_ms": hold,
    ]
  }

  func drag(
    pid: pid_t, windowID: CGWindowID, from: CGPoint, to: CGPoint, modifiers: [String], hold: Int,
    duration: Int?
  ) async throws -> [String: Any] {
    try requireIntentBudget(hold + (duration ?? 0))
    let window = try await targetWindow(pid: pid, windowID: windowID)
    try requirePoint(from, in: window)
    try requirePoint(to, in: window)
    try await activateTarget(pid)
    let distance = hypot(to.x - from.x, to.y - from.y)
    var steps = min(max(Int(ceil(distance / 24)), 1), 32)
    // A requested duration needs enough steps to stay as smooth as the default pacing.
    if let duration { steps = max(steps, Int(ceil(Double(duration) / 12))) }
    try await withModifiers(modifiers, pid: pid) { flags in
      try postMouse(.mouseMoved, point: from, window: window, pid: pid, flags: flags)
      var last = from
      try await withPressedInput {
        try postMouse(.leftMouseDown, point: from, window: window, pid: pid, flags: flags)
      } release: {
        try postMouse(
          .leftMouseUp, point: last, window: window, pid: pid, flags: flags, releasing: true)
      } body: {
        if hold > 0 { try await Task.sleep(for: .milliseconds(hold)) }
        let clock = ContinuousClock()
        let start = clock.now
        for step in 1...steps {
          let fraction = CGFloat(step) / CGFloat(steps)
          let point = CGPoint(
            x: from.x + (to.x - from.x) * fraction, y: from.y + (to.y - from.y) * fraction)
          try postMouse(.leftMouseDragged, point: point, window: window, pid: pid, flags: flags)
          last = point
          if let duration {
            // Sleep to an absolute target so the pacing error does not accumulate.
            try await Task.sleep(until: start + .milliseconds(duration * step / steps), clock: clock)
          } else {
            try await Task.sleep(for: .milliseconds(12))
          }
        }
      }
    }
    return [
      "pid": Int(pid), "window_id": Int(windowID), "from_x": from.x, "from_y": from.y, "to_x": to.x,
      "to_y": to.y, "steps": steps, "modifiers": modifiers, "hold_ms": hold,
      "duration_ms": duration as Any,
    ]
  }

  /// A planned burst of left taps, posted from the one process that holds the event grant.
  /// The measured offsets are the caller's only view of the cadence the burst achieved.
  func tapSequence(pid: pid_t, windowID: CGWindowID, taps: [Tap]) async throws -> [String: Any] {
    let window = try await targetWindow(pid: pid, windowID: windowID)
    for tap in taps { try requirePoint(tap.point, in: window) }
    try await activateTarget(pid)
    let clock = ContinuousClock()
    let start = clock.now
    var target = Duration.zero
    var offsets: [Int] = []
    for tap in taps {
      // Absolute targets, so a slow tap does not push every later one out.
      target += .milliseconds(tap.afterMilliseconds)
      try await Task.sleep(until: start + target, clock: clock)
      try postMouse(.mouseMoved, point: tap.point, window: window, pid: pid)
      try await withPressedInput {
        try postMouse(.leftMouseDown, point: tap.point, window: window, pid: pid)
      } release: {
        try postMouse(.leftMouseUp, point: tap.point, window: window, pid: pid, releasing: true)
      } body: {
        offsets.append(milliseconds(clock.now - start))
      }
    }
    return [
      "pid": Int(pid), "window_id": Int(windowID), "count": taps.count, "offsets_ms": offsets,
    ]
  }

  func scroll(
    pid: pid_t, windowID: CGWindowID, point: CGPoint, deltaX: Int, deltaY: Int, modifiers: [String]
  ) async throws -> [String: Any] {
    guard deltaX != 0 || deltaY != 0, (-1_000...1_000).contains(deltaX),
      (-1_000...1_000).contains(deltaY)
    else {
      throw HelperError.invalid(
        "scroll deltas must be within -1000 and 1000, with at least one nonzero")
    }
    let window = try await targetWindow(pid: pid, windowID: windowID)
    try requirePoint(point, in: window)
    try await activateTarget(pid)
    try await withModifiers(modifiers, pid: pid) { flags in
      try postMouse(.mouseMoved, point: point, window: window, pid: pid, flags: flags)
      try Task.checkCancellation()
      _ = try trackedTarget(pid)
      guard CGPreflightPostEventAccess() else {
        throw HelperError.unavailable("event-posting permission is missing")
      }
      // An AppKit event turned into a scroll, placed like postMouse places a click.
      guard let screen = NSScreen.screens.first,
        let event = NSEvent.mouseEvent(
          with: .mouseMoved,
          location: CGPoint(x: point.x, y: screen.frame.maxY - point.y),
          modifierFlags: NSEvent.ModifierFlags(rawValue: UInt(flags.rawValue))
            .intersection(.deviceIndependentFlagsMask),
          timestamp: ProcessInfo.processInfo.systemUptime,
          windowNumber: Int(window.windowID), context: nil,
          eventNumber: 0, clickCount: 0, pressure: 0),
        let cgEvent = event.cgEvent
      else { throw HelperError.operation("could not create PID-targeted scroll event") }
      cgEvent.type = .scrollWheel
      cgEvent.flags = flags
      cgEvent.setIntegerValueField(.scrollWheelEventDeltaAxis1, value: Int64(deltaY))
      cgEvent.setIntegerValueField(.scrollWheelEventPointDeltaAxis1, value: Int64(deltaY))
      cgEvent.setIntegerValueField(.scrollWheelEventDeltaAxis2, value: Int64(deltaX))
      cgEvent.setIntegerValueField(.scrollWheelEventPointDeltaAxis2, value: Int64(deltaX))
      cgEvent.setIntegerValueField(.scrollWheelEventIsContinuous, value: 1)
      cgEvent.postToPid(pid)
    }
    return [
      "pid": Int(pid), "window_id": Int(windowID), "x": point.x, "y": point.y, "delta_x": deltaX,
      "delta_y": deltaY, "modifiers": modifiers,
    ]
  }

  func capture(pid: pid_t, windowID: CGWindowID) async throws -> [String: Any] {
    let window = try await targetWindow(pid: pid, windowID: windowID)
    let configuration = SCStreamConfiguration()
    configuration.width = Int(window.frame.width)
    configuration.height = Int(window.frame.height)
    configuration.capturesAudio = false
    configuration.showsCursor = false
    let image = try await SCScreenshotManager.captureImage(
      contentFilter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration
    )
    try Task.checkCancellation()
    _ = try trackedTarget(pid)
    guard CGPreflightScreenCaptureAccess() else {
      throw HelperError.unavailable("Screen Recording permission changed during capture")
    }
    guard let data = png(image), data.count <= 11_500_000 else {
      throw HelperError.operation("PNG encoding failed or image exceeds the response limit")
    }
    try Task.checkCancellation()
    return [
      "pid": Int(pid), "window_id": Int(windowID), "width": image.width, "height": image.height,
      "png_base64": data.base64EncodedString(),
    ]
  }

  /// Releases the lease binding: the applications it tracks are closed, and the ledger is
  /// cleared once nothing it names still runs. What only the ledger names — an earlier
  /// helper process's work — is verified gone, never terminated.
  func release() async throws -> [String: Any] {
    handles.clear()
    lease = nil
    generation = UUID().uuidString.lowercased()
    let pending = Array(tracked.values)
    func alive() -> [TrackedApplication] {
      pending.filter { ProcessIdentity.read($0.pid)?.isSameProcess(as: $0.identity) == true }
    }
    for item in alive() { _ = NSRunningApplication(processIdentifier: item.pid)?.terminate() }
    for _ in 0..<20 where !alive().isEmpty { try await Task.sleep(for: .milliseconds(100)) }
    for item in alive() { _ = kill(item.pid, SIGKILL) }
    for _ in 0..<20 where !alive().isEmpty { try await Task.sleep(for: .milliseconds(100)) }
    guard alive().isEmpty else {
      throw HelperError.operation("tracked application did not terminate")
    }
    tracked.removeAll()
    children = children.filter { $0.value.isRunning }
    // Also verifies what an earlier connection or helper process left in the ledger.
    try ledger.clearAfterVerifiedCleanup(processes: ProcessIdentity.all(uid: getuid()))
    return ["clean": true]
  }
}

/// One LaunchServices launch bounded by a deadline; records whether LaunchServices answered.
final class LaunchReply: @unchecked Sendable {
  private let lock = NSLock()
  private var continuation: CheckedContinuation<pid_t, Error>?
  private var launchServicesAnswered = false

  var answered: Bool {
    lock.lock()
    defer { lock.unlock() }
    return launchServicesAnswered
  }

  func open(_ url: URL, configuration: NSWorkspace.OpenConfiguration, seconds: TimeInterval)
    async throws -> pid_t
  {
    try await withCheckedThrowingContinuation { continuation in
      lock.lock()
      self.continuation = continuation
      lock.unlock()
      NSWorkspace.shared.openApplication(at: url, configuration: configuration) { app, error in
        // Snapshot before the async hop: the application's live PID property can
        // change as a trampoline execs its sibling or the application exits.
        self.finish(
          answered: true,
          app.map { .success($0.processIdentifier) }
            ?? .failure(HelperError.operation(
              "LaunchServices could not launch the application: "
                + (error.map { String(describing: $0) } ?? "no application returned"))))
      }
      DispatchQueue.global().asyncAfter(deadline: .now() + max(seconds, 0)) {
        self.finish(answered: false, .failure(HelperError.operation(
          "LaunchServices did not launch the application before its deadline")))
      }
    }
  }

  private func finish(answered: Bool, _ result: Result<pid_t, Error>) {
    lock.lock()
    let pending = continuation
    continuation = nil
    if answered { launchServicesAnswered = true }
    lock.unlock()
    pending?.resume(with: result)
  }
}

func png(_ image: CGImage) -> Data? {
  let data = NSMutableData()
  guard let destination = CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil)
  else { return nil }
  CGImageDestinationAddImage(destination, image, nil)
  return CGImageDestinationFinalize(destination) ? data as Data : nil
}
