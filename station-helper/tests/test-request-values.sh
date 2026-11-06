#!/bin/sh
set -eu
test_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_build=$(mktemp -d /private/tmp/firm-station-request-values.XXXXXX)
trap 'rm -rf "$test_build"' EXIT HUP INT TERM
mkdir -p "$test_build/module-cache"
xcrun swiftc -module-cache-path "$test_build/module-cache" \
  "$test_source/RequestValues.swift" "$test_source/tests/RequestValuesTests.swift" \
  -o "$test_build/request-value-tests"
"$test_build/request-value-tests"
