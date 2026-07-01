"""``{…}`` concat enforces MSB-first ordered bit mapping."""

from __future__ import annotations

from hierwalk.connect_expand import build_expand_meta, expand_check_to_pairs
from hierwalk.connect_request import parse_connect_request_json
from hierwalk.connectivity import (
    ConnectivitySession,
    format_connect_results_tsv,
    run_connectivity_request,
)
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk import run_path_walk_connect


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


def test_braced_concat_assign_bit_precise_connectivity(tmp_path):
    """``assign bus = {a,b,c}`` must not collapse unrelated bits in text-conn."""
    verilog = """
    module top;
      wire wa, wb, wc, wq;
      wire [2:0] ASD;
      assign ASD = {wa, wb, wc};
      assign wq = wb;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    parent = batch.results[0]
    assert len(parent.sub_results) == 3
    by_pair = {
        (sr.endpoint_a.spec, sr.endpoint_b.spec): sr.connected
        for sr in parent.sub_results
    }
    assert by_pair[("top.wa", "top.wq")] is False
    assert by_pair[("top.wc", "top.wq")] is False
    assert by_pair[("top.wb", "top.wq")] is True


def _fan_concat_by_pair(batch):
    parent = batch.results[0]
    return {
        (sr.endpoint_a.spec, sr.endpoint_b.spec): sr.connected
        for sr in parent.sub_results
    }


def test_braced_concat_text_conn_bit_precise(tmp_path):
    """Text-conn (resolve_param_dims=False) must keep braced-concat bit precision."""
    verilog = """
    module top;
      wire wa, wb, wc, wq;
      wire [2:0] ASD;
      assign ASD = {wa, wb, wc};
      assign wq = wb;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ]
        }
    )
    session = ConnectivitySession(rows=rows, index=index, top=top)
    session.resolve_param_dims = False
    text_batch = session.run_text_request(req)
    by_pair = _fan_concat_by_pair(text_batch)
    assert by_pair[("top.wa", "top.wq")] is False
    assert by_pair[("top.wc", "top.wq")] is False
    assert by_pair[("top.wb", "top.wq")] is True


def test_braced_concat_arity_fallback_implicit_bus(tmp_path):
    """Undeclared-width bus uses concat arity for per-bit links (no bus clique)."""
    verilog = """
    module top;
      wire wa, wb, wc, wq;
      wire bus;
      assign bus = {wa, wb, wc};
      assign wq = wb;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ]
        }
    )
    session = ConnectivitySession(rows=rows, index=index, top=top)
    session.resolve_param_dims = False
    by_pair = _fan_concat_by_pair(session.run_text_request(req))
    assert by_pair[("top.wa", "top.wq")] is False
    assert by_pair[("top.wc", "top.wq")] is False
    assert by_pair[("top.wb", "top.wq")] is True


def test_braced_concat_path_walk_text_bit_precise(tmp_path, monkeypatch):
    """Path-walk text-conn pipeline must not collapse braced-concat bits."""
    monkeypatch.setenv("HIERWALK_CONNECT_JOBS", "4")
    verilog = """
    module top;
      wire wa, wb, wc, wq;
      wire [2:0] ASD;
      assign ASD = {wa, wb, wc};
      assign wq = wb;
    endmodule
    """
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    fl_path = tmp_path / "fl.f"
    fl_path.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path))
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ],
            "top": "top",
        }
    )
    batch, _, _ = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase="text",
        connect_jobs=4,
        no_cache=True,
    )
    by_pair = _fan_concat_by_pair(batch)
    assert by_pair[("top.wa", "top.wq")] is False
    assert by_pair[("top.wc", "top.wq")] is False
    assert by_pair[("top.wb", "top.wq")] is True