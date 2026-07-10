"""Normalize hierarchy specs: inst chain vs leaf (port/signal)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from hierwalk.inst_scan import coarse_hierarchy_path, inst_base_name


@dataclass(frozen=True)
class NormSpec:
    """Gate-aligned spec split: inst hops + optional leaf tail."""

    raw: str
    coarse: str
    top: str
    inst_segments: Tuple[str, ...]
    leaf_tail: Optional[str]
    full_key: str
    inst_key: str

    @property
    def inst_hop_count(self) -> int:
        return max(0, len(self.inst_segments) - 1)


def _segments_after_top(coarse: str, top: str) -> List[str]:
    parts = [inst_base_name(p) for p in coarse.split(".") if p]
    top_base = inst_base_name(top)
    if not parts:
        return [top_base]
    if parts[0] != top_base:
        if coarse.startswith(top_base + "."):
            return parts
        return [top_base, *parts]
    return parts


def normalize_spec(spec: str, *, top: str) -> NormSpec:
    """Split *spec* into top + inst segments; last segment may be leaf tail."""
    raw = str(spec or "").strip()
    coarse = coarse_hierarchy_path(raw)
    top_name = inst_base_name(top.strip())
    segs = _segments_after_top(coarse, top_name)
    inst_key = ".".join(segs)
    full_key = inst_key

    leaf_tail: Optional[str] = None
    if len(segs) > 1 and "." in coarse:
        leaf_tail = segs[-1]
        inst_segs = tuple(segs[:-1])
        inst_key = ".".join(inst_segs)
    else:
        inst_segs = tuple(segs)

    return NormSpec(
        raw=raw,
        coarse=coarse,
        top=top_name,
        inst_segments=inst_segs,
        leaf_tail=leaf_tail,
        full_key=full_key,
        inst_key=inst_key,
    )


def common_prefix_segments(paths: List[str]) -> Tuple[str, ...]:
    """Longest shared segment prefix among dot paths (pathlib-style)."""
    if not paths:
        return ()
    split = [p.split(".") for p in paths if p]
    if not split:
        return ()
    prefix: List[str] = []
    for cols in zip(*split):
        if len(set(cols)) == 1:
            prefix.append(cols[0])
        else:
            break
    return tuple(prefix)