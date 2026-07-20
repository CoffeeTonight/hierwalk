"""Resolve specs with tree LPM cache + session body cache."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Optional

from hierwalk.hierarchy_grep import HierarchyGrepSession

from hgpath.path_norm import NormSpec, normalize_spec
from hgpath.simple_exist import is_simple_exist_spec, resolve_simple_exist
from hgpath.tree_db import TreeDb, TreeEntry

LogFn = Optional[Callable[[str], None]]


def resolve_with_tree(
    spec: str,
    *,
    top: str,
    session: HierarchyGrepSession,
    tree: TreeDb,
    on_log: LogFn = None,
    simple_exist: bool = False,
) -> TreeEntry:
    norm = normalize_spec(spec, top=top)
    key = norm.coarse

    full = tree.get_full(key)
    if full is not None and full.ok and full.nodes:
        if on_log:
            on_log(
                f"lpm spec={key!r} hit=full hops={len(full.nodes)} "
                f"suffix_hops=0"
            )
        return full

    prefix_ent, shared = tree.longest_prefix(key)
    if on_log:
        suffix = max(0, len(key.split(".")) - shared)
        hit = "full" if full and full.ok else ("prefix" if shared else "miss")
        on_log(
            f"lpm spec={key!r} hit={hit} shared_hops={shared} suffix_hops={suffix}"
        )

    t0 = time.perf_counter()
    if simple_exist and is_simple_exist_spec(spec):
        result = resolve_simple_exist(session, spec, top=top)
        mode = "simple"
    else:
        result = session.resolve(key, top=top)
        mode = "full"
    port_tail = ""
    nodes = list(result.get("nodes") or [])
    if nodes and nodes[-1].get("kind") in ("port", "signal") and norm.leaf_tail:
        port_tail = norm.leaf_tail

    entry = tree.insert_result(key, result, port_tail=port_tail)
    if on_log:
        ms = (time.perf_counter() - t0) * 1000.0
        leaf = ""
        if entry.nodes:
            last = entry.nodes[-1]
            if last.kind:
                leaf = f"leaf={last.kind}:{last.segment}"
            elif last.role == "inst":
                leaf = "leaf=inst"
        preprocess = "comments-only" if mode == "simple" else "full"
        on_log(
            f"resolve spec={key!r} ok={entry.ok} mode={mode} preprocess={preprocess} "
            f"inst_hops={norm.inst_hop_count} {leaf} "
            f"files={len(entry.scoped_files)} elapsed_ms={ms:.1f}"
        )
    return entry