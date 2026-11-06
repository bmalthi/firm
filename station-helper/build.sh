#!/bin/sh
set -eu
helper_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
helper_build=${1:?build directory required}
helper_control_uid=${2:?control UID required}
helper_station_uid=${3:?first station UID required}
helper_identifier=dev.firm.station-helper
helper_environment=vm-session
helper_version=${FIRM_HELPER_BUILD_VERSION:-1}
case "$helper_version" in ''|*[!0-9.]*|.*|*.) echo 'Invalid helper build version.' >&2; exit 2;; esac
if [ -z "${FIRM_HELPER_SIGN_IDENTITY:-}" ]; then
  echo 'VM builds require FIRM_HELPER_SIGN_IDENTITY; use - explicitly for disposable development builds.' >&2
  exit 2
fi
case "$helper_control_uid:$helper_station_uid" in *[!0-9:]*|:) echo 'Control and station UIDs must be numeric.' >&2; exit 2;; esac
test "$helper_control_uid" != "$helper_station_uid" || { echo 'Control and station UIDs must differ.' >&2; exit 2; }
helper_app="$helper_build/Firm Station Helper.app"
mkdir -p "$helper_app/Contents/MacOS" "$helper_build/module-cache"
xcrun swiftc -parse-as-library -O -target "$(uname -m)-apple-macos14.0" \
  -module-cache-path "$helper_build/module-cache" \
  "$helper_source/Main.swift" "$helper_source/RequestFraming.swift" "$helper_source/PeerAuthentication.swift" "$helper_source/UnixSocketListener.swift" "$helper_source/StationHelper.swift" "$helper_source/InspectionBudget.swift" "$helper_source/Ledger.swift" "$helper_source/RequestValues.swift" "$helper_source/HeldInput.swift" "$helper_source/LaunchPolicy.swift" "$helper_source/ProcessIdentity.swift" "$helper_source/ExecutableCode.swift" -o "$helper_app/Contents/MacOS/FirmStationHelper"
cat > "$helper_app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>$helper_identifier</string>
<key>CFBundleName</key><string>Firm Station Helper</string>
<key>CFBundleExecutable</key><string>FirmStationHelper</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>$helper_version</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>FirmStationEnvironment</key><string>$helper_environment</string>
<key>FirmStationControlUID</key><integer>$helper_control_uid</integer>
<key>FirmStationFirstUID</key><integer>$helper_station_uid</integer>
</dict></plist>
PLIST
if [ -n "${FIRM_HELPER_SIGN_KEYCHAIN:-}" ]; then
  codesign --force --options runtime --timestamp=none --keychain "$FIRM_HELPER_SIGN_KEYCHAIN" \
    --sign "${FIRM_HELPER_SIGN_IDENTITY:--}" "$helper_app"
else
  codesign --force --options runtime --timestamp=none --sign "${FIRM_HELPER_SIGN_IDENTITY:--}" "$helper_app"
fi
codesign --verify --strict "$helper_app"
printf '%s\n' "$helper_app"
