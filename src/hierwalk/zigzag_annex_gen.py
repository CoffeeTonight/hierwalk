"""Zigzag comprehensive annex: vuln_plan + parse_matrix coverage grafted onto torture top."""

from __future__ import annotations

import re
import textwrap
from typing import Any, Dict, List, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.vuln_plan import VULN_PLAN, VulnCaseSpec

ZZ_VULN_ANNEX_RTL = "zz_vuln_annex.v"
ZZ_MATRIX_ANNEX_RTL = "zz_matrix_annex.v"

# parse_matrix_soc.v hierarchy anchors (module zz_matrix_soc under torture top).
MATRIX_HIERARCHY_PATHS: Tuple[str, ...] = (
    "zz_matrix_soc",
    "zz_matrix_soc.u_A",
    "zz_matrix_soc.u_cpusystem_top",
    "zz_matrix_soc.u_wrap",
    "zz_matrix_soc.u_BCD",
    "zz_matrix_soc.u_t0",
    "zz_matrix_soc.u_t1",
    "zz_matrix_soc.u_macro",
    "zz_matrix_soc.gen_blk[0].u_BCD_gen",
    "zz_matrix_soc.gen_blk[1].u_BCD_gen",
    "zz_matrix_soc.ifg_blk.u_ifg",
    "zz_matrix_soc.outer[0].inner[0].u_nest",
    "zz_matrix_soc.outer[0].inner[1].u_nest",
    "zz_matrix_soc.outer[1].inner[0].u_nest",
    "zz_matrix_soc.outer[1].inner[1].u_nest",
    "zz_matrix_soc.port_ifndef_blk.u_DEF",
    "zz_matrix_soc.arr_blk.u_arr[0]",
    "zz_matrix_soc.arr_blk.u_arr[1]",
)

MATRIX_ABSENT_PATHS: Tuple[str, ...] = (
    "zz_matrix_soc.u_ghost",
    "zz_matrix_soc.u_fake_blk",
)

# path-walk hierarchy defers some generate-array indices (see parse_matrix gaps).
MATRIX_HIERARCHY_KNOWN_GAPS: frozenset[str] = frozenset(
    {
        "zz_matrix_soc.arr_blk.u_arr[1]",
    }
)

# A2a needs inline-define positive path; external-ifdef u_a2 is the A2b negative probe.
_VULN_ENDPOINT_OVERRIDES: Dict[str, Tuple[str, str]] = {
    "A2a": ("u_e1.src", "u_e1.dst"),
    "B1": ("vuln_src_bind", "u_b1.dst"),
}

# Spine zigzag already exercises C2; annex still carries u_c2 for RTL parity.
_VULN_SPINE_ALIAS_IDS = frozenset({"C2"})

# Structural absence: text-conn may also report disconnected (no bloom tolerance).
_VULN_STRUCTURAL_NEGATIVE_CASE_IDS = frozenset({"A1", "A2b", "B5", "G3"})


def vuln_annex_inst(torture_top: str) -> str:
    return f"{torture_top}.u_vuln"


def matrix_soc_inst(torture_top: str) -> str:
    return f"{torture_top}.u_matrix"


def _rename_vuln_modules(src: str) -> str:
    out = src.replace("module vuln_top", "module zz_vuln_annex")
    out = out.replace(
        "bind vuln_top v_b1_bind u_b1_tgt (.src(src_bind), .dst(u_b1.dst));",
        "",
    )
    out = re.sub(r"\b(interface|module) v_", r"\1 zz_v_", out)
    out = re.sub(r"\bv_([a-z][a-z0-9_]*)", r"zz_v_\1", out)
    return out


def vuln_annex_rtl(*, decoys: int = 4) -> str:
    from hierwalk import vuln_gen

    body = vuln_gen._scenario_modules(decoys) + "\n\n" + vuln_gen._top_module(decoys)
    return _rename_vuln_modules(body).strip()


def matrix_annex_rtl() -> str:
    return textwrap.dedent(
        """
        `define ZZ_MX_CELL LEAF

        module zz_matrix_soc;
          parameter N_ARR = 1;
          localparam PASS_THRU = 1;
          wire clk, w_aa, w_bb, w, w_QW;

          // --- axis: line-comment directive trap + nested ifndef/elsif/else ---
          // `endif must not close early
          `ifndef ASD
          A u_A
          (
          .aa (w_aa));
          `elsif USE_B
          B u_B (.bb(w_bb));
          `else
          STUB u_stub ();
          `endif//ASD

          // --- axis: block-comment fake ifdef (must not affect stack) ---
          /*
          `ifdef FAKE_BLK
          FAKE u_fake_blk ();
          `endif
          */

          // --- axis: endif//LABEL same-line next instance ---
          `ifndef NO_CPU
          WRAP u_wrap (.x(w));
          `endif//NO_CPU CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));

          // --- axis: flat param override #(nested expr) ---
          BCD #(.a(2),.b(2-1)) u_BCD (.clk(clk));

          // --- axis: comma-separated + param on first only ---
          TINY #(.w(8)) u_t0 (.d(w)), u_t1 (.d(w));

          // --- axis: macro cell under ifndef ---
          `ifndef NO_MACRO
          `ZZ_MX_CELL u_macro ();
          `endif

          genvar gi;
          generate
            for (gi = 0; gi < 2; gi++) begin : gen_blk
        `ifdef GEN_LEAF
              LEAF #(.idx(gi)) u_leaf (.clk(clk));
        `elsif GEN_ALT
              ALT u_alt ();
        `else
              BCD #(.a(gi),.b(2-1)) u_BCD_gen (.clk(clk));
        `endif
            end

            if (PASS_THRU) begin : ifg_blk
              IFG_CHILD #(.k(3)) u_ifg (.clk(clk));
            end

            for (gi = 0; gi < 2; gi++) begin : outer
              for (genvar gj = 0; gj < 2; gj++) begin : inner
                NEST #(.oi(gi),.ij(gj)) u_nest (.clk(clk));
              end
            end

            if (1) begin : port_ifndef_blk
        `ifndef ABC
              DEF u_DEF (
        `ifndef PORT_X
                .QW (w_QW),
        `endif
                .CLK (clk)
              );
        `endif
            end

            if (1) begin : arr_blk
              ARR #(.n(N_ARR)) u_arr [0:N_ARR] (.clk(clk));
            end
          endgenerate
        endmodule

        module A; endmodule
        module B; endmodule
        module STUB; endmodule
        module WRAP; endmodule
        module CPUSYSTEM_TOP; endmodule
        module BCD; endmodule
        module TINY; endmodule
        module LEAF; endmodule
        module ALT; endmodule
        module IFG_CHILD; endmodule
        module NEST; endmodule
        module DEF(input CLK, output QW); endmodule
        module ARR; endmodule

        bind zz_matrix_soc ghost u_ghost ();
        module ghost; endmodule
        """
    ).strip()


def torture_top_annex_insts(torture_top: str) -> str:
    return textwrap.dedent(
        f"""
          wire vuln_src_bind;
          assign vuln_src_bind = strb_data[0];

          zz_vuln_annex u_vuln (
            .clk(clk),
            .rst_n(rst_n),
            .src_bind(vuln_src_bind)
          );
          zz_matrix_soc u_matrix ();

          bind {torture_top} zz_v_b1_bind u_vuln_b1_tgt (
            .src(vuln_src_bind),
            .dst(u_vuln.u_b1.dst)
          );
        """
    ).strip()


def _vuln_check_id(case_id: str) -> str:
    return f"zz_vuln_{case_id.lower()}"


def _remap_vuln_endpoint_for_case(spec: VulnCaseSpec, side: str, *, torture_top: str) -> str:
    if spec.case_id in _VULN_ENDPOINT_OVERRIDES:
        override_a, override_b = _VULN_ENDPOINT_OVERRIDES[spec.case_id]
        tail = override_a if side == "a" else override_b
        if tail == "vuln_src_bind":
            return f"{torture_top}.vuln_src_bind"
        return f"{torture_top}.u_vuln.{tail}"
    raw = spec.endpoint_a if side == "a" else spec.endpoint_b
    if raw == "vuln_top.src_bind":
        return f"{torture_top}.vuln_src_bind"
    if raw.startswith("vuln_top."):
        return f"{torture_top}.u_vuln.{raw[len('vuln_top.'):]}"
    return raw


def vuln_annex_checks(*, torture_top: str) -> Tuple[ConnectivityCheck, ...]:
    checks: List[ConnectivityCheck] = []
    for spec in VULN_PLAN:
        ep_a = _remap_vuln_endpoint_for_case(spec, "a", torture_top=torture_top)
        ep_b = _remap_vuln_endpoint_for_case(spec, "b", torture_top=torture_top)
        checks.append(
            ConnectivityCheck(
                ep_a,
                ep_b,
                check_id=_vuln_check_id(spec.case_id),
            )
        )
    return tuple(checks)


def vuln_annex_negative_ids() -> frozenset[str]:
    return frozenset(
        _vuln_check_id(spec.case_id)
        for spec in VULN_PLAN
        if not spec.expected_default
    )


def vuln_logical_only_negative_ids() -> frozenset[str]:
    """``expect_connected: false`` is enforced on logical-conn only.

    Text-conn may bloom-pass (name appears on masked/opaque RHS); that is
    correct coarse behavior — not a text-conn defect.
    """
    return frozenset(
        _vuln_check_id(spec.case_id)
        for spec in VULN_PLAN
        if not spec.expected_default
        and spec.case_id not in _VULN_STRUCTURAL_NEGATIVE_CASE_IDS
    )


def vuln_structural_negative_ids() -> frozenset[str]:
    """Disconnected in both phases when path-walk text-conn is working."""
    return frozenset(
        _vuln_check_id(spec.case_id)
        for spec in VULN_PLAN
        if spec.case_id in _VULN_STRUCTURAL_NEGATIVE_CASE_IDS
    )


def vuln_annex_verdict_skip_ids() -> frozenset[str]:
    return frozenset({_vuln_check_id(cid) for cid in _VULN_SPINE_ALIAS_IDS})


def matrix_hierarchy_check_id() -> str:
    return "zz_matrix_hier_batch"


def matrix_hierarchy_specs(*, torture_top: str) -> Tuple[str, ...]:
    paths: List[str] = [f"{torture_top}.u_matrix"]
    for rel in MATRIX_HIERARCHY_PATHS:
        if rel == "zz_matrix_soc" or rel in MATRIX_HIERARCHY_KNOWN_GAPS:
            continue
        paths.append(f"{torture_top}.u_matrix.{rel[len('zz_matrix_soc.'):]}")
    return tuple(paths)


def matrix_hierarchy_suite_spec(
    *,
    torture_top: str,
    matrix_rtl: str,
) -> Dict[str, Any]:
    expect = [
        {
            "side": "a",
            "path": f"{torture_top}.u_matrix.{suffix}",
            "module": module,
            "rtl_file": matrix_rtl,
        }
        for suffix, module in (
            ("u_A", "A"),
            ("u_BCD", "BCD"),
            ("gen_blk[0].u_BCD_gen", "BCD"),
            ("ifg_blk.u_ifg", "IFG_CHILD"),
            ("outer[0].inner[0].u_nest", "NEST"),
            ("port_ifndef_blk.u_DEF", "DEF"),
            ("arr_blk.u_arr[0]", "ARR"),
        )
    ]
    return {
        "id": matrix_hierarchy_check_id(),
        "a": f"{torture_top}.u_matrix.u_A",
        "b": f"{torture_top}.clk",
        "expect_hierarchy": expect,
    }


def vuln_annex_suite_specs(
    *,
    torture_top: str,
    spec_from_check,
) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    for chk in vuln_annex_checks(torture_top=torture_top):
        plan = VULN_PLAN_BY_CHECK_ID[chk.check_id]
        specs.append(
            spec_from_check(chk, expect_connected=plan.expected_default)
        )
    return specs


def vuln_mapping_rows() -> List[Dict[str, str]]:
    """Rows for JOBS.md comprehensive coverage table."""
    rows: List[Dict[str, str]] = []
    for spec in VULN_PLAN:
        cid = _vuln_check_id(spec.case_id)
        note = ""
        if spec.case_id == "A2a":
            note = "u_e1 inline-define (no global VULN_USE_PATH)"
        elif spec.case_id == "C2":
            note = "annex u_c2 + spine zigzag hub"
        elif spec.case_id == "B1":
            note = "bind at torture top → u_vuln.u_b1.dst"
        rows.append(
            {
                "source": f"vuln_plan {spec.case_id}",
                "group": spec.group.value,
                "zigzag_check": cid,
                "expect": "connected" if spec.expected_default else "disconnected",
                "note": note or spec.description,
            }
        )
    for path in MATRIX_HIERARCHY_PATHS:
        rows.append(
            {
                "source": "parse_matrix",
                "group": "hierarchy_axis",
                "zigzag_check": matrix_hierarchy_check_id(),
                "expect": "hierarchy_hit",
                "note": path,
            }
        )
    return rows


VULN_PLAN_BY_CHECK_ID: Dict[str, VulnCaseSpec] = {
    _vuln_check_id(spec.case_id): spec for spec in VULN_PLAN
}