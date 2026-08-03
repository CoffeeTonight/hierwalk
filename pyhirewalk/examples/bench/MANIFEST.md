# Open-source RTL bench corpus (≥20 designs)

All clones live under `/tmp/rtl-bench/` (shallow). Golden connectivity configs
for *runnable* slices are under `configs/`; full inventory is broader.

| # | Design | Path | Style / why hard |
|---|--------|------|------------------|
| 1 | **Ibex** | `ibex/` | RISC-V, prims, generate, packages |
| 2 | **SERV** | `serv/` | bit-serial core, generate-W, decode→alu |
| 3 | **PicoRV32** | `picorv32/` | generate MUL/DIV, PCPI, always @* |
| 4 | **DarkRISCV** | `darkriscv/` | ifdef threads, decode assigns |
| 5 | **CV32E40P** | `cv32e40p/` | PULP core, multi-stage, packages |
| 6 | **CV32E40X** | `cv32e40x/` | XIF, similar complexity |
| 7 | **SweRV EH1** | `swerv/` | WD commercial-class, large flists |
| 8 | **VeeR EL2** | `veer_el2/` | SweRV EL2 successor |
| 9 | **VexRiscv** | `VexRiscv/` | SpinalHDL-generated SV patterns |
| 10 | **ZipCPU** | `zipcpu/` | ZipSystem, formal-oriented |
| 11 | **wb2axip** | `wb2axip/` | skidbuffer, AXI bridges |
| 12 | **verilog-axi** | `verilog-axi/` | Alex Forencich AXI stack |
| 13 | **verilog-ethernet** | `verilog-ethernet/` | MAC/PCS, generate heavy |
| 14 | **pulp axi** | `pulp_axi/` | SV interfaces, xbar |
| 15 | **pulp common_cells** | `pulp_common_cells/` | CDC, spill, stream |
| 16 | **pulp reg_if** | `pulp_reg_if/` | register interface |
| 17 | **pulp riscv-dbg** | `pulp_riscv_dbg/` | DTM/DMI hierarchy |
| 18 | **pulp tech_cells** | `pulp_tech_cells/` | clock gates, pad cells |
| 19 | **pulp fpu** | `pulp_fpu/` | div/sqrt MVP |
| 20 | **BlackParrot** | `black_parrot/` | tiled manycore |
| 21 | **CVA6 / cvw** | `cvw/` (and cva6 if present) | application-class |
| 22 | **Gemmini** | `gemmini/` | systolic DNN accel |
| 23 | **LiteX / LiteEth** | `litex/`, `liteeth/` | generated SoC interconnect |
| 24 | **Microwatt** | `microwatt/` | PowerPC (VHDL-heavy; contrast) |
| 25 | **NVDLA** | `nvdla/` | large accelerator |
| 26 | **Sonata** | `sonata/` | OpenTitan-class system |
| 27 | **hard_patterns** | `examples/bench/hard_patterns/` | **synthetic stress**: generate-for, gen-if, interface/modport, case mux, bit-slice |

## Runnable golden configs (this directory)

| Config | Top | Hard patterns exercised |
|--------|-----|-------------------------|
| `hard_patterns` | `hard_top` | generate-for lanes, gen-if, case, xbar bits, interface pipe |
| `serv` | `serv_top` | generate, multi-instance, alu/decode wiring |
| `picorv32` | `picorv32` | generate MUL, assign ports, sub-instance PCPI |
| `skidbuffer` | `skidbuffer` | procedural reg path, bypass mux |
| `darkriscv` | `darkriscv` | ifdef, continuous decode |
| `axil_register_rd` | `axil_register_rd` | AXI-Lite channel register slice |
| `ibex` | `ibex_top` | full core cone (existing demo) |
| **`cv32e40p_fpu`** | `cv32e40p_top` **FPU=1** | **multi-dim `apu_operands[2:0][31:0]` fan-in from 3 ALU ops; cross-hier to FPU; mult `dot_char_op_a[3:0][8:0]`** |
| **`hard_zigzag`** | `zz_top` | **extreme: gen-if/for, case, multi-FF pipe, comb, param, ifdef, array fan-in, reconverge zigzag, 8-way branch fan-out + dead forest** |
| **`coi_ladder`** | `coi_top` | **COI-until (not bi-meet)**: FF-depth ladder for `coi_until.py --until FF:N` |

## Ground-truth method

1. **RTL review first** — write `expect: connected|disconnected` + `why` before running.
2. **Run** `python3 examples/bench/run_bench.py`.
3. **Debug** failures via `logs/<name>.log` and `work/<name>.hier_pyslang.json`.
4. **Improve** extractor / cone / slice policy; re-run until green.

Do **not** reverse-engineer golden from tool output alone.
