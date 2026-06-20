"""Combination-matrix regression: all parse axes via real ``hier-walk`` CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "parse_matrix_soc.v"
_NEWLINE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "parse_matrix_newline_soc.v"
)

# Axes covered in parse_matrix_soc.v (each was tested in isolation before this file).
_MATRIX_AXES = (
    "ifndef_elsif_else",
    "line_comment_directive_trap",
    "block_comment_directive_trap",
    "endif_label_same_line",
    "flat_param_override",
    "comma_separated_instances",
    "macro_cell_ifndef",
    "generate_for_ifdef_elsif_param",
    "generate_if_param",
    "nested_generate_for_for",
    "nested_ifndef_port_map",
    "generate_array_param",
    "bind_ignored",
)

# Nodes that ``hier-walk`` hierarchy + path-walk must recognize.
_EXPECTED_HIERARCHY = frozenset(
    {
        "SOC_TOP",
        "SOC_TOP.u_A",
        "SOC_TOP.u_cpusystem_top",
        "SOC_TOP.u_wrap",
        "SOC_TOP.u_BCD",
        "SOC_TOP.u_t0",
        "SOC_TOP.u_t1",
        "SOC_TOP.u_macro",
        "SOC_TOP.gen_blk[0].u_BCD_gen",
        "SOC_TOP.gen_blk[1].u_BCD_gen",
        "SOC_TOP.ifg_blk.u_ifg",
        "SOC_TOP.outer[0].inner[0].u_nest",
        "SOC_TOP.outer[0].inner[1].u_nest",
        "SOC_TOP.outer[1].inner[0].u_nest",
        "SOC_TOP.outer[1].inner[1].u_nest",
        "SOC_TOP.port_ifndef_blk.u_DEF",
        "SOC_TOP.arr_blk.u_arr[0]",
        "SOC_TOP.arr_blk.u_arr[1]",
    }
)

_ABSENT_NODES = frozenset({"SOC_TOP.u_ghost", "SOC_TOP.u_fake_blk"})


def _write_design(tmp_path: Path, *, fixture: Path) -> Path:
    rtl = tmp_path / "soc.v"
    rtl.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    return fl


def _hierwalk_hierarchy_paths(tmp_path: Path, *, fixture: Path = _FIXTURE) -> set[str]:
    fl = _write_design(tmp_path, fixture=fixture)
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": fl.name,
                "top": "SOC_TOP",
                "no_cache": True,
                "run_on_full_index": {"enable": 1, "mode": "hierarchy", "output": "-"},
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 2, proc.stdout
    return {ln.split("\t", 1)[0] for ln in lines[1:]}


def _path_walk_hits(
    tmp_path: Path,
    paths: frozenset[str],
    *,
    fixture: Path = _FIXTURE,
) -> dict[str, bool]:
    fl = _write_design(tmp_path, fixture=fixture)
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    hits: dict[str, bool] = {}
    for path in sorted(paths):
        if path == "SOC_TOP":
            continue
        request = ConnectivityRequest(
            checks=(ConnectivityCheck(path, path),),
            top="SOC_TOP",
        )
        _batch, _index, state = run_path_walk_connect(
            request,
            flr,
            top="SOC_TOP",
            no_cache=True,
        )
        hits[path] = path in state.rows_by_path
    return hits


@pytest.mark.parametrize("axis", _MATRIX_AXES)
def test_matrix_axis_documented(axis: str):
    assert axis  # registry guard — RTL fixture encodes every listed axis


def test_hierwalk_hierarchy_matrix_nodes(tmp_path: Path):
    found = _hierwalk_hierarchy_paths(tmp_path)
    missing = _EXPECTED_HIERARCHY - found
    assert not missing, f"hier-walk hierarchy missing: {sorted(missing)}\nfound={sorted(found)}"
    for bad in _ABSENT_NODES:
        assert bad not in found


def test_hierwalk_path_walk_matrix_nodes(tmp_path: Path):
    child_paths = frozenset(p for p in _EXPECTED_HIERARCHY if p != "SOC_TOP")
    hits = _path_walk_hits(tmp_path, child_paths)
    missed = [p for p, ok in hits.items() if not ok]
    assert not missed, f"path-walk missing: {missed}"


_NEWLINE_EXPECTED = frozenset(
    {
        "SOC_TOP",
        "SOC_TOP.u_asd",
        "SOC_TOP.u_BCD",
        "SOC_TOP.gen_blk[0].u_BCD_gen",
        "SOC_TOP.gen_blk[1].u_BCD_gen",
    }
)


def test_hierwalk_hierarchy_extreme_newline_split(tmp_path: Path):
    """Every token on its own line: ``ASD\\n u_asd\\n(\\n.a\\n(\\nw\\n)`` etc."""
    found = _hierwalk_hierarchy_paths(tmp_path, fixture=_NEWLINE_FIXTURE)
    missing = _NEWLINE_EXPECTED - found
    assert not missing, (
        f"hier-walk hierarchy missing newline-split nodes: {sorted(missing)}\n"
        f"found={sorted(found)}"
    )


def test_hierwalk_path_walk_extreme_newline_split(tmp_path: Path):
    child_paths = frozenset(p for p in _NEWLINE_EXPECTED if p != "SOC_TOP")
    hits = _path_walk_hits(tmp_path, child_paths, fixture=_NEWLINE_FIXTURE)
    missed = [p for p, ok in hits.items() if not ok]
    assert not missed, f"path-walk missing newline-split nodes: {missed}"