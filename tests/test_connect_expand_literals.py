"""Verify connect-expand examples and Verilog literal forms (1'b0, 2'h0, 3'h7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hierwalk.connect_expand import (
    _is_const_literal,
    build_expand_meta,
    expand_check_to_pairs,
    needs_expansion,
)
from hierwalk.connect_request import connect_request_to_json, parse_connect_request_json
from hierwalk.connectivity import run_connectivity_request
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


LITERALS = ("1'b0", "2'h0", "3'h7", "4'b1010", "8'd255", "1'b1", "2'hF")


@pytest.mark.parametrize("lit", LITERALS)
def test_verilog_literals_recognized(lit: str):
    assert _is_const_literal(lit)


def _elab(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows, top


@pytest.fixture
def concat_literal_rtl(tmp_path: Path):
    """Two signals with literals between them map to a 2-bit bus."""
    verilog = """
    module top(input logic clk);
      wire a, b;
      wire [1:0] bus2;
      assign a = clk;
      assign b = clk;
      assign bus2[1] = a;
      assign bus2[0] = b;
    endmodule
    """
    return _elab(verilog, tmp_path)


@pytest.fixture
def full_examples_rtl(tmp_path: Path):
    verilog = """
    module top(input logic clk, input logic src, input logic ref);
      wire a, b, d0, d1;
      wire [1:0] bus_b;
      wire tie0, tie1, tie2, tie3;
      wire xA, xB, yA, yB;

      assign a = clk;
      assign b = clk;
      assign bus_b[0] = a;
      assign bus_b[1] = b;
      assign d0 = src;
      assign d1 = src;
      assign tie0 = ref;
      assign tie1 = ref;
      assign tie2 = ref;
      assign tie3 = ref;
      assign xA = ref;
      assign xB = ref;
      assign yA = ref;
      assign yB = ref;
    endmodule
    """
    return _elab(verilog, tmp_path)


def test_concat_list_with_literals_rejected():
    with pytest.raises(ValueError, match="concat form"):
        build_expand_meta(["top.a", "1'b0", "2'h0", "top.b"], "top.bus2[1:0]")


def test_concat_oneliner_with_3h7_literal():
    meta = build_expand_meta(
        "{top.a, 1'b0, 2'h0, 3'h7, top.b}",
        "top.bus2[1:0]",
    )
    pairs = expand_check_to_pairs(
        "{top.a, 1'b0, 2'h0, 3'h7, top.b}",
        "top.bus2[1:0]",
        expand=meta,
    )
    assert len(pairs) == 2
    assert pairs[0].endpoint_a == "top.a"
    assert pairs[1].endpoint_a == "top.b"


def test_concat_string_literals_connectivity(concat_literal_rtl):
    index, rows, top = concat_literal_rtl
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "a": "{top.a, 1'b0, 2'h0, 3'h7, top.b}",
                    "b": "top.bus2[1:0]",
                }
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    r = batch.results[0]
    assert r.connected, f"errors={r.errors} note={r.note}"
    assert len(r.sub_results) == 2


def test_all_documented_examples_end_to_end(full_examples_rtl):
    index, rows, top = full_examples_rtl
    req = parse_connect_request_json(
        {
            "checks": [
                {"id": "simple", "a": "top.clk", "b": "top.a"},
                {"id": "fanout", "a": "top.src", "b": ["top.d0", "top.d1"]},
                {
                    "id": "array",
                    "a": ["top.a", "top.b"],
                    "b": "top.bus_b[1:0]",
                },
                {
                    "id": "concat_str",
                    "a": "{top.a, 1'b0, 2'h0, 3'h7, top.b}",
                    "b": "top.bus_b[1:0]",
                },
                {
                    "id": "loop_range",
                    "a": "top.tie{I}",
                    "b": "top.ref",
                    "loop": {"I": "0:3"},
                },
                {
                    "id": "loop_list",
                    "a": "top.tie{I}",
                    "b": "top.ref",
                    "loop": {"I": [0, 1, 2, 3]},
                },
                {
                    "id": "loop_csv",
                    "a": "top.{I}{J}",
                    "b": "top.ref",
                    "loop": {"I": "x,y", "J": "A,B"},
                },
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    by_id = {r.check_id: r for r in batch.results}
    assert len(by_id) == 7

    for cid in ("simple", "fanout", "array", "concat_str", "loop_range", "loop_list"):
        r = by_id[cid]
        assert r.connected, f"{cid}: errors={r.errors} note={r.note}"

    loop_csv = by_id["loop_csv"]
    assert loop_csv.connected
    assert len(loop_csv.sub_results) == 4
    specs = {sr.endpoint_a.spec for sr in loop_csv.sub_results}
    assert specs == {"top.xA", "top.xB", "top.yA", "top.yB"}


def test_round_trip_preserves_literal_concat_form():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "a": "{top.a, 1'b0, 2'h0, 3'h7, top.b}",
                    "b": "top.bus[1:0]",
                }
            ]
        }
    )
    again = parse_connect_request_json(json.loads(connect_request_to_json(req)))
    chk = again.checks[0]
    assert chk.expand is not None
    assert chk.expand.map_kind == "concat"
    assert "1'b0" in chk.expand.elements_a
    assert "2'h0" in chk.expand.elements_a
    assert "3'h7" in chk.expand.elements_a