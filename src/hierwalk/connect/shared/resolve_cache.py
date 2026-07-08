"""Cached endpoint resolution (text + logical)."""

from __future__ import annotations

import threading
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.shared.endpoints import (
    DeclNetCache,
    ModuleBodyCache,
    resolve_endpoint,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, FlatRow

EndpointResolveCache = Dict[str, Tuple[ConnectEndpoint, Tuple[str, ...]]]


def _copy_connect_endpoint(ep: ConnectEndpoint) -> ConnectEndpoint:
    return ConnectEndpoint(
        spec=ep.spec,
        inst_path=ep.inst_path,
        port_name=ep.port_name,
        module=ep.module,
        port_found=ep.port_found,
    )


def resolve_endpoint_cached(
    spec: str,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    *,
    top: str,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    cache: Optional[EndpointResolveCache] = None,
    cache_lock: Optional[threading.Lock] = None,
    decl_net_cache: Optional[DeclNetCache] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> Tuple[ConnectEndpoint, List[str]]:
    """Resolve one endpoint spec; reuse prior result when *cache* is shared."""
    text = (spec or "").strip()
    if cache is not None and text:
        if cache_lock is not None:
            with cache_lock:
                hit = cache.get(text)
        else:
            hit = cache.get(text)
        if hit is not None:
            ep, errs = hit
            return _copy_connect_endpoint(ep), list(errs)
    ep, errs = resolve_endpoint(
        spec,
        rows,
        index,
        top=top,
        require_port=False,
        rows_by_path=rows_by_path,
        decl_net_cache=decl_net_cache,
        module_body_cache=module_body_cache,
        sources=sources,
    )
    if cache is not None and text:
        stored = (ep, tuple(errs))
        if cache_lock is not None:
            with cache_lock:
                cache.setdefault(text, stored)
        else:
            cache.setdefault(text, stored)
    return ep, errs


_resolve_endpoint_cached = resolve_endpoint_cached