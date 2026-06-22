"""Confident (child filelist) vs recovery (subtree/global) resolve policy."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.path_walk_db import RESOLVE_CONFIDENT, RESOLVE_RECOVERY


def _write_nested_child_fl_design(tmp_path: Path) -> Path:
    (tmp_path / "top.v").write_text(
        "module SOC_TOP; BLK u_blk (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "core.v").write_text(
        "module CORE; LEAF u_leaf (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "leaf.v").write_text("module LEAF; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        "\n".join(
            str((tmp_path / n).resolve())
            for n in ("blk_real.v", "core.v", "leaf.v")
        )
        + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(f"-f {(lists / 'parent.f').resolve()}\n", encoding="utf-8")
    return root


def _write_stub_child_recovery_design(tmp_path: Path) -> tuple[Path, str]:
    (tmp_path / "top.v").write_text(
        "module SOC_TOP; BLK u_blk (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "blk_stub.v").write_text("module BLK; endmodule\n", encoding="utf-8")
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "core.v").write_text(
        "module CORE; LEAF u_leaf (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "leaf.v").write_text("module LEAF; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        str((tmp_path / "blk_stub.v").resolve()) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(
        "\n".join(
            [
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp_path / "blk_real.v").resolve()),
                str((tmp_path / "core.v").resolve()),
                str((tmp_path / "leaf.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "SOC_TOP.u_blk.u_core.u_leaf"


def test_confident_resolves_module_in_direct_child_filelist(tmp_path: Path):
    fl_path = _write_nested_child_fl_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.path_walk import create_path_walk_index

    index, mod_db = create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True)
    assert mod_db.resolve_child_edge(
        "SOC_TOP",
        {},
        "u_blk",
        current_file=str((tmp_path / "top.v").resolve()),
        policy=RESOLVE_CONFIDENT,
    ) is not None


def test_recovery_pass1_retries_mapped_candidates_when_pending_zero(tmp_path: Path):
    """Global tier0 done (pending==0) must still retry _module_to_files entries."""
    fl_path, _leaf = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.path_walk import create_path_walk_index

    _index, mod_db = create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True)
    blk_stub = str((tmp_path / "blk_stub.v").resolve())
    blk_real = str((tmp_path / "blk_real.v").resolve())

    assert mod_db._scan_remaining_sources_tier0(None, target_module="BLK") == 0
    assert "BLK" in mod_db._module_to_files
    assert blk_real in mod_db._module_to_files["BLK"]

    refreshed = mod_db._recovery_pass1_candidates(
        "BLK",
        pending=0,
        tried={blk_stub},
        avoid_file=blk_stub,
        scope_anchor=blk_stub,
        trace_label="module=BLK",
    )
    assert refreshed is not None
    assert blk_real in refreshed
    assert blk_stub not in refreshed

    edge = mod_db.resolve_child_edge(
        "BLK",
        {},
        "u_core",
        current_file=blk_stub,
        policy=RESOLVE_RECOVERY,
    )
    assert edge is not None
    assert edge.child_module == "CORE"


def test_confident_defers_then_recovery_walks_full_chain(tmp_path: Path):
    fl_path, leaf = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top="SOC_TOP",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="SOC_TOP",
        no_cache=True,
        trace_stream=buf,
    )
    text = buf.getvalue()
    assert "confident-miss defer" in text
    assert "recovery-pass start" in text
    assert leaf in state.rows_by_path
    assert batch.results[0].connected is True


def test_recovery_skips_duplicate_ensure_for_rewalk_targets(tmp_path: Path):
    """Recovery apply skips ensure_path when endpoint re-walk will cover it."""
    from hierwalk.path_walk import (
        _recovery_skip_ensure_targets,
        _recovery_target_covered_by_rewalk,
    )
    from hierwalk.path_walk_db import DeferredResolve, RESOLVE_RECOVERY

    leaf = "SOC_TOP.u_blk.u_core.u_leaf"
    spec_targets = {leaf: leaf}
    affected = [leaf]
    recovered = [
        DeferredResolve(
            kind="module",
            module_name="CORE",
            scope_anchor="SOC_TOP.u_blk.u_core",
            target_path="SOC_TOP.u_blk.u_core",
        ),
        DeferredResolve(
            kind="module",
            module_name="LEAF",
            scope_anchor=leaf,
            target_path=leaf,
        ),
    ]
    assert _recovery_target_covered_by_rewalk(
        "SOC_TOP.u_blk.u_core",
        spec_targets,
        affected,
    )
    skip = _recovery_skip_ensure_targets(recovered, spec_targets, affected)
    assert skip == {"SOC_TOP.u_blk.u_core", leaf}

    fl_path, leaf_path = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf_path, leaf_path),),
        top="SOC_TOP",
    )
    recovery_ensures: list[tuple[str, str]] = []

    from hierwalk import path_walk as pw

    orig_apply = pw.PathWalkState._apply_recovery_results

    def traced_apply(self, recovered_items, *, skip_targets=None):
        for item in recovered_items:
            path = item.target_path
            if path and (skip_targets is None or path not in skip_targets):
                recovery_ensures.append((path, RESOLVE_RECOVERY))
        return orig_apply(self, recovered_items, skip_targets=skip_targets)

    pw.PathWalkState._apply_recovery_results = traced_apply
    try:
        batch, _index, state = run_path_walk_connect(
            req,
            fl,
            top="SOC_TOP",
            no_cache=True,
        )
    finally:
        pw.PathWalkState._apply_recovery_results = orig_apply

    assert leaf_path in state.rows_by_path
    assert batch.results[0].connected is True
    assert recovery_ensures == []