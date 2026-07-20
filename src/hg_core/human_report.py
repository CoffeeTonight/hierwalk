"""Human-readable hgpath / hgconn report tables."""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck

from hg_core.summary import (
    classify_check_pair,
    classify_endpoint_failure,
    _hop_stats,
    _pct,
)
from hgconn.walk import ConnResult
from hgpath.tree_db import TreeEntry


def _truncate(text: str, width: int) -> str:
    raw = str(text or "").strip() or "-"
    if len(raw) <= width:
        return raw
    if width <= 3:
        return raw[:width]
    keep = width - 3
    head = max(1, keep * 2 // 3)
    tail = keep - head
    return f"{raw[:head]}...{raw[-tail:]}"


def _leaf_label(entry: TreeEntry) -> str:
    if entry.port_tail:
        return f"port:{entry.port_tail}"
    if entry.nodes:
        last = entry.nodes[-1]
        if last.kind:
            return f"{last.kind}:{last.segment}"
        if last.role == "inst":
            return f"inst:{last.segment}"
        return last.segment
    return "-"


def _fail_node(entry: TreeEntry) -> str:
    if entry.ok:
        return "-"
    if entry.nodes:
        return entry.nodes[-1].path or entry.nodes[-1].segment
    return "(root)"


def _status_mark(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    aligns: Optional[Sequence[str]] = None,
    max_widths: Optional[Sequence[int]] = None,
) -> List[str]:
    """Render a fixed-width pipe table."""
    if not headers:
        return []
    ncols = len(headers)
    aligns = aligns or ("left",) * ncols
    max_widths = max_widths or (0,) * ncols

    cols: List[List[str]] = []
    for i, header in enumerate(headers):
        limit = max_widths[i] if i < len(max_widths) else 0
        cap = limit if limit > 0 else 10_000
        col_vals = [_truncate(header, cap)]
        for row in rows:
            cell = row[i] if i < len(row) else ""
            col_vals.append(_truncate(str(cell), cap))
        cols.append(col_vals)

    widths = [max(len(v) for v in col) for col in cols]

    def _fmt_cell(text: str, width: int, align: str) -> str:
        if align == "right":
            return text.rjust(width)
        if align == "center":
            return text.center(width)
        return text.ljust(width)

    def _row(cells: Sequence[str]) -> str:
        parts = [
            _fmt_cell(cells[i], widths[i], aligns[i] if i < len(aligns) else "left")
            for i in range(ncols)
        ]
        return "  | ".join(parts)

    sep = "  | ".join("-" * w for w in widths)
    out = [_row(headers), sep]
    for row in rows:
        out.append(_row(row))
    return out


def _json_endpoint_rows(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
) -> List[Tuple[str, str, str, TreeEntry]]:
    """Each JSON check endpoint (a/b) in file order."""
    rows: List[Tuple[str, str, str, TreeEntry]] = []
    for chk, ea, eb in check_results:
        cid = chk.check_id or "-"
        rows.append((cid, "a", str(chk.endpoint_a), ea))
        rows.append((cid, "b", str(chk.endpoint_b), eb))
    return rows


def _summary_lines(
    *,
    tool: str,
    top: str,
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    entries: Mapping[str, TreeEntry],
    conn_results: Optional[Sequence[ConnResult]] = None,
    db_info: Optional[Mapping[str, object]] = None,
) -> List[str]:
    n_checks = len(check_results)
    endpoint_vals = list(entries.values())
    ep_pass = sum(1 for e in endpoint_vals if e.ok)
    ep_total = len(endpoint_vals)
    pair_pass = sum(1 for _c, ea, eb in check_results if ea.ok and eb.ok)
    min_h, max_h, avg_h = _hop_stats(endpoint_vals)

    lines = [
        "=" * 78,
        f"  1. SUMMARY",
        "=" * 78,
        f"  tool                         {tool}",
        f"  top                          {top or '-'}",
        f"  check pairs (JSON)           {n_checks}",
        f"  unique hierarchy endpoints   {ep_total}",
        f"  endpoint resolve             {ep_pass} / {ep_total}  ({_pct(ep_pass, ep_total)})",
        f"  pair resolve (a & b)         {pair_pass} / {n_checks}  ({_pct(pair_pass, n_checks)})",
        f"  inst hops                    min={min_h}  max={max_h}  avg={avg_h:.1f}",
    ]

    if conn_results is not None:
        conn_pass = sum(1 for r in conn_results if r.connected)
        modes = Counter(r.mode for r in conn_results if r.connected)
        lines.append(
            f"  text-conn (bloom)            {conn_pass} / {len(conn_results)}  "
            f"({_pct(conn_pass, len(conn_results))})"
        )
        if modes:
            mode_txt = ", ".join(f"{k}={v}" for k, v in sorted(modes.items()))
            lines.append(f"  conn pass modes              {mode_txt}")

    if ep_total > ep_pass:
        causes = Counter(classify_endpoint_failure(e) for e in endpoint_vals if not e.ok)
        cause_txt = ", ".join(f"{k}={v}" for k, v in sorted(causes.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"  endpoint fail causes         {cause_txt}")

    if db_info and db_info.get("simple_exist"):
        lines.append("  simple_exist                 on (preprocess: comments-only)")

    if db_info:
        for key in (
            "flat_db",
            "tree_db",
            "hierarchy_json",
            "modules",
            "rtl_files",
            "tree_nodes",
        ):
            if key in db_info:
                lines.append(f"  {key:<28} {db_info[key]}")

    return lines


def _endpoint_table_lines(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
) -> List[str]:
    headers = (
        "#",
        "check_id",
        "side",
        "hierarchy (JSON)",
        "status",
        "hops",
        "leaf",
        "fail_node",
        "error",
    )
    aligns = ("right", "left", "center", "left", "center", "right", "left", "left", "left")
    max_widths = (4, 10, 4, 40, 6, 4, 14, 24, 28)

    rows: List[List[str]] = []
    for idx, (cid, side, spec, ent) in enumerate(_json_endpoint_rows(check_results), start=1):
        err = ent.error or ("ambiguous" if ent.ambiguous else "-")
        rows.append(
            [
                str(idx),
                cid,
                side,
                spec,
                _status_mark(ent.ok),
                str(len(ent.nodes)),
                _leaf_label(ent),
                _fail_node(ent),
                err if not ent.ok else "-",
            ]
        )

    lines = [
        "",
        "=" * 78,
        "  2. HIERARCHY ENDPOINTS (JSON checks — existence / fail node)",
        "=" * 78,
    ]
    lines.extend(render_table(headers, rows, aligns=aligns, max_widths=max_widths))
    return lines


def _text_conn_table_lines(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    conn_results: Sequence[ConnResult],
) -> List[str]:
    headers = (
        "id",
        "leaf_a",
        "leaf_b",
        "conn",
        "mode",
        "detail",
        "ms",
    )
    aligns = ("left", "left", "left", "center", "left", "left", "right")
    max_widths = (10, 18, 18, 5, 14, 36, 8)

    rows: List[List[str]] = []
    for (chk, ea, eb), conn in zip(check_results, conn_results):
        cid = chk.check_id or "-"
        rows.append(
            [
                cid,
                _leaf_label(ea),
                _leaf_label(eb),
                "YES" if conn.connected else "NO",
                conn.mode,
                conn.detail or "-",
                f"{conn.elapsed_ms:.1f}",
            ]
        )

    lines = [
        "",
        "=" * 78,
        "  3. HIERARCHY GREP TEXT-CONN (bloom probe per check)",
        "=" * 78,
    ]
    lines.extend(render_table(headers, rows, aligns=aligns, max_widths=max_widths))
    return lines


def _pair_table_lines(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    *,
    conn_results: Optional[Sequence[ConnResult]] = None,
) -> List[str]:
    title = (
        "  4. HIERARCHY ↔ HIERARCHY (pair resolve + connectivity)"
        if conn_results is not None
        else "  4. HIERARCHY ↔ HIERARCHY (pair resolve)"
    )

    if conn_results is not None:
        headers = (
            "id",
            "hierarchy_a",
            "a",
            "hierarchy_b",
            "b",
            "pair",
            "conn",
            "mode",
        )
        aligns = ("left", "left", "center", "left", "center", "center", "center", "left")
        max_widths = (10, 30, 4, 30, 4, 5, 5, 12)
    else:
        headers = ("id", "hierarchy_a", "a", "hierarchy_b", "b", "pair")
        aligns = ("left", "left", "center", "left", "center", "center")
        max_widths = (10, 34, 4, 34, 4, 5)

    rows: List[List[str]] = []
    conn_iter = iter(conn_results or ())
    for chk, ea, eb in check_results:
        cid = chk.check_id or "-"
        pair_ok = ea.ok and eb.ok
        row = [
            cid,
            str(chk.endpoint_a),
            "OK" if ea.ok else "NO",
            str(chk.endpoint_b),
            "OK" if eb.ok else "NO",
            "PASS" if pair_ok else "FAIL",
        ]
        if conn_results is not None:
            conn = next(conn_iter, None)
            if conn is None:
                row.extend(["-", "-"])
            else:
                row.extend(
                    [
                        "YES" if conn.connected else "NO",
                        conn.mode,
                    ]
                )
        rows.append(row)

    lines = ["", "=" * 78, title, "=" * 78]
    lines.extend(render_table(headers, rows, aligns=aligns, max_widths=max_widths))
    return lines


def build_hgpath_human_report(
    *,
    top: str,
    entries: Mapping[str, TreeEntry],
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    db_info: Optional[Mapping[str, object]] = None,
) -> List[str]:
    if not check_results:
        return [
            "=" * 78,
            "  1. SUMMARY",
            "=" * 78,
            "  (no checks in input JSON)",
        ]
    lines: List[str] = []
    lines.extend(
        _summary_lines(
            tool="hgpath",
            top=top,
            check_results=check_results,
            entries=entries,
            db_info=db_info,
        )
    )
    lines.extend(_endpoint_table_lines(check_results))
    lines.extend(_pair_table_lines(check_results, conn_results=None))
    return lines


def build_hgconn_human_report(
    *,
    top: str,
    entries: Mapping[str, TreeEntry],
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    conn_results: Sequence[ConnResult],
) -> List[str]:
    if not check_results:
        return [
            "=" * 78,
            "  1. SUMMARY",
            "=" * 78,
            "  (no checks in input JSON)",
        ]
    lines: List[str] = []
    lines.extend(
        _summary_lines(
            tool="hgconn",
            top=top,
            check_results=check_results,
            entries=entries,
            conn_results=conn_results,
        )
    )
    lines.extend(_endpoint_table_lines(check_results))
    lines.extend(_text_conn_table_lines(check_results, conn_results))
    lines.extend(_pair_table_lines(check_results, conn_results=conn_results))
    return lines