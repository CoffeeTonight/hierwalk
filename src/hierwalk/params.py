"""Parameter / localparam parsing and constant folding for generate."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple


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

_PARAM_PAIR_RE = re.compile(
    r"(?:\b(?:parameter|localparam)\b\s+)?(?:\w+\s+)?"
    r"([A-Za-z_]\w*)\s*=",
    re.IGNORECASE,
)
_OVERRIDE_PAIR_RE = re.compile(
    r"\.?\s*([A-Za-z_]\w*)\s*\(\s*([^)]+)\s*\)",
    re.IGNORECASE,
)
_BODY_PARAM_STMT_RE = re.compile(
    r"(?:\b(?:parameter|localparam)\b\s+(?:\w+\s+)?[^;]+;)",
    re.IGNORECASE,
)
_EXPR_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_PARAM_KEYWORDS = frozenset(
    {
        "parameter", "localparam", "module", "endmodule", "generate", "endgenerate",
        "if", "else", "for", "while", "begin", "end", "genvar", "assign", "wire",
        "logic", "reg", "input", "output", "inout", "always", "initial", "function",
        "task", "typedef", "and", "or", "not", "signed", "unsigned", "integer",
    }
)


def _scan_param_value(text: str, start: int) -> Tuple[Optional[str], int]:
    i = start
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n:
        return None, i
    depth = 0
    val_start = i
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch in ",;" and depth == 0:
            return text[val_start:i].strip(), i
        i += 1
    return text[val_start:i].strip(), i


def _find_top_level_op(expr: str, op: str) -> Optional[int]:
    depth = 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and ch == op:
            return i
        i += 1
    return None


def parse_param_pairs(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _PARAM_PAIR_RE.finditer(text):
        val, _ = _scan_param_value(text, m.end())
        if val:
            out[m.group(1)] = val
    return out


def parse_param_overrides(text: str) -> Dict[str, str]:
    """Parse ``#(.N(P), x(1+2*(1+TWO)+1))`` with balanced parentheses."""
    out: Dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if text[i] == ".":
            i += 1
        m = re.match(r"([A-Za-z_]\w*)", text[i:])
        if not m:
            i += 1
            continue
        name = m.group(1)
        i += m.end()
        while i < n and text[i].isspace():
            i += 1
        if i >= n or text[i] != "(":
            continue
        end = _skip_balanced(text, i, "(", ")")
        if end <= i + 1:
            i = end
            continue
        out[name] = text[i + 1 : end - 1].strip()
        i = end
    return out


def split_module_header(chunk: str) -> Tuple[str, str]:
    """Return (header_param_text, module_body) from text after module name."""
    i = 0
    n = len(chunk)
    while i < n and chunk[i].isspace():
        i += 1
    header = ""
    if i < n and chunk[i] == "#":
        i += 1
        while i < n and chunk[i].isspace():
            i += 1
        if i < n and chunk[i] == "(":
            end = _skip_balanced(chunk, i, "(", ")")
            header = chunk[i + 1 : end - 1] if end > i + 1 else ""
            i = end
    while i < n and chunk[i].isspace():
        i += 1
    if i < n and chunk[i] == "(":
        i = _skip_balanced(chunk, i, "(", ")")
    while i < n and chunk[i].isspace():
        i += 1
    if i < n and chunk[i] == ";":
        i += 1
    return header, chunk[i:]


def strip_body_param_declarations(body: str) -> str:
    """Remove body ``parameter``/``localparam`` statements (index instance scan)."""
    if "parameter" not in body.lower() and "localparam" not in body.lower():
        return body
    lines: List[str] = []
    skip_until_semi = False
    for line in body.splitlines():
        if skip_until_semi:
            if ";" in line:
                skip_until_semi = False
            continue
        stripped = line.lstrip()
        lower = stripped.lower()
        if lower.startswith("parameter") or lower.startswith("localparam"):
            if ";" not in line:
                skip_until_semi = True
            continue
        lines.append(line)
    return "\n".join(lines)


def body_param_scan_skipped(body: str, *, max_body_bytes: Optional[int] = None) -> bool:
    """True when index scan would skip body parameter collection."""
    if max_body_bytes is None:
        from hierwalk.perf import body_param_scan_max

        max_body_bytes = body_param_scan_max()
    return max_body_bytes > 0 and len(body) > max_body_bytes


def collect_module_params(
    header_text: str,
    body: str,
    *,
    max_body_bytes: Optional[int] = None,
) -> Dict[str, str]:
    """Module header + body parameter/localparam declarations (defaults)."""
    params = parse_param_pairs(header_text)
    if body_param_scan_skipped(body, max_body_bytes=max_body_bytes):
        return params
    for m in _BODY_PARAM_STMT_RE.finditer(body):
        params.update(parse_param_pairs(m.group(0)))
    return params


def param_names_in_exprs(exprs: Iterable[str]) -> Set[str]:
    """Identifier tokens in dimension / override expressions (exclude keywords)."""
    names: Set[str] = set()
    for expr in exprs:
        for m in _EXPR_IDENT_RE.finditer(expr):
            tok = m.group(0)
            if tok.lower() not in _PARAM_KEYWORDS:
                names.add(tok)
    return names


def _find_body_param_value(body: str, name: str) -> Optional[str]:
    m = re.search(
        rf"\b(?:parameter|localparam)\b\s+(?:\w+\s+)?{re.escape(name)}\s*=",
        body,
        re.IGNORECASE,
    )
    if not m:
        return None
    val, _ = _scan_param_value(body, m.end())
    return val


def collect_body_params_closure(body: str, seeds: Set[str]) -> Dict[str, str]:
    """Resolve only body params reachable from ``seeds`` (name-targeted search)."""
    if not seeds:
        return {}
    pending = set(seeds)
    found: Dict[str, str] = {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        val = _find_body_param_value(body, name)
        if val is None:
            continue
        found[name] = val
        for dep in param_names_in_exprs([val]):
            if dep not in found:
                pending.add(dep)
    return found


def dim_exprs_from_inst_names(inst_names: Iterable[str]) -> List[str]:
    """Extract ``lo``/``hi`` from ``u[N-1:0]``-style instance names."""
    exprs: List[str] = []
    for inst in inst_names:
        for m in re.finditer(r"\[([^\]]+)\]", inst):
            inner = m.group(1).strip()
            if ":" not in inner:
                continue
            lo, hi = inner.split(":", 1)
            exprs.extend([lo.strip(), hi.strip()])
    return exprs


def instance_param_exprs(edges: Iterable[object]) -> List[str]:
    """Expressions from instance ``#(.name(expr))`` overrides."""
    exprs: List[str] = []
    for edge in edges:
        overrides = getattr(edge, "param_overrides", None) or {}
        exprs.extend(overrides.values())
    return exprs


def bracket_dim_exprs(body: str) -> List[str]:
    """``[lo:hi]`` fragments in *body* (connect/index param seeds)."""
    return [m.group(0) for m in re.finditer(r"\[[^\]]+\]", body)]


def collect_connect_module_params(
    header_text: str,
    body: str,
    *,
    instance_exprs: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Connect-time params: header + seeds from dims / instances (no full body scan)."""
    seeds = list(instance_exprs or [])
    seeds.extend(bracket_dim_exprs(body))
    return collect_index_module_params(header_text, body, seeds)


def collect_index_module_params(
    header_text: str,
    body: str,
    instance_exprs: Iterable[str],
) -> Dict[str, str]:
    """
    Index-time params: header defaults + body closure for instance dim/override refs.

    Never does a full body parameter scan (compact 20k-decl bodies stay fast).
    """
    params = parse_param_pairs(header_text)
    seeds = param_names_in_exprs(instance_exprs) - set(params)
    params.update(collect_body_params_closure(body, seeds))
    return params


def _tokenize_expr(expr: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(expr)
    while i < n:
        if expr[i].isspace():
            i += 1
            continue
        if expr[i] in "()+-*/":
            tokens.append(expr[i])
            i += 1
            continue
        m = re.match(
            r"(<=|>=|==|!=|<|>)|(\d+'[bdhBDH][0-9a-fA-FxzXZ_]+|-?\d+)|([A-Za-z_]\w*)",
            expr[i:],
            re.IGNORECASE,
        )
        if not m:
            i += 1
            continue
        tok = m.group(0)
        tokens.append(tok)
        i += len(tok)
    return tokens


def _eval_tokens(tokens: list[str], ctx: Mapping[str, str]) -> Optional[int]:
    if not tokens:
        return None

    def value_at(idx: int) -> Tuple[Optional[int], int]:
        if idx >= len(tokens):
            return None, idx
        tok = tokens[idx]
        if tok == "(":
            v, j = expr_at(idx + 1)
            if j >= len(tokens) or tokens[j] != ")":
                return None, j
            return v, j + 1
        if tok == "-":
            v, j = value_at(idx + 1)
            return (-v if v is not None else None), j
        if tok == "+":
            return value_at(idx + 1)
        if re.fullmatch(r"-?\d+", tok):
            return int(tok), idx + 1
        if re.fullmatch(r"\d+'[bdhBDH]", tok, re.I) and idx + 1 < len(tokens):
            lit = tok + tokens[idx + 1]
            v = _param_int(lit)
            return v, idx + 2
        v = _param_int(tok)
        if v is not None:
            return v, idx + 1
        if tok in ctx:
            return _param_int(ctx[tok]), idx + 1
        return None, idx + 1

    def term_at(idx: int) -> Tuple[Optional[int], int]:
        left, j = value_at(idx)
        while j < len(tokens) and tokens[j] == "*":
            right, j = value_at(j + 1)
            if left is None or right is None:
                return None, j
            left = left * right
        return left, j

    def sum_at(idx: int) -> Tuple[Optional[int], int]:
        left, j = term_at(idx)
        while j < len(tokens) and tokens[j] in ("+", "-"):
            op = tokens[j]
            right, j = term_at(j + 1)
            if left is None or right is None:
                return None, j
            left = left + right if op == "+" else left - right
        return left, j

    def expr_at(idx: int) -> Tuple[Optional[int], int]:
        return sum_at(idx)

    val, pos = expr_at(0)
    if pos != len(tokens):
        return None
    return val


def resolve_param_expr(expr: str, ctx: Mapping[str, str]) -> Optional[int]:
    expr = expr.strip()
    if not expr:
        return None
    if expr in ctx:
        v = _param_int(ctx[expr])
        if v is not None:
            return v
    qpos = _find_top_level_op(expr, "?")
    if qpos is not None:
        cond = expr[:qpos].strip()
        if cond.startswith("(") and cond.endswith(")"):
            cond = cond[1:-1].strip()
        rest = expr[qpos + 1 :]
        cpos = _find_top_level_op(rest, ":")
        if cpos is not None:
            t_expr = rest[:cpos].strip()
            f_expr = rest[cpos + 1 :].strip()
            cval = expr_is_true(cond, ctx)
            if cval is None:
                return None
            return resolve_param_expr(t_expr if cval else f_expr, ctx)
    v = _param_int(expr)
    if v is not None:
        return v
    return _eval_tokens(_tokenize_expr(expr), ctx)


def expr_is_true(expr: str, ctx: Mapping[str, str]) -> Optional[bool]:
    e = expr.strip()
    if e.lower() in ("1", "1'b1", "1'h1", "'1", "true"):
        return True
    if e.lower() in ("0", "1'b0", "1'h0", "'0", "false"):
        return False
    for op in (">=", "<=", "!=", "==", ">", "<"):
        parts = e.split(op, 1)
        if len(parts) != 2:
            continue
        left = resolve_param_expr(parts[0].strip(), ctx)
        right = resolve_param_expr(parts[1].strip(), ctx)
        if left is None or right is None:
            continue
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == "!=":
            return left != right
        if op == "==":
            return left == right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
    v = resolve_param_expr(e, ctx)
    if v is not None:
        return v != 0
    return None


def resolve_param_map(
    declarations: Mapping[str, str],
    *,
    overrides: Optional[Mapping[str, str]] = None,
    parent: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """
    Fold parameter/localparam declarations with optional parent scope and
    instance #(.) overrides. Returns name -> numeric string when possible.
    """
    raw: Dict[str, str] = dict(declarations)
    if overrides:
        raw.update(overrides)
    resolved: Dict[str, str] = {}
    if parent:
        for k, v in parent.items():
            iv = resolve_param_expr(v, parent) if not str(v).isdigit() else int(v)
            if iv is not None:
                resolved[k] = str(iv)

    for _ in range(len(raw) + 2):
        changed = False
        for name, expr in raw.items():
            ctx = {k: v for k, v in raw.items() if k != name}
            ctx.update(resolved)
            iv = resolve_param_expr(expr, ctx)
            if iv is None:
                continue
            new_v = str(iv)
            if resolved.get(name) != new_v:
                resolved[name] = new_v
                changed = True
        if not changed:
            break
    for name, expr in raw.items():
        if name not in resolved:
            resolved[name] = expr.strip()
    return resolved


def parse_bound_token(token: str, param_map: Mapping[str, str]) -> Optional[int]:
    token = token.strip()
    v = resolve_param_expr(token, param_map)
    if v is not None:
        return v
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    return None