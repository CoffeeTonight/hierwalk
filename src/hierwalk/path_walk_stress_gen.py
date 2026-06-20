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

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

SET_IDS: Tuple[str, ...] = ("A", "B", "C", "D")
DEPTH: int = 10
BUS_WIDTH: int = 10

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
    return textwrap.dedent(
        f"""
          {child} {ovr}u_next (
            .clk(clk),
            .rst_n(rst_n),
            .src_vec(src_vec),
            .chain_in(chain_vec),
            .chain_out(chain_out),
            .arr(arr)
          );
        """
    ).strip()


def _level_body(set_id: str, lvl: int) -> str:
    leaf = lvl == DEPTH - 1
    port_style = _PORT_STYLES[lvl % len(_PORT_STYLES)]
    decoy_style = _INST_DECOY_STYLES[lvl % len(_INST_DECOY_STYLES)]
    mod_open, body_decls, arr_alias, _ = _port_block(port_style, lvl)
    mod_open = mod_open.replace("{set}", set_id)
    zig_pre = ""
    zig_post = ""
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
    if leaf:
        arr_drv = textwrap.dedent(
            f"""
              logic [9:0] chain_vec;
              assign chain_vec = {chain_src};
              assign chain_vec[{lvl}] = src_vec[{lvl}];
              assign arr = chain_vec[9:0];
              assign chain_out = chain_vec;
            """
        ).strip()
        child = ""
    else:
        arr_drv = textwrap.dedent(
            f"""
              logic [9:0] chain_vec;
              assign chain_vec = {chain_src};
              assign chain_vec[{lvl}] = src_vec[{lvl}];
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
        arr_drv,
        arr_alias,
        child,
        decoy,
        "endmodule",
    ]
    return "\n          ".join(p for p in parts if p)


def _set_wrapper(set_id: str) -> str:
    return textwrap.dedent(
        f"""
        module pw_set_{set_id} (
          input  logic        clk,
          input  logic        rst_n,
          input  logic [9:0]  src_vec,
          output logic [9:0]  arr_view
        );
          logic [9:0] chain_zero;
          assign chain_zero = 10'b0;
          logic [9:0] chain_drop;
          pw_zig_{set_id}_0 u_next (
            .clk(clk),
            .rst_n(rst_n),
            .src_vec(src_vec),
            .chain_in(chain_zero),
            .chain_out(chain_drop),
            .arr(arr_view)
          );
        endmodule
        """
    ).strip()


def _top_module() -> str:
    ports: List[str] = ["input logic clk", "input logic rst_n"]
    insts: List[str] = []
    for sid in SET_IDS:
        for i in range(BUS_WIDTH):
            ports.append(f"input logic set{sid}_a{i}")
        insts.append(
            textwrap.dedent(
                f"""
                pw_set_{sid} u_set_{sid} (
                  .clk(clk),
                  .rst_n(rst_n),
                  .src_vec({{ {", ".join(f"set{sid}_a{j}" for j in range(BUS_WIDTH))} }})
                );
                """
            ).strip()
        )
    return textwrap.dedent(
        f"""
        `define PW_STRESS_TOP 1
        `define PW_STRESS_INST 1
        `define PW_STRESS_A 1
        `define PW_STRESS_B 1
        `define PW_STRESS_C 1
        `define PW_STRESS_D 1
        module pw_top (
          {", ".join(ports)}
        );
          {chr(10).join(insts)}
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
            checks.append(
                ConnectivityCheck(
                    f"{base}.arr[{bit}]",
                    f"pw_top.set{sid}_a{bit}",
                    f"{sid}_b{bit}",
                )
            )
    return tuple(checks)


def generate_path_walk_stress_design() -> PathWalkStressDesign:
    files: Dict[str, str] = {
        "pw_decoy.v": _decoy_module(),
        "pw_top.v": _top_module(),
    }
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
        defines={
            "PW_STRESS_INST": "1",
            "PW_STRESS_TOP": "1",
            "PW_STRESS_A": "1",
            "PW_STRESS_B": "1",
            "PW_STRESS_C": "1",
            "PW_STRESS_D": "1",
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
    from hierwalk.connect_request import write_connect_request

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