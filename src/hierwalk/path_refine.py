"""Path-scoped parameter refinement for port search hits."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.generate_fold import prepare_body_for_instance_scan
from hierwalk.index import DesignIndex, _ctx_key
from hierwalk.inst_scan import iter_module_blocks, scan_hierarchy_instances
from hierwalk.manifest import path_content_digest
from hierwalk.models import InstanceEdge
from hierwalk.params import collect_module_params, parse_param_pairs, resolve_param_map, split_module_header

_PARAM_DECL_RE = re.compile(
    r"\b(?:parameter|localparam)\b\s+(?:\w+\s+)?[^;]+;",
    re.IGNORECASE,
)


@dataclass
class PathRefineStep:
    inst_leaf: str
    module: str
    child_module: str
    file: str
    param_overrides: Dict[str, str] = field(default_factory=dict)
    scoped_params: Dict[str, str] = field(default_factory=dict)
    resolved_ctx: Dict[str, str] = field(default_factory=dict)


@dataclass
class PathRefineResult:
    full_path: str
    ok: bool
    param_ctx: Dict[str, str]
    steps: List[PathRefineStep] = field(default_factory=list)
    note: str = ""


_ModuleChunkCacheKey = Tuple[int, str, str]
_module_chunk_cache: Dict[_ModuleChunkCacheKey, Tuple[str, str, str]] = {}


def _module_chunk_cache_key(index: DesignIndex, mod_name: str, path: str) -> _ModuleChunkCacheKey:
    digest = path_content_digest(Path(path)) if path else None
    token = digest if digest is not None else path
    return (id(index), mod_name, token)


def clear_module_chunk_cache() -> None:
    _module_chunk_cache.clear()


def _module_chunk(index: DesignIndex, mod_name: str) -> tuple[str, str, str]:
    rec = index.get_module(mod_name)
    if not rec:
        return "", "", ""
    path = rec.file_path or ""
    cache_key = _module_chunk_cache_key(index, mod_name, path)
    cached = _module_chunk_cache.get(cache_key)
    if cached is not None:
        return cached

    body = index.module_body(mod_name)
    if not path:
        out = ("", "", body)
        _module_chunk_cache[cache_key] = out
        return out
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        out = (path, "", body)
        _module_chunk_cache[cache_key] = out
        return out
    for block in iter_module_blocks(text):
        if block["name"] != mod_name:
            continue
        header, mod_body = split_module_header(block["chunk"])
        out = (path, header, mod_body or body)
        _module_chunk_cache[cache_key] = out
        return out
    out = (path, "", body)
    _module_chunk_cache[cache_key] = out
    return out


def _header_params(header: str) -> Dict[str, str]:
    return parse_param_pairs(header) if header else {}


def _params_in_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _PARAM_DECL_RE.finditer(text):
        out.update(parse_param_pairs(m.group(0)))
    return out


def _inst_matches_target(
    inst: str,
    dims: str,
    target: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> bool:
    """Match instance names the same way as :func:`scan_hierarchy_instances`."""
    from hierwalk.inst_scan import _read_hier_inst_path, expand_inst_names

    if not inst:
        return False
    pmap = dict(param_map or {})
    target_inst, _ = _read_hier_inst_path(target, 0)
    want = (target_inst or target).lower()
    want_leaf = want.rsplit(".", 1)[-1]
    inst_path, _ = _read_hier_inst_path(inst, 0)
    if inst_path:
        if inst_path.lower() == want:
            return True
        if inst_path.rsplit(".", 1)[-1].lower() == want_leaf:
            return True
    if inst.lower() == want:
        return True
    if inst.rsplit(".", 1)[-1].lower() == want_leaf:
        return True
    for leaf in expand_inst_names(inst, dims, pmap):
        if leaf.lower() == want:
            return True
        if leaf.rsplit(".", 1)[-1].lower() == want_leaf:
            return True
    return False


def _edge_matches_inst_leaf(
    edge: InstanceEdge,
    inst_leaf: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> bool:
    """Match a scanned edge to *inst_leaf* (incl. array base names like ``u_core``)."""
    if _inst_matches_target(edge.inst_name, "", inst_leaf, param_map=param_map):
        return True
    base = edge.inst_name.split("[", 1)[0]
    if base == edge.inst_name:
        return False
    return _inst_matches_target(base, "", inst_leaf, param_map=param_map)


def _body_prefix_before_instance(
    body: str,
    inst_leaf: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """Return ``(body text before inst_leaf, params visible at that site)``."""
    from hierwalk.inst_scan import (
        _ATTR_RE,
        _BIND_LINE_RE,
        _KEYWORDS,
        _read_hier_inst_path,
        _read_ident,
        _skip_balanced,
        _skip_sv_attributes,
        slim_body_for_instance_scan,
    )

    from hierwalk.preprocess import strip_comments_for_instance_scan

    body = slim_body_for_instance_scan(strip_comments_for_instance_scan(body))
    clean = _ATTR_RE.sub(" ", body)
    clean = _BIND_LINE_RE.sub("", clean)
    n = len(clean)
    i = 0
    target = inst_leaf
    base_params = dict(param_map or {})
    accumulated_params = dict(base_params)
    param_scan_pos = 0

    def consume_hash(start: int) -> int:
        pos = start
        if pos >= n or clean[pos] != "#":
            return pos
        pos += 1
        while pos < n and clean[pos].isspace():
            pos += 1
        if pos >= n or clean[pos] != "(":
            return start
        return _skip_balanced(clean, pos, "(", ")")

    while i < n:
        decl_start = i
        while i < n and clean[i].isspace():
            i += 1
        if i >= n:
            break
        cell, j = _read_ident(clean, i)
        if not cell:
            i += 1
            continue
        if cell.lower() in _KEYWORDS:
            i = j
            continue
        k = j
        while k < n and clean[k].isspace():
            k += 1
        k = _skip_sv_attributes(clean, k)
        inst = ""
        if k < n and clean[k] == "#":
            k = consume_hash(k)
            while k < n and clean[k].isspace():
                k += 1
            k = _skip_sv_attributes(clean, k)
            inst, k = _read_hier_inst_path(clean, k)
        else:
            inst, k = _read_hier_inst_path(clean, k)
            if inst:
                while k < n and clean[k].isspace():
                    k += 1
                if k < n and clean[k] == "#":
                    k = consume_hash(k)
        if not inst:
            i += 1
            continue
        while k < n and clean[k].isspace():
            k += 1
        dims = ""
        while k < n and clean[k] == "[":
            end = _skip_balanced(clean, k, "[", "]")
            dims += clean[k:end]
            k = end
            while k < n and clean[k].isspace():
                k += 1
        if k >= n or clean[k] not in "(;":
            i += 1
            continue
        if decl_start > param_scan_pos:
            accumulated_params.update(_params_in_text(clean[param_scan_pos:decl_start]))
            param_scan_pos = decl_start
        site_params = dict(accumulated_params)
        if _inst_matches_target(inst, dims, target, param_map=site_params):
            return clean[:decl_start], site_params
        if clean[k] == "(":
            k = _skip_balanced(clean, k, "(", ")")
        while k < n and clean[k].isspace():
            k += 1
        if k < n and clean[k] == ",":
            k += 1
            while k < n and clean[k].isspace():
                k += 1
            inst2, k2 = _read_hier_inst_path(clean, k)
            if inst2:
                k = k2
                while k < n and clean[k].isspace():
                    k += 1
                dims2 = ""
                while k < n and clean[k] == "[":
                    end = _skip_balanced(clean, k, "[", "]")
                    dims2 += clean[k:end]
                    k = end
                    while k < n and clean[k].isspace():
                        k += 1
                if k < n and clean[k] in "(;":
                    if _inst_matches_target(inst2, dims2, target, param_map=site_params):
                        return clean[:decl_start], site_params
                    if clean[k] == "(":
                        k = _skip_balanced(clean, k, "(", ")")
            i = k2
            continue
        i = k
    return clean, accumulated_params


_InstanceScanCache = Dict[Tuple[str, str], List[InstanceEdge]]


def _instance_edges_for_parent(
    index: DesignIndex,
    parent_mod: str,
    parent_ctx: Mapping[str, str],
    *,
    scoped_params: Optional[Mapping[str, str]] = None,
    scan_cache: Optional[_InstanceScanCache] = None,
) -> List[InstanceEdge]:
    rec = index.get_module(parent_mod)
    if rec is None:
        return []
    if rec.stop_reason or rec.is_blackbox:
        return list(rec.instances)

    if scoped_params is not None:
        raw = dict(scoped_params)
    else:
        raw = dict(rec.raw_params)
    pmap = resolve_param_map(raw, parent=parent_ctx)
    cache_key = (parent_mod, _ctx_key(pmap))
    if scan_cache is not None:
        cached = scan_cache.get(cache_key)
        if cached is not None:
            return cached

    body = index.module_body(parent_mod)
    if not body:
        if rec.instances:
            edges = list(rec.instances)
            if scan_cache is not None:
                scan_cache[cache_key] = edges
            return edges
        return []

    folded = prepare_body_for_instance_scan(body, pmap)
    edges = scan_hierarchy_instances(folded, param_map=pmap)
    if scan_cache is not None:
        scan_cache[cache_key] = edges
    return edges


def scoped_module_params(
    index: DesignIndex,
    mod_name: str,
    inst_leaf: str,
) -> Dict[str, str]:
    """Header params plus body localparams declared before ``inst_leaf``."""
    _path, header, body = _module_chunk(index, mod_name)
    params = dict(_header_params(header))
    if body:
        _prefix, body_params = _body_prefix_before_instance(
            body,
            inst_leaf,
            param_map=params,
        )
        params.update(body_params)
        return params
    rec = index.get_module(mod_name)
    return dict(rec.raw_params) if rec else params


def find_child_instance(
    index: DesignIndex,
    parent_mod: str,
    inst_leaf: str,
    parent_ctx: Mapping[str, str],
    *,
    scoped_params: Optional[Mapping[str, str]] = None,
    scan_cache: Optional[_InstanceScanCache] = None,
) -> Optional[InstanceEdge]:
    edges = _instance_edges_for_parent(
        index,
        parent_mod,
        parent_ctx,
        scoped_params=scoped_params,
        scan_cache=scan_cache,
    )
    if not edges:
        return None
    if scoped_params is not None:
        raw = dict(scoped_params)
    else:
        rec = index.get_module(parent_mod)
        raw = dict(rec.raw_params) if rec else {}
    pmap = resolve_param_map(raw, parent=parent_ctx)
    for edge in edges:
        if _edge_matches_inst_leaf(edge, inst_leaf, param_map=pmap):
            return edge
    return None


def refine_param_ctx_for_path(
    index: DesignIndex,
    top: str,
    full_path: str,
) -> PathRefineResult:
    """
    Walk one hierarchy path and fold parameters with instance-scoped localparams.

    Used after a hierarchy hit to refine port dimension evaluation without
    re-indexing the full design.
    """
    parts = full_path.split(".")
    if not parts or parts[0] != top:
        return PathRefineResult(full_path, False, {}, note="path does not start at top")

    top_rec = index.get_module(top)
    if not top_rec:
        return PathRefineResult(full_path, False, {}, note=f"top not found: {top}")

    module_ctx: Dict[str, str] = {}
    steps: List[PathRefineStep] = []
    current_mod = top
    scan_cache: _InstanceScanCache = {}

    steps.append(
        PathRefineStep(
            inst_leaf=parts[0],
            module=top,
            child_module=top,
            file=top_rec.file_path,
            scoped_params=dict(_header_params(_module_chunk(index, top)[1])),
            resolved_ctx={},
        )
    )

    for inst_leaf in parts[1:]:
        scoped = scoped_module_params(index, current_mod, inst_leaf)
        site_ctx = resolve_param_map(scoped, parent=module_ctx)
        edge = find_child_instance(
            index,
            current_mod,
            inst_leaf,
            module_ctx,
            scoped_params=scoped,
            scan_cache=scan_cache,
        )
        if edge is None:
            return PathRefineResult(
                full_path,
                False,
                dict(module_ctx),
                steps=steps,
                note=f"instance not found: {current_mod}.{inst_leaf}",
            )
        child_rec = index.get_module(edge.child_module)
        _path, header, body = _module_chunk(index, edge.child_module)
        child_params = collect_module_params(header, body) if body or header else {}
        if not child_params and child_rec:
            child_params = dict(child_rec.raw_params)
        module_ctx = resolve_param_map(
            child_params,
            overrides=edge.param_overrides,
            parent=site_ctx,
        )
        steps.append(
            PathRefineStep(
                inst_leaf=inst_leaf,
                module=current_mod,
                child_module=edge.child_module,
                file=child_rec.file_path if child_rec else "",
                param_overrides=dict(edge.param_overrides),
                scoped_params=dict(scoped),
                resolved_ctx=dict(module_ctx),
            )
        )
        current_mod = edge.child_module

    return PathRefineResult(
        full_path,
        True,
        dict(module_ctx),
        steps=steps,
        note="path-refined",
    )


def refine_rows_param_ctx(
    index: DesignIndex,
    top: str,
    rows: Sequence,
) -> Dict[str, Dict[str, str]]:
    """Refine param ctx for each unique hierarchy path in ``rows``."""
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        path = getattr(row, "full_path", str(row))
        if path in out:
            continue
        result = refine_param_ctx_for_path(index, top, path)
        out[path] = result.param_ctx if result.ok else dict(getattr(row, "param_ctx", {}) or {})
    return out