#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== install =="
pip install -e . -q

echo "== version =="
PYTHONPATH=src python3 -c "import hierwalk; from hierwalk.path_walk_db import PATH_WALK_DB_VERSION; print(f'hierwalk {hierwalk.__version__} pw-db v{PATH_WALK_DB_VERSION}')"
hier-walk --version 2>/dev/null || PYTHONPATH=src python3 -m hierwalk.cli --version

echo "== pytest =="
PYTHONPATH=src python3 -m pytest \
  tests/test_manifest.py \
  tests/test_path_walk_db.py \
  tests/test_path_walk_resolve_policy.py \
  tests/test_path_walk_selective_inst.py \
  tests/test_startup.py \
  -q --tb=short

echo "== path-walk verify_fixes =="
cd "$ROOT/examples/verify_fixes"
PYTHONPATH="$ROOT/src" python3 -m hierwalk.cli run_soc_chain.json

echo "== path-walk connect_expand_verify =="
cd "$ROOT/examples/connect_expand_verify"
PYTHONPATH="$ROOT/src" python3 -m hierwalk.cli run_pathwalk.json

echo "OK: verify_0_3_28 complete"