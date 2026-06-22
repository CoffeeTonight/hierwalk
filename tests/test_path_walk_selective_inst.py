"""Selective instance lookup on path-walk hot path (no full tier1 before match)."""

from __future__ import annotations

import io
import time
from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import create_path_walk_index, run_path_walk_connect
from hierwalk.path_walk_db import RESOLVE_CONFIDENT


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


def test_find_top_in_allinst_v_scans_one_file(tmp_path: Path, monkeypatch):
    """Top in root-listed allinst.v must not tier0-scan the rest of the filelist."""
    n_stub = 120
    allinst = tmp_path / "allinst.v"
    allinst.write_text(
        "module SOC_TOP(input logic clk);\n"
        "  IP_BLK u_ip (.clk(clk));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub_paths = []
    for i in range(n_stub):
        stub = stub_dir / f"stub_{i}.v"
        stub.write_text(f"module STUB_{i} (); endmodule\n", encoding="utf-8")
        stub_paths.append(stub)
    child_fl = tmp_path / "stubs.f"
    child_fl.write_text("\n".join(str(p.resolve()) for p in stub_paths) + "\n", encoding="utf-8")
    root_fl = tmp_path / "design.f"
    root_fl.write_text(
        f"{allinst.resolve()}\n-f {child_fl.resolve()}\n",
        encoding="utf-8",
    )
    fl = parse_filelist(str(root_fl), index_cwd=str(tmp_path))

    tier0_calls: list[str] = []
    from hierwalk.path_walk_db import PathWalkModuleDb

    orig_tier0 = PathWalkModuleDb._tier0_scan_file

    def traced_tier0(self, path):
        tier0_calls.append(str(path))
        return orig_tier0(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "_tier0_scan_file", traced_tier0)

    t0 = time.perf_counter()
    create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True, jobs=1)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert tier0_calls
    assert all(Path(p).name == "allinst.v" for p in tier0_calls)
    assert len(tier0_calls) <= 2  # find + seed may re-enter cached file
    assert elapsed_ms < 5000.0


def test_hierarchy_source_order_is_linear_in_source_count(tmp_path: Path):
    """Regression: listing order must not re-scan all sources per filelist node."""
    n_stub = 400
    allinst = tmp_path / "allinst.v"
    allinst.write_text("module SOC_TOP (); endmodule\n", encoding="utf-8")
    stubs = []
    for i in range(n_stub):
        stub = tmp_path / f"stub_{i}.v"
        stub.write_text(f"module STUB_{i} (); endmodule\n", encoding="utf-8")
        stubs.append(stub)
    child_fl = tmp_path / "stubs.f"
    child_fl.write_text("\n".join(str(p.resolve()) for p in stubs) + "\n", encoding="utf-8")
    root_fl = tmp_path / "design.f"
    root_fl.write_text(
        f"{allinst.resolve()}\n-f {child_fl.resolve()}\n",
        encoding="utf-8",
    )
    fl = parse_filelist(str(root_fl), index_cwd=str(tmp_path))
    sources = [str(Path(p).resolve()) for p in fl.source_files]
    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb

    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        filelist_patterns=[],
        library_files=[],
        library_dirs=[],
        libexts=[],
        file_via_filelist={
            str(Path(k).resolve()): str(Path(v).resolve())
            for k, v in fl.source_via_filelist.items()
        },
        file_filelist_chain={
            str(Path(k).resolve()): v for k, v in fl.source_filelist_chain.items()
        },
        preprocess_include_dirs=[],
        preprocess_defines={},
    )
    mod_db = PathWalkModuleDb(
        sources,
        index,
        file_via_filelist={
            str(Path(k).resolve()): str(Path(v).resolve())
            for k, v in fl.source_via_filelist.items()
        },
        filelist_children={
            str(Path(k).resolve()): [str(Path(c).resolve()) for c in v]
            for k, v in fl.filelist_children.items()
        },
    )
    t0 = time.perf_counter()
    ordered = mod_db._order_sources_by_filelist_hierarchy(sources)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert ordered[0] == str(allinst.resolve())
    assert len(ordered) == len(sources)
    assert elapsed_ms < 500.0


def test_first_child_edge_skips_scoped_tier0_when_parent_seeded(tmp_path: Path, monkeypatch):
    """Seeded top module must not tier0-scan the child filelist pool before selective."""
    n_stub = 80
    top_rtl = tmp_path / "top.v"
    top_rtl.write_text(
        "module SOC_TOP(input logic clk);\n"
        "  IP_BLK u_ip (.clk(clk));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub_paths = []
    for i in range(n_stub):
        stub = stub_dir / f"stub_{i}.v"
        stub.write_text(f"module STUB_{i} (); endmodule\n", encoding="utf-8")
        stub_paths.append(stub)
    child_fl = tmp_path / "stubs.f"
    child_fl.write_text("\n".join(str(p.resolve()) for p in stub_paths) + "\n", encoding="utf-8")
    root_fl = tmp_path / "design.f"
    root_fl.write_text(
        f"{top_rtl.resolve()}\n-f {child_fl.resolve()}\n",
        encoding="utf-8",
    )
    fl = parse_filelist(str(root_fl), index_cwd=str(tmp_path))

    tier0_calls: list[str] = []
    from hierwalk.path_walk_db import PathWalkModuleDb

    orig_tier0 = PathWalkModuleDb._tier0_scan_file

    def traced_tier0(self, path):
        tier0_calls.append(str(path))
        return orig_tier0(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "_tier0_scan_file", traced_tier0)
    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)
    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", lambda self, _p: {})

    index, mod_db = create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True, jobs=1)
    tier0_calls.clear()

    edge = mod_db.resolve_child_edge(
        "SOC_TOP",
        {},
        "u_ip",
        current_file=str(top_rtl.resolve()),
        policy=RESOLVE_CONFIDENT,
    )
    assert edge is not None
    assert edge.child_module == "IP_BLK"
    assert tier0_calls == []


def test_selective_inst_find_uses_raw_not_full_preprocess(tmp_path: Path, monkeypatch):
    fl_path = _write_large_flat_top(tmp_path, n_inst=200)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    preprocess_calls: list[str] = []

    from hierwalk.preprocess import preprocess_file_for_index

    orig = preprocess_file_for_index

    def traced_preprocess(path, *args, **kwargs):
        preprocess_calls.append(str(path))
        return orig(path, *args, **kwargs)

    monkeypatch.setattr(
        "hierwalk.preprocess.preprocess_file_for_index",
        traced_preprocess,
    )
    from hierwalk.path_walk_db import PathWalkModuleDb

    monkeypatch.setattr(PathWalkModuleDb, "_warm_tier1_background", lambda self, _f: None)
    monkeypatch.setattr(PathWalkModuleDb, "tier1_scan_file", lambda self, _p: {})

    index, mod_db = create_path_walk_index(
        fl,
        "SOC_TOP",
        defines={},
        no_cache=True,
        jobs=1,
        diagnostic_inst_trace=True,
    )
    preprocess_calls.clear()
    edge = mod_db.resolve_child_edge(
        "SOC_TOP",
        {},
        "u_ip_50",
        current_file=str((tmp_path / "top.v").resolve()),
        policy=RESOLVE_CONFIDENT,
    )
    assert edge is not None
    assert preprocess_calls == []


def test_inst_find_emits_preprocess_and_scan_trace(tmp_path: Path):
    fl_path = _write_large_flat_top(tmp_path, n_inst=50)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    target = "SOC_TOP.u_ip_10"
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(target, target),),
        top="SOC_TOP",
    )
    run_path_walk_connect(
        req,
        fl,
        top="SOC_TOP",
        no_cache=True,
        jobs=1,
        trace_stream=buf,
        diagnostic_inst_trace=True,
    )
    text = buf.getvalue()
    assert "pw-db inst-resolve enter SOC_TOP.u_ip_10" in text
    assert "pw-db inst-find enter SOC_TOP.u_ip_10" in text
    assert "pw-db inst-find done SOC_TOP.u_ip_10" in text
    assert "pw-db preprocess" in text
    assert "source=raw" in text


def test_provisional_map_reports_activation_after_audit(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module TOP;
        `ifdef ENABLED
          CHILD u_gated ();
        `endif
          CHILD u_open ();
        endmodule
        module CHILD (); endmodule
        """,
        encoding="utf-8",
    )
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))

    from hierwalk.path_walk_db import PathWalkModuleDb

    index, mod_db = create_path_walk_index(
        fl,
        "TOP",
        defines={},
        no_cache=True,
        jobs=1,
        diagnostic_inst_trace=True,
    )
    mod_db.resolve_child_edge(
        "TOP",
        {},
        "u_gated",
        current_file=str(rtl.resolve()),
        policy=RESOLVE_CONFIDENT,
    )
    mod_db.resolve_child_edge(
        "TOP",
        {},
        "u_open",
        current_file=str(rtl.resolve()),
        policy=RESOLVE_CONFIDENT,
    )
    mod_db.drain_activation_audit(wait=True)
    lines = mod_db.format_activation_audit_lines()
    joined = "\n".join(lines)
    assert "u_gated" in joined
    assert "u_open" in joined
    gated = mod_db._mapped_inst_records[
        mod_db._mapped_inst_key("TOP", "u_gated", {})
    ]
    open_inst = mod_db._mapped_inst_records[
        mod_db._mapped_inst_key("TOP", "u_open", {})
    ]
    assert gated.activation == "inactive"
    assert open_inst.activation == "active"


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
    mod_db.drain_activation_audit(wait=True)
    find_calls.clear()
    state.ensure_path("SOC_TOP.u_ip_200")
    assert "u_ip_200" in find_calls
    assert "u_ip_100" not in find_calls