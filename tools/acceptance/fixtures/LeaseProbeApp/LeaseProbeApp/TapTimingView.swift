import Foundation
import SwiftUI
import UIKit

/// A full-screen view that records wall-clock and monotonic time for every touch-down.
/// Nothing here can be set from outside the app: the list grows only when a real touch
/// sequence reaches `touchesBegan` through the app's own responder chain, which is what
/// a check posting taps at the simulated device's screen needs to know.
final class TapRecordingView: UIView {
    var onTouchDown: ((TimeInterval, TimeInterval) -> Void)?

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesBegan(touches, with: event)
        onTouchDown?(
            Date().timeIntervalSince1970,
            ProcessInfo.processInfo.systemUptime)
    }
}

/// Writes what it received to the app's Documents directory, so a reader over the lease
/// shell can fetch it with `xcrun simctl get_app_container <udid> <bundle id> data`, and
/// shows the count on screen so a capture of the device says the same thing.
final class TapTimingController: UIViewController {
    static let fileName = "firm-taps.json"

    private struct TapDocument: Sendable {
        let count: Int
        let epochMilliseconds: [Int]
        let uptimeMilliseconds: [Int]
    }

    private let count = UILabel()
    private let writeQueue = DispatchQueue(label: "firm.tap-timing.write")
    private var epochMilliseconds: [Int] = []
    private var uptimeMilliseconds: [Int] = []
    private var writeError: String?

    override func loadView() {
        let surface = TapRecordingView()
        surface.onTouchDown = { [weak self] epoch, uptime in
            self?.record(epoch: epoch, uptime: uptime)
        }
        view = surface
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.black
        view.isUserInteractionEnabled = true
        view.accessibilityIdentifier = "tapSurface"

        // A label does not take touches, so the whole screen stays the recording surface.
        count.textColor = UIColor.white
        count.textAlignment = .center
        count.font = UIFont.monospacedDigitSystemFont(ofSize: 96, weight: .bold)
        count.adjustsFontSizeToFitWidth = true
        count.isAccessibilityElement = true
        count.accessibilityIdentifier = "tapCount"
        view.addSubview(count)
        publish()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        count.frame = view.bounds
    }

    private var file: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first?
            .appendingPathComponent(Self.fileName)
    }

    private func record(epoch: TimeInterval, uptime: TimeInterval) {
        epochMilliseconds.append(Int((epoch * 1000).rounded()))
        uptimeMilliseconds.append(Int((uptime * 1000).rounded()))
        let snapshot = TapDocument(
            count: epochMilliseconds.count,
            epochMilliseconds: epochMilliseconds,
            uptimeMilliseconds: uptimeMilliseconds)
        enqueueWrite(snapshot)
        publish()
    }

    /// Snapshots are captured on the main thread; serialization and atomic file writes
    /// run in order on a private queue so receipt handling never waits on the simulator's
    /// Documents filesystem.
    private func enqueueWrite(_ snapshot: TapDocument) {
        guard let file else {
            writeError = "no Documents directory"
            publish()
            return
        }
        let path = file.path
        writeQueue.async { [path, snapshot] in
            do {
                let document: [String: Any] = [
                    "count": snapshot.count,
                    "epoch_ms": snapshot.epochMilliseconds,
                    "uptime_ms": snapshot.uptimeMilliseconds,
                ]
                let data = try JSONSerialization.data(
                    withJSONObject: document, options: [.sortedKeys])
                try data.write(to: URL(fileURLWithPath: path), options: .atomic)
                DispatchQueue.main.async { [weak self] in
                    self?.finishWrite(error: nil)
                }
            } catch {
                let message = String(describing: error)
                DispatchQueue.main.async { [weak self] in
                    self?.finishWrite(error: message)
                }
            }
        }
    }

    private func finishWrite(error: String?) {
        writeError = error
        publish()
    }

    private func publish() {
        let text = "taps=\(epochMilliseconds.count)"
        count.text = writeError == nil ? text : text + " (unwritten)"
        count.accessibilityValue = text
    }
}

struct TapTimingView: UIViewControllerRepresentable {
    /// Selected at launch, the way `FIRM_ANIMATE` selects the rendering scene.
    static let launchArgument = "FIRM_TAP_TIMING"

    func makeUIViewController(context: Context) -> TapTimingController {
        TapTimingController()
    }

    func updateUIViewController(_ controller: TapTimingController, context: Context) {}
}
