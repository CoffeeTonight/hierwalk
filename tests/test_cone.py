"""Fanin/fanout cone mode (standalone COI debug)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.cone import fanin_cone, fanout_cone, format_cone_report
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.run_request import parse_run_request_json


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows


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


def test_fanout_cone_finds_ff_sink(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = fanout_cone("top.a", rows=rows, index=index, top="top")
    assert not result.errors
    ff_kinds = {b.kind for b in result.flip_flops}
    assert "ff-sink" in ff_kinds
    scopes = {b.scope for b in result.flip_flops}
    assert "top.u_m" in scopes


def test_fanin_cone_finds_ff_driver(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = fanin_cone("top.z", rows=rows, index=index, top="top")
    assert not result.errors
    assert any(b.kind == "ff-driver" for b in result.flip_flops)


def test_fanout_cone_port_out_boundary(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = fanout_cone("top.u_m.qout", rows=rows, index=index, top="top")
    assert not result.errors
    assert any(b.kind == "port-out" for b in result.ports)


def test_fanin_cone_port_in_boundary(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = fanin_cone("top.a", rows=rows, index=index, top="top")
    assert not result.errors
    assert any(b.kind == "port-in" for b in result.ports)


def test_fanout_cone_blackbox_boundary(tmp_path: Path):
    v = """
    module top(input logic a, output logic z);
      wire w;
      assign w = a;
      opaque u_o (.p(w), .q(z));
    endmodule
    module opaque(input logic p, output logic q);
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    result = fanout_cone("top.a", rows=rows, index=index, top="top")
    assert not result.errors
    assert result.blackboxes


def test_parse_run_request_cone_json(tmp_path: Path):
    cfg = parse_run_request_json(
        {
            "filelist": "design.f",
            "top": "top",
            "mode": "cone",
            "fanout-cone": "top.u_m.din",
            "cone-graph": "cone.dot",
        },
        base_dir=tmp_path,
    )
    assert cfg.fanout_cone == "top.u_m.din"
    assert cfg.cone_graph == str((tmp_path / "cone.dot").resolve())


def test_cli_fanout_cone_smoke(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": str(fl),
                "top": "top",
                "mode": "cone",
                "fanout-cone": "top.a",
                "no_cache": True,
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ff-sink" in proc.stderr or "ff-sink" in proc.stdout
    assert "kind\tscope\tnet" in proc.stdout


def test_cli_help_cone():
    proc = subprocess.run(
        ["hier-walk", "--help-cone"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "fanin" in proc.stdout.lower()
    assert "ff-sink" in proc.stdout