#!/bin/sh
set -eu
helper_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
helper_test=$(mktemp -d "${TMPDIR:-/tmp}/firm-held-input.XXXXXX")
trap 'rm -rf "$helper_test"' EXIT
xcrun swiftc -parse-as-library -module-cache-path "$helper_test/module-cache" \
  "$helper_source/HeldInput.swift" "$helper_source/tests/HeldInputTests.swift" \
  -o "$helper_test/test"
"$helper_test/test"
