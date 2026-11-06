import SwiftUI

struct ContentView: View {
    /// A launch argument picks the scene, so a run is what the launch asked for and the
    /// app has no way to be switched from outside once it is up.
    private var timingTaps: Bool {
        ProcessInfo.processInfo.arguments.contains(TapTimingView.launchArgument)
    }

    var body: some View {
        Group {
            if timingTaps {
                TapTimingView()
            } else {
                GestureProbeView()
            }
        }
        .ignoresSafeArea()
    }
}
