# Queued work (hwalk)

작업 대기 목록. 구현 전 설계/우선순위 확정용.

---

## J-001 — pw-db 후보 정렬: filelist distance + 이름 유사도

**상태:** done  
**우선순위:** high (recovery tier1 지연 완화)

### 문제

- `recovery-pass start defer=N` 이후 `pw-db hit`까지 긴 침묵 구간이 자주 발생.
- stderr trace는 tier0/tier1/miss를 숨기고 hit만 보여서 “멈춘 것처럼” 보임.
- recovery 시 후보 RTL 탐색 순서가 **filelist proximity만** 사용 (`_sort_by_filelist_proximity`).
- RTL 관례상 `inst` / `module` / 파일 stem이 유사한 경우가 많은데, 이름 힌트를 쓰지 않음.

### 현재 코드

- `path_walk_db.py`: `_filelist_proximity`, `_sort_by_filelist_proximity`, `_sort_module_files`, `_order_candidate_files`
- tier0 큐·tier1 try 순서 모두 proximity 기반 ( `_prefer_file` 학습 hit 제외)

### 제안

1. 복합 rank key: `(proximity_score, -name_score)` — proximity 우선, 동점/근접 시 이름 유사도.
2. 이름 힌트 소스 (가용 시):
   - `module_name` ↔ `Path(rtl).stem`, 파일 내 `module` decl 이름
   - edge resolve: `inst_leaf` ↔ stem (접두 `u_` strip 등)
   - optional: `child_module` from defer item
3. 유사도는 가벼운 휴리스틱 (substring / common prefix / token overlap). Levenshtein 전체 설계에는 불필요.
4. tier0 batch 제출 순서와 tier1 candidate try 순서 **둘 다** 동일 ranker 적용.
5. `scope_anchor` 없을 때(global recovery)도 stem↔module_name으로 partial order.

### 검증

- 기존: `tests/test_path_walk_resolve_policy.py`, `tests/test_path_walk_db.py`
- 추가: 동일 proximity에서 `foo.v`가 `zzz.v`보다 `u_foo`/`module foo` 탐색 시 앞서는 단위 테스트
- 회귀: zigzag torture / flat-suite (recovery defer 케이스)

### 관련 env (변경 없음 가정)

- `HIERWALK_PW_TIER0_GLOBAL_MAX`, `HIERWALK_PW_TIER1_MAX`, `HIERWALK_PW_MODULE_FILE_CAP`

---

## J-002 — recovery 구간 관측성 (trace 필터 완화 또는 heartbeat)

**상태:** done  
**우선순위:** medium

### 문제

- `path_walk_trace_show_message()`가 tier0/tier1/resolve/miss를 stderr에서 제거 → recovery 디버깅 어려움.

### 제안 (택1 또는 병행)

- `HIERWALK_PW_TRACE_VERBOSE=1` 시 tier0/tier1/miss emit
- 또는 N초마다 `recovery: tier0=X tier1=Y parsing=foo.v` heartbeat
- `HIERWALK_LOG_SLOW_FILES=1` 문서화/기본 help 언급

**터치:** `hierarchy_log.py`, `path_walk_db.py`, `help_text.py`

---

## J-003 — hierarchy 완료 → conn worker 파이프라인

**상태:** done  
**우선순위:** medium

### 문제

- 현재 path-walk text-conn: **전 endpoint hierarchy walk 완료 후** text COI batch (serial, jobs=1).
- 논의: check 단위로 hierarchy ready → conn queue → worker 가 이론상 처리량 개선.

### 제안

- check-level ready queue: endpoint A/B walk 끝난 check만 conn에 투입
- `mod_cache` 공유 정책 유지 (conn parallel 시 cache lock / session 설계 필요)

**터치:** `path_walk.py`, `connectivity.py`, run config (`jobs` / `connect_jobs` 분리?)

---

## J-004 — hierarchy 이후 conn 병렬 (현실적 2–4×)

**상태:** done  
**우선순위:** low–medium

### 문제

- hierarchy `jobs:16`은 slice fan-out(동일 trunk)에 이득 적음.
- conn COI는 hierarchy 완료 후 unique check 많을 때 2–4× 정도 기대 (GIL/dedup/design 크기 한계).

### 제안

- `connect_jobs` 또는 path-walk connect phase 전용 worker pool
- `ConnectivitySession.run_text_request` coarse dedup과 병행 설계

---

## J-006 — assign probe: regex → pre-index (회사 무한-hang 재현)

**상태:** done (0.3.28+)  
**우선순위:** **critical** (4.5MB+ RTL, signal-tail / net_exists hot path)

**출처:** GLM 5.2 분석 + 회사 실사용 중 끊은 케이스

### GLM 지적 (코드 대조)

| 주장 | 판정 |
|------|------|
| `_regex_net_in_assign()` L666 `(?:[^;]\|\n)` 에서 `\n` 중복 — `[^;]`가 이미 `\n` 포함 | **맞음** |
| 중복 alternation이 백트래킹 유발 | **부분 맞음** — newline마다 두 갈래 매칭 가능; “기하급수”보다는 **대형 RTL에서 다항/선형 폭발**에 가깝음 |
| target net miss 시 모든 assign 블록 끝까지 regex 스캔 | **맞음** — `re.search` 3회가 **전체 clean body** 대상 |
| 파일 클수록 `check_ms` 폭발 (`signal-tail` trace) | **맞음** — `path_walk._classify_signal_tail` → `classify_signal_tail_kind` → `_net_base_in_assign_regex_fast` |
| lazy miss cache만으로는 첫 probe마다 full cost | **맞음** — `_assign_probe_miss_cache`는 (digest,target) **재조회**만 cheap |
| drive cache는 adjacency build 후에만 채워짐 | **맞음** — `_seed_assign_drive_bases_cache`는 `build_module_connect_index` 끝에서만; hot path는 adjacency 없이 regex 호출 |

### 현재 코드 (핫 패스)

```664:706:hwalk/src/hierwalk/connect_scan.py
def _regex_net_in_assign(clean: str, target: str) -> bool:
    ...
    rf"\bassign\b(?:[^;]|\n)*?\b{esc}\b"   # + <= 패턴 2개 더
```

- 호출: `net_exists_in_module_fast`, `classify_signal_tail_kind`, `net_base_in_assign_probe`
- 대형 body (≥64KB): drive cache hit 시 O(1) — **단, connect index를 한 번도 안 만든 모듈은 miss**
- miss cache: 최대 8192 entry, **첫 miss per (module,target)는 항상 regex 3-pass**

### GLM 수정안 vs 구현 (0.3.28+)

| GLM 제안 | 구현 |
|----------|------|
| `_regex_net_in_assign` `\n` 중복 제거 | ✅ `[^;]*?` only |
| ≥256KB body assign: `str.find` O(n) | ✅ pre-index (`collect_assign_net_names`) + `_str_find_net_in_assign` 보조 |
| 대형 body 첫 miss → assign base frozenset | ✅ `_ensure_assign_drive_bases_index` |
| ≥256KB `_net_base_in_port_map_regex_fast` 문자열 fallback | ✅ pre-index (`instance_port_maps`) + `_str_find_net_in_port_map` |
| 임계값 256KB | ✅ `_LARGE_MODULE_PROBE_MIN = 256 * 1024` |

### 기대 효과

- 4.5MB 모듈 × N개 signal-tail probe: **O(N × file_size × #assign)** regex → **O(file_size) 1회 + O(N) lookup**
- 회사 “거의 무한” 체감의 주요 원인 후보 (recovery tier1과 **별도** 핫패스)

**터치:** `connect_scan.py`, `connect_endpoints.py`, `tests/test_connect_index_perf.py`

---

## J-005 — zigzag 종합 검증 무한 루프

**상태:** in_progress  
**우선순위:** high  
**작업 경로:** `~/tools/hierwalk` (프로젝트 `.venv` — `./scripts/dev-setup.sh`)

### 명제 (매 회차 반복)

1. **hierarchy TSV RTL 확장** — hit 노드에 RTL 절대경로·provenance
1-1. **설계 패턴 보강** — ROI 큰 / 실사용 패턴을 `zigzag_torture_gen.py` RTL·suite에 반영
2. **JSON conn phase 선택** — `connect_phase: "text"` / `"logical"` per-test
3. **복잡 conn JSON** — wire/ref/port/inst, 배열·list expand, fanout, intentional fail
4. **fanin/out + cone + io_trace** — blackbox, depth, ff/reg, cone decoy

**루프:** 패턴 추가 → suite verify → pytest → 이슈 수정 → PASS여도 1-1 미커버 있으면 다음 회차

### 1-1 패턴 표 (발췌)

| 패턴 | ROI | RTL / check | 상태 |
|------|-----|-------------|------|
| many→one fan-in merge | 높음 | `zz_fanin_merge` @ d4 (`merge_tap`) | done (회차17) |
| fan-in decoy | 중간 | `zz_fanin_merge_decoy` @ d4 | done (회차17) |
| inst port XOR expr | 높음 | `zz_port_expr_xor` @ d2 (`u_bridge_expr.din`) | done (회차17) |
| casex/casez route | 중간 | `zz_casex_route` / `zz_casez_route` @ d1/d3 | done (회차18) |
| ifdef/gen pass | 중간 | `zz_ifdef_pass` / `zz_gen_pass` @ d4/d5 | done (회차18) |
| bridge/merge probes | 중간 | `zz_zig_*`, `zz_merge_dummy`, `zz_expr_mapped` | done (회차18) |
| blackbox through | 중간 | `zz_bb_through` (hub `u_bb`) | done (회차18) |
| loop/concat expand | 높음 | `zz_loop_*`, `zz_literal_concat` | done (회차18) |
| inst port concat/OR | 높음 | `zz_port_concat`, `zz_port_expr_or` | done (회차18) |
| 4-signal fan-in | 높음 | `zz_fanin_merge4` @ d4 | done (회차18) |
| gen-for / mid-ifdef | 중간 | `zz_gen_for_unroll`, `zz_mid_ifdef_child` | done (회차18) |
| ifdef inactive (neg) | 중간 | `zz_ifdef_inactive` @ d4 | done (회차18) |

### 회차17 (1-1) — verify clean

- **RTL:** d2 `u_bridge_expr(.din(chain_in ^ shallow_return))`; d4 `merge_tap = fork_main[1][2] \| shallow_return[1][2]`
- **conn:** `zz_fanin_merge` — `shallow_return` + `fork_main` → `merge_tap` (`chain_in` 제외: text-conn fork 경로 한계)
- **검증:** `test_run_and_verify_zigzag_suite` PASS; `_build_checks()` / `_suite_conn_checks()` 동기화
- **개발 환경:** `~/tools/hierwalk/.venv` (이동 시 `./scripts/dev-setup.sh` 재실행)

### 회차19 (1-1) — subagent 피드백 반영

- **verifier:** logical phase 기본 `expect_connected: true` (display/hierarchy-only skip); text phase explicit False만 verdict 검증; `CONN_VERDICT_SKIP_IDS` 단일화 (`suite_conn_policy.py`)
- **RTL/check:** `zz_gen_tap1`, `zz_pong_replicate`, `zz_ff_barrier_tap`, `zz_multi_g3_empty`; `DESIGN_SUITE_CHECK_ALIASES`; suite `expect_connected` 일괄 부여; `zz_dw_vendor_inst` design-only (suite `zz_dw_vendor_ignored`)
- **cone/io:** merge_quad, literal_bus, mid_ifdef + io_trace 3종
- **규모:** design **46**, suite **58**, cone **18**, io **13**
- **아티팩트:** `HIERWALK_ARCHIVE_SUITE=1` 시 full suite PASS 후 `~/tools/zz_suite_artifacts/run_*` 보존
- **보강:** `_hierarchy_covers_path` check_id 무관 inst hit 제거; text summary skip-ID·positive/negative disconnect 노이즈 필터; round19 flat-suite 회귀 assert

### 회차18 (1-1) — gap-fill verify

- **RTL:** d1 `gen_tap0`+generate-for; d2 `u_bridge_concat`/`u_bridge_or`; d3 `u_mid_ifdef`; d4 `merge_quad`+`ifdef_else_net`; d5 `gen_pass_flat`; top loop ties+`literal_bus`+`u_dw_vendor`; blackbox `dout=din`
- **conn:** design **42** + suite **55** (+20); cone **15** + io **10**
- **검증:** fast pytest PASS → `test_run_and_verify_zigzag_suite` (full)

### 다음 1-1 후보

- generate hierarchy path (`g_real_d5.gen_pass`) when fold enabled
- 회차13~16 패턴 복원 (`zz_ifdef_else`, `zz_ff_barrier`, …)