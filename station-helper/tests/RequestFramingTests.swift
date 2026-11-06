import Foundation

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
  if !condition() { fatalError(message) }
}

@main enum RequestFramingTests {
  static func main() {
    var framer = RequestFramer()
    expect(framer.append(Data("{\"id\":1".utf8))?.isEmpty == true, "partial frame emitted")
    let first = framer.append(Data("}\n".utf8))!
    if case .request(let request) = first[0] {
      expect((request["id"] as? Int) == 1, "partial frame decoded incorrectly")
    } else { fatalError("partial frame was not decoded") }

    let many = framer.append(Data("{\"id\":2}\n{\"id\":3}\n".utf8))!
    expect(many.count == 2, "multiple messages were not framed")
    let malformed = framer.append(Data("not-json\n".utf8))!
    if case .malformed = malformed[0] {} else { fatalError("malformed JSON was accepted") }

    var exact = RequestFramer()
    let payload = String(repeating: "x", count: 65_527)
    expect(exact.append(Data(("{\"x\":\"" + payload + "\"}\n").utf8)) != nil,
      "valid 64KiB frame rejected")
    var oversized = RequestFramer()
    expect(oversized.append(Data(("{\"x\":\"" + String(repeating: "x", count: 65_528) + "\"}\n").utf8)) == nil,
      "oversized frame accepted")
    var unterminated = RequestFramer()
    expect(unterminated.append(Data(repeating: 32, count: 65_536)) == nil,
      "oversized unterminated frame accepted")
    print("request framing tests passed")
  }
}
