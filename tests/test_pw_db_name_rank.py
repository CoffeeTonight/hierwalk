"""pw-db candidate ordering: filelist proximity + RTL name similarity."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import create_path_walk_index


def test_resolve_rank_prefers_name_match_at_equal_proximity(tmp_path: Path):
    parent = tmp_path / "parent.v"
    parent.write_text("module parent; child u_child(); endmodule\n", encoding="utf-8")
    child_a = tmp_path / "child_a.v"
    child_a.write_text("module child_a; endmodule\n", encoding="utf-8")
    child_b = tmp_path / "zzz_unrelated.v"
    child_b.write_text("module zzz_unrelated; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(
        "\n".join(
            str(p.resolve())
            for p in (parent, child_a, child_b)
        )
        + "\n",
        encoding="utf-8",
    )
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    _index, mod_db = create_path_walk_index(fl, "parent", defines={}, no_cache=True)

    ranked = mod_db._sort_files_by_resolve_rank(
        [str(child_b.resolve()), str(child_a.resolve())],
        scope_anchor=str(parent.resolve()),
        module_name="child",
        inst_leaf="u_child",
    )
    assert Path(ranked[0]).name == "child_a.v"


def test_trace_verbose_shows_tier0_scan(monkeypatch):
    from hierwalk.hierarchy_log import path_walk_trace_show_message

    monkeypatch.setenv("HIERWALK_PW_TRACE_VERBOSE", "1")
    assert path_walk_trace_show_message("pw-db tier0 scan foo.v -> MOD")


def test_trace_heartbeat_always_visible():
    from hierwalk.hierarchy_log import path_walk_trace_show_message

    assert path_walk_trace_show_message(
        "pw-db heartbeat tier0=3 tier1=1 phase=parsing"
    )