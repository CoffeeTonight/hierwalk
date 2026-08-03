# COI-until loop engineering log

Method: **answer key → run → fail → root-cause → fix engine/golden → re-run** until green,
then raise difficulty.

## Harness

```bash
python3 examples/bench/run_coi_until_loop.py
python3 examples/bench/run_coi_until_loop.py --only coi_ladder,coi_zigzag
python3 examples/bench/run_coi_until_loop.py --list
```

Configs with `coi_until` / `coi_until_cases` are discovered automatically.

## Loop results (2026-08-01)

### L1 — ladder matrix (FF:1/2/3 + fan-in)

| Case | until | Result |
|------|-------|--------|
| `coi_ladder_ff1` | FF:1 | PASS |
| `coi_ladder_ff2` | FF:2 | PASS |
| `coi_ladder_ff3` | FF:3 | PASS |
| `coi_ladder_ff2_fanin_deep` | FF:2 fanin | PASS |

### L2 — hard_zigzag COI (not bi-meet)

| Case | until | Result |
|------|-------|--------|
| `coi_zz_ff1` | FF:1 lane y_q | PASS |
| `coi_zz_ff2` | FF:2 branch arms | PASS |
| `coi_zz_ff3` | FF:3 pipe q_s[0] | PASS |
| `coi_zz_ff4_mid` | FF:4 mid / q_s[1] | PASS |
| `coi_zz_ff5_result` | FF:5 result via mid | PASS |
| `coi_zz_noise_isolated` | FF:1 noise | PASS |

**TOTAL: 10/10 PASS** · search ≪ 10ms · wall dominated by compile/graph.

Example zigzag timings (from loop summary): `coi_zz_ff5_result` search≈8ms, total≈2.9s.

### L3 — engine fixes from failures

| Failure | Root cause | Fix |
|---------|------------|-----|
| zigzag FF:1 sat on `mux_w` not `y_q` | `always_comb` edges tagged `proc` | `procedureKind` → `comb` vs `proc` |
| noise `must_satisfy` with high FF:N | short path never reaches budget | `must_coi` expect + FF:1 case |
| mid/result FF under-count | **uninstantiated `g_mid0`** walked + whole-base assign approx `mid_o←q_s` | skip `isUninstantiated` generate; no whole-approx for assign/proc/comb (port only) |

Side effect: bi-meet `min_proc` more accurate (comb not counted; mid path needs real q_s[1] FF);
`hard_zigzag` / `hard_patterns` still PASS.

### L4 — OSS smoke (no golden, smoke only)

| Design | until | wall | n_coi | search |
|--------|-------|------|-------|--------|
| skidbuffer | FF:2 | ~1.5s | 5 | 0.4ms |
| picorv32 | FF:2 | ~4.6s | 37 | 1.5ms |
| cv32e40p_fpu | FF:2 | ~94s | 899 | 39ms |

### L5 — residual 3b closed (false mid)

**RTL:** STAGES=3 ⇒ `mid_o = q_s[1]` only (`g_mid`); `g_mid0` uninstantiated.

**Before:** `q_s[0]→mid_o` + `q_s→mid_o` approx ⇒ first-arrival result at **FF:4**.

**After (answer key first):**

| Case | until | Expect |
|------|-------|--------|
| `coi_zz_ff4_mid` | FF:4 | satisfy mid / q_s[1]; **not** result |
| `coi_zz_ff5_result` | FF:5 | satisfy result_q / south_q |

**Shipped tests:** `tests/test_coi_until.py` → `test_hard_zigzag_no_false_mid_from_qs0` (real compile + graph + coi_until).

## Path cost model

FF/assign/port counters follow the **shortest-hop BFS first-visit** path to each
net (`stats.path_cost_model = shortest_hop_bfs_first_visit`). This is not the
max-FF path. Documented in `coi_until` module docstring.

## Code-review harden (2026-08-02)

- Unified `skip_array_el` default **False** on `_neighbors` and `coi_until`
- Satisfied/exhausted list **dedupe**; removed dead `blocked_all_new`
- `verify_expect` strict-ish match (exact / `.suffix` / leaf segment only)
- `must_exhaust` alias; no soft “in COI ⇒ exhaust ok”
- Counters track `comb` / `array_el` for observability
- Loop harness deletes ephemeral configs after each case
- **False exhausted fix:** neighbors already visited (diamond/reconverge) →
  `internal`, not `exhausted` (only structural leaves exhaust)
- CLI: any unresolved seed → exit 1

## Residual risks / next loops

1. **First-arrival hop BFS** — still not max-FF path; multi-visit `(net, ff)` open.
2. **array_el whole→word-index** can still under-count stage arrays; strict
   `skip_array_el=True` is opt-in (breaks bit-blast xbar if overused).
3. **OSS goldens** — only smoke so far.
4. **Reuse compile** across cases — still recompiles per case.
5. Soft bit-select bi-meet recall after whole-approx assign mirrors removed.
