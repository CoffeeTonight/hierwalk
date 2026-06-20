"""Instance-level driver/sinker trace (inst + direction + path_kind)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.cone import fanin_cone
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.inst_trace import (
    InstTraceRequest,
    parse_inst_trace_json,
    run_inst_trace,
)
from hierwalk.run_request import parse_run_request_json


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


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows


def test_parse_inst_trace_json_aliases():
    req = parse_inst_trace_json(
        {
            "instance": "top.u_m",
            "direction": "in",
            "ff/comb": "ff",
        },
        top="top",
    )
    assert req.instance == "top.u_m"
    assert req.direction == "driver"
    assert req.path_kind == "ff"
    assert req.top == "top"


def test_parse_run_request_inst_trace_mode(tmp_path: Path):
    cfg = parse_run_request_json(
        {
            "filelist": "design.f",
            "top": "top",
            "mode": "inst-trace",
            "inst_trace": {
                "instance": "top.u_m",
                "direction": "both",
                "path_kind": "ff",
            },
        },
        base_dir=tmp_path,
    )
    assert cfg.inst_trace is not None
    assert cfg.inst_trace.instance == "top.u_m"
    assert cfg.inst_trace.path_kind == "ff"


def test_inst_trace_driver_ff_finds_port_in(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = run_inst_trace(
        InstTraceRequest(
            instance="top.u_m",
            direction="driver",
            path_kind="ff",
        ),
        rows=rows,
        index=index,
        top="top",
    )
    assert not result.errors
    din = next(pr for pr in result.port_results if pr.port_name == "din")
    assert any(
        b.kind == "port-in" and b.scope == "top"
        for b in din.cone.boundaries
    )


def test_inst_trace_sinker_comb_finds_port_out(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = run_inst_trace(
        InstTraceRequest(
            instance="top.u_m",
            direction="sinker",
            path_kind="comb",
        ),
        rows=rows,
        index=index,
        top="top",
    )
    assert not result.errors
    assert result.port_results[0].port_name == "qout"
    assert any(
        b.kind == "port-out" and b.scope == "top" and b.net == "z"
        for _, b in result.boundaries
    )


def test_inst_trace_both_traces_input_and_output(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    result = run_inst_trace(
        InstTraceRequest(instance="top.u_m", direction="both", path_kind="ff"),
        rows=rows,
        index=index,
        top="top",
    )
    assert not result.errors
    traced = {(pr.port_name, pr.trace_direction) for pr in result.port_results}
    assert ("din", "driver") in traced
    assert ("qout", "sinker") in traced


def test_path_kind_comb_stops_at_ff_driver(tmp_path: Path):
    index, rows = _index_and_rows(CONE_RTL, tmp_path)
    comb = fanin_cone("top.z", rows=rows, index=index, top="top", path_kind="comb")
    ff = fanin_cone("top.z", rows=rows, index=index, top="top", path_kind="ff")
    assert any(b.kind == "ff-driver" for b in comb.boundaries)
    assert any(b.kind == "port-in" for b in ff.boundaries)


def test_cli_inst_trace_smoke(tmp_path: Path):
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
                "mode": "inst-trace",
                "inst_trace": {
                    "instance": "top.u_m",
                    "direction": "driver",
                    "path_kind": "ff",
                },
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
    assert "origin_port\ttrace_direction" in proc.stdout
    assert "port-in" in proc.stdout