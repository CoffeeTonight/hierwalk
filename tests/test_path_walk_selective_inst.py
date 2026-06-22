"""Selective instance lookup on path-walk hot path (no full tier1 before match)."""

from __future__ import annotations

import time
from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import create_path_walk_index, run_path_walk_connect


def _write_large_flat_top(tmp_path: Path, n_inst: int) -> Path:
    body = "module SOC_TOP(input logic clk);\n"
    body += "".join(f"  IP_BLK_{i % 100} u_ip_{i} (.clk(clk));\n" for i in range(n_inst))
    body += "endmodule\n"
    body += "\n".join(
        f"module IP_BLK_{m}(input logic clk); endmodule" for m in range(100)
    )
    body += "\n"
    rtl = tmp_path / "top.v"
    rtl.write_text(body, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    return fl


def test_create_path_walk_index_seeds_top_without_tier1(tmp_path: Path, monkeypatch):
    fl_path = _write_large_flat_top(tmp_path, n_inst=8000)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    calls: list[str] = []

    from hierwalk.path_walk_db import PathWalkModuleDb

    orig = PathWalkModuleDb.tier1_scan_file

    def traced_tier1(self, path):
        calls.append(str(path))
        return orig(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", traced_tier1)

    create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True, jobs=1)
    assert calls == []


def test_selective_child_edge_finds_one_inst_in_large_top(tmp_path: Path, monkeypatch):
    fl_path = _write_large_flat_top(tmp_path, n_inst=12000)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    tier1_calls: list[str] = []

    from hierwalk.path_walk_db import PathWalkModuleDb

    orig = PathWalkModuleDb.tier1_scan_file

    def traced_tier1(self, path):
        tier1_calls.append(str(path))
        return orig(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", traced_tier1)
    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)

    target = "SOC_TOP.u_ip_9999"
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(target, target),),
        top="SOC_TOP",
    )
    t0 = time.perf_counter()
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="SOC_TOP",
        no_cache=True,
        jobs=1,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert batch.results[0].connected is True
    assert target in state.rows_by_path
    assert tier1_calls == []
    assert elapsed_ms < 15_000.0