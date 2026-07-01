"""Bidirectional text-grep walk over elaborated hierarchy nets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect.logical.scan import (
    _expand_concat_elements,
    _is_braced_concat_rhs,
    _is_compound_port_map_expr,
    _is_const_literal,
    _is_literal_slice_suffix,
    _port_select_suffix,
    _range_to_bit_indices,
    extract_connect_nodes,
    instance_cell_types,
)
from hierwalk.connect.logical.walk_log import CoiWalkDiagnostic
from hierwalk.connect.shared.endpoints import (
    TextGrepIndexCacheKey,
    _empty_module_passthrough_ports,
    _port_param_ctx,
)
from hierwalk.connect.text.index import (
    TextGrepCache,
    TextGrepIndex,
    build_text_grep_index,
    text_grep_index,
    text_net_representative,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectHop, ElabIndex, FlatRow
from hierwalk.params import resolve_param_expr

NetState = Tuple[str, str]
PrevStep = Tuple[NetState, str, str]

_TEXT_HOP_GRANULARITY: Dict[str, str] = {
    "net-alias": "text-bloom",
    "inst-blackbox": "structural",
    "child-down": "bit-precise",
    "child-hier": "bit-precise",
    "parent-up": "bit-precise",
    "parent-hier-ref": "bit-precise",
    "intra-module": "structural",
}


def _annotate_text_hop_detail(
    kind: str,
    detail: str,
    *,
    granularity: Optional[str] = None,
) -> str:
    tag = granularity or _TEXT_HOP_GRANULARITY.get(kind)
    if tag:
        return f"{detail} [{tag}]"
    return detail


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
class TextWalkSessionCaches:
    """Cross-check text-walk memo (reused across checks in one session)."""

    port_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)
    net_rep_cache: Dict[Tuple[int, str], str] = field(default_factory=dict)
    child_text_idx_cache: Dict[
        Tuple[str, Tuple[Tuple[str, str], ...], Tuple[Tuple[str, str], ...], bool],
        TextGrepIndex,
    ] = field(default_factory=dict)
    equiv_cache: Dict[Tuple[NetState, NetState], bool] = field(default_factory=dict)
    blackbox_link_cache: Dict[Tuple[str, Tuple[Tuple[str, str], ...], str, str], bool] = (
        field(default_factory=dict)
    )
    parent_up_cache: Dict[
        Tuple[str, str, str],
        List[Tuple[str, str, str, bool, Optional[str]]],
    ] = field(default_factory=dict)
    scope_mod_idx: Dict[str, TextGrepIndex] = field(default_factory=dict)


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
    child_text_idx_cache: Dict[
        Tuple[str, Tuple[Tuple[str, str], ...], Tuple[Tuple[str, str], ...], bool],
        TextGrepIndex,
    ] = field(default_factory=dict)
    equiv_cache: Dict[Tuple[NetState, NetState], bool] = field(default_factory=dict)
    blackbox_link_cache: Dict[Tuple[str, Tuple[Tuple[str, str], ...], str, str], bool] = (
        field(default_factory=dict)
    )
    parent_up_cache: Dict[
        Tuple[str, str, str],
        List[Tuple[str, str, str, bool, Optional[str]]],
    ] = field(default_factory=dict)
    scope_mod_idx: Dict[str, TextGrepIndex] = field(default_factory=dict)


_FRONTIER_SORT_THRESHOLD = 12


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
    walk_caches: Optional[TextWalkSessionCaches] = None,
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
    wc = walk_caches
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
        port_rep_cache=wc.port_rep_cache if wc is not None else {},
        net_rep_cache=wc.net_rep_cache if wc is not None else {},
        child_text_idx_cache=wc.child_text_idx_cache if wc is not None else {},
        equiv_cache=wc.equiv_cache if wc is not None else {},
        blackbox_link_cache=wc.blackbox_link_cache if wc is not None else {},
        parent_up_cache=wc.parent_up_cache if wc is not None else {},
        scope_mod_idx=wc.scope_mod_idx if wc is not None else {},
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
    preserve_port_select: bool = False,
) -> NetState:
    if preserve_port_select and "[" in net:
        return scope, net
    if net_rep_cache is not None:
        return scope, _cached_net_rep(mod_idx, net, net_rep_cache)
    return scope, text_net_representative(mod_idx, net)


def _text_states_equivalent(
    left: NetState,
    right: NetState,
    ctx: _SearchCtx,
) -> bool:
    if left == right:
        return True
    cache_key = (left, right) if left <= right else (right, left)
    cached = ctx.equiv_cache.get(cache_key)
    if cached is not None:
        return cached
    if left[0] != right[0]:
        ctx.equiv_cache[cache_key] = False
        return False
    l_scope, l_net = left
    r_net = right[1]
    if "[" in l_net and "[" in r_net:
        ok = l_net == r_net
        ctx.equiv_cache[cache_key] = ok
        return ok
    # One side sliced, one base: bloom via net_rep (text-conn semantics).
    mod_idx = _mod_idx_for_scope(ctx, l_scope)
    if mod_idx is None:
        ctx.equiv_cache[cache_key] = False
        return False
    l_rep = _cached_net_rep(mod_idx, l_net, ctx.net_rep_cache)
    r_rep = _cached_net_rep(mod_idx, r_net, ctx.net_rep_cache)
    ok = l_rep == r_rep
    ctx.equiv_cache[cache_key] = ok
    return ok


def _goal_match(ctx: _SearchCtx, state: NetState) -> bool:
    scope, rep = state
    if ctx.goal_scope_only:
        return scope == ctx.goal_scope
    return scope == ctx.goal_scope and rep == ctx.goal_rep


def _child_text_idx_cache_key(
    cell: str,
    param_map: Mapping[str, str],
    *,
    defines: Mapping[str, str],
    over_approximate_if: bool,
) -> Tuple[str, Tuple[Tuple[str, str], ...], Tuple[Tuple[str, str], ...], bool]:
    return (
        cell,
        tuple(sorted(param_map.items())),
        tuple(sorted(defines.items())),
        over_approximate_if,
    )


def _cached_child_text_grep_index(
    ctx: _SearchCtx,
    cell: str,
    child_body: str,
    param_map: Mapping[str, str],
) -> Optional[TextGrepIndex]:
    if not child_body.strip():
        return None
    key = _child_text_idx_cache_key(
        cell,
        param_map,
        defines=ctx.defines,
        over_approximate_if=ctx.over_approximate_if,
    )
    hit = ctx.child_text_idx_cache.get(key)
    if hit is not None:
        return hit
    built = build_text_grep_index(
        child_body,
        param_map=param_map,
        defines=ctx.defines,
        over_approximate_if=ctx.over_approximate_if,
    )
    ctx.child_text_idx_cache[key] = built
    return built


def _mod_idx_for_scope(ctx: _SearchCtx, scope: str) -> Optional[TextGrepIndex]:
    hit = ctx.scope_mod_idx.get(scope)
    if hit is not None:
        return hit
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return None
    mod_ctx = _cached_param_ctx(ctx, row)
    built = text_grep_index(
        ctx.grep_cache,
        ctx.index,
        row.module,
        mod_ctx,
        defines=ctx.defines,
        over_approximate_if=ctx.over_approximate_if,
    )
    ctx.scope_mod_idx[scope] = built
    return built


def _text_child_port_links_input_or_empty_passthrough(
    ctx: _SearchCtx,
    cell: str,
    child_body: str,
    in_port: str,
    out_port: str,
    param_map: Mapping[str, str],
) -> bool:
    """Child COI link, or scalar 1-in/1-out empty-module vendor passthrough."""
    out_base = out_port.split("[", 1)[0]
    bb_key = (cell, tuple(sorted(param_map.items())), in_port, out_base)
    cached = ctx.blackbox_link_cache.get(bb_key)
    if cached is not None:
        return cached
    if not child_body.strip():
        passthrough = _empty_module_passthrough_ports(
            ctx.index,
            cell,
            param_map,
            defines=ctx.defines,
        )
        if not passthrough:
            ctx.blackbox_link_cache[bb_key] = False
            return False
        in_base = in_port.split("[", 1)[0]
        ok = in_base == passthrough[0] and out_base == passthrough[1]
        ctx.blackbox_link_cache[bb_key] = ok
        return ok
    child_idx = _cached_child_text_grep_index(ctx, cell, child_body, param_map)
    if child_idx is None:
        ctx.blackbox_link_cache[bb_key] = False
        return False
    ok = _text_child_port_links_input(child_idx, in_port, out_base)
    ctx.blackbox_link_cache[bb_key] = ok
    return ok


def _text_child_port_links_input(
    child_idx: TextGrepIndex,
    in_port: str,
    out_port: str,
) -> bool:
    """True when child text-grep COI links *out_port* back to *in_port*."""
    in_rep = text_net_representative(child_idx, in_port)
    out_rep = text_net_representative(child_idx, out_port)
    if in_rep == out_rep:
        return True
    seen: Set[str] = {in_rep}
    frontier: List[str] = [in_rep]
    while frontier:
        cur = frontier.pop()
        for peer in child_idx.rep_adj.get(cur, ()):
            if peer == out_rep:
                return True
            if peer in seen:
                continue
            seen.add(peer)
            frontier.append(peer)
    return False


def _text_inst_blackbox_out_roots(
    ctx: _SearchCtx,
    row: FlatRow,
    mod_idx: TextGrepIndex,
    inst_leaf: str,
    in_port: str,
) -> FrozenSet[str]:
    """
    Text grep through an instance when the child inst is not hierarchy-walked.

    Only simple (non-compound) port maps qualify — XOR/concat inst maps still
    require a child-down walk so unrelated operands are not coarse-linked.
    Output ports must be driven from *in_port* in the child module body so
    const-tied or unrelated outputs (e.g. ``decoy_out = 12'b0``) are excluded.
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

    pmap = _cached_param_ctx(ctx, row)
    parent_rec = ctx.index.get_module(row.module)
    parent_body = ctx.index.module_body(row.module) if parent_rec else ""
    cell = instance_cell_types(parent_body, param_map=pmap).get(inst_leaf, "")
    child_body = ""
    if cell:
        child_rec = ctx.index.get_module(cell)
        child_body = ctx.index.module_body(cell) if child_rec else ""
    out_roots: Set[str] = set()
    for port_name, expr in ports:
        if port_name == in_port:
            continue
        port_base = port_name.split("[", 1)[0]
        if not cell or not _text_child_port_links_input_or_empty_passthrough(
            ctx,
            cell,
            child_body,
            in_port,
            port_base,
            pmap,
        ):
            continue
        text = re.sub(r"\s+", "", expr.strip())
        if not text or _is_compound_port_map_expr(expr) or _is_const_literal(expr, pmap):
            continue
        roots = mod_idx.expr_roots.get(expr)
        if roots:
            out_roots.update(roots)
        elif re.match(r"^(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)$", text):
            out_roots.add(text)
    return frozenset(out_roots)


def _compute_parent_up_targets(
    ctx: _SearchCtx,
    scope: str,
    net: str,
    rep: str,
    row: FlatRow,
    mod_idx: TextGrepIndex,
    mod_ctx: Mapping[str, str],
) -> List[Tuple[str, str, bool, Optional[str]]]:
    """Return cached parent-up rows: (target_net, detail, preserve_sel, granularity)."""
    parent_path = row.parent_path
    if not parent_path:
        return []
    parent_row = ctx.rows_by_path.get(parent_path)
    if parent_row is None:
        return []
    parent_idx = _mod_idx_for_scope(ctx, parent_path)
    if parent_idx is None:
        return []
    targets: List[Tuple[str, str, bool, Optional[str]]] = []
    child_lbl = _net_label(scope, net)
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
        lbl = _net_label(scope, net if net != port_name else port_name)
        if not roots and expr.strip():
            targets.append(
                (
                    expr.strip(),
                    (
                        f"{lbl} -> {_net_label(parent_path, expr.strip())} "
                        f"(port map {row.inst_leaf}.{port_name} = {expr} "
                        f"in parent {parent_row.module})"
                    ),
                    False,
                    "structural",
                )
            )
        for root in roots:
            targets.append(
                (
                    root,
                    (
                        f"{lbl} -> {_net_label(parent_path, root)} "
                        f"(port map {row.inst_leaf}.{port_name} = {expr} "
                        f"in parent {parent_row.module})"
                    ),
                    True,
                    None,
                )
            )
    return targets


def _expand_state(
    state: NetState,
    ctx: _SearchCtx,
) -> List[_ExpandEdge]:
    scope, net = state
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return []
    mod_idx = _mod_idx_for_scope(ctx, scope)
    if mod_idx is None:
        return []
    mod_ctx = _cached_param_ctx(ctx, row)
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
        preserve_port_select: bool = False,
        granularity: Optional[str] = None,
    ) -> None:
        idx = target_mod_idx or mod_idx
        key = _state_key(
            nxt_scope,
            nxt_net,
            idx,
            net_rep_cache=ctx.net_rep_cache,
            preserve_port_select=preserve_port_select,
        )
        if key not in seen_local:
            seen_local.add(key)
            out.append(
                _ExpandEdge(
                    key,
                    kind,
                    _annotate_text_hop_detail(kind, detail, granularity=granularity),
                )
            )

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

    alias = mod_idx.net_rep.get(net) or mod_idx.net_rep.get(rep)
    if alias and alias not in {net, rep}:
        push(
            scope,
            alias,
            kind="net-alias",
            detail=(
                f"{here} ~ {_net_label(scope, alias)} "
                f"(net_rep alias in module {mod_name})"
            ),
        )

    for inst_leaf, port in mod_idx.net_to_children.get(rep, ()):
        child_path = ctx.child_by_parent_leaf.get((scope, inst_leaf))
        if child_path:
            child_row = ctx.rows_by_path.get(child_path)
            if child_row is not None:
                child_idx = _mod_idx_for_scope(ctx, child_path)
                if child_idx is None:
                    continue
                push(
                    child_path,
                    port,
                    kind="child-down",
                    detail=(
                        f"{here} -> {_net_label(child_path, port)} "
                        f"(instance {inst_leaf} port .{port} in {mod_name})"
                    ),
                    target_mod_idx=child_idx,
                    preserve_port_select=True,
                )
                continue
        for out_root in _text_inst_blackbox_out_roots(ctx, row, mod_idx, inst_leaf, port):
            push(
                scope,
                out_root,
                kind="inst-blackbox",
                detail=(
                    f"{here} ~ {_net_label(scope, out_root)} "
                    f"(text grep through {inst_leaf} port .{port} in {mod_name})"
                ),
                preserve_port_select=True,
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
        child_idx = _mod_idx_for_scope(ctx, child_path)
        if child_idx is None:
            continue
        push(
            child_path,
            port,
            kind="child-hier",
            detail=(
                f"{here} -> {_net_label(child_path, port)} "
                f"(hier ref {inst_leaf}.{port} in {mod_name})"
            ),
            target_mod_idx=child_idx,
            preserve_port_select=True,
        )

    pu_key = (scope, rep, net)
    parent_targets = ctx.parent_up_cache.get(pu_key)
    if parent_targets is None:
        parent_targets = _compute_parent_up_targets(
            ctx,
            scope,
            net,
            rep,
            row,
            mod_idx,
            mod_ctx,
        )
        ctx.parent_up_cache[pu_key] = parent_targets
    parent_path = row.parent_path or ""
    parent_idx = _mod_idx_for_scope(ctx, parent_path) if parent_path else None
    for target_net, detail, preserve_sel, granularity in parent_targets:
        push(
            parent_path,
            target_net,
            kind="parent-up",
            detail=detail,
            target_mod_idx=parent_idx,
            preserve_port_select=preserve_sel,
            granularity=granularity,
        )
    if parent_path and parent_idx is not None:
        parent_row = ctx.rows_by_path.get(parent_path)
        if parent_row is not None:
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


def _equiv_rep_key(
    state: NetState,
    ctx: _SearchCtx,
    *,
    mod_idx: Optional[TextGrepIndex] = None,
) -> Optional[Tuple[str, str]]:
    scope, net = state
    if "[" in net:
        return None
    if mod_idx is None:
        mod_idx = _mod_idx_for_scope(ctx, scope)
    if mod_idx is None:
        return None
    rep = _cached_net_rep(mod_idx, net, ctx.net_rep_cache)
    return (scope, rep)


@dataclass
class _SeenRepIndex:
    by_exact: Dict[Tuple[str, str], Set[NetState]] = field(default_factory=dict)
    by_rep: Dict[Tuple[str, str], Set[NetState]] = field(default_factory=dict)

    def add(
        self,
        state: NetState,
        ctx: _SearchCtx,
        *,
        mod_idx: Optional[TextGrepIndex] = None,
    ) -> None:
        scope, net = state
        self.by_exact.setdefault((scope, net), set()).add(state)
        key = _equiv_rep_key(state, ctx, mod_idx=mod_idx)
        if key is not None:
            self.by_rep.setdefault(key, set()).add(state)

    def find_in(
        self,
        state: NetState,
        candidates: Set[NetState],
        ctx: _SearchCtx,
        *,
        mod_idx: Optional[TextGrepIndex] = None,
    ) -> Optional[NetState]:
        scope, net = state
        overlap = self.by_exact.get((scope, net), set()) & candidates
        if overlap:
            return min(overlap, key=lambda s: (len(s[1]), s[1]))
        key = _equiv_rep_key(state, ctx, mod_idx=mod_idx)
        if key is not None:
            overlap = self.by_rep.get(key, set()) & candidates
            if overlap:
                return min(overlap, key=lambda s: (len(s[1]), s[1]))
        return None


def _find_equivalent_in(
    state: NetState,
    candidates: Set[NetState],
    ctx: _SearchCtx,
    *,
    rep_index: Optional[_SeenRepIndex] = None,
) -> Optional[NetState]:
    if state in candidates:
        return state
    if rep_index is not None:
        hit = rep_index.find_in(state, candidates, ctx)
        if hit is not None:
            return hit
    for other in candidates:
        if _text_states_equivalent(state, other, ctx):
            return other
    return None


def _find_equivalent_with_prev(
    meet: NetState,
    seen: Set[NetState],
    prev: Mapping[NetState, PrevStep],
    endpoint: NetState,
    ctx: _SearchCtx,
) -> Optional[NetState]:
    if meet == endpoint or meet in prev:
        return meet
    for state in seen:
        if state not in prev and state != endpoint:
            continue
        if state == meet or _text_states_equivalent(meet, state, ctx):
            return state
    return None


def _resolve_trace_meets(
    meet: NetState,
    start: NetState,
    goal: NetState,
    prev_f: Mapping[NetState, PrevStep],
    prev_b: Mapping[NetState, PrevStep],
    seen_f: Set[NetState],
    seen_b: Set[NetState],
    ctx: _SearchCtx,
) -> Tuple[NetState, NetState]:
    meet_f = _find_equivalent_with_prev(meet, seen_f, prev_f, start, ctx) or meet
    meet_b = _find_equivalent_with_prev(meet, seen_b, prev_b, goal, ctx) or meet
    for state in seen_f & seen_b:
        if not _text_states_equivalent(meet, state, ctx):
            continue
        if meet_f not in prev_f and meet_f != start and (
            state in prev_f or state == start
        ):
            meet_f = state
        if meet_b not in prev_b and meet_b != goal and (
            state in prev_b or state == goal
        ):
            meet_b = state
    return meet_f, meet_b


def _meet(
    front_a: Set[NetState],
    seen_b: Set[NetState],
    ctx: _SearchCtx,
    *,
    seen_b_rep: Optional[_SeenRepIndex] = None,
) -> Optional[NetState]:
    for state in front_a:
        if state in seen_b:
            return state
        witness = _find_equivalent_in(
            state,
            seen_b,
            ctx,
            rep_index=seen_b_rep,
        )
        if witness is not None:
            return witness
        if ctx.goal_scope_only and state[0] == ctx.goal_scope:
            return state
    return None


def _meet_seen(
    seen_a: Set[NetState],
    seen_b: Set[NetState],
    ctx: _SearchCtx,
    *,
    seen_b_rep: Optional[_SeenRepIndex] = None,
) -> Optional[NetState]:
    both = seen_a & seen_b
    if both:
        return min(both, key=lambda s: (len(s[0]), s[0], s[1]))
    for state in seen_a:
        witness = _find_equivalent_in(
            state,
            seen_b,
            ctx,
            rep_index=seen_b_rep,
        )
        if witness is not None:
            return witness
    return None


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
    walk_caches: Optional[TextWalkSessionCaches] = None,
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
        walk_caches=walk_caches,
    )

    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0, None
    start_idx = _mod_idx_for_scope(ctx, start[0])
    if start_idx is None:
        return False, [], 0, None
    start_key: NetState = (start[0], start[1])
    if not start[1]:
        start_key = _state_key(
            start[0],
            start[1],
            start_idx,
            net_rep_cache=ctx.net_rep_cache,
        )

    if _goal_match(ctx, _state_key(
        start[0],
        start[1],
        start_idx,
        net_rep_cache=ctx.net_rep_cache,
    )):
        return True, [], len(cache), None

    if goal_scope_only:
        goal_key = (goal[0], "")
    else:
        goal_row = ctx.rows_by_path.get(goal[0])
        if goal_row is None:
            return False, [], len(cache), None
        goal_key = (goal[0], goal[1])
        if not goal[1]:
            goal_idx = _mod_idx_for_scope(ctx, goal[0])
            if goal_idx is None:
                return False, [], len(cache), None
            goal_key = _state_key(
                goal[0],
                goal[1],
                goal_idx,
                net_rep_cache=ctx.net_rep_cache,
            )

    seen_f: Set[NetState] = {start_key}
    seen_b: Set[NetState] = {goal_key}
    seen_f_rep = _SeenRepIndex()
    seen_b_rep = _SeenRepIndex()
    seen_f_rep.add(start_key, ctx, mod_idx=start_idx)
    seen_b_rep.add(goal_key, ctx, mod_idx=_mod_idx_for_scope(ctx, goal_key[0]))
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
        *,
        rep_index: _SeenRepIndex,
        other_rep_index: _SeenRepIndex,
    ) -> Set[NetState]:
        toward_depth = ctx.depth_by_path.get(toward_scope, 0)
        ordered: List[Tuple[int, int, NetState]] = []
        for state in frontier:
            scope = state[0]
            depth = ctx.depth_by_path.get(scope, 10**9)
            h = abs(depth - toward_depth)
            ordered.append((h, len(scope), state))
        if len(ordered) > _FRONTIER_SORT_THRESHOLD:
            ordered.sort()

        next_front: Set[NetState] = set()
        for _, _, state in ordered:
            for edge in _expand_state(state, ctx):
                nxt = edge.state
                if nxt in seen:
                    continue
                seen.add(nxt)
                rep_index.add(
                    nxt,
                    ctx,
                    mod_idx=_mod_idx_for_scope(ctx, nxt[0]),
                )
                prev[nxt] = (state, edge.kind, edge.detail)
                if _find_equivalent_in(
                    nxt,
                    other_seen,
                    ctx,
                    rep_index=other_rep_index,
                ) is not None:
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
        hit = _meet(front_f, seen_b, ctx, seen_b_rep=seen_b_rep)
        if hit is not None:
            return _done(
                True,
                _reconstruct_bidirectional(
                    hit,
                    prev_f,
                    prev_b,
                    start_key,
                    goal_key,
                    trace,
                    seen_f=seen_f,
                    seen_b=seen_b,
                    ctx=ctx,
                ),
            )
        hit = _meet(front_b, seen_f, ctx, seen_b_rep=seen_f_rep)
        if hit is not None:
            return _done(
                True,
                _reconstruct_bidirectional(
                    hit,
                    prev_f,
                    prev_b,
                    start_key,
                    goal_key,
                    trace,
                    seen_f=seen_f,
                    seen_b=seen_b,
                    ctx=ctx,
                ),
            )

        if front_f and (not front_b or len(front_f) <= len(front_b)):
            nxt = expand_frontier(
                front_f,
                seen_f,
                prev_f,
                seen_b,
                ctx.goal_scope,
                rep_index=seen_f_rep,
                other_rep_index=seen_b_rep,
            )
            if len(nxt) == 1:
                hit = next(iter(nxt))
                if _find_equivalent_in(
                    hit,
                    seen_b,
                    ctx,
                    rep_index=seen_b_rep,
                ) is not None:
                    return _done(
                        True,
                        _reconstruct_bidirectional(
                            hit,
                            prev_f,
                            prev_b,
                            start_key,
                            goal_key,
                            trace,
                            seen_f=seen_f,
                            seen_b=seen_b,
                            ctx=ctx,
                        ),
                    )
            front_f = nxt
        elif front_b:
            nxt = expand_frontier(
                front_b,
                seen_b,
                prev_b,
                seen_f,
                start[0],
                rep_index=seen_b_rep,
                other_rep_index=seen_f_rep,
            )
            if len(nxt) == 1:
                hit = next(iter(nxt))
                if _find_equivalent_in(
                    hit,
                    seen_f,
                    ctx,
                    rep_index=seen_f_rep,
                ) is not None:
                    return _done(
                        True,
                        _reconstruct_bidirectional(
                            hit,
                            prev_f,
                            prev_b,
                            start_key,
                            goal_key,
                            trace,
                            seen_f=seen_f,
                            seen_b=seen_b,
                            ctx=ctx,
                        ),
                    )
            front_b = nxt
        else:
            break

    hit = _meet_seen(seen_f, seen_b, ctx, seen_b_rep=seen_b_rep)
    if hit is not None:
        return _done(
            True,
            _reconstruct_bidirectional(
                hit,
                prev_f,
                prev_b,
                start_key,
                goal_key,
                trace,
                seen_f=seen_f,
                seen_b=seen_b,
                ctx=ctx,
            ),
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
    walk_caches: Optional[TextWalkSessionCaches] = None,
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
        walk_caches=walk_caches,
    )
    start_row = ctx.rows_by_path.get(start[0])
    if start_row is None:
        return False, [], 0, None
    start_idx = _mod_idx_for_scope(ctx, start[0])
    if start_idx is None:
        return False, [], 0, None
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
        if len(front) > _FRONTIER_SORT_THRESHOLD:
            ordered = sorted(
                front,
                key=lambda s: (_heuristic_distance(ctx, s[0]), len(s[0])),
            )
        else:
            ordered = list(front)
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
    *,
    seen_f: Optional[Set[NetState]] = None,
    seen_b: Optional[Set[NetState]] = None,
    ctx: Optional[_SearchCtx] = None,
) -> List[ConnectHop]:
    if not trace:
        return [ConnectHop(kind="coi", detail="structural COI path")]
    meet_f = meet
    meet_b = meet
    if ctx is not None and seen_f is not None and seen_b is not None:
        meet_f, meet_b = _resolve_trace_meets(
            meet,
            start,
            goal,
            prev_f,
            prev_b,
            seen_f,
            seen_b,
            ctx,
        )
    hops: List[ConnectHop] = []
    cur = meet_f
    while cur != start and cur in prev_f:
        _, kind, detail = prev_f[cur]
        hops.append(ConnectHop(kind=kind, detail=detail))
        cur = prev_f[cur][0]
    hops.reverse()
    cur = meet_b
    tail: List[ConnectHop] = []
    while cur != goal and cur in prev_b:
        _, kind, detail = prev_b[cur]
        tail.append(ConnectHop(kind=kind, detail=detail))
        cur = prev_b[cur][0]
    hops.extend(tail)
    return hops
