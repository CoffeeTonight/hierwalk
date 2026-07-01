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