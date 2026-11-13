#!/usr/bin/env bash
# Print one shard of the test files, one path per line, for CI to pass to pytest.
#
# Usage: scripts/ci-shard.sh unit|e2e INDEX TOTAL
#   unit  test files under homeoa/, tests/ and plugins/
#   e2e   test files under e2e/
# INDEX runs from 0 to TOTAL-1. Files are dealt out in turn, so every file lands in
# exactly one shard.
set -euo pipefail
cd "$(dirname "$0")/.."

suite="${1:?usage: scripts/ci-shard.sh unit|e2e INDEX TOTAL}"
index="${2:?missing INDEX}"
total="${3:?missing TOTAL}"

case "$suite" in
  unit) pattern='^(homeoa|tests|plugins)/(.*/)?(test_[^/]*|tests)\.py$' ;;
  e2e) pattern='^e2e/test_[^/]*\.py$' ;;
  *) echo "ci-shard: unknown suite $suite" >&2; exit 2 ;;
esac

git ls-files | grep -E "$pattern" | sort | awk -v i="$index" -v n="$total" 'NR % n == i'
