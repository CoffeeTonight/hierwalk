"""Lazy processing policy: defer heavy work until connect/elab needs it."""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Sequence, Set

from hierwalk.connect_expand import endpoint_specs_from_expand
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return False
    return default


def lazy_processing_enabled() -> bool:
    """Master switch (default on). ``HIERWALK_LAZY=0`` restores eager index/elab."""
    return _env_bool("HIERWALK_LAZY", default=True)


def lazy_index_ifdef() -> bool:
    """
    Apply ``ifdef`` during index preprocess.

    Default is **on** (inactive branches stripped at index). Set
    ``HIERWALK_LAZY_IFDEF=0`` to defer ifdef until connect/elab.
    """
    if not lazy_processing_enabled():
        return True
    return _env_bool("HIERWALK_LAZY_IFDEF", default=True)


def lazy_filelist_defer_exists() -> bool:
    """Skip ``exists()`` while expanding filelists (lazy default on)."""
    return lazy_processing_enabled()


def lazy_scoped_connect_elab() -> bool:
    """Elaborate only endpoint prefix paths for connect/check (lazy default on)."""
    return lazy_processing_enabled()


def lazy_on_demand_full_preprocess() -> bool:
    """Upgrade to full preprocess (macro/bind/ifdef) when generate-fold elab needs it."""
    return lazy_processing_enabled()


def endpoint_specs_from_checks(checks: Sequence[ConnectivityCheck]) -> List[str]:
    out: List[str] = []
    for chk in checks:
        if chk.expand is not None:
            out.extend(endpoint_specs_from_expand(chk.expand))
        else:
            if chk.endpoint_a:
                out.append(chk.endpoint_a)
            if chk.endpoint_b:
                out.append(chk.endpoint_b)
    return out


def endpoint_specs_from_request(
    request: Optional[ConnectivityRequest],
    *,
    pair: Optional[tuple[str, str]] = None,
) -> List[str]:
    specs: List[str] = []
    if request is not None:
        specs.extend(endpoint_specs_from_checks(request.checks))
    if pair is not None:
        a, b = pair
        if a:
            specs.append(a)
        if b:
            specs.append(b)
    return specs


def hierarchy_prefixes(specs: Iterable[str]) -> Set[str]:
    """All dotted prefixes for endpoint specs (hierarchy path candidates)."""
    out: Set[str] = set()
    for raw in specs:
        spec = str(raw).strip()
        if not spec:
            continue
        parts = spec.split(".")
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def elab_scope_paths(
    endpoint_specs: Iterable[str],
    *,
    top: str = "",
) -> Set[str]:
    """
    Instance paths to elaborate: prefixes of every endpoint spec.

    When *top* is set, ensure it is included.
    """
    scope = hierarchy_prefixes(endpoint_specs)
    if top:
        scope.add(top)
        filtered = {p for p in scope if p == top or p.startswith(top + ".")}
        if filtered:
            scope = filtered
    return scope


def child_path_in_scope(child_path: str, scope_paths: Optional[Set[str]]) -> bool:
    if not scope_paths:
        return True
    for sp in scope_paths:
        if sp == child_path or sp.startswith(child_path + "."):
            return True
    return False