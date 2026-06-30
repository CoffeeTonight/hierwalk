"""Connect artifacts must list endpoints even when COI returns no rows."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_artifacts import (
    HierarchyEvidenceRow,
    SignalTailRecord,
    build_connect_results_from_request,
    collect_hierarchy_evidence,
    compact_hierarchy_evidence,
    format_connect_hierarchy_tsv,
    normalize_connect_results,
    normalize_hierarchy_kind,
)
from hierwalk.connect_expand import build_expand_meta, parse_endpoint_elements
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connectivity import ConnectivitySession, format_connect_results_report
from hierwalk.connect_endpoints import classify_signal_tail_kind
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _session(tmp_path: Path) -> ConnectivitySession:
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top(input logic clk);\n"
        "  wire c;\n"
        "  assign c = clk;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    text = rtl.read_text(encoding="utf-8")
    index = DesignIndex.build({str(rtl): text})
    _, rows = elaborate(index, "top")
    return ConnectivitySession(rows=rows, index=index, top="top")


def test_normalize_connect_results_from_request_when_coi_empty(tmp_path: Path):
    session = _session(tmp_path)
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.c", check_id="t"),),
        top="top",
    )
    out = normalize_connect_results(req, (), session)
    assert len(out) == 1
    assert out[0].check_id == "t"
    assert "top.clk" in out[0].endpoint_a.spec
    assert "top.c" in out[0].endpoint_b.spec
    report = format_connect_results_report(out, phase="text")
    assert any("top.clk -> top.c" in line for line in report)


def test_hierarchy_tsv_includes_signal_tail_hits(tmp_path: Path):
    session = _session(tmp_path)
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.c", check_id="t"),),
        top="top",
    )
    results = build_connect_results_from_request(req, session)
    tails = (
        SignalTailRecord(
            target_path="top.c",
            parent_path="top",
            tail="c",
            kind="wire",
            hit=True,
            module="top",
        ),
    )
    body = format_connect_hierarchy_tsv(
        results,
        session.rows_by_path,
        phase="text",
        signal_tails=tails,
    )
    rows = [ln for ln in body.splitlines() if ln.startswith("t\t")]
    assert len(rows) == 2
    assert "top.clk" in body
    assert "top.c" in body


def test_collect_hierarchy_evidence_includes_inst_port_wire_reg(tmp_path: Path):
    session = _session(tmp_path)
    row = session.rows_by_path["top"]
    assert classify_signal_tail_kind(session.index, row, "clk", top="top") == "port"
    assert classify_signal_tail_kind(session.index, row, "c", top="top") == "wire"
    rtl = tmp_path / "top_reg.v"
    rtl.write_text(
        "module top(input logic clk);\n"
        "  reg r;\n"
        "  assign r = clk;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    text = rtl.read_text(encoding="utf-8")
    index = DesignIndex.build({str(rtl): text})
    _, rows = elaborate(index, "top")
    reg_session = ConnectivitySession(rows=rows, index=index, top="top")
    reg_row = reg_session.rows_by_path["top"]
    assert classify_signal_tail_kind(index, reg_row, "r", top="top") == "reg"
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.c", check_id="t"),),
        top="top",
    )
    results = build_connect_results_from_request(req, session)
    tails = (
        SignalTailRecord("top.c", "top", "c", "wire", True, "top"),
        SignalTailRecord("top.clk", "top", "clk", "port", True, "top"),
    )
    evidence = collect_hierarchy_evidence(
        results,
        session.rows_by_path,
        signal_tails=tails,
        index=session.index,
        top="top",
    )
    kinds = {row.kind for row in evidence}
    assert "inst" in kinds
    assert "port" in kinds
    assert "wire" in kinds
    assert normalize_hierarchy_kind("wire-prefix") == "wire"
    report = format_connect_results_report(
        results,
        phase="text",
        rows_by_path=session.rows_by_path,
        signal_tails=tails,
        index=session.index,
        top="top",
    )
    assert any("port" in line for line in report)
    assert any("wire" in line for line in report)
    compact = compact_hierarchy_evidence(evidence)
    assert len([r for r in compact if r.check_id == "t" and r.side == "a"]) == 1
    assert len([r for r in compact if r.check_id == "t" and r.side == "b"]) == 1


def test_hierarchy_tsv_expands_list_endpoint_display(tmp_path: Path):
    """Bracket list display must not become paths like ``[top`` in hierarchy TSV."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "  wire e, u;\n"
        "  wire c;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(rows=rows, index=index, top="top")

    display_a, _, _, _ = parse_endpoint_elements(
        ["top.a.b.e.r.t", "top.a.b.u.i.o"]
    )
    display_b, _, _, _ = parse_endpoint_elements("top.a.b.c")
    meta = build_expand_meta(
        ["top.a.b.e.r.t", "top.a.b.u.i.o"],
        "top.a.b.c",
    )
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(display_a, display_b, check_id="lst", expand=meta),
        ),
        top="top",
    )
    results = build_connect_results_from_request(req, session)
    assert len(results) == 1
    assert len(results[0].sub_results) == 2

    body = format_connect_hierarchy_tsv(
        results,
        session.rows_by_path,
        phase="text",
        index=index,
        top="top",
    )
    assert "[top" not in body
    assert "top.a.b.e.r.t" in body
    assert "top.a.b.u.i.o" in body
    assert "top.a.b.c" in body

    evidence = collect_hierarchy_evidence(
        results,
        session.rows_by_path,
        index=index,
        top="top",
    )
    paths = {row.path for row in evidence}
    assert "[top" not in paths
    assert "top" in paths