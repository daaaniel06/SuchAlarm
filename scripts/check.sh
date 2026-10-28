#!/bin/sh
# All checks before a commit: lint, format, types and tests (Python and frontend).
# ./scripts/check.sh --fix formats first instead of only checking.
set -eu
cd "$(dirname "$0")/.."
PY=${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}

if [ "${1:-}" = "--fix" ]; then
  "$PY" -m ruff check --fix .
  "$PY" -m ruff format .
  npm run -s format
fi
"$PY" -m ruff check .
"$PY" -m ruff format --check .
"$PY" -m mypy
npm run -s format:check
npm run -s lint
npm test --silent
"$PY" -m unittest discover -s tests -p 'test_*.py'
echo "Alles grün."
