"""Path-walk tier1 must not inline massive `` `include `` trees by default."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hierwalk.index import DesignIndex
from hierwalk.path_walk_db import PathWalkModuleDb


def test_tier1_default_skips_include_expansion(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HIERWALK_PW_TIER1_INCLUDES", raising=False)
    inc = tmp_path / "huge.vh"
    inc.write_text(
        "\n".join(f"`define INC_{i} 1" for i in range(4000)) + "\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "bla.v"
    rtl.write_text(
        f'`include "{inc.name}"\n'
        "module top();\n"
        "  child u_a ();\n"
        "endmodule\n"
        "module child(); endmodule\n",
        encoding="utf-8",
    )
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources(
        [path],
        include_dirs=[str(tmp_path)],
        defines={},
    )
    db = PathWalkModuleDb(
        [path],
        index,
        include_dirs=[str(tmp_path)],
        defines={},
        no_cache=True,
    )

    t0 = time.perf_counter()
    modules = db.tier1_scan_file(path)
    elapsed = time.perf_counter() - t0

    assert elapsed < 5.0
    top = modules["top"]
    assert any(e.inst_name == "u_a" for e in top.instances)


def test_tier1_opt_in_follows_includes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIERWALK_PW_TIER1_INCLUDES", "1")
    body = tmp_path / "body.v"
    body.write_text(
        "module top();\n"
        "  child u_z ();\n"
        "endmodule\n"
        "module child(); endmodule\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "wrap.v"
    rtl.write_text(f'`include "{body.name}"\n', encoding="utf-8")
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources(
        [path],
        include_dirs=[str(tmp_path)],
        defines={},
    )
    db = PathWalkModuleDb(
        [path],
        index,
        include_dirs=[str(tmp_path)],
        defines={},
        no_cache=True,
    )
    modules = db.tier1_scan_file(path)
    assert "top" in modules
    assert any(e.inst_name == "u_z" for e in modules["top"].instances)