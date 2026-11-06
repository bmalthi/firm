import Darwin
import Foundation
import Security

enum StationEnvironment: String {
  case virtualMachine = "vm-session"

  static func parse(_ value: String, isVirtualMachine: Bool) throws -> Self {
    guard let mode = Self(rawValue: value) else {
      throw HelperError.invalid("unknown station environment")
    }
    guard isVirtualMachine else {
      throw HelperError.unavailable("vm-session requires a virtual machine")
    }
    return mode
  }

  static var isVirtualMachine: Bool {
    var value: Int32 = 0
    var size = MemoryLayout.size(ofValue: value)
    return sysctlbyname("kern.hv_vmm_present", &value, &size, nil, 0) == 0 && value == 1
  }

  func sessionIssue(screenLocked: Bool?, setupAssistantRunning: Bool) -> String? {
    guard let locked = screenLocked else { return "VM screen lock state is unavailable" }
    if locked { return "station screen is locked" }
    if setupAssistantRunning { return "station Setup Assistant is still running" }
    return nil
  }
}

/// One signed bundle serves the image's station accounts; its peer check still
/// authenticates exactly the UID named by the control account's relay.
struct StationAccounts {
  let control: uid_t
  let first: uid_t

  init(control: Int, first: Int) throws {
    guard control > 0, first > 0, let controlUID = uid_t(exactly: control),
      let firstUID = uid_t(exactly: first), firstUID <= uid_t.max - 7,
      !(first...(first + 7)).contains(control)
    else { throw HelperError.unavailable("invalid control and first station UIDs") }
    self.control = controlUID
    self.first = firstUID
  }

  func contains(_ uid: uid_t) -> Bool {
    uid >= first && uid - first < 8
  }

  static func load() throws -> Self {
    guard let control = Bundle.main.object(forInfoDictionaryKey: "FirmStationControlUID") as? Int,
      let first = Bundle.main.object(forInfoDictionaryKey: "FirmStationFirstUID") as? Int
    else { throw HelperError.unavailable("bundle is missing control and first station UIDs") }
    return try Self(control: control, first: first)
  }
}

/// The firm, role and lease the helper is bound to. The launch policy, ledger
/// and tracked applications belong to the lease across relay connections, so a
/// later connection presenting the same binding adopts it.
struct LeaseBinding: Equatable {
  let firmID: String
  let roleID: String
  let leaseID: String

  init(firmID: String, roleID: String, leaseID: String) throws {
    for value in [firmID, roleID, leaseID] where !Self.isIdentifier(value) {
      throw HelperError.invalid("binding requires a valid firm, role and lease")
    }
    self.firmID = firmID
    self.roleID = roleID
    self.leaseID = leaseID
  }

  /// Form only: these are labels the control account supplies, never the helper's own
  /// check of who holds the lease.
  private static func isIdentifier(_ text: String) -> Bool {
    !text.isEmpty && text.utf8.count <= 64
      && text.range(of: "^[a-z0-9][a-z0-9_-]*$", options: .regularExpression) != nil
  }
}

/// Standard launches keep each app responsible for its own protected data. Direct
/// launches deliberately inherit the helper's grants, for tools opening the drive.
enum AppLaunchMode: String {
  case launchServices = "launch_services"
  case direct

  static func parse(_ value: Any?) throws -> Self {
    guard let value, !(value is NSNull) else { return .launchServices }
    guard let name = value as? String, let mode = Self(rawValue: name) else {
      throw HelperError.invalid("launch_mode must be launch_services or direct")
    }
    return mode
  }

  func validate(bundle: URL) throws {
    if self == .direct && LaunchPolicy.requiresLaunchServices(bundle) {
      throw HelperError.invalid("Apple platform apps require launch_services")
    }
  }
}

/// The environment a launch gives an app, built from the station account's own home, user
/// and Darwin temporary directory. A direct child gets exactly this environment and starts
/// in that home; LaunchServices adds these variables over the session's. It confines no
/// writes.
struct LaunchPolicy: Equatable {
  let binding: LeaseBinding
  let home: String
  let user: String
  let darwinTemp: String

  static func resolvedPath(_ path: String) throws -> String {
    guard path.hasPrefix("/"), !path.utf8.contains(0),
      let resolved = realpath(path, nil)
    else { throw HelperError.invalid("cannot resolve lease directory") }
    defer { free(resolved) }
    return String(cString: resolved)
  }

  static func canonicalDirectory(_ path: String, owner: uid_t) throws -> String {
    var info = stat()
    guard try resolvedPath(path) == path,
      lstat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
      info.st_uid == owner
    else { throw HelperError.invalid("lease directory must be canonical and correctly owned") }
    return path
  }

  static func load(binding: LeaseBinding) throws -> Self {
    guard let entry = getpwuid(getuid()) else {
      throw HelperError.unavailable("cannot resolve station account")
    }
    let user = String(cString: entry.pointee.pw_name)
    let home = String(cString: entry.pointee.pw_dir)
    _ = try canonicalDirectory(home, owner: getuid())
    func directory(_ key: Int32) throws -> String {
      let length = confstr(key, nil, 0)
      guard length > 1, length <= 4096 else {
        throw HelperError.unavailable("cannot resolve Darwin user directory")
      }
      var bytes = [CChar](repeating: 0, count: length)
      guard confstr(key, &bytes, length) == length else {
        throw HelperError.unavailable("Darwin user directory changed")
      }
      return try resolvedPath(String(cString: bytes))
    }
    return Self(binding: binding, home: home, user: user,
      darwinTemp: try directory(_CS_DARWIN_USER_TEMP_DIR))
  }

  /// Apple platform apps, which macOS launch constraints kill when another process spawns
  /// them directly, so launchd must start them through LaunchServices. Decided by the
  /// code signature: a platform identifier on Apple-anchored code. Other applications
  /// may use either launch mode.
  static func requiresLaunchServices(_ bundle: URL) -> Bool {
    var code: SecStaticCode?
    var information: CFDictionary?
    var requirement: SecRequirement?
    guard SecStaticCodeCreateWithPath(bundle as CFURL, [], &code) == errSecSuccess, let code,
      SecCodeCopySigningInformation(
        code, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess,
      let platform = (information as? [String: Any])?[kSecCodeInfoPlatformIdentifier as String]
        as? Int, platform != 0,
      SecRequirementCreateWithString("anchor apple" as CFString, [], &requirement)
        == errSecSuccess
    else { return false }
    return SecStaticCodeCheckValidity(code, [], requirement) == errSecSuccess
  }

  var environment: [String: String] {
    ["HOME": home, "USER": user, "LOGNAME": user, "TMPDIR": darwinTemp + "/",
     "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
  }
}
