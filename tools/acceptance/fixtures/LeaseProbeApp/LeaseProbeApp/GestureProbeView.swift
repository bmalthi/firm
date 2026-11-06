import SpriteKit
import SwiftUI
import UIKit

/// A scene that never goes idle: one node repeats an action forever, so the view keeps
/// rendering and XCUITest's wait-for-idle gate has something to wait for. `frames` is
/// what the app itself counted, so a test can show the scene really rendered.
final class ProbeScene: SKScene {
    private let spinner = SKSpriteNode(color: SKColor.cyan, size: CGSize(width: 64, height: 64))
    private(set) var frames = 0

    override func didMove(to view: SKView) {
        backgroundColor = SKColor.darkGray
        if spinner.parent == nil {
            spinner.position = CGPoint(x: size.width / 2, y: size.height / 2)
            spinner.run(SKAction.repeatForever(SKAction.rotate(byAngle: .pi, duration: 1)))
            addChild(spinner)
        }
    }

    override func didChangeSize(_ oldSize: CGSize) {
        spinner.position = CGPoint(x: size.width / 2, y: size.height / 2)
    }

    override func update(_ currentTime: TimeInterval) {
        frames += 1
    }
}

/// Records what the gesture recognisers received, and nothing else. There is no way to
/// set the readout from a test: it changes only when a real touch sequence reaches a
/// recogniser through the app's own responder chain.
final class GestureProbeController: UIViewController {
    private let skView = SKView()
    private let surface = UIView()
    private let readout = UILabel()
    private var scene: ProbeScene?
    private var zoom: CGFloat = 1
    private var presses = 0
    private var swipe = "none"
    private var speed: CGFloat = 0

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.black

        // A launch argument, not an input: the simulator recordings need the scene
        // rendering, and the only input path this fixture accepts is a real touch.
        skView.isPaused = !ProcessInfo.processInfo.arguments.contains("FIRM_ANIMATE")
        skView.accessibilityIdentifier = "probeScene"
        view.addSubview(skView)

        surface.backgroundColor = UIColor.clear
        surface.isUserInteractionEnabled = true
        surface.accessibilityIdentifier = "probeSurface"
        view.addSubview(surface)
        surface.addGestureRecognizer(
            UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:))))
        let press = UILongPressGestureRecognizer(target: self, action: #selector(pressed(_:)))
        press.minimumPressDuration = 0.5
        surface.addGestureRecognizer(press)
        surface.addGestureRecognizer(
            UIPanGestureRecognizer(target: self, action: #selector(panned(_:))))

        readout.textColor = UIColor.white
        readout.adjustsFontSizeToFitWidth = true
        readout.isAccessibilityElement = true
        readout.accessibilityIdentifier = "gestureReadout"
        view.addSubview(readout)

        let presented = ProbeScene(size: view.bounds.size)
        presented.scaleMode = .resizeFill
        skView.presentScene(presented)
        scene = presented
        publish()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let bounds = view.bounds
        skView.frame = bounds
        surface.frame = bounds
        readout.frame = CGRect(x: 16, y: bounds.minY + 80, width: bounds.width - 32, height: 44)
        scene?.size = bounds.size
    }

    private func publish() {
        let text = String(
            format: "zoom=%.2f press=%d swipe=%@ speed=%.0f frames=%d",
            Double(zoom), presses, swipe, Double(speed), scene?.frames ?? 0)
        readout.text = text
        readout.accessibilityValue = text
    }

    @objc private func pinched(_ recognizer: UIPinchGestureRecognizer) {
        if recognizer.state == .changed || recognizer.state == .ended {
            zoom *= recognizer.scale
            recognizer.scale = 1
            publish()
        }
    }

    @objc private func pressed(_ recognizer: UILongPressGestureRecognizer) {
        if recognizer.state == .began {
            presses += 1
            publish()
        }
    }

    @objc private func panned(_ recognizer: UIPanGestureRecognizer) {
        guard recognizer.state == .ended else { return }
        let moved = recognizer.translation(in: recognizer.view)
        let velocity = recognizer.velocity(in: recognizer.view)
        swipe = moved.x < 0 ? "left" : "right"
        speed = abs(velocity.x)
        publish()
    }
}

struct GestureProbeView: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> GestureProbeController {
        GestureProbeController()
    }

    func updateUIViewController(_ controller: GestureProbeController, context: Context) {}
}
