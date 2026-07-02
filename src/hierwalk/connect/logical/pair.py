"""Logical COI pair connectivity (bit-precise / constant-fold)."""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.logical.scan import ModuleConnectIndex
from hierwalk.connect.text.index import TextGrepCache
from hierwalk.connect.logical.search import (
    _bidirectional_coi,
    _connect_note,
    _forward_coi_to_scope,
)
from hierwalk.connect.logical.walk_log import build_walk_notes
from hierwalk.connect.shared.endpoints import _prune_rows_lca
from hierwalk.connect.shared.modes import _has_port, _is_ancestor, _mode
from hierwalk.connect.shared.resolve_cache import (
    EndpointResolveCache,
    resolve_endpoint_cached,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, ElabIndex, FlatRow


def connect_pair(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    effective_defines: Mapping[str, str],
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = True,
    over_approximate_if: Optional[bool] = None,
    mod_cache: Dict[Tuple[str, str, str, str, str, bool, bool, bool], ModuleConnectIndex],
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str = "",
    elab_index: Optional[ElabIndex] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    resolve_param_dims: bool = True,
    text_grep_cache: Optional[TextGrepCache] = None,
    endpoint_cache: Optional[EndpointResolveCache] = None,
    endpoint_cache_lock: Optional[object] = None,
) -> ConnectResult:
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
    )
    ep_b, err_b = resolve_endpoint_cached(
        endpoint_b,
        rows,
        index,
        top=top,
        rows_by_path=lookup,
        cache=endpoint_cache,
        cache_lock=endpoint_cache_lock,
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

    if mode == "port-port":
        start = (ep_a.inst_path, ep_a.port_name or "")
        goal = (ep_b.inst_path, ep_b.port_name or "")
        ok, hops, mod_n, diag = _bidirectional_coi(
            start,
            goal,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            ff_barrier=ff_barrier,
            over_approximate_if=over_approximate_if,
            mod_cache=mod_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
            resolve_param_dims=resolve_param_dims,
            text_grep_cache=text_grep_cache,
        )
        walk_notes: List[str] = []
        if not ok and diag is not None:
            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup or {},
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
            note=_connect_note(ok, mod_n),
            check_id=check_id,
            walk_notes=walk_notes,
            coi_walk=diag,
        )

    if mode == "port-hierarchy":
        port_ep = ep_a if _has_port(ep_a) else ep_b
        hier_ep = ep_b if _has_port(ep_a) else ep_a
        start = (port_ep.inst_path, port_ep.port_name or "")
        ok, hops, mod_n, diag = _forward_coi_to_scope(
            start,
            hier_ep.inst_path,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            ff_barrier=ff_barrier,
            over_approximate_if=over_approximate_if,
            mod_cache=mod_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
            resolve_param_dims=resolve_param_dims,
            text_grep_cache=text_grep_cache,
        )
        walk_notes: List[str] = []
        if not ok and diag is not None:
            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup or {},
                start=start,
                goal=(hier_ep.inst_path, ""),
            )
        return ConnectResult(
            ep_a,
            ep_b,
            ok,
            mode,
            hops=hops,
            errors=errors,
            note=_connect_note(ok, mod_n, hier=True),
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


_connect_pair = connect_pair