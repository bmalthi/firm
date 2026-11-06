import XCTest

@testable import LeaseProbeKit

final class ScoreTests: XCTestCase {
    func testZeroDoubles() {
        XCTAssertEqual(Score(value: 0).doubled, 0)
    }

    func testPositiveDoubles() {
        XCTAssertEqual(Score(value: 21).doubled, 42)
    }

    func testNegativeDoubles() {
        XCTAssertEqual(Score(value: -3).doubled, -6)
    }
}
