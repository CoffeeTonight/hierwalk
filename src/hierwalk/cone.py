"""Fanin/fanout COI cones with FF / port / blackbox boundaries (standalone path)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, IO, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect_endpoints import _module_index
from hierwalk.connect_scan import ModuleConnectIndex, net_representative
from hierwalk.connectivity import resolve_endpoint
from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow
from hierwalk.trace_stop import TraceStopPolicy, trace_stop_boundary_kind
from hierwalk.params import resolve_param_map
from hierwalk.path_refine import refine_param_ctx_for_path
from hierwalk.port_scan import scan_ports_detail_from_module_text

NetState = Tuple[str, str]


@dataclass(frozen=True)
class ConeBoundary:
    kind: str
    scope: str
    net: str
    module: str
    detail: str

    @property
    def label(self) -> str:
        return f"{self.scope}:{self.net}" if self.net else self.scope


@dataclass(frozen=True)
class ConeEdge:
    kind: str
    detail: str
    from_scope: str
    from_net: str
    to_scope: str
    to_net: str


@dataclass
class ConeResult:
    origin_spec: str
    origin_scope: str
    origin_net: str
    direction: str
    boundaries: List[ConeBoundary] = field(default_factory=list)
    edges: List[ConeEdge] = field(default_factory=list)
    nets_visited: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def flip_flops(self) -> List[ConeBoundary]:
        return [b for b in self.boundaries if b.kind.startswith("ff-")]

    @property
    def ports(self) -> List[ConeBoundary]:
        return [b for b in self.boundaries if b.kind.startswith("port-")]

    @property
    def blackboxes(self) -> List[ConeBoundary]:
        return [b for b in self.boundaries if b.kind == "blackbox"]


@dataclass
class ConeModuleIndex:
    comb: ModuleConnectIndex
    ff_d_reps: FrozenSet[str]
    ff_q_reps: FrozenSet[str]
    input_reps: FrozenSet[str]
    output_reps: FrozenSet[str]


@dataclass
class _ConeCtx:
    rows_by_path: Dict[str, FlatRow]
    child_by_parent_leaf: Dict[Tuple[str, str], str]
    index: DesignIndex
    top: str
    mod_cache: Dict[Tuple[str, str], ConeModuleIndex]
    defines: Mapping[str, str]
    over_approximate_if: bool
    direction: str
    path_kind: str = "comb"
    comb_cache: Dict[Tuple[str, str, str, str, str, bool, bool], ModuleConnectIndex] = field(
        default_factory=dict
    )
    trace_stop: TraceStopPolicy = field(default_factory=TraceStopPolicy)


def _net_label(scope: str, net: str) -> str:
    return f"{scope}:{net}" if net else scope


def _port_reps_from_source(
    index: DesignIndex,
    mod_name: str,
    comb: ModuleConnectIndex,
    param_ctx: Mapping[str, str],
) -> Tuple[Set[str], Set[str]]:
    rec = index.get_module(mod_name)
    in_reps: Set[str] = set()
    out_reps: Set[str] = set()
    if not rec or not rec.file_path:
        return in_reps, out_reps
    from hierwalk.port_scan import port_index_for_design_module

    port_index = port_index_for_design_module(index, mod_name, param_ctx)
    if port_index:
        for info in port_index.values():
            decl = info.decl.lower()
            for name in info.names:
                rep = comb.net_rep.get(name, name)
                if decl.startswith("input"):
                    in_reps.add(rep)
                elif decl.startswith("output"):
                    out_reps.add(rep)
        return in_reps, out_reps
    src = index._source_text(rec.file_path)
    if not src:
        return in_reps, out_reps
    for info in scan_ports_detail_from_module_text(src, mod_name, param_ctx=param_ctx):
        decl = info.decl.lower()
        for name in info.names:
            rep = comb.net_rep.get(name, name)
            if decl.startswith("input"):
                in_reps.add(rep)
            elif decl.startswith("output"):
                out_reps.add(rep)
    return in_reps, out_reps


def _build_cone_module_index(
    index: DesignIndex,
    mod_name: str,
    param_ctx: Mapping[str, str],
    *,
    defines: Mapping[str, str] | None,
    over_approximate_if: bool,
    cache: Dict[Tuple[str, str], ConeModuleIndex],
    comb_cache: Dict[Tuple[str, str, str, str, str, bool, bool], ModuleConnectIndex],
) -> ConeModuleIndex:
    ctx_key = "|".join(f"{k}={v}" for k, v in sorted(param_ctx.items()))
    key = (mod_name, ctx_key)
    hit = cache.get(key)
    if hit is not None:
        return hit
    rec = index.get_module(mod_name)
    body = index.module_body(mod_name) if rec else ""
    if not body.strip():
        empty = ConeModuleIndex(
            comb=ModuleConnectIndex(),
            ff_d_reps=frozenset(),
            ff_q_reps=frozenset(),
            input_reps=frozenset(),
            output_reps=frozenset(),
        )
        cache[key] = empty
        return empty
    pmap = dict(param_ctx)
    comb = _module_index(
        comb_cache,
        index,
        mod_name,
        pmap,
        defines=defines,
        over_approximate_if=over_approximate_if,
        ff_barrier=True,
    )
    ff_d_reps = frozenset(
        {comb.net_rep.get(n, n) for n in comb.ff_d_roots}
    )
    ff_q_reps = frozenset(
        {comb.net_rep.get(n, n) for n in comb.ff_q_roots}
    )
    in_reps, out_reps = _port_reps_from_source(
        index,
        mod_name,
        comb,
        pmap,
    )
    built = ConeModuleIndex(
        comb=comb,
        ff_d_reps=ff_d_reps,
        ff_q_reps=ff_q_reps,
        input_reps=frozenset(in_reps),
        output_reps=frozenset(out_reps),
    )
    cache[key] = built
    return built


def _param_ctx_for_row(
    index: DesignIndex,
    row: FlatRow,
    top: str,
) -> Mapping[str, str]:
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


def _cached_cone_mod(ctx: _ConeCtx, row: FlatRow) -> ConeModuleIndex:
    pmap = _param_ctx_for_row(ctx.index, row, ctx.top)
    return _build_cone_module_index(
        ctx.index,
        row.module,
        pmap,
        defines=ctx.defines,
        over_approximate_if=ctx.over_approximate_if,
        cache=ctx.mod_cache,
        comb_cache=ctx.comb_cache,
    )


def _state_key(scope: str, net: str, mod_idx: ConeModuleIndex) -> NetState:
    rep = net_representative(mod_idx.comb, net)
    return scope, rep


def _is_blackbox_instance(ctx: _ConeCtx, row: FlatRow) -> bool:
    rec = ctx.index.get_module(row.module)
    if rec is None:
        return True
    if rec.is_blackbox or rec.stop_reason:
        return True
    return not index_module_has_body(ctx.index, row.module)


def index_module_has_body(index: DesignIndex, mod_name: str) -> bool:
    rec = index.get_module(mod_name)
    return bool(rec and index.module_body(mod_name).strip())


def _boundary_at_state(
    ctx: _ConeCtx,
    state: NetState,
    *,
    is_origin: bool = False,
) -> Optional[ConeBoundary]:
    scope, rep = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return None
    mod_idx = _cached_cone_mod(ctx, row)
    mod_name = row.module
    stop = trace_stop_boundary_kind(
        scope,
        top=ctx.top,
        row=row,
        policy=ctx.trace_stop,
        is_origin=is_origin,
    )
    if stop is not None:
        kind, detail = stop
        return ConeBoundary(kind, scope, rep, mod_name, detail)
    if _is_blackbox_instance(ctx, row) and not is_origin:
        return ConeBoundary(
            "blackbox",
            scope,
            rep,
            mod_name,
            f"blackbox / opaque instance {mod_name}",
        )
    if ctx.direction == "fanout":
        if ctx.path_kind == "comb" and rep in mod_idx.ff_d_reps:
            return ConeBoundary(
                "ff-sink",
                scope,
                rep,
                mod_name,
                f"always_ff D input in {mod_name}",
            )
        if rep in mod_idx.output_reps and not is_origin:
            return ConeBoundary(
                "port-out",
                scope,
                rep,
                mod_name,
                f"module output port in {mod_name}",
            )
    else:
        if ctx.path_kind == "comb" and rep in mod_idx.ff_q_reps:
            return ConeBoundary(
                "ff-driver",
                scope,
                rep,
                mod_name,
                f"always_ff Q output in {mod_name}",
            )
        if rep in mod_idx.input_reps and not is_origin:
            return ConeBoundary(
                "port-in",
                scope,
                rep,
                mod_name,
                f"module input port in {mod_name}",
            )
    return None


def _expand_fanout(
    state: NetState,
    ctx: _ConeCtx,
) -> List[Tuple[NetState, str, str]]:
    scope, net = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return []
    mod_idx = _cached_cone_mod(ctx, row)
    comb = mod_idx.comb
    rep = net_representative(comb, net)
    here = _net_label(scope, net)
    mod_name = row.module
    out: List[Tuple[NetState, str, str]] = []
    seen: Set[NetState] = set()

    def push(
        nxt_scope: str,
        nxt_net: str,
        kind: str,
        detail: str,
        target: Optional[ConeModuleIndex] = None,
    ) -> None:
        tmod = target or mod_idx
        key = _state_key(nxt_scope, nxt_net, tmod)
        if key not in seen:
            seen.add(key)
            out.append((key, kind, detail))

    for peer in comb.rep_adj.get(rep, ()):
        push(
            scope,
            peer,
            "intra-module",
            f"{here} ~ {_net_label(scope, peer)} (comb in {mod_name})",
        )

    if ctx.path_kind == "ff" and rep in mod_idx.ff_d_reps:
        for q_rep in mod_idx.ff_q_reps:
            push(
                scope,
                q_rep,
                "ff-interior",
                f"{here} -> {_net_label(scope, q_rep)} "
                f"(always_ff Q in {mod_name})",
            )

    for inst_leaf, port in comb.net_to_children.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if not child_path:
            continue
        child_row = ctx.rows_by_path.get(child_path)
        if child_row is None:
            continue
        if _is_blackbox_instance(ctx, child_row):
            push(
                child_path,
                port,
                "blackbox",
                f"{here} -> {_net_label(child_path, port)} "
                f"(blackbox {child_row.module} instance {inst_leaf})",
                target=mod_idx,
            )
            continue
        child_mod = _cached_cone_mod(ctx, child_row)
        push(
            child_path,
            port,
            "child-down",
            f"{here} -> {_net_label(child_path, port)} "
            f"(instance {inst_leaf}.{port})",
            target=child_mod,
        )

    for inst_leaf, port in comb.hier_links.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if not child_path:
            continue
        child_row = ctx.rows_by_path.get(child_path)
        if child_row is None:
            continue
        child_rec = ctx.index.get_module(child_row.module)
        if child_rec is not None and child_rec.is_interface:
            continue
        child_mod = _cached_cone_mod(ctx, child_row)
        push(
            child_path,
            port,
            "child-hier",
            f"{here} -> {_net_label(child_path, port)} (hier {inst_leaf})",
            target=child_mod,
        )

    parent_path = row.parent_path
    if parent_path:
        parent_row = ctx.rows_by_path.get(parent_path)
        if parent_row is not None:
            parent_mod = _cached_cone_mod(ctx, parent_row)
            parent_comb = parent_mod.comb
            for port_name, expr in parent_comb.inst_ports.get(row.inst_leaf, ()):
                if net_representative(comb, port_name) != rep:
                    continue
                roots = parent_comb.expr_roots.get(expr) or frozenset()
                child_lbl = _net_label(scope, port_name)
                if not roots and expr.strip():
                    push(
                        parent_path,
                        expr.strip(),
                        "parent-up",
                        f"{child_lbl} -> {_net_label(parent_path, expr.strip())} "
                        f"(port map {row.inst_leaf}.{port_name}={expr})",
                        target=parent_mod,
                    )
                for root in roots:
                    push(
                        parent_path,
                        root,
                        "parent-up",
                        f"{child_lbl} -> {_net_label(parent_path, root)} "
                        f"(port map {row.inst_leaf}.{port_name}={expr})",
                        target=parent_mod,
                    )
    return out


def _expand_fanin(
    state: NetState,
    ctx: _ConeCtx,
) -> List[Tuple[NetState, str, str]]:
    scope, net = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return []
    mod_idx = _cached_cone_mod(ctx, row)
    comb = mod_idx.comb
    rep = net_representative(comb, net)
    here = _net_label(scope, net)
    mod_name = row.module
    out: List[Tuple[NetState, str, str]] = []
    seen: Set[NetState] = set()

    def push(
        nxt_scope: str,
        nxt_net: str,
        kind: str,
        detail: str,
        target: Optional[ConeModuleIndex] = None,
    ) -> None:
        tmod = target or mod_idx
        key = _state_key(nxt_scope, nxt_net, tmod)
        if key not in seen:
            seen.add(key)
            out.append((key, kind, detail))

    for peer in comb.rep_adj.get(rep, ()):
        push(
            scope,
            peer,
            "intra-module",
            f"{_net_label(scope, peer)} ~ {here} (comb in {mod_name})",
        )

    if ctx.path_kind == "ff" and rep in mod_idx.ff_q_reps:
        for d_rep in mod_idx.ff_d_reps:
            push(
                scope,
                d_rep,
                "ff-interior",
                f"{_net_label(scope, d_rep)} <- {here} (always_ff D in {mod_name})",
            )

    for inst_leaf, port in comb.net_to_children.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if not child_path:
            continue
        child_row = ctx.rows_by_path.get(child_path)
        if child_row is None:
            continue
        if _is_blackbox_instance(ctx, child_row):
            push(
                child_path,
                port,
                "blackbox",
                f"{here} <- {_net_label(child_path, port)} "
                f"(blackbox {child_row.module} instance {inst_leaf})",
                target=mod_idx,
            )
            continue
        child_mod = _cached_cone_mod(ctx, child_row)
        push(
            child_path,
            port,
            "child-down",
            f"{here} <- {_net_label(child_path, port)} "
            f"(instance {inst_leaf}.{port})",
            target=child_mod,
        )

    parent_path = row.parent_path
    if parent_path:
        parent_row = ctx.rows_by_path.get(parent_path)
        if parent_row is not None:
            parent_mod = _cached_cone_mod(ctx, parent_row)
            parent_comb = parent_mod.comb
            for port_name, expr in parent_comb.inst_ports.get(row.inst_leaf, ()):
                if net_representative(comb, port_name) != rep:
                    continue
                roots = parent_comb.expr_roots.get(expr) or frozenset()
                child_lbl = _net_label(scope, port_name)
                if not roots and expr.strip():
                    push(
                        parent_path,
                        expr.strip(),
                        "parent-up",
                        f"{_net_label(parent_path, expr.strip())} -> {child_lbl} "
                        f"(port map {row.inst_leaf}.{port_name}={expr})",
                        target=parent_mod,
                    )
                for root in roots:
                    push(
                        parent_path,
                        root,
                        "parent-up",
                        f"{_net_label(parent_path, root)} -> {child_lbl} "
                        f"(port map {row.inst_leaf}.{port_name}={expr})",
                        target=parent_mod,
                    )
            for parent_rep, pairs in parent_comb.net_to_children.items():
                for inst_leaf, port in pairs:
                    if inst_leaf != row.inst_leaf:
                        continue
                    child_rep = net_representative(comb, port)
                    if child_rep != rep:
                        continue
                    push(
                        parent_path,
                        parent_rep,
                        "parent-down",
                        f"{_net_label(parent_path, parent_rep)} -> {here} "
                        f"(instance {inst_leaf}.{port})",
                        target=parent_mod,
                    )
            if _is_blackbox_instance(ctx, row):
                for port_name, _expr in parent_comb.inst_ports.get(row.inst_leaf, ()):
                    if net_representative(comb, port_name) == rep:
                        push(
                            parent_path,
                            port_name,
                            "blackbox",
                            f"blackbox driver into {here} via {parent_path}.{row.inst_leaf}",
                            target=parent_mod,
                        )

    return out


def _run_cone(
    endpoint: str,
    *,
    direction: str,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    path_kind: str = "comb",
    trace_stop: Optional[TraceStopPolicy] = None,
    ignore_hierarchy: Sequence[str] = (),
    trace_max_depth: Optional[int] = None,
) -> ConeResult:
    stop_policy = trace_stop or TraceStopPolicy(
        ignore_hierarchy=tuple(ignore_hierarchy),
        trace_max_depth=trace_max_depth,
    )
    ep, errs = resolve_endpoint(endpoint, rows, index, top=top, require_port=False)
    if errs:
        return ConeResult(
            origin_spec=endpoint,
            origin_scope=ep.inst_path,
            origin_net=ep.port_name or "",
            direction=direction,
            errors=list(errs),
        )
    rows_by_path = {r.full_path: r for r in rows}
    child_by_parent_leaf = {
        (r.parent_path, r.inst_leaf): r.full_path
        for r in rows
        if r.parent_path
    }
    ctx = _ConeCtx(
        rows_by_path=rows_by_path,
        child_by_parent_leaf=child_by_parent_leaf,
        index=index,
        top=top,
        mod_cache={},
        defines=dict(defines or {}),
        over_approximate_if=over_approximate_if,
        direction=direction,
        path_kind=path_kind,
        trace_stop=stop_policy,
        comb_cache={},
    )
    row = rows_by_path.get(ep.inst_path)
    if row is None:
        return ConeResult(
            origin_spec=endpoint,
            origin_scope=ep.inst_path,
            origin_net=ep.port_name or "",
            direction=direction,
            errors=[f"hierarchy not found: {ep.inst_path}"],
        )
    start_mod = _cached_cone_mod(ctx, row)
    start = _state_key(ep.inst_path, ep.port_name or "", start_mod)

    expand = _expand_fanout if direction == "fanout" else _expand_fanin
    visited: Set[NetState] = {start}
    frontier: List[NetState] = [start]
    boundaries: Dict[Tuple[str, str, str], ConeBoundary] = {}
    edges: List[ConeEdge] = []

    while frontier:
        next_front: List[NetState] = []
        for state in frontier:
            boundary = _boundary_at_state(
                ctx,
                state,
                is_origin=(state == start),
            )
            if boundary is not None and state != start:
                boundaries[(boundary.kind, boundary.scope, boundary.net)] = boundary
                continue
            for nxt, kind, detail in expand(state, ctx):
                edges.append(
                    ConeEdge(
                        kind,
                        detail,
                        state[0],
                        state[1],
                        nxt[0],
                        nxt[1],
                    )
                )
                b2 = _boundary_at_state(ctx, nxt, is_origin=False)
                if b2 is not None:
                    boundaries[(b2.kind, b2.scope, b2.net)] = b2
                    continue
                if nxt not in visited:
                    visited.add(nxt)
                    next_front.append(nxt)
        frontier = next_front

    if direction == "fanin":
        origin_boundary = _boundary_at_state(ctx, start, is_origin=False)
        if origin_boundary is not None and origin_boundary.kind == "port-in":
            boundaries[
                (origin_boundary.kind, origin_boundary.scope, origin_boundary.net)
            ] = origin_boundary

    return ConeResult(
        origin_spec=endpoint,
        origin_scope=start[0],
        origin_net=start[1],
        direction=direction,
        boundaries=sorted(boundaries.values(), key=lambda b: (b.kind, b.label)),
        edges=edges,
        nets_visited=len(visited),
    )


def fanout_cone(
    endpoint: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    path_kind: str = "comb",
    trace_stop: Optional[TraceStopPolicy] = None,
    ignore_hierarchy: Sequence[str] = (),
    trace_max_depth: Optional[int] = None,
) -> ConeResult:
    return _run_cone(
        endpoint,
        direction="fanout",
        rows=rows,
        index=index,
        top=top,
        defines=defines,
        over_approximate_if=over_approximate_if,
        path_kind=path_kind,
        trace_stop=trace_stop,
        ignore_hierarchy=ignore_hierarchy,
        trace_max_depth=trace_max_depth,
    )


def fanin_cone(
    endpoint: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    path_kind: str = "comb",
    trace_stop: Optional[TraceStopPolicy] = None,
    ignore_hierarchy: Sequence[str] = (),
    trace_max_depth: Optional[int] = None,
) -> ConeResult:
    return _run_cone(
        endpoint,
        direction="fanin",
        rows=rows,
        index=index,
        top=top,
        defines=defines,
        over_approximate_if=over_approximate_if,
        path_kind=path_kind,
        trace_stop=trace_stop,
        ignore_hierarchy=ignore_hierarchy,
        trace_max_depth=trace_max_depth,
    )


def format_cone_tsv(
    result: ConeResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> str:
    from hierwalk.hierarchy_log import provenance_fields

    lines = [
        "kind\tscope\tnet\tmodule\tdetail\trtl\tvia_filelist\tfilelist_chain",
    ]
    for b in result.boundaries:
        prov = (
            provenance_fields(b.scope, rows_by_path)
            if rows_by_path is not None
            else {}
        )
        lines.append(
            f"{b.kind}\t{b.scope}\t{b.net}\t{b.module}\t{b.detail}\t"
            f"{prov.get('rtl', '')}\t{prov.get('via_filelist', '')}\t"
            f"{prov.get('filelist_chain', '')}"
        )
    lines.append(f"# origin\t{result.origin_spec}")
    lines.append(f"# direction\t{result.direction}")
    lines.append(f"# nets_visited\t{result.nets_visited}")
    lines.append(f"# ff_count\t{len(result.flip_flops)}")
    lines.append(f"# port_count\t{len(result.ports)}")
    lines.append(f"# blackbox_count\t{len(result.blackboxes)}")
    if rows_by_path is not None:
        origin_prov = provenance_fields(result.origin_scope, rows_by_path)
        lines.append(f"# origin_rtl\t{origin_prov.get('rtl', '')}")
        lines.append(
            f"# origin_via_filelist\t{origin_prov.get('via_filelist', '')}"
        )
        lines.append(
            f"# origin_filelist_chain\t{origin_prov.get('filelist_chain', '')}"
        )
    if result.errors:
        lines.append(f"# errors\t{' | '.join(result.errors)}")
    return "\n".join(lines) + "\n"


def format_cone_report(
    result: ConeResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> str:
    from hierwalk.hierarchy_log import format_scope_provenance_line

    lines = [
        f"cone {result.direction}: {result.origin_spec}",
        f"visited nets: {result.nets_visited}",
    ]
    if result.errors:
        lines.append("errors:")
        lines.extend(f"  - {e}" for e in result.errors)
        return "\n".join(lines) + "\n"

    def _append_boundary(prefix: str, b: ConeBoundary) -> None:
        lines.append(prefix)
        if rows_by_path is not None and b.scope:
            lines.append(f"    {format_scope_provenance_line(b.scope, rows_by_path)}")

    if result.flip_flops:
        lines.append(f"flip-flops ({len(result.flip_flops)}):")
        for i, b in enumerate(result.flip_flops, 1):
            _append_boundary(
                f"  {i}. [{b.kind}] {b.label} ({b.module}) — {b.detail}",
                b,
            )
    if result.ports:
        lines.append(f"ports ({len(result.ports)}):")
        for i, b in enumerate(result.ports, 1):
            _append_boundary(f"  {i}. [{b.kind}] {b.label} ({b.module})", b)
    if result.blackboxes:
        lines.append(f"blackboxes ({len(result.blackboxes)}):")
        for i, b in enumerate(result.blackboxes, 1):
            _append_boundary(f"  {i}. {b.label} ({b.module})", b)
    if not result.boundaries:
        lines.append("no boundaries reached (check endpoint or expand rules)")
    return "\n".join(lines) + "\n"


def print_cone_report(
    result: ConeResult,
    *,
    stream: IO[str] = sys.stderr,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> None:
    print(
        format_cone_report(result, rows_by_path=rows_by_path),
        end="",
        file=stream,
        flush=True,
    )


def write_cone_dot(
    result: ConeResult,
    path: str,
    *,
    max_nodes: int = 200,
) -> None:
    """Write a Graphviz DOT sketch of visited cone edges."""
    nodes: Set[str] = set()
    if len(result.edges) > max_nodes * 2:
        return
    for e in result.edges:
        nodes.add(f"{e.from_scope}:{e.from_net}")
        nodes.add(f"{e.to_scope}:{e.to_net}")
    boundary_labels = {b.label: b.kind for b in result.boundaries}
    lines = ["digraph cone {", '  rankdir="LR";']
    for n in sorted(nodes):
        shape = "box" if n in boundary_labels else "ellipse"
        label = f"{n}\\n[{boundary_labels[n]}]" if n in boundary_labels else n
        lines.append(f'  "{n}" [label="{label}", shape={shape}];')
    for e in result.edges[: max_nodes * 2]:
        a = f"{e.from_scope}:{e.from_net}"
        b = f"{e.to_scope}:{e.to_net}"
        lines.append(f'  "{a}" -> "{b}" [label="{e.kind}"];')
    lines.append("}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")