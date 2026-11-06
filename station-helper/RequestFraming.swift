import Foundation

enum FramedRequest {
  case request([String: Any])
  case malformed
}

struct RequestFramer {
  private var buffer = Data()
  private let maximumFrameBytes = 65_536

  init(buffer: Data = Data()) { self.buffer = buffer }
  var pending: Data { buffer }

  mutating func append(_ data: Data) -> [FramedRequest]? {
    buffer.append(data)
    var requests: [FramedRequest] = []
    while let end = buffer.firstIndex(of: 10) {
      let line = buffer.prefix(upTo: end)
      buffer.removeSubrange(...end)
      guard line.count < maximumFrameBytes else { return nil }
      guard !line.isEmpty else { continue }
      if let request = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
        requests.append(.request(request))
      } else {
        requests.append(.malformed)
      }
    }
    guard buffer.count < maximumFrameBytes else { return nil }
    return requests
  }
}
