"""
HierPyslangApp — pyslang-only structural connectivity (generate + bit-select meta).

Uses elaborated instance tree (generate folded under defines/params).
Keeps hier_conn (regex) separate; this is the precision engine for hard structure.

Compile context:
  - defines → slang -D
  - env → applied to os.environ (filelist $VAR, include paths)
  - filelist / -I include dirs
  - top module
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

# NetKey: (instance hierarchy, base net name, normalized select)
# select is "" for whole-net, or e.g. "[0]" / "[31:0]" for literal ElementSelect.
# Approx / non-literal selects stay on whole-net key and set evidence select_approx.
NetKey = Tuple[str, str, str]
Evidence = Dict[str, Any]


def _norm_sel_key(sel: Optional[str], approx: bool = False) -> str:
    """Canonical select for NetKey; empty if whole-net or non-literal."""
    if approx or not sel:
        return ""
    from pyhirewalk.conn.slice_policy import normalize_sel

    n, ap = normalize_sel(sel)
    if ap or not n:
        return ""
    return n


def netkey(hier: str, base: str, sel: Optional[str] = None, *, approx: bool = False) -> NetKey:
    return (hier, base, _norm_sel_key(sel, approx))


def netkey_fmt(k: NetKey) -> str:
    h, b, s = k
    return f"{h}.{b}{s}" if h else f"{b}{s}"

_SKIP = frozenset(
    "if for case while return assign begin end else unique default "
    "logic bit wire reg input output inout genvar".split()
)
_LIT_SEL = re.compile(r"^\s*(\d+)\s*(?::\s*(\d+)\s*)?$")
_PATH_SEL = re.compile(r"^([A-Za-z_]\w*)((?:\[[^\]]+\])*)$")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(
        f"[hier_pyslang] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Bit-select helpers
# ---------------------------------------------------------------------------


def parse_path_tail(seg: str) -> Tuple[str, Optional[str], bool]:
    """signal or signal[3] / signal[7:4] → (base, sel_fmt|None, approx)."""
    m = _PATH_SEL.match(seg.strip())
    if not m:
        return seg.split("[", 1)[0], None, False
    base, brackets = m.group(1), m.group(2) or ""
    if not brackets:
        return base, None, False
    parts = re.findall(r"\[([^\]]*)\]", brackets)
    if len(parts) != 1:
        return base, None, True
    lm = _LIT_SEL.match(parts[0])
    if not lm:
        return base, None, True
    a = int(lm.group(1))
    b = int(lm.group(2)) if lm.group(2) is not None else a
    if a == b:
        return base, f"[{a}]", False
    return base, f"[{a}:{b}]", False


def _const_int(expr: Any) -> Optional[int]:
    """Best-effort integer from a pyslang expression (literal / genvar / param)."""
    if expr is None:
        return None
    # Prefer .constant — works for genvar indices after elab (no EvalContext needed)
    try:
        cv = getattr(expr, "constant", None)
        if cv is not None:
            if hasattr(cv, "integer"):
                return int(cv.integer)
            if isinstance(cv, int):
                return int(cv)
            s = str(cv).strip()
            # slang may print "8'h0" / "32'd3" / plain "3"
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                return int(s)
            m = re.search(r"(-?\d+)\s*$", s)
            if m and "'" not in s.split(m.group(1))[0][-2:]:
                # last resort: trailing digits
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
            # bare int-like from ConstantValue
            try:
                return int(cv)
            except Exception:
                pass
    except Exception:
        pass
    try:
        if hasattr(expr, "eval"):
            # pyslang 11: eval requires EvalContext — skip if wrong arity
            import inspect

            try:
                sig = inspect.signature(expr.eval)
                if len(sig.parameters) <= 1:
                    cv = expr.eval()
                else:
                    cv = None
            except Exception:
                cv = None
            if cv is not None and hasattr(cv, "integer"):
                return int(cv.integer)
            if cv is not None:
                s = str(cv)
                if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                    return int(s)
    except Exception:
        pass
    try:
        s = str(expr).strip()
        if s.isdigit():
            return int(s)
    except Exception:
        pass
    return None


def expr_select_fmt(expr: Any) -> Tuple[Optional[str], bool]:
    """If expr is ElementSelect/RangeSelect → (fmt|None, approx).

    Literal indices → fmt like ``[3]`` / ``[7:4]``, approx=False.
    Non-literal (genvar, param expr) → fmt=None, approx=True.
    """
    if expr is None:
        return None, False
    tname = type(expr).__name__
    kind = str(getattr(expr, "kind", ""))
    is_elem = (
        "ElementSelect" in tname
        or kind.endswith("ElementSelect")
        or "ElementSelect" in kind
    )
    is_range = (
        "RangeSelect" in tname
        or kind.endswith("RangeSelect")
        or "RangeSelect" in kind
    )
    if is_elem:
        idx = getattr(expr, "selector", None) or getattr(expr, "index", None)
        n = _const_int(idx)
        if n is not None:
            return f"[{n}]", False
        # genvar / param index — structural select present but not literal map
        return None, True
    if is_range:
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        # some AST use selector for range bounds
        if left is None and hasattr(expr, "selector"):
            left = getattr(expr, "selector", None)
        a, b = _const_int(left), _const_int(right)
        if a is not None and b is not None:
            if a == b:
                return f"[{a}]", False
            return f"[{a}:{b}]", False
        return None, True
    return None, False


def cone_instance_prefixes(checks: List[Dict[str, Any]]) -> Set[str]:
    """Instance-path prefixes for seeds (exclude leaf signal segment)."""
    prefs: Set[str] = set()
    for ch in checks:
        for p in list(ch.get("a") or []) + list(ch.get("b") or []):
            segs = str(p).split(".")
            if len(segs) < 2:
                continue
            # prefixes of the instance path (drop signal tail)
            for i in range(1, len(segs)):
                prefs.add(".".join(segs[:i]))
    return prefs


def is_cone_hier(hier: str, cone_prefs: Optional[Set[str]]) -> bool:
    """True if hier is on a seed cone: ancestor of a seed, the seed, or under it.

    Seed prefixes are instance paths (signal leaf stripped). Without the
    "under seed" arm, top-level seeds like ``top.a_i`` only list ``top`` and
    never enter child instances — port edges to children disappear.
    """
    if not cone_prefs:
        return True
    for pref in cone_prefs:
        if pref == hier:
            return True
        # hier is an ancestor of a seed instance
        if pref.startswith(hier + "."):
            return True
        # hier is inside a seed instance (or under a prefix that is the top)
        if hier.startswith(pref + "."):
            return True
    return False


def select_cone_files(
    all_files: List[str],
    *,
    modules_json: Optional[Path],
    top: str,
    t0: float,
) -> List[str]:
    """
    Shrink filelist for faster compile when modules_json is available.

    Keeps: top, all *pkg*, prim_* used by map that appear as package/prim,
    and modules whose definition file lives next to top (same RTL dir) —
    enough for Ibex-style cores. Falls back to all_files if map missing.
    """
    if modules_json is None or not Path(modules_json).is_file():
        _log("cone-files: no modules_json — using full filelist", t0)
        return all_files
    try:
        doc = json.loads(Path(modules_json).read_text(encoding="utf-8"))
        mods = doc.get("modules") or doc
        if not isinstance(mods, dict):
            return all_files
    except Exception as e:
        _log(f"cone-files: map load failed {e}", t0)
        return all_files

    # name → list of paths
    name_to_files: Dict[str, List[str]] = {}
    for name, paths in mods.items():
        if isinstance(paths, list):
            name_to_files[str(name)] = [str(p) for p in paths]
        elif isinstance(paths, str):
            name_to_files[str(name)] = [paths]

    top_files = name_to_files.get(top) or []
    rtl_dirs: Set[str] = set()
    for tf in top_files:
        rtl_dirs.add(str(Path(tf).resolve().parent))

    needed: Set[str] = set()
    # packages always
    for name, flist in name_to_files.items():
        nl = name.lower()
        if nl.endswith("_pkg") or nl.endswith("pkg") or "package" in nl:
            for f in flist:
                needed.add(str(Path(f).resolve()))
        # prim packages / small prims often required for elab
        if name.startswith("prim_") and (
            "pkg" in name or name in ("prim_assert", "prim_pkg", "prim_buf",
                                      "prim_flop", "prim_clock_gating",
                                      "prim_and2", "prim_xor2", "prim_xnor2")
        ):
            for f in flist:
                needed.add(str(Path(f).resolve()))

    # all modules defined under same directory as top (core RTL)
    for name, flist in name_to_files.items():
        for f in flist:
            fp = str(Path(f).resolve())
            if str(Path(fp).parent) in rtl_dirs:
                needed.add(fp)

    # prim files already in all_files that are referenced by name in map and
    # under vendor prim paths — include generic prims present in all_files
    all_resolved = [str(Path(f).resolve()) for f in all_files]
    all_set = set(all_resolved)
    # intersection: keep order of all_files
    out = [f for f in all_resolved if f in needed]
    # ensure top files present
    for tf in top_files:
        tfr = str(Path(tf).resolve())
        if tfr in all_set and tfr not in out:
            out.insert(0, tfr)

    if len(out) < 3:
        _log(
            f"cone-files: too small ({len(out)}) — fallback full list n={len(all_files)}",
            t0,
        )
        return all_files
    _log(
        f"cone-files: {len(out)}/{len(all_files)} files "
        f"(rtl_dirs={sorted(rtl_dirs)})",
        t0,
    )
    return out


def expr_net_refs(expr: Any) -> List[Tuple[str, Optional[str], bool]]:
    """
    Named nets in expression with optional literal select.
    Returns list of (base_name, sel_fmt|None, select_approx).
    """
    out: List[Tuple[str, Optional[str], bool]] = []
    seen: Set[str] = set()

    def add(name: str, sel: Optional[str], approx: bool) -> None:
        if not name or name in _SKIP or name.startswith("$"):
            return
        if name in ("k", "i", "j", "n", "idx", "gi", "gj", "gk"):
            return
        if name in seen and not sel:
            return
        # allow same base with different sels as one base for graph key
        if name not in seen:
            seen.add(name)
            out.append((name, sel, approx))
        elif sel:
            # update first entry if it had no sel
            for i, (n, s, a) in enumerate(out):
                if n == name and s is None and sel:
                    out[i] = (n, sel, approx or a)
                    break

    def walk(e: Any) -> None:
        if e is None:
            return
        tname = type(e).__name__
        kind = str(getattr(e, "kind", ""))

        # Port connections sometimes wrap actual as AssignmentExpression
        # (left = ElementSelect(lane_y[gi]), right = EmptyArgument).
        if "Assignment" in tname or kind.endswith("Assignment"):
            left = getattr(e, "left", None)
            right = getattr(e, "right", None)
            walk(left)
            # skip EmptyArgument / void right-hand
            if right is not None and "Empty" not in type(right).__name__:
                walk(right)
            return

        # select wrappers: walk value + capture sel
        sel, approx = expr_select_fmt(e)
        if sel is not None or approx:
            val = getattr(e, "value", None) or getattr(e, "expr", None) or getattr(
                e, "left", None
            )
            # NamedValue under select
            try:
                if val is not None:
                    sym = val.getSymbolReference()
                    if sym is not None:
                        n = getattr(sym, "name", None)
                        if n:
                            add(n, sel, approx)
                            return
            except Exception:
                pass
            walk(val)
            return

        try:
            sym = e.getSymbolReference()
            if sym is not None:
                n = getattr(sym, "name", None)
                kind_s = str(getattr(sym, "kind", ""))
                if "Genvar" in kind_s:
                    return
                if n:
                    add(n, None, False)
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
        ):
            if not hasattr(e, attr):
                continue
            try:
                v = getattr(e, attr)
            except Exception:
                continue
            if v is not None and "Expression" in type(v).__name__:
                walk(v)
        for meth in ("elements", "operands", "expressions", "arguments"):
            if not hasattr(e, meth):
                continue
            try:
                xs = getattr(e, meth)
                xs = xs() if callable(xs) else xs
                for x in xs or []:
                    if x is None:
                        continue
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


def expr_names(expr: Any) -> Set[str]:
    return {n for n, _s, _a in expr_net_refs(expr)}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    dst: NetKey
    kind: str  # assign | port | proc | comb | array_el
    evidence: Evidence


@dataclass
class Graph:
    forward: Dict[NetKey, List[Edge]] = field(default_factory=dict)
    backward: Dict[NetKey, List[Tuple[NetKey, Edge]]] = field(default_factory=dict)
    inst_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # generate block paths discovered under instances (for debug)
    gen_blocks: List[str] = field(default_factory=list)
    n_assign: int = 0
    n_port: int = 0
    n_proc: int = 0
    n_comb: int = 0  # always_comb procedural (not FF)
    n_array_el: int = 0
    n_literal_sel: int = 0  # edges with dst_sel or src_sels (literal)
    n_approx_sel: int = 0  # ElementSelect with non-literal index
    sel_examples: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, src: NetKey, edge: Edge) -> None:
        if src == edge.dst:
            return
        for e in self.forward.get(src, []):
            if e.dst == edge.dst and e.kind == edge.kind:
                # merge sel meta if new
                if edge.evidence.get("dst_sel") and not e.evidence.get("dst_sel"):
                    e.evidence.update(
                        {
                            k: edge.evidence[k]
                            for k in ("dst_sel", "src_sel", "src_sels", "select_approx")
                            if k in edge.evidence
                        }
                    )
                return
        self.forward.setdefault(src, []).append(edge)
        self.backward.setdefault(edge.dst, []).append((src, edge))
        if edge.kind == "assign":
            self.n_assign += 1
        elif edge.kind == "port":
            self.n_port += 1
        elif edge.kind == "proc":
            self.n_proc += 1
        elif edge.kind == "comb":
            self.n_comb += 1
        elif edge.kind == "array_el":
            self.n_array_el += 1
        has_lit = bool(
            edge.evidence.get("dst_sel")
            or edge.evidence.get("src_sels")
            or (len(src) > 2 and src[2])
            or (len(edge.dst) > 2 and edge.dst[2])
        )
        has_approx = bool(edge.evidence.get("select_approx"))
        if has_lit:
            self.n_literal_sel += 1
            if len(self.sel_examples) < 12:
                self.sel_examples.append(
                    {
                        "src": netkey_fmt(src),
                        "dst": netkey_fmt(edge.dst),
                        "dst_sel": edge.evidence.get("dst_sel") or edge.dst[2] or None,
                        "src_sels": edge.evidence.get("src_sels"),
                        "kind": edge.kind,
                        "line": edge.evidence.get("line"),
                    }
                )
        elif has_approx:
            self.n_approx_sel += 1


# ---------------------------------------------------------------------------
# Compile (env + defines)
# ---------------------------------------------------------------------------


def expand_env_str(s: str, env: Dict[str, str]) -> str:
    """Expand $VAR and ${VAR} using env map + os.environ."""
    merged = dict(os.environ)
    merged.update(env)

    def repl(m: re.Match) -> str:
        key = m.group(1) or m.group(2)
        return merged.get(key, m.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, s)


def read_filelist(path: Path, env: Optional[Dict[str, str]] = None) -> List[str]:
    """Legacy flat reader (path-per-line only). Prefer :func:`load_rtl_sources`."""
    env = env or {}
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("+") or line.startswith("-"):
            continue
        line = expand_env_str(line, env)
        out.append(line)
    return out


def load_rtl_sources(
    filelist: Path,
    *,
    env: Optional[Dict[str, str]] = None,
    index_cwd: Optional[Path] = None,
    t0: float,
) -> Tuple[List[str], List[str], Dict[str, str], List[str]]:
    """
    Expand an EDA filelist into RTL source paths for pyslang.

    Uses the same ``expand_filelist`` as build_db (supports ``-f``/``-F``,
    ``+incdir+``, ``+define+``, ``$VAR``). The old naive reader skipped every
    line starting with ``+`` or ``-``, which made real company .f files look
    empty even when run JSON hierarchies were fine.

    Returns
    -------
    files, incdirs, defines_from_f, errors
    """
    from pyhirewalk.filelist.expand import expand_filelist

    fl = Path(filelist).expanduser()
    if not fl.is_file():
        return [], [], {}, [f"filelist not found: {fl}"]

    env = env or {}
    try:
        result = expand_filelist(
            fl,
            env=env,
            index_cwd=index_cwd,
            on_progress=lambda m: _log(f"  filelist: {m}", t0),
        )
    except Exception as e:
        return [], [], {}, [f"expand_filelist failed: {e}"]

    files = [str(p.resolve()) for p in result.source_files]
    incdirs = [str(p.resolve()) for p in result.incdirs]
    defs = dict(result.defines)
    errors = list(result.errors)
    if result.unresolved_env:
        _log(
            f"filelist unresolved $VAR: {result.unresolved_env}",
            t0,
        )
    _log(
        f"filelist expand: path={fl} sources={len(files)} "
        f"incdirs={len(incdirs)} defines_in_f={len(defs)} "
        f"errors={len(errors)}",
        t0,
    )
    if not files:
        # Diagnostic: show why it looks empty
        try:
            raw_lines = fl.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            raw_lines = []
        non_empty = [ln.strip() for ln in raw_lines if ln.strip() and not ln.strip().startswith("//")]
        sample = non_empty[:12]
        _log(
            f"filelist empty sources. raw non-comment lines={len(non_empty)} "
            f"sample={sample!r}",
            t0,
        )
        _log(
            "hint: hier_pyslang needs RTL filelist (paths / -f nested), "
            "not only run_conn_check hierarchies. "
            "If .f is only +incdir+/+define+/-f, check nested paths and env.",
            t0,
        )
    return files, incdirs, defs, errors


def apply_config_env(env: Dict[str, str], *, t0: float) -> None:
    """Apply run JSON env to process (pyslang / path expand see same vars)."""
    if not env:
        _log("env: (none from config)", t0)
        return
    for k, v in sorted(env.items()):
        os.environ[k] = str(v)
        _log(f"env set {k}={v}", t0)


def compile_design(
    *,
    files: List[str],
    top: str,
    defines: Dict[str, str],
    includes: List[str],
    t0: float,
    parameters: Optional[Dict[str, str]] = None,
) -> Tuple[Any, Any, List[Any], bool]:
    import pyslang

    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    parts = ["slang"]
    for k, v in sorted(defines.items()):
        if v is None or v == "":
            parts.append(f"-D{k}")
        else:
            parts.append(f"-D{k}={v}")
    # Top-module parameter overrides (slang -Gname=value)
    for k, v in sorted((parameters or {}).items()):
        parts.append(f"-G{k}={v}")
    for inc in includes:
        parts.append(f"-I{inc}")
    parts.append(f"--top={top}")
    parts.extend(files)
    cmdline = " ".join(shlex.quote(p) for p in parts)
    _log(
        f"compile start top={top} n_files={len(files)} n_defines={len(defines)} "
        f"n_params={len(parameters or {})} n_includes={len(includes)}",
        t0,
    )
    _log(f"defines: {sorted(defines.keys())}", t0)
    if parameters:
        _log(f"parameters(-G): {sorted(parameters.items())}", t0)
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
    diags = list(comp.getAllDiagnostics())
    fatal = bool(
        comp.hasFatalErrors()
        if callable(getattr(comp, "hasFatalErrors", None))
        else getattr(comp, "hasFatalErrors", False)
    )
    _log(
        f"compile done tops={len(root.topInstances)} diags={len(diags)} fatal={fatal}",
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


def _ev(sm: Any, sym: Any, snippet: str = "", via: str = "slang", **extra: Any) -> Evidence:
    fp, ln = _file_line(sm, sym)
    if not snippet:
        snippet = f"{Path(fp).name}:{ln}" if fp else via
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    ev: Evidence = {"file": fp, "line": ln, "snippet": snippet, "via": via}
    ev.update(extra)
    return ev


def build_graph(
    root: Any,
    sm: Any,
    *,
    t0: float,
    max_walk: int = 300_000,
    cone_prefs: Optional[Set[str]] = None,
) -> Graph:
    """Elaborated tree → dependency graph (generate already folded by slang).

    If cone_prefs is set, only walk instance subtrees that lie on seed paths
    (ancestor prefixes) — major extract speedup on large tops.
    """
    import pyslang

    InstanceSymbol = pyslang.ast.InstanceSymbol
    ContinuousAssignSymbol = pyslang.ast.ContinuousAssignSymbol
    ProceduralBlockSymbol = pyslang.ast.ProceduralBlockSymbol
    GenerateBlockSymbol = getattr(pyslang.ast, "GenerateBlockSymbol", type(None))
    GenerateBlockArraySymbol = getattr(pyslang.ast, "GenerateBlockArraySymbol", type(None))
    NetSymbol = getattr(pyslang.ast, "NetSymbol", type(None))
    VariableSymbol = getattr(pyslang.ast, "VariableSymbol", type(None))

    g = Graph()
    walked = [0]
    skipped_inst = [0]
    if cone_prefs:
        _log(f"graph cone: {len(cone_prefs)} instance prefixes", t0)

    def add_local(
        hier: str,
        src: str,
        dst: str,
        kind: str,
        sym: Any,
        snip: str = "",
        dst_sel: Optional[str] = None,
        src_sel: Optional[str] = None,
        src_sels: Optional[Dict[str, str]] = None,
        select_approx: bool = False,
    ) -> None:
        if not src or not dst:
            return
        if src in _SKIP or dst in _SKIP:
            return
        # Same base with different selects is a real edge (e.g. bus[0]<-x).
        # Same base+same sel is a no-op.
        dsk = _norm_sel_key(dst_sel, select_approx and not dst_sel)
        ssk = _norm_sel_key(src_sel, select_approx and not src_sel)
        # if select_approx only on one side, that side uses whole key
        if select_approx:
            # keep literal side if present
            if dst_sel and _norm_sel_key(dst_sel, False):
                dsk = _norm_sel_key(dst_sel, False)
            else:
                dsk = ""
            if src_sel and _norm_sel_key(src_sel, False):
                ssk = _norm_sel_key(src_sel, False)
            else:
                ssk = ""
        if src == dst and ssk == dsk:
            return
        extra: Dict[str, Any] = {}
        if dsk:
            extra["dst_sel"] = dsk
        elif dst_sel:
            extra["dst_sel"] = dst_sel
        if ssk:
            extra["src_sel"] = ssk
        elif src_sel:
            extra["src_sel"] = src_sel
        if src_sels:
            extra["src_sels"] = dict(src_sels)
        if select_approx:
            extra["select_approx"] = True
        sk: NetKey = netkey(hier, src, ssk or None)
        dk: NetKey = netkey(hier, dst, dsk or None)
        g.add(sk, Edge(dst=dk, kind=kind, evidence=_ev(sm, sym, snip, via=kind, **extra)))
        def _el_whole_links(base: str, el_key: NetKey, el_sel: str) -> None:
            whole = netkey(hier, base, "")
            if whole == el_key:
                return
            ev = _ev(
                sm, sym, f"{base}{el_sel} ⊂ {base}", via="array_el", dst_sel=el_sel
            )
            # element → whole (write aggregates into whole)
            g.add(el_key, Edge(dst=whole, kind="array_el", evidence=ev))
            # whole → element (so fanin/fanout bi-meet can meet co-drivers;
            # wrong-element *seed* claims still blocked by direct-edge filter)
            g.add(
                whole,
                Edge(
                    dst=el_key,
                    kind="array_el",
                    evidence=dict(ev, via="array_el_rd"),
                ),
            )

        if dsk:
            _el_whole_links(dst, dk, dsk)
        if ssk:
            _el_whole_links(src, sk, ssk)
            # structural read: whole base also drives the consumer of element.
            # Only for *port* connections (hierarchical pin binding). For
            # assign/proc/comb, mirroring mid_o <- q_s[1] as mid_o <- q_s
            # creates FF-count shortcuts (q_s[0]→whole→mid skips q_s[1]).
            if kind == "port":
                whole_s = netkey(hier, src, "")
                if whole_s != dk:
                    g.add(
                        whole_s,
                        Edge(
                            dst=dk,
                            kind=kind,
                            evidence=_ev(
                                sm,
                                sym,
                                snip + f" (via {src})",
                                via=kind,
                                **{**extra, "src_sel": None, "select_approx": True},
                            ),
                        ),
                    )

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
        dst_refs = expr_net_refs(left)
        src_refs = expr_net_refs(right)
        src_sels = {n: s for n, s, _a in src_refs if s}
        approx = any(a for _n, _s, a in dst_refs + src_refs)
        snip = kind
        try:
            if dst_refs and src_refs:
                d0, ds, _ = dst_refs[0]
                snip = f"{d0}{ds or ''} <- {','.join(n+(s or '') for n,s,_ in src_refs[:4])}"
            elif dst_refs:
                d0, ds, _ = dst_refs[0]
                snip = f"{d0}{ds or ''} <- ..."
        except Exception:
            pass
        for dst, dsel, _da in dst_refs:
            for src, ssel, _sa in src_refs:
                add_local(
                    hier,
                    src,
                    dst,
                    kind,
                    host,
                    snip,
                    dst_sel=dsel,
                    src_sel=ssel,
                    src_sels=src_sels or None,
                    select_approx=approx,
                )

    def _edge_kind_for_procedure(pk: Any) -> str:
        """Map pyslang ProceduralBlockKind → graph edge kind.

        always_ff / latch / legacy always @  → proc  (counts as FF in coi_until)
        always_comb                         → comb  (not an FF)
        initial / final                     → assign
        """
        try:
            K = pyslang.ast.ProceduralBlockKind
            if pk == K.AlwaysComb:
                return "comb"
            if pk in (K.AlwaysFF, K.AlwaysLatch, K.Always):
                return "proc"
            return "assign"
        except Exception:
            s = str(pk)
            if "AlwaysComb" in s:
                return "comb"
            if "AlwaysFF" in s or "AlwaysLatch" in s or s.endswith("Always"):
                return "proc"
            return "assign"

    def walk_stmt(
        hier: str, stmt: Any, host: Any, depth: int = 0, *, edge_kind: str = "proc"
    ) -> None:
        # No global id-seen: Case arms may share AST nodes.
        if stmt is None or depth > 64:
            return
        tname = type(stmt).__name__
        sk = str(getattr(stmt, "kind", ""))
        if tname == "ExpressionStatement" or sk.endswith("ExpressionStatement"):
            walk_assignment_expr(
                hier,
                getattr(stmt, "expr", None) or getattr(stmt, "expression", None),
                host,
                kind=edge_kind,
            )
        elif "Assignment" in tname or "Assignment" in sk:
            walk_assignment_expr(hier, stmt, host, kind=edge_kind)
        if hasattr(stmt, "list") and stmt.list is not None:
            try:
                for x in list(stmt.list):
                    walk_stmt(hier, x, host, depth + 1, edge_kind=edge_kind)
            except Exception:
                pass
        if hasattr(stmt, "items") and stmt.items is not None:
            try:
                for it in list(stmt.items):
                    walk_stmt(
                        hier,
                        getattr(it, "stmt", None),
                        host,
                        depth + 1,
                        edge_kind=edge_kind,
                    )
            except Exception:
                pass
        for attr in (
            "body",
            "statement",
            "stmt",  # TimedStatement (always_ff / always @) uses .stmt
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
                    walk_stmt(hier, x, host, depth + 1, edge_kind=edge_kind)
            else:
                walk_stmt(hier, v, host, depth + 1, edge_kind=edge_kind)

    def walk_scope(hier: str, sym: Any, *, owner_hier: Optional[str] = None) -> None:
        """Walk scope members. ``hier`` is net-key hierarchy; ``owner_hier`` is
        the enclosing *instance* path used as port-actual parent (skips generate
        array indices — actuals live on the parent module instance).
        """
        walked[0] += 1
        if walked[0] > max_walk:
            return
        if owner_hier is None:
            owner_hier = hier
        # Prefer slang hierarchicalPath so generate-for indices appear
        # (GenerateBlockSymbol.name is often empty; path is g_lane[0]).
        if GenerateBlockSymbol and isinstance(sym, GenerateBlockSymbol):
            # Uninstantiated generate-if/else arms still appear in the AST
            # (e.g. g_mid0 when STAGES>=2). Walking them injects false edges
            # such as mid_o <- q_s[0] alongside the true mid_o <- q_s[1].
            if bool(getattr(sym, "isUninstantiated", False)):
                return
            hp = getattr(sym, "hierarchicalPath", None)
            if hp:
                hier = str(hp)
            gname = getattr(sym, "name", "") or ""
            g.gen_blocks.append(hier if not gname else f"{hier}")
        if GenerateBlockArraySymbol and isinstance(sym, GenerateBlockArraySymbol):
            if bool(getattr(sym, "isUninstantiated", False)):
                return
            hp = getattr(sym, "hierarchicalPath", None)
            gname = getattr(sym, "name", "") or ""
            label = str(hp) if hp else (f"{hier}.{gname}[]" if gname else f"{hier}.gen[]")
            g.gen_blocks.append(label)
            # Prefer .entries (indexed blocks) over bare iteration
            entries = getattr(sym, "entries", None)
            if entries is not None:
                try:
                    for ent in list(entries):
                        walk_scope(hier, ent, owner_hier=owner_hier)
                    return
                except Exception:
                    pass

        if isinstance(sym, ContinuousAssignSymbol):
            # Net keys use the enclosing *instance* path. Assigns inside
            # generate-if/for still drive module-level nets (mux_w, inv_w);
            # using generate hierarchicalPath here splits one net into
            # g_inv.mux_w vs u_lane.mux_w and breaks structural meet.
            walk_assignment_expr(owner_hier, sym.assignment, sym, kind="assign")
            return
        # wire/reg x = expr;  (initializer, not ContinuousAssignSymbol)
        if (NetSymbol and isinstance(sym, NetSymbol)) or (
            VariableSymbol and isinstance(sym, VariableSymbol)
        ):
            init = getattr(sym, "initializer", None)
            name = getattr(sym, "name", None)
            if init is not None and name:
                src_refs = expr_net_refs(init)
                src_sels = {n: s for n, s, _a in src_refs if s}
                approx = any(a for _n, _s, a in src_refs)
                snip = f"{name} = init"
                try:
                    snip = (
                        f"{name} <- {','.join(n+(s or '') for n,s,_ in src_refs[:4])}"
                        if src_refs
                        else snip
                    )
                except Exception:
                    pass
                for src, ssel, _sa in src_refs:
                    add_local(
                        owner_hier,
                        src,
                        name,
                        "assign",
                        sym,
                        snip,
                        src_sel=ssel,
                        src_sels=src_sels or None,
                        select_approx=approx,
                    )
            return
        if isinstance(sym, ProceduralBlockSymbol):
            pk = getattr(sym, "procedureKind", None)
            ek = _edge_kind_for_procedure(pk)
            try:
                walk_stmt(owner_hier, sym.body, sym, edge_kind=ek)
            except Exception:
                pass
            try:
                for ch in sym:
                    walk_scope(hier, ch, owner_hier=owner_hier)
            except Exception:
                pass
            return
        if isinstance(sym, InstanceSymbol):
            return
        try:
            for ch in sym:
                if isinstance(ch, InstanceSymbol):
                    # Port actuals bind in the owning instance, not generate path
                    walk_instance(owner_hier, ch)
                else:
                    walk_scope(hier, ch, owner_hier=owner_hier)
        except Exception:
            pass

    def walk_instance(parent_hier: str, inst: Any) -> None:
        walked[0] += 1
        if walked[0] > max_walk:
            return
        name = getattr(inst, "name", "") or ""
        # hierarchicalPath includes generate-for indices (g_lane[0].u_lane)
        hp = getattr(inst, "hierarchicalPath", None)
        if hp:
            hier = str(hp)
        else:
            hier = f"{parent_hier}.{name}" if parent_hier else name
        # Cone: only enter instances on seed ancestor chain
        if not is_cone_hier(hier, cone_prefs):
            skipped_inst[0] += 1
            return
        try:
            body = inst.body
            mtype = body.name
        except Exception:
            body = None
            mtype = "?"
        fp, _ = _file_line(sm, body if body is not None else inst)
        g.inst_info[hier] = {
            "module": mtype,
            "file": fp,
            "inst": name,
            "parent": parent_hier,
        }
        pcs = []
        try:
            pcs = list(inst.portConnections)
        except Exception:
            pcs = []
        for pc in pcs:
            try:
                formal = pc.port.name
                expr = getattr(pc, "expression", None)
                actuals = expr_net_refs(expr) if expr is not None else []
                if not formal or not actuals:
                    continue
                snip = f".{formal}({actuals[0][0]}{actuals[0][1] or ''})"
                ev = _ev(sm, inst, snip, via="port")
                if actuals[0][1]:
                    ev["src_sel"] = actuals[0][1]
                    ev["src_sels"] = {actuals[0][0]: actuals[0][1]}
                if any(a for _n, _s, a in actuals):
                    ev["select_approx"] = True
                # Actuals live on the parent *module instance* (not generate cell)
                ph = parent_hier if parent_hier else hier
                for actual, asel, aa in actuals:
                    ssk = _norm_sel_key(asel, aa)
                    src_p: NetKey = netkey(ph, actual, ssk or None)
                    dst_c: NetKey = netkey(hier, formal, "")
                    ev_f = dict(ev)
                    if ssk:
                        ev_f["src_sel"] = ssk
                        ev_f["src_sels"] = {actual: ssk}
                    elif asel:
                        ev_f["src_sel"] = asel
                        ev_f["src_sels"] = {actual: asel}
                    if aa:
                        ev_f["select_approx"] = True
                    g.add(src_p, Edge(dst=dst_c, kind="port", evidence=ev_f))
                    g.add(
                        dst_c,
                        Edge(
                            dst=src_p,
                            kind="port",
                            evidence=dict(ev_f, via="port_rev"),
                        ),
                    )
                    g.n_port += 1
                    # actual with element select: whole ↔ element on parent
                    if ssk:
                        whole = netkey(ph, actual, "")
                        if whole != src_p:
                            ev_el = _ev(
                                sm,
                                inst,
                                f"{actual}{ssk} ⊂ {actual}",
                                via="array_el",
                                dst_sel=ssk,
                            )
                            g.add(
                                src_p,
                                Edge(dst=whole, kind="array_el", evidence=ev_el),
                            )
                            g.add(
                                whole,
                                Edge(
                                    dst=src_p,
                                    kind="array_el",
                                    evidence=dict(ev_el, via="array_el_rd"),
                                ),
                            )
            except Exception:
                continue
        if body is None:
            return
        try:
            for ch in body:
                if isinstance(ch, InstanceSymbol):
                    walk_instance(hier, ch)
                else:
                    # local logic / generate under this cone instance
                    walk_scope(hier, ch, owner_hier=hier)
        except Exception:
            pass

    for tinst in list(root.topInstances):
        walk_instance("", tinst)

    # Mirror whole-port connections onto matching element keys so
    # lane_y[i] → in_i[i] without bridging [i]↔[j] through whole.
    _mirror_port_elements(g, sm)

    _log(
        f"graph walk={walked[0]} skip_inst={skipped_inst[0]} "
        f"insts={len(g.inst_info)} gen_blocks={len(g.gen_blocks)} "
        f"fwd={len(g.forward)} assign={g.n_assign} port={g.n_port} "
        f"proc={g.n_proc} comb={g.n_comb} "
        f"lit_sel={g.n_literal_sel} approx_sel={g.n_approx_sel}",
        t0,
    )
    if g.sel_examples:
        _log(f"literal-sel examples (up to 12):", t0)
        for ex in g.sel_examples[:8]:
            _log(
                f"  {ex.get('kind')} {ex.get('src')} -> {ex.get('dst')} "
                f"dst_sel={ex.get('dst_sel')} src_sels={ex.get('src_sels')} "
                f"L{ex.get('line')}",
                t0,
            )
    return g


def _mirror_port_elements(g: Graph, sm: Any) -> None:
    """For whole↔whole port edges, add element↔element ports for shared indices."""
    wholes: List[Tuple[NetKey, NetKey, Edge]] = []
    for src, edges in list(g.forward.items()):
        if src[2]:
            continue
        for e in edges:
            if e.kind != "port" or e.dst[2]:
                continue
            wholes.append((src, e.dst, e))
    all_keys = set(g.forward.keys()) | set(g.backward.keys())
    for src, dst, e in wholes:
        sels: Set[str] = set()
        for nk in all_keys:
            if nk[0] == src[0] and nk[1] == src[1] and nk[2]:
                sels.add(nk[2])
            if nk[0] == dst[0] and nk[1] == dst[1] and nk[2]:
                sels.add(nk[2])
        for sel in sels:
            s_el = netkey(src[0], src[1], sel)
            d_el = netkey(dst[0], dst[1], sel)
            if s_el == d_el:
                continue
            snip = e.evidence.get("snippet") or f".port({src[1]}{sel})"
            ev = dict(
                e.evidence,
                via="port_el",
                src_sel=sel,
                dst_sel=sel,
                snippet=f"{snip} el{sel}",
            )
            g.add(s_el, Edge(dst=d_el, kind="port", evidence=ev))
            g.add(
                d_el,
                Edge(
                    dst=s_el,
                    kind="port",
                    evidence=dict(ev, via="port_el_rev"),
                ),
            )


def resolve_path(g: Graph, path: str) -> Optional[Tuple[NetKey, Optional[str], bool]]:
    """hierarchy → (NetKey with select, seed_sel, approx)."""
    path = path.strip()
    if not path or path in g.inst_info:
        return None
    parts = path.split(".")
    last = parts[-1]
    base, sel, approx = parse_path_tail(last)
    nsel = _norm_sel_key(sel, approx)
    for i in range(len(parts) - 1, 0, -1):
        pref = ".".join(parts[:i])
        if pref in g.inst_info:
            rest = parts[i:]
            if len(rest) == 1:
                k = netkey(pref, base, nsel or None, approx=approx)
                # Prefer exact element key if it appears in the graph; else whole.
                if nsel and k not in g.forward and k not in g.backward:
                    # still OK — seed may only touch whole via array_el after expand
                    pass
                return (k, nsel or sel, approx)
            break
    return None


def _element_keys_for_whole(g: Graph, k: NetKey) -> List[NetKey]:
    """If k is whole-net (sel==''), return all element keys same hier+base in graph."""
    if len(k) < 3 or k[2]:
        return []
    out: List[NetKey] = []
    seen: Set[NetKey] = set()
    for store in (g.forward, g.backward):
        for nk in store:
            if nk[0] == k[0] and nk[1] == k[1] and nk[2] and nk not in seen:
                seen.add(nk)
                out.append(nk)
    return out


def bi_meet(
    g: Graph,
    a_keys: List[Tuple[str, NetKey]],
    b_keys: List[Tuple[str, NetKey]],
    *,
    max_hops: int,
    max_nodes: int,
    t0: float,
    check_id: str,
    # path → sel_class (normalized sel, "" whole, or approx:path)
    a_sel_class: Optional[Dict[str, str]] = None,
    b_sel_class: Optional[Dict[str, str]] = None,
    allow_base_meet: bool = True,
) -> Dict[str, Any]:
    """
    Bi-meet with **bit-slice label isolation**.

    Expansion is **undirected** on the structural graph (forward ∪ reverse
    neighbors for both a and b). Endpoint groups may both be module inputs,
    both outputs, or mixed — port direction and a/b ordering do not require
    a=driver / b=load. Assign/FF edges still carry direction for evidence,
    but search walks them both ways for connectivity.

    Labels (hierarchy paths) are stored per ``sel_class`` so ``bus[0]`` and
    ``bus[1]`` never OR-merge into one structural meet without annotation.

    Pair emission uses :func:`annotate_pair_slice` (slice_policy).
    """
    from pyhirewalk.conn.slice_policy import annotate_pair_slice

    a_sel_class = a_sel_class or {}
    b_sel_class = b_sel_class or {}

    def _undirected_neighbors(key: NetKey) -> List[Tuple[NetKey, Evidence]]:
        out: List[Tuple[NetKey, Evidence]] = []
        for e in g.forward.get(key, []):
            out.append((e.dst, e.evidence))
        for src, e in g.backward.get(key, []):
            # reverse walk: keep evidence; mark via for reconstruct clarity
            ev = dict(e.evidence)
            via = str(ev.get("via") or "")
            if via and not via.endswith("_rev") and via not in (
                "port_rev",
                "array_el_rd",
            ):
                ev = dict(ev, via=f"{via}_rev")
            out.append((src, ev))
        return out

    def _is_array_el_via(ev: Evidence) -> bool:
        via = str(ev.get("via") or "")
        return via.startswith("array_el")

    def _next_pins(
        key: NetKey, nk: NetKey, ev: Evidence, pins: frozenset
    ) -> Optional[frozenset]:
        """Track element→whole pin; ban whole→other-element (wrong-index hop).

        pins: frozenset of ((hier, base), sel) — which element "owns" the whole
        for this search branch.
        """
        d = dict(pins)
        same_base = key[0] == nk[0] and key[1] == nk[1]
        if not same_base or (not key[2] and not nk[2]):
            return frozenset(d.items())
        base_id = (key[0], key[1])
        # element → whole: pin this element onto the base
        if key[2] and not nk[2]:
            d[base_id] = key[2]
            return frozenset(d.items())
        # whole → element
        if not key[2] and nk[2]:
            pin = d.get(base_id)
            # Only enforce pin on explicit array_el bookkeeping edges.
            # Real data edges whole→el (if any) still allowed when unpinned.
            if _is_array_el_via(ev) and pin is not None and pin != nk[2]:
                return None
            if pin is None and _is_array_el_via(ev):
                d[base_id] = nk[2]
            return frozenset(d.items())
        # element → other element of same base (no whole): only if same sel
        if key[2] and nk[2] and key[2] != nk[2] and _is_array_el_via(ev):
            return None
        return frozenset(d.items())

    # lab_*[net][sel_class] = set of seed paths
    lab_a: Dict[NetKey, Dict[str, Set[str]]] = {}
    lab_b: Dict[NetKey, Dict[str, Set[str]]] = {}
    prev_a: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
    prev_b: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]] = {}
    # queue: (key, hops, pins) — pins prevent wrong-element hops via whole
    qa: Deque[Tuple[NetKey, int, frozenset]] = deque()
    qb: Deque[Tuple[NetKey, int, frozenset]] = deque()

    def _cls_a(path: str) -> str:
        return a_sel_class.get(path, "")

    def _cls_b(path: str) -> str:
        return b_sel_class.get(path, "")

    def _add_labels(
        lab: Dict[NetKey, Dict[str, Set[str]]],
        key: NetKey,
        class_to_paths: Dict[str, Set[str]],
    ) -> bool:
        """Merge labels; return True if something new was added."""
        changed = False
        bucket = lab.setdefault(key, {})
        for sc, paths in class_to_paths.items():
            if not paths:
                continue
            cur = bucket.setdefault(sc, set())
            before = len(cur)
            cur |= paths
            if len(cur) > before:
                changed = True
        return changed

    def _seed_side(
        path: str,
        k: NetKey,
        lab: Dict[NetKey, Dict[str, Set[str]]],
        prev: Dict[NetKey, Tuple[Optional[NetKey], Optional[Evidence]]],
        q: Deque[Tuple[NetKey, int, frozenset]],
        sc: str,
    ) -> None:
        # Whole-net: also seed every known element of that base.
        # Element seed: also expand whole base, but pin whole to that element
        # so array_el cannot hop to a different index.
        keys: List[NetKey] = [k]
        if not k[2]:
            keys.extend(_element_keys_for_whole(g, k))
        else:
            keys.append(netkey(k[0], k[1], ""))
        for kk in keys:
            first = kk not in lab
            _add_labels(lab, kk, {sc: {path}})
            if first:
                prev[kk] = (None, None)
            # pin companion whole / element seeds from an element path
            if k[2]:
                pins = frozenset({((k[0], k[1]), k[2])})
            else:
                pins = frozenset()
            q.append((kk, 0, pins))

    for path, k in a_keys:
        _seed_side(path, k, lab_a, prev_a, qa, _cls_a(path))
    for path, k in b_keys:
        _seed_side(path, k, lab_b, prev_b, qb, _cls_b(path))

    meets: List[NetKey] = [k for k in lab_a if k in lab_b]
    nodes = 0
    hit_max_nodes = False
    hit_max_hops = False
    n_array_el_blocked = 0
    while (qa or qb) and nodes < max_nodes:
        if qb and (not qa or len(qb) <= len(qa)):
            side, q = "b", qb
        elif qa:
            side, q = "a", qa
        else:
            break
        key, hops, pins = q.popleft()
        if hops >= max_hops:
            hit_max_hops = True
            continue
        nodes += 1
        if side == "a":
            lab, prev, qside, other = lab_a, prev_a, qa, lab_b
        else:
            lab, prev, qside, other = lab_b, prev_b, qb, lab_a
        src_labels = lab.get(key) or {}
        for nk, ev in _undirected_neighbors(key):
            new_pins = _next_pins(key, nk, ev, pins)
            if new_pins is None:
                n_array_el_blocked += 1
                continue
            first = nk not in lab
            if first:
                prev[nk] = (key, ev)
            grew = _add_labels(lab, nk, src_labels)
            if first or grew:
                qside.append((nk, hops + 1, new_pins))
            if nk in other and (first or grew):
                meets.append(nk)
    if nodes >= max_nodes and (qa or qb):
        hit_max_nodes = True
    # Bind before pair emission — long weak pairs are dropped when search truncated.
    truncated = bool(hit_max_nodes or hit_max_hops)

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
            if out and out[-1].get("file") == e.get("file") and out[-1].get(
                "line"
            ) == e.get("line"):
                continue
            out.append(e)
        return out

    def flat_paths(lab: Dict[str, Set[str]]) -> List[Tuple[str, str]]:
        """List of (path, sel_class)."""
        rows: List[Tuple[str, str]] = []
        for sc, paths in lab.items():
            for p in paths:
                rows.append((p, sc))
        return rows

    # path → primary NetKey seed
    a_path_key = {p: k for p, k in a_keys}
    b_path_key = {p: k for p, k in b_keys}

    # Gather candidate meets per (src,dst); pick best for accurate select match
    cand: Dict[Tuple[str, str, str, str], List[NetKey]] = {}
    # key: (src, dst, sc_a, sc_b)
    for mk in meets:
        la = lab_a.get(mk) or {}
        lb = lab_b.get(mk) or {}
        for src, sc_a in flat_paths(la):
            for dst, sc_b in flat_paths(lb):
                cand.setdefault((src, dst, sc_a, sc_b), []).append(mk)

    def _meet_score(mk: NetKey, src: str, dst: str) -> int:
        """Higher is better — prefer meets on seed keys / matching select."""
        score = 0
        ak = a_path_key.get(src)
        bk = b_path_key.get(dst)
        if ak is not None and mk == ak:
            score += 80
        if bk is not None and mk == bk:
            score += 100
        if ak is not None and mk[0] == ak[0] and mk[1] == ak[1]:
            score += 20
            if ak[2] and mk[2] == ak[2]:
                score += 40
        if bk is not None and mk[0] == bk[0] and mk[1] == bk[1]:
            score += 25
            if bk[2] and mk[2] == bk[2]:
                score += 50
            elif bk[2] and mk[2] and mk[2] != bk[2]:
                score -= 80  # wrong element of same array
        # Prefer short evidence chains
        score -= min(len(reconstruct(mk)), 30)
        return score

    pairs = []
    seen_pair: Set[Tuple[str, str]] = set()
    n_suppressed = 0
    for (src, dst, sc_a, sc_b), mks in cand.items():
        pk = (src, dst)
        if pk in seen_pair:
            continue
        # STRICT: two different literal slice classes never pair *unless*
        # there is a direct structural edge between those element keys
        # (e.g. pipeline q_s[0] -> q_s[1] is intentional, not a false merge).
        if (
            sc_a
            and sc_b
            and sc_a != sc_b
            and not sc_a.startswith("approx:")
            and not sc_b.startswith("approx:")
        ):
            ak0 = a_path_key.get(src)
            bk0 = b_path_key.get(dst)
            direct_ab = False
            if ak0 is not None and bk0 is not None:
                direct_ab = any(e.dst == bk0 for e in g.forward.get(ak0, [])) or any(
                    e.dst == ak0 for e in g.forward.get(bk0, [])
                )
            if not direct_ab:
                n_suppressed += 1
                continue
        if (bool(sc_a) ^ bool(sc_b)) and (
            (sc_a and not sc_a.startswith("approx:"))
            or (sc_b and not sc_b.startswith("approx:"))
        ):
            if not allow_base_meet:
                n_suppressed += 1
                continue
        # unique meet keys preserve order then score
        uniq_m: List[NetKey] = []
        seen_m: Set[NetKey] = set()
        for mk in mks:
            if mk not in seen_m:
                seen_m.add(mk)
                uniq_m.append(mk)
        mk = max(uniq_m, key=lambda m: _meet_score(m, src, dst))
        bk = b_path_key.get(dst)
        ak = a_path_key.get(src)
        # Partitioned-array destinations (multiple elements written by different
        # sources, e.g. apu_operands[i] = op_*): require a *direct* edge from
        # the source base to that exact element. Soft bit-select seeds (no such
        # multi-writer structure) use normal bi-meet + seed labels.
        evidence: Optional[List[Evidence]] = None
        if ak is not None and bk is not None:
            ak_whole = netkey(ak[0], ak[1], "")
            writers_to_el = []
            # Prefer real data edges from the *element/source key*, then whole.
            # Skip array_el* for "direct" evidence (those are bookkeeping links).
            for src_k in (ak, ak_whole):
                for e in g.forward.get(src_k, []):
                    if e.kind.startswith("array_el"):
                        continue
                    if e.dst[0] == bk[0] and e.dst[1] == bk[1] and e.dst[2]:
                        writers_to_el.append((src_k, e))
            if bk[2]:
                # Exact element destination: must have direct non-array_el edge
                direct_e = None
                for src_k, e in writers_to_el:
                    if e.dst == bk:
                        # prefer edge originating at ak (not only whole)
                        if src_k == ak or direct_e is None:
                            direct_e = e
                        if src_k == ak:
                            break
                # also allow ak -> bk even when bk.sel matches via full key equality
                if direct_e is None:
                    for e in g.forward.get(ak, []):
                        if e.dst == bk and not e.kind.startswith("array_el"):
                            direct_e = e
                            break
                if direct_e is None:
                    if writers_to_el:
                        # Source drives *other* elements of this base, not bk
                        # (e.g. lane1.y_q -> lane_yq[1], not [0]).
                        n_suppressed += 1
                        continue
                    # soft bit-select: no multi-writer element map from this src
                    evidence = reconstruct(mk)
                else:
                    evidence = [direct_e.evidence]
                    mk = direct_e.dst
            elif writers_to_el:
                # Whole destination but source maps into partitioned array:
                _sk, e0 = writers_to_el[0]
                # prefer writers from ak itself
                for src_k, e in writers_to_el:
                    if src_k == ak:
                        e0 = e
                        break
                evidence = [e0.evidence]
                mk = e0.dst
        if evidence is None:
            evidence = reconstruct(mk)
        # Under search truncation, drop long weak pairs (undirected graphs on
        # large cores otherwise report almost everything as connected).
        if truncated and len(evidence) > 8:
            n_suppressed += 1
            continue
        seen_pair.add(pk)
        slice_meta = annotate_pair_slice(
            src, dst, evidence, allow_base_meet=allow_base_meet
        )
        if not slice_meta.get("pair_allowed", True):
            n_suppressed += 1
            continue
        # Upgrade level when meet/evidence carries matching element select
        level = slice_meta["connectivity_level"]
        if bk is not None and bk[2] and (
            (mk[2] == bk[2] and mk[1] == bk[1])
            or any(e.get("dst_sel") == bk[2] for e in evidence)
        ):
            from pyhirewalk.conn.slice_policy import LEVEL_SLICE_HINT

            if level == "base":
                level = LEVEL_SLICE_HINT
        pairs.append(
            {
                "src": src,
                "dst": dst,
                "meet": {
                    "hier": mk[0],
                    "net": mk[1],
                    "sel": mk[2] if len(mk) > 2 else "",
                },
                "evidence": evidence,
                "connectivity_level": level,
                "src_sel": slice_meta.get("src_sel"),
                "dst_sel": slice_meta.get("dst_sel") or (bk[2] if bk and bk[2] else None),
                "src_select_approx": slice_meta.get("src_select_approx"),
                "dst_select_approx": slice_meta.get("dst_select_approx"),
                "slice_notes": slice_meta.get("slice_notes") or [],
                "src_sel_class": sc_a,
                "dst_sel_class": sc_b,
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
    if truncated:
        _log(
            f"meet check={check_id} TRUNCATED "
            f"hit_max_nodes={hit_max_nodes} hit_max_hops={hit_max_hops} "
            f"nodes={nodes}/{max_nodes} hops_cap={max_hops} "
            f"queue_left={len(qa) + len(qb)}",
            t0,
        )
    _log(
        f"meet check={check_id} nodes={nodes} pairs={len(pairs)} "
        f"slice_suppressed={n_suppressed} array_el_blocked={n_array_el_blocked} "
        f"truncated={truncated} "
        f"|Va|={len(lab_a)} |Vb|={len(lab_b)}",
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
            "slice_pairs_suppressed": n_suppressed,
            "array_el_hops_blocked": n_array_el_blocked,
            "truncated": truncated,
            "hit_max_nodes": hit_max_nodes,
            "hit_max_hops": hit_max_hops,
            "max_nodes": max_nodes,
            "max_hops": max_hops,
            "queue_left": len(qa) + len(qb),
        },
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class HierPyslangApp:
    """pyslang structural COI: env+defines → elab → graph → bi-meet."""

    def __init__(
        self,
        *,
        config: Path,
        out: Optional[Path] = None,
        filelist: Optional[Path] = None,
        top: Optional[str] = None,
        includes: Optional[List[str]] = None,
        max_hops: int = 48,
        max_nodes: int = 8000,
        checks: Optional[List[Dict[str, Any]]] = None,
        defines: Optional[Dict[str, str]] = None,
        env: Optional[Dict[str, str]] = None,
        cone_walk: bool = True,
        cone_files: bool = False,
        modules_json: Optional[Path] = None,
        allow_base_meet: bool = True,
    ) -> None:
        self.config = Path(config)
        self.out = Path(out) if out else None
        self.filelist = Path(filelist) if filelist else None
        self.top = top
        self.includes = list(includes or [])
        self.max_hops = max_hops
        self.max_nodes = max_nodes
        self._checks = checks
        self._defines = defines
        self._env = env
        self.cone_walk = cone_walk
        self.cone_files = cone_files
        self.modules_json = Path(modules_json) if modules_json else None
        # whole-net seed may pair with sliced seed at connectivity_level=base
        self.allow_base_meet = allow_base_meet

    def run(self) -> int:
        t0 = time.perf_counter()
        _log(f"START config={self.config}", t0)

        from pyhirewalk.run_config import (
            load_hier_conn_inputs,
            load_run_config,
            parse_env_block,
        )

        cfg = load_run_config(self.config)
        env = dict(self._env if self._env is not None else cfg.env)
        apply_config_env(env, t0=t0)

        if self._checks is None:
            checks, defines_c, _ = load_hier_conn_inputs(self.config)
        else:
            checks = self._checks
            defines_c = {}
        defines = dict(cfg.defines)
        defines.update(defines_c)
        if self._defines:
            defines.update(self._defines)

        top = self.top or cfg.top or ""
        if not top:
            _log("ERROR: top module required (config top or --top)", t0)
            return 2

        fl = self.filelist or cfg.filelist
        if fl is None:
            _log(
                "ERROR: no filelist in config (need 'filelist' pointing at "
                "RTL .f or path list). run_conn_check.checks alone is not enough.",
                t0,
            )
            return 2
        fl_path = Path(fl)
        if not fl_path.is_file():
            _log(
                f"ERROR: filelist not found: {fl_path} "
                f"(resolved from config; cwd={Path.cwd()})",
                t0,
            )
            return 2

        # Full EDA expand (-f/-F, +incdir+, +define+, $VAR) — not naive line skip
        files, fl_incdirs, fl_defines, fl_errors = load_rtl_sources(
            fl_path,
            env=env,
            index_cwd=cfg.index_cwd,
            t0=t0,
        )
        # merge +define+ from filelist under config defines (config wins)
        for k, v in fl_defines.items():
            defines.setdefault(k, v)
        if fl_errors:
            for e in fl_errors[:8]:
                _log(f"filelist warn: {e}", t0)
        if not files:
            _log(
                f"ERROR: empty filelist after expand: {fl_path} "
                f"(checks/hierarchies are loaded separately; "
                f"n_checks will be {len(checks) if checks else 0})",
                t0,
            )
            return 2

        map_path = self.modules_json or cfg.modules_json
        if self.cone_files:
            before_n = len(files)
            files = select_cone_files(
                files, modules_json=map_path, top=top, t0=t0
            )
            if not files:
                _log(
                    f"ERROR: cone-files removed all sources "
                    f"(was {before_n}). Try without --cone-files.",
                    t0,
                )
                return 2

        includes = list(self.includes)
        includes.extend(fl_incdirs)
        # default includes from cwd / common SV roots
        if cfg.index_cwd:
            includes.append(str(Path(cfg.index_cwd).resolve()))
        # env-based include hints
        for key in ("IBEX_ROOT", "RTL_ROOT", "PROJ"):
            if key in env:
                root = Path(env[key])
                for sub in ("rtl", "vendor/lowrisc_ip/ip/prim/rtl",
                            "vendor/lowrisc_ip/ip/prim_generic/rtl"):
                    p = root / sub
                    if p.is_dir():
                        includes.append(str(p.resolve()))
        # unique preserve order
        seen_i: Set[str] = set()
        uniq_inc: List[str] = []
        for i in includes:
            i = expand_env_str(i, env)
            if i not in seen_i:
                seen_i.add(i)
                uniq_inc.append(i)

        if not checks:
            _log(
                "ERROR: no run_conn_check.checks "
                "(hierarchies a/b must be under run_conn_check.checks)",
                t0,
            )
            return 2
        _log(f"n_checks={len(checks)} n_rtl_sources={len(files)} top={top}", t0)

        t_c0 = time.perf_counter()
        try:
            # Optional top-module parameters from config JSON "parameters"
            parameters: Dict[str, str] = {}
            raw_params = (cfg.raw or {}).get("parameters")
            if isinstance(raw_params, dict):
                parameters = {str(k): str(v) for k, v in raw_params.items()}
            comp, root, diags, fatal = compile_design(
                files=files,
                top=top,
                defines=defines,
                includes=uniq_inc,
                t0=t0,
                parameters=parameters or None,
            )
        except Exception as e:
            _log(f"ERROR compile: {e}", t0)
            return 3
        t_comp = time.perf_counter() - t_c0
        sm = comp.sourceManager

        cone_prefs: Optional[Set[str]] = None
        if self.cone_walk:
            cone_prefs = cone_instance_prefixes(checks)
            _log(
                f"cone_walk on: {len(cone_prefs)} prefixes "
                f"e.g. {sorted(cone_prefs)[:6]}",
                t0,
            )

        t_g0 = time.perf_counter()
        g = build_graph(root, sm, t0=t0, cone_prefs=cone_prefs)
        t_graph = time.perf_counter() - t_g0

        from pyhirewalk.conn.slice_policy import (
            seeds_from_paths,
            sel_class,
            summarize_seed_group,
            SeedRec,
        )

        out_checks: List[Dict[str, Any]] = []
        total_pairs = 0
        t_s0 = time.perf_counter()
        for ch in checks:
            cid = ch["id"]
            _log(f"check START id={cid}", t0)
            a_keys: List[Tuple[str, NetKey]] = []
            b_keys: List[Tuple[str, NetKey]] = []
            a_sc: Dict[str, str] = {}
            b_sc: Dict[str, str] = {}
            miss: List[Dict[str, Any]] = []
            seed_sel: Dict[str, Any] = {}
            seeds_a: List[SeedRec] = []
            seeds_b: List[SeedRec] = []

            for p in ch["a"]:
                r = resolve_path(g, p)
                if r is None:
                    miss.append({"src": p, "dst": None, "reason": "path_miss"})
                    _log(f"  path_miss a {p}", t0)
                    continue
                k, sel, approx = r
                # Prefer slice_policy parse for sel_class (path string)
                recs = seeds_from_paths([p])
                rec = recs[0] if recs else None
                if rec is None:
                    continue
                # Align hier/base/sel with resolved NetKey (authoritative from graph)
                key_sel = k[2] if len(k) > 2 and k[2] else (sel if sel else rec.sel)
                rec = SeedRec(
                    path=p,
                    hier=k[0],
                    base=k[1],
                    sel=key_sel if key_sel else None,
                    select_approx=approx or rec.select_approx,
                )
                seeds_a.append(rec)
                a_keys.append((p, k))
                a_sc[p] = sel_class(rec)
                seed_sel[p] = {
                    "sel": rec.sel,
                    "approx": rec.select_approx,
                    "sel_class": a_sc[p],
                    "net_key": list(k),
                }
                _log(
                    f"  seed a {p} -> {k} sel={rec.sel} class={a_sc[p]!r} "
                    f"approx={rec.select_approx}",
                    t0,
                )
            for p in ch["b"]:
                r = resolve_path(g, p)
                if r is None:
                    miss.append({"src": None, "dst": p, "reason": "path_miss"})
                    _log(f"  path_miss b {p}", t0)
                    continue
                k, sel, approx = r
                recs = seeds_from_paths([p])
                rec = recs[0] if recs else None
                if rec is None:
                    continue
                key_sel = k[2] if len(k) > 2 and k[2] else (sel if sel else rec.sel)
                rec = SeedRec(
                    path=p,
                    hier=k[0],
                    base=k[1],
                    sel=key_sel if key_sel else None,
                    select_approx=approx or rec.select_approx,
                )
                seeds_b.append(rec)
                b_keys.append((p, k))
                b_sc[p] = sel_class(rec)
                seed_sel[p] = {
                    "sel": rec.sel,
                    "approx": rec.select_approx,
                    "sel_class": b_sc[p],
                    "net_key": list(k),
                }
                _log(
                    f"  seed b {p} -> {k} sel={rec.sel} class={b_sc[p]!r} "
                    f"approx={rec.select_approx}",
                    t0,
                )

            seed_summary = {
                "a": summarize_seed_group(seeds_a),
                "b": summarize_seed_group(seeds_b),
                "policy": (
                    "Different literal bit selects never share a pair; "
                    "base-level meet is labeled connectivity_level=base."
                ),
            }
            _log(
                f"  seed summary a={seed_summary['a']} b={seed_summary['b']}",
                t0,
            )

            if a_keys and b_keys:
                res = bi_meet(
                    g,
                    a_keys,
                    b_keys,
                    max_hops=self.max_hops,
                    max_nodes=self.max_nodes,
                    t0=t0,
                    check_id=cid,
                    a_sel_class=a_sc,
                    b_sel_class=b_sc,
                    allow_base_meet=self.allow_base_meet,
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
                    f"  PAIR {pr['src']} -> {pr['dst']} "
                    f"level={pr.get('connectivity_level')} "
                    f"src_sel={pr.get('src_sel')} dst_sel={pr.get('dst_sel')} "
                    f"ev_n={len(pr.get('evidence') or [])}",
                    t0,
                )
                for note in pr.get("slice_notes") or []:
                    _log(f"    slice_note: {note}", t0)
            out_checks.append(
                {
                    "id": cid,
                    "pairs": res["pairs"],
                    "unconnected": res["unconnected"],
                    "stats": res["stats"],
                    "seed_select": seed_sel,
                    "seed_summary": seed_summary,
                    "engine": "hier_pyslang",
                }
            )
        t_search = time.perf_counter() - t_s0
        total = time.perf_counter() - t0

        doc = {
            "schema_version": 1,
            "meta": {
                "tool": "hier_pyslang",
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "config": str(self.config.resolve()),
                "top": top,
                "filelist": str(Path(fl).resolve()),
                "n_files": len(files),
                "defines": sorted(defines.keys()),
                "env_keys": sorted(env.keys()),
                "includes": uniq_inc,
                "n_diags": len(diags),
                "fatal": fatal,
                "graph": {
                    "n_instances": len(g.inst_info),
                    "n_gen_blocks": len(g.gen_blocks),
                    "n_assign": g.n_assign,
                    "n_port": g.n_port,
                    "n_proc": g.n_proc,
                    "n_literal_sel_edges": g.n_literal_sel,
                    "n_approx_sel_edges": g.n_approx_sel,
                    "literal_sel_examples": g.sel_examples,
                    "fwd_keys": len(g.forward),
                    "cone_walk": self.cone_walk,
                    "cone_files": self.cone_files,
                    "n_files_compiled": len(files),
                },
                "timings_sec": {
                    "compile": round(t_comp, 6),
                    "graph_extract": round(t_graph, 6),
                    "search": round(t_search, 6),
                    "total": round(total, 6),
                },
                "stats": {"n_checks": len(checks), "n_pairs": total_pairs},
            },
            "checks": out_checks,
        }
        text = json.dumps(doc, indent=2) + "\n"
        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(text, encoding="utf-8")
            _log(f"wrote {self.out}", t0)
        else:
            sys.stdout.write(text)
        _log(
            f"TIMING compile={t_comp:.3f}s extract={t_graph:.3f}s "
            f"search={t_search:.3f}s TOTAL={total:.3f}s pairs={total_pairs}",
            t0,
        )
        print(f"TOTAL_HIER_PYSLANG_SEC: {total:.3f}", file=sys.stderr)
        return 0 if not fatal else 1

    @classmethod
    def main(cls, argv: Optional[List[str]] = None) -> int:
        ap = argparse.ArgumentParser(
            description="hier_pyslang: pyslang structural COI (generate + bit-select meta)"
        )
        ap.add_argument("--config", "-c", type=Path, required=True)
        ap.add_argument("-o", "--out", type=Path, default=None)
        ap.add_argument("--filelist", type=Path, default=None)
        ap.add_argument("--top", default=None)
        ap.add_argument(
            "-I",
            dest="includes",
            action="append",
            default=[],
            help="extra include dir (repeatable)",
        )
        ap.add_argument(
            "-D",
            "--define",
            action="append",
            default=[],
            metavar="NAME[=VAL]",
            help="extra define (merged on config defines)",
        )
        ap.add_argument("--max-hops", type=int, default=48)
        ap.add_argument("--max-nodes", type=int, default=8000)
        ap.add_argument(
            "--cone-walk",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="only extract graph under seed hierarchy prefixes (default on)",
        )
        ap.add_argument(
            "--cone-files",
            action="store_true",
            help="shrink filelist via modules_json (packages + top RTL dir)",
        )
        ap.add_argument(
            "--map",
            type=Path,
            default=None,
            help="modules JSON for --cone-files (else config modules_json)",
        )
        ap.add_argument(
            "--allow-base-meet",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="allow whole-net seed to pair with sliced seed "
            "(labeled connectivity_level=base; default on)",
        )
        args = ap.parse_args(argv)

        extra_defs: Dict[str, str] = {}
        for d in args.define:
            if "=" in d:
                k, v = d.split("=", 1)
                extra_defs[k.strip()] = v.strip()
            else:
                extra_defs[d.strip()] = "1"

        return cls(
            config=args.config,
            out=args.out,
            filelist=args.filelist,
            top=args.top,
            includes=args.includes,
            max_hops=args.max_hops,
            max_nodes=args.max_nodes,
            defines=extra_defs or None,
            cone_walk=args.cone_walk,
            cone_files=args.cone_files,
            modules_json=args.map,
            allow_base_meet=args.allow_base_meet,
        ).run()


def main(argv: Optional[List[str]] = None) -> int:
    return HierPyslangApp.main(argv)
