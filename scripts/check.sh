#!/usr/bin/env bash
# Local checks before merging: lint, format, i18n extraction, unit tests.
# Usage: scripts/check.sh [extra pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> ruff check"
uv run ruff check .
echo "==> ruff format --check"
uv run ruff format --check .

echo "==> makemessages (extraction sanity)"
err="$(mktemp)"
trap 'rm -f "$err"' EXIT
uv run python manage.py makemessages -l en --no-obsolete \
  --ignore=.venv --ignore=e2e --ignore=design --ignore=staticfiles --ignore=.claude 2>"$err"
if grep -iE "warning|error" "$err"; then
  echo "makemessages reported problems" >&2
  exit 1
fi

echo "==> pytest"
uv run pytest "$@"
