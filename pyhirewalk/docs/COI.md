# COI (Cone of Influence) — 전문 문헌 노트 & pyhirewalk 적용 방안

> **문서 성격**
>
> - **Part A**: 형식 검증·하드웨어 모델 체킹 문헌에서 정의된 COI (직관 설명 아님, 원문 정의 기반).
> - **Part B**: 그 정의를 pyhirewalk의 “두 hierarchy 신호 그룹 간 구조적 connectivity”에 **어떻게 매핑**할지 (프로젝트 계획).
>
> 구현(`hier_conn`)은 이 문서 리뷰 후에만 진행한다.  
> 증거는 **file / line / snippet** 만. 로그는 **타임스탬프 + 누적 초 + 현재 hierarchy**.

로컬 원문 PDF (읽은 사본):

| 파일 | 내용 |
|------|------|
| `downloads/clarke_biere_bmc_fmsd2001.pdf` | Clarke, Biere, Raimi, Zhu — BMC + classical/bounded COI |
| `downloads/spin25_onthefly_coi.pdf` | Telbisz et al., SPIN 2025 — static vs on-the-fly COI |
| `downloads/nuseen_nusmv.pdf` | NuSeen — NuSMV COI option 설명 |
| `downloads/fmcad98_coi.pdf` | Berezin, Biere, Clarke, Zhu FMCAD’98 — COI as reduction |
| `downloads/complexity_reduction.pdf` | CMU lecture — COI among state-explosion techniques |

---

# 0. 한 줄 프레이밍 (프로젝트 본질)

> **이건 전 칩 RTL을 다 읽는 게임이 아니라,  
> RTL 위의 knowledge graph 를 “목적에 맞는 정보만”으로  
> 얼마나 싸게 완성하느냐의 게임이다.**

## 0.1 하드 제약 — **전 RTL / 전 칩 합성 금지**

| 사실 | 함의 |
|------|------|
| RTL **~13k 파일** | 질의마다 전 파일 open·전량 parse/elab **불가** |
| 전 칩 합성 **4–5시간** 급 | Yosys/상용 합성 **전체 넷리스트** 를 conn 경로에 두면 안 됨 |
| 어떤 도구든 | 입력이 “design 전체 filelist 필수” 이면 **채택 조건 위반** |

**허용되는 입력 범위 (전부 “목적 cone” 한정):**

```text
OK:  modules.json 이 가리키는 후보 파일 중
     hier_resolve path 조상 + port_map 으로 새로 밟는 모듈만
OK:  그 모듈 본문 1회 파싱 / (옵션) 그 파일들만 scoped pyslang
OK:  동일 파일 집합만 scoped Yosys·slang-netlist (opt-in, 실패 시 skip)
NG:  13k 전체 slang elab / 전체 Verilator XML / 전체 합성
NG:  “전기 분석을 위해 일단 풀 넷리스트” 전제
```

외부 OSS(Yosys, Netlist Paths, slang-netlist 등)도  
**같은 제약으로만** 붙인다: *subset filelist + top/closure*, wall time budget, 실패 시 approx.  
제약과 충돌하면 도구를 쓰지 않는다.

| 층 | 무엇인가 | 완성 기준 |
|----|----------|-----------|
| **지식 그래프** | 모듈·파일·인스턴스·port·net·assign/FF 줄·의존 엣지 | 질의에 답할 수 있는 **부분 그래프**면 충분 |
| **목적** | 예: 두 hierarchy 그룹 간 structural connectivity | pair + `{file,line,snippet}` evidence |
| **효율** | 연 파일 수, hop, 분기 폭, 벽시계 | cone **밖**을 안 열고, 멀어지는 가지를 늦추거나 cut |

문헌 COI reduction 이 하는 일도 같은 구조다:  
dependency graph 전체 대신 **명세의 cone** 만 남긴다 (Part A).  
pyhirewalk 파이프라인은 그 게임을 **단계적으로** 둔다.

```text
build_db     →  전역이 아닌 “이름→파일” 인덱스 (그래프 노드 후보 지도)
hier_resolve →  path 존재·스코프만 (연결 엣지 아직 최소)
hier_conn    →  S/T 목적 cone 만 온디맨드로 채움 (meet 시 조기 종료)
```

**성공 지표 (이 관점):**

1. **필요 충분**: 목적 질의에 대한 답 + 리뷰 가능한 evidence.
2. **최소 개방**: 같은 답을 위해 연 RTL/엣지 수가 적을수록 좋음.
3. **미결정 명시**: budget cut 은 “모른다”이지, 거짓 단정이 아님.
4. **재사용**: 한 번 연 모듈의 local 의존 인덱스는 캐시 가능 (같은 cone 재질의).

Part A = 문헌이 이 게임을 formal MC 에서 어떻게 정의했는지.  
Part B = 우리 목적(그룹 connectivity)에 대한 **부분 그래프 완성 전략**.

---

# Part A — 전문 문헌에서의 COI

## A.1 용어가 등장하는 맥락

문헌에서 **Cone of Influence (COI) reduction** 은 거의 항상 **모델 체킹의 상태 폭발(state-space explosion) 완화** 기법으로 분류된다.

- CMU 형식검증 강의 자료는 state-space reduction 목록에 *compositional reasoning, abstraction, symbolic (BDD) methods, partial-order reduction, symmetry, **cone of influence reduction*** 을 나란히 둔다 (Sharygina, *Formal Verification by Model Checking*, Lecture on complexity reduction).
- NuSMV 계열 툴 문서·논문은 “프로퍼티에 관여하지 않는 변수를 모델에서 제거”하는 옵션으로 COI를 노출한다 (아래 A.4).
- BMC·IC3 계열 최신 논문도 AIG/넷리스트에서 **transitive fan-in** 으로 COI를 계산해 SAT 변수·결정 영역을 줄인다 (아래 A.5).

즉 전문 자료에서의 COI는 “신호 두 개의 전기 연결 질의 UI”가 아니라,  
**명세(프로퍼티)에 영향을 줄 수 있는 상태 변수/노드 집합만 남기고 나머지를 모델에서 제거**하는 **축소(reduction)** 이다.

---

## A.2 Classical COI reduction — Clarke et al. (정의 원문 요약)

**출처**: Edmund Clarke, Armin Biere, Richard Raimi, Yunshan Zhu,  
*Bounded Model Checking Using Satisfiability Solving*, Formal Methods in System Design, 19(1), 2001  
(§5.1 *Bounded Cone of Influence*; 동일 정의가 저자 튜토리얼/FMSD 판본에 반복).

### A.2.1 목적

> *“The Cone of Influence Reduction is a well known technique that reduces the size of a model if the propositional formulae in the specification do not depend on all state variables in the structure.”*

명세의 명제 식이 구조의 **모든** 상태 변수에 의존하지 않을 때, 모델을 작게 만든다.

### A.2.2 구성 절차 (dependency graph)

문헌이 제시하는 절차는 다음과 같다 (의역 없이 구조만 정리):

1. **명세에 등장하는 상태 변수**에서 시작한다.
2. **dependency graph** 를 만든다.
   - 노드 = 상태 변수
   - 엣지: 변수 \(v\) 에서, \(v\) 가 **combinationally depends** 하는 다른 상태 변수들로 나간다  
     (*“a state variable is represented by a node, and that node has edges emanating out to nodes representing those state variables upon which it combinationally depends.”*)
3. 그 그래프에 속한 상태 변수 집합을 명세 변수의 **COI** 라 부른다.
4. **classical COI 밖**의 변수는 명세의 진위에 영향을 줄 수 없으므로 **모델에서 제거**한다  
   (*“The variables not in the classical COI can not influence the validity of the specification and can therefore be removed from the model.”*)

이 논문은 이를 **“classical” COI** 라 불러, 뒤에 나오는 bounded COI 와 구분한다.

### A.2.3 역사·동치 관계 (문헌 주석)

동일 절 footnote:

> *“The cone of influence reduction seems to have been discovered and utilized by a number of people, independently. We note that it can be seen as a special case of Kurshan’s localization reduction.”*

- 여러 그룹이 독립적으로 사용.
- **Kurshan localization reduction** 의 특수한 경우로 볼 수 있다.

### A.2.4 회로 실험에서의 효과

같은 논문 §5.4: PowerPC 서브블록 실험에서 **classical COI 적용 전/후** latch·PI 개수를 표로 보고한다 (Table 4 *Before and After Classical COI*).  
명세마다 cone 크기가 다르며, 같은 블록의 여러 AG\(p\) 명세가 비슷한 cone을 공유하는 경우가 많다.

---

## A.3 Bounded COI — 같은 문헌의 확장

**출처**: 동일 Clarke/Biere/Raimi/Zhu FMSD 2001 §5.1; 형식 정의는 인용 [4] (BMC 원 논문 계열).

### 직관 (논문 설명)

Bounded time interval 안에서는 classical COI의 **모든** 상태 변수를 **매 시점** 고려할 필요가 없다.

예: \(\mathrm{EF}\,p\) 를 \(k=0\) 에서 검사하면,

- \(p\) 가 **combinationally depends** 하는 상태 변수(초기 support)만 필요.
- 그 초기값이 \(p\) 를 참이 되게 하면, classical COI의 나머지 없이도 \(\mathrm{EF}\,p\) 가 참.

\(k=1\) 로 늘리면:

- 초기 support에 더해, 그 변수들이 **한 스텝 전**에 의존하는 support를 합친다 (회로에 feedback 있으면 겹침 가능).
- 이 합집합은 **항상 classical COI 의 부분집합**.

BMC unrolling 식(논문 formula 1)을 **해당 \(k\) 의 bounded COI 변수만** 펼치면, 전체 classical COI로 펼친 것보다 일반적으로 **더 작은 CNF** 를 얻는다.

### 실험적 관찰 (같은 논문)

- short counterexample 탐색에서 bounded COI가 강점 보강.
- \(k\) 가 커지면(대략 10 근처) bounded COI 이득이 classical 대비 줄어든다 — 시간이 길어지면 classical cone의 대부분 변수를 결국 평가하게 되기 때문.

---

## A.4 툴 관행 — NuSMV / NuSeen

**출처**: Arcaini, Gargantini, Riccobene, *NuSeen: a tool framework for the NuSMV model checker* (ICST 등 계열).

실행 옵션 설명 원문 요지:

> *“enable cone of influence reduction : NuSMV applies the cone of influence reduction technique … (i.e., variables that are not involved in the verified properties are removed from the model) that can speed up the verification process.”*

- 검증 대상 **프로퍼티에 관여하지 않는 변수 제거**.
- Clarke 계열 classical COI 와 동일한 목적 서술.

NuSeen은 변수 간 **dependency graph** 시각화 도구도 제공 (모델 디버깅·모듈 분할용). 이는 COI 계산의 **입력 그래프**와 같은 계열의 자료구조다.

NuSMV 본체 논문·매뉴얼에서도 cone of influence reduction 이 상태 폭발 완화 feature로 반복 언급된다 (Cimatti et al. *NuSMV: a new symbolic model checker* 등).

---

## A.5 하드웨어 AIG / SAT 모델 체킹에서의 COI

### A.5.1 노드 COI = transitive fan-in

**출처**: Su, Yang, Ci, Li, Bu, Huang, *Deeply Optimizing the SAT Solver for the IC3 Algorithm* (GipSAT; arXiv HTML).

AIG 배경 절:

> *“The Cone of Influence (COI) of a node is the set of all nodes that could potentially influence its value, which can be obtained by recursively traversing its fanins.”*

- 방향: **fan-in 재귀 순회** (값이 어디서 오는가).
- IC3 relative induction 질의 \(sat(F_i \land c \land T \land \lnot c')\) 에서,  
  \(c'\) 값에 필요한 영역은 \(\mathrm{COI}(c')\) 로 한정 가능.  
  논문은 domain \(= \mathcal{V}(F_i) \cup \mathcal{V}(c) \cup \mathrm{COI}(c')\) 만 decide/BCP 하면 충분하다고 정리.

### A.5.2 Constraint COI

**출처**: Yu, Che, Zhang, *FRAIG-BMC* (arXiv 2025).

> *“We compute the cone of influence (COI) of the constraints, namely the set of nodes that are in the transitive fan-in of the \(C_k\) nodes.”*

- 제약 노드의 **transitive fan-in** = constraint COI.
- 시뮬레이션·SAT 샘플링을 그 cone 안으로 제한.

### A.5.3 요약 (하드웨어 구조 관점)

| 용어 | 문헌 의미 |
|------|-----------|
| COI of node \(n\) | \(n\) 에 영향을 줄 수 있는 노드 = **fan-in closure** |
| COI of property/spec | 명세 원자·출력의 fan-in closure (상태변수/래치 단위로 집계하면 classical COI) |
| 제거 대상 | closure **밖** — 프로퍼티 진위에 무관 |

---

## A.6 Static COI vs on-the-fly COI (소프트웨어 MC)

**출처**: Telbisz, Bajczi, Szekeres, Vörös, *On-the-fly Cone-of-Influence Reduction for Model Checking Concurrent Software*, SPIN 2025  
(`downloads/spin25_onthefly_coi.pdf`).

### 전통(static) COI (논문이 인용하는 표준)

- 모델에 대한 **정적 data-flow / control-flow 분석**.
- 프로퍼티에 대해 **모든 문맥에서 완전히 불필요한(completely redundant)** 변수·문장을 제거.
- program slicing 과 같은 계열로 분류.

### On-the-fly 확장 (SPIN’25 기여)

- 동시성에서 한 interleaving에선 쓰이고 다른 데선 안 쓰이는 문장이 많음 → static COI가 보수적.
- 상태 탐색 **중** 현재 스레드 국소 상태를 반영한 data-flow graph 로,  
  “지금 문맥에서는 결과가 이후 조건문에 쓰이지 않음” 이면 문장 평가 생략.
- 논문: 전통 COI는 *completely redundant variables (redundant in all thread contexts)* 를 제거;  
  자신들은 *redundant in the current state … with respect to the verified property* 를 제거.

pyhirewalk 1차 범위와 직접 대응은 약하지만, 문헌이 구분하는 축은 명확하다:

| 축 | static / classical | dynamic / on-the-fly / bounded |
|----|--------------------|--------------------------------|
| 분석 시점 | 검증 전 1회 | 탐색·unroll 중 |
| 제거 강도 | 전 문맥 무관 요소만 | 시점·문맥 의존 요소도 |
| 안전성 | 보수적(과소 제거) | 더 fine-grained (증명이 필요) |

---

## A.7 관련·혼동하기 쉬운 개념 (문헌 위치)

| 개념 | 관계 |
|------|------|
| **Localization reduction (Kurshan)** | classical COI 의 일반화/상위 기법으로 문헌이 명시 |
| **Program slicing** | “프로퍼티에 무관한 문장 제거” — SPIN’25가 COI와 나란히 둠 |
| **Observability / controllability cone (ATPG)** | 테스트 이론의 관측·제어 가능 영역; 이름 “cone” 공유, **목적·정의식은 MC COI와 다름** |
| **Transitive fan-in / fan-out (합성·STA)** | 구조 그래프 연산; AIG 논문의 COI 정의와 **계산은 동일 계열**, 용도는 타이밍·논리 등 |
| **Bidirectional BFS / meet-in-the-middle** | **그래프 경로 탐색** 고전 기법. formal COI reduction 논문의 핵심 정의는 아님.  
  “그룹 간 연결”을 찾을 때 **공학적으로** 쓰는 탐색 전략 (Part B) |

---

## A.8 문헌 정의 한 줄 요약 (사실만)

1. **COI(명세)** = 명세 변수에서 시작해 **combinational dependency** 를 따라 닫힌 상태 변수 집합 (dependency graph).  
2. **COI reduction** = 그 집합 **밖**을 모델에서 삭제해 검증 부담을 줄임.  
3. **Bounded COI** = 유한 horizon \(k\) 에서 시점별로 필요한 support 만 취해 classical COI 의 부분집합으로 CNF를 축소.  
4. **노드 COI (AIG)** = 해당 노드의 **transitive fan-in**.  
5. **Static vs on-the-fly** = 전역 불필요 제거 vs 현재 문맥 불필요 제거.  
6. 목적은 항상 **프로퍼티 보존 하의 모델 축소** (connectivity 리포트가 1차 목적 아님).

---

# Part B — pyhirewalk 적용 (프로젝트 계획)

> 이 파트는 문헌 **사실**이 아니라, Part A를 입력으로 한 **설계 선택**이다.  
> 사용자 요구(양 그룹 connectivity, port 경계, evidence=file/line/snippet)를 문헌 용어로 정합시킨다.

## B.1 문제 재정식화 (문헌 용어로)

| 문헌 개념 | pyhirewalk 대응 |
|-----------|-----------------|
| “specification atoms / property variables” | 그룹 **S**, **T** 의 leaf 신호 (hierarchy path) |
| dependency graph 노드 | 모듈 스코프 net / port / (FF 출력) |
| combinational dependency 엣지 | `assign` RHS→LHS, combo always, port actual↔formal |
| sequential 확장 | FF: D 쪽 fan-in 과 Q 쪽 fan-out 을 잇는 엣지 (1차 structural) |
| classical COI(S) | S leaf 의 **backward** dependency closure (fan-in cone) |
| dual: “영향받는 집합” | S leaf 의 **forward** dependency closure (fan-out) — 문헌 COI 정의의 대칭 연산 |
| “변수 제거” | 우리는 전체 칩 모델 제거보다 **탐색 중 cone 밖을 방문하지 않음** (lazy) |
| 검증 결과 true/false | 연결 **존재 여부** + evidence path |

**핵심 질의 (1차):**

> \(\exists\, s\in S_{\mathrm{leaf}},\; t\in T_{\mathrm{leaf}}\)  s.t.  
> structural dependency path \(s \rightsquigarrow t\) (또는 양방향 정의의 meet)  
> 이 성립하는가?  
> 성립하면 path 위 각 의존 단계의 **{file, line, snippet}**.

이것은 문헌의 “COI 밖 삭제 후 모델 체킹”이 아니라,  
**두 seed 집합의 dependency cone 교차(및 경로 재구성)** 에 가깝다.  
그래도 **그래프·dependency·fan-in closure** 는 Part A 와 동일한 수학적 객체다.

---

## B.2 Structural vs formal (우리가 하는 / 안 하는 것)

| | formal COI reduction (문헌) | pyhirewalk 1차 |
|--|----------------------------|----------------|
| 입력 | FSM + temporal property | RTL + hierarchy paths |
| 출력 | 축소 모델 위 MC 결과 | 연결 pair + evidence |
| 의존 | next-state / combo 함수 | 텍스트·스코프 구조 의존 (assign/FF/port map) |
| 완전성 | 프로퍼티 동치 보존이 목표 | structural over-approx 가능 (실제 상수 dead path 남음) |
| 의미론 | BDD/SAT/IC3 | 없음 (추후 opt-in pyslang) |

1차는 **structural dependency COI** (A.5 fan-in/out 계열).  
semantic constant-prop·formal 동치 축소는 비목표.

---

## B.3 Combinational / sequential

문헌 classical COI 엣지는 *combinationally depends* 를 강조한다.  
순차 회로에서는 next-state 함수를 통해 래치가 cone에 들어가고, BMC의 **bounded COI** 가 시점을 자른다.

pyhirewalk:

- **Combinational step**: assign / combo → 같은 “시간 단계” 의존.
- **Sequential step**: FF 줄에서 Q←D 를 증거로 기록하고 cone을 다음 단으로 확장 (multi-cycle structural).
- **Budget hops**: bounded COI의 “\(k\) 로 support 제한” 과 유사한 **실용 bound** (max_hops / max_files). 문헌 formal bound와 수학적 동치는 아님.

---

## B.4 왜 단방향 classical cone 전개만으로는 부족한가 (공학)

문헌 COI reduction 은 보통 **한 프로퍼티 집합**에서 backward closure 한 번이다.  
우리는 **두 그룹** 사이의 **경로 존재**를 원한다.

- S 만 fan-out: T 와 무관한 IP로 cone 폭발.
- T 만 fan-in: 대칭 문제.

따라서 탐색 전략으로 **양측 동시 확장 + meet** 을 쓴다  
(그래프 이론 bidirectional search; formal COI 논문의 정의 항목은 아님 — A.7 참고).

문헌과의 정합:

- S 쪽 forward closure ≈ “S 가 영향 주는 집합”
- T 쪽 backward closure ≈ classical COI(T) (T 값에 영향 주는 집합)
- **교차 비공허** ⇒ structural 연결 후보; path reconstruct ⇒ evidence

---

## B.5 그래프 모델 (구현 추상)

### B.5.1 노드

```text
NetKey = (module_type | file_id, local_name)
```

Hierarchy path 는 엔드포인트 라벨. 내부 키는 모듈 스코프 신호.

### B.5.2 엣지 (dependency, 문헌의 “combinationally depends” 근사)

| 종류 | 의미 | 경계 |
|------|------|------|
| `assign` / combo | LHS depends on RHS idents | 모듈 내부 |
| `ff` | Q depends on D (enable 러프) | 내부; 증거 줄 |
| `port_map` | parent actual ↔ child formal | **모듈 경계** |

방향 규약 (구현 일관):

- 엣지 \(u \to v\): “\(v\) 가 \(u\) 에 **의존**” 또는 “\(u\) 가 \(v\) 에 **영향**” 중 하나를 문서·코드에서 고정.  
  권장: **영향 방향** \(driver \to load\) (fan-out 순방향 = 엣지 방향).  
  Backward COI = 엣지 역방향 도달.

### B.5.3 모듈 내 종료 (사용자 규칙)

```text
seed → … (assign/FF, 각 줄 evidence) → port
port에 닿기 전 다른 모듈로 점프 금지
port 이후 instance port_map 으로만 인접 모듈 진입
```

문헌의 “래치/상태변수 단위 집계”와 달리, 우리는 **포트 경계를 모듈 간 cut** 으로 둔다 (계층 RTL 탐색 비용 제어).

---

## B.6 양측 COI 탐색 알고리즘 (계획)

### B.6.0 방향 고정: S = fan-out, T = fan-in (효율의 핵심 가정)

실무 질의 형태:

> 그룹 **S (sources)** 가 영향 주는 쪽 ↔ 그룹 **T (sinks)** 가 영향 받는 쪽  
> 연결 = \(\exists s\in S, t\in T\)  s.t. structural path \(s \xrightarrow{\ *\ } t\)

| 그룹 | 역할 | 탐색 방향 | 쓰는 엣지 |
|------|------|-----------|-----------|
| **S** | source | **forward only** (fan-out) | driver→load, out port → child/parent actual |
| **T** | sink | **backward only** (fan-in) | load←driver, in port ← actual |

**하지 않는 것 (비용 낭비):**

- S 에서 fan-in, T 에서 fan-out (질의 방향과 반대)
- undirected “양쪽으로 다 펼치기”
- 네 방향 (S±, T±) 동시

이미 meet-in-the-middle 이지만, **방향이 고정된 bi-search** 라서 분기 폭이 대칭 bi-search보다 작다.

```text
        S leaves                    T leaves
           │                           │
           ▼ fan-out                   ▲ fan-in
        ... mid nets ...  ←—— meet ——→  ...
```

### B.6.0.1 효율 힌트 (이 가정 전용)

| # | 힌트 | 이유 |
|---|------|------|
| **D1** | 방향 락 | 코드·API에 `--src` / `--dst` (또는 source_group / sink_group). 양방향 혼용 플래그 없음(P1). |
| **D2** | **smaller-frontier-first** | \(|F_s|\) vs \(|F_t|\) 중 작은 쪽만 expand. fan-out이 버스에서 폭발하면 T fan-in 쪽이 더 싸게 깊어짐. |
| **D3** | **포트 방향 필터** | S forward: `output`/`inout` 로만 모듈 탈출, child `input` 으로만 진입. T backward: `input`/`inout` 으로만 탈출(역행), driver 쪽 `output` 으로 진입. **잘못된 dir 포트맵 엣지 스킵** → 경계 분기 대폭 감소. |
| **D4** | **RHS vs LHS 스캔 분리** | S 쪽 확장 시 “RHS에 현재 net” 문장만 (load 찾기). T 쪽 확장 시 “LHS = 현재 net” 문장만 (driver 찾기). 한 파일을 두 인덱스로 캐시: `uses[net]`, `driven_by[net]`. |
| **D5** | 다중 seed + **공통 경로 캐시 (OR merge)** | §B.6.0.5. 여러 a 가 한 net 으로 OR/합류해도 그 아래 cone 은 **한 번만** expand. |
| **D6** | **모듈 집합 사전 필터 (cheap)** | resolve 결과로 `mods(S)`, `mods(T)`, 경로 상 LCA·중간 모듈 집합 `M_path` 근사. S forward가 `M_path ∪ mods(T)` 와 무관한 IP로  ent 깊어지면 beam 최하위. (완전 차단은 위험 → 우선순위만) |
| **D7** | **한쪽에 붙이기 옵션** | \|S\|≪\|T\| 이거나 S가 이미 좁은 포트면: S만 먼저 k hop fan-out 후, 그 frontier를 T backward의 “목표 visited”로 쓸 수도 있음. 기본은 여전히 bi; 치우친 크기일 때만. |
| **D8** | meet 즉시 path 확정 | S→meet 는 out-경로, meet→T 는 in-경로 역순. evidence 체인 방향이 질의 방향과 일치해야 함 (T 쪽 edge를 뒤집어 기록). |
| **D9** | budget도 방향별 | `max_hops_src`, `max_hops_dst` 분리 가능. 버스 source는 hops_src 작게, sink 쪽은 깊게 등. |
| **D10** | const 말단은 T 쪽에 유리 | T fan-in이 `x<=0` 만나면 그 가지 **종료** (S와 만날 수 없음). S fan-out이 const drive만 하는 net은 load 없으면 종료. |

### B.6.0.2 포트 방향 필터 (스케치)

```text
# S forward, at port boundary:
if net is formal of instance port:
  only traverse if port_dir in {output, inout}  # driving out of this module
  or port_dir in {input, inout} when descending into child via actual→formal

# T backward, at port boundary:
  only traverse opposite: sink side enters via inputs; climb to parent actual, etc.
```

방향 정보가 파싱 실패면: **스킵 + parse_skip** (틀린 연결 evidence 금지; D3를 느슨히 열어 오염하지 않음).  
`inout` 은 양방향 허용하되 beam 순위는 낮춤.

### B.6.0.3 smaller-frontier vs zigzag

| 전략 | 언제 |
|------|------|
| **smaller-frontier-first** (권장 기본) | 한쪽이 버스/wide fan-out 일 때 자동으로 반대쪽(대개 T fan-in) 위주 |
| zigzag 교대 | 디버그·균등 진행 로그용 |
| expand-only-S / only-T | 프로파일용; 제품 기본 아님 |

복잡도 직관: 경로 길이 \(L\) 일 때 단방향 \(\sim b^{L}\), 양방향 고정 방향 \(\sim b^{L/2}+b^{L/2}\).  
S/T 역할 고정은 상수 인자뿐 아니라 **잘못된 반쪽 cone 전체**를 제거한다.

### B.6.0.4 의존 방향 ≠ hierarchy 깊이 (지그재그)

**잠그는 것**과 **자유인 것**을 섞지 않는다.

| 축 | 고정? | 의미 |
|----|-------|------|
| **의존(data) 방향** | 고정 | fanout 쪽 = driver→load 만, fanin 쪽 = load←driver 만 |
| **hierarchy 이동** | **자유 (지그재그)** | child로 내려감 / parent로 올라감 / 형제로 횡단 — 깊이가 늘었다 줄었다 함 |

RTL 연결 경로는 전형적으로:

```text
deep leaf  --up-->  parent port  --side-->  sibling inst  --down-->  other deep leaf
   depth↑              depth↓                  depth~                   depth↑
```

- “정방향 탐색” = **의존 그래프에서 forward** 이지, “hierarchy depth 단조 증가” 가 **아님**.
- “역방향 탐색” = **의존 그래프에서 backward** 이지, “depth 단조 감소” 가 **아님**.
- 상대 그룹을 향해 가다 보면 depth 가 깊어졌다 얕아졌다 할 수 있고,  
  어느 순간 depth 상으로는 멀어 보여도 **모듈/LCA 상으로는 상대 쪽에 더 가까울** 수 있다.

**휴리스틱 금지:**

- `depth` 만으로 “상대에게 접근 중” 판정 (단조 가정 깨짐)
- “한 번 parent로 올라갔으면 다시 child로 안 감” 같은 잘못된 prune
- fan-out/fan-in 을 hierarchy up/down 과 동일시

**휴리스틱 권장 (비단조 nearness):**

| 신호 | 용도 |
|------|------|
| resolve path **공통 prefix 길이** / LCA 깊이 | 상대 그룹 path 와의 계층 근접 |
| `module(v) ∈ mods(other) ∪ ancestors(other) ∪ M_mid` | 모듈 집합 겹침 |
| port 경계 통과 후 **other 쪽 인스턴스 서브트리 진입** | 횡단 성공 감지 |
| meet-adjacent | 최우선 (depth 무관) |

```text
near_other(v) =
  high  if v ∈ visited_other
  high  if hier_path(v) shares long prefix with some other-group path
  mid   if module(v) in module_closure(other)
  low   else
# depth(v) 단독 항 없음
```

로그에도 `depth=` 만 쓰지 말고 `hier=... lca_score=... side=fanin|fanout` 을 남긴다.

### B.6.0.5 공통 경로·OR 합류와 탐색 캐시 (**필수**)

그룹 a 의 **여러 hierarchy** 가 중간에서 합류(OR / wired join / 공통 net·포트)한 뒤 b 에 닿는 경우가 흔하다.

```text
a1 ──┐
     ├──► mid ──► … ──► b
a2 ──┘
```

**이미 있던 것 (부분):**

- multi-seed 를 한 `visited_s` / `visited_t` 로 합친다 (D5 요지) → mid 재방문 시 **재expand 안 함**.
- 모듈 파일당 `LocalDepGraph` 1회 파싱 캐시.

**명시적으로 필요한 캐시 (이번에 고정):**

| 캐시 | 키 | 값 | 효과 |
|------|-----|-----|------|
| **C1 side-visited** | `(side, NetKey)` | first-touch meta | 같은 side 에서 mid 재도달 시 **expand 스킵** (공통 하류 cone 1회) |
| **C2 reach labels** | `(side, NetKey)` | 도달 가능 seed id 집합 (또는 bitset) | a1·a2 가 둘 다 mid 에 오면 label **합집합**만 갱신; expand는 C1 때문에 1회 |
| **C3 prev / join** | `(side, NetKey)` | 대표 prev 1개 + (옵션) join 엣지 리스트 | path reconstruct; multi-pair 는 label×로 |
| **C4 local graph** | `file_id` / module | `uses[]`, `driven_by[]`, port_map | 파싱 반복 방지 |
| **C5 meet memo** | `(net)` or `(s_lab,t_lab)` | 이미 낸 pair | 동일 meet 중복 리포트 방지 |
| **C6 edge evidence** | `(file,line)` or edge id | `{file,line,snippet}` | 같은 할당 줄 재스니펫 안 함 |

#### OR 합류 시 알고리즘

```text
on reach(side, v, from_u, seed_labels_u, edge):
  if v not in visited[side]:
    visited[side].add(v)
    labels[side][v] = seed_labels_u
    prev[side][v] = from_u          # 대표 경로 1개
    enqueue expand v
  else:
    # 공통 경로 hit — expand 하지 않음
    labels[side][v] |= seed_labels_u   # OR: 새 source/sink 만 기록
    # optional: joins[side][v].append(from_u)  for multi-path evidence
    # 이미 expand 끝난 노드: 하류는 기존 cone 재사용
    if v in visited[other]:
      emit pairs for (new labels × other labels) not yet in C5
```

- **탐색 비용**: 합류점 아래는 **O(cone)** 1회. a 가 N 개여도 공통 구간 N 배 안 팜.
- **연결 완전성 (structural)**: label 합집합으로 “어느 a 가 이 mid 를 통해 b 에 닿는가” 복원.
- **evidence**: 기본은 대표 path 1개 (C3 prev). 사용자가 multi-path 원하면 joins 로 추가 path (P2+).

#### 하지 말 것

- seed 마다 **독립 BFS** (공통 mid 를 N 번 expand) — 캐시 없음과 동일, 비목표.
- visited 만 있고 label 없음 — a2→mid→b 연결을 a1 대표 path 만으로 오인·누락할 수 있음.
- “경로 문자열 전체” 를 키로 한 캐시 (폭발). 키는 **NetKey + side**.

#### Phase1/2 와의 관계

- Phase1 meet: `labels_s[v] × labels_t[v]` (또는 path ends) 로 pair.
- Phase2 orphan: T 쪽 visited/label 재사용, 이미 닫힌 하류 재탐색 없음.

**결론:** “고려는 multi-seed visited 수준으로 들어가 있었으나, **OR 합류 + label 합집합 + meet memo** 를 필수 캐시로 문서에 명시한다.” 구현 P1 에서 C1·C2·C4·C5 는 반드시 포함.

### B.6.0.6 배열·다차원·bit slice 매핑 (**필수 고려, 이전엔 약했음**)

#### 정직한 상태

| 기존 | 내용 |
|------|------|
| `hier_resolve` | path segment 의 `[…]` 를 strip 해 **base name** 으로 존재 검사, `select` / `needs_detail` 플래그만 남김 |
| 초기 conn 계획 | P1 “base name only”, bit-select 는 P3 로 **미룸** |
| **부족** | a/b 가 **2–3차원 배열**, **한 신호의 여러 slice를 따로 기입**, 경로 중 **뭉침/분기**, **slice마다 다른 매핑** 을 그래프 키·캐시·pair 단위로 다루지 않음 |

실무 질의에는 아래가 나오므로, **설계 필수**로 격상한다. (구현 난이도는 단계적.)

#### 등장 패턴

```text
# 그룹 a: 한 논리 버스의 slice 를 여러 줄로
a:  top.u.d[3:0]
    top.u.d[7:4]
    top.u.mem[1][3:0]          # 2D
    top.u.buf[i][j][k]         # 3D + 상수/파라미터 인덱스

# 그룹 b: 배열/슬라이스 sink
b:  top.v.q[7:0]
    top.v.q[15:8]

# RTL 중간
assign y[7:0]  = x[7:0];           # 뭉쳐서 이동 (bundle)
assign y[3]    = x[0];             # bit 분기·재매핑
assign y[7:4]  = x[3:0] ^ z[3:0];  # slice 단위 연산
always_ff q[i] <= d[i];            # 생성/루프 인덱스 (needs_detail)
```

| 현상 | 의미 |
|------|------|
| **Bundle** | 벡터/슬라이스 통째로 같은 driver→load (bit 정렬 유지 가정 또는 명시 슬라이스 동형) |
| **Split** | 한 net 의 서로 다른 bit 가 다른 문장·포트로 갈라짐 |
| **Permute / partial map** | `y[3] = x[0]` 처럼 인덱스 재매핑 |
| **Group 기입** | 사용자가 이미 split 단위로 a/b 리스트에 넣음 |
| **OR across slices** | 여러 slice seed 가 중간 벡터로 합류 후 다시 split (C1/C2 와 결합) |

#### 신호 좌표 모델

```text
SignalRef:
  base: str                   # d, mem, buf
  select: SelectExpr | None   # 정규화된 선택자
  # SelectExpr 예:
  #   Full            — 전체 (폭 미지/미기입)
  #   Bit(i)          — [i]
  #   Range(hi,lo)    — [hi:lo]  (방향 정규화 hi>=lo 또는 SV 선언 순 보존 정책 택1)
  #   Multi([dim…])   — [a][b][c] 또는 [a][hi:lo] 혼합
  #   Unknown(raw)    — 파싱 실패·파라미터 의존 → needs_detail

NetKey (conn 그래프):
  (module|file, base, select_key)
  # select_key = 정규화 문자열 또는 canonical interval set
```

- **같은 base 라도 select 가 다르면 다른 노드** (기본).  
  `d[3:0]` 과 `d[7:4]` 는 별 seed·별 visited 키.
- **Full** 과 **Range** 의 관계: 겹치면 **alias / cover** 규칙 (아래).

#### 커버·겹침 규칙 (탐색·meet)

```text
covers(A, B)  : A 의 bit 집합 ⊇ B
overlaps(A, B): 교집합 비공허
```

| 상황 | 동작 |
|------|------|
| edge `x[7:0] → y[7:0]` | bundle: 양측 Full/동형 range 로 전파 |
| edge `x[3] → y[0]` | bit map: 키 `(x,Bit3) → (y,Bit0)`; 다른 bit 비전파 |
| seed `x[3:0]`, visited 에 `x[7:0]` 이미 있음 | **overlap**: label 합치고, 미커버 bit 만 추가 expand 하거나 (정밀 모드) bitset diff |
| seed `x[3:0]`, 문장은 `x <= …` (LHS full) | 문장이 full drive 면 seed slice 는 그 driver 의 **부분 관측**; fan-in 은 full 문장 evidence + slice 주석 |
| meet `v` 에서 S select 과 T select | **overlap 있을 때만** pair. pair 에 `src_select` / `dst_select` / `overlap` 기록 |
| 폭·인덱스 모름 (`needs_detail`) | base 단위 **과근사** + 리포트 `slice_approx: true` (precision 원칙: 단정 연결 금지 옵션 가능) |

#### 캐시 키 확장 (B.6.0.5 수정)

| 캐시 | 키 (개정) |
|------|-----------|
| C1 visited | `(side, module, base, select_key)` |
| C2 labels | 동일 + seed id (seed 자체가 slice-specific) |
| C4 local | 문장 파싱 시 LHS/RHS 에 **select 추출** (`x[3:0]`, `x[i]` raw) |
| C5 meet | `(src_seed, dst_seed)` 또는 `(net, sel_s, sel_t)` — **slice pair 단위** |

같은 base 로 키를 뭉개면 `d[3:0]→b0` 과 `d[7:4]→b1` 이 잘못 합쳐짐 → **OR 캐시와 충돌**.  
slice-aware 키가 기본; “bundle 모드”는 명시 옵션.

#### Evidence

```json
{
  "file": "...", "line": 10,
  "snippet": "assign y[3:0] = x[7:4];",
  "src_select": "[7:4]",
  "dst_select": "[3:0]"
}
```

- 사용자 대면 최소 필드 정책과 충돌 시: snippet 에 슬라이스가 보이면 필수 메타는 optional,  
  **JSON pair 레벨** 에는 `src`/`dst` path 에 사용자가 준 select 문자열 유지.

#### 구현 단계 (난이도 정직)

| Phase | slice 지원 |
|-------|------------|
| **P1** | path/seed 의 select **문자열 보존**; 그래프 키는 `(module,base,select_raw)`; 문장에서 `[…]` 동반 ident 추출; **동형 전체/`assign a=b` only** 정확, 그 외 base 폴백 시 `slice_approx` |
| **P2** | Range/Bit 정규화, `overlaps` meet, bitset 또는 interval set 으로 cover diff, 부분 매핑 문장 |
| **P3** | 다차원·파라미터 인덱스·generate `i`, packed/unpacked, pyslang 폭 조회 |

**P1 최소 보장:**

1. 사용자가 나눈 slice seed 를 **합치지 않음** (리스트 엔트리 = 별 노드).  
2. snippet/path 에 적힌 select 가 pair 결과에 **그대로** 남음.  
3. 파서가 select 를 못 읽으면 과근사하지 않고 `parse_skip` / `needs_detail` (틀린 bit 연결 단정 금지).

#### RHS / 대입 형태 분류 — **bit 위치에 영향 주는 것 주의**

신호 전달 문장의 RHS(또는 nonblocking LHS 대입원)는 형태가 다양하다.  
**의존 존재(structural edge)** 와 **bit 위치 보존(position-preserving)** 을 分け서 다룬다.

| 형태 | 예 | bit 위치 | 탐색/매핑 주의 |
|------|-----|----------|----------------|
| **단순 대입** | `y = x;` / `y <= x;` | 동폭이면 **1:1 위치 보존** (선언 방향 동일 가정) | 기본 bundle. 폭 불명이면 `slice_approx` |
| **배열·벡터 통째 대입** | `y = x;` (`logic [N:0]`) | 동형이면 보존 | 단순 대입과 동일 취급 |
| **배열 bit slice 대입** | `y[3:0] = x[7:4];` | **구간 이동** — src bit ↔ dst bit **재매핑 필수** | LHS/RHS select 둘 다 파싱. meet·label 은 **매핑된 bit** 만. `y[3]←x[7]`, `y[0]←x[4]` 식 |
| **부분 bit 대입** | `y[3] = x[0];` | 단일 bit 재매핑 | 다른 bit 에 엣지 만들지 말 것 |
| **concatenation 묶음** | `y = {a, b, c};` / `{y1,y2} = x;` | **위치 재배치·폭 분할** — 가장 위험 | 아래 concat 규칙. 순서를 무시하고 base only 연결하면 **잘못된 bit path** |
| **복제/리터럴 섞인 concat** | `y = {n{a}}`, `{16'h0, x}` | const 구간 + 신호 구간 | const 구간은 net 엣지 없음; 신호 구간만 map |
| **연산 대입** | `y = x & z;`, `y = x + 1;`, `y = x ? a : b` | 비트별/워드 연산 — 위치는 대체로 동형이나 **의미 혼합** | structural: RHS 각 ident → LHS (over-approx). **bit-accurate pair 단정 금지** unless op 가 bit-parallel 로 증명 가능 (`&`,`|` 등). `+`,`*` 는 slice pair 에 `op_mix` 태그 |
| **스트림/캐스트 등** | `{>>{x}}`, `type'(x)` | 위치·패킹 변경 가능 | P1 `parse_skip` 또는 `slice_approx`; 틀린 매핑 내리지 않음 |

##### 특히 조심: bit slice **위치**에 영향 주는 대입

다음이 나오면 “이름만 연결” 금지. **position map** 을 만들거나, 못 만들면 근사 단정 금지.

1. **이종 slice 대입** — `y[lo_y:…] = x[lo_x:…]` (LHS·RHS range 시작이 다름)  
2. **concatenation** — `{a,b,c}` 는 MSB←a … LSB←c (SV 규칙) 로 **구간을 이어 붙임**  
3. **양쪽 concat** — `{y1,y2} = {a,b}` 등 분할·재조립  
4. **part select + concat 혼합** — `y[7:0] = {x[3:0], z[3:0]}`  
5. **endian/선언 방향 불일치** — `[0:7]` vs `[7:0]` (정규화 정책 필요)

```text
# concat 위치 map 스케치 (SV: {a,b} = a 가 상위)
# y[W-1:0] = { a[Wa-1:0], b[Wb-1:0] }  단 W=Wa+Wb
#   y[W-1 : W-Wa]  ↔  a[Wa-1:0]
#   y[Wb-1 : 0]    ↔  b[Wb-1:0]

# slice 이동
# y[3:0] = x[7:4]
#   y[3]↔x[7], y[2]↔x[6], y[1]↔x[5], y[0]↔x[4]
```

##### 구현 정책

| 등급 | 동작 |
|------|------|
| **P1 정확** | 단순/동형 벡터 대입; 동형 slice (`y[3:0]=x[3:0]`); 파싱 성공한 **이종 slice** 에 한해 interval map; 단순 1단 concat `{a,b}` → y full (폭 알 때) |
| **P1 보수** | 복잡한 concat·중첩·연산+slice·폭 미지 → edge 는 **base structural** 만 넣거나 **스킵**; pair 에 `bit_map: unknown` / evidence 는 줄 snippet 만 (리뷰어가 눈으로 확인). **임의 bit 정렬 추측 금지** |
| **P2** | 중첩 concat, 다중 part-select, bitset map, pair 별 overlap |
| **P3** | 연산 bit-parallel 판별, cast/stream, generate 인덱스 |

##### Evidence 와의 관계

- 경로 순 evidence 는 문장 순서 그대로 (B.7.1).  
- slice/concat 문장은 snippet 에 **LHS·RHS select 가 보이게** 줄 전체를 담는 것이 중요 (리뷰가 map 을 검증).  
- 내부 Edge 에 optional `bit_map: [(src_bit, dst_bit), …]` 또는 `src_range→dst_range` — JSON 기본 출력은 짧게, 디버그/정밀 모드에서 노출.

##### OR 캐시와의 관계

- concat 으로 `a`,`b` 가 `y` 의 **서로 다른 bit 구간** 에 들어가면, visited 키는 `(y, select_segment)` 로 쪼개야 함.  
- `y` full 한 키만 쓰면 a 의 bit 와 b 의 bit 가 한 cone 으로 섞여 **잘못된 meet** 가능 → B.6.0.5·B.6.0.6 과 동일 이유로 **select_key 필수**.

##### 실무 전제: generate + parameter 슬라이스 폭발

타깃 RTL 은 다음이 **흔하고 양이 많다**:

```text
parameter int W = 32;
parameter int N = 8;
generate
  for (genvar i = 0; i < N; i++) begin : g
    assign y[i*W +: W] = x[i*W +: W];   // 또는 [i][W-1:0]
    always_ff @(posedge clk) q[i] <= d[i];
  end
endgenerate
```

| 사실 | 함의 |
|------|------|
| slice 경계가 **상수 리터럴이 아님** (`i*W`, `W-1`, `+:`) | 텍스트 정규식만으로 position map **불완전** |
| generate 로 **동형 패턴이 N배** | 인스턴스마다 문장 복제 vs 템플릿 1회 — 둘 다 비용·키 설계 이슈 |
| 사용자가 a/b 에 `…[3][7:0]` 등 **구체 좌표**를 줄 수 있음 | seed 쪽 select 는 신뢰; RTL 중간은 param 미전개 |
| full elab (pyslang/generator unrolling) 없으면 폭·i 범위 미지 | **전수 bit-accurate conn 은 1차 비목표** |

**이 전제에서의 전략 (중요도 순):**

1. **Seed 좌표는 보존**  
   그룹 a/b 에 적힌 hierarchy+select 는 그대로 pair 끝점. generate index 가 path 에 있으면 resolve `needs_detail` 과 동일 계열.

2. **패턴 인식 > 전수 전개 (P1–P2)**  
   - 동형: `y[i*W +: W] = x[i*W +: W]` → “같은 gen 인덱스 채널” structural 링크 (i 를 **심볼**로 유지).  
   - 사용자가 `i=3` 고정 seed 면, 그 채널만 concretize 시도; 실패 시 `slice_approx` / 채널 단위 meet.  
   - **N개 문장으로 펼쳐 저장하지 않음** — `(template_id, gen_index_symbol|const)` 키.

3. **parameter 값**  
   - run config / `+define+` / 모듈 헤더 `parameter` **상수 리터럴**만 1차 해석.  
   - 다른 param 의존·함수 호출 → 미해석, map 단정 금지.

4. **탐색 그래프 해상도 (권장 기본)**  
   | 모드 | 노드 단위 | 언제 |
   |------|-----------|------|
   | **channel** (기본) | base + gen 축/버스 채널 (param 식은 심볼) | generate 다량 |
   | **concrete slice** | 숫자 `[hi:lo]` / `[i]` 고정 | seed·문장 모두 리터럴 |
   | **base-only fallback** | base 만 | 파싱 실패; pair 에 `slice_approx` |

5. **budget**  
   generate 복제 cone 을 bit 단위로 풀면 frontier 폭발 → channel 모드 + OR 캐시(C1) + beam 필수.  
   “param slice 가 많다” = **해상도를 의도적으로 낮추는 이유**이지, bit 루프를 더 도는 이유가 아님.

6. **Phase / 도구**  
   - P1: 리터럴 slice + seed 보존 + 동형 gen 패턴 러프 링크 + 모르면 skip.  
   - P2: `+:`/`-:` , 단순 `i*W` 선형식, channel meet.  
   - P3: scoped elab/pyslang 으로 param·generate 수치화 opt-in (전체 칩 아님, cone 파일만).

7. **리포트 정직성**  
   ```text
   pair: src=…g[3].…  dst=…
   slice_resolve: concrete | channel | approx | unknown
   ```
   generate/param 미전개로 bit map 을 못 만들면 **연결 후보 + evidence snippet** 까지,  
   “bit 3 이 bit 7 로 간다” 류 단정은 `concrete` 일 때만.

**한 줄:** generate·parameter slice 폭발 환경에서는 **전수 bit 탐색이 아니라  
seed 정밀 + 중간 channel/패턴 + 캐시·budget** 이 맞는 난이도 대응이다.

##### 난이도·구문 판정 범위 = **신호가 속한 RTL 블록 전체**

한 줄·한 할당만 보고 “단순 대입이다 / bit map 가능하다” 를 정하면 틀린다.

| 보면 안 되는 단위 | 이유 |
|------------------|------|
| 단일 줄 snippet | 옆에 `generate`·`parameter`·다른 slice 대입·concat 이 있음 |
| seed path 문자열만 | 중간 모듈 본문 구문 난이도 미반영 |
| 파일 일부 grep hit 만 | 선언 폭·`localparam`·동일 base 의 다른 드라이버 놓침 |

**봐야 하는 단위 (권장):**

```text
소유 스코프 = resolve 가 준 (module_type, file) 의
  1) 모듈 본문 전체 (endmodule 까지)     ← 1차 기본 “RTL 블록”
  2) 신호가 generate 안이면 그 generate
     프레임(+ 동일 genvar 축) 포함
  3) (옵션) 패키지/헤더 parameter 는
     모듈이 import·파라미터 오버라이드하는 범위
```

즉 **해당 신호가 사는 모듈 RTL 전체를 한 번 열어** 다음을 판정한 뒤, 그 모듈 안 탐색 모드를 고른다.

| 블록 스캔으로 얻는 것 | 용도 |
|----------------------|------|
| 이 base 의 **모든** driver/load 문장 | 다중 드라이버·OR·slice 분기 |
| `parameter`/`localparam`/`genvar` | 폭·인덱스 식 해석 가능 여부 |
| `generate for/if` 존재·패턴 | channel 모드 필요 여부 |
| concat / `+:` / 연산 대입 혼재 | `concrete` vs `approx` vs `parse_hard` |
| port 목록·방향 | 경계 필터 (B.6.0) |

```text
on first touch of module M (for signal s):
  if M not in module_profile_cache:
    parse entire module body once   # C4 확장: LocalDepGraph + difficulty profile
    profile[M] = {
      hard_features: [generate, param_slice, concat, ...],
      slice_mode: concrete | channel | approx,
      nets: uses/driven_by with selects,
      ports: ...
    }
  use profile[M].slice_mode for all expands inside M
```

- **비용:** 모듈 단위 1회 파싱은 “줄만 보는 것보다 비싸 보이지만”, 같은 모듈을 hop 마다 재스캔하는 것보다 싸고, C4 캐시와 맞음.  
- **전 칩 오픈 아님:** cone 이 닿는 모듈만. 다만 **그 모듈은 토막이 아니라 전체**.  
- **어려운 구문 혼재 여부** 는 이 프로필로 결정 → 리포트 `module_profile: hard_features=[…]`.  
- 블록 전체를 아직 안 읽었는데 bit map 을 단정하지 않음.

**한 줄:** 해석 난이도는 줄 단위가 아니라 **신호 소유 모듈(RTL 블록) 전체 스캔 결과** 로 정한다.

##### 단순 assign 체인 vs 상위 컨텍스트가 필요한 경우

| 연결 양상 | 보면 되는 범위 | 난이도 |
|-----------|----------------|--------|
| `assign` / 단순 `always` 로만 net 이어짐 | **해당 모듈 본문** (+ port_map 한 단) | 낮음 |
| **`ifdef` / `ifndef` / `elsif`** | 모듈 + **compile defines** (`+define+`, run JSON env) — 파일리스트·빌드 컨텍스트 | 중 |
| **`generate`** (`for`/`if`/`case`) | 모듈 + **유효 parameter 값** (아래) | 중~고 |
| **`parameter` / `localparam`** | 모듈 기본값 + **인스턴스 오버라이드** + (가능하면) 패키지 | 중~고 |
| parameter **상속·전달** | `Parent #(.W(W)) u_child(...)` 처럼 상위 param 이 하위로 흐름 → **hierarchy path 상의 조상 모듈** 까지 | 고 |

즉 “어려운 구문이 섞였는가 / slice 폭이 얼마인가” 는:

```text
local module body
  + CompileContext.defines          # ifdef
  + instance parameter bindings     # along hier path from resolve
  + (optional) package / `include` 상수
```

**Hierarchical context (path-scoped):**

```text
hier: top.u_a.u_b.sig
resolve 가 준 각 단:
  top        → file_T, module Top,    params_bound_0
  u_a        → file_A, module A,      params_bound_from Top's inst
  u_b        → file_B, module B,      params_bound_from A's inst
  sig        → local in B

해석 시:
  1) leaf 모듈 B 본문 전체
  2) B 인스턴스 시점의 #(.P(v)) · 위치 파라미터
  3) v 가 식별자면 A(및 필요 시 Top) 의 parameter 정의·재전달 추적
  4) ifdef 는 전역/컨텍스트 defines (상위 “파일”이 아니라 컴파일 단위)
```

| 소스 | 누가 주나 |
|------|-----------|
| defines | 이미 `build_db` / run JSON / filelist `+define+` → **CompileContext** 재사용 |
| 인스턴스 param | 상위 모듈 본문의 `Mod #(.W(32)) u_b` 또는 `.W(W)` — **parent RTL 전체** 스캔 (C4를 path 단마다) |
| 기본 param | child 모듈 헤더 `parameter int W = 8` |
| 미해석 | 표현식·함수·다른 generate 결과 → `param_unknown`, slice_mode 강등 |

**탐색 정책에 미치는 영향:**

1. **쉬운 모듈** (profile: plain assigns only, no gen/ifdef/param-slice)  
   → local only, concrete/base 가능.  
2. **ifdef 만**  
   → defines 적용 후 전처리 관점 스캔 (또는 양 가지 보수적 union + 태그).  
3. **generate/param**  
   → leaf + **path 조상 쪽으로 param binding 해석** 시도; 실패 시 channel/approx.  
4. **전 칩 상위 무차별 open 금지**  
   → open 범위 = `resolve path` 상의 모듈 체인 + cone 이 port_map 으로 새로 밟는 모듈.  
   “상위까지” = **그 신호 hierarchy 의 ancestor**, 임의 top 전부가 아님.

```text
context_key = (module, file, frozen_param_bindings, defines_id)
# 같은 모듈 타입이라도 #(.W(8)) vs #(.W(32)) 인스턴스는 profile/캐시 분리
```

**1차 구현 현실 (정직):**

| Phase | 상위 컨텍스트 |
|-------|----------------|
| P1 | defines(CompileContext); 인스턴스 `#(.P(리터럴))` 만; `.P(식별자)` 는 parent 헤더 리터럴 기본값 1단 lookup 또는 unknown |
| P2 | path 따라 식별자 param 전달 체인; generate if 조건 단순 비교 |
| P3 | 패키지·표현식·scoped elab |

**한 줄:** 단순 assign 체인은 로컬로 끝난다.  
**ifdef / generate / parameter(상속 포함)** 가 섞이면 **compile defines + hierarchy 조상 모듈의 인스턴스 바인딩** 까지 봐야 하며, 그건 “전 칩”이 아니라 **resolve path 컨텍스트** 로 한정한다.

### B.6.1 입력 형태: 그룹 a / b + resolve 방향 메타

사용자 입력:

```text
group_a: [ hier_path, ... ]     # 묶음 (leaf = 신호)
group_b: [ hier_path, ... ]
# 역할: 명시 권장
#   --fanout a --fanin b   또는 JSON role 필드
# 미지정 시: resolve 의 port_dir 로 다수결 추정 (애매하면 에러/경고 후 중단)
```

**`hier_resolve` 보강 (conn 전제):** leaf(및 가능하면 중간 port)에 최소:

| 필드 | 값 |
|------|-----|
| `port_dir` | `input` / `output` / `inout` / `buffer` / `unknown` |
| `net_kind` | `port` / `wire` / `reg` / `logic` / `unknown` (있으면) |

- `buffer`: 양방향 투과에 가깝게 취급하되 beam 순위는 `inout` 과 유사 (낮춤).
- `unknown`: 방향 필터 **적용 안 함**이 아니라 **경계 교차 금지 + parse_skip** (틀린 연결 방지). 로컬 모듈 안 탐색은 가능.

역할 매핑:

| role | 그룹 | 탐색 |
|------|------|------|
| **fanout** (S) | 보통 output 쪽 묶음 | forward only |
| **fanin** (T) | 보통 input 쪽 묶음 | backward only |

### B.6.2 골격 — **2페이즈** (제안 채택·정밀화)

핵심 아이디어 (사용자 제안):

1. **Phase 1 (conn)**: fan-in 역탐색에 자원을 더 두고, fanout hierarchy에 가까워지면 fan-out 정방향을 키워 **meet** 로 연결 수집.  
2. meet 안 하는 fan-in 역경로 = 관심 밖 / 그룹 누락 / 버그 중 하나 → 당장 끊지 말고.  
3. “논리 종단(FF·연산 assign)에서 끊기”는 **meet 여부를 모르면 시기상조** → 연결 수집이 **포화**한 뒤.  
4. **Phase 2 (orphan close)**: 남은 fan-in 역탐색 frontier 를 종단 규칙으로 마저 닫아 리포트.

```text
# ----- Phase 1: CONNECT (meet-oriented) -----
F_s, visited_s ← fanout leaves   # forward
F_t, visited_t ← fanin  leaves   # backward
bias: resource_t > resource_s    # 기본 가중 (아래 스케줄)

while not connect_saturated():
  # 우선순위: (1) meet-adjacent (2) near opposite hierarchy (3) fan-in 쪽 가중
  side = pick_side_biased()      # 기본 fan-in 우세, near-S 이면 fan-out boost
  expand one node on side
  if neighbor in visited_other: record MEET pair + evidence

# ----- Phase 2: ORPHAN_CLOSE (fan-in reverse leftovers) -----
# Phase1 에서 fanout visited 와 한 번도 meet 하지 않은 T-side open paths
for each active branch in F_t (and optional unexpanded T backlog):
  continue BACKWARD only until terminal(v) or orphan_budget
  report as orphan_terminal | orphan_cut | (late meet if hits visited_s)
```

**해석 수정:** 제안 문장 “fanout의 나머지 역탐색”은 질의 방향상  
**fan-in 쪽 나머지 역탐색(orphan)** 으로 구현한다.  
(fan-out 그룹을 역방향으로 푸는 것은 방향 락 위반·비용 대비 이득 없음.  
 late meet 만 위해 Phase2 중 `visited_s` 와의 교차는 계속 검사.)

### B.6.2.1 Phase 1 자원 스케줄 (fan-in 우세 + near-fanout boost)

```text
score_expand(side, node):
  if neighbor would meet:           +1000
  if side==S and module near T:     +50    # H2/H3
  if side==T and module near S:     +80    # fan-in 이 fanout hier 에 접근 → 곧 meet 후보
  if side==T:                       +20    # 기본 fan-in 편향 (자원 더 투입)
  if side==S:                       +0
  - beam_rank ...

pick: highest score among frontier heads; tie → smaller |F|
```

- **평소**: fan-in 역탐색에 hop/파일 예산 비율 예) `budget_t : budget_s = 2:1` 또는 3:1.  
- **near-S 감지** (T frontier 의 모듈/파일이 `mods(S)`·resolve path·LCA 와 겹침):  
  같은 라운드에 **S fan-out 정방향 1~N hop 강제 expand** (boost).  
  → “가까워지는 COI 확인 후 fanout 정방향” 을 스케줄로 구현.

### B.6.2.2 `connect_saturated` ( “연결 모두 확인” 의 운영 정의 )

완전 판정 불가 → **포화 조건(OR)** 으로 Phase1 종료:

| 조건 | 의미 |
|------|------|
| `F_s`·`F_t` 둘 다 비었음 | 더 이상 meet 후보 없음 |
| `max_meets` 도달 (옵션) | 사용자 상한 |
| `max_hops_*` / `max_files` Phase1 분 소진 | 예산 |
| `no_new_meet` for K expands | 정체 |
| `F_s` 비고 `F_t` 만 남음 + T 가 `near_S` 가 아님이 L hop 지속 | fan-in 만 먼 곳으로 감 → Phase2 로 이관 |

이 시점이 “연결 수집 단계 끝”. **전수 연결 증명 아님** (리포트에 `phase1_complete: saturated` 명시).

### B.6.2.3 Phase 2 종단 규칙 (orphan fan-in reverse)

Phase1 에서 **어느 S leaf 경로와도 meet 안 한** T-역탐색 가지를 대상으로:

| 종단 `terminal(v)` | 리포트 태그 | 해석 힌트 |
|--------------------|-------------|-----------|
| const only driver (`x<=0`) | `term_const` | 고정값; 그룹 누락 가능성 낮음 |
| FF 입력 단에서 더 이상 data 선행 없음 / Q에서 한 단만 보고 중지 정책 | `term_ff` | 시퀀셜 경계 (정책: 1단 stop vs multi-cycle 계속은 플래그) |
| assign/combo 의 RHS 가 **순수 연산+const** 만 (타 관심 net 없음) | `term_logic` | “논리 연산 지점” — **완전 파싱 어려움** → P1은 const/단일 lit, P2에서 op 출현 |
| undriven / 선언만 | `term_undriven` | 버그·미연결 후보 |
| blackbox / 방향 unknown 경계 | `term_blackbox` | 미결정 |
| orphan_budget 소진 | `orphan_cut` | 미결정 |

**의도적으로 어렵다고 한 “논리 연산 종단”:**  
meet 전에 쓰면 진짜 S 경로를 일찍 자를 수 있음 → **Phase2 전용**.  
Phase1 에서는 const 말단·visited·budget 만으로 끊고, 연산 종단은 orphan close 에서만.

**orphan 해석 (자동 분류는 힌트만, 단정 금지):**

| 관찰 | 가능한 원인 (3자) |
|------|-------------------|
| term_* 으로 깨끗이 닫힘, S 모듈과 거리 멂 | 관심 밖 경로 |
| term_undriven / 열린 포트 / 맵 밖 | 기입 누락 또는 버그 |
| Phase2 중 **late meet** (`visited_s` 와 교차) | Phase1 포화 조기 → pair 추가 (보너스) |

### B.6.2.4 페이즈별 출력

```json
{
  "pairs": [ ... ],
  "orphans": [
    {
      "sink": "top.u_b.sig",
      "terminal": { "file", "line", "snippet" },
      "tag": "term_ff|term_const|term_logic|term_undriven|orphan_cut",
      "evidence": [ ... ]
    }
  ],
  "phase1": { "meets": N, "stop": "saturated|budget|empty" },
  "phase2": { "orphans_closed": M, "late_meets": K }
}
```

### B.6.3 분기·“멀어짐” 제어 (휴리스틱 — 문헌 COI 정의 밖)

| ID | 내용 |
|----|------|
| H1 Meet-first | 상대 visited 이웃 최우선 |
| H2 Frontier 모듈 근접 | 상대 frontier 모듈/파일과 겹치면 우선 |
| H3 Hierarchy LCA | resolve path 공통 prefix 쪽 인스턴스 우선 |
| H4 Beam width \(W\) | 층당 상위 \(W\) 만; 나머지 cut 기록 |
| H5 Budget | max_hops, max_files, max_edges → `cut` |

거리 휴리스틱 (구현용):

```text
h(v, other) =
  0 if v ∈ visited_other
  1 if module(v) ∈ modules(frontier_other)
  2 if module(v) in other group resolve paths
  3 else
```

### B.6.4 문헌 용어로 본 결과 해석

| 결과 | 의미 |
|------|------|
| meet | S forward cone ∩ T backward cone ≠ ∅ 및 경로 존재 (structural) |
| no meet + budget 소진 | **미결정** (classical COI 전체 계산 안 함) — `cut` 보고 |
| no meet + 양쪽 막힘 (blackbox) | 구조상 연결 증거 없음 (과소 근사 가능) |

---

## B.7 Evidence 규칙 (정확성 우선)

리포트 evidence 한 줄은 **그 줄이 주장하는 의존이 실제로 성립**해야 한다.  
“이름 `x`가 텍스트에 보인다” ≠ evidence. **역할(LHS driver / RHS use)이 맞을 때만** 엣지·증거.

### B.7.1 출력 형태 · **순서 = 탐색 경로 순**

```json
"evidence": [
  { "file": "/abs/a.sv", "line": 10, "snippet": "assign y = x;" },
  { "file": "/abs/a.sv", "line": 22, "snippet": "always_ff @(posedge clk) q <= y;" },
  { "file": "/abs/top.sv", "line": 40, "snippet": "UART_TX u0 (.a(n), .b(m));" }
]
```

- 사용자 대면 필드: **file / line / snippet** 만.
- 구현 내부 Edge는 `lhs`, `rhs_idents[]`, `kind` 를 가져도 됨 (정확성 검증용; JSON 기본 출력 optional).

**순서 규칙 (필수):**

- `evidence[]` 는 **연결 경로를 source(fanout) → sink(fanin) 방향으로 따라갈 때의 엣지 순** 과 동일.
- 즉 리뷰어가 배열을 **위에서 아래로** 읽으면, 신호가 전달·의존되는 순서와 같다.
- bi-search 로 T 쪽은 역방향으로 밟았더라도, **리포트 시 path reconstruct 후 뒤집어서** S→T 순으로 기록한다. (탐색 방문 시각순·파일명순·줄번호순 정렬 금지)
- meet 점에서 S-path prefix + T-path reversed suffix 를 이어 붙인 결과가 최종 `evidence[]`.
- OR 합류 시 대표 path(C3 prev) 기준 한 줄 순서; multi-path 옵션 시 path마다 각자의 순서 유지.
- 같은 물리 줄이 경로에 두 번 의미 없이 반복되면 연속 중복만 접을 수 있음(옵션). **순서를 바꿔 재정렬하지는 않음.**

### B.7.2 문장 단위 — `x<=0` 과 `x<=y&z` (예상함)

할당 **문장** 단위로 LHS / RHS를 분리한다. 둘 다 hit 되지만 **역할이 다름**.

| 문장 | LHS | RHS idents | 의존 (영향 방향 driver→load) |
|------|-----|------------|------------------------------|
| `x <= 0;` | `x` | *(const, net 없음)* | **net→net 엣지 없음**. “x가 이 줄에서 drive됨”만. fan-in 확장 시 선행 net 추가 없음 (const 말단). |
| `x <= y;` | `x` | `y` | `y → x` |
| `x <= y & z;` | `x` | `y`, `z` | `y → x` **그리고** `z → x` |
| `x <= y ? a : b;` | `x` | `y`,`a`,`b` | 각 RHS → `x` (조건 의미 무시, structural over-approx) |
| `assign x = y & z;` | 동일 | 동일 | 동일 |

**탐색 방향별:**

| 보고 있는 net | `x <= 0;` | `x <= y & z;` |
|---------------|-----------|----------------|
| fan-**in** of `x` (누가 x를 만드나) | driver 문장 hit, 선행 net 없음 | 이웃 `y`,`z` + 이 줄 evidence |
| fan-**out** of `x` (x가 누굴 만드나) | RHS에 x 없음 → **out 이웃 없음** | (이 문장만으로는 out 없음; LHS가 x) |
| fan-out of `y` | 무관 | 이웃 `x` + 같은 evidence |

스캐너가 “줄에 x 있음”만 보고 양방향 엣지를 걸면 **근거 오류**. 반드시 LHS/RHS 역할 게이트.

### B.7.3 always 블록

```text
always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) x <= 0;
  else        x <= y & z;
end
```

| 해야 함 | 하지 말아야 함 |
|---------|----------------|
| 본문의 `<=` / `=` **할당 문장**만 data dependency | sensitivity list의 `clk`/`rst_n`을 data fan-in에 넣기 (1차 기본 **제외**) |
| 할당마다 LHS·RHS 분리, 줄별 evidence | `always` 헤더 한 줄로 x 증거 퉁치기 |
| 다중 할당 → 다중 엣지 | 다른 driver 무시 |
| `\bx\b` word boundary | `xx`, `x_q` 부분 매칭 |
| `strip_sv_comments` 후 스캔 | 주석 안 할당을 evidence로 |

**if/else·case (1차):** 모든 분기 할당을 structural **union** (죽은 분기 포함 가능).  
조건 ident (`if (en)`) 는 P1 기본 **data path만** (control dep 넣으면 cone 오염). 넣으면 optional + over-approx 표시.

### B.7.4 Evidence 채택 게이트

엣지 \(u \to v\) 생성 시 모두 만족:

1. 할당 문장 파싱 성공 (LHS ident + RHS 토큰).
2. 역할: forward at `u` → RHS∋`u`, LHS=`v` / backward at `v` → LHS=`v`, RHS∋`u` 또는 const 말단.
3. snippet = 그 할당이 있는 물리 줄 (cap ~200자).
4. file/line = 원본 좌표.

**거부:** 선언만 있는 줄, `$display("x")`, port_map을 always로 오인, 부분 식별자 매칭.

### B.7.5 const·다중 드라이버

| 상황 | 동작 |
|------|------|
| `x <= 0` / `'0` | const 말단; pair 중간이면 “앞 net 없음” |
| `x <= y & z` | backward 시 y·z 둘 다 후보 |
| 다중 always/assign → 같은 x | 드라이버별 엣지; visited로 루프 방지 |
| `x <= x + 1` | self-edge; 재방문 금지 |

### B.7.6 철학

- **틀린 evidence 한 줄이, 못 찾은 경로보다 해롭다** → precision > recall.
- 애매하면 엣지 금지, `cut` / `parse_skip` 으로 정직한 미결정.
- MD는 pair당 evidence bullet; 디버그 모드에서만 lhs/rhs/kind.

---

## B.8 로그

```text
[YYYY-MM-DD HH:MM:SS] (+   0.012s) hier_conn START
[YYYY-MM-DD HH:MM:SS] (+   0.050s) explore src  chip_top.u_uart0.txd  file=...
[YYYY-MM-DD HH:MM:SS] (+   0.051s) explore dst  chip_top.u_noc_e0.out file=...
[YYYY-MM-DD HH:MM:SS] (+   0.120s) meet  ...  pairs+=3
[YYYY-MM-DD HH:MM:SS] (+   1.200s) TOTAL_HIER_CONN_SEC=1.200
```

---

## B.9 파이프라인

```text
build_db      → modules.json
hier_resolve  → hier_resolve.json (+ .miss.md)
hier_conn     → hier_conn.json / .md
                --map --resolve --src-list --dst-list
```

- resolve `miss` 제외  
- `ok_needs_detail` 은 base 신호로 탐색, flag 한 줄

---

## B.10 구현 페이즈

| Phase | 내용 | 문헌 대응 |
|-------|------|-----------|
| **P1** | 모듈 내 assign/FF; port 경계; bi-BFS; OR 캐시(C1–C5); seed select 보존; evidence; 로그 | structural COI + slice identity |
| **P2** | 2페이즈·fan-in 우세; Range overlap meet; interval/bitset; beam | 효율 + slice 매핑 정밀 |
| **P3** | 다차원·generate index·pyslang 폭; always 정밀 | 나머지 배열/의미론 |

---

# Part C — 정리

### 문헌에서 확인한 사실

1. COI reduction 의 표준 정의는 **명세 변수의 combinational dependency graph closure** 와 **그 밖 변수 제거** 이다 (Clarke/Biere/Raimi/Zhu).
2. **Bounded COI** 는 유한 \(k\) 에서 필요한 support만 취해 CNF를 더 줄인다.
3. 툴(NuSMV)은 “프로퍼티에 무관한 변수 제거”로 동일 기법을 노출한다.
4. AIG/IC3 문헌은 노드 COI 를 **fan-in 재귀** 로 정의하고 SAT domain 축소에 쓴다.
5. SPIN’25는 static(전역 불필요) vs on-the-fly(문맥 불필요) 를 구분한다.
6. COI 는 Kurshan localization 의 특수한 경우로 언급된다.

### 프로젝트 결정 (리뷰 대상)

1. 그룹 S/T leaf 를 “명세 원자” 자리에 둔다.
2. 1차는 **structural dependency** (assign/FF/port_map).
3. 연결 = **S forward cone 과 T backward cone 의 meet + path evidence**.
4. 탐색은 **bidirectional + beam/budget** (문헌 COI 정의의 상위 공학 층).
5. 증거 **file/line/snippet**, 로그 **시간+hierarchy**.
6. **코딩 전** 이 문서 합의.

---

## 참고 문헌 (읽은 자료)

1. E. Clarke, A. Biere, R. Raimi, Y. Zhu. *Bounded Model Checking Using Satisfiability Solving*. FMSD 19(1), 2001. §5.1 Classical / Bounded COI.  
2. C. Telbisz, L. Bajczi, D. Szekeres, A. Vörös. *On-the-fly Cone-of-Influence Reduction for Model Checking Concurrent Software*. SPIN 2025.  
3. P. Arcaini, A. Gargantini, E. Riccobene. *NuSeen: a tool framework for the NuSMV model checker*. (COI option 설명).  
4. Y. Su et al. *Deeply Optimizing the SAT Solver for the IC3 Algorithm* (GipSAT). COI = recursive fan-ins.  
5. C. Yu, W. Che, H. Zhang. *FRAIG-BMC*. Constraint COI = transitive fan-in.  
6. S. Berezin, A. Biere, E. Clarke, Y. Zhu. FMCAD’98 (COI reduction 언급).  
7. N. Sharygina. CMU guest lectures — complexity reduction (COI listed among techniques).  
8. R. Kurshan. *Computer-Aided Verification of Coordinating Processes* — localization reduction (Clarke et al.이 COI의 상위 개념으로 인용).

PDF 사본: `pyhirewalk/downloads/`.
