"""Path-walk stress: 4 sets × 10-deep zigzag × 10-bit array (40 checks)."""

from __future__ import annotations

import re

import pytest

from hierwalk.connect_request import ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.path_walk_stress_gen import (
    DEPTH,
    SET_IDS,
    build_connect_request,
    generate_path_walk_stress_design,
    write_stress_artifacts,
)


@pytest.fixture
def stress_bundle(tmp_path):
    fl, req_path, design = write_stress_artifacts(tmp_path / "pw_stress")
    return fl, design


def test_stress_design_shape():
    design = generate_path_walk_stress_design()
    assert design.depth == DEPTH
    assert design.sets == SET_IDS
    assert len(design.checks) == 4 * 10
    assert "pw_top.v" in design.files
    assert "pw_zig_A_0.v" in design.files
    zig_mods = [n for n in design.files if re.match(r"pw_zig_[ABCD]_\d+\.v$", n)]
    assert len(zig_mods) == 4 * DEPTH


def test_path_walk_stress_four_sets_zigzag(stress_bundle, tmp_path):
    fl_path, design = stress_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    request = build_connect_request(design)
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top=design.top,
        on_progress=None,
    )
    assert len(batch.results) == 40
    failures = [r for r in batch.results if not r.connected]
    if failures:
        lines = [
            f"{r.check_id}: {r.endpoint_a.spec} -> {r.endpoint_b.spec} "
            f"connected={r.connected} errors={r.errors} note={r.note}"
            for r in failures[:8]
        ]
        pytest.fail(
            "path-walk stress connect failures "
            f"({len(failures)}/40):\n" + "\n".join(lines)
        )
    assert batch.modules_cached >= 4
    assert state.stats.modules_loaded <= 4 * DEPTH + len(SET_IDS) + 8
    assert state.stats.modules_loaded >= DEPTH
    assert state.stats.checks_run == 40


def test_path_walk_stress_fewer_modules_than_full_index(stress_bundle, tmp_path):
    """On-demand load should not pull every RTL file from the filelist."""
    fl_path, design = stress_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    request = ConnectivityRequest(
        checks=design.checks[:10],
        top=design.top,
        defines=build_connect_request(design).defines,
    )
    _batch, index, state = run_path_walk_connect(request, fl, top=design.top)
    assert state.stats.modules_loaded <= 4 * DEPTH + len(SET_IDS) + 4
    assert len(index.modules) <= 50