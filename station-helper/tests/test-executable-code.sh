#!/bin/sh
set -eu
test_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_build=$(mktemp -d /private/tmp/firm-executable-code.XXXXXX)
trap 'rm -rf "$test_build"' EXIT HUP INT TERM
mkdir -p "$test_build/module-cache"
xcrun swiftc -module-cache-path "$test_build/module-cache" \
  "$test_source/ExecutableCode.swift" "$test_source/tests/ExecutableCodeTests.swift" \
  -o "$test_build/executable-code-tests"
codesign --force --timestamp=none --sign - "$test_build/executable-code-tests"
cp "$test_build/executable-code-tests" "$test_build/other-code"
codesign --force --timestamp=none --sign - --identifier dev.firm.executable-code-other \
  "$test_build/other-code"
"$test_build/executable-code-tests" "$test_build/other-code"
