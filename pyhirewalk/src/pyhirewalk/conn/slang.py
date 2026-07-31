"""Scoped pyslang elaborate → structural dependency edges (HierSlang).

Regex ceiling companion: use for generate/param-heavy modules where
``conn.scan`` is weak. Not a full-chip netlist; structural connectivity
+ evidence only. Cache compilations per (top, files, defines).
"""

from __future__ import annotations

import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from pyhirewalk.conn.scan import LocalDepGraph, Edge, Evidence
from pyhirewalk.conn.search import ConnSearch, Endpoint, NetKey, Path_norm, SearchResult

LogFn = Callable[[str], None]

_KW = frozenset(
    "if for case while return assign begin end else unique default "
    "logic bit wire reg input output inout".split()
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def expr_net_names(expr: Any) -> Set[str]:
    """Collect net/variable names referenced in a slang Expression."""
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
                if n and n not in _KW and not n.startswith("$"):
                    # skip pure genvar-looking single letters only if kind says Genvar
                    kind = str(getattr(sym, "kind", ""))
                    if "Genvar" in kind:
                        return
                    out.add(n)
        except Exception:
            pass
        for attr in (
            "left",
            "right",
            "expr",
            "expression",
            "operand",
            "value",
            "selector",
            "cl",
            "cr",
        ):
            if not hasattr(e, attr):
                continue
            try:
                v = getattr(e, attr)
            except Exception:
                continue
            if v is not None and "Expression" in type(v).__name__:
                walk(v)
        for meth in ("elements", "operands", "expressions"):
            if not hasattr(e, meth):
                continue
            try:
                xs = getattr(e, meth)
                xs = xs() if callable(xs) else xs
                for x in xs or []:
                    if x is not None and "Expression" in type(x).__name__:
                        walk(x)
            except Exception:
                pass

    walk(expr)
    return out


@dataclass
class SlangCompileResult:
    top: str
    n_files: int
    parse_sec: float
    elab_sec: float
    total_sec: float
    n_diags: int
    n_instances: int
    n_assign_edges: int
    n_port_edges: int
    fatal: bool = False


class HierSlangEngine:
    """Compile a scoped file set with pyslang and build LocalDepGraph(s)."""

    def __init__(
        self,
        *,
        defines: Optional[Dict[str, str]] = None,
        include_dirs: Optional[List[str]] = None,
        log: Optional[LogFn] = None,
        max_walk_nodes: int = 200_000,
    ) -> None:
        self.defines = dict(defines or {})
        self.include_dirs = list(include_dirs or [])
        self._log: LogFn = log or (lambda _m: None)
        self.max_walk_nodes = max_walk_nodes
        # cache key → (graphs by file, compile meta)
        self._cache: Dict[str, Tuple[Dict[str, LocalDepGraph], SlangCompileResult]] = {}

    def _cache_key(self, top: str, files: List[str]) -> str:
        defs = ",".join(f"{k}={self.defines[k]}" for k in sorted(self.defines))
        return f"{top}|{defs}|{'|'.join(files)}"

    def compile_and_extract(
        self,
        *,
        top: str,
        files: List[str],
    ) -> Tuple[Dict[str, LocalDepGraph], SlangCompileResult]:
        files = [str(Path(f).resolve()) for f in files if f]
        key = self._cache_key(top, files)
        if key in self._cache:
            self._log(f"slang cache hit top={top} files={len(files)}")
            return self._cache[key]

        try:
            import pyslang
        except ImportError as e:
            raise RuntimeError("pyslang required for hier_slang") from e

        t0 = time.perf_counter()
        driver = pyslang.driver.Driver()
        driver.addStandardArgs()
        parts = ["slang"]
        for k, v in self.defines.items():
            if v is None or v == "":
                parts.append(f"-D{k}")
            else:
                parts.append(f"-D{k}={v}")
        for inc in self.include_dirs:
            parts.append(f"-I{inc}")
        parts.append(f"--top={top}")
        parts.extend(files)
        cmdline = " ".join(shlex.quote(p) for p in parts)
        self._log(f"slang compile start top={top} n_files={len(files)}")
        if not driver.parseCommandLine(cmdline):
            raise RuntimeError("slang parseCommandLine failed")
        if not driver.processOptions():
            raise RuntimeError("slang processOptions failed")
        t1 = time.perf_counter()
        if not driver.parseAllSources():
            self._log("slang parseAllSources returned false (continuing)")
        t2 = time.perf_counter()
        comp = driver.createCompilation()
        try:
            driver.reportCompilation(comp, True)
        except Exception as e:
            self._log(f"slang reportCompilation: {e}")
        root = comp.getRoot()
        t3 = time.perf_counter()
        diags = list(comp.getAllDiagnostics())
        fatal = bool(
            comp.hasFatalErrors()
            if callable(getattr(comp, "hasFatalErrors", None))
            else getattr(comp, "hasFatalErrors", False)
        )
        sm = comp.sourceManager

        graphs: Dict[str, LocalDepGraph] = {}
        n_inst = 0
        n_asg = 0
        n_port = 0
        walked = 0

        InstanceSymbol = pyslang.ast.InstanceSymbol
        ContinuousAssignSymbol = pyslang.ast.ContinuousAssignSymbol
        ProceduralBlockSymbol = pyslang.ast.ProceduralBlockSymbol

        def file_of(sym: Any) -> str:
            loc = getattr(sym, "location", None)
            if loc is None:
                return ""
            try:
                return str(Path(sm.getFileName(loc)).resolve())
            except Exception:
                try:
                    return str(sm.getFileName(loc))
                except Exception:
                    return ""

        def line_of(sym: Any) -> int:
            loc = getattr(sym, "location", None)
            if loc is None:
                return 0
            try:
                return int(sm.getLineNumber(loc))
            except Exception:
                return 0

        def snippet_of(sym: Any, cap: int = 120) -> str:
            # Cheap only — str(syntax) / full-file reads dominate extract time.
            fp = file_of(sym)
            ln = line_of(sym)
            base = Path(fp).name if fp else "?"
            kind = type(sym).__name__.replace("Symbol", "")
            return f"{base}:{ln} [{kind}]"

        def ev_of(sym: Any) -> Evidence:
            return {
                "file": file_of(sym),
                "line": line_of(sym),
                "snippet": snippet_of(sym),
                "via": "slang",
            }

        def graph_for(fp: str, module: str = "") -> LocalDepGraph:
            fp = Path_norm(fp) if fp else ""
            if fp not in graphs:
                graphs[fp] = LocalDepGraph(file=fp, module=module or None)
            elif module and not graphs[fp].module:
                graphs[fp].module = module
            return graphs[fp]

        def add_assign_edge(g: LocalDepGraph, src: str, dst: str, sym: Any) -> None:
            nonlocal n_asg
            if not src or not dst or src == dst:
                return
            if src in _KW or dst in _KW:
                return
            # skip genvar / loop index noise (single short lowercase idents from gen)
            if src in ("k", "i", "j", "n", "idx", "genvar") or dst in (
                "k",
                "i",
                "j",
                "n",
                "idx",
            ):
                return
            # dedupe identical assign edges
            for e in g.forward.get(src, []):
                if e.dst == dst and e.kind == "assign":
                    return
            g.add_fwd(
                src,
                Edge(dst=dst, kind="assign", evidence=ev_of(sym)),
            )
            n_asg += 1

        def handle_assignment_expr(g: LocalDepGraph, asg: Any, sym: Any) -> None:
            if asg is None:
                return
            # unwrap ExpressionStatement → AssignmentExpression
            if type(asg).__name__ == "ExpressionStatement" or str(
                getattr(asg, "kind", "")
            ).endswith("ExpressionStatement"):
                asg = getattr(asg, "expr", None) or getattr(asg, "expression", None)
            if asg is None:
                return
            if "Assignment" not in str(getattr(asg, "kind", type(asg).__name__)):
                return
            left = getattr(asg, "left", None)
            right = getattr(asg, "right", None)
            dsts = expr_net_names(left)
            srcs = expr_net_names(right)
            for dst in dsts:
                for src in srcs:
                    add_assign_edge(g, src, dst, sym)

        _stmt_seen: Set[int] = set()

        def walk_statement(g: LocalDepGraph, stmt: Any, host: Any) -> None:
            if stmt is None:
                return
            sid = id(stmt)
            if sid in _stmt_seen:
                return
            _stmt_seen.add(sid)
            kind = type(stmt).__name__
            sk = str(getattr(stmt, "kind", ""))

            # ExpressionStatement.expr → AssignmentExpression
            if kind == "ExpressionStatement" or sk.endswith("ExpressionStatement"):
                handle_assignment_expr(
                    g,
                    getattr(stmt, "expr", None)
                    or getattr(stmt, "expression", None),
                    host,
                )
            elif "Assignment" in kind or "Assignment" in sk:
                handle_assignment_expr(g, stmt, host)

            # StatementList.list (always_comb begin … end)
            if hasattr(stmt, "list"):
                try:
                    for x in stmt.list:
                        walk_statement(g, x, host)
                except Exception:
                    pass

            # CaseStatement.items → ItemGroup.stmt
            if hasattr(stmt, "items"):
                try:
                    for it in stmt.items or []:
                        st = getattr(it, "stmt", None) or getattr(it, "statement", None)
                        walk_statement(g, st, host)
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
                        walk_statement(g, x, host)
                else:
                    walk_statement(g, v, host)

        def walk_sym(sym: Any, module_hint: str = "", parent_file: str = "") -> None:
            nonlocal walked, n_inst, n_port
            walked += 1
            if walked > self.max_walk_nodes:
                return

            if isinstance(sym, InstanceSymbol):
                n_inst += 1
                try:
                    body = sym.body
                    mname = body.name
                except Exception:
                    body = None
                    mname = module_hint
                child_file = file_of(body) if body is not None else file_of(sym)
                if not child_file and body is not None:
                    try:
                        child_file = file_of(body.definition) if hasattr(body, "definition") else ""
                    except Exception:
                        child_file = ""
                # parent file: caller's scope file
                pfile = parent_file or file_of(getattr(sym, "parentScope", None)) or child_file
                pg = graph_for(pfile, module_hint)
                cg = graph_for(child_file, mname)
                inst_name = getattr(sym, "name", "") or ""
                # pyslang 11: InstanceSymbol.portConnections (property), .expression
                pcs = []
                try:
                    pcs = list(sym.portConnections)
                except Exception:
                    try:
                        pcs = list(sym.getPortConnections())
                    except Exception:
                        pcs = []
                for pc in pcs:
                    try:
                        port = pc.port
                        formal = getattr(port, "name", None) or ""
                        expr = getattr(pc, "expression", None)
                        if expr is None and hasattr(pc, "getExpression"):
                            try:
                                expr = pc.getExpression()
                            except Exception:
                                expr = None
                        actuals = list(expr_net_names(expr)) if expr is not None else []
                        if not formal or not actuals:
                            continue
                        primary = actuals[0]
                        ev = ev_of(sym)
                        ev["snippet"] = f".{formal}({primary})"
                        ev["via"] = "slang_port"
                        pg.instances[inst_name] = mname
                        pg.port_maps.append(
                            (inst_name, formal, primary, mname, dict(ev))
                        )
                        for actual in actuals:
                            pg.add_fwd(
                                actual,
                                Edge(
                                    dst=formal,
                                    kind="port_map",
                                    evidence=dict(ev),
                                    inst=inst_name,
                                    formal=formal,
                                    child_module=mname,
                                    into_child=True,
                                ),
                            )
                            n_port += 1
                    except Exception:
                        continue
                if body is not None:
                    walk_sym(body, mname, parent_file=child_file)
                return

            if isinstance(sym, ContinuousAssignSymbol):
                fp = file_of(sym) or parent_file
                g = graph_for(fp, module_hint)
                try:
                    handle_assignment_expr(g, sym.assignment, sym)
                except Exception:
                    pass
                return

            if isinstance(sym, ProceduralBlockSymbol):
                fp = file_of(sym) or parent_file
                g = graph_for(fp, module_hint)
                try:
                    walk_statement(g, sym.body, sym)
                except Exception:
                    pass
                try:
                    for ch in sym:
                        walk_sym(ch, module_hint, parent_file=fp)
                except Exception:
                    pass
                return

            # InstanceBody / GenerateBlock: iterate children, keep file context
            fp = parent_file or file_of(sym)
            try:
                for ch in sym:
                    walk_sym(ch, module_hint, parent_file=fp)
            except Exception:
                pass

        tops = list(root.topInstances)
        if not tops:
            self._log(f"slang WARNING: no topInstances for --top={top}")
        for tinst in tops:
            walk_sym(tinst, top)

        meta = SlangCompileResult(
            top=top,
            n_files=len(files),
            parse_sec=round(t2 - t1, 6),
            elab_sec=round(t3 - t2, 6),
            total_sec=round(time.perf_counter() - t0, 6),
            n_diags=len(diags),
            n_instances=n_inst,
            n_assign_edges=n_asg,
            n_port_edges=n_port,
            fatal=fatal,
        )
        self._log(
            f"slang done top={top} total={meta.total_sec:.3f}s "
            f"parse={meta.parse_sec:.3f}s elab={meta.elab_sec:.3f}s "
            f"inst={n_inst} asg_edges={n_asg} port_edges={n_port} "
            f"diags={len(diags)} walk={walked} graphs={len(graphs)}"
        )
        self._cache[key] = (graphs, meta)
        return graphs, meta


class HierSlangSearch(ConnSearch):
    """ConnSearch that prefers slang-built LocalDepGraph when registered."""

    def __init__(self, *args: Any, slang: Optional[HierSlangEngine] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.slang = slang
        self._slang_files: List[str] = []
        self._slang_top: Optional[str] = None

    def load_slang_graphs(self, graphs: Dict[str, LocalDepGraph]) -> None:
        for fp, g in graphs.items():
            self._graphs[Path_norm(fp)] = g
            # register instances for boundary climb
            for inst, typ in g.instances.items():
                files = self.module_files.get(typ) or []
                if files:
                    self._inst_child_file[(Path_norm(fp), inst)] = Path_norm(files[0])

    def set_slang_context(self, top: str, files: List[str]) -> None:
        self._slang_top = top
        self._slang_files = list(files)


class HierSlangApp:
    """Run structural meet using pyslang-derived graphs (same check schema as hier_conn)."""

    def __init__(
        self,
        *,
        config: Path,
        map_path: Path,
        resolve_json: Path,
        out: Optional[Path] = None,
        files: Optional[List[str]] = None,
        top: str = "ibex_core",
        include_dirs: Optional[List[str]] = None,
        max_hops: int = 64,
        max_nodes: int = 8000,
        checks: Optional[List[Dict[str, Any]]] = None,
        defines: Optional[Dict[str, str]] = None,
    ) -> None:
        self.config = Path(config)
        self.map_path = Path(map_path)
        self.resolve_json = Path(resolve_json)
        self.out = Path(out) if out else None
        self.files = list(files or [])
        self.top = top
        self.include_dirs = list(include_dirs or [])
        self.max_hops = max_hops
        self.max_nodes = max_nodes
        self._checks = checks
        self._defines = defines

    def run(self) -> int:
        import json
        import sys

        t0 = time.perf_counter()
        tool = "hier_slang"

        def log(msg: str) -> None:
            print(
                f"[{tool}] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
                file=sys.stderr,
            )

        log(f"START config={self.config} top={self.top}")
        if not self.resolve_json.is_file():
            log("ERROR: --resolve required")
            return 2

        from pyhirewalk.run_config import load_hier_conn_inputs
        from hier_resolve import ModuleMap  # type: ignore

        if self._checks is None or self._defines is None:
            checks, defines, _ = load_hier_conn_inputs(self.config)
        else:
            checks, defines = self._checks, self._defines
        # merge defines
        defs = dict(defines)
        log(f"n_checks={len(checks)} n_defines={len(defs)} n_files={len(self.files)}")

        if not self.files:
            log("ERROR: no slang file list (pass files= or pyhirewalk prep)")
            return 2

        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        mmap = ModuleMap.load(self.map_path)
        module_files = {k: list(v) for k, v in mmap.modules.items()}

        engine = HierSlangEngine(
            defines=defs,
            include_dirs=self.include_dirs,
            log=log,
        )
        try:
            graphs, meta = engine.compile_and_extract(top=self.top, files=self.files)
        except Exception as e:
            log(f"ERROR slang compile: {e}")
            return 3

        # Hybrid: merge regex *local* assign/ff only (never port_map — slang owns
        # hierarchy boundaries; regex port_map + slang climb explodes the graph).
        from pyhirewalk.conn.scan import scan_module_file

        known = set(module_files.keys())
        merged = 0
        for fp, g in list(graphs.items()):
            if not fp or not Path(fp).is_file():
                continue
            try:
                rg = scan_module_file(fp, defs, known_modules=known)
            except Exception:
                continue
            for src, edges in rg.forward.items():
                for e in edges:
                    if e.kind == "port_map":
                        continue
                    if any(
                        x.dst == e.dst and x.kind == e.kind
                        for x in g.forward.get(src, [])
                    ):
                        continue
                    g.add_fwd(src, e)
                    merged += 1
        log(f"hybrid merge regex local edges into slang: +{merged}")

        search = HierSlangSearch(
            defines=defs,
            module_files=module_files,
            max_hops=min(self.max_hops, 24),
            max_nodes=min(self.max_nodes, 1200),
            log=log,
            slang=engine,
            progress_every=256,
        )
        search.load_slang_graphs(graphs)
        # Do NOT register every elab instance globally — that re-opens full-chip
        # climb. Per-check register from hier_resolve nodes only (same as conn).

        resolve_by_path: Dict[str, Dict[str, Any]] = {}
        doc_r = json.loads(self.resolve_json.read_text(encoding="utf-8"))
        for r in doc_r.get("results") or []:
            if r.get("path"):
                resolve_by_path[str(r["path"])] = r

        # reuse HierConnApp endpoint helper
        from pyhirewalk.conn.app import HierConnApp

        helper = HierConnApp(
            config=self.config,
            map_path=self.map_path,
            resolve_json=self.resolve_json,
            checks=checks,
            defines=defs,
        )

        out_checks: List[Dict[str, Any]] = []
        total_pairs = 0
        for ch in checks:
            cid = ch["id"]
            log(f"check START id={cid}")
            # fresh instance registry per check (resolve-only)
            search._inst_child_file.clear()
            a_ends: List[Endpoint] = []
            b_ends: List[Endpoint] = []
            miss_a: List[str] = []
            miss_b: List[str] = []
            for p in ch["a"]:
                ep = helper._endpoint_from_resolve(p, resolve_by_path, search, t0)
                if ep is None:
                    miss_a.append(p)
                else:
                    a_ends.append(ep)
            for p in ch["b"]:
                ep = helper._endpoint_from_resolve(p, resolve_by_path, search, t0)
                if ep is None:
                    miss_b.append(p)
                else:
                    b_ends.append(ep)

            if a_ends and b_ends:
                sr = search.run_check(cid, a_ends, b_ends)
            else:
                sr = SearchResult()
            for p in miss_a:
                sr.unconnected.append({"src": p, "dst": None, "reason": "resolve_miss"})
            for p in miss_b:
                sr.unconnected.append({"src": None, "dst": p, "reason": "resolve_miss"})
            total_pairs += len(sr.pairs)
            log(
                f"check END id={cid} pairs={len(sr.pairs)} "
                f"unconnected={len(sr.unconnected)}"
            )
            out_checks.append(
                {
                    "id": cid,
                    "pairs": sr.pairs,
                    "unconnected": sr.unconnected,
                    "orphans": [],
                    "cuts": sr.cuts,
                    "stats": sr.stats,
                    "engine": "slang",
                }
            )

        total = time.perf_counter() - t0
        doc = {
            "schema_version": 1,
            "meta": {
                "tool": "hier_slang",
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "config": str(self.config.resolve()),
                "module_map": str(self.map_path.resolve()),
                "resolve": str(self.resolve_json.resolve()),
                "top": self.top,
                "n_files": len(self.files),
                "slang_compile": {
                    "parse_sec": meta.parse_sec,
                    "elab_sec": meta.elab_sec,
                    "total_sec": meta.total_sec,
                    "n_diags": meta.n_diags,
                    "n_instances": meta.n_instances,
                    "n_assign_edges": meta.n_assign_edges,
                    "n_port_edges": meta.n_port_edges,
                    "fatal": meta.fatal,
                },
                "stats": {
                    "n_checks": len(checks),
                    "n_pairs": total_pairs,
                    "total_sec": round(total, 6),
                },
            },
            "checks": out_checks,
        }
        text = json.dumps(doc, indent=2) + "\n"
        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(text, encoding="utf-8")
            log(f"wrote {self.out}")
        else:
            sys.stdout.write(text)
        log(f"TOTAL_HIER_SLANG_SEC={total:.3f}")
        print(f"TOTAL_HIER_SLANG_SEC: {total:.3f}", file=sys.stderr)
        return 0

    @classmethod
    def main(cls, argv: Optional[List[str]] = None) -> int:
        import argparse
        import json
        import sys

        from pyhirewalk.run_config import load_hier_conn_inputs

        ap = argparse.ArgumentParser(description="pyslang structural COI (HierSlangApp)")
        ap.add_argument("--config", "-c", type=Path, required=True)
        ap.add_argument("--map", "-m", type=Path, default=None)
        ap.add_argument("--resolve", type=Path, required=True)
        ap.add_argument("-o", "--out", type=Path, default=None)
        ap.add_argument("--top", default="ibex_core")
        ap.add_argument(
            "--files",
            type=Path,
            default=None,
            help="filelist (.f) of SV sources for scoped slang compile",
        )
        ap.add_argument("--max-hops", type=int, default=64)
        ap.add_argument("--max-nodes", type=int, default=8000)
        ap.add_argument(
            "-I",
            dest="includes",
            action="append",
            default=[],
            help="include dir for slang",
        )
        args = ap.parse_args(argv)

        checks, defines, cfg_map = load_hier_conn_inputs(args.config)
        map_path = args.map if args.map is not None else cfg_map
        if map_path is None:
            ap.error("need --map or config modules_json")

        files: List[str] = []
        if args.files and args.files.is_file():
            for line in args.files.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("//") or line.startswith("+") or line.startswith("-"):
                    continue
                files.append(line)

        # optional include dirs from file parents
        incs = list(args.includes)
        return cls(
            config=args.config,
            map_path=map_path,
            resolve_json=args.resolve,
            out=args.out,
            files=files,
            top=args.top,
            include_dirs=incs,
            max_hops=args.max_hops,
            max_nodes=args.max_nodes,
            checks=checks,
            defines=defines,
        ).run()


def main(argv: Optional[List[str]] = None) -> int:
    return HierSlangApp.main(argv)
