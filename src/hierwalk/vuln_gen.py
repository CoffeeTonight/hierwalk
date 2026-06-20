"""Randomized RTL that exercises known connectivity vulnerability classes."""

from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from hierwalk.vuln_plan import VULN_PLAN, VulnCaseSpec


@dataclass(frozen=True)
class VulnDesign:
    verilog: str
    top: str
    seed: int
    decoy_count: int
    defines: Dict[str, str]
    cases: Tuple[VulnCaseSpec, ...]


def _decoy_line(i: int) -> str:
    return (
        f"  v_decoy u_dec_{i} (.clk(1'b0), .rst_n(1'b1), .noise());"
    )


def _scenario_modules(decoys: int) -> str:
    decoy_mod = textwrap.dedent(
        """
        module v_decoy(input clk, rst_n, output noise);
          assign noise = clk ^ rst_n;
        endmodule
        """
    ).strip()
    decoy_body = "\n".join(_decoy_line(i) for i in range(decoys))

    return textwrap.dedent(
        f"""
        `define VULN_TIE(a,b) assign a = b;

        {decoy_mod}

        module v_over_if(input src, output dst);
          wire link;
          assign link = src;
          generate
            if (MYSTERY) begin : g
              assign dst = link;
            end else begin
              assign dst = 1'b0;
            end
          endgenerate
        endmodule

        module v_ifdef_path(input src, output dst);
          wire link;
          `ifdef VULN_USE_PATH
            assign link = src;
          `else
            assign link = 1'b0;
          `endif
          assign dst = link;
        endmodule

        module v_case_union(input clk, rst_n, src, output dst);
          logic link;
          logic [1:0] sel;
          assign sel = 2'b01;
          always_ff @(posedge clk) begin
            if (!rst_n)
              link <= 1'b0;
            else begin
              case (sel)
                2'b00: link <= src;
                default: link <= 1'b0;
              endcase
            end
          end
          assign dst = link;
        endmodule

        module v_array_bus(input src, output dst);
          logic [1:0] hop;
          assign hop[0] = src;
          assign hop[2] = hop[0];
          assign dst = hop[1];
        endmodule

        module v_ff_chain(input clk, rst_n, src, output dst);
          logic q;
          always_ff @(posedge clk) begin
            if (!rst_n)
              q <= 1'b0;
            else
              q <= src;
          end
          assign dst = q;
        endmodule

        module v_b1_core(input src, output dst);
          assign dst = src;
        endmodule

        module v_b1_bind(input src, output dst);
          assign dst = src;
        endmodule

        module v_prim_and(input src, output dst);
          wire a, b, c;
          assign a = src;
          assign b = src;
          and u_g (c, a, b);
          assign dst = c;
        endmodule

        module v_child_hidden(input p, output hidden);
          assign hidden = p;
        endmodule

        module v_hier_gap(input src, output dst);
          wire bridge;
          v_child_hidden u_c (.p(src), .hidden(bridge));
          assign dst = bridge;
        endmodule

        module v_ifdef_inline(input src, output dst);
          `define VULN_INLINE 1
          wire link;
          `ifdef VULN_INLINE
            assign link = src;
          `else
            assign link = 1'b0;
          `endif
          assign dst = link;
        endmodule

        module v_blackbox(input src, output dst);
        endmodule

        module v_unresolved_if(input src, output dst);
          wire link;
          assign link = src;
          generate
            if (UNRESOLVED_PARAM) begin
              assign dst = link;
            end
          endgenerate
        endmodule

        module v_macro_path(input src, output dst);
          `define VULN_TIE_DST_SRC assign dst = src;
          `VULN_TIE_DST_SRC
        endmodule

        module v_function_gap(input src, output dst);
          assign dst = src & 1'b0;
        endmodule

        module v_zig_pass(input src, output dst);
          wire link;
          assign link = src;
          assign dst = link;
        endmodule

        module v_scale_path(input src, output dst);
          wire link;
          assign link = src;
          assign dst = link;
        {decoy_body}
        endmodule

        module v_zigzag(input src, output dst);
          wire xfer;
          v_zig_pass u_ping (.src(src), .dst(xfer));
          v_zig_pass u_pong (.src(xfer), .dst(dst));
        endmodule

        module v_hier_ok(input src, output dst);
          v_child_hidden u_c (.p(src), .hidden());
          assign dst = u_c.hidden;
        endmodule

        module v_case_labels(input src, output dst);
          logic link;
          logic b00;
          logic [1:0] sel;
          assign sel = 2'b01;
          assign b00 = 1'b0;
          always_comb begin
            case (sel)
              2'b00: link = src;
              default: link = 1'b0;
            endcase
          end
          assign dst = link;
        endmodule

        module v_concat_bit(input src, output dst);
          assign dst = {{src, 1'b0}}[0];
        endmodule

        module v_casez_wildcard(input src, output dst);
          logic link;
          logic [1:0] sel;
          assign sel = 2'b01;
          always_comb begin
            casez (sel)
              2'b?0: link = src;
              default: link = 1'b0;
            endcase
          end
          assign dst = link;
        endmodule

        module v_multi_driver(input src, output dst);
          wire link;
          assign link = src;
          assign link = 1'b0;
          assign dst = link;
        endmodule

        module v_multi_in_blackbox(input a, b, output y);
        endmodule

        interface v_g4_if;
          logic sig;
        endinterface

        module v_intf_hier(input src, output dst);
          v_g4_if u_if();
          assign u_if.sig = src;
          assign dst = u_if.sig;
        endmodule

        module v_one_tieoff(input src, output dst);
          wire link;
          assign link = src;
          assign link = 1'b1;
          assign dst = link;
        endmodule

        module v_param_tieoff(input src, output dst);
          localparam TIE = 1'b0;
          wire link;
          assign link = src;
          assign link = TIE;
          assign dst = link;
        endmodule

        module v_self_xor(input src, output dst);
          assign dst = src ^ src;
        endmodule

        module v_supply_mask(input src, output dst);
          supply0 gnd;
          assign dst = src & gnd;
        endmodule

        module v_comb_last_wins(input src, output dst);
          logic link;
          always_comb begin
            link = src;
            link = 1'b0;
          end
          assign dst = link;
        endmodule

        module v_casez_opaque(input src, output dst);
          logic link;
          logic [1:0] sel;
          always_comb begin
            casez (sel)
              2'b?0: link = src;
              default: link = 1'b0;
            endcase
          end
          assign dst = link;
        endmodule

        module v_ternary_opaque(input src, output dst);
          assign dst = sel ? src : 1'b0;
        endmodule

        module v_if_opaque(input src, output dst);
          logic link;
          always_comb if (opaque) link = src; else link = 1'b0;
          assign dst = link;
        endmodule

        module v_for_chain(input src, output dst);
          wire [3:0] chain;
          assign chain[0] = src;
          generate
            for (genvar gi = 1; gi < 4; gi = gi + 1) begin
              assign chain[gi] = chain[gi - 1];
            end
          endgenerate
          assign dst = chain[3];
        endmodule

        module v_concat_high(input src, output dst);
          assign dst = {{src, 1'b0}}[1];
        endmodule

        module v_ifdef_oneline(input src, output dst);
          wire link;
          `ifdef GHOST assign link=src; `else assign link=1'b0; `endif
          assign dst = link;
        endmodule

        module v_ff_if_noelse(input clk, input src, output dst);
          logic q;
          always_ff @(posedge clk) if (opaque) q <= src;
          assign dst = q;
        endmodule

        module v_param_idx0(input src, output dst);
          parameter P = 0;
          assign dst = src[P];
        endmodule

        module v_concat_oob(input src, output dst);
          assign dst = {{src, src}}[2];
        endmodule

        module v_add_zero(input src, output dst);
          assign dst = src + 1'b0;
        endmodule

        module v_sub_self(input src, output dst);
          assign dst = src - src;
        endmodule

        module v_bits_fn(input src, output dst);
          assign dst = $bits(src);
        endmodule

        module v_for_if_chain(input src, output dst);
          wire [3:0] chain;
          assign chain[0] = src;
          generate
            for (genvar gi = 0; gi < 4; gi = gi + 1)
              if (gi > 0) assign chain[gi] = chain[gi - 1];
          endgenerate
          assign dst = chain[3];
        endmodule

        module v_for_chain_nogenvar(input src, output dst);
          wire [3:0] chain;
          assign chain[0] = src;
          generate
            for (gi = 1; gi < 4; gi = gi + 1) assign chain[gi] = chain[gi - 1];
          endgenerate
          assign dst = chain[3];
        endmodule

        module bb_j25(input p, output q); endmodule

        module v_bb_passthrough(input src, output dst);
          wire mid;
          bb_j25 u (.p(src), .q(mid));
          assign dst = mid;
        endmodule

        module v_reduce_or(input src, output dst);
          assign dst = |src;
        endmodule

        module v_replicate_bit(input src, output dst);
          assign dst = {{2{{src}}}}[1];
        endmodule
        """
    ).strip()


def _top_module(decoys: int) -> str:
    decoys_in_top = "\n".join(_decoy_line(1000 + i) for i in range(max(0, decoys // 4)))
    return textwrap.dedent(
        f"""
        module vuln_top (
          input  logic clk,
          input  logic rst_n,
          input  logic src_bind,
          output logic dummy_out
        );
          wire n_a1, n_a2, n_a3, n_a4, n_a5;
          wire n_b2, n_b3, n_b4, n_b5, n_b6, n_b7, n_c1, n_c2, n_d1, n_d2, n_d3, n_e1;
          wire n_g1, n_g2, n_g3, n_g4;
          wire n_h2, n_h3, n_h4, n_h5, n_h6, n_h7, n_h8, n_h9, n_h10, n_i11;
          wire n_j35, n_j9, n_j20, n_j25, n_k13, n_l2, n_m1, n_m2, n_m4, n_m7, n_m9, n_n9;
          assign dummy_out = 1'b0;

          v_over_if     u_a1 (.src(n_a1), .dst());
          v_ifdef_path  u_a2 (.src(n_a2), .dst());
          v_case_union  u_a3 (.clk(clk), .rst_n(rst_n), .src(n_a3), .dst());
          v_array_bus   u_a4 (.src(n_a4), .dst());
          v_ff_chain    u_a5 (.clk(clk), .rst_n(rst_n), .src(n_a5), .dst());
          v_b1_core     u_b1 (.src(1'b0), .dst());
          v_prim_and    u_b2 (.src(n_b2), .dst());
          v_hier_gap    u_b3 (.src(n_b3), .dst());
          v_blackbox    u_b4 (.src(n_b4), .dst());
          v_unresolved_if u_b5 (.src(n_b5), .dst());
          v_macro_path  u_b6 (.src(n_b6), .dst());
          v_function_gap u_b7 (.src(n_b7), .dst());
          v_scale_path  u_c1 (.src(n_c1), .dst());
          v_zigzag      u_c2 (.src(n_c2), .dst());
          v_hier_ok     u_d1 (.src(n_d1), .dst());
          v_case_labels u_d2 (.src(n_d2), .dst());
          v_concat_bit  u_d3 (.src(n_d3), .dst());
          v_ifdef_inline u_e1 (.src(n_e1), .dst());
          v_casez_wildcard u_g1 (.src(n_g1), .dst());
          v_multi_driver  u_g2 (.src(n_g2), .dst());
          v_multi_in_blackbox u_g3 (.a(n_g3), .b(1'b0), .y());
          v_intf_hier     u_g4 (.src(n_g4), .dst());
          v_one_tieoff    u_h2 (.src(n_h2), .dst());
          v_param_tieoff  u_h3 (.src(n_h3), .dst());
          v_self_xor      u_h4 (.src(n_h4), .dst());
          v_supply_mask   u_h5 (.src(n_h5), .dst());
          v_comb_last_wins u_h6 (.src(n_h6), .dst());
          v_casez_opaque  u_h7 (.src(n_h7), .dst());
          v_ternary_opaque u_h8 (.src(n_h8), .dst());
          v_if_opaque     u_h9 (.src(n_h9), .dst());
          v_for_chain     u_h10 (.src(n_h10), .dst());
          v_concat_high   u_i11 (.src(n_i11), .dst());
          v_ifdef_oneline u_j35 (.src(n_j35), .dst());
          v_ff_if_noelse  u_j9  (.clk(clk), .src(n_j9), .dst());
          v_for_chain_nogenvar u_j20 (.src(n_j20), .dst());
          v_bb_passthrough u_j25 (.src(n_j25), .dst());
          v_param_idx0    u_k13 (.src(n_k13), .dst());
          v_concat_oob    u_l2  (.src(n_l2), .dst());
          v_add_zero      u_m1  (.src(n_m1), .dst());
          v_sub_self      u_m2  (.src(n_m2), .dst());
          v_reduce_or     u_m4  (.src(n_m4), .dst());
          v_replicate_bit u_m7  (.src(n_m7), .dst());
          v_bits_fn       u_m9  (.src(n_m9), .dst());
          v_for_if_chain  u_n9  (.src(n_n9), .dst());
        {decoys_in_top}
        endmodule

        bind vuln_top v_b1_bind u_b1_tgt (.src(src_bind), .dst(u_b1.dst));
        """
    ).strip()


def generate_vuln_design(*, seed: Optional[int] = None) -> VulnDesign:
    rng = random.Random(seed)
    seed_val = seed if seed is not None else rng.randint(0, 2**31 - 1)
    rng = random.Random(seed_val)
    decoys = 8 + rng.randint(0, 12)
    verilog = _scenario_modules(decoys) + "\n\n" + _top_module(decoys) + "\n"
    return VulnDesign(
        verilog=verilog,
        top="vuln_top",
        seed=seed_val,
        decoy_count=decoys,
        defines={"VULN_USE_PATH": "1"},
        cases=VULN_PLAN,
    )


@dataclass
class VulnCaseResult:
    case_id: str
    group: str
    expected_default: bool
    expected_strict: bool
    actual_default: bool
    actual_strict: bool
    ok_default: bool
    ok_strict: bool
    note_default: str
    note_strict: str


@dataclass
class VulnTrialResult:
    seed: int
    decoy_count: int
    case_results: List[VulnCaseResult]
    default_pass: int
    strict_pass: int
    total: int
    gen_sec: float
    verify_sec: float
    surprises_default: List[str] = field(default_factory=list)
    surprises_strict: List[str] = field(default_factory=list)


def _case_endpoints(spec: VulnCaseSpec) -> Tuple[str, str]:
    return spec.endpoint_a, spec.endpoint_b


def run_vuln_trial(*, seed: Optional[int] = None) -> Tuple[VulnDesign, VulnTrialResult]:
    import time
    import tempfile
    from pathlib import Path

    from hierwalk.connectivity import check_connectivity
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    t0 = time.perf_counter()
    design = generate_vuln_design(seed=seed)
    t_gen = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="hierwalk_vuln_") as tmp:
        path = Path(tmp) / f"vuln_{design.seed}.v"
        path.write_text(design.verilog, encoding="utf-8")
        index = DesignIndex.build({str(path): design.verilog})
        _root, rows = elaborate(index, design.top)

        results: List[VulnCaseResult] = []
        surprises_d: List[str] = []
        surprises_s: List[str] = []

        for spec in design.cases:
            ep_a, ep_b = _case_endpoints(spec)
            case_defines = (
                dict(spec.defines)
                if spec.defines
                else {}
            )

            r_def = check_connectivity(
                ep_a,
                ep_b,
                rows=rows,
                index=index,
                top=design.top,
                defines=case_defines,
                strict_generate=False,
                over_approximate_if=False,
            )
            r_str = check_connectivity(
                ep_a,
                ep_b,
                rows=rows,
                index=index,
                top=design.top,
                defines=case_defines,
                strict_generate=True,
                ff_barrier=True,
            )
            ok_d = r_def.connected == spec.expected_default
            ok_s = r_str.connected == spec.expected_strict
            if not ok_d:
                surprises_d.append(
                    f"{spec.case_id}: want={spec.expected_default} got={r_def.connected}"
                )
            if not ok_s:
                surprises_s.append(
                    f"{spec.case_id}: want={spec.expected_strict} got={r_str.connected}"
                )
            results.append(
                VulnCaseResult(
                    case_id=spec.case_id,
                    group=spec.group.value,
                    expected_default=spec.expected_default,
                    expected_strict=spec.expected_strict,
                    actual_default=r_def.connected,
                    actual_strict=r_str.connected,
                    ok_default=ok_d,
                    ok_strict=ok_s,
                    note_default=r_def.note or "",
                    note_strict=r_str.note or "",
                )
            )

        t_verify = time.perf_counter()
        trial = VulnTrialResult(
            seed=design.seed,
            decoy_count=design.decoy_count,
            case_results=results,
            default_pass=sum(1 for r in results if r.ok_default),
            strict_pass=sum(1 for r in results if r.ok_strict),
            total=len(results),
            gen_sec=t_gen - t0,
            verify_sec=t_verify - t_gen,
            surprises_default=surprises_d,
            surprises_strict=surprises_s,
        )
    return design, trial


def run_vuln_batch(trials: int = 10, *, base_seed: int = 424242) -> List[VulnTrialResult]:
    out: List[VulnTrialResult] = []
    for i in range(trials):
        _, trial = run_vuln_trial(seed=base_seed + i * 7919)
        out.append(trial)
    return out


def format_vuln_report(
    trials: Sequence[VulnTrialResult],
    *,
    show_plan: bool = False,
) -> str:
    from hierwalk.vuln_plan import remediation_summary

    lines: List[str] = []
    if show_plan:
        lines.extend(remediation_summary())
        lines.append("")

    header = (
        "trial  seed        decoy  def_ok  str_ok  total  "
        "gen_ms  verify_ms  surprises(d/s)"
    )
    lines.append(header)
    for i, t in enumerate(trials):
        lines.append(
            f"{i:5d}  {t.seed:10d}  {t.decoy_count:5d}  "
            f"{t.default_pass:3d}/{t.total:3d}  {t.strict_pass:3d}/{t.total:3d}  "
            f"{t.total:5d}  {t.gen_sec * 1e3:6.1f}  {t.verify_sec * 1e3:8.1f}  "
            f"{len(t.surprises_default):3d}/{len(t.surprises_strict):3d}"
        )

    if trials:
        agg: Dict[str, List[VulnCaseResult]] = {}
        for t in trials:
            for cr in t.case_results:
                agg.setdefault(cr.case_id, []).append(cr)
        lines.append("")
        lines.append("per-case pass rate (default / strict) over batch:")
        for case_id in [c.case_id for c in VULN_PLAN]:
            bucket = agg.get(case_id, [])
            if not bucket:
                continue
            d_ok = sum(1 for x in bucket if x.ok_default)
            s_ok = sum(1 for x in bucket if x.ok_strict)
            n = len(bucket)
            lines.append(
                f"  {case_id:4s}  default {d_ok}/{n}  strict {s_ok}/{n}  "
                f"[{bucket[0].group}]"
            )

        lines.append("")
        all_surprises_d = sorted({s for t in trials for s in t.surprises_default})
        all_surprises_s = sorted({s for t in trials for s in t.surprises_strict})
        if all_surprises_d:
            lines.append("default-mode surprises (any trial):")
            for s in all_surprises_d:
                lines.append(f"  ! {s}")
        if all_surprises_s:
            lines.append("strict-mode surprises (any trial):")
            for s in all_surprises_s:
                lines.append(f"  ! {s}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Vulnerability regression benchmark")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--plan", action="store_true", help="print remediation plan")
    args = parser.parse_args()
    if args.seed is not None:
        _, trial = run_vuln_trial(seed=args.seed)
        print(format_vuln_report([trial], show_plan=args.plan))
    else:
        print(
            format_vuln_report(
                run_vuln_batch(args.trials),
                show_plan=args.plan,
            )
        )