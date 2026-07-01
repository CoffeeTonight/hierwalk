"""Regression tests for connect-pipeline rows, bind memo, and tier1 defines cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


_PIPELINE_RTL = """
module top(input logic a0, input logic a1, output logic z0, output logic z1);
  leaf u0 (.in(a0), .out(z0));
  leaf u1 (.in(a1), .out(z1));
endmodule
module leaf(input logic in, output logic out);
  assign out = in;
endmodule
"""


def test_connect_pipeline_resolves_deep_endpoints(tmp_path: Path, monkeypatch):
    """Pipeline mode must use per-check hierarchy rows, not shell root-only rows."""
    monkeypatch.setenv("HIERWALK_CONNECT_JOBS", "4")
    rtl = tmp_path / "d.v"
    rtl.write_text(_PIPELINE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    fl_result = parse_filelist(str(fl))
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.u0.in", "top.z0", check_id="c0"),
            ConnectivityCheck("top.u1.in", "top.z1", check_id="c1"),
        ),
        top="top",
    )
    batch, _index, state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        connect_jobs=4,
        no_cache=True,
        connect_phase="text",
    )
    assert len(batch.results) == 2
    for result in batch.results:
        assert not result.errors, result.errors
        assert result.connected, (
            f"{result.endpoint_a.spec} -> {result.endpoint_b.spec}: {result.note}"
        )
    assert len(state.rows_by_path) >= 3


def test_bind_memo_hit_skips_bind_rescan(tmp_path: Path):
    from hierwalk.connect_endpoints import (
        _clear_module_index_key_memo,
        _resolve_module_index_key,
    )
    from hierwalk.connect_scan import collect_bind_records_for_module
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top(input a, output z);
          wire n;
          assign z = n;
          assign n = a;
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _clear_module_index_key_memo()
    _resolve_module_index_key(
        index, "top", {}, None, ff_barrier=False, over_approximate_if=True
    )
    with patch(
        "hierwalk.connect_endpoints.collect_bind_records_for_module",
        wraps=collect_bind_records_for_module,
    ) as spy:
        _resolve_module_index_key(
            index, "top", {}, None, ff_barrier=False, over_approximate_if=True
        )
    assert spy.call_count == 0


def test_tier1_defines_cached_until_index_changes(tmp_path: Path):
    from hierwalk.connectivity import _effective_defines
    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={})
    db = PathWalkModuleDb([path], index, defines={})

    with patch(
        "hierwalk.connectivity._effective_defines",
        wraps=_effective_defines,
    ) as spy:
        db._tier1_defines()
        db._tier1_defines()
        db._tier1_defines()
    assert spy.call_count == 0

    db._invalidate_tier1_defines_cache()
    with patch(
        "hierwalk.connectivity._effective_defines",
        wraps=_effective_defines,
    ) as spy2:
        db._tier1_defines()
    assert spy2.call_count == 1


def test_tier1_defines_survives_module_growth_without_rescan(tmp_path: Path):
    from hierwalk.connectivity import _effective_defines
    from hierwalk.index import DesignIndex, ModuleRecord
    from hierwalk.path_walk_db import PathWalkModuleDb

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={})
    db = PathWalkModuleDb([path], index, defines={})
    db._tier1_defines()

    index.modules["leaf"] = ModuleRecord(
        module_name="leaf",
        file_path=path,
        body="module leaf(); endmodule",
        raw_params={},
        instances=[],
    )
    index._rebuild_file_modules()

    with patch(
        "hierwalk.connectivity._effective_defines",
        wraps=_effective_defines,
    ) as spy:
        db._tier1_defines()
        db._tier1_defines()
    assert spy.call_count == 0


def test_design_bind_index_scans_once_per_design(tmp_path: Path):
    from hierwalk.connect_scan import (
        clear_bind_records_memo,
        collect_bind_records_for_module,
    )
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
        module top(input a, output z);
          wire n;
          assign z = n;
          assign n = a;
        endmodule
        module leaf(input in, output out);
          assign out = in;
        endmodule
        bind top bind_leaf u (.in(a), .out(z));
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    clear_bind_records_memo()
    with patch(
        "hierwalk.connect_scan._scan_design_bind_index",
        wraps=__import__(
            "hierwalk.connect_scan", fromlist=["_scan_design_bind_index"]
        )._scan_design_bind_index,
    ) as spy:
        collect_bind_records_for_module(index, "top")
        collect_bind_records_for_module(index, "leaf")
        collect_bind_records_for_module(index, "top")
    assert spy.call_count == 1


def test_connectivity_defines_cache_ignores_module_growth(tmp_path: Path):
    from hierwalk.connectivity import ConnectivitySession
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex, ModuleRecord

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    path = str(rtl.resolve())
    index = DesignIndex.build({path: rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        sources=[path],
        resolve_param_dims=False,
    )
    session.effective_defines()

    index.modules["extra"] = ModuleRecord(
        module_name="extra",
        file_path=path,
        body="module extra(); endmodule",
        raw_params={},
        instances=[],
    )
    index._rebuild_file_modules()

    with patch(
        "hierwalk.connect_scan.collect_design_defines",
    ) as spy:
        session.effective_defines()
    assert spy.call_count == 0


def test_bind_scan_skipped_when_design_has_no_bind(tmp_path: Path):
    from hierwalk.connect_scan import (
        _scan_design_bind_index,
        clear_bind_records_memo,
        collect_bind_records_for_module,
    )
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top(input a, output z); assign z = a; endmodule\n",
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    clear_bind_records_memo()
    with patch(
        "hierwalk.connect_scan._scan_design_bind_index",
        wraps=_scan_design_bind_index,
    ) as spy:
        assert collect_bind_records_for_module(index, "top") == []
    assert spy.call_count == 0


def test_tier1_reuses_preprocessed_text_cache(tmp_path: Path):
    from unittest.mock import patch

    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb
    from hierwalk.preprocess import preprocess_file_for_index

    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top(input a, output z);
          assign z = a;
        endmodule
        """,
        encoding="utf-8",
    )
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={})
    db = PathWalkModuleDb([path], index, defines={}, no_cache=True)
    with patch(
        "hierwalk.preprocess.preprocess_file_for_index",
        wraps=preprocess_file_for_index,
    ) as spy:
        db.tier1_scan_file(path)
        db._tier0_preprocessed_text(path)
    assert spy.call_count == 1


def test_port_param_ctx_skips_refine_for_text_conn(tmp_path: Path):
    from dataclasses import replace

    from hierwalk.connect_endpoints import _port_param_ctx
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    empty_row = replace(rows[0], param_ctx={}, param_ctx_folded=False)
    with patch(
        "hierwalk.connect_endpoints.refine_param_ctx_for_path",
    ) as spy:
        _port_param_ctx(index, empty_row, "top", resolve_param_dims=False)
    assert spy.call_count == 0


def test_text_conn_lite_skips_comb_always_and_ff_metadata(tmp_path: Path):
    from unittest.mock import patch

    from hierwalk.connect_scan import (
        _parse_comb_always_stmt,
        build_module_connect_index,
        scan_ff_adjacency,
    )

    body = """
    module m(input logic a, output logic z);
      logic q, d;
      always_ff @(posedge clk) q <= d;
      always_comb begin
        if (a) z = d; else z = 1'b0;
      end
      assign d = a;
    endmodule
    """
    with patch(
        "hierwalk.connect_scan._parse_comb_always_stmt",
        wraps=_parse_comb_always_stmt,
    ) as comb_spy:
        with patch(
            "hierwalk.connect_scan.scan_ff_adjacency",
            wraps=scan_ff_adjacency,
        ) as ff_spy:
            build_module_connect_index(
                body,
                resolve_param_dims=False,
                ff_barrier=True,
            )
    assert comb_spy.call_count == 0
    assert ff_spy.call_args.kwargs.get("ff_net_lines") is None
    assert ff_spy.call_args.kwargs.get("ff_d_roots") is None


def test_tier0_worker_reuses_preprocessed_sidecar(tmp_path: Path):
    from unittest.mock import patch

    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import (
        PathWalkModuleDb,
        _tier0_worker_scan,
        path_walk_db_cache_key,
    )
    from hierwalk.preprocess import preprocess_file_for_index

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    path = str(rtl.resolve())
    cache_dir = tmp_path / "pw-cache"
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={})
    cache_key = path_walk_db_cache_key([path], defines={})
    db = PathWalkModuleDb(
        [path],
        index,
        defines={},
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    db._preprocessed_text_for_file(path)
    job = db._tier0_make_job(path)
    with patch(
        "hierwalk.preprocess.preprocess_file_for_index",
        wraps=preprocess_file_for_index,
    ) as spy:
        result = _tier0_worker_scan(job)
    assert spy.call_count == 0
    assert "top" in result.names


def test_suite_session_pipeline_text_conn(tmp_path: Path, monkeypatch):
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
    from hierwalk.filelist import parse_filelist
    from hierwalk.path_walk import (
        clear_path_walk_suite_session,
        run_path_walk_connect,
    )

    monkeypatch.setenv("HIERWALK_CONNECT_JOBS", "4")
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
        module top(input logic a0, input logic a1, output logic z0, output logic z1);
          leaf u0 (.in(a0), .out(z0));
          leaf u1 (.in(a1), .out(z1));
        endmodule
        module leaf(input logic in, output logic out);
          assign out = in;
        endmodule
        """,
        encoding="utf-8",
    )
    fl_path = tmp_path / "fl.f"
    fl_path.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path))
    clear_path_walk_suite_session()
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.u0.in", "top.z0", check_id="c0"),
            ConnectivityCheck("top.u1.in", "top.z1", check_id="c1"),
        ),
        top="top",
    )
    batch, _, state = run_path_walk_connect(
        request,
        fl,
        top="top",
        connect_jobs=4,
        no_cache=True,
        connect_phase="text",
        reuse_suite_session=True,
    )
    assert len(batch.results) == 2
    for result in batch.results:
        assert result.connected, result.errors
    assert len(state.rows_by_path) >= 3
    clear_path_walk_suite_session()