"""Tier1 defines cache must stay valid across tier0 jobs and tier1 apply."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hierwalk.index import DesignIndex
from hierwalk.path_walk_db import PathWalkModuleDb


def test_tier0_make_job_after_apply_file_modules(tmp_path: Path):
    """``_apply_file_modules`` must not leave ``_tier1_defines_cache`` as None."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top(input a); child u_c(); endmodule\n"
        "module child(); endmodule\n",
        encoding="utf-8",
    )
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={"SEED": "1"})
    db = PathWalkModuleDb([path], index, defines={"SEED": "1"}, no_cache=True)

    modules = db.tier1_scan_file(path)
    db._apply_file_modules(path, modules)

    job = db._tier0_make_job(path)
    assert ("SEED", "1") in job.defines


def test_seed_top_module_tier1_warms_instances(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top(input a); child u_a(); endmodule\n"
        "module child(); endmodule\n",
        encoding="utf-8",
    )
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources([path], include_dirs=[], defines={})
    db = PathWalkModuleDb([path], index, defines={}, no_cache=True)

    db.seed_top_module("top", path)
    rec = index.get_module("top")
    assert rec is not None
    assert any(e.inst_name == "u_a" for e in rec.instances)


def test_include_closure_digest_direct_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HIERWALK_PW_INCLUDE_CLOSURE_FULL", raising=False)
    rtl = tmp_path / "top.v"
    inc = tmp_path / "a.vh"
    inc.write_text("`define A 1\n", encoding="utf-8")
    nested = tmp_path / "b.vh"
    nested.write_text(f'`include "{inc.name}"\n', encoding="utf-8")
    rtl.write_text(
        f'`include "{nested.name}"\nmodule top(); endmodule\n',
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

    with patch(
        "hierwalk.preprocess._collect_include_closure",
    ) as full_spy:
        db._include_closure_digest(path)

    full_spy.assert_not_called()