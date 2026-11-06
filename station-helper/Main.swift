import AppKit
import Darwin
import Foundation

@MainActor final class JSONServer {
  let helper: StationHelper
  let output: FileHandle
  private var pending: [[String: Any]] = []
  private var running = false
  private var closing: (() -> Void)?
  private var current: Task<Void, Never>?
  private var disconnectStarted = false
  private var deadlineTask: Task<Void, Never>?
  var onWriteFailure: (() -> Void)?
  init(helper: StationHelper, output: FileHandle) {
    self.helper = helper
    self.output = output
  }
  func enqueue(_ request: [String: Any]) {
    guard !disconnectStarted, closing == nil else { return }
    guard pending.count < 64 else {
      write([
        "id": request["id"] as Any, "ok": false,
        "error": ["code": "busy", "message": "request queue is full"],
      ])
      return
    }
    pending.append(request)
    if !running { next() }
  }
  /// Ends this connection without releasing the lease binding: its applications stay
  /// open and the next connection presenting the same
  /// binding adopts them. Only a release, on request or through the lease's release,
  /// closes them.
  func disconnected(_ done: @escaping () -> Void) {
    guard !disconnectStarted else { return }
    disconnectStarted = true
    deadlineTask?.cancel()
    deadlineTask = nil
    pending.removeAll()
    current?.cancel()
    closing = done
    finishIfIdle()
  }
  private func finishIfIdle() {
    guard !running, pending.isEmpty, let done = closing else { return }
    closing = nil
    done()
  }
  private func next() {
    guard !pending.isEmpty else {
      running = false
      finishIfIdle()
      return
    }
    running = true
    let request = pending.removeFirst()
    let task = Task { @MainActor [weak self] in
      guard let self else { return }
      self.write(await self.process(request))
      self.current = nil
      self.next()
    }
    current = task
  }
  private func process(_ request: [String: Any]) async -> [String: Any] {
    let id = request["id"]
    func ok(_ result: [String: Any]) -> [String: Any] {
      ["id": id as Any, "ok": true, "result": result]
    }
    func fail(_ error: Error) -> [String: Any] {
      let e = error as? HelperError
      return [
        "id": id as Any, "ok": false,
        "error": ["code": e?.code ?? "operation_failed", "message": String(describing: error)],
      ]
    }
    do {
      try Task.checkCancellation()
      guard let op = request["op"] as? String else { throw HelperError.invalid("op is required") }
      // Input is a bounded interactive activity. A windowless helper otherwise leaves
      // its timers and WindowServer IPC subject to background scheduling delays.
      let activity = ["type_text", "press_key", "click", "drag", "scroll", "tap_sequence"].contains(op)
        ? ProcessInfo.processInfo.beginActivity(
          options: [.userInitiated, .latencyCritical], reason: "Deliver leased desktop input")
        : nil
      defer { if let activity { ProcessInfo.processInfo.endActivity(activity) } }
      switch op {
      case "status": return ok(helper.status())
      case "bind_lease":
        let duration = try request["duration_seconds"] == nil ? 1800
          : integer(request, "duration_seconds")
        guard (1...3600).contains(duration) else {
          throw HelperError.invalid("duration_seconds must be between 1 and 3600")
        }
        let result = try helper.bindLease(
          LeaseBinding(
            firmID: try string(request, "firm_id"), roleID: try string(request, "role_id"),
            leaseID: try string(request, "lease_id")))
        deadlineTask = Task { @MainActor [weak self] in
          do { try await Task.sleep(for: .seconds(duration)) } catch { return }
          self?.onWriteFailure?()
        }
        return ok(result)
      case "launch_app":
        return ok(try await helper.launch(
          path: try string(request, "path"), mode: try AppLaunchMode.parse(request["launch_mode"])))
      case "release":
        let result = try await helper.release()
        deadlineTask?.cancel()
        deadlineTask = nil
        return ok(result)
      case "list_apps": return ok(try helper.listApps())
      case "list_windows":
        return ok(try await helper.listWindows(pid: request["pid"] == nil ? nil : try pid(request)))
      case "inspect_ax":
        let handle = request["handle"] == nil || request["handle"] is NSNull
          ? nil : try string(request, "handle")
        return ok(
          try helper.inspect(
            pid: try pid(request), depth: bounded(request["depth"], 3, 0...8),
            maximum: bounded(request["max_elements"], 100, 1...500), handle: handle))
      case "action":
        return ok(
          try helper.action(
            pid: try pid(request), handle: try string(request, "handle"),
            action: try string(request, "action")))
      case "focus":
        return ok(try helper.focus(pid: try pid(request), handle: try string(request, "handle")))
      case "type_text":
        return ok(
          try await helper.type(
            pid: try pid(request), text: try string(request, "text"),
            handle: request["handle"] as? String))
      case "press_key":
        return ok(try await helper.pressKey(
          pid: try pid(request), keyCode: integer(request, "key_code"),
          modifiers: try modifierNames(request)))
      case "capture_window":
        return ok(try await helper.capture(pid: try pid(request), windowID: try windowID(request)))
      case "click":
        return ok(
          try await helper.click(
            pid: try pid(request), windowID: try windowID(request),
            point: try point(request, "x", "y"), button: request["button"] as? String ?? "left",
            modifiers: try modifierNames(request),
            hold: try milliseconds(request, "hold_ms", 0...10_000) ?? 0))
      case "drag":
        return ok(
          try await helper.drag(
            pid: try pid(request), windowID: try windowID(request),
            from: try point(request, "from_x", "from_y"), to: try point(request, "to_x", "to_y"),
            modifiers: try modifierNames(request),
            hold: try milliseconds(request, "hold_ms", 0...10_000) ?? 0,
            duration: try milliseconds(request, "duration_ms", 50...10_000)))
      case "scroll":
        return ok(
          try await helper.scroll(
            pid: try pid(request), windowID: try windowID(request),
            point: try point(request, "x", "y"),
            deltaX: try optionalInteger(request, "delta_x") ?? 0,
            deltaY: try optionalInteger(request, "delta_y") ?? 0,
            modifiers: try modifierNames(request)))
      case "tap_sequence":
        return ok(
          try await helper.tapSequence(
            pid: try pid(request), windowID: try windowID(request), taps: try taps(request)))
      default: throw HelperError.invalid("unknown operation")
      }
    } catch { return fail(error) }
  }
  private func write(_ object: [String: Any]) {
    guard !disconnectStarted else { return }
    let data: Data
    if let encoded = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
      data = encoded
    } else {
      let fallback: [String: Any] = [
        "id": object["id"] as Any, "ok": false,
        "error": [
          "code": "serialization_failed", "message": "response could not be encoded as JSON",
        ],
      ]
      guard
        let encoded = try? JSONSerialization.data(withJSONObject: fallback, options: [.sortedKeys])
      else { return }
      data = encoded
    }
    var response = data
    response.append(10)
    do {
      try output.write(contentsOf: response)
    } catch {
      onWriteFailure?()
    }
  }
}

func pid(_ r: [String: Any]) throws -> pid_t {
  let v = try integer(r, "pid")
  guard v > 0, v <= Int(Int32.max) else { throw HelperError.invalid("pid must be positive") }
  return pid_t(v)
}
func windowID(_ r: [String: Any]) throws -> CGWindowID {
  let value = try integer(r, "window_id")
  guard value >= 0, value <= Int(UInt32.max) else {
    throw HelperError.invalid("window_id must be an unsigned 32-bit integer")
  }
  return CGWindowID(value)
}
func string(_ r: [String: Any], _ key: String) throws -> String {
  guard let v = r[key] as? String, !v.isEmpty, v.utf8.count <= maximumInputBytes else {
    throw HelperError.invalid("\(key) must be a nonempty string up to \(maximumInputBytes) bytes")
  }
  return v
}
@MainActor func feed(_ data: Data, buffer: inout Data, server: JSONServer) -> Bool {
  var framer = RequestFramer(buffer: buffer)
  guard let frames = framer.append(data) else { return false }
  buffer = framer.pending
  for frame in frames {
    switch frame {
    case .request(let request): server.enqueue(request)
    case .malformed: server.enqueue(["op": "__invalid__"])
    }
  }
  return true
}

final class SocketServer {
  let control: uid_t
  let helper: StationHelper
  private let endpoint: UnixSocketListener
  private var listener: Int32 { endpoint.descriptor }
  init(endpoint: UnixSocketListener, control: uid_t, helper: StationHelper) {
    self.endpoint = endpoint
    self.control = control
    self.helper = helper
  }
  func run() {
    DispatchQueue.global(qos: .userInitiated).async { [self] in
      while true {
        let client = accept(listener, nil, nil)
        if client < 0 { continue }
        guard isControlAccountPeer(client, control: control) else {
          close(client)
          continue
        }
        guard setSocketSendTimeout(client) else {
          close(client)
          continue
        }
        let wait = DispatchSemaphore(value: 0)
        DispatchQueue.main.async { [self] in
          helper.beginConnection()
          let server = JSONServer(
            helper: helper, output: FileHandle(fileDescriptor: client, closeOnDealloc: false))
          let completed: () -> Void = {
            shutdown(client, SHUT_RDWR)
            close(client)
            wait.signal()
          }
          server.onWriteFailure = { [weak server] in server?.disconnected(completed) }
          readSocket(client, server: server, done: completed)
        }
        wait.wait()
      }
    }
  }
}

func setSocketSendTimeout(_ socket: Int32) -> Bool {
  var timeout = timeval(tv_sec: 3, tv_usec: 0)
  return withUnsafePointer(to: &timeout) {
    setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, $0, socklen_t(MemoryLayout<timeval>.size)) == 0
  }
}

func readSocket(_ fd: Int32, server: JSONServer, done: @escaping () -> Void) {
  DispatchQueue.global(qos: .userInitiated).async {
    var buffer = Data()
    var bytes = [UInt8](repeating: 0, count: 4096)
    while true {
      let count = recv(fd, &bytes, bytes.count, 0)
      if count <= 0 { break }
      let input = Data(bytes[0..<Int(count)])
      var valid = true
      DispatchQueue.main.sync {
        MainActor.assumeIsolated { valid = feed(input, buffer: &buffer, server: server) }
      }
      if !valid { break }
    }
    DispatchQueue.main.async { server.disconnected(done) }
  }
}

@main enum Main {
  @MainActor static func main() {
    do {
      _ = signal(SIGPIPE, SIG_IGN)
      let args = Array(CommandLine.arguments.dropFirst())
      if args.first == "--verify-peer" {
        let accounts = try StationAccounts.load()
        guard args.count == 3, let descriptor = Int32(args[1]), descriptor >= 0,
          let station = uid_t(args[2]), accounts.contains(station),
          getuid() == accounts.control, let executable = Bundle.main.executableURL else { exit(2) }
        try verifyHelperPeer(descriptor, station: station, executable: executable)
        return
      }
      guard args.count == 2, args[0] == "--socket" else {
        FileHandle.standardError.write(Data(
          "usage: FirmStationHelper --socket <path> | --verify-peer <descriptor> <station-uid>\n"
            .utf8))
        exit(2)
      }
      let config = try Configuration.load()
      let app = NSApplication.shared
      app.setActivationPolicy(.accessory)
      let endpoint = try UnixSocketListener(path: args[1], station: config.stationUID)
      let helper = StationHelper(config: config, ledger: try LeaseLedger(socketPath: args[1]))
      let socket = SocketServer(endpoint: endpoint, control: config.controlUID, helper: helper)
      socket.run()
      withExtendedLifetime(socket) { app.run() }
    } catch {
      let object: [String: Any] = [
        "ok": false, "error": ["code": "not_configured", "message": String(describing: error)],
      ]
      let data = try! JSONSerialization.data(withJSONObject: object)
      FileHandle.standardOutput.write(data)
      FileHandle.standardOutput.write(Data("\n".utf8))
      exit(2)
    }
  }
}
