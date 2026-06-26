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

## J-005 — (슬롯) 추가 항목

**상태:** queued  

기타 회사/GLM 피드백 이어서 적기:

-