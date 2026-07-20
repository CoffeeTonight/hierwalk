# hierwalk

**EN** RTL hierarchy path-walk and structural connectivity verification (regex path-walk; optional pyslang hierarchy).  
**KO** RTL hierarchy path-walk · 구조적 connectivity 검증 (기본 path-walk는 regex; hierarchy gate는 optional pyslang).

```bash
pip install -e .
# optional · pyslangwalk (module-index + pyslang hierarchy)
pip install -e ".[pyslangwalk]"

hier-walk design.f --top TOP -o instances.tsv
```

## Usage · 사용

```bash
# instance list · instance 목록
hier-walk filelist.f --top chip_top -o instances.tsv

# connectivity batch · connectivity 배치 (connect_phase from JSON or default)
hier-walk filelist.f --top chip_top --check-connect-batch checks.json -o conn.tsv

# run JSON (filelist + mode + connect_phase + checks in one file)
hier-walk run.json -o out.tsv
```

**EN** Batch `checks.json` needs `top` and `checks: [{ "id", "a", "b" }, …]`. Full field list: `hier-walk --help-config`, connectivity: `hier-walk --help-connect`.  
**KO** `checks.json`에는 `top`, `checks: [{ "id", "a", "b" }, …]`가 필요합니다. 전체 필드: `hier-walk --help-config`, connectivity: `hier-walk --help-connect`.

## `connect_phase` · connectivity 단계

**EN** Path-walk connectivity is selected with `connect_phase` (JSON) or a dedicated CLI flag.  
**KO** path-walk connectivity 단계는 JSON `connect_phase` 또는 전용 CLI 플래그로 고릅니다.

| `connect_phase` | EN | KO | CLI shortcut |
|-----------------|----|----|--------------|
| `text` | Text-conn (name bloom / COI) | text-conn | `--check-connect-batch` + phase in JSON |
| `logical` | Bit-precise logical COI | logical-conn | same |
| `both` | Text then logical (default-ish path-walk) | text + logical | same |
| `hgrep` | **Hierarchy gate only** via hierarchy_grep (no text-COI) | hierarchy_grep gate만 | `--check-hgrep FILE` |
| `pyslangwalk` | **grep_hie + pyslang hierarchy**, then **text-COI** on survivors | grep_hie + pyslang → text | `--check-pyslangwalk FILE` |

**EN** CLI flags `--check-hgrep`, `--check-pyslangwalk`, and `--check-connect-batch` are mutually exclusive.  
**KO** `--check-hgrep` / `--check-pyslangwalk` / `--check-connect-batch` 는 서로 배타입니다.

### Shared index: `grep_hie.json`

**EN** Both `hgrep` and `pyslangwalk` build/reuse a **module → file** map at `.db_{TOP}/grep_hie.json` (under `--cache-dir` / `HIERWALK_CACHE_DIR` when set). Later runs reuse it when RTL paths match.  
**KO** `hgrep`와 `pyslangwalk` 모두 모듈→파일 맵 `.db_{TOP}/grep_hie.json`을 만들고 재사용합니다.

- **EN** Rebuild: `"refresh-cache": true` in JSON, or CLI `--refresh-cache`.  
- **KO** 재생성: JSON `"refresh-cache": true` 또는 `--refresh-cache`.

---

## Hierarchy grep (`hgrep`) · hierarchy_grep gate only

**EN** Endpoint hierarchy resolve via text hierarchy_grep. **No connect-coi / text-COI.**  
**KO** hierarchy_grep으로 endpoint hierarchy만 검사. **connectivity(text-COI) 없음.**

### JSON (RUN / suite)

```json
{
  "filelist": "design.f",
  "top": "chip_top",
  "mode": "path-walk",
  "connect_phase": "hgrep",
  "output": "conn_hgrep.tsv",
  "checks": [
    { "id": "hg1", "a": "chip_top.u_a.out", "b": "chip_top.u_b.in" }
  ]
}
```

Suite-style block (same key):

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

### CLI / script

```bash
hier-walk design.f --top top --check-hgrep checks.json -o conn.tsv
hier-walk design.f --top top --check-hgrep checks.json --refresh-cache

scripts/run_hierarchy_grep.py checks.json -f design.f --top top
scripts/run_hierarchy_grep.py checks.json -f design.f --top top --refresh-cache
```

**EN** Artifacts: `conn.hgrep_gate.report`, `grep_hie.json`. TSV `mode=hgrep`.  
**KO** 산출물: `conn.hgrep_gate.report`, `grep_hie.json`. TSV `mode=hgrep`.

---

## Pyslang walk (`pyslangwalk`) · grep_hie + pyslang → text

**EN** Hierarchy walk with **pyslang**, opening only RTL files on the path (looked up via `grep_hie` module→file index). Hierarchy survivors then run path-walk **text-COI**.  
**KO** **pyslang**으로 hierarchy walk — `grep_hie` 모듈→파일 인덱스로 **경로상 RTL만** open. hierarchy 통과 check만 path-walk **text-COI**.

Requires optional dep: `pip install -e ".[pyslangwalk]"` (or `pip install 'pyslang>=11'`).

### Pipeline

```
grep_hie.json (module → file)
    → pyslang hierarchy gate (path-scoped open)
    → survivors only
    → path-walk text-COI
```

| Stage result | TSV / summary `mode` |
|--------------|----------------------|
| Hierarchy miss | `pyslangwalk` |
| Hierarchy ok + text | `pyslangwalk+text` |

### JSON (RUN — recommended)

```json
{
  "filelist": "filelist.f",
  "top": "zz_torture_top",
  "index-cwd": "/path/to/rtl",
  "mode": "path-walk",
  "connect_phase": "pyslangwalk",
  "include-ff": true,
  "defines": {
    "ZZ_TORTURE": "1"
  },
  "output": "conn_pyslangwalk.tsv",
  "checks": [
    {
      "id": "clk_deep",
      "a": "zz_torture_top.clk",
      "b": "zz_torture_top.u_zigzag.u_deep.d1.clk"
    }
  ]
}
```

**EN** Key switch: `"connect_phase": "pyslangwalk"` (alias: `"connect-phase"`).  
**KO** 스위치: `"connect_phase": "pyslangwalk"` (`"connect-phase"` 동일).

```bash
hier-walk run_pyslangwalk.json
# or
hier-walk --config run_pyslangwalk.json
```

### JSON (checks only) + CLI flag

**EN** Connect-only batch need not set `connect_phase` if you pass `--check-pyslangwalk` (flag forces the phase).  
**KO** checks 전용 JSON에는 `connect_phase`가 없어도 됩니다. `--check-pyslangwalk`가 phase를 고정합니다.

```json
{
  "top": "zz_torture_top",
  "defines": { "ZZ_TORTURE": "1" },
  "include_ff": true,
  "checks": [
    {
      "id": "clk_deep",
      "a": "zz_torture_top.clk",
      "b": "zz_torture_top.u_zigzag.u_deep.d1.clk"
    }
  ]
}
```

```bash
hier-walk filelist.f \
  --top zz_torture_top \
  --index-cwd . \
  --check-pyslangwalk checks.json \
  --include-ff \
  -o conn_pyslangwalk.tsv

# rebuild module index
hier-walk filelist.f --top zz_torture_top \
  --check-pyslangwalk checks.json --refresh-cache
```

### Check item fields · check 항목

| Field | Aliases | Notes |
|-------|---------|--------|
| `a` / `b` | `from`/`to`, `src`/`dst`, `endpoint_a`/`endpoint_b` | Hierarchical endpoints |
| `id` | `name` | Optional; TSV `check_id` |

Common batch options (snake or kebab): `include-ff`, `connect-trace` / `connect-log`, `refresh-cache`, `no-cache`, `defines`, `jobs`.

### Zigzag helper · zigzag 헬퍼

```bash
python scripts/run_zigzag_pyslangwalk.py
```

**EN** Writes under the demo work dir (see script): report, `pyslangwalk_summary.json`, `pyslangwalk.hier-walk.log`, connect TSV, `grep_hie.json`.  
**KO** 스크립트 work dir에 report / summary / log / TSV / `grep_hie.json` 생성.

### Artifacts · 산출물

| File | EN | KO |
|------|----|----|
| `grep_hie.json` | Module→file cache | 모듈→파일 캐시 |
| `conn_pyslangwalk*.tsv` | Connect results | 연결 결과 |
| `*.hier-walk.log` | Hierarchy gate log (text stage may be sparse) | hierarchy 로그 (text 구간은 적을 수 있음) |

---

## Connect layout · connectivity 구조

**EN** Structural connectivity lives under `hierwalk/connect/`:

| Package | Role |
|---------|------|
| `connect/shared/` | Module find, preprocess, endpoint resolve, request/expand |
| `connect/text/` | Text-conn: coarse RHS **name grep** (`text_grep_cache`, `text/walk.py`) |
| `connect/logical/` | Logical-conn: bit-precise COI / constant-fold (`mod_cache`, `logical/search.py`) |
| `connect/hierarchy_grep_gate.py` | `connect_phase: hgrep` |
| `connect/pyslang_walk_gate.py` | `connect_phase: pyslangwalk` (then text) |
| `connect/pipeline/` | Artifacts, validation |
| `connect/session.py` | `ConnectivitySession` — `run_text_request` vs `run_request` |
| `pyslang_walk.py` | Per-path pyslang open + hierarchy resolve |

Text-conn asks “does this **name** appear on the RHS?” (`assign a = b * 0` → text passes).  
Logical-conn asks “does the value **actually propagate**?” (same example → logical fails).

**KO** 구조적 connectivity는 `hierwalk/connect/` 아래에 있습니다.

| 패키지 | 역할 |
|--------|------|
| `connect/shared/` | 모듈 탐색, 전처리, endpoint resolve, request/expand |
| `connect/text/` | Text-conn: RHS **이름 grep** |
| `connect/logical/` | Logical-conn: 비트 정밀 COI / 상수 접기 |
| `connect/hierarchy_grep_gate.py` | `connect_phase: hgrep` |
| `connect/pyslang_walk_gate.py` | `connect_phase: pyslangwalk` → text |
| `connect/pipeline/` | 아티팩트, 검증 |
| `connect/session.py` | `ConnectivitySession` |
| `pyslang_walk.py` | 경로별 pyslang open + hierarchy resolve |

## Modes · 모드

| Mode | EN | KO |
|------|----|----|
| `hierarchy` | Instance TSV (default) | instance TSV (기본) |
| `find-top` | Top-module candidates | top module 후보 |
| `search` | Path / instance search | path / instance 검색 |
| `check-connect` / `check-connect-batch` | Connectivity check | connectivity 검사 |
| `check-hgrep` / `connect_phase: hgrep` | Hierarchy grep gate only (`grep_hie.json`) | hierarchy_grep gate만 |
| `check-pyslangwalk` / `connect_phase: pyslangwalk` | grep_hie + pyslang hierarchy → text-COI | grep_hie + pyslang → text |
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
