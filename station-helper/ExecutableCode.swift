import Darwin
import Foundation
import Security

/// The exact signed code present at a launch executable, independent of its later path.
/// A caller must separately bind the PID to the process it launched; this type only
/// verifies the live process's code signature and never grants authority by pathname.
struct ExecutableCode {
  private let requirement: SecRequirement

  init?(path: String) {
    guard path.hasPrefix("/") else { return nil }
    var code: SecStaticCode?
    guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code)
      == errSecSuccess, let code
    else { return nil }
    var information: CFDictionary?
    guard SecCodeCopySigningInformation(code, [], &information) == errSecSuccess,
      let hash = (information as? [String: Any])?[kSecCodeInfoUnique as String] as? Data,
      !hash.isEmpty
    else { return nil }
    let hex = hash.map { String(format: "%02x", $0) }.joined()
    var requirement: SecRequirement?
    guard SecRequirementCreateWithString("cdhash H\"\(hex)\"" as CFString, [], &requirement)
      == errSecSuccess, let requirement
    else { return nil }
    self.requirement = requirement
  }

  func matches(pid: pid_t) -> Bool {
    guard pid > 0 else { return false }
    var code: SecCode?
    guard SecCodeCopyGuestWithAttributes(
      nil, [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary, [], &code
    ) == errSecSuccess, let code else { return false }
    return SecCodeCheckValidity(code, [], requirement) == errSecSuccess
  }
}
