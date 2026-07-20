"""Batch resolve checks with prefix clustering."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.hierarchy_grep import HierarchyGrepSession

from hgpath.path_norm import common_prefix_segments, normalize_spec
from hgpath.tree_db import TreeDb, TreeEntry
from hgpath.walker import resolve_with_tree

LogFn = Optional[Callable[[str], None]]


@dataclass
class BatchResult:
    entries: Dict[str, TreeEntry]
    check_results: List[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]]


def _collect_specs(checks: Sequence[ConnectivityCheck]) -> List[str]:
    specs: List[str] = []
    for chk in checks:
        specs.append(str(chk.endpoint_a))
        specs.append(str(chk.endpoint_b))
    return [s for s in specs if s.strip()]


def run_batch(
    checks: Sequence[ConnectivityCheck],
    *,
    top: str,
    session: HierarchyGrepSession,
    tree: TreeDb,
    on_log: LogFn = None,
    simple_exist: bool = False,
) -> BatchResult:
    specs = _collect_specs(checks)
    norm_keys = [normalize_spec(s, top=top).coarse for s in specs]
    unique = sorted(set(norm_keys))
    prefix = common_prefix_segments(unique)
    if on_log:
        on_log(
            f"cluster checks={len(checks)} specs={len(specs)} "
            f"unique_paths={len(unique)} common_prefix={'.'.join(prefix) or '-'} "
            f"simple_exist={simple_exist}"
        )

    spec_entries: Dict[str, TreeEntry] = {}
    ordered = sorted(unique, key=lambda k: (len(k.split(".")), k))
    for key in ordered:
        spec = next(s for s in specs if normalize_spec(s, top=top).coarse == key)
        spec_entries[key] = resolve_with_tree(
            spec,
            top=top,
            session=session,
            tree=tree,
            on_log=on_log,
            simple_exist=simple_exist,
        )

    check_results: List[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]] = []
    for chk in checks:
        t0 = time.perf_counter()
        ka = normalize_spec(str(chk.endpoint_a), top=top).coarse
        kb = normalize_spec(str(chk.endpoint_b), top=top).coarse
        ea = spec_entries.get(ka) or resolve_with_tree(
            str(chk.endpoint_a),
            top=top,
            session=session,
            tree=tree,
            on_log=on_log,
            simple_exist=simple_exist,
        )
        eb = spec_entries.get(kb) or resolve_with_tree(
            str(chk.endpoint_b),
            top=top,
            session=session,
            tree=tree,
            on_log=on_log,
            simple_exist=simple_exist,
        )
        spec_entries[ka] = ea
        spec_entries[kb] = eb
        ok = ea.ok and eb.ok
        if on_log:
            ms = (time.perf_counter() - t0) * 1000.0
            on_log(
                f"check-done id={chk.check_id or '-'} status={'pass' if ok else 'fail'} "
                f"inst_hops={max(len(ea.nodes), len(eb.nodes))} "
                f"files={len(set(ea.scoped_files) | set(eb.scoped_files))} "
                f"elapsed_ms={ms:.1f}"
            )
        check_results.append((chk, ea, eb))

    if on_log:
        on_log(f"tree-ready nodes={tree.node_count}")
    return BatchResult(entries=spec_entries, check_results=check_results)