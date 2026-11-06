# Tap-to-fly iOS game

Build a small original iOS arcade game: tap to give a flying character an upward
impulse, navigate gaps between scrolling obstacles, and earn one point for each
obstacle passed. Give it its own name, simple visual style, and original geometric
art. Do not use Flappy Bird branding, artwork, sounds, or copied code.

Use Swift and Apple's native frameworks, such as SpriteKit with SwiftUI. Keep the
scope to one polished playable screen with a start state, score, collision and
game-over state, and a reliable restart button. Include brief on-screen controls.
Use no external services, downloaded assets, or third-party dependencies. Target
an iPhone simulator available on the configured Mac station; no physical device,
paid developer account, or App Store submission is required.

This task should exercise Firm's Mac station and iOS workflow. Acquire a Mac
station, inspect the available Xcode and simulator runtimes, build the project,
install it in an iPhone simulator, launch it, and exercise the controls. If no
usable station or runtime is available, report the blocker to the owner rather
than claiming simulator validation or substituting a browser game.

Hire at most two specialists, including an independent tester. Test scoring,
collision, and restart behavior, using deterministic game-logic tests where
practical. On the simulator, verify that taps affect flight, collisions end a run,
and restarting resets the score and game state. Capture a screenshot of the game
running. Clearly distinguish automated checks from observed simulator behavior;
report only checks actually performed.

Deliver the complete Xcode project, source, screenshot, and concise build/run
instructions in `shared/tap-to-fly/`. Copy deliverables back from the Mac station
before releasing the lease. Attach the instructions and screenshot to your owner
message, identify the project path, and report the simulator/device and OS version,
build and test results, and known limitations. Prioritize a working, verified game
over extra features.
