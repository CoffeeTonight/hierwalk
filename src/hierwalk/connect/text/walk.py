"""Bidirectional text-grep walk over elaborated hierarchy nets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect.logical.scan import (
    _expand_concat_elements,
    _is_braced_concat_rhs,
    _is_compound_port_map_expr,
    _is_literal_slice_suffix,
    _port_select_suffix,
    _range_to_bit_indices,
    extract_connect_nodes,
)
from hierwalk.connect.logical.walk_log import CoiWalkDiagnostic
from hierwalk.connect.shared.endpoints import TextGrepIndexCacheKey, _port_param_ctx
from hierwalk.connect.text.index import (
    TextGrepCache,
    TextGrepIndex,
    text_grep_index,
    text_net_representative,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectHop, ElabIndex, FlatRow
from hierwalk.params import resolve_param_expr

NetState = Tuple[str, str]
PrevStep = Tuple[NetState, str, str]


def _net_label(scope: str, net: str) -> str:
    return f"{scope}:{net}" if net else scope


def _cached_net_rep(
    mod_idx: TextGrepIndex,
    net: str,
    cache: Dict[Tuple[int, str], str],
) -> str:
    key = (id(mod_idx), net)
    hit = cache.get(key)
    if hit is not None:
        return hit
    rep = text_net_representative(mod_idx, net)
    cache[key] = rep
    return rep


def _cached_port_rep(
    mod_idx: TextGrepIndex,
    port_name: str,
    cache: Dict[Tuple[int, str], str],
) -> str:
    key = (id(mod_idx), port_name)
    hit = cache.get(key)
    if hit is not None:
        return hit
    rep = text_net_representative(mod_idx, port_name)
    cache[key] = rep
    return rep


def _child_port_rep_matches(
    mod_idx: TextGrepIndex,
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
        else text_net_representative(mod_idx, port_name)
    )
    if child_net:
        if child_net == port_name or child_net.startswith(port_name + "["):
            return True
        child_rep = (
            _cached_port_rep(mod_idx, child_net, rep_cache)
            if rep_cache is not None
            else text_net_representative(mod_idx, child_net)
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
    parent_idx: TextGrepIndex,
    param_map: Mapping[str, str],
    *,
    coarse_slices: bool = True,
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
            piece = parts[bit_idx].strip()
            if _is_compound_port_map_expr(piece):
                return frozenset(extract_connect_nodes(piece, dict(param_map)))
            return frozenset({piece})
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
    roots = parent_idx.expr_roots.get(expr) or frozenset()
    if roots:
        if coarse_slices:
            if _is_braced_concat_rhs(expr) and bit_idx is None:
                return roots
            if _is_compound_port_map_expr(expr):
                if suffix and _is_literal_slice_suffix(suffix):
                    return frozenset(
                        root + suffix
                        for root in roots
                        if "[" not in root.split(".", 1)[0]
                    )
                return roots
            return roots
        if _is_braced_concat_rhs(expr) and bit_idx is None:
            return frozenset()
        if _is_compound_port_map_expr(expr):
            if suffix and _is_literal_slice_suffix(suffix):
                return frozenset(
                    root + suffix
                    for root in roots
                    if "[" not in root.split(".", 1)[0]
                )
            return frozenset()
        return roots
    return frozenset()


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
    grep_cache: Dict[TextGrepIndexCacheKey, TextGrepIndex]
    goal_scope: str
    goal_rep: str
    goal_scope_only: bool
    defines: Mapping[str, str] = field(default_factory=dict)
    param_ctx_cache: Dict[str, Mapping[str, str]] = field(default_factory=dict)
    over_approximate_if: bool = True
    port_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)
    net_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)


def _heuristic_distance(ctx: _SearchCtx, scope: str) -> int:
    return ctx.dist_to_goal.get(scope, 10**9)


def _distance_to_scope(ctx: _SearchCtx, scope: str, target_scope: str) -> int:
    if scope == target_scope:
        return 0
    depth_a = ctx.depth_by_path.get(scope, 10**9)
    depth_b = ctx.depth_by_path.get(target_scope, 10**9)
    return abs(depth_a - depth_b) + _heuristic_distance(ctx, scope)


def _pick_nearest_state(
    seen: Set[NetState],
    target_scope: str,
    ctx: _SearchCtx,
) -> Optional[NetState]:
    if not seen:
        return None
    return min(
        seen,
        key=lambda s: (
            _distance_to_scope(ctx, s[0], target_scope),
            len(s[0]),
            s[0],
            s[1],
        ),
    )


def _scopes_from_search(
    seen_f: Set[NetState],
    seen_b: Set[NetState],
    ctx: _SearchCtx,
) -> Tuple[str, ...]:
    scopes = {s[0] for s in seen_f} | {s[0] for s in seen_b}
    return tuple(sorted(scopes, key=lambda p: (ctx.depth_by_path.get(p, 10**9), p)))


def _failure_walk_diagnostic(
    *,
    seen_f: Set[NetState],
    seen_b: Set[NetState],
    prev_f: Mapping[NetState, PrevStep],
    prev_b: Mapping[NetState, PrevStep],
    start_key: NetState,
    goal_key: NetState,
    ctx: _SearchCtx,
    trace: bool,
    modules_parsed: int,
) -> CoiWalkDiagnostic:
    nearest_a = _pick_nearest_state(seen_f, goal_key[0], ctx) or start_key
    nearest_b = _pick_nearest_state(seen_b, start_key[0], ctx) or goal_key
    hops_a: List[ConnectHop] = []
    hops_b: List[ConnectHop] = []
    if nearest_a != start_key and nearest_a in prev_f:
        hops_a = _reconstruct_forward(start_key, nearest_a, prev_f)
    if nearest_b != goal_key and nearest_b in prev_b:
        hops_b = _reconstruct_forward(goal_key, nearest_b, prev_b)
    return CoiWalkDiagnostic(
        nearest_from_a=nearest_a,
        nearest_from_b=nearest_b,
        hops_from_a=tuple(hops_a),
        hops_from_b=tuple(hops_b),
        scopes_visited=_scopes_from_search(seen_f, seen_b, ctx),
        modules_parsed=modules_parsed,
    )


def _cached_param_ctx(ctx: _SearchCtx, row: FlatRow) -> Mapping[str, str]:
    from hierwalk.connect.shared.endpoints import _shared_cache_lock

    path = row.full_path
    hit = ctx.param_ctx_cache.get(path)
    if hit is not None:
        return hit
    with _shared_cache_lock(ctx.param_ctx_cache, path):
        hit = ctx.param_ctx_cache.get(path)
        if hit is not None:
            return hit
        pmap = _port_param_ctx(
            ctx.index,
            row,
            ctx.top,
            resolve_param_dims=False,
        )
        ctx.param_ctx_cache[path] = pmap
        return pmap


def _build_search_ctx(
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    goal: NetState,
    *,
    goal_scope_only: bool,
    grep_cache: Dict[TextGrepIndexCacheKey, TextGrepIndex],
    defines: Mapping[str, str] | None = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    over_approximate_if: bool = True,
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
        gctx = _port_param_ctx(
            index,
            goal_mod,
            top,
            resolve_param_dims=False,
        )
        gidx = text_grep_index(
            grep_cache,
            index,
            goal_mod.module,
            gctx,
            defines=defines,
            over_approximate_if=over_approximate_if,
        )
        goal_rep = text_net_representative(gidx, goal_net)
    return _SearchCtx(
        rows_by_path=rows_by_path,
        child_by_parent_leaf=child_by_parent_leaf,
        depth_by_path=depth_by_path,
        dist_to_goal=dist_to_goal,
        index=index,
        top=top,
        grep_cache=grep_cache,
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
    mod_idx: TextGrepIndex,
    *,
    net_rep_cache: Optional[Dict[Tuple[int, str], str]] = None,
) -> NetState:
    if net_rep_cache is not None:
        return scope, _cached_net_rep(mod_idx, net, net_rep_cache)
    return scope, text_net_representative(mod_idx, net)


def _goal_match(ctx: _SearchCtx, state: NetState) -> bool:
    scope, rep = state
    if ctx.goal_scope_only:
        return scope == ctx.goal_scope
    return scope == ctx.goal_scope and rep == ctx.goal_rep


def _text_inst_blackbox_out_roots(
    mod_idx: TextGrepIndex,
    inst_leaf: str,
    in_port: str,
) -> FrozenSet[str]:
    """
    Text grep through an instance when the child inst is not hierarchy-walked.

    Only simple (non-compound) port maps qualify — XOR/concat inst maps still
    require a child-down walk so unrelated operands are not coarse-linked.
    """
    ports = mod_idx.inst_ports.get(inst_leaf)
    if not ports:
        return frozenset()
    in_expr = ""
    for port_name, expr in ports:
        if port_name == in_port:
            in_expr = expr
            break
    if not in_expr.strip() or _is_compound_port_map_expr(in_expr):
        return frozenset()
    out_roots: Set[str] = set()
    for port_name, expr in ports:
        if port_name == in_port:
            continue
        text = re.sub(r"\s+", "", expr.strip())
        if not text or _is_compound_port_map_expr(expr):
            continue
        roots = mod_idx.expr_roots.get(expr)
        if roots:
            out_roots.update(roots)
        elif re.match(r"^(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)$", text):
            out_roots.add(text)
    return frozenset(out_roots)


def _expand_state(
    state: NetState,
    ctx: _SearchCtx,
) -> List[_ExpandEdge]:
    scope, net = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return []
    mod_ctx = _cached_param_ctx(ctx, row)
    mod_idx = text_grep_index(ctx.grep_cache,
        ctx.index,
        row.module,
        mod_ctx, defines=ctx.defines, over_approximate_if=ctx.over_approximate_if)
    rep = _cached_net_rep(mod_idx, net, ctx.net_rep_cache)

    out: List[_ExpandEdge] = []
    seen_local: Set[NetState] = set()

    def push(
        nxt_scope: str,
        nxt_net: str,
        *,
        kind: str,
        detail: str,
        target_mod_idx: Optional[TextGrepIndex] = None,
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
        if child_path:
            child_row = ctx.rows_by_path.get(child_path)
            if child_row is not None:
                child_ctx = _cached_param_ctx(ctx, child_row)
                child_idx = text_grep_index(
                    ctx.grep_cache,
                    ctx.index,
                    child_row.module,
                    child_ctx,
                    defines=ctx.defines,
                    over_approximate_if=ctx.over_approximate_if,
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
                continue
        for out_root in _text_inst_blackbox_out_roots(mod_idx, inst_leaf, port):
            push(
                scope,
                out_root,
                kind="inst-blackbox",
                detail=(
                    f"{here} ~ {_net_label(scope, out_root)} "
                    f"(text grep through {inst_leaf} port .{port} in {mod_name})"
                ),
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
        child_idx = text_grep_index(ctx.grep_cache,
            ctx.index,
            child_row.module,
            child_ctx, defines=ctx.defines, over_approximate_if=ctx.over_approximate_if)
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
            parent_idx = text_grep_index(ctx.grep_cache,
                ctx.index,
                parent_row.module,
                parent_ctx, defines=ctx.defines, over_approximate_if=ctx.over_approximate_if)
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
                    coarse_slices=True,
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


def text_connect_note(ok: bool, modules_parsed: int, *, hier: bool = False) -> str:
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


def bidirectional_text_grep(
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
    over_approximate_if: Optional[bool] = None,
    grep_cache: Optional[TextGrepCache] = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    elab_index: Optional[ElabIndex] = None,
) -> Tuple[bool, List[ConnectHop], int, Optional[CoiWalkDiagnostic]]:
    over_approx = _resolve_over_approximate_if(strict_generate, over_approximate_if)
    cache = grep_cache if grep_cache is not None else {}
    ctx = _build_search_ctx(
        rows,
        index,
        top,
        goal,
        goal_scope_only=goal_scope_only,
        grep_cache=cache,
        defines=defines,
        param_ctx_cache=param_ctx_cache,
        over_approximate_if=over_approx,
        elab_index=elab_index,
    )

    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0, None
    start_ctx = _cached_param_ctx(ctx, start_row)
    start_idx = text_grep_index(
        cache,
        index,
        start_row.module,
        start_ctx,
        defines=defines,
        over_approximate_if=over_approx,
    )
    start_key = _state_key(
        start[0],
        start[1],
        start_idx,
        net_rep_cache=ctx.net_rep_cache,
    )

    if _goal_match(ctx, start_key):
        return True, [], len(cache), None

    if goal_scope_only:
        goal_key = (goal[0], "")
    else:
        goal_row = ctx.rows_by_path.get(goal[0])
        if goal_row is None:
            return False, [], len(cache), None
        goal_ctx = _cached_param_ctx(ctx, goal_row)
        goal_idx = text_grep_index(cache,
            index,
            goal_row.module,
            goal_ctx, defines=defines, over_approximate_if=over_approx)
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

    def _done(
        ok: bool,
        hops: List[ConnectHop],
        *,
        diag: Optional[CoiWalkDiagnostic] = None,
    ) -> Tuple[bool, List[ConnectHop], int, Optional[CoiWalkDiagnostic]]:
        return ok, hops, len(cache), diag

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

    diag = _failure_walk_diagnostic(
        seen_f=seen_f,
        seen_b=seen_b,
        prev_f=prev_f,
        prev_b=prev_b,
        start_key=start_key,
        goal_key=goal_key,
        ctx=ctx,
        trace=trace,
        modules_parsed=len(cache),
    )
    return False, [], len(cache), diag


def forward_text_grep_to_scope(
    start: NetState,
    goal_scope: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None = None,
    trace: bool = False,
    strict_generate: bool = False,
    over_approximate_if: Optional[bool] = None,
    grep_cache: Optional[TextGrepCache] = None,
    param_ctx_cache: Optional[Dict[str, Mapping[str, str]]] = None,
    elab_index: Optional[ElabIndex] = None,
) -> Tuple[bool, List[ConnectHop], int, Optional[CoiWalkDiagnostic]]:
    over_approx = _resolve_over_approximate_if(strict_generate, over_approximate_if)
    cache = grep_cache if grep_cache is not None else {}
    ctx = _build_search_ctx(
        rows,
        index,
        top,
        (goal_scope, ""),
        goal_scope_only=True,
        grep_cache=cache,
        defines=defines,
        param_ctx_cache=param_ctx_cache,
        over_approximate_if=over_approx,
        elab_index=elab_index,
    )
    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0, None
    start_ctx = _cached_param_ctx(ctx, start_row)
    start_idx = text_grep_index(cache,
        index,
        start_row.module,
        start_ctx, defines=defines, over_approximate_if=over_approx)
    start_key = _state_key(
        start[0],
        start[1],
        start_idx,
        net_rep_cache=ctx.net_rep_cache,
    )
    if start_key[0] == goal_scope:
        return True, [], len(cache), None

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
                        return True, hops, mod_n, None
                    return (
                        True,
                        [ConnectHop(kind="coi", detail="structural COI path")],
                        mod_n,
                        None,
                    )
                next_front.add(nxt)
        front = next_front
    goal_key = (goal_scope, "")
    nearest = _pick_nearest_state(seen, goal_scope, ctx) or start_key
    hops_a: List[ConnectHop] = []
    if nearest != start_key and nearest in prev:
        hops_a = _reconstruct_forward(start_key, nearest, prev)
    diag = CoiWalkDiagnostic(
        nearest_from_a=nearest,
        nearest_from_b=goal_key,
        hops_from_a=tuple(hops_a),
        hops_from_b=(),
        scopes_visited=tuple(
            sorted({s[0] for s in seen}, key=lambda p: (ctx.depth_by_path.get(p, 10**9), p))
        ),
        modules_parsed=len(cache),
    )
    return False, [], len(cache), diag


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
