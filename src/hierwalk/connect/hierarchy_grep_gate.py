"""Tier0 hierarchy_grep gate for text-conn (existence + scoped RTL)."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from hierwalk.connect.shared.endpoints import _port_exists, inst_leaf_exists_in_module
from hierwalk.connect.shared.expand import hierarchy_endpoint_specs, parse_list_display_spec
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.hierarchy_grep import HierarchyGrepSession, abs_rtl_path
from hierwalk.index import DesignIndex
from hierwalk.inst_scan import coarse_hierarchy_path, expand_inst_names
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow, InstanceEdge
from hierwalk.params import resolve_param_map

if TYPE_CHECKING:
    from hierwalk.connect.session import ConnectivitySession
    from hierwalk.path_walk import PathWalkState


_GATE_REPORT_LOCK = threading.Lock()
_GATE_REPORT_HEADERS: Set[str] = set()


def emit_hgrep_gate_log(log_line: str) -> None:
    """Always emit tier0 gate lines to stderr for captured connect logs."""
    if not log_line:
        return
    print(log_line, file=sys.stderr, flush=True)


def announce_hgrep_gate_report_path(
    report_path: Optional[str | Path],
    *,
    on_emit: Optional[Any] = None,
) -> None:
    """Log resolved gate report destination before connect-coi starts."""
    if report_path is not None:
        path = Path(report_path).expanduser().resolve()
        line = f"connect-pipeline hgrep-gate-report path={path}"
    else:
        line = "connect-pipeline hgrep-gate-report disabled"
    emit_hgrep_gate_log(line)
    if on_emit is not None:
        on_emit(line)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def resolve_hgrep_gate_report_path(
    *,
    connect_output_dir: Optional[Path] = None,
    connect_output_name: str = "conn.tsv",
) -> Optional[Path]:
    """Resolve per-run hierarchy_grep gate report path (or ``None`` when disabled)."""
    raw = os.environ.get("HIERWALK_HGREP_GATE_REPORT", "").strip()
    if raw.lower() in ("0", "false", "no", "off"):
        return None
    if raw:
        return Path(raw).expanduser()
    if connect_output_dir is None:
        return None
    from hierwalk.connect.pipeline.artifacts import connect_output_paths

    return connect_output_paths(connect_output_dir, connect_output_name).hgrep_gate_report


def _gate_report_payload(
    gate: "HierarchyGrepCheckGate",
    *,
    check_id: str,
    endpoint_a: Any,
    endpoint_b: Any,
    top: str,
) -> Dict[str, Any]:
    return {
        "resolved_at": _utc_now_iso(),
        "check_id": check_id,
        "top": top,
        "endpoint_a": str(endpoint_a),
        "endpoint_b": str(endpoint_b),
        "status": gate.status,
        "use_grep_fast_path": gate.use_grep_fast_path,
        "scoped_files": list(gate.scoped_files),
        "endpoints": [
            {
                "spec": g.spec,
                "hierarchy_input": g.hierarchy_input,
                "hierarchy": g.hierarchy,
                "port_tail": g.port_tail,
                "ok": g.ok,
                "ambiguous": g.ambiguous,
                "error": g.error,
                "scoped_files": list(g.scoped_files),
            }
            for g in gate.endpoint_gates
        ],
        "rows": [
            {
                "full_path": r.full_path,
                "module": r.module,
                "file": r.file,
                "parent_path": r.parent_path,
            }
            for r in gate.rows
        ],
    }


def format_hierarchy_grep_gate_report(
    gate: "HierarchyGrepCheckGate",
    *,
    check_id: str = "",
    endpoint_a: Any = "",
    endpoint_b: Any = "",
    top: str = "",
) -> str:
    """Human-readable per-check gate report with embedded JSON."""
    cid = check_id or "-"
    lines = [
        "Hierarchy grep gate report",
        f"  resolved_at: {_utc_now_iso()}",
        f"  check_id: {cid}",
        f"  top: {top}",
        f"  endpoint_a: {endpoint_a}",
        f"  endpoint_b: {endpoint_b}",
        f"  status: {gate.status}",
        f"  use_grep_fast_path: {gate.use_grep_fast_path}",
        f"  scoped_files: {len(gate.scoped_files)}",
    ]
    for fpath in gate.scoped_files:
        lines.append(f"    - {fpath}")
    lines.append("  endpoints:")
    for g in gate.endpoint_gates:
        flag = "OK" if g.ok else "MISS"
        amb = " ambiguous" if g.ambiguous else ""
        tail = f" port_tail={g.port_tail!r}" if g.port_tail else ""
        lines.append(
            f"    [{flag}{amb}] {g.spec!r} hier={g.hierarchy!r}{tail} "
            f"files={len(g.scoped_files)}"
        )
        if g.hierarchy_input != g.hierarchy:
            lines.append(f"         hierarchy_input={g.hierarchy_input!r}")
        if g.error:
            lines.append(f"         error={g.error}")
    if gate.rows:
        lines.append("  rows:")
        for row in gate.rows:
            lines.append(
                f"    {row.full_path} module={row.module!r} file={row.file}"
            )
    payload = _gate_report_payload(
        gate,
        check_id=cid,
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
        top=top,
    )
    lines.append("json:")
    lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def write_hierarchy_grep_gate_report(
    gate: "HierarchyGrepCheckGate",
    *,
    report_path: str | Path,
    check_id: str = "",
    endpoint_a: Any = "",
    endpoint_b: Any = "",
    top: str = "",
) -> Path:
    """Append one gate report block to *report_path* as soon as the check finishes."""
    path = Path(report_path).expanduser()
    text = format_hierarchy_grep_gate_report(
        gate,
        check_id=check_id,
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
        top=top,
    )
    with _GATE_REPORT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve())
        write_header = key not in _GATE_REPORT_HEADERS
        if write_header:
            _GATE_REPORT_HEADERS.add(key)
        with path.open("a", encoding="utf-8") as fh:
            if write_header:
                fh.write(
                    "Hierarchy grep gate batch report\n"
                    f"  started_at: {_utc_now_iso()}\n"
                    f"  report_path: {path}\n"
                    "\n"
                )
            fh.write(f"--- check {check_id or '-'} ---\n")
            fh.write(text)
            fh.write("\n\n")
    emit_hgrep_gate_log(f"hgrep-gate-report check={check_id or '-'} path={path}")
    return path


def hydrate_gate_scoped_rtl(
    state: "PathWalkState",
    *,
    scoped_files: Sequence[str],
    gate_rows: Sequence[FlatRow],
) -> None:
    """Parse gate-scoped RTL into pw-db/cache without seeding rows_by_path."""
    seen: Set[str] = set()
    for fpath in (*scoped_files, *(r.file for r in gate_rows if r.file)):
        key = abs_rtl_path(fpath)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            state.mod_db.tier1_scan_file(key)
        except Exception:
            pass
    for row in gate_rows:
        if row.module and row.file:
            state._cached_module_body(row)


def _inst_leaf_matches_name(inst_leaf: str, inst_name: str) -> bool:
    if inst_leaf == inst_name:
        return True
    prefix = inst_name + "."
    return inst_leaf.startswith(prefix) or inst_leaf.lower().startswith(prefix.lower())


def _match_inst_edge_for_leaf(
    index: DesignIndex,
    parent: FlatRow,
    inst_leaf: str,
) -> Optional[InstanceEdge]:
    """Pick the longest folded instance edge matching *inst_leaf* under *parent*."""
    best_name = ""
    best_edge: Optional[InstanceEdge] = None
    parent_ctx = parent.param_ctx or {}
    for edge in index.instances_for_walk(parent.module, parent_ctx):
        child_rec = index.get_module(edge.child_module)
        probe = resolve_param_map(
            child_rec.raw_params if child_rec else {},
            parent=parent_ctx,
        )
        for name in expand_inst_names(edge.inst_name, "", probe):
            if not _inst_leaf_matches_name(inst_leaf, name):
                continue
            if len(name) > len(best_name):
                best_name = name
                best_edge = edge
    return best_edge


def fold_gate_rows_with_param_ctx(
    gate_rows: Sequence[FlatRow],
    *,
    index: DesignIndex,
    top: str,
) -> Tuple[FlatRow, ...]:
    """Fold param_ctx along grep inst chains without path-walk tier0."""
    by_path = {r.full_path: r for r in gate_rows}
    ordered = sorted(gate_rows, key=lambda r: (r.depth, r.full_path))
    folded: Dict[str, FlatRow] = {}

    for row in ordered:
        if row.full_path == top:
            rec = index.get_module(top)
            pmap = resolve_param_map(rec.raw_params) if rec else {}
        else:
            parent_path = row.parent_path or ""
            parent = folded.get(parent_path) or by_path.get(parent_path)
            pmap: Dict[str, str] = {}
            if parent is not None:
                edge = _match_inst_edge_for_leaf(index, parent, row.inst_leaf)
                child_rec = index.get_module(row.module)
                if edge is not None and child_rec is not None:
                    pmap = dict(
                        resolve_param_map(
                            child_rec.raw_params,
                            overrides=edge.param_overrides,
                            parent=parent.param_ctx,
                        )
                    )
                elif child_rec is not None:
                    pmap = dict(resolve_param_map(child_rec.raw_params, parent=parent.param_ctx))
        stop = index.module_stop_reason(row.module)
        folded[row.full_path] = FlatRow(
            full_path=row.full_path,
            inst_leaf=row.inst_leaf,
            module=row.module,
            depth=row.depth,
            parent_path=row.parent_path,
            file=row.file,
            stop_reason=stop or row.stop_reason,
            via_filelist=row.via_filelist or index.filelist_for(row.file),
            filelist_chain=row.filelist_chain or index.filelist_chain_for(row.file),
            param_ctx=pmap,
            param_ctx_folded=True,
            refine_status=row.refine_status or "grep",
        )
    return tuple(folded[p] for p in sorted(folded))


def implicated_rtl_files(gate: "HierarchyGrepCheckGate") -> Tuple[str, ...]:
    """Absolute RTL paths grep implicated for this check (no filelist expansion)."""
    files = {abs_rtl_path(f) for f in gate.scoped_files if f}
    for row in gate.rows:
        if row.file:
            files.add(abs_rtl_path(row.file))
    return tuple(sorted(files))


def seed_gate_inst_chain(
    state: "PathWalkState",
    gate: "HierarchyGrepCheckGate",
    *,
    scoped_sources: Sequence[str],
) -> Tuple[FlatRow, ...]:
    """
    Tier1-hydrate grep-implicated RTL and seed folded inst rows for text COI.

    Does not call ``ensure_path`` or tier0 queue scan — only files from
    :func:`implicated_rtl_files` are tier1-scanned.
    """
    implicated = implicated_rtl_files(gate)
    hydrate_gate_scoped_rtl(
        state,
        scoped_files=implicated,
        gate_rows=gate.rows,
    )
    folded = fold_gate_rows_with_param_ctx(
        gate.rows,
        index=state.index,
        top=state.top,
    )
    state.ensure_root()
    seeded: List[FlatRow] = []
    for row in sorted(folded, key=lambda r: (r.depth, r.full_path)):
        state.rows_by_path[row.full_path] = row
        if row.parent_path:
            state._children_by_parent.setdefault(row.parent_path, set()).add(
                row.full_path
            )
        if row.module and row.file:
            try:
                state.mod_db._ensure_module_light(row.module, row.file)
            except Exception:
                pass
            state._cached_module_body(row)
        seeded.append(row)
    return tuple(seeded)


@dataclass(frozen=True)
class HierarchyGrepEndpointGate:
    spec: str
    hierarchy_input: str
    hierarchy: str
    port_tail: str
    ok: bool
    ambiguous: bool
    error: str
    scoped_files: Tuple[str, ...]
    rows: Tuple[FlatRow, ...]


@dataclass
class HierarchyGrepCheckGate:
    """Per-check tier0 gate outcome."""

    status: str  # pass | fallback
    log_line: str
    scoped_files: Tuple[str, ...] = ()
    rows: Tuple[FlatRow, ...] = ()
    endpoint_gates: Tuple[HierarchyGrepEndpointGate, ...] = ()
    fast_fail_result: Optional[ConnectResult] = None

    @property
    def use_grep_fast_path(self) -> bool:
        return self.status == "pass" and self.fast_fail_result is None


def prepare_hierarchy_grep_session(
    sources: Sequence[str],
    *,
    top: str,
) -> HierarchyGrepSession:
    """Build grep session once per text-conn batch."""
    paths = [abs_rtl_path(p) for p in sources if p]
    return HierarchyGrepSession.from_rtl_paths(
        paths,
        build_file_index_background=True,
    )


def _specs_for_gate(raw: Any) -> Tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        out: List[str] = []
        for item in raw:
            out.extend(_specs_for_gate(item))
        return tuple(out)
    text = str(raw or "").strip()
    if not text:
        return ()
    listed = parse_list_display_spec(text)
    if listed is not None:
        return tuple(listed)
    return hierarchy_endpoint_specs(text)


def _files_from_resolve(result: Mapping[str, Any]) -> Set[str]:
    files: Set[str] = set()
    for node in result.get("nodes", ()):
        for key in ("file", "hit_file", "child_decl_file"):
            val = node.get(key)
            if val:
                files.add(abs_rtl_path(val))
    return files


def _resolve_inst_chain_for_gate(
    spec: str,
    *,
    session: HierarchyGrepSession,
    top: str,
) -> Tuple[str, str, Mapping[str, Any]]:
    """
    Resolve instance hierarchy for tier0 gate (port/signal tail stripped).

    Returns ``(hierarchy_path, port_tail, resolve_result)``.
    """
    coarse = coarse_hierarchy_path(spec)
    probe = session.resolve(coarse, top=top)
    if not probe.get("ok"):
        return coarse, "", probe
    nodes = list(probe.get("nodes") or [])
    if nodes and nodes[-1].get("kind") in ("port", "signal") and "." in coarse:
        parts = coarse.split(".")
        hier = ".".join(parts[:-1])
        port_tail = parts[-1]
        inst = session.resolve(hier, top=top)
        if inst.get("ok"):
            return hier, port_tail, inst
    return coarse, "", probe


def flat_rows_from_resolve(
    result: Mapping[str, Any],
    *,
    index: DesignIndex,
) -> List[FlatRow]:
    """Synthesize minimal FlatRow chain from a grep resolve result."""
    nodes = result.get("nodes") or []
    if not nodes:
        return []
    rows: List[FlatRow] = []
    seen: Set[str] = set()
    path_parts: List[str] = []

    def _add(
        full_path: str,
        *,
        inst_leaf: str,
        module: str,
        parent: Optional[str],
        file_path: str,
    ) -> None:
        if full_path in seen or not file_path:
            return
        seen.add(full_path)
        depth = full_path.count(".")
        rows.append(
            FlatRow(
                full_path=full_path,
                inst_leaf=inst_leaf,
                module=module,
                depth=depth,
                parent_path=parent,
                file=abs_rtl_path(file_path),
                via_filelist=index.filelist_for(file_path),
                filelist_chain=index.filelist_chain_for(file_path),
                refine_status="grep",
            )
        )

    for i, node in enumerate(nodes):
        seg = str(node.get("segment", ""))
        if not seg:
            continue
        if i == 0:
            path_parts = [seg]
            _add(
                seg,
                inst_leaf=seg,
                module=str(node.get("module", seg)),
                parent=None,
                file_path=str(node.get("hit_file") or node.get("file") or ""),
            )
            continue

        path_parts.append(seg)
        full_path = ".".join(path_parts)
        role = node.get("role", "")
        if role == "genblk":
            continue
        if role == "inst":
            _add(
                full_path,
                inst_leaf=seg,
                module=str(node.get("child_module") or node.get("module", "")),
                parent=".".join(path_parts[:-1]),
                file_path=str(
                    node.get("child_decl_file")
                    or node.get("hit_file")
                    or node.get("file")
                    or ""
                ),
            )
            continue
        if role == "leaf" and node.get("kind") == "inst":
            _add(
                full_path,
                inst_leaf=seg,
                module=str(node.get("child_module") or node.get("module", "")),
                parent=".".join(path_parts[:-1]),
                file_path=str(
                    node.get("child_decl_file")
                    or node.get("hit_file")
                    or node.get("file")
                    or ""
                ),
            )

    return rows


def _row_body_for_gate_probe(
    index: DesignIndex,
    row: FlatRow,
    *,
    module_body_cache: Optional[Mapping[str, str]] = None,
) -> str:
    from hierwalk.connect.shared.endpoints import _resolve_row_module_body

    body = _resolve_row_module_body(
        index,
        row,
        module_body_cache=module_body_cache,
    )
    if not body.strip() and row.file and hasattr(index, "_source_text"):
        try:
            body = index._source_text(row.file, full=True)
        except Exception:
            body = ""
    return body


def _peel_grep_wire_tail(
    gate: HierarchyGrepEndpointGate,
    *,
    index: DesignIndex,
    scoped_files: Sequence[str] = (),
) -> HierarchyGrepEndpointGate:
    """When grep labels a module-local wire as an inst leaf, peel it as port_tail."""
    from hierwalk.connect.shared.endpoints import wire_tail_exists_fast

    if gate.port_tail or not gate.ok:
        return gate
    hier = gate.hierarchy
    if "." not in hier:
        return gate
    parent, leaf = hier.rsplit(".", 1)
    parent_row = next((r for r in gate.rows if r.full_path == parent), None)
    if parent_row is None:
        return gate
    body = _row_body_for_gate_probe(index, parent_row)
    if inst_leaf_exists_in_module(index, parent_row, leaf, body=body):
        return gate
    if not wire_tail_exists_fast(body, leaf) and not _port_exists(
        index,
        parent_row,
        leaf,
        top="",
        sources=scoped_files or None,
    ):
        return gate
    filtered_rows = tuple(r for r in gate.rows if r.full_path != hier)
    return HierarchyGrepEndpointGate(
        spec=gate.spec,
        hierarchy_input=gate.hierarchy_input,
        hierarchy=parent,
        port_tail=leaf,
        ok=gate.ok,
        ambiguous=gate.ambiguous,
        error=gate.error,
        scoped_files=gate.scoped_files,
        rows=filtered_rows,
    )


def _gate_endpoint(
    spec: str,
    *,
    session: HierarchyGrepSession,
    top: str,
    index: DesignIndex,
) -> HierarchyGrepEndpointGate:
    hier, port_tail, result = _resolve_inst_chain_for_gate(
        spec,
        session=session,
        top=top,
    )
    rows = tuple(flat_rows_from_resolve(result, index=index))
    return HierarchyGrepEndpointGate(
        spec=spec,
        hierarchy_input=str(result.get("hierarchy_input", spec)),
        hierarchy=str(result.get("hierarchy", hier)),
        port_tail=port_tail,
        ok=bool(result.get("ok")),
        ambiguous=bool(result.get("ambiguous")),
        error=str(result.get("error", "")),
        scoped_files=tuple(sorted(_files_from_resolve(result))),
        rows=rows,
    )


def _merge_rows(row_groups: Sequence[Sequence[FlatRow]]) -> Tuple[FlatRow, ...]:
    by_path: Dict[str, FlatRow] = {}
    for group in row_groups:
        for row in group:
            by_path[row.full_path] = row
    return tuple(by_path[k] for k in sorted(by_path))


def _inst_endpoints_need_walk(
    endpoint_gates: Sequence[HierarchyGrepEndpointGate],
    rows: Sequence[FlatRow],
    *,
    index: DesignIndex,
    scoped_files: Sequence[str] = (),
    module_body_cache: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when grep extended an endpoint past a real instance leaf."""
    from hierwalk.connect.shared.endpoints import (
        _resolve_row_module_body,
        wire_tail_exists_fast,
    )

    rows_by_path = {r.full_path: r for r in rows}
    for g in endpoint_gates:
        if g.port_tail:
            continue
        hier = g.hierarchy
        if "." not in hier:
            continue
        parent, leaf = hier.rsplit(".", 1)
        row = rows_by_path.get(parent)
        if row is None:
            return True
        if inst_leaf_exists_in_module(
            index,
            row,
            leaf,
            module_body_cache=module_body_cache,
        ):
            continue
        body = _row_body_for_gate_probe(
            index,
            row,
            module_body_cache=module_body_cache,
        )
        if wire_tail_exists_fast(body, leaf):
            continue
        if _port_exists(
            index,
            row,
            leaf,
            top="",
            sources=scoped_files or None,
            module_body_cache=module_body_cache,
        ):
            continue
        return True
    return False


def _fast_fail_result(
    chk: ConnectivityCheck,
    *,
    spec: str,
    gate: HierarchyGrepEndpointGate,
) -> ConnectResult:
    ep = ConnectEndpoint(
        spec=spec,
        inst_path=gate.hierarchy,
        port_name="",
        module="",
        port_found=False,
    )
    err = gate.error or f"hierarchy grep miss: {gate.hierarchy_input}"
    return ConnectResult(
        ep,
        ConnectEndpoint(spec=chk.endpoint_b, inst_path="", port_name="", module=""),
        False,
        "unknown",
        errors=[err],
        check_id=chk.check_id,
        note="tier0 hierarchy_grep miss",
    )


def gate_connect_check(
    chk: ConnectivityCheck,
    session: HierarchyGrepSession,
    *,
    top: str,
    index: DesignIndex,
    hard_fail_on_miss: bool = False,
    report_path: Optional[str | Path] = None,
) -> HierarchyGrepCheckGate:
    """
    Tier0 gate for one connect check.

    ``pass`` when every endpoint spec resolves cleanly (non-ambiguous).
    ``fallback`` triggers path-walk when grep is inconclusive.
    """
    def _finish(gate: HierarchyGrepCheckGate) -> HierarchyGrepCheckGate:
        emit_hgrep_gate_log(gate.log_line)
        if report_path is not None:
            write_hierarchy_grep_gate_report(
                gate,
                report_path=report_path,
                check_id=str(chk.check_id or ""),
                endpoint_a=chk.endpoint_a,
                endpoint_b=chk.endpoint_b,
                top=top,
            )
        return gate

    if chk.expand is not None:
        if getattr(chk.expand, "map_kind", "") == "waypoint-fanout":
            return _finish(
                HierarchyGrepCheckGate(
                    status="fallback",
                    log_line=(
                        f"hgrep-gate check={chk.check_id or '-'} status=fallback "
                        "reason=waypoint-fanout"
                    ),
                )
            )

    specs_a = _specs_for_gate(chk.endpoint_a)
    specs_b = _specs_for_gate(chk.endpoint_b)
    if not specs_a or not specs_b:
        return _finish(
            HierarchyGrepCheckGate(
                status="fallback",
                log_line=(
                    f"hgrep-gate check={chk.check_id or '-'} status=fallback "
                    "reason=empty-endpoint"
                ),
            )
        )

    raw_gates = tuple(
        _gate_endpoint(spec, session=session, top=top, index=index)
        for spec in (*specs_a, *specs_b)
    )
    peel_scoped = tuple(
        sorted({abs_rtl_path(f) for g in raw_gates for f in g.scoped_files if f})
    )
    gates = tuple(
        _peel_grep_wire_tail(g, index=index, scoped_files=peel_scoped)
        for g in raw_gates
    )

    scoped: Set[str] = set()
    row_groups: List[Sequence[FlatRow]] = []
    parts: List[str] = [
        f"hgrep-gate check={chk.check_id or '-'}",
        f"endpoints={len(gates)}",
    ]

    for g in gates:
        tail_note = f":port={g.port_tail!r}" if g.port_tail else ""
        parts.append(
            f"{g.spec!r}:ok={g.ok}:amb={g.ambiguous}:files={len(g.scoped_files)}"
            f":hier={g.hierarchy!r}{tail_note}"
        )
        scoped.update(g.scoped_files)
        if g.rows:
            row_groups.append(g.rows)

    if any(not g.ok for g in gates):
        if any(g.ambiguous for g in gates) and not hard_fail_on_miss:
            return _finish(
                HierarchyGrepCheckGate(
                    status="fallback",
                    log_line=" ".join(parts) + " status=fallback reason=grep-miss-ambiguous",
                    scoped_files=tuple(sorted(scoped)),
                    endpoint_gates=gates,
                )
            )
        miss = next(g for g in gates if not g.ok)
        return _finish(
            HierarchyGrepCheckGate(
                status="reject",
                log_line=" ".join(parts) + " status=reject reason=grep-miss",
                scoped_files=tuple(sorted(scoped)),
                endpoint_gates=gates,
                fast_fail_result=_fast_fail_result(
                    chk,
                    spec=miss.spec,
                    gate=miss,
                ),
            )
        )

    if any(g.ambiguous for g in gates):
        return _finish(
            HierarchyGrepCheckGate(
                status="fallback",
                log_line=" ".join(parts) + " status=fallback reason=ambiguous",
                scoped_files=tuple(sorted(scoped)),
                rows=_merge_rows(row_groups),
                endpoint_gates=gates,
            )
        )

    merged = _merge_rows(row_groups)
    if not merged:
        return _finish(
            HierarchyGrepCheckGate(
                status="fallback",
                log_line=" ".join(parts) + " status=fallback reason=no-rows",
                scoped_files=tuple(sorted(scoped)),
                endpoint_gates=gates,
            )
        )

    if _inst_endpoints_need_walk(
        gates,
        merged,
        index=index,
        scoped_files=tuple(sorted(scoped)),
    ):
        return _finish(
            HierarchyGrepCheckGate(
                status="fallback",
                log_line=" ".join(parts) + " status=fallback reason=inst-coverage",
                scoped_files=tuple(sorted(scoped)),
                rows=merged,
                endpoint_gates=gates,
            )
        )

    return _finish(
        HierarchyGrepCheckGate(
            status="pass",
            log_line=" ".join(parts) + f" status=pass scoped_files={len(scoped)}",
            scoped_files=tuple(sorted(scoped)),
            rows=merged,
            endpoint_gates=gates,
        )
    )


def scoped_sources_for_gate(
    gate: HierarchyGrepCheckGate,
    all_sources: Sequence[str],
    *,
    index: Optional[DesignIndex] = None,
) -> Sequence[str]:
    """
    Return grep-implicated RTL paths for text COI (tier0 gate survivor files).

    Only absolute paths recorded by hierarchy_grep resolve are opened — not the
    whole design or an entire filelist subtree.
    """
    del index  # kept for call-site compatibility
    if not gate.scoped_files:
        return all_sources
    allowed = {abs_rtl_path(s) for s in all_sources if s}
    scoped = {abs_rtl_path(f) for f in gate.scoped_files if f}
    if allowed:
        scoped &= allowed
    return tuple(sorted(scoped)) if scoped else all_sources


def text_check_from_gate(
    gate: HierarchyGrepCheckGate,
    chk: ConnectivityCheck,
    conn_session: "ConnectivitySession",
    *,
    trace: bool,
    dedup_cache: Dict[Tuple[Any, ...], ConnectResult],
    dedup_stats: List[int],
    dedup_lock: Optional[threading.Lock] = None,
    state: Optional["PathWalkState"] = None,
) -> Optional[ConnectResult]:
    """
    Tier0 gate orchestration for one text-conn check.

    ``reject`` → fast-fail; ``pass`` → inst-chain seed + text COI;
    ``fallback`` → ``None`` (caller runs full scoped path-walk).
    """
    if gate.fast_fail_result is not None:
        return gate.fast_fail_result
    if not gate.use_grep_fast_path:
        return None
    if state is None:
        return None

    from hierwalk.models import ElabIndex

    scoped_sources = scoped_sources_for_gate(
        gate,
        conn_session.sources or (),
        index=conn_session.index,
    )
    seeded_rows = seed_gate_inst_chain(
        state,
        gate,
        scoped_sources=scoped_sources,
    )
    worker_elab = ElabIndex.from_rows(list(seeded_rows))
    result = conn_session.text_check_entry(
        chk,
        trace=trace,
        dedup_cache=dedup_cache,
        dedup_stats=dedup_stats,
        dedup_lock=dedup_lock,
        rows=seeded_rows,
        elab_index=worker_elab,
        hgrep_gate_rows=seeded_rows,
        hgrep_scoped_sources=scoped_sources,
    )
    return result


def connect_result_from_hgrep_gate(
    chk: ConnectivityCheck,
    gate: HierarchyGrepCheckGate,
) -> ConnectResult:
    """Map a tier0 gate outcome to a connect TSV row (hgrep-only phase)."""
    if gate.fast_fail_result is not None:
        return gate.fast_fail_result
    ep_a = ConnectEndpoint(
        spec=str(chk.endpoint_a),
        inst_path="",
        port_name="",
        module="",
    )
    ep_b = ConnectEndpoint(
        spec=str(chk.endpoint_b),
        inst_path="",
        port_name="",
        module="",
    )
    ok = gate.status == "pass"
    return ConnectResult(
        ep_a,
        ep_b,
        ok,
        "hgrep",
        errors=() if ok else (f"hgrep-gate status={gate.status}",),
        check_id=chk.check_id,
        note=(
            f"hgrep-gate {gate.status}; "
            f"scoped_files={len(gate.scoped_files)}; "
            f"fast_path={gate.use_grep_fast_path}"
        ),
    )


def run_hgrep_connect_batch(
    request: ConnectivityRequest,
    sources: Sequence[str],
    *,
    top: str,
    connect_output_dir: Optional[Path] = None,
    connect_output_name: str = "conn.tsv",
    on_emit: Optional[Any] = None,
) -> Tuple[Any, DesignIndex]:
    """
    Run hierarchy_grep gate for every check — no path-walk, no connect-coi.

    Used when JSON sets ``connect_phase: hgrep``.
    """
    from hierwalk.connect.session import ConnectivityBatchResult

    top_name = (request.top or top or "").strip()
    if not top_name:
        raise ValueError("top module required for connect_phase=hgrep")
    paths = [abs_rtl_path(p) for p in sources if p]
    if not paths:
        raise ValueError("no RTL sources for connect_phase=hgrep")

    index = DesignIndex.build(
        {
            p: Path(p).read_text(encoding="utf-8", errors="ignore")
            for p in paths
        }
    )
    session = prepare_hierarchy_grep_session(paths, top=top_name)
    session.file_grep_index(wait=True)
    report_path = resolve_hgrep_gate_report_path(
        connect_output_dir=connect_output_dir,
        connect_output_name=connect_output_name,
    )
    announce_hgrep_gate_report_path(report_path, on_emit=on_emit)

    results: List[ConnectResult] = []
    for chk in request.checks:
        gate = gate_connect_check(
            chk,
            session,
            top=top_name,
            index=index,
            report_path=report_path,
        )
        if on_emit is not None:
            on_emit(gate.log_line)
        results.append(connect_result_from_hgrep_gate(chk, gate))

    if on_emit is not None:
        on_emit(
            f"connect-hgrep done checks={len(results)} "
            f"pass={sum(1 for r in results if r.connected)} "
            f"report={report_path}"
        )
    return (
        ConnectivityBatchResult(results=tuple(results), modules_cached=0),
        index,
    )