"""Path-walk trie dedup and branch-point helpers."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import (
    _path_trie_branch_points,
    _path_trie_from_specs,
    _sorted_unique_specs,
    _walk_endpoint_specs,
    PathWalkState,
    create_path_walk_index,
    run_path_walk_connect,
)
from hierwalk.top_find import resolve_top_modules


def test_sorted_unique_specs_dedups_and_orders():
    specs = [
        "top.b.a",
        "top.a",
        "top.b.a",
        "top.a.z",
    ]
    assert _sorted_unique_specs(specs) == ["top.a", "top.a.z", "top.b.a"]


def test_path_trie_branch_points():
    root = _path_trie_from_specs(
        [
            "top.u_soc.u_cpusystem.b",
            "top.u_soc.u_ifdef.c",
            "top.u_soc.u_mid.x",
        ],
        top="top",
    )
    branches = _path_trie_branch_points(root)
    assert branches == ["top.u_soc"]


def test_walk_fewer_target_calls_with_duplicate_specs(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
    module top(input wire_a, input wire_b, input wire_c);
      wire [2:0] bus_out;
      assign bus_out[0] = wire_a;
      assign bus_out[1] = wire_b;
      assign bus_out[2] = wire_c;
    endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "f.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    fl_res = parse_filelist(str(fl), index_cwd=str(tmp_path))
    checks = tuple(
        ConnectivityCheck("top.bus_out[0]", "top.wire_a", f"n{i}")
        for i in range(20)
    )
    request = ConnectivityRequest(checks=checks, top="top")
    batch, _index, state = run_path_walk_connect(request, fl_res, top="top")
    assert len(batch.results) == 20
    assert all(r.connected for r in batch.results)
    assert state.stats.endpoint_specs_raw == 40
    assert state.stats.endpoint_specs_unique == 2
    assert state.stats.walk_target_calls == 1
    assert state.stats.walk_target_skipped == 1
    assert state.stats.walk_target_skipped >= 0


def test_parallel_walk_matches_sequential(tmp_path: Path):
    rtl = tmp_path / "branch.v"
    rtl.write_text(
        """
    module leaf(input wire_a, input wire_b);
    endmodule

    module mid(input wire_a, input wire_b);
      leaf u_a(.wire_a(wire_a), .wire_b(wire_b));
      leaf u_b(.wire_a(wire_a), .wire_b(wire_b));
    endmodule

    module top(input wire_a, input wire_b);
      mid u_left(.wire_a(wire_a), .wire_b(wire_b));
      mid u_right(.wire_a(wire_a), .wire_b(wire_b));
    endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "f.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    fl_res = parse_filelist(str(fl), index_cwd=str(tmp_path))
    checks = (
        ConnectivityCheck("top.u_left.u_a.wire_a", "top.wire_a", "left_a"),
        ConnectivityCheck("top.u_left.u_b.wire_b", "top.wire_b", "left_b"),
        ConnectivityCheck("top.u_right.u_a.wire_a", "top.wire_a", "right_a"),
        ConnectivityCheck("top.u_right.u_b.wire_b", "top.wire_b", "right_b"),
    )
    request = ConnectivityRequest(checks=checks, top="top")

    _batch_seq, _index_seq, state_seq = run_path_walk_connect(
        request,
        fl_res,
        top="top",
        jobs=1,
        no_cache=True,
    )
    _batch_par, _index_par, state_par = run_path_walk_connect(
        request,
        fl_res,
        top="top",
        jobs=4,
        no_cache=True,
    )

    assert set(state_seq.rows_by_path) == set(state_par.rows_by_path)
    assert state_par.stats.walk_parallel_workers == 4
    assert state_par.stats.walk_parallel_branches >= 2
    assert state_par.stats.walk_parallel_branches >= 1
    assert state_seq.stats.walk_target_calls == state_par.stats.walk_target_calls


def test_parallel_walk_on_trie_branch_points(tmp_path: Path):
    rtl = tmp_path / "soc.v"
    rtl.write_text(
        """
    module leaf(input wire_x);
    endmodule
    module cpusystem(input wire_x);
      leaf u_leaf(.wire_x(wire_x));
    endmodule
    module ifdef_mod(input wire_x);
      leaf u_leaf(.wire_x(wire_x));
    endmodule
    module mid(input wire_x);
      cpusystem u_cpusystem(.wire_x(wire_x));
      ifdef_mod u_ifdef(.wire_x(wire_x));
    endmodule
    module top(input wire_x);
      mid u_soc(.wire_x(wire_x));
    endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "f.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    fl_res = parse_filelist(str(fl), index_cwd=str(tmp_path))
    specs = [
        "top.u_soc.u_cpusystem.u_leaf.wire_x",
        "top.u_soc.u_ifdef.u_leaf.wire_x",
    ]
    root = _path_trie_from_specs(specs, top="top")
    assert _path_trie_branch_points(root) == ["top.u_soc"]

    index, mod_db = create_path_walk_index(
        fl_res,
        "top",
        defines=fl_res.defines,
        no_cache=True,
        jobs=4,
    )
    top_name = resolve_top_modules(index, top="top", filelist_tops=fl_res.top_modules)[0]
    state = PathWalkState(index=index, top=top_name, mod_db=mod_db)
    state.ensure_root()
    _walk_endpoint_specs(state, specs, jobs=4)
    assert state.stats.walk_parallel_workers == 2
    assert state.stats.walk_parallel_branches == 1
    assert state.stats.walk_parallel_branches >= 1
    assert "top.u_soc.u_cpusystem.u_leaf" in state.rows_by_path
    assert "top.u_soc.u_ifdef.u_leaf" in state.rows_by_path


def test_parallel_walk_logs_branch_workers(tmp_path: Path):
    rtl = tmp_path / "soc.v"
    rtl.write_text(
        """
    module leaf(input wire_x);
    endmodule
    module cpusystem(input wire_x);
      leaf u_leaf(.wire_x(wire_x));
    endmodule
    module ifdef_mod(input wire_x);
      leaf u_leaf(.wire_x(wire_x));
    endmodule
    module mid(input wire_x);
      cpusystem u_cpusystem(.wire_x(wire_x));
      ifdef_mod u_ifdef(.wire_x(wire_x));
    endmodule
    module top(input wire_x);
      mid u_soc(.wire_x(wire_x));
    endmodule
    """,
        encoding="utf-8",
    )
    fl = tmp_path / "f.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    fl_res = parse_filelist(str(fl), index_cwd=str(tmp_path))
    specs = [
        "top.u_soc.u_cpusystem.u_leaf",
        "top.u_soc.u_ifdef.u_leaf",
    ]
    trace = io.StringIO()
    progress: list[str] = []

    def on_progress(msg: str) -> None:
        progress.append(msg)

    index, mod_db = create_path_walk_index(
        fl_res,
        "top",
        defines=fl_res.defines,
        no_cache=True,
        jobs=4,
        on_progress=on_progress,
    )
    top_name = resolve_top_modules(index, top="top", filelist_tops=fl_res.top_modules)[0]
    state = PathWalkState(
        index=index,
        top=top_name,
        mod_db=mod_db,
        trace_stream=trace,
        on_progress=on_progress,
    )
    state.ensure_root()
    _walk_endpoint_specs(state, specs, jobs=4)

    log = trace.getvalue()
    assert "parallel walk enabled requested_jobs=4 workers=2" in log
    assert "parallel fork at=top.u_soc requested_jobs=4 workers=2" in log
    assert "branches=u_cpusystem,u_ifdef" in log
    assert "parallel worker j=1/2 branch=u_cpusystem from=top.u_soc" in log
    assert "parallel worker j=2/2 branch=u_ifdef from=top.u_soc" in log