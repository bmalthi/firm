import Darwin
import Foundation

struct ProcessEnumerationError: Error, CustomStringConvertible {
  let description = "station process list could not be read completely"
}

typealias ProcessLister = (UnsafeMutableRawPointer?, Int32) -> Int32

/// Whether a resolved executable is one of a bundle's own: a file directly inside
/// `directory`, the bundle's resolved `Contents/MacOS`. A bundle's main executable may
/// hand off to a sibling beside it — Xcode's DeviceHub is `DevicesTrampoline`, which
/// becomes `DeviceHub` — and the process that results is still that bundle's. Nothing
/// deeper and nothing outside that one directory counts.
func isBundleExecutable(_ executable: String, in directory: String) -> Bool {
  guard directory.hasPrefix("/"), executable.hasPrefix(directory + "/") else { return false }
  let name = executable.dropFirst(directory.count + 1)
  return !name.isEmpty && !name.contains("/")
}

/// A process named by pid, uid, resolved executable and kernel start time, so a reused
/// pid is never mistaken for the process it replaced.
struct ProcessIdentity: Equatable {
  let pid: pid_t
  let uid: uid_t
  let executable: String
  let startSeconds: UInt64
  let startMicroseconds: UInt64

  static func read(_ pid: pid_t) -> Self? {
    guard pid > 0 else { return nil }
    func bsd() -> proc_bsdinfo? {
      var info = proc_bsdinfo()
      let size = Int32(MemoryLayout<proc_bsdinfo>.size)
      guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
      return info
    }
    guard let before = bsd(), before.pbi_start_tvsec > 0 else { return nil }
    // SDK PROC_PIDPATHINFO_MAXSIZE is (4 * MAXPATHLEN), not imported into Swift.
    var bytes = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(pid, &bytes, UInt32(bytes.count)) > 0,
      let resolved = realpath(String(cString: bytes), nil)
    else { return nil }
    defer { free(resolved) }
    guard let after = bsd(), before.pbi_uid == after.pbi_uid,
      before.pbi_start_tvsec == after.pbi_start_tvsec,
      before.pbi_start_tvusec == after.pbi_start_tvusec
    else { return nil }
    return Self(pid: pid, uid: after.pbi_uid, executable: String(cString: resolved),
                startSeconds: after.pbi_start_tvsec, startMicroseconds: after.pbi_start_tvusec)
  }

  /// Every process of `uid` that could be identified; one that exits mid-scan is skipped.
  /// A saturated or failed system listing is an error because cleanup must never treat an
  /// incomplete snapshot as proof that a recorded application is gone.
  static func all(
    uid: uid_t, listProcesses: ProcessLister = proc_listallpids
  ) throws -> [Self] {
    let reported = listProcesses(nil, 0)
    guard reported > 0 else { throw ProcessEnumerationError() }
    var capacity = Int(reported) + 64
    guard capacity <= Int(Int32.max) / MemoryLayout<pid_t>.size else {
      throw ProcessEnumerationError()
    }
    for _ in 0..<8 {
      var pids = [pid_t](repeating: 0, count: capacity)
      let count = pids.withUnsafeMutableBytes { buffer in
        listProcesses(buffer.baseAddress, Int32(buffer.count))
      }
      guard count > 0 else { throw ProcessEnumerationError() }
      if Int(count) < pids.count {
        return pids.prefix(Int(count)).compactMap { read($0) }.filter { $0.uid == uid }
      }
      guard capacity <= Int(Int32.max) / (2 * MemoryLayout<pid_t>.size) else {
        throw ProcessEnumerationError()
      }
      capacity *= 2
    }
    throw ProcessEnumerationError()
  }

  func isSameProcess(as other: Self) -> Bool {
    pid == other.pid && uid == other.uid && startSeconds == other.startSeconds
      && startMicroseconds == other.startMicroseconds
  }

  /// Whether this is the same kernel process as `launched` and is currently running one
  /// of the requested bundle's own executables. The executable may change across exec,
  /// but authority never follows a reused PID or an exec outside the bundle directory.
  func isAuthorizedBundleProcess(as launched: Self, executableDirectory: String) -> Bool {
    isSameProcess(as: launched) && isBundleExecutable(executable, in: executableDirectory)
  }
}
