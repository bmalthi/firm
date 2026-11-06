Mac lease notes: the shared drive is durable storage; check out and build in the
returned `scratch` directory because the drive mount does not support atomic rename.
Copy deliverables to the drive before release. Release clears scratch, simulator
devices, temporary work, Library caches, logs, build products, the Trash and other
top-of-home additions. It keeps Homebrew installs and apps in `~/Applications`, and
does not clear standard folders such as Documents, Downloads and Desktop; the next
holder shares those folders, so keep work out of them. Homebrew installs persist and
are shared across roles; do not remove what others may rely on. Other roles can share
machine resources. You are not an administrator.

Check which tools are installed. For iOS apps, validate on the newest installed iOS
simulator runtime unless the mandate explicitly targets another version; create a
device for that runtime if needed rather than choosing an older existing device.
Older-runtime checks are additional coverage. Record the build SDK and test runtime
separately. After launch, verify the app stays open and its UI responds; a successful
launch command alone does not prove startup succeeded.

If Xcode needs another simulator platform, use only
the approved installer command:
`sudo -n /usr/local/libexec/firm-station-add-platform <iOS|watchOS|tvOS|visionOS>`.
Simulator lives in Xcode Device Hub, not `Simulator.app`. Device Hub opens on a
placeholder: inspect its sidebar, press the device entry with `desktop_action`, then
inspect the screen. If `desktop_inspect` returns `truncated`, inspect again with a
returned element's `handle` to read that branch.

Use the normal launch mode for browsers to reach their protected profiles. Apple
platform apps such as TextEdit require it because macOS kills them when launched
directly. For tools that must open the drive, `launch_mode="direct"` inherits the
helper's Network Volumes permission. Xcode Device Hub uses a trampoline; launch it
with `launch_mode="direct"` so its process stays identifiable across startup.
Apps launched through the desktop tools stay open between tool calls. Use
`desktop_close_apps` to close those apps; `mac_release` ends the lease and stops
everything you started on the Mac.

Desktop tools use one pointer and show one still image at a time: they cannot hear
or make two-finger gestures, and a tap sequence cannot be observed between taps.
Tap-sequence offsets report when input was posted, not when the app received it.
Use SSH, UI tests or `simctl` when the work needs those capabilities.

Use a screenshot's returned `drive_path` when citing its saved file. A capture image
can still be returned if saving fails; without `drive_path` there is no confirmed
saved screenshot.
