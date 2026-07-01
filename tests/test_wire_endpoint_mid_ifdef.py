"""Signal/net endpoints: ifdef-style mid_ifdef chain (unified_verify pattern)."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import check_connectivity
from hierwalk.connect.shared.endpoints import resolve_endpoint
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk import run_path_walk_connect


def _mid_ifdef_text() -> str:
    return """
module MID_IFDEF_CHILD1;
input a;
output b;
wire b=a;
endmodule

module MID_IFDEF_CHILD;
input a;
output b;
MID_IFDEF_CHILD1 u_mid_ifdef_child1 (.a(a), .b(b));
endmodule

module mid_ifdef (
module mid_ifdef (
    input logic clk,
    input logic rst_n
);
wire c;
MID_IFDEF_CHILD u_mid_ifdef_child (.a(clk), .b(c));
endmodule

module hc_verify_top (input logic clk, input logic rst_n);
    mid_ifdef u_ifdef (.clk(clk), .rst_n(rst_n));
endmodule
"""


def test_mid_ifdef_child1_b_connects_to_internal_wire_c(tmp_path: Path):
    rtl = tmp_path / "soc.v"
    rtl.write_text(_mid_ifdef_text(), encoding="utf-8")
    index = DesignIndex.build({str(rtl): _mid_ifdef_text()})
    _, rows = elaborate(index, "hc_verify_top")

    ep, errors = resolve_endpoint(
        "hc_verify_top.u_ifdef.c",
        rows,
        index,
        top="hc_verify_top",
    )
    assert not errors
    assert ep.port_found

    r = check_connectivity(
        "hc_verify_top.u_ifdef.u_mid_ifdef_child.u_mid_ifdef_child1.b",
        "hc_verify_top.u_ifdef.c",
        rows=rows,
        index=index,
        top="hc_verify_top",
    )
    assert r.connected


def test_path_walk_no_miss_inst_for_internal_wire_c(tmp_path: Path):
    """``hc_verify_top.u_ifdef.c`` is a signal endpoint; must not log ``miss inst=c``."""
    rtl = tmp_path / "soc.v"
    rtl.write_text(_mid_ifdef_text(), encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    buf = io.StringIO()
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                "hc_verify_top.u_ifdef.u_mid_ifdef_child.u_mid_ifdef_child1.b",
                "hc_verify_top.u_ifdef.c",
            ),
        ),
        top="hc_verify_top",
    )
    batch, _index, _state = run_path_walk_connect(
        req,
        flr,
        top="hc_verify_top",
        no_cache=True,
        trace_stream=buf,
    )
    assert batch.results[0].connected is True
    text = buf.getvalue()
    text_phase = text.split("connect-text-conn done", 1)[0]
    assert "miss inst=c under hc_verify_top.u_ifdef" not in text_phase
    assert "miss inst=c " not in text_phase
    assert "signal-tail hit kind=wire" in text
    assert "tail='c'" in text
    assert "target=hc_verify_top.u_ifdef.c" in text