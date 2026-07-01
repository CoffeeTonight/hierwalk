"""Connectivity check: assign + instance port-map tracing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest

from hierwalk.connect.logical.scan import (
    build_module_connect_index,
    extract_signal_roots,
    net_representative,
    prepare_connect_body,
    scan_assign_adjacency,
    scan_ff_adjacency,
    split_statements,
)
from hierwalk.generate_fold import fold_generate_regions
from hierwalk.connect.shared.request import (
    ConnectivityCheck,
    ConnectivityRequest,
    load_connect_request,
    parse_connect_request_json,
)
from hierwalk.connect.session import (
    ConnectivitySession,
    check_connectivity,
    check_connectivity_batch,
    format_connect_results_tsv,
    load_connect_pairs,
    parse_connect_endpoint,
    resolve_endpoint,
    run_connectivity_request,
)
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _cli_connect_result_line(stdout: str) -> str:
    """Last data row from hier-walk connect TSV stdout (skip ``#`` comments)."""
    lines = [
        ln
        for ln in stdout.strip().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert lines, "expected connect TSV output"
    return lines[-1]


UNIFIED_VERIFY = Path(
    "/home/user/tools/CodeFromAI/hc_hierarchy/design/unified_verify"
)
FILELIST = UNIFIED_VERIFY / "filelist.f"
TOP = "hc_verify_top"


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows


def test_extract_signal_roots():
    assert extract_signal_roots("clk") == {"clk"}
    assert extract_signal_roots("status[gi]") == {"status", "gi"}
    assert "idx" in extract_signal_roots("idx[3]")


def test_scan_assign_and_port_maps():
    body = """
    wire a, b, c;
    assign a = clk;
    child u_c (.p(c), .q(clk));
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "a") == net_representative(idx, "clk")
    assert idx.inst_ports["u_c"] == [("p", "c"), ("q", "clk")]
    assert ("u_c", "q") in idx.net_to_children.get(
        net_representative(idx, "clk"), []
    )


def test_parse_endpoint_generate_dotted_inst_leaf(tmp_path: Path):
    v = """
    module top(input logic clk);
      soc u_soc();
    endmodule
    module soc(input logic clk);
      generate
        if (1) begin : gen_blk
          for (genvar gi = 0; gi < 2; gi++) begin : gen_loop
            leaf u_cell(.clk(clk));
          end
        end
      endgenerate
    endmodule
    module leaf(input logic clk); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    rows_by_path = {r.full_path: r for r in rows}
    inst, port = parse_connect_endpoint(
        "top.u_soc.gen_blk.gen_loop[0].u_cell.clk",
        rows_by_path,
        index=index,
        top="top",
    )
    assert inst == "top.u_soc.gen_blk.gen_loop[0].u_cell"
    assert port == "clk"


def test_parse_endpoint_longest_match(tmp_path: Path):
    v = """
    module top(input logic p);
      child u_child(input logic p);
    endmodule
    module child(input logic p); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    rows_by_path = {r.full_path: r for r in rows}
    inst, port = parse_connect_endpoint(
        "top.u_child.p", rows_by_path, index=index, top="top"
    )
    assert inst == "top.u_child"
    assert port == "p"
    inst2, port2 = parse_connect_endpoint(
        "top.p", rows_by_path, index=index, top="top"
    )
    assert inst2 == "top"
    assert port2 == "p"


def test_connectivity_within_module_assign(tmp_path: Path):
    v = """
    module top(input logic clk, input logic rst_n, input logic [3] idx);
      wire tie;
      assign tie = clk;
      child u0 (.clk(tie), .idx(idx));
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.clk", "top.u0.clk", rows=rows, index=index, top="top")
    assert r.connected
    assert r.mode == "port-port"


def test_connectivity_top_to_child_port(tmp_path: Path):
    v = """
    module top(input logic clk, input logic [3] idx);
      child u_c (.clk(clk), .idx(idx));
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.idx", "top.u_c.idx", rows=rows, index=index, top="top")
    assert r.connected
    assert r.mode == "port-port"


def test_connectivity_port_reaches_hierarchy(tmp_path: Path):
    v = """
    module top(input logic clk, input logic [3] idx);
      child u_c (.clk(clk), .idx(idx));
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.idx", "top.u_c", rows=rows, index=index, top="top")
    assert r.connected
    assert r.mode == "port-hierarchy"


def test_connectivity_negative_unrelated(tmp_path: Path):
    v = """
    module top(input logic clk, input logic [3] idx);
      child u_c (.clk(clk), .idx(idx));
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.clk", "top.u_c.idx", rows=rows, index=index, top="top")
    assert not r.connected


def test_split_statements_respects_begin_end():
    body = """
    always_ff @(posedge clk) begin
      if (rst) q <= 0;
      else q <= c + d;
    end
    assign a = b;
    """
    stmts = split_statements(body)
    assert len(stmts) == 2
    assert _stmt_has(stmts[0], "always_ff")
    assert _stmt_has(stmts[0], "q <=")
    assert stmts[1].strip().startswith("assign")


def _stmt_has(stmt: str, needle: str) -> bool:
    return needle in stmt


def test_split_statements_after_sized_literal_in_ff():
    body = """
    always_ff @(posedge clk) begin
      if (rst)
        r <= 1'b0;
      else
        r <= c;
    end
    assign q = r;
    """
    stmts = split_statements(body)
    assert len(stmts) == 2
    assert stmts[0].strip().endswith("end")
    assert stmts[1].strip().startswith("assign")


def test_split_statements_multiline_assign():
    body = "assign\n a=\nb\n + c;"
    stmts = split_statements(body)
    assert len(stmts) == 1
    adj = scan_assign_adjacency(body)
    assert "c" in adj.get("a", set())


def test_multiline_assign_expression():
    body = "assign\n a=\nb\n + c;"
    adj = scan_assign_adjacency(body)
    assert net_representative(build_module_connect_index(body), "a") == net_representative(
        build_module_connect_index(body), "b"
    )
    assert "c" in adj.get("a", set())


def test_multiline_ff_nb_assign():
    body = """
    always_ff @(posedge clk)
      q <=
        c + d;
    """
    adj = scan_ff_adjacency(body)
    assert "c" in adj.get("q", set())
    assert "d" in adj.get("q", set())
    assert "posedge" not in adj.get("q", set())


def test_ff_if_else_begin_end():
    body = """
    always_ff @(posedge clk) begin
      if (rst)
        q <= 0;
      else
        q <= c + d;
    end
    """
    adj = scan_ff_adjacency(body)
    assert "c" in adj.get("q", set())
    assert "d" in adj.get("q", set())


def test_ff_ignores_comparison_le_in_condition():
    body = """
    always_ff @(posedge clk) begin
      if (a <= b)
        q <= c;
    end
    """
    adj = scan_ff_adjacency(body)
    assert adj.get("q") == {"c"}
    assert "a" not in adj.get("b", set())


def test_generate_fold_ff_connect(tmp_path: Path):
    body = """
    generate
      for (gi = 0; gi < 2; gi = gi + 1) begin
        always_ff @(posedge clk) r <= d_in;
      end
    endgenerate
    """
    folded = fold_generate_regions(body, {})
    adj = scan_ff_adjacency(folded)
    assert "d_in" in adj.get("r", set())


def test_narrow_port_bus_md_suffix_links_via_port_bases():
    body = """
    wire [1:0] bus_b;
    assign bus_b[0] = src;
    assign bus_b[1] = dst;
    """
    idx = build_module_connect_index(
        body,
        port_decl_widths={"bus_b": [0, 1]},
    )
    assert net_representative(idx, "src") == net_representative(idx, "dst")
    assert net_representative(idx, "bus_b") == net_representative(idx, "bus_b[0]")


def test_internal_two_suffix_md_bus_does_not_merge_bits():
    """Sparse internal [1:0] vector: no port base, no false hop-style merge."""
    body = """
    logic [1:0] hop;
    assign hop[0] = src;
    assign dst = hop[1];
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "hop[0]") != net_representative(idx, "hop[1]")
    assert net_representative(idx, "src") != net_representative(idx, "dst")


def test_casex_ff_connectivity():
    body = """
    logic link;
    logic [3:0] key;
    assign key = 4'b?010;
    always_ff @(posedge clk) begin
      casex (key)
        4'b??1?: link <= probe_in;
        default: link <= probe_in;
      endcase
    end
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "probe_in") == net_representative(idx, "link")


def test_casex_comb_connectivity():
    body = """
    logic link;
    logic [3:0] key_0;
    assign key_0 = 4'b?0?1;
    always_comb begin
      casex (key_0)
        4'b??1?: link = probe_in;
        default: link = probe_in;
      endcase
    end
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "probe_in") == net_representative(idx, "link")


def test_generate_fold_if_labeled_begin_connectivity():
    body = """
    wire link;
    generate
      if (1) begin : ifg_0
        assign link = probe_in;
      end
    endgenerate
    """
    folded = fold_generate_regions(body, {})
    assert "ifg_0" not in folded
    adj = scan_assign_adjacency(folded)
    assert "probe_in" in adj.get("link", set())


def test_connectivity_messy_rtl_through_ff(tmp_path: Path):
    v = """
    module top(input logic clk, input logic rst, input logic c, input logic d);
      mid u_m (.clk(clk), .rst(rst), .c(c), .d(d), .q(qout));
    endmodule
    module mid(
      input logic clk, input logic rst, input logic c, input logic d,
      output logic q
    );
      logic r;
      always_ff @(posedge clk) begin
        if (rst)
          r <= 1'b0;
        else
          r <=
            c +
            d;
      end
      assign q = r;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    assert check_connectivity(
        "top.c", "top.u_m.q", rows=rows, index=index, top="top", ff_barrier=False
    ).connected


def test_ifdef_selects_active_branch():
    body = """
`ifdef USE_ALT
  assign out = alt;
`else
  assign out = def;
`endif
"""
    on = prepare_connect_body(body, defines={"USE_ALT": "1"})
    off = prepare_connect_body(body, defines={})
    assert "alt" in scan_assign_adjacency(on).get("out", set())
    assert "def" in scan_assign_adjacency(off).get("out", set())
    assert "def" not in scan_assign_adjacency(on).get("out", set())


def test_if_generate_unresolved_prefers_else_branch():
    body = """
generate
  if (USE_ALT) begin
    assign out = alt;
  end else begin
    assign out = def;
  end
endgenerate
"""
    text = prepare_connect_body(body, defines={}, over_approximate_if=True)
    peers = scan_assign_adjacency(text).get("out", set())
    assert peers == {"def"}


def test_primitive_and_gate_connectivity():
    body = """
    wire a, b, c;
    assign a = src;
    assign b = src;
    and u_g (c, a, b);
    assign dst = c;
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "src") == net_representative(idx, "dst")


def test_bind_connectivity_src_to_hier_port(tmp_path):
    v = tmp_path / "bind.v"
    v.write_text(
        """
        module core(input src, output dst);
          assign dst = 1'b0;
        endmodule
        module tie(input src, output dst);
          assign dst = src;
        endmodule
        module top(input src_bind, output out);
          core u_core (.src(1'b0), .dst());
        endmodule
        bind top tie u_tie (.src(src_bind), .dst(u_core.dst));
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert check_connectivity(
        "top.src_bind", "top.u_core.dst", rows=rows, index=index, top="top"
    ).connected


def test_hier_ref_assign_connectivity(tmp_path):
    v = tmp_path / "hier.v"
    v.write_text(
        """
        module child(input p, output hidden);
          assign hidden = p;
        endmodule
        module parent(input src, output dst);
          child u_c (.p(src), .hidden());
          assign dst = u_c.hidden;
        endmodule
        module top(input src, output dst);
          parent u_p (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_ff_case_constant_fold_active_arm_only():
    body = """
    logic link;
    logic [1:0] sel;
    assign sel = 2'b01;
    always_ff @(posedge clk) begin
      case (sel)
        2'b00: link <= src;
        default: link <= 1'b0;
      endcase
    end
    """
    adj = scan_ff_adjacency(body)
    assert "src" not in adj.get("link", set())
    adj_on = scan_ff_adjacency(body.replace("2'b01", "2'b00"))
    assert "src" in adj_on.get("link", set())


def test_param_folded_nested_array_index_connectivity(tmp_path):
    v = tmp_path / "arr.v"
    v.write_text(
        """
        module leaf #(
          parameter int BASE = 2,
          parameter int STRIDE = 4,
          parameter int LEVEL = 1
        )(
          input src,
          output dst
        );
          localparam int LEAF_LP = BASE * STRIDE + LEVEL;
          localparam int LEAF_IDX = LEAF_LP[1:0];
          logic [1:0][1:0] leaf_arr;
          logic leaf_q;
          assign leaf_arr[0][0] = src;
          assign leaf_arr[1][LEAF_IDX] = leaf_arr[0][0];
          always_ff @(posedge clk) leaf_q <= leaf_arr[1][LEAF_IDX];
          assign dst = leaf_q;
        endmodule
        module top(input src, output dst);
          leaf #(.BASE(2), .STRIDE(4), .LEVEL(5)) u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    pmap = {"BASE": "2", "STRIDE": "4", "LEVEL": "5", "LEAF_LP": "13", "LEAF_IDX": "1"}
    idx = build_module_connect_index(
        index.module_body("leaf"),
        param_map=pmap,
    )
    assert net_representative(idx, "src") == net_representative(idx, "dst")


def test_empty_module_port_passthrough(tmp_path):
    v = tmp_path / "bb.v"
    v.write_text(
        """
        module blackbox(input src, output dst);
        endmodule
        module top(input src, output dst);
          blackbox u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_instance_port_maps_multi_dim_array():
    from hierwalk.connect.logical.scan import instance_port_maps

    body = """
    md2d_leaf g[0:1][0:2] (.clk(clk), .probe_in(probe_in), .probe_out(leaf_out));
    """
    ports = instance_port_maps(body, param_map={})
    assert set(ports) == {
        "g[0][0]",
        "g[0][1]",
        "g[0][2]",
        "g[1][0]",
        "g[1][1]",
        "g[1][2]",
    }
    assert ports["g[0][2]"] == [
        ("clk", "clk"),
        ("probe_in", "probe_in"),
        ("probe_out", "leaf_out[0][2]"),
    ]


def test_connectivity_md2_port_per_slice(tmp_path: Path):
    """2D bus ``a[2:0][3:0]``: bare ``.a`` and per-slice ``a[i][j]`` connect."""
    v = tmp_path / "md2.v"
    v.write_text(
        """
        module leaf(input logic [2:0][3:0] a, output logic [2:0][3:0] y);
          wire [2:0][3:0] comb_w;
          genvar i, j;
          generate
            for (i = 0; i < 3; i++) begin : gi
              for (j = 0; j < 4; j++) begin : gj
                assign comb_w[i][j] = a[i][j];
                assign y[i][j] = comb_w[i][j];
              end
            end
          endgenerate
        endmodule
        module mid(input logic [2:0][3:0] a, output logic [2:0][3:0] y);
          leaf u_leaf (.a(a), .y(y));
        endmodule
        module top(input logic [2:0] src, output logic [2:0][3:0] out);
          wire [2:0][3:0] bus;
          assign bus[0][0] = src[0];
          assign bus[1][2] = src[1];
          mid u_mid (.a(bus), .y(out));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert check_connectivity(
        "top.src[0]", "top.u_mid.a[0][0]", rows=rows, index=index, top="top"
    ).connected
    assert check_connectivity(
        "top.src[1]", "top.u_mid.a[1][2]", rows=rows, index=index, top="top"
    ).connected
    assert check_connectivity(
        "top.u_mid.a", "top.u_mid.u_leaf.a", rows=rows, index=index, top="top"
    ).connected
    assert check_connectivity(
        "top.u_mid.a[0][0]", "top.u_mid.u_leaf.a[0][0]",
        rows=rows, index=index, top="top",
    ).connected


def test_nested_bracket_select_token_parse():
    from hierwalk.connect.logical.scan import extract_connect_nodes

    nodes = extract_connect_nodes(
        "leaf_arr[1][LEAF_LP[1:0]]",
        {"LEAF_LP": "12"},
    )
    assert nodes == {"leaf_arr[1][0]"}


def test_constant_tieoff_masks_do_not_connect_src():
    from hierwalk.connect.logical.scan import (
        _effective_assign_rhs_roots,
        _grep_assign_rhs_roots,
    )

    assert not _effective_assign_rhs_roots("1'b0")
    assert not _effective_assign_rhs_roots("src & 1'b0")
    assert not _effective_assign_rhs_roots("src ? 1'b0 : 1'b0")
    assert not _effective_assign_rhs_roots("src * 0")
    assert "src" in _effective_assign_rhs_roots("1'b0 | src")
    assert "src" in _grep_assign_rhs_roots("src & 1'b0")
    assert "src" in _grep_assign_rhs_roots("src * 0")
    body = """
    assign dst = src & 1'b0;
    """
    idx = build_module_connect_index(body)
    assert net_representative(idx, "src") != net_representative(idx, "dst")


def test_vuln_b7_masked_tieoff_no_path(tmp_path):
    v = tmp_path / "b7.v"
    v.write_text(
        """
        module v_function_gap(input src, output dst);
          assign dst = src & 1'b0;
        endmodule
        module top(input src, output dst);
          v_function_gap u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_a5_default_comb_only_blocks_ff_path(tmp_path):
    v = tmp_path / "a5.v"
    v.write_text(
        """
        module v_ff_chain(input clk, rst_n, src, output dst);
          logic q;
          always_ff @(posedge clk) begin
            if (!rst_n) q <= 1'b0;
            else q <= src;
          end
          assign dst = q;
        endmodule
        module top(input clk, rst_n, src, output dst);
          v_ff_chain u (.clk(clk), .rst_n(rst_n), .src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected
    assert check_connectivity(
        "top.src",
        "top.dst",
        rows=rows,
        index=index,
        top="top",
        ff_barrier=False,
    ).connected


def test_ff_barrier_blocks_sequential_edges():
    body = """
    logic q;
    always_ff @(posedge clk) q <= src;
    assign dst = q;
    """
    loose = build_module_connect_index(body, ff_barrier=False)
    strict = build_module_connect_index(body, ff_barrier=True)
    assert net_representative(loose, "src") == net_representative(loose, "dst")
    assert net_representative(strict, "src") != net_representative(strict, "dst")


def test_comb_case_constant_fold_active_arm_only():
    body = """
    logic link;
    logic [1:0] sel;
    assign sel = 2'b01;
    always_comb begin
      case (sel)
        2'b00: link = src;
        default: link = 1'b0;
      endcase
    end
    """
    adj = scan_assign_adjacency(body)
    assert "src" not in adj.get("link", set())
    body_on = body.replace("2'b01", "2'b00")
    adj_on = scan_assign_adjacency(body_on)
    assert "src" in adj_on.get("link", set())


def test_case_item_label_not_in_adjacency():
    body = """
    logic link;
    logic b00;
    always_comb begin
      case (sel)
        2'b00: link = src;
        default: link = 1'b0;
      endcase
    end
    """
    adj = scan_assign_adjacency(body)
    assert "b00" not in adj


def test_param_pairs_balanced_parens_and_ternary():
    from hierwalk.params import parse_param_pairs, resolve_param_map

    text = """
    localparam int WIN = (BASE + LEVEL) - BASE + 1;
    localparam int PASS_THRU = (WIN > 0) ? 1 : 0;
    """
    pairs = parse_param_pairs(text)
    assert pairs["WIN"] == "(BASE + LEVEL) - BASE + 1"
    assert pairs["PASS_THRU"] == "(WIN > 0) ? 1 : 0"
    resolved = resolve_param_map(pairs, parent={"BASE": "2", "LEVEL": "4"})
    assert resolved["WIN"] == "5"
    assert resolved["PASS_THRU"] == "1"


def test_vuln_a1_unresolved_generate_no_false_path(tmp_path):
    v = tmp_path / "a1.v"
    v.write_text(
        """
        module v_over_if(input src, output dst);
          wire link;
          assign link = src;
          generate
            if (MYSTERY) begin
              assign dst = link;
            end else begin
              assign dst = 1'b0;
            end
          endgenerate
        endmodule
        module top(input src, output dst);
          v_over_if u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_b5_unresolved_generate_no_false_path(tmp_path):
    v = tmp_path / "b5.v"
    v.write_text(
        """
        module v_unresolved_if(input src, output dst);
          wire link;
          assign link = src;
          generate
            if (UNRESOLVED_PARAM) begin
              assign dst = link;
            end
          endgenerate
        endmodule
        module top(input src, output dst);
          v_unresolved_if u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_a2b_ifdef_else_without_define(tmp_path):
    v = tmp_path / "a2b.v"
    v.write_text(
        """
        module v_ifdef_path(input src, output dst);
          wire link;
          `ifdef VULN_USE_PATH
            assign link = src;
          `else
            assign link = 1'b0;
          `endif
          assign dst = link;
        endmodule
        module top(input src, output dst);
          v_ifdef_path u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected
    assert check_connectivity(
        "top.src",
        "top.dst",
        rows=rows,
        index=index,
        top="top",
        defines={"VULN_USE_PATH": "1"},
    ).connected


def test_if_generate_strict_drops_unresolved():
    body = """
wire link;
assign link = src;
generate
  if (UNRESOLVED_PARAM) begin
    assign dst = link;
  end
endgenerate
"""
    loose = prepare_connect_body(body, over_approximate_if=True)
    strict = prepare_connect_body(body, over_approximate_if=False)
    assert "dst" not in scan_assign_adjacency(loose).get("link", set())
    assert "dst" not in scan_assign_adjacency(strict).get("link", set())


def test_wire_decl_alias():
    body = "wire q = r;"
    adj = scan_assign_adjacency(body)
    assert net_representative(build_module_connect_index(body), "q") == net_representative(
        build_module_connect_index(body), "r"
    )


def test_always_comb_blocking_assign():
    body = """
    always_comb begin
      a = b + c;
    end
    """
    adj = scan_assign_adjacency(body)
    assert "b" in adj.get("a", set())
    assert "c" in adj.get("a", set())


def test_case_item_ff_inside():
    body = """
    always_ff @(posedge clk) begin
      case (sel)
        1'b0: q <= a;
        1'b1: q <= b;
      endcase
    end
    """
    adj = scan_ff_adjacency(body)
    assert "a" in adj.get("q", set())
    assert "b" in adj.get("q", set())


def test_ff_adjacency_scan():
    body = """
    logic q, d;
    always_ff @(posedge clk) begin
      q <= d;
    end
    """
    adj = scan_ff_adjacency(body)
    assert "d" in adj.get("q", set())
    assert "q" in adj.get("d", set())


def test_connectivity_through_ff(tmp_path: Path):
    v = """
    module top(input logic clk, input logic d);
      mid u_m (.clk(clk), .din(d), .dout(qout));
    endmodule
    module mid(input logic clk, input logic din, output logic qout);
      logic q;
      always_ff @(posedge clk) q <= din;
      assign qout = q;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity(
        "top.d", "top.u_m.qout", rows=rows, index=index, top="top", ff_barrier=False
    )
    assert r.connected
    assert not check_connectivity(
        "top.d", "top.u_m.qout", rows=rows, index=index, top="top"
    ).connected


def test_connectivity_cross_hierarchy_via_lca(tmp_path: Path):
    """Sibling subtrees meet at parent nets (LCA upward then downward)."""
    v = """
    module top(input logic src, output logic dst);
      wire bridge;
      left u_l (.in(src), .out(bridge));
      right u_r (.in(bridge), .out(dst));
    endmodule
    module left(input logic in, output logic out);
      assign out = in;
    endmodule
    module right(input logic in, output logic out);
      assign out = in;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.src", "top.dst", rows=rows, index=index, top="top")
    assert r.connected
    assert r.mode == "port-port"
    assert "module(s)" in r.note


def test_connectivity_cross_hierarchy_deep_branches(tmp_path: Path):
    v = """
    module top(input logic a, input logic b);
      wire mid;
      path_a u_a (.x(a), .y(mid));
      path_b u_b (.x(mid), .y(b));
    endmodule
    module path_a(input logic x, output logic y);
      leaf_a u_leaf (.p(x), .q(y));
    endmodule
    module path_b(input logic x, output logic y);
      leaf_b u_leaf (.p(x), .q(y));
    endmodule
    module leaf_a(input logic p, output logic q);
      assign q = p;
    endmodule
    module leaf_b(input logic p, output logic q);
      assign q = p;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity(
        "top.u_a.u_leaf.p", "top.u_b.u_leaf.q", rows=rows, index=index, top="top"
    )
    assert r.connected
    assert r.mode == "port-port"


def test_connectivity_trace_emits_hops(tmp_path: Path):
    v = """
    module top(input logic clk);
      child u_c (.clk(clk));
    endmodule
    module child(input logic clk); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity(
        "top.clk", "top.u_c.clk", rows=rows, index=index, top="top", trace=True
    )
    assert r.connected
    assert len(r.hops) >= 1
    assert r.hops[0].kind in ("child-down", "parent-up", "intra-module")
    assert "instance" in r.hops[0].detail or "port map" in r.hops[0].detail


def test_connect_trace_prints_report_to_terminal(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
    module top(input logic clk);
      child u0 (.clk(clk));
    endmodule
    module child(input logic clk); endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "files.f"
    fl.write_text(str(rtl) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--no-cache",
            "--quiet",
            "--check-connect",
            "top.clk",
            "top.u0.clk",
            "--connect-trace",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "True" in proc.stdout
    assert "--- connectivity path evidence ---" in proc.stderr
    assert "path evidence:" in proc.stderr
    assert "child-down" in proc.stderr or "parent-up" in proc.stderr


def test_connect_log_cli_emits_steps(tmp_path: Path, capsys):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
    module top(input logic clk);
      child u0 (.clk(clk));
    endmodule
    module child(input logic clk); endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "files.f"
    fl.write_text(str(rtl) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--no-cache",
            "--quiet",
            "--check-connect",
            "top.clk",
            "top.u0.clk",
            "--connect-log",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "True" in proc.stdout
    assert "--- connectivity path evidence ---" in proc.stderr
    assert "path evidence:" in proc.stderr
    assert "child-down" in proc.stderr or "parent-up" in proc.stderr


def test_coi_prunes_unrelated_clk_branches(tmp_path: Path):
    """Many clk siblings; only idx path should be explored for idx query."""
    branches = "\n".join(
        f"      leaf u_clk{i} (.clk(clk), .x(1'b0));"
        for i in range(20)
    )
    v = f"""
    module top(input logic clk, input logic [3] idx);
      child u_goal (.clk(clk), .idx(idx));
{branches}
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    module leaf(input logic clk, input logic x); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.idx", "top.u_goal.idx", rows=rows, index=index, top="top")
    assert r.connected
    r2 = check_connectivity("top.idx", "top.u_clk5.clk", rows=rows, index=index, top="top")
    assert not r2.connected


def test_resolve_missing_hierarchy(tmp_path: Path):
    v = "module top(); endmodule"
    index, rows = _index_and_rows(v, tmp_path)
    ep, errs = resolve_endpoint("top.u_missing.clk", rows, index, top="top")
    assert ep.spec == "top.u_missing.clk"
    assert not ep.module
    assert any("hierarchy not found" in e for e in errs)
    assert any("top" in e for e in errs)


def test_connect_missing_hierarchy_reports_errors(tmp_path: Path):
    v = "module top(input logic clk); endmodule"
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity(
        "top.u_missing.clk",
        "top.clk",
        rows=rows,
        index=index,
        top="top",
    )
    assert not r.connected
    assert any("hierarchy not found" in e for e in r.errors)


@pytest.mark.skipif(
    not FILELIST.is_file(),
    reason="unified_verify corpus not available",
)
def test_unified_verify_idx_connect():
    cmd = [
        "hier-walk",
        str(FILELIST),
        "--top",
        TOP,
        "--no-cache",
        "--quiet",
        "--check-connect",
        "hc_verify_top.idx",
        "hc_verify_top.u_ecc_engine_00.idx",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(UNIFIED_VERIFY),
        capture_output=True,
        text=True,
        check=True,
    )
    line = _cli_connect_result_line(proc.stdout)
    assert "True" in line
    assert "port-port" in line


def test_vuln_g1_casez_wildcard_no_fp(tmp_path):
    v = tmp_path / "g1.v"
    v.write_text(
        """
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
        module top(input src, output dst);
          v_casez_wildcard u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_g2_multi_driver_const_masks_src(tmp_path):
    v = tmp_path / "g2.v"
    v.write_text(
        """
        module v_multi_driver(input src, output dst);
          wire link;
          assign link = src;
          assign link = 1'b0;
          assign dst = link;
        endmodule
        module top(input src, output dst);
          v_multi_driver u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_g3_multi_input_blackbox_no_passthrough(tmp_path):
    v = tmp_path / "g3.v"
    v.write_text(
        """
        module v_multi_in_blackbox(input a, b, output y);
        endmodule
        module top(input a, output y);
          v_multi_in_blackbox u (.a(a), .b(1'b0), .y(y));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.a", "top.y", rows=rows, index=index, top="top"
    ).connected


def test_vuln_i11_concat_high_bit_no_src(tmp_path):
    v = tmp_path / "i11.v"
    v.write_text(
        """
        module m(input src, output dst);
          assign dst = {src, 1'b0}[1];
        endmodule
        module top(input src, output dst);
          m u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_ff_last_wins_masks_src_when_ff_enabled(tmp_path):
    v = tmp_path / "ff_lw.v"
    v.write_text(
        """
        module m(input clk, input src, output dst);
          logic q;
          always_ff @(posedge clk) begin
            q <= src;
            q <= 1'b0;
          end
          assign dst = q;
        endmodule
        module top(input clk, input src, output dst);
          m u (.clk(1'b0), .src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src",
        "top.dst",
        rows=rows,
        index=index,
        top="top",
        ff_barrier=False,
    ).connected


def test_ff_unresolved_if_else_only_when_ff_enabled(tmp_path):
    v = tmp_path / "ff_if.v"
    v.write_text(
        """
        module m(input clk, input src, output dst);
          logic q;
          always_ff @(posedge clk) if (opaque) q <= src; else q <= 1'b0;
          assign dst = q;
        endmodule
        module top(input clk, input src, output dst);
          m u (.clk(1'b0), .src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src",
        "top.dst",
        rows=rows,
        index=index,
        top="top",
        ff_barrier=False,
    ).connected


def test_vuln_h2_nonzero_literal_tieoff(tmp_path):
    v = tmp_path / "h2.v"
    v.write_text(
        """
        module m(input src, output dst);
          wire link;
          assign link = src;
          assign link = 1'b1;
          assign dst = link;
        endmodule
        module top(input src, output dst);
          m u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_h7_unresolved_casez_no_fp(tmp_path):
    v = tmp_path / "h7.v"
    v.write_text(
        """
        module m(input src, output dst);
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
        module top(input src, output dst);
          m u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_h10_generate_for_eq_inc_chain(tmp_path):
    v = tmp_path / "h10.v"
    v.write_text(
        """
        module m(input src, output dst);
          wire [3:0] chain;
          assign chain[0] = src;
          generate
            for (genvar gi = 1; gi < 4; gi = gi + 1) begin
              assign chain[gi] = chain[gi - 1];
            end
          endgenerate
          assign dst = chain[3];
        endmodule
        module top(input src, output dst);
          m u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_vuln_g4_interface_hier_ref_excluded(tmp_path):
    v = tmp_path / "g4.v"
    v.write_text(
        """
        interface v_g4_if;
          logic sig;
        endinterface
        module v_intf_hier(input src, output dst);
          v_g4_if u_if();
          assign u_if.sig = src;
          assign dst = u_if.sig;
        endmodule
        module top(input src, output dst);
          v_intf_hier u (.src(src), .dst(dst));
        endmodule
        """,
        encoding="utf-8",
    )
    index, rows = _index_and_rows(v.read_text(), tmp_path)
    assert not check_connectivity(
        "top.src", "top.dst", rows=rows, index=index, top="top"
    ).connected


def test_connectivity_session_reuses_mod_cache(tmp_path: Path):
    v = """
    module top(input logic s0, s1, output logic d0, d1);
      wire h0, h1;
      assign h0 = s0;
      assign h1 = s1;
      assign d0 = h0;
      assign d1 = h1;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    session = ConnectivitySession(rows=rows, index=index, top="top")

    from hierwalk.connect.logical.scan import (
        clear_module_connect_index_cache,
        module_connect_index_stats,
    )

    clear_module_connect_index_cache()
    r0 = session.check("top.s0", "top.d0")
    r1 = session.check("top.s1", "top.d1")
    uncached, hits = module_connect_index_stats()

    clear_module_connect_index_cache()
    check_connectivity("top.s0", "top.d0", rows=rows, index=index, top="top")
    check_connectivity("top.s1", "top.d1", rows=rows, index=index, top="top")
    iso_uncached, iso_hits = module_connect_index_stats()

    assert r0.connected and r1.connected
    assert session.modules_cached >= 1
    assert uncached == 1
    assert hits == 0
    assert iso_uncached == 1
    # Second standalone call may load the disk sidecar written by the first.
    assert iso_hits == 0


def test_check_connectivity_batch_same_api_as_single(tmp_path: Path):
    v = """
    module top(input logic s0, s1, output logic d0, d1);
      assign d0 = s0;
      assign d1 = s1;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    batch = check_connectivity_batch(
        [("top.s0", "top.d0"), ("top.s1", "top.d1")],
        rows=rows,
        index=index,
        top="top",
        ff_barrier=True,
    )
    assert len(batch.results) == 2
    assert all(r.connected for r in batch.results)
    assert batch.modules_cached >= 1
    tsv = format_connect_results_tsv(batch.results, modules_cached=batch.modules_cached)
    assert "check_id\tendpoint_a\tendpoint_b" in tsv
    assert "# modules_cached\t" in tsv


def test_load_connect_pairs_file(tmp_path: Path):
    pairs_file = tmp_path / "pairs.tsv"
    pairs_file.write_text(
        "# fan-out\n"
        "top.s0\ttop.d0\n"
        "top.s1 top.d1\n",
        encoding="utf-8",
    )
    assert load_connect_pairs(str(pairs_file)) == [
        ("top.s0", "top.d0"),
        ("top.s1", "top.d1"),
    ]


def test_parse_connect_request_json_full_options():
    req = parse_connect_request_json(
        {
            "top": "stress_top",
            "defines": {"STRESS_USE_IN": "1"},
            "include_ff": True,
            "connect_trace": True,
            "strict_generate": True,
            "over_approximate_if": False,
            "checks": [
                {"id": "clk", "a": "top.clk", "b": "top.u0.clk"},
            ],
        }
    )
    assert req.top == "stress_top"
    assert req.defines == {"STRESS_USE_IN": "1"}
    assert req.include_ff
    assert req.trace
    assert req.strict_generate
    assert req.over_approximate_if is False
    assert len(req.checks) == 1
    assert req.checks[0].check_id == "clk"


def test_run_connectivity_request_missing_hierarchy(tmp_path: Path):
    v = "module top(input logic clk); endmodule"
    index, rows = _index_and_rows(v, tmp_path)
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.u_nope.clk", "top.clk", check_id="bad_hier"),
        ),
        top="top",
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top="top")
    assert len(batch.results) == 1
    r = batch.results[0]
    assert r.check_id == "bad_hier"
    assert not r.connected
    assert any("hierarchy not found" in e for e in r.errors)
    tsv = format_connect_results_tsv(batch.results)
    assert "bad_hier" in tsv
    assert "hierarchy not found" in tsv


def test_parse_connect_pairs_json_shapes():
    from hierwalk.connect.session import parse_connect_pairs_json

    assert parse_connect_pairs_json(
        [["top.a", "top.b"], ["top.c", "top.d"]]
    ) == [("top.a", "top.b"), ("top.c", "top.d")]
    assert parse_connect_pairs_json(
        {"pairs": [["top.a", "top.b"]]}
    ) == [("top.a", "top.b")]
    assert parse_connect_pairs_json(
        {
            "checks": [
                {"from": "top.clk", "to": "top.u0.clk"},
                {"a": "top.rst_n", "b": "top.u1.clk"},
            ]
        }
    ) == [("top.clk", "top.u0.clk"), ("top.rst_n", "top.u1.clk")]


def test_cli_check_connect_batch_json(tmp_path: Path):
    rtl = tmp_path / "d.v"
    pairs = tmp_path / "pairs.json"
    rtl.write_text(
        """
    module top(input logic clk, input logic rst_n);
      child u0 (.clk(clk));
      child u1 (.clk(rst_n));
    endmodule
    module child(input logic clk); endmodule
    """,
        encoding="utf-8",
    )
    pairs.write_text(
        json.dumps(
            {
                "checks": [
                    {"a": "top.clk", "b": "top.u0.clk"},
                    {"a": "top.rst_n", "b": "top.u1.clk"},
                ]
            }
        ),
        encoding="utf-8",
    )
    fl = tmp_path / "files.f"
    fl.write_text(str(rtl) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--no-cache",
            "--check-connect-batch",
            str(pairs),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 3
    assert all("True" in ln for ln in lines[1:])


def test_cli_check_connect_batch(tmp_path: Path):
    rtl = tmp_path / "d.v"
    pairs = tmp_path / "pairs.tsv"
    rtl.write_text(
        """
    module top(input logic clk, input logic rst_n);
      child u0 (.clk(clk));
      child u1 (.clk(rst_n));
    endmodule
    module child(input logic clk); endmodule
    """,
        encoding="utf-8",
    )
    pairs.write_text(
        "top.clk\ttop.u0.clk\n"
        "top.rst_n\ttop.u1.clk\n",
        encoding="utf-8",
    )
    fl = tmp_path / "files.f"
    fl.write_text(str(rtl) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--no-cache",
            "--check-connect-batch",
            str(pairs),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines[0].startswith("# connect results")
    assert lines[1].startswith("check_id\tendpoint_a\t")
    assert len(lines) == 5  # marker + header + 2 rows + modules_cached comment
    assert all("True" in ln for ln in lines[2:4])
    assert lines[-1].startswith("# modules_cached\t")


def test_connectivity_session_check_many(tmp_path: Path):
    v = """
    module top(input logic clk, input logic rst_n, input logic [3] idx);
      wire tie0, tie1;
      assign tie0 = clk;
      assign tie1 = rst_n;
      child u0 (.clk(tie0), .idx(idx));
      child u1 (.clk(tie1), .idx(idx));
    endmodule
    module child(input logic clk, input logic [3] idx); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    session = ConnectivitySession(rows=rows, index=index, top="top")
    results = session.check_many(
        [
            ("top.clk", "top.u0.clk"),
            ("top.rst_n", "top.u1.clk"),
        ]
    )
    assert len(results) == 2
    assert all(r.connected for r in results)


@pytest.mark.skipif(
    not FILELIST.is_file(),
    reason="unified_verify corpus not available",
)
def test_unified_verify_clk_reaches_gen_soc():
    cmd = [
        "hier-walk",
        str(FILELIST),
        "--top",
        TOP,
        "--no-cache",
        "--quiet",
        "--check-connect",
        "hc_verify_top.clk",
        "hc_verify_top.u_gen_soc",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(UNIFIED_VERIFY),
        capture_output=True,
        text=True,
        check=True,
    )
    line = _cli_connect_result_line(proc.stdout)
    assert "True" in line
    assert "port-hierarchy" in line


@pytest.mark.skipif(
    not FILELIST.is_file(),
    reason="unified_verify corpus not available",
)
def test_unified_verify_clk_not_idx():
    cmd = [
        "hier-walk",
        str(FILELIST),
        "--top",
        TOP,
        "--no-cache",
        "--quiet",
        "--check-connect",
        "hc_verify_top.clk",
        "hc_verify_top.u_ecc_engine_00.idx",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(UNIFIED_VERIFY),
        capture_output=True,
        text=True,
        check=True,
    )
    line = _cli_connect_result_line(proc.stdout)
    assert "False" in line