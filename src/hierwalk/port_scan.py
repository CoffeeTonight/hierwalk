"""Lightweight module port scan from RTL source (header only)."""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.inst_scan import _MODULE_BLOCK_RE
from hierwalk.models import PortInfo
from hierwalk.params import resolve_param_expr, split_module_header

_DIM_RE = re.compile(r"\[([^\]]+)\]")
_DEFAULT_MAX_EXPAND = 512

_PORT_DIR_RE = re.compile(
    r"\b(input|output|inout)\b",
    re.IGNORECASE,
)
_INTF_PORT_RE = re.compile(
    r"\.\s*([A-Za-z_]\w*)\s*\(",
)

_KEYWORDS = frozenset(
    {
        "input", "output", "inout", "wire", "reg", "logic", "signed",
        "unsigned", "var", "parameter", "localparam",
    }
)


def _strip_intf_ports(header: str) -> str:
    return _INTF_PORT_RE.sub(" ", header)


def parse_dim_bounds(spec: str) -> tuple[int, int]:
    text = spec.strip()
    if ":" in text:
        hi_s, lo_s = text.split(":", 1)
        return int(hi_s.strip()), int(lo_s.strip())
    value = int(text.strip())
    return value, 0


def resolve_dim_spec(spec: str, ctx: Mapping[str, str]) -> Optional[tuple[int, int]]:
    """Evaluate ``[WIDTH-1:0]`` / ``[3]`` bounds using module/instance parameters."""
    spec = spec.strip()
    if ":" in spec:
        hi_s, lo_s = spec.split(":", 1)
        hi = resolve_param_expr(hi_s.strip(), ctx)
        lo = resolve_param_expr(lo_s.strip(), ctx)
        if hi is None or lo is None:
            return None
        return hi, lo
    v = resolve_param_expr(spec, ctx)
    if v is None:
        return None
    return v, 0


def indices_for_bounds(high: int, low: int) -> List[int]:
    if high >= low:
        return list(range(high, low - 1, -1))
    return list(range(high, low + 1))


def index_in_bounds(idx: int, high: int, low: int) -> bool:
    if high >= low:
        return low <= idx <= high
    return high <= idx <= low


def _literal_dim_token(token: str) -> Optional[int]:
    token = token.strip()
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    return None


def match_literal_port_indices(
    info: PortInfo,
    literal_dims: Sequence[str],
    ctx: Mapping[str, str],
) -> bool:
    """True when ``literal_dims`` lie within parameter-resolved port bounds."""
    if not info.dim_specs or len(literal_dims) != len(info.dim_specs):
        return False
    for lit, spec in zip(literal_dims, info.dim_specs):
        idx = _literal_dim_token(lit)
        if idx is None:
            return False
        bounds = resolve_dim_spec(spec, ctx)
        if bounds is None:
            return False
        hi, lo = bounds
        if not index_in_bounds(idx, hi, lo):
            return False
    return True


def materialize_literal_port_name(base: str, literal_dims: Sequence[str]) -> str:
    return base + "".join(f"[{d}]" for d in literal_dims)


def expand_port_name(
    base: str,
    width: str = "",
    *,
    max_expand: int = _DEFAULT_MAX_EXPAND,
) -> List[str]:
    base = (base or "").strip()
    if not base:
        return []
    specs = [parse_dim_bounds(m) for m in _DIM_RE.findall(width or "")]
    if not specs:
        return [base]

    index_lists: List[List[int]] = []
    for hi, lo in specs:
        idxs = indices_for_bounds(hi, lo)
        if len(idxs) > max_expand:
            idxs = idxs[:max_expand]
        index_lists.append(idxs)

    out: List[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    def walk(dim_no: int, prefix: str) -> None:
        if dim_no >= len(index_lists):
            add(prefix)
            return
        for idx in index_lists[dim_no]:
            walk(dim_no + 1, f"{prefix}[{idx}]")

    walk(0, base)

    for fixed_dims in range(len(specs)):
        prefix_lists = index_lists[:fixed_dims]
        combos = [()] if not prefix_lists else product(*prefix_lists)
        for prefix_indices in combos:
            alias = base
            for idx in prefix_indices:
                alias += f"[{idx}]"
            for dim_no in range(fixed_dims, len(specs)):
                hi, lo = specs[dim_no]
                alias += f"[{hi}:{lo}]"
            add(alias)

    return out


def expand_port_dims(
    base: str,
    dim_specs: List[str],
    ctx: Mapping[str, str],
    *,
    max_expand: int = _DEFAULT_MAX_EXPAND,
) -> tuple[List[str], str]:
    """Expand port dims; unresolved parameter exprs keep a symbolic alias."""
    if not dim_specs:
        return [base], "resolved"

    bounds: List[tuple[int, int]] = []
    for spec in dim_specs:
        b = resolve_dim_spec(spec, ctx)
        if b is None:
            symbolic = base + "".join(f"[{s}]" for s in dim_specs)
            return sorted({base, symbolic}), f"unresolved: {', '.join(dim_specs)}"
        bounds.append(b)

    width = "".join(f"[{hi}:{lo}]" for hi, lo in bounds)
    names = expand_port_name(base, width, max_expand=max_expand)
    if base not in names:
        names = [base, *names]
    return names, "resolved"


def _split_port_list(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
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


def _parse_port_token(token: str) -> Optional[tuple[str, List[str]]]:
    token = token.strip()
    if not token:
        return None
    m = re.search(r"\b([A-Za-z_]\w*)\s*$", token)
    if not m:
        return None
    name = m.group(1)
    if name.lower() in _KEYWORDS:
        return None
    dims = _DIM_RE.findall(token)
    return name, dims


def _strip_port_type_prefix(token: str) -> str:
    text = token.strip()
    text = re.sub(r"^\s*\b(input|output|inout)\b\s+", "", text, flags=re.IGNORECASE)
    while True:
        nxt = re.sub(
            r"^\s*\b(wire|reg|logic|signed|unsigned|var)\b\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        if nxt == text:
            break
        text = nxt
    return text


def _decl_line(lines: List[str], base_name: str) -> int:
    for i, line in enumerate(lines, 1):
        if base_name in line and _PORT_DIR_RE.search(line):
            return i
    for i, line in enumerate(lines, 1):
        if re.search(rf"\b{re.escape(base_name)}\b", line):
            return i
    return 0


def _port_info_from_token(
    token: str,
    ctx: Mapping[str, str],
    lines: List[str],
) -> Optional[PortInfo]:
    parsed = _parse_port_token(_strip_port_type_prefix(token))
    if parsed is None:
        return None
    base, dim_specs = parsed
    names, note = expand_port_dims(base, dim_specs, ctx)
    line = _decl_line(lines, base)
    decl = token.strip().replace("\n", " ")[:200]
    return PortInfo(
        base_name=base,
        names=names,
        dim_specs=dim_specs,
        line=line,
        decl=decl,
        param_note=note,
    )


def _collect_ports_from_decl(
    decl: str,
    ctx: Mapping[str, str],
    lines: List[str],
) -> List[PortInfo]:
    out: List[PortInfo] = []
    active_dir = ""
    for token in _split_port_list(decl):
        stripped = token.strip()
        dir_m = re.match(r"^(input|output|inout)\b\s*(.*)$", stripped, re.IGNORECASE)
        if dir_m:
            active_dir = dir_m.group(1).lower()
            body = dir_m.group(2).strip()
        else:
            body = stripped
        if not body:
            continue
        prefixed = f"{active_dir} {body}" if active_dir else body
        info = _port_info_from_token(prefixed, ctx, lines)
        if info is not None:
            if active_dir and not info.decl.lower().startswith(active_dir):
                info = PortInfo(
                    base_name=info.base_name,
                    names=info.names,
                    dim_specs=info.dim_specs,
                    line=info.line,
                    decl=f"{active_dir} {info.decl}",
                    param_note=info.param_note,
                )
            out.append(info)
    return out


def scan_ports_detail_from_module_text(
    text: str,
    module_name: str,
    *,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> List[PortInfo]:
    ctx = dict(param_ctx or {})
    lines = text.splitlines()
    infos: List[PortInfo] = []
    for m in _MODULE_BLOCK_RE.finditer(text):
        if m.group(1) != module_name:
            continue
        chunk = m.group(2)
        header, body = split_module_header(chunk)
        header_text = _strip_intf_ports(header)
        for pm in _PORT_DIR_RE.finditer(header_text):
            tail = header_text[pm.end() :]
            semi = tail.find(";")
            chunk_decl = tail if semi < 0 else tail[:semi]
            infos.extend(_collect_ports_from_decl(chunk_decl, ctx, lines))
        for pm in re.finditer(
            r"\b([A-Za-z_]\w*)\s*((?:\[[^\]]+\])*)\s*(?:,|\))\s*(input|output|inout)\b",
            header_text,
            re.IGNORECASE,
        ):
            dim_specs = _DIM_RE.findall(pm.group(2) or "")
            names, note = expand_port_dims(pm.group(1), dim_specs, ctx)
            infos.append(
                PortInfo(
                    base_name=pm.group(1),
                    names=names,
                    dim_specs=dim_specs,
                    line=_decl_line(lines, pm.group(1)),
                    decl=pm.group(0).strip()[:200],
                    param_note=note,
                )
            )
        port_list = re.search(r"\(\s*([^)]*)\)\s*;", chunk, re.DOTALL)
        if port_list:
            infos.extend(_collect_ports_from_decl(port_list.group(1), ctx, lines))
        prefix = body[:4000]
        for pm in re.finditer(r"\b(input|output|inout)\b[^;]*;", prefix, re.IGNORECASE):
            decl = re.sub(
                r"^\s*\b(input|output|inout)\b",
                "",
                pm.group(0),
                flags=re.IGNORECASE,
            ).rstrip(";").strip()
            infos.extend(_collect_ports_from_decl(decl, ctx, lines))
        return infos
    return []


def _param_ctx_key(ctx: Mapping[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(ctx.items()))


def _build_port_index(infos: List[PortInfo]) -> Dict[str, PortInfo]:
    index: Dict[str, PortInfo] = {}
    for info in infos:
        for name in info.names:
            index[name] = info
    return index


@lru_cache(maxsize=4096)
def _cached_port_index_from_text(
    module_text: str,
    module_name: str,
    ctx_key: str,
    param_items: Tuple[Tuple[str, str], ...],
) -> Dict[str, PortInfo]:
    ctx = dict(param_items)
    infos = scan_ports_detail_from_module_text(
        module_text,
        module_name,
        param_ctx=ctx,
    )
    return _build_port_index(infos)


@lru_cache(maxsize=512)
def _read_source_text(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


@lru_cache(maxsize=4096)
def _cached_port_index_from_file(
    file_path: str,
    module_name: str,
    ctx_key: str,
    param_items: Tuple[Tuple[str, str], ...],
) -> Dict[str, PortInfo]:
    text = _read_source_text(file_path)
    if not text:
        return {}
    return _cached_port_index_from_text(text, module_name, ctx_key, param_items)


def port_index_for_module(
    file_path: str,
    module_name: str,
    param_ctx: Optional[Mapping[str, str]] = None,
    *,
    module_text: Optional[str] = None,
) -> Dict[str, PortInfo]:
    if not file_path and not module_text:
        return {}
    ctx = dict(param_ctx or {})
    items = tuple(sorted(ctx.items()))
    key = _param_ctx_key(ctx)
    if module_text is not None:
        return dict(
            _cached_port_index_from_text(module_text, module_name, key, items)
        )
    return dict(_cached_port_index_from_file(file_path, module_name, key, items))


def port_index_for_design_module(
    index: object,
    module_name: str,
    param_ctx: Optional[Mapping[str, str]] = None,
    *,
    defines: Mapping[str, str] | None = None,
) -> Dict[str, PortInfo]:
    """Port index resolved via :class:`DesignIndex` (preprocessed RTL when available)."""
    get_module = getattr(index, "get_module", None)
    if not callable(get_module):
        return {}
    rec = get_module(module_name)
    if rec is None:
        return {}
    module_text = None
    source_text = getattr(index, "_source_text", None)
    if callable(source_text) and rec.file_path:
        if defines is not None:
            eff: Mapping[str, str] = defines
        else:
            from hierwalk.lazy_scope import lazy_processing_enabled

            seed = getattr(index, "seed_preprocess_defines", None)
            if lazy_processing_enabled() and callable(seed):
                eff = seed()
            else:
                effective = getattr(index, "effective_defines", None)
                eff = effective() if callable(effective) else {}
        module_text = source_text(rec.file_path, full=True, defines=eff)
    return port_index_for_module(
        rec.file_path,
        module_name,
        param_ctx,
        module_text=module_text or None,
    )


def ports_for_design_module(
    index: object,
    module_name: str,
    param_ctx: Optional[Mapping[str, str]] = None,
    *,
    defines: Mapping[str, str] | None = None,
) -> Set[str]:
    return set(
        port_index_for_design_module(
            index,
            module_name,
            param_ctx,
            defines=defines,
        )
    )


def ports_for_module(
    file_path: str,
    module_name: str,
    param_ctx: Optional[Mapping[str, str]] = None,
    *,
    index: object | None = None,
    defines: Mapping[str, str] | None = None,
) -> Set[str]:
    if index is not None:
        return ports_for_design_module(
            index,
            module_name,
            param_ctx,
            defines=defines,
        )
    return set(port_index_for_module(file_path, module_name, param_ctx))


def scan_ports_from_module_text(
    text: str,
    module_name: str,
    *,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> Set[str]:
    infos = scan_ports_detail_from_module_text(text, module_name, param_ctx=param_ctx)
    names: Set[str] = set()
    for info in infos:
        names.update(info.names)
    return names


def scan_ports_from_file(
    file_path: str | Path,
    module_name: str,
    *,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> Set[str]:
    return ports_for_module(str(file_path), module_name, param_ctx)


def port_glob_match(port_name: str, pattern: str) -> bool:
    import fnmatch

    if "[" in pattern or "]" in pattern:
        if "*" in pattern or "?" in pattern:
            return _port_bracket_glob_match(port_name, pattern)
        return port_name.lower() == pattern.lower()
    if any(ch in pattern for ch in "*?[]"):
        return fnmatch.fnmatchcase(port_name, pattern)
    return port_name.lower() == pattern.lower()


def _port_bracket_glob_match(port_name: str, pattern: str) -> bool:
    import fnmatch

    port_base, port_dims = _split_port_base_dims(port_name)
    pat_base, pat_dims = _split_port_base_dims(pattern)
    if not fnmatch.fnmatchcase(port_base, pat_base):
        return False
    if len(port_dims) != len(pat_dims):
        # symbolic pattern data[WIDTH-1:0] vs materialized data[31:0]
        if "[" in pattern and port_name.lower() == pattern.lower():
            return True
        return False
    for port_dim, pat_dim in zip(port_dims, pat_dims):
        if pat_dim == "*":
            continue
        if not fnmatch.fnmatchcase(port_dim, pat_dim):
            return False
    return True


def _split_port_base_dims(name: str) -> tuple[str, List[str]]:
    base_m = re.match(r"^([A-Za-z_]\w*)", name)
    if not base_m:
        return name, []
    base = base_m.group(1)
    dims = _DIM_RE.findall(name[len(base) :])
    return base, dims


def matching_ports(
    port_index: Mapping[str, PortInfo],
    pattern: str,
    *,
    param_ctx: Optional[Mapping[str, str]] = None,
) -> List[str]:
    direct = sorted(name for name in port_index if port_glob_match(name, pattern))
    if direct:
        return direct

    if not param_ctx:
        return []

    pat_base, pat_dims = _split_port_base_dims(pattern)
    if not pat_dims or any(ch in d for ch in "*?[]" for d in pat_dims):
        return []

    seen: set[str] = set()
    out: List[str] = []
    for info in port_index.values():
        if info.base_name in seen:
            continue
        seen.add(info.base_name)
        if pat_base.lower() != info.base_name.lower():
            continue
        if match_literal_port_indices(info, pat_dims, param_ctx):
            out.append(materialize_literal_port_name(pat_base, pat_dims))
    return sorted(set(out))