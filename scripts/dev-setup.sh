#!/usr/bin/env bash
# Create or refresh the project-local venv (run after cloning or moving the repo).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found" >&2
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "error: python3 >= 3.9 required (see pyproject.toml)" >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  if ! python3 -m venv .venv 2>/dev/null; then
    echo "error: python3 -m venv failed (install python3-venv?)" >&2
    exit 1
  fi
fi

.venv/bin/pip install -U pip
.venv/bin/pip install -e . pytest
.venv/bin/python -c "import hierwalk; import pytest"
echo "OK: $ROOT/.venv (hier-walk editable)"
echo "Use: source $ROOT/.venv/bin/activate"
echo "If .venv is broken, run: rm -rf .venv && $0"