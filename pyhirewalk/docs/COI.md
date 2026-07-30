# COI & hier_conn — 문헌 요약 + 구현 스펙

> **Part A**: formal verification 문헌의 COI (사실).  
> **Part B**: pyhirewalk 구조 연결(`hier_conn`) 설계 (결정).  
>
> 증거(사용자 대면): **file / line / snippet** (+ path 순).  
> 로그: **타임스탬프 + 누적 초 + 현재 hierarchy**.

PDF 사본: `downloads/` (Clarke BMC, SPIN’25, NuSeen 등).

---

# 0. 본질·하드 제약

**목적:** 전 칩이 아니라, RTL 위 **부분 지식 그래프**를 질의(a↔b 연결)에 맞게 싸게 채운다.

| 금지 | 허용 |
|------|------|
| ~13k 전 파일 open/elab | path 조상 + cone이 밟는 모듈만 |
| 전 칩 합성 (수 시간) | 동일 subset 위 scoped 도구(opt-in) |
| “풀 넷리스트 필수” | 실패 시 approx / cut |

```text
build_db      → modules.json (이름→파일)
hier_resolve  → path 존재·스코프 (run_conn_check a∪b)
hier_conn     → a fan-out ∩ b fan-in meet + evidence
```

조상 RTL **위치**는 resolve path에 이미 있음(재검색 없음). 비용은 필요할 때 그 파일을 **해석**하는 것.

**도구 전략:** 기본 = script 구조 그래프. 난관(복잡 port/param/generate)만 **해당 클로저**에 scoped pyslang 등. 전 칩 pyslang 금지.

---

# Part A — 문헌 COI (요약)

## A.1 정의

형식 검증에서 **Cone of Influence reduction** = 명세에 영향 없는 상태 변수를 모델에서 제거해 상태 폭발을 줄이는 기법.

**Classical COI** (Clarke, Biere, Raimi, Zhu, FMSD 2001 §5.1):

1. 명세 변수에서 시작  
2. **dependency graph**: 노드=상태변수, 엣지=combinational dependency  
3. 그 closure = COI; 밖은 제거  

특수한 경우로 **Kurshan localization** 에 속한다고 문헌이 적시.  
**Bounded COI**: 유한 horizon \(k\) 에서 시점별 support만 취해 classical 의 부분집합으로 CNF 축소.

## A.2 다른 출처 (한 줄)

| 출처 | 요지 |
|------|------|
| NuSMV / NuSeen | 프로퍼티에 무관한 변수 제거 옵션 |
| AIG/IC3 (GipSAT 등) | 노드 COI = **fan-in 재귀** (transitive fan-in) |
| SPIN’25 | static COI vs on-the-fly(문맥) 제거 |
| FRAIG-BMC 등 | constraint COI = 제약 노드 fan-in |

## A.3 우리 문제와의 관계

문헌 COI = **프로퍼티 보존 모델 축소**.  
우리는 같은 dependency 아이디어로 **두 그룹 사이 structural path + evidence**.  
양방향 BFS·beam은 문헌 COI 정의가 아니라 **탐색 공학**.

---

# Part B — hier_conn 설계

## B.0 실행 형태

```bash
python3 hier_conn.py \
  --config run.json \
  --map essential.modules.json \
  --resolve hier_resolve.json \
  -o hier_conn.json
```

| 요구 | 구현 |
|------|------|
| class | `HierConnApp` (+ `ConnSearch`, `LocalDepGraph`) |
| 단일 실행 | `hier_conn.py` 직접 실행 (`src` bootstrap, install 불필요) |
| 입력 JSON | 기존 run JSON (`run_conn_check.checks` a/b, `defines`, `env`, `modules_json`) |
| resolve 결과 | **`--resolve` 필수** — 그 결과물만 seed |

## B.1 입력 계약

상세 run JSON: **`docs/run_json.md`**.

| 입력 | 용도 |
|------|------|
| `--config run.json` | checks `id`/`a`/`b`, `defines`, `env`(map 경로), `modules_json` 후보 |
| `--map` | modules JSON (**CLI 우선**, 없으면 config `modules_json`) |
| `--resolve` | `hier_resolve` 출력 JSON (**필수**) |

**seed 규칙 (핵심):**

```text
for each check:
  A_seeds = { p in check.a | resolve[p].status in {ok, ok_needs_detail} }
  B_seeds = { p in check.b | resolve[p].status in {ok, ok_needs_detail} }
  miss 인 path → COI 탐색 안 함, unconnected reason=resolve_miss
```

- resolve에 **없는** path → resolve_miss (conn이 다시 resolve 호출하지 않음; 결과물만 사용).  
- **a** = fanout(정방향), **b** = fanin(역방향).  
- leaf 위치(file/module/local name)와 인스턴스 체인(parent file, inst → child file)은 **resolve nodes** 에서만 취함.

## B.2 질의

각 check:

> \(A\_seeds \times B\_seeds\) 중 structural path \(s \xrightarrow{*} t\) 가 있는가?  
> 있으면 evidence를 path 위 엣지 **발견 순서(a→b path)대로 append**.

1차 = structural (assign / FF / named port_map). snippet 의미론 비목표.

## B.3 탐색 전략

### B.3.1 전제

1. **대상 축소:** seed = resolve 성공 hierarchy만.  
2. **그래프 온디맨드:** seed가 가리키는 file(+ port_map으로 새로 밟는 file)만 스캔. 전 RTL 금지.  
3. **방향 락:** a 쪽 expand = forward(driver→load)만, b 쪽 = backward(load←driver)만.  
4. **hierarchy depth 자유:** up/down/횡단 지그재그 허용; depth 단조 prune 금지.  
5. **evidence:** path 위 엣지 순 append. 재정렬·별도 다듬기 없음.

### B.3.2 시드 배치

```text
from resolve result for path p:
  leaf.file, leaf.name (base), leaf.module
  for consecutive nodes: register_instance(parent.file, child.base, child.file)

F_a, visited_a, label_a[net] = seed path(s)   # forward side
F_b, visited_b, label_b[net] = seed path(s)   # backward side
prev_a / prev_b for path reconstruct (edge evidence 체인)
```

같은 net에 여러 seed → label **합집합**, expand는 **1회** (OR 합류 캐시).

### B.3.3 Phase 1 — CONNECT (meet)

```text
while budget and (F_a or F_b):
  # 기본: 더 작은 frontier 쪽 expand (보통 fan-in/b)
  # meet 인접 이웃 최우선
  expand one net on chosen side

  side a:  forward neighbors (assign/FF load, port into child, …)
  side b:  backward neighbors (drivers, climb port to parent actual, …)

  on first visit of net n:
    if n already in other visited:
      MEET → reconstruct evidence a→meet→b, append edges in that order
      record pair (label_a × label_b) once (C5)

stop when:
  both frontiers empty | max_hops | max_nodes | optional max_meets
  (not a full proof of “no connection”)
```

**모듈 경계:**

- 로컬: assign/FF 로 net 이동.  
- 아래로: parent actual → child formal (`port_map`, resolve가 준 child file).  
- 위로: child formal → parent actual (b backward / a forward 각각 방향 규칙).  
- port 방향 unknown이면 경계 억지 확장 금지에 가깝게 skip (precision).

**예산 기본:** max_hops, max_nodes, (옵션) max_files. 초과 → `cuts[]`.

### B.3.4 Phase 2 — ORPHAN (b 잔여, 후순위)

Phase1 포화 후, **어느 a seed와도 meet 안 한 b-side open branch** 만:

- 계속 backward until const / FF 말단 / undriven / budget  
- late meet 있으면 pair 추가  
- 그 외 `orphans[]` (관심 밖·누락·버그 **힌트**, 단정 금지)

P1 구현에서는 Phase2 생략 가능; 전략상 위치만 고정.

### B.3.5 캐시

| ID | 역할 |
|----|------|
| C1 visited (side, file, name) | 같은 쪽 재expand 금지 |
| C2 labels | seed path 합집합 |
| C3 prev + evidence | path 재구성 → evidence append 순서 |
| C4 LocalDepGraph per file | 모듈 1회 스캔 |
| C5 meet pair set | (src_path, dst_path) 중복 방지 |

### B.3.6 전처리 (스캔 시)

파일 open 시: comment strip → **defines로 ifdef 평가** → 본문 전체 스캔.  
param/generate 정밀은 후순위; 실패 시 cut/approx.

## B.4 그래프 요소 (스캔)

**NetKey** = `(file, local_base_name)` (P1; select는 path/seed에 보존, 키는 base)

| 엣지 | 방향 |
|------|------|
| assign / combo | RHS → LHS |
| ff | D → Q |
| port_map | actual ↔ formal (경계 통과 시 파일 전환) |

## B.5 출력

```json
{
  "schema_version": 1,
  "meta": { "config", "module_map", "resolve", "defines", "stats" },
  "checks": [{
    "id": "cpu",
    "pairs": [{ "src", "dst", "evidence": [{ "file", "line", "snippet" }] }],
    "unconnected": [{ "src", "dst", "reason": "resolve_miss|no_meet" }],
    "orphans": [],
    "cuts": []
  }]
}
```

로그: check 단위 START/END, meet, `TOTAL_HIER_CONN_SEC`.

## B.5 전처리·모듈 스캔

1. `strip_sv_comments`  
2. **`apply_sv_ifdefs(text, defines)`** — 지시어 무시 금지.  
   `ifdef` = 정의됨, `ifndef` = 미정의 시 활성. 비활성 줄은 빈 줄(줄번호 유지).  
3. 모듈 **본문 전체** 1회 스캔 → profile (generate/param/concat 난이도).  
4. 단순 assign만이면 로컬로 충분.  
5. param/generate면 path **조상** 인스턴스 `#(.P)` 해석 (파일 위치는 resolve가 이미 앎).  
   리터럴 우선; 식별자 전달 실패 시 channel/approx.

**대입 형태 (bit 위치 주의):**

| 형태 | 정책 |
|------|------|
| `y=x` 동형 | bundle OK |
| `y[3:0]=x[7:4]` | range map 또는 실패 시 단정 금지 |
| `{a,b}` concat | 위치 재배치 — map 불가면 skip/unknown |
| `y=x&z` | structural 다중 선행; bit-accurate 단정 신중 |

generate·param slice 다량 → **channel 모드** (전수 bit 루프 금지).

## B.6 Evidence

- 사용자 필드: `{file, line, snippet}`  
- **순서 = a→b 경로 순** (b쪽 역탐색분은 reconstruct 시 뒤집기). 파일/줄 재정렬 금지.  
- 엣지 조건: LHS/RHS **역할** 일치 (`x<=0` vs `x<=y&z` 구분).  
- always: 본문 할당만; sensitivity clk/rst는 data fan-in 기본 제외.  
- **틀린 evidence > 미발견 경로** → precision 우선; 애매하면 엣지 안 만듦.

snippet을 NLP로 이해하진 않음. 정합성은 **그래프·역할·path** 로 검사; snippet은 사람 검수용.

## B.7 출력·로그

```json
{
  "checks": [{
    "id": "cpu",
    "pairs": [{
      "src": "...", "dst": "...",
      "evidence": [{ "file", "line", "snippet" }]
    }],
    "unconnected": [],
    "orphans": [],
    "cuts": []
  }],
  "meta": { "defines": [], "stats": {} }
}
```

```text
[ts] (+sec) hier_conn START check=cpu
[ts] (+sec) explore a=... / b=...
[ts] (+sec) meet ...
[ts] (+sec) TOTAL_HIER_CONN_SEC=...
```

## B.8 구현 페이즈

| Phase | 내용 |
|-------|------|
| **P0** | `hier_conn.py` + `--config` → checks 루프, JSON/로그 뼈대 |
| **P1** | 모듈 스캔(assign/FF/named port); ifdef+defines; **bi-meet + 2페이즈 골격**; C1–C5; evidence 경로 순; fixture e2e |
| **P2** | beam·fan-in 우세; slice overlap; param 체인; MD; multi-check |
| **P3** | channel/gen 정밀; **난관만** scoped pyslang; 다차원 |

**P1 비목표:** 전수 bit-accurate, 위치 포트 전부, 전 칩 elab.

---

# Part C — 결정 요약

1. 문헌 COI = dependency closure + 무관 변수 제거; 우리는 **a forward ∩ b backward meet**.  
2. 입력 = **run JSON** `run_conn_check.checks` (a/b).  
3. 기본 script 구조 그래프; 난관만 scoped 정밀 도구.  
4. 전 RTL/전 합성 금지; cone + path 조상만.  
5. OR 캐시·slice 키·evidence 경로 순·precision 우선.  
6. ifdef는 **defines 평가** (삭제 무시 아님).

---

## 참고 (문헌)

1. Clarke, Biere, Raimi, Zhu — BMC / FMSD 2001 §5.1 Classical & Bounded COI  
2. Telbisz et al. — On-the-fly COI, SPIN 2025  
3. NuSeen / NuSMV — COI reduction option  
4. Su et al. — GipSAT; COI = recursive fan-ins  
5. Kurshan — localization reduction (COI의 상위 개념으로 인용됨)

관련: `docs/run_json.md`, `DESIGN.md` §3.3.
