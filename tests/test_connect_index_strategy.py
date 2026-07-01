"""Connect runs must default to path-walk (on-demand preprocess), not full filelist index."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hierwalk.cli_execute import execute_run
from hierwalk.run_request import (
    RunConfig,
    resolve_effective_index_strategy,
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


def test_connect_defaults_to_path_walk_index_strategy():
    cfg = RunConfig(
        filelist="fl.f",
        top="top",
        check_connect=("top.a", "top.z"),
    )
    assert resolve_effective_index_strategy(cfg, "check-connect") == "path-walk"


def test_explicit_full_index_connect_opt_in():
    cfg = RunConfig(
        filelist="fl.f",
        top="top",
        check_connect=("top.a", "top.z"),
        index_strategy="full-index",
    )
    assert resolve_effective_index_strategy(cfg, "check-connect") == "full-index"


def test_execute_run_cli_check_connect_skips_full_index_loader(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")

    class _Args:
        filelist = str(fl)
        top = "top"
        check_connect = ["top.a", "top.z"]
        check_connect_batch = None
        find_top = False
        all_tops = False
        output = "-"
        index_cwd = None
        define = []
        max_depth = None
        search = None
        search_subtree = False
        search_path = None
        search_module = False
        search_case_insensitive = False
        connect_trace = False
        connect_log = False
        include_ff = False
        fanin_cone = None
        fanout_cone = None
        cone_graph = None
        ignore_path = []
        ignore_path_file = []
        ignore_module = []
        ignore_filelist = []
        jobs = 0
        low_memory = False
        cache_dir = None
        no_cache = True
        refresh_cache = False
        quiet = True
        log_file = None
        no_log_file = True
        mode = ""

    cfg = run_config_from_args(_Args())

    class _Ap:
        def error(self, msg):
            raise SystemExit(msg)

    with patch("hierwalk.cli_execute.load_or_build_index") as load_full:
        load_full.side_effect = AssertionError(
            "load_or_build_index must not run for default check-connect"
        )
        rc = execute_run(cfg, _Ap())
    assert rc == 0
    load_full.assert_not_called()


def test_run_json_check_connect_batch_defaults_path_walk(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": str(fl),
                "top": "top",
                "mode": "check-connect-batch",
                "no_cache": True,
                "quiet": True,
                "no_log_file": True,
                "connect": {
                    "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                },
                "output": "-",
            }
        ),
        encoding="utf-8",
    )

    class _Ap:
        def error(self, msg):
            raise SystemExit(msg)

    from hierwalk.run_request import load_run_request

    cfg = load_run_request(run_json)
    assert resolve_effective_index_strategy(cfg, "check-connect-batch") == "path-walk"

    with patch("hierwalk.cli_execute.load_or_build_index") as load_full:
        load_full.side_effect = AssertionError("full index loader called")
        rc = execute_run(cfg, _Ap())
    assert rc == 0
    load_full.assert_not_called()