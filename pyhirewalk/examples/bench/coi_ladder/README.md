# coi_ladder — COI-until (FF:N) validation

Separate from `hard_zigzag`. Clear FF-depth ladder so stop conditions are
RTL-obvious.

## Topology (fan-out from `a_i`)

```text
a_i --comb--> pre_w                 (ff=0)
                |-- pure_comb_o     (ff=0 leaf → exhaust under FF:2)
                |--FF#1--> s0_q
                             |-- short_o          (ff=1 leaf → exhaust)
                             |--FF#2--> s1_q      (ff=2 → SATISFY, stop)
                             |            +--FF#3--> s2_q / deep_o  (must NOT enter)
                             +--FF#2--> fork_q[i] (parallel SATISFY)
noise_i --> dead_q                  (not on a_i COI)
```

## Answer key (`until=FF:2`)

| Expect | Nets |
|--------|------|
| **must_satisfy** | `s1_q` / `u_s1.q_o`, `fork_q_o[0]`, `fork_q_o[1]` |
| **must_not_coi** | `s2_q`, `deep_o`, `dead_*` |
| **exhausted leaves** | `pure_comb_o` (0 FF), `short_o` (1 FF) |
| **max_ff_in_coi** | 2 (stop discipline) |

## Run

```bash
cd /path/to/pyhirewalk

# full loop matrix (FF:1/2/3 + fan-in)
python3 examples/bench/run_coi_until_loop.py --only coi_ladder

# single case
python3 coi_until.py \
  --config examples/bench/configs/coi_ladder.json \
  --until FF:2 --verify \
  -o examples/bench/work/coi_ladder.coi_until.json
```

Logs: stderr with timestamps + cumulative seconds.  
JSON: `meta.timings_sec` (compile/graph/search/total) + `coi.satisfied|exhausted`.  
Loop log: `examples/bench/COI_UNTIL_LOOP.md`.

**FF counters** follow the shortest-hop BFS path to each net (not max-FF path).
