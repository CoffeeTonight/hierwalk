"""Pyslang structural (electrical) connectivity — no logic evaluation.

Uses hgrep/grep_hie ``module → file`` scoped RTL only, builds a
``pyslang.ast.Compilation``, then unions pure wiring:

* instance port connections (``.port(expr)``)
* continuous ``assign`` of wire-only expressions

Wire-only expressions: NamedValue, ElementSelect, RangeSelect, Conversion of
those, and Concatenation of those. Arithmetic / conditional / calls are ignored.

Produces per-a-signal lines locating which b bit-slice (if any) shares a net.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.hierarchy_grep import abs_rtl_path

LogFn = Optional[Callable[[str], None]]

# SourceManager forbids reusing the same path string across SyntaxTree loads.
_BUFFER_SEQ = itertools.count()


def _display_rtl_path(path: str) -> str:
    """Strip internal buffer suffixes (``#pyslang-electrical-N``) for reports."""
    text = str(path or "").strip()
    if "#pyslang-electrical-" in text:
        return text.split("#pyslang-electrical-", 1)[0]
    if "#probe" in text:
        return text.split("#probe", 1)[0]
    return text


def _require_pyslang():
    try:
        import pyslang  # noqa: F401
        from pyslang import ast, syntax

        return ast, syntax
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "pyslang electrical walk requires the 'pyslang' package "
            "(pip install pyslang)"
        ) from exc


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------


class _UF:
    def __init__(self) -> None:
        self._p: Dict[str, str] = {}
        self._meta: Dict[str, Dict[str, str]] = {}

    def add(self, x: str, *, file: str = "", node: str = "") -> None:
        if not x:
            return
        if x not in self._p:
            self._p[x] = x
            self._meta[x] = {"file": file or "", "node": node or x}

    def find(self, x: str) -> str:
        if x not in self._p:
            self.add(x)
        while self._p[x] != x:
            self._p[x] = self._p[self._p[x]]
            x = self._p[x]
        return x

    def union(self, a: str, b: str) -> None:
        if not a or not b:
            return
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def component(self, x: str) -> List[str]:
        if x not in self._p and not any(
            k == x or k.startswith(x + "[") for k in self._p
        ):
            return []
        # Match exact or best prefix keys present in the graph.
        keys = self._keys_matching(x)
        if not keys:
            return []
        roots = {self.find(k) for k in keys}
        out: List[str] = []
        for k in self._p:
            if self.find(k) in roots:
                out.append(k)
        return sorted(set(out))

    def _keys_matching(self, spec: str) -> List[str]:
        spec = (spec or "").strip()
        if not spec:
            return []
        if spec in self._p:
            return [spec]
        # bare path may appear only as selected form
        hits = [k for k in self._p if k == spec or k.startswith(spec + "[")]
        return hits

    def meta(self, x: str) -> Dict[str, str]:
        self.add(x)
        return dict(self._meta.get(self.find(x), self._meta.get(x, {})))

    def set_meta(self, x: str, *, file: str = "", node: str = "") -> None:
        self.add(x, file=file, node=node)
        m = self._meta.setdefault(x, {"file": "", "node": x})
        if file:
            m["file"] = file
        if node:
            m["node"] = node


# ---------------------------------------------------------------------------
# Expression → electrical terminal keys (no logic ops)
# ---------------------------------------------------------------------------


def _as_int(val: Any) -> Optional[int]:
    """Coerce pyslang ConstantValue / SVInt / int / str to Python int."""
    if val is None:
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    # pyslang.ConstantValue
    conv = getattr(val, "convertToInt", None)
    if callable(conv):
        try:
            return int(conv())
        except (TypeError, ValueError, RuntimeError):
            pass
    inner = getattr(val, "value", None)
    if inner is not None and inner is not val:
        got = _as_int(inner)
        if got is not None:
            return got
    try:
        text = str(val).strip()
        if not text or text in ("None", "<null>"):
            return None
        # SV styled ``2'b10`` / ``8'hff`` — take bit payload when present
        if "'" in text:
            payload = text.split("'", 1)[-1]
            payload = payload.lstrip("sS")
            if payload[:1] in "bBoOdDhH":
                base = {"b": 2, "B": 2, "o": 8, "O": 8, "d": 10, "D": 10, "h": 16, "H": 16}[
                    payload[0]
                ]
                return int(payload[1:].replace("_", ""), base)
            return int(payload.replace("_", ""), 0)
        return int(text, 0)
    except (TypeError, ValueError):
        return None


def _const_int(expr: Any) -> Optional[int]:
    """
    Constant index for selects — literals, parameters, and folded constant exprs.

    Uses pyslang elaboration (``expr.constant``, ``ParameterSymbol.value``).
    Runtime variables return None (not statically electrical-fixed).
    """
    if expr is None:
        return None

    # 1) pyslang already folded (BinaryOp N-1, param ref, etc.)
    c = getattr(expr, "constant", None)
    got = _as_int(c)
    if got is not None:
        return got

    name = type(expr).__name__
    kind = _kind_name(expr)

    # 2) Integer literal
    if name == "IntegerLiteral" or kind == "IntegerLiteral":
        return _as_int(getattr(expr, "value", None))

    # 3) NamedValue → Parameter / Enum value
    if name == "NamedValueExpression" or kind == "NamedValue":
        sym = getattr(expr, "symbol", None)
        if sym is None:
            return None
        sk = str(getattr(sym, "kind", "") or "")
        # Parameter / localparam / SpecParameter
        if "Parameter" in sk or type(sym).__name__ in (
            "ParameterSymbol",
            "SpecParameterSymbol",
        ):
            return _as_int(getattr(sym, "value", None))
        # Enum constant sometimes usable
        if "Enum" in sk or "EnumValue" in type(sym).__name__:
            return _as_int(getattr(sym, "value", None))
        return None

    # 4) Conversion of a constant
    if name == "ConversionExpression" or kind == "Conversion":
        return _const_int(getattr(expr, "operand", None))

    return None


def _kind_name(expr: Any) -> str:
    k = getattr(expr, "kind", None)
    if k is None:
        return type(expr).__name__
    s = str(k)
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    if ":" in s:
        s = s.split(":")[0].strip()
    return s.replace("ExpressionKind.", "")


def electrical_terminals(expr: Any) -> List[str]:
    """Return hierarchical terminal keys for a wire-only expression; else []."""
    if expr is None:
        return []
    name = type(expr).__name__
    kind = _kind_name(expr)

    if name == "NamedValueExpression" or kind == "NamedValue":
        sym = getattr(expr, "symbol", None)
        if sym is None:
            return []
        # Parameters are not nets — only used as select indices via _const_int.
        sk = str(getattr(sym, "kind", "") or "")
        if "Parameter" in sk or type(sym).__name__ in (
            "ParameterSymbol",
            "SpecParameterSymbol",
        ):
            return []
        hp = str(getattr(sym, "hierarchicalPath", "") or getattr(sym, "name", "") or "")
        return [hp] if hp else []

    if name == "ElementSelectExpression" or kind == "ElementSelect":
        bases = electrical_terminals(getattr(expr, "value", None))
        idx = _const_int(getattr(expr, "selector", None))
        if idx is None or not bases:
            return []
        return [f"{b}[{idx}]" for b in bases]

    if name == "RangeSelectExpression" or kind == "RangeSelect":
        bases = electrical_terminals(getattr(expr, "value", None))
        left = _const_int(getattr(expr, "left", None))
        right = _const_int(getattr(expr, "right", None))
        if left is None or right is None or not bases:
            return []
        return [f"{b}[{left}:{right}]" for b in bases]

    if name == "ConversionExpression" or kind == "Conversion":
        return electrical_terminals(getattr(expr, "operand", None))

    if name == "AssignmentExpression" or kind == "Assignment":
        # Output port connection: formal drives left (actual net).
        return electrical_terminals(getattr(expr, "left", None))

    if name == "EmptyArgumentExpression" or kind == "EmptyArgument":
        return []

    if name == "ConcatenationExpression" or kind == "Concatenation":
        # Pure concat of wire terminals only — link each operand as its own key.
        out: List[str] = []
        ops = getattr(expr, "operands", None) or getattr(expr, "elements", None) or ()
        try:
            for op in ops:
                t = electrical_terminals(op)
                if not t:
                    return []  # mixed with logic → reject whole concat
                out.extend(t)
        except TypeError:
            return []
        return out

    # Anything else (Binary, Conditional, Call, …) is not pure electrical wire.
    return []


def _file_of(comp: Any, loc: Any) -> str:
    if loc is None:
        return ""
    try:
        sm = comp.sourceManager
        buf = getattr(loc, "buffer", None)
        if buf is None:
            raw = sm.getFullPath(loc) if hasattr(sm, "getFullPath") else ""
        else:
            raw = str(sm.getFullPath(buf) or "")
        return _display_rtl_path(raw)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Graph build
# ---------------------------------------------------------------------------


def build_electrical_graph(
    rtl_files: Sequence[str],
    *,
    top: str = "",
    defines: Optional[Dict[str, str]] = None,
    on_log: LogFn = None,
) -> Tuple[_UF, List[str]]:
    """
    Compile *rtl_files* and return (union-find graph, diagnostics messages).

    *defines* is applied only as a note today; callers should pre-filter ifdef
    text when needed (hierarchy session already path-scopes files).
    """
    del defines  # compilation uses file text as-is; ifdef filter is pre-step if needed
    ast, syntax = _require_pyslang()
    uf = _UF()
    diags: List[str] = []
    files = [abs_rtl_path(f) for f in rtl_files if f and Path(f).is_file()]
    if not files:
        return uf, ["no rtl files for electrical compile"]

    if on_log:
        on_log(f"pyslang-electrical compile files={len(files)} top={top or '-'}")

    comp = ast.Compilation()
    trees_ok = 0
    for fpath in files:
        try:
            text = Path(fpath).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            diags.append(f"read fail {fpath}: {exc}")
            continue
        # Globally unique buffer path (counter) — same fpath#elec-0 twice in one
        # process used to yield empty Compilation (nets≈0) after the first call.
        buffer_path = f"{fpath}#pyslang-electrical-{next(_BUFFER_SEQ)}"
        try:
            tree = syntax.SyntaxTree.fromText(text, path=buffer_path)
            comp.addSyntaxTree(tree)
            trees_ok += 1
        except Exception as exc:
            diags.append(f"addSyntaxTree fail {fpath}: {exc}")
            continue

    if trees_ok == 0:
        return uf, diags or ["no syntax trees added"]

    try:
        root = comp.getRoot()
    except Exception as exc:
        return uf, [f"compilation getRoot failed: {exc}"]

    for d in list(comp.getAllDiagnostics() or ())[:12]:
        try:
            diags.append(str(d))
        except Exception:
            pass

    tops = list(root.topInstances or ())
    if on_log:
        on_log(
            f"pyslang-electrical elaborated tops={len(tops)} trees={trees_ok}"
        )

    def link(a: str, b: str, *, file: str = "") -> None:
        if not a or not b:
            return
        uf.add(a, file=file, node=a)
        uf.add(b, file=file, node=b)
        if file:
            uf.set_meta(a, file=file)
            uf.set_meta(b, file=file)
        uf.union(a, b)

    def handle_instance(inst: Any) -> None:
        if type(inst).__name__ != "InstanceSymbol":
            return
        try:
            pcs = list(inst.portConnections or ())
        except Exception:
            pcs = []
        for pc in pcs:
            port = getattr(pc, "port", None)
            if port is None:
                continue
            formal = str(getattr(port, "hierarchicalPath", "") or "")
            if not formal:
                continue
            floc = _file_of(comp, getattr(port, "location", None) or getattr(inst, "location", None))
            uf.add(formal, file=floc, node=formal)
            # Also link port's internal variable if distinct.
            internal = getattr(port, "internalSymbol", None)
            if internal is not None:
                ih = str(getattr(internal, "hierarchicalPath", "") or "")
                if ih and ih != formal:
                    link(formal, ih, file=floc)
            expr = getattr(pc, "expression", None)
            for actual in electrical_terminals(expr):
                link(formal, actual, file=floc)

    def handle_assign(sym: Any) -> None:
        if type(sym).__name__ != "ContinuousAssignSymbol":
            return
        asn = getattr(sym, "assignment", None)
        if asn is None:
            return
        left = electrical_terminals(getattr(asn, "left", None))
        right = electrical_terminals(getattr(asn, "right", None))
        if not left or not right:
            return  # not pure electrical both sides
        floc = _file_of(comp, getattr(sym, "location", None))
        for L in left:
            for R in right:
                link(L, R, file=floc)

    def visitor(sym: Any) -> int:
        try:
            handle_instance(sym)
            handle_assign(sym)
        except Exception:
            pass
        return 0  # VisitAction.Advance

    for top_inst in tops:
        try:
            top_inst.visit(visitor)
        except Exception as exc:
            diags.append(f"visit failed: {exc}")

    if on_log:
        on_log(
            f"pyslang-electrical nets≈{len(uf._p)} edges_linked"
            + (f" diags={len(diags)}" if diags else "")
        )
    return uf, diags


# ---------------------------------------------------------------------------
# Query + report rows
# ---------------------------------------------------------------------------


@dataclass
class ElectricalP2PRow:
    check_id: str
    a: str
    b_slice: str
    status: str  # PASS | FAIL
    fail_node: str = ""
    fail_rtl: str = ""
    note: str = ""


def _b_matches(terminal: str, b_bases: Sequence[str]) -> bool:
    for b in b_bases:
        if not b:
            continue
        if terminal == b or terminal.startswith(b + "["):
            return True
        # terminal is parent of b? rare
        if b.startswith(terminal + "["):
            return True
    return False


def _best_b_slices(component: Sequence[str], b_bases: Sequence[str]) -> List[str]:
    hits = [t for t in component if _b_matches(t, b_bases)]
    if not hits:
        return []
    # Prefer more specific (longer) terminals; drop pure base if a selected form exists.
    hits = sorted(set(hits), key=lambda s: (-len(s), s))
    selected = [h for h in hits if "[" in h]
    if selected:
        # keep all distinct selected forms (not only longest one base)
        return sorted(selected)
    return hits[:1]


def query_a_to_b(
    uf: _UF,
    *,
    check_id: str,
    a_specs: Sequence[str],
    b_specs: Sequence[str],
    a_fail: Optional[Dict[str, Tuple[str, str]]] = None,
) -> List[ElectricalP2PRow]:
    """
    One report row per a→b_slice mapping (or one FAIL row per unresolved a).

    *a_fail* maps a_spec → (fail_node, fail_rtl) when hierarchy resolve failed.
    """
    a_fail = a_fail or {}
    rows: List[ElectricalP2PRow] = []
    b_bases = [str(b).strip() for b in b_specs if str(b).strip()]

    for a in a_specs:
        a = str(a).strip()
        if not a:
            continue
        if a in a_fail:
            node, rtl = a_fail[a]
            rows.append(
                ElectricalP2PRow(
                    check_id=check_id,
                    a=a,
                    b_slice="",
                    status="FAIL",
                    fail_node=node or a,
                    fail_rtl=rtl or "",
                    note="hierarchy miss",
                )
            )
            continue

        comp = uf.component(a)
        if not comp and a not in uf._p:
            # try progressive strip of selects for lookup
            base = a.split("[", 1)[0]
            comp = uf.component(base)
        slices = _best_b_slices(comp, b_bases) if comp else []
        if not slices:
            # Keep fail_node as the a endpoint (not an arbitrary UF parent root).
            rtl = ""
            if a in uf._p:
                rtl = _display_rtl_path(uf._meta.get(a, {}).get("file") or "")
            rows.append(
                ElectricalP2PRow(
                    check_id=check_id,
                    a=a,
                    b_slice="",
                    status="FAIL",
                    fail_node=a,
                    fail_rtl=rtl,
                    note="no electrical path to b",
                )
            )
            continue
        for sl in slices:
            rows.append(
                ElectricalP2PRow(
                    check_id=check_id,
                    a=a,
                    b_slice=sl,
                    status="PASS",
                )
            )
    return rows


def format_electrical_report(
    rows: Sequence[ElectricalP2PRow],
    *,
    top: str = "",
    title: str = "pyslangwalk electrical p2p",
) -> str:
    lines = [
        f"# {title}",
        "# structural wiring only (port map + pure assign); no logic evaluation",
        f"# top={top or '-'}",
        "# check_id | a | b_slice | status | fail_node | fail_rtl",
    ]
    for r in rows:
        lines.append(
            f"{r.check_id or '-'} | {r.a} | {r.b_slice or '-'} | {r.status} | "
            f"{r.fail_node or '-'} | {_display_rtl_path(r.fail_rtl) or '-'}"
        )
    if len(rows) == 0:
        lines.append("# (no rows)")
    lines.append("")
    return "\n".join(lines)


def write_electrical_report(
    path: Path,
    rows: Sequence[ElectricalP2PRow],
    *,
    top: str = "",
) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_electrical_report(rows, top=top), encoding="utf-8")
    return path


