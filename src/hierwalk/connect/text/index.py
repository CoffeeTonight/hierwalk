"""Text-conn module graph (grep-only, no constant-fold / FF metadata)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect.layer import assign_adj_from_text_grep
from hierwalk.connect.logical.scan import (
    ModuleConnectIndex,
    apply_bind_connectivity,
    apply_empty_module_passthrough,
    build_module_connect_index,
    collect_bind_records_for_module,
    net_representative,
)
from hierwalk.connect.shared.endpoints import (
    ModuleBodyCache,
    TextGrepIndexCacheKey,
    _empty_module_passthrough_ports,
    _mod_cache_lock,
    _resolve_module_index_key,
    make_cell_module_body_lookup,
)
from hierwalk.index import DesignIndex

TextGrepCache = Dict[TextGrepIndexCacheKey, "TextGrepIndex"]


def module_body_for_text_grep(
    index: DesignIndex,
    mod_name: str,
    *,
    module_body_cache: Optional[ModuleBodyCache] = None,
) -> str:
    """Module body for L2 grep index; prefers session cache, then pw-db-seeded TU text."""
    rec = index.get_module(mod_name)
    if not rec:
        return ""
    if rec.body:
        return rec.body
    if module_body_cache:
        file_path = rec.file_path or ""
        for key in (
            mod_name,
            file_path,
            str(Path(file_path)) if file_path else "",
            str(Path(file_path).resolve()) if file_path else "",
        ):
            if key:
                hit = module_body_cache.get(key)
                if hit:
                    return hit
    return index.module_body(mod_name)


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


def enrich_text_grep_to_logical_index(
    text_idx: TextGrepIndex,
    body: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
    defines: Optional[Mapping[str, str]] = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    source_file: str | None = None,
    include_dirs: Optional[Sequence[str]] = None,
    port_decl_widths: Optional[Mapping[str, List[int]]] = None,
    port_decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    module_body_lookup: Optional[Callable[[str], str]] = None,
) -> ModuleConnectIndex:
    """L3 logical COI: reuse L2 grep adjacency, add FF/param-dim enrichment."""
    return build_module_connect_index(
        body,
        param_map=param_map,
        defines=defines,
        over_approximate_if=over_approximate_if,
        fold_generate=True,
        ff_barrier=ff_barrier,
        resolve_param_dims=True,
        source_file=source_file,
        include_dirs=include_dirs,
        port_decl_widths=port_decl_widths,
        port_decl_md_suffixes=port_decl_md_suffixes,
        text_seed_adj=assign_adj_from_text_grep(text_idx),
        module_body_lookup=module_body_lookup,
    )


def build_text_grep_index(
    body: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
    defines: Optional[Mapping[str, str]] = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = True,
    module_body_lookup: Optional[Callable[[str], str]] = None,
    bind_records: Optional[Sequence[object]] = None,
    bind_index: Optional[DesignIndex] = None,
) -> TextGrepIndex:
    """Build grep-only adjacency (no generate-fold, no param dim resolve)."""
    mci = build_module_connect_index(
        body,
        param_map=param_map,
        defines=defines,
        over_approximate_if=over_approximate_if,
        fold_generate=False,
        ff_barrier=ff_barrier,
        resolve_param_dims=False,
        module_body_lookup=module_body_lookup,
    )
    if bind_records and bind_index is not None:
        apply_bind_connectivity(
            mci,
            bind_records,
            bind_index,
            param_map=param_map,
            defines=defines,
            over_approximate_if=over_approximate_if,
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
    ff_barrier: bool = True,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
    on_cache_miss: Optional[Callable[[], None]] = None,
) -> TextGrepIndex:
    """Cached text grep index (separate from logical ``mod_cache``)."""
    key, _binds = _resolve_module_index_key(
        index,
        mod_name,
        param_ctx,
        defines,
        ff_barrier=ff_barrier,
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
        body = module_body_for_text_grep(
            index,
            mod_name,
            module_body_cache=module_body_cache,
        )
        binds = collect_bind_records_for_module(index, mod_name)
        body_lookup = make_cell_module_body_lookup(
            index,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        if not body.strip():
            mci = ModuleConnectIndex()
            passthrough = _empty_module_passthrough_ports(
                index,
                mod_name,
                param_ctx,
                defines=defines,
            )
            if passthrough:
                apply_empty_module_passthrough(mci, passthrough[0], passthrough[1])
            if binds:
                apply_bind_connectivity(
                    mci,
                    binds,
                    index,
                    param_map=param_ctx,
                    defines=defines,
                    over_approximate_if=over_approximate_if,
                )
            built = TextGrepIndex.from_module_index(mci)
        else:
            built = build_text_grep_index(
                body,
                param_map=param_ctx,
                defines=defines,
                over_approximate_if=over_approximate_if,
                ff_barrier=ff_barrier,
                module_body_lookup=body_lookup,
                bind_records=binds,
                bind_index=index,
            )
        cache[key] = built
        if on_cache_miss is not None:
            on_cache_miss()
        return built


