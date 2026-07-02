#!/bin/bash
set -euo pipefail
cd /home/user/Desktop/hierwalk/examples/stress_seed42
export PYTHONPATH=/home/user/Desktop/hierwalk/src
OUT=/tmp/hierwalk_capture_report.txt
{
  echo "=== IMPORT CHECK ==="
  python3 -c "import hierwalk; print('ok')" 2>&1
  echo ""
  echo "=== PYTEST ==="
  python3 -m pytest /home/user/Desktop/hierwalk/tests/test_connect_pipeline_fixes.py::test_tier1_reuses_preprocessed_text_cache -q --tb=short 2>&1
  echo ""
  echo "=== PATH_WALK RUN (start $(date -Iseconds)) ==="
  HIERWALK_PP_LOG=1 python3 -m hierwalk path_walk_example.json 2>&1 | tee /tmp/hierwalk_run.log
  echo ""
  echo "=== PATH_WALK RUN (end $(date -Iseconds)) ==="
  echo ""
  echo "=== COUNTS ==="
  echo -n "pp-miss: "; grep -c 'pp-miss' /tmp/hierwalk_run.log 2>/dev/null || echo 0
  echo -n "pp-t0: "; grep -c 'pp-t0' /tmp/hierwalk_run.log 2>/dev/null || echo 0
  echo -n "pw-db init: "; grep -c 'pw-db init' /tmp/hierwalk_run.log 2>/dev/null || echo 0
  echo -n "pp-miss tier0: "; grep -c 'pp-miss.*tier0\|pp-t0' /tmp/hierwalk_run.log 2>/dev/null || echo 0
  echo ""
  echo "=== LOG FILES ==="
  ls -la *.hier-walk.log .db_stress_top/*.hier-walk.log 2>/dev/null || true
} | tee "$OUT"
echo "Report written to $OUT"