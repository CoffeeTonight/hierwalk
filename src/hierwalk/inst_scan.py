"""Synthesizable module instance scan (text, post-preprocess)."""

from __future__ import annotations

import re
from typing import Dict, Iterator, List, Mapping, Optional, Set, Tuple, TypedDict

_IDENT = r"[A-Za-z_]\w*"
_IDENT_RE = re.compile(_IDENT)
_ESC_IDENT = r"\\(?:[A-Za-z_]\w*|\S+)"
_DIM_PART_RE = re.compile(r"\[[^\]]+\]")

_KEYWORDS = frozenset(
    {
        "module", "endmodule", "interface", "endinterface", "package", "endpackage",
        "program", "endprogram", "input", "output", "inout", "wire", "wand", "wor",
        "reg", "logic", "assign", "always", "always_ff", "always_comb", "initial",
        "parameter", "localparam", "genvar", "generate", "endgenerate", "if", "else",
        "case", "endcase", "for", "while", "function", "endfunction", "task", "endtask",
        "typedef", "specify", "endspecify", "primitive", "table", "endtable", "bind",
        "begin", "end", "fork", "join", "return", "import", "export", "virtual",
        "class", "endclass", "covergroup", "endgroup", "property", "endproperty",
        "sequence", "endsequence", "assert", "assume", "cover", "restrict",
        "and", "or", "nand", "nor", "xor", "xnor", "buf", "not", "tran", "pullup",
        "pulldown", "defparam", "cell", "config", "endconfig", "liblist", "design",
    }
)

_ATTR_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_PARAM_RE = re.compile(
    r"(?:parameter|localparam)\s+(?:\w+\s+)?([A-Za-z_]\w*)\s*=\s*([^;,\n]+)",
    re.IGNORECASE,
)
_BIND_LINE_RE = re.compile(r"^\s*bind\b", re.IGNORECASE | re.MULTILINE)
_DIRECTIVE_LINE_RE = re.compile(
    r"^\s*`(?:define|undef|include|ifdef|ifndef|elsif|else|endif)\b",
    re.IGNORECASE,
)
_MACRO_ONLY_LINE_RE = re.compile(r"^\s*(?:`[A-Za-z_]\w*\s*)+$")
_ENDIF_DIRECTIVE_SUFFIX_RE = re.compile(
    r"^\s*`(?:endif|else)\b\s*(.*)$",
    re.IGNORECASE,
)
_LARGE_BODY_ATTR_SKIP = 512 * 1024
_LARGE_BODY_SLIM = 256 * 1024
_ANCHOR_WINDOW_BACK = 8192
_ANCHOR_WINDOW_FWD = 65536
_MAX_ANCHOR_TRIES = 64

_MODULE_KIND_END = {
    "module": "endmodule",
    "interface": "endinterface",
    "program": "endprogram",
}
_MODULE_START_RE = re.compile(
    r"\b(module|interface|program)\s+([A-Za-z_]\w*)\b",
    re.IGNORECASE,
)
_END_KW_PATTERNS: Dict[str, re.Pattern[str]] = {}


from hierwalk.models import InstanceEdge
from hierwalk.params import parse_bound_token, parse_param_overrides


def _skip_sv_attributes(text: str, start: int) -> int:
    """Skip ``(* ... *)`` attribute regions (may be nested)."""
    pos = start
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos + 1 >= n or text[pos : pos + 2] != "(*":
            break
        pos += 2
        depth = 1
        while pos < n and depth:
            if pos + 1 < n and text[pos : pos + 2] == "(*":
                depth += 1
                pos += 2
            elif pos + 1 < n and text[pos : pos + 2] == "*)":
                depth -= 1
                pos += 2
            else:
                pos += 1
    return pos


def _skip_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    if start >= len(text) or text[start] != open_ch:
        return start
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _read_ident(text: str, i: int) -> Tuple[str, int]:
    if i < len(text) and text[i] == "\\":
        j = i + 1
        while j < len(text) and not text[j].isspace():
            j += 1
        return text[i:j], j
    m = _IDENT_RE.match(text, i)
    if not m:
        return "", i
    return m.group(0), m.end()


def _read_hier_inst_path(text: str, i: int) -> Tuple[str, int]:
    """Read ``scope[i].leaf`` or plain ``inst`` before ``(`` / ``;``."""
    parts: List[str] = []
    pos = i
    while True:
        ident, pos = _read_ident(text, pos)
        if not ident:
            break
        seg = ident
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text) and text[pos] == "[":
            end = _skip_balanced(text, pos, "[", "]")
            inner = text[pos + 1 : end - 1]
            after = end
            while after < len(text) and text[after].isspace():
                after += 1
            if ":" in inner and (after >= len(text) or text[after] != "."):
                parts.append(seg)
                return ".".join(parts), pos
            seg += text[pos:end]
            pos = end
            while pos < len(text) and text[pos].isspace():
                pos += 1
        parts.append(seg)
        if pos >= len(text) or text[pos] != ".":
            break
        pos += 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if not parts:
        return "", i
    return ".".join(parts), pos


def _parse_param_map(header_and_body: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for m in _PARAM_RE.finditer(header_and_body):
        params[m.group(1)] = m.group(2).strip()
    return params


def _param_int(val: str) -> Optional[int]:
    val = val.strip().strip('"').strip("'")
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"\d+'[bdhBDH][0-9a-fA-FxzXZ_]+", val):
        if val.lower().startswith("0b") or "'b" in val.lower() or "'B" in val:
            bits = val.split("'")[-1].lstrip("bB")
            try:
                return int(bits.replace("_", ""), 2)
            except ValueError:
                return None
        if "'d" in val or "'D" in val:
            try:
                return int(val.split("'")[-1].lstrip("dD").replace("_", ""))
            except ValueError:
                return None
        if "'h" in val.lower():
            try:
                return int(val.split("'")[-1].lstrip("hH"), 16)
            except ValueError:
                return None
    return None


def expand_inst_names(
    base: str,
    dim_text: str,
    param_map: Mapping[str, str],
    *,
    max_width: int = 64,
) -> List[str]:
    dims = dim_text.strip()
    if not dims:
        return [base]
    if not dims.startswith("["):
        return [base + dims]
    names = [base]
    for part in _DIM_PART_RE.findall(dims):
        inner = part[1:-1].strip()
        if ":" not in inner:
            names = [f"{n}{part}" for n in names]
            continue
        lo_t, hi_t = inner.split(":", 1)
        lo = parse_bound_token(lo_t, param_map)
        hi = parse_bound_token(hi_t, param_map)
        if lo is None or hi is None:
            names = [f"{n}{part}" for n in names]
            continue
        if hi < lo:
            lo, hi = hi, lo
        width = hi - lo + 1
        if width > max_width:
            names = [f"{n}{part}" for n in names]
            continue
        expanded: List[str] = []
        for n in names:
            for i in range(lo, hi + 1):
                expanded.append(f"{n}[{i}]")
        names = expanded
    return names


def _iter_body_lines(body: str) -> Iterator[str]:
    start = 0
    n = len(body)
    i = 0
    while i < n:
        if body[i] == "\n":
            yield body[start:i]
            start = i + 1
        elif body[i] == "\r":
            end = i
            if i + 1 < n and body[i + 1] == "\n":
                i += 1
            yield body[start:end]
            start = i + 1
        i += 1
    if start < n:
        yield body[start:]


def slim_body_for_instance_scan(body: str) -> str:
    from hierwalk.preprocess import rtl_after_ifdef_label_comment
    """
    Drop `` `ifdef `` / `` `ifndef `` / bare `` `MACRO `` lines before instance walk.

    When index defers ifdef filtering, directive lines remain in the body; stripping
    them avoids confusing the instance scanner (including nested `` `ifndef `` inside
    port lists) while keeping RTL in all conditional branches.
    """
    if "`" not in body:
        return body
    kept: List[str] = []
    for line in _iter_body_lines(body):
        if _DIRECTIVE_LINE_RE.match(line):
            trailing = rtl_after_ifdef_label_comment(line)
            if not trailing:
                suffix_m = _ENDIF_DIRECTIVE_SUFFIX_RE.match(line)
                if suffix_m is not None:
                    trailing = suffix_m.group(1).strip()
            if trailing:
                kept.append(trailing)
            continue
        if _MACRO_ONLY_LINE_RE.match(line):
            continue
        kept.append(line)
    if not kept:
        return body
    return "\n".join(kept)


def _end_keyword_pattern(end_kw: str) -> re.Pattern[str]:
    pat = _END_KW_PATTERNS.get(end_kw)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(end_kw)}\b", re.IGNORECASE)
        _END_KW_PATTERNS[end_kw] = pat
    return pat


class ModuleBlock(TypedDict):
    name: str
    chunk: str
    kind: str
    start: int


def iter_module_blocks(text: str) -> Iterator[ModuleBlock]:
    """Yield module chunks without a whole-file DOTALL regex."""
    for m in _MODULE_START_RE.finditer(text):
        kind = m.group(1).lower()
        name = m.group(2)
        end_kw = _MODULE_KIND_END.get(kind)
        if not end_kw:
            continue
        end_m = _end_keyword_pattern(end_kw).search(text, m.end())
        if not end_m:
            continue
        yield {
            "name": name,
            "chunk": text[m.end() : end_m.start()],
            "kind": kind,
            "start": m.start(),
        }


def instance_edge_matches_leaf(
    edge: InstanceEdge,
    inst_leaf: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when *inst_leaf* names *edge* (incl. folded array indices)."""
    if not inst_leaf:
        return False
    pmap = dict(param_map or {})
    if edge.inst_name == inst_leaf:
        return True
    if inst_leaf in expand_inst_names(edge.inst_name, "", pmap):
        return True
    leaf_lower = inst_leaf.lower()
    if edge.inst_name.lower() == leaf_lower:
        return True
    return any(name.lower() == leaf_lower for name in expand_inst_names(edge.inst_name, "", pmap))


def find_instance_by_child_module(
    body: str,
    child_module: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Optional[InstanceEdge]:
    """Return the first instance of *child_module* (for type-not-inst miss hints)."""
    if not body or not child_module:
        return None
    want = child_module.lower()
    for edge in _iter_hierarchy_instance_edges(body, param_map=param_map):
        if edge.child_module.lower() == want:
            return edge
    return None


def _prepare_instance_scan_text(body: str) -> str:
    from hierwalk.preprocess import strip_comments_for_instance_scan

    work = slim_body_for_instance_scan(strip_comments_for_instance_scan(body))
    if len(work) <= _LARGE_BODY_ATTR_SKIP:
        clean = _ATTR_RE.sub(" ", work)
        clean = _BIND_LINE_RE.sub("", clean)
        return clean
    return work


def _inst_leaf_anchor_pattern(inst_leaf: str) -> re.Pattern[str]:
    leaf = inst_leaf.strip()
    if not leaf:
        return re.compile(r"(?!x)x")
    if leaf.startswith("\\"):
        return re.compile(re.escape(leaf) + r"(?=\s)", re.IGNORECASE)
    if "[" in leaf:
        base, rest = leaf.split("[", 1)
        pat = (
            r"(?<![A-Za-z0-9_$\\])"
            + re.escape(base)
            + r"\s*\["
            + re.escape(rest)
        )
        return re.compile(pat, re.IGNORECASE)
    pat = (
        r"(?<![A-Za-z0-9_$\\])"
        + re.escape(leaf)
        + r"(?=\s*[\[;(]|\s*[,;]|\s*$|\s*\.|\s*#)"
    )
    return re.compile(pat, re.IGNORECASE)


def _instance_stmt_window(
    clean: str,
    anchor: int,
    *,
    max_back: int = _ANCHOR_WINDOW_BACK,
    max_fwd: int = _ANCHOR_WINDOW_FWD,
) -> str:
    back_lo = max(0, anchor - max_back)
    prev_semi = clean.rfind(";", back_lo, anchor)
    start = prev_semi + 1 if prev_semi >= 0 else back_lo
    fwd_hi = min(len(clean), anchor + max_fwd)
    depth_paren = 0
    end = fwd_hi
    i = anchor
    while i < fwd_hi:
        ch = clean[i]
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == ";" and depth_paren == 0:
            end = i + 1
            break
        i += 1
    return clean[start:end]


def _find_hierarchy_instance_anchored(
    clean: str,
    inst_leaf: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Optional[InstanceEdge]:
    """Grep-style anchor on *inst_leaf*, then parse only the local statement."""
    pat = _inst_leaf_anchor_pattern(inst_leaf)
    tries = 0
    for m in pat.finditer(clean):
        tries += 1
        if tries > _MAX_ANCHOR_TRIES:
            break
        window = _instance_stmt_window(clean, m.start())
        for edge in _iter_hierarchy_instance_edges_on_clean(
            window,
            param_map=param_map,
        ):
            if instance_edge_matches_leaf(edge, inst_leaf, param_map=param_map):
                return edge
    return None


def find_hierarchy_instance(
    body: str,
    inst_leaf: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Optional[InstanceEdge]:
    """
    Selective instance lookup: scan until *inst_leaf* matches, then stop.

    Large bodies (e.g. monolithic ``allinst.v``) use grep-style anchoring on
    *inst_leaf* instead of walking every instance from the top of the module.
    """
    if not body or not inst_leaf:
        return None
    clean = _prepare_instance_scan_text(body)
    if len(clean) > _LARGE_BODY_SLIM:
        hit = _find_hierarchy_instance_anchored(
            clean,
            inst_leaf,
            param_map=param_map,
        )
        if hit is not None:
            return hit
    for edge in _iter_hierarchy_instance_edges_on_clean(clean, param_map=param_map):
        if instance_edge_matches_leaf(edge, inst_leaf, param_map=param_map):
            return edge
    return None


def scan_hierarchy_instances(
    body: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> List[InstanceEdge]:
    """
    Find synthesizable ``cell_type inst_name`` pairs in a module body.

    Supports:
      cell u (...);
      cell #(P=1) u (...);
      cell u [3:0] (...);
      cell #(P=1) u [N-1:0] (...);
      cell u;
      cell u [3:0];
      generate / if / for bodies;
      comma-separated: cell u1 (...), u2 (...);
    """
    return list(_iter_hierarchy_instance_edges(body, param_map=param_map))


def _iter_hierarchy_instance_edges(
    body: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Iterator[InstanceEdge]:
    clean = _prepare_instance_scan_text(body)
    yield from _iter_hierarchy_instance_edges_on_clean(clean, param_map=param_map)


def _iter_hierarchy_instance_edges_on_clean(
    clean: str,
    *,
    param_map: Optional[Mapping[str, str]] = None,
) -> Iterator[InstanceEdge]:
    pmap = dict(param_map or {})
    seen: Set[Tuple[str, str]] = set()
    n = len(clean)
    i = 0

    def add(
        cell: str,
        inst: str,
        dims: str,
        overrides: Optional[Dict[str, str]] = None,
    ) -> Iterator[InstanceEdge]:
        if cell.lower() in _KEYWORDS:
            return
        for leaf in expand_inst_names(inst, dims, pmap):
            key = (leaf, cell)
            if key in seen:
                continue
            seen.add(key)
            yield InstanceEdge(
                inst_name=leaf,
                child_module=cell,
                param_overrides=dict(overrides or {}),
            )

    def consume_hash(start: int) -> Tuple[Dict[str, str], int]:
        pos = start
        if pos >= n or clean[pos] != "#":
            return {}, pos
        pos += 1
        while pos < n and clean[pos].isspace():
            pos += 1
        if pos >= n or clean[pos] != "(":
            return {}, start
        hash_end = _skip_balanced(clean, pos, "(", ")")
        inner = clean[pos + 1 : hash_end - 1] if hash_end > pos + 1 else ""
        return parse_param_overrides(inner), hash_end

    while i < n:
        while i < n and clean[i].isspace():
            i += 1
        if i >= n:
            break
        cell, j = _read_ident(clean, i)
        if not cell:
            i += 1
            continue
        if cell.lower() in _KEYWORDS:
            i = j
            continue
        k = j
        while k < n and clean[k].isspace():
            k += 1
        k = _skip_sv_attributes(clean, k)
        overrides: Dict[str, str] = {}
        inst = ""
        if k < n and clean[k] == "#":
            overrides, k = consume_hash(k)
            while k < n and clean[k].isspace():
                k += 1
            k = _skip_sv_attributes(clean, k)
            inst, k = _read_hier_inst_path(clean, k)
        else:
            inst, k = _read_hier_inst_path(clean, k)
            if inst:
                while k < n and clean[k].isspace():
                    k += 1
                if k < n and clean[k] == "#":
                    overrides, k = consume_hash(k)
        if not inst:
            i += 1
            continue
        while k < n and clean[k].isspace():
            k += 1
        dims = ""
        while k < n and clean[k] == "[":
            end = _skip_balanced(clean, k, "[", "]")
            dims += clean[k:end]
            k = end
            while k < n and clean[k].isspace():
                k += 1
        if k >= n or clean[k] not in "(;":
            i += 1
            continue
        yield from add(cell, inst, dims, overrides)
        if clean[k] == "(":
            k = _skip_balanced(clean, k, "(", ")")
        while k < n and clean[k].isspace():
            k += 1
        if k < n and clean[k] == ",":
            k += 1
            while k < n and clean[k].isspace():
                k += 1
            inst2, k2 = _read_hier_inst_path(clean, k)
            if inst2:
                k = k2
                while k < n and clean[k].isspace():
                    k += 1
                dims2 = ""
                while k < n and clean[k] == "[":
                    end = _skip_balanced(clean, k, "[", "]")
                    dims2 += clean[k:end]
                    k = end
                    while k < n and clean[k].isspace():
                        k += 1
                if k < n and clean[k] in "(;":
                    yield from add(cell, inst2, dims2, overrides)
                    if clean[k] == "(":
                        k = _skip_balanced(clean, k, "(", ")")
            i = k
            continue
        i = k


_MODULE_BLOCK_RE = re.compile(
    r"\b(?:module|interface|program)\s+([A-Za-z_]\w*)\b(.*?)\b(?:endmodule|endinterface|endprogram)\b",
    re.IGNORECASE | re.DOTALL,
)


