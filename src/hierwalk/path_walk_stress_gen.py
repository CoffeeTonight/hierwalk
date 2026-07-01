"""
Stress RTL for path-walk: 4 sets × 10-deep zigzag × 10-bit array + a0..a9 drivers.

Main hierarchy path always uses instance name ``u_next`` (path-walk friendly).
Each level rotates port declarations and adds a decoy instance in a difficult form.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest

SET_IDS: Tuple[str, ...] = ("A", "B", "C", "D")
CROSS_ARMS: Tuple[str, ...] = ("A", "B")
DEPTH: int = 10
BUS_WIDTH: int = 10
BRIDGE_LEVEL: int = 5

_DRIVE_STYLES: Tuple[str, ...] = (
    "wire_assign",
    "reg_ff",
    "always_comb",
    "array_stash",
    "reg_assign",
)

_INST_DECOY_STYLES: Tuple[str, ...] = (
    "comma_chain",
    "array_inst",
    "gen_if",
    "gen_for",
    "ifdef_wrap",
    "attr_inst",
    "param_override",
    "multiline_hash",
    "plain",
    "comma_chain",
)

_PORT_STYLES: Tuple[str, ...] = (
    "ansi_full",
    "nonansi_body",
    "mixed_header_body",
    "param_vector",
    "wire_reg_ansi",
    "old_port_list",
    "array_bus",
    "attr_ports",
    "bracket_dims",
    "localparam_width",
)


@dataclass(frozen=True)
class PathWalkStressDesign:
    files: Dict[str, str]
    top: str
    depth: int
    bus_width: int
    sets: Tuple[str, ...]
    checks: Tuple[ConnectivityCheck, ...]


def _leaf_path(set_id: str) -> str:
    return "pw_top.u_set_" + set_id + ".u_next" + ".u_next" * (DEPTH - 1)


def _mid_inst_path(set_id: str, level: int = BRIDGE_LEVEL) -> str:
    hops = max(0, min(level, DEPTH - 1))
    return "pw_top.u_set_" + set_id + ".u_next" + ".u_next" * hops


def _is_cross_arm(set_id: str) -> bool:
    return set_id in CROSS_ARMS


def _link_port_decls() -> str:
    return textwrap.dedent(
        """
          input  logic [9:0] link_in,
          output logic [9:0] link_out,
        """
    ).strip()


def _drive_chain_body(chain_src: str) -> str:
    """Main chain stays combinational; FF/always patterns live in side noise."""
    return textwrap.dedent(
        f"""
          logic [9:0] chain_local;
          assign chain_local = {chain_src};
        """
    ).strip()


def _drive_noise_body(style: str, chain_src: str, lvl: int) -> str:
    if style == "reg_ff":
        return textwrap.dedent(
            f"""
              logic [9:0] noise_ff_{lvl};
              always_ff @(posedge clk) begin
                if (!rst_n)
                  noise_ff_{lvl} <= 10'b0;
                else
                  noise_ff_{lvl} <= {chain_src};
              end
            """
        ).strip()
    if style == "always_comb":
        return textwrap.dedent(
            f"""
              logic [9:0] noise_comb_{lvl};
              always_comb noise_comb_{lvl} = {chain_src} ^ 10'b0;
            """
        ).strip()
    if style == "array_stash":
        return textwrap.dedent(
            f"""
              logic [9:0] stash_arr_{lvl} [0:1];
              logic [9:0] noise_arr_{lvl};
              assign stash_arr_{lvl}[0] = {chain_src};
              assign stash_arr_{lvl}[1] = stash_arr_{lvl}[0];
              assign noise_arr_{lvl} = stash_arr_{lvl}[1];
            """
        ).strip()
    if style == "reg_assign":
        return textwrap.dedent(
            f"""
              reg [9:0] noise_reg_{lvl};
              logic [9:0] noise_alias_{lvl};
              always @(posedge clk) noise_reg_{lvl} <= {chain_src};
              assign noise_alias_{lvl} = noise_reg_{lvl};
            """
        ).strip()
    return textwrap.dedent(
        f"""
          wire noise_wire_{lvl};
          assign noise_wire_{lvl} = ^{chain_src};
        """
    ).strip()


def _similar_name_trap(lvl: int) -> str:
    return textwrap.dedent(
        f"""
          wire [9:0] u_next_shadow;
          wire [9:0] chain_in_alias;
          assign u_next_shadow = chain_in[9:0];
          assign chain_in_alias = u_next_shadow;
        """
    ).strip()


def _bridge_module() -> str:
    return textwrap.dedent(
        """
        module pw_bridge (
          input  logic        clk,
          input  logic        rst_n,
          input  logic [9:0]  from_a,
          input  logic [9:0]  from_b,
          output logic [9:0]  to_a,
          output logic [9:0]  to_b
        );
          logic [9:0] stash;
          logic [9:0] u_next;
          wire  [9:0] chain_in;
          assign chain_in = from_a;
          assign to_b = from_a;
          assign to_a = from_b;
          always_ff @(posedge clk) begin
            if (!rst_n)
              stash <= 10'b0;
            else
              stash <= chain_in;
          end
          assign u_next = stash;
          wire [9:0] trap_drop_a, trap_drop_b;
          pw_decoy u_d_bridge (.clk(clk), .rst_n(rst_n));
          pw_clutter_trap u_trap_a (.clk(clk), .rst_n(rst_n), .din(from_a), .dout(trap_drop_a));
          pw_clutter_trap u_trap_b (.clk(clk), .rst_n(rst_n), .din(from_b), .dout(trap_drop_b));
        endmodule
        """
    ).strip()


def _clutter_modules() -> Dict[str, str]:
    trap = textwrap.dedent(
        """
        module pw_clutter_trap (
          input  logic        clk,
          input  logic        rst_n,
          input  logic [9:0]  din,
          output logic [9:0]  dout
        );
          wire [9:0] u_next;
          wire [9:0] chain_in;
          assign chain_in = din;
          assign u_next = chain_in;
          pw_decoy u_d0 (.clk(clk), .rst_n(rst_n)),
          pw_decoy u_d1 (.clk(clk), .rst_n(rst_n));
          `ifdef PW_STRESS_CLUTTER
            assign dout = u_next;
          `else
            assign dout = din;
          `endif
        endmodule
        """
    ).strip()
    noise = textwrap.dedent(
        """
        module pw_clutter_noise #(
          parameter int IDX = 0
        ) (
          input logic clk,
          input logic rst_n
        );
          generate
            for (genvar gi = 0; gi < 1; gi++) begin : g_noise
              if (IDX >= 0) begin : g_if_noise
                pw_decoy u_d (.clk(clk), .rst_n(rst_n));
              end
            end
          endgenerate
          wire [3:0] n;
          assign n = {clk, rst_n, clk, rst_n} ^ IDX;
        endmodule
        """
    ).strip()
    return {
        "pw_clutter_trap.v": trap,
        "pw_clutter_noise.v": noise,
    }


def _port_block(style: str, lvl: int) -> Tuple[str, str, str, str]:
    """Returns mod_open, body_decls, arr_assign, mod_close_hint."""
    w = BUS_WIDTH
    arr_out = "output logic [9:0] arr"
    if style == "ansi_full":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} #(
                  parameter int LVL = {lvl}
                )(
                  input  logic              clk,
                  input  logic              rst_n,
                  input  logic [{w}-1:0]   src_vec,
                  input  logic [{w}-1:0]   chain_in,
                  output logic [{w}-1:0]   chain_out,
                  {arr_out}
                );
                """
            ).strip(),
            "",
            "",
            "",
        )
    if style == "nonansi_body":
        return (
            f"module pw_zig_{{set}}_{lvl} (clk, rst_n);\n",
            textwrap.dedent(
                f"""
                input  logic [{w}-1:0] src_vec;
                input  logic [{w}-1:0] chain_in;
                output logic [{w}-1:0] chain_out;
                {arr_out};
                """
            ).strip(),
            "",
            "",
        )
    if style == "mixed_header_body":
        return (
            "module pw_zig_{set}_" + str(lvl) + " (input logic clk, input logic rst_n);\n",
            textwrap.dedent(
                f"""
                input  wire [{w}-1:0] src_vec;
                input  wire [{w}-1:0] chain_in;
                output wire [{w}-1:0] chain_out;
                {arr_out};
                """
            ).strip(),
            "",
            "",
        )
    if style == "param_vector":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} #(
                  parameter int BW = {w}
                )(
                  input  logic [BW-1:0] src_vec,
                  input  logic [BW-1:0] chain_in,
                  output logic [BW-1:0] chain_out,
                  {arr_out},
                  input  logic clk,
                  input  logic rst_n
                );
                """
            ).strip(),
            "",
            "",
            "",
        )
    if style == "wire_reg_ansi":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} (
                  input wire clk, input wire rst_n,
                  input wire [{w}-1:0] src_vec,
                  input reg [{w}-1:0] chain_in,
                  output wire [{w}-1:0] chain_out,
                  output reg [9:0] arr
                );
                """
            ).strip(),
            "",
            "",
            "",
        )
    if style == "old_port_list":
        return (
            f"module pw_zig_{{set}}_{lvl} ();\n",
            textwrap.dedent(
                f"""
                input clk;
                input rst_n;
                input [{w}-1:0] src_vec;
                input [{w}-1:0] chain_in;
                output [{w}-1:0] chain_out;
                output [9:0] arr;
                """
            ).strip(),
            "",
            "",
        )
    if style == "array_bus":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} (
                  input  logic clk,
                  input  logic rst_n,
                  input  logic [{w}-1:0] src_vec,
                  input  logic [{w}-1:0] chain_in,
                  output logic [{w}-1:0] chain_out,
                  output logic [9:0] bus_arr
                );
                """
            ).strip(),
            "",
            "assign bus_arr = arr;",
            "",
        )
    if style == "attr_ports":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} (
                  (* keep = "true" *) input  logic clk,
                  (* keep = "true" *) input  logic rst_n,
                  input  logic [{w}-1:0] src_vec,
                  input  logic [{w}-1:0] chain_in,
                  output logic [{w}-1:0] chain_out,
                  {arr_out}
                );
                """
            ).strip(),
            "",
            "",
            "",
        )
    if style == "bracket_dims":
        return (
            textwrap.dedent(
                f"""
                module pw_zig_{{set}}_{lvl} (
                  input  logic clk,
                  input  logic rst_n,
                  input  logic [0:{w}-1] src_vec,
                  input  logic [0:{w}-1] chain_in,
                  output logic [0:{w}-1] chain_out,
                  output logic [0:9] arr
                );
                """
            ).strip(),
            "",
            "",
            "",
        )
    # localparam_width
    return (
        "module pw_zig_{set}_" + str(lvl) + " (input logic clk, input logic rst_n);\n",
        textwrap.dedent(
            f"""
            localparam int LPW = {w};
            input  logic [LPW-1:0] src_vec;
            input  logic [LPW-1:0] chain_in;
            output logic [LPW-1:0] chain_out;
            {arr_out};
            """
        ).strip(),
        "",
        "",
    )


def _decoy_inst(style: str, lvl: int, set_id: str) -> str:
    if style == "comma_chain":
        return textwrap.dedent(
            f"""
              pw_decoy #(.D({lvl})) u_d0 (.clk(clk), .rst_n(rst_n)),
              pw_decoy u_d1 (.clk(clk), .rst_n(rst_n));
            """
        ).strip()
    if style == "array_inst":
        return f"pw_decoy u_darr [0:0] (.clk(clk), .rst_n(rst_n));"
    if style == "gen_if":
        return textwrap.dedent(
            f"""
              generate
                if (LVL >= 0) begin : g_dec_{lvl}
                  pw_decoy u_d_if (.clk(clk), .rst_n(rst_n));
                end
              endgenerate
            """
        ).strip()
    if style == "gen_for":
        return textwrap.dedent(
            f"""
              generate
                for (genvar gi = 0; gi < 1; gi++) begin : g_df_{lvl}
                  pw_decoy u_d_for (.clk(clk), .rst_n(rst_n));
                end
              endgenerate
            """
        ).strip()
    if style == "ifdef_wrap":
        return textwrap.dedent(
            f"""
              `ifdef PW_STRESS_INST
                pw_decoy u_d_ifdef (.clk(clk), .rst_n(rst_n));
              `endif
            """
        ).strip()
    if style == "attr_inst":
        return (
            f'(* keep_hierarchy = "yes" *) pw_decoy u_d_attr (.clk(clk), .rst_n(rst_n));'
        )
    if style == "param_override":
        return (
            f"pw_decoy #(.D({lvl})) u_d_p (.clk(clk), .rst_n(rst_n));"
        )
    if style == "multiline_hash":
        return textwrap.dedent(
            f"""
              pw_decoy #(
                .D( {lvl} )
              ) u_d_mh (.clk(clk), .rst_n(rst_n));
            """
        ).strip()
    return f"pw_decoy u_d_plain (.clk(clk), .rst_n(rst_n));"


def _main_child(lvl: int, set_id: str, leaf: bool) -> str:
    if leaf:
        return ""
    child = f"pw_zig_{set_id}_{lvl + 1}"
    ovr = f"#(.LVL({lvl + 1})) " if lvl % 3 == 0 else ""
    link_ports = ""
    if _is_cross_arm(set_id) and lvl < BRIDGE_LEVEL:
        link_ports = textwrap.dedent(
            """
            ,
            .link_in(link_in),
            .link_out(link_out)
            """
        ).strip()
    chain_net = "chain_child" if (_is_cross_arm(set_id) and lvl == BRIDGE_LEVEL) else "chain_vec"
    return textwrap.dedent(
        f"""
          {child} {ovr}u_next (
            .clk(clk),
            .rst_n(rst_n),
            .src_vec(src_vec),
            .chain_in({chain_net}),
            .chain_out(chain_out),
            .arr(arr){link_ports}
          );
        """
    ).strip()


def _level_body(set_id: str, lvl: int) -> str:
    leaf = lvl == DEPTH - 1
    port_style = _PORT_STYLES[lvl % len(_PORT_STYLES)]
    decoy_style = _INST_DECOY_STYLES[lvl % len(_INST_DECOY_STYLES)]
    drive_style = _DRIVE_STYLES[lvl % len(_DRIVE_STYLES)]
    mod_open, body_decls, arr_alias, _ = _port_block(port_style, lvl)
    mod_open = mod_open.replace("{set}", set_id)
    cross_link_level = _is_cross_arm(set_id) and lvl <= BRIDGE_LEVEL
    if cross_link_level:
        port_style = "ansi_full"
        mod_open, body_decls, arr_alias, _ = _port_block(port_style, lvl)
        mod_open = mod_open.replace("{set}", set_id)
        link_ansi = ",\n          " + _link_port_decls().replace("\n", "\n          ")
        if "\n        );" in mod_open:
            mod_open = mod_open.replace("\n        );", link_ansi + "\n        );", 1)
        else:
            link_body = _link_port_decls().replace(",", ";\n          ") + ";"
            body_decls = (body_decls + "\n          " + link_body) if body_decls else link_body
    zig_pre = ""
    if lvl % 2 == 1:
        zig_pre = textwrap.dedent(
            f"""
              logic [9:0] zig_pre;
              pw_zig_ping_{set_id}_{lvl} u_ping (
                .clk(clk),
                .rst_n(rst_n),
                .din(chain_in[9:0]),
                .dout(zig_pre)
              );
            """
        ).strip()
        chain_src = "zig_pre"
    else:
        chain_src = "chain_in[9:0]"
    drive = _drive_chain_body(chain_src)
    noise_drv = _drive_noise_body(drive_style, chain_src, lvl)
    similar = _similar_name_trap(lvl) if lvl % 3 == 1 else ""
    link_merge = ""
    if cross_link_level and lvl == BRIDGE_LEVEL:
        hi = BRIDGE_LEVEL + 1
        lo = BRIDGE_LEVEL
        link_merge = textwrap.dedent(
            f"""
              logic [9:0] chain_vec;
              logic [9:0] chain_child;
              assign link_out = chain_local;
              assign chain_vec = chain_local;
              assign chain_child = {{chain_local[9:{hi}], link_in[{lo}], chain_local[{lo}-1:0]}};
            """
        ).strip()
    elif cross_link_level:
        link_merge = textwrap.dedent(
            """
              logic [9:0] chain_vec;
              assign chain_vec = chain_local;
            """
        ).strip()
    else:
        link_merge = textwrap.dedent(
            """
              logic [9:0] chain_vec;
              assign chain_vec = chain_local;
            """
        ).strip()
    src_inject = f"assign chain_local[{lvl}] = src_vec[{lvl}];"
    if leaf:
        arr_drv = textwrap.dedent(
            f"""
              {drive}
              {src_inject}
              {link_merge}
              assign arr = chain_vec[9:0];
              assign chain_out = chain_vec;
            """
        ).strip()
        child = ""
    else:
        arr_drv = textwrap.dedent(
            f"""
              {drive}
              {src_inject}
              {link_merge}
              assign chain_out = chain_vec;
            """
        ).strip()
        child = _main_child(lvl, set_id, leaf)
        arr_drv += "\n          assign arr = u_next.arr;"
    decoy = _decoy_inst(decoy_style, lvl, set_id)
    noise = textwrap.dedent(
        f"""
        `define PW_SET_{set_id}_LVL_{lvl} 1
        `ifdef PW_STRESS_{set_id}
        `endif
        """
    ).strip()
    parts = [
        noise,
        mod_open,
        body_decls,
        zig_pre,
        similar,
        noise_drv,
        arr_drv,
        arr_alias,
        child,
        decoy,
        "endmodule",
    ]
    return "\n          ".join(p for p in parts if p)


def _set_wrapper(set_id: str) -> str:
    link_hdr = ""
    link_wire = ""
    link_conn = ""
    if _is_cross_arm(set_id):
        link_hdr = _link_port_decls() + "\n          "
        link_wire = "logic [9:0] link_drop;\n          "
        link_conn = textwrap.dedent(
            """
            ,
            .link_in(link_in),
            .link_out(link_out)
            """
        ).strip()
    return textwrap.dedent(
        f"""
        module pw_set_{set_id} (
          input  logic        clk,
          input  logic        rst_n,
          input  logic [9:0]  src_vec,
          output logic [9:0]  arr_view,
          {link_hdr}
        );
          logic [9:0] chain_zero;
          assign chain_zero = 10'b0;
          logic [9:0] chain_drop;
          {link_wire}pw_zig_{set_id}_0 u_next (
            .clk(clk),
            .rst_n(rst_n),
            .src_vec(src_vec),
            .chain_in(chain_zero),
            .chain_out(chain_drop),
            .arr(arr_view){link_conn}
          );
        endmodule
        """
    ).strip()


def _top_src_vec(set_id: str) -> str:
    if set_id not in CROSS_ARMS:
        return "{ " + ", ".join(f"set{set_id}_a{j}" for j in range(BUS_WIDTH)) + " }"
    peer = CROSS_ARMS[1] if set_id == CROSS_ARMS[0] else CROSS_ARMS[0]
    bits: List[str] = []
    for j in range(BUS_WIDTH):
        if j == BRIDGE_LEVEL:
            bits.append(f"set{peer}_a{j}")
        else:
            bits.append(f"set{set_id}_a{j}")
    return "{ " + ", ".join(bits) + " }"


def _top_module() -> str:
    ports: List[str] = ["input logic clk", "input logic rst_n"]
    decls: List[str] = []
    insts: List[str] = []
    for sid in SET_IDS:
        for i in range(BUS_WIDTH):
            ports.append(f"input logic set{sid}_a{i}")
        link_conn = ""
        src_vec = _top_src_vec(sid)
        if _is_cross_arm(sid):
            decls.append(f"wire [9:0] cross_{sid.lower()}_out;")
            decls.append(f"wire [9:0] cross_{sid.lower()}_in;")
            link_conn = textwrap.dedent(
                f"""
                ,
                .link_out(cross_{sid.lower()}_out),
                .link_in(cross_{sid.lower()}_in)
                """
            ).strip()
        insts.append(
            textwrap.dedent(
                f"""
                pw_set_{sid} u_set_{sid} (
                  .clk(clk),
                  .rst_n(rst_n),
                  .src_vec({src_vec}){link_conn}
                );
                """
            ).strip()
        )
    bridge = ""
    if len(CROSS_ARMS) == 2:
        bridge = textwrap.dedent(
            """
            pw_bridge u_cross_bridge (
              .clk(clk),
              .rst_n(rst_n),
              .from_a(cross_a_out),
              .from_b(cross_b_out),
              .to_a(cross_a_in),
              .to_b(cross_b_in)
            );
            pw_clutter_noise #(.IDX(0)) u_clutter_top (.clk(clk), .rst_n(rst_n));
            """
        ).strip()
    decl_block = "\n          ".join(decls)
    return textwrap.dedent(
        f"""
        `define PW_STRESS_TOP 1
        `define PW_STRESS_INST 1
        `define PW_STRESS_A 1
        `define PW_STRESS_B 1
        `define PW_STRESS_C 1
        `define PW_STRESS_D 1
        `define PW_STRESS_CLUTTER 1
        module pw_top (
          {", ".join(ports)}
        );
          {decl_block}
          {chr(10).join(insts)}
          {bridge}
        endmodule
        """
    ).strip()


def _decoy_module() -> str:
    return textwrap.dedent(
        """
        module pw_decoy #(parameter int D = 0) (
          input logic clk,
          input logic rst_n
        );
          wire noise;
          assign noise = clk ^ rst_n ^ D;
        endmodule
        """
    ).strip()


def _ping_module(set_id: str, lvl: int) -> str:
    return textwrap.dedent(
        f"""
        module pw_zig_ping_{set_id}_{lvl} (
          input  logic clk,
          input  logic rst_n,
          input  logic [9:0] din,
          output logic [9:0] dout
        );
          assign dout = din;
        endmodule
        """
    ).strip()


def _build_checks() -> Tuple[ConnectivityCheck, ...]:
    checks: List[ConnectivityCheck] = []
    for sid in SET_IDS:
        base = _leaf_path(sid)
        for bit in range(BUS_WIDTH):
            if sid in CROSS_ARMS and bit == BRIDGE_LEVEL:
                continue
            checks.append(
                ConnectivityCheck(
                    f"{base}.arr[{bit}]",
                    f"pw_top.set{sid}_a{bit}",
                    f"{sid}_b{bit}",
                )
            )
    if len(CROSS_ARMS) == 2:
        arm_a, arm_b = CROSS_ARMS
        leaf_a = _leaf_path(arm_a)
        leaf_b = _leaf_path(arm_b)
        bit = BRIDGE_LEVEL
        checks.append(
            ConnectivityCheck(
                f"pw_top.set{arm_a}_a{bit}",
                f"{leaf_b}.arr[{bit}]",
                f"cross_port_{arm_a}_to_{arm_b}",
            )
        )
        checks.append(
            ConnectivityCheck(
                f"pw_top.set{arm_b}_a{bit}",
                f"{leaf_a}.arr[{bit}]",
                f"cross_{arm_a}{arm_b}_rev{bit}",
            )
        )
    return tuple(checks)


def generate_path_walk_stress_design() -> PathWalkStressDesign:
    files: Dict[str, str] = {
        "pw_decoy.v": _decoy_module(),
        "pw_bridge.v": _bridge_module(),
        "pw_top.v": _top_module(),
    }
    files.update(_clutter_modules())
    for sid in SET_IDS:
        for lvl in range(DEPTH):
            files[f"pw_zig_{sid}_{lvl}.v"] = _level_body(sid, lvl)
            if lvl % 2 == 1:
                files[f"pw_zig_ping_{sid}_{lvl}.v"] = _ping_module(sid, lvl)
        files[f"pw_set_{sid}.v"] = _set_wrapper(sid)
    return PathWalkStressDesign(
        files=files,
        top="pw_top",
        depth=DEPTH,
        bus_width=BUS_WIDTH,
        sets=SET_IDS,
        checks=_build_checks(),
    )


def build_connect_request(design: PathWalkStressDesign) -> ConnectivityRequest:
    return ConnectivityRequest(
        checks=design.checks,
        top=design.top,
        include_ff=False,
        defines={
            "PW_STRESS_INST": "1",
            "PW_STRESS_TOP": "1",
            "PW_STRESS_A": "1",
            "PW_STRESS_B": "1",
            "PW_STRESS_C": "1",
            "PW_STRESS_D": "1",
            "PW_STRESS_CLUTTER": "1",
        },
    )


def write_stress_artifacts(root: Path) -> Tuple[Path, Path, PathWalkStressDesign]:
    design = generate_path_walk_stress_design()
    root.mkdir(parents=True, exist_ok=True)
    for name, text in design.files.items():
        (root / name).write_text(text + "\n", encoding="utf-8")
    fl = root / "filelist.f"
    fl.write_text(
        "\n".join(str((root / n).resolve()) for n in sorted(design.files)) + "\n",
        encoding="utf-8",
    )
    req_path = root / "pw_stress.connect.json"
    from hierwalk.connect.shared.request import write_connect_request

    write_connect_request(req_path, build_connect_request(design))
    return fl, req_path, design


def main() -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Generate path-walk stress RTL (4×10×10)")
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
    run_path = args.out_dir / "pw_stress.run.json"
    run_path.write_text(json.dumps(run_json, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(design.files)} RTL files, {len(design.checks)} checks", file=sys.stderr)
    print(f"  filelist: {fl}", file=sys.stderr)
    print(f"  connect:  {req_path}", file=sys.stderr)
    print(f"  run:      {run_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())