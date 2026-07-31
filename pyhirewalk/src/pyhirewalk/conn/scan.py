"""Per-module structural dependency scan (assign / FF / named port_map).

Regex ceiling: structural edges + evidence only. Literal bit-selects
(`x[3]`, `x[7:4]`) are recorded on evidence; non-literal selects stay
base-name connectivity with select_approx (no param/generate elab).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Evidence: user-facing triple (+ optional sel meta)
Evidence = Dict[str, object]

_IDENT = re.compile(r"[A-Za-z_]\w*")
# continuous assign; allow optional bit select on LHS; single-line ; terminated
_ASSIGN = re.compile(
    r"\bassign\b\s+"
    r"(.+?)"
    r"\s*=\s*"
    r"([^;]+);",
    re.M | re.S,
)
# procedural: <= or single = (NOT ==, !=, <= already, >=, ===)
# use negative lookbehind/ahead so "==" is not treated as assign
_PROC_ASG = re.compile(
    r"^\s*([A-Za-z_]\w*)"
    r"(?:\s*(\[[^\]]+\]))?"
    r"\s*(?:<=|(?<![=!<>])=(?!=))"
    r"\s*([^;]+);",
    re.M,
)
# named port: .formal( actual_expr )  — actual may be concat/expression
_PORT_CONN = re.compile(
    r"\.([A-Za-z_]\w*)\s*\(",
)
# net + optional [sel]:  name, name[3], name[7:4]  (after first ident)
_NET_REF = re.compile(
    r"([A-Za-z_]\w*)"
    r"((?:\s*\[[^\]]+\])*)"
)
# integer literal select body: 3  or  7:4  (spaces ok)
_LIT_SEL_BODY = re.compile(
    r"^\s*(\d+)\s*(?::\s*(\d+)\s*)?$"
)

_KW = frozenset(
    "if for case while return assign typedef always always_ff always_comb "
    "always_latch initial final generate end endmodule endinterface endpackage "
    "begin endfunction endtask endgenerate else elseif endcase module "
    "input output inout wire reg logic bit signed unsigned var".split()
)


@dataclass(frozen=True)
class BitSel:
    """Closed integer range; msb/lsb as written (no param)."""

    msb: int
    lsb: int

    def fmt(self) -> str:
        if self.msb == self.lsb:
            return f"[{self.msb}]"
        return f"[{self.msb}:{self.lsb}]"


def parse_literal_select(bracket_chunk: str) -> Tuple[Optional[BitSel], bool]:
    """Parse trailing `[...]` chain after a net name.

    Returns (sel, is_approx):
      - no brackets → (None, False)
      - single literal [3] or [7:4] → (BitSel, False)
      - multi-dim / non-literal (param, expr) → (None, True)  # approx
    """
    if not bracket_chunk or not bracket_chunk.strip():
        return None, False
    parts = re.findall(r"\[([^\]]*)\]", bracket_chunk)
    if not parts:
        return None, False
    if len(parts) != 1:
        # multi-dim array index: structural base only
        return None, True
    m = _LIT_SEL_BODY.match(parts[0])
    if not m:
        return None, True
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) is not None else a
    return BitSel(msb=a, lsb=b), False


def parse_net_ref(token: str) -> Tuple[Optional[str], Optional[BitSel], bool]:
    """First net in token: (base, literal_sel|None, select_approx)."""
    token = token.strip()
    m = _NET_REF.match(token)
    if not m:
        return None, None, False
    base = m.group(1)
    if base in _KW:
        return None, None, False
    sel, approx = parse_literal_select(m.group(2) or "")
    return base, sel, approx


def net_refs_in_expr(expr: str) -> List[Tuple[str, Optional[BitSel], bool]]:
    """Identifier + optional select refs in expression (dedupe by base order)."""
    cleaned = re.sub(
        r"\d+\s*'\s*[sS]?[bBhHdDoO]\s*[0-9a-fA-FxXzZ_?]+",
        " ",
        expr,
    )
    out: List[Tuple[str, Optional[BitSel], bool]] = []
    seen: Set[str] = set()
    for m in _NET_REF.finditer(cleaned):
        base = m.group(1)
        if base in _KW or base in seen:
            continue
        sel, approx = parse_literal_select(m.group(2) or "")
        seen.add(base)
        out.append((base, sel, approx))
    return out


def _idents_in_expr(expr: str) -> List[str]:
    """Identifier tokens in expression (skip keywords and sized literals)."""
    return [b for b, _s, _a in net_refs_in_expr(expr)]


def _lhs_base(lhs: str) -> Optional[str]:
    """First identifier in LHS (handles x, x[3:0], {a,b} takes all via idents)."""
    base, _sel, _approx = parse_net_ref(lhs)
    if base:
        return base
    idents = _idents_in_expr(lhs)
    return None if not idents else idents[0]


def _lhs_bases(lhs: str) -> List[str]:
    """All target nets on LHS (plain or concat)."""
    lhs = lhs.strip()
    if lhs.startswith("{"):
        return _idents_in_expr(lhs)
    base = _lhs_base(lhs)
    return [base] if base else []


def _lhs_targets(lhs: str) -> List[Tuple[str, Optional[BitSel], bool]]:
    """LHS nets with optional literal select (concat → each element)."""
    lhs = lhs.strip()
    if lhs.startswith("{"):
        return net_refs_in_expr(lhs)
    one = parse_net_ref(lhs)
    if one[0]:
        return [one]
    return []


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _snippet_at(text: str, pos: int, cap: int = 200) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end < 0:
        line_end = len(text)
    sn = text[line_start:line_end].strip()
    if len(sn) > cap:
        sn = sn[: cap - 3] + "..."
    return sn


def _ev(file: str, text: str, pos: int) -> Evidence:
    return {
        "file": file,
        "line": _line_of(text, pos),
        "snippet": _snippet_at(text, pos),
    }


def _skip_balanced(s: str, i: int, op: str = "(", cl: str = ")") -> int:
    if i >= len(s) or s[i] != op:
        return -1
    d = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == op:
            d += 1
        elif c == cl:
            d -= 1
            if d == 0:
                return i + 1
        i += 1
    return -1


@dataclass
class Edge:
    """driver -> load (influence direction). Keys always base names."""

    dst: str  # local net base name
    kind: str  # assign | ff | port_map
    evidence: Evidence
    inst: Optional[str] = None
    formal: Optional[str] = None
    child_module: Optional[str] = None
    into_child: Optional[bool] = None
    # S7a: literal select meta (None = whole net / unknown)
    dst_sel: Optional[BitSel] = None
    src_sel: Optional[BitSel] = None
    select_approx: bool = False


def _ev_with_sel(
    base: Evidence,
    *,
    dst_sel: Optional[BitSel] = None,
    src_sel: Optional[BitSel] = None,
    src_sels: Optional[Dict[str, str]] = None,
    select_approx: bool = False,
) -> Evidence:
    """Copy evidence and attach optional literal select metadata."""
    ev = dict(base)
    if dst_sel is not None:
        ev["dst_sel"] = dst_sel.fmt()
    if src_sel is not None:
        ev["src_sel"] = src_sel.fmt()
    if src_sels:
        ev["src_sels"] = dict(src_sels)
    if select_approx:
        ev["select_approx"] = True
    return ev


@dataclass
class LocalDepGraph:
    file: str
    module: Optional[str] = None
    ports: Dict[str, str] = field(default_factory=dict)
    forward: Dict[str, List[Edge]] = field(default_factory=dict)
    # reverse index: load -> list of (driver, edge) for O(1) backward expand
    backward: Dict[str, List[Tuple[str, Edge]]] = field(default_factory=dict)
    instances: Dict[str, str] = field(default_factory=dict)
    port_maps: List[Tuple[str, str, str, str, Evidence]] = field(
        default_factory=list
    )

    def add_fwd(self, src: str, edge: Edge) -> None:
        self.forward.setdefault(src, []).append(edge)
        self.backward.setdefault(edge.dst, []).append((src, edge))


def scan_module_file(
    file_path: str,
    defines: Optional[Dict[str, str]] = None,
    *,
    known_modules: Optional[set] = None,
) -> LocalDepGraph:
    """Scan one RTL file after comment strip + ifdef eval."""
    fp = str(Path(file_path).resolve())
    try:
        raw = Path(fp).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return LocalDepGraph(file=fp)

    from hier_resolve import apply_sv_ifdefs, strip_comments

    text = apply_sv_ifdefs(strip_comments(raw), defines or {})
    g = LocalDepGraph(file=fp)

    mm = re.search(r"\bmodule\s+([A-Za-z_]\w*)\b", text)
    if mm:
        g.module = mm.group(1)

    type_kw = frozenset(
        "wire reg logic bit signed unsigned var ref integer int tri "
        "byte shortint longint time realtime".split()
    )
    parts = re.split(r"\b(input|output|inout)\b", text, flags=re.I)
    pi = 1
    while pi + 1 < len(parts):
        direction = parts[pi].lower()
        chunk = parts[pi + 1]
        if ";" in chunk:
            chunk = chunk.split(";", 1)[0]
        for idm in _IDENT.finditer(chunk):
            w = idm.group(0)
            wl = w.lower()
            if wl in ("input", "output", "inout") or wl in type_kw:
                continue
            g.ports.setdefault(w, direction)
        pi += 2

    # continuous assign (may span lines until ;)
    for m in _ASSIGN.finditer(text):
        lhs, rhs = m.group(1), m.group(2)
        ev0 = _ev(fp, text, m.start())
        lhs_tgts = _lhs_targets(lhs)
        rhs_refs = net_refs_in_expr(rhs)
        src_sels: Dict[str, str] = {}
        rhs_approx = False
        for src, ssel, sap in rhs_refs:
            if ssel is not None:
                src_sels[src] = ssel.fmt()
            if sap:
                rhs_approx = True
        for dst, dsel, dapprox in lhs_tgts:
            approx = dapprox or rhs_approx
            for src, ssel, sap in rhs_refs:
                if src == dst:
                    continue
                ev = _ev_with_sel(
                    ev0,
                    dst_sel=dsel,
                    src_sel=ssel,
                    src_sels=src_sels or None,
                    select_approx=approx or sap,
                )
                g.add_fwd(
                    src,
                    Edge(
                        dst=dst,
                        kind="assign",
                        evidence=ev,
                        dst_sel=dsel,
                        src_sel=ssel,
                        select_approx=approx or sap,
                    ),
                )

    # procedural <= or = (not ==)
    for m in _PROC_ASG.finditer(text):
        line = m.group(0)
        if re.match(r"\s*assign\b", line):
            continue
        lhs = m.group(1)
        lhs_br = m.group(2) or ""
        rhs = m.group(3)
        if lhs in _KW:
            continue
        # guard: if line still looks like comparison-only, skip
        if re.search(r"==|!=|===|!==", line) and "<=" not in line:
            # might still be `a = b == c` which is assign — only skip pure ==
            if re.match(
                r"^\s*[A-Za-z_]\w*(?:\s*\[[^\]]+\])?\s*==",
                line,
            ):
                continue
        ev0 = _ev(fp, text, m.start())
        dsel, dapprox = parse_literal_select(lhs_br)
        rhs_refs = net_refs_in_expr(rhs)
        src_sels: Dict[str, str] = {}
        rhs_approx = False
        for src, ssel, sap in rhs_refs:
            if ssel is not None:
                src_sels[src] = ssel.fmt()
            if sap:
                rhs_approx = True
        kind = "ff" if "<=" in line else "assign"
        for src, ssel, sap in rhs_refs:
            if src == lhs:
                continue
            approx = dapprox or rhs_approx or sap
            ev = _ev_with_sel(
                ev0,
                dst_sel=dsel,
                src_sel=ssel,
                src_sels=src_sels or None,
                select_approx=approx,
            )
            g.add_fwd(
                src,
                Edge(
                    dst=lhs,
                    kind=kind,
                    evidence=ev,
                    dst_sel=dsel,
                    src_sel=ssel,
                    select_approx=approx,
                ),
            )

    known = known_modules or set()
    # instance heads: typ inst (
    inst_head = re.compile(
        r"\b([A-Za-z_]\w*)\b"
        r"(?:\s*#\s*\()"  # optional params — handle below
    )
    # simpler scan: find "Type inst (" with optional #()
    i, n = 0, len(text)
    while i < n:
        m = _IDENT.match(text, i)
        if not m:
            i += 1
            continue
        typ = m.group(0)
        j = m.end()
        # skip ws
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j < n and text[j] == "#":
            j += 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] == "(":
                j = _skip_balanced(text, j)
                if j < 0:
                    break
            while j < n and text[j] in " \t\r\n":
                j += 1
        m2 = _IDENT.match(text, j)
        if not m2:
            i = m.end()
            continue
        inst = m2.group(0)
        k = m2.end()
        while k < n and text[k] in " \t\r\n":
            k += 1
        while k < n and text[k] == "[":
            k = _skip_balanced(text, k, "[", "]")
            if k < 0:
                break
            while k < n and text[k] in " \t\r\n":
                k += 1
        if k >= n or text[k] != "(":
            i = m.end()
            continue
        if typ in _KW or inst in _KW:
            i = m.end()
            continue
        if known and typ not in known:
            i = m.end()
            continue
        g.instances[inst] = typ
        start = k
        end = _skip_balanced(text, start)
        if end < 0:
            i = m.end()
            continue
        body = text[start + 1 : end - 1]
        ev = _ev(fp, text, m.start())
        # parse .formal( expr ) with balanced parens inside expr
        bi = 0
        blen = len(body)
        while bi < blen:
            pm = _PORT_CONN.search(body, bi)
            if not pm:
                break
            formal = pm.group(1)
            expr_start = pm.end()  # after '('
            # find matching close for this port conn
            depth = 1
            ei = expr_start
            while ei < blen and depth:
                if body[ei] == "(":
                    depth += 1
                elif body[ei] == ")":
                    depth -= 1
                ei += 1
            actual_expr = body[expr_start : ei - 1].strip()
            bi = ei
            # skip if empty
            if not actual_expr:
                continue
            actuals = _idents_in_expr(actual_expr)
            # record port_map with primary actual (first ident) for climb keys;
            # all actual idents drive into formal for forward
            primary = actuals[0] if actuals else None
            if primary is None:
                # constant-only connection
                continue
            g.port_maps.append((inst, formal, primary, typ, dict(ev)))
            for actual in actuals:
                g.add_fwd(
                    actual,
                    Edge(
                        dst=formal,
                        kind="port_map",
                        evidence=dict(ev),
                        inst=inst,
                        formal=formal,
                        child_module=typ,
                        into_child=True,
                    ),
                )
        i = m.end()

    return g
