"""Integration search tests on hc_hierarchy unified_verify corpus."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_CANDIDATE_ROOTS = (
    Path("/home/user/tools/__CFI/hc_hierarchy/design/unified_verify"),
    Path("/home/user/tools/CodeFromAI/hc_hierarchy/design/unified_verify"),
)

UNIFIED_VERIFY = next((p for p in _CANDIDATE_ROOTS if (p / "filelist.f").is_file()), None)
FILELIST = UNIFIED_VERIFY / "filelist.f" if UNIFIED_VERIFY else None
TOP = "hc_verify_top"

pytestmark = pytest.mark.skipif(
    FILELIST is None,
    reason="unified_verify corpus not available",
)


def _scan(*extra: str) -> list[dict[str, str]]:
    cmd = [
        "hier-walk",
        str(FILELIST),
        "--top",
        TOP,
        "--no-cache",
        "--quiet",
        *extra,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(UNIFIED_VERIFY),
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cols = line.split("\t")
        rows.append(dict(zip(headers, cols)))
    return rows


def _paths(rows: list[dict[str, str]]) -> set[str]:
    return {r["full_path"] for r in rows}


def test_unified_verify_baseline_row_count():
    rows = _scan()
    assert len(rows) >= 40


def test_search_inst_glob_bind():
    rows = _scan("--search", "*bind*")
    assert _paths(rows) == {"hc_verify_top.u_bind_wrap"}


def test_search_multi_pattern_or():
    rows = _scan("--search", "u_bind*,u_ecc*")
    assert _paths(rows) == {
        "hc_verify_top.u_bind_wrap",
        "hc_verify_top.u_ecc_engine_00",
    }


def test_search_dotted_path_segments():
    rows = _scan("--search", "hc_verify_top.*u_*bind*.*sub*")
    assert _paths(rows) == {"hc_verify_top.u_bind_wrap.u_sub"}


def test_search_gen_instances():
    rows = _scan("--search", "*gen*")
    assert _paths(rows) == {
        "hc_verify_top.u_gen_if",
        "hc_verify_top.u_gen_soc",
        "hc_verify_top.u_param_gen",
    }


def test_search_dotted_gen_cell():
    rows = _scan("--search", "hc_verify_top.*u_*gen*..*cell*")
    paths = _paths(rows)
    assert "hc_verify_top.u_gen_soc.gen_blk.gen_loop[0].u_cell" in paths
    assert "hc_verify_top.u_gen_soc.gen_blk.gen_loop[1].u_cell" in paths


def test_search_subtree_under_gen_soc():
    rows = _scan("--search", "u_gen_soc", "--search-subtree")
    paths = _paths(rows)
    assert "hc_verify_top.u_gen_soc" in paths
    assert "hc_verify_top.u_gen_soc.gen_blk.gen_loop[0].u_cell" in paths
    assert "hc_verify_top.u_gen_soc.gen_blk.gen_loop[1].u_cell" in paths
    assert "hc_verify_top.u_gen_soc.u_alt" in paths
    assert all(p.startswith("hc_verify_top.u_gen_soc") for p in paths)


def test_search_path_clk_glob():
    rows = _scan("--search-path", "hc_verify_top.u_*.clk")
    paths = _paths(rows)
    assert "hc_verify_top.u_bus[0].clk" in paths
    assert "hc_verify_top.u_ecc_engine_00.clk" in paths
    assert all(r["port_found"] == "True" for r in rows)


def test_search_path_ecc_idx_literal():
    rows = _scan("--search-path", "hc_verify_top.u_ecc_engine_00.idx[3]")
    assert len(rows) == 1
    hit = rows[0]
    assert hit["full_path"] == "hc_verify_top.u_ecc_engine_00.idx[3]"
    assert hit["port_found"] == "True"
    assert hit["port"] == "idx[3]"
    assert "path-refined" in hit.get("port_param_note", "")
    assert hit.get("path_chain", "")


def test_search_path_arr_hierarchy_exists():
    rows = _scan("--search", "hc_verify_top.u_arr.b[*].c[*]")
    assert {
        "hc_verify_top.u_arr.b[0].c[0]",
        "hc_verify_top.u_arr.b[1].c[1]",
    } <= _paths(rows)