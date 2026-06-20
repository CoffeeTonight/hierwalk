"""Hierarchy elaboration via module-index dict lookup."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.index import DesignIndex
from hierwalk.lazy_scope import child_path_in_scope
from hierwalk.models import ElabNode, FlatRow
from hierwalk.params import resolve_param_map


def _resolve_elab_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def elaborate(
    index: DesignIndex,
    top: str,
    *,
    max_depth: Optional[int] = None,
    scope_paths: Optional[Set[str]] = None,
) -> tuple[ElabNode, List[FlatRow]]:
    if top not in index.modules:
        raise ValueError(f"Top module not found: {top}")

    rows: List[FlatRow] = []
    seen_paths: Set[str] = set()

    def add_row(
        mod: str,
        path: str,
        depth: int,
        parent: Optional[str],
        *,
        inst_leaf: str,
        file_path: str,
        stop_reason: str,
        via_filelist: str = "",
        filelist_chain: str = "",
        param_ctx: Optional[Mapping[str, str]] = None,
    ) -> None:
        if path in seen_paths:
            return
        seen_paths.add(path)
        rows.append(
            FlatRow(
                full_path=path,
                inst_leaf=inst_leaf,
                module=mod,
                depth=depth,
                parent_path=parent,
                file=file_path,
                stop_reason=stop_reason,
                via_filelist=via_filelist,
                filelist_chain=filelist_chain,
                param_ctx=dict(param_ctx or {}),
            )
        )

    def stitch(
        mod_name: str,
        inst_leaf: str,
        full_path: str,
        depth: int,
        parent_path: Optional[str],
        parent_ctx: Mapping[str, str],
        overrides: Mapping[str, str],
    ) -> ElabNode:
        rec = index.get_module(mod_name)
        stop = index.module_stop_reason(mod_name)
        pmap = resolve_param_map(
            rec.raw_params if rec else {},
            overrides=overrides,
            parent=parent_ctx,
        )
        node = ElabNode(
            inst_name=inst_leaf,
            module=mod_name,
            full_path=full_path,
            file_path=rec.file_path if rec else "",
            param_ctx=dict(pmap),
            stop_reason=stop,
            children=[],
        )
        add_row(
            mod_name,
            full_path,
            depth,
            parent_path,
            inst_leaf=inst_leaf,
            file_path=node.file_path,
            stop_reason=stop,
            via_filelist=index.filelist_for(node.file_path),
            filelist_chain=index.filelist_chain_for(node.file_path),
            param_ctx=pmap,
        )
        if stop:
            return node
        if max_depth is not None and depth >= max_depth:
            return node

        edges = index.instances_for(mod_name, parent_ctx, overrides)
        for edge in edges:
            child_path = f"{full_path}.{edge.inst_name}"
            if child_path in seen_paths:
                continue
            if not child_path_in_scope(child_path, scope_paths):
                continue
            child = stitch(
                edge.child_module,
                edge.inst_name,
                child_path,
                depth + 1,
                full_path,
                pmap,
                edge.param_overrides,
            )
            node.children.append(child)
        return node

    root = stitch(top, top, top, 0, None, {}, {})
    return root, rows


def flatten(
    index: DesignIndex,
    top: str,
    *,
    max_depth: Optional[int] = None,
    scope_paths: Optional[Set[str]] = None,
) -> List[FlatRow]:
    _, rows = elaborate(
        index,
        top,
        max_depth=max_depth,
        scope_paths=scope_paths,
    )
    return rows


def elaborate_tops_parallel(
    index: DesignIndex,
    tops: Sequence[str],
    *,
    max_depth: Optional[int] = None,
    scope_paths: Optional[Set[str]] = None,
    jobs: int = 0,
    get_cached: Optional[
        Callable[[str], Optional[Tuple[ElabNode, List[FlatRow]]]]
    ] = None,
    store_cached: Optional[
        Callable[[str, ElabNode, List[FlatRow]], None]
    ] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Tuple[List[ElabNode], List[FlatRow], int]:
    """Elaborate one or more tops; optional cache hooks; parallel when ``jobs`` > 1."""
    tops_list = [t for t in tops if t]
    if not tops_list:
        return [], [], 0

    workers = _resolve_elab_jobs(jobs, len(tops_list))
    t0 = time.perf_counter()
    cache_hits = 0

    def _elab_one(top_name: str) -> Tuple[str, Tuple[ElabNode, List[FlatRow]], bool]:
        if get_cached is not None:
            cached = get_cached(top_name)
            if cached is not None:
                return top_name, cached, True
        if on_progress:
            on_progress(f"elab: elaborating top {top_name}")
        root, part = elaborate(
            index,
            top_name,
            max_depth=max_depth,
            scope_paths=scope_paths,
        )
        if store_cached is not None:
            store_cached(top_name, root, part)
        return top_name, (root, part), False

    ordered: List[Tuple[str, Tuple[ElabNode, List[FlatRow]]]] = []
    if workers == 1 or len(tops_list) == 1:
        for top_name in tops_list:
            name, data, hit = _elab_one(top_name)
            if hit:
                cache_hits += 1
            ordered.append((name, data))
    else:
        results: dict[str, Tuple[ElabNode, List[FlatRow]]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, data, hit in pool.map(_elab_one, tops_list):
                results[name] = data
                if hit:
                    cache_hits += 1
        ordered = [(top_name, results[top_name]) for top_name in tops_list]

    roots = [data[0] for _, data in ordered]
    rows: List[FlatRow] = []
    for _, data in ordered:
        rows.extend(data[1])

    if on_progress and tops_list:
        elapsed = time.perf_counter() - t0
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"elab: done {len(tops_list)} top(s) in {elapsed:.1f}s "
            f"({cache_hits} cache hits, {workers} workers, jobs={jobs_note})"
        )
    return roots, rows, cache_hits