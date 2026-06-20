"""Tests for rich connectivity check expansion (list, concat, loop, map)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hierwalk.connect_expand import (
    build_expand_meta,
    expand_check_to_pairs,
    parse_endpoint_elements,
)
from hierwalk.connect_request import (
    parse_connect_request_json,
    connect_request_to_json,
)
from hierwalk.connectivity import check_connectivity, run_connectivity_request
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows, top


@pytest.fixture
def simple_bus_rtl(tmp_path: Path):
    verilog = """
    module top(input logic clk);
      wire a0, a1;
      wire [1:0] bus_b;
      assign a0 = clk;
      assign a1 = clk;
      assign bus_b[0] = a0;
      assign bus_b[1] = a1;
    endmodule
    """
    return _index_and_rows(verilog, tmp_path)


@pytest.fixture
def fanout_rtl(tmp_path: Path):
    verilog = """
    module top(input logic src);
      wire d0, d1;
      assign d0 = src;
      assign d1 = src;
    endmodule
    """
    return _index_and_rows(verilog, tmp_path)


def test_parse_concat_one_liner():
    display, elements, is_list, is_concat = parse_endpoint_elements(
        "{top.a, 1'b0, top.b}"
    )
    assert is_list
    assert is_concat
    assert display == "{top.a, 1'b0, top.b}"
    assert elements == ("top.a", "1'b0", "top.b")


def test_list_with_literals_rejected():
    with pytest.raises(ValueError, match="concat form"):
        build_expand_meta(["top.a", "1'b0", "top.b"], "top.bus_b[1:0]")


def test_parse_connect_request_loop_without_kind():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "gen_clk",
                    "a": "top.gen_loop{I}.tie",
                    "b": "top.ref",
                    "loop": {"I": "0:3"},
                },
                {
                    "id": "bus_map",
                    "a": ["top.bus_a[0]", "top.bus_a[1]"],
                    "b": "top.bus_b[1:0]",
                },
                {
                    "id": "concat",
                    "a": "{top.a0, 1'b0, top.a1}",
                    "b": "top.bus_b[1:0]",
                },
            ]
        }
    )
    assert len(req.checks) == 3
    assert req.checks[0].expand is not None
    assert len(req.checks[0].expand.elements_a) == 4
    assert req.checks[1].expand is not None
    assert req.checks[1].expand.map_kind == "array"
    assert req.checks[2].expand is not None
    assert req.checks[2].expand.map_kind == "concat"


def test_expand_loop_range_string():
    meta = build_expand_meta(
        "top.gen_loop{I}.tie",
        "top.ref",
        loop={"I": "0:3"},
    )
    pairs = expand_check_to_pairs("top.gen_loop{I}.tie", "top.ref", expand=meta)
    assert len(pairs) == 4
    assert pairs[0].endpoint_a == "top.gen_loop0.tie"
    assert pairs[3].endpoint_a == "top.gen_loop3.tie"
    assert all(p.endpoint_b == "top.ref" for p in pairs)


def test_expand_loop_explicit_list():
    meta = build_expand_meta(
        "top.cell{I}.sig",
        "top.ref",
        loop={"I": [0, 1, 2, 3, 5]},
    )
    pairs = expand_check_to_pairs("top.cell{I}.sig", "top.ref", expand=meta)
    assert [p.endpoint_a for p in pairs] == [
        "top.cell0.sig",
        "top.cell1.sig",
        "top.cell2.sig",
        "top.cell3.sig",
        "top.cell5.sig",
    ]


def test_expand_loop_adjacent_placeholders_cartesian():
    meta = build_expand_meta(
        "top.cell{I}{J}.sig",
        "top.ref",
        loop={"I": "x,y,z", "J": "A,B,C"},
    )
    pairs = expand_check_to_pairs("top.cell{I}{J}.sig", "top.ref", expand=meta)
    assert len(pairs) == 9
    assert [p.endpoint_a for p in pairs] == [
        "top.cellxA.sig",
        "top.cellxB.sig",
        "top.cellxC.sig",
        "top.cellyA.sig",
        "top.cellyB.sig",
        "top.cellyC.sig",
        "top.cellzA.sig",
        "top.cellzB.sig",
        "top.cellzC.sig",
    ]


def test_expand_array_bit_align_without_kind():
    meta = build_expand_meta(
        ["top.x[0]", "top.x[1]"],
        "top.y[1:0]",
        map_spec={"bit_align": "msb"},
    )
    pairs = expand_check_to_pairs("[top.x[0], top.x[1]]", "top.y[1:0]", expand=meta)
    assert len(pairs) == 2
    assert pairs[0].endpoint_a == "top.x[0]"
    assert pairs[0].endpoint_b == "top.y[1]"
    assert pairs[1].endpoint_b == "top.y[0]"


def test_expand_concat_skips_literals():
    meta = build_expand_meta("{top.a0, 1'b0, top.a1}", "top.bus_b[1:0]")
    pairs = expand_check_to_pairs("{top.a0, 1'b0, top.a1}", "top.bus_b[1:0]", expand=meta)
    assert len(pairs) == 2
    assert pairs[0].endpoint_a == "top.a0"
    assert pairs[0].endpoint_b == "top.bus_b[1]"
    assert pairs[1].endpoint_a == "top.a1"
    assert pairs[1].endpoint_b == "top.bus_b[0]"


def test_fanout_inferred_without_kind(fanout_rtl):
    index, rows, top = fanout_rtl
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": "top.src",
                    "b": ["top.d0", "top.d1"],
                }
            ]
        }
    )
    assert req.checks[0].expand is not None
    assert req.checks[0].expand.map_kind == "fanout"
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    r = batch.results[0]
    assert r.connected
    assert len(r.sub_results) == 2


def test_array_bus_connectivity_without_kind(simple_bus_rtl):
    index, rows, top = simple_bus_rtl
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "a": ["top.a0", "top.a1"],
                    "b": "top.bus_b[1:0]",
                }
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    r = batch.results[0]
    assert r.connected
    assert len(r.sub_results) == 2


def test_concat_string_connectivity(simple_bus_rtl):
    index, rows, top = simple_bus_rtl
    result = check_connectivity(
        "{top.a0, 1'b0, top.a1}",
        "top.bus_b[1:0]",
        rows=rows,
        index=index,
        top=top,
    )
    assert not result.connected or result.mode == "unknown"

    req = parse_connect_request_json(
        {
            "checks": [
                {"a": "{top.a0, 1'b0, top.a1}", "b": "top.bus_b[1:0]"},
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    assert batch.results[0].connected


def test_round_trip_json_uses_loop_not_kind():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "x",
                    "a": "top.cell{I}{J}.sig",
                    "b": "top.ref",
                    "loop": {"I": "x,y,z", "J": "A,B,C"},
                },
                {
                    "id": "y",
                    "a": ["top.p0", "top.p1"],
                    "b": "top.bus[1:0]",
                    "map": {"bit_align": "msb"},
                },
            ]
        }
    )
    payload = json.loads(connect_request_to_json(req))
    assert "loop" in payload["checks"][0]
    assert "bind" not in payload["checks"][0]
    assert "kind" not in payload["checks"][1].get("map", {})

    again = parse_connect_request_json(payload)
    assert again.checks[0].expand is not None
    assert len(again.checks[0].expand.elements_a) == 9
    assert again.checks[1].expand.bit_align == "msb"


def test_bind_alias_still_parsed():
    req = parse_connect_request_json(
        {
            "checks": [
                {"a": "top.gen{I}.x", "b": "top.y", "bind": {"I": "0:1"}},
            ]
        }
    )
    assert req.checks[0].expand is not None
    assert len(req.checks[0].expand.elements_a) == 2