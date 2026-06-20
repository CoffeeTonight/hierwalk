"""Randomized deep-hierarchy RTL for connectivity stress tests."""

from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hierwalk.connect_request import (
    ConnectivityCheck,
    ConnectivityRequest,
    connect_request_to_json,
    write_connect_request,
)
from hierwalk.run_request import RunConfig, write_run_request


_CONSTRUCT_NAMES = (
    "assign",
    "always_ff",
    "wire_alias",
    "generate_for",
    "case_ff",
    "comb_if",
    "ifdef",
    "if_generate",
    "nested_ifdef",
    "concat_replicate",
    "case_comb",
    "double_ff",
    "gen_nested",
    "ternary_assign",
    "chained_alias",
    "param_ifgenerate",
    "indexed_part",
    "fanout_noise",
    "casex_ff",
    "casez_ff",
    "casex_comb",
    "casez_comb",
    "mdarray_hop",
    "for_array_fill",
    "param_expr_mux",
)


@dataclass(frozen=True)
class StressConfig:
    """Tunable stress profile (defaults: depth~20, branch~8, zigzag cross-hierarchy)."""

    depth_base: int = 20
    depth_jitter: int = 3
    branch_base: int = 8
    branch_jitter: int = 2
    min_depth: int = 12
    multi_file: bool = True
    shuffle_constructs: bool = True
    decoy_arrays: bool = True
    param_child_overrides: bool = True
    zigzag: bool = True
    zigzag_rungs: Optional[int] = None
    tunnel_depth: Optional[int] = None

    @classmethod
    def standard(cls) -> StressConfig:
        return cls(
            depth_base=10,
            depth_jitter=2,
            branch_base=5,
            branch_jitter=0,
            min_depth=3,
            multi_file=False,
            shuffle_constructs=False,
            decoy_arrays=False,
            param_child_overrides=False,
            zigzag=False,
        )

    @classmethod
    def extreme(cls) -> StressConfig:
        return cls()


EXTREME_CONFIG = StressConfig.extreme()
STANDARD_CONFIG = StressConfig.standard()


@dataclass(frozen=True)
class StressDesign:
    """Generated RTL bundle for one connectivity stress trial."""

    verilog: str
    files: Dict[str, str]
    top: str
    endpoint_port_port: Tuple[str, str]
    endpoint_port_inst: Tuple[str, str]
    endpoint_cross: Tuple[str, str]
    depth: int
    branch_factor: int
    seed: int
    spine_path: str
    construct_schedule: Tuple[str, ...]
    defines: Dict[str, str]
    layout: str
    num_rungs: int
    tunnel_depth: int
    config: StressConfig = EXTREME_CONFIG

    @property
    def filename(self) -> str:
        return f"stress_{self.seed}_d{self.depth}.v"

    @property
    def endpoint_a(self) -> str:
        return self.endpoint_port_port[0]

    @property
    def endpoint_b(self) -> str:
        return self.endpoint_port_port[1]


def build_stress_run_config(design: StressDesign) -> RunConfig:
    """Full hier-walk run JSON for a generated stress design."""
    import json

    connect_req = build_stress_connect_request(design)
    connect_inline = json.loads(connect_request_to_json(connect_req))
    connect_inline.pop("top", None)
    connect_inline.pop("defines", None)
    return RunConfig(
        filelist="filelist.f",
        top=design.top,
        defines=tuple(connect_req.defines.items()),
        include_ff=connect_req.include_ff,
        connect_trace=connect_req.trace,
        strict_generate=connect_req.strict_generate,
        over_approximate_if=connect_req.over_approximate_if,
        connect_inline=connect_inline,
        no_cache=True,
        output="-",
    )


def build_stress_connect_request(design: StressDesign) -> ConnectivityRequest:
    """JSON connectivity request matching a generated stress design."""
    pp_a, pp_b = design.endpoint_port_port
    pi_a, pi_b = design.endpoint_port_inst
    cx_a, cx_b = design.endpoint_cross
    return ConnectivityRequest(
        top=design.top,
        defines=dict(design.defines),
        include_ff=True,
        trace=False,
        checks=(
            ConnectivityCheck(pp_a, pp_b, check_id="port_port"),
            ConnectivityCheck(pi_a, pi_b, check_id="port_inst"),
            ConnectivityCheck(cx_a, cx_b, check_id="cross_hierarchy"),
            ConnectivityCheck(
                f"{design.top}.u_missing.probe_in",
                pp_b,
                check_id="missing_hierarchy",
            ),
        ),
    )


def write_stress_artifacts(
    design: StressDesign,
    out_dir: Union[str, "Path"],
) -> Dict[str, str]:
    """
    Write RTL file(s), filelist, and ``connect.json`` for one stress design.

    Returns map of artifact label -> path written.
    """
    from pathlib import Path

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: Dict[str, str] = {}

    if design.files:
        for rel, text in design.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written[rel] = str(path)
        fl_lines = [str(root / rel) for rel in sorted(design.files)]
    else:
        rtl_path = root / design.filename
        rtl_path.write_text(design.verilog, encoding="utf-8")
        written[design.filename] = str(rtl_path)
        fl_lines = [str(rtl_path)]

    fl_path = root / "filelist.f"
    fl_path.write_text("\n".join(fl_lines) + "\n", encoding="utf-8")
    written["filelist.f"] = str(fl_path)

    req_path = root / f"stress_{design.seed}_d{design.depth}.connect.json"
    write_connect_request(req_path, build_stress_connect_request(design))
    written["connect.json"] = str(req_path)

    run_path = root / f"stress_{design.seed}_d{design.depth}.run.json"
    write_run_request(run_path, build_stress_run_config(design))
    written["run.json"] = str(run_path)
    return written


def _spine_hier(top: str, depth: int) -> str:
    return ".".join([top] + ["u_spine"] * depth)


def _tunnel_hier(base: str, hops: int, child: str = "u_next") -> str:
    if hops <= 0:
        return base
    return ".".join([base] + [child] * hops)


def _zigzag_dims(
    depth: int,
    *,
    cfg: StressConfig,
) -> Tuple[int, int]:
    rungs = cfg.zigzag_rungs or max(4, (depth + 2) // 4)
    td = cfg.tunnel_depth or max(2, max(2, depth // max(1, rungs) // 2))
    return rungs, td


def _param_decl_block(
    *,
    level: int,
    base: int = 2,
    include_tunnel: bool = False,
    tunnel_depth: int = 3,
) -> str:
    stride_expr = "BASE + 2"
    span_expr = "BASE + LEVEL"
    win_expr = f"({span_expr}) - BASE + 1"
    pass_expr = f"({win_expr} > 0) ? 1 : 0"
    lines = [
        f"parameter int BASE = {base}",
        f"parameter int STRIDE = {stride_expr}",
        f"parameter int LEVEL = {level}",
        f"localparam int SPAN = {span_expr}",
        f"localparam int WIN = {win_expr}",
        f"localparam int PASS_THRU = {pass_expr}",
        f"localparam int LP_OFF = BASE * STRIDE + LEVEL",
    ]
    if include_tunnel:
        lines.insert(4, f"parameter int TUNNEL_DEPTH = {tunnel_depth}")
        lines.append("localparam int TAIL = TUNNEL_DEPTH - 1")
    return ";\n  ".join(lines) + ";"


def _schedule_for_levels(
    n_levels: int,
    *,
    rng: random.Random,
    shuffle: bool,
) -> Tuple[str, ...]:
    if n_levels <= 0:
        return ()
    pool = list(_CONSTRUCT_NAMES)
    if shuffle:
        rng.shuffle(pool)
    return tuple(pool[i % len(pool)] for i in range(n_levels))


def _construct_body(kind: str, level: int, *, rng: random.Random) -> str:
    """Drive ``link`` from ``probe_in``."""
    sel = level % 4
    zmask = f"4'b?{sel:01b}?{1 - (sel % 2)}"
    noise = f"logic noise_{level}; assign noise_{level} = probe_in & 1'b0;"
    if kind == "assign":
        return textwrap.dedent(
            f"""
            wire link;
            assign link = probe_in;
            {noise}
            """
        ).strip()
    if kind == "wire_alias":
        return textwrap.dedent(
            f"""
            wire link = probe_in;
            {noise}
            """
        ).strip()
    if kind == "always_ff":
        return textwrap.dedent(
            f"""
            logic link;
            always_ff @(posedge clk) begin
              if (!rst_n)
                link <= 1'b0;
              else
                link <= probe_in;
            end
            {noise}
            """
        ).strip()
    if kind == "generate_for":
        return textwrap.dedent(
            f"""
            wire link;
            generate
              for (genvar gi = 0; gi < 1; gi++) begin : gen_pass_{level}
                assign link = probe_in;
              end
            endgenerate
            """
        ).strip()
    if kind == "case_ff":
        return textwrap.dedent(
            f"""
            logic link;
            logic [1:0] sel_{level};
            assign sel_{level} = 2'b{sel:02b};
            always_ff @(posedge clk) begin
              case (sel_{level})
                2'b00: link <= probe_in;
                2'b01: link <= probe_in;
                2'b10: link <= probe_in;
                default: link <= probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "comb_if":
        return textwrap.dedent(
            f"""
            logic link;
            always_comb begin
              if (en_{level})
                link = probe_in;
              else
                link = 1'b0;
            end
            """
        ).strip()
    if kind == "ifdef":
        return textwrap.dedent(
            """
            wire link;
            `ifdef STRESS_USE_IN
              assign link = probe_in;
            `else
              assign link = 1'b0;
            `endif
            """
        ).strip()
    if kind == "if_generate":
        return textwrap.dedent(
            f"""
            wire link;
            generate
              if (1) begin : ifg_{level}
                assign link = probe_in;
              end
            endgenerate
            """
        ).strip()
    if kind == "nested_ifdef":
        return textwrap.dedent(
            """
            wire link;
            `ifdef STRESS_USE_IN
              `ifdef STRESS_ALT
                assign link = 1'b0;
              `else
                assign link = probe_in;
              `endif
            `else
              assign link = 1'b0;
            `endif
            """
        ).strip()
    if kind == "concat_replicate":
        return textwrap.dedent(
            """
            wire link;
            assign link = {probe_in};
            """
        ).strip()
    if kind == "case_comb":
        return textwrap.dedent(
            f"""
            logic link;
            logic [1:0] sel_{level};
            assign sel_{level} = 2'b{sel:02b};
            always_comb begin
              case (sel_{level})
                2'b00, 2'b01: link = probe_in;
                2'b10, 2'b11: link = probe_in;
                default: link = probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "double_ff":
        return textwrap.dedent(
            """
            logic mid_q, link;
            always_ff @(posedge clk) begin
              if (!rst_n)
                mid_q <= 1'b0;
              else
                mid_q <= probe_in;
            end
            always_ff @(posedge clk) begin
              if (!rst_n)
                link <= 1'b0;
              else
                link <= mid_q;
            end
            """
        ).strip()
    if kind == "gen_nested":
        return textwrap.dedent(
            f"""
            wire link;
            generate
              for (genvar gi = 0; gi < 1; gi++) begin : gn_{level}
                if (PASS_THRU) begin : gn_if_{level}
                  assign link = probe_in;
                end
              end
            endgenerate
            """
        ).strip()
    if kind == "ternary_assign":
        return textwrap.dedent(
            f"""
            wire link;
            assign link = en_{level} ? probe_in : 1'b0;
            """
        ).strip()
    if kind == "chained_alias":
        return textwrap.dedent(
            f"""
            wire hop_a_{level}, hop_b_{level}, link;
            assign hop_a_{level} = probe_in;
            assign hop_b_{level} = hop_a_{level};
            assign link = hop_b_{level};
            """
        ).strip()
    if kind == "param_ifgenerate":
        return textwrap.dedent(
            f"""
            wire link;
            generate
              if (PASS_THRU) begin : pig_{level}
                assign link = probe_in;
              end
            endgenerate
            """
        ).strip()
    if kind == "indexed_part":
        return textwrap.dedent(
            """
            wire link;
            assign link = {1'b0, probe_in}[0];
            """
        ).strip()
    if kind == "fanout_noise":
        return textwrap.dedent(
            f"""
            wire link, shadow_{level};
            assign shadow_{level} = probe_in;
            assign link = probe_in;
            """
        ).strip()
    if kind == "casex_ff":
        return textwrap.dedent(
            f"""
            logic link;
            logic [3:0] key_{level};
            assign key_{level} = {zmask};
            always_ff @(posedge clk) begin
              casex (key_{level})
                4'b??1?: link <= probe_in;
                4'b???0: link <= probe_in;
                default: link <= probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "casez_ff":
        return textwrap.dedent(
            f"""
            logic link;
            logic [3:0] key_{level};
            assign key_{level} = 4'b{sel:04b};
            always_ff @(posedge clk) begin
              casez (key_{level})
                4'b1???: link <= probe_in;
                4'b0???: link <= probe_in;
                default: link <= probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "casex_comb":
        return textwrap.dedent(
            f"""
            logic link;
            logic [3:0] key_{level};
            assign key_{level} = {zmask};
            always_comb begin
              casex (key_{level})
                4'b??1?: link = probe_in;
                default: link = probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "casez_comb":
        return textwrap.dedent(
            f"""
            logic link;
            logic [3:0] key_{level};
            assign key_{level} = 4'b{sel:04b};
            always_comb begin
              casez (key_{level})
                4'b1???: link = probe_in;
                default: link = probe_in;
              endcase
            end
            """
        ).strip()
    if kind == "mdarray_hop":
        return textwrap.dedent(
            f"""
            logic [1:0][2:0] hop_arr_{level};
            logic [1:0] row_{level};
            assign row_{level} = LEVEL[1:0];
            assign hop_arr_{level}[0][0] = probe_in;
            assign hop_arr_{level}[1][row_{level}[1:0]] = hop_arr_{level}[0][0];
            wire link;
            assign link = hop_arr_{level}[1][0];
            """
        ).strip()
    if kind == "for_array_fill":
        return textwrap.dedent(
            f"""
            logic [1:0][1:0] arr_{level};
            integer ji_{level};
            always_comb begin
              for (ji_{level} = 0; ji_{level} < 1; ji_{level} = ji_{level} + 1)
                arr_{level}[ji_{level}][0] = probe_in;
            end
            wire link;
            assign link = arr_{level}[0][0];
            """
        ).strip()
    if kind == "param_expr_mux":
        return textwrap.dedent(
            f"""
            wire link;
            generate
              if (WIN > 0) begin : pem_{level}
                assign link = (PASS_THRU != 0) ? probe_in : 1'b0;
              end
            endgenerate
            """
        ).strip()
    return "wire link; assign link = probe_in;"


def _decoy_instances(
    branch_factor: int,
    level: int,
    *,
    comma_chain: bool,
    use_arrays: bool,
    rng: random.Random,
) -> str:
    n_decoy = branch_factor - 1
    if n_decoy <= 0:
        return ""

    lines: List[str] = []
    remaining = n_decoy
    arr_size = min(3, max(1, remaining // 2)) if use_arrays and remaining >= 2 else 0

    if arr_size >= 2:
        lines.append(
            f"  stress_decoy u_d{level}_arr [0:{arr_size - 1}] "
            f"(.clk(clk), .rst_n(rst_n), .noise());"
        )
        remaining -= arr_size

    if remaining >= 2 and comma_chain:
        lines.append(
            f"  stress_decoy u_d{level}_0 (.clk(clk), .rst_n(rst_n), .noise()),\n"
            f"             u_d{level}_1 (.clk(clk), .rst_n(rst_n), .noise());"
        )
        remaining -= 2
    elif remaining >= 1:
        lines.append(
            f"  stress_decoy u_d{level}_0 (.clk(clk), .rst_n(rst_n), .noise());"
        )
        remaining -= 1

    for j in range(remaining):
        idx = arr_size + 2 + j
        lines.append(
            f"  stress_decoy u_d{level}_{idx} (.clk(clk), .rst_n(rst_n), .noise());"
        )

    if level % 3 == 0:
        lines.append(
            f"  stress_decoy #(.DECOY_ID({level})) "
            f"u_d{level}_p (.clk(clk), .rst_n(rst_n), .noise());"
        )
    return "\n".join(lines)


def _child_override(level: int, *, enabled: bool) -> str:
    if not enabled:
        return ""
    return f"#(.BASE(BASE), .STRIDE(STRIDE), .LEVEL({level})) "


# --- linear spine (standard profile) ---


def _spine_module(
    level: int,
    depth: int,
    branch_factor: int,
    construct: str,
    *,
    rng: random.Random,
    config: StressConfig,
) -> str:
    has_child = level + 1 < depth - 1
    child_mod = (
        f"stress_spine_{level + 1}" if level + 1 < depth - 1 else "stress_leaf"
    )
    decoys = _decoy_instances(
        branch_factor,
        level,
        comma_chain=level % 2 == 0,
        use_arrays=config.decoy_arrays,
        rng=rng,
    )
    body = _construct_body(construct, level, rng=rng)
    child_ovr = _child_override(level + 1, enabled=config.param_child_overrides)
    params = _param_decl_block(level=level)

    ports = textwrap.dedent(
        f"""
        input  logic clk,
        input  logic rst_n,
        input  logic en_{level},
        input  logic probe_in,
        output logic probe_out
        """
    ).strip()

    if has_child:
        en_conn = (
            f".en_{level + 1}(en_{level}),\n      "
            if child_mod.startswith("stress_spine_")
            else ""
        )
        child = textwrap.dedent(
            f"""
            {child_mod} {child_ovr}u_spine (
              .clk(clk),
              .rst_n(rst_n),
              {en_conn}.probe_in(link),
              .probe_out(probe_out)
            );
            """
        ).strip()
    else:
        child = textwrap.dedent(
            f"""
            stress_leaf {child_ovr}u_spine (
              .clk(clk),
              .rst_n(rst_n),
              .probe_in(link),
              .probe_out(probe_out)
            );
            """
        ).strip()

    return textwrap.dedent(
        f"""
        module stress_spine_{level} #(
          {params}
        )(
        {ports}
        );
          {body}
        {child}
        {decoys}
        endmodule
        """
    ).strip()


# --- zigzag: ping/pong siblings + tunnel chains ---


def _tunnel_module(
    tlevel: int,
    tunnel_depth: int,
    branch_factor: int,
    construct: str,
    *,
    rng: random.Random,
    config: StressConfig,
) -> str:
    has_child = tlevel + 1 < tunnel_depth - 1
    child_mod = (
        f"stress_tunnel_{tlevel + 1}"
        if tlevel + 1 < tunnel_depth - 1
        else "stress_leaf"
    )
    decoys = _decoy_instances(
        max(3, branch_factor // 2),
        100 + tlevel,
        comma_chain=tlevel % 2 == 0,
        use_arrays=config.decoy_arrays,
        rng=rng,
    )
    body = _construct_body(construct, tlevel, rng=rng)
    child_ovr = _child_override(tlevel + 1, enabled=config.param_child_overrides)
    params = _param_decl_block(
        level=tlevel, include_tunnel=True, tunnel_depth=tunnel_depth
    )

    ports = textwrap.dedent(
        f"""
        input  logic clk,
        input  logic rst_n,
        input  logic en_{tlevel},
        input  logic probe_in,
        output logic probe_out
        """
    ).strip()

    if has_child:
        en_conn = (
            f".en_{tlevel + 1}(en_{tlevel}),\n      "
            if child_mod.startswith("stress_tunnel_")
            else ""
        )
        child = textwrap.dedent(
            f"""
            {child_mod} {child_ovr}u_next (
              .clk(clk),
              .rst_n(rst_n),
              {en_conn}.probe_in(link),
              .probe_out(probe_out)
            );
            """
        ).strip()
    else:
        child = textwrap.dedent(
            f"""
            stress_leaf {child_ovr}u_next (
              .clk(clk),
              .rst_n(rst_n),
              .probe_in(link),
              .probe_out(probe_out)
            );
            """
        ).strip()

    return textwrap.dedent(
        f"""
        module stress_tunnel_{tlevel} #(
          {params}
        )(
        {ports}
        );
          {body}
        {child}
        {decoys}
        endmodule
        """
    ).strip()


def _zigzag_rung_module(
    branch_factor: int,
    tunnel_depth: int,
    *,
    config: StressConfig,
) -> str:
    decoys = _decoy_instances(
        branch_factor,
        200,
        comma_chain=True,
        use_arrays=config.decoy_arrays,
        rng=random.Random(7),
    )
    params = _param_decl_block(level=0, include_tunnel=True, tunnel_depth=tunnel_depth)
    return textwrap.dedent(
        f"""
        module stress_rung #(
          {params}
          parameter int RUNG_ID = 0
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic probe_in,
          output logic probe_out
        );
          localparam int EN_BIT = (RUNG_ID + PASS_THRU) % 2;
          wire xfer;
          stress_ping #(
            .BASE(BASE),
            .STRIDE(STRIDE),
            .LEVEL(BASE + RUNG_ID * STRIDE),
            .TUNNEL_DEPTH(TUNNEL_DEPTH)
          ) u_ping (
            .clk(clk),
            .rst_n(rst_n),
            .en_0(EN_BIT),
            .probe_in(probe_in),
            .probe_out(xfer)
          );
          stress_pong #(
            .BASE(BASE),
            .STRIDE(STRIDE),
            .LEVEL(BASE + RUNG_ID * STRIDE + TUNNEL_DEPTH),
            .TUNNEL_DEPTH(TUNNEL_DEPTH)
          ) u_pong (
            .clk(clk),
            .rst_n(rst_n),
            .en_0(1'b1),
            .probe_in(xfer),
            .probe_out(probe_out)
          );
        {decoys}
        endmodule
        """
    ).strip()


def _build_ping_pong(
    kind: str,
    construct: str,
    level: int,
    *,
    tunnel_depth: int,
) -> str:
    body = _construct_body(construct, level, rng=random.Random(level))
    params = _param_decl_block(
        level=level, include_tunnel=True, tunnel_depth=tunnel_depth
    )
    return textwrap.dedent(
        f"""
        module stress_{kind} #(
          {params}
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic en_0,
          input  logic probe_in,
          output logic probe_out
        );
          wire link, chain_out;
          {body}
          stress_tunnel_0 #(
            .BASE(BASE),
            .STRIDE(STRIDE),
            .LEVEL(LEVEL + 1),
            .TUNNEL_DEPTH(TUNNEL_DEPTH)
          ) u_tunnel0 (
            .clk(clk),
            .rst_n(rst_n),
            .en_0(en_0),
            .probe_in(link),
            .probe_out(chain_out)
          );
          assign probe_out = chain_out;
        endmodule
        """
    ).strip()


def _zigzag_top_module(
    num_rungs: int,
    tunnel_depth: int,
    branch_factor: int,
    *,
    config: StressConfig,
) -> str:
    decoys = _decoy_instances(
        branch_factor,
        99,
        comma_chain=True,
        use_arrays=config.decoy_arrays,
        rng=random.Random(0),
    )
    hop_decls = "\n  ".join(
        f"wire hop_{i};" for i in range(num_rungs + 1)
    )
    rung_insts = "\n".join(
        textwrap.dedent(
            f"""
            stress_rung #(
              .BASE(BASE),
              .STRIDE(STRIDE),
              .TUNNEL_DEPTH({tunnel_depth}),
              .RUNG_ID({ri})
            ) u_rung{ri} (
              .clk(clk),
              .rst_n(rst_n),
              .probe_in(hop_{ri}),
              .probe_out(hop_{ri + 1})
            );
            """
        ).strip()
        for ri in range(num_rungs)
    )
    return textwrap.dedent(
        f"""
        module stress_top #(
          parameter int BASE = 3,
          parameter int STRIDE = BASE + 2,
          parameter int LEG_STEP = STRIDE - 1,
          parameter int TUNNEL_DEPTH = {tunnel_depth},
          parameter int NUM_RUNGS = {num_rungs},
          parameter int RUNG_BASE = BASE * STRIDE
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic probe_in,
          output logic probe_out
        );
          {hop_decls}
          assign hop_0 = probe_in;
          {rung_insts}
          assign probe_out = hop_{num_rungs};
        {decoys}
        endmodule
        """
    ).strip()


def _leaf_module() -> str:
    return textwrap.dedent(
        """
        module stress_leaf #(
          parameter int BASE = 2,
          parameter int STRIDE = BASE + 2,
          parameter int LEVEL = 0,
          parameter int PASS_THRU = 1
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic probe_in,
          output logic probe_out
        );
          logic [1:0][1:0] leaf_arr;
          logic leaf_q;
          localparam int LEAF_LP = BASE * STRIDE + LEVEL;
          localparam int LEAF_IDX = LEAF_LP[1:0];
          `ifdef STRESS_USE_IN
            assign leaf_arr[0][0] = probe_in;
            assign leaf_arr[1][LEAF_IDX] = leaf_arr[0][0];
            always_ff @(posedge clk) begin
              if (!rst_n)
                leaf_q <= 1'b0;
              else
                leaf_q <= leaf_arr[1][LEAF_IDX];
            end
            assign probe_out = {leaf_q};
          `else
            assign probe_out = 1'b0;
          `endif
        endmodule
        """
    ).strip()


def _decoy_module() -> str:
    return textwrap.dedent(
        """
        module stress_decoy #(
          parameter int DECOY_ID = 0,
          parameter int DECOY_BASE = 1
        )(
          input  logic clk,
          input  logic rst_n,
          output logic noise
        );
          localparam int DECOY_SPAN = DECOY_BASE + DECOY_ID;
          logic r;
          logic [3:0] cnt;
          always_ff @(posedge clk) begin
            if (!rst_n) begin
              r <= 1'b0;
              cnt <= DECOY_SPAN[3:0];
            end else begin
              r <= ~r;
              cnt <= cnt + 1'b1;
            end
          end
          assign noise = r ^ ^cnt;
        endmodule
        """
    ).strip()


def _top_module_linear(
    depth: int,
    branch_factor: int,
    *,
    config: StressConfig,
) -> str:
    decoys = _decoy_instances(
        branch_factor,
        99,
        comma_chain=True,
        use_arrays=config.decoy_arrays,
        rng=random.Random(0),
    )
    if depth <= 1:
        spine_inst = textwrap.dedent(
            """
            stress_leaf u_spine (
              .clk(clk),
              .rst_n(rst_n),
              .probe_in(probe_in),
              .probe_out(probe_out)
            );
            """
        ).strip()
    else:
        ovr = _child_override(0, enabled=config.param_child_overrides)
        spine_inst = textwrap.dedent(
            f"""
            stress_spine_0 {ovr}u_spine (
              .clk(clk),
              .rst_n(rst_n),
              .en_0(1'b1),
              .probe_in(probe_in),
              .probe_out(probe_out)
            );
            """
        ).strip()
    return textwrap.dedent(
        f"""
        module stress_top #(
          parameter int BASE = 3,
          parameter int STRIDE = BASE + 2
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic probe_in,
          output logic probe_out
        );
          {spine_inst}
        {decoys}
        endmodule
        """
    ).strip()


def _split_rtl_files(
    chunks: Mapping[str, str],
    *,
    seed: int,
    multi_file: bool,
) -> Dict[str, str]:
    if not multi_file:
        return {f"stress_{seed}_d{chunks['depth']}.v": chunks["single"]}

    files: Dict[str, str] = {}
    files["stress_common.v"] = "\n\n".join(
        [chunks["decoy"], chunks["leaf"]]
    ) + "\n"
    for key, blob in chunks.get("extra_modules", {}).items():
        files[key] = blob + "\n"
    spine_parts = chunks["spine_list"].split("\n\n")
    group = 3
    for i in range(0, len(spine_parts), group):
        blob = "\n\n".join(spine_parts[i : i + group]) + "\n"
        files[f"stress_spine_{i // group}.v"] = blob
    files["stress_top.v"] = chunks["top"] + "\n"
    return files


def _generate_linear(
    *,
    depth: int,
    branch_factor: int,
    seed_val: int,
    rng: random.Random,
    cfg: StressConfig,
) -> StressDesign:
    spine_levels = max(0, depth - 1)
    schedule = _schedule_for_levels(
        spine_levels,
        rng=rng,
        shuffle=cfg.shuffle_constructs,
    )
    spine_chunks = [
        _spine_module(level, depth, branch_factor, schedule[level], rng=rng, config=cfg)
        for level in range(spine_levels)
    ]
    decoy = _decoy_module()
    leaf = _leaf_module()
    top = _top_module_linear(depth, branch_factor, config=cfg)
    single = "\n\n".join([decoy, leaf, *spine_chunks, top]) + "\n"
    files = _split_rtl_files(
        {
            "depth": str(depth),
            "decoy": decoy,
            "leaf": leaf,
            "spine_list": "\n\n".join(spine_chunks),
            "top": top,
            "single": single,
        },
        seed=seed_val,
        multi_file=cfg.multi_file,
    )
    top_name = "stress_top"
    spine = _spine_hier(top_name, depth)
    defines = {"STRESS_USE_IN": "1", "STRESS_ALT": "0"}
    return StressDesign(
        verilog=single,
        files=files,
        top=top_name,
        endpoint_port_port=(f"{top_name}.probe_in", f"{spine}.probe_out"),
        endpoint_port_inst=(f"{top_name}.probe_in", spine),
        endpoint_cross=(f"{top_name}.probe_in", f"{spine}.probe_out"),
        depth=depth,
        branch_factor=branch_factor,
        seed=seed_val,
        spine_path=spine,
        construct_schedule=schedule,
        defines=defines,
        layout="linear",
        num_rungs=0,
        tunnel_depth=0,
        config=cfg,
    )


def _generate_zigzag(
    *,
    depth: int,
    branch_factor: int,
    seed_val: int,
    rng: random.Random,
    cfg: StressConfig,
) -> StressDesign:
    num_rungs, tunnel_depth = _zigzag_dims(depth, cfg=cfg)
    total_levels = num_rungs * 2 * (tunnel_depth + 1)
    schedule = _schedule_for_levels(
        total_levels,
        rng=rng,
        shuffle=cfg.shuffle_constructs,
    )

    decoy = _decoy_module()
    leaf = _leaf_module()
    tunnel_chunks = [
        _tunnel_module(
            tlevel,
            tunnel_depth,
            branch_factor,
            schedule[(tlevel + 1) % len(schedule)],
            rng=rng,
            config=cfg,
        )
        for tlevel in range(max(0, tunnel_depth - 1))
    ]
    ping_sched = schedule[0 % len(schedule)]
    pong_sched = schedule[(tunnel_depth + 1) % len(schedule)]
    ping = _build_ping_pong("ping", ping_sched, 0, tunnel_depth=tunnel_depth)
    pong = _build_ping_pong("pong", pong_sched, tunnel_depth, tunnel_depth=tunnel_depth)
    rung = _zigzag_rung_module(branch_factor, tunnel_depth, config=cfg)
    top = _zigzag_top_module(num_rungs, tunnel_depth, branch_factor, config=cfg)

    spine_parts = [*tunnel_chunks, ping, pong, rung]
    single = "\n\n".join([decoy, leaf, *spine_parts, top]) + "\n"
    extra = {
        "stress_ping.v": ping,
        "stress_pong.v": pong,
        "stress_rung.v": rung,
    }
    files = _split_rtl_files(
        {
            "depth": str(depth),
            "decoy": decoy,
            "leaf": leaf,
            "spine_list": "\n\n".join(spine_parts),
            "top": top,
            "single": single,
            "extra_modules": extra,
        },
        seed=seed_val,
        multi_file=cfg.multi_file,
    )

    top_name = "stress_top"
    mid_rung = num_rungs // 2
    last_rung = num_rungs - 1
    inst_b = f"{top_name}.u_rung{last_rung}.u_pong"
    cross_b = f"{top_name}.u_rung{mid_rung}.u_pong.probe_out"

    defines = {"STRESS_USE_IN": "1", "STRESS_ALT": "0"}
    return StressDesign(
        verilog=single,
        files=files,
        top=top_name,
        endpoint_port_port=(f"{top_name}.probe_in", f"{top_name}.probe_out"),
        endpoint_port_inst=(f"{top_name}.probe_in", inst_b),
        endpoint_cross=(f"{top_name}.probe_in", cross_b),
        depth=depth,
        branch_factor=branch_factor,
        seed=seed_val,
        spine_path=inst_b,
        construct_schedule=schedule,
        defines=defines,
        layout="zigzag",
        num_rungs=num_rungs,
        tunnel_depth=tunnel_depth,
        config=cfg,
    )


def generate_stress_design(
    *,
    depth: Optional[int] = None,
    branch_factor: Optional[int] = None,
    seed: Optional[int] = None,
    config: Optional[StressConfig] = None,
) -> StressDesign:
    """Build stress RTL (zigzag cross-hierarchy by default for extreme profile)."""
    cfg = config or EXTREME_CONFIG
    rng = random.Random(seed)
    if depth is None:
        jitter = cfg.depth_jitter
        depth = cfg.depth_base + rng.randint(-jitter, jitter)
    depth = max(cfg.min_depth, depth)

    if branch_factor is None:
        bf_j = cfg.branch_jitter
        branch_factor = cfg.branch_base + rng.randint(-bf_j, bf_j)
    branch_factor = max(3, branch_factor)

    seed_val = seed if seed is not None else rng.randint(0, 2**31 - 1)
    rng = random.Random(seed_val)

    if cfg.zigzag:
        return _generate_zigzag(
            depth=depth,
            branch_factor=branch_factor,
            seed_val=seed_val,
            rng=rng,
            cfg=cfg,
        )
    return _generate_linear(
        depth=depth,
        branch_factor=branch_factor,
        seed_val=seed_val,
        rng=rng,
        cfg=cfg,
    )


@dataclass
class StressTrialResult:
    seed: int
    depth: int
    branch_factor: int
    connected: bool
    connected_port_port: bool
    connected_port_inst: bool
    connected_cross: bool
    modules_parsed_note_pp: str
    modules_parsed_note_pi: str
    modules_parsed_note_x: str
    gen_sec: float
    index_sec: float
    elab_sec: float
    connect_sec: float
    total_sec: float
    instance_rows: int
    module_count: int
    file_count: int
    layout: str
    errors: List[str] = field(default_factory=list)


def run_stress_trial(
    *,
    seed: Optional[int] = None,
    depth: Optional[int] = None,
    branch_factor: Optional[int] = None,
    config: Optional[StressConfig] = None,
) -> Tuple[StressDesign, StressTrialResult]:
    """Generate RTL and run port-port, port-inst, and cross-branch connectivity."""
    import time
    import tempfile
    from pathlib import Path

    from hierwalk.connectivity import check_connectivity
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    t0 = time.perf_counter()
    design = generate_stress_design(
        depth=depth,
        branch_factor=branch_factor,
        seed=seed,
        config=config,
    )
    t_gen = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="hierwalk_stress_") as tmp:
        root = Path(tmp)
        file_map: Dict[str, str] = {}
        for rel, text in design.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            file_map[str(path)] = text

        index = DesignIndex.build(file_map)
        t_index = time.perf_counter()

        _root, rows = elaborate(index, design.top)
        t_elab = time.perf_counter()

        ep_pp_a, ep_pp_b = design.endpoint_port_port
        r_pp = check_connectivity(
            ep_pp_a,
            ep_pp_b,
            rows=rows,
            index=index,
            top=design.top,
            defines=design.defines,
            trace=False,
            ff_barrier=False,
        )
        ep_pi_a, ep_pi_b = design.endpoint_port_inst
        r_pi = check_connectivity(
            ep_pi_a,
            ep_pi_b,
            rows=rows,
            index=index,
            top=design.top,
            defines=design.defines,
            trace=False,
            ff_barrier=False,
        )
        ep_x_a, ep_x_b = design.endpoint_cross
        r_x = check_connectivity(
            ep_x_a,
            ep_x_b,
            rows=rows,
            index=index,
            top=design.top,
            defines=design.defines,
            trace=False,
            ff_barrier=False,
        )
        t_connect = time.perf_counter()

        trial = StressTrialResult(
            seed=design.seed,
            depth=design.depth,
            branch_factor=design.branch_factor,
            connected=r_pp.connected and r_pi.connected and r_x.connected,
            connected_port_port=r_pp.connected,
            connected_port_inst=r_pi.connected,
            connected_cross=r_x.connected,
            modules_parsed_note_pp=r_pp.note or "",
            modules_parsed_note_pi=r_pi.note or "",
            modules_parsed_note_x=r_x.note or "",
            gen_sec=t_gen - t0,
            index_sec=t_index - t_gen,
            elab_sec=t_elab - t_index,
            connect_sec=t_connect - t_elab,
            total_sec=t_connect - t0,
            instance_rows=len(rows),
            module_count=len(index.modules),
            file_count=len(design.files),
            layout=design.layout,
            errors=list(r_pp.errors) + list(r_pi.errors) + list(r_x.errors),
        )
    return design, trial


def run_stress_batch(
    trials: int = 10,
    *,
    base_seed: int = 20260613,
    config: Optional[StressConfig] = None,
) -> List[StressTrialResult]:
    """Run multiple randomized stress trials with distinct seeds."""
    out: List[StressTrialResult] = []
    for i in range(trials):
        _, trial = run_stress_trial(
            seed=base_seed + i * 9973,
            config=config,
        )
        out.append(trial)
    return out


def format_stress_report(results: Sequence[StressTrialResult]) -> str:
    if not results:
        return "no trials"
    header = (
        "trial  seed        depth  br   inst  files  mods  lay  "
        "pp  pi  xz  gen_ms  idx_ms  elab_ms  conn_ms  total_ms  note"
    )
    lines = [header]
    for i, r in enumerate(results):
        if not r.connected_port_port:
            note = r.modules_parsed_note_pp
        elif not r.connected_port_inst:
            note = r.modules_parsed_note_pi
        elif not r.connected_cross:
            note = r.modules_parsed_note_x
        else:
            note = r.modules_parsed_note_pp
        lines.append(
            f"{i:5d}  {r.seed:10d}  {r.depth:5d}  {r.branch_factor:3d}  "
            f"{r.instance_rows:5d}  {r.file_count:5d}  {r.module_count:4d}  "
            f"{r.layout[:3]:3s}  "
            f"{'Y' if r.connected_port_port else 'N':2s}  "
            f"{'Y' if r.connected_port_inst else 'N':2s}  "
            f"{'Y' if r.connected_cross else 'N':2s}  "
            f"{r.gen_sec * 1e3:6.1f}  {r.index_sec * 1e3:6.1f}  "
            f"{r.elab_sec * 1e3:7.1f}  {r.connect_sec * 1e3:7.1f}  "
            f"{r.total_sec * 1e3:8.1f}  {note}"
        )
    totals = [r.total_sec for r in results]
    lines.append(
        f"avg total_ms: {sum(totals) / len(totals) * 1e3:.1f}  "
        f"max total_ms: {max(totals) * 1e3:.1f}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Connectivity stress benchmark")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--branch-factor", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None, help="single-trial seed")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument(
        "--standard",
        action="store_true",
        help="use legacy linear depth~10 branch~5 single-file profile",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="write RTL, filelist.f, and connect.json for a single generated design",
    )
    args = parser.parse_args()
    profile = STANDARD_CONFIG if args.standard else EXTREME_CONFIG
    if args.seed is not None or args.depth is not None:
        design = generate_stress_design(
            seed=args.seed,
            depth=args.depth,
            branch_factor=args.branch_factor,
            config=profile,
        )
        if args.out_dir:
            paths = write_stress_artifacts(design, args.out_dir)
            for label, path in sorted(paths.items()):
                print(f"wrote {label}: {path}")
        _, trial = run_stress_trial(
            seed=args.seed,
            depth=args.depth,
            branch_factor=args.branch_factor,
            config=profile,
        )
        print(format_stress_report([trial]))
    else:
        print(
            format_stress_report(
                run_stress_batch(args.trials, config=profile),
            )
        )