# hierwalk

합성용 module instance 스캔 + connectivity / path-walk 검증 (pyslang 없음).

```bash
pip install -e .
hier-walk design.f --top TOP -o out.tsv
hier-walk --help-config    # run JSON 필드 전체
```

## 참고 문서 (사용자 · LLM)

아래 파일·명령을 우선 참고하세요. 상세 필드는 `--help-config` / `--help-connect` / `--help-cone` / `--help-inst-trace`.

### 테스트 예제 vs 실전 SoC hierarchy

| 구분 | 어디서 | LLM이 읽을 문서 |
|------|--------|-----------------|
| **테스트·문법 학습** | `examples/stress_seed42/` | 이 README + JSON 헤더 주석 (pytest/CI용 작은 RTL) |
| **실전 hierarchy 검증** | `../hc_hierarchy/design/unified_verify/` | [`../hc_hierarchy/design/README.md`](../hc_hierarchy/design/README.md) → [`../hc_hierarchy/design/unified_verify/README.md`](../hc_hierarchy/design/unified_verify/README.md) |

`stress_seed42` JSON은 **run JSON 문법·`jobs`·env 템플릿**용입니다. 실제 SoC 블록·경로·기능 매핑은 hierarchy README 두 개를 따릅니다 (`hc_verify_top`, `filelist.f`, `run_pathwalk_*.json`).

**자사 RTL에 적용:** `path_walk_example.json`을 복사한 뒤 **`filelist` · `top` · `run_conn_check.checks[].a/b` · `output`만** 바꿉니다. `jobs`·`env`·`mode: path-walk` 구조는 그대로 둡니다.

| 참고 | 경로 | 용도 |
|------|------|------|
| Path-walk 옵션 (템플릿) | `examples/stress_seed42/path_walk_example.json` | `jobs` 병렬 walk, `env.HIERWALK_PW_DB_*` (EN+KO 주석 표) — **테스트용** |
| Flat run 템플릿 | `examples/stress_seed42/flat_run_example.json` | connect 한 step + trace/cone/index 주석 템플릿 |
| 전 step suite | `examples/stress_seed42/stress_42_d8.suite.json` | conn + inst-trace + cone path-walk |
| Search 패턴 | `examples/stress_seed42/search_example.json` | search 문법·플래그 (`hier-walk --example`로 복사 가능) |
| Connect batch | `examples/stress_seed42/stress_42_d8.connect.json` | checks 배치 JSON |
| Connect expand | `examples/connect_expand_verify/` | `[]` `{}` loop expand 검증 |
| **Waypoint fanout (PyCharm)** | `examples/waypoint_fanout_verify/` | `scripts/debug_waypoint_fanout.py`, `.run/` 설정, `connect_waypoint.json` |
| **Design corpus 개요** | [`../hc_hierarchy/design/README.md`](../hc_hierarchy/design/README.md) | synthetic / multihost / **unified_verify** 선택표 (`어떤 걸 쓸지`) |
| **통합 SoC hierarchy** | [`../hc_hierarchy/design/unified_verify/README.md`](../hc_hierarchy/design/unified_verify/README.md) | 블록·경로·기능 매핑, `run_pathwalk_*.json` |
| 통합 corpus 디렉터리 | `../hc_hierarchy/design/unified_verify/` | `filelist.f`, top `hc_verify_top`, 실행 JSON |
| Run JSON 도움말 | `hier-walk --help-config` | flat suite 필드·env·jobs |
| Path-walk env | `hier-walk --help` (HIERWALK_PW_DB_*) | post-verify DB build / prefetch cap |

`unified_verify`는 hierwalk 형제 디렉터리 `hc_hierarchy` 아래에 있습니다. 없으면 테스트가 skip됩니다 (`CodeFromAI/hc_hierarchy/...` 경로도 동일 corpus).

**PyCharm 디버그 (waypoint-fanout):**

| Run 설정 | 디버깅 | 용도 |
|----------|--------|------|
| **debug waypoint fanout** | ✅ (스택 얕음) | `waypoint_fanout` 핵심만 빠르게 |
| **hier-walk waypoint example** | ✅ (전체 파이프라인) | filelist → index → TSV 까지 |
| **pytest waypoint fanout trace** | ✅ | 테스트 한 건 |

공통: `src` → Sources Root (또는 `pip install -e .`). `.run/` 설정은 `PYTHONPATH=src` 포함.

`hier-walk` 디버그: Run **hier-walk waypoint example** → **Debug**(벌레). 브레이크포인트 예: `connectivity.py` `check()`, `waypoint_fanout.py` `_trace_origin_fanout`. check 1개면 단일 프로세스(스레드 풀 없음).

```bash
cd examples/waypoint_fanout_verify
python ../../scripts/debug_waypoint_fanout.py
python -m hierwalk.cli run_waypoint_fanout.json   # PyCharm과 동일
```

**실행 (테스트 corpus — `stress_seed42`):**

```bash
cd examples/stress_seed42
hier-walk path_walk_example.json
hier-walk flat_run_example.json
hier-walk search_example.json
```

**실행 (실전 SoC — `unified_verify`, hierarchy README 참고):**

```bash
cd ../hc_hierarchy/design/unified_verify
hier-walk run_pathwalk_deep_conn.json
hier-walk run_pathwalk_anchor_chains.json
# 기타 run_pw_*.json, run_unified_verify_*.json 참고
```

**생성 산출물** (`*.tsv`, `*.hier-walk.log`)은 `.gitignore` 대상이며 example 실행 시 자동 생성됩니다.