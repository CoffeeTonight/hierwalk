#!/usr/bin/env python3
"""
pyslang-only structural connectivity between two hierarchy groups (a / b).

Experiment target: lowRISC Ibex (filelist next to this script).

  python3 examples/ibex/pyslang_group_conn.py
  python3 examples/ibex/pyslang_group_conn.py --max-nodes 3000 -o /tmp/out.json

Goals:
  - Use ONLY pyslang elaborated AST (no regex LocalDepGraph).
  - See if script-level bi-meet on slang facts is practical.
  - Emit pairs + evidence {file,line,snippet} + timings.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Paths / default Ibex setup
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_DEFAULT_FL = _HERE / "ibex.f"
_DEFAULT_IBEX = Path("/tmp/rtl-bench/ibex")
_DEFAULT_OUT = _HERE / "work" / "pyslang_group_conn.json"

# NetKey: (module_hier_path, local_net)  — module instance path without signal
# e.g. ("ibex_top.u_ibex_core.ex_block_i.alu_i", "operand_a_i")
NetKey = Tuple[str, str]
Evidence = Dict[str, Any]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(
        f"[pyslang_conn] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Expression → net names
# ---------------------------------------------------------------------------

_SKIP = frozenset(
    "if for case while return assign begin end else unique default "
    "logic bit wire reg input output inout genvar".split()
)


def expr_names(expr: Any) -> Set[str]:
    out: Set[str] = set()
    if expr is None:
        return out

    def walk(e: Any) -> None:
        if e is None:
            return
        try:
            sym = e.getSymbolReference()
            if sym is not None:
                n = getattr(sym, "name", None)
                if not n or n in _SKIP or n.startswith("$"):
                    return
                kind = str(getattr(sym, "kind", ""))
                if "Genvar" in kind:
                    return
                if n in ("k", "i", "j", "n", "idx"):
                    return
                out.add(n)
                return
        except Exception:
            pass
        for attr in (
            "left",
            "right",
            "operand",
            "expr",
            "expression",
            "value",
            "selector",
            "cl",
            "cr",
            "thisClass",
        ):
            if not hasattr(e, attr):
                continue
            try:
                v = getattr(e, attr)
            except Exception:
                continue
            if v is not None and "Expression" in type(v).__name__:
                walk(v)
        for meth in (
            "elements",
            "operands",
            "expressions",
            "arguments",  # CallExpression ($unsigned(x), ...)
        ):
            if not hasattr(e, meth):
                continue
            try:
                xs = getattr(e, meth)
                xs = xs() if callable(xs) else xs
                for x in xs or []:
                    if x is None:
                        continue
                    # Call args may be wrapped
                    if "Expression" in type(x).__name__:
                        walk(x)
                    elif hasattr(x, "expr"):
                        walk(getattr(x, "expr", None))
                    elif hasattr(x, "expression"):
                        walk(getattr(x, "expression", None))
            except Exception:
                pass

    walk(expr)
    return out


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    dst: NetKey
    kind: str  # assign | port | proc
    evidence: Evidence


@dataclass
class Graph:
    # forward: driver -> loads
    forward: Dict[NetKey, List[Edge]] = field(default_factory=dict)
    backward: Dict[NetKey, List[Tuple[NetKey, Edge]]] = field(default_factory=dict)
    # path -> (file, module_type, body_symbol_id optional)
    inst_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    n_assign: int = 0
    n_port: int = 0
    n_proc: int = 0

    def add(self, src: NetKey, edge: Edge) -> None:
        if src == edge.dst:
            return
        # dedupe
        for e in self.forward.get(src, []):
            if e.dst == edge.dst and e.kind == edge.kind:
                return
        self.forward.setdefault(src, []).append(edge)
        self.backward.setdefault(edge.dst, []).append((src, edge))
        if edge.kind == "assign":
            self.n_assign += 1
        elif edge.kind == "port":
            self.n_port += 1
        elif edge.kind == "proc":
            self.n_proc += 1


# ---------------------------------------------------------------------------
# Compile + extract
# ---------------------------------------------------------------------------


def read_filelist(path: Path) -> List[str]:
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("+") or line.startswith("-"):
            continue
        out.append(line)
    return out


def compile_design(
    *,
    files: List[str],
    top: str,
    defines: Dict[str, str],
    includes: List[str],
    t0: float,
) -> Any:
    import pyslang

    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    parts = ["slang"]
    for k, v in defines.items():
        if v is None or v == "":
            parts.append(f"-D{k}")
        else:
            parts.append(f"-D{k}={v}")
    for inc in includes:
        parts.append(f"-I{inc}")
    parts.append(f"--top={top}")
    parts.extend(files)
    cmdline = " ".join(shlex.quote(p) for p in parts)
    _log(f"compile start top={top} n_files={len(files)}", t0)
    if not driver.parseCommandLine(cmdline):
        raise RuntimeError("parseCommandLine failed")
    if not driver.processOptions():
        raise RuntimeError("processOptions failed")
    if not driver.parseAllSources():
        _log("parseAllSources returned false (continuing)", t0)
    comp = driver.createCompilation()
    try:
        driver.reportCompilation(comp, True)
    except Exception as e:
        _log(f"reportCompilation: {e}", t0)
    root = comp.getRoot()
    n_tops = len(root.topInstances)
    diags = list(comp.getAllDiagnostics())
    fatal = bool(
        comp.hasFatalErrors()
        if callable(getattr(comp, "hasFatalErrors", None))
        else getattr(comp, "hasFatalErrors", False)
    )
    _log(
        f"compile done tops={n_tops} diags={len(diags)} fatal={fatal}",
        t0,
    )
    return comp, root, diags, fatal


def _file_line(sm: Any, sym: Any) -> Tuple[str, int]:
    loc = getattr(sym, "location", None)
    if loc is None:
        return "", 0
    try:
        return str(Path(sm.getFileName(loc)).resolve()), int(sm.getLineNumber(loc))
    except Exception:
        try:
            return str(sm.getFileName(loc)), int(sm.getLineNumber(loc))
        except Exception:
            return "", 0


def _ev(sm: Any, sym: Any, snippet: str = "", via: str = "slang") -> Evidence:
    fp, ln = _file_line(sm, sym)
    if not snippet:
        snippet = f"{Path(fp).name}:{ln}" if fp else via
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    return {"file": fp, "line": ln, "snippet": snippet, "via": via}


def build_graph(
    root: Any,
    sm: Any,
    *,
    t0: float,
    max_walk: int = 300_000,
) -> Graph:
    """Walk elaborated instance tree; emit assign/port/proc edges on NetKeys."""
    import pyslang

    InstanceSymbol = pyslang.ast.InstanceSymbol
    ContinuousAssignSymbol = pyslang.ast.ContinuousAssignSymbol
    ProceduralBlockSymbol = pyslang.ast.ProceduralBlockSymbol

    g = Graph()
    walked = [0]
    # NOTE: do NOT use a global stmt id set — slang CaseStatement arms often
    # share ConditionalStatement / sub-tree object identities; skipping by id
    # drops later arms (e.g. result_o mux cases).

    def add_local(
        hier: str, src: str, dst: str, kind: str, sym: Any, snip: str = ""
    ) -> None:
        if not src or not dst or src == dst:
            return
        if src in _SKIP or dst in _SKIP:
            return
        sk: NetKey = (hier, src)
        dk: NetKey = (hier, dst)
        g.add(sk, Edge(dst=dk, kind=kind, evidence=_ev(sm, sym, snip, via=kind)))

    def walk_assignment_expr(
        hier: str, asg: Any, host: Any, kind: str = "assign"
    ) -> None:
        if asg is None:
            return
        if type(asg).__name__ == "ExpressionStatement":
            asg = getattr(asg, "expr", None) or getattr(asg, "expression", None)
        if asg is None:
            return
        if "Assignment" not in str(getattr(asg, "kind", type(asg).__name__)):
            return
        left = getattr(asg, "left", None)
        right = getattr(asg, "right", None)
        dsts = expr_names(left)
        srcs = expr_names(right)
        # cheap snippet (str(asg) is slow on large exprs)
        snip = kind
        try:
            if dsts and srcs:
                snip = f"{next(iter(dsts))} <- {','.join(sorted(srcs)[:4])}"
            elif dsts:
                snip = f"{next(iter(dsts))} <- ..."
        except Exception:
            pass
        for dst in dsts:
            for src in srcs:
                add_local(hier, src, dst, kind, host, snip)

    def walk_stmt(hier: str, stmt: Any, host: Any, depth: int = 0) -> None:
        if stmt is None or depth > 64:
            return
        tname = type(stmt).__name__
        sk = str(getattr(stmt, "kind", ""))

        if tname == "ExpressionStatement" or sk.endswith("ExpressionStatement"):
            walk_assignment_expr(
                hier,
                getattr(stmt, "expr", None) or getattr(stmt, "expression", None),
                host,
                kind="proc",
            )
        elif "Assignment" in tname or "Assignment" in sk:
            walk_assignment_expr(hier, stmt, host, kind="proc")

        # StatementList
        if hasattr(stmt, "list") and stmt.list is not None:
            try:
                for x in list(stmt.list):
                    walk_stmt(hier, x, host, depth + 1)
            except Exception:
                pass
        # CaseStatement.ItemGroup list (must re-visit shared subtrees per arm)
        if hasattr(stmt, "items") and stmt.items is not None:
            try:
                for it in list(stmt.items):
                    walk_stmt(hier, getattr(it, "stmt", None), host, depth + 1)
            except Exception:
                pass
        for attr in (
            "body",
            "statement",
            "ifTrue",
            "ifFalse",
            "defaultCase",
            "ifClause",
            "elseClause",
        ):
            if not hasattr(stmt, attr):
                continue
            try:
                v = getattr(stmt, attr)
            except Exception:
                continue
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                for x in v:
                    walk_stmt(hier, x, host, depth + 1)
            else:
                walk_stmt(hier, v, host, depth + 1)

    def walk_scope(hier: str, sym: Any) -> None:
        walked[0] += 1
        if walked[0] > max_walk:
            return
        tname = type(sym).__name__

        if isinstance(sym, ContinuousAssignSymbol):
            walk_assignment_expr(hier, sym.assignment, sym, kind="assign")
            return

        if isinstance(sym, ProceduralBlockSymbol):
            try:
                walk_stmt(hier, sym.body, sym)
            except Exception:
                pass
            # still walk nested symbols if any
            try:
                for ch in sym:
                    walk_scope(hier, ch)
            except Exception:
                pass
            return

        if isinstance(sym, InstanceSymbol):
            # handled by walk_instance
            return

        try:
            for ch in sym:
                if isinstance(ch, InstanceSymbol):
                    walk_instance(hier, ch)
                else:
                    walk_scope(hier, ch)
        except Exception:
            pass

    def walk_instance(parent_hier: str, inst: Any) -> None:
        walked[0] += 1
        if walked[0] > max_walk:
            return
        name = getattr(inst, "name", "") or ""
        hier = f"{parent_hier}.{name}" if parent_hier else name
        try:
            body = inst.body
            mtype = body.name
        except Exception:
            body = None
            mtype = "?"
        fp, _ln = _file_line(sm, body if body is not None else inst)
        g.inst_info[hier] = {
            "module": mtype,
            "file": fp,
            "inst": name,
            "parent": parent_hier,
        }

        # port connections: parent actual <-> child formal
        pcs = []
        try:
            pcs = list(inst.portConnections)
        except Exception:
            pcs = []
        for pc in pcs:
            try:
                formal = pc.port.name
                expr = getattr(pc, "expression", None)
                actuals = list(expr_names(expr)) if expr is not None else []
                if not formal or not actuals:
                    continue
                # parent net (actual) drives child formal (into child)
                # and for outputs we also need reverse: handled by direction later
                # Structural undirected-ish for meet: both directions with same evidence
                snip = f".{formal}({actuals[0]})"
                ev = _ev(sm, inst, snip, via="port")
                for actual in actuals:
                    # parent scope nets live on parent_hier
                    ph = parent_hier
                    if not ph:
                        # top instance: actuals are top ports / same module
                        ph = hier
                    src_p: NetKey = (ph, actual)
                    dst_c: NetKey = (hier, formal)
                    # influence: actual -> formal (into child)
                    g.add(
                        src_p,
                        Edge(dst=dst_c, kind="port", evidence=dict(ev)),
                    )
                    # reverse structural link for bi-meet (formal -> actual)
                    g.add(
                        dst_c,
                        Edge(
                            dst=src_p,
                            kind="port",
                            evidence=dict(ev, via="port_rev"),
                        ),
                    )
                    g.n_port += 1
            except Exception:
                continue

        if body is None:
            return
        # body members: assigns, procs, nested instances
        try:
            for ch in body:
                if isinstance(ch, InstanceSymbol):
                    walk_instance(hier, ch)
                else:
                    walk_scope(hier, ch)
        except Exception:
            pass

    tops = list(root.topInstances)
    if not tops:
        _log("WARNING: no topInstances", t0)
    for tinst in tops:
        walk_instance("", tinst)

    _log(
        f"graph: walk={walked[0]} insts={len(g.inst_info)} "
        f"fwd_keys={len(g.forward)} assign={g.n_assign} port={g.n_port} proc={g.n_proc}",
        t0,
    )
    return g


# ---------------------------------------------------------------------------
# Path resolve on elab tree
# ---------------------------------------------------------------------------


def resolve_path(g: Graph, path: str) -> Optional[NetKey]:
    """Map 'top.u.v.signal' -> (instance_hier, signal)."""
    path = path.strip()
    if not path:
        return None
    # longest instance prefix in inst_info
    # try full path as instance (no signal) — reject
    if path in g.inst_info:
        return None
    parts = path.split(".")
    # signal is last segment (strip [select])
    sig = parts[-1].split("[", 1)[0]
    # find longest prefix that is an instance path
    for i in range(len(parts) - 1, 0, -1):
        pref = ".".join(parts[:i])
        if pref in g.inst_info:
            # remaining must be single signal (or signal only)
            rest = parts[i:]
            if len(rest) == 1:
                return (pref, sig)
            # multi-part rest not supported (nested without inst_info)
            break
    # maybe top.module is first component only
    if len(parts) >= 2:
        # try progressively
        for i in range(1, len(parts)):
            pref = ".".join(parts[:i])
            if pref in g.inst_info and i == len(parts) - 1:
                return (pref, sig)
    return None


# ---------------------------------------------------------------------------
# Bi-meet search
# ---------------------------------------------------------------------------


def bi_meet(
    g: Graph,
    a_keys: List[Tuple[str, NetKey]],
    b_keys: List[Tuple[str, NetKey]],
    *,
    max_hops: int = 48,
    max_nodes: int = 8000,
    t0: float,
    check_id: str = "",
) -> Dict[str, Any]:
    """a = fanout seeds (path, key), b = fanin seeds."""
    lab_a: Dict[NetKey, Set[str]] = {}
    lab_b: Dict[NetKey, Set[str]] = {}
    prev_a: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
    prev_b: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
    qa: Deque[Tuple[NetKey, int]] = deque()
    qb: Deque[Tuple[NetKey, int]] = deque()

    for path, k in a_keys:
        if k not in lab_a:
            lab_a[k] = set()
            prev_a[k] = (None, None)
            qa.append((k, 0))
        lab_a[k].add(path)
    for path, k in b_keys:
        if k not in lab_b:
            lab_b[k] = set()
            prev_b[k] = (None, None)
            qb.append((k, 0))
        lab_b[k].add(path)

    meets: List[NetKey] = []
    for k in lab_a:
        if k in lab_b:
            meets.append(k)

    nodes = 0
    while (qa or qb) and nodes < max_nodes:
        if qb and (not qa or len(qb) <= len(qa)):
            side, q = "b", qb
        elif qa:
            side, q = "a", qa
        else:
            break
        key, hops = q.popleft()
        if hops >= max_hops:
            continue
        nodes += 1

        if side == "a":
            neigh = [(e.dst, e.evidence) for e in g.forward.get(key, [])]
        else:
            neigh = [(src, e.evidence) for src, e in g.backward.get(key, [])]

        if side == "a":
            for nk, ev in neigh:
                first = nk not in lab_a
                if first:
                    lab_a[nk] = set()
                    prev_a[nk] = (key, ev)
                    qa.append((nk, hops + 1))
                before = len(lab_a[nk])
                lab_a[nk] |= lab_a[key]
                if first and nk in lab_b:
                    meets.append(nk)
                elif not first and len(lab_a[nk]) > before and nk in lab_b:
                    meets.append(nk)
        else:
            for nk, ev in neigh:
                first = nk not in lab_b
                if first:
                    lab_b[nk] = set()
                    prev_b[nk] = (key, ev)
                    qb.append((nk, hops + 1))
                before = len(lab_b[nk])
                lab_b[nk] |= lab_b[key]
                if first and nk in lab_a:
                    meets.append(nk)
                elif not first and len(lab_b[nk]) > before and nk in lab_a:
                    meets.append(nk)

    def reconstruct(meet: NetKey) -> List[Evidence]:
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
        stack_b: List[Evidence] = []
        cur = meet
        seen_b: Set[NetKey] = set()
        while cur is not None and cur not in seen_b:
            seen_b.add(cur)
            pr, ev = prev_b.get(cur, (None, None))
            if ev is not None:
                stack_b.append(ev)
            cur = pr
        out: List[Evidence] = []
        for e in stack_a + stack_b:
            if out and out[-1].get("file") == e.get("file") and out[-1].get("line") == e.get(
                "line"
            ):
                continue
            out.append(e)
        return out

    pairs = []
    seen_pair: Set[Tuple[str, str]] = set()
    for mk in meets:
        for src in lab_a.get(mk, ()):
            for dst in lab_b.get(mk, ()):
                pk = (src, dst)
                if pk in seen_pair:
                    continue
                seen_pair.add(pk)
                pairs.append(
                    {
                        "src": src,
                        "dst": dst,
                        "meet": {"hier": mk[0], "net": mk[1]},
                        "evidence": reconstruct(mk),
                    }
                )

    unconnected = []
    paired_src = {p["src"] for p in pairs}
    paired_dst = {p["dst"] for p in pairs}
    for path, _k in a_keys:
        if path not in paired_src:
            unconnected.append({"src": path, "dst": None, "reason": "no_meet"})
    for path, _k in b_keys:
        if path not in paired_dst:
            unconnected.append({"src": None, "dst": path, "reason": "no_meet"})

    _log(
        f"meet check={check_id} nodes={nodes} pairs={len(pairs)} "
        f"|Va|={len(lab_a)} |Vb|={len(lab_b)} meets_raw={len(meets)}",
        t0,
    )
    return {
        "pairs": pairs,
        "unconnected": unconnected,
        "stats": {
            "nodes_expanded": nodes,
            "visited_a": len(lab_a),
            "visited_b": len(lab_b),
            "meets_raw": len(meets),
            "pairs": len(pairs),
        },
    }


# ---------------------------------------------------------------------------
# Default checks (Ibex)
# ---------------------------------------------------------------------------

DEFAULT_CHECKS = [
    {
        "id": "alu_leaf",
        "a": ["ibex_top.u_ibex_core.ex_block_i.alu_i.operand_a_i"],
        "b": ["ibex_top.u_ibex_core.ex_block_i.alu_i.result_o"],
    },
    {
        "id": "alu_adder",
        "a": ["ibex_top.u_ibex_core.ex_block_i.alu_i.operand_a_i"],
        "b": ["ibex_top.u_ibex_core.ex_block_i.alu_i.adder_result_o"],
    },
    {
        "id": "alu_equal_out",
        "a": ["ibex_top.u_ibex_core.ex_block_i.alu_i.adder_result"],
        "b": ["ibex_top.u_ibex_core.ex_block_i.alu_i.is_equal_result_o"],
    },
    {
        "id": "ex_to_alu",
        "a": ["ibex_top.u_ibex_core.ex_block_i.alu_operand_a_i"],
        "b": ["ibex_top.u_ibex_core.ex_block_i.alu_i.operand_a_i"],
    },
    {
        "id": "if_to_id_instr",
        "a": ["ibex_top.u_ibex_core.if_stage_i.instr_rdata_id_o"],
        "b": ["ibex_top.u_ibex_core.id_stage_i.instr_rdata_i"],
    },
    {
        "id": "no_conn_noise",
        "a": ["ibex_top.u_ibex_core.ex_block_i.alu_i.result_o"],
        "b": ["ibex_top.u_ibex_core.if_stage_i.pc_if_o"],
    },
]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="pyslang-only group connectivity (Ibex experiment)")
    ap.add_argument("--filelist", type=Path, default=_DEFAULT_FL)
    ap.add_argument("--top", default="ibex_top")
    ap.add_argument("--ibex-root", type=Path, default=_DEFAULT_IBEX)
    ap.add_argument("-o", "--out", type=Path, default=_DEFAULT_OUT)
    ap.add_argument("--max-hops", type=int, default=48)
    ap.add_argument("--max-nodes", type=int, default=8000)
    ap.add_argument(
        "--checks-json",
        type=Path,
        default=None,
        help="optional run JSON with run_conn_check.checks; else built-in Ibex set",
    )
    args = ap.parse_args(argv)
    t0 = time.perf_counter()

    if not args.filelist.is_file():
        _log(f"ERROR missing filelist {args.filelist}", t0)
        return 2
    files = read_filelist(args.filelist)
    ibex = args.ibex_root
    includes = [
        str(ibex / "rtl"),
        str(ibex / "vendor/lowrisc_ip/ip/prim/rtl"),
        str(ibex / "vendor/lowrisc_ip/ip/prim_generic/rtl"),
    ]
    defines = {
        "SYNTHESIS": "1",
        "PRIM_DEFAULT_IMPL": "prim_pkg::ImplGeneric",
    }

    checks = DEFAULT_CHECKS
    if args.checks_json and args.checks_json.is_file():
        doc = json.loads(args.checks_json.read_text(encoding="utf-8"))
        ch = (doc.get("run_conn_check") or {}).get("checks")
        if ch:
            checks = ch
            _log(f"loaded {len(checks)} checks from {args.checks_json}", t0)

    t_comp0 = time.perf_counter()
    comp, root, diags, fatal = compile_design(
        files=files, top=args.top, defines=defines, includes=includes, t0=t0
    )
    sm = comp.sourceManager
    t_comp = time.perf_counter() - t_comp0

    t_g0 = time.perf_counter()
    g = build_graph(root, sm, t0=t0)
    t_graph = time.perf_counter() - t_g0

    # sample inst paths
    sample_insts = sorted(g.inst_info.keys())[:12]
    _log(f"sample instances: {sample_insts}", t0)

    out_checks = []
    total_pairs = 0
    t_search0 = time.perf_counter()
    for ch in checks:
        cid = ch["id"]
        a_keys: List[Tuple[str, NetKey]] = []
        b_keys: List[Tuple[str, NetKey]] = []
        miss: List[Dict[str, Any]] = []
        for p in ch["a"]:
            k = resolve_path(g, p)
            if k is None:
                miss.append({"src": p, "dst": None, "reason": "path_miss"})
                _log(f"  path_miss a {p}", t0)
            else:
                a_keys.append((p, k))
                _log(f"  seed a {p} -> {k}", t0)
        for p in ch["b"]:
            k = resolve_path(g, p)
            if k is None:
                miss.append({"src": None, "dst": p, "reason": "path_miss"})
                _log(f"  path_miss b {p}", t0)
            else:
                b_keys.append((p, k))
                _log(f"  seed b {p} -> {k}", t0)

        if a_keys and b_keys:
            res = bi_meet(
                g,
                a_keys,
                b_keys,
                max_hops=args.max_hops,
                max_nodes=args.max_nodes,
                t0=t0,
                check_id=cid,
            )
        else:
            res = {
                "pairs": [],
                "unconnected": [],
                "stats": {"nodes_expanded": 0, "pairs": 0},
            }
        res["unconnected"] = miss + res.get("unconnected", [])
        total_pairs += len(res["pairs"])
        for pr in res["pairs"]:
            _log(
                f"  PAIR {pr['src']} -> {pr['dst']} meet={pr['meet']} "
                f"ev_n={len(pr['evidence'])}",
                t0,
            )
            for i, ev in enumerate(pr["evidence"][:4]):
                _log(
                    f"    ev[{i}] {ev.get('via')} L{ev.get('line')} "
                    f"{(ev.get('snippet') or '')[:80]}",
                    t0,
                )
        out_checks.append(
            {
                "id": cid,
                "pairs": res["pairs"],
                "unconnected": res["unconnected"],
                "stats": res["stats"],
                "engine": "pyslang_only",
            }
        )
    t_search = time.perf_counter() - t_search0
    total = time.perf_counter() - t0

    # usability summary for the experiment
    degree = [len(v) for v in g.forward.values()]
    usability = {
        "can_resolve_all_seeds": all(
            u.get("reason") != "path_miss"
            for c in out_checks
            for u in c.get("unconnected") or []
        )
        or total_pairs > 0,
        "n_instances": len(g.inst_info),
        "n_forward_keys": len(g.forward),
        "avg_out_degree": (sum(degree) / len(degree)) if degree else 0,
        "max_out_degree": max(degree) if degree else 0,
        "n_assign_edges": g.n_assign,
        "n_port_edges": g.n_port,
        "n_proc_edges": g.n_proc,
        "script_exploration_ok": True,
        "notes": [
            "NetKey=(instance_hier, local_net) from elab tree",
            "Edges: continuous assign, procedural assign, port actual↔formal",
            "Bi-meet is plain Python BFS on that graph",
            "Port edges added both directions for structural bi-meet",
        ],
    }

    doc = {
        "schema_version": 1,
        "meta": {
            "tool": "pyslang_group_conn",
            "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "top": args.top,
            "n_files": len(files),
            "n_diags": len(diags),
            "fatal": fatal,
            "timings_sec": {
                "compile": round(t_comp, 6),
                "graph_extract": round(t_graph, 6),
                "search": round(t_search, 6),
                "total": round(total, 6),
            },
            "stats": {
                "n_checks": len(checks),
                "n_pairs": total_pairs,
            },
            "usability": usability,
        },
        "checks": out_checks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _log(f"wrote {args.out}", t0)
    _log(
        f"TIMING compile={t_comp:.3f}s extract={t_graph:.3f}s "
        f"search={t_search:.3f}s TOTAL={total:.3f}s pairs={total_pairs}",
        t0,
    )
    print(f"TOTAL_PYSLANG_GROUP_CONN_SEC: {total:.3f}", file=sys.stderr)
    # exit 0 even if some no_meet — experiment script
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
