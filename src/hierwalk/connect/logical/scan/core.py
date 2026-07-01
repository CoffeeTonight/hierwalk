"""Parse assign, FF, and instance port-map connectivity within a module body."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.generate_fold import fold_generate_regions, prepare_body_for_instance_scan
from hierwalk.params import (
    _find_top_level_op,
    _param_int,
    collect_connect_module_params,
    resolve_param_expr,
    resolve_param_map,
)
from hierwalk.inst_scan import (
    _ATTR_RE,
    _BIND_LINE_RE,
    _KEYWORDS,
    _read_ident,
    _skip_balanced,
)

from hierwalk.connect.logical.scan.types import (
    BindRecord,
    ConnectEdgeProv,
    ModuleConnectIndex,
    binds_digest,
)

_IDENT_TOKEN_RE = re.compile(r"(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)")
_SIZED_LITERAL_RE = re.compile(
    r"\d+'[bdhBDH][0-9a-fA-FxzXZ?_]+",
    re.IGNORECASE,
)
_LVALUE_TAIL_RE = re.compile(
    r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)(?:\s*\[[^\]]+\])*)\s*$",
)


_PRIMITIVE_GATES = frozenset(
    {"and", "nand", "or", "nor", "xor", "xnor", "buf", "not"}
)

_HIER_REF_RE = re.compile(
    r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))"
    r"\s*\.\s*"
    r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)(?:\s*\[[^\]]+\])*)"
)


def _clean_body(body: str) -> str:
    clean = _ATTR_RE.sub(" ", body)
    return _BIND_LINE_RE.sub("", clean)


def _clean_body_for_connect_scan(body: str) -> str:
    """Strip comments/ifdef directive lines before statement splitting (align with inst_scan)."""
    from hierwalk.inst_scan import slim_body_for_instance_scan
    from hierwalk.preprocess import strip_comments_for_instance_scan

    work = slim_body_for_instance_scan(strip_comments_for_instance_scan(body))
    return _clean_body(work)


_SELECT_NODE_RE = re.compile(
    r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)(?:\s*\[[^\]]+\])+)"
)


def _mask_sized_literals(expr: str) -> str:
    return _SIZED_LITERAL_RE.sub(" ", expr)


def extract_signal_roots(expr: str) -> Set[str]:
    roots: Set[str] = set()
    masked = _mask_sized_literals(expr)
    for m in _IDENT_TOKEN_RE.finditer(masked):
        name = m.group(0)
        if name.lower() in _KEYWORDS:
            continue
        roots.add(name)
    return roots


def extract_hier_refs(expr: str) -> List[Tuple[str, str]]:
    """Return ``(inst_leaf, port_or_net)`` for dotted hierarchical references."""
    out: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for m in _HIER_REF_RE.finditer(expr):
        inst = m.group(1)
        tail = re.sub(r"\s+", "", m.group(2))
        port = re.match(
            r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
            tail,
        )
        if not port:
            continue
        key = (inst, port.group(1))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _split_case_item_label(stmt: str) -> Tuple[str, str]:
    """Return ``(label, body)`` for one case item (``2'b00: x = y``)."""
    text = stmt.strip()
    if not text:
        return "", text
    n = len(text)
    in_string: Optional[str] = None
    paren = brack = brace = 0
    i = 0
    while i < n:
        ch = text[i]
        if in_string:
            if ch == in_string and (i == 0 or text[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(text, i)
            if delim:
                in_string = delim
            i = nxt
            continue
        if ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            brack += 1
        elif ch == "]" and brack:
            brack -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace:
            brace -= 1
        elif ch == ":" and paren == brack == brace == 0:
            return text[:i].strip(), text[i + 1 :].strip()
        i += 1
    return "", text


def _strip_case_item_label(stmt: str) -> str:
    """Drop ``2'b00:`` / ``default:`` prefix before parsing procedural assigns."""
    text = stmt.strip()
    if not text:
        return text
    n = len(text)
    in_string: Optional[str] = None
    paren = brack = brace = 0
    i = 0
    while i < n:
        ch = text[i]
        if in_string:
            if ch == in_string and (i == 0 or text[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(text, i)
            if delim:
                in_string = delim
            i = nxt
            continue
        if ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            brack += 1
        elif ch == "]" and brack:
            brack -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace:
            brace -= 1
        elif ch == ":" and paren == brack == brace == 0:
            rest = text[i + 1 :].strip()
            return rest if rest else text
        i += 1
    return text


def _fold_bracket_on_value(
    val: int,
    inner: str,
    param_map: Mapping[str, str],
) -> Optional[int]:
    inner = inner.strip()
    if ":" in inner:
        msb_s, lsb_s = inner.split(":", 1)
        msb = resolve_param_expr(msb_s.strip(), param_map)
        lsb = resolve_param_expr(lsb_s.strip(), param_map)
        if msb is None or lsb is None:
            return None
        lo, hi = min(lsb, msb), max(lsb, msb)
        width = hi - lo + 1
        return (val >> lo) & ((1 << width) - 1)
    bit = resolve_param_expr(inner, param_map)
    if bit is None:
        return None
    return (val >> bit) & 1


def _eval_index_expr(expr: str, param_map: Mapping[str, str]) -> Optional[int]:
    expr_ns = re.sub(r"\s+", "", expr.strip())
    if not expr_ns:
        return None
    m = re.match(
        r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))(\[[^\]]+\])+$",
        expr_ns,
    )
    if m:
        base = m.group(1)
        base_val = resolve_param_expr(base, param_map)
        if base_val is None:
            return resolve_param_expr(expr, param_map)
        pos = len(base)
        val = base_val
        while pos < len(expr_ns) and expr_ns[pos] == "[":
            end = _skip_balanced(expr_ns, pos, "[", "]")
            inner = expr_ns[pos + 1 : end - 1]
            pos = end
            folded = _fold_bracket_on_value(val, inner, param_map)
            if folded is None:
                return None
            val = folded
        return val
    return resolve_param_expr(expr, param_map)


def _canonicalize_connect_node(
    token: str,
    param_map: Mapping[str, str] | None = None,
) -> str:
    pmap = dict(param_map or {})
    token = re.sub(r"\s+", "", token)
    m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", token)
    if not m:
        return token
    base = m.group(1)
    pos = len(base)
    parts = [base]
    while pos < len(token) and token[pos] == "[":
        end = _skip_balanced(token, pos, "[", "]")
        inner = token[pos + 1 : end - 1]
        pos = end
        idx = _eval_index_expr(inner, pmap)
        if idx is not None:
            parts.append(f"[{idx}]")
        else:
            inner_clean = re.sub(r"\s+", "", inner)
            parts.append(f"[{inner_clean}]")
    return "".join(parts)


def _is_concat_part_select_expr(expr: str) -> bool:
    text = re.sub(r"\s+", "", expr.strip())
    return bool(re.match(r"^\{.+}\[[^\]]+\]$", text))


def _is_braced_concat_rhs(expr: str) -> bool:
    """True for ``{a,b,c}`` replication/concat (not ``{a,b}[i]`` part-select)."""
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    text = re.sub(r"\s+", "", text)
    if len(text) < 2 or text[0] != "{" or text[-1] != "}":
        return False
    return re.search(r"\}\[", text) is None


def _is_compound_port_map_expr(expr: str) -> bool:
    """True when a port-map expr mixes operands (``a|b``, ``x^y``) — no child-down COI."""
    if _is_braced_concat_rhs(expr):
        return False
    text = re.sub(r"\s+", "", expr.strip())
    if not text:
        return False
    scrub = re.sub(r"\[[^\]]+\]", "", text)
    return bool(re.search(r"[|^&+\-*/%?]", scrub))


def _expand_concat_elements(inner: str) -> List[str]:
    """Expand ``{4{a}}`` replications and top-level comma concat pieces."""
    parts = _split_concat_top_level(inner)
    expanded: List[str] = []
    for part in parts:
        token = part.strip()
        m = re.match(r"^(\d+)\{(.+)\}$", token)
        if m:
            count = int(m.group(1))
            sub = _expand_concat_elements(m.group(2))
            for _ in range(count):
                expanded.extend(sub)
        else:
            expanded.append(token)
    return expanded


def _split_concat_top_level(inner: str) -> List[str]:
    """Split ``a,b,{c,d}`` at top-level commas inside one concat."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            token = "".join(cur).strip()
            if token:
                parts.append(token)
            cur = []
            continue
        cur.append(ch)
    token = "".join(cur).strip()
    if token:
        parts.append(token)
    return parts


def _concat_part_select_operand(
    expr: str,
    param_map: Mapping[str, str] | None = None,
) -> Optional[str]:
    """Return the concat element selected by ``{a,b}[i]`` (left-to-right index)."""
    text = re.sub(r"\s+", "", expr.strip())
    if not text.endswith("]"):
        return None
    m = re.match(r"^\{(.+)\}\[([^\]]+)\]$", text)
    if not m:
        return None
    inner, idx_s = m.group(1), m.group(2)
    parts = _expand_concat_elements(inner)
    if not parts:
        return None
    pmap = dict(param_map or {})
    idx = resolve_param_expr(idx_s, pmap)
    if idx is None:
        idx = _param_int(idx_s)
    if idx is None or idx < 0 or idx >= len(parts):
        return None
    return parts[idx].strip()


def _iter_select_tokens(expr: str) -> List[str]:
    """Find signal tokens with balanced bracket selects."""
    tokens: List[str] = []
    n = len(expr)
    i = 0
    while i < n:
        m = _IDENT_TOKEN_RE.match(expr, i)
        if not m:
            i += 1
            continue
        name = m.group(0)
        if name.lower() in _KEYWORDS:
            i = m.end()
            continue
        pos = m.end()
        while pos < n and expr[pos].isspace():
            pos += 1
        if pos >= n or expr[pos] != "[":
            i = m.end()
            continue
        start = m.start()
        while pos < n and expr[pos] == "[":
            pos = _skip_balanced(expr, pos, "[", "]")
        tokens.append(re.sub(r"\s+", "", expr[start:pos]))
        i = pos
    return tokens


def extract_connect_nodes(
    expr: str,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """Signal roots with indexed/part-select granularity for COI (``bus[0]`` ≠ ``bus[1]``)."""
    pmap = dict(param_map or {})
    concat_sel = _concat_part_select_operand(expr, pmap)
    if concat_sel is not None:
        return extract_connect_nodes(concat_sel, pmap)
    if _is_concat_part_select_expr(expr):
        return set()
    tokens = _iter_select_tokens(expr)
    if tokens:
        return {_canonicalize_connect_node(t, pmap) for t in tokens}
    return extract_signal_roots(expr)


def _local_connect_nodes(
    expr: str,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """Like :func:`extract_connect_nodes` but drop ``inst.port`` hierarchical tails."""
    nodes = set(extract_connect_nodes(expr, param_map))
    for inst, port in extract_hier_refs(expr):
        nodes.discard(inst)
        nodes.discard(port)
    return nodes


_BARE_IDENT_TOKEN_RE = re.compile(
    r"(?<![.\w\\])(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)(?![.\w])",
)
_BARE_ID_EXPR_CACHE: Dict[str, FrozenSet[str]] = {}
_BARE_ID_EXPR_CACHE_MAX = 8192


def _bare_identifiers_in_expr(expr: str) -> FrozenSet[str]:
    """Local identifier tokens in *expr* (one scan for iface port checks)."""
    hit = _BARE_ID_EXPR_CACHE.get(expr)
    if hit is not None:
        return hit
    found = frozenset(_BARE_IDENT_TOKEN_RE.findall(expr))
    if len(_BARE_ID_EXPR_CACHE) >= _BARE_ID_EXPR_CACHE_MAX:
        _BARE_ID_EXPR_CACHE.clear()
    _BARE_ID_EXPR_CACHE[expr] = found
    return found


def _bare_identifier_in_expr(expr: str, name: str) -> bool:
    """True when *name* appears as a local token, not only as ``inst.name``."""
    if not name:
        return False
    return name in _bare_identifiers_in_expr(expr)


def _cached_expr_roots(
    expr: str,
    cache: Dict[str, FrozenSet[str]],
    *,
    param_map: Mapping[str, str] | None = None,
) -> FrozenSet[str]:
    pmap = dict(param_map or {})
    key = expr if not pmap else f"{expr}\0{tuple(sorted(pmap.items()))}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    roots = frozenset(extract_connect_nodes(expr, pmap))
    cache[key] = roots
    return roots


def _edge_prov_key(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _record_edge_prov(
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]],
    a: str,
    b: str,
    *,
    line: int,
    kind: str,
) -> None:
    if edge_prov is None or not a or not b or a == b:
        return
    key = _edge_prov_key(a, b)
    if key not in edge_prov:
        edge_prov[key] = ConnectEdgeProv(line=line, kind=kind)


def _add_undirected(
    adj: Dict[str, Set[str]],
    a: str,
    b: str,
    *,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    line: int = 0,
    kind: str = "",
) -> None:
    if not a or not b or a == b:
        return
    adj.setdefault(a, set()).add(b)
    adj.setdefault(b, set()).add(a)
    if edge_prov is not None and line > 0 and kind:
        _record_edge_prov(edge_prov, a, b, line=line, kind=kind)


def lookup_edge_prov(
    mod_idx: ModuleConnectIndex,
    from_net: str,
    to_net: str,
) -> Optional[ConnectEdgeProv]:
    ra = mod_idx.net_rep.get(from_net, from_net)
    rb = mod_idx.net_rep.get(to_net, to_net)
    return mod_idx.edge_prov.get(_edge_prov_key(ra, rb))


def _range_to_bit_indices(
    inner: str,
    param_map: Mapping[str, str],
) -> Optional[List[int]]:
    inner = inner.strip()
    if ":" not in inner:
        return None
    msb_s, lsb_s = inner.split(":", 1)
    msb = resolve_param_expr(msb_s.strip(), param_map)
    lsb = resolve_param_expr(lsb_s.strip(), param_map)
    if msb is None or lsb is None:
        return None
    if msb >= lsb:
        return list(range(lsb, msb + 1))
    return list(range(msb, lsb + 1))


def _literal_range_to_bit_indices(inner: str) -> Optional[List[int]]:
    """``[2:0]`` style ranges only — no parameter or expression evaluation."""
    inner = inner.strip()
    if ":" not in inner:
        return None
    msb_s, lsb_s = inner.split(":", 1)
    msb_s, lsb_s = msb_s.strip(), lsb_s.strip()
    if not msb_s.isdigit() or not lsb_s.isdigit():
        return None
    msb, lsb = int(msb_s), int(lsb_s)
    if msb >= lsb:
        return list(range(lsb, msb + 1))
    return list(range(msb, lsb + 1))


def _collect_literal_decl_bit_indices(body: str) -> Dict[str, List[int]]:
    """Literal bus widths for text-conn (no parametric dim resolution)."""
    out: Dict[str, List[int]] = {}
    pat = re.compile(
        r"\b(?:input|output|inout)?\s*(?:logic|wire|reg)?\s*\[([^\]]+)\]\s*"
        r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
        re.IGNORECASE,
    )
    for m in pat.finditer(body):
        bits = _literal_range_to_bit_indices(m.group(1))
        if bits:
            out.setdefault(m.group(2), bits)
    return out


def _collect_decl_bit_indices(
    body: str,
    param_map: Mapping[str, str],
) -> Dict[str, List[int]]:
    """Infer bus bit indices from ``logic [9:0] foo`` / port declarations in *body*."""
    pmap = dict(param_map)
    out: Dict[str, List[int]] = {}
    pat = re.compile(
        r"\b(?:input|output|inout)?\s*(?:logic|wire|reg)?\s*\[([^\]]+)\]\s*"
        r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
        re.IGNORECASE,
    )
    for m in pat.finditer(body):
        bits = _range_to_bit_indices(m.group(1), pmap)
        if bits:
            out.setdefault(m.group(2), bits)
    return out


def _is_literal_slice_suffix(suffix: str) -> bool:
    """True for literal slice tails like ``[0]`` or ``[0][1]`` (no parametric selects)."""
    return bool(suffix and re.match(r"^(?:\[\d+\])+$", suffix))


def _split_net_base_suffix(net: str) -> tuple[str, str]:
    """``bus[0][1]`` -> (``bus``, ``[0][1]``); ``clk`` -> (``clk``, ````)."""
    text = re.sub(r"\s+", "", net.strip())
    m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", text)
    if not m:
        return text, ""
    base = m.group(1)
    return base, text[len(base) :]


def _port_select_suffix(port_name: str, child_net: str) -> Optional[str]:
    """Dimension suffix of *child_net* relative to inst port *port_name*."""
    if child_net == port_name:
        return ""
    prefix = port_name + "["
    if child_net.startswith(prefix):
        return child_net[len(port_name) :]
    return None


def _ensure_adj_node(adj: Dict[str, Set[str]], net: str) -> None:
    """Register *net* so it appears in compressed ``net_rep`` (self-rooted)."""
    if net:
        adj.setdefault(net, set())


def _collect_declared_net_names(body: str) -> Set[str]:
    """
    Declared ``wire``/``logic``/``reg`` and port identifiers usable as signal endpoints.

    Skips ``wire x = expr`` alias lines (handled by assign adjacency).
    """
    names: Set[str] = set()
    for stmt in split_statements(_clean_body(body)):
        if _is_decl_alias_statement(stmt):
            continue
        pos = _skip_ws(stmt, 0)
        kind = ""
        for kw in ("input", "output", "inout", "wire", "logic", "reg"):
            if _word_at(stmt, pos, kw):
                kind = kw
                pos = _skip_ws(stmt, pos + len(kw))
                break
        if not kind:
            continue
        if kind in ("wire", "logic", "reg"):
            pos = _skip_ws(stmt, pos)
            if pos < len(stmt) and stmt[pos] == "(":
                pos = _skip_balanced(stmt, pos, "(", ")")
        while pos < len(stmt):
            pos = _skip_ws(stmt, pos)
            if pos < len(stmt) and stmt[pos] == "[":
                pos = _skip_balanced(stmt, pos, "[", "]")
                continue
            ident, pos = _read_ident(stmt, pos)
            if ident:
                names.add(ident)
            pos = _skip_ws(stmt, pos)
            if pos < len(stmt) and stmt[pos] == ",":
                pos += 1
                continue
            break
    return names


_LARGE_MODULE_PROBE_MIN = 256 * 1024
_ASSIGN_PROBE_MISS_CACHE_MAX = 8192
_assign_drive_bases_cache: Dict[str, FrozenSet[str]] = {}
_port_map_bases_cache: Dict[str, FrozenSet[str]] = {}
_assign_probe_miss_cache: Dict[Tuple[str, str], None] = {}
_assign_probe_miss_cache_guard = threading.Lock()
_large_module_probe_cache_guard = threading.Lock()


def _module_body_digest(body: str) -> str:
    clean = _clean_body(body)
    return hashlib.sha256(clean.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def _assign_drive_bases_from_body(body: str) -> FrozenSet[str]:
    return frozenset(collect_assign_net_names(body))


def _ensure_assign_drive_bases_index(body: str) -> FrozenSet[str]:
    """One O(n) statement walk per large body; O(1) membership after (no body regex)."""
    digest = _module_body_digest(body)
    cached = _assign_drive_bases_cache.get(digest)
    if cached is not None:
        return cached
    with _large_module_probe_cache_guard:
        cached = _assign_drive_bases_cache.get(digest)
        if cached is not None:
            return cached
        bases = _assign_drive_bases_from_body(body)
        _assign_drive_bases_cache[digest] = bases
        return bases


def _port_map_bases_from_body(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> FrozenSet[str]:
    pmap = dict(param_map or {})
    names: Set[str] = set()
    for _inst, ports in instance_port_maps(body, param_map=pmap).items():
        for _port, expr in ports:
            names.update(_net_name_bases(extract_connect_nodes(expr, pmap)))
    return frozenset(names)


def _ensure_port_map_bases_index(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> FrozenSet[str]:
    """One O(n) inst/port walk per large body; O(1) membership after (no body regex)."""
    digest = _module_body_digest(body)
    cached = _port_map_bases_cache.get(digest)
    if cached is not None:
        return cached
    with _large_module_probe_cache_guard:
        cached = _port_map_bases_cache.get(digest)
        if cached is not None:
            return cached
        bases = _port_map_bases_from_body(body, param_map=param_map)
        _port_map_bases_cache[digest] = bases
        return bases


def _seed_assign_drive_bases_cache(body: str, bases: Iterable[str]) -> None:
    if len(body) < _LARGE_MODULE_PROBE_MIN:
        return
    digest = _module_body_digest(body)
    if digest in _assign_drive_bases_cache:
        return
    _assign_drive_bases_cache[digest] = _assign_drive_bases_from_body(body)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _word_boundary_find(haystack: str, needle: str, start: int = 0) -> int:
    """Case-insensitive str.find with identifier word boundaries."""
    if not needle:
        return -1
    hlow = haystack.lower()
    nlow = needle.lower()
    nlen = len(needle)
    pos = start
    while pos < len(haystack):
        idx = hlow.find(nlow, pos)
        if idx < 0:
            return -1
        before = haystack[idx - 1] if idx > 0 else ""
        after_i = idx + nlen
        after = haystack[after_i] if after_i < len(haystack) else ""
        if not (before and _is_word_char(before)) and not (
            after and _is_word_char(after)
        ):
            return idx
        pos = idx + 1
    return -1


def _str_find_net_in_assign(clean: str, target: str) -> bool:
    """O(n) str scan for assign/``<=`` drives (no whole-body regex)."""
    if not clean or not target:
        return False
    pos = 0
    assign_kw = "assign"
    while pos < len(clean):
        idx = _word_boundary_find(clean, target, pos)
        if idx < 0:
            return False
        window_start = max(0, idx - 96)
        prefix = clean[window_start:idx].lower()
        if assign_kw in prefix or "<=" in prefix:
            return True
        pos = idx + 1
    return False


def _regex_net_in_assign(clean: str, target: str) -> bool:
    """Small-body fallback — ``[^;]`` only (no redundant ``\\n`` alternation)."""
    esc = re.escape(target)
    if re.search(
        rf"\bassign\b[^;]*?\b{esc}\b",
        clean,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(rf"\b{esc}\s*<=", clean, flags=re.IGNORECASE):
        return True
    if re.search(
        rf"<=\s*[^;]*?\b{esc}\b",
        clean,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _net_base_in_assign_regex_fast(body: str, base: str) -> bool:
    """Probe assign/``<=`` drives; large bodies use pre-indexed frozenset."""
    if not body or not base:
        return False
    target = base.split("[", 1)[0].split(".", 1)[0]
    if not target:
        return False
    if len(body) >= _LARGE_MODULE_PROBE_MIN:
        return target in _ensure_assign_drive_bases_index(body)
    clean = _clean_body(body)
    if len(clean) >= _LARGE_MODULE_PROBE_MIN:
        return _str_find_net_in_assign(clean, target)
    return _regex_net_in_assign(clean, target)


def _net_name_bases(names: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for name in names:
        if not name:
            continue
        out.add(name)
        out.add(name.split("[", 1)[0].split(".", 1)[0])
    return out


def collect_assign_net_names(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """
    Net roots appearing in continuous/procedural drives (no adjacency graph).

    Includes implicit nets created only by ``assign`` (no ``wire`` decl), matching
    synthesizer behavior (lint may warn).
    """
    pmap = dict(param_map or {})
    names: Set[str] = set()
    for stmt in _iter_connect_statements(body):
        if _stmt_starts_with(stmt, "assign"):
            pos = _skip_ws(stmt, 6)
            eq = _find_blocking_eq(stmt[pos:])
            if eq is None:
                continue
            lhs = stmt[pos : pos + eq]
            rhs = stmt[pos + eq + 1 :]
            names.update(_local_connect_nodes(lhs, pmap))
            names.update(extract_connect_nodes(rhs, pmap))
            continue
        nb = _nb_assign_from_stmt(stmt, param_map=pmap)
        if nb is not None:
            lhs, rhs = nb
            names.update(lhs)
            names.update(rhs)
            continue
        if _is_decl_alias_statement(stmt):
            continue
        eq = _find_blocking_eq(stmt)
        if eq is not None:
            names.update(_local_connect_nodes(stmt[:eq], pmap))
            names.update(extract_connect_nodes(stmt[eq + 1 :], pmap))
    return _net_name_bases(names)


def _str_find_net_in_port_map(clean: str, target: str) -> bool:
    """O(n) str scan: *target* as port actual after ``.inst (`` (no whole-body regex)."""
    if not clean or not target:
        return False
    pos = 0
    while pos < len(clean):
        idx = _word_boundary_find(clean, target, pos)
        if idx < 0:
            return False
        window_start = max(0, idx - 160)
        prefix = clean[window_start:idx]
        if re.search(
            r"\.(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)\s*\([^)]*$",
            prefix,
            flags=re.IGNORECASE,
        ):
            return True
        pos = idx + 1
    return False


def _net_base_in_port_map_regex_fast(body: str, base: str) -> bool:
    """Probe port-map nets; large bodies use pre-index or O(n) str scan."""
    if not body or not base:
        return False
    target = base.split("[", 1)[0].split(".", 1)[0]
    if not target:
        return False
    if len(body) >= _LARGE_MODULE_PROBE_MIN:
        return target in _ensure_port_map_bases_index(body)
    clean = _clean_body(body)
    if len(clean) >= _LARGE_MODULE_PROBE_MIN:
        return _str_find_net_in_port_map(clean, target)
    esc = re.escape(target)
    pat = re.compile(
        rf"\.(?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*)\s*\([^)]*\b{esc}\b",
        re.IGNORECASE,
    )
    return pat.search(clean) is not None


def net_base_in_port_map_probe(
    body: str,
    base: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> bool:
    """Early-exit: does *base* appear in any instance port expression?"""
    if not body or not base:
        return False
    target = base.split("[", 1)[0].split(".", 1)[0]
    if not target:
        return False
    if _net_base_in_port_map_regex_fast(body, target):
        return True
    pmap = dict(param_map or {})
    for _inst, ports in instance_port_maps(body, param_map=pmap).items():
        for _port, expr in ports:
            for net in extract_connect_nodes(expr, pmap):
                if net.split("[", 1)[0].split(".", 1)[0] == target:
                    return True
    return False


def net_base_in_assign_probe(
    body: str,
    base: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> bool:
    """Early-exit: does *base* appear as an assign/procedural drive endpoint?"""
    if not body or not base:
        return False
    target = base.split("[", 1)[0].split(".", 1)[0]
    if not target:
        return False
    if not _net_base_in_assign_regex_fast(body, target):
        return False
    pmap = dict(param_map or {})
    for stmt in _iter_connect_statements(body):
        if _stmt_starts_with(stmt, "assign"):
            pos = _skip_ws(stmt, 6)
            eq = _find_blocking_eq(stmt[pos:])
            if eq is None:
                continue
            lhs = stmt[pos : pos + eq]
            rhs = stmt[pos + eq + 1 :]
            pools = (_local_connect_nodes(lhs, pmap), extract_connect_nodes(rhs, pmap))
        else:
            nb = _nb_assign_from_stmt(stmt, param_map=pmap)
            if nb is not None:
                pools = nb
            elif _is_decl_alias_statement(stmt):
                continue
            else:
                eq = _find_blocking_eq(stmt)
                if eq is None:
                    continue
                pools = (
                    _local_connect_nodes(stmt[:eq], pmap),
                    extract_connect_nodes(stmt[eq + 1 :], pmap),
                )
        for group in pools:
            for net in group:
                if net.split("[", 1)[0].split(".", 1)[0] == target:
                    return True
    return False


def _seed_adj_from_instance_ports(
    adj: Dict[str, Set[str]],
    inst_ports: Mapping[str, List[Tuple[str, str]]],
    expr_cache: Dict[str, FrozenSet[str]],
    *,
    param_map: Mapping[str, str],
) -> None:
    """Instance port-map expressions are valid internal signal/net endpoints."""
    for _inst, ports in inst_ports.items():
        for _port, expr in ports:
            for root in _cached_expr_roots(expr, expr_cache, param_map=param_map):
                _ensure_adj_node(adj, root)


_MD_DECL_SUFFIX_CAP = 256
_MD_DIM_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def _collect_decl_md_suffixes(
    body: str,
    param_map: Mapping[str, str],
    *,
    resolve_param_dims: bool = True,
) -> Dict[str, List[str]]:
    """Materialized multi-dim suffixes from ``logic [2:0][3:0] foo`` declarations."""
    from hierwalk.port_scan import expand_port_name, indices_for_bounds, resolve_dim_spec

    pmap = dict(param_map)
    out: Dict[str, List[str]] = {}
    pat = re.compile(
        r"\b(?:input|output|inout)?\s*(?:logic|wire|reg)?\s*((?:\[[^\]]+\])+)\s*"
        r"((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
        re.IGNORECASE,
    )
    for m in pat.finditer(body):
        dims = m.group(1)
        base = m.group(2)
        dim_specs = _MD_DIM_BRACKET_RE.findall(dims)
        names: List[str] = []
        if not resolve_param_dims:
            names = [base]
        elif dim_specs and any(
            resolve_dim_spec(spec, pmap) is None for spec in dim_specs
        ):
            try:
                names = expand_port_name(base, dims)
            except ValueError:
                names = [base]
        else:
            index_lists: List[List[int]] = []
            for spec in dim_specs:
                bounds = resolve_dim_spec(spec, pmap)
                if bounds is None:
                    index_lists = []
                    break
                index_lists.append(indices_for_bounds(bounds[0], bounds[1]))
            if index_lists:
                product = 1
                for idxs in index_lists:
                    product *= len(idxs)
                if product > _MD_DECL_SUFFIX_CAP:
                    try:
                        names = expand_port_name(base, dims)
                    except ValueError:
                        names = [base]
                else:
                    cur = [base]
                    for idxs in index_lists:
                        nxt: List[str] = []
                        for prefix in cur:
                            for idx in idxs:
                                nxt.append(f"{prefix}[{idx}]")
                        cur = nxt
                    names = cur
            else:
                try:
                    names = expand_port_name(base, dims)
                except ValueError:
                    names = [base]
        suffixes = [
            name[len(base) :]
            for name in names
            if name != base and name.startswith(base + "[")
        ]
        if suffixes:
            out[base] = sorted(set(suffixes))
    return out


def _md_suffixes_for_token(
    token: str,
    param_map: Mapping[str, str],
    decl_md_suffixes: Mapping[str, List[str]],
    decl_widths: Mapping[str, List[int]],
    *,
    resolve_param_dims: bool = True,
) -> Optional[List[str]]:
    """Per-element suffixes for a net token (supports ``[i][j]`` buses)."""
    base, suffix = _split_net_base_suffix(token)
    if suffix:
        if ":" in suffix:
            if not resolve_param_dims:
                return [suffix]
            from hierwalk.port_scan import expand_port_name, resolve_dim_spec

            dim_specs = _MD_DIM_BRACKET_RE.findall(suffix)
            resolved_width = suffix
            if dim_specs:
                resolved_parts: List[str] = []
                for spec in dim_specs:
                    if ":" in spec:
                        bounds = resolve_dim_spec(spec, param_map)
                        if bounds is None:
                            resolved_parts = []
                            break
                        resolved_parts.append(f"[{bounds[0]}:{bounds[1]}]")
                    else:
                        val = resolve_dim_spec(spec, param_map)
                        if val is None:
                            resolved_parts = []
                            break
                        resolved_parts.append(f"[{val}:0]")
                if resolved_parts:
                    resolved_width = "".join(resolved_parts)
            try:
                names = expand_port_name(base, resolved_width)
            except ValueError:
                return [suffix]
            out = sorted(
                {
                    name[len(base) :]
                    for name in names
                    if name != base
                    and name.startswith(base + "[")
                    and ":" not in name[len(base) :]
                }
            )
            return out or [suffix]
        return [suffix]
    md = decl_md_suffixes.get(base)
    if md:
        return list(md)
    bits = decl_widths.get(base)
    if bits:
        return [f"[{i}]" for i in bits]
    return None


def _bit_suffixes_for_token(
    token: str,
    param_map: Mapping[str, str],
    decl_widths: Mapping[str, List[int]],
    *,
    resolve_param_dims: bool = True,
) -> Optional[List[str]]:
    text = re.sub(r"\s+", "", token.strip())
    m = re.match(
        r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))(\[[^\]]+\])?$",
        text,
    )
    if not m:
        return None
    base, sel = m.group(1), m.group(2)
    if sel:
        if not resolve_param_dims:
            return [sel]
        inner = sel[1:-1]
        if ":" in inner:
            bits = _range_to_bit_indices(inner, param_map)
            if bits:
                return [f"[{i}]" for i in bits]
        idx = resolve_param_expr(inner, param_map)
        if idx is not None:
            return [f"[{idx}]"]
        return [sel]
    bits = decl_widths.get(base)
    if bits:
        return [f"[{i}]" for i in bits]
    return None


def _default_bus_bits(
    decl_widths: Mapping[str, List[int]],
) -> Optional[List[int]]:
    if not decl_widths:
        return None
    by_len: Dict[int, List[int]] = {}
    for bits in decl_widths.values():
        by_len.setdefault(len(bits), list(bits))
    if len(by_len) == 1:
        return next(iter(by_len.values()))
    return max(by_len.values(), key=len)


def _expand_hier_bit_links(
    hier_links: Dict[str, List[Tuple[str, str]]],
    hier_ref_targets: Dict[Tuple[str, str], Set[str]],
    *,
    decl_widths: Mapping[str, List[int]],
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    adj: Optional[Dict[str, Set[str]]] = None,
    iface_insts: Optional[Set[str]] = None,
    param_map: Optional[Mapping[str, str]] = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
) -> None:
    """Expand ``assign arr = u_next.arr`` style links to per-bit hier edges."""
    md = dict(decl_md_suffixes or {})
    fallback_bits = _default_bus_bits(decl_widths)
    ifaces = iface_insts or set()
    pmap = dict(param_map or {})
    extra_links: Dict[str, List[Tuple[str, str]]] = {}
    extra_targets: Dict[Tuple[str, str], Set[str]] = {}

    def _bridge_extra(net: str, inst: str, port: str) -> None:
        if adj is None:
            return
        _bridge_hier_pair_to_adj(
            net,
            inst,
            port,
            adj,
            iface_insts=ifaces,
            param_map=pmap,
            decl_widths=decl_widths,
            decl_md_suffixes=md,
            edge_prov=edge_prov,
        )
    for net, pairs in hier_links.items():
        if "[" in net:
            continue
        suffixes = md.get(net)
        if suffixes:
            for inst, port in pairs:
                port_base = port.split("[", 1)[0]
                for sfx in suffixes:
                    bit_net = f"{net}{sfx}"
                    bit_port = port if "[" in port else f"{port_base}{sfx}"
                    extra_links.setdefault(bit_net, []).append((inst, bit_port))
                    extra_targets.setdefault((inst, bit_port), set()).add(bit_net)
            continue
        bits = decl_widths.get(net) or fallback_bits
        if not bits:
            continue
        for inst, port in pairs:
            port_base = port.split("[", 1)[0]
            port_bits = decl_widths.get(port_base, bits)
            if len(port_bits) != len(bits):
                port_bits = bits
            for bit_i, port_bit_i in zip(bits, port_bits):
                bit_net = f"{net}[{bit_i}]"
                bit_port = (
                    f"{port_base}[{port_bit_i}]"
                    if "[" not in port
                    else port
                )
                extra_links.setdefault(bit_net, []).append((inst, bit_port))
                extra_targets.setdefault((inst, bit_port), set()).add(bit_net)
    for net, pairs in extra_links.items():
        hier_links.setdefault(net, []).extend(pairs)
    for key, nets in extra_targets.items():
        hier_ref_targets.setdefault(key, set()).update(nets)
    if adj is not None:
        for net, pairs in hier_links.items():
            for inst, port in pairs:
                _bridge_extra(net, inst, port)


def _expand_assign_bit_links(
    a: str,
    b: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str],
    decl_widths: Mapping[str, List[int]],
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    line: int = 0,
    kind: str = "",
    resolve_param_dims: bool = True,
) -> None:
    """Add per-bit edges for vector / part-select assigns (``arr = bus[9:0]``)."""
    if not resolve_param_dims:
        return
    pmap = dict(param_map)
    md = dict(decl_md_suffixes or {})
    a_sfx = _md_suffixes_for_token(
        a, pmap, md, decl_widths, resolve_param_dims=resolve_param_dims
    )
    b_sfx = _md_suffixes_for_token(
        b, pmap, md, decl_widths, resolve_param_dims=resolve_param_dims
    )
    if not a_sfx and not b_sfx:
        return
    a_base, _ = _split_net_base_suffix(a)
    b_base, _ = _split_net_base_suffix(b)
    if not a_sfx:
        a_sfx = b_sfx
    if not b_sfx:
        b_sfx = a_sfx
    if not a_sfx or not b_sfx or len(a_sfx) != len(b_sfx):
        return
    for asx, bsx in zip(a_sfx, b_sfx):
        _add_undirected(
            adj,
            a_base + asx,
            b_base + bsx,
            edge_prov=edge_prov,
            line=line,
            kind=kind,
        )


class _UnionFind:
    __slots__ = ("parent", "rank")

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0

    def find(self, x: str) -> str:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != x:
            nxt = parent[x]
            parent[x] = root
            x = nxt
        return root

    def union(self, a: str, b: str) -> None:
        self.add(a)
        self.add(b)
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        rank = self.rank
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent = self.parent
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1


def _word_at(text: str, pos: int, word: str) -> bool:
    n = len(text)
    wlen = len(word)
    if pos < 0 or pos + wlen > n:
        return False
    if text[pos : pos + wlen].lower() != word.lower():
        return False
    before = pos == 0 or not (text[pos - 1].isalnum() or text[pos - 1] == "_")
    after = pos + wlen >= n or not (text[pos + wlen].isalnum() or text[pos + wlen] == "_")
    return before and after


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _is_sized_literal_quote(text: str, pos: int) -> bool:
    """True when ``'`` opens ``1'b0`` / ``8'hff`` (not a string delimiter)."""
    if pos >= len(text) or text[pos] != "'":
        return False
    j = pos - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0 or not text[j].isdigit():
        return False
    return pos + 1 < len(text) and text[pos + 1] in "bBdDhH"


def _skip_sized_literal(text: str, pos: int) -> int:
    """Advance past ``'b0`` after optional leading digits were consumed."""
    i = pos + 1
    n = len(text)
    if i < n and text[i] in "bBdDhH":
        i += 1
    while i < n and (text[i].isalnum() or text[i] in "_?"):
        i += 1
    return i


def _scan_string_or_literal(text: str, pos: int) -> Tuple[Optional[str], int]:
    """Return (string_delim, next_pos). Sized literals yield (None, after_literal)."""
    ch = text[pos]
    if ch == "'":
        if _is_sized_literal_quote(text, pos):
            return None, _skip_sized_literal(text, pos)
        return "'", pos + 1
    if ch == '"':
        return '"', pos + 1
    return None, pos + 1


def _case_keyword_at(text: str, pos: int) -> Optional[str]:
    for kw in ("casex", "casez", "case"):
        if _word_at(text, pos, kw):
            return kw
    return None


def _advance_keyword_depths(text: str, pos: int, depths: Dict[str, int]) -> int:
    """Update begin/generate/case depths; return chars consumed (0 or word len)."""
    for long_kw, slot, delta_neg in (
        ("endgenerate", "generate", True),
        ("endcase", "case", True),
        ("endfunction", "function", True),
        ("endtask", "task", True),
    ):
        if _word_at(text, pos, long_kw) and depths.get(slot, 0) > 0:
            depths[slot] -= 1
            return len(long_kw)
    if _word_at(text, pos, "fork"):
        depths["fork"] = depths.get("fork", 0) + 1
        return 4
    if _word_at(text, pos, "join") and depths.get("fork", 0) > 0:
        depths["fork"] -= 1
        return 4
    if _word_at(text, pos, "generate"):
        depths["generate"] = depths.get("generate", 0) + 1
        return 8
    case_kw = _case_keyword_at(text, pos)
    if case_kw:
        depths["case"] = depths.get("case", 0) + 1
        return len(case_kw)
    if _word_at(text, pos, "begin"):
        depths["begin"] = depths.get("begin", 0) + 1
        return 5
    if _word_at(text, pos, "end") and depths.get("begin", 0) > 0:
        depths["begin"] -= 1
        return 3
    return 0


_LINE_SPLIT_STRUCTURAL_RE = re.compile(
    r"\b(?:begin|endcase|endgenerate|endfunction|endtask|fork|join|"
    r"case|generate|always|function|task|initial|specify|property|sequence)\b",
    re.IGNORECASE,
)
_LINE_SPLIT_MIN_BYTES = 64 * 1024


def _split_statements_line_fast(clean: str) -> Optional[List[str]]:
    """
    O(lines) splitter for large flat bodies (one ``;``-terminated stmt per line).

    Avoids char-wise ``split_statements`` on assign-heavy modules (50k+ lines).
    """
    if len(clean) < _LINE_SPLIT_MIN_BYTES:
        return None
    if _LINE_SPLIT_STRUCTURAL_RE.search(clean):
        return None
    out: List[str] = []
    for raw in clean.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("`"):
            continue
        if line.endswith(";"):
            stmt = line[:-1].strip()
        elif _word_at(line, 0, "endmodule") or _word_at(line, 0, "endinterface"):
            stmt = line
        elif _word_at(line, 0, "endprogram"):
            stmt = line
        else:
            return None
        if stmt:
            out.append(stmt)
    return out or None


@lru_cache(maxsize=16)
def _split_statements_cached(clean: str) -> Tuple[str, ...]:
    fast = _split_statements_line_fast(clean)
    if fast is not None:
        return tuple(fast)
    return tuple(_split_statements_slow(clean))


def split_statements(text: str) -> List[str]:
    """
    Split RTL body on ``;`` at structural depth zero.

    Respects ``()[]{}`` nesting and ``begin/end``, ``generate/endgenerate``,
    ``case/endcase``, ``fork/join`` so procedural blocks stay intact until
    their closing keyword, then sub-split recursively.
    """
    clean = text.strip()
    if not clean:
        return []
    return list(_split_statements_cached(clean))


def _split_statements_slow(text: str) -> List[str]:
    clean = text.strip()
    if not clean:
        return []

    out: List[str] = []
    start = 0
    i = 0
    n = len(clean)
    paren = brack = brace = 0
    depths: Dict[str, int] = {}
    in_string: Optional[str] = None

    while i < n:
        ch = clean[i]

        if in_string:
            if ch == in_string and (i == 0 or clean[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(clean, i)
            if delim:
                in_string = delim
            i = nxt
            continue

        if paren == brack == brace == 0:
            if (
                _word_at(clean, i, "end")
                and depths.get("begin", 0) == 1
                and depths.get("generate", 0) == 0
                and depths.get("case", 0) == 0
                and depths.get("fork", 0) == 0
            ):
                end_pos = i + 3
                after = _skip_ws(clean, end_pos)
                if after < n and clean[after] != ";":
                    chunk = clean[start:end_pos].strip()
                    if chunk:
                        out.extend(_finalize_statement_chunks(chunk))
                    depths["begin"] = 0
                    start = after
                    i = after
                    continue
            consumed = _advance_keyword_depths(clean, i, depths)
            if consumed:
                i += consumed
                continue

        if ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            brack += 1
        elif ch == "]" and brack:
            brack -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace:
            brace -= 1
        elif ch == ";":
            if (
                paren == brack == brace == 0
                and depths.get("begin", 0) == 0
                and depths.get("generate", 0) == 0
                and depths.get("case", 0) == 0
                and depths.get("fork", 0) == 0
            ):
                chunk = clean[start:i].strip()
                if chunk:
                    out.extend(_finalize_statement_chunks(chunk))
                start = i + 1
        i += 1

    tail = clean[start:].strip()
    if tail:
        out.extend(_finalize_statement_chunks(tail))
    return out


def _finalize_statement_chunks(chunk: str) -> List[str]:
    """Re-split bare ``begin/end`` blocks; keep ``always_* begin`` intact."""
    pos = _skip_ws(chunk, 0)
    if _word_at(chunk, pos, "begin") and not _is_sequential_statement(chunk):
        body, _after = _extract_begin_end_body(chunk, pos)
        if body:
            return split_statements(body)
    return [chunk]


def _extract_case_endcase_body(text: str, case_pos: int) -> Tuple[str, int]:
    """Return (case item text, index after ``endcase``)."""
    n = len(text)
    case_kw = _case_keyword_at(text, case_pos)
    if not case_kw:
        return "", case_pos
    i = case_pos + len(case_kw)
    i = _skip_ws(text, i)
    if i < n and text[i] == "(":
        i = _skip_balanced(text, i, "(", ")")
    i = _skip_ws(text, i)
    body_start = i
    depth = 1
    while i < n and depth > 0:
        nested = _case_keyword_at(text, i)
        if nested:
            depth += 1
            i += len(nested)
            continue
        if _word_at(text, i, "endcase"):
            depth -= 1
            if depth == 0:
                return text[body_start:i].strip(), _skip_ws(text, i + 7)
            i += 7
            continue
        i += 1
    return "", case_pos


def _extract_begin_end_body(text: str, begin_pos: int) -> Tuple[str, int]:
    body_start = begin_pos + 5
    body_start = _skip_ws(text, body_start)
    if body_start < len(text) and text[body_start] == ":":
        _, body_start = _read_ident(text, body_start + 1)
        body_start = _skip_ws(text, body_start)
    n = len(text)
    depth = 1
    i = body_start
    while i < n and depth > 0:
        if _word_at(text, i, "begin"):
            depth += 1
            i += 5
            continue
        if _word_at(text, i, "end"):
            depth -= 1
            if depth == 0:
                return text[body_start:i].strip(), _skip_ws(text, i + 3)
            i += 3
            continue
        i += 1
    return "", begin_pos


def _skip_sensitivity_list(text: str, pos: int) -> int:
    pos = _skip_ws(text, pos)
    n = len(text)
    if pos < n and text[pos] == "@":
        pos += 1
        pos = _skip_ws(text, pos)
    if pos < n and text[pos] == "(":
        pos = _skip_balanced(text, pos, "(", ")")
    return pos


def _stmt_starts_with(stmt: str, kw: str) -> bool:
    pos = _skip_ws(stmt, 0)
    return _word_at(stmt, pos, kw)


def _find_blocking_eq(stmt: str) -> Optional[int]:
    n = len(stmt)
    paren = brack = brace = 0
    in_string: Optional[str] = None
    i = 0
    while i < n:
        ch = stmt[i]
        if in_string:
            if ch == in_string and (i == 0 or stmt[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(stmt, i)
            if delim:
                in_string = delim
            i = nxt
            continue
        if ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            brack += 1
        elif ch == "]" and brack:
            brack -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace:
            brace -= 1
        elif ch == "=" and paren == brack == brace == 0:
            if i > 0 and stmt[i - 1] in "<>!=":
                i += 1
                continue
            if i + 1 < n and stmt[i + 1] == "=":
                i += 2
                continue
            return i
        i += 1
    return None


def _lhs_roots_before_nb(
    stmt: str,
    le_pos: int,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    left = stmt[:le_pos].rstrip()
    m = _LVALUE_TAIL_RE.search(left)
    if not m:
        return set()
    return extract_connect_nodes(m.group(1), param_map)


def _nb_assign_lhs_nodes(
    stmt: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """LHS roots for ``q <= expr`` regardless of whether RHS has COI roots."""
    n = len(stmt)
    in_string: Optional[str] = None
    i = 0
    while i < n - 1:
        ch = stmt[i]
        if in_string:
            if ch == in_string and (i == 0 or stmt[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(stmt, i)
            if delim:
                in_string = delim
            i = nxt
            continue
        if stmt[i] == "<" and stmt[i + 1] == "=":
            if i > 0 and stmt[i - 1] == "<":
                i += 2
                continue
            lhs = _lhs_roots_before_nb(stmt, i, param_map=param_map)
            if lhs:
                return lhs
            i += 2
            continue
        i += 1
    return set()


def _nb_assign_from_stmt(
    stmt: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Optional[Tuple[Set[str], Set[str]]]:
    pmap = dict(param_map or {})
    n = len(stmt)
    in_string: Optional[str] = None
    i = 0
    while i < n - 1:
        ch = stmt[i]
        if in_string:
            if ch == in_string and (i == 0 or stmt[i - 1] != "\\"):
                in_string = None
            i += 1
            continue
        if ch in "\"'":
            delim, nxt = _scan_string_or_literal(stmt, i)
            if delim:
                in_string = delim
            i = nxt
            continue
        if stmt[i] == "<" and stmt[i + 1] == "=":
            if i > 0 and stmt[i - 1] == "<":
                i += 2
                continue
            lhs = _lhs_roots_before_nb(stmt, i, param_map=pmap)
            rhs = stmt[i + 2 :].strip()
            if lhs and rhs and "<=" not in rhs:
                rhs_roots = extract_connect_nodes(rhs, pmap)
                if rhs_roots:
                    return lhs, rhs_roots
            i += 2
            continue
        i += 1
    return None


def _is_combinational_always(stmt: str) -> bool:
    pos = _skip_ws(stmt, 0)
    if _word_at(stmt, pos, "always_comb"):
        return True
    if _word_at(stmt, pos, "always"):
        rest = stmt[pos + 6 :]
        return bool(re.search(r"@\s*\(\s*\*\s*\)", rest, re.IGNORECASE | re.DOTALL))
    return False


def _is_sequential_statement(stmt: str) -> bool:
    pos = _skip_ws(stmt, 0)
    if _word_at(stmt, pos, "always_ff"):
        return True
    if _word_at(stmt, pos, "always"):
        rest = stmt[pos + 6 :]
        return bool(
            re.search(r"@\s*\([^)]*(?:posedge|negedge)", rest, re.IGNORECASE | re.DOTALL)
        )
    return False


def _procedural_inner_statements(stmt: str) -> List[str]:
    pos = _skip_ws(stmt, 0)
    if _word_at(stmt, pos, "always_ff"):
        pos += 9
    elif _word_at(stmt, pos, "always_comb"):
        pos += 11
    elif _word_at(stmt, pos, "always"):
        pos += 6
    else:
        return [stmt]
    pos = _skip_sensitivity_list(stmt, pos)
    pos = _skip_ws(stmt, pos)
    if pos >= len(stmt):
        return []
    if _word_at(stmt, pos, "begin"):
        body, _ = _extract_begin_end_body(stmt, pos)
        return split_statements(body) if body else []
    if stmt[pos] == "{":
        close = _skip_balanced(stmt, pos, "{", "}")
        inner = stmt[pos + 1 : close - 1].strip()
        return split_statements(inner) if inner else []
    return [stmt[pos:].strip()]


def _sequential_inner_statements(stmt: str) -> List[str]:
    return _procedural_inner_statements(stmt)


_LITERAL_RHS_RE = re.compile(
    r"^\s*(?:\d+'[bdhBDH][0-9a-fA-FxzXZ?_]+|\d+)\s*$",
    re.IGNORECASE,
)

_ZERO_LITERAL_RE = re.compile(
    r"^\s*(?:0|1?'[bhdBDH]?0+)\s*$",
    re.IGNORECASE,
)

_FUNC_CALL_RHS_RE = re.compile(
    r"^\s*((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))\s*\(",
)
_SYSTEM_FUNC_RHS_RE = re.compile(
    r"^\s*\$[A-Za-z_]\w*\s*\(",
)


def _strip_outer_parens(expr: str) -> str:
    text = expr.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        matched = True
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(text) - 1:
                    matched = False
                    break
        if matched and depth == 0:
            text = text[1:-1].strip()
        else:
            break
    return text


def _is_zero_literal(expr: str) -> bool:
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    if not text:
        return False
    if _ZERO_LITERAL_RE.match(text):
        return True
    val = _param_int(text)
    return val == 0


def _is_const_literal(expr: str, param_map: Mapping[str, str] | None = None) -> bool:
    """True for literal or parameter-resolved constant RHS (any value)."""
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    if not text:
        return True
    if _LITERAL_RHS_RE.match(text):
        return True
    pmap = dict(param_map or {})
    if pmap and resolve_param_expr(text, pmap) is not None:
        return True
    return False


def _negated_expr(expr: str) -> Optional[str]:
    text = _strip_outer_parens(expr.strip())
    if text.startswith("~"):
        return text[1:].strip()
    if text.startswith("!"):
        inner = text[1:].strip()
        if inner and inner[0].isdigit():
            return None
        return inner
    return None


def _is_identity_expr(expr: str, pmap: Mapping[str, str]) -> bool:
    """Detect ``a+0``, ``a*1``, ``a-a`` — no structural COI through arithmetic."""
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    for op in ("+", "-", "*"):
        pos = _find_top_level_op(text, op)
        if pos is None:
            continue
        left = text[:pos].strip()
        right = text[pos + 1 :].strip()
        if op == "+":
            if _is_zero_literal(left) or _is_zero_literal(right):
                return True
        elif op == "-":
            if left == right:
                return True
        elif op == "*":
            one = _is_const_literal(left, pmap) and _param_int(left) == 1
            one = one or (_is_const_literal(right, pmap) and _param_int(right) == 1)
            if one:
                return True
    return False


def _is_self_cancel_expr(expr: str) -> bool:
    """Detect ``a ^ a``, ``a & ~a``, ``a | ~a`` style constant folds."""
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    xor_pos = _find_top_level_op(text, "^")
    if xor_pos is not None:
        left = text[:xor_pos].strip()
        right = text[xor_pos + 1 :].strip()
        if left == right:
            return True
    and_pos = _find_top_level_op(text, "&")
    if and_pos is not None:
        left = text[:and_pos].strip()
        right = text[and_pos + 1 :].strip()
        if left == right or _negated_expr(left) == right or _negated_expr(right) == left:
            return True
    or_pos = _find_top_level_op(text, "|")
    if or_pos is not None:
        left = text[:or_pos].strip()
        right = text[or_pos + 1 :].strip()
        if _negated_expr(left) == right or _negated_expr(right) == left:
            return True
    return False


def _grep_assign_rhs_roots(
    expr: str,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """Text-conn: any identifier appearing on the assign RHS (no constant-fold mask)."""
    pmap = dict(param_map or {})
    roots = set(extract_connect_nodes(expr, pmap))
    for inst, port in extract_hier_refs(expr):
        roots.discard(inst)
        roots.discard(port)
    return roots


def _effective_assign_rhs_roots(
    expr: str,
    param_map: Mapping[str, str] | None = None,
    *,
    zero_nets: Mapping[str, int] | None = None,
    over_approximate_if: bool = True,
    grep_only: bool = False,
) -> Set[str]:
    """Drop constant tie-offs and opaque function-call RHS from structural COI."""
    if grep_only:
        return _grep_assign_rhs_roots(expr, param_map)
    pmap = dict(param_map or {})
    znets = dict(zero_nets or {})
    text = _strip_outer_parens(expr.strip().rstrip(";"))
    if not text or _is_const_literal(text, pmap):
        return set()
    if _is_zero_literal(text):
        return set()
    if _FUNC_CALL_RHS_RE.match(text):
        return set()
    if _SYSTEM_FUNC_RHS_RE.match(text):
        return set()
    if _is_identity_expr(text, pmap):
        return set()
    if _is_self_cancel_expr(text):
        return set()

    qpos = _find_top_level_op(text, "?")
    if qpos is not None:
        cond = text[:qpos].strip()
        true_arm = text[qpos + 1 :]
        cpos = _find_top_level_op(true_arm, ":")
        if cpos is not None:
            when_true = true_arm[:cpos].strip()
            when_false = true_arm[cpos + 1 :].strip()
            if _is_zero_literal(when_true) and _is_zero_literal(when_false):
                return set()
            cond_val = resolve_param_expr(cond, pmap)
            if cond_val is None:
                cond_val = _param_int(cond)
            if cond_val is not None:
                arm = when_true if cond_val else when_false
                return _effective_assign_rhs_roots(
                    arm,
                    pmap,
                    zero_nets=znets,
                    over_approximate_if=over_approximate_if,
                    grep_only=grep_only,
                )
            if over_approximate_if:
                return (
                    _effective_assign_rhs_roots(
                        when_true,
                        pmap,
                        zero_nets=znets,
                        over_approximate_if=over_approximate_if,
                        grep_only=grep_only,
                    )
                    | _effective_assign_rhs_roots(
                        when_false,
                        pmap,
                        zero_nets=znets,
                        over_approximate_if=over_approximate_if,
                        grep_only=grep_only,
                    )
                )
            return _effective_assign_rhs_roots(
                when_false,
                pmap,
                zero_nets=znets,
                over_approximate_if=over_approximate_if,
                grep_only=grep_only,
            )

    and_pos = _find_top_level_op(text, "&")
    if and_pos is not None:
        left = text[:and_pos].strip()
        right = text[and_pos + 1 :].strip()
        if _is_zero_literal(right) or _is_zero_literal(left):
            return set()
        if _is_self_cancel_expr(text):
            return set()
        for side in (left, right):
            side_root = next(iter(extract_connect_nodes(side, pmap)), "")
            if side_root and znets.get(side_root) == 0:
                return set()

    or_pos = _find_top_level_op(text, "|")
    if or_pos is not None:
        left = text[:or_pos].strip()
        right = text[or_pos + 1 :].strip()
        if _is_zero_literal(left):
            return _effective_assign_rhs_roots(
                right, param_map, zero_nets=znets, grep_only=grep_only
            )
        if _is_zero_literal(right):
            return _effective_assign_rhs_roots(
                left, param_map, zero_nets=znets, grep_only=grep_only
            )

    mul_pos = _find_top_level_op(text, "*")
    if mul_pos is not None:
        left = text[:mul_pos].strip()
        right = text[mul_pos + 1 :].strip()
        if _is_zero_literal(left) or _is_zero_literal(right):
            return set()

    roots = extract_connect_nodes(text, param_map)
    for inst, port in extract_hier_refs(text):
        roots.discard(inst)
        roots.discard(port)
    return roots


def _const_assign_from_stmt(
    stmt: str,
    pmap: Mapping[str, str],
) -> Optional[Tuple[str, int]]:
    if not _stmt_starts_with(stmt, "assign"):
        return None
    pos = _skip_ws(stmt, 6)
    eq = _find_blocking_eq(stmt[pos:])
    if eq is None:
        return None
    lhs = extract_connect_nodes(stmt[pos : pos + eq], pmap)
    if len(lhs) != 1:
        return None
    rhs = stmt[pos + eq + 1 :].strip().rstrip(";")
    val = _param_int(rhs) if _LITERAL_RHS_RE.match(rhs) else None
    if val is None:
        val = _eval_index_expr(rhs, pmap)
    if val is None:
        val = resolve_param_expr(rhs, pmap)
    if val is None:
        return None
    return next(iter(lhs)), val


def _collect_const_assigns_from_stmts(
    stmts: Sequence[str],
    *,
    param_map: Mapping[str, str],
    known: Optional[Mapping[str, int]] = None,
) -> Dict[str, int]:
    pmap = _enriched_param_map(param_map, known or {})
    consts: Dict[str, int] = {}
    for stmt in stmts:
        hit = _const_assign_from_stmt(stmt, pmap)
        if hit is not None:
            consts[hit[0]] = hit[1]
    return consts


def _collect_const_assigns_fixed(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
    max_rounds: int = 4,
) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Grow const map with at most *max_rounds* passes over one statement list."""
    pmap = dict(param_map or {})
    stmts = split_statements(_clean_body(body))
    consts: Dict[str, int] = {}
    for _ in range(max_rounds):
        grown = _collect_const_assigns_from_stmts(stmts, param_map=pmap, known=consts)
        if not grown:
            break
        consts.update(grown)
        pmap = _enriched_param_map(pmap, consts)
    return consts, pmap


def _collect_const_assigns(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Dict[str, int]:
    """Map signal -> integer value for ``assign sel = 2'b01`` style ties."""
    consts, _ = _collect_const_assigns_fixed(body, param_map=param_map)
    return consts


def _enriched_param_map(
    param_map: Mapping[str, str],
    consts: Mapping[str, int],
) -> Dict[str, str]:
    out = dict(param_map)
    for name, val in consts.items():
        out[name] = str(val)
    return out


def _case_label_matches(label: str, sel_val: int) -> bool:
    head = label.split(",")[0].strip()
    if not head or head.lower() == "default":
        return False
    val = _param_int(head)
    return val is not None and val == sel_val


def _parse_sized_literal_bits(label: str) -> Optional[Tuple[int, str]]:
    """Return ``(width, bit_pattern)`` for ``2'b?0``-style case labels."""
    text = re.sub(r"\s+", "", label.split(",")[0].strip())
    if not text or text.lower() == "default":
        return None
    m = re.fullmatch(
        r"(\d+)'([bBdDhH])([0-9a-fA-FxzXZ?_]+)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    width = int(m.group(1))
    base = m.group(2).lower()
    digits = m.group(3)
    if base == "b":
        if len(digits) > width:
            digits = digits[-width:]
        elif len(digits) < width:
            digits = digits.zfill(width)
        return width, digits.lower()
    if base == "d":
        try:
            val = int(digits.replace("_", ""), 10)
        except ValueError:
            return None
        return width, format(val & ((1 << width) - 1), f"0{width}b")
    if base == "h":
        try:
            val = int(digits.replace("_", ""), 16)
        except ValueError:
            return None
        bit_len = max(width, len(digits) * 4)
        return width, format(val & ((1 << width) - 1), f"0{width}b")
    return None


def _case_wildcard_matches(label: str, sel_val: int, mode: str) -> bool:
    """Match ``casez``/``casex`` labels against a constant-folded selector."""
    if _case_label_matches(label, sel_val):
        return True
    parsed = _parse_sized_literal_bits(label)
    if not parsed:
        return False
    width, pattern = parsed
    sel_bits = format(sel_val & ((1 << width) - 1), f"0{width}b")
    if len(pattern) > width:
        pattern = pattern[-width:]
    elif len(pattern) < width:
        pattern = pattern.zfill(width)
    dont_care = {"?", "z"}
    if mode == "casex":
        dont_care |= {"x"}
    for lb, sb in zip(pattern, sel_bits):
        if lb in dont_care:
            continue
        if lb != sb:
            return False
    return True


def _case_label_matches_mode(label: str, sel_val: int, mode: str) -> bool:
    if mode == "case":
        return _case_label_matches(label, sel_val)
    return _case_wildcard_matches(label, sel_val, mode)


def _extract_case_selector(stmt: str, case_pos: int) -> Tuple[str, str]:
    """Return ``(selector_expr, case_items_body)`` from ``case (sel) ... endcase``."""
    case_kw = _case_keyword_at(stmt, case_pos)
    if not case_kw:
        return "", ""
    i = case_pos + len(case_kw)
    i = _skip_ws(stmt, i)
    if i < len(stmt) and stmt[i] == "(":
        end = _skip_balanced(stmt, i, "(", ")")
        selector = stmt[i + 1 : end - 1].strip()
        i = _skip_ws(stmt, end)
    else:
        selector = ""
    body, _ = _extract_case_endcase_body(stmt, case_pos)
    return selector, body


def _parse_comb_assign_piece(
    piece: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
    over_approximate_if: bool = True,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
) -> None:
    pmap = dict(param_map or {})
    piece = _strip_case_item_label(piece)
    eq = _find_blocking_eq(piece)
    if eq is None:
        return
    rhs_roots = _effective_assign_rhs_roots(
        piece[eq + 1 :],
        pmap,
        zero_nets=zero_nets,
        over_approximate_if=over_approximate_if,
    )
    if not rhs_roots:
        return
    for a in _local_connect_nodes(piece[:eq], pmap):
        for b in rhs_roots:
            _add_undirected(
                adj,
                a,
                b,
                edge_prov=edge_prov,
                line=stmt_line,
                kind="comb-always",
            )


def _parse_folded_case_body(
    body: str,
    adj: Dict[str, Set[str]],
    sel_val: int,
    *,
    param_map: Mapping[str, str] | None = None,
    case_mode: str = "case",
) -> None:
    default_body = ""
    matched = False
    for item in split_statements(body):
        label, rest = _split_case_item_label(item)
        if label.lower() == "default":
            default_body = rest
            continue
        if _case_label_matches_mode(label, sel_val, case_mode):
            _parse_comb_assign_piece(rest, adj, param_map=param_map)
            matched = True
    if not matched and default_body:
        _parse_comb_assign_piece(default_body, adj, param_map=param_map)


def _condition_is_const(
    cond: str,
    consts: Mapping[str, int],
    param_map: Mapping[str, str],
) -> Optional[bool]:
    text = cond.strip()
    if text in consts:
        return consts[text] != 0
    val = resolve_param_expr(text, param_map)
    if val is not None:
        return val != 0
    lit = _param_int(text)
    if lit is not None:
        return lit != 0
    return None


def _if_condition_has_relational(cond: str) -> bool:
    text = re.sub(r"\s+", "", cond)
    for op in ("<=", ">=", "==", "!=", "<", ">"):
        if op in text:
            return True
    return False


def _split_if_else(inner: str) -> Optional[Tuple[str, str, str]]:
    """Return ``(cond, then_part, else_part)`` for ``if (c) a; else b;``."""
    pos = _skip_ws(inner, 0)
    if not _word_at(inner, pos, "if"):
        return None
    pos = _skip_ws(inner, pos + 2)
    if pos >= len(inner) or inner[pos] != "(":
        return None
    end = _skip_balanced(inner, pos, "(", ")")
    cond = inner[pos + 1 : end - 1].strip()
    rest = inner[end:].strip()
    m = re.search(r"\belse\b", rest, re.IGNORECASE)
    if m:
        then_part = rest[: m.start()].strip().rstrip(";")
        else_part = rest[m.end() :].strip().rstrip(";")
    else:
        then_part = rest.strip().rstrip(";")
        else_part = ""
    return cond, then_part, else_part


def _parse_comb_case_union_all(
    body: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
) -> None:
    """Over-approximate unresolved comb ``case`` by unioning every arm."""
    default_body = ""
    for item in split_statements(body):
        label, rest = _split_case_item_label(item)
        if label.lower() == "default":
            default_body = rest
            continue
        _parse_comb_assign_piece(rest, adj, param_map=param_map, zero_nets=zero_nets)
    if default_body:
        _parse_comb_assign_piece(default_body, adj, param_map=param_map, zero_nets=zero_nets)


def _parse_comb_always_stmt(
    stmt: str,
    adj: Dict[str, Set[str]],
    *,
    consts: Mapping[str, int] | None = None,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
    module_body: str = "",
    over_approximate_if: bool = True,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
) -> None:
    if not _is_combinational_always(stmt):
        return
    sel_consts = dict(consts or {})
    pmap = dict(param_map or {})
    inners = _procedural_inner_statements(stmt)
    last_plain: Dict[str, str] = {}
    deferred: List[str] = []
    for inner in inners:
        pos = _skip_ws(inner, 0)
        case_kw = _case_keyword_at(inner, pos)
        if case_kw in ("case", "casez", "casex"):
            selector, body = _extract_case_selector(inner, pos)
            sel_key = re.sub(r"\s+", "", selector)
            m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", sel_key)
            if m and body:
                var = m.group(1)
                if var in sel_consts:
                    _parse_folded_case_body(
                        body,
                        adj,
                        sel_consts[var],
                        param_map=pmap,
                        case_mode=case_kw,
                    )
                elif case_kw == "case":
                    _parse_comb_case_union_all(
                        body, adj, param_map=pmap, zero_nets=zero_nets
                    )
                elif _selector_wildcard_assigned(module_body, var, pmap):
                    _parse_comb_case_union_all(
                        body, adj, param_map=pmap, zero_nets=zero_nets
                    )
            continue
        if_split = _split_if_else(inner)
        if if_split is not None:
            cond, then_part, else_part = if_split
            truth = _condition_is_const(cond, sel_consts, pmap)
            if truth is True:
                if then_part:
                    deferred.append(then_part)
            elif truth is False:
                if else_part:
                    deferred.append(else_part)
            elif over_approximate_if:
                if then_part:
                    deferred.append(then_part)
                if else_part:
                    deferred.append(else_part)
            elif else_part:
                deferred.append(else_part)
            continue
        eq = _find_blocking_eq(inner)
        if eq is not None:
            for lhs in _local_connect_nodes(inner[:eq], pmap):
                last_plain[lhs] = inner
            continue
        deferred.append(inner)
    for inner in dict.fromkeys(last_plain.values()):
        _parse_comb_assign_piece(
            inner,
            adj,
            param_map=pmap,
            zero_nets=zero_nets,
            over_approximate_if=over_approximate_if,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
        )
    for inner in deferred:
        if inner:
            _parse_comb_if_or_assign_piece(
                inner,
                adj,
                sel_consts,
                param_map=pmap,
                zero_nets=zero_nets,
                over_approximate_if=over_approximate_if,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
            )


def _parse_comb_if_or_assign_piece(
    inner: str,
    adj: Dict[str, Set[str]],
    sel_consts: Mapping[str, int],
    *,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
    over_approximate_if: bool = True,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
) -> None:
    pmap = dict(param_map or {})
    if_split = _split_if_else(inner)
    if if_split is not None:
        cond, then_part, else_part = if_split
        truth = _condition_is_const(cond, sel_consts, pmap)
        if truth is True:
            if then_part:
                _parse_comb_if_or_assign_piece(
                    then_part,
                    adj,
                    sel_consts,
                    param_map=pmap,
                    zero_nets=zero_nets,
                    over_approximate_if=over_approximate_if,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                )
        elif truth is False:
            if else_part:
                _parse_comb_if_or_assign_piece(
                    else_part,
                    adj,
                    sel_consts,
                    param_map=pmap,
                    zero_nets=zero_nets,
                    over_approximate_if=over_approximate_if,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                )
        elif over_approximate_if:
            if then_part:
                _parse_comb_if_or_assign_piece(
                    then_part,
                    adj,
                    sel_consts,
                    param_map=pmap,
                    zero_nets=zero_nets,
                    over_approximate_if=over_approximate_if,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                )
            if else_part:
                _parse_comb_if_or_assign_piece(
                    else_part,
                    adj,
                    sel_consts,
                    param_map=pmap,
                    zero_nets=zero_nets,
                    over_approximate_if=over_approximate_if,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                )
        elif else_part:
            _parse_comb_if_or_assign_piece(
                else_part,
                adj,
                sel_consts,
                param_map=pmap,
                zero_nets=zero_nets,
                over_approximate_if=over_approximate_if,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
            )
        return
    _parse_comb_assign_piece(
        inner,
        adj,
        param_map=pmap,
        zero_nets=zero_nets,
        over_approximate_if=over_approximate_if,
        edge_prov=edge_prov,
        stmt_line=stmt_line,
    )


def _is_decl_alias_statement(stmt: str) -> bool:
    pos = _skip_ws(stmt, 0)
    if not any(_word_at(stmt, pos, kw) for kw in ("wire", "logic", "reg")):
        return False
    return _find_blocking_eq(stmt) is not None


def _parse_decl_alias_stmt(
    stmt: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
) -> None:
    if not _is_decl_alias_statement(stmt):
        return
    eq = _find_blocking_eq(stmt)
    if eq is None:
        return
    pmap = dict(param_map or {})
    rhs_roots = _effective_assign_rhs_roots(
        stmt[eq + 1 :], pmap, zero_nets=zero_nets
    )
    if not rhs_roots:
        return
    for a in _local_connect_nodes(stmt[:eq], pmap):
        for b in rhs_roots:
            _add_undirected(adj, a, b)


def _is_instance_statement(stmt: str) -> bool:
    pos = _skip_ws(stmt, 0)
    cell, nxt = _read_ident(stmt, pos)
    if not cell or cell.lower() in _KEYWORDS:
        return False
    nxt = _skip_ws(stmt, nxt)
    if nxt < len(stmt) and stmt[nxt] == "#":
        return True
    inst, _ = _read_ident(stmt, nxt)
    return bool(inst)


def _flatten_connect_statements(stmt: str) -> List[str]:
    """Unwrap ``for/generate begin`` wrappers; surface embedded procedural stmts."""
    stmt = stmt.strip()
    if not stmt:
        return []
    if (
        _is_sequential_statement(stmt)
        or _is_combinational_always(stmt)
        or _stmt_starts_with(stmt, "assign")
        or _is_instance_statement(stmt)
    ):
        return [stmt]

    pos = _skip_ws(stmt, 0)
    if _case_keyword_at(stmt, pos):
        return []

    pos = 0
    while pos < len(stmt):
        if _word_at(stmt, pos, "begin"):
            body, _ = _extract_begin_end_body(stmt, pos)
            if body:
                flat: List[str] = []
                for inner in split_statements(body):
                    flat.extend(_flatten_connect_statements(inner))
                return flat
            break
        pos += 1

    m = re.search(r"\balways_ff\b", stmt, re.IGNORECASE)
    if m:
        return _flatten_connect_statements(stmt[m.start() :])
    m2 = re.search(r"\balways\s*@", stmt, re.IGNORECASE)
    if m2 and _is_sequential_statement(stmt[m2.start() :]):
        return _flatten_connect_statements(stmt[m2.start() :])
    if _nb_assign_from_stmt(stmt):
        return [stmt]
    if _is_decl_alias_statement(stmt):
        return [stmt]
    return []


def prepare_connect_body(
    body: str,
    param_map: Mapping[str, str] | None = None,
    defines: Mapping[str, str] | None = None,
    *,
    over_approximate_if: bool = True,
    source_file: str | None = None,
    include_dirs: Sequence[str] | None = None,
) -> str:
    """Apply in-body ``define``, macro expand, ``ifdef``, and generate fold."""
    from pathlib import Path

    from hierwalk.preprocess import _preprocess_conditional_pass

    pmap = dict(defines or {})
    inc = [Path(p) for p in include_dirs] if include_dirs else ()
    src_path = Path(source_file) if source_file else None
    filtered = _preprocess_conditional_pass(
        body,
        pmap,
        apply_ifdef=True,
        source_file=src_path,
        include_dirs=inc,
        visiting=set(),
    )
    body_params = collect_connect_module_params("", filtered)
    full_pmap = resolve_param_map(
        body_params,
        parent=pmap,
        overrides=dict(param_map or {}),
    )
    fold_ctx = dict(pmap)
    fold_ctx.update(full_pmap)
    return prepare_body_for_instance_scan(
        filtered,
        fold_ctx,
        over_approximate_if=over_approximate_if,
    )


def _is_primitive_gate_statement(stmt: str) -> bool:
    pos = _skip_ws(stmt, 0)
    gate, _ = _read_ident(stmt, pos)
    return bool(gate and gate.lower() in _PRIMITIVE_GATES)


def _merge_split_else_stmts(stmts: Sequence[str]) -> List[str]:
    """Rejoin ``if (c) x;`` / ``else y;`` pairs split by ``split_statements``."""
    out: List[str] = []
    for stmt in stmts:
        text = stmt.strip()
        if text.lower().startswith("else") and out:
            out[-1] = out[-1].rstrip().rstrip(";") + "; " + text
        else:
            out.append(stmt)
    return out


def _offset_line(body: str, offset: int) -> int:
    if offset <= 0:
        return 1
    return body[:offset].count("\n") + 1


def _stmt_offset(body: str, stmt: str, search_from: int = 0) -> int:
    needle = stmt.strip()
    if not needle:
        return -1
    pos = body.find(needle, search_from)
    if pos >= 0:
        return pos
    short = needle[: min(64, len(needle))]
    return body.find(short, search_from)


def _iter_connect_statements_with_lines(body: str) -> Iterable[Tuple[str, int]]:
    clean = _clean_body_for_connect_scan(body)
    search_from = 0
    for stmt in _merge_split_else_stmts(split_statements(clean)):
        pieces = _flatten_connect_statements(stmt)
        if pieces:
            for piece in pieces:
                pos = _stmt_offset(clean, piece, search_from)
                line = _offset_line(clean, pos) if pos >= 0 else 0
                yield piece, line
                if pos >= 0:
                    search_from = pos + 1
        elif _is_primitive_gate_statement(stmt):
            pos = _stmt_offset(clean, stmt, search_from)
            line = _offset_line(clean, pos) if pos >= 0 else 0
            yield stmt.strip(), line
            if pos >= 0:
                search_from = pos + 1


def _iter_connect_statements(body: str) -> List[str]:
    return [stmt for stmt, _line in _iter_connect_statements_with_lines(body)]


def _bridge_hier_pair_to_adj(
    net: str,
    inst: str,
    port: str,
    adj: Dict[str, Set[str]],
    *,
    iface_insts: Set[str],
    param_map: Mapping[str, str],
    decl_widths: Mapping[str, List[int]],
    decl_md_suffixes: Mapping[str, List[str]],
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
) -> None:
    if inst in iface_insts:
        return
    hier_net = f"{inst}.{port}"
    if hier_net in adj.get(net, ()):
        return
    _add_undirected(adj, net, hier_net, edge_prov=edge_prov)
    if "[" in net or "[" in port:
        _expand_assign_bit_links(
            net,
            hier_net,
            adj,
            param_map=param_map,
            decl_widths=decl_widths,
            decl_md_suffixes=decl_md_suffixes,
            edge_prov=edge_prov,
        )


def _link_braced_concat_assign(
    lhs: str,
    rhs: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str],
    decl_widths: Mapping[str, List[int]],
    decl_md_suffixes: Mapping[str, List[str]],
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    zero_nets: Optional[Mapping[str, int]] = None,
    over_approximate_if: bool = True,
    iface_insts: Optional[Set[str]] = None,
    braced_concat_bases: Optional[Set[str]] = None,
) -> bool:
    """
    ``lhs = {a,b,…}`` MSB-first: leftmost element maps to the MSB of *lhs* bus.
    """
    if not _is_braced_concat_rhs(rhs):
        return False
    pmap = dict(param_map)
    inner = _strip_outer_parens(rhs.strip().rstrip(";")).strip()[1:-1]
    parts = _expand_concat_elements(inner)
    if not parts:
        return False
    lhs_nodes = list(_local_connect_nodes(lhs, pmap))
    if len(lhs_nodes) != 1:
        return False
    lhs_token = lhs_nodes[0]
    lhs_base, _ = _split_net_base_suffix(lhs_token)
    bits = decl_widths.get(lhs_base)
    if not bits:
        bits = list(range(len(parts)))
    elif len(bits) != len(parts):
        return False
    # Verilog concat is MSB-first; ``[N:0]`` bit lists are stored LSB-first.
    if bits[-1] >= bits[0]:
        concat_bits = list(reversed(bits))
    else:
        concat_bits = list(bits)
    lhs_sfx = [f"[{i}]" for i in concat_bits]
    znets = dict(zero_nets or {})
    ifaces = iface_insts or set()
    for sfx, part in zip(lhs_sfx, parts):
        if _is_const_literal(part, pmap):
            continue
        part_roots = _effective_assign_rhs_roots(
            part,
            pmap,
            zero_nets=znets,
            over_approximate_if=over_approximate_if,
        )
        if not part_roots:
            part_roots = set(extract_connect_nodes(part, pmap))
        if not part_roots:
            continue
        lhs_bit = lhs_base + sfx
        for root in part_roots:
            _add_undirected(
                adj,
                lhs_bit,
                root,
                edge_prov=edge_prov,
                line=stmt_line,
                kind="assign",
            )
    if braced_concat_bases is not None:
        braced_concat_bases.add(lhs_base)
    return True


def _register_hier_assign(
    lhs: str,
    rhs: str,
    *,
    hier_links: Dict[str, List[Tuple[str, str]]],
    hier_ref_targets: Dict[Tuple[str, str], Set[str]],
    adj: Optional[Dict[str, Set[str]]] = None,
    iface_insts: Optional[Set[str]] = None,
    param_map: Optional[Mapping[str, str]] = None,
    decl_widths: Optional[Mapping[str, List[int]]] = None,
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
) -> None:
    lhs_nodes = extract_connect_nodes(lhs)
    rhs_hier = extract_hier_refs(rhs)
    lhs_hier = extract_hier_refs(lhs)
    for net in lhs_nodes:
        for inst, port in rhs_hier:
            hier_links.setdefault(net, []).append((inst, port))
            hier_ref_targets.setdefault((inst, port), set()).add(net)
    for net in extract_connect_nodes(rhs):
        for inst, port in lhs_hier:
            hier_links.setdefault(net, []).append((inst, port))
            hier_ref_targets.setdefault((inst, port), set()).add(net)


def _parse_assign_stmt(
    stmt: str,
    adj: Dict[str, Set[str]],
    *,
    hier_links: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    hier_ref_targets: Optional[Dict[Tuple[str, str], Set[str]]] = None,
    param_map: Mapping[str, str] | None = None,
    zero_nets: Mapping[str, int] | None = None,
    over_approximate_if: bool = True,
    decl_widths: Optional[Mapping[str, List[int]]] = None,
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    iface_insts: Optional[Set[str]] = None,
    braced_concat_bases: Optional[Set[str]] = None,
    grep_only: bool = False,
) -> None:
    pmap = dict(param_map or {})
    if not _stmt_starts_with(stmt, "assign"):
        return
    pos = _skip_ws(stmt, 6)
    eq = _find_blocking_eq(stmt[pos:])
    if eq is None:
        return
    lhs = stmt[pos : pos + eq]
    rhs = stmt[pos + eq + 1 :]
    lhs_hier = extract_hier_refs(lhs)
    rhs_hier = extract_hier_refs(rhs)
    hier_pairs = lhs_hier + rhs_hier
    ifaces = iface_insts or set()
    widths = dict(decl_widths or {})
    md_suffixes = dict(decl_md_suffixes or {})
    if hier_links is not None and hier_ref_targets is not None:
        if hier_pairs:
            _register_hier_assign(
                lhs,
                rhs,
                hier_links=hier_links,
                hier_ref_targets=hier_ref_targets,
                adj=adj,
                iface_insts=ifaces,
                param_map=pmap,
                decl_widths=widths,
                decl_md_suffixes=md_suffixes,
                edge_prov=edge_prov,
            )
    if _link_braced_concat_assign(
        lhs,
        rhs,
        adj,
        param_map=pmap,
        decl_widths=widths,
        decl_md_suffixes=md_suffixes,
        edge_prov=edge_prov,
        stmt_line=stmt_line,
        zero_nets=zero_nets,
        over_approximate_if=over_approximate_if,
        iface_insts=ifaces,
        braced_concat_bases=braced_concat_bases,
    ):
        return
    if _is_braced_concat_rhs(rhs):
        return
    rhs_roots = _effective_assign_rhs_roots(
        rhs,
        pmap,
        zero_nets=zero_nets,
        over_approximate_if=over_approximate_if,
        grep_only=grep_only,
    )
    if not rhs_roots:
        return
    if ifaces:
        rhs_iface_ports = {port for inst, port in rhs_hier if inst in ifaces}
        lhs_iface_ports = {port for inst, port in lhs_hier if inst in ifaces}
        lhs_bare = _bare_identifiers_in_expr(lhs)
        rhs_bare = _bare_identifiers_in_expr(rhs)
        lhs_iface_skip = {port for port in lhs_iface_ports if port not in lhs_bare}
        rhs_iface_skip = {port for port in rhs_iface_ports if port not in rhs_bare}
    else:
        lhs_iface_skip = set()
        rhs_iface_skip = set()
    for a in _local_connect_nodes(lhs, pmap):
        if a in lhs_iface_skip:
            continue
        for b in rhs_roots:
            if b in rhs_iface_skip:
                continue
            _add_undirected(
                adj,
                a,
                b,
                edge_prov=edge_prov,
                line=stmt_line,
                kind="assign",
            )
            _expand_assign_bit_links(
                a,
                b,
                adj,
                param_map=pmap,
                decl_widths=widths,
                decl_md_suffixes=md_suffixes,
                edge_prov=edge_prov,
                line=stmt_line,
                kind="assign",
            )


def _parse_primitive_gate_stmt(stmt: str, adj: Dict[str, Set[str]]) -> None:
    pos = _skip_ws(stmt, 0)
    gate, pos = _read_ident(stmt, pos)
    if not gate or gate.lower() not in _PRIMITIVE_GATES:
        return
    pos = _skip_ws(stmt, pos)
    if pos < len(stmt) and stmt[pos] == "(":
        inst = ""
    else:
        inst, pos = _read_ident(stmt, pos)
        if not inst:
            return
        pos = _skip_ws(stmt, pos)
    if pos >= len(stmt) or stmt[pos] != "(":
        return
    end = _skip_balanced(stmt, pos, "(", ")")
    inner = stmt[pos + 1 : end - 1]
    ports: List[str] = []
    for piece in inner.split(","):
        name = piece.strip()
        if not name:
            continue
        m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", name)
        if m:
            ports.append(m.group(1))
    if len(ports) < 2:
        return
    out_port, in_ports = ports[0], ports[1:]
    for inp in in_ports:
        _add_undirected(adj, out_port, inp)


def _parse_ff_assign_piece(
    piece: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str] | None = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    ff_net_lines: Optional[Dict[str, int]] = None,
) -> None:
    piece = _strip_case_item_label(piece)
    pair = _nb_assign_from_stmt(piece, param_map=param_map)
    if not pair:
        return
    lhs, rhs = pair
    if ff_net_lines is not None and stmt_line > 0:
        for net in lhs | rhs:
            ff_net_lines.setdefault(net, stmt_line)
    for q in lhs:
        for d in rhs:
            _add_undirected(
                adj,
                q,
                d,
                edge_prov=edge_prov,
                line=stmt_line,
                kind="always_ff",
            )


def _selector_wildcard_assigned(
    body: str,
    var: str,
    param_map: Mapping[str, str] | None = None,
) -> bool:
    """True when ``assign var = 4'b?01`` style ties use x/z/? literals."""
    pmap = dict(param_map or {})
    pat = re.compile(
        rf"\bassign\s+{re.escape(var)}\s*=\s*([^;]+)",
        re.IGNORECASE,
    )
    for stmt in split_statements(_clean_body(body)):
        m = pat.search(stmt)
        if m and re.search(r"[xzXZ?]", m.group(1)):
            return True
    return False


def _parse_ff_case_union_all(
    body: str,
    adj: Dict[str, Set[str]],
    *,
    param_map: Mapping[str, str] | None = None,
) -> None:
    """Over-approximate unresolved ``case`` by unioning every arm."""
    default_body = ""
    for item in split_statements(body):
        label, rest = _split_case_item_label(item)
        if label.lower() == "default":
            default_body = rest
            continue
        _parse_ff_assign_piece(rest, adj, param_map=param_map)
    if default_body:
        _parse_ff_assign_piece(default_body, adj, param_map=param_map)


def _parse_folded_ff_case_body(
    body: str,
    adj: Dict[str, Set[str]],
    sel_val: int,
    *,
    param_map: Mapping[str, str] | None = None,
    case_mode: str = "case",
) -> None:
    default_body = ""
    matched = False
    for item in split_statements(body):
        label, rest = _split_case_item_label(item)
        if label.lower() == "default":
            default_body = rest
            continue
        if _case_label_matches_mode(label, sel_val, case_mode):
            _parse_ff_assign_piece(rest, adj, param_map=param_map)
            matched = True
    if not matched and default_body:
        _parse_ff_assign_piece(default_body, adj, param_map=param_map)


def _unwrap_else_begin(stmt: str) -> Optional[str]:
    pos = _skip_ws(stmt, 0)
    if not _word_at(stmt, pos, "else"):
        return None
    pos = _skip_ws(stmt, pos + 4)
    if _word_at(stmt, pos, "begin"):
        body, _ = _extract_begin_end_body(stmt, pos)
        return body
    return stmt[pos:].strip()


def _parse_ff_inner_piece(
    inner: str,
    adj: Dict[str, Set[str]],
    consts: Mapping[str, int],
    *,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    ff_net_lines: Optional[Dict[str, int]] = None,
) -> None:
    pmap = dict(param_map or {})
    pos = _skip_ws(inner, 0)
    case_kw = _case_keyword_at(inner, pos)
    if case_kw in ("case", "casez", "casex"):
        selector, body = _extract_case_selector(inner, pos)
        sel_key = re.sub(r"\s+", "", selector)
        m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", sel_key)
        if m and body:
            var = m.group(1)
            if var in consts:
                _parse_folded_ff_case_body(
                    body,
                    adj,
                    consts[var],
                    param_map=pmap,
                    case_mode=case_kw,
                )
                return
            if case_kw == "case":
                _parse_ff_case_union_all(body, adj, param_map=pmap)
                return
            if _selector_wildcard_assigned(module_body, var, pmap):
                _parse_ff_case_union_all(body, adj, param_map=pmap)
        return
    if_split = _split_if_else(inner)
    if if_split is not None:
        cond, then_part, else_part = if_split
        truth = _condition_is_const(cond, consts, pmap)
        if truth is True:
            if then_part:
                _parse_ff_inner_piece(
                    then_part,
                    adj,
                    consts,
                    param_map=pmap,
                    module_body=module_body,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                    ff_net_lines=ff_net_lines,
                )
        elif truth is False:
            if else_part:
                _parse_ff_inner_piece(
                    else_part,
                    adj,
                    consts,
                    param_map=pmap,
                    module_body=module_body,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                    ff_net_lines=ff_net_lines,
                )
        elif else_part:
            _parse_ff_inner_piece(
                else_part,
                adj,
                consts,
                param_map=pmap,
                module_body=module_body,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
                ff_net_lines=ff_net_lines,
            )
        elif then_part and _if_condition_has_relational(cond):
            _parse_ff_inner_piece(
                then_part,
                adj,
                consts,
                param_map=pmap,
                module_body=module_body,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
                ff_net_lines=ff_net_lines,
            )
        return
    _parse_ff_assign_piece(
        inner,
        adj,
        param_map=pmap,
        edge_prov=edge_prov,
        stmt_line=stmt_line,
        ff_net_lines=ff_net_lines,
    )


def _parse_ff_statement_list(
    inners: Sequence[str],
    adj: Dict[str, Set[str]],
    sel_consts: Mapping[str, int],
    *,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    ff_net_lines: Optional[Dict[str, int]] = None,
    ff_d_roots: Optional[Set[str]] = None,
    ff_q_roots: Optional[Set[str]] = None,
) -> None:
    pmap = dict(param_map or {})
    collect_endpoints = ff_d_roots is not None and ff_q_roots is not None
    last_nb: Dict[str, str] = {}
    deferred: List[str] = []
    for inner in inners:
        if_split = _split_if_else(inner)
        if if_split is not None:
            deferred.append(inner)
            continue
        pos = _skip_ws(inner, 0)
        if _case_keyword_at(inner, pos) in ("case", "casez", "casex"):
            deferred.append(inner)
            continue
        lhs_nodes = _nb_assign_lhs_nodes(inner, param_map=pmap)
        if lhs_nodes:
            for lhs in lhs_nodes:
                last_nb[lhs] = inner
            continue
        deferred.append(inner)
    for inner in dict.fromkeys(last_nb.values()):
        if collect_endpoints:
            _collect_ff_endpoints_piece(
                inner, ff_d_roots, ff_q_roots, param_map=pmap
            )
        _parse_ff_assign_piece(
            inner,
            adj,
            param_map=pmap,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
            ff_net_lines=ff_net_lines,
        )
    for inner in deferred:
        if collect_endpoints:
            _collect_ff_endpoints_deferred(
                inner,
                ff_d_roots,
                ff_q_roots,
                param_map=pmap,
                module_body=module_body,
            )
        _parse_ff_inner_piece(
            inner,
            adj,
            sel_consts,
            param_map=pmap,
            module_body=module_body,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
            ff_net_lines=ff_net_lines,
        )


def _parse_ff_stmt(
    stmt: str,
    adj: Dict[str, Set[str]],
    *,
    consts: Mapping[str, int] | None = None,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    stmt_line: int = 0,
    ff_net_lines: Optional[Dict[str, int]] = None,
    ff_d_roots: Optional[Set[str]] = None,
    ff_q_roots: Optional[Set[str]] = None,
) -> None:
    if not _is_sequential_statement(stmt):
        return
    sel_consts = dict(consts or {})
    pmap = dict(param_map or {})
    inners = _sequential_inner_statements(stmt)
    batch: List[str] = []
    for inner in inners:
        else_body = _unwrap_else_begin(inner)
        if else_body:
            if batch:
                _parse_ff_statement_list(
                    batch,
                    adj,
                    sel_consts,
                    param_map=pmap,
                    module_body=module_body,
                    edge_prov=edge_prov,
                    stmt_line=stmt_line,
                    ff_net_lines=ff_net_lines,
                    ff_d_roots=ff_d_roots,
                    ff_q_roots=ff_q_roots,
                )
                batch = []
            _parse_ff_statement_list(
                split_statements(else_body),
                adj,
                sel_consts,
                param_map=pmap,
                module_body=module_body,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
                ff_net_lines=ff_net_lines,
                ff_d_roots=ff_d_roots,
                ff_q_roots=ff_q_roots,
            )
            continue
        batch.append(inner)
    if batch:
        _parse_ff_statement_list(
            batch,
            adj,
            sel_consts,
            param_map=pmap,
            module_body=module_body,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
            ff_net_lines=ff_net_lines,
            ff_d_roots=ff_d_roots,
            ff_q_roots=ff_q_roots,
        )


def _collect_supply_net_values(body: str) -> Dict[str, int]:
    """Map ``supply0``/``supply1`` net names to 0/1."""
    driven: Dict[str, int] = {}
    for stmt in split_statements(_clean_body(body)):
        for m in re.finditer(
            r"\bsupply([01])\s+((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
            stmt,
            re.IGNORECASE,
        ):
            driven[m.group(2)] = int(m.group(1))
    return driven


def _collect_const_driven_lhs(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Set[str]:
    """Nets with at least one constant-only ``assign`` driver."""
    pmap = dict(param_map or {})
    driven: Set[str] = set()
    for stmt in _iter_connect_statements(body):
        if not _stmt_starts_with(stmt, "assign"):
            continue
        pos = _skip_ws(stmt, 6)
        eq = _find_blocking_eq(stmt[pos:])
        if eq is None:
            continue
        lhs = extract_connect_nodes(stmt[pos : pos + eq], pmap)
        if len(lhs) != 1:
            continue
        rhs = stmt[pos + eq + 1 :]
        if _is_const_literal(rhs, pmap) or not _effective_assign_rhs_roots(rhs, pmap):
            driven.add(next(iter(lhs)))
    driven.update(name for name, val in _collect_supply_net_values(body).items() if val == 0)
    return driven


def _prune_const_driven_adjacency(
    adj: Dict[str, Set[str]],
    const_driven: Set[str],
) -> None:
    """Drop variable edges through nets that are also constant-tied."""
    for net in const_driven:
        peers = adj.pop(net, set())
        for peer in peers:
            adj[peer].discard(net)


def scan_assign_adjacency(
    body: str,
    *,
    hier_links: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    hier_ref_targets: Optional[Dict[Tuple[str, str], Set[str]]] = None,
    param_map: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
    decl_widths: Optional[Mapping[str, List[int]]] = None,
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    iface_insts: Optional[Set[str]] = None,
    braced_concat_bases: Optional[Set[str]] = None,
    skip_comb_always: bool = False,
    grep_only: bool = False,
) -> Dict[str, Set[str]]:
    consts, pmap = _collect_const_assigns_fixed(body, param_map=param_map)
    zero_nets = {
        name: val
        for name, val in _collect_supply_net_values(body).items()
        if val == 0
    }
    adj: Dict[str, Set[str]] = {}
    if decl_widths is not None and decl_md_suffixes is not None:
        widths = {name: list(bits) for name, bits in decl_widths.items()}
        md_suffixes = {name: list(sfx) for name, sfx in decl_md_suffixes.items()}
    else:
        widths = _collect_decl_bit_indices(body, pmap)
        for name, bits in (decl_widths or {}).items():
            widths.setdefault(name, list(bits))
        md_suffixes = _collect_decl_md_suffixes(body, pmap)
        for name, suffixes in (decl_md_suffixes or {}).items():
            md_suffixes.setdefault(name, list(suffixes))
    for stmt, stmt_line in _iter_connect_statements_with_lines(body):
        _parse_assign_stmt(
            stmt,
            adj,
            hier_links=hier_links,
            hier_ref_targets=hier_ref_targets,
            param_map=pmap,
            zero_nets=zero_nets,
            over_approximate_if=over_approximate_if,
            decl_widths=widths,
            decl_md_suffixes=md_suffixes,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
            iface_insts=iface_insts,
            braced_concat_bases=braced_concat_bases,
            grep_only=grep_only,
        )
        _parse_decl_alias_stmt(stmt, adj, param_map=pmap, zero_nets=zero_nets)
        _parse_primitive_gate_stmt(stmt, adj)
        if not skip_comb_always:
            _parse_comb_always_stmt(
                stmt,
                adj,
                consts=consts,
                param_map=pmap,
                zero_nets=zero_nets,
                module_body=body,
                over_approximate_if=over_approximate_if,
                edge_prov=edge_prov,
                stmt_line=stmt_line,
            )
    if not grep_only:
        const_driven = _collect_const_driven_lhs(body, param_map=pmap)
        if const_driven:
            _prune_const_driven_adjacency(adj, const_driven)
    _seed_assign_drive_bases_cache(body, adj.keys())
    return adj


def scan_ff_endpoint_sets(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Tuple[Set[str], Set[str]]:
    """Return (D-input roots, Q-output roots) from ``always_ff`` without D->Q edges."""
    consts, pmap = _collect_const_assigns_fixed(body, param_map=param_map)
    d_roots: Set[str] = set()
    q_roots: Set[str] = set()
    for stmt in _iter_connect_statements(body):
        if not _is_sequential_statement(stmt):
            continue
        inners = _sequential_inner_statements(stmt)
        batch: List[str] = []
        for inner in inners:
            else_body = _unwrap_else_begin(inner)
            if else_body:
                if batch:
                    _collect_ff_endpoints_batch(
                        batch,
                        d_roots,
                        q_roots,
                        param_map=pmap,
                        module_body=body,
                    )
                    batch = []
                _collect_ff_endpoints_batch(
                    split_statements(else_body),
                    d_roots,
                    q_roots,
                    param_map=pmap,
                    module_body=body,
                )
                continue
            batch.append(inner)
        if batch:
            _collect_ff_endpoints_batch(
                batch, d_roots, q_roots, param_map=pmap, module_body=body
            )
    return d_roots, q_roots


def _collect_ff_endpoints_batch(
    inners: Sequence[str],
    d_roots: Set[str],
    q_roots: Set[str],
    *,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
) -> None:
    pmap = dict(param_map or {})
    last_nb: Dict[str, str] = {}
    deferred: List[str] = []
    for inner in inners:
        if _split_if_else(inner) is not None:
            deferred.append(inner)
            continue
        pos = _skip_ws(inner, 0)
        if _case_keyword_at(inner, pos) in ("case", "casez", "casex"):
            deferred.append(inner)
            continue
        lhs_nodes = _nb_assign_lhs_nodes(inner, param_map=pmap)
        if lhs_nodes:
            for lhs in lhs_nodes:
                last_nb[lhs] = inner
            continue
        deferred.append(inner)
    for inner in dict.fromkeys(last_nb.values()):
        _collect_ff_endpoints_piece(inner, d_roots, q_roots, param_map=pmap)
    for inner in deferred:
        _collect_ff_endpoints_deferred(
            inner, d_roots, q_roots, param_map=pmap, module_body=module_body
        )


def _collect_ff_endpoints_piece(
    piece: str,
    d_roots: Set[str],
    q_roots: Set[str],
    *,
    param_map: Mapping[str, str] | None = None,
) -> None:
    piece = _strip_case_item_label(piece)
    pair = _nb_assign_from_stmt(piece, param_map=param_map)
    if not pair:
        return
    lhs, rhs = pair
    q_roots.update(lhs)
    d_roots.update(rhs)


def _collect_ff_endpoints_deferred(
    inner: str,
    d_roots: Set[str],
    q_roots: Set[str],
    *,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
) -> None:
    pmap = dict(param_map or {})
    consts = _collect_const_assigns(module_body, param_map=pmap)
    _collect_ff_inner_piece(
        inner,
        d_roots,
        q_roots,
        consts,
        param_map=pmap,
        module_body=module_body,
    )


def _collect_ff_inner_piece(
    inner: str,
    d_roots: Set[str],
    q_roots: Set[str],
    consts: Mapping[str, int],
    *,
    param_map: Mapping[str, str] | None = None,
    module_body: str = "",
) -> None:
    pmap = dict(param_map or {})
    pos = _skip_ws(inner, 0)
    case_kw = _case_keyword_at(inner, pos)
    if case_kw in ("case", "casez", "casex"):
        selector, body = _extract_case_selector(inner, pos)
        sel_key = re.sub(r"\s+", "", selector)
        m = re.match(r"^((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))", sel_key)
        if m and body:
            var = m.group(1)
            if var in consts:
                _collect_ff_folded_case_body(
                    body,
                    d_roots,
                    q_roots,
                    consts[var],
                    param_map=pmap,
                    case_mode=case_kw,
                )
                return
            if case_kw == "case":
                _collect_ff_case_union_all(body, d_roots, q_roots, param_map=pmap)
                return
            if _selector_wildcard_assigned(module_body, var, pmap):
                _collect_ff_case_union_all(body, d_roots, q_roots, param_map=pmap)
        return
    if_split = _split_if_else(inner)
    if if_split is not None:
        cond, then_part, else_part = if_split
        truth = _condition_is_const(cond, consts, pmap)
        if truth is True:
            if then_part:
                _collect_ff_inner_piece(
                    then_part,
                    d_roots,
                    q_roots,
                    consts,
                    param_map=pmap,
                    module_body=module_body,
                )
        elif truth is False:
            if else_part:
                _collect_ff_inner_piece(
                    else_part,
                    d_roots,
                    q_roots,
                    consts,
                    param_map=pmap,
                    module_body=module_body,
                )
        elif else_part:
            _collect_ff_inner_piece(
                else_part,
                d_roots,
                q_roots,
                consts,
                param_map=pmap,
                module_body=module_body,
            )
        elif then_part and _if_condition_has_relational(cond):
            _collect_ff_inner_piece(
                then_part,
                d_roots,
                q_roots,
                consts,
                param_map=pmap,
                module_body=module_body,
            )
        return
    _collect_ff_endpoints_piece(inner, d_roots, q_roots, param_map=pmap)


def _collect_ff_case_union_all(
    body: str,
    d_roots: Set[str],
    q_roots: Set[str],
    *,
    param_map: Mapping[str, str] | None = None,
) -> None:
    default_body = ""
    for item in split_statements(body):
        label, rest = _split_case_item_label(item)
        if label.lower() == "default":
            default_body = rest
            continue
        _collect_ff_endpoints_piece(rest, d_roots, q_roots, param_map=param_map)
    if default_body:
        _collect_ff_endpoints_piece(default_body, d_roots, q_roots, param_map=param_map)


def _collect_ff_folded_case_body(
    body: str,
    d_roots: Set[str],
    q_roots: Set[str],
    sel_val: int,
    *,
    param_map: Mapping[str, str] | None = None,
    case_mode: str = "case",
) -> None:
    tmp: Dict[str, Set[str]] = {}
    _parse_folded_ff_case_body(
        body, tmp, sel_val, param_map=param_map, case_mode=case_mode
    )
    for q, ds in tmp.items():
        q_roots.add(q)
        d_roots.update(ds)


def scan_ff_adjacency(
    body: str,
    *,
    ff_barrier: bool = False,
    param_map: Mapping[str, str] | None = None,
    edge_prov: Optional[Dict[Tuple[str, str], ConnectEdgeProv]] = None,
    ff_net_lines: Optional[Dict[str, int]] = None,
    ff_d_roots: Optional[Set[str]] = None,
    ff_q_roots: Optional[Set[str]] = None,
) -> Dict[str, Set[str]]:
    record_meta = edge_prov is not None or ff_net_lines is not None
    collect_endpoints = ff_d_roots is not None and ff_q_roots is not None
    if ff_barrier and not record_meta and not collect_endpoints:
        return {}
    consts, pmap = _collect_const_assigns_fixed(body, param_map=param_map)
    adj: Dict[str, Set[str]] = {}
    for stmt, stmt_line in _iter_connect_statements_with_lines(body):
        _parse_ff_stmt(
            stmt,
            adj,
            consts=consts,
            param_map=pmap,
            module_body=body,
            edge_prov=edge_prov,
            stmt_line=stmt_line,
            ff_net_lines=ff_net_lines,
            ff_d_roots=ff_d_roots if collect_endpoints else None,
            ff_q_roots=ff_q_roots if collect_endpoints else None,
        )
    if ff_barrier:
        return {}
    return adj


def _parse_port_list(text: str, start: int) -> Tuple[List[Tuple[str, str]], int]:
    ports: List[Tuple[str, str]] = []
    if start >= len(text) or text[start] != "(":
        return ports, start
    i = start + 1
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] == ")":
            return ports, i + 1
        if text[i] == ",":
            i += 1
            continue
        if text[i] != ".":
            i = _skip_balanced(text, i, "(", ")") if text[i] == "(" else i + 1
            continue
        i += 1
        port, i = _read_ident(text, i)
        if not port:
            continue
        while i < n and text[i].isspace():
            i += 1
        if i >= n or text[i] != "(":
            continue
        end = _skip_balanced(text, i, "(", ")")
        expr = text[i + 1 : end - 1].strip() if end > i + 1 else ""
        ports.append((port, expr))
        i = end
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == ",":
            i += 1
    return ports, i


def instance_port_maps(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
    inst_stmt_lines: Optional[Dict[str, int]] = None,
    cell_types: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Tuple[str, str]]]:
    pmap = dict(param_map or {})
    out: Dict[str, List[Tuple[str, str]]] = {}
    for stmt, stmt_line in _iter_connect_statements_with_lines(body):
        leaves = _flatten_connect_statements(stmt)
        if not leaves:
            leaves = [stmt]
        for leaf in leaves:
            if not _is_instance_statement(leaf):
                continue
            _parse_instance_stmts_comma_chain(
                leaf,
                out,
                pmap,
                inst_stmt_lines=inst_stmt_lines,
                stmt_line=stmt_line,
                cell_types=cell_types,
            )
    return out


def _array_bundle_port_expr(inst_leaf: str, port_expr: str) -> str:
    """
    ``cell arr[i][j] (... .p(net));`` connects each ``p`` to ``net[i][j]`` when *net*
    is an unindexed bundle identifier (e.g. ``leaf_out``), not a shared scalar.
    """
    expr = port_expr.strip()
    if not expr or "[" in expr:
        return expr
    lb = inst_leaf.find("[")
    if lb < 0:
        return expr
    if expr in ("clk", "rst_n", "probe_in"):
        return expr
    return expr + inst_leaf[lb:]


def _parse_instance_stmts_comma_chain(
    stmt: str,
    out: Dict[str, List[Tuple[str, str]]],
    pmap: Mapping[str, str],
    *,
    inst_stmt_lines: Optional[Dict[str, int]] = None,
    stmt_line: int = 0,
    cell_types: Optional[Dict[str, str]] = None,
) -> None:
    """``cell u0 (...), u1 (...);`` within one statement."""
    from hierwalk.inst_scan import _read_hier_inst_path, expand_inst_names

    pos = _skip_ws(stmt, 0)
    cell, pos = _read_ident(stmt, pos)
    if not cell or cell.lower() in _KEYWORDS:
        return
    pos = _skip_ws(stmt, pos)
    if pos < len(stmt) and stmt[pos] == "#":
        pos += 1
        pos = _skip_ws(stmt, pos)
        if pos < len(stmt) and stmt[pos] == "(":
            pos = _skip_balanced(stmt, pos, "(", ")")
        pos = _skip_ws(stmt, pos)

    while pos < len(stmt):
        inst, nxt = _read_hier_inst_path(stmt, pos)
        if not inst:
            break
        nxt = _skip_ws(stmt, nxt)
        dims = ""
        while nxt < len(stmt) and stmt[nxt] == "[":
            end = _skip_balanced(stmt, nxt, "[", "]")
            dims += stmt[nxt:end]
            nxt = end
            nxt = _skip_ws(stmt, nxt)
        if nxt >= len(stmt) or stmt[nxt] != "(":
            break
        ports, nxt = _parse_port_list(stmt, nxt)
        for leaf in expand_inst_names(inst, dims, pmap):
            if cell_types is not None:
                cell_types[leaf] = cell
            if ports:
                out[leaf] = [
                    (port, _array_bundle_port_expr(leaf, expr))
                    for port, expr in ports
                ]
            if inst_stmt_lines is not None and stmt_line > 0:
                inst_stmt_lines.setdefault(leaf, stmt_line)
        nxt = _skip_ws(stmt, nxt)
        if nxt < len(stmt) and stmt[nxt] == ",":
            pos = _skip_ws(stmt, nxt + 1)
            continue
        break


def _build_compressed_local(
    assign_adj: Mapping[str, Set[str]],
    ff_adj: Mapping[str, Set[str]],
) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
    uf = _UnionFind()
    for adj in (assign_adj, ff_adj):
        for node, peers in adj.items():
            uf.add(node)
            for peer in peers:
                uf.union(node, peer)
    rep_adj: Dict[str, Set[str]] = {}
    for adj in (assign_adj, ff_adj):
        for a, peers in adj.items():
            ra = uf.find(a)
            uf.add(ra)
            for b in peers:
                rb = uf.find(b)
                if ra != rb:
                    rep_adj.setdefault(ra, set()).add(rb)
                    rep_adj.setdefault(rb, set()).add(ra)
    net_rep: Dict[str, str] = {}
    for adj in (assign_adj, ff_adj):
        for node in adj:
            uf.add(node)
            net_rep[node] = uf.find(node)
            for peer in adj[node]:
                uf.add(peer)
                net_rep[peer] = uf.find(peer)
    return net_rep, rep_adj


def _build_reverse_port_index(
    inst_ports: Mapping[str, List[Tuple[str, str]]],
    expr_cache: Dict[str, FrozenSet[str]],
    net_rep: Mapping[str, str],
    *,
    param_map: Mapping[str, str] | None = None,
    decl_widths: Optional[Mapping[str, List[int]]] = None,
    decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
) -> Dict[str, List[Tuple[str, str]]]:
    pmap = dict(param_map or {})
    widths = dict(decl_widths or {})
    md = dict(decl_md_suffixes or {})
    out: Dict[str, List[Tuple[str, str]]] = {}

    def _add(rep: str, inst: str, port: str) -> None:
        out.setdefault(rep, []).append((inst, port))

    for inst, ports in inst_ports.items():
        for port, expr in ports:
            if _is_compound_port_map_expr(expr):
                _cached_expr_roots(expr, expr_cache, param_map=pmap)
                continue
            if _is_braced_concat_rhs(expr):
                text = re.sub(r"\s+", "", expr.strip())
                parts = _expand_concat_elements(text[1:-1])
                port_base = port.split("[", 1)[0]
                for i, part in enumerate(parts):
                    piece = part.strip()
                    if not piece:
                        continue
                    if _is_compound_port_map_expr(piece):
                        continue
                    for root in extract_connect_nodes(piece, pmap):
                        rep = net_rep.get(root, root)
                        if "[" in port:
                            _add(rep, inst, port)
                        else:
                            _add(rep, inst, f"{port_base}[{i}]")
                continue
            roots: Set[str] = set()
            for root in _cached_expr_roots(expr, expr_cache, param_map=pmap):
                roots.add(root)
                base = root.split("[", 1)[0]
                for sfx in _md_suffixes_for_token(root, pmap, md, widths) or ():
                    roots.add(base + sfx)
            for root in roots:
                rep = net_rep.get(root, root)
                _add(rep, inst, port)
                if "[" in root and "[" not in port:
                    _add(rep, inst, port.split("[", 1)[0] + root[root.index("[") :])

    for rep, pairs in list(out.items()):
        if "[" in rep:
            continue
        suffixes = md.get(rep)
        if suffixes:
            for sfx in suffixes:
                bit_key = f"{rep}{sfx}"
                bit_rep = net_rep.get(bit_key, bit_key)
                if bit_rep == rep:
                    continue
                for inst, port in list(pairs):
                    port_base = port.split("[", 1)[0]
                    bit_port = port if "[" in port else f"{port_base}{sfx}"
                    _add(bit_rep, inst, bit_port)
            continue
        bits = widths.get(rep)
        if not bits:
            continue
        for bit_i in bits:
            bit_key = f"{rep}[{bit_i}]"
            bit_rep = net_rep.get(bit_key, bit_key)
            if bit_rep == rep:
                continue
            for inst, port in list(pairs):
                port_base = port.split("[", 1)[0]
                bit_port = port if "[" in port else f"{port_base}[{bit_i}]"
                _add(bit_rep, inst, bit_port)
    return out


def apply_empty_module_passthrough(
    mod_idx: ModuleConnectIndex,
    input_port: str,
    output_port: str,
) -> None:
    """Connect a lone scalar input to output when the module has no body COI."""
    if mod_idx.rep_adj:
        return
    mod_idx.net_rep.setdefault(input_port, input_port)
    mod_idx.net_rep.setdefault(output_port, output_port)
    rep_in = mod_idx.net_rep[input_port]
    rep_out = mod_idx.net_rep[output_port]
    mod_idx.rep_adj.setdefault(rep_in, set()).add(rep_out)
    mod_idx.rep_adj.setdefault(rep_out, set()).add(rep_in)


def _apply_ifdef_only(body: str, defines: Mapping[str, str] | None) -> str:
    from hierwalk.preprocess import _preprocess_conditional_pass

    pmap = dict(defines or {})
    return _preprocess_conditional_pass(body, pmap, apply_ifdef=True)


_BIND_STMT_RE = re.compile(
    r"\bbind\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)


def _parse_bind_line(stmt: str) -> Optional[Tuple[str, BindRecord]]:
    m = _BIND_STMT_RE.search(stmt)
    if not m:
        return None
    target, cell, inst = m.group(1), m.group(2), m.group(3)
    pos = m.end()
    pos = _skip_ws(stmt, pos)
    if pos >= len(stmt) or stmt[pos] != "(":
        return None
    ports, _ = _parse_port_list(stmt, pos)
    if not ports:
        return None
    return target, BindRecord(cell=cell, inst_leaf=inst, ports=list(ports))


def scan_bind_records(text: str) -> Dict[str, List[BindRecord]]:
    """Collect ``bind`` instances keyed by target module name."""
    out: Dict[str, List[BindRecord]] = {}
    for stmt in split_statements(text):
        parsed = _parse_bind_line(stmt)
        if parsed is None:
            continue
        target, rec = parsed
        out.setdefault(target, []).append(rec)
    return out


def apply_bind_connectivity(
    mod_idx: ModuleConnectIndex,
    binds: Sequence[BindRecord],
    index: object,
    *,
    param_map: Mapping[str, str] | None = None,
    defines: Mapping[str, str] | None = None,
    over_approximate_if: bool = True,
) -> None:
    """Fold ``bind`` cells into parent COI (transparent bind-module port ties)."""
    pmap = dict(param_map or {})
    for bind in binds:
        get_module = getattr(index, "get_module", None)
        module_body = getattr(index, "module_body", None)
        if not callable(get_module) or not callable(module_body):
            continue
        rec = get_module(bind.cell)
        if not rec:
            continue
        cell_body = rec.body or module_body(bind.cell)
        cell_idx = build_module_connect_index(
            cell_body,
            param_map=pmap,
            defines=defines,
            fold_generate=True,
            over_approximate_if=over_approximate_if,
            source_file=rec.file_path or None,
            include_dirs=list(
                getattr(index, "_preprocess_include_dirs", ()) or ()
            ),
        )
        port_to_expr = {p: e for p, e in bind.ports}
        groups: Dict[str, Set[str]] = {}
        for port in port_to_expr:
            cre = net_representative(cell_idx, port)
            groups.setdefault(cre, set()).add(port)
        for group_ports in groups.values():
            parent_reps: Set[str] = set()
            hier_pairs: List[Tuple[str, str]] = []
            for port in group_ports:
                expr = port_to_expr.get(port, "")
                hier_pairs.extend(extract_hier_refs(expr))
                for root in extract_connect_nodes(expr):
                    parent_reps.add(mod_idx.net_rep.get(root, root))
            rep_list = sorted(parent_reps)
            for i, a in enumerate(rep_list):
                for b in rep_list[i + 1 :]:
                    mod_idx.rep_adj.setdefault(a, set()).add(b)
                    mod_idx.rep_adj.setdefault(b, set()).add(a)
            for rep in parent_reps:
                for inst, port in hier_pairs:
                    mod_idx.hier_links.setdefault(rep, []).append((inst, port))
                    mod_idx.hier_ref_targets.setdefault((inst, port), set()).add(rep)


def _dedupe_paths_preserve_order(paths: Sequence[str]) -> List[str]:
    from pathlib import Path

    seen: Set[str] = set()
    out: List[str] = []
    for raw in paths:
        key = str(Path(raw).resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def design_parse_sources(index: object) -> List[str]:
    """RTL source paths for cross-file define collection (filelist order when known)."""
    stored = getattr(index, "_parse_sources", None)
    if stored:
        return list(stored)
    modules = getattr(index, "modules", None)
    if isinstance(modules, Mapping):
        seen: Set[str] = set()
        out: List[str] = []
        for rec in modules.values():
            fp = (getattr(rec, "file_path", "") or "").strip()
            if not fp:
                continue
            key = str(Path(fp).resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        if out:
            return out
    file_modules = getattr(index, "file_modules", None)
    if isinstance(file_modules, Mapping):
        return list(file_modules)
    return []


def collect_design_defines(
    index: object,
    *,
    sources: Optional[Sequence[str]] = None,
    extra_defines: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """
    Active-branch `` `define `` / `` `undef `` across RTL (ifdef order, includes).

    Filelist/extra defines seed the map; each RTL file may add or `` `undef `` names.
    The result is final — callers must not re-merge filelist on top (undef would be lost).
    """
    from pathlib import Path

    base = dict(getattr(index, "_preprocess_defines", {}) or {})
    if extra_defines:
        base.update(extra_defines)
    if sources is not None:
        paths = _dedupe_paths_preserve_order(sources)
    else:
        paths = design_parse_sources(index)
    if not paths:
        return dict(base)

    from hierwalk.preprocess import accumulate_defines_from_file, include_guard_macro_names

    inc = [Path(p) for p in getattr(index, "_preprocess_include_dirs", ()) or ()]
    skip = tuple(
        getattr(index, "ignore_path_patterns", None)
        or getattr(index, "_skip_path_patterns", ())
        or ()
    )
    out: Dict[str, str] = dict(base)
    seen: Set[str] = set()
    for fpath in paths:
        if fpath in seen:
            continue
        seen.add(fpath)
        path = Path(fpath)
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            raw = ""
        guards = include_guard_macro_names(raw)
        accumulate_defines_from_file(
            path,
            out,
            inc,
            set(),
            skip_path_patterns=skip,
            apply_ifdef=True,
        )
        for name in guards:
            out.pop(name, None)
    return dict(out)


_bind_records_memo: Dict[Tuple[int, str, str], Tuple[BindRecord, ...]] = {}
_design_bind_index: Dict[int, Tuple[str, Dict[str, Tuple[BindRecord, ...]]]] = {}
_bind_records_memo_guard = threading.Lock()


def _design_source_paths_fallback(index: object) -> List[str]:
    """File paths when ``_parse_sources`` is empty (module first-seen order)."""
    paths = design_parse_sources(index)
    if paths:
        return paths
    file_modules = getattr(index, "file_modules", None)
    if isinstance(file_modules, Mapping):
        return list(file_modules)
    return []


def _paths_content_digest(paths: Sequence[str]) -> str:
    if not paths:
        return "0" * 16
    from pathlib import Path

    from hierwalk.manifest import path_content_digest

    hasher = hashlib.sha256()
    for fpath in paths:
        digest = path_content_digest(Path(fpath)) or ""
        if not digest:
            try:
                digest = hashlib.sha256(
                    Path(fpath).read_bytes()
                ).hexdigest()[:16]
            except OSError:
                digest = "missing"
        hasher.update(fpath.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def sources_content_digest(paths: Sequence[str]) -> str:
    """Content digest for an explicit RTL source list (define-cache stamp)."""
    return _paths_content_digest(paths)


def file_modules_bind_digest(index: object) -> str:
    """Digest of RTL files in *index* for bind-memo invalidation."""
    paths = _design_source_paths_fallback(index)
    digest = _paths_content_digest(paths)
    paths_stamp = tuple(paths)
    cached_stamp = getattr(index, "_bind_digest_paths_stamp", None)
    cached_digest = getattr(index, "_bind_digest_value", None)
    if cached_stamp == paths_stamp and cached_digest == digest:
        return digest
    try:
        index._bind_digest_paths_stamp = paths_stamp
        index._bind_digest_value = digest
    except (AttributeError, TypeError):
        pass
    return digest


def clear_bind_records_memo() -> None:
    """Drop per-design bind scan memo (for tests)."""
    with _bind_records_memo_guard:
        _bind_records_memo.clear()
        _design_bind_index.clear()


_BIND_SOURCE_PROBE_RE = re.compile(r"^\s*bind\s+", re.IGNORECASE | re.MULTILINE)


def _raw_source_might_contain_bind(raw: str) -> bool:
    return bool(_BIND_SOURCE_PROBE_RE.search(raw))


def _design_sources_might_contain_bind(paths: Sequence[str]) -> bool:
    from pathlib import Path

    for fpath in paths:
        path = Path(fpath)
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _raw_source_might_contain_bind(raw):
            return True
    return False


def _scan_design_bind_index(
    index: object,
    paths: Sequence[str],
) -> Dict[str, Tuple[BindRecord, ...]]:
    if not paths:
        return {}
    if not _design_sources_might_contain_bind(paths):
        return {}
    from pathlib import Path

    from hierwalk.preprocess import include_guard_macro_names, preprocess_file_for_index

    inc = [Path(p) for p in getattr(index, "_preprocess_include_dirs", ()) or ()]
    skip = tuple(
        getattr(index, "ignore_path_patterns", None)
        or getattr(index, "_skip_path_patterns", ())
        or ()
    )
    running: Dict[str, str] = dict(getattr(index, "_preprocess_defines", {}) or {})
    by_target: Dict[str, List[BindRecord]] = {}
    for fpath in paths:
        path = Path(fpath)
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not _raw_source_might_contain_bind(raw):
            continue
        guards = include_guard_macro_names(raw)
        defs = dict(running)
        text = preprocess_file_for_index(
            path,
            inc,
            defs,
            set(),
            skip_path_patterns=skip,
            apply_ifdef=True,
        )
        for name in guards:
            defs.pop(name, None)
        running = defs
        for target, records in scan_bind_records(text).items():
            by_target.setdefault(target, []).extend(records)
    return {name: tuple(recs) for name, recs in by_target.items()}


def _design_bind_lookup(index: object) -> Dict[str, Tuple[BindRecord, ...]]:
    files_digest = file_modules_bind_digest(index)
    index_id = id(index)
    with _bind_records_memo_guard:
        entry = _design_bind_index.get(index_id)
        if entry is not None and entry[0] == files_digest:
            return entry[1]
    paths = _design_source_paths_fallback(index)
    if not _design_sources_might_contain_bind(paths):
        scanned: Dict[str, Tuple[BindRecord, ...]] = {}
    else:
        scanned = _scan_design_bind_index(index, paths)
    with _bind_records_memo_guard:
        entry = _design_bind_index.get(index_id)
        if entry is not None and entry[0] == files_digest:
            return entry[1]
        _design_bind_index[index_id] = (files_digest, scanned)
    return scanned


def collect_bind_records_for_module(index: object, mod_name: str) -> List[BindRecord]:
    """Scan RTL sources for ``bind`` statements targeting *mod_name*."""
    files_digest = file_modules_bind_digest(index)
    memo_key = (id(index), mod_name, files_digest)
    with _bind_records_memo_guard:
        hit = _bind_records_memo.get(memo_key)
        if hit is not None:
            return list(hit)
    by_target = _design_bind_lookup(index)
    frozen = by_target.get(mod_name, ())
    with _bind_records_memo_guard:
        _bind_records_memo[memo_key] = frozen
    return list(frozen)


def _interface_mod_names_in_body(body: str) -> Set[str]:
    return {
        m.group(1)
        for m in re.finditer(
            r"\binterface\s+((?:\\(?:[A-Za-z_]\w*|\S+)|[A-Za-z_]\w*))",
            body,
        )
    }


def instance_cell_types(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
) -> Dict[str, str]:
    """Instance leaf -> cell module type (reuses instance statement scan)."""
    pmap = dict(param_map or {})
    out: Dict[str, str] = {}
    for stmt, _line in _iter_connect_statements_with_lines(body):
        leaves = _flatten_connect_statements(stmt)
        if not leaves:
            leaves = [stmt]
        for leaf in leaves:
            if not _is_instance_statement(leaf):
                continue
            _parse_instance_stmts_comma_chain(
                leaf,
                {},
                pmap,
                cell_types=out,
            )
    return out


def _interface_inst_names_from_scan(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
    cell_types: Optional[Mapping[str, str]] = None,
) -> Set[str]:
    """Interface instances via instance-scan cell types (no extra body regex)."""
    iface_mods = _interface_mod_names_in_body(body)
    types = dict(cell_types or instance_cell_types(body, param_map=param_map))
    return {
        inst
        for inst, cell in types.items()
        if cell in iface_mods or cell.endswith("_if")
    }


_BUILD_INDEX_MEMO_VERSION = 1
_build_index_mem_cache: Dict[Tuple[object, ...], ModuleConnectIndex] = {}
_build_index_cache_key_locks: Dict[Tuple[object, ...], threading.Lock] = {}
_build_index_cache_key_locks_guard = threading.Lock()
_build_index_uncached_calls = 0
_build_index_mem_hits = 0


def _build_index_mem_cache_lock(key: Tuple[object, ...]) -> threading.Lock:
    with _build_index_cache_key_locks_guard:
        lock = _build_index_cache_key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_index_cache_key_locks[key] = lock
        return lock


def _defines_digest(defines: Mapping[str, str] | None) -> str:
    hasher = hashlib.sha256()
    for key in sorted(defines or {}):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(defines[key]).encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def _param_map_digest(param_map: Mapping[str, str] | None) -> str:
    hasher = hashlib.sha256()
    for key in sorted(param_map or {}):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(param_map[key]).encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def _port_decl_maps_digest(
    widths: Optional[Mapping[str, List[int]]],
    suffixes: Optional[Mapping[str, List[str]]],
) -> str:
    hasher = hashlib.sha256()
    for name in sorted(widths or {}):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(",".join(str(b) for b in widths[name]).encode("utf-8"))
        hasher.update(b"\0")
    for name in sorted(suffixes or {}):
        hasher.update(b"sfx:")
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(",".join(suffixes[name]).encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def _build_index_cache_key(
    body: str,
    *,
    param_map: Mapping[str, str] | None,
    defines: Mapping[str, str] | None,
    fold_generate: bool,
    over_approximate_if: bool,
    ff_barrier: bool,
    resolve_param_dims: bool,
    port_decl_widths: Optional[Mapping[str, List[int]]],
    port_decl_md_suffixes: Optional[Mapping[str, List[str]]],
    prepared_body: Optional[str],
    source_file: str | None = None,
) -> Tuple[object, ...]:
    body_digest = hashlib.sha256(
        body.encode("utf-8", errors="surrogateescape")
    ).hexdigest()[:16]
    prepared_digest = ""
    if prepared_body is not None:
        prepared_digest = hashlib.sha256(
            prepared_body.encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:16]
    source_digest = ""
    if source_file:
        source_digest = hashlib.sha256(
            str(Path(source_file).resolve()).encode("utf-8")
        ).hexdigest()[:16]
    return (
        _BUILD_INDEX_MEMO_VERSION,
        body_digest,
        prepared_digest,
        source_digest,
        _param_map_digest(param_map),
        _defines_digest(defines),
        fold_generate,
        over_approximate_if,
        ff_barrier,
        resolve_param_dims,
        _port_decl_maps_digest(port_decl_widths, port_decl_md_suffixes),
    )


def clear_module_connect_index_mem_cache() -> None:
    """Drop in-process memo entries without resetting build/hit counters."""
    _build_index_mem_cache.clear()
    with _assign_probe_miss_cache_guard:
        _assign_probe_miss_cache.clear()
    with _large_module_probe_cache_guard:
        _assign_drive_bases_cache.clear()
        _port_map_bases_cache.clear()


def clear_module_connect_index_cache() -> None:
    """Reset in-process build_module_connect_index memo (for tests)."""
    global _build_index_uncached_calls, _build_index_mem_hits
    clear_module_connect_index_mem_cache()
    clear_bind_records_memo()
    from hierwalk.connect.shared.endpoints import (
        _clear_module_index_key_memo,
        clear_module_connect_sidecar_cache,
    )

    _clear_module_index_key_memo()
    clear_module_connect_sidecar_cache()
    _build_index_uncached_calls = 0
    _build_index_mem_hits = 0


def module_connect_index_stats() -> Tuple[int, int]:
    """Return (uncached_build_calls, mem_cache_hits) since last clear."""
    return _build_index_uncached_calls, _build_index_mem_hits


def _scalar_bases_from_inst_ports(
    inst_ports: Mapping[str, List[Tuple[str, str]]],
    param_map: Mapping[str, str],
) -> Set[str]:
    """Whole-net instance port maps imply a scalar bus hookup (not slice-only)."""
    cache: Dict[str, FrozenSet[str]] = {}
    scalars: Set[str] = set()
    for _inst, ports in inst_ports.items():
        for _port, expr in ports:
            if _is_compound_port_map_expr(expr) or _is_braced_concat_rhs(expr):
                continue
            for root in _cached_expr_roots(expr, cache, param_map=param_map):
                base = root.split("[", 1)[0]
                if root == base:
                    scalars.add(base)
    return scalars


def _compute_bit_precise_bases(
    assign_adj: Mapping[str, Set[str]],
    *,
    extra: Optional[Set[str]] = None,
    scalar_bases_extra: Optional[Set[str]] = None,
) -> FrozenSet[str]:
    """Bases wired only via literal single-bit slices (skip bloom base promotion)."""
    slice_bases: Set[str] = set()
    scalar_bases: Set[str] = set(scalar_bases_extra or ())
    for key in assign_adj:
        if "[" not in key:
            scalar_bases.add(key)
            continue
        base, suffix = _split_net_base_suffix(key)
        if base and _is_literal_slice_suffix(suffix):
            slice_bases.add(base)
    out = slice_bases - scalar_bases
    if extra:
        out |= extra
    return frozenset(out)


def _promote_slice_edges_to_bases(
    adj: Dict[str, Set[str]],
    *,
    skip_bases: Optional[Set[str]] = None,
) -> None:
    """Text-conn bloom filter: any slice edge also links the participating base nets."""
    skip = skip_bases or set()
    extra: List[Tuple[str, str]] = []
    for a, peers in adj.items():
        a_base, _ = _split_net_base_suffix(a)
        if a_base in skip:
            continue
        for b in peers:
            b_base, _ = _split_net_base_suffix(b)
            if b_base in skip:
                continue
            if a_base != a or b_base != b:
                extra.append((a_base, b))
                extra.append((a, b_base))
                if a_base != b_base:
                    extra.append((a_base, b_base))
    for a, b in extra:
        _add_undirected(adj, a, b)


def build_module_connect_index(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
    defines: Mapping[str, str] | None = None,
    fold_generate: bool = True,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    resolve_param_dims: bool = True,
    port_decl_widths: Optional[Mapping[str, List[int]]] = None,
    port_decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    prepared_body: Optional[str] = None,
    source_file: str | None = None,
    include_dirs: Sequence[str] | None = None,
) -> ModuleConnectIndex:
    if os.environ.get("HIERWALK_CONNECT_INDEX_MEMO", "").lower() in (
        "0",
        "off",
        "false",
    ):
        return _build_module_connect_index_uncached(
            body,
            param_map=param_map,
            defines=defines,
            fold_generate=fold_generate,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            resolve_param_dims=resolve_param_dims,
            port_decl_widths=port_decl_widths,
            port_decl_md_suffixes=port_decl_md_suffixes,
            prepared_body=prepared_body,
            source_file=source_file,
            include_dirs=include_dirs,
        )
    key = _build_index_cache_key(
        body,
        param_map=param_map,
        defines=defines,
        fold_generate=fold_generate,
        over_approximate_if=over_approximate_if,
        ff_barrier=ff_barrier,
        resolve_param_dims=resolve_param_dims,
        port_decl_widths=port_decl_widths,
        port_decl_md_suffixes=port_decl_md_suffixes,
        prepared_body=prepared_body,
        source_file=source_file,
    )
    with _build_index_mem_cache_lock(key):
        hit = _build_index_mem_cache.get(key)
        if hit is not None:
            global _build_index_mem_hits
            _build_index_mem_hits += 1
            return hit
        built = _build_module_connect_index_uncached(
            body,
            param_map=param_map,
            defines=defines,
            fold_generate=fold_generate,
            over_approximate_if=over_approximate_if,
            ff_barrier=ff_barrier,
            resolve_param_dims=resolve_param_dims,
            port_decl_widths=port_decl_widths,
            port_decl_md_suffixes=port_decl_md_suffixes,
            prepared_body=prepared_body,
            source_file=source_file,
            include_dirs=include_dirs,
        )
        _build_index_mem_cache[key] = built
        return built


def _build_module_connect_index_uncached(
    body: str,
    *,
    param_map: Mapping[str, str] | None = None,
    defines: Mapping[str, str] | None = None,
    fold_generate: bool = True,
    over_approximate_if: bool = True,
    ff_barrier: bool = False,
    resolve_param_dims: bool = True,
    port_decl_widths: Optional[Mapping[str, List[int]]] = None,
    port_decl_md_suffixes: Optional[Mapping[str, List[str]]] = None,
    prepared_body: Optional[str] = None,
    source_file: str | None = None,
    include_dirs: Sequence[str] | None = None,
) -> ModuleConnectIndex:
    global _build_index_uncached_calls
    _build_index_uncached_calls += 1
    pmap = dict(param_map or {})
    if prepared_body is not None:
        text = prepared_body
    elif fold_generate:
        text = prepare_connect_body(
            body,
            param_map=pmap,
            defines=defines,
            over_approximate_if=over_approximate_if,
            source_file=source_file,
            include_dirs=include_dirs,
        )
    else:
        text = _apply_ifdef_only(body, defines)
    body_params = collect_connect_module_params("", text)
    full_pmap = resolve_param_map(body_params, parent=pmap, overrides=pmap)
    hier_links: Dict[str, List[Tuple[str, str]]] = {}
    hier_ref_targets: Dict[Tuple[str, str], Set[str]] = {}
    braced_concat_bases: Set[str] = set()
    if resolve_param_dims:
        decl_widths = _collect_decl_bit_indices(text, full_pmap)
        for name, bits in (port_decl_widths or {}).items():
            decl_widths.setdefault(name, list(bits))
    else:
        decl_widths = _collect_literal_decl_bit_indices(text)
    decl_md_suffixes = _collect_decl_md_suffixes(
        text,
        full_pmap,
        resolve_param_dims=resolve_param_dims,
    )
    for name, suffixes in (port_decl_md_suffixes or {}).items():
        decl_md_suffixes.setdefault(name, list(suffixes))
    raw_edge_prov: Dict[Tuple[str, str], ConnectEdgeProv] = {}
    inst_stmt_lines: Dict[str, int] = {}
    cell_types: Dict[str, str] = {}
    inst_ports = instance_port_maps(
        text,
        param_map=full_pmap,
        inst_stmt_lines=inst_stmt_lines,
        cell_types=cell_types,
    )
    iface_insts = _interface_inst_names_from_scan(
        text,
        param_map=full_pmap,
        cell_types=cell_types,
    )
    text_conn_lite = not resolve_param_dims
    assign_adj = scan_assign_adjacency(
        text,
        hier_links=hier_links,
        hier_ref_targets=hier_ref_targets,
        param_map=full_pmap,
        over_approximate_if=over_approximate_if,
        decl_widths=decl_widths,
        decl_md_suffixes=decl_md_suffixes,
        edge_prov=raw_edge_prov,
        iface_insts=iface_insts,
        braced_concat_bases=braced_concat_bases,
        skip_comb_always=text_conn_lite,
        grep_only=text_conn_lite,
    )
    bit_precise_bases: FrozenSet[str] = frozenset()
    if not resolve_param_dims:
        # Text-conn: coarse grep bloom only — never mark bases bit-precise.
        _promote_slice_edges_to_bases(assign_adj)
    _expand_hier_bit_links(
        hier_links,
        hier_ref_targets,
        decl_widths=decl_widths,
        decl_md_suffixes=decl_md_suffixes,
        adj=assign_adj,
        iface_insts=iface_insts,
        param_map=full_pmap,
        edge_prov=raw_edge_prov,
    )
    port_bases = {
        port.split("[", 1)[0]
        for pairs in inst_ports.values()
        for _, port in pairs
        if port
    }
    port_bases.update(port_decl_widths or {})
    vector_bases_set = frozenset(set(decl_widths) | set(decl_md_suffixes))
    assign_adj_bases: Set[str] = set()
    assign_adj_scalar: Set[str] = set()
    bits_by_base: Dict[str, List[str]] = {}
    scalar_bases: Set[str] = set(vector_bases_set)
    for key in assign_adj:
        assign_adj_bases.add(key)
        if "[" in key:
            base = key.split("[", 1)[0]
            assign_adj_bases.add(base)
            scalar_bases.add(base)
            bits_by_base.setdefault(base, []).append(key)
        else:
            assign_adj_scalar.add(key)
    # MD suffix links are gated (not a full decl_widths clique). Wide vectors
    # rely on vector_bases + net_representative collapse after compression.
    for base, suffixes in decl_md_suffixes.items():
        if not suffixes:
            continue
        if base not in assign_adj_bases:
            continue
        if base not in assign_adj_scalar:
            # Bit-sliced assigns only (e.g. ``{a,b,c}`` concat): never clique the
            # undeclared scalar base onto every slice — that collapses unrelated bits.
            if len(suffixes) <= 2 and base in port_bases:
                for sfx in suffixes:
                    _add_undirected(
                        assign_adj,
                        base,
                        f"{base}{sfx}",
                        edge_prov=raw_edge_prov,
                    )
            continue
        for sfx in suffixes:
            _add_undirected(
                assign_adj,
                base,
                f"{base}{sfx}",
                edge_prov=raw_edge_prov,
            )
    ff_net_lines: Dict[str, int] = {}
    ff_d_raw: Set[str] = set()
    ff_q_raw: Set[str] = set()
    ff_adj = scan_ff_adjacency(
        text,
        ff_barrier=ff_barrier,
        param_map=full_pmap,
        edge_prov=raw_edge_prov if not text_conn_lite else None,
        ff_net_lines=ff_net_lines if not text_conn_lite else None,
        ff_d_roots=ff_d_raw if not text_conn_lite else None,
        ff_q_roots=ff_q_raw if not text_conn_lite else None,
    )

    expr_cache_seed: Dict[str, FrozenSet[str]] = {}
    _seed_adj_from_instance_ports(
        assign_adj,
        inst_ports,
        expr_cache_seed,
        param_map=full_pmap,
    )
    for name in _collect_declared_net_names(text):
        _ensure_adj_node(assign_adj, name)
    net_rep, rep_adj = _build_compressed_local(assign_adj, ff_adj)
    expr_cache: Dict[str, FrozenSet[str]] = {}
    net_to_children = _build_reverse_port_index(
        inst_ports,
        expr_cache,
        net_rep,
        param_map=full_pmap,
        decl_widths=decl_widths,
        decl_md_suffixes=decl_md_suffixes,
    )
    rep_hier_links: Dict[str, List[Tuple[str, str]]] = {}
    for net, pairs in hier_links.items():
        rep = net_rep.get(net, net)
        rep_hier_links.setdefault(rep, []).extend(pairs)
    rep_hier_targets: Dict[Tuple[str, str], Set[str]] = {}
    for (inst, port), nets in hier_ref_targets.items():
        for net in nets:
            rep = net_rep.get(net, net)
            rep_hier_targets.setdefault((inst, port), set()).add(rep)
    rep_edge_prov: Dict[Tuple[str, str], ConnectEdgeProv] = {}
    for (a, b), prov in raw_edge_prov.items():
        ra = net_rep.get(a, a)
        rb = net_rep.get(b, b)
        key = _edge_prov_key(ra, rb)
        rep_edge_prov.setdefault(key, prov)
    rep_ff_net_lines: Dict[str, int] = {}
    for net, line in ff_net_lines.items():
        rep = net_rep.get(net, net)
        rep_ff_net_lines.setdefault(rep, line)
    vector_scalar_rep: Dict[str, str] = {}
    for base in scalar_bases:
        bit_keys = bits_by_base.get(base, ())
        if not bit_keys:
            continue
        reps = {net_rep[k] for k in bit_keys}
        if len(reps) == 1 and (
            f"{base}[0]" in net_rep or base in vector_bases_set
        ):
            vector_scalar_rep[base] = next(iter(reps))
    return ModuleConnectIndex(
        inst_ports=dict(inst_ports),
        net_rep=net_rep,
        rep_adj=rep_adj,
        net_to_children=net_to_children,
        expr_roots=expr_cache,
        hier_links=rep_hier_links,
        hier_ref_targets=rep_hier_targets,
        edge_prov=rep_edge_prov,
        inst_stmt_lines=dict(inst_stmt_lines),
        ff_net_lines=rep_ff_net_lines,
        ff_d_roots=frozenset(ff_d_raw),
        ff_q_roots=frozenset(ff_q_raw),
        vector_bases=vector_bases_set,
        vector_scalar_rep=vector_scalar_rep,
        bit_precise_bases=bit_precise_bases,
        resolve_param_dims=resolve_param_dims,
    )


def _coarse_net_representative(mod_idx: ModuleConnectIndex, net: str) -> str:
    """Text-conn grep pass: collapse slice selects to the base bus rep."""
    base, _suffix = _split_net_base_suffix(net)
    if not base or base == net:
        return net
    base_hit = mod_idx.net_rep.get(base)
    if base_hit is not None:
        return base_hit
    cached = mod_idx.vector_scalar_rep.get(base)
    if cached is not None:
        return cached
    if base in mod_idx.vector_bases:
        return mod_idx.net_rep.get(base, base)
    return mod_idx.net_rep.get(base, base)


def net_representative(mod_idx: ModuleConnectIndex, net: str) -> str:
    hit = mod_idx.net_rep.get(net)
    if hit is not None:
        return hit
    if "[" not in net:
        cached = mod_idx.vector_scalar_rep.get(net)
        if cached is not None:
            return cached
        return net
    if not mod_idx.resolve_param_dims:
        return _coarse_net_representative(mod_idx, net)
    return net