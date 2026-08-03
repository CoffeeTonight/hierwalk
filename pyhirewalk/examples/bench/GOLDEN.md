# Ground-truth connectivity cases (RTL-first)

Method: **review RTL → write expect → run tool → fix tool or tighten golden**.
Never invent expect from tool output alone.

## Suite status (2026-08-01)

```text
python3 examples/bench/run_bench.py --only hard_patterns,serv,picorv32,skidbuffer,darkriscv,axil_register_rd,ibex,cv32e40p_fpu
# expected: all PASS
```

| Design | Time | Checks | Hard patterns |
|--------|------|--------|----------------|
| hard_patterns | ~2.5s | 8 | generate-for, gen-if, case, interface, bit-slice |
| serv | ~4s | 4 | bit-serial ALU, decode→alu ports |
| picorv32 | ~3s | 4 | always @* ALU, assign ports |
| skidbuffer | ~1.3s | 2 | always_ff + generate OPT_*, bypass |
| darkriscv | ~2s | 3 | net initializers, ternary wire, always_ff decode |
| axil_register_rd | ~1.8s | 2 | AXI-Lite channel pipeline regs |
| ibex | ~19s | 8 | core cone, port, slice isolation |
| **cv32e40p_fpu** | **~49s** | **5** | **multi-dim array fan-in, FPU/APU, generate, cross-hier** |

## Complex path groups (two groups a[] / b[])

### 1. hard_patterns — generate + xbar + interface

**RTL:** `examples/bench/hard_patterns/rtl/`

| id | Group A | Group B | Expect | Why (RTL) |
|----|---------|---------|--------|-----------|
| lane0_port_a | `hard_top.a_i` | `hard_top.g_lane[0].u_lane.a_i` | connected | generate-for port map |
| lane0_internal_y | `…u_lane.a_i` | `…u_lane.y_o` | connected | case mux + generate-if inv |
| a_to_result_cross | `hard_top.a_i` | `hard_top.result_o` | connected | a→lanes→xbar→result |
| stream_data | `hard_top.result_o` | `hard_top.stream_data_o` | connected | interface pipe s0→u_pipe→s1 |
| lane_vs_stream_valid | `…y_o` | `hard_top.stream_valid_o` | disconnected | data vs control |

### 2. SERV — multi-instance wire-through

**RTL:** `/tmp/rtl-bench/serv/rtl/serv_{top,alu,decode}.v`

| id | Group A | Group B | Expect |
|----|---------|---------|--------|
| alu_rs1_to_rd | `serv_top.alu.i_rs1` | `serv_top.alu.o_rd` | connected |
| decode_alu_sub | `serv_top.decode.o_alu_sub` | `serv_top.alu.i_sub` | connected |
| noise_alu_to_ibus | `serv_top.alu.o_rd` | `serv_top.i_ibus_rdt` | disconnected |

### 3. PicoRV32 — procedural ALU

| id | Group A | Group B | Expect |
|----|---------|---------|--------|
| reg_op1_to_alu_out | `picorv32.reg_op1` | `picorv32.alu_out` | connected |
| noise_mem_rdata_to_trap | `picorv32.mem_rdata` | `picorv32.trap` | disconnected |

### 4. DarkRISCV — initializer + decode

| id | Group A | Group B | Expect |
|----|---------|---------|--------|
| idata_to_xidata | `darkriscv.IDATA` | `darkriscv.XIDATA` | connected |
| idatax_to_xlui | `darkriscv.IDATAX` | `darkriscv.XLUI` | connected |

### 5. Ibex — production-shaped cone

See `configs/ibex.json` (from `examples/ibex/run_ibex.json` + noise fix).
Noise pair uses **`debug_req_i`** (external pin), not `pc_if_o` — RF-mediated
structural paths made the old pair a false “disconnect”.

### 6. CV32E40P + FPU — multi-dim array fan-in (hard open-source)

**Design:** OpenHW Group [cv32e40p](https://github.com/openhwgroup/cv32e40p) under `/tmp/rtl-bench/cv32e40p`,
`parameters.FPU=1` (enables APU path; default FPU=0 ties array to 0).

**RTL evidence** (`rtl/cv32e40p_id_stage.sv` gen_apu):

```systemverilog
logic [APU_NARGS_CPU-1:0][31:0] apu_operands;  // [2:0][31:0]
if (APU_NARGS_CPU >= 1) assign apu_operands[0] = alu_operand_a;
if (APU_NARGS_CPU >= 2) assign apu_operands[1] = alu_operand_b;
if (APU_NARGS_CPU >= 3) assign apu_operands[2] = alu_operand_c;
```

| id | Group A | Group B | Expect | Difficulty |
|----|---------|---------|--------|------------|
| `alu_ops_into_apu_array` | 3× `alu_operand_{a,b,c}` | `id_stage_i.apu_operands` | connected | multi-source → packed 2D array |
| `alu_ops_to_top_apu_array` | same 3 operands | `cv32e40p_top.apu_operands` | connected | array + pipeline FF + multi-module |
| `top_array_to_fp_wrapper` | top `apu_operands` | `fpu_gen.fp_wrapper_i.apu_operands_i` | connected | generate-if FPU + interface array |
| `mult_dot_char_packed_array` | `mult_i.dot_op_a_i` | `mult_i.dot_char_op_a` `[3:0][8:0]` | connected | byte-sliced pack into 2D array |
| `noise_apu_array_vs_debug_req` | top array | `debug_req_i` | disconnected | negative control |

Config: `configs/cv32e40p_fpu.json` · filelist: `filelists/cv32e40p_fpu.f` (FPU manifest).

**Tool needs:** `-G FPU=1` (config `parameters`), full FPU filelist (~56 RTL files).

## Bugs found & fixed while hardening

1. **Generate hierarchy collapsed** — empty `GenerateBlockSymbol.name`; now use `hierarchicalPath` / `entries`.
2. **Generate-if split module nets** — assigns under generate used generate path as NetKey; now `owner_hier`.
3. **Cone skipped children of top-only seeds** — `is_cone_hier` now includes descendants of prefixes.
4. **`TimedStatement.stmt` ignored** — `always_ff` / `always @` produced `n_proc=0`; walk `.stmt`.
5. **Net initializers ignored** — `wire x = y` is not ContinuousAssign; walk `NetSymbol.initializer`.
6. **Genvar port index** — `selector.constant` after elab; `_const_int` reads `.constant`.
7. **Port actual AssignmentExpression** — `lane_y[gi]` wrapped as Assignment; walk left ElementSelect.
8. **Noise goldens too weak** — long structural paths via RF looked “connected”; pick true external/unrelated sinks.

## hard_zigzag (extreme)

```bash
python3 examples/bench/run_bench.py --only hard_zigzag
# 13/13 PASS — end-to-end a_i → result_q_o with proc≥6, evidence≥22
```

See `hard_zigzag/README.md`. Mixes generate-if/for, case, multi-stage FF,
comb, parameters, ifdef, multi-dim array, and reconvergent zig paths.

## Multi-FF / multi-hop checks

Golden checks may set:

- `min_proc`: minimum `via=proc` edges in best pair (posedge/always bodies)
- `min_evidence`: minimum evidence hops in best pair

| Config | Multi-FF style checks |
|--------|------------------------|
| **axil_register_rd** (`AR_REG_TYPE=2` skid) | `ff1_s_to_temp_*`, `ff2_temp_to_mreg_*`, R-channel temp→sreg |
| **cv32e40p_fpu** | `ff1_id_ex_*` (ID/EX always_ff), `multi_ff_operand_a_to_top_apu` (assign+FF+ports) |
| **darkriscv** | `multi_ff_idata_to_xidata` (wire + always_ff, min_evidence≥2) |
| **skidbuffer** | `idata_to_odata/rdata` with `min_proc=1` |

```bash
python3 examples/bench/run_bench.py --only axil_register_rd,darkriscv,skidbuffer,cv32e40p_fpu
```

## Select-accurate matching (pyslang NetKey)

As of the select-aware graph:

- **NetKey** = `(hier, base, sel)` with `sel` like `""` / `"[0]"` / `"[31:0]"`.
- **Partitioned arrays** (e.g. `apu_operands[i] = op_*`): element seeds require a
  **direct** pyslang edge; wrong index is `disconnected`.
- **Soft bit seeds** on buses without multi-writer element edges: expand via whole
  base + seed labels (`slice_identity` / base).
- CV32E40P element checks: `elem_a_to_apu0`, `elem_b_to_apu1`, `elem_c_to_apu2`,
  `elem_b_not_apu0` — all green.

## Known gaps (tracked)

- Soft bit-select paths still mostly **seed-label** identity when RTL has no
  per-bit assign edges.
- Full-chip cones (SweRV, BlackParrot, NVDLA, …) clone-only, not all goldened.
- `pyslang.analysis.ValueDriver` not used yet.

## How to extend

1. Pick design under `/tmp/rtl-bench/<name>/`.
2. RTL-review two path groups; write `configs/<name>.json` with `expect` + `why`.
3. `python3 examples/bench/run_bench.py --only <name>`
4. Fix extractor or refine golden; never flip expect without RTL evidence.
