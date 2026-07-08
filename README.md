# hierwalk

**EN** RTL hierarchy path-walk and structural connectivity verification (only regex).  
**KO** RTL hierarchy path-walk · 구조적 connectivity 검증 도구 (only regex).

```bash
pip install -e .
hier-walk design.f --top TOP -o instances.tsv
```

## Usage · 사용

```bash
# instance list · instance 목록
hier-walk filelist.f --top chip_top -o instances.tsv

# connectivity batch · connectivity 배치
hier-walk filelist.f --top chip_top --check-connect-batch checks.json -o conn.tsv

# run JSON
hier-walk run.json -o out.tsv
```

**EN** `checks.json` needs `top` and `checks: [{ "id", "a", "b" }, …]`. See `hier-walk --help-config` for all fields.  
**KO** `checks.json`에는 `top`, `checks: [{ "id", "a", "b" }, …]`가 필요합니다. 전체 필드는 `hier-walk --help-config`.

## Hierarchy grep (hgrep) · hierarchy grep

**EN** `connect_phase: hgrep` runs **hierarchy_grep gate only** (no connect-coi). Grep index is cached as `.db_{TOP}/grep_hie.json`; later runs reuse it when RTL paths match. Use `refresh-cache` (JSON or CLI `--refresh-cache`) to delete `grep_hie.json` and rebuild.  
**KO** `connect_phase: hgrep`는 **hierarchy_grep gate만** 실행합니다 (connect-coi 없음). grep 인덱스는 `.db_{TOP}/grep_hie.json`에 저장되며, RTL 경로가 같으면 다음 실행에서 재사용합니다. `refresh-cache`(JSON) 또는 `--refresh-cache`(CLI)로 `grep_hie.json`을 지우고 다시 만듭니다.

### RUN.json / flat suite

```json
"run_conn_check": {
  "enable": 1,
  "mode": "path-walk",
  "connect_phase": "hgrep",
  "checks": [
    { "id": "hg1", "a": "top.u_a.out", "b": "top.u_b.in" }
  ]
}
```

**EN** Force rebuild: add `"refresh-cache": true` at the top level or in the block.  
**KO** 캐시 초기화: 최상위 또는 블록에 `"refresh-cache": true` 추가.

```bash
hier-walk RUN.json
```

### Standalone · 단독 실행

```bash
# CLI — same checks JSON as --check-connect-batch
hier-walk design.f --top top --check-hgrep checks.json -o conn.tsv
hier-walk design.f --top top --check-hgrep checks.json --refresh-cache

# Script wrapper
scripts/run_hierarchy_grep.py checks.json -f design.f --top top
scripts/run_hierarchy_grep.py checks.json -f design.f --top top --refresh-cache
```

**EN** Artifacts: `conn.hgrep_gate.report` and `grep_hie.json` under `.db_{TOP}/`. Stdout TSV rows use `mode=hgrep`.  
**KO** 산출물: `.db_{TOP}/` 아래 `conn.hgrep_gate.report`, `grep_hie.json`. TSV `mode=hgrep`.

## Connect layout · connectivity 구조

**EN** Structural connectivity lives under `hierwalk/connect/`:

| Package | Role |
|---------|------|
| `connect/shared/` | Module find, preprocess, endpoint resolve, request/expand |
| `connect/text/` | Text-conn: coarse RHS **name grep** (`text_grep_cache`, `text/walk.py`) |
| `connect/logical/` | Logical-conn: bit-precise COI / constant-fold (`mod_cache`, `logical/search.py`) |
| `connect/pipeline/` | Artifacts, validation |
| `connect/session.py` | `ConnectivitySession` — `run_text_request` vs `run_request` |

Text-conn asks “does this **name** appear on the RHS?” (`assign a = b * 0` → text passes).  
Logical-conn asks “does the value **actually propagate**?” (same example → logical fails).

**KO** 구조적 connectivity는 `hierwalk/connect/` 아래에 있습니다.

| 패키지 | 역할 |
|--------|------|
| `connect/shared/` | 모듈 탐색, 전처리, endpoint resolve, request/expand |
| `connect/text/` | Text-conn: RHS **이름 grep** (`text_grep_cache`, `text/walk.py`) |
| `connect/logical/` | Logical-conn: 비트 정밀 COI / 상수 접기 (`mod_cache`) |
| `connect/pipeline/` | 아티팩트, 검증 |
| `connect/session.py` | `ConnectivitySession` — `run_text_request` vs `run_request` |

## Modes · 모드

| Mode | EN | KO |
|------|----|----|
| `hierarchy` | Instance TSV (default) | instance TSV (기본) |
| `find-top` | Top-module candidates | top module 후보 |
| `search` | Path / instance search | path / instance 검색 |
| `check-connect` / `check-connect-batch` | Connectivity check | connectivity 검사 |
| `check-hgrep` / `connect_phase: hgrep` | Hierarchy grep gate only (cached `grep_hie.json`) | hierarchy grep gate만 (`grep_hie.json` 캐시) |
| `fanin-cone` / `fanout-cone` | COI cone | COI cone |
| `inst-trace` | Driver / sinker trace | driver / sinker trace |
| `path-walk` | Large SoC path-walk + connect | 대형 SoC path-walk + connect |

## Examples · 예제

**EN** `examples/stress_seed42/` — `path_walk_example.json`, `flat_run_example.json`, `search_example.json`  
**KO** `examples/stress_seed42/` — 위 JSON 템플릿

```bash
cd examples/stress_seed42 && hier-walk path_walk_example.json
```

## Work dir · 작업 디렉터리

**EN** Cache, DB, logs, and temp files default to `.db_{TOP}/` under `--index-cwd` (or cwd). Override with `--cache-dir` or `$HIERWALK_CACHE_DIR`.  
**KO** 캐시·DB·로그·임시파일은 기본적으로 `--index-cwd`(또는 cwd) 아래 `.db_{TOP}/` 에 생성됩니다.

## Tests · 테스트

```bash
python -m pytest tests/ -q
```

## Corp PC · 회사 PC (`hier-walk` not found)

**EN** `pip install -e .` can succeed while the `hier-walk` script is not on PATH (user install dir, pyenv, IT policy). Use one of these instead.  
**KO** `pip install -e .`는 됐는데 `hier-walk`만 없는 경우가 많습니다 (PATH·권한). 아래 중 하나를 쓰세요.

```bash
# A) pip install 후 — hier-walk 스크립트 없어도 OK
cd hierwalk && python3 -m pip install -e .
python3 -m hierwalk design.f --top TOP -o out.tsv

# B) pip 없이 — PYTHONPATH만 · no pip
cd hierwalk
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m hierwalk --version

# C) pip 없이 — wrapper가 PYTHONPATH 설정 · no pip
cd hierwalk && chmod +x scripts/hier-walk
./scripts/hier-walk design.f --top TOP -o out.tsv

# D) venv (권장) — clone/이동 후에도 동일
cd hierwalk
./scripts/dev-setup.sh
source .venv/bin/activate
hier-walk --version
```
