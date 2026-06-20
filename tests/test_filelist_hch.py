"""Filelist expansion matches hc_hierarchy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HCH_SRC = Path("/home/user/tools/CodeFromAI/hc_hierarchy/src")
if HCH_SRC.is_dir() and str(HCH_SRC) not in sys.path:
    sys.path.insert(0, str(HCH_SRC))

from hierwalk.filelist import parse_filelist


@pytest.mark.skipif(not HCH_SRC.is_dir(), reason="hc_hierarchy not available")
def test_filelist_matches_hc_unified_verify():
    from hch.ingest.filelist import parse_filelist_simple

    repo = HCH_SRC.parent / "design" / "unified_verify"
    fl = repo / "filelist.f"
    if not fl.is_file():
        pytest.skip("fixture missing")
    cwd = str(repo)
    hc = parse_filelist_simple(str(fl), index_cwd=cwd)
    si = parse_filelist(str(fl), index_cwd=cwd)
    hc_src = {p.resolve() for p in hc.source_files}
    si_src = {p.resolve() for p in si.source_files}
    assert si_src == hc_src
    assert set(si.defines) == set(hc.defines)
    assert {p.resolve() for p in si.include_dirs} == {p.resolve() for p in hc.incdirs}