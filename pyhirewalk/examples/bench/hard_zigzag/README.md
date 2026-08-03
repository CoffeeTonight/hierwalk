# hard_zigzag — extreme COI stress design

Constructs mixed in one elaboratable top (`zz_top`):

| Feature | Where |
|---------|--------|
| **generate-for** | `g_lane[i].u_lane`, `zz_pipe` stages, `zz_xbar` bits, **`zz_branch` arms** |
| **generate-if** | lane invert, pipe mid tap, wide/narrow slice, **per-arm transform** |
| **case** | lane op mux, xbar lane pick, zig mode mix, **nested 2-level branch mux** |
| **FF** | lane `y_q_o`, **8 arm FFs**, pipe `q_s[0..STAGES-1]`, zig `south_q_o` |
| **comb** | assigns + always_comb throughout; **pairwise OR-tree** |
| **parameter** | `W,N,STAGES,B_ARMS,D_ARMS,USE_INV`, per-lane `LANE_INV` |
| **ifdef** | `ZZ_ALT_PATH` in `zz_zig`, optional `ZZ_SIDE` on top |
| **branch / fan-out** | `u_branch`: 8 live arms + 4 dead noise arms |

## Zigzag intended path

```text
a_i
  -> g_lane[k].u_lane (port)
  -> case / gen-if / param slice (comb)
  -> y_q_o (FF #1)
  -> lane_yq[k] (array)
  -> u_xbar case + gen-for bits
  -> u_branch.d_i
       |-- g_arm[0..7] distinct comb + FF   (8-way FAN-OUT)
       |     |-- nested case (lo/hi) -> join_o
       |     +-- pairwise OR-tree     -> any_o
       +-- g_dead[0..3] from noise_i only (never joins)
  -> u_pipe d_i=join_o -> q_s[0]..q_s[STAGES-1] (FF #2..#N)
  -> east=q_o, north=mid_o (reconverge)
  -> u_zig case + ifdef (comb)
  -> south_q_o (FF) / west_o (comb)
  -> result_q_o / result_o
```

## Branch exploration goldens (answer key)

| id | Expect | What it tests |
|----|--------|----------------|
| `branch_fanout_d_to_all_arms` | connected, **min_pairs=8** | single source finds all 8 arm sinks |
| `branch_fanout_via_top_pads` | connected, min_pairs=3 | sparse multi-sink via top pads |
| `branch_nested_mux_to_join` | connected, min_pairs=4 | reconverge through nested case |
| `branch_or_tree_reconverge` | connected, min_pairs=4 | full OR-tree meet |
| `branch_d_to_join_through_mux` | connected | fan-out then join on main path |
| `branch_join_into_pipe_and_result` | connected | branch sits on extreme tail |
| `branch_a_reaches_any_o` | connected | long path + all-arm reconverge |
| `dead_noise_to_dead0` | connected | noise forest is real |
| `noise_dead_not_on_join/result` | disconnected | dead forest isolation |
| `live_arm_not_dead` | disconnected | live vs dead isolation |

## Run

```bash
cd /path/to/pyhirewalk

# default: no ZZ_ALT_PATH
python3 examples/bench/run_bench.py --only hard_zigzag

# ifdef alternate polarity (topology unchanged, including branch)
python3 examples/bench/run_bench.py --only hard_zigzag_alt

python3 examples/bench/run_bench.py --only hard_zigzag,hard_zigzag_alt
```

| Config | Defines | Notes |
|--------|---------|-------|
| `hard_zigzag` | (none) | full suite incl. branch fan-out |
| `hard_zigzag_alt` | `ZZ_ALT_PATH=1` | same topology; zig `alt_w = ~mix_w` |

## COI-until (FF budget, not bi-meet)

```bash
python3 examples/bench/run_coi_until_loop.py --only coi_zigzag
# cases: FF:1 lane → FF:2 branch → FF:3 pipe0 → FF:4 result (mid path) + noise
```

See `examples/bench/COI_UNTIL_LOOP.md`.
