"""Text-conn pair checks (grep pass, not propagation)."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.shared.endpoints import (
    DeclNetCache,
    ModuleBodyCache,
    _lca,
    _prune_rows_lca,
)
from hierwalk.connect.shared.modes import _has_port, _is_ancestor, _mode
from hierwalk.connect.shared.resolve_cache import (
    EndpointResolveCache,
    resolve_endpoint_cached,
)
from hierwalk.connect.text.dedup import (
    fanout_text_result,
    text_dedup_key,
)
from hierwalk.connect.text.index import TextGrepCache
from hierwalk.connect.text.walk import (
    TextWalkSessionCaches,
    _resolve_over_approximate_if,
    bidirectional_text_grep,
    forward_text_grep_to_scope,
    text_connect_note,
    text_walk_verdict_key,
)
from hierwalk.connect.logical.walk_log import build_walk_notes
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, ElabIndex, FlatRow


def connect_pair_text(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    effective_defines: Mapping[str, str],
    trace: bool,
    strict_generate: bool,
    ff_barrier: bool,
    over_approximate_if: Optional[bool],
    text_grep_cache: TextGrepCache,
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str,
    elab_index: Optional[ElabIndex],
    rows_by_path: Optional[Mapping[str, FlatRow]],
    endpoint_cache: Optional[EndpointResolveCache] = None,
    endpoint_cache_lock: Optional[threading.Lock] = None,
    walk_caches: Optional[TextWalkSessionCaches] = None,
    decl_net_cache: Optional[DeclNetCache] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
    walk_cache_lock: Optional[threading.Lock] = None,
) -> ConnectResult:
    """
    Text-conn: name grep only — not whether a value actually propagates.

    ``assign a = b * 0`` passes text-conn because *b* appears on the RHS.
    Logical-conn applies constant-fold / tie-off masks and reports disconnect.
    """
    lookup = (
        rows_by_path
        if rows_by_path is not None
        else (elab_index.rows_by_path if elab_index is not None else None)
    )
    ep_a, err_a = resolve_endpoint_cached(
        endpoint_a,
        rows,
        index,
        top=top,
        rows_by_path=lookup,
        cache=endpoint_cache,
        cache_lock=endpoint_cache_lock,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
    )
    ep_b, err_b = resolve_endpoint_cached(
        endpoint_b,
        rows,
        index,
        top=top,
        rows_by_path=lookup,
        cache=endpoint_cache,
        cache_lock=endpoint_cache_lock,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
    )
    errors = list(err_a) + list(err_b)

    if errors:
        mode = _mode(ep_a, ep_b) if ep_a.module and ep_b.module else "unknown"
        return ConnectResult(
            ep_a,
            ep_b,
            False,
            mode,
            errors=errors,
            check_id=check_id,
        )

    if _has_port(ep_a) and not ep_a.port_found:
        return ConnectResult(
            ep_a, ep_b, False, _mode(ep_a, ep_b), errors=errors, check_id=check_id
        )
    if _has_port(ep_b) and not ep_b.port_found:
        return ConnectResult(
            ep_a, ep_b, False, _mode(ep_a, ep_b), errors=errors, check_id=check_id
        )

    pruned = _prune_rows_lca(rows, ep_a.inst_path, ep_b.inst_path)
    mode = _mode(ep_a, ep_b)
    over_approx = _resolve_over_approximate_if(strict_generate, over_approximate_if)
    lca = _lca(ep_a.inst_path, ep_b.inst_path)
    lookup_map = lookup or {}

    def _lookup_walk_verdict(
        start: Tuple[str, str],
        goal: Tuple[str, str],
    ) -> Optional[Tuple[bool, int, object]]:
        if trace or walk_caches is None:
            return None
        vkey = text_walk_verdict_key(
            mode,
            lca=lca,
            start=start,
            goal=goal,
            rows_by_path=lookup_map,
            index=index,
            top=top,
            grep_cache=text_grep_cache,
            walk_caches=walk_caches,
            defines=effective_defines,
            over_approximate_if=over_approx,
            ff_barrier=ff_barrier,
            param_ctx_cache=param_ctx_cache,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        if vkey is None:
            return None
        if walk_cache_lock is not None:
            with walk_cache_lock:
                hit = walk_caches.walk_verdict_cache.get(vkey)
                if hit is None:
                    return None
                walk_caches.walk_verdict_hits += 1
                return hit
        hit = walk_caches.walk_verdict_cache.get(vkey)
        if hit is None:
            return None
        walk_caches.walk_verdict_hits += 1
        return hit

    def _store_walk_verdict(
        start: Tuple[str, str],
        goal: Tuple[str, str],
        ok: bool,
        mod_n: int,
        diag: object,
    ) -> None:
        if trace or walk_caches is None:
            return
        vkey = text_walk_verdict_key(
            mode,
            lca=lca,
            start=start,
            goal=goal,
            rows_by_path=lookup_map,
            index=index,
            top=top,
            grep_cache=text_grep_cache,
            walk_caches=walk_caches,
            defines=effective_defines,
            over_approximate_if=over_approx,
            ff_barrier=ff_barrier,
            param_ctx_cache=param_ctx_cache,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        if vkey is None:
            return
        stored = (ok, mod_n, diag)
        if walk_cache_lock is not None:
            with walk_cache_lock:
                walk_caches.walk_verdict_cache.setdefault(vkey, stored)
        else:
            walk_caches.walk_verdict_cache.setdefault(vkey, stored)

    if mode == "port-port":
        start = (ep_a.inst_path, ep_a.port_name or "")
        goal = (ep_b.inst_path, ep_b.port_name or "")
        cached = _lookup_walk_verdict(start, goal)
        if cached is not None:
            ok, mod_n, diag = cached
            walk_notes: List[str] = []
            if not ok and diag is not None:
                walk_notes = build_walk_notes(
                    diag,
                    rows_by_path=lookup_map,
                    start=start,
                    goal=goal,
                )
            return ConnectResult(
                ep_a,
                ep_b,
                ok,
                mode,
                hops=[],
                errors=errors,
                note=text_connect_note(ok, mod_n),
                check_id=check_id,
                walk_notes=walk_notes,
                coi_walk=diag,
            )
        ok, hops, mod_n, diag = bidirectional_text_grep(
            start,
            goal,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            grep_cache=text_grep_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
            walk_caches=walk_caches,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        _store_walk_verdict(start, goal, ok, mod_n, diag)
        walk_notes: List[str] = []
        if not ok and diag is not None:
            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup_map,
                start=start,
                goal=goal,
            )
        return ConnectResult(
            ep_a,
            ep_b,
            ok,
            mode,
            hops=hops,
            errors=errors,
            note=text_connect_note(ok, mod_n),
            check_id=check_id,
            walk_notes=walk_notes,
            coi_walk=diag,
        )

    if mode == "port-hierarchy":
        port_ep = ep_a if _has_port(ep_a) else ep_b
        hier_ep = ep_b if _has_port(ep_a) else ep_a
        start = (port_ep.inst_path, port_ep.port_name or "")
        goal = (hier_ep.inst_path, "")
        cached = _lookup_walk_verdict(start, goal)
        if cached is not None:
            ok, mod_n, diag = cached
            walk_notes: List[str] = []
            if not ok and diag is not None:
                walk_notes = build_walk_notes(
                    diag,
                    rows_by_path=lookup_map,
                    start=start,
                    goal=goal,
                )
            return ConnectResult(
                ep_a,
                ep_b,
                ok,
                mode,
                hops=[],
                errors=errors,
                note=text_connect_note(ok, mod_n, hier=True),
                check_id=check_id,
                walk_notes=walk_notes,
                coi_walk=diag,
            )
        ok, hops, mod_n, diag = forward_text_grep_to_scope(
            start,
            hier_ep.inst_path,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            grep_cache=text_grep_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
            walk_caches=walk_caches,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        _store_walk_verdict(start, goal, ok, mod_n, diag)
        walk_notes: List[str] = []
        if not ok and diag is not None:
            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup_map,
                start=start,
                goal=goal,
            )
        return ConnectResult(
            ep_a,
            ep_b,
            ok,
            mode,
            hops=hops,
            errors=errors,
            note=text_connect_note(ok, mod_n, hier=True),
            check_id=check_id,
            walk_notes=walk_notes,
            coi_walk=diag,
        )

    return ConnectResult(
        ep_a,
        ep_b,
        ep_a.inst_path == ep_b.inst_path
        or _is_ancestor(ep_a.inst_path, ep_b.inst_path)
        or _is_ancestor(ep_b.inst_path, ep_a.inst_path),
        "hierarchy-hierarchy",
        errors=errors,
        note="same or ancestor/descendant (no port trace)",
        check_id=check_id,
    )


_connect_pair_text = connect_pair_text


def connect_pair_text_deduped(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    effective_defines: Mapping[str, str],
    trace: bool,
    strict_generate: bool,
    ff_barrier: bool,
    over_approximate_if: Optional[bool],
    text_grep_cache: TextGrepCache,
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str,
    elab_index: Optional[ElabIndex],
    rows_by_path: Mapping[str, FlatRow],
    dedup_cache: Dict[Tuple[Any, ...], ConnectResult],
    dedup_stats: List[int],
    dedup_lock: Optional[threading.Lock] = None,
    endpoint_cache: Optional[EndpointResolveCache] = None,
    endpoint_cache_lock: Optional[threading.Lock] = None,
    walk_caches: Optional[TextWalkSessionCaches] = None,
    decl_net_cache: Optional[DeclNetCache] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> ConnectResult:
    lookup = rows_by_path
    ep_a, err_a = resolve_endpoint_cached(
        endpoint_a,
        rows,
        index,
        top=top,
        rows_by_path=lookup,
        cache=endpoint_cache,
        cache_lock=endpoint_cache_lock,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
    )
    ep_b, err_b = resolve_endpoint_cached(
        endpoint_b,
        rows,
        index,
        top=top,
        rows_by_path=lookup,
        cache=endpoint_cache,
        cache_lock=endpoint_cache_lock,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
    )
    errors = list(err_a) + list(err_b)
    key = text_dedup_key(ep_a, ep_b, errors)
    dedup_stats[0] += 1

    if dedup_lock is not None:
        with dedup_lock:
            hit = dedup_cache.get(key)
    else:
        hit = dedup_cache.get(key)
    if hit is not None:
        return fanout_text_result(
            hit,
            spec_a=endpoint_a,
            spec_b=endpoint_b,
            ep_a=ep_a,
            ep_b=ep_b,
            check_id=check_id,
        )

    result = connect_pair_text(
        endpoint_a,
        endpoint_b,
        rows=rows,
        index=index,
        top=top,
        effective_defines=effective_defines,
        trace=trace,
        strict_generate=strict_generate,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        text_grep_cache=text_grep_cache,
        param_ctx_cache=param_ctx_cache,
        check_id=check_id,
        elab_index=elab_index,
        rows_by_path=rows_by_path,
        endpoint_cache=endpoint_cache,
        endpoint_cache_lock=endpoint_cache_lock,
        walk_caches=walk_caches,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
        walk_cache_lock=dedup_lock,
    )

    if dedup_lock is not None:
        with dedup_lock:
            existing = dedup_cache.get(key)
            if existing is not None:
                return fanout_text_result(
                    existing,
                    spec_a=endpoint_a,
                    spec_b=endpoint_b,
                    ep_a=ep_a,
                    ep_b=ep_b,
                    check_id=check_id,
                )
            dedup_cache[key] = result
            dedup_stats[1] += 1
    else:
        dedup_cache[key] = result
        dedup_stats[1] += 1
    return result


_connect_pair_text_deduped = connect_pair_text_deduped