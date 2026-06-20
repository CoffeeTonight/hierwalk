"""``{…}`` concat enforces MSB-first ordered bit mapping."""

from __future__ import annotations

from hierwalk.connect_expand import build_expand_meta, expand_check_to_pairs
from hierwalk.connect_request import parse_connect_request_json
from hierwalk.connectivity import format_connect_results_tsv, run_connectivity_request
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _elab(verilog: str, tmp_path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows, top


def test_concat_msb_first_pair_order():
    meta = build_expand_meta("{top.a, top.b}", "top.bus[1:0]")
    assert meta.map_kind == "concat"
    pairs = expand_check_to_pairs("{top.a, top.b}", "top.bus[1:0]", expand=meta)
    assert [(p.endpoint_a, p.endpoint_b, p.sub_id) for p in pairs] == [
        ("top.a", "top.bus[1]", "[0]"),
        ("top.b", "top.bus[0]", "[1]"),
    ]


def test_array_lsb_zip_differs_from_concat(tmp_path):
    meta_concat = build_expand_meta("{top.a, top.b}", "top.bus[1:0]")
    meta_array = build_expand_meta(["top.a", "top.b"], "top.bus[1:0]")
    assert meta_concat.map_kind == "concat"
    assert meta_array.map_kind == "array"
    concat_pairs = expand_check_to_pairs(
        "{top.a, top.b}", "top.bus[1:0]", expand=meta_concat
    )
    array_pairs = expand_check_to_pairs(
        "[top.a, top.b]", "top.bus[1:0]", expand=meta_array
    )
    assert concat_pairs[0].endpoint_b == "top.bus[1]"
    assert array_pairs[0].endpoint_b == "top.bus[0]"


def test_concat_correct_order_connectivity(tmp_path):
    verilog = """
    module top(input logic clk, input logic rst);
      wire a, b;
      wire [1:0] bus;
      assign a = clk;
      assign b = rst;
      assign bus[1] = a;
      assign bus[0] = b;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "concat",
                    "a": "{top.a, top.b}",
                    "b": "top.bus[1:0]",
                }
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    parent = batch.results[0]
    assert parent.connected
    assert len(parent.sub_results) == 2

    tsv = format_connect_results_tsv(batch.results)
    assert "concat[0]\ttop.a\ttop.bus[1]\tTrue" in tsv
    assert "concat[1]\ttop.b\ttop.bus[0]\tTrue" in tsv