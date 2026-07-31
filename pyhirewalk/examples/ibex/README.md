# Ibex demo for pyhirewalk

Target: [lowRISC/ibex](https://github.com/lowRISC/ibex) (clone under `$IBEX_ROOT`, default `/tmp/rtl-bench/ibex`).

## Run

```bash
# once: clone ibex
git clone --depth 1 https://github.com/lowRISC/ibex.git /tmp/rtl-bench/ibex

cd /path/to/pyhirewalk

# A) full pipeline (regex conn + slang hybrid)
python3 pyhirewalk.py --target ibex --steps all

# B) hier_pyslang — env + defines + generate + bit-select meta
#    --cone-walk (default): extract only under seed hierarchy prefixes
#    --cone-files: shrink filelist via modules_json (faster compile)
python3 hier_pyslang.py --config examples/ibex/run_ibex.json \
  --cone-files --map examples/ibex/work/ibex.modules.json \
  -o examples/ibex/work/hier_pyslang.json
# full-design extract: add --no-cone-walk

# C) early experiment script (still usable)
python3 examples/ibex/pyslang_group_conn.py
```

Outputs under `examples/ibex/work/`:

| File | Role |
|------|------|
| `ibex.modules.json` | build_db module map |
| `hier_resolve.json` | path resolve |
| `hier_conn.json` | **regex** structural meet |
| `hier_pyslang.json` | **pyslang** structural meet (env/defines/elab) |
| `pyslang_group_conn.json` | early experiment dump |
| `pyhirewalk_summary.json` | timings |

## Engines

| Tool | Role |
|------|------|
| **hier_conn** | regex; cheap path; leave as-is |
| **hier_pyslang** | pyslang elab; generate folded; env+defines; **bit-slice isolation** (`slice_policy.py`) |

### Bit-slice rules (do not blur)

- Graph expand key = `(instance_hier, base_name)` (structure).
- Labels / pairs are isolated by **sel_class** (`[0]` ≠ `[1]`).
- Pair fields: `connectivity_level` = `base` | `slice_identity` | `slice_hint` | `select_approx`.
- Different literal slices **never** form a pair with each other.
- Whole-net vs slice may pair only with `--allow-base-meet` and level=`base` notes.

`pyhirewalk.py --target ibex --steps all` runs db → resolve → conn → **pyslang**.

## Checks (see `run_ibex.json`)

| id | Expect |
|----|--------|
| `alu_leaf` | slang often finds; regex weak on always_comb mux |
| `alu_equal_out` | both (continuous assign chain) |
| `ex_to_alu` | both (named port) |
| `if_to_id_instr` | both (core wire between stages) |
| `no_conn_noise` | neither (unrelated) |
