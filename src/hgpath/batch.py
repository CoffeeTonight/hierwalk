"""Batch resolve checks with prefix clustering."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.shared.expand import hierarchy_endpoint_specs
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


def _endpoint_specs(raw: object) -> Tuple[str, ...]:
    """
    Expand list/concat display into real hierarchy paths.

    JSON list endpoints are stored as display strings like
    ``[xa.b.c, xa.d.e.r]``. Passing that blob through ``coarse_hierarchy_path``
    splits on ``.`` and ``inst_base_name('[xa')`` becomes empty — logs then show
    ``.b.c, xa.d.e.r]`` (top stripped from the first element only).
    """
    return hierarchy_endpoint_specs(str(raw or ""))


def _collect_specs(checks: Sequence[ConnectivityCheck]) -> List[str]:
    specs: List[str] = []
    for chk in checks:
        specs.extend(_endpoint_specs(chk.endpoint_a))
        specs.extend(_endpoint_specs(chk.endpoint_b))
    return [s for s in specs if s.strip()]


def _coarse_key(spec: str, *, top: str) -> str:
    return normalize_spec(spec, top=top).coarse


def _pick_side_entry(entries: Sequence[TreeEntry]) -> TreeEntry:
    """Prefer first miss for reporting; else first entry."""
    if not entries:
        raise ValueError("empty endpoint entries")
    for ent in entries:
        if not ent.ok:
            return ent
    return entries[0]


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
    norm_keys = [_coarse_key(s, top=top) for s in specs]
    unique = sorted(set(norm_keys))
    prefix = common_prefix_segments(unique)
    if on_log:
        on_log(
            f"cluster checks={len(checks)} specs={len(specs)} "
            f"unique_paths={len(unique)} common_prefix={'.'.join(prefix) or '-'} "
            f"simple_exist={simple_exist}"
        )

    # Map coarse key → original expanded path (for resolve).
    key_to_spec: Dict[str, str] = {}
    for s in specs:
        key_to_spec[_coarse_key(s, top=top)] = s

    spec_entries: Dict[str, TreeEntry] = {}
    ordered = sorted(unique, key=lambda k: (len(k.split(".")), k))
    for key in ordered:
        spec = key_to_spec.get(key) or key
        # Never resolve a list-display blob; key is already expanded coarse path.
        if "[" in spec or "]" in spec:
            if on_log:
                on_log(f"resolve skip invalid-list-blob spec={spec!r}")
            continue
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
        specs_a = _endpoint_specs(chk.endpoint_a)
        specs_b = _endpoint_specs(chk.endpoint_b)
        if not specs_a or not specs_b:
            if on_log:
                on_log(
                    f"check-done id={chk.check_id or '-'} status=fail "
                    f"reason=empty-endpoint elapsed_ms=0.0"
                )
            continue

        entries_a: List[TreeEntry] = []
        for s in specs_a:
            ka = _coarse_key(s, top=top)
            ent = spec_entries.get(ka)
            if ent is None:
                ent = resolve_with_tree(
                    s,
                    top=top,
                    session=session,
                    tree=tree,
                    on_log=on_log,
                    simple_exist=simple_exist,
                )
                spec_entries[ka] = ent
            entries_a.append(ent)
            if on_log and len(specs_a) > 1:
                on_log(
                    f"list-ep a[{len(entries_a)-1}] spec={s!r} "
                    f"coarse={ka!r} ok={ent.ok}"
                )

        entries_b: List[TreeEntry] = []
        for s in specs_b:
            kb = _coarse_key(s, top=top)
            ent = spec_entries.get(kb)
            if ent is None:
                ent = resolve_with_tree(
                    s,
                    top=top,
                    session=session,
                    tree=tree,
                    on_log=on_log,
                    simple_exist=simple_exist,
                )
                spec_entries[kb] = ent
            entries_b.append(ent)
            if on_log and len(specs_b) > 1:
                on_log(
                    f"list-ep b[{len(entries_b)-1}] spec={s!r} "
                    f"coarse={kb!r} ok={ent.ok}"
                )

        ea = _pick_side_entry(entries_a)
        eb = _pick_side_entry(entries_b)
        ok = all(e.ok for e in entries_a) and all(e.ok for e in entries_b)
        if on_log:
            ms = (time.perf_counter() - t0) * 1000.0
            fail_a = [s for s, e in zip(specs_a, entries_a) if not e.ok]
            fail_b = [s for s, e in zip(specs_b, entries_b) if not e.ok]
            detail = ""
            if fail_a or fail_b:
                detail = f" fail_a={fail_a or '-'} fail_b={fail_b or '-'}"
            on_log(
                f"check-done id={chk.check_id or '-'} status={'pass' if ok else 'fail'} "
                f"inst_hops={max(len(ea.nodes), len(eb.nodes))} "
                f"files={len(set(ea.scoped_files) | set(eb.scoped_files))} "
                f"elapsed_ms={ms:.1f}{detail}"
            )
        check_results.append((chk, ea, eb))

    if on_log:
        on_log(f"tree-ready nodes={tree.node_count}")
    return BatchResult(entries=spec_entries, check_results=check_results)
