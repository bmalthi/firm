import Darwin
import Foundation
import Security

func isControlAccountPeer(_ socket: Int32, control: uid_t) -> Bool {
  var uid: uid_t = .max
  var gid: gid_t = .max
  return getpeereid(socket, &uid, &gid) == 0 && uid == control
}

/// Authenticate the process attached to this socket, including its PID version.
/// A station app has the helper's UID, so Unix credentials alone are insufficient.
func verifyHelperPeer(_ socket: Int32, station: uid_t, executable: URL) throws {
  var uid: uid_t = .max
  var gid: gid_t = .max
  guard getpeereid(socket, &uid, &gid) == 0, uid == station else {
    throw HelperError.invalid("helper socket peer is not the station account")
  }
  var token = audit_token_t()
  var size = socklen_t(MemoryLayout<audit_token_t>.size)
  guard getsockopt(socket, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &size) == 0,
    size == MemoryLayout<audit_token_t>.size
  else { throw HelperError.invalid("Cannot identify helper socket peer") }
  let audit = withUnsafeBytes(of: token) { Data($0) }
  var peer: SecCode?
  guard SecCodeCopyGuestWithAttributes(
    nil, [kSecGuestAttributeAudit: audit] as CFDictionary, [], &peer
  ) == errSecSuccess, let peer else {
    throw HelperError.invalid("helper socket peer process is unavailable")
  }
  var installed: SecStaticCode?
  guard SecStaticCodeCreateWithPath(executable as CFURL, [], &installed) == errSecSuccess,
    let installed else { throw HelperError.invalid("Installed station helper is unavailable") }
  var requirement: SecRequirement?
  guard SecCodeCopyDesignatedRequirement(installed, [], &requirement) == errSecSuccess,
    let requirement,
    SecCodeCheckValidity(peer, [], requirement) == errSecSuccess
  else { throw HelperError.invalid("helper socket peer is not the signed helper") }
  var peerStatic: SecStaticCode?
  var information: CFDictionary?
  guard SecCodeCopyStaticCode(peer, [], &peerStatic) == errSecSuccess, let peerStatic,
    SecCodeCopySigningInformation(peerStatic, SecCSFlags(rawValue: kSecCSSigningInformation),
    &information) == errSecSuccess,
    let flags = (information as? [String: Any])?[kSecCodeInfoFlags as String] as? NSNumber,
    flags.uint32Value & 0x10000 != 0 // CS_RUNTIME: reject helpers permitting code injection.
  else { throw HelperError.invalid("station helper requires hardened runtime signing") }
}
