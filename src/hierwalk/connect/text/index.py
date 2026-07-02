"""Text-conn module graph (grep-only, no constant-fold / FF metadata)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect.logical.scan import (
    ModuleConnectIndex,
    build_module_connect_index,
    net_representative,
)
from hierwalk.connect.shared.endpoints import (
    TextGrepIndexCacheKey,
    _mod_cache_lock,
    _resolve_module_index_key,
)
from hierwalk.index import DesignIndex

TextGrepCache = Dict[TextGrepIndexCacheKey, "TextGrepIndex"]


@dataclass
class TextGrepIndex:
    """Coarse RHS-name adjacency for text-conn walks (no logical COI metadata)."""

    inst_ports: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    net_rep: Dict[str, str] = field(default_factory=dict)
    rep_adj: Dict[str, Set[str]] = field(default_factory=dict)
    net_to_children: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    expr_roots: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    hier_links: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    hier_ref_targets: Dict[Tuple[str, str], Set[str]] = field(default_factory=dict)
    vector_bases: FrozenSet[str] = field(default_factory=frozenset)
    vector_scalar_rep: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_module_index(cls, mci: ModuleConnectIndex) -> TextGrepIndex:
        return cls(
            inst_ports=mci.inst_ports,
            net_rep=mci.net_rep,
            rep_adj=mci.rep_adj,
            net_to_children=mci.net_to_children,
            expr_roots=mci.expr_roots,
            hier_links=mci.hier_links,
            hier_ref_targets=mci.hier_ref_targets,
            vector_bases=mci.vector_bases,
            vector_scalar_rep=mci.vector_scalar_rep,
        )


def text_net_representative(idx: TextGrepIndex, net: str) -> str:
    """Coarse base-level net representative for text grep walks."""
    proxy = ModuleConnectIndex(
        net_rep=idx.net_rep,
        vector_scalar_rep=idx.vector_scalar_rep,
        resolve_param_dims=False,
    )
    return net_representative(proxy, net)


def build_text_grep_index(
    body: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
    defines: Optional[Mapping[str, str]] = None,
    over_approximate_if: bool = True,
) -> TextGrepIndex:
    """Build grep-only adjacency (no FF scan, no generate-fold, no param dim resolve)."""
    mci = build_module_connect_index(
        body,
        param_map=param_map,
        defines=defines,
        over_approximate_if=over_approximate_if,
        fold_generate=False,
        ff_barrier=True,
        resolve_param_dims=False,
    )
    return TextGrepIndex.from_module_index(mci)


def text_grep_index(
    cache: TextGrepCache,
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    *,
    on_cache_miss: Optional[Callable[[], None]] = None,
) -> TextGrepIndex:
    """Cached text grep index (separate from logical ``mod_cache``)."""
    key, _binds = _resolve_module_index_key(
        index,
        mod_name,
        param_ctx,
        defines,
        ff_barrier=True,
        over_approximate_if=over_approximate_if,
        resolve_param_dims=False,
    )
    hit = cache.get(key)
    if hit is not None:
        return hit
    with _mod_cache_lock(cache, key):
        hit = cache.get(key)
        if hit is not None:
            return hit
        rec = index.get_module(mod_name)
        body = index.module_body(mod_name) if rec else ""
        built = build_text_grep_index(
            body,
            param_map=param_ctx,
            defines=defines,
            over_approximate_if=over_approximate_if,
        )
        cache[key] = built
        if on_cache_miss is not None:
            on_cache_miss()
        return built


