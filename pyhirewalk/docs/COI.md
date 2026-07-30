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

## B.1 입력 (run JSON, build_db와 동일 문서)

상세 스키마: **`docs/run_json.md`**.

```jsonc
{
  "filelist": "...", "top": "...", "jobs": 8,
  "env": { "PROJ": "..." },           // .f 경로 $VAR
  "defines": { "NO_CPU": "1" },       // `ifdef / +define+
  "build_db": { "modules_json": "work/essential.modules.json", ... },
  "run_conn_check": {
    "checks": [
      {
        "id": "cpu",
        "a": [ "top.u_src.sig", "top.u_src.bus[3:0]" ],  // 기본 fanout
        "b": [ "top.u_dst.sig" ]                         // 기본 fanin
      }
    ]
  }
}
```

| 도구 | JSON 사용 |
|------|-----------|
| build_db | filelist, top, env, defines, build_db, jobs |
| hier_resolve | **checks a∪b만** path (`load_hier_resolve_inputs`); defines/map만 부가 |
| hier_conn | 동일 checks 그룹 + modules map + resolve 결과 |

- **a** = fanout(정방향), **b** = fanin(역방향). `a_role`/`b_role` 로 덮어쓰기 가능.  
- resolve **miss** leaf는 그룹 제외.  
- `port_dir`: resolve에 있으면 사용; 없으면 **conn이 모듈 스캔으로 채움** (resolve 선행 필수 아님).

## B.2 질의

각 check에 대해:

> \(\exists\, s\in a,\; t\in b\) structural path \(s \xrightarrow{*} t\) ?  
> 있으면 evidence를 **fanout→fanin 경로 순**으로.

1차 = structural (assign / FF / port_map). 의미론·타이밍 비목표.

## B.3 그래프

**NetKey** = `(module_or_file, base_name, select_key)`  
같은 base라도 `[3:0]` vs `[7:4]` 는 다른 노드. seed slice를 base로 뭉개지 않음.

| 엣지 | 의미 | 방향 (영향 driver→load) |
|------|------|-------------------------|
| assign / combo | LHS ← RHS idents | RHS → LHS |
| ff | Q ← D | D → Q |
| port_map | actual ↔ formal | 방향·port_dir에 맞게 |

**모듈 안:** port에 닿을 때까지 로컬; 그다음 port_map으로만 인접 모듈.  
**의존 방향 고정** ≠ hierarchy 깊이: up/down/횡단 **지그재그** 가능. nearness는 LCA·모듈 겹침이지 depth 단조 아님.

### 캐시 (필수)

| ID | 키 | 역할 |
|----|-----|------|
| C1 visited | (side, NetKey) | 공통 구간 expand 1회 (OR 합류) |
| C2 labels | 동일 | 도달 seed 집합 합집합 |
| C3 prev | 동일 | path reconstruct |
| C4 local | file + param context | uses/driven_by, ports, difficulty |
| C5 meet | pair id | 중복 리포트 방지 |

여러 a가 mid에서 합류해도 하류는 1번만 팜; label로 어느 a인지 복원.

## B.4 탐색: 2페이즈

### Phase 1 — CONNECT

```text
F_a ← a leaves  # forward only
F_b ← b leaves  # backward only
자원: b(fan-in) 우세; near-a 이면 a forward boost
포트: out/in 필터 (unknown 경계는 느슨히 열지 말고 skip)
smaller-frontier / meet-first / beam·budget
meet → pair + evidence (a-path + reverse(b-path))
포화 시 종료: frontier 소진 | meet 정체 | budget  (전수 증명 아님)
```

### Phase 2 — ORPHAN (b 역탐색 잔여)

meet 안 한 b 가지를 종단까지:

| 태그 | |
|------|--|
| term_const | `x<=0` 등 |
| term_ff | 시퀀셜 경계 |
| term_undriven / blackbox / orphan_cut | 미결정·후보 |

논리 연산 조기 절단은 meet 전에 쓰지 않음. late meet 허용.  
orphan = 관심 밖 / 기입 누락 / 버그 **힌트만** (자동 단정 금지).

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
