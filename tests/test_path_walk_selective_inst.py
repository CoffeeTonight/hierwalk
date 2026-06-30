"""Selective instance lookup on path-walk hot path (no full tier1 before match)."""

from __future__ import annotations

import time
from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import (
    build_path_walk_state,
    create_path_walk_index,
    run_path_walk_connect,
)


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


def test_deep_hierarchy_through_assign_heavy_parent(tmp_path: Path, monkeypatch):
    """``top.a.b.c`` must resolve ``b`` in a 50k-assign parent without tier1."""
    n = 50_000
    body = "module TOP(input logic clk);\n  MOD_A a (.clk(clk));\nendmodule\n"
    body += "module MOD_A(input logic clk);\n"
    body += "".join(f"  assign w{i} = clk;\n" for i in range(n))
    body += "  MOD_B b (.clk(clk));\nendmodule\n"
    body += "module MOD_B(input logic clk);\n  MOD_C c (.clk(clk));\nendmodule\n"
    body += "module MOD_C(input logic clk);\n  wire deep;\n  assign deep = clk;\nendmodule\n"
    rtl = tmp_path / "chain.v"
    rtl.write_text(body, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))

    tier1_calls: list[str] = []
    from hierwalk.path_walk_db import PathWalkModuleDb

    orig = PathWalkModuleDb.tier1_scan_file

    def traced_tier1(self, path):
        tier1_calls.append(str(path))
        return orig(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", traced_tier1)
    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)

    req = ConnectivityRequest(
        checks=(ConnectivityCheck("TOP.a.b.c.deep", "TOP.clk", check_id="chain"),),
        top="TOP",
    )
    index, mod_db = create_path_walk_index(flr, "TOP", defines={}, no_cache=True, jobs=1)
    t0 = time.perf_counter()
    state = build_path_walk_state(index, "TOP", req, mod_db, jobs=1)
    walk_ms = (time.perf_counter() - t0) * 1000.0

    assert "TOP.a.b.c" in state.rows_by_path
    assert tier1_calls == []
    assert walk_ms < 30_000.0


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


def test_empty_param_ctx_skips_path_refine_on_signal_tail(tmp_path: Path, monkeypatch):
    """Folded but empty param_ctx must not trigger full-path refine on large parents."""
    n = 50_000
    body = "module TOP(input logic clk);\n  MOD_A a (.clk(clk));\nendmodule\n"
    body += "module MOD_A(input logic clk);\n"
    body += "".join(f"  assign w{i} = clk;\n" for i in range(n))
    body += "  MOD_B b (.clk(clk));\nendmodule\n"
    body += "module MOD_B(input logic clk);\n  MOD_C c (.clk(clk));\nendmodule\n"
    body += "module MOD_C(input logic clk);\n  wire deep;\n  assign deep = clk;\nendmodule\n"
    rtl = tmp_path / "chain.v"
    rtl.write_text(body, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))

    refine_calls: list[str] = []

    import hierwalk.path_refine as pr

    orig = pr.refine_param_ctx_for_path

    def traced_refine(index, top, full_path):
        refine_calls.append(full_path)
        return orig(index, top, full_path)

    monkeypatch.setattr(pr, "refine_param_ctx_for_path", traced_refine)
    monkeypatch.setattr(
        "hierwalk.connect_endpoints.refine_param_ctx_for_path",
        traced_refine,
    )

    from hierwalk.path_walk_db import PathWalkModuleDb

    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)

    req = ConnectivityRequest(
        checks=(ConnectivityCheck("TOP.a.b.c.deep", "TOP.clk", check_id="chain"),),
        top="TOP",
    )
    index, mod_db = create_path_walk_index(flr, "TOP", defines={}, no_cache=True, jobs=1)
    t0 = time.perf_counter()
    state = build_path_walk_state(index, "TOP", req, mod_db, jobs=1)
    walk_ms = (time.perf_counter() - t0) * 1000.0

    assert refine_calls == [], "path-walk must not refine param ctx on signal-tail probes"
    assert walk_ms < 30_000.0

    refine_calls.clear()
    batch, _index, _state = run_path_walk_connect(
        req,
        flr,
        top="TOP",
        no_cache=True,
        jobs=1,
    )

    assert batch.results[0].connected is True


def test_find_instance_by_child_module_returns_first_matching_edge():
    from hierwalk.inst_scan import find_instance_by_child_module

    body = """
    module parent;
      IP_BLK u_ip_0 ();
      OTHER u_other ();
      IP_BLK u_ip_1 ();
    endmodule
    """
    edge = find_instance_by_child_module(body, "IP_BLK")
    assert edge is not None
    assert edge.inst_name == "u_ip_0"
    assert edge.child_module == "IP_BLK"


def test_ensure_path_skips_signal_tail_on_intermediate_inst_hops(
    tmp_path: Path,
    monkeypatch,
):
    """Mid-hop instance names must not run signal-tail before child-edge resolution."""
    n = 50_000
    body = "module TOP(input logic clk);\n  MOD_A a (.clk(clk));\nendmodule\n"
    body += "module MOD_A(input logic clk);\n"
    body += "".join(f"  assign w{i} = clk;\n" for i in range(n))
    body += "  MOD_B b (.clk(clk));\nendmodule\n"
    body += "module MOD_B(input logic clk);\n  MOD_C c (.clk(clk));\nendmodule\n"
    body += "module MOD_C(input logic clk);\nendmodule\n"
    rtl = tmp_path / "chain.v"
    rtl.write_text(body, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))

    classify_calls: list[tuple[str, str]] = []

    from hierwalk.path_walk import PathWalkState

    orig = PathWalkState._classify_signal_tail

    def traced_classify(self, parent_path, signal_name, row):
        classify_calls.append((parent_path, signal_name))
        return orig(self, parent_path, signal_name, row)

    monkeypatch.setattr(PathWalkState, "_classify_signal_tail", traced_classify)

    from hierwalk.path_walk_db import PathWalkModuleDb

    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)

    index, mod_db = create_path_walk_index(flr, "TOP", defines={}, no_cache=True, jobs=1)
    state = PathWalkState(index=index, top="TOP", mod_db=mod_db)
    state.ensure_root()
    t0 = time.perf_counter()
    assert state.ensure_path("TOP.a.b.c")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert classify_calls == []
    assert elapsed_ms < 30_000.0
    assert "TOP.a.b.c" in state.rows_by_path