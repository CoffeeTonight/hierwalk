"""Bit-slice policy: do not conflate different selects."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyhirewalk.conn.slice_policy import (  # noqa: E402
    LEVEL_BASE,
    LEVEL_SLICE_ID,
    annotate_pair_slice,
    coalesce_seeds,
    normalize_sel,
    parse_hierarchy_seed,
    parse_sel_string,
    ranges_compatible_for_pair,
    seeds_from_paths,
    sel_class,
)


def test_normalize_sel_order() -> None:
    n, approx = normalize_sel("[4:7]")
    assert not approx
    assert n == "[7:4]"
    n2, _ = normalize_sel("[3]")
    assert n2 == "[3]"


def test_parse_hierarchy_seed_slice() -> None:
    h, b, s, a = parse_hierarchy_seed("top.u.bus[7:4]")
    assert h == "top.u" and b == "bus" and s == "[7:4]" and not a
    h2, b2, s2, a2 = parse_hierarchy_seed("top.u.bus[WIDTH-1:0]")
    assert b2 == "bus" and s2 is None and a2


def test_different_sels_not_same_strict_key() -> None:
    seeds = seeds_from_paths(
        ["top.u.bus[0]", "top.u.bus[1]", "top.u.bus[0]"]
    )
    groups = coalesce_seeds(seeds, mode="strict_slice")
    # [0] coalesced (2 paths), [1] separate
    assert len(groups) == 2
    sizes = sorted(len(g.members) for g in groups)
    assert sizes == [1, 2]


def test_base_mode_warns_mixed() -> None:
    seeds = seeds_from_paths(["top.u.bus", "top.u.bus[3:0]"])
    groups = coalesce_seeds(seeds, mode="base")
    assert len(groups) == 1
    assert groups[0].mixed_or_approx is True


def test_non_overlapping_range_level_base() -> None:
    ok, level = ranges_compatible_for_pair("[7:4]", "[3:0]", allow_base=True)
    assert ok and level == LEVEL_BASE
    ok2, level2 = ranges_compatible_for_pair("[3]", "[3]", allow_base=True)
    assert ok2 and level2 == LEVEL_SLICE_ID


def test_annotate_pair_notes_different_sels() -> None:
    meta = annotate_pair_slice(
        "top.u.x[0]",
        "top.u.y[1]",
        evidence=[],
        allow_base_meet=True,
    )
    assert meta["connectivity_level"] == LEVEL_BASE
    assert meta["src_sel"] == "[0]" and meta["dst_sel"] == "[1]"
    assert any("differ" in n for n in meta["slice_notes"])


def test_sel_class_isolates() -> None:
    s0 = seeds_from_paths(["t.u.b[0]"])[0]
    s1 = seeds_from_paths(["t.u.b[1]"])[0]
    assert sel_class(s0) != sel_class(s1)
    assert sel_class(s0) == "[0]"
