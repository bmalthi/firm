import Darwin
import Foundation

enum HelperError: Error {
  case invalid(String), unavailable(String), operation(String)
}

func expectFailure(_ action: () throws -> Void) {
  do { try action(); fatalError("expected refusal") } catch {}
}

func pathIdentity(_ path: String) -> ino_t? {
  var info = stat()
  return lstat(path, &info) == 0 ? info.st_ino : nil
}

func rawSocket(_ path: String, listening: Bool) -> Int32 {
  let fd = socket(AF_UNIX, SOCK_STREAM, 0)
  precondition(fd >= 0)
  var address = sockaddr_un()
  address.sun_family = sa_family_t(AF_UNIX)
  withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: Array(path.utf8) + [0]) }
  let result = withUnsafePointer(to: &address) {
    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
      bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
    }
  }
  precondition(result == 0)
  if listening { precondition(listen(fd, 2) == 0) }
  return fd
}

@main enum UnixSocketListenerTests {
  static func main() throws {
    if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "child" {
      let endpoint = try UnixSocketListener(path: CommandLine.arguments[2], station: getuid())
      // The parent kills us to exercise kernel lock release without Swift deinit.
      withExtendedLifetime(endpoint) { while true { pause() } }
      return
    }
    let root = "/private/tmp/firm-socket-\(UUID().uuidString.prefix(8))"
    try FileManager.default.createDirectory(atPath: root, withIntermediateDirectories: false)
    precondition(chmod(root, 0o755) == 0)
    defer { try? FileManager.default.removeItem(atPath: root) }
    let path = root + "/station.sock"

    // A second helper must not unlink the running helper's socket on failed init.
    var first: UnixSocketListener? = try UnixSocketListener(path: path, station: getuid())
    let firstInode = pathIdentity(path)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    expectFailure { _ = try UnixSocketListener(path: root + "/other.sock", station: getuid()) }
    expectFailure { _ = try UnixSocketListener(path: path + "\0ignored", station: getuid()) }
    precondition(pathIdentity(path) == firstInode)
    withExtendedLifetime(first) {}
    first = nil
    precondition(pathIdentity(path) == nil)
    // POSIX APIs truncate at NUL; reject before creating or unlinking the prefix.
    expectFailure { _ = try UnixSocketListener(path: path + "\0ignored", station: getuid()) }
    expectFailure { _ = try UnixSocketListener(path: root + "\0ignored/socket", station: getuid()) }
    precondition(pathIdentity(path) == nil)

    // A live socket owned by a process outside our lock must also survive.
    let independent = rawSocket(path, listening: true)
    let independentInode = pathIdentity(path)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    precondition(pathIdentity(path) == independentInode)
    close(independent)

    // A closed socket is safe to replace. A dirty app ledger is never reset.
    let ledger = try LeaseLedger(socketPath: path)
    try ledger.beginLaunch(directory: "/Applications/One.app/Contents/MacOS")
    let marker = root + "/.firm-station-helper-ledger.json"
    let markerBefore = try Data(contentsOf: URL(fileURLWithPath: marker))
    var recovered: UnixSocketListener? = try UnixSocketListener(path: path, station: getuid())
    let restartedLedger = try LeaseLedger(socketPath: path)
    precondition(!restartedLedger.isClean)
    expectFailure { try restartedLedger.beginLaunch(directory: "/Applications/Two.app/Contents/MacOS") }
    let markerAfter = try Data(contentsOf: URL(fileURLWithPath: marker))
    precondition(markerAfter == markerBefore)
    withExtendedLifetime(recovered) {}
    recovered = nil

    // Actual abrupt termination leaves a socket but releases the lifetime lock.
    for termination in [SIGTERM, SIGKILL] {
      let child = Process()
      child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
      child.arguments = ["child", path]
      try child.run()
      for _ in 0..<200 {
        if pathIdentity(path) != nil { break }
        usleep(10_000)
      }
      precondition(pathIdentity(path) != nil)
      expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
      precondition(kill(child.processIdentifier, termination) == 0)
      child.waitUntilExit()
      precondition(pathIdentity(path) != nil)
      var afterCrash: UnixSocketListener? = try UnixSocketListener(path: path, station: getuid())
      withExtendedLifetime(afterCrash) {}
      afterCrash = nil
    }

    // Refuse files and symlinks without changing them or their target.
    try Data("sentinel".utf8).write(to: URL(fileURLWithPath: path))
    let fileInode = pathIdentity(path)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    precondition(pathIdentity(path) == fileInode)
    try FileManager.default.moveItem(atPath: path, toPath: root + "/target")
    precondition(symlink("target", path) == 0)
    let linkInode = pathIdentity(path)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    precondition(pathIdentity(path) == linkInode)
    precondition((try? String(contentsOfFile: root + "/target", encoding: .utf8)) == "sentinel")
    precondition(unlink(path) == 0)

    // Cleanup only removes the inode it created, even if the path was replaced.
    var replaced: UnixSocketListener? = try UnixSocketListener(path: path, station: getuid())
    precondition(unlink(path) == 0)
    let replacement = rawSocket(path, listening: true)
    let replacementInode = pathIdentity(path)
    withExtendedLifetime(replaced) {}
    replaced = nil
    precondition(pathIdentity(path) == replacementInode)
    close(replacement)
    precondition(unlink(path) == 0)

    let parentLink = root + "/parent-link"
    precondition(symlink(root, parentLink) == 0)
    expectFailure {
      _ = try UnixSocketListener(path: parentLink + "/station.sock", station: getuid())
    }
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid() + 1) }

    let lockPath = root + "/.firm-station-helper.lock"
    precondition(chmod(lockPath, 0o666) == 0)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    precondition(unlink(lockPath) == 0)
    precondition(symlink("target", lockPath) == 0)
    expectFailure { _ = try UnixSocketListener(path: path, station: getuid()) }
    precondition((try? String(contentsOfFile: root + "/target", encoding: .utf8)) == "sentinel")
    print("Unix socket lifecycle tests passed")
  }
}
