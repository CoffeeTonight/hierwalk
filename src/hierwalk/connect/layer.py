"""Connect parse layers: L0 raw body, L1 preprocess, L2 text grep, L3 logical COI."""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Set, TYPE_CHECKING

from hierwalk.connect.shared.endpoints import ModuleIndexCacheKey

if TYPE_CHECKING:
    from hierwalk.connect.text.index import TextGrepCache, TextGrepIndex


def text_cache_key_for_logical(
    logical_key: ModuleIndexCacheKey,
    *,
    text_ff_barrier: bool = True,
) -> ModuleIndexCacheKey:
    """Map a logical ``mod_cache`` key to the matching L2 text-grep cache key."""
    mod, ctx, body_d, defines_d, bind_d, _ff, over_approx, _resolve = logical_key
    return (
        mod,
        ctx,
        body_d,
        defines_d,
        bind_d,
        text_ff_barrier,
        over_approx,
        False,
    )


def find_text_grep_seed(
    text_grep_cache: Optional["TextGrepCache"],
    logical_key: ModuleIndexCacheKey,
) -> Optional["TextGrepIndex"]:
    """Return warmed L2 text index for an L3 logical key, if present."""
    if not text_grep_cache:
        return None
    return text_grep_cache.get(text_cache_key_for_logical(logical_key))


def assign_adj_from_text_grep(text_idx: "TextGrepIndex") -> Dict[str, Set[str]]:
    """Expand L2 compressed ``rep_adj`` into an undirected assign adjacency seed."""
    adj: Dict[str, Set[str]] = {}
    for left, neighbors in text_idx.rep_adj.items():
        bucket = adj.setdefault(left, set())
        for right in neighbors:
            bucket.add(right)
            adj.setdefault(right, set()).add(left)
    return adj