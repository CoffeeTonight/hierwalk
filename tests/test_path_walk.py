"""Path-walk mode: on-demand RTL along endpoint paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hierwalk.connect.shared.request import (
    ConnectivityCheck,
    ConnectivityRequest,
    parse_connect_request_json,
)
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import PathWalkState, run_path_walk_connect


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


def _write_expand_array_hierarchy_design(tmp_path: Path) -> Path:
    rtl = tmp_path / "expand_array.v"
    rtl.write_text(
        """
        module leaf_d(output d);
          assign d = 1'b1;
        endmodule

        module chain_c;
          leaf_d u_d();
        endmodule

        module chain_b;
          chain_c c();
        endmodule

        module leaf_i(output i);
          assign i = 1'b0;
        endmodule

        module chain_k(output i);
          leaf_i u_i(.i(i));
        endmodule

        module chain_m(output i);
          chain_k k(.i(i));
        endmodule

        module chain_n(output i);
          chain_m m(.i(i));
        endmodule

        module chain_a;
          chain_b b();
          chain_n n();
          assign n.m.k.u_i.i = b.c.u_d.d;
        endmodule

        module top;
          chain_a a();
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    return fl


def test_path_walk_expand_array_lca_per_expanded_pair(tmp_path: Path):
    """
    a:[top.a.b.c.d, top.a.b.r.t], b:[top.a.n.m.k.i] must conn each expanded pair.

    Before the LCA fix, display strings like ``[top.a.b.c.d, top.a.b.r.t]`` were
    passed to hierarchy walk as a single bogus spec, so the hit ``c.d`` pair never
    got LCA/COI even though endpoint trie walk touched both a paths individually.
    """
    fl_path = _write_expand_array_hierarchy_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    hit_a = "top.a.b.c.d"
    miss_a = "top.a.b.r.t"
    hit_b = "top.a.n.m.k.i"
    request = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "arr",
                    "a": [hit_a, miss_a],
                    "b": [hit_b],
                }
            ]
        }
    )
    lca_calls: list[tuple[str, str]] = []
    orig_lca = PathWalkState.ensure_lca_subtree

    def _track_lca(self, path_a: str, path_b: str) -> None:
        lca_calls.append((path_a, path_b))
        return orig_lca(self, path_a, path_b)

    with patch.object(PathWalkState, "ensure_lca_subtree", _track_lca):
        batch, _index, _state = run_path_walk_connect(request, fl, top="top")

    assert any(hit_a in a or hit_a in b for a, b in lca_calls)
    assert any(miss_a in a or miss_a in b for a, b in lca_calls)
    assert len(lca_calls) >= 2

    result = batch.results[0]
    assert len(result.sub_results) == 2
    by_a = {sr.endpoint_a.spec: sr for sr in result.sub_results}

    hit = by_a[hit_a]
    miss = by_a[miss_a]
    assert not hit.errors
    assert hit.mode != "unknown"
    assert hit.connected is True
    assert hit.hops

    assert miss.connected is False
    assert any("hierarchy" in err.lower() for err in miss.errors)