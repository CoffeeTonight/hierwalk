#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

run_case() {
  local name="$1"
  local json="$2"
  echo "======== $name ========"
  hier-walk "$json" --index-cwd "$DIR" 2>"/tmp/hierwalk_${name}.stderr" | tail -5
  echo "--- stderr (path-walk highlights) ---"
  rg -n "path-walk|miss inst=|connected|error|ValueError" "/tmp/hierwalk_${name}.stderr" || true
  echo "--- TSV result ---"
  local out
  out=$(python3 -c "import json; print(json.load(open('$json'))['run_conn_check']['output'])")
  if [[ -f "$out" ]]; then
    cat "$out"
  else
    echo "(no output file $out)"
  fi
  echo
}

run_case soc_chain run_soc_chain.json
run_case abcd run_abcd.json
run_case port_suffix run_port_suffix.json
run_case param_array run_param_array.json
run_case stress_deep run_stress_example.json

echo "======== topo reject (CLI --check-connect) ========"
hier-walk design.f --top top --mode path-walk --check-connect topo.u topo.u --index-cwd "$DIR" --no-cache 2>&1 | tail -8 || true