import Darwin
import Foundation

/// The durable record of applications the socket service launched and has not yet
/// verified gone; it outlives a helper crash or a failed cleanup.
///
/// Clearing it never terminates anything. It verifies absence against a snapshot of
/// the station account's processes: a recorded app by pid and kernel start time, or a
/// pending launch by its captured pid and kernel start time when available. Before that
/// capture, it uses the bundle's executable directory because a main executable may
/// hand off to a sibling beside it.
final class LeaseLedger {
  private let url: URL
  private(set) var apps: [ProcessIdentity] = []
  private(set) var launching: String?
  private(set) var launchingIdentity: ProcessIdentity?
  private var unreadable = false

  init(socketPath: String) throws {
    url = URL(fileURLWithPath: socketPath).deletingLastPathComponent().appendingPathComponent(
      ".firm-station-helper-ledger.json")
    guard FileManager.default.fileExists(atPath: url.path) else { return }
    guard let data = try? Data(contentsOf: url),
      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let entries = object["apps"] as? [[String: Any]]
    else {
      unreadable = true
      return
    }
    for entry in entries {
      guard let identity = Self.identity(from: entry) else {
        unreadable = true
        return
      }
      apps.append(identity)
    }
    if let value = object["launching"] {
      guard let value = value as? String, value.hasPrefix("/") else {
        unreadable = true
        return
      }
      launching = value
    }
    if let value = object["launching_identity"] {
      guard launching != nil, let entry = value as? [String: Any],
        let identity = Self.identity(from: entry)
      else {
        unreadable = true
        return
      }
      launchingIdentity = identity
    }
  }

  var isClean: Bool {
    !unreadable && apps.isEmpty && launching == nil && launchingIdentity == nil
  }

  /// What the ledger names beyond `owned`, the apps a live connection still tracks.
  func entries(excluding owned: [ProcessIdentity] = []) -> [String] {
    if unreadable { return ["unreadable ledger \(url.path); replace the firm"] }
    return apps.filter { !owned.contains($0) }.map { "pid \($0.pid) \($0.executable)" }
      + (launching.map { ["unverified launch of \($0)"] } ?? [])
  }

  /// Opens a pending launch, named by the bundle's resolved executable directory: which
  /// of a bundle's executables the process ends up running is not known until it runs.
  func beginLaunch(directory: String) throws {
    guard !unreadable, launching == nil, launchingIdentity == nil else {
      throw HelperError.unavailable(
        "station helper ledger names applications not verified gone: "
          + entries().joined(separator: ", "))
    }
    launching = directory
    try persist()
  }

  /// Persists the returned PID, UID and kernel start time as soon as they are known.
  /// Code authorization is checked before the process can receive GUI control.
  func identifyLaunch(_ identity: ProcessIdentity) throws {
    guard !unreadable, launching != nil, launchingIdentity == nil,
      identity.pid > 0, identity.uid > 0,
      identity.startSeconds > 0, identity.startMicroseconds < 1_000_000
    else {
      throw HelperError.operation("launch ledger identity state is invalid")
    }
    self.launchingIdentity = identity
    try persist()
  }

  /// Records the process after the caller has authorized its executable for this bundle.
  func record(_ identity: ProcessIdentity) throws {
    guard launching != nil, let launchingIdentity,
      identity.isSameProcess(as: launchingIdentity)
    else {
      throw HelperError.operation("launch ledger state is invalid")
    }
    apps.append(identity)
    self.launching = nil
    self.launchingIdentity = nil
    try persist()
  }

  /// Drops a failed launch once no station process runs one of that bundle's executables.
  func abandonLaunch(processes: [ProcessIdentity]) throws {
    guard let launching else { return }
    guard !pendingLaunchIsRunning(in: processes, directory: launching) else {
      throw HelperError.operation("failed launch of \(launching) is still running")
    }
    self.launching = nil
    launchingIdentity = nil
    try persist()
  }

  /// Entries still running in `processes`, the station account's current processes.
  func remaining(in processes: [ProcessIdentity]) -> [String] {
    if unreadable { return entries() }
    let running = apps.filter { app in processes.contains { $0.isSameProcess(as: app) } }
      .map { "pid \($0.pid) \($0.executable)" }
    guard let launching, pendingLaunchIsRunning(in: processes, directory: launching) else {
      return running
    }
    return running + ["unverified launch of \(launching)"]
  }

  private func pendingLaunchIsRunning(in processes: [ProcessIdentity], directory: String) -> Bool {
    if let launchingIdentity,
      processes.contains(where: { $0.isSameProcess(as: launchingIdentity) })
    {
      return true
    }
    return processes.contains(where: { isBundleExecutable($0.executable, in: directory) })
  }

  func clearAfterVerifiedCleanup(processes: [ProcessIdentity]) throws {
    let running = remaining(in: processes)
    guard running.isEmpty else {
      throw HelperError.operation(
        "applications are still running: \(running.joined(separator: ", "))")
    }
    apps.removeAll()
    launching = nil
    launchingIdentity = nil
    try persist()
  }

  private func persist() throws {
    guard !isClean else {
      try? FileManager.default.removeItem(at: url)
      guard !FileManager.default.fileExists(atPath: url.path) else {
        throw HelperError.operation("could not remove the launch ledger")
      }
      return
    }
    var object: [String: Any] = [
      "apps": apps.map { Self.dictionary(from: $0) },
    ]
    if let launching { object["launching"] = launching }
    if let launchingIdentity {
      object["launching_identity"] = Self.dictionary(from: launchingIdentity)
    }
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    try data.write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
  }

  private static func identity(from entry: [String: Any]) -> ProcessIdentity? {
    guard let pid = entry["pid"] as? Int, let uid = entry["uid"] as? Int,
      let executable = entry["executable"] as? String,
      let seconds = entry["start_seconds"] as? UInt64,
      let microseconds = entry["start_microseconds"] as? UInt64,
      let pid = pid_t(exactly: pid), let uid = uid_t(exactly: uid),
      pid > 0, uid > 0, seconds > 0, microseconds < 1_000_000
    else { return nil }
    return ProcessIdentity(pid: pid, uid: uid, executable: executable,
      startSeconds: seconds, startMicroseconds: microseconds)
  }

  private static func dictionary(from identity: ProcessIdentity) -> [String: Any] {
    [
      "pid": Int(identity.pid), "uid": Int(identity.uid), "executable": identity.executable,
      "start_seconds": identity.startSeconds, "start_microseconds": identity.startMicroseconds,
    ]
  }
}
