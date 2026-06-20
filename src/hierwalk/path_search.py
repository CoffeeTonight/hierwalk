"""Hierarchy path and port search with ``*`` / ``?`` globs."""

from __future__ import annotations

from typing import List, Mapping, Optional, Sequence, Tuple

from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow, PortInfo, SearchHit
from hierwalk.params import resolve_param_map
from hierwalk.path_chain import attach_path_chains
from hierwalk.path_refine import refine_param_ctx_for_path
from hierwalk.port_scan import matching_ports, port_index_for_module
from hierwalk.search import _segment_match, hit_from_row


def hierarchy_glob_match(
    path: str,
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> bool:
    path_parts = path.split(".")
    pat_parts = pattern.split(".")
    if len(path_parts) != len(pat_parts):
        return False
    return all(
        _segment_match(
            part,
            glob_part,
            case_insensitive=case_insensitive,
        )
        for part, glob_part in zip(path_parts, pat_parts)
    )


def parse_hierarchy_port_pattern(
    pattern: str,
    rows: Optional[Sequence[FlatRow]] = None,
    *,
    case_insensitive: bool = False,
) -> Tuple[str, Optional[str]]:
    """
    Split trailing port segment only when the full pattern is not an instance path.

    Three-or-more segments are not always ``inst.port`` — e.g. ``top.u_mid.u_leaf``
    is a 3-level instance path. Try full-path instance match first (when *rows* are
    available), then treat the last segment as a port name.
    """
    parts = pattern.split(".")
    if len(parts) < 3:
        return pattern, None
    if rows is not None and any(
        hierarchy_glob_match(
            row.full_path,
            pattern,
            case_insensitive=case_insensitive,
        )
        for row in rows
    ):
        return pattern, None
    return ".".join(parts[:-1]), parts[-1]


def _port_param_ctx(index: DesignIndex, row: FlatRow) -> Mapping[str, str]:
    if row.param_ctx:
        return row.param_ctx
    rec = index.get_module(row.module)
    if not rec:
        return {}
    return resolve_param_map(rec.raw_params)


def _top_from_rows(rows: Sequence[FlatRow]) -> str:
    if not rows:
        return ""
    return rows[0].full_path.split(".", 1)[0]


def _port_search_miss_note(
    row: FlatRow,
    port_pat: str,
    port_index: Mapping[str, PortInfo],
    *,
    refine_note: str = "",
) -> str:
    """Explain a hierarchy match with no matching port (pattern vs RTL)."""
    base_ports = sorted({info.base_name for info in port_index.values()})
    parts: List[str] = []
    if refine_note:
        parts.append(refine_note)
    parts.append(
        f"port not found: '{port_pat}' on {row.module} ({row.file})"
    )
    if base_ports:
        parts.append(
            f"declared ports ({len(base_ports)}): {', '.join(base_ports[:20])}"
        )
        leaf = port_pat.split("[", 1)[0].split(".", 1)[0]
        similar = [
            p
            for p in base_ports
            if leaf.lower() in p.lower() or p.lower().startswith(leaf.lower())
        ]
        if similar:
            parts.append(f"similar: {', '.join(similar[:8])}")
    else:
        parts.append("no ports parsed for this module (blackbox or parse limit)")
    return "; ".join(parts)


def search_hierarchy_path(
    rows: Sequence[FlatRow],
    pattern: str,
    index: DesignIndex,
    *,
    require_port: bool = True,
    refine_port_ctx: bool = True,
    case_insensitive: bool = False,
) -> List[SearchHit]:
    inst_pat, port_pat = parse_hierarchy_port_pattern(
        pattern,
        rows,
        case_insensitive=case_insensitive,
    )
    top = _top_from_rows(rows)
    refine_cache: dict = {}
    hits: List[SearchHit] = []
    for row in rows:
        if not hierarchy_glob_match(
            row.full_path,
            inst_pat,
            case_insensitive=case_insensitive,
        ):
            continue
        if port_pat is None:
            hit = hit_from_row(
                row, matched_name=row.inst_leaf, match_kind="hierarchy"
            )
            hits.append(hit)
            continue
        if not require_port:
            hit = hit_from_row(
                row,
                matched_name=port_pat,
                match_kind="hierarchy-port",
                full_path=f"{row.full_path}.{port_pat}",
            )
            hit.port_name = port_pat
            hits.append(hit)
            continue

        ctx = _port_param_ctx(index, row)
        refine_note = ""
        if refine_port_ctx and top:
            if row.full_path not in refine_cache:
                refine_cache[row.full_path] = refine_param_ctx_for_path(
                    index, top, row.full_path
                )
            refined = refine_cache[row.full_path]
            if refined.ok:
                ctx = refined.param_ctx
                refine_note = refined.note
        port_index = port_index_for_module(row.file, row.module, ctx)
        matched = matching_ports(port_index, port_pat, param_ctx=ctx)
        if not matched:
            if "*" in inst_pat or "?" in inst_pat or "[" in inst_pat:
                continue
            hit = hit_from_row(
                row,
                matched_name=port_pat,
                match_kind="hierarchy-port-miss",
                full_path=f"{row.full_path}.{port_pat}",
            )
            hit.port_name = port_pat
            hit.port_found = False
            hit.port_param_note = _port_search_miss_note(
                row,
                port_pat,
                port_index,
                refine_note=refine_note,
            )
            hits.append(hit)
            continue
        for port_name in matched:
            info = port_index[port_name]
            hit = hit_from_row(
                row,
                matched_name=port_name,
                match_kind="hierarchy-port",
                full_path=f"{row.full_path}.{port_name}",
            )
            hit.port_name = port_name
            hit.port_found = True
            hit.port_line = info.line
            hit.port_decl = info.decl
            note = info.param_note
            if refine_note and note:
                hit.port_param_note = f"{refine_note}; {note}"
            else:
                hit.port_param_note = refine_note or note
            hits.append(hit)
    return attach_path_chains(
        hits,
        index,
        rows,
        top=top,
        refine_paths=refine_port_ctx,
        refine_cache=refine_cache,
    )