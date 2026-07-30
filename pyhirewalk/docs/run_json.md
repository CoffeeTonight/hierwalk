# Run JSON — 통합 실행 설정

`paths.txt` 대신 **하나의 JSON** 으로 compile context + hierarchy + conn 질의를 관리한다.  
`build_db` / `hier_resolve` / (예정) `hier_conn` 이 같은 문서를 읽는다.

JSONC 허용 (`//`, `/* */`, trailing comma).  
상대 경로는 **이 JSON 파일이 있는 디렉터리** 기준.  
`env` 의 `$VAR` 는 filelist·경로 필드 확장에 사용 (Verilog define 과 별개).

---

## 최소 골격

```jsonc
{
  "filelist": "filelist.f",
  "top": "chip_top",
  "jobs": 8,

  "cwd": ".",

  "env": {
    "PROJ": "/proj/chip",
    "RTL_ROOT": "${PROJ}/rtl"
  },

  // RTL `ifdef / `ifndef 및 +define+  (path env 와 분리)
  "defines": {
    "NO_CPU": "1",
    "FEATURE_A": "1"
  },

  "build_db": {
    "output": "work/essential.sqlite",
    "work_dir": "work",
    "mode": "fast",
    "scan_workers": 8,
    "modules_json": "work/essential.modules.json"
  },

  // optional: flat path resolve only
  "hier_resolve": {
    "paths": [
      "chip_top.u_sys.u_cpu.clk"
    ]
  },

  // group connectivity (a = fanout sources, b = fanin sinks by default)
  "run_conn_check": {
    "checks": [
      {
        "id": "cpu",
        "a": [
          "chip_top.u_sys.u_cpu.o_req_valid",
          "chip_top.u_sys.u_cpu.o_req_data[31:0]"
        ],
        "b": [
          "chip_top.u_sys.u_noc.i_cpu_valid",
          "chip_top.u_sys.u_noc.i_cpu_data[31:0]"
        ]
      },
      {
        "id": "debug_uart",
        "a": ["chip_top.u_dbg.txd"],
        "b": ["chip_top.u_uart.rxd"],
        "a_role": "fanout",
        "b_role": "fanin"
      }
    ]
  }
}
```

---

## 필드 요약

| 키 | 용도 |
|----|------|
| **filelist** | 최상위 `.f` (필수) |
| **top** | top module 이름 |
| **jobs** | 병렬도 (build_db scan 등). `build_db.scan_workers` 와 동일 계열 |
| **cwd** / index_cwd | filelist 해석 cwd (`-F` 등) |
| **env** | 셸/`$PROJ` 경로 확장 (filelist 안 경로) |
| **defines** | `` `ifdef`` / `` `ifndef`` / `+define+` 매크로. 예: `"NO_CPU": "1"` |
| **build_db** | sqlite / modules.json / mode / workers |
| **modules_json** | modulename→filepath 맵 (build 출력 = resolve/conn 입력) |
| **hier_resolve.paths** | (옵션) 단건 path 나열 — **그룹 질의는 run_conn_check 권장** |
| **run_conn_check.checks** | id + **a[]** + **b[]** hierarchy 묶음 |

### defines vs env

| | **defines** | **env** |
|--|-------------|---------|
| 예 | `NO_CPU`, `SYNTHESIS` | `PROJ`, `RTL_ROOT` |
| 쓰임 | RTL 전처리 조건, slang `+define+` | `.f` 안 `$VAR` 경로 |
| hier_resolve | `apply_sv_ifdefs` 에 전달 | filelist expand 만 |

### check 한 항목

| 필드 | 의미 |
|------|------|
| **id** | 체크 이름 (로그·리포트 키) |
| **a** | hierarchy path 배열 (기본 **fanout / source**) |
| **b** | hierarchy path 배열 (기본 **fanin / sink**) |
| **a_role** / **b_role** | 기본 `fanout` / `fanin` (바꿀 때만) |

path 한 줄 = resolve 와 동일 문법 (`top.u_x.sig`, slice는 `[7:0]` 등 포함 가능).

`checks` 는 배열 또는 id→객체 맵 모두 허용:

```jsonc
"checks": {
  "cpu": { "a": ["..."], "b": ["..."] }
}
```

---

## 도구별 사용

```bash
# 1) 인덱스
python3 build_db.py --config run.json

# 2) resolve — JSON에서 run_conn_check.checks[].a/b 만 hierarchy 로 사용
#    (filelist/env/hier_resolve.paths/기타 키는 path 입력으로 쓰지 않음)
python3 hier_resolve.py --config run.json --map work/essential.modules.json \
  -o work/hier_resolve.json

# 3) conn (예정) — 같은 checks 그룹 사용
python3 hier_conn.py --config run.json
```

동일 `defines` / `env` / `modules_json` 이 전 단계에 공유된다.

### hier_resolve 입력 변환 (checks a/b only)

```text
run.json
  run_conn_check.checks[i].a[]  ──┐
  run_conn_check.checks[i].b[]  ──┼→ load_hier_resolve_inputs()
  defines (ifdef only)          ──┤
  modules_json (map only)       ──┘
       ✗ filelist / env / hier_resolve.paths / 기타 키 → hierarchy 아님
                                            ▼
                                   List[str] paths
                                            │
                                   resolve_many(paths)   # 기존 함수
```

헬퍼: `pyhirewalk.run_config.load_hier_resolve_inputs(path)`.

---

## 로더 API

```python
from pyhirewalk.run_config import load_run_config

cfg = load_run_config("run.json")
cfg.filelist, cfg.top, cfg.jobs
cfg.defines          # {"NO_CPU": "1", ...}
cfg.env
cfg.conn_checks      # tuple[ConnCheck, ...]
cfg.conn_checks[0].id, .a, .b
cfg.resolve_paths
cfg.modules_json
```

파서: `src/pyhirewalk/run_config.py` (`ConnCheck`, `parse_conn_checks`, `load_run_config`).
