"""Wire/signal endpoints (not only module ports)."""

from __future__ import annotations

import time
from pathlib import Path

from hierwalk.connectivity import check_connectivity
from hierwalk.connect_endpoints import (
    is_module_local_signal_name,
    net_exists_in_module_fast,
    parse_connect_endpoint,
    resolve_endpoint,
)
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist


def _index_and_rows(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows


def test_parse_connect_endpoint_accepts_assign_only_implicit_wire(tmp_path: Path):
    v = """
    module top(input logic clk);
      assign bridge = clk;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    lookup = {r.full_path: r for r in rows}
    hier, tail = parse_connect_endpoint("top.bridge", lookup, index=index, top="top")
    assert hier == "top"
    assert tail == "bridge"


def test_parse_connect_endpoint_accepts_internal_wire(tmp_path: Path):
    v = """
    module top(input logic clk);
      wire bridge;
      assign bridge = clk;
      child u0 (.clk(bridge));
    endmodule
    module child(input logic clk); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    lookup = {r.full_path: r for r in rows}
    hier, tail = parse_connect_endpoint("top.bridge", lookup, index=index, top="top")
    assert hier == "top"
    assert tail == "bridge"


def test_resolve_wire_endpoint(tmp_path: Path):
    v = """
    module top(input logic clk);
      wire bridge;
      assign bridge = clk;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    ep, errors = resolve_endpoint("top.bridge", rows, index, top="top")
    assert not errors
    assert ep.port_found
    assert ep.inst_path == "top"
    assert ep.port_name == "bridge"


def test_internal_wire_connected_only_via_instance_port(tmp_path: Path):
    """``wire c`` with no assign — only ``.p(c)`` — is a valid signal endpoint."""
    v = """
    module top(input logic clk);
      wire c;
      child u0 (.out(c));
    endmodule
    module child(output logic out);
      assign out = 1'b0;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    ep, errors = resolve_endpoint("top.c", rows, index, top="top")
    assert not errors
    assert ep.port_found
    r = check_connectivity(
        "top.u0.out",
        "top.c",
        rows=rows,
        index=index,
        top="top",
    )
    assert r.connected


def test_connectivity_port_to_internal_wire(tmp_path: Path):
    v = """
    module top(input logic clk);
      wire bridge;
      assign bridge = clk;
      child u0 (.clk(bridge));
    endmodule
    module child(input logic clk); endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    r = check_connectivity("top.clk", "top.bridge", rows=rows, index=index, top="top")
    assert r.connected


def test_signal_tail_dotted_miss_large_module_fast(tmp_path: Path):
    """Instance-like dotted tail must not run full assign/port-map collection on miss."""
    n = 50_000
    body = "module BIG(input logic clk);\n"
    body += "".join(f"  assign w{i} = clk;\n" for i in range(n))
    body += "  child u_core (.clk(clk));\nendmodule\n"
    body += "module child(input logic clk); LEAF u_leaf (); endmodule\nmodule LEAF; endmodule\n"
    rtl = tmp_path / "big.v"
    rtl.write_text(body, encoding="utf-8")
    index = DesignIndex.build({str(rtl): body})
    _, rows = elaborate(index, "BIG")
    row = {r.full_path: r for r in rows}["BIG"]
    from hierwalk.path_walk import PathWalkState
    from hierwalk.path_walk_db import PathWalkModuleDb

    state = PathWalkState(
        index=index,
        top="BIG",
        mod_db=PathWalkModuleDb([str(rtl)], index),
    )
    t0 = time.perf_counter()
    hit = state._resolve_signal_tail("BIG", "u_core.u_leaf", target_path="BIG.u_core.u_leaf")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert hit is False
    assert elapsed_ms < 500.0


def test_dotted_tail_is_not_module_local_signal_name():
    assert not is_module_local_signal_name("c.d")
    assert not is_module_local_signal_name("u_core.u_leaf")
    assert is_module_local_signal_name("c")
    assert is_module_local_signal_name("bus[0]")


def test_net_exists_port_map_only_wire(tmp_path: Path):
    v = """
    module top(input logic clk);
      child u0 (.out(only_in_port));
    endmodule
    module child(output logic out); assign out = clk; endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    row = {r.full_path: r for r in rows}["top"]
    assert net_exists_in_module_fast(index, row, "only_in_port", top="top")


def test_signal_tail_wire_probe_large_module_under_one_second(tmp_path: Path):
    """4300+ line module: wire-tail regex probe must not run full connect/index walks."""
    lines = 4302
    body = "module BIG(input logic clk);\n"
    body += "".join(f"  wire w{i};\n" for i in range(lines - 10))
    body += "  assign w_last = clk;\nendmodule\n"
    rtl = tmp_path / "big.v"
    rtl.write_text(body, encoding="utf-8")
    index = DesignIndex.build({str(rtl): body})
    _, rows = elaborate(index, "BIG")
    row = {r.full_path: r for r in rows}["BIG"]
    from hierwalk.path_walk import PathWalkState
    from hierwalk.path_walk_db import PathWalkModuleDb

    state = PathWalkState(
        index=index,
        top="BIG",
        mod_db=PathWalkModuleDb([str(rtl)], index),
    )
    t0 = time.perf_counter()
    kind, check_ms = state._classify_signal_tail("BIG", "w_last", row)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert kind == "wire"
    assert check_ms < 500.0
    assert elapsed_ms < 500.0


def test_net_exists_in_module_fast_assign_only_implicit(tmp_path: Path):
    """Implicit net from ``assign`` only (no ``wire``) is a signal endpoint."""
    v = """
    module top(input logic clk);
      assign bridge = clk;
    endmodule
    """
    index, rows = _index_and_rows(v, tmp_path)
    row = {r.full_path: r for r in rows}["top"]
    assert net_exists_in_module_fast(index, row, "bridge", top="top")


def test_path_walk_assign_only_implicit_wire(tmp_path: Path):
    """``assign bridge = clk`` without ``wire bridge`` must not log instance miss."""
    v = """
    module top(input logic clk);
      assign bridge = clk;
      child u0 (.clk(bridge));
    endmodule
    module child(input logic clk); endmodule
    """
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    import io

    buf = io.StringIO()
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.bridge"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        trace_stream=buf,
    )
    assert batch.results[0].connected is True
    text = buf.getvalue()
    assert "miss inst=bridge" not in text


def test_path_walk_miss_hints_module_type_not_inst_name(tmp_path: Path):
    (tmp_path / "top.v").write_text(
        """
        module SOC_TOP;
          CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "cpu.v").write_text("module CPUSYSTEM_TOP; endmodule\n", encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(str((tmp_path / n).resolve()) for n in ("top.v", "cpu.v")) + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    import io

    buf = io.StringIO()
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("SOC_TOP.CPUSYSTEM_TOP", "SOC_TOP.CPUSYSTEM_TOP"),),
        top="SOC_TOP",
    )
    run_path_walk_connect(req, flr, top="SOC_TOP", no_cache=True, trace_stream=buf)
    text = buf.getvalue()
    assert "module type" in text
    assert "u_cpusystem_top" in text