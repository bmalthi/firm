import Darwin
import Foundation

enum HelperError: Error { case invalid(String); case unavailable(String) }

func rejects(_ body: () throws -> Void) {
  do { try body(); fatalError("expected rejection") } catch {}
}

@main enum Tests {
  static func main() throws {
    // A lease binding names the firm, the role and the lease it belongs to.
    let lease = String(repeating: "a1", count: 16)
    let bound = try LeaseBinding(firmID: "station-test", roleID: "e-1", leaseID: lease)
    let again = try LeaseBinding(firmID: "station-test", roleID: "e-1", leaseID: lease)
    // A later lease of the same role has a different binding and cannot adopt this one.
    let next = try LeaseBinding(
      firmID: "station-test", roleID: "e-1", leaseID: String(repeating: "b2", count: 16))
    assert(bound == again && bound != next)
    for (firm, role, id) in [
      ("../escape", "e1", lease), ("", "e1", lease), ("firm", "../role", lease),
      ("firm", "", lease), ("Firm", "e1", lease), ("firm", "e1", ""), ("firm", "e1", "../lease"),
      ("firm", "e1", String(repeating: "a", count: 65)),
    ] {
      rejects { _ = try LeaseBinding(firmID: firm, roleID: role, leaseID: id) }
    }
    let directory = URL(fileURLWithPath: NSTemporaryDirectory())
      .appendingPathComponent("firm-launch-policy-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: directory) }
    let canonical = try LaunchPolicy.resolvedPath(directory.path)
    _ = try LaunchPolicy.canonicalDirectory(canonical, owner: getuid())
    // Foundation preserves /var in URL paths on macOS; POSIX realpath must
    // resolve it to /private/var before checking Darwin directory boundaries.
    let varPath = try LaunchPolicy.resolvedPath("/var")
    assert(varPath == "/private/var")
    let trailingPath = try LaunchPolicy.resolvedPath(canonical + "/")
    assert(trailingPath == canonical)
    rejects { _ = try LaunchPolicy.resolvedPath(canonical + "\0/ignored") }
    rejects { _ = try LaunchPolicy.resolvedPath("relative") }
    rejects { _ = try LaunchPolicy.canonicalDirectory(canonical + "/", owner: getuid()) }
    rejects { _ = try LaunchPolicy.canonicalDirectory(canonical, owner: getuid() + 1) }
    let link = directory.appendingPathComponent("alias")
    try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: canonical)
    rejects { _ = try LaunchPolicy.canonicalDirectory(link.path, owner: getuid()) }
    let policy = LaunchPolicy(binding: bound,
      home: canonical + "/home", user: "station", darwinTemp: canonical + "/temp")
    assert(Set(policy.environment.keys) == ["HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG"])
    assert(policy.environment["DYLD_INSERT_LIBRARIES"] == nil)
    assert(policy.environment["HOME"] == policy.home)
    rejects { _ = try StationEnvironment.parse("vm-session", isVirtualMachine: false) }
    rejects { _ = try StationEnvironment.parse("not-an-environment", isVirtualMachine: true) }
    rejects { _ = try StationEnvironment.parse("unsafe", isVirtualMachine: true) }
    let environment = try StationEnvironment.parse("vm-session", isVirtualMachine: true)
    assert(environment == .virtualMachine)
    // Console ownership deliberately is not an input to session readiness.
    assert(environment.sessionIssue(screenLocked: false, setupAssistantRunning: false) == nil)
    assert(environment.sessionIssue(screenLocked: nil, setupAssistantRunning: false) != nil)
    assert(environment.sessionIssue(screenLocked: true, setupAssistantRunning: false) != nil)
    assert(environment.sessionIssue(screenLocked: false, setupAssistantRunning: true) != nil)
    let accounts = try StationAccounts(control: 501, first: 502)
    assert(!accounts.contains(0) && !accounts.contains(501))
    for uid: uid_t in 502..<510 { assert(accounts.contains(uid)) }
    assert(!accounts.contains(510) && !accounts.contains(.max))
    for (control, first) in [(0, 502), (501, 0), (502, 502), (509, 502),
                             (501, Int(uid_t.max) - 6), (Int(uid_t.max) + 1, 502)] {
      rejects { _ = try StationAccounts(control: control, first: first) }
    }
    // Standard application launches are the default, with an explicit direct mode
    // for inherited permissions. Platform signatures forbid that direct mode.
    for (value, expected): (Any?, AppLaunchMode) in [
      (nil, .launchServices), (NSNull(), .launchServices),
      ("launch_services", .launchServices), ("direct", .direct),
    ] {
      let mode = try AppLaunchMode.parse(value)
      assert(mode == expected)
    }
    for invalid: Any in ["automatic", "", false, 2, ["direct"]] {
      rejects { _ = try AppLaunchMode.parse(invalid) }
    }
    let textEdit = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
    try AppLaunchMode.launchServices.validate(bundle: textEdit)
    rejects { try AppLaunchMode.direct.validate(bundle: textEdit) }
    try AppLaunchMode.direct.validate(bundle: directory)
    assert(LaunchPolicy.requiresLaunchServices(URL(fileURLWithPath: "/System/Applications/TextEdit.app")))
    assert(!LaunchPolicy.requiresLaunchServices(URL(fileURLWithPath: CommandLine.arguments[0])))
    assert(!LaunchPolicy.requiresLaunchServices(directory))
    for path in CommandLine.arguments.dropFirst() {
      assert(!LaunchPolicy.requiresLaunchServices(URL(fileURLWithPath: path)), path)
    }
    if FileManager.default.fileExists(atPath: "/Applications/Xcode.app") {
      assert(!LaunchPolicy.requiresLaunchServices(URL(fileURLWithPath: "/Applications/Xcode.app")))
    }
    print("Launch policy validation and launch path tests passed")
  }
}
