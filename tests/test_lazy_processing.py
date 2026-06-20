"""Lazy processing: scoped elab, filelist defer, include warm policy."""

from __future__ import annotations

from pathlib import Path

from hierwalk.cache import build_design_index
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.lazy_scope import elab_scope_paths
from hierwalk.perf import include_warm_enabled
from hierwalk.preprocess import _warm_include_cache_for_sources


def test_elab_scope_paths_filters_under_top():
    scope = elab_scope_paths(
        ["soc.u_pcie.clk", "soc.u_mem.din"],
        top="soc",
    )
    assert "soc" in scope
    assert "soc.u_pcie" in scope
    assert "soc.u_pcie.clk" in scope
    assert "soc.u_mem.din" in scope
    assert "other" not in scope


def test_scoped_elab_skips_off_path_children(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
module leaf; endmodule
module mid; leaf u_l (); endmodule
module top;
  mid u_on ();
  mid u_off ();
endmodule
""",
        encoding="utf-8",
    )
    top_f = tmp_path / "design.f"
    top_f.write_text(f"{rtl}\n", encoding="utf-8")
    fl = parse_filelist(str(top_f))
    index = build_design_index(
        fl,
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
        ignore_filelists=[],
        jobs=1,
    )
    scope = elab_scope_paths(["top.u_on.u_l"], top="top")

    _, rows = elaborate(index, "top", scope_paths=scope)
    paths = {r.full_path for r in rows}

    assert "top" in paths
    assert "top.u_on" in paths
    assert "top.u_on.u_l" in paths
    assert "top.u_off" not in paths


def test_lazy_filelist_skips_nested_ignore_filelist(tmp_path: Path):
    block_f = tmp_path / "block.f"
    main_f = tmp_path / "main.f"
    block_dir = tmp_path / "block_rtl"
    main_dir = tmp_path / "main_rtl"
    block_dir.mkdir()
    main_dir.mkdir()
    block_v = block_dir / "block.v"
    main_v = main_dir / "main.v"
    block_v.write_text("module block; endmodule\n", encoding="utf-8")
    main_v.write_text("module main; endmodule\n", encoding="utf-8")
    block_f.write_text(f"{block_v}\n", encoding="utf-8")
    main_f.write_text(f"{main_v}\n", encoding="utf-8")
    top_f = tmp_path / "top.f"
    top_f.write_text(f"-f {block_f.name}\n-f {main_f.name}\n", encoding="utf-8")

    fl = parse_filelist(
        str(top_f),
        ignore_filelists=["block.f"],
        defer_source_exists=True,
    )
    sources = {p.name for p in fl.source_files}
    assert "main.v" in sources
    assert "block.v" not in sources


def test_include_warm_opt_in_off_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("HIERWALK_INCLUDE_WARM", raising=False)
    monkeypatch.delenv("HIERWALK_NO_INCLUDE_WARM", raising=False)
    assert include_warm_enabled() is False

    src = tmp_path / "m.v"
    src.write_text('`include "missing.vh"\nmodule m; endmodule\n', encoding="utf-8")
    lines: list[str] = []
    warmed = _warm_include_cache_for_sources(
        [src],
        [tmp_path],
        {},
        on_progress=lines.append,
    )
    assert warmed == 0
    assert any("HIERWALK_INCLUDE_WARM=1" in line for line in lines)
    assert not any("include discovery" in line for line in lines)