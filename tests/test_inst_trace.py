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
        b.kind == "port-out" and b.scope == "top"
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


BUS_ALIAS_RTL = """
module top(
  input  logic clk,
  input  logic [1:0] data,
  output logic [1:0] out
);
  bus_leaf u (
    .clk(clk),
    .a(data),
    .z(out)
  );
endmodule
module bus_leaf(
  input  logic clk,
  input  logic [1:0] a,
  output logic [1:0] z
);
  assign z = a;
endmodule
"""


def test_inst_trace_dedups_bus_alias_seeds(tmp_path: Path):
    index, rows = _index_and_rows(BUS_ALIAS_RTL, tmp_path)
    from hierwalk.inst_trace import _ports_for_instance, _seed_ports

    row = next(r for r in rows if r.full_path == "top.u")
    ports = _ports_for_instance(index, row, "top")
    raw = _seed_ports(ports, "both")
    assert len(raw) > 4
    result = run_inst_trace(
        InstTraceRequest(instance="top.u", direction="both", path_kind="comb"),
        rows=rows,
        index=index,
        top="top",
    )
    assert not result.errors
    assert len(result.port_results) < len(raw)
    traced = {(pr.port_name, pr.trace_direction) for pr in result.port_results}
    assert ("a", "driver") in traced
    assert ("z", "sinker") in traced
    assert not any("[" in name for name, _ in traced)


def test_cone_child_down_uses_inst_ports_not_alias_fanout(tmp_path: Path):
    """Child-down must not fan out on net_to_children slice aliases."""
    from hierwalk.cone import _child_down_links
    from hierwalk.connect.shared.endpoints import _module_index
    from hierwalk.connect.logical.scan import net_representative

    index, rows = _index_and_rows(BUS_ALIAS_RTL, tmp_path)
    comb = _module_index(
        {},
        index,
        "top",
        {},
        defines={},
        over_approximate_if=True,
        ff_barrier=True,
    )
    rep = net_representative(comb, "data")
    alias_children = len(comb.net_to_children.get(rep, ()))
    links = _child_down_links(comb, rep)
    assert alias_children >= 2
    assert len(links) < alias_children
    assert ("u", "a") in links


def test_inst_trace_reuses_module_cache_across_port_cones(tmp_path: Path):
    from unittest.mock import patch

    index, rows = _index_and_rows(BUS_ALIAS_RTL, tmp_path)
    dedup_misses: list[str] = []
    cone_misses: list[str] = []
    comb_misses: list[str] = []
    in_cone_phase = False
    from hierwalk import cone as cone_mod

    orig_build = cone_mod._build_cone_module_index
    orig_module_index = cone_mod._module_index

    def counting_build(index_obj, mod_name, param_ctx, **kwargs):
        cache = kwargs.get("cache")
        ctx_key = "|".join(f"{k}={v}" for k, v in sorted(param_ctx.items()))
        key = (mod_name, ctx_key)
        if cache is None or key not in cache:
            target = cone_misses if in_cone_phase else dedup_misses
            target.append(mod_name)
        return orig_build(index_obj, mod_name, param_ctx, **kwargs)

    def counting_module_index(comb_cache, index_obj, mod_name, pmap, **kwargs):
        ctx_key = "|".join(f"{k}={v}" for k, v in sorted(pmap.items()))
        cache_key = (
            mod_name,
            ctx_key,
            "|".join(f"{k}={v}" for k, v in sorted((kwargs.get("defines") or {}).items())),
            str(kwargs.get("over_approximate_if", True)),
            str(kwargs.get("ff_barrier", False)),
        )
        if comb_cache is None or cache_key not in comb_cache:
            target = cone_misses if in_cone_phase else dedup_misses
            target.append(f"comb:{mod_name}")
        return orig_module_index(
            comb_cache, index_obj, mod_name, pmap, **kwargs
        )

    import hierwalk.inst_trace as inst_trace_mod

    orig_fanin = inst_trace_mod.fanin_cone
    orig_fanout = inst_trace_mod.fanout_cone

    def fanin_with_phase(*args, **kwargs):
        nonlocal in_cone_phase
        in_cone_phase = True
        try:
            return orig_fanin(*args, **kwargs)
        finally:
            in_cone_phase = False

    def fanout_with_phase(*args, **kwargs):
        nonlocal in_cone_phase
        in_cone_phase = True
        try:
            return orig_fanout(*args, **kwargs)
        finally:
            in_cone_phase = False

    with (
        patch.object(cone_mod, "_build_cone_module_index", side_effect=counting_build),
        patch.object(
            inst_trace_mod, "_build_cone_module_index", side_effect=counting_build
        ),
        patch.object(cone_mod, "_module_index", side_effect=counting_module_index),
        patch.object(inst_trace_mod, "fanin_cone", side_effect=fanin_with_phase),
        patch.object(inst_trace_mod, "fanout_cone", side_effect=fanout_with_phase),
    ):
        result = run_inst_trace(
            InstTraceRequest(instance="top.u", direction="both", path_kind="comb"),
            rows=rows,
            index=index,
            top="top",
        )
    traced = {(pr.port_name, pr.trace_direction) for pr in result.port_results}
    assert not result.errors
    assert ("a", "driver") in traced
    assert ("z", "sinker") in traced
    assert len(result.port_results) >= 2
    assert dedup_misses.count("bus_leaf") == 1
    assert cone_misses.count("bus_leaf") == 0
    assert cone_misses.count("comb:bus_leaf") == 0


NON_ANSI_RTL = """
module top(input logic clk);
  deep u (.clk(clk));
endmodule
module deep(clk, rst_n, din, dout);
  input logic clk;
  input logic rst_n;
  input logic din;
  output logic dout;
  assign dout = din;
endmodule
"""


def test_non_ansi_body_ports_get_direction(tmp_path: Path):
    index, rows = _index_and_rows(NON_ANSI_RTL, tmp_path)
    result = run_inst_trace(
        InstTraceRequest(instance="top.u", direction="driver", path_kind="comb"),
        rows=rows,
        index=index,
        top="top",
    )
    assert not result.errors
    assert result.port_results
    traced = {pr.port_name for pr in result.port_results}
    assert "din" in traced


def test_run_inst_trace_uses_effective_defines_not_filelist_seed(tmp_path: Path):
    """Cone must not re-merge filelist defines on top of RTL `` `undef ``."""
    from unittest.mock import patch

    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_text("`define FOO 1\n", encoding="utf-8")
    b.write_text(
        "`undef FOO\n"
        "module top(input logic a, output logic z);\n"
        "`ifdef FOO\n"
        "  assign z = a;\n"
        "`else\n"
        "  assign z = 1'b0;\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    a_path = str(a.resolve())
    b_path = str(b.resolve())
    index = DesignIndex.build_from_sources(
        [a_path, b_path],
        include_dirs=[str(tmp_path)],
        defines={"FOO": "1"},
        jobs=1,
        low_memory=True,
    )
    _, rows = elaborate(index, "top")
    captured: list[object] = []

    def _capture_fanin(*_args, **kwargs):
        captured.append(kwargs.get("defines"))
        return fanin_cone(*_args, **kwargs)

    with patch("hierwalk.inst_trace.fanin_cone", side_effect=_capture_fanin):
        run_inst_trace(
            InstTraceRequest(
                instance="top.z",
                direction="driver",
                path_kind="comb",
            ),
            rows=rows,
            index=index,
            top="top",
        )
    assert captured
    assert "FOO" not in (captured[0] or {})