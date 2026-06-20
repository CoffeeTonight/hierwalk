"""Unit tests for instance-name search token normalization."""

from __future__ import annotations

from hierwalk.models import FlatRow
from hierwalk.search import _instance_search_name, row_matches_search_pattern


def _row(inst_leaf: str, *, full_path: str = "top.u") -> FlatRow:
    return FlatRow(
        full_path=full_path,
        inst_leaf=inst_leaf,
        module="leaf",
        depth=1,
        parent_path="top",
        file="leaf.v",
    )


def test_instance_search_name_preserves_escaped_dotted_id():
    row = _row(r"\foo.bar", full_path=r"top.\foo.bar")
    assert _instance_search_name(row) == r"\foo.bar"


def test_instance_search_name_uses_trailing_segment_for_unescaped_dotted_leaf():
    row = _row("genblk.u_target", full_path="top.genblk.u_target")
    assert _instance_search_name(row) == "u_target"
    assert row_matches_search_pattern(
        row,
        "u_target",
        match_inst=True,
        match_module=False,
    )
    assert not row_matches_search_pattern(
        row,
        "genblk.u_target",
        match_inst=True,
        match_module=False,
        pattern_kind="auto",
    )