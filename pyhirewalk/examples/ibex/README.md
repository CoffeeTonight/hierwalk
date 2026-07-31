# Ibex demo for pyhirewalk

Target: [lowRISC/ibex](https://github.com/lowRISC/ibex) (clone under `$IBEX_ROOT`, default `/tmp/rtl-bench/ibex`).

## Run

```bash
# once: clone ibex
git clone --depth 1 https://github.com/lowRISC/ibex.git /tmp/rtl-bench/ibex

cd /path/to/pyhirewalk
python3 pyhirewalk.py --target ibex --steps all
```

Outputs under `examples/ibex/work/`:

| File | Role |
|------|------|
| `ibex.modules.json` | build_db module map |
| `hier_resolve.json` | path resolve |
| `hier_conn.json` | regex structural meet |
| `hier_slang.json` | pyslang structural meet |
| `pyhirewalk_summary.json` | timings |

## Engines

- **hier_conn** (`conn/`): regex assign / named port + resolve-only hierarchy climb  
- **hier_slang** (`conn/slang.py`): scoped pyslang elab → edges + hybrid local regex assign  

Boundary climb uses **only instances on hier_resolve paths** (no inventing children from the module map).

## Checks (see `run_ibex.json`)

| id | Expect |
|----|--------|
| `alu_leaf` | slang often finds; regex weak on always_comb mux |
| `alu_equal_out` | both (continuous assign chain) |
| `ex_to_alu` | both (named port) |
| `if_to_id_instr` | both (core wire between stages) |
| `no_conn_noise` | neither (unrelated) |
