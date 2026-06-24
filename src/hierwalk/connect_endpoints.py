"""Hierarchy endpoint resolution and per-module connect graphs."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

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


def _mod_cache_lock(
    cache: Dict[Tuple[str, str], ModuleConnectIndex],
    key: Tuple[str, str],
) -> threading.Lock:
    return _shared_cache_lock(cache, key)

from hierwalk.connect_scan import (
    ModuleConnectIndex,
    _clean_body,
    _collect_declared_net_names,
    _net_base_in_assign_regex_fast,
    _net_name_bases,
    apply_bind_connectivity,
    apply_empty_module_passthrough,
    build_module_connect_index,
    collect_assign_net_names,
    collect_bind_records_for_module,
    extract_connect_nodes,
    net_base_in_assign_probe,
    net_base_in_port_map_probe,
    instance_port_maps,
)
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


def _port_decl_md_suffixes(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
) -> Dict[str, List[str]]:
    rec = index.get_module(mod_name)
    if not rec or not rec.file_path:
        return {}
    try:
        text = Path(rec.file_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
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
) -> Dict[str, List[int]]:
    rec = index.get_module(mod_name)
    if not rec or not rec.file_path:
        return {}
    try:
        text = Path(rec.file_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
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
    text = spec.strip()
    if text in rows_by_path:
        return text, None
    parts = text.split(".")
    for i in range(len(parts) - 1, 0, -1):
        hier = ".".join(parts[:i])
        row = rows_by_path.get(hier)
        if row is None:
            continue
        tail = ".".join(parts[i:])
        if not tail:
            return hier, None
        if index is not None:
            if _port_exists(index, row, tail, top=top):
                return hier, tail
            if _net_exists_in_module(index, row, tail, top=top):
                return hier, tail
        if "." not in tail:
            return hier, tail
    return text, None


def _port_param_ctx(index: DesignIndex, row: FlatRow, top: str) -> Mapping[str, str]:
    if top:
        refined = refine_param_ctx_for_path(index, top, row.full_path)
        if refined.ok and refined.param_ctx:
            return refined.param_ctx
    if row.param_ctx:
        return row.param_ctx
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
    if _net_base_in_assign_regex_fast(body, base):
        return True
    if param_ctx is not None and net_base_in_assign_probe(
        body,
        base,
        param_map=param_ctx,
    ):
        return True
    return False


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
        ports = sorted(ports_for_module(row.file, row.module, ctx))
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
    if wire_tail_exists_fast(text, net_name, param_ctx=row.param_ctx or None):
        return True
    ctx = (
        dict(param_ctx)
        if param_ctx is not None
        else (dict(row.param_ctx) if row.param_ctx else _port_param_ctx(index, row, top))
    )
    key = _decl_net_cache_key(row, ctx)
    if cache is not None and key in cache:
        names = cache[key]
        return net_name in names or base in names
    if text and wire_tail_exists_fast(text, net_name, param_ctx=ctx):
        return True
    if _port_exists(index, row, net_name, top=top, param_ctx=ctx):
        return True
    if not text:
        return False
    # Single-name probes only — never walk the full module decl/assign set here.
    if _net_base_declared_fast(text, base):
        _cache_note_decl_net_hit(cache, key, net_name, base)
        return True
    if net_base_in_assign_probe(text, base, param_map=ctx):
        _cache_note_decl_net_hit(cache, key, net_name, base)
        return True
    if probe_inst_leaf_regex_fast(text, base):
        return False
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
    decl_widths = _port_decl_bit_indices(index, row.module, ctx)
    mod_idx = build_module_connect_index(
        body,
        param_map=ctx,
        port_decl_widths=decl_widths,
        port_decl_md_suffixes=_port_decl_md_suffixes(index, row.module, ctx),
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
    ports = sorted(ports_for_module(row.file, row.module, ctx))
    if _net_exists_in_module(index, row, port_name, top=top):
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
        decl_widths = _port_decl_bit_indices(index, row.module, ctx)
        mod_idx = build_module_connect_index(
            body,
            param_map=ctx,
            port_decl_widths=decl_widths,
            port_decl_md_suffixes=_port_decl_md_suffixes(index, row.module, ctx),
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
    if _net_exists_in_module(index, row, port_name, top=top):
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
) -> Optional[Tuple[str, str]]:
    rec = index.get_module(mod_name)
    if not rec or not rec.file_path:
        return None
    from pathlib import Path

    from hierwalk.port_scan import scan_ports_detail_from_module_text

    try:
        text = Path(rec.file_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
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


def _module_index(
    cache: Dict[Tuple[str, str], ModuleConnectIndex],
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
) -> ModuleConnectIndex:
    ctx_key = "|".join(f"{k}={v}" for k, v in sorted(param_ctx.items()))
    key = (mod_name, ctx_key)
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
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
        )
        return built


def _build_module_index_entry(
    cache: Dict[Tuple[str, str], ModuleConnectIndex],
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    key: Tuple[str, str],
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
) -> ModuleConnectIndex:
    rec = index.get_module(mod_name)
    body = index.module_body(mod_name) if rec else ""
    if not body.strip():
        built = ModuleConnectIndex()
        passthrough = _empty_module_passthrough_ports(index, mod_name, param_ctx)
        if passthrough:
            apply_empty_module_passthrough(built, passthrough[0], passthrough[1])
    else:
        built = build_module_connect_index(
            body,
            param_map=param_ctx,
            defines=defines,
            fold_generate=True,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            port_decl_widths=_port_decl_bit_indices(index, mod_name, param_ctx),
            port_decl_md_suffixes=_port_decl_md_suffixes(
                index, mod_name, param_ctx
            ),
        )
        binds = collect_bind_records_for_module(index, mod_name)
        if binds:
            apply_bind_connectivity(
                built,
                binds,
                index,
                param_map=param_ctx,
                defines=defines,
                over_approximate_if=over_approximate_if,
            )
    cache[key] = built
    return built
