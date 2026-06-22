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


def test_preprocessed_text_disk_cache(tmp_path: Path, monkeypatch):
    fl_path = _write_large_flat_top(tmp_path, n_inst=200)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    rtl = str((tmp_path / "top.v").resolve())
    cache_dir = tmp_path / ".db_cache"
    preprocess_calls: list[str] = []

    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb, path_walk_db_cache_key
    from hierwalk.preprocess import preprocess_file_for_index

    orig = preprocess_file_for_index

    def traced_preprocess(path, *args, **kwargs):
        preprocess_calls.append(str(path))
        return orig(path, *args, **kwargs)

    monkeypatch.setattr(
        "hierwalk.preprocess.preprocess_file_for_index",
        traced_preprocess,
    )

    sources = [rtl]
    cache_key = path_walk_db_cache_key(sources, defines={})
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        filelist_patterns=[],
        library_files=[],
        library_dirs=[],
        libexts=[],
        file_via_filelist={},
        file_filelist_chain={},
        preprocess_include_dirs=[],
        preprocess_defines={},
    )
    mod_db = PathWalkModuleDb(
        sources,
        index,
        cache_dir=cache_dir,
        cache_key=cache_key,
        no_cache=False,
    )
    mod_db._preprocessed_text_for_file(rtl)
    assert len(preprocess_calls) == 1

    preprocess_calls.clear()
    mod_db2 = PathWalkModuleDb(
        sources,
        index,
        cache_dir=cache_dir,
        cache_key=cache_key,
        no_cache=False,
    )
    mod_db2._preprocessed_text_for_file(rtl)
    assert preprocess_calls == []


def test_inst_leaf_index_avoids_repeat_selective_scan(tmp_path: Path, monkeypatch):
    fl_path = _write_large_flat_top(tmp_path, n_inst=6000)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))

    from hierwalk import inst_scan

    orig_find = inst_scan.find_hierarchy_instance
    find_calls: list[str] = []

    def traced_find(body, inst_leaf, *, param_map=None):
        find_calls.append(inst_leaf)
        return orig_find(body, inst_leaf, param_map=param_map)

    monkeypatch.setattr(inst_scan, "find_hierarchy_instance", traced_find)
    monkeypatch.setattr(
        "hierwalk.path_walk_db.find_hierarchy_instance",
        traced_find,
    )

    from hierwalk.path_walk_db import PathWalkModuleDb

    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)
    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", lambda self, _p: {})

    index, mod_db = create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True, jobs=1)
    from hierwalk.path_walk import PathWalkState

    state = PathWalkState(index=index, top="SOC_TOP", mod_db=mod_db)
    state.ensure_root()
    state.ensure_path("SOC_TOP.u_ip_100")
    find_calls.clear()
    state.ensure_path("SOC_TOP.u_ip_200")
    assert "u_ip_200" in find_calls
    assert "u_ip_100" not in find_calls