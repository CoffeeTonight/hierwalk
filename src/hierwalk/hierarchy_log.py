"""Hierarchy node provenance for stderr / error reports."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import IO, List, Mapping, Optional, Sequence, TextIO

from hierwalk.models import ConnectEndpoint, ConnectHop, FlatRow, InstanceEdge, ModuleRecord, PathChainLink
from hierwalk.progress import format_hierwalk_log

_PREFIX = "[hier-walk hierarchy]"
_PATH_WALK_PREFIX = "[hier-walk path-walk]"
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"
_PATH_WALK_MISS_REASON_STOP_TOKENS = (
    "; have:",
    "; pw-db ",
    "; hint:",
    ")  parent ",
    ")  (no parent",
)


def path_walk_stream_color_enabled(stream: TextIO) -> bool:
    """Color miss reasons on interactive terminals only (not log files)."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("HIERWALK_COLOR", "").lower() in {"0", "false", "no"}:
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


def colorize_path_walk_miss_reason(message: str, *, enable: bool = True) -> str:
    """Highlight the miss reason (``cause=…`` summary) in red ANSI when *enable*."""
    if not enable or "miss inst=" not in message or "cause=" not in message:
        return message
    open_paren = message.find("(cause=")
    if open_paren < 0:
        return message
    start = open_paren + 1
    end = -1
    for token in _PATH_WALK_MISS_REASON_STOP_TOKENS:
        pos = message.find(token, start)
        if pos >= 0 and (end < 0 or pos < end):
            end = pos
    if end < 0:
        end = message.find(")", start)
    if end < 0:
        return message
    return (
        message[:start]
        + _ANSI_RED
        + message[start:end]
        + _ANSI_RESET
        + message[end:]
    )


def resolve_absolute_rtl_path(file_path: str) -> str:
    """Normalize an RTL path string to an absolute filesystem path."""
    if not file_path:
        return ""
    try:
        return str(Path(file_path).expanduser().resolve())
    except (OSError, RuntimeError):
        return file_path


def provenance_fields(
    scope: str,
    rows_by_path: Mapping[str, FlatRow],
) -> dict[str, str]:
    """Machine-readable rtl + filelist fields for one hierarchy *scope*."""
    row = rows_by_path.get(scope) if scope else None
    if row is None:
        return {
            "module": "",
            "rtl": "",
            "via_filelist": "",
            "filelist_chain": "",
        }
    return {
        "module": row.module,
        "rtl": resolve_absolute_rtl_path(row.file or ""),
        "via_filelist": str(row.via_filelist or ""),
        "filelist_chain": str(row.filelist_chain or ""),
    }


def endpoint_provenance_fields(
    ep: ConnectEndpoint,
    rows_by_path: Mapping[str, FlatRow],
) -> dict[str, str]:
    """Provenance for a connect/io endpoint (inst path + optional port)."""
    base = provenance_fields(ep.inst_path, rows_by_path)
    if ep.port_name and not base["rtl"]:
        base["note"] = ep.spec
    elif ep.port_name:
        base["port"] = ep.port_name
    if ep.module and not base["module"]:
        base["module"] = ep.module
    return base


def format_row_provenance(row: FlatRow, *, compact: bool = False) -> str:
    """RTL file + filelist chain for one elaborated instance row."""
    parts = [f"module={row.module}"]
    if row.file:
        rtl = resolve_absolute_rtl_path(row.file) if not compact else Path(row.file).name
        parts.append(f"rtl={rtl}")
    if row.via_filelist:
        via = row.via_filelist if not compact else Path(row.via_filelist).name
        parts.append(f"via_filelist={via}")
    if row.filelist_chain:
        parts.append(f"filelist_chain={row.filelist_chain}")
    if row.stop_reason:
        parts.append(f"stop={row.stop_reason}")
    return "  ".join(parts)


def format_hierarchy_row_line(row: FlatRow) -> str:
    return f"{row.full_path}  {format_row_provenance(row)}"


def format_hierarchy_rows_report(
    rows: Sequence[FlatRow],
    *,
    limit: Optional[int] = None,
    title: str = "Hierarchy instances (rtl + filelist)",
) -> List[str]:
    lines = [title]
    shown = list(rows) if limit is None else list(rows[:limit])
    for row in shown:
        lines.append(f"  {format_hierarchy_row_line(row)}")
    if limit is not None and len(rows) > limit:
        lines.append(f"  ... {len(rows) - limit} more (see output TSV)")
    return lines


def emit_hierarchy_rows_log(
    rows: Sequence[FlatRow],
    *,
    stream: TextIO,
    limit: Optional[int] = 200,
    title: Optional[str] = None,
) -> None:
    if not rows:
        return
    head = title or f"{len(rows)} instance(s)"
    print(format_hierwalk_log(head, prefix=_PREFIX), file=stream, flush=True)
    shown = list(rows) if limit is None or len(rows) <= limit else list(rows[:limit])
    for row in shown:
        print(
            format_hierwalk_log(format_hierarchy_row_line(row), prefix=f"{_PREFIX}  "),
            file=stream,
            flush=True,
        )
    if limit is not None and len(rows) > limit:
        print(
            format_hierwalk_log(
                f"... {len(rows) - limit} more (see output TSV)",
                prefix=f"{_PREFIX}  ",
            ),
            file=stream,
            flush=True,
        )


def rows_lookup(rows: Sequence[FlatRow]) -> dict[str, FlatRow]:
    return {r.full_path: r for r in rows}


def format_endpoint_provenance_line(
    label: str,
    ep: ConnectEndpoint,
    rows_by_path: Mapping[str, FlatRow],
) -> str:
    """One endpoint with rtl + filelist when elaboration row exists."""
    row = rows_by_path.get(ep.inst_path) if ep.inst_path else None
    port_note = f"  port={ep.port_name}" if ep.port_name else ""
    if row is not None:
        return f"{label}: {format_hierarchy_row_line(row)}{port_note}"
    parts = [f"{label}: {ep.spec}"]
    if ep.module:
        parts.append(f"module={ep.module}")
    if ep.inst_path:
        parts.append(f"inst={ep.inst_path}")
    parts.append("(no elaboration row)")
    return "  ".join(parts)


def format_path_provenance_line(
    label: str,
    path: str,
    rows_by_path: Mapping[str, FlatRow],
) -> str:
    row = rows_by_path.get(path) if path else None
    if row is not None:
        return f"{label}: {format_hierarchy_row_line(row)}"
    return f"{label}: {path}  (no elaboration row)"


def emit_path_provenance_log(
    path: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    stream: TextIO,
    label: str = "origin",
    prefix: str = _PREFIX,
) -> None:
    if not path:
        return
    print(
        format_hierwalk_log(
            format_path_provenance_line(label, path, rows_by_path),
            prefix=prefix,
        ),
        file=stream,
        flush=True,
    )


def _lca(path_a: str, path_b: str) -> str:
    parts_a = path_a.split(".")
    parts_b = path_b.split(".")
    common: List[str] = []
    for a, b in zip(parts_a, parts_b):
        if a != b:
            break
        common.append(a)
    return ".".join(common)


def hierarchy_spine_between(path_a: str, path_b: str) -> List[str]:
    """Ordered instance paths from LCA down each endpoint branch."""
    if not path_a and not path_b:
        return []
    if not path_a:
        path_a = path_b
    if not path_b:
        path_b = path_a
    lca = _lca(path_a, path_b)
    lca_depth = len(lca.split(".")) if lca else 0
    nodes: List[str] = []
    seen: set[str] = set()

    def _append(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            nodes.append(path)

    for end in (path_a, path_b):
        parts = end.split(".")
        for depth in range(max(lca_depth, 1), len(parts) + 1):
            _append(".".join(parts[:depth]))
    return nodes


def scopes_from_hop_detail(detail: str) -> List[str]:
    """Extract hierarchy scopes from connect hop detail (scope:net labels)."""
    if not detail or detail == "structural COI path":
        return []
    head = detail.split(" (", 1)[0]
    scopes: List[str] = []
    seen: set[str] = set()
    for part in head.split(" -> "):
        part = part.strip()
        if not part:
            continue
        scope = part.rsplit(":", 1)[0] if ":" in part else part
        if scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    return scopes


def collect_hop_scopes(hops: Sequence[ConnectHop]) -> List[str]:
    """Ordered unique scopes referenced by connect hop details."""
    scopes: List[str] = []
    seen: set[str] = set()
    for hop in hops:
        for scope in scopes_from_hop_detail(hop.detail):
            if scope not in seen:
                seen.add(scope)
                scopes.append(scope)
    return scopes


def format_scope_provenance_line(
    scope: str,
    rows_by_path: Mapping[str, FlatRow],
) -> str:
    row = rows_by_path.get(scope)
    if row is not None:
        return format_hierarchy_row_line(row)
    return f"{scope}  (no elaboration row)"


def format_scopes_provenance_lines(
    scopes: Sequence[str],
    rows_by_path: Mapping[str, FlatRow],
    *,
    indent: str = "  ",
) -> List[str]:
    return [f"{indent}{format_scope_provenance_line(scope, rows_by_path)}" for scope in scopes]


def emit_scopes_provenance_log(
    scopes: Sequence[str],
    rows_by_path: Mapping[str, FlatRow],
    *,
    stream: TextIO,
    prefix: str,
    title: str,
    indent: str = "  ",
) -> None:
    if not scopes:
        return
    if title:
        print(format_hierwalk_log(title, prefix=f"{prefix}  "), file=stream, flush=True)
    for line in format_scopes_provenance_lines(scopes, rows_by_path, indent=indent):
        print(format_hierwalk_log(line, prefix=f"{prefix}  "), file=stream, flush=True)


def path_spine_prefixes(path: str) -> List[str]:
    """Ordered instance paths from root down to *path* (inclusive)."""
    parts = path.split(".")
    return [".".join(parts[: depth + 1]) for depth in range(len(parts))]


def format_path_walk_node_line(
    path: str,
    row: FlatRow,
    *,
    action: str = "ok",
) -> str:
    return f"{action} {path}  {format_row_provenance(row)}"


def classify_path_walk_inst_miss(
    *,
    parent_rec: Optional[ModuleRecord],
    miss_leaf: str,
    edges: Sequence[InstanceEdge],
    candidate_files: Sequence[str],
    raw_source_has_inst: bool = False,
) -> str:
    """Short cause tag for a missing child instance edge during path-walk."""
    miss_lower = miss_leaf.lower()
    for edge in edges:
        if edge.child_module.lower() == miss_lower:
            return "type-not-inst"
    for edge in edges:
        if edge.inst_name.startswith(miss_leaf + "[") or edge.inst_name.lower().startswith(
            miss_lower + "["
        ):
            return "array-index"
    if parent_rec is None:
        return "no-module"
    if parent_rec.stop_reason or parent_rec.is_blackbox:
        return "ignored"
    if not parent_rec.file_path:
        return "no-file"
    if not candidate_files:
        return "no-file"
    if len(candidate_files) > 1:
        return "dup-module"
    if raw_source_has_inst and not edges:
        return "ifdef-filtered"
    return "no-inst"


def classify_path_walk_child_miss(child_rec: Optional[ModuleRecord]) -> str:
    """Short cause tag when the child module type could not be elaborated."""
    if child_rec is None:
        return "no-module"
    if child_rec.stop_reason or child_rec.is_blackbox:
        return "ignored"
    if not child_rec.file_path:
        return "no-file"
    return "no-module"


def path_walk_inst_miss_reason(
    *,
    parent_mod: str,
    parent_rec: Optional[ModuleRecord],
    miss_leaf: str,
    edges: Sequence[InstanceEdge],
    candidate_files: Sequence[str],
    raw_source_has_inst: bool = False,
) -> str:
    """Human-readable miss line body with a leading ``cause=`` tag."""
    cause = classify_path_walk_inst_miss(
        parent_rec=parent_rec,
        miss_leaf=miss_leaf,
        edges=edges,
        candidate_files=candidate_files,
        raw_source_has_inst=raw_source_has_inst,
    )
    have = ""
    if edges:
        have = "; ".join(f"{e.inst_name}->{e.child_module}" for e in edges[:8])
    type_hint = ""
    miss_lower = miss_leaf.lower()
    if cause == "type-not-inst":
        for edge in edges:
            if edge.child_module.lower() == miss_lower:
                type_hint = (
                    f"; hint: {miss_leaf!r} is module type — "
                    f"use inst name {edge.inst_name!r} "
                    f"(path-walk uses instance names, not module types)"
                )
                break
    elif cause == "array-index":
        indexed = sorted(
            {
                e.inst_name
                for e in edges
                if e.inst_name.startswith(miss_leaf + "[")
                or e.inst_name.lower().startswith(miss_lower + "[")
            }
        )
        if indexed:
            type_hint = (
                f"; hint: {miss_leaf!r} is an array instance — "
                f"use indexed name e.g. {indexed[0]!r}"
            )
    elif cause == "ifdef-filtered":
        type_hint = (
            f"; hint: {miss_leaf!r} appears in parent RTL source but was removed "
            f"by `` `ifdef ``/`` `ifndef `` filtering — check filelist ``+define+`` "
            f"and whether the instance sits inside a gated block"
        )
    cand = "; ".join(Path(f).name for f in candidate_files[:8])
    parts = [f"cause={cause}"]
    if cause == "ignored" and parent_rec is not None:
        if parent_rec.stop_reason:
            parts.append(f"stop={parent_rec.stop_reason}")
        elif parent_rec.is_blackbox:
            parts.append("blackbox=1")
    parts.append("instance edge not found in parent module")
    if have:
        parts.append(f"have: {have}")
    reason = "; ".join(parts) + type_hint
    if parent_mod:
        reason += f"; pw-db {parent_mod} files: {cand or '(tier0 none)'}"
    return reason


def path_walk_child_miss_reason(
    *,
    child_mod: str,
    child_rec: Optional[ModuleRecord],
) -> str:
    cause = classify_path_walk_child_miss(child_rec)
    parts = [f"cause={cause}", f"child module {child_mod!r} not loaded"]
    if cause == "ignored" and child_rec is not None:
        if child_rec.stop_reason:
            parts.append(f"stop={child_rec.stop_reason}")
        elif child_rec.is_blackbox:
            parts.append("blackbox=1")
    elif cause == "no-file" and child_rec is not None and not child_rec.file_path:
        parts.append("rtl=(none)")
    return "; ".join(parts)


def format_path_walk_miss_line(
    parent_path: str,
    parent_row: FlatRow,
    inst_leaf: str,
    *,
    reason: str,
) -> str:
    return (
        f"miss inst={inst_leaf} under {parent_path} ({reason})  "
        f"parent {format_row_provenance(parent_row)}"
    )


def format_path_walk_spine_lines(
    path: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    indent: str = "  ",
) -> List[str]:
    lines: List[str] = []
    for prefix in path_spine_prefixes(path):
        row = rows_by_path.get(prefix)
        if row is None:
            lines.append(f"{indent}{prefix}  (no elaboration row)")
            break
        lines.append(f"{indent}{format_path_walk_node_line(prefix, row)}")
    return lines


def format_confident_defer_line(
    *,
    kind: str,
    module: str,
    inst_leaf: str,
    scope_anchor: str,
    child_filelist: str,
    reason: str,
    target_path: str = "",
) -> str:
    anchor_name = Path(scope_anchor).name if scope_anchor else "?"
    target = f" target={target_path}" if target_path else ""
    child_fl = f" child_fl={child_filelist}" if child_filelist else ""
    inst = f" inst={inst_leaf!r}" if inst_leaf else ""
    return (
        f"confident-miss defer kind={kind} module={module}{inst} "
        f"anchor={anchor_name}{child_fl} reason={reason}{target}"
    )


def format_signal_tail_line(
    *,
    hit: bool,
    kind: str,
    parent_path: str,
    tail: str,
    target_path: str,
    module: str,
    rtl_file: str,
    rtl_lines: int = 0,
    check_ms: float = 0.0,
) -> str:
    """Log line when an endpoint suffix is resolved as port/wire rather than instance."""
    status = "hit" if hit else "miss"
    rtl_name = Path(rtl_file).name if rtl_file else "?"
    target = f" target={target_path}" if target_path else ""
    timing = f" check_ms={check_ms:.1f}" if check_ms > 0 else ""
    lines_note = f" lines={rtl_lines}" if rtl_lines > 0 else ""
    return (
        f"signal-tail {status} kind={kind} scope={parent_path} tail={tail!r}"
        f"{target} module={module} rtl={rtl_name}{lines_note}{timing}"
    )


def path_walk_trace_show_message(message: str) -> bool:
    """
    Whether a path-walk trace line should be emitted.

    Search steps (tier0/tier1 scans, candidate tries, expands) are suppressed;
    resolved nodes and pw-db hits are kept. Miss lines are kept on failure.
    ``HIERWALK_PW_TRACE_VERBOSE=1`` shows all pw-db search steps.
    """
    from hierwalk.perf import pw_trace_verbose

    msg = message.strip()
    if not msg:
        return False
    if msg.startswith("signal-tail "):
        return True
    if msg.startswith("confident-miss "):
        return True
    if msg.startswith("recovery-pass "):
        return True
    if msg.startswith("pw-db heartbeat "):
        if "tier0=" in msg or "tier1=" in msg:
            return pw_trace_verbose()
        return True
    if msg.startswith("connect-pipeline "):
        return True
    if msg.startswith("walk target="):
        return False
    if msg.startswith("pw-db v"):
        return False
    if msg.startswith("pw-db "):
        if pw_trace_verbose():
            return True
        if " load failed " in msg:
            return True
        return " edge hit " in msg or msg.startswith("pw-db   hit ")
    return True


def write_path_walk_trace_section(
    fh: TextIO,
    *,
    phase: str = "",
) -> None:
    """Append a path-walk trace section header (does not open or close *fh*)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    phase_tag = f" ({phase})" if phase else ""
    fh.write(f"\n# path-walk trace{phase_tag} {stamp}\n")
    fh.flush()


def open_path_walk_trace_log(
    log_path: Path,
    *,
    phase: str = "",
) -> TextIO:
    """Append path-walk trace section to the run log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("a", encoding="utf-8")
    write_path_walk_trace_section(fh, phase=phase)
    return fh


def emit_path_walk_log(
    message: str,
    *,
    stream: TextIO,
    prefix: str = _PATH_WALK_PREFIX,
    color_miss: Optional[bool] = None,
) -> None:
    if not message:
        return
    text = message
    if color_miss is None:
        color_miss = path_walk_stream_color_enabled(stream)
    if color_miss:
        text = colorize_path_walk_miss_reason(text)
    print(format_hierwalk_log(text, prefix=prefix), file=stream, flush=True)


def emit_path_walk_node_log(
    path: str,
    row: FlatRow,
    *,
    stream: TextIO,
    action: str = "ok",
    prefix: str = _PATH_WALK_PREFIX,
) -> None:
    emit_path_walk_log(
        format_path_walk_node_line(path, row, action=action),
        stream=stream,
        prefix=prefix,
    )


def emit_path_walk_miss_log(
    parent_path: str,
    parent_row: FlatRow,
    inst_leaf: str,
    *,
    stream: TextIO,
    reason: str,
    prefix: str = _PATH_WALK_PREFIX,
) -> None:
    emit_path_walk_log(
        format_path_walk_miss_line(parent_path, parent_row, inst_leaf, reason=reason),
        stream=stream,
        prefix=prefix,
    )
    for line in format_path_walk_spine_lines(parent_path, {parent_path: parent_row}):
        print(format_hierwalk_log(line, prefix=prefix), file=stream, flush=True)


def emit_path_walk_spine_log(
    path: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    stream: TextIO,
    title: str = "spine",
    prefix: str = _PATH_WALK_PREFIX,
) -> None:
    if not path:
        return
    emit_path_walk_log(f"{title} -> {path}", stream=stream, prefix=prefix)
    for line in format_path_walk_spine_lines(path, rows_by_path):
        print(format_hierwalk_log(line, prefix=prefix), file=stream, flush=True)


def format_path_link_provenance(link: PathChainLink) -> str:
    parts: List[str] = []
    if link.rtl_file:
        parts.append(f"rtl={link.rtl_file}")
    if link.inst_decl_file and link.inst_decl_file != link.rtl_file:
        parts.append(f"decl_in={link.inst_decl_file}")
    if link.via_filelist:
        parts.append(f"via_filelist={link.via_filelist}")
    if link.filelist_chain:
        parts.append(f"filelist_chain={link.filelist_chain}")
    if link.inst_decl_via_filelist:
        parts.append(f"decl_via_filelist={link.inst_decl_via_filelist}")
    if link.inst_decl_filelist_chain:
        parts.append(f"decl_filelist_chain={link.inst_decl_filelist_chain}")
    return "  ".join(parts)