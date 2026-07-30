"""Bidirectional structural meet between fanout (a) and fanin (b) groups."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from pyhirewalk.conn.scan import Edge, LocalDepGraph, scan_module_file

# NetKey: (file, local_name)
NetKey = Tuple[str, str]
Evidence = Dict[str, object]


@dataclass
class Endpoint:
    path: str
    file: str
    module: str
    name: str


@dataclass
class SearchResult:
    pairs: List[Dict[str, Any]] = field(default_factory=list)
    unconnected: List[Dict[str, Any]] = field(default_factory=list)
    cuts: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


class ConnSearch:
    def __init__(
        self,
        *,
        defines: Dict[str, str],
        module_files: Dict[str, List[str]],  # modulename -> files
        max_hops: int = 64,
        max_nodes: int = 5000,
    ) -> None:
        self.defines = defines
        self.module_files = module_files
        self.max_hops = max_hops
        self.max_nodes = max_nodes
        self._graphs: Dict[str, LocalDepGraph] = {}
        # parent context: (parent_file, inst) -> child_file (first path)
        # filled when we know instance binding from resolve nodes
        self._inst_child_file: Dict[Tuple[str, str], str] = {}
        # reverse: child_file -> list of (parent_file, inst, port_maps...)
        self.known_modules = set(module_files.keys())

    def graph(self, file: str) -> LocalDepGraph:
        fp = str(file)
        if fp not in self._graphs:
            self._graphs[fp] = scan_module_file(
                fp, self.defines, known_modules=self.known_modules
            )
        return self._graphs[fp]

    def register_instance(
        self, parent_file: str, inst: str, child_file: str
    ) -> None:
        self._inst_child_file[(str(parent_file), inst)] = str(child_file)

    def run_check(
        self,
        check_id: str,
        a_ends: List[Endpoint],
        b_ends: List[Endpoint],
    ) -> SearchResult:
        """
        a = fanout seeds (forward), b = fanin seeds (backward).
        """
        res = SearchResult()
        if not a_ends or not b_ends:
            for e in a_ends:
                res.unconnected.append(
                    {"src": e.path, "dst": None, "reason": "empty_group_b"}
                )
            for e in b_ends:
                res.unconnected.append(
                    {"src": None, "dst": e.path, "reason": "empty_group_a"}
                )
            return res

        # side A forward
        vis_a: Dict[NetKey, str] = {}  # key -> seed path label
        prev_a: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
        # side B backward: we store keys reached; edges reverse
        vis_b: Dict[NetKey, str] = {}
        prev_b: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}

        qa: deque[Tuple[NetKey, int]] = deque()
        qb: deque[Tuple[NetKey, int]] = deque()

        for e in a_ends:
            k: NetKey = (e.file, e.name)
            if k not in vis_a:
                vis_a[k] = e.path
                prev_a[k] = (None, None)
                qa.append((k, 0))
        for e in b_ends:
            k = (e.file, e.name)
            if k not in vis_b:
                vis_b[k] = e.path
                prev_b[k] = (None, None)
                qb.append((k, 0))

        meets: List[NetKey] = []
        # seed already meet?
        for k in list(vis_a.keys()):
            if k in vis_b:
                meets.append(k)

        nodes = 0
        while (qa or qb) and nodes < self.max_nodes and len(meets) < 32:
            # prefer smaller frontier (often b / fan-in)
            if qb and (not qa or len(qb) <= len(qa)):
                side = "b"
                q = qb
            elif qa:
                side = "a"
                q = qa
            else:
                break

            key, hops = q.popleft()
            if hops >= self.max_hops:
                res.cuts.append({"where": f"{key[0]}:{key[1]}", "reason": "max_hops"})
                continue
            nodes += 1

            if side == "a":
                for nk, ev in self._neighbors_forward(key):
                    if nk not in vis_a:
                        vis_a[nk] = vis_a[key]
                        prev_a[nk] = (key, ev)
                        q.append((nk, hops + 1))
                        if nk in vis_b:
                            meets.append(nk)
            else:
                for nk, ev in self._neighbors_backward(key):
                    if nk not in vis_b:
                        vis_b[nk] = vis_b[key]
                        prev_b[nk] = (key, ev)
                        q.append((nk, hops + 1))
                        if nk in vis_a:
                            meets.append(nk)

        # emit pairs from meets
        seen_pair: Set[Tuple[str, str]] = set()
        for mk in meets:
            src_path = vis_a.get(mk)
            dst_path = vis_b.get(mk)
            if not src_path or not dst_path:
                continue
            pk = (src_path, dst_path)
            if pk in seen_pair:
                continue
            seen_pair.add(pk)
            evidence = self._reconstruct(mk, prev_a, prev_b)
            res.pairs.append(
                {
                    "src": src_path,
                    "dst": dst_path,
                    "evidence": evidence,
                }
            )

        # unconnected: a×b pairs not reported (coarse: each endpoint without any pair)
        paired_src = {p["src"] for p in res.pairs}
        paired_dst = {p["dst"] for p in res.pairs}
        for e in a_ends:
            if e.path not in paired_src:
                res.unconnected.append(
                    {"src": e.path, "dst": None, "reason": "no_meet"}
                )
        for e in b_ends:
            if e.path not in paired_dst:
                res.unconnected.append(
                    {"src": None, "dst": e.path, "reason": "no_meet"}
                )

        res.stats = {
            "nodes_expanded": nodes,
            "visited_a": len(vis_a),
            "visited_b": len(vis_b),
            "meets": len(meets),
            "files_scanned": len(self._graphs),
        }
        return res

    def _neighbors_forward(
        self, key: NetKey
    ) -> List[Tuple[NetKey, Evidence]]:
        file, name = key
        g = self.graph(file)
        out: List[Tuple[NetKey, Evidence]] = []
        for e in g.forward.get(name, []):
            if e.kind == "port_map" and e.into_child and e.inst and e.child_module:
                # cross into child: formal lives in child file
                child_file = self._inst_child_file.get((file, e.inst))
                if not child_file:
                    # try module map first file
                    files = self.module_files.get(e.child_module) or []
                    if not files:
                        continue
                    child_file = files[0]
                    self._inst_child_file[(file, e.inst)] = child_file
                out.append(((child_file, e.dst), e.evidence))
            elif e.kind != "port_map":
                out.append(((file, e.dst), e.evidence))
        # if name is child output formal appearing as port_map actual on parent:
        # handled when we have reverse edges — also scan port_maps where actual is driven
        # from child output: parent actual is load of child formal
        for inst, formal, actual, child_mod, ev in g.port_maps:
            if actual != name:
                continue
            # name is actual; if we are forwarding from inside child we need other graph
            pass
        return out

    def _neighbors_backward(
        self, key: NetKey
    ) -> List[Tuple[NetKey, Evidence]]:
        """Who drives this net? reverse of forward edges + port_map climb."""
        file, name = key
        g = self.graph(file)
        out: List[Tuple[NetKey, Evidence]] = []

        # reverse local assign/ff: find src such that edge.dst == name
        for src, edges in g.forward.items():
            for e in edges:
                if e.kind == "port_map":
                    continue
                if e.dst == name:
                    out.append(((file, src), e.evidence))

        # port as child formal: climb to parent actual
        # We need parents that instantiate this module — use reverse inst index
        for (pf, inst), cf in list(self._inst_child_file.items()):
            if cf != file:
                continue
            pg = self.graph(pf)
            for p_inst, formal, actual, child_mod, ev in pg.port_maps:
                if p_inst != inst or formal != name:
                    continue
                out.append(((pf, actual), ev))

        # if name is actual on this module, go into child output formal (backward into child)
        for inst, formal, actual, child_mod, ev in g.port_maps:
            if actual != name:
                continue
            child_file = self._inst_child_file.get((file, inst))
            if not child_file:
                files = self.module_files.get(child_mod) or []
                if not files:
                    continue
                child_file = files[0]
                self._inst_child_file[(file, inst)] = child_file
            # only climb into child if formal is output-like (or unknown)
            cg = self.graph(child_file)
            direction = cg.ports.get(formal, "unknown")
            if direction in ("output", "inout", "unknown"):
                out.append(((child_file, formal), ev))

        return out

    def _reconstruct(
        self,
        meet: NetKey,
        prev_a: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]],
        prev_b: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]],
    ) -> List[Evidence]:
        # a-path from seed to meet
        chain_a: List[Evidence] = []
        cur: Optional[NetKey] = meet
        while cur is not None:
            pr, ev = prev_a[cur]
            if ev is not None:
                chain_a.append(ev)
            cur = pr
        chain_a.reverse()  # seed -> meet order of edges (each ev is edge into node)

        # b-path: prev_b walks from meet toward seed b; edges were stored on step to child
        # When we expand backward from u to v (v drives u), we set prev_b[v] = (u, ev)
        # Wait - in code: from key, neighbor nk is driver; prev_b[nk] = (key, ev)
        # So from meet, follow prev_b to go toward... actually we set:
        #   for nk, ev in neighbors_backward(key):  # nk drives key
        #     prev_b[nk] = (key, ev)
        # So prev_b[driver] = (load, ev). From meet, we need drivers... 
        # Seed b is in vis_b; meet is reached. To reconstruct load<-driver path from meet to b seed:
        # Start at meet, we need to go along reverse of discovery toward seed.
        # Discovery: start seed s, expand to drivers d, prev_b[d]=(s,ev) means we went s <- d with edge?
        # neighbors_backward(s) returns drivers of s. We visit driver d from s:
        #   prev_b[d] = (s, ev) where ev is edge d->s
        # Path from meet M to seed: if M was reached from parent P, prev_b[M]=(P,ev).
        # Follow M -> P -> ... -> seed. Evidence order for a->b: ... then reverse(this chain)'s edges
        chain_b_rev: List[Evidence] = []
        cur = meet
        seen: Set[NetKey] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            pr, ev = prev_b[cur]
            if ev is not None:
                chain_b_rev.append(ev)
            cur = pr
        # chain_b_rev is edges from meet toward b-seed (driver direction steps)
        # For a->b report: after meeting, continue toward b: reverse chain_b_rev
        chain_b = list(reversed(chain_b_rev))

        # Merge: path a to meet (edges into nodes toward meet) + path meet to b
        # Avoid duplicating meet node edges: chain_a ends with edge into meet; chain_b starts from meet out
        out = chain_a + chain_b
        # drop consecutive duplicate snippets
        dedup: List[Evidence] = []
        for e in out:
            if dedup and dedup[-1].get("file") == e.get("file") and dedup[-1].get(
                "line"
            ) == e.get("line"):
                continue
            dedup.append(e)
        return dedup
