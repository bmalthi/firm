import Darwin
import Foundation

enum HelperError: Error { case invalid(String), unavailable(String), operation(String) }

@main enum PeerAuthenticationTests {
  static func main() throws {
    let arguments = CommandLine.arguments
    if arguments.count == 3, arguments[1] == "serve" {
      let endpoint = try UnixSocketListener(path: arguments[2], station: getuid())
      #if IMPOSTOR
      FileHandle.standardOutput.write(Data("impostor\n".utf8))
      #else
      FileHandle.standardOutput.write(Data("helper\n".utf8))
      #endif
      let client = accept(endpoint.descriptor, nil, nil)
      guard client >= 0 else { fatalError("accept failed") }
      var byte: UInt8 = 0
      _ = read(client, &byte, 1)
      close(client)
      return
    }
    let expectNonHardenedRejection = arguments.count == 3 && arguments[2] == "--expect-nonhardened"
    let executable = URL(fileURLWithPath: arguments[0]).standardizedFileURL
    let directory = URL(fileURLWithPath: NSTemporaryDirectory())
      .appendingPathComponent("firm-peer-" + UUID().uuidString.prefix(8))
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
      attributes: [.posixPermissions: 0o755])
    defer { try? FileManager.default.removeItem(at: directory) }
    let servers = expectNonHardenedRejection
      ? [URL(fileURLWithPath: arguments[1]).standardizedFileURL]
      : [executable, URL(fileURLWithPath: arguments[1]).standardizedFileURL]
    for (index, server) in servers.enumerated() {
      let path = directory.appendingPathComponent("socket-\(index)").path
      let process = Process()
      let ready = Pipe()
      process.executableURL = server
      process.arguments = ["serve", path]
      process.standardOutput = ready
      try process.run()
      defer { if process.isRunning { process.terminate() }; process.waitUntilExit() }
      guard !ready.fileHandleForReading.availableData.isEmpty else { fatalError("server failed") }
      let connection = socket(AF_UNIX, SOCK_STREAM, 0)
      guard connection >= 0 else { fatalError("socket failed") }
      defer { close(connection) }
      var address = sockaddr_un()
      address.sun_family = sa_family_t(AF_UNIX)
      address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
      withUnsafeMutableBytes(of: &address.sun_path) {
        $0.copyBytes(from: Array(path.utf8) + [0])
      }
      let connected = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
          connect(connection, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
      }
      guard connected == 0 else { fatalError("connect failed") }
      guard isControlAccountPeer(connection, control: getuid()),
        !isControlAccountPeer(connection, control: getuid() + 1),
        !isControlAccountPeer(-1, control: getuid()) else { fatalError("peer UID gate failed") }
      if expectNonHardenedRejection {
        do {
          try verifyHelperPeer(connection, station: getuid(), executable: server)
          fatalError("Non-hardened helper was accepted")
        } catch HelperError.invalid(let message) {
          guard message == "station helper requires hardened runtime signing" else {
            fatalError("Unexpected non-hardened rejection: \(message)")
          }
        }
        print("Non-hardened helper rejection passed")
        continue
      }
      if index == 0 {
        try verifyHelperPeer(connection, station: getuid(), executable: executable)
        do {
          try verifyHelperPeer(connection, station: getuid() + 1, executable: executable)
          fatalError("Wrong UID was accepted")
        } catch HelperError.invalid { }
      } else {
        do {
          try verifyHelperPeer(connection, station: getuid(), executable: executable)
          fatalError("Same-UID impostor was accepted")
        } catch HelperError.invalid { }
      }
    }
    print("Signed peer authentication and same-UID impostor rejection passed")
  }
}
