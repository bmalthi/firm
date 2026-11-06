import Darwin
import Foundation

/// The lock is deliberately persistent: unlinking it would let two processes lock
/// different inodes. It covers the whole helper lifetime, including ledger reads.
final class UnixSocketListener {
  let descriptor: Int32
  private let directory: Int32
  private let lock: Int32
  private let name: String
  private let identity: stat

  init(path: String, station: uid_t) throws {
    guard path.hasPrefix("/"), path.utf8.count <= 103, !path.utf8.contains(0) else {
      throw HelperError.invalid("socket path must be absolute, at most 103 bytes, and contain no NUL")
    }
    let url = URL(fileURLWithPath: path)
    let parent = url.deletingLastPathComponent().path
    let name = url.lastPathComponent
    let directory = open(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard directory >= 0 else { throw HelperError.invalid("cannot open socket parent") }
    var lock: Int32 = -1
    var listener: Int32 = -1
    var boundIdentity: stat?
    var initialized = false
    defer {
      if !initialized {
        if let boundIdentity { Self.removeIfMatching(directory, name, boundIdentity) }
        if listener >= 0 { close(listener) }
        if lock >= 0 { close(lock) }
        close(directory)
      }
    }
    var parentInfo = stat()
    guard fstat(directory, &parentInfo) == 0, parentInfo.st_uid == station,
      (parentInfo.st_mode & 0o777) == 0o755
    else {
      throw HelperError.invalid("socket parent must be a station-owned directory mode 0755")
    }
    lock = openat(
      directory, ".firm-station-helper.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC,
      mode_t(0o600))
    var lockInfo = stat()
    guard lock >= 0, fstat(lock, &lockInfo) == 0,
      (lockInfo.st_mode & S_IFMT) == S_IFREG, lockInfo.st_uid == station,
      lockInfo.st_nlink == 1, (lockInfo.st_mode & 0o777) == 0o600
    else { throw HelperError.invalid("unsafe station helper lock file") }
    guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
      throw HelperError.unavailable("another station helper owns this socket directory")
    }

    var existing = stat()
    if fstatat(directory, name, &existing, AT_SYMLINK_NOFOLLOW) == 0 {
      guard (existing.st_mode & S_IFMT) == S_IFSOCK, existing.st_uid == station else {
        throw HelperError.invalid("existing socket path is not a station-owned socket")
      }
      // Another process can bind this path without participating in our lock.
      // Only ECONNREFUSED proves the socket is stale; ambiguous failures fail closed.
      let probe = socket(AF_UNIX, SOCK_STREAM, 0)
      guard probe >= 0 else { throw HelperError.operation("socket probe creation failed") }
      defer { close(probe) }
      guard fcntl(probe, F_SETFL, O_NONBLOCK) == 0 else {
        throw HelperError.operation("socket probe setup failed")
      }
      let connected = Self.withAddress(path) { connect(probe, $0, $1) }
      let connectionError = errno
      guard connected < 0, connectionError == ECONNREFUSED else {
        throw HelperError.unavailable("existing socket is live or cannot be safely recovered")
      }
      guard Self.matches(directory, name, existing), unlinkat(directory, name, 0) == 0 else {
        throw HelperError.operation("stale socket changed during recovery")
      }
    } else if errno != ENOENT {
      throw HelperError.operation("cannot inspect existing socket path")
    }

    listener = socket(AF_UNIX, SOCK_STREAM, 0)
    guard listener >= 0, fcntl(listener, F_SETFD, FD_CLOEXEC) == 0 else {
      throw HelperError.operation("socket creation failed")
    }
    guard Self.withAddress(path, { bind(listener, $0, $1) }) == 0 else {
      // A failed bind never owns the path and must never unlink it.
      throw HelperError.operation("socket bind failed: \(String(cString: strerror(errno)))")
    }
    var created = stat()
    guard fstatat(directory, name, &created, AT_SYMLINK_NOFOLLOW) == 0,
      (created.st_mode & S_IFMT) == S_IFSOCK, created.st_uid == station
    else { throw HelperError.operation("cannot identify bound socket") }
    boundIdentity = created
    guard fchmodat(directory, name, 0o666, AT_SYMLINK_NOFOLLOW) == 0,
      listen(listener, 1) == 0
    else { throw HelperError.operation("socket permission/listen setup failed") }
    self.descriptor = listener
    self.directory = directory
    self.lock = lock
    self.name = name
    self.identity = created
    initialized = true
  }

  deinit {
    Self.removeIfMatching(directory, name, identity)
    close(descriptor)
    close(lock)
    close(directory)
  }

  private static func matches(_ directory: Int32, _ name: String, _ expected: stat) -> Bool {
    var actual = stat()
    return fstatat(directory, name, &actual, AT_SYMLINK_NOFOLLOW) == 0
      && actual.st_dev == expected.st_dev && actual.st_ino == expected.st_ino
      && (actual.st_mode & S_IFMT) == S_IFSOCK && actual.st_uid == expected.st_uid
  }

  private static func removeIfMatching(_ directory: Int32, _ name: String, _ expected: stat) {
    if matches(directory, name, expected) { _ = unlinkat(directory, name, 0) }
  }

  private static func withAddress<T>(
    _ path: String, _ body: (UnsafePointer<sockaddr>, socklen_t) -> T
  ) -> T {
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: Array(path.utf8) + [0]) }
    return withUnsafePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        body($0, socklen_t(MemoryLayout<sockaddr_un>.size))
      }
    }
  }
}
