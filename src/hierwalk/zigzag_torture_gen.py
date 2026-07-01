"""
Maximum-complexity zigzag torture RTL for path-walk + connectivity stress.

Topology: ``zz_torture_top.u_zigzag`` with sibling deep arm (d1..d5) and shallow arm
(r1..r4).  Data ping-pongs deep → bridge → shallow → branch → deep; lateral
``mid_tap`` / ``shallow_tap`` bridges tie the arms.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.shared.expand import build_expand_meta
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.suite_conn_policy import CONN_VERDICT_SKIP_IDS

TOP = "zz_torture_top"
HUB = f"{TOP}.u_zigzag"
DEEP_ARM = f"{HUB}.u_deep"
SHALLOW_ARM = f"{HUB}.u_shallow"
DEEP_D5 = f"{DEEP_ARM}.d1.d2.d3.d4.d5"
DEEP_D4 = f"{DEEP_ARM}.d1.d2.d3.d4"
DEEP_D3 = f"{DEEP_ARM}.d1.d2.d3"
DEEP_D2 = f"{DEEP_ARM}.d1.d2"
SHALLOW_R4 = f"{SHALLOW_ARM}.r1.r2.r3.r4"
D1_SHADOW = f"{DEEP_ARM}.d1.d1_shadow"
D1_NEXT_DECOY = f"{DEEP_ARM}.d1.u_next_decoy"
R3_ALT = f"{SHALLOW_ARM}.r1.r2.r3.r3_alt"
COLLISION = f"{TOP}.u_collision"
BLACKBOX = f"{HUB}.u_bb"
ZZ_COMMON_RTL = "zz_common.v"
ZZ_FAKE_DEEP_RTL = "zz_fake_deep.v"
DW_VENDOR_RTL = "DW_zz_vendor.v"
DW_VENDOR_CELL = "DW_zz_vendor_cell"
DW_VENDOR_INST = f"{TOP}.u_dw_vendor"
CONE_FANOUT = f"{DEEP_ARM}.mid_tap[1][2]"
CONE_FANIN = f"{SHALLOW_R4}.leaf_in"
CONE_MERGE_TAP = f"{DEEP_D4}.merge_tap"
CONE_EXPR_XOR = f"{DEEP_D2}.u_bridge_expr.din[1][2]"
DEEP_D1 = f"{DEEP_ARM}.d1"
SHALLOW_R2 = f"{SHALLOW_ARM}.r1.r2"

# Design check_id -> suite JSON id (same endpoints, different names).
DESIGN_SUITE_CHECK_ALIASES: Dict[str, str] = {
    "zz_deep_chain_hop": "zz_inst_hop",
    "zz_cube3d_slice": "zz_wire_ref",
    "zz_shallow_tap": "zz_port_ref",
    "zz_list_endpoints": "zz_list_expand",
}

SUITE_CONN_NEGATIVE_IDS = frozenset(
    {
        "zz_fanin_merge_decoy",
        "zz_ifdef_inactive",
        "zz_missing_hierarchy",
        "zz_intentional_fail",
        "zz_fake_deep_not_on_spine",
        "zz_multi_g3_empty",
    }
)

SUITE_CONN_VERDICT_SKIP_IDS = CONN_VERDICT_SKIP_IDS

DEEP_DEPTH = 5
SHALLOW_DEPTH = 4
STRB_MAX = 8

DEFINES: Dict[str, str] = {
    "ZZ_TORTURE": "1",
    "ZZ_USE_CASE": "1",
    "ZZ_REAL_GEN": "1",
    "ZZ_REAL_IFDEF": "1",
}


@dataclass(frozen=True)
class ZigzagTortureDesign:
    files: Dict[str, str]
    top: str
    deep_path: str
    shallow_path: str
    checks: Tuple[ConnectivityCheck, ...]
    hierarchy_specs: Tuple[str, ...]


def _defines_preamble() -> str:
    return textwrap.dedent(
        """
        `ifndef ZZ_TORTURE
        `define ZZ_TORTURE 1
        `endif
        `ifndef ZZ_USE_CASE
        `define ZZ_USE_CASE 1
        `endif
        `ifndef ZZ_REAL_GEN
        `define ZZ_REAL_GEN 1
        `endif
        `ifndef ZZ_REAL_IFDEF
        `define ZZ_REAL_IFDEF 1
        `endif
        """
    ).strip()


def _decoy_leaf() -> str:
    return textwrap.dedent(
        """
        module zz_decoy_leaf (
          input logic clk,
          input logic rst_n
        );
          wire noise;
          assign noise = clk;
        endmodule
        """
    ).strip()


def _decoy_module() -> str:
    return textwrap.dedent(
        """
        module zz_decoy #(
          parameter int D = 0
        ) (
          input  logic clk,
          input  logic rst_n,
          output logic noise
        );
          assign noise = clk ^ rst_n ^ D;
        endmodule
        """
    ).strip()


def _ifndef_guard_ping_module() -> str:
    """Include-guard module: `` `ifndef `` must precede in-body `` `define ``."""
    return textwrap.dedent(
        """
        `ifndef ZZ_IFNDEF_PING_BODY_
        `define ZZ_IFNDEF_PING_BODY_
        module zz_ifndef_ping (
          input  logic [2:0][3:0] din,
          output logic [2:0][3:0] dout
        );
          assign dout = din;
        endmodule
        `endif
        """
    ).strip()


def _bridge_modules() -> str:
    return textwrap.dedent(
        """
        module zz_bridge_ping (
          input  logic [2:0][3:0] din,
          output logic [2:0][3:0] dout
        );
          assign dout = din;
        endmodule

        module zz_bridge_pong (
          input  logic [2:0][3:0] din,
          output logic [2:0][3:0] dout
        );
          assign dout = {3{4{din[1][2]}}}[11:0];
        endmodule

        module zz_y_fork (
          input  logic [2:0][3:0] din,
          output logic [2:0][3:0] main_out,
          output logic [2:0][3:0] decoy_out
        );
          assign main_out = din;
          assign decoy_out = 12'b0;
        endmodule

        module zz_y_merge (
          input  logic [2:0][3:0] main_in,
          input  logic [2:0][3:0] side_in,
          output logic [2:0][3:0] dout
        );
          // side_in XOR cancels: dout == main_in (merge_dummy off shallow spine)
          assign dout = main_in ^ side_in ^ side_in;
        endmodule

        module zz_bridge_narrow (
          input  logic [1:0] din,
          output logic [1:0] dout
        );
          assign dout = din;
        endmodule

        module zz_empty_multi (
          input  logic a,
          input  logic b,
          output logic y
        );
        endmodule
        """
    ).strip()


def _blackbox_module() -> str:
    return textwrap.dedent(
        """
        module zz_blackbox (
          input  logic [2:0][3:0] din,
          output logic [2:0][3:0] dout
        );
          assign dout = din;
        endmodule
        """
    ).strip()


def _collision_module() -> str:
    return textwrap.dedent(
        """
        module zz_leaf_w_e;
          wire stub;
          assign stub = 1'b0;
        endmodule

        module zz_collision_d (
          output logic w_e
        );
          zz_leaf_w_e w_e ();
          assign w_e = 1'b0;
        endmodule
        """
    ).strip()


def _fake_deep_decoy_file() -> str:
    """Looks like the real deep arm but is never instantiated."""
    return textwrap.dedent(
        """
        // Decoy corpus: path-shaped names with no tie-in to zz_torture_top.
        module zz_fake_deep_arm (
          input  logic clk,
          input  logic [2:0][3:0] a,
          output logic [2:0][3:0] mid_tap
        );
          zz_fake_d1 d1_shadow (.clk(clk), .a(a), .mid_tap(mid_tap));
          zz_fake_d1 u_next_decoy (.clk(clk), .a(a), .mid_tap());
        endmodule

        module zz_fake_d1 (
          input  logic clk,
          input  logic [2:0][3:0] a,
          output logic [2:0][3:0] mid_tap
        );
          assign mid_tap = 12'b0;
        endmodule
        """
    ).strip()


def _deep_level_body(lvl: int) -> str:
    """Generate zz_deep_d{lvl} with rotating complexity."""
    mod = f"zz_deep_d{lvl}"
    nxt = lvl + 1
    child_mod = f"zz_deep_d{nxt}"
    leaf = lvl == DEEP_DEPTH
    port_style = lvl % 4

    if port_style == 0:
        header = textwrap.dedent(
            f"""
            module {mod} #(
              parameter int STRB_MAX = {STRB_MAX},
              parameter int LVL = {lvl}
            )(
              input  logic              clk,
              input  logic              rst_n,
              input  logic [2:0][3:0]   chain_in,
              output logic [2:0][3:0]   chain_out,
              input  logic [STRB_MAX-1:0] strb_in,
              output logic [2:0][3:0]   mid_tap,
              input  logic [2:0][3:0]   shallow_return,
              output logic              leaf_out,
              input  logic              leaf_in
            );
            """
        ).strip()
        body_decls = ""
    elif port_style == 1:
        header = f"module {mod} (clk, rst_n, chain_in, chain_out);\n"
        body_decls = textwrap.dedent(
            f"""
            input  logic clk;
            input  logic rst_n;
            input  logic [2:0][3:0] chain_in;
            output logic [2:0][3:0] chain_out;
            input  logic [STRB_MAX-1:0] strb_in;
            output logic [2:0][3:0] mid_tap;
            input  logic [2:0][3:0] shallow_return;
            output logic leaf_out;
            input  logic leaf_in;
            parameter int STRB_MAX = {STRB_MAX};
            parameter int LVL = {lvl};
            """
        ).strip()
    elif port_style == 2:
        header = f"module {mod} (input logic clk, input logic rst_n);\n"
        body_decls = textwrap.dedent(
            f"""
            input  wire [2:0][3:0] chain_in;
            output wire [2:0][3:0] chain_out;
            input  wire [STRB_MAX-1:0] strb_in;
            output wire [2:0][3:0] mid_tap;
            input  wire [2:0][3:0] shallow_return;
            output wire leaf_out;
            input  wire leaf_in;
            parameter int STRB_MAX = {STRB_MAX};
            parameter int LVL = {lvl};
            """
        ).strip()
    else:
        header = textwrap.dedent(
            f"""
            module {mod} (
              (* keep = "true" *) input  logic clk,
              (* keep = "true" *) input  logic rst_n,
              input  logic [2:0][3:0] chain_in,
              output logic [2:0][3:0] chain_out,
              input  logic [STRB_MAX-1:0] strb_in,
              output logic [2:0][3:0] mid_tap,
              input  logic [2:0][3:0] shallow_return,
              output logic leaf_out,
              input  logic leaf_in
            );
            """
        ).strip()
        body_decls = f"parameter int STRB_MAX = {STRB_MAX};\n  parameter int LVL = {lvl};"

    bus2d_decl = "logic [2:0][3:0] bus2d;"
    cube_decl = ""
    cube_assign = ""
    if lvl == 3:
        cube_decl = "logic [1:0][2:0][3:0] cube3d;"
        cube_assign = textwrap.dedent(
            """
            assign cube3d[0][1][3] = chain_in[1][3];
            assign cube3d[1][2][0] = chain_in[2][0];
            """
        ).strip()

    zig_block = ""
    chain_src = "chain_in"
    if lvl == 2:
        # zig_to_shallow/zig_decoy/expr_mapped: connectivity probes only (off spine)
        zig_block = textwrap.dedent(
            """
            logic [2:0][3:0] zig_to_shallow, zig_decoy, expr_mapped;
            logic [1:0] concat_tap, or_tap;
            zz_bridge_ping u_bridge_deep (
              .din(chain_in),
              .dout(zig_to_shallow)
            );
            zz_bridge_pong u_bridge_decoy (
              .din(shallow_return),
              .dout(zig_decoy)
            );
            zz_bridge_ping u_bridge_expr (
              .din(chain_in ^ shallow_return),
              .dout(expr_mapped)
            );
            zz_bridge_narrow u_bridge_concat (
              .din({chain_in[1][2], shallow_return[1][2]}),
              .dout(concat_tap)
            );
            zz_bridge_narrow u_bridge_or (
              .din({chain_in[1][2] | shallow_return[1][2], chain_in[0][0]}),
              .dout(or_tap)
            );
            """
        ).strip()
    elif lvl == 4:
        # merge_dummy: u_merge output probe; merge_tap is the fan-in merge target net
        zig_block = textwrap.dedent(
            """
            logic [2:0][3:0] fork_main, fork_decoy, merge_dummy;
            logic merge_tap, merge_quad;
            zz_y_fork u_fork (
              .din(chain_in),
              .main_out(fork_main),
              .decoy_out(fork_decoy)
            );
            zz_y_merge u_merge (
              .main_in(fork_main),
              .side_in(shallow_return),
              .dout(merge_dummy)
            );
            assign merge_tap = fork_main[1][2] | shallow_return[1][2];
            assign merge_quad = fork_main[1][2] | shallow_return[1][2]
                              | fork_main[0][0] | shallow_return[0][0];
            wire grep_zero_a, grep_zero_b;
            assign grep_zero_a = grep_zero_b * 0;
            wire grep_mask_src, grep_mask_dst;
            assign grep_mask_dst = grep_mask_src & 1'b0;
            """
        ).strip()

    casex_block = ""
    if lvl == 1:
        casex_block = textwrap.dedent(
            """
            logic route_casex;
            logic [3:0] key_casex;
            assign key_casex = 4'b?1??;
            always_comb begin
              casex (key_casex)
                4'b??1?: route_casex = chain_in[0][0];
                default: route_casex = chain_in[0][0];
              endcase
            end
            """
        ).strip()
        route_wire = "route_casex"
    elif lvl == 3:
        casex_block = textwrap.dedent(
            """
            logic route_casez;
            logic [3:0] key_casez;
            assign key_casez = 4'b1010;
            always_comb begin
              casez (key_casez)
                4'b1???: route_casez = chain_in[2][1];
                default: route_casez = chain_in[2][1];
              endcase
            end
            """
        ).strip()
        route_wire = "route_casez"
    else:
        route_wire = f"{chain_src}[0][0]"

    ff_clk = textwrap.dedent(
        """
        logic clk_ff;
        always_ff @(posedge clk) begin
          if (!rst_n)
            clk_ff <= 1'b0;
          else
            clk_ff <= clk;
        end
        """
    ).strip()

    ff_barrier = ""
    if lvl == 1:
        ff_barrier = textwrap.dedent(
            """
            logic ff_barrier_tap;
            always_ff @(posedge clk) begin
              if (!rst_n)
                ff_barrier_tap <= 1'b0;
              else
                ff_barrier_tap <= chain_in[0][0];
            end
            """
        ).strip()

    strb_hop = ""
    if lvl == 2:
        strb_hop = textwrap.dedent(
            """
            logic [STRB_MAX-1:0] strb_vec;
            assign strb_vec = strb_in;
            """
        ).strip()

    ifndef_define_block = ""
    if lvl == 2:
        ifndef_define_block = textwrap.dedent(
            """
            `ifndef ZZ_IFNDEF_INST_
            `define ZZ_IFNDEF_INST_
              logic [2:0][3:0] ifndef_mix_tap;
              zz_ifndef_ping u_ifndef_mix (
                .din(chain_in),
                .dout(ifndef_mix_tap)
              );
            `endif
            """
        ).strip()

    decoys = textwrap.dedent(
        f"""
        zz_decoy #(.D({lvl})) d{lvl}_shadow (.clk(clk), .rst_n(rst_n), .noise()),
        zz_decoy u_next_decoy (.clk(clk), .rst_n(rst_n), .noise());
        zz_decoy u_darr [0:0] (.clk(clk), .rst_n(rst_n), .noise());
        """
    ).strip()

    gen_block = ""
    if lvl == 1:
        gen_block = textwrap.dedent(
            """
            `ifdef ZZ_REAL_GEN
            logic gen_tap0, gen_tap1;
            assign gen_tap0 = chain_in[0][0];
            assign gen_tap1 = chain_in[1][0];
            generate
              for (genvar gi = 0; gi < 2; gi++) begin : gen_unroll
                logic gen_tap;
                assign gen_tap = chain_in[gi][0];
              end
            endgenerate
            `endif
            """
        ).strip()
    elif lvl == 5:
        gen_block = textwrap.dedent(
            """
            `ifdef ZZ_REAL_GEN
            logic gen_pass_flat;
            assign gen_pass_flat = leaf_in;
            generate
              if (LVL == 5) begin : g_real_d5
                logic gen_pass;
                assign gen_pass = leaf_in;
              end
            endgenerate
            `endif
            """
        ).strip()

    ifdef_block = ""
    if lvl == 3:
        ifdef_block = textwrap.dedent(
            """
            `ifdef ZZ_REAL_IFDEF
              logic [2:0][3:0] mid_ifdef_tap;
              zz_bridge_ping u_mid_ifdef (
                .din(chain_in),
                .dout(mid_ifdef_tap)
              );
            `endif
            """
        ).strip()
    elif lvl == 4:
        ifdef_block = textwrap.dedent(
            """
            `ifdef ZZ_REAL_IFDEF
              logic ifdef_pass;
              assign ifdef_pass = chain_in[1][1];
            `endif
            `ifdef ZZ_IFDEF_DECOY_ONLY
              logic ifdef_else_net;
              assign ifdef_else_net = chain_in[2][2];
            `endif
            """
        ).strip()

    mid_assign = ""
    if lvl == 3:
        mid_assign = "assign mid_tap = bus2d;"

    if leaf:
        child = ""
        leaf_assign = textwrap.dedent(
            f"""
            assign bus2d = {chain_src};
            assign chain_out = bus2d;
            assign leaf_out = bus2d[1][2];
            {mid_assign}
            """
        ).strip()
    else:
        child = textwrap.dedent(
            f"""
            {child_mod} d{nxt} (
              .clk(clk_ff),
              .rst_n(rst_n),
              .chain_in(bus2d),
              .chain_out(chain_out),
              .strb_in(strb_in),
              .mid_tap(mid_tap),
              .shallow_return(shallow_return),
              .leaf_out(leaf_out),
              .leaf_in(leaf_in)
            );
            """
        ).strip()
        leaf_assign = textwrap.dedent(
            f"""
            assign bus2d = {chain_src};
            {mid_assign}
            """
        ).strip()

    parts = [
        header,
        body_decls,
        bus2d_decl,
        cube_decl,
        ff_clk,
        ff_barrier,
        zig_block,
        casex_block,
        strb_hop,
        leaf_assign,
        cube_assign,
        gen_block,
        ifdef_block,
        ifndef_define_block,
        child,
        decoys,
        "endmodule",
    ]
    return "\n  ".join(p for p in parts if p)


def _shallow_level_body(lvl: int) -> str:
    mod = f"zz_shallow_r{lvl}"
    nxt = lvl + 1
    child_mod = f"zz_shallow_r{nxt}"
    leaf = lvl == SHALLOW_DEPTH
    style = lvl % 3

    if style == 0:
        header = textwrap.dedent(
            f"""
            module {mod} (
              input  logic clk,
              input  logic rst_n,
              input  logic [2:0][3:0] chain_in,
              output logic [2:0][3:0] chain_out,
              output logic [2:0][3:0] shallow_tap,
              input  logic              leaf_in,
              output logic              leaf_out
            );
            """
        ).strip()
    elif style == 1:
        header = f"module {mod} (clk, rst_n, chain_in, chain_out);\n"
        body = textwrap.dedent(
            """
            input  logic clk;
            input  logic rst_n;
            input  logic [2:0][3:0] chain_in;
            output logic [2:0][3:0] chain_out;
            output logic [2:0][3:0] shallow_tap;
            input  logic leaf_in;
            output logic leaf_out;
            """
        ).strip()
    else:
        header = textwrap.dedent(
            f"""
            module {mod} #(
              parameter int RUNG = {lvl}
            )(
              input  logic clk,
              input  logic rst_n,
              input  logic [2:0][3:0] chain_in,
              output logic [2:0][3:0] chain_out,
              output logic [2:0][3:0] shallow_tap,
              input  logic leaf_in,
              output logic leaf_out
            );
            """
        ).strip()
        body = ""

    zig = ""
    chain_src = "chain_in"
    if lvl == 2:
        zig = textwrap.dedent(
            """
            logic [2:0][3:0] pong_bus;
            zz_bridge_pong u_pong_r2 (
              .din(chain_in),
              .dout(pong_bus)
            );
            """
        ).strip()
        chain_src = "pong_bus"

    decoy_alt = ""
    if lvl == 3:
        decoy_alt = "zz_decoy r3_alt (.clk(clk), .rst_n(rst_n), .noise());"

    tap_assign = ""
    if lvl == 2:
        tap_assign = "assign shallow_tap = chain_out;"

    if leaf:
        child = ""
        out_assign = textwrap.dedent(
            """
            assign chain_out = chain_in;
            assign leaf_out = leaf_in;
            """
        ).strip()
    else:
        child = textwrap.dedent(
            f"""
            logic [2:0][3:0] child_out;
            assign child_out = {chain_src};
            {child_mod} r{nxt} (
              .clk(clk),
              .rst_n(rst_n),
              .chain_in(child_out),
              .chain_out(chain_out),
              .shallow_tap(shallow_tap),
              .leaf_in(leaf_in),
              .leaf_out(leaf_out)
            );
            """
        ).strip()
        out_assign = ""

    parts = [
        header,
        locals().get("body", ""),
        zig,
        out_assign,
        tap_assign,
        child,
        decoy_alt,
        "endmodule",
    ]
    return "\n  ".join(p for p in parts if p)


def _deep_arm() -> str:
    return textwrap.dedent(
        f"""
        module zz_deep_arm #(
          parameter int STRB_MAX = {STRB_MAX}
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic [2:0][3:0] a,
          input  logic [STRB_MAX-1:0] strb_in,
          output logic [2:0][3:0] mid_tap,
          input  logic [2:0][3:0] shallow_return,
          output logic leaf_out,
          input  logic leaf_in
        );
          logic [2:0][3:0] chain_root;
          assign chain_root = a;
          zz_deep_d1 d1 (
            .clk(clk),
            .rst_n(rst_n),
            .chain_in(chain_root),
            .chain_out(),
            .strb_in(strb_in),
            .mid_tap(mid_tap),
            .shallow_return(shallow_return),
            .leaf_out(leaf_out),
            .leaf_in(leaf_in)
          );
        endmodule
        """
    ).strip()


def _shallow_arm() -> str:
    return textwrap.dedent(
        """
        module zz_shallow_arm (
          input  logic clk,
          input  logic rst_n,
          input  logic [2:0][3:0] a,
          output logic [2:0][3:0] shallow_tap,
          input  logic leaf_in,
          output logic leaf_out
        );
          logic [2:0][3:0] chain_root;
          assign chain_root = a;
          zz_shallow_r1 r1 (
            .clk(clk),
            .rst_n(rst_n),
            .chain_in(chain_root),
            .chain_out(),
            .shallow_tap(shallow_tap),
            .leaf_in(leaf_in),
            .leaf_out(leaf_out)
          );
        endmodule
        """
    ).strip()


def _zigzag_hub() -> str:
    return textwrap.dedent(
        f"""
        module zz_zigzag #(
          parameter int STRB_MAX = {STRB_MAX}
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic [2:0][3:0] data,
          input  logic [STRB_MAX-1:0] strb_data,
          output logic [1:0][2:0][3:0] status_cube,
          output logic leaf_echo
        );
          logic [2:0][3:0] mid_wire, shallow_bus, shallow_side;
          logic deep_leaf, shallow_leaf;
          logic [STRB_MAX-1:0] strb_pass;

          assign strb_pass = strb_data;

          zz_deep_arm #(
            .STRB_MAX(STRB_MAX)
          ) u_deep (
            .clk(clk),
            .rst_n(rst_n),
            .a(data),
            .strb_in(strb_pass),
            .mid_tap(mid_wire),
            .shallow_return(shallow_side),
            .leaf_out(deep_leaf),
            .leaf_in(shallow_leaf)
          );

          assign shallow_bus = mid_wire;

          zz_shallow_arm u_shallow (
            .clk(clk),
            .rst_n(rst_n),
            .a(shallow_bus),
            .shallow_tap(shallow_side),
            .leaf_in(deep_leaf),
            .leaf_out(shallow_leaf)
          );

          assign leaf_echo = shallow_leaf;
          assign status_cube[0][1][3] = data[1][3];
          assign status_cube[1][2][0] = data[2][0];

          logic [2:0][3:0] bb_out;
          zz_blackbox u_bb (
            .din(mid_wire),
            .dout(bb_out)
          );

          logic empty_multi_y;
          zz_empty_multi u_empty_multi (
            .a(mid_wire[0][0]),
            .b(mid_wire[0][1]),
            .y(empty_multi_y)
          );

          `ifdef ZZ_DECOY_ONLY
            zz_fake_deep_arm u_fake_deep (.clk(clk), .a(data), .mid_tap());
          `endif
        endmodule
        """
    ).strip()


def _dw_vendor_module() -> str:
    """Vendor-named RTL (DW_*.v): must be dropped by ignore-path glob in path-walk."""
    return textwrap.dedent(
        f"""
        module {DW_VENDOR_CELL} (input logic in);
          wire noise;
          assign noise = in;
        endmodule
        """
    ).strip()


def _top_module() -> str:
    return textwrap.dedent(
        f"""
        module {TOP} #(
          parameter int STRB_MAX = {STRB_MAX}
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic [2:0][3:0] data,
          input  logic [STRB_MAX-1:0] strb_data,
          output logic [1:0][2:0][3:0] status_cube,
          output logic leaf_echo
        );
          logic loop_ref;
          logic loop_tie0, loop_tie1, loop_tie2, loop_tie3;
          logic loop_xA, loop_xB, loop_yA, loop_yB;
          logic [1:0] literal_bus;

          assign loop_ref = clk;
          assign loop_tie0 = loop_ref;
          assign loop_tie1 = loop_ref;
          assign loop_tie2 = loop_ref;
          assign loop_tie3 = loop_ref;
          assign loop_xA = loop_ref;
          assign loop_xB = loop_ref;
          assign loop_yA = loop_ref;
          assign loop_yB = loop_ref;
          assign literal_bus[1] = data[0][0];
          assign literal_bus[0] = data[1][2];

          zz_zigzag #(
            .STRB_MAX(STRB_MAX)
          ) u_zigzag (
            .clk(clk),
            .rst_n(rst_n),
            .data(data),
            .strb_data(strb_data),
            .status_cube(status_cube),
            .leaf_echo(leaf_echo)
          );
          zz_collision_d u_collision ();
          {DW_VENDOR_CELL} u_dw_vendor (.in(clk));
        endmodule
        """
    ).strip()


def _hierarchy_specs() -> Tuple[str, ...]:
    return (
        DEEP_D5,
        SHALLOW_R4,
        DEEP_ARM,
        SHALLOW_ARM,
        DEEP_D3,
        DEEP_D4,
        f"{SHALLOW_ARM}.r1.r2",
        f"{DEEP_D2}.u_ifndef_mix",
        COLLISION,
        f"{DEEP_ARM}.d1.d1_shadow",
        f"{DEEP_ARM}.d1.u_next_decoy",
        f"{SHALLOW_ARM}.r1.r2.r3.r3_alt",
    )


def _list_endpoint_check() -> ConnectivityCheck:
    ep_a = (f"{DEEP_D5}.leaf_out", f"{SHALLOW_R4}.leaf_out")
    ep_b = (f"{SHALLOW_R4}.leaf_in", f"{DEEP_D5}.leaf_in")
    expand = build_expand_meta(ep_a, ep_b)
    display_a = f"[{ep_a[0]}, {ep_a[1]}]"
    display_b = f"[{ep_b[0]}, {ep_b[1]}]"
    return ConnectivityCheck(display_a, display_b, check_id="zz_list_endpoints", expand=expand)


def _list_fanout_check(
    check_id: str,
    sources: Sequence[str],
    sink: str,
) -> ConnectivityCheck:
    ep_a = tuple(sources)
    expand = build_expand_meta(ep_a, sink)
    display_a = "[" + ", ".join(ep_a) + "]"
    return ConnectivityCheck(display_a, sink, check_id=check_id, expand=expand)


def _loop_values_for_suite(values: Tuple[str, ...]) -> Any:
    if not values:
        return []
    if all(v.isdigit() or (v.startswith("-") and v[1:].isdigit()) for v in values):
        nums = [int(v) for v in values]
        if len(nums) > 1:
            step = nums[1] - nums[0]
            if step != 0 and all(nums[i] - nums[i - 1] == step for i in range(1, len(nums))):
                return f"{nums[0]}:{nums[-1]}"
        return nums
    if len(values) == 1 and ":" in values[0]:
        return values[0]
    if len(values) == 1 and "," in values[0]:
        return values[0]
    return list(values)


def _suite_spec_from_check(
    chk: ConnectivityCheck,
    *,
    check_id: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Mirror a ConnectivityCheck into suite JSON dict form."""
    spec: Dict[str, Any] = {"id": check_id or chk.check_id, "b": chk.endpoint_b}
    if chk.expand is not None:
        if chk.expand.loop:
            spec["a"] = chk.endpoint_a
            spec["loop"] = {
                k: _loop_values_for_suite(v) for k, v in chk.expand.loop
            }
        elif chk.expand.map_kind == "concat":
            if chk.expand.concat_a or (
                isinstance(chk.endpoint_a, str) and chk.endpoint_a.strip().startswith("{")
            ):
                spec["a"] = chk.endpoint_a
            else:
                spec["a"] = "{" + ", ".join(chk.expand.elements_a) + "}"
        elif chk.expand.map_kind == "array":
            spec["a"] = list(chk.expand.elements_a)
            spec["b"] = list(chk.expand.elements_b)
        elif chk.expand.map_kind == "fanout" and len(chk.expand.elements_a) > 1:
            spec["a"] = list(chk.expand.elements_a)
        else:
            spec["a"] = chk.endpoint_a
    else:
        spec["a"] = chk.endpoint_a
    spec.update(extra)
    return spec


def _round18_design_checks() -> Tuple[ConnectivityCheck, ...]:
    """Gap-fill checks: RTL probes + expand/loop/ifdef patterns (회차18)."""
    return (
        ConnectivityCheck(
            f"{DEEP_D1}.chain_in[0][0]",
            f"{DEEP_D1}.route_casex",
            check_id="zz_casex_route",
        ),
        ConnectivityCheck(
            f"{DEEP_D3}.chain_in[2][1]",
            f"{DEEP_D3}.route_casez",
            check_id="zz_casez_route",
        ),
        ConnectivityCheck(
            f"{DEEP_D4}.chain_in[1][1]",
            f"{DEEP_D4}.ifdef_pass",
            check_id="zz_ifdef_pass",
        ),
        ConnectivityCheck(
            f"{DEEP_D5}.leaf_in",
            f"{DEEP_D5}.gen_pass_flat",
            check_id="zz_gen_pass",
        ),
        _list_fanout_check(
            "zz_expr_mapped",
            (
                f"{DEEP_D2}.chain_in[1][2]",
                f"{DEEP_D2}.shallow_return[1][2]",
            ),
            f"{DEEP_D2}.u_bridge_expr.dout[1][2]",
        ),
        ConnectivityCheck(
            f"{DEEP_D2}.chain_in[0][0]",
            f"{DEEP_D2}.u_bridge_deep.dout[0][0]",
            check_id="zz_zig_to_shallow",
        ),
        ConnectivityCheck(
            f"{DEEP_D2}.shallow_return[1][2]",
            f"{DEEP_D2}.u_bridge_decoy.din[1][2]",
            check_id="zz_zig_decoy",
        ),
        ConnectivityCheck(
            f"{DEEP_D4}.fork_main[1][2]",
            f"{DEEP_D4}.u_merge.dout[1][2]",
            check_id="zz_merge_dummy",
        ),
        ConnectivityCheck(
            f"{DEEP_ARM}.mid_tap[1][2]",
            f"{BLACKBOX}.dout[1][2]",
            check_id="zz_bb_through",
        ),
        ConnectivityCheck(
            f"{DW_VENDOR_INST}.in",
            f"{TOP}.clk",
            check_id="zz_dw_vendor_inst",
        ),
        ConnectivityCheck(
            f"{TOP}.loop_tie{{I}}",
            f"{TOP}.loop_ref",
            check_id="zz_loop_range",
            expand=build_expand_meta(
                f"{TOP}.loop_tie{{I}}",
                f"{TOP}.loop_ref",
                loop={"I": "0:3"},
            ),
        ),
        ConnectivityCheck(
            f"{TOP}.loop_tie{{I}}",
            f"{TOP}.loop_ref",
            check_id="zz_loop_list",
            expand=build_expand_meta(
                f"{TOP}.loop_tie{{I}}",
                f"{TOP}.loop_ref",
                loop={"I": [0, 1, 2, 3]},
            ),
        ),
        ConnectivityCheck(
            f"{TOP}.loop_{{I}}{{J}}",
            f"{TOP}.loop_ref",
            check_id="zz_loop_csv",
            expand=build_expand_meta(
                f"{TOP}.loop_{{I}}{{J}}",
                f"{TOP}.loop_ref",
                loop={"I": "x,y", "J": "A,B"},
            ),
        ),
        _list_fanout_check(
            "zz_port_concat",
            (
                f"{DEEP_D2}.chain_in[1][2]",
                f"{DEEP_D2}.shallow_return[1][2]",
            ),
            f"{DEEP_D2}.u_bridge_concat.din[1]",
        ),
        _list_fanout_check(
            "zz_port_expr_or",
            (
                f"{DEEP_D2}.chain_in[1][2]",
                f"{DEEP_D2}.shallow_return[1][2]",
            ),
            f"{DEEP_D2}.u_bridge_or.din[1]",
        ),
        _list_fanout_check(
            "zz_fanin_merge4",
            (
                f"{DEEP_D4}.fork_main[1][2]",
                f"{DEEP_D4}.shallow_return[1][2]",
                f"{DEEP_D4}.fork_main[0][0]",
                f"{DEEP_D4}.shallow_return[0][0]",
            ),
            f"{DEEP_D4}.merge_quad",
        ),
        ConnectivityCheck(
            f"{DEEP_D1}.chain_in[0][0]",
            f"{DEEP_D1}.gen_tap0",
            check_id="zz_gen_for_unroll",
        ),
        ConnectivityCheck(
            f"{DEEP_D4}.chain_in[2][2]",
            f"{DEEP_D4}.ifdef_else_net",
            check_id="zz_ifdef_inactive",
        ),
        ConnectivityCheck(
            f"{{{TOP}.data[0][0], {TOP}.data[1][2]}}",
            f"{TOP}.literal_bus[1:0]",
            check_id="zz_literal_concat",
            expand=build_expand_meta(
                f"{{{TOP}.data[0][0], {TOP}.data[1][2]}}",
                f"{TOP}.literal_bus[1:0]",
            ),
        ),
        ConnectivityCheck(
            f"{DEEP_D3}.chain_in[0][0]",
            f"{DEEP_D3}.u_mid_ifdef.dout[0][0]",
            check_id="zz_mid_ifdef_child",
        ),
    )


def _round20_design_checks() -> Tuple[ConnectivityCheck, ...]:
    """Round20: `` `ifndef `` / `` `define `` include-guard order on spine (회차20)."""
    return (
        ConnectivityCheck(
            f"{DEEP_D2}.chain_in[0][0]",
            f"{DEEP_D2}.u_ifndef_mix.dout[0][0]",
            check_id="zz_ifndef_define_mix",
        ),
    )


def _round19_design_checks() -> Tuple[ConnectivityCheck, ...]:
    """Round19: vuln-plan gaps + round18 cone/io blind spots (회차19)."""
    return (
        ConnectivityCheck(
            f"{DEEP_D1}.chain_in[1][0]",
            f"{DEEP_D1}.gen_tap1",
            check_id="zz_gen_tap1",
        ),
        ConnectivityCheck(
            f"{SHALLOW_R2}.chain_in[1][2]",
            f"{SHALLOW_R2}.u_pong_r2.din[1][2]",
            check_id="zz_pong_replicate",
        ),
        ConnectivityCheck(
            f"{DEEP_D1}.chain_in[0][0]",
            f"{DEEP_D1}.ff_barrier_tap",
            check_id="zz_ff_barrier_tap",
        ),
        ConnectivityCheck(
            f"{HUB}.u_empty_multi.a",
            f"{HUB}.u_empty_multi.y",
            check_id="zz_multi_g3_empty",
        ),
    )


def _gap_suite_specs(checks: Tuple[ConnectivityCheck, ...]) -> List[Dict[str, Any]]:
    """Suite JSON for gap-fill design checks with explicit expect_connected."""
    specs: List[Dict[str, Any]] = []
    for chk in checks:
        if chk.check_id == "zz_dw_vendor_inst":
            continue
        extra: Dict[str, Any] = {}
        if chk.check_id in SUITE_CONN_NEGATIVE_IDS:
            extra["expect_connected"] = False
        else:
            extra["expect_connected"] = True
        specs.append(_suite_spec_from_check(chk, **extra))
    return specs


def _apply_suite_conn_defaults(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach expect_connected defaults for suite verifier logical phase."""
    for spec in specs:
        cid = str(spec.get("id") or "")
        if "expect_connected" in spec:
            continue
        if cid in SUITE_CONN_VERDICT_SKIP_IDS:
            continue
        if cid in SUITE_CONN_NEGATIVE_IDS:
            spec["expect_connected"] = False
        else:
            spec["expect_connected"] = True
    return specs


def _fanin_merge_check() -> ConnectivityCheck:
    """Fan-in via fanout expand: list sources in a, scalar sink in b."""
    ep_a = (
        f"{DEEP_D4}.chain_in[1][2]",
        f"{DEEP_D4}.shallow_return[1][2]",
        f"{DEEP_D4}.fork_main[1][2]",
    )
    ep_b = f"{DEEP_D4}.merge_tap"
    expand = build_expand_meta(ep_a, ep_b)
    display_a = f"[{ep_a[0]}, {ep_a[1]}]"
    return ConnectivityCheck(display_a, ep_b, check_id="zz_fanin_merge", expand=expand)


def _fanin_merge_decoy_check() -> ConnectivityCheck:
    return ConnectivityCheck(
        f"{DEEP_D4}.fork_decoy[1][2]",
        f"{DEEP_D4}.merge_tap",
        check_id="zz_fanin_merge_decoy",
    )


def _port_expr_xor_check() -> ConnectivityCheck:
    ep_a = (
        f"{DEEP_D2}.chain_in[1][2]",
        f"{DEEP_D2}.shallow_return[1][2]",
    )
    ep_b = f"{DEEP_D2}.u_bridge_expr.din[1][2]"
    expand = build_expand_meta(ep_a, ep_b)
    display_a = f"[{ep_a[0]}, {ep_a[1]}]"
    return ConnectivityCheck(display_a, ep_b, check_id="zz_port_expr_xor", expand=expand)


def _build_checks() -> Tuple[ConnectivityCheck, ...]:
    checks: List[ConnectivityCheck] = [
        ConnectivityCheck(f"{TOP}.data[0][0]", f"{DEEP_ARM}.a[0][0]", "zz_src_deep_a00"),
        ConnectivityCheck(f"{DEEP_ARM}.mid_tap", f"{SHALLOW_ARM}.a", "zz_mid_to_shallow"),
        ConnectivityCheck(
            f"{DEEP_ARM}.mid_tap[1][2]",
            f"{SHALLOW_ARM}.a[1][2]",
            "zz_mid_slice",
        ),
        ConnectivityCheck(f"{TOP}.clk", f"{DEEP_D5}.clk", "zz_clk_deep"),
        ConnectivityCheck(f"{DEEP_D5}.leaf_out", f"{SHALLOW_R4}.leaf_in", "zz_cross_leaf"),
        ConnectivityCheck(f"{TOP}.data[1][2]", f"{DEEP_D5}.leaf_out", "zz_top_deep_2d"),
        ConnectivityCheck(
            f"{TOP}.data[1][3]",
            f"{DEEP_D3}.cube3d[0][1][3]",
            "zz_cube3d_slice",
        ),
        ConnectivityCheck(f"{TOP}.strb_data", f"{DEEP_D3}.strb_in", "zz_strb_param"),
        ConnectivityCheck(f"{COLLISION}.w_e", f"{COLLISION}.w_e", "zz_collision_port"),
        ConnectivityCheck(f"{DEEP_D3}.chain_in", f"{DEEP_D5}.chain_out", "zz_d3_port_port"),
        ConnectivityCheck(
            f"{SHALLOW_ARM}.shallow_tap[1][1]",
            f"{SHALLOW_ARM}.r1.r2.shallow_tap[1][1]",
            "zz_shallow_tap",
        ),
        ConnectivityCheck(
            f"{TOP}.data[2][0]",
            f"{DEEP_D3}.mid_tap[2][0]",
            "zz_top_shallow_bus",
        ),
        ConnectivityCheck(
            f"{HUB}.status_cube[0][1][3]",
            f"{TOP}.data[1][3]",
            "zz_status_cube_top",
        ),
        ConnectivityCheck(f"{TOP}.leaf_echo", f"{SHALLOW_R4}.leaf_out", "zz_leaf_echo"),
        ConnectivityCheck(
            f"{DEEP_ARM}.d1.chain_in[0][0]",
            f"{DEEP_ARM}.d1.d2.chain_in[0][0]",
            "zz_deep_chain_hop",
        ),
        ConnectivityCheck(
            f"{HUB}.u_deep.d1.d2.d3.mid_tap[2][3]",
            f"{HUB}.u_shallow.a[2][3]",
            "zz_mid_tap_cross_arm",
        ),
        ConnectivityCheck(
            f"{TOP}.data[0]",
            f"{TOP}.data[0]",
            "zz_top_self",
        ),
        ConnectivityCheck(
            f"{TOP}.u_zigzag.u_missing.probe",
            f"{TOP}.data[0][0]",
            "zz_missing_hierarchy",
        ),
        _fanin_merge_check(),
        _fanin_merge_decoy_check(),
        _port_expr_xor_check(),
        _list_endpoint_check(),
        *_round18_design_checks(),
        *_round19_design_checks(),
        *_round20_design_checks(),
    ]
    return tuple(checks)


def generate_zigzag_torture_design() -> ZigzagTortureDesign:
    preamble = _defines_preamble()
    files: Dict[str, str] = {
        DW_VENDOR_RTL: _dw_vendor_module(),
        "zz_common.v": "\n\n".join(
            [
                preamble,
                _decoy_leaf(),
                _decoy_module(),
                _bridge_modules(),
                _ifndef_guard_ping_module(),
                _blackbox_module(),
                _collision_module(),
            ]
        ),
        "zz_fake_deep.v": _fake_deep_decoy_file(),
        "zz_deep_arm.v": _deep_arm(),
        "zz_shallow_arm.v": _shallow_arm(),
        "zz_zigzag.v": _zigzag_hub(),
        "zz_torture_top.v": _top_module(),
    }
    for lvl in range(1, DEEP_DEPTH + 1):
        files[f"zz_deep_d{lvl}.v"] = _deep_level_body(lvl)
    for lvl in range(1, SHALLOW_DEPTH + 1):
        files[f"zz_shallow_r{lvl}.v"] = _shallow_level_body(lvl)

    return ZigzagTortureDesign(
        files=files,
        top=TOP,
        deep_path=DEEP_D5,
        shallow_path=SHALLOW_R4,
        checks=_build_checks(),
        hierarchy_specs=_hierarchy_specs(),
    )


def build_connect_request(design: ZigzagTortureDesign) -> ConnectivityRequest:
    return ConnectivityRequest(
        checks=design.checks,
        top=design.top,
        defines=dict(DEFINES),
        include_ff=True,
    )


def _suite_conn_checks() -> List[Dict[str, Any]]:
    """Conn checks covering scalar/list/fanout/concat/display/fail cases."""
    list_a = [f"{DEEP_D5}.leaf_out", f"{SHALLOW_R4}.leaf_out"]
    list_b = [f"{SHALLOW_R4}.leaf_in", f"{DEEP_D5}.leaf_in"]
    hier_list_a = [DEEP_D5, SHALLOW_R4]
    specs: List[Dict[str, Any]] = [
        {"id": "zz_src_deep_a00", "a": f"{TOP}.data[0][0]", "b": f"{DEEP_ARM}.a[0][0]"},
        {"id": "zz_mid_to_shallow", "a": f"{DEEP_ARM}.mid_tap", "b": f"{SHALLOW_ARM}.a"},
        {
            "id": "zz_mid_slice",
            "a": f"{DEEP_ARM}.mid_tap[1][2]",
            "b": f"{SHALLOW_ARM}.a[1][2]",
        },
        {"id": "zz_clk_deep", "a": f"{TOP}.clk", "b": f"{DEEP_D5}.clk"},
        {"id": "zz_cross_leaf", "a": f"{DEEP_D5}.leaf_out", "b": f"{SHALLOW_R4}.leaf_in"},
        {"id": "zz_top_deep_2d", "a": f"{TOP}.data[1][2]", "b": f"{DEEP_D5}.leaf_out"},
        {
            "id": "zz_top_shallow_bus",
            "a": f"{TOP}.data[2][0]",
            "b": f"{DEEP_D3}.mid_tap[2][0]",
        },
        {
            "id": "zz_status_cube_top",
            "a": f"{HUB}.status_cube[0][1][3]",
            "b": f"{TOP}.data[1][3]",
        },
        {"id": "zz_leaf_echo", "a": f"{TOP}.leaf_echo", "b": f"{SHALLOW_R4}.leaf_out"},
        {"id": "zz_top_self", "a": f"{TOP}.data[0]", "b": f"{TOP}.data[0]"},
        {"id": "zz_strb_param", "a": f"{TOP}.strb_data", "b": f"{DEEP_D3}.strb_in"},
        {
            "id": "zz_strb_slice",
            "a": f"{TOP}.strb_data[3]",
            "b": f"{DEEP_D3}.strb_in[3]",
        },
        {"id": "zz_d3_port_port", "a": f"{DEEP_D3}.chain_in", "b": f"{DEEP_D5}.chain_out"},
        {
            "id": "zz_mid_tap_cross_arm",
            "a": f"{HUB}.u_deep.d1.d2.d3.mid_tap[2][3]",
            "b": f"{HUB}.u_shallow.a[2][3]",
        },
        {"id": "zz_collision_port", "a": f"{COLLISION}.w_e", "b": f"{COLLISION}.w_e"},
        {
            "id": "zz_inst_hop",
            "a": f"{DEEP_ARM}.d1.chain_in[0][0]",
            "b": f"{DEEP_ARM}.d1.d2.chain_in[0][0]",
        },
        {
            "id": "zz_wire_ref",
            "a": f"{TOP}.data[1][3]",
            "b": f"{DEEP_D3}.cube3d[0][1][3]",
        },
        {
            "id": "zz_port_ref",
            "a": f"{SHALLOW_ARM}.shallow_tap[1][1]",
            "b": f"{SHALLOW_ARM}.r1.r2.shallow_tap[1][1]",
        },
        {
            "id": "zz_fanout_mid",
            "a": f"{DEEP_ARM}.mid_tap[1][2]",
            "b": [f"{SHALLOW_ARM}.a[1][2]", f"{DEEP_D5}.leaf_out"],
        },
        # fan-in: expand treats list-a + scalar-b as fanout (sources -> sink)
        {
            "id": "zz_fanin_merge",
            "a": [
                f"{DEEP_D4}.chain_in[1][2]",
                f"{DEEP_D4}.shallow_return[1][2]",
                f"{DEEP_D4}.fork_main[1][2]",
            ],
            "b": f"{DEEP_D4}.merge_tap",
            "expect_connected": True,
        },
        {
            "id": "zz_fanin_merge_decoy",
            "a": f"{DEEP_D4}.fork_decoy[1][2]",
            "b": f"{DEEP_D4}.merge_tap",
            "expect_connected": False,
        },
        {
            "id": "zz_port_expr_xor",
            "a": [
                f"{DEEP_D2}.chain_in[1][2]",
                f"{DEEP_D2}.shallow_return[1][2]",
            ],
            "b": f"{DEEP_D2}.u_bridge_expr.din[1][2]",
            "expect_connected": True,
        },
        {"id": "zz_list_expand", "a": list_a, "b": list_b},
        {
            "id": "zz_list_display",
            "a": f"[{DEEP_D5}, {SHALLOW_R4}]",
            "b": f"{DEEP_D5}.leaf_out",
        },
        {
            "id": "zz_hier_array",
            "a": hier_list_a,
            "b": f"{DEEP_D5}.leaf_out",
        },
        {
            "id": "zz_array_zip",
            "a": [f"{TOP}.data[0][0]", f"{TOP}.data[1][2]"],
            "b": [f"{DEEP_ARM}.a[0][0]", f"{DEEP_D5}.leaf_out"],
        },
        {
            "id": "zz_wire_list_display",
            "a": f"[{TOP}.data[0][0], {TOP}.data[1][2]]",
            "b": f"{DEEP_ARM}.a[0][0]",
        },
        {
            "id": "zz_missing_hierarchy",
            "a": f"{TOP}.u_zigzag.u_missing.probe",
            "b": f"{TOP}.data[0][0]",
            "expect_connected": False,
        },
        {
            "id": "zz_intentional_fail",
            "a": f"{TOP}.data[9][9]",
            "b": f"{SHALLOW_R4}.leaf_in",
            "expect_connected": False,
        },
        {
            "id": "zz_common_inst_batch",
            "a": [D1_SHADOW, COLLISION, R3_ALT],
            "b": f"{TOP}.clk",
            "expect_hierarchy": [
                {
                    "side": "a",
                    "path": D1_SHADOW,
                    "module": "zz_decoy",
                    "rtl_file": ZZ_COMMON_RTL,
                },
                {
                    "side": "a",
                    "path": COLLISION,
                    "module": "zz_collision_d",
                    "rtl_file": ZZ_COMMON_RTL,
                },
                {
                    "side": "a",
                    "path": R3_ALT,
                    "module": "zz_decoy",
                    "rtl_file": ZZ_COMMON_RTL,
                },
            ],
        },
        {
            "id": "zz_common_inst_display",
            "a": f"[{D1_SHADOW}, {COLLISION}]",
            "b": f"{TOP}.clk",
            "expect_hierarchy": [
                {
                    "side": "a",
                    "path": D1_SHADOW,
                    "module": "zz_decoy",
                    "rtl_file": ZZ_COMMON_RTL,
                },
                {
                    "side": "a",
                    "path": COLLISION,
                    "module": "zz_collision_d",
                    "rtl_file": ZZ_COMMON_RTL,
                },
            ],
        },
        {
            "id": "zz_bridge_d2_bus",
            "a": f"{DEEP_D2}.chain_in[0][0]",
            "b": f"{DEEP_D2}.chain_in[2][3]",
            "expect_hierarchy": [
                {
                    "side": "a",
                    "path": DEEP_D2,
                    "module": "zz_deep_d2",
                    "rtl_file": "zz_deep_d2.v",
                },
            ],
        },
        {
            "id": "zz_collision_nested_same_file",
            "a": f"{COLLISION}.w_e",
            "b": f"{COLLISION}.w_e",
            "expect_hierarchy": [
                {
                    "side": "a",
                    "path": COLLISION,
                    "module": "zz_collision_d",
                    "rtl_file": ZZ_COMMON_RTL,
                },
            ],
        },
        {
            "id": "zz_fake_deep_not_on_spine",
            "a": f"{TOP}.u_zigzag.u_fake_deep",
            "b": f"{TOP}.data[0][0]",
            "expect_connected": False,
        },
        {
            "id": "zz_dw_vendor_ignored",
            "a": f"{TOP}.clk",
            "b": f"{TOP}.clk",
        },
        *_gap_suite_specs(_round18_design_checks()),
        *_gap_suite_specs(_round19_design_checks()),
        *_gap_suite_specs(_round20_design_checks()),
    ]
    return _apply_suite_conn_defaults(specs)


def build_flat_suite_document(design: ZigzagTortureDesign) -> Dict[str, Any]:
    """Comprehensive flat-suite JSON: conn (text/logical), io-trace, fanin/fanout cones."""
    checks = _suite_conn_checks()
    return {
        "filelist": "filelist.f",
        "top": design.top,
        "defines": dict(DEFINES),
        "run_on_full_index": {"enable": 0},
        "tests": [
            {
                "name": "conn_text",
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "connect_phase": "text",
                    "include_ff": True,
                    "ignore-path": ["DW_*"],
                    "checks": checks,
                    "output": "zz_conn.tsv",
                },
            },
            {
                "name": "conn_logical",
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "connect_phase": "logical",
                    "include_ff": True,
                    "ignore-path": ["DW_*"],
                    "checks": checks,
                    "output": "zz_conn.tsv",
                },
            },
            {
                "name": "io_trace_ff_inst",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": f"{DEEP_ARM}.d1",
                    "direction": "driver",
                    "path_kind": "ff",
                    "trace_max_depth": 6,
                    "output": "zz_io_ff.tsv",
                },
            },
            {
                "name": "io_trace_comb_arm",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": DEEP_ARM,
                    "direction": "both",
                    "path_kind": "comb",
                    "trace_max_depth": 8,
                    "output": "zz_io_comb.tsv",
                },
            },
            {
                "name": "io_trace_blackbox",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": BLACKBOX,
                    "direction": "sinker",
                    "path_kind": "comb",
                    "trace_max_depth": 3,
                    "output": "zz_io_bb.tsv",
                },
            },
            {
                "name": "io_trace_bb_driver",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": BLACKBOX,
                    "direction": "driver",
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_io_bb_drv.tsv",
                },
            },
            {
                "name": "io_trace_shallow_sinker",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": SHALLOW_ARM,
                    "direction": "sinker",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_io_shallow.tsv",
                },
            },
            {
                "name": "io_trace_common_decoy",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": D1_SHADOW,
                    "direction": "driver",
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_io_common_decoy.tsv",
                },
            },
            {
                "name": "io_trace_bridge_d2",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": DEEP_D2,
                    "direction": "both",
                    "path_kind": "comb",
                    "trace_max_depth": 5,
                    "output": "zz_io_bridge_d2.tsv",
                },
            },
            {
                "name": "io_trace_merge_d4",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": DEEP_D4,
                    "direction": "both",
                    "path_kind": "comb",
                    "trace_max_depth": 5,
                    "output": "zz_io_merge_d4.tsv",
                },
            },
            {
                "name": "io_trace_ifdef_d4",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": DEEP_D4,
                    "direction": "driver",
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_io_ifdef_d4.tsv",
                },
            },
            {
                "name": "io_trace_expr_xor_d2",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": f"{DEEP_D2}.u_bridge_expr",
                    "direction": "sinker",
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_io_expr_xor.tsv",
                },
            },
            {
                "name": "cone_fanout_deep",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": CONE_FANOUT,
                    "path_kind": "comb",
                    "trace_max_depth": 12,
                    "include_ff": True,
                    "output": "zz_cone_fanout.tsv",
                },
            },
            {
                "name": "cone_fanin_leaf",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": CONE_FANIN,
                    "path_kind": "comb",
                    "trace_max_depth": 10,
                    "include_ff": True,
                    "output": "zz_cone_fanin.tsv",
                },
            },
            {
                "name": "cone_fanout_blackbox",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": CONE_FANOUT,
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_cone_bb.tsv",
                },
            },
            {
                "name": "cone_fanin_port",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": f"{SHALLOW_ARM}.a[1][2]",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "include_ff": True,
                    "output": "zz_cone_fanin_port.tsv",
                },
            },
            {
                "name": "cone_fanout_inst",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{DEEP_D3}.leaf_out",
                    "path_kind": "ff",
                    "trace_max_depth": 5,
                    "include_ff": True,
                    "output": "zz_cone_fanout_ff.tsv",
                },
            },
            {
                "name": "cone_fanin_blackbox",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": f"{BLACKBOX}.dout[1][2]",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_cone_fanin_bb.tsv",
                },
            },
            {
                "name": "cone_fanout_top_port",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{TOP}.data[1][2]",
                    "path_kind": "comb",
                    "trace_max_depth": 14,
                    "include_ff": True,
                    "output": "zz_cone_fanout_top.tsv",
                },
            },
            {
                "name": "cone_fanin_ff",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": f"{DEEP_D5}.leaf_out",
                    "path_kind": "ff",
                    "trace_max_depth": 8,
                    "include_ff": True,
                    "output": "zz_cone_fanin_ff.tsv",
                },
            },
            {
                "name": "cone_fanout_common_decoy",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{D1_SHADOW}.noise",
                    "path_kind": "comb",
                    "trace_max_depth": 5,
                    "output": "zz_cone_common_decoy.tsv",
                },
            },
            {
                "name": "cone_fanin_merge_tap",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": CONE_MERGE_TAP,
                    "path_kind": "comb",
                    "trace_max_depth": 8,
                    "include_ff": True,
                    "output": "zz_cone_merge_tap.tsv",
                },
            },
            {
                "name": "cone_fanin_expr_xor",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": CONE_EXPR_XOR,
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "include_ff": True,
                    "output": "zz_cone_expr_xor.tsv",
                },
            },
            {
                "name": "cone_fanout_ifdef_pass",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{DEEP_D4}.ifdef_pass",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_cone_ifdef_pass.tsv",
                },
            },
            {
                "name": "cone_fanin_gen_pass",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": f"{DEEP_D5}.gen_pass_flat",
                    "path_kind": "comb",
                    "trace_max_depth": 8,
                    "include_ff": True,
                    "output": "zz_cone_gen_pass.tsv",
                },
            },
            {
                "name": "cone_fanout_casex",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{DEEP_D1}.route_casex",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_cone_casex.tsv",
                },
            },
            {
                "name": "cone_fanout_casez",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{DEEP_D3}.route_casez",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_cone_casez.tsv",
                },
            },
            {
                "name": "cone_fanin_merge_quad",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanin_cone": f"{DEEP_D4}.merge_quad",
                    "path_kind": "comb",
                    "trace_max_depth": 8,
                    "include_ff": True,
                    "output": "zz_cone_merge_quad.tsv",
                },
            },
            {
                "name": "cone_fanout_literal_bus",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{TOP}.literal_bus[1]",
                    "path_kind": "comb",
                    "trace_max_depth": 8,
                    "include_ff": True,
                    "output": "zz_cone_literal_bus.tsv",
                },
            },
            {
                "name": "cone_fanout_mid_ifdef",
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "fanout_cone": f"{DEEP_D3}.u_mid_ifdef.dout[0][0]",
                    "path_kind": "comb",
                    "trace_max_depth": 6,
                    "output": "zz_cone_mid_ifdef.tsv",
                },
            },
            {
                "name": "io_trace_mid_ifdef_d3",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": f"{DEEP_D3}.u_mid_ifdef",
                    "direction": "both",
                    "path_kind": "comb",
                    "trace_max_depth": 5,
                    "output": "zz_io_mid_ifdef.tsv",
                },
            },
            {
                "name": "io_trace_pong_r2",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": SHALLOW_R2,
                    "direction": "both",
                    "path_kind": "comb",
                    "trace_max_depth": 5,
                    "output": "zz_io_pong_r2.tsv",
                },
            },
            {
                "name": "io_trace_top_loop",
                "run_io_trace": {
                    "enable": 1,
                    "mode": "path-walk",
                    "instance": TOP,
                    "direction": "driver",
                    "path_kind": "comb",
                    "trace_max_depth": 4,
                    "output": "zz_io_top_loop.tsv",
                },
            },
        ],
    }


def write_flat_suite_artifacts(root: Path) -> Tuple[Path, Path, ZigzagTortureDesign]:
    """Write RTL, filelist, and flat-suite JSON for comprehensive verification."""
    fl, _req, design = write_stress_artifacts(root)
    suite_path = root / "zz_torture.suite.json"
    suite_path.write_text(
        json.dumps(build_flat_suite_document(design), indent=2) + "\n",
        encoding="utf-8",
    )
    return fl, suite_path, design


def write_stress_artifacts(root: Path) -> Tuple[Path, Path, ZigzagTortureDesign]:
    design = generate_zigzag_torture_design()
    root.mkdir(parents=True, exist_ok=True)
    for name, text in design.files.items():
        (root / name).write_text(text + "\n", encoding="utf-8")
    fl = root / "filelist.f"
    fl.write_text(
        "\n".join(str((root / n).resolve()) for n in sorted(design.files)) + "\n",
        encoding="utf-8",
    )
    req_path = root / "zz_torture.connect.json"
    from hierwalk.connect.shared.request import write_connect_request

    write_connect_request(req_path, build_connect_request(design))
    return fl, req_path, design


def main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Generate zigzag torture RTL corpus")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    fl, req_path, design = write_stress_artifacts(args.out_dir)
    run_json = {
        "filelist": str(fl),
        "mode": "path-walk",
        "top": design.top,
        "check_connect_batch": str(req_path),
        "output": "connect.tsv",
        "defines": build_connect_request(design).defines,
    }
    run_path = args.out_dir / "zz_torture.run.json"
    run_path.write_text(json.dumps(run_json, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {len(design.files)} RTL files, {len(design.checks)} checks",
        file=sys.stderr,
    )
    print(f"  filelist: {fl}", file=sys.stderr)
    print(f"  connect:  {req_path}", file=sys.stderr)
    print(f"  run:      {run_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())