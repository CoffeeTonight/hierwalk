"""Bidirectional COI search over elaborated hierarchy nets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect_endpoints import _module_index, _port_param_ctx
from hierwalk.connect_scan import (
    ModuleConnectIndex,
    _expand_concat_elements,
    _port_select_suffix,
    _range_to_bit_indices,
    net_representative,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectHop, ElabIndex, FlatRow
from hierwalk.params import resolve_param_expr

NetState = Tuple[str, str]
PrevStep = Tuple[NetState, str, str]


def _net_label(scope: str, net: str) -> str:
    return f"{scope}:{net}" if net else scope


def _cached_net_rep(
    mod_idx: ModuleConnectIndex,
    net: str,
    cache: Dict[Tuple[int, str], str],
) -> str:
    key = (id(mod_idx), net)
    hit = cache.get(key)
    if hit is not None:
        return hit
    rep = net_representative(mod_idx, net)
    cache[key] = rep
    return rep


def _cached_port_rep(
    mod_idx: ModuleConnectIndex,
    port_name: str,
    cache: Dict[Tuple[int, str], str],
) -> str:
    key = (id(mod_idx), port_name)
    hit = cache.get(key)
    if hit is not None:
        return hit
    rep = net_representative(mod_idx, port_name)
    cache[key] = rep
    return rep


def _child_port_rep_matches(
    mod_idx: ModuleConnectIndex,
    port_name: str,
    rep: str,
    *,
    child_net: str = "",
    rep_cache: Optional[Dict[Tuple[int, str], str]] = None,
) -> bool:
    """Match parent inst-port *port_name* to the child net being traced."""
    port_rep = (
        _cached_port_rep(mod_idx, port_name, rep_cache)
        if rep_cache is not None
        else net_representative(mod_idx, port_name)
    )
    if child_net:
        if child_net == port_name or child_net.startswith(port_name + "["):
            return True
        child_rep = (
            _cached_port_rep(mod_idx, child_net, rep_cache)
            if rep_cache is not None
            else net_representative(mod_idx, child_net)
        )
        if child_rep != rep or port_rep != rep:
            return False
        child_base = child_net.split("[", 1)[0]
        port_base = port_name.split("[", 1)[0]
        if child_base == port_base or child_rep == port_rep:
            return True
        peers = mod_idx.rep_adj.get(port_rep, ())
        return child_rep in peers
    if rep.startswith(port_name + "["):
        return True
    return port_rep == rep


def _parent_port_map_roots(
    port_name: str,
    expr: str,
    child_net: str,
    parent_idx: ModuleConnectIndex,
    param_map: Mapping[str, str],
) -> FrozenSet[str]:
    """Resolve parent nets for a child port-bit via instance port map *expr*."""
    text = re.sub(r"\s+", "", expr.strip())
    suffix = _port_select_suffix(port_name, child_net)
    if suffix is not None and re.match(
        r"^(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)\s*$",
        text,
    ):
        return frozenset({text + suffix})
    bit_idx: Optional[int] = None
    if suffix is not None and suffix.count("[") == 1:
        m = re.match(r"^\[([^\]]+)\]$", suffix)
        if m and ":" not in m.group(1):
            bit_idx = resolve_param_expr(m.group(1), dict(param_map))
    elif child_net.startswith(port_name + "["):
        m = re.match(rf"^{re.escape(port_name)}\[([^\]]+)\]", child_net)
        if m:
            bit_idx = resolve_param_expr(m.group(1), dict(param_map))
    if bit_idx is not None and text.startswith("{") and text.endswith("}"):
        parts = _expand_concat_elements(text[1:-1])
        if 0 <= bit_idx < len(parts):
            return frozenset({parts[bit_idx].strip()})
    if bit_idx is not None:
        m = re.match(
            r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))\[([^\]]+)\]$",
            text,
        )
        if m:
            base, sel = m.group(1), m.group(2)
            if ":" in sel:
                bits = _range_to_bit_indices(sel, param_map)
                if bits and bit_idx in bits:
                    return frozenset({f"{base}[{bit_idx}]"})
            elif resolve_param_expr(sel, dict(param_map)) == bit_idx:
                return frozenset({text})
    if bit_idx is not None and re.match(
        r"^(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)$",
        text,
    ):
        return frozenset({f"{text}[{bit_idx}]"})
    return parent_idx.expr_roots.get(expr) or frozenset()


@dataclass(frozen=True)
class _ExpandEdge:
    state: NetState
    kind: str
    detail: str


@dataclass
class _SearchCtx:
    rows_by_path: Dict[str, FlatRow]
    child_by_parent_leaf: Dict[Tuple[str, str], str]
    depth_by_path: Dict[str, int]
    dist_to_goal: Dict[str, int]
    index: DesignIndex
    top: str
    mod_cache: Dict[Tuple[str, str], ModuleConnectIndex]
    goal_scope: str
    goal_rep: str
    goal_scope_only: bool
    defines: Mapping[str, str] = field(default_factory=dict)
    param_ctx_cache: Dict[str, Mapping[str, str]] = field(default_factory=dict)
    over_approximate_if: bool = True
    ff_barrier: bool = False
    port_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)
    net_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)


def _heuristic_distance(ctx: _SearchCtx, scope: str) -> int:
    return ctx.dist_to_goal.get(scope, 10**9)


def _cached_param_ctx(ctx: _SearchCtx, row: FlatRow) -> Mapping[str, str]:
    from hierwalk.connect_endpoints import _shared_cache_lock

    path = row.full_path
    hit = ctx.param_ctx_cache.get(path)
    if hit is not None:
        return hit
    with _shared_cache_lock(ctx.param_ctx_cache, path):
        hit = ctx.param_ctx_cache.get(path)
        if hit is not None:
            return hit
        pmap = _port_param_ctx(ctx.index, row, ctx.top)
        ctx.param_ctx_cache[path] = pmap
        return pmap


def _build_search_ctx(
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    goal: NetState,
    *,
    goal_scope_only: bool,
    mod_cache: Dict[Tuple[str, str], ModuleConnectIndex],
    defines: Mapping[str, str] | None = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    elab_index: Optional[ElabIndex] = None,
) -> _SearchCtx:
    if elab_index is not None:
        rows_by_path = elab_index.rows_by_path
        child_by_parent_leaf = elab_index.child_by_parent_leaf
        depth_by_path = elab_index.depth_by_path
    else:
        rows_by_path = {r.full_path: r for r in rows}
        child_by_parent_leaf = {}
        depth_by_path = {}
        for row in rows:
            depth_by_path[row.full_path] = row.depth
            if row.parent_path:
                child_by_parent_leaf[(row.parent_path, row.inst_leaf)] = row.full_path
    goal_scope, goal_net = goal
    goal_depth = depth_by_path.get(goal_scope, 0)
    dist_to_goal = {
        path: abs(depth - goal_depth) for path, depth in depth_by_path.items()
    }
    goal_mod = rows_by_path.get(goal_scope)
    goal_rep = goal_net
    if goal_mod and goal_net:
        gctx = _port_param_ctx(index, goal_mod, top)
        gidx = _module_index(
            mod_cache,
            index,
            goal_mod.module,
            gctx,
            defines=defines,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
        )
        goal_rep = net_representative(gidx, goal_net)
    return _SearchCtx(
        rows_by_path=rows_by_path,
        child_by_parent_leaf=child_by_parent_leaf,
        depth_by_path=depth_by_path,
        dist_to_goal=dist_to_goal,
        index=index,
        top=top,
        mod_cache=mod_cache,
        goal_scope=goal_scope,
        goal_rep=goal_rep,
        goal_scope_only=goal_scope_only,
        defines=dict(defines or {}),
        param_ctx_cache=(
            param_ctx_cache
            if param_ctx_cache is not None
            else {}
        ),
        over_approximate_if=over_approximate_if,
        ff_barrier=ff_barrier,
    )


def _tree_distance(ctx: _SearchCtx, scope_a: str, scope_b: str) -> int:
    if scope_a == scope_b:
        return 0
    da = ctx.depth_by_path.get(scope_a)
    db = ctx.depth_by_path.get(scope_b)
    if da is None or db is None:
        return 10_000
    parts_a = scope_a.split(".")
    parts_b = scope_b.split(".")
    common = 0
    for x, y in zip(parts_a, parts_b):
        if x != y:
            break
        common += 1
    return da + db - 2 * common


def _state_key(
    scope: str,
    net: str,
    mod_idx: ModuleConnectIndex,
    *,
    net_rep_cache: Optional[Dict[Tuple[int, str], str]] = None,
) -> NetState:
    if net_rep_cache is not None:
        return scope, _cached_net_rep(mod_idx, net, net_rep_cache)
    return scope, net_representative(mod_idx, net)


def _goal_match(ctx: _SearchCtx, state: NetState) -> bool:
    scope, rep = state
    if ctx.goal_scope_only:
        return scope == ctx.goal_scope
    return scope == ctx.goal_scope and rep == ctx.goal_rep


def _expand_state(
    state: NetState,
    ctx: _SearchCtx,
) -> List[_ExpandEdge]:
    scope, net = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return []
    mod_ctx = _cached_param_ctx(ctx, row)
    mod_idx = _module_index(
        ctx.mod_cache,
        ctx.index,
        row.module,
        mod_ctx,
        defines=ctx.defines,
        over_approximate_if=ctx.over_approximate_if,
        ff_barrier=ctx.ff_barrier,
    )
    rep = _cached_net_rep(mod_idx, net, ctx.net_rep_cache)

    out: List[_ExpandEdge] = []
    seen_local: Set[NetState] = set()

    def push(
        nxt_scope: str,
        nxt_net: str,
        *,
        kind: str,
        detail: str,
        target_mod_idx: Optional[ModuleConnectIndex] = None,
    ) -> None:
        idx = target_mod_idx or mod_idx
        key = _state_key(nxt_scope, nxt_net, idx, net_rep_cache=ctx.net_rep_cache)
        if key not in seen_local:
            seen_local.add(key)
            out.append(_ExpandEdge(key, kind, detail))

    here = _net_label(scope, net)
    mod_name = row.module

    for peer_rep in mod_idx.rep_adj.get(rep, ()):
        push(
            scope,
            peer_rep,
            kind="intra-module",
            detail=(
                f"{here} ~ {_net_label(scope, peer_rep)} "
                f"(assign/alias/ff in module {mod_name})"
            ),
        )

    for inst_leaf, port in mod_idx.net_to_children.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if not child_path:
            continue
        child_row = ctx.rows_by_path.get(child_path)
        if child_row is None:
            continue
        child_ctx = _cached_param_ctx(ctx, child_row)
        child_idx = _module_index(
            ctx.mod_cache,
            ctx.index,
            child_row.module,
            child_ctx,
            defines=ctx.defines,
            over_approximate_if=ctx.over_approximate_if,
            ff_barrier=ctx.ff_barrier,
        )
        push(
            child_path,
            port,
            kind="child-down",
            detail=(
                f"{here} -> {_net_label(child_path, port)} "
                f"(instance {inst_leaf} port .{port} in {mod_name})"
            ),
            target_mod_idx=child_idx,
        )

    for inst_leaf, port in mod_idx.hier_links.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if not child_path:
            continue
        child_row = ctx.rows_by_path.get(child_path)
        if child_row is None:
            continue
        child_rec = ctx.index.get_module(child_row.module)
        if child_rec is not None and child_rec.is_interface:
            continue
        child_ctx = _cached_param_ctx(ctx, child_row)
        child_idx = _module_index(
            ctx.mod_cache,
            ctx.index,
            child_row.module,
            child_ctx,
            defines=ctx.defines,
            over_approximate_if=ctx.over_approximate_if,
            ff_barrier=ctx.ff_barrier,
        )
        push(
            child_path,
            port,
            kind="child-hier",
            detail=(
                f"{here} -> {_net_label(child_path, port)} "
                f"(hier ref {inst_leaf}.{port} in {mod_name})"
            ),
            target_mod_idx=child_idx,
        )

    parent_path = row.parent_path
    if parent_path:
        parent_row = ctx.rows_by_path.get(parent_path)
        if parent_row is not None:
            parent_ctx = _cached_param_ctx(ctx, parent_row)
            parent_idx = _module_index(
                ctx.mod_cache,
                ctx.index,
                parent_row.module,
                parent_ctx,
                defines=ctx.defines,
                over_approximate_if=ctx.over_approximate_if,
                ff_barrier=ctx.ff_barrier,
            )
            for port_name, expr in parent_idx.inst_ports.get(row.inst_leaf, ()):
                if not _child_port_rep_matches(
                    mod_idx,
                    port_name,
                    rep,
                    child_net=net,
                    rep_cache=ctx.port_rep_cache,
                ):
                    continue
                roots = _parent_port_map_roots(
                    port_name,
                    expr,
                    net,
                    parent_idx,
                    mod_ctx,
                )
                child_lbl = _net_label(scope, net if net != port_name else port_name)
                if not roots and expr.strip():
                    push(
                        parent_path,
                        expr.strip(),
                        kind="parent-up",
                        detail=(
                            f"{child_lbl} -> {_net_label(parent_path, expr.strip())} "
                            f"(port map {row.inst_leaf}.{port_name} = {expr} "
                            f"in parent {parent_row.module})"
                        ),
                        target_mod_idx=parent_idx,
                    )
                for root in roots:
                    push(
                        parent_path,
                        root,
                        kind="parent-up",
                        detail=(
                            f"{child_lbl} -> {_net_label(parent_path, root)} "
                            f"(port map {row.inst_leaf}.{port_name} = {expr} "
                            f"in parent {parent_row.module})"
                        ),
                        target_mod_idx=parent_idx,
                    )
            cur_rec = ctx.index.get_module(row.module)
            skip_iface_hier = cur_rec is not None and cur_rec.is_interface
            for (inst_leaf, port), parent_reps in parent_idx.hier_ref_targets.items():
                if inst_leaf != row.inst_leaf:
                    continue
                if not _child_port_rep_matches(
                    mod_idx,
                    port,
                    rep,
                    child_net=net,
                    rep_cache=ctx.port_rep_cache,
                ):
                    continue
                if skip_iface_hier:
                    continue
                child_lbl = _net_label(scope, port)
                for parent_net in parent_reps:
                    push(
                        parent_path,
                        parent_net,
                        kind="parent-hier-ref",
                        detail=(
                            f"{child_lbl} -> {_net_label(parent_path, parent_net)} "
                            f"(parent hier ref to {inst_leaf}.{port} "
                            f"in {parent_row.module})"
                        ),
                        target_mod_idx=parent_idx,
                    )

    out.sort(key=lambda e: _heuristic_distance(ctx, e.state[0]))
    return out


def _meet(
    front_a: Set[NetState],
    seen_b: Set[NetState],
    ctx: _SearchCtx,
) -> Optional[NetState]:
    for state in front_a:
        if state in seen_b:
            return state
        if ctx.goal_scope_only and state[0] == ctx.goal_scope:
            return state
    return None


def _meet_seen(
    seen_a: Set[NetState],
    seen_b: Set[NetState],
) -> Optional[NetState]:
    both = seen_a & seen_b
    if not both:
        return None
    return min(both, key=lambda s: (len(s[0]), s[0], s[1]))


def _connect_note(ok: bool, modules_parsed: int, *, hier: bool = False) -> str:
    suffix = f"; {modules_parsed} module(s)"
    if hier:
        return ("reaches hierarchy" if ok else "does not reach hierarchy") + suffix
    return ("connected" if ok else "no path") + suffix


def _resolve_over_approximate_if(
    strict_generate: bool,
    over_approximate_if: Optional[bool],
) -> bool:
    if over_approximate_if is not None:
        return over_approximate_if
    return not strict_generate


def _bidirectional_coi(
    start: NetState,
    goal: NetState,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    goal_scope_only: bool = False,
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = False,
    over_approximate_if: Optional[bool] = None,
    mod_cache: Optional[Dict[Tuple[str, str], ModuleConnectIndex]] = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    elab_index: Optional[ElabIndex] = None,
) -> Tuple[bool, List[ConnectHop], int]:
    over_approx = _resolve_over_approximate_if(strict_generate, over_approximate_if)
    cache = mod_cache if mod_cache is not None else {}
    ctx = _build_search_ctx(
        rows,
        index,
        top,
        goal,
        goal_scope_only=goal_scope_only,
        mod_cache=cache,
        defines=defines,
        param_ctx_cache=param_ctx_cache,
        over_approximate_if=over_approx,
        ff_barrier=ff_barrier,
        elab_index=elab_index,
    )

    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0
    start_ctx = _cached_param_ctx(ctx, start_row)
    start_idx = _module_index(
        cache,
        index,
        start_row.module,
        start_ctx,
        defines=defines,
        over_approximate_if=over_approx,
        ff_barrier=ff_barrier,
    )
    start_key = _state_key(
        start[0],
        start[1],
        start_idx,
        net_rep_cache=ctx.net_rep_cache,
    )

    if _goal_match(ctx, start_key):
        return True, [], len(cache)

    if goal_scope_only:
        goal_key = (goal[0], "")
    else:
        goal_row = ctx.rows_by_path.get(goal[0])
        if goal_row is None:
            return False, [], len(cache)
        goal_ctx = _cached_param_ctx(ctx, goal_row)
        goal_idx = _module_index(
            cache,
            index,
            goal_row.module,
            goal_ctx,
            defines=defines,
            over_approximate_if=over_approx,
            ff_barrier=ff_barrier,
        )
        goal_key = _state_key(
            goal[0],
            goal[1],
            goal_idx,
            net_rep_cache=ctx.net_rep_cache,
        )

    seen_f: Set[NetState] = {start_key}
    seen_b: Set[NetState] = {goal_key}
    front_f: Set[NetState] = {start_key}
    front_b: Set[NetState] = {goal_key}
    prev_f: Dict[NetState, PrevStep] = {}
    prev_b: Dict[NetState, PrevStep] = {}

    def expand_frontier(
        frontier: Set[NetState],
        seen: Set[NetState],
        prev: Dict[NetState, PrevStep],
        other_seen: Set[NetState],
        toward_scope: str,
    ) -> Set[NetState]:
        toward_depth = ctx.depth_by_path.get(toward_scope, 0)
        ordered: List[Tuple[int, int, NetState]] = []
        for state in frontier:
            scope = state[0]
            depth = ctx.depth_by_path.get(scope, 10**9)
            h = abs(depth - toward_depth)
            ordered.append((h, len(scope), state))
        ordered.sort()

        next_front: Set[NetState] = set()
        for _, _, state in ordered:
            for edge in _expand_state(state, ctx):
                nxt = edge.state
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (state, edge.kind, edge.detail)
                if nxt in other_seen:
                    return {nxt}
                next_front.add(nxt)
        return next_front

    mod_n = len(cache)

    def _done(ok: bool, hops: List[ConnectHop]) -> Tuple[bool, List[ConnectHop], int]:
        return ok, hops, len(cache)

    while front_f or front_b:
        hit = _meet(front_f, seen_b, ctx)
        if hit is not None:
            return _done(
                True,
                _reconstruct_bidirectional(hit, prev_f, prev_b, start_key, goal_key, trace),
            )
        hit = _meet(front_b, seen_f, ctx)
        if hit is not None:
            return _done(
                True,
                _reconstruct_bidirectional(hit, prev_f, prev_b, start_key, goal_key, trace),
            )

        if front_f and (not front_b or len(front_f) <= len(front_b)):
            nxt = expand_frontier(front_f, seen_f, prev_f, seen_b, ctx.goal_scope)
            if len(nxt) == 1 and next(iter(nxt)) in seen_b:
                hit = next(iter(nxt))
                return _done(
                    True,
                    _reconstruct_bidirectional(
                        hit, prev_f, prev_b, start_key, goal_key, trace
                    ),
                )
            front_f = nxt
        elif front_b:
            nxt = expand_frontier(front_b, seen_b, prev_b, seen_f, start[0])
            if len(nxt) == 1 and next(iter(nxt)) in seen_f:
                hit = next(iter(nxt))
                return _done(
                    True,
                    _reconstruct_bidirectional(
                        hit, prev_f, prev_b, start_key, goal_key, trace
                    ),
                )
            front_b = nxt
        else:
            break

    hit = _meet_seen(seen_f, seen_b)
    if hit is not None:
        return _done(
            True,
            _reconstruct_bidirectional(hit, prev_f, prev_b, start_key, goal_key, trace),
        )

    return False, [], len(cache)


def _forward_coi_to_scope(
    start: NetState,
    goal_scope: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = False,
    over_approximate_if: Optional[bool] = None,
    mod_cache: Optional[Dict[Tuple[str, str], ModuleConnectIndex]] = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    elab_index: Optional[ElabIndex] = None,
) -> Tuple[bool, List[ConnectHop], int]:
    over_approx = _resolve_over_approximate_if(strict_generate, over_approximate_if)
    cache = mod_cache if mod_cache is not None else {}
    ctx = _build_search_ctx(
        rows,
        index,
        top,
        (goal_scope, ""),
        goal_scope_only=True,
        mod_cache=cache,
        defines=defines,
        param_ctx_cache=param_ctx_cache,
        over_approximate_if=over_approx,
        ff_barrier=ff_barrier,
        elab_index=elab_index,
    )
    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0
    start_ctx = _cached_param_ctx(ctx, start_row)
    start_idx = _module_index(
        cache,
        index,
        start_row.module,
        start_ctx,
        defines=defines,
        over_approximate_if=over_approx,
        ff_barrier=ff_barrier,
    )
    start_key = _state_key(
        start[0],
        start[1],
        start_idx,
        net_rep_cache=ctx.net_rep_cache,
    )
    if start_key[0] == goal_scope:
        return True, [], len(cache)

    seen: Set[NetState] = {start_key}
    front: Set[NetState] = {start_key}
    prev: Dict[NetState, PrevStep] = {}

    while front:
        ordered = sorted(
            front,
            key=lambda s: (_heuristic_distance(ctx, s[0]), len(s[0])),
        )
        next_front: Set[NetState] = set()
        for state in ordered:
            for edge in _expand_state(state, ctx):
                nxt = edge.state
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (state, edge.kind, edge.detail)
                if nxt[0] == goal_scope:
                    mod_n = len(cache)
                    if trace:
                        hops = _reconstruct_forward(start_key, nxt, prev)
                        return True, hops, mod_n
                    return True, [ConnectHop(kind="coi", detail="structural COI path")], mod_n
                next_front.add(nxt)
        front = next_front
    return False, [], len(cache)


def _reconstruct_forward(
    start: NetState,
    end: NetState,
    prev: Mapping[NetState, PrevStep],
) -> List[ConnectHop]:
    hops: List[ConnectHop] = []
    cur = end
    while cur != start and cur in prev:
        _, kind, detail = prev[cur]
        hops.append(ConnectHop(kind=kind, detail=detail))
        cur = prev[cur][0]
    hops.reverse()
    return hops


def _reconstruct_bidirectional(
    meet: NetState,
    prev_f: Mapping[NetState, PrevStep],
    prev_b: Mapping[NetState, PrevStep],
    start: NetState,
    goal: NetState,
    trace: bool,
) -> List[ConnectHop]:
    if not trace:
        return [ConnectHop(kind="coi", detail="structural COI path")]
    hops: List[ConnectHop] = []
    cur = meet
    while cur != start and cur in prev_f:
        _, kind, detail = prev_f[cur]
        hops.append(ConnectHop(kind=kind, detail=detail))
        cur = prev_f[cur][0]
    hops.reverse()
    cur = meet
    tail: List[ConnectHop] = []
    while cur != goal and cur in prev_b:
        _, kind, detail = prev_b[cur]
        tail.append(ConnectHop(kind=kind, detail=detail))
        cur = prev_b[cur][0]
    hops.extend(tail)
    return hops
