"""--check-connect-batch JSON must accept full run-level configuration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.cli import _build_parser
from hierwalk.connect_request import try_parse_connect_request_json
from hierwalk.run_request import (
    merge_options_from_connect_batch_json,
    resolve_effective_run_mode,
    run_config_from_args,
)


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


def test_try_parse_connect_without_checks_returns_none():
    assert try_parse_connect_request_json({"filelist": "top.f", "mode": "inst-trace"}) is None


def test_batch_json_applies_ignore_module_and_max_depth(tmp_path: Path):
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "mode": "hierarchy",
                "ignore_module": ["bb_mod"],
                "max_depth": 3,
                "no_cache": True,
            }
        ),
        encoding="utf-8",
    )
    ap = _build_parser()
    args = ap.parse_args(["--check-connect-batch", str(batch)])
    cli = run_config_from_args(args)
    merged, _src, _, _ = merge_options_from_connect_batch_json(cli, batch, args)
    assert merged.filelist.endswith("design.f")
    assert merged.mode == "hierarchy"
    assert merged.ignore_module == ("bb_mod",)
    assert merged.max_depth == 3
    assert merged.no_cache is True
    assert resolve_effective_run_mode(merged, None) == "hierarchy"


def test_inst_trace_mode_from_batch_json_only(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "mode": "inst-trace",
                "top": "top",
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
        ["hier-walk", "--check-connect-batch", str(batch)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "run: mode=inst-trace" in proc.stderr
    assert "inst-trace:" in proc.stderr
    assert "port-in" in proc.stdout


def test_path_walk_from_batch_without_config(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        "module top(input a, output z); assign z = a; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "mode": "path-walk",
                "top": "top",
                "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                "no_cache": True,
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", "--check-connect-batch", str(batch)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "run: mode=path-walk" in proc.stderr
    assert "path-walk: on-demand index" in proc.stderr