"""Hierarchy path → RTL file mapping."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.path_chain import (
    attach_path_chains,
    build_path_chain,
    format_path_chain_compact,
    format_path_chain_report,
)
from hierwalk.path_search import search_hierarchy_path
from hierwalk.preprocess import preprocess_file
from hierwalk.filelist import FilelistResult
from hierwalk.report import RunReport


def test_path_chain_maps_inst_and_port(tmp_path):
    top_v = tmp_path / "top.v"
    mid_v = tmp_path / "mid.v"
    top_v.write_text(
        """
module top;
  mid u_mid ( );
endmodule
""",
        encoding="utf-8",
    )
    mid_v.write_text(
        """
module mid (
    input wire clk
);
endmodule
""",
        encoding="utf-8",
    )
    top_text = preprocess_file(top_v, [], {})
    mid_text = preprocess_file(mid_v, [], {})
    index = DesignIndex.build({str(top_v): top_text, str(mid_v): mid_text})
    _root, rows = elaborate(index, "top")

    hits = search_hierarchy_path(rows, "top.u_mid.clk", index)
    assert len(hits) == 1
    chain = hits[0].path_chain
    assert len(chain) == 3
    assert chain[0].role == "root"
    assert chain[0].module == "top"
    assert chain[0].rtl_file == str(top_v)
    assert chain[1].role == "instance"
    assert chain[1].inst == "u_mid"
    assert chain[1].module == "mid"
    assert chain[1].rtl_file == str(mid_v)
    assert chain[1].inst_decl_file == str(top_v)
    assert chain[2].role == "port"
    assert chain[2].port_name == "clk"
    assert chain[2].rtl_file == str(mid_v)
    assert chain[2].port_line > 0

    compact = format_path_chain_compact(chain)
    assert "root|top|top" in compact
    assert "instance|u_mid|mid" in compact
    assert "port|clk|mid" in compact


def test_path_chain_report_section(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  leaf u_leaf ( );
endmodule
module leaf ( input wire a ); endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    hits = search_hierarchy_path(rows, "top.u_leaf.a", index)

    report = RunReport(
        filelist_path=str(tmp_path / "design.f"),
        elapsed_sec=0.1,
        fl=FilelistResult(source_files=[str(rtl)]),
        index=index,
        search_hits=len(hits),
        search_pattern="top.u_leaf.a",
        search_hit_details=hits,
        mode="search",
    )
    body = "\n".join(report.lines())
    assert "Search path mapping" in body
    assert "top.u_leaf.a" in body
    report_lines = format_path_chain_report(hits[0].path_chain)
    assert any("u_leaf" in line and "inst" in line for line in report_lines)
    assert any("port" in line for line in report_lines)