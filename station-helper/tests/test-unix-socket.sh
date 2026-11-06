#!/bin/sh
set -eu
test_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_build=$(mktemp -d /private/tmp/firm-station-socket.XXXXXX)
trap 'rm -rf "$test_build"' EXIT HUP INT TERM
mkdir -p "$test_build/module-cache"
xcrun swiftc -module-cache-path "$test_build/module-cache" \
  "$test_source/UnixSocketListener.swift" "$test_source/Ledger.swift" "$test_source/ProcessIdentity.swift" \
  "$test_source/tests/UnixSocketListenerTests.swift" -o "$test_build/socket-tests"
"$test_build/socket-tests"
