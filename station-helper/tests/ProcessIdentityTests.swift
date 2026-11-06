import Darwin
import Foundation

func expectEnumerationError(_ work: () throws -> Void) {
  do {
    try work()
    fatalError("expected process enumeration error")
  } catch is ProcessEnumerationError {
  } catch {
    fatalError("unexpected error: \(error)")
  }
}

struct IdentityTestFailure: Error, CustomStringConvertible {
  let description: String
}

func waitForIdentity(
  _ pid: pid_t, matching: (ProcessIdentity) -> Bool
) -> ProcessIdentity? {
  let clock = ContinuousClock()
  let deadline = clock.now + .seconds(2)
  repeat {
    if let identity = ProcessIdentity.read(pid), matching(identity) { return identity }
    usleep(1_000)
  } while clock.now < deadline
  return nil
}

func verifyRealExecIdentity() throws {
  let child = Process()
  let input = Pipe()
  child.executableURL = URL(fileURLWithPath: "/bin/sh")
  child.arguments = ["-c", "read ignored; exec /bin/sleep 5"]
  child.standardInput = input
  child.standardOutput = FileHandle.nullDevice
  child.standardError = FileHandle.nullDevice
  try child.run()
  defer {
    try? input.fileHandleForWriting.close()
    if child.isRunning { child.terminate() }
    child.waitUntilExit()
  }
  guard let before = waitForIdentity(child.processIdentifier, matching: {
    $0.executable == "/bin/sh"
  }) else {
    throw IdentityTestFailure(description: "could not observe shell before exec")
  }
  try input.fileHandleForWriting.write(contentsOf: Data("\n".utf8))
  try input.fileHandleForWriting.close()
  guard let after = waitForIdentity(child.processIdentifier, matching: {
    $0.executable == "/bin/sleep"
  }) else {
    throw IdentityTestFailure(description: "could not observe sleep after exec")
  }
  guard after.executable != before.executable, after.isSameProcess(as: before),
    after.startSeconds == before.startSeconds,
    after.startMicroseconds == before.startMicroseconds
  else {
    throw IdentityTestFailure(description: "kernel process identity changed across exec")
  }
}

@main enum Tests {
  static func main() throws {
    guard let own = ProcessIdentity.read(getpid()) else { fatalError("cannot inspect self") }
    assert(own.uid == getuid() && own.pid == getpid())
    assert(own.startSeconds > 0 && own.startMicroseconds < 1_000_000)
    assert(ProcessIdentity.read(getpid()) == own)
    assert(ProcessIdentity.read(-1) == nil)
    let station = try ProcessIdentity.all(uid: getuid())
    assert(station.contains(own) && station.allSatisfy { $0.uid == getuid() })
    let otherAccount = try ProcessIdentity.all(uid: getuid() + 1)
    assert(!otherAccount.contains { $0.pid == own.pid })

    // Cleanup may use only a complete process snapshot. Either syscall phase failing,
    // or a snapshot that stays saturated, must throw rather than look like no processes.
    expectEnumerationError {
      _ = try ProcessIdentity.all(uid: getuid(), listProcesses: { _, _ in 0 })
    }
    var secondCall = 0
    expectEnumerationError {
      _ = try ProcessIdentity.all(uid: getuid(), listProcesses: { buffer, _ in
        secondCall += 1
        return buffer == nil ? 1 : 0
      })
    }
    assert(secondCall == 2)
    var saturatedCalls = 0
    expectEnumerationError {
      _ = try ProcessIdentity.all(uid: getuid(), listProcesses: { buffer, bytes in
        if buffer == nil { return 1 }
        saturatedCalls += 1
        return bytes / Int32(MemoryLayout<pid_t>.size)
      })
    }
    assert(saturatedCalls > 1)

    // A snapshot that grows once and then fits is retried and returned normally.
    var retryCalls = 0
    let retried = try ProcessIdentity.all(uid: getuid(), listProcesses: { buffer, bytes in
      guard let buffer else { return 1 }
      retryCalls += 1
      let capacity = Int(bytes) / MemoryLayout<pid_t>.size
      buffer.bindMemory(to: pid_t.self, capacity: capacity)[0] = getpid()
      return retryCalls == 1 ? Int32(capacity) : 1
    })
    assert(retryCalls == 2 && retried == [own])

    let changed = ProcessIdentity(pid: own.pid, uid: own.uid, executable: own.executable,
      startSeconds: own.startSeconds, startMicroseconds: own.startMicroseconds + 1)
    assert(!changed.isSameProcess(as: own))
    let otherPID = ProcessIdentity(pid: own.pid + 1, uid: own.uid, executable: own.executable,
      startSeconds: own.startSeconds, startMicroseconds: own.startMicroseconds)
    assert(!otherPID.isSameProcess(as: own))
    let otherUID = ProcessIdentity(pid: own.pid, uid: own.uid + 1, executable: own.executable,
      startSeconds: own.startSeconds, startMicroseconds: own.startMicroseconds)
    assert(!otherUID.isSameProcess(as: own))
    let execTransition = ProcessIdentity(pid: own.pid, uid: own.uid, executable: "/another/exe",
      startSeconds: own.startSeconds, startMicroseconds: own.startMicroseconds)
    assert(execTransition.isSameProcess(as: own))
    assert(execTransition != own) // full tracked identity also requires executable match
    try verifyRealExecIdentity()
    // A bundle owns the executables directly inside its own Contents/MacOS, so the
    // sibling a trampoline becomes is still that bundle's; nothing else is.
    let macOS = "/Applications/Xcode.app/Contents/Applications/DeviceHub.app/Contents/MacOS"
    assert(isBundleExecutable(macOS + "/DevicesTrampoline", in: macOS))
    assert(isBundleExecutable(macOS + "/DeviceHub", in: macOS))
    let launched = ProcessIdentity(pid: 42, uid: 502, executable: macOS + "/DevicesTrampoline",
      startSeconds: 100, startMicroseconds: 200)
    let siblingExec = ProcessIdentity(pid: 42, uid: 502, executable: macOS + "/DeviceHub",
      startSeconds: 100, startMicroseconds: 200)
    assert(siblingExec.isAuthorizedBundleProcess(as: launched, executableDirectory: macOS))
    let outsideExec = ProcessIdentity(pid: 42, uid: 502, executable: "/bin/sleep",
      startSeconds: 100, startMicroseconds: 200)
    assert(outsideExec.isSameProcess(as: launched))
    assert(!outsideExec.isAuthorizedBundleProcess(as: launched, executableDirectory: macOS))
    let reusedPID = ProcessIdentity(pid: 42, uid: 502, executable: macOS + "/DeviceHub",
      startSeconds: 101, startMicroseconds: 200)
    assert(!reusedPID.isSameProcess(as: launched))
    assert(!reusedPID.isAuthorizedBundleProcess(as: launched, executableDirectory: macOS))
    for outside in [
      macOS, macOS + "/", macOS + "/Helpers/Inner", macOS + "-other/DeviceHub",
      "/Applications/Xcode.app/Contents/MacOS/Xcode", "/bin/sleep", "",
    ] {
      assert(!isBundleExecutable(outside, in: macOS), outside)
    }
    assert(!isBundleExecutable("relative/DeviceHub", in: "relative"))
    let directory = FileManager.default.temporaryDirectory
      .appendingPathComponent("firm-identity-alias-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: directory) }
    let alias = directory.appendingPathComponent("sleep-alias")
    try FileManager.default.createSymbolicLink(at: alias,
      withDestinationURL: URL(fileURLWithPath: "/bin/sleep"))
    let child = Process()
    child.executableURL = alias
    child.arguments = ["5"]
    try child.run()
    guard let spawned = ProcessIdentity.read(child.processIdentifier) else {
      child.terminate(); child.waitUntilExit(); fatalError("cannot inspect child")
    }
    assert(spawned.uid == getuid() && spawned.executable == "/bin/sleep")
    child.terminate()
    child.waitUntilExit()
    assert(ProcessIdentity.read(child.processIdentifier) == nil)
    print("Process identity tests passed")
  }
}
