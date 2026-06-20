# hierwalk

**EN** RTL hierarchy path-walk and structural connectivity verification (no pyslang).  
**KO** RTL hierarchy path-walk · 구조적 connectivity 검증 도구 (pyslang 없음).

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

## Modes · 모드

| Mode | EN | KO |
|------|----|----|
| `hierarchy` | Instance TSV (default) | instance TSV (기본) |
| `find-top` | Top-module candidates | top module 후보 |
| `search` | Path / instance search | path / instance 검색 |
| `check-connect` / `check-connect-batch` | Connectivity check | connectivity 검사 |
| `fanin-cone` / `fanout-cone` | COI cone | COI cone |
| `inst-trace` | Driver / sinker trace | driver / sinker trace |
| `path-walk` | Large SoC path-walk + connect | 대형 SoC path-walk + connect |

## Examples · 예제

**EN** `examples/stress_seed42/` — `path_walk_example.json`, `flat_run_example.json`, `search_example.json`  
**KO** `examples/stress_seed42/` — 위 JSON 템플릿

```bash
cd examples/stress_seed42 && hier-walk path_walk_example.json
```

## Tests · 테스트

```bash
python -m pytest tests/ -q
```