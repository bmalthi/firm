import XCTest

/// One fast test, kept in its own class so the silent-skip scheme can run this class,
/// skip the gesture class, and still report TEST SUCCEEDED while running fewer tests
/// than the scheme names.
final class SmokeUITests: XCTestCase {
    func testAppLaunches() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.staticTexts["gestureReadout"].waitForExistence(timeout: 60))
    }
}
