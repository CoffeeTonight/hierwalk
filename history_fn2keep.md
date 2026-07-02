# history_fn2keep.md

검증된 코드 경로·함수 레지스트리. **새 최적화는 이 목록의 동작을 대체하지 말고, opt-in 레이어로만 추가.**

변경 시 이 파일도 함께 갱신한다.

마지막 갱신: 2026-07-02 (tier1 define accumulate)

---

## 유지 원칙

1. **KEEP 먼저** — 아래 함수/흐름은 회귀 테스트로 보호된 기본 경로다.
2. **REPLACE 금지** — 성능 개선은 별도 fast path 추가 후, 메타데이터 없으면 KEEP으로 fall through.
3. **회귀 시** — 먼저 KEEP 경로가 살아 있는지 확인하고, fast path만 롤백한다.
4. **문서 갱신** — KEEP 함수 시그니처·분기 조건·가드 테스트를 바꾸면 이 파일의 해당 항목을 수정한다.

---

## path-walk module DB (`src/hierwalk/path_walk_db.py`)

### Tier-0 module decl resolve — `_ensure_regex_candidates`

**KEEP.** path-walk 전체의 module→file 후보 수집 진입점.

**고정 분기 순서 (순서 바꾸지 말 것):**

```
1. scope_anchor 있음
   → _scoped_pool_for_policy + _tier0_scan_sources
   → CONFIDENT + hit 이면 return
   → CONFIDENT + miss 이면 return []        # 전역 queue 안 탐
   → RECOVERY 는 hit 여부와 무관하게 아래로 진행

2. center_listing + filelist hierarchy 메타 있음
   → _has_filelist_hierarchy() == True 일 때만
   → _progressive_tier0_decl_scan (FAST PATH)
   → CONFIDENT + hit/miss 처리 후 return 또는 fall through 금지 (confident miss → [])

3. progressive 미사용
   → _tier0_regex_queue_scan (DEFAULT KEEP PATH)

4. policy == RECOVERY
   → 남은 소스 global tier0 (stub만 map에 있어도 반드시 실행)

5. _sort_module_files 로 return
```

| 함수 | 역할 | 대체 금지 이유 |
|------|------|----------------|
| `_scoped_pool_for_policy` | anchor/filelist 기준 scoped RTL pool | child resolve 정확도 |
| `_tier0_regex_queue_scan` | ranked `_regex_queue` + 배치 병렬 + hit 조기 종료 | flat filelist·메타 없음 환경 기본 |
| `_progressive_tier0_decl_scan` | filelist shell별 tier0 (전역 13k queue 없음) | **opt-in fast path** — hierarchy 메타 있을 때만 |
| `_has_filelist_hierarchy` | progressive 게이트 (`root_filelist` 또는 `filelist_children`) | flat `.f` 를 progressive로 보내지 않기 위함 |
| `_tier0_target_module` | recovery·dup decl 시 첫 hit 후에도 scan 계속 | recovery 회귀 방지 |
| `_tier0_scan_sources` / `_tier0_scan_sources_parallel` | sync vs parallel; **배치 단위 submit** + target hit 시 미제출 | parallel 조기 종료 |
| `tier0_regex_module_names_from_path` | line-at-a-time decl + light `ifdef`/`define` | preprocess 없이 tier0; gated module 숨김 |
| `_infer_root_filelist` | root listing 추론 | progressive center |
| `_sort_files_by_resolve_rank` | filelist proximity + 이름 유사도 정렬 | queue/progressive 순서 |

**tier0에서 preprocess 금지** — tier0는 `_tier0_scan_file` / `tier0_regex_module_names_from_path` 만. preprocess·instance scan은 tier1.

**tier1 preprocess 1회화:**

| 함수 | 역할 |
|------|------|
| `_publish_preprocessed_to_index` | tier1 preprocess 후 `DesignIndex.register_preprocessed_source` 시드 |
| `tier1_scan_file` (내부) | validated scan; index에 body 공유 |

### KEEP: tier1 define accumulate (lazy, scoped)

**`_ensure_defines_for_file`** — tier1/preprocess 직전 호출. **전역 0..N 소스 일괄 `accumulate_defines_from_file` 금지.**

| 함수 | 역할 | 금지 |
|------|------|------|
| `_define_chain_sources` | hierarchy 있으면 ancestor chain RTL; **flat `.f` 는 `None` → global prefix KEEP** | unrelated branch 13k 스캔 |
| `_ensure_defines_for_file` | chain 내·`_define_sources_upto`..idx 만 batch | `follow_includes` 기본 off |
| `accumulate_defines_from_file` | define/undef/ifdef only (`defines_only`) | tier1 에서 include closure 전개 |
| `preprocess_file_for_index` | **단일 touched file** full preprocess (include OK) | — |

**`HIERWALK_PW_DEFINE_INCLUDES=1`** — tier1 define accumulate 가 `` `include `` 를 따라감 (기본 **off**). 느리면 켜지 말 것; include 안 define 필요 시 filelist `+define+` 또는 RTL 직접 define 권장.

**`HIERWALK_PW_DEFINE_ACCUM_MAX`** (기본 128) — chain batch 상한; 초과 시 target 파일만.

### KEEP: lazy index body / defines (`index.py`, `port_scan.py`, `path_walk.py`)

| 함수 | lazy 기본 | 금지 |
|------|-----------|------|
| `DesignIndex.seed_preprocess_defines` | filelist `+define+` 만 | — |
| `DesignIndex.effective_defines(full=False)` | seed only | `top.a` 에서 13k `collect_design_defines` |
| `DesignIndex._source_text` | seed defines | `effective_defines()` 묵시 호출 |
| `port_index_for_design_module` | seed defines | — |
| `index.module_body` / `_source_text` | lazy 시 seed defines 로 **단일 파일** preprocess | `effective_defines()` 경유 13k walk |
| `port_index_for_design_module` | lazy 시 `seed_preprocess_defines` | `effective_defines()` 경유 |

`index.effective_defines()` 는 명시 호출 시 전체 RTL walk 유지 (connect 호환). `top.a` hot path 는 `_source_text`·port_scan 이 seed 사용.

전체 RTL define merge 필요 시 `collect_design_defines(...)` 명시 호출.

### 인스턴스 resolve (tier1)

| 함수 | 역할 |
|------|------|
| `resolve_child_edge` | tier0 후보 + tier1 validated 로 child edge |
| `_order_candidate_files` | `_ensure_regex_candidates` 래퍼 |

---

## text-conn / connect (`src/hierwalk/connect/`)

### KEEP: lazy prewarm (기본 off)

| 위치 | 함수/플래그 | 기본 |
|------|-------------|------|
| `perf.py` | `text_grep_prewarm_enabled()` | `HIERWALK_TEXT_GREP_PREWARM=0` |
| `connect/session.py` | prewarm 분기 | opt-in 일 때만 전체 RTL preprocess |
| `connect/text/index.py` | `module_body_for_text_grep` | `module_body_cache` 우선 |

**원칙:** text-conn grep 은 index body / cache hit 우선. 전체 design preprocess prewarm 은 명시 opt-in.

### KEEP: module body cache 연동

| 파일 | 내용 |
|------|------|
| `index.py` | `register_preprocessed_source` |
| `connect/text/walk.py`, `pair.py` | `module_body_cache` threading |
| `path_walk_db.py` | tier1 후 index 시드 |

---

## perf knobs (`src/hierwalk/perf.py`)

| env | 기본 | KEEP 의미 |
|-----|------|-----------|
| `HIERWALK_TEXT_GREP_PREWARM` | `0` | lazy prewarm |
| `HIERWALK_PW_FL_SHELL_MAX` | `12` | confident progressive shell cap |
| `HIERWALK_PW_MODULE_FILE_CAP` | `32` | confident per-module file cap |
| `HIERWALK_PW_TIER0_GLOBAL_SCAN_MAX` | (perf.py 참고) | recovery global scan 상한 |
| `HIERWALK_PW_DEFINE_INCLUDES` | `0` | tier1 define accumulate include 추적 (기본 off) |
| `HIERWALK_PW_DEFINE_ACCUM_MAX` | `128` | tier1 define batch cap (0=무제한) |
| `HIERWALK_PW_INCLUDE_CLOSURE_MAX` | `200` (full 모드만) | transitive closure 상한 |
| `HIERWALK_PW_INCLUDE_CLOSURE_FULL` | `0` | `1` 이면 transitive closure (느림) |

---

## 가드 테스트 (KEEP 경로 수정 시 필수 통과)

```bash
pytest tests/test_path_walk_db.py \
       tests/test_path_walk_resolve_policy.py \
       tests/test_path_walk_progressive_fl.py \
       tests/test_connect_pipeline_fixes.py::test_text_grep_prewarm_lazy_by_default \
       tests/test_perf.py::test_text_grep_prewarm_opt_in
```

| 테스트 | KEEP 보호 대상 |
|--------|----------------|
| `test_tier0_parallel_finds_module_without_waiting_for_all` | `_regex_queue` + parallel 배치 조기 종료 |
| `test_tier0_hides_ifdef_gated_module` | tier0 `ifdef` 필터 |
| `test_ensure_regex_candidates_recovery_scans_past_stub_map` | recovery stub map 후 global scan |
| `test_top_module_found_in_root_shell_without_deep_scan` | progressive fast path (hierarchy 있을 때) |
| `test_text_grep_prewarm_lazy_by_default` | text-conn lazy prewarm |

---

## 회귀 이력 (교훈)

| 날짜 | 실수 | 교훈 |
|------|------|------|
| 2026-07 | `_regex_queue` 를 progressive 로 **대체** → flat filelist·recovery·parallel 조기 종료 깨짐 | fast path 추가 ≠ default 교체 |
| 2026-07 | tier0 전역 queue 13k 한번에 → top 찾기 10분+ | progressive 는 hierarchy 메타 있을 때만 |
| 2026-07 | text-conn path-walk 시 전체 preprocess prewarm | lazy + `register_preprocessed_source` 1회화 |
| 2026-07 | recovery scoped-pool hit 시 조기 return | RECOVERY 는 map 에 stub만 있어도 global scan 계속 |
| 2026-07 | tier1 `_ensure_defines_for_file` 가 0..idx 전역+include closure | chain scope + `follow_includes=False` 기본 |
| 2026-07 | `top.a` signal-tail → `module_body` → `effective_defines` 13k | `_source_text`/`port_scan` seed defines |
| 2026-07 | tier1 `_include_closure_digest` 무제한 BFS → `pp-*` 전에 `_resolve_include` 강종 | direct closure + `_resolve_include` cache + `pp-closure start` 로그 |
| 2026-07 | `_apply_file_modules` 가 define cache 무효화 → 매 preprocess 0..idx 재누적 | define cache 유지; tier0 는 `_tier1_defines()` 사용 |
| 2026-07 | `top.a` 첫 hit cold preprocess + tier0 `NoneType` | seed 시 tier1 prewarm; `_tier0_make_job` 안전화 |

---

## 변경 체크리스트 (PR/패치 전)

- [ ] KEEP 함수를 삭제·인라인·다른 이름으로 흡수하지 않았는가?
- [ ] 새 fast path 가 메타 없을 때 `_tier0_regex_queue_scan` 으로 fall through 하는가?
- [ ] `RESOLVE_CONFIDENT` 와 `RESOLVE_RECOVERY` 분기를 바꿨다면 recovery 테스트 재실행했는가?
- [ ] tier0 에 preprocess 를 다시 넣지 않았는가?
- [ ] 이 파일(`history_fn2keep.md`) 해당 섹션을 갱신했는가?