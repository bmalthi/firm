import XCTest

/// Real gestures, through the device's own touch pipeline, against the running app.
/// Nothing here calls app code: every assertion reads the readout the app's gesture
/// recognisers wrote. Each gesture call is timed and the time printed; the acceptance
/// script parses those lines and enforces the bound, so the bound lives in one place.
final class GestureUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    private func launch(animated: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        if animated {
            app.launchArguments = ["FIRM_ANIMATE"]
        }
        app.launch()
        XCTAssertTrue(app.staticTexts["gestureReadout"].waitForExistence(timeout: 60))
        return app
    }

    private func field(_ app: XCUIApplication, _ key: String) -> String {
        let readout = app.staticTexts["gestureReadout"].label
        for pair in readout.split(separator: " ") {
            let parts = pair.split(separator: "=", maxSplits: 1)
            if parts.count == 2 && String(parts[0]) == key {
                return String(parts[1])
            }
        }
        return ""
    }

    private func timed(_ phase: String, _ gesture: String, _ body: () -> Void) {
        let started = Date()
        body()
        let seconds = Date().timeIntervalSince(started)
        print(String(format: "GESTURE_TIMING %@ %@ %.3f", phase, gesture, seconds))
    }

    private func attach(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func gesture(_ app: XCUIApplication, phase: String) {
        let surface = app.otherElements["probeSurface"]
        XCTAssertTrue(surface.waitForExistence(timeout: 30))
        timed(phase, "pinch") { surface.pinch(withScale: 2.0, velocity: 1.0) }
        XCTAssertGreaterThan(Double(field(app, "zoom")) ?? 0, 1.2, "the pinch did not reach the recogniser")
        timed(phase, "press") { surface.press(forDuration: 1.2) }
        XCTAssertEqual(field(app, "press"), "1", "the long press did not reach the recogniser")
        timed(phase, "swipe") { surface.swipeLeft(velocity: .fast) }
        XCTAssertEqual(field(app, "swipe"), "left", "the swipe did not reach the recogniser")
        XCTAssertGreaterThan(Double(field(app, "speed")) ?? 0, 0, "the swipe carried no velocity")
        attach("\(phase)-after-gestures")
    }

    func testGesturesReachRecognisersWhileIdle() {
        gesture(launch(animated: false), phase: "idle")
    }

    func testGesturesReachRecognisersWhileSceneRenders() {
        let app = launch(animated: true)
        gesture(app, phase: "rendering")
        XCTAssertGreaterThan(Int(field(app, "frames")) ?? 0, 0, "the SpriteKit scene never rendered")
    }

    func testRenderingSceneKeepsAdvancing() {
        let app = launch(animated: true)
        let surface = app.otherElements["probeSurface"]
        XCTAssertTrue(surface.waitForExistence(timeout: 30))
        timed("rendering", "press-first") { surface.press(forDuration: 0.8) }
        let first = Int(field(app, "frames")) ?? 0
        attach("rendering-first-sample")
        timed("rendering", "press-second") { surface.press(forDuration: 0.8) }
        let second = Int(field(app, "frames")) ?? 0
        attach("rendering-second-sample")
        XCTAssertGreaterThan(second, first, "the scene stopped rendering between gestures")
    }
}
