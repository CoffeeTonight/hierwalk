"""Hierarchy endpoint resolution and per-module connect graphs."""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect.logical.scan import BindRecord

_cache_key_locks: Dict[Tuple[int, object], threading.Lock] = {}
_cache_key_locks_guard = threading.Lock()


def _shared_cache_lock(cache: Dict, key: object) -> threading.Lock:
    """Per-(cache, key) lock so parallel workers serialize only colliding cache misses."""
    lock_id = (id(cache), key)
    with _cache_key_locks_guard:
        lock = _cache_key_locks.get(lock_id)
        if lock is None:
            lock = threading.Lock()
            _cache_key_locks[lock_id] = lock
        return lock


ModuleIndexCacheKey = Tuple[str, str, str, str, str, bool, bool, bool]
TextGrepIndexCacheKey = ModuleIndexCacheKey


def _mod_cache_lock(
    cache: Dict[ModuleIndexCacheKey, ModuleConnectIndex],
    key: ModuleIndexCacheKey,
) -> threading.Lock:
    return _shared_cache_lock(cache, key)


def _param_ctx_key(param_ctx: Mapping[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(param_ctx.items()))


def make_module_index_cache_key(
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    body_digest: str,
    defines_digest: str,
    bind_digest: str,
    ff_barrier: bool = False,
    over_approximate_if: bool = True,
    resolve_param_dims: bool = True,
) -> ModuleIndexCacheKey:
    return (
        mod_name,
        _param_ctx_key(param_ctx),
        body_digest,
        defines_digest,
        bind_digest,
        ff_barrier,
        over_approximate_if,
        resolve_param_dims,
    )

from hierwalk.cache import _pickle_load, get_active_work_dir
from hierwalk.connect.logical.scan import (
    ModuleConnectIndex,
    _clean_body,
    _collect_declared_net_names,
    _defines_digest,
    _net_base_in_assign_regex_fast,
    _net_base_in_port_map_regex_fast,
    _net_name_bases,
    apply_bind_connectivity,
    apply_empty_module_passthrough,
    binds_digest,
    build_module_connect_index,
    collect_assign_net_names,
    collect_bind_records_for_module,
    extract_connect_nodes,
    file_modules_bind_digest,
    net_base_in_assign_probe,
    net_base_in_port_map_probe,
    instance_port_maps,
)
from hierwalk.manifest import path_content_digest
from hierwalk.hierarchy_log import format_row_provenance
from hierwalk.index import DesignIndex
from hierwalk.inst_scan import probe_inst_leaf_regex_fast
from hierwalk.models import ConnectEndpoint, FlatRow
from hierwalk.params import resolve_param_map
from hierwalk.path_refine import refine_param_ctx_for_path
from hierwalk.port_scan import (
    matching_ports,
    port_index_for_design_module,
    ports_for_module,
    scan_ports_detail_from_module_text,
)


def _module_source_text(
    index: DesignIndex,
    mod_name: str,
    *,
    defines: Mapping[str, str] | None = None,
) -> str:
    rec = index.get_module(mod_name)
    if not rec or not rec.file_path:
        return ""
    eff = defines if defines is not None else index.effective_defines()
    return index._source_text(rec.file_path, defines=eff)


def _port_decl_md_suffixes(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    defines: Mapping[str, str] | None = None,
) -> Dict[str, List[str]]:
    text = _module_source_text(index, mod_name, defines=defines)
    if not text:
        return {}
    out: Dict[str, List[str]] = {}
    for info in scan_ports_detail_from_module_text(
        text,
        mod_name,
        param_ctx=param_ctx,
    ):
        suffixes: List[str] = []
        for name in info.names:
            if name.startswith(info.base_name + "["):
                suffixes.append(name[len(info.base_name) :])
        if suffixes:
            out[info.base_name] = sorted(set(suffixes))
    return out


def _port_decl_bit_indices(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    defines: Mapping[str, str] | None = None,
) -> Dict[str, List[int]]:
    text = _module_source_text(index, mod_name, defines=defines)
    if not text:
        return {}
    out: Dict[str, List[int]] = {}
    for info in scan_ports_detail_from_module_text(
        text,
        mod_name,
        param_ctx=param_ctx,
    ):
        bits: List[int] = []
        for name in info.names:
            m = re.match(rf"^{re.escape(info.base_name)}\[(\d+)\]$", name)
            if m:
                bits.append(int(m.group(1)))
        if bits:
            out[info.base_name] = sorted(set(bits))
    return out

def parse_connect_endpoint(
    spec: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> Tuple[str, Optional[str]]:
    """
    Split *spec* into ``(inst_path, port_or_signal_tail)``.

    Intermediate dotted segments are instances only. Port/wire/reg resolution
    applies to the **last** segment of *spec* (when its parent row exists).
    """
    text = spec.strip()
    parts = text.split(".")
    if len(parts) >= 2 and index is not None:
        parent = ".".join(parts[:-1])
        leaf = parts[-1]
        parent_row = rows_by_path.get(parent)
        if parent_row is not None and leaf and _port_exists(
            index,
            parent_row,
            leaf,
            top=top,
        ):
            return parent, leaf
    if text in rows_by_path:
        return text, None
    # Only the final spec segment may be port/wire/reg; earlier segments are instances.
    last_idx = len(parts) - 1
    for i in range(last_idx, 0, -1):
        if i != last_idx:
            continue
        hier = ".".join(parts[:i])
        row = rows_by_path.get(hier)
        if row is None:
            continue
        tail = parts[-1]
        if not tail:
            return hier, None
        if index is not None:
            if _port_exists(index, row, tail, top=top):
                return hier, tail
            if net_exists_in_module_fast(
                index,
                row,
                tail,
                top=top,
                param_ctx=_row_param_ctx_optional(row),
            ):
                return hier, tail
        return hier, tail
    return text, None


def _row_param_ctx_optional(row: FlatRow) -> Optional[Mapping[str, str]]:
    """Return row ctx for port probes; None only when path-refine may still be needed."""
    if row.param_ctx_folded:
        return row.param_ctx
    if row.param_ctx:
        return row.param_ctx
    return None


def param_ctx_usable_for_dims(ctx: Mapping[str, str]) -> bool:
    """True when *ctx* has concrete values suitable for parametric bit indexing."""
    if not ctx:
        return False
    return all(str(v).strip() != k for k, v in ctx.items())


def _port_param_ctx(
    index: DesignIndex,
    row: FlatRow,
    top: str,
    *,
    resolve_param_dims: bool = False,
) -> Mapping[str, str]:
    stored = row.param_ctx
    if resolve_param_dims and top and not param_ctx_usable_for_dims(stored):
        refined = refine_param_ctx_for_path(index, top, row.full_path)
        if refined.ok and refined.param_ctx:
            return refined.param_ctx
    if row.param_ctx_folded:
        return stored
    if stored:
        return stored
    if resolve_param_dims and top:
        refined = refine_param_ctx_for_path(index, top, row.full_path)
        if refined.ok and refined.param_ctx:
            return refined.param_ctx
    rec = index.get_module(row.module)
    if not rec:
        return {}
    return resolve_param_map(rec.raw_params)




def _port_exists(
    index: DesignIndex,
    row: FlatRow,
    port_name: str,
    *,
    top: str,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> bool:
    ctx = (
        dict(param_ctx)
        if param_ctx is not None
        else _port_param_ctx(index, row, top)
    )
    port_index = port_index_for_design_module(index, row.module, ctx)
    return bool(matching_ports(port_index, port_name, param_ctx=ctx))


def is_module_local_signal_name(name: str) -> bool:
    """True when *name* is a single module-local identifier (not a dotted hierarchy tail)."""
    text = name.strip()
    return bool(text) and "." not in text.split("[", 1)[0]


def wire_tail_exists_fast(
    body: str,
    net_name: str,
    *,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> bool:
    """
    Cheapest wire/reg tail probe: decl/assign regex only (no param refine, no stmt walk).
    """
    if not body or not net_name or not is_module_local_signal_name(net_name):
        return False
    base = net_name.split("[", 1)[0].split(".", 1)[0]
    if _net_base_declared_fast(body, base):
        return True
    if param_ctx:
        if net_base_in_assign_probe(body, base, param_map=param_ctx):
            return True
        return net_base_in_port_map_probe(body, base, param_map=param_ctx)
    if _net_base_in_assign_regex_fast(body, base):
        return True
    return _net_base_in_port_map_regex_fast(body, base)


def _nearest_hierarchy_row(
    spec: str,
    rows_by_path: Mapping[str, FlatRow],
) -> Tuple[str, Optional[FlatRow]]:
    text = spec.strip()
    if text in rows_by_path:
        return text, rows_by_path[text]
    parts = text.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        row = rows_by_path.get(prefix)
        if row is not None:
            return prefix, row
    return "", None


def _child_instances(
    parent_path: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    limit: int = 12,
) -> List[str]:
    kids = sorted(
        r.inst_leaf
        for r in rows_by_path.values()
        if r.parent_path == parent_path
    )
    return kids[:limit]


def _suggest_instances(
    needle: str,
    children: Sequence[str],
    *,
    limit: int = 8,
) -> List[str]:
    base = needle.split("[", 1)[0]
    out: List[str] = []
    for name in children:
        if base in name or name.startswith(base[: max(1, len(base) // 2)]):
            out.append(name)
    return out[:limit]


def _explain_hierarchy_miss(
    spec: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    index: DesignIndex,
    top: str,
    broken_prefix: str,
) -> List[str]:
    errors: List[str] = []
    text = spec.strip()
    nearest, row = _nearest_hierarchy_row(text, rows_by_path)

    if row is None:
        roots = sorted({p.split(".", 1)[0] for p in rows_by_path})
        errors.append(f"hierarchy not found: '{text}' — no matching instance path")
        if broken_prefix:
            errors.append(f"missing instance prefix: '{broken_prefix}'")
        if roots:
            errors.append(f"elab roots ({len(roots)}): {', '.join(roots[:8])}")
        return errors

    remainder = text[len(nearest) + 1 :] if len(nearest) < len(text) else ""
    errors.append(
        f"hierarchy not found: '{text}' — path stops at '{nearest}' "
        f"({format_row_provenance(row)})"
    )
    if remainder:
        errors.append(f"unresolved suffix: '{remainder}'")
    children = _child_instances(nearest, rows_by_path)
    if children:
        errors.append(f"instances under '{nearest}':")
        for leaf in children:
            child_path = f"{nearest}.{leaf}" if nearest else leaf
            child_row = rows_by_path.get(child_path)
            if child_row is not None:
                errors.append(f"  {child_path}  ({format_row_provenance(child_row)})")
            else:
                errors.append(f"  {child_path}")
        first_seg = remainder.split(".", 1)[0] if remainder else ""
        similar = _suggest_instances(first_seg, children)
        if similar:
            errors.append(f"similar instance names: {', '.join(similar)}")

    if remainder and "." not in remainder:
        ctx = _port_param_ctx(index, row, top)
        ports = sorted(ports_for_module(row.file, row.module, ctx, index=index))
        if ports:
            errors.append(
                f"ports on '{nearest}' ({row.module}, {len(ports)}): "
                f"{', '.join(ports[:16])}"
            )
            leaf = remainder.split("[", 1)[0]
            port_hits = [p for p in ports if leaf in p or p.startswith(leaf)]
            if port_hits:
                errors.append(f"similar port names: {', '.join(port_hits[:8])}")
    return errors


def _module_body_for_row(index: DesignIndex, row: FlatRow) -> str:
    body = index.module_body(row.module)
    if body:
        return body
    rec = index.get_module(row.module)
    if rec is None:
        return ""
    if rec.body:
        return rec.body
    return ""


DeclNetCacheKey = Tuple[str, Tuple[Tuple[str, str], ...]]
DeclNetCache = Dict[DeclNetCacheKey, Set[str]]
ModuleBodyCache = Dict[str, str]

_CELL_MODULE_BODY_CACHE_PREFIX = "cell:"


def lookup_cell_module_body(
    index: DesignIndex,
    cell: str,
    *,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> str:
    """
    Resolve a child cell module body for inst-blackbox passthrough.

    Path-walk often knows a cell type before the module is registered in
    ``DesignIndex`` (e.g. ``zz_y_fork`` in ``zz_common.v``). Fall back to
    preprocessed/indexed RTL and the design filelist when ``get_module`` misses.
    """
    from hierwalk.index import _module_header_body

    name = (cell or "").strip()
    if not name:
        return ""
    cache_key = f"{_CELL_MODULE_BODY_CACHE_PREFIX}{name}"
    if module_body_cache is not None and cache_key in module_body_cache:
        return module_body_cache[cache_key]

    rec = index.get_module(name)
    if rec is not None:
        body = index.module_body(name)
        if body.strip():
            if module_body_cache is not None:
                module_body_cache[cache_key] = body
            return body

    preprocessed = getattr(index, "_preprocessed_sources", None) or {}
    for text in preprocessed.values():
        if not text:
            continue
        _header, body = _module_header_body(text, name)
        if body.strip():
            if module_body_cache is not None:
                module_body_cache[cache_key] = body
            return body

    paths: List[str] = []
    if sources:
        paths.extend(sources)
    else:
        from hierwalk.connect.logical.scan import design_parse_sources

        paths.extend(design_parse_sources(index))

    seen_paths: Set[str] = set()
    for fpath in paths:
        if not fpath:
            continue
        resolved = str(Path(fpath).resolve())
        for key in (fpath, resolved):
            if key in seen_paths:
                continue
            seen_paths.add(key)
            text = preprocessed.get(key, "")
            if not text.strip():
                text = index._source_text(fpath)
            if not text.strip():
                continue
            _header, body = _module_header_body(text, name)
            if body.strip():
                if module_body_cache is not None:
                    module_body_cache[cache_key] = body
                return body

    if module_body_cache is not None:
        module_body_cache[cache_key] = ""
    return ""


def make_cell_module_body_lookup(
    index: DesignIndex,
    *,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> Callable[[str], str]:
    def _lookup(cell: str) -> str:
        return lookup_cell_module_body(
            index,
            cell,
            module_body_cache=module_body_cache,
            sources=sources,
        )

    return _lookup


def _module_body_cache_key(row: FlatRow) -> str:
    return str(row.file or row.module)


def _cached_module_body_for_row(
    index: DesignIndex,
    row: FlatRow,
    *,
    cache: Optional[ModuleBodyCache] = None,
) -> str:
    if cache is None:
        return _module_body_for_row(index, row)
    key = _module_body_cache_key(row)
    hit = cache.get(key)
    if hit is not None:
        return hit
    body = _module_body_for_row(index, row)
    cache[key] = body
    return body


def _decl_net_cache_key(row: FlatRow, ctx: Mapping[str, str]) -> DeclNetCacheKey:
    from hierwalk.index import _ctx_key

    return row.module, _ctx_key(ctx)


def _cache_note_decl_net_hit(
    cache: Optional[DeclNetCache],
    key: DeclNetCacheKey,
    net_name: str,
    base: str,
) -> None:
    if cache is None:
        return
    bucket = cache.get(key)
    if bucket is None:
        bucket = set()
        cache[key] = bucket
    for name in (net_name, base):
        if name:
            bucket.add(name)


def _net_base_decl_match(body: str, base: str, type_kw: str) -> bool:
    if not body or not base:
        return False
    clean = _clean_body(body)
    esc = re.escape(base)
    pat = re.compile(
        rf"\b{re.escape(type_kw)}\b\s*"
        rf"(?:\([^)]*\)\s*)?"
        rf"(?:(?:\[[^\]]+\]\s*)*)"
        rf"(?:{esc}\b|(?:(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)\s*,\s*)*{esc}\b)",
        re.IGNORECASE,
    )
    return pat.search(clean) is not None


def _net_base_is_reg_fast(body: str, base: str) -> bool:
    """True when *base* is declared as a ``reg`` in module *body*."""
    return _net_base_decl_match(body, base, "reg")


def _net_base_is_logic_fast(body: str, base: str) -> bool:
    """True when *base* is declared as ``logic`` in module *body*."""
    return _net_base_decl_match(body, base, "logic")


def _net_base_is_wire_fast(body: str, base: str) -> bool:
    """True when *base* is declared as ``wire`` in module *body*."""
    return _net_base_decl_match(body, base, "wire")


def classify_signal_tail_kind(
    index: DesignIndex,
    row: FlatRow,
    signal_name: str,
    *,
    top: str,
    body: Optional[str] = None,
) -> Optional[str]:
    """Classify a module-local tail as ``port``, ``wire``, ``logic``, ``reg``, or unknown."""
    if not signal_name or not is_module_local_signal_name(signal_name):
        return None
    text = body if body is not None else _module_body_for_row(index, row)
    if not text:
        return None
    stem = signal_name.split("[", 1)[0]
    if _port_exists(index, row, signal_name, top=top):
        return "port"
    if probe_inst_leaf_regex_fast(text, stem):
        return None
    base = stem.split(".", 1)[0]
    if _net_base_is_reg_fast(text, base):
        return "reg"
    if _net_base_is_logic_fast(text, base):
        return "logic"
    if _net_base_is_wire_fast(text, base):
        return "wire"
    if _net_base_in_assign_regex_fast(text, base):
        return "wire"
    if _net_base_in_port_map_regex_fast(text, base):
        return "wire"
    if wire_tail_exists_fast(text, signal_name):
        return "wire"
    return None


def _net_base_declared_fast(body: str, base: str) -> bool:
    """
    Single-name declaration probe (wire/logic/reg/port) without full statement split.

    Used on the path-walk signal-tail hot path before building a per-module decl cache.
    """
    if not body or not base:
        return False
    clean = _clean_body(body)
    esc = re.escape(base)
    pat = re.compile(
        rf"(?:\b(?:input|output|inout|wire|logic|reg)\b\s*)"
        rf"(?:\([^)]*\)\s*)?"
        rf"(?:(?:\[[^\]]+\]\s*)*)"
        rf"(?:{esc}\b|(?:(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)\s*,\s*)*{esc}\b)",
        re.IGNORECASE,
    )
    return pat.search(clean) is not None


def module_declared_net_names(
    index: DesignIndex,
    row: FlatRow,
    *,
    top: str,
    cache: Optional[DeclNetCache] = None,
    param_ctx: Optional[Mapping[str, str]] = None,
    body: Optional[str] = None,
    deep: bool = False,
) -> Set[str]:
    """Port + declared wire/logic/reg (+ optional assign/port-map nets; no connect graph)."""
    ctx = (
        dict(param_ctx)
        if param_ctx is not None
        else _port_param_ctx(index, row, top)
    )
    key = _decl_net_cache_key(row, ctx)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    names: Set[str] = set()
    port_index = port_index_for_design_module(index, row.module, ctx)
    for info in port_index.values():
        names.update(info.names)
    text = body if body is not None else _module_body_for_row(index, row)
    if text:
        names.update(_collect_declared_net_names(text))
        if deep:
            names.update(collect_assign_net_names(text, param_map=ctx))
            for _inst, ports in instance_port_maps(text, param_map=ctx).items():
                for _port, expr in ports:
                    names.update(_net_name_bases(extract_connect_nodes(expr, ctx)))
    if cache is not None:
        cache[key] = names
    return names


def net_exists_in_module_fast(
    index: DesignIndex,
    row: FlatRow,
    net_name: str,
    *,
    top: str,
    cache: Optional[DeclNetCache] = None,
    param_ctx: Optional[Mapping[str, str]] = None,
    body: Optional[str] = None,
) -> bool:
    """
    Fast signal-tail existence check: ports, declarations, assign-driven implicit nets.

    Wire/reg probes run before param-refine and before full statement walks.
    Avoids :func:`build_module_connect_index` (assign/FF scan + UF compression).
    """
    if not net_name or not is_module_local_signal_name(net_name):
        return False
    base = net_name.split("[", 1)[0].split(".", 1)[0]
    text = body if body is not None else _module_body_for_row(index, row)
    ctx = (
        dict(param_ctx)
        if param_ctx is not None
        else (
            dict(row.param_ctx)
            if row.param_ctx_folded or row.param_ctx
            else dict(_port_param_ctx(index, row, top))
        )
    )
    use_param_probe = bool(ctx) and not row.param_ctx_folded
    if wire_tail_exists_fast(
        text,
        net_name,
        param_ctx=ctx if use_param_probe else None,
    ):
        return True
    key = _decl_net_cache_key(row, ctx)
    if cache is not None and key in cache:
        names = cache[key]
        return net_name in names or base in names
    if _port_exists(index, row, net_name, top=top, param_ctx=ctx):
        return True
    if not text:
        return False
    # Single-name probes only — never walk the full module decl/assign set here.
    if _net_base_declared_fast(text, base):
        _cache_note_decl_net_hit(cache, key, net_name, base)
        return True
    if probe_inst_leaf_regex_fast(text, base):
        return False
    if _net_base_in_assign_regex_fast(text, base):
        _cache_note_decl_net_hit(cache, key, net_name, base)
        return True
    if _net_base_in_port_map_regex_fast(text, base):
        _cache_note_decl_net_hit(cache, key, net_name, base)
        return True
    if use_param_probe:
        if net_base_in_assign_probe(text, base, param_map=ctx):
            _cache_note_decl_net_hit(cache, key, net_name, base)
            return True
        if net_base_in_port_map_probe(text, base, param_map=ctx):
            _cache_note_decl_net_hit(cache, key, net_name, base)
            return True
    return False


def _net_exists_in_module(
    index: DesignIndex,
    row: FlatRow,
    net_name: str,
    *,
    top: str,
) -> bool:
    """True when *net_name* is a declared/used port or internal wire/reg in *row*."""
    if not net_name:
        return False
    if _port_exists(index, row, net_name, top=top):
        return True
    ctx = _port_param_ctx(index, row, top)
    body = _module_body_for_row(index, row)
    if not body:
        return False
    from hierwalk.connect.session import _effective_defines

    eff = index.effective_defines()
    decl_widths = _port_decl_bit_indices(index, row.module, ctx, defines=eff)
    mod_idx = build_module_connect_index(
        body,
        param_map=ctx,
        defines=eff,
        port_decl_widths=decl_widths,
        port_decl_md_suffixes=_port_decl_md_suffixes(index, row.module, ctx, defines=eff),
    )
    if net_name in mod_idx.net_rep:
        return True
    base = net_name.split("[", 1)[0].split(".", 1)[0]
    if base in mod_idx.net_rep:
        return True
    return False


def _explain_port_miss(
    inst_path: str,
    port_name: str,
    row: FlatRow,
    *,
    index: DesignIndex,
    top: str,
) -> List[str]:
    ctx = _port_param_ctx(index, row, top)
    ports = sorted(ports_for_module(row.file, row.module, ctx, index=index))
    if net_exists_in_module_fast(
        index,
        row,
        port_name,
        top=top,
        param_ctx=_row_param_ctx_optional(row),
        body=_module_body_for_row(index, row),
    ):
        return []
    errors = [
        f"signal/port not found: '{inst_path}.{port_name}' on module {row.module} "
        f"({row.file})"
    ]
    if ports:
        errors.append(f"declared ports ({len(ports)}): {', '.join(ports[:20])}")
        leaf = port_name.split("[", 1)[0].split(".", 1)[0]
        hits = [p for p in ports if leaf in p or p.startswith(leaf)]
        if hits:
            errors.append(f"similar ports: {', '.join(hits[:8])}")
    else:
        errors.append("no ports parsed for this module (blackbox or parse limit)")
    ctx = _port_param_ctx(index, row, top)
    body = _module_body_for_row(index, row)
    if body:
        from hierwalk.connect.session import _effective_defines

        eff = index.effective_defines()
        decl_widths = _port_decl_bit_indices(index, row.module, ctx, defines=eff)
        mod_idx = build_module_connect_index(
            body,
            param_map=ctx,
            defines=eff,
            port_decl_widths=decl_widths,
            port_decl_md_suffixes=_port_decl_md_suffixes(index, row.module, ctx, defines=eff),
        )
        internal = sorted(
            n
            for n in mod_idx.net_rep
            if n.split("[", 1)[0] == port_name.split("[", 1)[0]
            or n.startswith(port_name.split("[", 1)[0] + "[")
        )
        if internal:
            errors.append(
                f"similar internal signals: {', '.join(internal[:8])}"
            )
    return errors


def resolve_endpoint(
    spec: str,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    *,
    top: str,
    require_port: bool = False,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    decl_net_cache: Optional[DeclNetCache] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
) -> Tuple[ConnectEndpoint, List[str]]:
    if rows_by_path is not None:
        lookup = rows_by_path
    else:
        lookup = {r.full_path: r for r in rows}
    text = spec.strip()
    inst_path, port_name = parse_connect_endpoint(
        text,
        lookup,
        index=index,
        top=top,
    )
    errors: List[str] = []
    row = lookup.get(inst_path) if inst_path else None

    if row is None:
        broken = inst_path or text
        errors.extend(
            _explain_hierarchy_miss(
                text,
                lookup,
                index=index,
                top=top,
                broken_prefix=broken,
            )
        )
        return ConnectEndpoint(
            spec=text,
            inst_path=inst_path or "",
            port_name=port_name or "",
            module="",
            port_found=False,
        ), errors

    ep = ConnectEndpoint(
        spec=text,
        inst_path=inst_path,
        port_name=port_name or "",
        module=row.module,
        port_found=False,
    )
    if port_name is None:
        if require_port:
            errors.append(f"port required but not given: {spec}")
        return ep, errors
    if net_exists_in_module_fast(
        index,
        row,
        port_name,
        top=top,
        cache=decl_net_cache,
        param_ctx=_row_param_ctx_optional(row),
        body=_cached_module_body_for_row(
            index,
            row,
            cache=module_body_cache,
        ),
    ):
        ep.port_found = True
        return ep, errors
    errors.extend(_explain_port_miss(inst_path, port_name, row, index=index, top=top))
    return ep, errors


def _lca(path_a: str, path_b: str) -> str:
    parts_a = path_a.split(".")
    parts_b = path_b.split(".")
    common: List[str] = []
    for a, b in zip(parts_a, parts_b):
        if a != b:
            break
        common.append(a)
    return ".".join(common)


def _prune_rows_lca(rows: Sequence[FlatRow], path_a: str, path_b: str) -> List[FlatRow]:
    lca = _lca(path_a, path_b)
    if not lca:
        return list(rows)
    child_prefix = lca + "."
    return [
        r
        for r in rows
        if r.full_path == lca or r.full_path.startswith(child_prefix)
    ]


def _empty_module_passthrough_ports(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    defines: Mapping[str, str] | None = None,
) -> Optional[Tuple[str, str]]:
    from hierwalk.port_scan import scan_ports_detail_from_module_text

    text = _module_source_text(index, mod_name, defines=defines)
    if not text:
        return None
    inputs: List[str] = []
    outputs: List[str] = []
    for info in scan_ports_detail_from_module_text(
        text, mod_name, param_ctx=param_ctx
    ):
        decl = info.decl.lower()
        if decl.startswith("input"):
            inputs.extend(info.names)
        elif decl.startswith("output"):
            outputs.extend(info.names)
    if len(inputs) == 1 and len(outputs) == 1:
        return inputs[0], outputs[0]
    return None


_CONNECT_INDEX_SIDECAR_VERSION = 5

_ModuleIndexKeyMemoEntry = Tuple[str, str, ModuleIndexCacheKey, Tuple[BindRecord, ...]]
_module_index_key_memo: Dict[
    Tuple[int, str, str, str, bool, bool, str],
    _ModuleIndexKeyMemoEntry,
] = {}
_module_index_key_memo_guard = threading.Lock()


def _clear_module_index_key_memo() -> None:
    with _module_index_key_memo_guard:
        _module_index_key_memo.clear()


def _resolve_module_index_key(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    defines: Mapping[str, str] | None,
    *,
    ff_barrier: bool,
    over_approximate_if: bool,
    resolve_param_dims: bool = True,
) -> Tuple[ModuleIndexCacheKey, List[BindRecord]]:
    rec = index.get_module(mod_name)
    body = index.module_body(mod_name) if rec else ""
    body_digest = _module_body_digest(
        mod_name,
        body,
        rec.file_path if rec else None,
    )
    defines_digest = _defines_digest(defines)
    files_digest = file_modules_bind_digest(index)
    partial = (
        id(index),
        mod_name,
        _param_ctx_key(param_ctx),
        defines_digest,
        ff_barrier,
        over_approximate_if,
        resolve_param_dims,
        files_digest,
    )
    with _module_index_key_memo_guard:
        entry = _module_index_key_memo.get(partial)
        if entry is not None and entry[0] == body_digest:
            return entry[2], list(entry[3])
    binds = collect_bind_records_for_module(index, mod_name)
    bind_digest = binds_digest(binds)
    full_key = make_module_index_cache_key(
        mod_name,
        param_ctx,
        body_digest=body_digest,
        defines_digest=defines_digest,
        bind_digest=bind_digest,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        resolve_param_dims=resolve_param_dims,
    )
    frozen_binds = tuple(binds)
    with _module_index_key_memo_guard:
        _module_index_key_memo[partial] = (
            body_digest,
            bind_digest,
            full_key,
            frozen_binds,
        )
    return full_key, binds


@dataclass(frozen=True)
class _ModuleConnectSidecarMeta:
    version: int
    mod_name: str
    ctx_key: str
    body_digest: str
    defines_digest: str
    bind_digest: str
    ff_barrier: bool
    over_approximate_if: bool
    resolve_param_dims: bool = True


def _module_body_digest(mod_name: str, body: str, rec_file_path: Optional[str]) -> str:
    if rec_file_path:
        digest = path_content_digest(Path(rec_file_path))
        if digest:
            return digest[:16]
    return hashlib.sha256(body.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def _module_connect_sidecar_root() -> Optional[Path]:
    active = get_active_work_dir()
    if active is not None:
        return active / "connect_index"
    env = os.environ.get("HIERWALK_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve() / "connect_index"
    return None


def _module_connect_sidecar_path(cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
    return _module_connect_sidecar_root() / f"{digest}.mci.pkl"


def _module_connect_sidecar_key(
    *,
    mod_name: str,
    ctx_key: str,
    body_digest: str,
    defines_digest: str,
    bind_digest: str,
    ff_barrier: bool,
    over_approximate_if: bool,
    resolve_param_dims: bool = True,
) -> str:
    return (
        f"v={_CONNECT_INDEX_SIDECAR_VERSION}|{mod_name}|{ctx_key}|"
        f"{body_digest}|{defines_digest}|{bind_digest}|"
        f"{int(ff_barrier)}|{int(over_approximate_if)}|{int(resolve_param_dims)}"
    )


def _load_module_connect_sidecar(
    cache_key: str,
    *,
    meta: _ModuleConnectSidecarMeta,
) -> Optional[ModuleConnectIndex]:
    if os.environ.get("HIERWALK_CONNECT_INDEX_SIDECAR", "").lower() in ("0", "off", "false"):
        return None
    if _module_connect_sidecar_root() is None:
        return None
    path = _module_connect_sidecar_path(cache_key)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            obj = _pickle_load(fh)
    except (OSError, pickle.UnpicklingError, EOFError, ValueError):
        return None
    if not isinstance(obj, tuple) or len(obj) != 2:
        return None
    stored_meta, idx = obj
    if not isinstance(stored_meta, _ModuleConnectSidecarMeta):
        return None
    if stored_meta != meta:
        return None
    if not isinstance(idx, ModuleConnectIndex):
        return None
    return idx


def clear_module_connect_sidecar_cache() -> None:
    """Remove on-disk connect-index sidecars (test isolation)."""
    root = _module_connect_sidecar_root()
    if root is None or not root.is_dir():
        return
    for path in root.glob("*.mci.pkl"):
        try:
            path.unlink()
        except OSError:
            pass


def _save_module_connect_sidecar(
    cache_key: str,
    idx: ModuleConnectIndex,
    *,
    meta: _ModuleConnectSidecarMeta,
) -> None:
    if os.environ.get("HIERWALK_CONNECT_INDEX_SIDECAR", "").lower() in ("0", "off", "false"):
        return
    if _module_connect_sidecar_root() is None:
        return
    path = _module_connect_sidecar_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump((meta, idx), fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
    except OSError:
        return


def _module_index(
    cache: Dict[ModuleIndexCacheKey, ModuleConnectIndex],
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    resolve_param_dims: bool = True,
    text_grep_cache: Optional[Dict[ModuleIndexCacheKey, object]] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> ModuleConnectIndex:
    key, binds = _resolve_module_index_key(
        index,
        mod_name,
        param_ctx,
        defines,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        resolve_param_dims=resolve_param_dims,
    )
    hit = cache.get(key)
    if hit is not None:
        return hit
    with _mod_cache_lock(cache, key):
        hit = cache.get(key)
        if hit is not None:
            return hit
        built = _build_module_index_entry(
            cache,
            index,
            mod_name,
            param_ctx,
            key=key,
            defines=defines,
            binds=binds,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            resolve_param_dims=resolve_param_dims,
            text_grep_cache=text_grep_cache,
            module_body_cache=module_body_cache,
            sources=sources,
        )
        return built


def _build_module_index_entry(
    cache: Dict[ModuleIndexCacheKey, ModuleConnectIndex],
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    key: ModuleIndexCacheKey,
    defines: Mapping[str, str] | None = None,
    binds: Optional[Sequence[BindRecord]] = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    resolve_param_dims: bool = True,
    text_grep_cache: Optional[Dict[ModuleIndexCacheKey, object]] = None,
    module_body_cache: Optional[ModuleBodyCache] = None,
    sources: Optional[Sequence[str]] = None,
) -> ModuleConnectIndex:
    from hierwalk.connect.layer import find_text_grep_seed
    from hierwalk.connect.text.index import enrich_text_grep_to_logical_index

    rec = index.get_module(mod_name)
    body = index.module_body(mod_name) if rec else ""
    ctx_key = key[1]
    body_digest = key[2]
    defines_digest = key[3]
    bind_digest = key[4]
    bind_list = (
        list(binds)
        if binds is not None
        else collect_bind_records_for_module(index, mod_name)
    )
    sidecar_meta = _ModuleConnectSidecarMeta(
        version=_CONNECT_INDEX_SIDECAR_VERSION,
        mod_name=mod_name,
        ctx_key=ctx_key,
        body_digest=body_digest,
        defines_digest=defines_digest,
        bind_digest=bind_digest,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        resolve_param_dims=resolve_param_dims,
    )
    sidecar_key = _module_connect_sidecar_key(
        mod_name=mod_name,
        ctx_key=ctx_key,
        body_digest=body_digest,
        defines_digest=defines_digest,
        bind_digest=bind_digest,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        resolve_param_dims=resolve_param_dims,
    )
    disk_hit = _load_module_connect_sidecar(sidecar_key, meta=sidecar_meta)
    if disk_hit is not None:
        cache[key] = disk_hit
        return disk_hit
    if not body.strip():
        built = ModuleConnectIndex()
        passthrough = _empty_module_passthrough_ports(
            index, mod_name, param_ctx, defines=defines
        )
        if passthrough:
            apply_empty_module_passthrough(built, passthrough[0], passthrough[1])
    else:
        source_file = rec.file_path if rec else None
        include_dirs = list(getattr(index, "_preprocess_include_dirs", ()) or ())
        text_seed = (
            find_text_grep_seed(text_grep_cache, key)
            if resolve_param_dims and text_grep_cache
            else None
        )
        port_widths = (
            _port_decl_bit_indices(index, mod_name, param_ctx, defines=defines)
            if resolve_param_dims
            else None
        )
        port_md = (
            _port_decl_md_suffixes(index, mod_name, param_ctx, defines=defines)
            if resolve_param_dims
            else None
        )
        module_body_lookup = make_cell_module_body_lookup(
            index,
            module_body_cache=module_body_cache,
            sources=sources,
        )

        if text_seed is not None:
            base = enrich_text_grep_to_logical_index(
                text_seed,
                body,
                param_map=param_ctx,
                defines=defines,
                over_approximate_if=over_approximate_if,
                ff_barrier=ff_barrier,
                source_file=source_file,
                include_dirs=include_dirs,
                port_decl_widths=port_widths,
                port_decl_md_suffixes=port_md,
                module_body_lookup=module_body_lookup,
            )
        else:
            base = build_module_connect_index(
                body,
                param_map=param_ctx,
                defines=defines,
                fold_generate=True,
                over_approximate_if=over_approximate_if,
                ff_barrier=ff_barrier,
                resolve_param_dims=resolve_param_dims,
                port_decl_widths=port_widths,
                port_decl_md_suffixes=port_md,
                source_file=source_file,
                include_dirs=include_dirs,
                module_body_lookup=module_body_lookup,
                design_index=index,
            )
        if bind_list:
            built = base.copy()
            apply_bind_connectivity(
                built,
                bind_list,
                index,
                param_map=param_ctx,
                defines=defines,
                over_approximate_if=over_approximate_if,
            )
        else:
            built = base
    cache[key] = built
    _save_module_connect_sidecar(sidecar_key, built, meta=sidecar_meta)
    return built
