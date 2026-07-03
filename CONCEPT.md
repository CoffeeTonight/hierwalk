# hierwalk — 도구 컨셉 & 구현 로직

> **목적**: 이 문서만 읽고 hierwalk를 처음부터 다시 구현할 수 있도록, **함수명 없이** 설계·로직·정책·회귀 교훈을 정리한다.  
> **대상 독자**: LLM, 후속 개발자.  
> **최종 갱신**: 2026-07-02

---

## 1. 이 도구가 하는 일

**hierwalk**는 대형 SoC RTL filelist에서 **계층(instance) 탐색**과 **구조적 connectivity 검증**을 수행하는 도구다.

- 파서/시뮬레이터 없이 **정규식 + 경량 전처리**만 사용한다.
- 수천~수만 RTL 파일에서도 동작해야 한다. 전체 design을 한 번에 elaborate하지 않는 **lazy path-walk** 모드가 핵심이다.
- 입력: filelist(`.f`), top module, `+define+`, ignore 규칙, connectivity check JSON.
- 출력: instance TSV, connectivity TSV, trace/cone TSV, 검색 결과 등.

**두 가지 큰 실행 전략**

| 전략 | 언제 | 특징 |
|------|------|------|
| **full-index** | 소~중형, hierarchy/search/find-top | filelist 전체를 index에 올린 뒤 elaboration |
| **path-walk** | 대형 SoC, connectivity/trace/cone | **걸린 경로만** tier0/tier1로 점진 resolve |

기본 철학: **“전체 RTL preprocess 금지, 필요한 파일만, 필요한 깊이만.”**

---

## 2. 입력·설정 모델

### 2.1 filelist

- Verilog/SystemVerilog 소스 목록, library dir, include dir, `+define+`.
- filelist가 **계층형**(`.f` 안에 `-f` 중첩)이면 **shell tree 메타**를 추출한다.
  - 어떤 RTL이 어떤 listing에 속하는지
  - listing 간 parent/child 관계
- flat filelist(단일 `.f`에 파일만 나열)는 hierarchy 메타가 없다 → 다른 탐색 전략 사용.

### 2.2 run JSON (flat suite)

한 JSON에 여러 단계를 형제 블록으로 둔다.

| 블록 | 역할 |
|------|------|
| `run_on_full_index` | hierarchy / search / find-top (full-index 전용) |
| `run_conn_check` | point-to-point connectivity |
| `run_io_trace` | instance driver/sinker trace |
| `run_cone_trace` | fanin/fanout COI cone |

각 verification 블록의 `mode`는 **index 전략**만 고른다: `full-index` vs `path-walk`.

**중요한 merge 규칙**

- `run_on_full_index.enable=0`이면 full-index 단계는 아예 비활성.
- 이때 verification 블록이 `full-index`를 요청해도 **강제로 path-walk**.
- `run_on_full_index`의 `ignore-path: []`는 **그 블록 자체가 비활성일 때** per-step ignore를 지우지 않는다. per-step ignore가 살아 있어야 한다.
- ignore, jobs, cache-dir 등은 **path-walk만 쓸 때는 각 verification 블록에 직접** 둔다.

### 2.3 ignore-path

세 종류 패턴:

1. **경로 glob** — resolved absolute path에 매칭 (`QR_*`, `pcielinktop` 등)
2. **module 이름** — 해당 module을 ignorePath stub으로 처리
3. **filelist 이름** — 특정 listing subtree 제외

동작 원칙:

- ignore된 RTL은 **parse source list에서 제거**한다 (path-walk DB 초기화 시 partition).
- include 전처리 시에도 ignore 경로는 stub으로 대체, 실제 파일 read 안 함.
- hierarchy에서 ignore module은 **leaf stub** — 내부 인스턴스 탐색 중단.

**반드시 지킬 것**: path-walk index 생성 시 filelist의 **전체 소스**가 아니라 **partition 후 남은 소스만** module DB에 넣는다. 안 그러면 `thru=2/8866`처럼 ignore가 안 먹고 전체 filelist를 순회한다.

---

## 3. Design Index (공유 메모리 모델)

전체 design의 **module name → ModuleRecord** 맵. path-walk와 full-index가 공유한다.

### 3.1 lazy 기본

- index 생성 시 **filelist seed define만** 갖고 시작한다 (`+define+`).
- **전체 RTL을 돌며 define을 merge하지 않는다** — hot path(`top.a` 첫 신호)에서 수천 파일 스캔 방지.
- module body가 필요할 때 **그 파일 하나만** preprocess한다.
- 전체 define merge가 꼭 필요한 connect 호환 경로만 **명시적 full walk**를 허용한다.

### 3.2 preprocessed body 공유

- path-walk tier1이 어떤 파일을 preprocess하면, 그 결과 텍스트를 index에 **등록**한다.
- 이후 text-conn / logical-conn / port scan은 **같은 body를 재사용**한다. 파일당 preprocess 1회화.
- text-conn용 **전체 design prewarm**은 기본 **꺼짐** (opt-in만).

### 3.3 parse_sources

- ignore partition 후 실제 parse 대상 경로 목록.
- index의 module 맵과 path-walk DB의 source list가 **동일 집합**이어야 한다.

---

## 4. Path-walk Module DB — 핵심 아키텍처

path-walk 전용 **증분 module DB**. full-index build/load와 **분리된 캐시 네임스페이스**.

### 4.1 데이터

- **source list**: partition된 RTL 절대경로, filelist 순서 유지.
- **defines**: filelist seed + tier1에서 lazy 누적.
- **per-file 캐시** (disk sidecar, pickle):
  - **regex tier**: 파일에서 module 선언 이름만 (전처리 없음)
  - **validated tier**: instance scan까지 끝난 ModuleRecord
  - **preprocessed tier**: 전처리된 단일 파일 텍스트
- **in-memory**: module→file 후보, instance edge, inst-leaf index.

캐시 키에 포함되는 것:

- 파일 content digest
- defines digest
- include closure digest (아래 참고)
- preprocess tag: `no-inc` vs `inc` (include 인라인 여부)

### 4.2 Tier-0 — module 선언 찾기 (preprocess 금지)

**목적**: “이 module 이름이 어느 RTL 파일에 선언돼 있나?” 후보 수집.

**절대 하지 않는 것**: tier0에서 preprocess, instance scan, include 전개.

**방법**:

- 파일을 한 줄씩 읽으며 `module`/`interface`/`program` 선언 정규식 매칭.
- 가벼운 `` `ifdef `` / define 가드로 **비활성 module 숨김**.
- 결과는 per-file regex sidecar에 캐시.

**후보 수집 분기 (순서 고정, 바꾸지 말 것)**:

```
1. scope anchor 있음 (현재 계층 위치의 RTL)
   → scoped pool + tier0 scan
   → CONFIDENT + hit → return
   → CONFIDENT + miss → return []   (전역 queue 안 탐)
   → RECOVERY → 아래로 계속

2. filelist hierarchy 메타 있음 + center listing 있음
   → progressive shell scan (FAST PATH, opt-in 성격이지만 hierarchy 있을 때만)
   → CONFIDENT면 confident miss 시 [] return

3. 기본 경로: ranked regex queue scan
   → filelist proximity + 이름 유사도로 파일 순위
   → 배치 병렬 submit, target module hit 시 **미제출 조기 종료**

4. policy == RECOVERY
   → stub만 map에 있어도 global tier0 반드시 실행

5. 후보 파일 정렬 후 return
```

**policy 두 종류**:

| policy | 의미 |
|--------|------|
| **CONFIDENT** | 정상 child resolve. scoped/progressive로 좁힌 뒤 miss면 빈 후보 허용 |
| **RECOVERY** | 이전에 stub/미해결로 남은 instance 재시도. scoped hit해도 global scan 계속 |

**progressive shell scan**: filelist tree를 BFS로 shell 단위 tier0. 전역 13k queue를 한 번에 돌지 않음. **flat filelist에는 사용 금지** (메타 게이트 필수).

**tier0 병렬**: 소스 N개 이상이면 process pool. 배치 단위 submit + target hit 시 남은 배치 skip.

### 4.3 Tier-1 — validated instance scan (preprocess 허용)

**목적**: 후보 파일에서 실제 module body 파싱, instance edge, parameter, generate 등.

**흐름** (파일 하나 touch 시):

1. **define 누적** (lazy, scoped) — 아래 4.4
2. **include closure digest** 계산 — 아래 4.5
3. sidecar hit 검사 (content + defines + closure + preprocess tag)
4. miss 시 **단일 파일 preprocess** → validated scan → sidecar 저장
5. preprocess 결과를 **DesignIndex에 등록** (body 공유)

**tier1 preprocess include 정책 (기본 off)**:

- 기본: **해당 RTL 파일 텍스트만** 처리. `` `include `` 를 인라인하지 않음 (`no-inc`).
- 이유: wrapper `.v` 하나가 include tree 전체(수 MB, 수백 초)를 끌어올 수 있음.
- opt-in: 환경변수로 include 인라인 허용. 대형 tree에서는 매우 느림.

**tier1에서 preprocess 1회화**: 같은 파일·같은 define·같은 tag면 재호출 안 함.

### 4.4 Define 누적 — lazy & scoped

**금지**: tier1 직전에 source[0..N] 전체에 대해 define merge + include closure.

**올바른 방식**:

- filelist 순서 index `idx`까지, **아직 누적 안 한 구간만** batch.
- filelist hierarchy가 있으면: 현재 파일의 **ancestor listing chain**에 속한 RTL만 batch.
- flat filelist: chain 없음 → **global prefix** (0..idx) — flat 환경 호환 KEEP.
- `` `include `` 를 따라 define 수집하는 것도 **기본 off** (define-only 스캔).
- batch 크기 상한 (기본 128). 초과 시 **target 파일만** 누적.

**define cache 무효화 금지**: 파일 module map 갱신할 때 define 누적 상태를 리셋하면, 매 preprocess마다 0..idx 재스캔 → 수 분 병목. cache는 filelist/define stamp가 바뀔 때만 무효화.

### 4.5 Include closure digest

sidecar 유효성 검사용. preprocess 입력이 바뀌었는지 빠르게 판별.

**기본 (direct / 1-hop)**: 해당 파일 안의 `` `include `` 줄에서 **직접 참조만** digest. transitive BFS 안 함.

**opt-in full closure**: transitive include BFS + 상한 (기본 200). 느리면 hang — 기본 off.

**include resolve 최적화**: `is_file()` 우선, resolved path 캐시.

### 4.6 Instance resolve (tier0 → tier1)

1. tier0로 module→file 후보
2. 후보를 ranking 정렬
3. tier1 validated scan으로 child instance edge 확정
4. 실패 시 deferred queue → recovery pass

**recovery pass**: session 내 미해결 instance를 RECOVERY policy로 재시도. iteration 상한 있음.

### 4.7 Path-walk 그래프 탐색

- top에서 시작, 사용자 endpoint (`top.u_mid.sig`)까지 **경로를 따라** instance를 열어 감.
- 각 hop마다 tier0/tier1이 **필요할 때만** 호출.
- COI / constant fold / text grep은 별 패키지에서 이 walk row를 소비.

**DB build 모드**:

- 기본: **lazy** — verify 중 touched RTL만 tier0/tier1.
- opt-in: verify 끝난 뒤 full tier1 prefetch (백그라운드 스레드).

---

## 5. Preprocess 엔진

### 5.1 역할

- `` `define `` / `` `undef `` / `` `ifdef `` 처리
- `` `include `` resolve (include dir + source 상대경로)
- 매크로 간단 확장, comment strip
- ignore-path에 걸린 include는 stub comment로 대체

### 5.2 호출 위치별 정책

| 호출 맥락 | include 전개 | 범위 |
|-----------|--------------|------|
| tier0 regex scan | **안 함** | — |
| tier1 define accumulate | **기본 안 함** | define/ifdef only |
| tier1 module preprocess | **기본 안 함** (`no-inc`) | 단일 TU |
| full-index / 명시 full define | 설정 따름 | 넓을 수 있음 |
| text-conn prewarm | opt-in | 전체 design |

### 5.3 관측 로그 (`HIERWALK_PP_LOG=1`)

stderr에 단계별 ms / MiB / tag:

| 태그 | 의미 |
|------|------|
| `pp-defines` | define 누적 (files, chain, thru=i/N) |
| `pp-closure` | include closure digest 시작 |
| `pp-miss` | preprocess cache miss (ms, MiB, `no-inc`/`inc`) |
| `pp-t0` | tier0 scan (preprocess 없어야 정상) |
| `pp-t1` | tier1 validated 경로 |

**해석 예**: `pp-miss bla.v 1111ms 2.3MiB no-inc` → include 없이도 파일 자체가 2.3MB이거나 macro 확장 결과. `thru=2/8866` → source list가 8866개면 ignore partition 미적용 의심.

---

## 6. Connectivity 파이프라인

### 6.1 공통

- endpoint 문자열 파싱: hierarchy path, bit select, array slice.
- path-walk로 instance chain + module body 확보.

### 6.2 Text-conn

- 질문: “RHS에 이 **이름**이 텍스트로 나오나?”
- module body에서 name grep. 값 전파/상수 fold는 안 봄.
- `assign a = b * 0` → b 이름 있으면 **pass** (text 기준).

### 6.3 Logical-conn

- 질문: “값이 **실제로** 전파되나?”
- bit-precise COI, constant fold, generate param resolve.
- 위 예 → **fail** (logical 기준).

### 6.4 실행 순서 (전형적)

1. path-walk index + module DB 생성 (partition, lazy defines)
2. top seed + 첫 hop tier1 prewarm (첫 endpoint cold miss 방지)
3. check마다 walk → text 또는 logical 판정
4. TSV artifact 출력

**ConnectivitySession**: logical 단계에서 불필요한 param dim resolve는 끄는 경량 모드 사용.

---

## 7. 캐시·디스크 레이아웃

기본 작업 디렉터리: `--index-cwd` 또는 cwd 아래 `.db_{TOP}/`

```
.db_{TOP}/
  path-walk-db/{cache_key}/
    regex/{file_token}.pkl
    validated/{file_token}_{defines}_{tag}.pkl
    preprocessed/...
    module_index.tsv      # 사람이 읽는 스냅샷
  (full-index 캐시는 별 네임스페이스)
```

**cache_key**: source 목록 digest + defines + include dirs + skip patterns + schema version.

**원칙**:

- content digest가 바뀌면 sidecar 무효.
- defines digest가 바뀌면 validated/preprocessed 무효.
- preprocess tag (`inc`/`no-inc`) 다르면 공유 안 함.

---

## 8. 성능·동작 knob (환경변수)

| 변수 | 기본 | 의미 |
|------|------|------|
| `HIERWALK_PW_TIER1_INCLUDES` | `0` | tier1 preprocess include 인라인 |
| `HIERWALK_PW_DEFINE_INCLUDES` | `0` | define 누적 시 include 추적 |
| `HIERWALK_PW_DEFINE_ACCUM_MAX` | `128` | define batch 상한 (0=무제한) |
| `HIERWALK_PW_INCLUDE_CLOSURE_FULL` | `0` | transitive include closure |
| `HIERWALK_PW_INCLUDE_CLOSURE_MAX` | `200` | full closure 상한 |
| `HIERWALK_PW_FL_SHELL_MAX` | `12` | progressive shell depth cap |
| `HIERWALK_PW_MODULE_FILE_CAP` | `32` | confident tier0 per-step file cap |
| `HIERWALK_PW_TIER0_GLOBAL_MAX` | `128` | recovery global scan cap |
| `HIERWALK_PW_DB_BUILD` | `off` | post-verify full DB build |
| `HIERWALK_TEXT_GREP_PREWARM` | `0` | text-conn 전체 prewarm |
| `HIERWALK_PP_LOG` | — | preprocess 단계 로그 |
| `HIERWALK_IGNORE_PATH` | — | 기본 ignore glob |

---

## 9. 설계 원칙 (반드시 지킬 것)

### 9.1 KEEP vs fast path

- 검증된 기본 경로(**KEEP**)를 성능 개선으로 **대체하지 않는다**.
- fast path(progressive shell, parallel early exit 등)는 **메타/조건 있을 때만** 진입.
- 조건 없으면 기본 regex queue로 fall through.

### 9.2 작업량 최소화가 1순위

Python 언어 한계보다 **불필요한 전처리 범위**가 병목의 대부분.

- include off만으로 preprocess **수백 초 → 1초대** 가능 (실측).
- ignore partition으로 source 수 자체를 줄인다.
- define/effective_defines 전역 walk는 hot path에서 금지.

### 9.3 tier 경계

| tier | 허용 | 금지 |
|------|------|------|
| 0 | line-at-a-time decl regex, 가벼운 ifdef | preprocess, instance scan |
| 1 | 단일 파일 preprocess, validated scan, define lazy 누적 | 전역 0..N 일괄 preprocess |

### 9.4 회귀에서 배운 anti-pattern

| 실수 | 결과 | 교훈 |
|------|------|------|
| progressive가 default regex queue **대체** | flat/recovery/parallel 깨짐 | fast path 추가 ≠ default 교체 |
| tier0 전역 queue 13k 일괄 | top 찾기 10분+ | hierarchy 있을 때만 progressive |
| text-conn 시 전체 preprocess prewarm | path-walk 이점 상실 | lazy + body 등록 1회화 |
| recovery에서 scoped hit 후 return | stub만 있는 module 영구 미해결 | RECOVERY는 global scan 계속 |
| define 누적에 include closure | tier1 전 0..idx+include hang | chain scope, include off |
| `top.a`에서 effective_defines 전역 | 13k define walk | seed define만 |
| include closure 무제한 BFS | `pp-*` 전 hang | 1-hop digest 기본 |
| module map 갱신 시 define cache wipe | 매번 0..idx 재누적 | stamp 기반만 무효화 |
| path-walk init에 partition 생략 | ignore 무용, 8866 전체 순회 | partition 후 source만 DB |

---

## 10. 배포·실행 환경 가정

- **OS**: 회사 기본 Linux. 시스템 패키지 추가 설치 어려울 수 있음.
- **pip**: 허용. `pip install -e .` 로 패치본 고정 권장.
- **추가 네이티브 런타임**(Rust/Go 등) 없이 Python + stdlib + wheel로 동작.
- `python -m hierwalk` 로 CLI entry 가능 (`hier-walk` 스크립트 없어도 됨).

---

## 11. LLM 재구현 체크리스트

구현 시 아래 순서로 맞추면 된다.

### Phase A — 기반

- [ ] filelist 파서 (sources, libs, incdir, defines, nested `.f` 메타)
- [ ] ignore-path: glob 매칭, partition, stub scan
- [ ] DesignIndex: module map, lazy seed defines, preprocessed body 등록
- [ ] preprocess: define/ifdef/include (ignore stub), no-inc 모드

### Phase B — path-walk DB

- [ ] partition된 source list로 DB init (로그: N sources, M ignored)
- [ ] tier0: regex decl, policy 분기, queue/progressive/recovery 순서 고정
- [ ] tier1: lazy define 누적 → closure digest → preprocess → validated scan
- [ ] disk sidecar + digest cache key
- [ ] deferred + recovery pass

### Phase C — walk + connect

- [ ] hierarchy path parse, instance edge resolve
- [ ] path-walk row 모델, top seed + 첫 hop prewarm
- [ ] text-conn (name grep, lazy body)
- [ ] logical-conn (COI, constant fold)
- [ ] run JSON merge, flat suite, artifact TSV

### Phase D — 검증

- [ ] flat filelist tier0 queue + parallel early exit
- [ ] hierarchy progressive shell (deep 없이 root shell에서 top 발견)
- [ ] recovery가 stub map 이후 global scan
- [ ] ignore glob 시 QR/DW 파일 tier0/tier1 미호출
- [ ] tier1 no-inc 기본, opt-in inc
- [ ] define cache가 module map 갱신에 안 지워짐
- [ ] `pp-defines` thru가 partition N과 일치

---

## 12. 한 줄 요약

**hierwalk**는 대형 RTL에서 **전체 elaborate 없이**, filelist 계층·ignore·lazy define·tier0/tier1 증분 DB·단일파일 preprocess(`no-inc`)·sidecar 캐시로 **걸린 hierarchy path만 열고** text/logical connectivity를 검증하는 도구다. 빠름의 핵심은 더 빠른 언어가 아니라 **탐색 전에 하는 일을 줄이는 것**이다.