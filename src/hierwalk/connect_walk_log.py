"""Human-readable COI walk diagnostics for connect trace / connect_log."""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, List, Mapping, Optional, Sequence, Tuple

from hierwalk.hierarchy_log import (
    collect_hop_scopes,
    format_hierarchy_row_line,
    format_scope_provenance_line,
    format_scopes_provenance_lines,
    hierarchy_spine_between,
    scopes_from_hop_detail,
)
from hierwalk.models import ConnectHop, ConnectResult, FlatRow
from hierwalk.progress import format_hierwalk_log

NetState = Tuple[str, str]

_CONNECT_PREFIX = "[hier-walk connect]"


@dataclass(frozen=True)
class CoiWalkDiagnostic:
    """Partial bidirectional COI frontier when start/goal nets never meet."""

    nearest_from_a: NetState
    nearest_from_b: NetState
    hops_from_a: Tuple[ConnectHop, ...]
    hops_from_b: Tuple[ConnectHop, ...]
    scopes_visited: Tuple[str, ...]
    modules_parsed: int = 0


def _net_label(scope: str, net: str) -> str:
    return f"{scope}:{net}" if net else scope


def format_connect_log_line(message: str, *, prefix: str = _CONNECT_PREFIX) -> str:
    """Timestamped connect log line (matches path-walk stderr style)."""
    return format_hierwalk_log(message, prefix=prefix)


def _emit_line(stream: IO[str], message: str, *, prefix: str) -> None:
    print(format_connect_log_line(message, prefix=prefix), file=stream, flush=True)


def _scope_sort_key(scope: str, rows_by_path: Mapping[str, FlatRow]) -> Tuple[int, str]:
    row = rows_by_path.get(scope)
    depth = row.depth if row is not None else scope.count(".")
    return (depth, scope)


def build_walk_notes(
    diagnostic: CoiWalkDiagnostic,
    *,
    rows_by_path: Mapping[str, FlatRow],
    start: NetState,
    goal: NetState,
) -> List[str]:
    """Short machine-oriented summary lines stored on :class:`ConnectResult`."""
    a_scope, a_net = diagnostic.nearest_from_a
    b_scope, b_net = diagnostic.nearest_from_b
    lines = [
        (
            f"COI from A: reached {_net_label(a_scope, a_net)} "
            f"({len(diagnostic.hops_from_a)} hop(s))"
        ),
        (
            f"COI from B: reached {_net_label(b_scope, b_net)} "
            f"({len(diagnostic.hops_from_b)} hop(s))"
        ),
        (
            f"gap: A frontier {_net_label(a_scope, a_net)} vs "
            f"B frontier {_net_label(b_scope, b_net)} "
            f"(start {_net_label(*start)} goal {_net_label(*goal)})"
        ),
        f"scopes visited: {len(diagnostic.scopes_visited)}",
        f"modules parsed: {diagnostic.modules_parsed}",
    ]
    rtl_seen: List[str] = []
    for scope in diagnostic.scopes_visited:
        row = rows_by_path.get(scope)
        if row is not None and row.file and row.file not in rtl_seen:
            rtl_seen.append(row.file)
    if rtl_seen:
        lines.append(f"rtl touched: {len(rtl_seen)} file(s)")
    return lines


def _emit_hop_walk(
    stream: IO[str],
    *,
    prefix: str,
    title: str,
    hops: Sequence[ConnectHop],
    rows_by_path: Mapping[str, FlatRow],
    indent: str,
) -> None:
    if not hops:
        return
    _emit_line(stream, title, prefix=prefix)
    for i, hop in enumerate(hops, 1):
        _emit_line(stream, f"{indent}{i}. [{hop.kind}] {hop.detail}", prefix=prefix)
        hop_scopes = scopes_from_hop_detail(hop.detail)
        for scope_line in format_scopes_provenance_lines(
            hop_scopes,
            rows_by_path,
            indent=indent + "   ",
        ):
            _emit_line(stream, scope_line, prefix=prefix)


def emit_connect_walk_report(
    result: ConnectResult,
    *,
    stream: IO[str],
    prefix: str,
    rows_by_path: Mapping[str, FlatRow],
    diagnostic: Optional[CoiWalkDiagnostic] = None,
) -> None:
    """
    Emit COI walk evidence for one connect check (success or failure).

    On failure, shows hierarchy spine, partial A/B frontiers, scopes, and RTL paths.
    """
    if rows_by_path is None:
        return

    spine = hierarchy_spine_between(
        result.endpoint_a.inst_path,
        result.endpoint_b.inst_path,
    )
    if spine:
        _emit_line(stream, "path hierarchy (rtl + filelist):", prefix=prefix)
        for line in format_scopes_provenance_lines(spine, rows_by_path, indent="    "):
            _emit_line(stream, line, prefix=prefix)

    hops = list(result.hops)
    if result.connected and hops:
        _emit_line(stream, "path evidence:", prefix=prefix)
        for i, hop in enumerate(hops, 1):
            _emit_line(stream, f"  {i}. [{hop.kind}] {hop.detail}", prefix=prefix)
            hop_scopes = scopes_from_hop_detail(hop.detail)
            for line in format_scopes_provenance_lines(
                hop_scopes,
                rows_by_path,
                indent="      ",
            ):
                _emit_line(stream, line, prefix=prefix)
        return

    if not result.connected:
        _emit_line(stream, "connect walk (COI search):", prefix=prefix)
        if diagnostic is not None:
            a_scope, a_net = diagnostic.nearest_from_a
            b_scope, b_net = diagnostic.nearest_from_b
            _emit_line(
                stream,
                (
                    f"  nearest from A: {_net_label(a_scope, a_net)} "
                    f"({len(diagnostic.hops_from_a)} hop(s) shown)"
                ),
                prefix=prefix,
            )
            row_a = rows_by_path.get(a_scope)
            if row_a is not None:
                _emit_line(
                    stream,
                    f"    {format_hierarchy_row_line(row_a)}",
                    prefix=prefix,
                )
            _emit_line(
                stream,
                (
                    f"  nearest from B: {_net_label(b_scope, b_net)} "
                    f"({len(diagnostic.hops_from_b)} hop(s) shown)"
                ),
                prefix=prefix,
            )
            row_b = rows_by_path.get(b_scope)
            if row_b is not None:
                _emit_line(
                    stream,
                    f"    {format_hierarchy_row_line(row_b)}",
                    prefix=prefix,
                )
            _emit_hop_walk(
                stream,
                prefix=prefix,
                title="  walk from A (toward B):",
                hops=diagnostic.hops_from_a,
                rows_by_path=rows_by_path,
                indent="    ",
            )
            _emit_hop_walk(
                stream,
                prefix=prefix,
                title="  walk from B (toward A):",
                hops=diagnostic.hops_from_b,
                rows_by_path=rows_by_path,
                indent="    ",
            )
            if diagnostic.scopes_visited:
                _emit_line(
                    stream,
                    f"  scopes visited ({len(diagnostic.scopes_visited)}):",
                    prefix=prefix,
                )
                ordered = sorted(
                    diagnostic.scopes_visited,
                    key=lambda s: _scope_sort_key(s, rows_by_path),
                )
                for scope in ordered:
                    _emit_line(
                        stream,
                        f"    {format_scope_provenance_line(scope, rows_by_path)}",
                        prefix=prefix,
                    )
        elif hops:
            _emit_hop_walk(
                stream,
                prefix=prefix,
                title="  partial path evidence:",
                hops=hops,
                rows_by_path=rows_by_path,
                indent="    ",
            )
        else:
            _emit_line(
                stream,
                "  (no hop detail; enable connect_trace / connect_log for COI steps)",
                prefix=prefix,
            )

        for note in result.walk_notes:
            _emit_line(stream, f"  note: {note}", prefix=prefix)

        hop_scopes = collect_hop_scopes(hops)
        extra_scopes = [
            s
            for s in hop_scopes
            if s not in spine and (diagnostic is None or s not in diagnostic.scopes_visited)
        ]
        if extra_scopes:
            _emit_line(stream, "  extra scopes from hops:", prefix=prefix)
            for line in format_scopes_provenance_lines(
                extra_scopes,
                rows_by_path,
                indent="    ",
            ):
                _emit_line(stream, line, prefix=prefix)