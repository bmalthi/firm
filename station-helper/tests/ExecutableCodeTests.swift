import Darwin
import Foundation

struct ExecutableCodeTestFailure: Error, CustomStringConvertible {
  let description: String
}

func waitUntil(_ condition: () -> Bool) -> Bool {
  let clock = ContinuousClock()
  let deadline = clock.now + .seconds(2)
  repeat {
    if condition() { return true }
    usleep(1_000)
  } while clock.now < deadline
  return false
}

func withProcess(
  executable: URL, arguments: [String], _ body: (Process) throws -> Void
) throws {
  let process = Process()
  process.executableURL = executable
  process.arguments = arguments
  process.standardInput = FileHandle.nullDevice
  process.standardOutput = FileHandle.nullDevice
  process.standardError = FileHandle.nullDevice
  try process.run()
  defer {
    if process.isRunning { process.terminate() }
    process.waitUntilExit()
  }
  try body(process)
}

@main enum ExecutableCodeTests {
  static func main() throws {
    if CommandLine.arguments.dropFirst() == ["--wait"] {
      sleep(5)
      return
    }
    guard CommandLine.arguments.count == 2 else {
      throw ExecutableCodeTestFailure(description: "other signed executable path is required")
    }
    let original = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
    guard let expected = ExecutableCode(path: original.path) else {
      throw ExecutableCodeTestFailure(description: "signed test executable was not recognized")
    }
    let directory = FileManager.default.temporaryDirectory
      .appendingPathComponent("firm-executable-code-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: directory) }
    let relocated = directory.appendingPathComponent("relocated-test")
    try FileManager.default.copyItem(at: original, to: relocated)

    try withProcess(executable: relocated, arguments: ["--wait"]) { process in
      guard waitUntil({ expected.matches(pid: process.processIdentifier) }) else {
        throw ExecutableCodeTestFailure(
          description: "relocated identical signed code was not accepted")
      }
    }

    let other = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
    guard let otherCode = ExecutableCode(path: other.path) else {
      throw ExecutableCodeTestFailure(description: "other signed executable was not recognized")
    }
    try withProcess(executable: other, arguments: ["--wait"]) { process in
      guard waitUntil({ otherCode.matches(pid: process.processIdentifier) }) else {
        throw ExecutableCodeTestFailure(description: "could not verify other live executable")
      }
      guard !expected.matches(pid: process.processIdentifier) else {
        throw ExecutableCodeTestFailure(description: "different signed code was accepted")
      }
    }
    guard !expected.matches(pid: -1) else {
      throw ExecutableCodeTestFailure(description: "invalid PID was accepted")
    }
    let unsigned = directory.appendingPathComponent("unsigned")
    try Data("not executable code".utf8).write(to: unsigned)
    guard ExecutableCode(path: unsigned.path) == nil else {
      throw ExecutableCodeTestFailure(description: "unsigned file produced a code requirement")
    }
    print("Exact executable code tests passed")
  }
}
