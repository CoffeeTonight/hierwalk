"""Waypoint-qualified fanout trace for connectivity checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from hierwalk.cone import (
    ConeModuleIndex,
    _ConeCtx,
    _boundary_at_state,
    _cached_cone_mod,
    _expand_fanout,
    _state_key,
)
from hierwalk.connect_endpoints import resolve_endpoint
from hierwalk.connect_scan import ModuleConnectIndex, lookup_edge_prov, net_representative
from hierwalk.index import DesignIndex
from hierwalk.inst_trace import _ports_for_instance
from hierwalk.models import ConnectEndpoint, ConnectResult, ElabIndex, FlatRow
NetState = Tuple[str, str]
BfsState = Tuple[str, str, bool]


@dataclass(frozen=True)
class WaypointFanoutEvent:
    source: str
    event_kind: str
    scope: str
    net: str
    rtl_file: str
    rtl_line: int
    waypoint_hit: str
    waypoint_qualified: str
    is_terminator: str
    side: str = ""
    peer_matched: str = ""
    path_kind: str = ""


@dataclass
class WaypointSet:
    port_nets: Set[Tuple[str, str]] = field(default_factory=set)
    inst_prefixes: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ResolvedPeer:
    spec: str
    inst_path: str
    port_name: str
    is_inst: bool


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _is_inst_spec(spec: str, ep: ConnectEndpoint, rows_by_path: Mapping[str, FlatRow]) -> bool:
    text = spec.strip()
    if text in rows_by_path:
        return True
    return text == ep.inst_path and not ep.port_name


def normalize_waypoints(
    specs: Sequence[str],
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> Tuple[WaypointSet, List[str], Tuple[_ResolvedPeer, ...]]:
    lookup = rows_by_path if rows_by_path is not None else {r.full_path: r for r in rows}
    waypoints = WaypointSet()
    errors: List[str] = []
    resolved: List[_ResolvedPeer] = []
    for spec in specs:
        text = str(spec).strip()
        if not text:
            errors.append("waypoint spec must be non-empty")
            continue
        ep, ep_errs = resolve_endpoint(
            text,
            rows,
            index,
            top=top,
            require_port=False,
            rows_by_path=lookup,
        )
        errors.extend(ep_errs)
        if ep_errs:
            continue
        is_inst = _is_inst_spec(text, ep, lookup)
        resolved.append(
            _ResolvedPeer(
                spec=text,
                inst_path=ep.inst_path,
                port_name=ep.port_name or "",
                is_inst=is_inst,
            )
        )
        if is_inst:
            waypoints.inst_prefixes.add(ep.inst_path)
            continue
        if ep.port_name:
            waypoints.port_nets.add((ep.inst_path, ep.port_name))
        else:
            errors.append(f"waypoint not resolved: {text}")
    return waypoints, errors, tuple(resolved)


def _origins_for_resolved_peer(
    peer: _ResolvedPeer,
    *,
    index: DesignIndex,
    lookup: Mapping[str, FlatRow],
    top: str,
) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    row = lookup.get(peer.inst_path)
    if row is None:
        return [], [f"hierarchy not found: {peer.inst_path}"]
    if peer.is_inst:
        ports = _ports_for_instance(index, row, top)
        if not ports:
            return [], [f"no ports for instance origin {peer.spec}"]
        origins: List[Tuple[str, str, str]] = []
        for port_name, port_dir in ports:
            if port_dir in ("output", "inout", "input"):
                label = f"{peer.inst_path}.{port_name}"
                origins.append((label, peer.inst_path, port_name))
        return origins, []
    if peer.port_name:
        return [(peer.spec, peer.inst_path, peer.port_name)], []
    return [], [f"fanout origin not resolved: {peer.spec}"]


def expand_fanout_origins(
    specs: Sequence[str],
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    resolved_peers: Optional[Sequence[_ResolvedPeer]] = None,
) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """Return (source_label, scope, net) fanout seeds."""
    lookup = rows_by_path if rows_by_path is not None else {r.full_path: r for r in rows}
    peer_by_spec = (
        {peer.spec: peer for peer in resolved_peers}
        if resolved_peers is not None
        else {}
    )
    origins: List[Tuple[str, str, str]] = []
    errors: List[str] = []
    for spec in specs:
        text = str(spec).strip()
        if not text:
            errors.append("fanout origin must be non-empty")
            continue
        peer = peer_by_spec.get(text)
        if peer is not None:
            part_origins, part_errs = _origins_for_resolved_peer(
                peer,
                index=index,
                lookup=lookup,
                top=top,
            )
            origins.extend(part_origins)
            errors.extend(part_errs)
            continue
        ep, ep_errs = resolve_endpoint(
            text,
            rows,
            index,
            top=top,
            require_port=False,
            rows_by_path=lookup,
        )
        errors.extend(ep_errs)
        if ep_errs:
            continue
        row = lookup.get(ep.inst_path)
        if row is None:
            errors.append(f"hierarchy not found: {ep.inst_path}")
            continue
        if _is_inst_spec(text, ep, lookup):
            ports = _ports_for_instance(index, row, top)
            if not ports:
                errors.append(f"no ports for instance origin {text}")
                continue
            for port_name, port_dir in ports:
                label = f"{ep.inst_path}.{port_name}"
                if port_dir in ("output", "inout"):
                    origins.append((label, ep.inst_path, port_name))
                elif port_dir == "input":
                    origins.append((label, ep.inst_path, port_name))
            continue
        origins.append((text, ep.inst_path, ep.port_name or ""))
    return origins, errors


def _waypoint_hit(
    scope: str,
    net: str,
    waypoints: WaypointSet,
) -> bool:
    if (scope, net) in waypoints.port_nets:
        return True
    for prefix in waypoints.inst_prefixes:
        if scope == prefix or scope.startswith(prefix + "."):
            return True
    return False


def _resolve_peer_specs(
    peer_specs: Sequence[str],
    rows: Sequence[FlatRow],
    rows_by_path: Mapping[str, FlatRow],
    index: DesignIndex,
    top: str,
) -> Tuple[_ResolvedPeer, ...]:
    out: List[_ResolvedPeer] = []
    for spec in peer_specs:
        text = str(spec).strip()
        if not text:
            continue
        ep, ep_errs = resolve_endpoint(
            text,
            rows,
            index,
            top=top,
            require_port=False,
            rows_by_path=rows_by_path,
        )
        if ep_errs:
            continue
        out.append(
            _ResolvedPeer(
                spec=text,
                inst_path=ep.inst_path,
                port_name=ep.port_name or "",
                is_inst=_is_inst_spec(text, ep, rows_by_path),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class _PeerHitIndex:
    inst_specs: Tuple[Tuple[str, str], ...]
    port_specs: Dict[Tuple[str, str], Tuple[str, ...]]


def _build_peer_hit_index(resolved_peers: Sequence[_ResolvedPeer]) -> _PeerHitIndex:
    inst_specs: List[Tuple[str, str]] = []
    port_specs: Dict[Tuple[str, str], List[str]] = {}
    for peer in resolved_peers:
        if peer.is_inst:
            inst_specs.append((peer.inst_path, peer.spec))
        elif peer.port_name:
            port_specs.setdefault((peer.inst_path, peer.port_name), []).append(
                peer.spec
            )
    return _PeerHitIndex(
        inst_specs=tuple(inst_specs),
        port_specs={key: tuple(vals) for key, vals in port_specs.items()},
    )


def _peer_labels_for_hit(
    scope: str,
    net: str,
    peer_index: _PeerHitIndex,
    *,
    peers: WaypointSet,
    already_hit: Optional[bool] = None,
) -> str:
    if not peer_index.inst_specs and not peer_index.port_specs:
        return "-"
    if already_hit is None:
        hit = _waypoint_hit(scope, net, peers)
    else:
        hit = already_hit
    if not hit:
        return "-"
    labels: List[str] = []
    port_labels = peer_index.port_specs.get((scope, net))
    if port_labels:
        labels.extend(port_labels)
    for inst_path, spec in peer_index.inst_specs:
        if scope == inst_path or scope.startswith(inst_path + "."):
            labels.append(spec)
    if labels:
        return ",".join(labels)
    return f"{scope}.{net}" if net else scope


def _ff_net_line(mod_idx, net: str) -> int:
    comb = mod_idx.comb
    rep = net_representative(comb, net)
    return comb.ff_net_lines.get(net, comb.ff_net_lines.get(rep, 0))


def _rtl_line_for_edge(
    ctx: _ConeCtx,
    from_scope: str,
    from_net: str,
    to_scope: str,
    to_net: str,
    event_kind: str,
    inst_leaf: str = "",
) -> int:
    if event_kind in ("ff-interior", "ff-sink", "ff-driver"):
        for scope, net in ((from_scope, from_net), (to_scope, to_net)):
            row = ctx.rows_by_path.get(scope)
            if row is None:
                continue
            mod_idx = _cached_cone_mod(ctx, row)
            line = _ff_net_line(mod_idx, net)
            if line:
                return line
    if event_kind in ("child-down", "child-hier") and inst_leaf:
        row = ctx.rows_by_path.get(from_scope)
        if row is None:
            return 0
        mod_idx = _cached_cone_mod(ctx, row)
        return mod_idx.comb.inst_stmt_lines.get(inst_leaf, 0)
    scope = from_scope if event_kind != "parent-up" else to_scope
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return 0
    mod_idx = _cached_cone_mod(ctx, row)
    prov = lookup_edge_prov(mod_idx.comb, from_net, to_net)
    return prov.line if prov is not None else 0


def _rtl_line_for_boundary(
    ctx: _ConeCtx,
    scope: str,
    net: str,
    event_kind: str,
) -> int:
    row = ctx.rows_by_path.get(scope)
    if row is None:
        return 0
    mod_idx = _cached_cone_mod(ctx, row)
    if event_kind.startswith("ff-"):
        line = _ff_net_line(mod_idx, net)
        if line:
            return line
    return 0


def _trace_origin_fanout(
    source: str,
    start_scope: str,
    start_net: str,
    *,
    ctx: _ConeCtx,
    waypoints: WaypointSet,
    rows_by_path: Mapping[str, FlatRow],
    side: str = "",
    peer_index: Optional[_PeerHitIndex] = None,
    path_kind: str = "",
    emit_interior: bool = False,
) -> List[WaypointFanoutEvent]:
    row = rows_by_path.get(start_scope)
    if row is None:
        return []
    start_mod = _cached_cone_mod(ctx, row)
    start = _state_key(start_scope, start_net, start_mod)
    start_qualified = _waypoint_hit(start[0], start[1], waypoints)

    visited: Set[BfsState] = {(start[0], start[1], start_qualified)}
    frontier: List[BfsState] = [(start[0], start[1], start_qualified)]
    events: List[WaypointFanoutEvent] = []

    while frontier:
        next_front: List[BfsState] = []
        for scope, net, qualified in frontier:
            state: NetState = (scope, net)
            boundary = _boundary_at_state(
                ctx,
                state,
                is_origin=(state == start),
            )
            if boundary is not None and state != start:
                hit = _waypoint_hit(scope, net, waypoints)
                qual = qualified or hit
                row_hit = rows_by_path.get(scope)
                peer = (
                    _peer_labels_for_hit(
                        scope,
                        net,
                        peer_index,
                        peers=waypoints,
                        already_hit=hit,
                    )
                    if peer_index is not None
                    else "-"
                )
                events.append(
                    WaypointFanoutEvent(
                        source=source,
                        event_kind=boundary.kind,
                        scope=scope,
                        net=net,
                        rtl_file=row_hit.file if row_hit else "",
                        rtl_line=_rtl_line_for_boundary(
                            ctx, scope, net, boundary.kind
                        ),
                        waypoint_hit=_yn(hit),
                        waypoint_qualified=_yn(qual),
                        is_terminator="Y",
                        side=side,
                        peer_matched=peer,
                        path_kind=path_kind,
                    )
                )
                continue

            for nxt, kind, detail in _expand_fanout(state, ctx):
                nxt_scope, nxt_net = nxt
                hit = _waypoint_hit(nxt_scope, nxt_net, waypoints)
                qual = qualified or hit
                inst_leaf = ""
                if kind in ("child-down", "child-hier"):
                    parent_row = rows_by_path.get(scope)
                    if parent_row is not None:
                        suffix = nxt_scope[len(scope) + 1 :] if nxt_scope.startswith(scope + ".") else ""
                        inst_leaf = suffix.split(".", 1)[0] if suffix else ""
                rtl_line = _rtl_line_for_edge(
                    ctx,
                    scope,
                    net,
                    nxt_scope,
                    nxt_net,
                    kind,
                    inst_leaf=inst_leaf,
                )
                row_nxt = rows_by_path.get(nxt_scope)
                if emit_interior or (
                    ctx.path_kind == "ff" and kind.startswith("ff-")
                ):
                    events.append(
                        WaypointFanoutEvent(
                            source=source,
                            event_kind=kind,
                            scope=nxt_scope,
                            net=nxt_net,
                            rtl_file=row_nxt.file if row_nxt else "",
                            rtl_line=rtl_line,
                            waypoint_hit=_yn(hit),
                            waypoint_qualified=_yn(qual),
                            is_terminator="N",
                            side=side,
                            peer_matched="-",
                            path_kind=path_kind,
                        )
                    )
                b2 = _boundary_at_state(ctx, nxt, is_origin=False)
                if b2 is not None:
                    term_hit = _waypoint_hit(nxt_scope, nxt_net, waypoints)
                    term_qual = qual or term_hit
                    term_line = _rtl_line_for_boundary(
                        ctx, nxt_scope, nxt_net, b2.kind
                    )
                    if not term_line:
                        term_line = rtl_line
                    term_peer = (
                        _peer_labels_for_hit(
                            nxt_scope,
                            nxt_net,
                            peer_index,
                            peers=waypoints,
                            already_hit=term_hit,
                        )
                        if peer_index is not None
                        else "-"
                    )
                    events.append(
                        WaypointFanoutEvent(
                            source=source,
                            event_kind=b2.kind,
                            scope=nxt_scope,
                            net=nxt_net,
                            rtl_file=row_nxt.file if row_nxt else "",
                            rtl_line=term_line,
                            waypoint_hit=_yn(term_hit),
                            waypoint_qualified=_yn(term_qual),
                            is_terminator="Y",
                            side=side,
                            peer_matched=term_peer,
                            path_kind=path_kind,
                        )
                    )
                    continue
                key: BfsState = (nxt_scope, nxt_net, qual)
                if key not in visited:
                    visited.add(key)
                    next_front.append(key)
        frontier = next_front
    return events


def run_waypoint_fanout_check(
    a_specs: Sequence[str],
    b_specs: Sequence[str],
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    path_kind: Union[str, Sequence[str]] = "comb",
    direction: str = "fanout",
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    check_id: str = "",
    endpoint_a: str = "",
    endpoint_b: str = "",
    connect_trace: bool = False,
    trace_interior: bool = False,
    full_path_kinds: bool = False,
    elab_index: Optional[ElabIndex] = None,
    comb_cache: Optional[Dict[Tuple[str, str, str, str, str, bool, bool], ModuleConnectIndex]] = None,
) -> Tuple[ConnectResult, List[WaypointFanoutEvent]]:
    if elab_index is not None:
        rows_by_path = elab_index.rows_by_path
        child_by_parent_leaf = elab_index.child_by_parent_leaf
    else:
        rows_by_path = {r.full_path: r for r in rows}
        child_by_parent_leaf = {
            (r.parent_path, r.inst_leaf): r.full_path
            for r in rows
            if r.parent_path
        }
    dual = direction == "both"
    peers_b, wp_errs_b, resolved_peers_b = normalize_waypoints(
        b_specs, rows, index, top, rows_by_path=rows_by_path
    )
    peers_a = WaypointSet()
    resolved_peers_a: Tuple[_ResolvedPeer, ...] = ()
    wp_errs_a: List[str] = []
    if dual:
        peers_a, wp_errs_a, resolved_peers_a = normalize_waypoints(
            a_specs, rows, index, top, rows_by_path=rows_by_path
        )
    origins_a, origin_errs_a = expand_fanout_origins(
        a_specs,
        rows=rows,
        index=index,
        top=top,
        rows_by_path=rows_by_path,
        resolved_peers=resolved_peers_a if dual else None,
    )
    errors = list(wp_errs_b) + list(origin_errs_a)
    origins_b: List[Tuple[str, str, str]] = []
    if dual:
        origins_b, origin_errs_b = expand_fanout_origins(
            b_specs,
            rows=rows,
            index=index,
            top=top,
            rows_by_path=rows_by_path,
            resolved_peers=resolved_peers_b,
        )
        errors.extend(wp_errs_a)
        errors.extend(origin_errs_b)
    ep_a = ConnectEndpoint(
        spec=endpoint_a or ",".join(a_specs),
        inst_path=origins_a[0][1] if origins_a else "",
        port_name=origins_a[0][2] if origins_a else "",
    )
    ep_b = ConnectEndpoint(
        spec=endpoint_b or ",".join(b_specs),
        inst_path="",
        port_name="",
    )

    if errors or (not origins_a and not origins_b):
        return (
            ConnectResult(
                ep_a,
                ep_b,
                False,
                "waypoint-fanout",
                errors=errors or ["no fanout origins resolved"],
                check_id=check_id,
                waypoint_events=(),
            ),
            [],
        )

    kinds = (path_kind,) if isinstance(path_kind, str) else tuple(path_kind)
    multi_kind = len(kinds) > 1
    peer_index_b = (
        _build_peer_hit_index(resolved_peers_b)
        if dual
        else None
    )
    peer_index_a = (
        _build_peer_hit_index(resolved_peers_a)
        if dual
        else None
    )
    shared_mod_cache: Dict[Tuple[str, str], ConeModuleIndex] = {}

    emit_interior = trace_interior or connect_trace
    short_circuit_kinds = not full_path_kinds and not connect_trace

    all_events: List[WaypointFanoutEvent] = []
    for pk in kinds:
        ctx = _ConeCtx(
            rows_by_path=rows_by_path,
            child_by_parent_leaf=child_by_parent_leaf,
            index=index,
            top=top,
            mod_cache=shared_mod_cache,
            defines=dict(defines or {}),
            over_approximate_if=over_approximate_if,
            direction="fanout",
            path_kind=pk,
            comb_cache=comb_cache,
        )
        pk_label = pk if multi_kind else ""
        kind_events: List[WaypointFanoutEvent] = []
        for source, scope, net in origins_a:
            kind_events.extend(
                _trace_origin_fanout(
                    source,
                    scope,
                    net,
                    ctx=ctx,
                    waypoints=peers_b,
                    rows_by_path=rows_by_path,
                    side="a-fanout" if dual else "",
                    peer_index=peer_index_b,
                    path_kind=pk_label,
                    emit_interior=emit_interior,
                )
            )
        if dual:
            for source, scope, net in origins_b:
                kind_events.extend(
                    _trace_origin_fanout(
                        source,
                        scope,
                        net,
                        ctx=ctx,
                        waypoints=peers_a,
                        rows_by_path=rows_by_path,
                        side="b-fanout",
                        peer_index=peer_index_a,
                        path_kind=pk_label,
                        emit_interior=emit_interior,
                    )
                )
        all_events.extend(kind_events)
        if short_circuit_kinds and multi_kind:
            if any(
                e.is_terminator == "Y" and e.waypoint_qualified == "Y"
                for e in kind_events
            ):
                break

    terminators = [e for e in all_events if e.is_terminator == "Y"]
    peer_terms = [e for e in terminators if e.waypoint_qualified == "Y"]
    off_path_terms = [e for e in terminators if e.waypoint_qualified != "Y"]
    connected = bool(peer_terms) and not errors
    pk_note = ",".join(kinds) if multi_kind else ""
    if dual:
        note = (
            f"waypoint-fanout direction=both "
            f"path_kinds={pk_note or kinds[0]} "
            f"a_origins={len(origins_a)} b_origins={len(origins_b)} "
            f"events={len(all_events)} peer_terminators={len(peer_terms)} "
            f"off_path_terminators={len(off_path_terms)}"
        )
    else:
        note = (
            f"waypoint-fanout path_kinds={pk_note or kinds[0]} "
            f"origins={len(origins_a)} events={len(all_events)} "
            f"peer_terminators={len(peer_terms)} "
            f"off_path_terminators={len(off_path_terms)}"
        )
    return (
        ConnectResult(
            ep_a,
            ep_b,
            connected,
            "waypoint-fanout",
            errors=errors,
            note=note,
            check_id=check_id,
            waypoint_events=tuple(all_events),
        ),
        all_events,
    )


def format_waypoint_fanout_tsv(events: Sequence[WaypointFanoutEvent]) -> str:
    dual = any(ev.side for ev in events)
    multi_pk = any(ev.path_kind for ev in events)
    if dual:
        header = "source\tside"
        if multi_pk:
            header += "\tpath_kind"
        header += (
            "\tevent_kind\tscope\tnet\trtl_file\trtl_line\t"
            "peer_hit\tpeer_qualified\tpeer_matched\tis_terminator"
        )
    else:
        header = "source"
        if multi_pk:
            header += "\tpath_kind"
        header += (
            "\tevent_kind\tscope\tnet\trtl_file\trtl_line\t"
            "waypoint_hit\twaypoint_qualified\tis_terminator"
        )
    lines = [header]
    for ev in events:
        if dual:
            row = f"{ev.source}\t{ev.side}"
            if multi_pk:
                row += f"\t{ev.path_kind}"
            row += (
                f"\t{ev.event_kind}\t{ev.scope}\t{ev.net}\t"
                f"{ev.rtl_file}\t{ev.rtl_line}\t"
                f"{ev.waypoint_hit}\t{ev.waypoint_qualified}\t"
                f"{ev.peer_matched}\t{ev.is_terminator}"
            )
            lines.append(row)
        else:
            row = f"{ev.source}"
            if multi_pk:
                row += f"\t{ev.path_kind}"
            row += (
                f"\t{ev.event_kind}\t{ev.scope}\t{ev.net}\t"
                f"{ev.rtl_file}\t{ev.rtl_line}\t"
                f"{ev.waypoint_hit}\t{ev.waypoint_qualified}\t{ev.is_terminator}"
            )
            lines.append(row)
    return "\n".join(lines) + "\n"