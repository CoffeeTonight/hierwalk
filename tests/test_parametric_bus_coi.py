"""COI must tolerate parametric bus bounds like ``[STRB_MAX-1:0]``."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_scan import _md_suffixes_for_token, build_module_connect_index
from hierwalk.connectivity import check_connectivity
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def test_md_suffixes_parametric_bus_range_does_not_raise():
    suffixes = _md_suffixes_for_token(
        "data[STRB_MAX-1:0]",
        {"STRB_MAX": "4"},
        {},
        {},
    )
    assert suffixes is not None
    assert suffixes


def test_build_module_connect_index_parametric_port_width(tmp_path: Path):
    v = """
    module pkg;
      localparam int STRB_MAX = 4;
    endmodule
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    row = {r.full_path: r for r in rows}["top"]
    body = index.module_body("top")
    idx = build_module_connect_index(body, param_map={"STRB_MAX": "4"})
    assert "w_e" in idx.net_rep or any("w_e" in k for k in idx.net_rep)


def test_text_conn_skips_parametric_dim_resolution(tmp_path: Path):
    """Text-conn (structural) must not evaluate ``STRB_MAX`` — decl/assign only."""
    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    text_session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    text_result = text_session.check("top.data", "top.w_e")
    assert not any("STRB_MAX" in err for err in text_result.errors)
    assert text_result.connected

    logical_session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=True,
    )
    logical_result = logical_session.check("top.data[0]", "top.w_e[0]")
    assert logical_result.connected


def test_connectivity_parametric_bus_endpoints(tmp_path: Path):
    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    result = check_connectivity(
        "top.data[0]",
        "top.w_e[0]",
        rows=rows,
        index=index,
        top="top",
    )
    assert result.connected