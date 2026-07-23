"""Structural electrical p2p (pyslang AST) + pyslangwalk.report."""

from __future__ import annotations

from pathlib import Path

import pytest

pyslang = pytest.importorskip("pyslang")

from hierwalk.connect.pyslang_electrical import (
    build_electrical_graph,
    format_electrical_report,
    query_a_to_b,
)
from hierwalk.connect.pyslang_walk_gate import run_pyslangwalk_connect_batch
from hierwalk.connect.shared.request import parse_connect_request_json


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_electrical_bus_slice_mapping(tmp_path: Path):
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s0, s1;
          leaf u0 (.d(bus[0]), .q(s0));
          leaf u1 (.d(bus[1]), .q(s1));
        endmodule
        """,
    )
    uf, _ = build_electrical_graph([rtl], top="top")
    rows = query_a_to_b(
        uf,
        check_id="map",
        a_specs=["top.s0", "top.s1"],
        b_specs=["top.bus"],
    )
    by_a = {r.a: r for r in rows}
    assert by_a["top.s0"].status == "PASS"
    assert by_a["top.s0"].b_slice == "top.bus[0]"
    assert by_a["top.s1"].status == "PASS"
    assert by_a["top.s1"].b_slice == "top.bus[1]"


def test_electrical_second_compile_in_process_not_empty(tmp_path: Path):
    """SourceManager path reuse must not yield nets≈0 on the second call."""
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s0;
          leaf u0 (.d(bus[0]), .q(s0));
        endmodule
        """,
    )
    uf1, _ = build_electrical_graph([rtl], top="top")
    uf2, _ = build_electrical_graph([rtl], top="top")
    assert len(uf1._p) > 0
    assert len(uf2._p) > 0
    rows = query_a_to_b(
        uf2, check_id="c", a_specs=["top.s0"], b_specs=["top.bus"]
    )
    assert rows[0].status == "PASS"
    assert rows[0].b_slice == "top.bus[0]"


def test_electrical_parameter_index_idx(tmp_path: Path):
    """``bus[IDX]`` / ``bus[N-1]`` fold via pyslang parameter elaboration."""
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          parameter int IDX = 1;
          parameter int N = 2;
          logic [3:0][3:0] bus;
          logic [3:0] s_param, s_expr;
          leaf u0 (.d(bus[IDX]), .q(s_param));
          leaf u1 (.d(bus[N-1]), .q(s_expr));
        endmodule
        """,
    )
    uf, _ = build_electrical_graph([rtl], top="top")
    rows = query_a_to_b(
        uf,
        check_id="param",
        a_specs=["top.s_param", "top.s_expr"],
        b_specs=["top.bus"],
    )
    by_a = {r.a: r for r in rows}
    assert by_a["top.s_param"].status == "PASS"
    assert by_a["top.s_param"].b_slice == "top.bus[1]"
    assert by_a["top.s_expr"].status == "PASS"
    assert by_a["top.s_expr"].b_slice == "top.bus[1]"


def test_electrical_runtime_var_index_not_folded(tmp_path: Path):
    """True runtime variable select must not invent a bit-slice edge."""
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s;
          int idx;
          leaf u0 (.d(bus[idx]), .q(s));
        endmodule
        """,
    )
    uf, _ = build_electrical_graph([rtl], top="top")
    rows = query_a_to_b(
        uf,
        check_id="var",
        a_specs=["top.s"],
        b_specs=["top.bus"],
    )
    assert rows[0].status == "FAIL"


def test_electrical_no_logic_ops_linked(tmp_path: Path):
    """``assign y = a & b`` must not create electrical edges."""
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module top;
          logic [3:0] a, b, y;
          assign y = a & b;
        endmodule
        """,
    )
    uf, _ = build_electrical_graph([rtl], top="top")
    rows = query_a_to_b(
        uf,
        check_id="logic",
        a_specs=["top.a"],
        b_specs=["top.y"],
    )
    assert rows[0].status == "FAIL"


def test_pyslangwalk_writes_electrical_report(tmp_path: Path):
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s0, s1;
          leaf u0 (.d(bus[0]), .q(s0));
          leaf u1 (.d(bus[1]), .q(s1));
        endmodule
        """,
    )
    req = parse_connect_request_json(
        {
            "top": "top",
            "checks": [
                {
                    "id": "ab",
                    "a": ["top.s0", "top.s1"],
                    "b": ["top.bus"],
                },
            ],
        }
    )
    db = tmp_path / "db"
    batch, _, _ = run_pyslangwalk_connect_batch(
        req,
        [rtl],
        top="top",
        connect_output_dir=db,
        text_coi=False,
    )
    assert batch.results
    report = db / "pyslangwalk.report"
    assert report.is_file(), "pyslangwalk.report must be written"
    text = report.read_text(encoding="utf-8")
    assert "top.s0" in text and "top.s1" in text
    assert "PASS" in text
    assert "top.bus[0]" in text and "top.bus[1]" in text
    data_lines = [
        ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")
    ]
    assert len(data_lines) >= 2
    for ln in data_lines:
        parts = [p.strip() for p in ln.split("|")]
        assert len(parts) >= 4


def test_format_report_one_a_per_line():
    from hierwalk.connect.pyslang_electrical import ElectricalP2PRow

    text = format_electrical_report(
        [
            ElectricalP2PRow("c1", "top.a0", "top.bus[0]", "PASS"),
            ElectricalP2PRow("c1", "top.a1", "top.bus[1]", "PASS"),
            ElectricalP2PRow(
                "c2", "top.x", "", "FAIL", fail_node="x", fail_rtl="/t.v"
            ),
        ],
        top="top",
    )
    lines = [ln for ln in text.splitlines() if " | " in ln and not ln.startswith("#")]
    assert len(lines) == 3
    assert lines[0].count("|") >= 4
