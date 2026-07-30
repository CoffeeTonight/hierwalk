"""Per-module structural dependency scan (assign / FF / named port_map)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

def _prep_text(raw: str, defines: Optional[Dict[str, str]]) -> str:
    """Comment strip + ifdef (hier_resolve helpers; project root on sys.path)."""
    # Deferred import: hier_resolve.py is at project root, not under src/.
    from hier_resolve import apply_sv_ifdefs, strip_comments

    return apply_sv_ifdefs(strip_comments(raw), defines or {})

# Evidence: user-facing triple
Evidence = Dict[str, object]

_IDENT = re.compile(r"[A-Za-z_]\w*")
_ASSIGN = re.compile(
    r"\bassign\s+"
    r"([A-Za-z_]\w*)"
    r"(?:\s*(\[[^\]]+\]))?"
    r"\s*=\s*"
    r"([^;]+);",
    re.M,
)
# nonblocking or blocking assign in procedural block (line-oriented after prep)
_PROC_ASG = re.compile(
    r"^\s*([A-Za-z_]\w*)"
    r"(?:\s*(\[[^\]]+\]))?"
    r"\s*(?:<=|=)\s*"
    r"([^;]+);",
    re.M,
)
_PORT_DIR = re.compile(
    r"\b(input|output|inout)\b"
    r"(?:\s+(?:wire|reg|logic|bit|signed|unsigned|var))*"
    r"(?:\s+\[[^\]]+\])*"
    r"\s+([A-Za-z_]\w*)\b"
)
# ModName #(...) inst_name (
_INST_HEAD = re.compile(
    r"\b([A-Za-z_]\w*)\b"
    r"(?:\s*#\s*\([^;]*?\))?"
    r"\s+([A-Za-z_]\w*)\s*"
    r"(?:\[[^\]]*\]\s*)*"
    r"\(",
    re.S,
)
_PORT_CONN = re.compile(
    r"\.([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)(?:\s*\[[^\]]+\])?\s*\)"
)

_KW = frozenset(
    "if for case while return assign typedef always always_ff always_comb "
    "always_latch initial final generate end endmodule endinterface endpackage "
    "begin endfunction endtask endgenerate else elseif endcase module "
    "input output inout wire reg logic".split()
)


def _idents_in_expr(expr: str) -> List[str]:
    """Identifier tokens in RHS (skip keywords)."""
    out: List[str] = []
    for m in _IDENT.finditer(expr):
        w = m.group(0)
        if w in _KW:
            continue
        out.append(w)
    return out


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


@dataclass
class Edge:
    """driver -> load (influence direction)."""

    dst: str  # local net base name
    kind: str  # assign | ff | port_map
    evidence: Evidence
    # port_map only
    inst: Optional[str] = None
    formal: Optional[str] = None
    child_module: Optional[str] = None
    # for port_map: True if this edge goes into child (parent actual -> child formal)
    into_child: Optional[bool] = None


@dataclass
class LocalDepGraph:
    file: str
    module: Optional[str] = None
    ports: Dict[str, str] = field(default_factory=dict)  # name -> input|output|inout
    # forward[src] = edges leaving src (src drives dst)
    forward: Dict[str, List[Edge]] = field(default_factory=dict)
    # instances in this module: inst_name -> child module type
    instances: Dict[str, str] = field(default_factory=dict)
    # port maps: list of (inst, formal, actual, child_mod, evidence)
    port_maps: List[Tuple[str, str, str, str, Evidence]] = field(default_factory=list)

    def add_fwd(self, src: str, edge: Edge) -> None:
        self.forward.setdefault(src, []).append(edge)


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

    text = _prep_text(raw, defines)
    g = LocalDepGraph(file=fp)

    # module name
    mm = re.search(r"\bmodule\s+([A-Za-z_]\w*)\b", text)
    if mm:
        g.module = mm.group(1)

    # ports
    for m in _PORT_DIR.finditer(text):
        g.ports[m.group(2)] = m.group(1)

    # continuous assign: RHS idents -> LHS
    for m in _ASSIGN.finditer(text):
        lhs = m.group(1)
        rhs = m.group(3)
        ev = _ev(fp, text, m.start())
        for src in _idents_in_expr(rhs):
            if src == lhs:
                continue
            g.add_fwd(src, Edge(dst=lhs, kind="assign", evidence=ev))

    # procedural <= / = (simple single-line)
    for m in _PROC_ASG.finditer(text):
        # skip assign keyword lines already handled
        line = m.group(0)
        if re.match(r"\s*assign\b", line):
            continue
        lhs = m.group(1)
        rhs = m.group(3)
        if lhs in _KW:
            continue
        ev = _ev(fp, text, m.start())
        kind = "ff" if "<=" in line else "assign"
        idents = _idents_in_expr(rhs)
        if not idents:
            # const drive — no net edge; record as self terminal optional
            continue
        for src in idents:
            if src == lhs:
                continue
            g.add_fwd(src, Edge(dst=lhs, kind=kind, evidence=ev))

    # instances + named port maps
    known = known_modules or set()
    for m in _INST_HEAD.finditer(text):
        typ, inst = m.group(1), m.group(2)
        if typ in _KW or inst in _KW:
            continue
        if known and typ not in known:
            continue
        g.instances[inst] = typ
        # extract port connections until matching close paren of instance
        start = m.end() - 1  # at '('
        depth = 0
        i = start
        n = len(text)
        while i < n:
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    body = text[start + 1 : i]
                    ev = _ev(fp, text, m.start())
                    for pm in _PORT_CONN.finditer(body):
                        formal, actual = pm.group(1), pm.group(2)
                        g.port_maps.append((inst, formal, actual, typ, dict(ev)))
                        # parent actual <-> child formal:
                        # actual drives into child input: actual -> (cross)
                        # child output drives actual: (cross) -> actual
                        # store both as port_map edges for search to interpret
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
                    break
            i += 1

    return g
