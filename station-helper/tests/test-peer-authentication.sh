#!/bin/sh
set -eu
test_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_build=$(mktemp -d /private/tmp/firm-peer-auth.XXXXXX)
trap 'rm -rf "$test_build"' EXIT HUP INT TERM
mkdir -p "$test_build/module-cache"
for target in helper impostor; do
  if [ "$target" = impostor ]; then set -- -D IMPOSTOR; else set --; fi
  xcrun swiftc -module-cache-path "$test_build/module-cache" "$@" \
    "$test_source/PeerAuthentication.swift" "$test_source/UnixSocketListener.swift" \
    "$test_source/tests/PeerAuthenticationTests.swift" -o "$test_build/$target"
  codesign --force --options runtime --timestamp=none --sign - "$test_build/$target"
done
"$test_build/helper" "$test_build/impostor"

xcrun swiftc -module-cache-path "$test_build/module-cache" \
  "$test_source/PeerAuthentication.swift" "$test_source/UnixSocketListener.swift" \
  "$test_source/tests/PeerAuthenticationTests.swift" -o "$test_build/nonhardened"
codesign --force --timestamp=none --sign - "$test_build/nonhardened"
"$test_build/nonhardened" "$test_build/nonhardened" --expect-nonhardened
