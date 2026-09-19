#!/usr/bin/env bash
# Remove local build and test junk. Tracked files, tests/*/replay, and .borhan/ are untouched.
#
#   ./scripts/clean.sh          repo junk only
#   ./scripts/clean.sh --deep   also prune the uv cache and repack git
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf .venv dist build .ruff_cache .pytest_cache scratch
find . -name __pycache__ -type d -prune -exec rm -rf {} +
find . -name '*.py[co]' -delete

if [[ "${1:-}" == "--deep" ]]; then
  uv cache prune
  git gc --prune=now
fi
