#!/bin/sh
set -eu
test_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_build=$(mktemp -d /private/tmp/firm-station-launch-policy.XXXXXX)
trap 'rm -rf "$test_build"' EXIT HUP INT TERM
mkdir -p "$test_build/module-cache"
xcrun swiftc -module-cache-path "$test_build/module-cache" \
  "$test_source/LaunchPolicy.swift" "$test_source/tests/LaunchPolicyTests.swift" \
  -o "$test_build/launch-policy-tests"
# A locally built app: an ad-hoc signed bundle, like a role's build on the drive.
local_app="$test_build/Local Build.app"
mkdir -p "$local_app/Contents/MacOS"
cp "$test_build/launch-policy-tests" "$local_app/Contents/MacOS/LocalBuild"
printf '%s' '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.firm.local-build</string><key>CFBundleExecutable</key><string>LocalBuild</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>' > "$local_app/Contents/Info.plist"
codesign --force --timestamp=none --sign - "$local_app"
"$test_build/launch-policy-tests" "$local_app"
