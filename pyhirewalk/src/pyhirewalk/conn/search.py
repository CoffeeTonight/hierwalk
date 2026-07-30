"""Bidirectional structural meet between fanout (a) and fanin (b) groups."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pyhirewalk.conn.scan import LocalDepGraph, scan_module_file

# NetKey: (file, local_name)
NetKey = Tuple[str, str]
Evidence = Dict[str, object]
LogFn = Callable[[str], None]


@dataclass
class Endpoint:
    path: str
    file: str
    module: str
    name: str
    port_dir: Optional[str] = None  # input|output|inout|None
    fan: Optional[str] = None  # fanin|fanout|inout|internal


@dataclass
class SearchResult:
    pairs: List[Dict[str, Any]] = field(default_factory=list)
    unconnected: List[Dict[str, Any]] = field(default_factory=list)
    cuts: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


def _fmt_ev(ev: Evidence) -> str:
    return (
        f"{ev.get('file')}:{ev.get('line')} | {ev.get('snippet')}"
    )


class ConnSearch:
    def __init__(
        self,
        *,
        defines: Dict[str, str],
        module_files: Dict[str, List[str]],
        max_hops: int = 64,
        max_nodes: int = 5000,
        log: Optional[LogFn] = None,
        progress_every: int = 32,
    ) -> None:
        self.defines = defines
        self.module_files = module_files
        self.max_hops = max_hops
        self.max_nodes = max_nodes
        self._log: LogFn = log or (lambda _m: None)
        self.progress_every = max(1, progress_every)
        self._graphs: Dict[str, LocalDepGraph] = {}
        self._inst_child_file: Dict[Tuple[str, str], str] = {}
        self.known_modules = set(module_files.keys())

    def graph(self, file: str) -> LocalDepGraph:
        fp = str(Path_norm(file))
        if fp not in self._graphs:
            self._log(f"scan open {fp}")
            self._graphs[fp] = scan_module_file(
                fp, self.defines, known_modules=self.known_modules
            )
            g = self._graphs[fp]
            self._log(
                f"scan done  {fp} ports={len(g.ports)} "
                f"fwd_nets={len(g.forward)} port_maps={len(g.port_maps)}"
            )
        return self._graphs[fp]

    def register_instance(
        self, parent_file: str, inst: str, child_file: str
    ) -> None:
        self._inst_child_file[(Path_norm(parent_file), inst)] = Path_norm(child_file)

    def run_check(
        self,
        check_id: str,
        a_ends: List[Endpoint],
        b_ends: List[Endpoint],
    ) -> SearchResult:
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

        # C1/C2: visited + label sets (OR merge)
        lab_a: Dict[NetKey, Set[str]] = {}
        lab_b: Dict[NetKey, Set[str]] = {}
        # C3: prev[key] = (prev_key|None, evidence|None)  edge into key from prev
        prev_a: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
        prev_b: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}

        qa: deque[Tuple[NetKey, int]] = deque()
        qb: deque[Tuple[NetKey, int]] = deque()

        for e in a_ends:
            k: NetKey = (Path_norm(e.file), e.name)
            if k not in lab_a:
                lab_a[k] = set()
                prev_a[k] = (None, None)
                qa.append((k, 0))
            lab_a[k].add(e.path)
            self._log(
                f"seed fanout path={e.path} net={e.name} "
                f"fan={e.fan} port_dir={e.port_dir} file={Path_norm(e.file)}"
            )
        for e in b_ends:
            k = (Path_norm(e.file), e.name)
            if k not in lab_b:
                lab_b[k] = set()
                prev_b[k] = (None, None)
                qb.append((k, 0))
            lab_b[k].add(e.path)
            self._log(
                f"seed fanin  path={e.path} net={e.name} "
                f"fan={e.fan} port_dir={e.port_dir} file={Path_norm(e.file)}"
            )

        meets: List[NetKey] = []
        for k in lab_a:
            if k in lab_b:
                meets.append(k)
                self._log(
                    f"seed-meet net={k[1]} file={k[0]} "
                    f"(a and b share seed net)"
                )

        nodes = 0
        stop_reason = "frontiers_empty"
        while (qa or qb) and nodes < self.max_nodes:
            if qb and (not qa or len(qb) <= len(qa)):
                side, q = "b", qb
            elif qa:
                side, q = "a", qa
            else:
                break

            key, hops = q.popleft()
            if hops >= self.max_hops:
                res.cuts.append(
                    {"where": f"{key[0]}:{key[1]}", "reason": "max_hops"}
                )
                self._log(
                    f"cut max_hops side={side} net={key[1]} hops={hops} file={key[0]}"
                )
                continue
            nodes += 1
            labels = lab_a.get(key) if side == "a" else lab_b.get(key)
            label_s = ",".join(sorted(labels or ()))[:80]
            self._log(
                f"expand side={side} hops={hops} net={key[1]} "
                f"labels={label_s!r} file={key[0]} "
                f"|Fa|={len(qa)} |Fb|={len(qb)}"
            )

            if side == "a":
                neigh = self._neighbors_forward(key)
            else:
                neigh = self._neighbors_backward(key)

            if not neigh:
                self._log(
                    f"dead-end side={side} net={key[1]} hops={hops} "
                    f"(no structural neighbors)"
                )

            new_edges = 0
            revisits = 0
            if side == "a":
                for nk, ev in neigh:
                    nk = (Path_norm(nk[0]), nk[1])
                    first = nk not in lab_a
                    if first:
                        lab_a[nk] = set()
                        prev_a[nk] = (key, ev)
                        qa.append((nk, hops + 1))
                        new_edges += 1
                        self._log(f"evidence fanout {_fmt_ev(ev)}")
                    else:
                        revisits += 1
                    before = len(lab_a[nk])
                    lab_a[nk] |= lab_a[key]
                    if not first and len(lab_a[nk]) > before:
                        self._log(
                            f"label-merge fanout net={nk[1]} "
                            f"+labels from {key[1]}"
                        )
                    if not first and len(lab_a[nk]) > before and nk in lab_b:
                        meets.append(nk)
                    if first and nk in lab_b:
                        meets.append(nk)
                        self._log(
                            f"frontier-meet net={nk[1]} via fanout from {key[1]}"
                        )
            else:
                for nk, ev in neigh:
                    nk = (Path_norm(nk[0]), nk[1])
                    first = nk not in lab_b
                    if first:
                        lab_b[nk] = set()
                        prev_b[nk] = (key, ev)
                        qb.append((nk, hops + 1))
                        new_edges += 1
                        self._log(f"evidence fanin  {_fmt_ev(ev)}")
                    else:
                        revisits += 1
                    before = len(lab_b[nk])
                    lab_b[nk] |= lab_b[key]
                    if not first and len(lab_b[nk]) > before:
                        self._log(
                            f"label-merge fanin  net={nk[1]} "
                            f"+labels from {key[1]}"
                        )
                    if not first and len(lab_b[nk]) > before and nk in lab_a:
                        meets.append(nk)
                    if first and nk in lab_a:
                        meets.append(nk)
                        self._log(
                            f"frontier-meet net={nk[1]} via fanin from {key[1]}"
                        )

            if neigh and new_edges == 0:
                self._log(
                    f"expand-no-new side={side} net={key[1]} "
                    f"neighbors={len(neigh)} revisits={revisits}"
                )

            if nodes == 1 or nodes % self.progress_every == 0:
                self._log(
                    f"progress check={check_id} nodes={nodes} "
                    f"hops={hops} side={side} "
                    f"|Fa|={len(qa)} |Fb|={len(qb)} "
                    f"visited_a={len(lab_a)} visited_b={len(lab_b)} "
                    f"files={len(self._graphs)} meets_pending={len(meets)}"
                )

        if nodes >= self.max_nodes and (qa or qb):
            stop_reason = "max_nodes"
            res.cuts.append({"where": check_id, "reason": "max_nodes"})
            self._log(f"stop check={check_id} reason=max_nodes nodes={nodes}")
        else:
            self._log(
                f"stop check={check_id} reason={stop_reason} nodes={nodes} "
                f"|Fa|={len(qa)} |Fb|={len(qb)}"
            )

        # C5 pairs — log evidence chain when pair is recorded
        seen_pair: Set[Tuple[str, str]] = set()
        for mk in meets:
            for src_path in lab_a.get(mk, ()):
                for dst_path in lab_b.get(mk, ()):
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
                    self._log(f"meet src={src_path} dst={dst_path}")
                    for i, ev in enumerate(evidence):
                        self._log(f"  evidence[{i}] {_fmt_ev(ev)}")

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
            "visited_a": len(lab_a),
            "visited_b": len(lab_b),
            "meets_raw": len(meets),
            "pairs": len(res.pairs),
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
                child_file = self._inst_child_file.get((file, e.inst))
                if not child_file:
                    files = self.module_files.get(e.child_module) or []
                    if not files:
                        continue
                    child_file = Path_norm(files[0])
                    self._inst_child_file[(file, e.inst)] = child_file
                # only descend into child input-like formals when known
                cg = self.graph(child_file)
                d = cg.ports.get(e.dst, "unknown")
                if d in ("input", "inout", "unknown"):
                    out.append(((child_file, e.dst), e.evidence))
            elif e.kind != "port_map":
                out.append(((file, e.dst), e.evidence))
        return out

    def _neighbors_backward(
        self, key: NetKey
    ) -> List[Tuple[NetKey, Evidence]]:
        file, name = key
        g = self.graph(file)
        out: List[Tuple[NetKey, Evidence]] = []

        for src, edges in g.forward.items():
            for e in edges:
                if e.kind == "port_map":
                    continue
                if e.dst == name:
                    out.append(((file, src), e.evidence))

        # climb to parent: this formal -> parent actual
        for (pf, inst), cf in list(self._inst_child_file.items()):
            if cf != file:
                continue
            pg = self.graph(pf)
            for p_inst, formal, actual, _child_mod, ev in pg.port_maps:
                if p_inst != inst or formal != name:
                    continue
                out.append(((pf, actual), ev))

        # from parent actual into child output formal (backward into child)
        for inst, formal, actual, child_mod, ev in g.port_maps:
            if actual != name:
                continue
            child_file = self._inst_child_file.get((file, inst))
            if not child_file:
                files = self.module_files.get(child_mod) or []
                if not files:
                    continue
                child_file = Path_norm(files[0])
                self._inst_child_file[(file, inst)] = child_file
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
        """Evidence along a-seed → meet → b-seed; append path edge order only."""
        # a side: walk meet back to seed, collect edge-into-node, reverse
        stack_a: List[Evidence] = []
        cur: Optional[NetKey] = meet
        seen: Set[NetKey] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            pr, ev = prev_a.get(cur, (None, None))
            if ev is not None:
                stack_a.append(ev)
            cur = pr
        stack_a.reverse()

        # b side: prev_b[driver]=(load, ev) when expanded load←driver
        # walk meet toward b-seed via prev_b; collect edges then reverse for meet→seed
        stack_b: List[Evidence] = []
        cur = meet
        seen_b: Set[NetKey] = set()
        while cur is not None and cur not in seen_b:
            seen_b.add(cur)
            pr, ev = prev_b.get(cur, (None, None))
            if ev is not None:
                stack_b.append(ev)
            cur = pr
        stack_b.reverse()

        out: List[Evidence] = []
        for e in stack_a + stack_b:
            if out and out[-1].get("file") == e.get("file") and out[-1].get(
                "line"
            ) == e.get("line"):
                continue
            out.append(e)
        return out


def Path_norm(p: str) -> str:
    from pathlib import Path as P

    try:
        return str(P(p).resolve())
    except OSError:
        return str(p)
