"""hgrep + path-walk integration: full walk must not be clamped by partial scopes."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.hierarchy_grep_gate import (
    HierarchyGrepCheckGate,
    scoped_sources_for_gate,
)
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.connect.shared.request import ConnectivityRequest


def test_scoped_sources_for_gate_fallback_uses_all_sources():
    gate = HierarchyGrepCheckGate(
        status="fallback",
        log_line="test",
        scoped_files=("/only/partial.v",),
        rows=(),
    )
    all_src = ("/a.v", "/b.v", "/only/partial.v")
    got = scoped_sources_for_gate(gate, all_src)
    assert set(got) == set(all_src)


def test_scoped_sources_for_gate_pass_keeps_scope():
    gate = HierarchyGrepCheckGate(
        status="pass",
        log_line="test",
        scoped_files=("/only/partial.v",),
        rows=(),
    )
    all_src = ("/a.v", "/b.v", "/only/partial.v")
    got = scoped_sources_for_gate(gate, all_src)
    assert list(got) == ["/only/partial.v"]


def test_zigzag_scope_checks_pass_with_hgrep_pathwalk(tmp_path: Path):
    """Regression: scope/ifdef endpoints must connect under hgrep+text pipeline."""
    zz = Path("/home/user/Desktop/hgrep_demo/.zz_verify")
    if not (zz / "zz_torture.connect.json").is_file():
        import pytest

        pytest.skip("zigzag artifacts missing")

    from hierwalk.connect.shared.request import load_connect_request

    base = load_connect_request(zz / "zz_torture.connect.json")
    want = {
        "zz_scope_confident_b",
        "zz_ifdef_nested_u_b",
        "zz_scope_b_to_c",
        "zz_scope_c_to_b",
    }
    checks = tuple(c for c in base.checks if c.check_id in want)
    assert len(checks) == 4
    fl = parse_filelist(
        str(zz / "filelist.f"),
        index_cwd=str(zz),
        extra_defines=dict(base.defines) or None,
    )
    req = ConnectivityRequest(
        checks=checks,
        top=base.top,
        defines=dict(base.defines),
        include_ff=True,
    )
    work = tmp_path / "db"
    batch, _idx, _st = run_path_walk_connect(
        req,
        fl,
        top=base.top,
        extra_defines=dict(base.defines),
        no_cache=True,
        connect_phase="text",
        connect_output_dir=work,
        connect_output_name="conn.tsv",
    )
    fails = []
    for r in batch.results:
        ok = (
            all(sr.connected for sr in r.sub_results)
            if r.sub_results
            else bool(r.connected)
        )
        if not ok:
            fails.append(f"{r.check_id}: {r.errors} {r.note}")
    assert not fails, "\n".join(fails)
