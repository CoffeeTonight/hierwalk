"""Hierarchy path / port glob search."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.path_refine import refine_param_ctx_for_path
from hierwalk.path_search import parse_hierarchy_port_pattern, search_hierarchy_path
from hierwalk.port_scan import (
    expand_port_name,
    match_literal_port_indices,
    port_index_for_module,
    scan_ports_from_module_text,
)
from hierwalk.preprocess import preprocess_file
from hierwalk.search import search


def test_parse_hierarchy_port_pattern():
    assert parse_hierarchy_port_pattern("top.u_*") == ("top.u_*", None)
    assert parse_hierarchy_port_pattern("top.u_mid.clk") == ("top.u_mid", "clk")
    assert parse_hierarchy_port_pattern("top.u_*.clk") == ("top.u_*", "clk")


def test_parse_hierarchy_port_pattern_keeps_deep_instance_path(tmp_path):
    from hierwalk.models import FlatRow

    rows = [
        FlatRow(
            full_path="top.u_mid.u_leaf",
            inst_leaf="u_leaf",
            module="leaf",
            depth=2,
            parent_path="top.u_mid",
            file="a.v",
        ),
    ]
    assert parse_hierarchy_port_pattern("top.u_mid.u_leaf", rows) == (
        "top.u_mid.u_leaf",
        None,
    )
    assert parse_hierarchy_port_pattern("top.u_mid.u_leaf") == (
        "top.u_mid",
        "u_leaf",
    )


def test_port_scan_and_path_search(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  mid u_mid0 ( );
  mid u_mid1 ( );
endmodule
module mid (
    input wire clk,
    input wire reset,
    output wire out
);
  leaf u_leaf ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    ports = scan_ports_from_module_text(text, "mid")
    assert "clk" in ports
    assert "reset" in ports

    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    path_hits = search_hierarchy_path(rows, "top.u_*", index)
    assert len(path_hits) == 2

    port_hits = search_hierarchy_path(rows, "top.u_mid*.clk", index)
    assert len(port_hits) == 2
    assert all(h.port_found for h in port_hits)
    assert {h.full_path for h in port_hits} == {
        "top.u_mid0.clk",
        "top.u_mid1.clk",
    }

    miss = search_hierarchy_path(rows, "top.u_mid0.nope", index)
    assert len(miss) == 1
    assert miss[0].port_found is False
    assert miss[0].match_kind == "hierarchy-port-miss"
    assert miss[0].full_path == "top.u_mid0.nope"
    assert "port not found" in miss[0].port_param_note
    assert "clk" in miss[0].port_param_note

    hier_miss = search_hierarchy_path(rows, "top.u_nope.clk", index)
    assert hier_miss == []


def test_param_port_symbolic_and_line(tmp_path):
    rtl = tmp_path / "p.v"
    rtl.write_text(
        """
module top;
  child #( .W(16) ) u0 ();
endmodule
module child #(parameter int W = 8) (
    input logic [W-1:0] data
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    row = next(r for r in rows if r.inst_leaf == "u0")
    from hierwalk.port_scan import port_index_for_module

    idx = port_index_for_module(row.file, row.module, row.param_ctx)
    assert "data" in idx
    assert "data[15]" in idx
    info = idx["data[15]"]
    assert info.line > 0
    assert info.param_note == "resolved"
    assert "W-1:0" in info.decl or "data" in info.decl


def test_array_port_expand_and_search(tmp_path):
    rtl = tmp_path / "arr.v"
    rtl.write_text(
        """
module top;
  arr_mid u_mid ( );
endmodule
module arr_mid (
    input logic [1:0][2:0] a,
    input logic [2:0] data
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    ports = scan_ports_from_module_text(text, "arr_mid")
    assert "a[0][1]" in ports
    assert "a[1][2]" in ports
    assert "data[2]" in ports
    assert expand_port_name("a", "[1:0][2:0]") == [
        "a[1][2]",
        "a[1][1]",
        "a[1][0]",
        "a[0][2]",
        "a[0][1]",
        "a[0][0]",
        "a[1:0][2:0]",
        "a[1][2:0]",
        "a[0][2:0]",
    ]

    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    hits = search_hierarchy_path(rows, "top.u_mid.a[0][1]", index)
    assert len(hits) == 1
    assert hits[0].port_found
    assert hits[0].full_path == "top.u_mid.a[0][1]"

    hits_glob = search_hierarchy_path(rows, "top.u_mid.a[*][1]", index)
    assert {h.port_name for h in hits_glob} == {"a[0][1]", "a[1][1]"}


def test_param_2d_port_literal_search(tmp_path):
    rtl = tmp_path / "p2d.v"
    rtl.write_text(
        """
module top;
  child #( .M(1), .N(2) ) u0 ();
endmodule
module child #(
    parameter int M = 0,
    parameter int N = 0
) (
    input logic [M:0][N:0] a
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    row = next(r for r in rows if r.inst_leaf == "u0")

    hits = search_hierarchy_path(rows, "top.u0.a[0][1]", index)
    assert len(hits) == 1
    assert hits[0].port_found
    assert hits[0].full_path == "top.u0.a[0][1]"
    assert "resolved" in hits[0].port_param_note

    miss = search_hierarchy_path(rows, "top.u0.a[9][9]", index)
    assert len(miss) == 1
    assert miss[0].port_found is False
    assert miss[0].match_kind == "hierarchy-port-miss"


def test_param_port_literal_bounds_without_full_expand(tmp_path):
    rtl = tmp_path / "pcap.v"
    rtl.write_text(
        """
module top;
  child #( .W(32) ) u0 ();
endmodule
module child #(parameter int W = 8) (
    input logic [W-1:0] data
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    row = next(r for r in rows if r.inst_leaf == "u0")
    idx = port_index_for_module(row.file, row.module, row.param_ctx)
    info = idx["data"]
    assert match_literal_port_indices(info, ["15"], row.param_ctx)

    hits = search_hierarchy_path(rows, "top.u0.data[15]", index)
    assert len(hits) == 1
    assert hits[0].full_path == "top.u0.data[15]"


def test_unresolved_param_port_literal_misses(tmp_path):
    rtl = tmp_path / "unres.v"
    rtl.write_text(
        """
module top;
  child u0 ();
endmodule
module child #(
    parameter int M = P,
    parameter int N = Q
) (
    input logic [M-1:0][N-1:0] a
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    row = next(r for r in rows if r.inst_leaf == "u0")
    idx = port_index_for_module(row.file, row.module, row.param_ctx)
    assert "a[M-1:0][N-1:0]" in idx
    assert "a[0][1]" not in idx
    assert idx["a[M-1:0][N-1:0]"].param_note.startswith("unresolved")

    miss = search_hierarchy_path(rows, "top.u0.a[0][1]", index)
    assert len(miss) == 1
    assert miss[0].port_found is False
    assert miss[0].match_kind == "hierarchy-port-miss"

    sym = search_hierarchy_path(rows, "top.u0.a[M-1:0][N-1:0]", index)
    assert len(sym) == 1


def test_path_refine_scoped_localparam_order(tmp_path):
    rtl = tmp_path / "scope.v"
    rtl.write_text(
        """
module top;
  child #( .W(W) ) u_early ();
  localparam W = 16;
  child #( .W(W) ) u_late ();
endmodule
module child #(parameter int W = 4) (
    input logic [W-1:0] data
);
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    early = refine_param_ctx_for_path(index, "top", "top.u_early")
    late = refine_param_ctx_for_path(index, "top", "top.u_late")
    assert early.ok and late.ok
    assert early.param_ctx.get("W") != "16"
    assert late.param_ctx.get("W") == "16"

    early_hits = search_hierarchy_path(rows, "top.u_early.data[15]", index)
    assert len(early_hits) == 1
    assert early_hits[0].port_found is False
    assert early_hits[0].match_kind == "hierarchy-port-miss"
    late_hits = search_hierarchy_path(rows, "top.u_late.data[15]", index)
    assert len(late_hits) == 1
    assert "path-refined" in late_hits[0].port_param_note


def test_hierarchy_glob_case_insensitive_literal(tmp_path):
    rtl = tmp_path / "case.v"
    rtl.write_text(
        """
module top;
  mid u_mid0 ( );
endmodule
module mid ( input wire clk ); endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    hits = search_hierarchy_path(
        rows, "top.U_MID0.clk", index, case_insensitive=True
    )
    assert len(hits) == 1
    assert hits[0].port_found
    assert hits[0].full_path == "top.u_mid0.clk"


def test_inst_search_glob():
    from hierwalk.models import FlatRow

    rows = [
        FlatRow(
            full_path="top.u_pcie0",
            inst_leaf="u_pcie0",
            module="pcie",
            depth=1,
            parent_path="top",
            file="a.v",
        ),
        FlatRow(
            full_path="top.u_ddr0",
            inst_leaf="u_ddr0",
            module="ddr",
            depth=1,
            parent_path="top",
            file="b.v",
        ),
    ]
    hits = search("u_pcie*", rows=rows)
    assert len(hits) == 1
    assert hits[0].matched_name == "u_pcie0"