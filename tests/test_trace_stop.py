"""Trace stop policy: ignore_hierarchy + trace_max_depth."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.cone import fanout_cone
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.inst_trace import parse_inst_trace_json, run_inst_trace
from hierwalk.trace_stop import (
    hierarchy_ignore_match,
    parse_trace_stop_policy,
)


DEEP_RTL = """
module top(input logic a, output logic z);
  wire w;
  assign w = a;
  block_a u_a (.x(w), .o());
  block_b u_b (.i(w), .z(z));
endmodule
module block_a(input logic x, output logic o);
  leaf deep (.p(x), .q(o));
endmodule
module block_b(input logic i, output logic z);
  assign z = i;
endmodule
module leaf(input logic p, output logic q);
  assign q = p;
endmodule
"""


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows


def test_parse_trace_stop_policy_numeric_in_ignore_list():
    pol = parse_trace_stop_policy({"ignore_hierarchy": ["top.a.*", 2]})
    assert pol.ignore_hierarchy == ("top.a.*",)
    assert pol.trace_max_depth == 2


def test_parse_trace_stop_policy_explicit_depth():
    pol = parse_trace_stop_policy(
        {
            "ignore_hierarchy": ["top.u_a.*"],
            "trace_max_depth": 1,
        }
    )
    assert pol.trace_max_depth == 1


def test_hierarchy_ignore_descendants_only():
    assert hierarchy_ignore_match("top.a.b.c", "top.a.b.c.*") is False
    assert hierarchy_ignore_match("top.a.b.c.d", "top.a.b.c.*") is True


def test_fanout_cone_ignore_hierarchy_stops_below_prefix(tmp_path: Path):
    index, rows = _index_and_rows(DEEP_RTL, tmp_path)
    origin = "top.u_a.x"
    open_result = fanout_cone(origin, rows=rows, index=index, top="top", path_kind="ff")
    stopped = fanout_cone(
        origin,
        rows=rows,
        index=index,
        top="top",
        path_kind="ff",
        ignore_hierarchy=["top.u_a.*"],
    )
    assert any(b.scope == "top.u_a.deep" for b in open_result.boundaries)
    assert any(
        b.kind == "ignore-hierarchy" and b.scope == "top.u_a.deep"
        for b in stopped.boundaries
    )


CONE_RTL = """
module top(input logic clk, input logic a, output logic z);
  wire mid;
  assign mid = a;
  mid u_m (.clk(clk), .din(mid), .qout(z));
endmodule
module mid(input logic clk, input logic din, output logic qout);
  logic r;
  always_ff @(posedge clk) r <= din;
  assign qout = r;
endmodule
"""


def test_fanout_cone_trace_max_depth_limits_hierarchy(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    deep = fanout_cone(
        "top.a",
        rows=rows,
        index=index,
        top="top",
        path_kind="comb",
    )
    shallow = fanout_cone(
        "top.a",
        rows=rows,
        index=index,
        top="top",
        path_kind="comb",
        trace_max_depth=0,
    )
    assert any(b.kind == "ff-sink" for b in deep.flip_flops)
    assert not any(b.kind == "ff-sink" for b in shallow.flip_flops)
    assert any(b.kind == "trace-depth" for b in shallow.boundaries)


def test_inst_trace_parse_and_run_honors_trace_stop(tmp_path: Path):
    index, rows = _index_and_rows(DEEP_RTL, tmp_path)
    req = parse_inst_trace_json(
        {
            "instance": "top.u_a",
            "direction": "sinker",
            "path_kind": "ff",
            "ignore_hierarchy": ["top.u_a.*"],
        }
    )
    assert req.trace_max_depth is None
    assert req.ignore_hierarchy == ("top.u_a.*",)
    result = run_inst_trace(req, rows=rows, index=index, top="top")
    assert not result.errors
    assert result.port_results
    kinds = {b.kind for pr in result.port_results for b in pr.cone.boundaries}
    assert "ignore-hierarchy" in kinds


def test_parse_rejects_negative_depth():
    with pytest.raises(ValueError, match="trace_max_depth"):
        parse_trace_stop_policy({"trace_max_depth": -1})