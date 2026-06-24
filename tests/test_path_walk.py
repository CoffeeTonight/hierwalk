"""Path-walk mode: on-demand RTL along endpoint paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def _write_bus_design(tmp_path: Path) -> Path:
    rtl = tmp_path / "bus_top.v"
    rtl.write_text(
        """
        module leaf(input in, output out);
          assign out = in;
        endmodule

        module bus_mux(
          input wire_a,
          input wire_b,
          input wire_c,
          output [2:0] bus_out
        );
          assign bus_out[0] = wire_a;
          assign bus_out[1] = wire_b;
          assign bus_out[2] = wire_c;
        endmodule

        module top(
          input wire_a,
          input wire_b,
          input wire_c,
          output [2:0] y
        );
          bus_mux u_mux (
            .wire_a(wire_a),
            .wire_b(wire_b),
            .wire_c(wire_c),
            .bus_out(y)
          );
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    return fl


def test_path_walk_array_bit_drivers(tmp_path: Path):
    fl_path = _write_bus_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    checks = tuple(
        ConnectivityCheck(
            f"top.u_mux.bus_out[{i}]",
            src,
            f"bit{i}",
        )
        for i, src in enumerate(["top.wire_a", "top.wire_b", "top.wire_c"])
    )
    request = ConnectivityRequest(checks=checks, top="top")
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top="top",
    )
    assert len(index.modules) <= 4
    assert len(state.rows_by_path) >= 2
    assert batch.modules_cached >= 1
    by_id = {r.check_id: r for r in batch.results}
    assert by_id["bit0"].connected is True
    assert by_id["bit1"].connected is True
    assert by_id["bit2"].connected is True


def test_path_walk_reuses_mod_cache_across_many_checks(tmp_path: Path):
    fl_path = _write_bus_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    checks = tuple(
        ConnectivityCheck(
            "top.u_mux.bus_out[0]",
            f"top.wire_a",
            f"n{i}",
        )
        for i in range(20)
    )
    request = ConnectivityRequest(checks=checks, top="top")
    batch, _index, _state = run_path_walk_connect(request, fl, top="top")
    assert len(batch.results) == 20
    assert batch.modules_cached >= 1
    assert all(r.connected for r in batch.results)