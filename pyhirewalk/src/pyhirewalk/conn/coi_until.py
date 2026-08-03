"""
COI expansion until stop conditions (e.g. FF:2).

Unlike bi-meet (a↔b pair search), this walks the structural graph from seed
group *a* only, counting path attributes (FF/proc crossings) and stopping a
frontier when:

  * the stop condition is **satisfied** (e.g. path crossed ≥ N FFs), or
  * the frontier has **no remaining edges** (exhausted, even if under budget).

**Path cost model (important):** visit key is NetKey only; first arrival is
**fewest hops (BFS)**. The FF/assign/port counters on a node are those of that
shortest-hop path — not the max-FF path to the same net.

Default direction is fan-out (forward edges = influence of a). Fan-in and
undirected are available for reverse / bidirectional cones.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from pyhirewalk.conn.pyslang_app import (
    Evidence,
    Graph,
    NetKey,
    netkey_fmt,
    resolve_path,
)

# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

_COND_RE = re.compile(
    r"^\s*(FF|ff|proc|PROC|hop|HOP|assign|ASSIGN|port|PORT)\s*:\s*(\d+)\s*$"
)


@dataclass(frozen=True)
class StopCond:
    """Parsed stop rule. Currently one primary counter + optional hop cap."""

    kind: str  # ff | hop | assign | port
    limit: int

    @staticmethod
    def parse(spec: str) -> "StopCond":
        """Parse 'FF:2', 'proc:2', 'hop:16', 'assign:3', 'port:1'."""
        m = _COND_RE.match(spec or "")
        if not m:
            raise ValueError(
                f"bad until condition {spec!r}; expected e.g. FF:2, hop:16, assign:3"
            )
        raw = m.group(1).lower()
        n = int(m.group(2))
        if n < 0:
            raise ValueError(f"until limit must be >= 0, got {n}")
        if raw in ("ff", "proc"):
            kind = "ff"
        elif raw == "hop":
            kind = "hop"
        elif raw == "assign":
            kind = "assign"
        elif raw == "port":
            kind = "port"
        else:
            kind = raw
        return StopCond(kind=kind, limit=n)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "limit": self.limit, "spec": f"{self.kind}:{self.limit}"}


def _edge_is_ff(kind: str, via: str) -> bool:
    """always_ff / latch / legacy always @ (kind=proc) count as one FF hop.

    Fan-in reverse edges use via=proc_rev — still an FF boundary.
    always_comb is kind=comb and must not match.
    """
    if kind == "proc":
        return True
    v = (via or "").lower()
    return v == "proc" or v.startswith("proc")


def _edge_bucket(kind: str, via: str) -> str:
    if _edge_is_ff(kind, via):
        return "ff"
    # always_comb: structural, not FF; also not counted toward until=assign
    if kind == "comb" or (via or "").startswith("comb"):
        return "comb"
    if kind == "assign" or (via or "").startswith("assign"):
        return "assign"
    if kind == "port" or (via or "").startswith("port"):
        return "port"
    if kind.startswith("array_el") or (via or "").startswith("array_el"):
        return "array_el"
    return kind or "other"


@dataclass
class PathCounters:
    ff: int = 0
    assign: int = 0
    port: int = 0
    hop: int = 0
    comb: int = 0
    array_el: int = 0

    def bump(self, bucket: str) -> "PathCounters":
        c = PathCounters(
            ff=self.ff,
            assign=self.assign,
            port=self.port,
            hop=self.hop + 1,
            comb=self.comb,
            array_el=self.array_el,
        )
        if bucket == "ff":
            c.ff += 1
        elif bucket == "assign":
            c.assign += 1
        elif bucket == "port":
            c.port += 1
        elif bucket == "comb":
            c.comb += 1
        elif bucket == "array_el":
            c.array_el += 1
        return c

    def value(self, kind: str) -> int:
        if kind == "ff":
            return self.ff
        if kind == "hop":
            return self.hop
        if kind == "assign":
            return self.assign
        if kind == "port":
            return self.port
        return 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "ff": self.ff,
            "assign": self.assign,
            "port": self.port,
            "hop": self.hop,
            "comb": self.comb,
            "array_el": self.array_el,
        }


# Node lifecycle statuses
_STATUS_OPEN = "open"
_STATUS_SATISFIED = "satisfied"
_STATUS_EXHAUSTED = "exhausted"
_STATUS_INTERNAL = "internal"
_STATUS_TRUNCATED = "truncated"


@dataclass
class CoiNode:
    key: NetKey
    seed: str
    counters: PathCounters
    status: str  # open | satisfied | exhausted | internal | truncated
    parent: Optional[NetKey] = None
    via_edge: Optional[Evidence] = None

    def path_fmt(self) -> str:
        return netkey_fmt(self.key)


@dataclass
class CoiUntilResult:
    seeds: List[str]
    until: StopCond
    direction: str
    nodes: Dict[NetKey, CoiNode] = field(default_factory=dict)
    satisfied: List[NetKey] = field(default_factory=list)
    exhausted: List[NetKey] = field(default_factory=list)
    truncated_leaves: List[NetKey] = field(default_factory=list)
    unresolved_seeds: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    timings_sec: Dict[str, float] = field(default_factory=dict)

    def reconstruct(self, leaf: NetKey) -> List[Evidence]:
        """Evidence chain seed → leaf (forward order)."""
        chain: List[Evidence] = []
        cur: Optional[NetKey] = leaf
        seen: Set[NetKey] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            node = self.nodes.get(cur)
            if node is None:
                break
            if node.via_edge is not None:
                chain.append(node.via_edge)
            cur = node.parent
        chain.reverse()
        return chain

    def to_json(self) -> Dict[str, Any]:
        def _rows(keys: List[NetKey]) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for k in keys:
                n = self.nodes[k]
                ev = self.reconstruct(k)
                rows.append(
                    {
                        "net": netkey_fmt(k),
                        "hier": k[0],
                        "base": k[1],
                        "sel": k[2] if len(k) > 2 else "",
                        "seed": n.seed,
                        "status": n.status,
                        "counters": n.counters.to_dict(),
                        "evidence": ev,
                        "evidence_len": len(ev),
                    }
                )
            return rows

        sat_rows = _rows(self.satisfied)
        exh_rows = _rows(self.exhausted)
        return {
            "seeds": list(self.seeds),
            "until": self.until.to_dict(),
            "direction": self.direction,
            "unresolved_seeds": list(self.unresolved_seeds),
            "satisfied": sat_rows,
            "exhausted": exh_rows,
            "truncated_leaves": [netkey_fmt(k) for k in self.truncated_leaves],
            "coi_nets": sorted(netkey_fmt(k) for k in self.nodes),
            "n_coi": len(self.nodes),
            "n_satisfied": len(self.satisfied),
            "n_exhausted": len(self.exhausted),
            "stats": self.stats,
            "timings_sec": self.timings_sec,
        }


def _neighbors(
    g: Graph,
    key: NetKey,
    direction: str,
    *,
    skip_array_el: bool = False,
) -> List[Tuple[NetKey, Evidence, str]]:
    """Return (dst, evidence, edge_kind) for expansion.

    skip_array_el: when True, drop *all* array_el bookkeeping edges (strict).
    Default False — bit-blasted paths (xbar picked[i], staged_d[7:0]) need
    el↔whole links. Stage-skip risk is mitigated at graph extract (uninstantiated
    generate skip, no whole-approx assign mirrors).
    """
    out: List[Tuple[NetKey, Evidence, str]] = []

    def _keep(kind: str, via: str) -> bool:
        if not skip_array_el:
            return True
        via_l = (via or "").lower()
        if kind.startswith("array_el") or via_l.startswith("array_el"):
            return False
        return True

    if direction in ("fanout", "both", "forward"):
        for e in g.forward.get(key, []):
            via = str(e.evidence.get("via") or "")
            if _keep(e.kind, via):
                out.append((e.dst, e.evidence, e.kind))
    if direction in ("fanin", "both", "backward"):
        for src, e in g.backward.get(key, []):
            via0 = str(e.evidence.get("via") or "")
            if not _keep(e.kind, via0):
                continue
            ev = dict(e.evidence)
            via = via0
            if via and not via.endswith("_rev"):
                ev = dict(ev, via=f"{via}_rev")
            out.append((src, ev, e.kind))
    return out


def _edge_counts_toward(cond_kind: str, bucket: str) -> bool:
    """True if taking this edge increases the stop counter."""
    if cond_kind == "hop":
        return True  # every hop counts
    return bucket == cond_kind


def coi_until(
    g: Graph,
    seeds: List[str],
    *,
    until: StopCond | str,
    direction: str = "fanout",
    max_nodes: int = 8000,
    max_hops: int = 64,
    skip_array_el: bool = False,
) -> CoiUntilResult:
    """
    Expand COI from *seeds* until *until* is met on a path, or the path dies.

    Stop policy for FF / assign / port budgets:
      * Cross at most *limit* counting edges (e.g. ≤2 proc/FF).
      * After the limit is reached, still absorb non-counting edges
        (port/assign aliases of the last FF) so `s1_q` is included when
        the 2nd FF was `u_s1.q_o`.
      * Never take another counting edge past the limit (no 3rd FF).

    For hop:N, stop expanding entirely once hop count reaches N.

    Visited key is NetKey only (first arrival wins — BFS fewest hops).
    """
    t0 = time.perf_counter()
    cond = until if isinstance(until, StopCond) else StopCond.parse(str(until))
    direction = (direction or "fanout").lower()
    if direction in ("fwd", "out", "forward"):
        direction = "fanout"
    if direction in ("bwd", "in", "backward", "rev"):
        direction = "fanin"
    if direction not in ("fanout", "fanin", "both"):
        raise ValueError(f"direction must be fanout|fanin|both, got {direction!r}")

    result = CoiUntilResult(seeds=list(seeds), until=cond, direction=direction)
    # queue: (key, seed_path, counters)
    q: Deque[Tuple[NetKey, str, PathCounters]] = deque()
    visited: Set[NetKey] = set()
    sat_seen: Set[NetKey] = set()
    exh_seen: Set[NetKey] = set()

    def _mark_satisfied(k: NetKey, node: CoiNode) -> None:
        node.status = _STATUS_SATISFIED
        if k not in sat_seen:
            sat_seen.add(k)
            result.satisfied.append(k)

    def _mark_exhausted(k: NetKey, node: CoiNode) -> None:
        node.status = _STATUS_EXHAUSTED
        if k not in exh_seen:
            exh_seen.add(k)
            result.exhausted.append(k)

    def _seed_node(k: NetKey, sp: str, parent: Optional[NetKey], ev: Optional[Evidence]) -> None:
        if k in visited:
            return
        visited.add(k)
        ctr0 = PathCounters()
        node = CoiNode(
            key=k,
            seed=sp,
            counters=ctr0,
            status=_STATUS_OPEN,
            parent=parent,
            via_edge=ev,
        )
        result.nodes[k] = node
        if cond.limit == 0:
            _mark_satisfied(k, node)
        else:
            q.append((k, sp, ctr0))

    for sp in seeds:
        resolved = resolve_path(g, sp)
        if resolved is None:
            result.unresolved_seeds.append(sp)
            continue
        k, _sel, _ap = resolved
        _seed_node(k, sp, None, None)
        # whole → known elements (zero counting cost)
        if not k[2]:
            for ek in _element_keys(g, k):
                _seed_node(
                    ek,
                    sp,
                    k,
                    {
                        "via": "array_el_seed",
                        "snippet": f"{netkey_fmt(ek)} ⊂ {netkey_fmt(k)}",
                    },
                )

    n_expanded = 0
    n_edges = 0
    n_ff_edges = 0
    n_blocked_over_budget = 0
    hit_max_nodes = False
    hit_max_hops = False

    while q:
        if n_expanded >= max_nodes:
            hit_max_nodes = True
            while q:
                k, _s, _c = q.popleft()
                if k in result.nodes and result.nodes[k].status == _STATUS_OPEN:
                    result.nodes[k].status = _STATUS_TRUNCATED
                    result.truncated_leaves.append(k)
            break

        key, seed, ctr = q.popleft()
        node = result.nodes.get(key)
        if node is None:
            continue
        # satisfied nodes may still be expanded for non-counting aliases
        if node.status not in (_STATUS_OPEN, _STATUS_SATISFIED):
            continue
        if ctr.hop >= max_hops:
            hit_max_hops = True
            if node.status == _STATUS_OPEN:
                node.status = _STATUS_TRUNCATED
                result.truncated_leaves.append(key)
            continue

        n_expanded += 1
        at_budget = ctr.value(cond.kind) >= cond.limit
        # hop budget: hard stop — no further edges once met
        if at_budget and cond.kind == "hop":
            if node.status == _STATUS_OPEN:
                _mark_satisfied(key, node)
            continue

        neigh = _neighbors(g, key, direction, skip_array_el=skip_array_el)
        if not neigh:
            # Structural leaf: no outgoing edges in this direction.
            if ctr.value(cond.kind) >= cond.limit:
                if node.status == _STATUS_OPEN:
                    _mark_satisfied(key, node)
            else:
                if node.status == _STATUS_OPEN:
                    _mark_exhausted(key, node)
            continue

        progressed = False
        n_already_visited = 0
        for nk, ev, kind in neigh:
            n_edges += 1
            bucket = _edge_bucket(kind, str(ev.get("via") or ""))
            if bucket == "ff":
                n_ff_edges += 1
            counts = _edge_counts_toward(cond.kind, bucket)
            # At budget: only non-counting edges (aliases). Past budget: never.
            if at_budget and counts:
                n_blocked_over_budget += 1
                continue
            new_ctr = ctr.bump(bucket)
            if new_ctr.value(cond.kind) > cond.limit:
                n_blocked_over_budget += 1
                continue
            if new_ctr.hop > max_hops:
                hit_max_hops = True
                continue
            if nk in visited:
                n_already_visited += 1
                continue
            visited.add(nk)
            progressed = True
            child = CoiNode(
                key=nk,
                seed=seed,
                counters=new_ctr,
                status=_STATUS_OPEN,
                parent=key,
                via_edge=dict(ev),
            )
            if new_ctr.value(cond.kind) >= cond.limit:
                _mark_satisfied(nk, child)
            result.nodes[nk] = child
            # Enqueue: open always; satisfied only if non-hop (alias absorb)
            if child.status == _STATUS_OPEN or (
                child.status == _STATUS_SATISFIED and cond.kind != "hop"
            ):
                q.append((nk, seed, new_ctr))

        if node.status == _STATUS_OPEN:
            if ctr.value(cond.kind) >= cond.limit:
                _mark_satisfied(key, node)
            elif progressed:
                # Expanded into new children.
                node.status = _STATUS_INTERNAL
            elif n_already_visited > 0:
                # Diamond/reconverge: neighbors exist but were reached first via
                # another path — not a dead leaf (would mis-fire must_exhaust).
                node.status = _STATUS_INTERNAL
            else:
                # Had structural neighbors but none usable (budget/hops only) and
                # under stop budget — treat as early end of this branch.
                _mark_exhausted(key, node)

    for k, n in result.nodes.items():
        if n.status == _STATUS_OPEN:
            if n.counters.value(cond.kind) >= cond.limit:
                _mark_satisfied(k, n)
            else:
                _mark_exhausted(k, n)

    t_search = time.perf_counter() - t0
    result.stats = {
        "nodes_expanded": n_expanded,
        "n_coi": len(result.nodes),
        "n_satisfied": len(result.satisfied),
        "n_exhausted": len(result.exhausted),
        "n_truncated": len(result.truncated_leaves),
        "n_edges_touched": n_edges,
        "n_ff_edges_touched": n_ff_edges,
        "n_blocked_over_budget": n_blocked_over_budget,
        "max_nodes": max_nodes,
        "max_hops": max_hops,
        "hit_max_nodes": hit_max_nodes,
        "hit_max_hops": hit_max_hops,
        "unresolved_seeds": len(result.unresolved_seeds),
        "skip_array_el": skip_array_el,
        "path_cost_model": "shortest_hop_bfs_first_visit",
    }
    result.timings_sec = {"search": round(t_search, 6)}
    return result


def _element_keys(g: Graph, k: NetKey) -> List[NetKey]:
    if k[2]:
        return []
    out: List[NetKey] = []
    seen: Set[NetKey] = set()
    for store in (g.forward, g.backward):
        for nk in store:
            if nk[0] == k[0] and nk[1] == k[1] and nk[2] and nk not in seen:
                seen.add(nk)
                out.append(nk)
    return out


def _net_matches(pattern: str, net: str) -> bool:
    """Strict-ish hierarchical net match for answer keys.

    Accepts:
      * exact full path: ``zz_top.u_pipe.mid_o``
      * suffix with module boundary: pattern ``u_pipe.mid_o`` matches
        ``zz_top.u_pipe.mid_o`` (requires ``.`` before pattern or full equal)
      * leaf name: ``mid_o`` matches ``…mid_o`` only as the final segment
      * leaf with select: ``arm_q_o[0]`` matches final segment equal or
        ``arm_q_o[0]`` / ``arm_q_o`` + select already in pattern

    Rejects bare substring matches (``q`` must not match ``y_q_o``).
    """
    p = pattern.strip()
    n = net.strip()
    if not p or not n:
        return False
    if n == p:
        return True
    if n.endswith("." + p):
        return True
    # final segment (may include [sel])
    leaf_n = n.rsplit(".", 1)[-1]
    leaf_p = p.rsplit(".", 1)[-1]
    if leaf_n == leaf_p:
        # only if pattern was a single segment or suffix already handled
        if "." not in p:
            return True
    return False


def _match_any(pattern: str, pool: Set[str]) -> List[str]:
    """Return all nets in pool matching pattern (strict-ish)."""
    return [x for x in pool if _net_matches(pattern, x)]


def verify_expect(
    result: CoiUntilResult, expect: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Check answer-key expectations against a COI result.

    expect keys (all optional):
      must_satisfy: nets that must appear with status=satisfied
      must_coi: nets that must appear in COI (any status)
      must_not_coi: nets that must not appear in COI
      must_exhaust: nets that must appear as exhausted leaves
                    (alias: must_exhaust_any — same strict match)
      min_satisfied: int
      max_ff_in_coi: int — every node counters.ff must be <= this
    """
    coi_set = {netkey_fmt(k) for k in result.nodes}
    sat_set = {netkey_fmt(k) for k in result.satisfied}
    exh_set = {netkey_fmt(k) for k in result.exhausted}
    failures: List[str] = []

    for p in expect.get("must_satisfy") or []:
        if not _match_any(str(p), sat_set):
            failures.append(
                f"must_satisfy missing: {p} (sat={sorted(sat_set)[:12]})"
            )

    for p in expect.get("must_coi") or []:
        if not _match_any(str(p), coi_set):
            failures.append(f"must_coi missing: {p}")

    for p in expect.get("must_not_coi") or []:
        hits = _match_any(str(p), coi_set)
        if hits:
            failures.append(f"must_not_coi present: {p} hits={hits[:4]}")

    exhaust_pats = list(expect.get("must_exhaust") or [])
    exhaust_pats.extend(expect.get("must_exhaust_any") or [])
    for p in exhaust_pats:
        if not _match_any(str(p), exh_set):
            failures.append(
                f"must_exhaust missing: {p} (exh={sorted(exh_set)[:12]})"
            )

    min_sat = expect.get("min_satisfied")
    if min_sat is not None and len(result.satisfied) < int(min_sat):
        failures.append(
            f"min_satisfied {min_sat} > actual {len(result.satisfied)}"
        )

    max_ff = expect.get("max_ff_in_coi")
    if max_ff is not None:
        bad = [
            (netkey_fmt(k), n.counters.ff)
            for k, n in result.nodes.items()
            if n.counters.ff > int(max_ff)
        ]
        if bad:
            failures.append(f"max_ff_in_coi={max_ff} violated by {bad[:8]}")

    return {
        "pass": len(failures) == 0,
        "failures": failures,
        "n_satisfied": len(result.satisfied),
        "n_exhausted": len(result.exhausted),
        "n_coi": len(result.nodes),
    }
