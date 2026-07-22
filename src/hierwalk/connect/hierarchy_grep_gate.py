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
from hierwalk.hierarchy_grep import (
    HierarchyGrepSession,
    abs_rtl_path,
    dump_grep_hie,
    grep_hie_sources_match,
    load_grep_hie,
    remove_grep_hie,
    resolve_grep_hie_path,
)
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
    """Emit tier0 gate lines to stderr with the standard hier-walk timestamp."""
    if not log_line:
        return
    from hierwalk.hierarchy_log import emit_path_walk_log

    emit_path_walk_log(log_line, stream=sys.stderr)


def _emit_hgrep_check_milestones(
    done: int,
    total: int,
    *,
    on_emit: Optional[Any] = None,
    state: Optional[Dict[str, int]] = None,
) -> None:
    """Emit check-resolution milestones at 0/25/50/75/100% (and final check)."""
    if total <= 0:
        return
    from hierwalk.hierarchy_grep import emit_hgrep_milestone

    pct = min(100, int(done * 100 / total))
    bucket = 100 if done >= total else (pct // 25) * 25
    seen = state if state is not None else {}
    key = f"pct:{bucket}"
    if seen.get(key):
        return
    if done == 0:
        emit_hgrep_milestone(
            "hierarchy-check-start",
            f"checks=0/{total} pct=0%",
            on_emit=on_emit,
        )
        seen[key] = 1
        return
    emit_hgrep_milestone(
        "hierarchy-check",
        f"checks={done}/{total} pct={pct}%",
        on_emit=on_emit,
    )
    seen[key] = 1


def _emit_hgrep_trace(log_line: str, *, on_emit: Optional[Any] = None) -> None:
    """stderr (timestamped) plus optional trace hook (log file / tests)."""
    if not log_line:
        return
    emit_hgrep_gate_log(log_line)
    if on_emit is not None:
        on_emit(log_line)


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
    _emit_hgrep_trace(line, on_emit=on_emit)


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
                "inst_leaf": r.inst_leaf,
                "module": r.module,
                "depth": r.depth,
                "file": r.file,
                "parent_path": r.parent_path,
                "refine_status": r.refine_status or "grep",
            }
            for r in gate.rows
        ],
    }


def _format_list_field(label: str, raw: Any) -> List[str]:
    """Pretty-print list/concat endpoint display for gate report files."""
    text = str(raw or "").strip()
    listed = parse_list_display_spec(text) if text else None
    if listed is not None and len(listed) > 1:
        out = [f"  {label}: list[{len(listed)}]"]
        for i, path in enumerate(listed):
            out.append(f"    [{i}] {path}")
        return out
    return [f"  {label}: {text or '-'}"]


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
    ]
    lines.extend(_format_list_field("endpoint_a", endpoint_a))
    lines.extend(_format_list_field("endpoint_b", endpoint_b))
    lines.extend(
        [
            f"  status: {gate.status}",
            f"  use_grep_fast_path: {gate.use_grep_fast_path}",
            f"  scoped_files: {len(gate.scoped_files)}",
        ]
    )
    for fpath in gate.scoped_files:
        lines.append(f"    - {fpath}")
    # Group endpoint gates by a/b when possible (specs order: a* then b*).
    lines.append("  endpoint hierarchy (per item):")
    specs_a = _specs_for_gate(endpoint_a)
    n_a = len(specs_a) if specs_a else 0
    for i, g in enumerate(gate.endpoint_gates):
        if n_a > 0 and i < n_a:
            side = f"a[{i}]" if n_a > 1 else "a"
        elif n_a > 0:
            bi = i - n_a
            side = f"b[{bi}]" if (len(gate.endpoint_gates) - n_a) > 1 else "b"
        else:
            side = f"[{i}]"
        flag = "PASS" if g.ok else "FAIL"
        amb = " ambiguous" if g.ambiguous else ""
        tail = f" port={g.port_tail}" if g.port_tail else ""
        lines.append(f"    {side:8} {flag}{amb}  {g.spec}{tail}")
        if g.hierarchy and g.hierarchy != g.spec:
            lines.append(f"             hier={g.hierarchy}")
        if g.error:
            lines.append(f"             reason: {g.error}")
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
    # Stream path-walk handoff JSON beside the human gate report.
    try:
        from hierwalk.connect.hgrep_pathwalk_handoff import (
            check_gate_to_handoff,
            resolve_pathwalk_handoff_path,
            upsert_pathwalk_handoff_check,
        )
        from hierwalk.connect.shared.request import ConnectivityCheck

        handoff_path = resolve_pathwalk_handoff_path(path)
        if handoff_path is not None:
            chk = ConnectivityCheck(
                str(endpoint_a),
                str(endpoint_b),
                check_id=str(check_id or ""),
            )
            upsert_pathwalk_handoff_check(
                handoff_path,
                check_gate_to_handoff(gate, chk, top=str(top or "")),
                top=str(top or ""),
            )
    except Exception:
        pass
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
    work_dir: Optional[Path] = None,
    cache_path: Optional[str | Path] = None,
    filelist: str | Path = "",
    index_cwd: Optional[str | Path] = None,
    paths_normalized: bool = False,
    refresh_cache: bool = False,
    on_emit: Optional[Any] = None,
) -> HierarchyGrepSession:
    """
    Build or load grep session once per text-conn / hgrep batch.

    When *work_dir* is set, persists ``grep_hie.json`` there. Reuses the cache
    on later runs unless *refresh_cache* (JSON ``refresh-cache`` / CLI
    ``--refresh-cache``) deletes it first.
    """
    from hierwalk.hierarchy_grep import emit_hgrep_milestone, normalize_rtl_paths

    paths = normalize_rtl_paths(sources, already_normalized=paths_normalized)
    emit_hgrep_milestone(
        "filelist-ready",
        f"sources={len(paths)} top={top or '-'}",
        on_emit=on_emit,
    )
    resolved_cache: Optional[Path] = None
    if cache_path is not None:
        resolved_cache = Path(cache_path).expanduser().resolve()
    elif work_dir is not None:
        resolved_cache = resolve_grep_hie_path(work_dir)

    if resolved_cache is not None:
        if refresh_cache and remove_grep_hie(resolved_cache):
            line = f"hgrep-cache clean path={resolved_cache}"
            _emit_hgrep_trace(line, on_emit=on_emit)
        if resolved_cache.is_file() and not refresh_cache:
            try:
                cached = load_grep_hie(resolved_cache)
                if grep_hie_sources_match(cached, paths):
                    line = f"hgrep-cache hit path={resolved_cache}"
                    _emit_hgrep_trace(line, on_emit=on_emit)
                    mod_index = cached.get("module_index") or {}
                    emit_hgrep_milestone(
                        "grep-hie-loaded",
                        (
                            f"from=cache modules={len(mod_index)} "
                            f"rtl_files={len(cached.get('rtl_paths', ()))} "
                            f"path={resolved_cache}"
                        ),
                        on_emit=on_emit,
                    )
                    return HierarchyGrepSession.from_grep_hie_cache(
                        cached,
                        cache_path=resolved_cache,
                    )
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    session = HierarchyGrepSession.from_rtl_paths(
        paths,
        paths_normalized=True,
        build_file_index_background=False,
        on_emit=on_emit,
    )
    if resolved_cache is not None:
        dump_grep_hie(
            session,
            resolved_cache,
            top=top,
            filelist=filelist,
            index_cwd=index_cwd,
        )
        file_index = session.file_grep_index(wait=False)
        emit_hgrep_milestone(
            "grep-hie-index-ready",
            (
                f"modules={len(session.module_index)} "
                f"rtl_files={len(session.rtl_paths)} "
                f"file_entries={len(file_index)}"
            ),
            on_emit=on_emit,
        )
        line = f"hgrep-cache write path={resolved_cache}"
        _emit_hgrep_trace(line, on_emit=on_emit)
        emit_hgrep_milestone(
            "grep-hie-saved",
            (
                f"path={resolved_cache} modules={len(session.module_index)} "
                f"rtl_files={len(session.rtl_paths)}"
            ),
            on_emit=on_emit,
        )
    return session


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

    if row.file:
        key = abs_rtl_path(row.file)
        if module_body_cache is not None:
            cached = module_body_cache.get(key)
            if cached is not None and cached.strip():
                return cached
        try:
            body = Path(key).read_text(encoding="utf-8", errors="ignore")
            if body.strip():
                if module_body_cache is not None:
                    module_body_cache[key] = body  # type: ignore[index]
                return body
        except OSError:
            pass
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
    module_body_cache: Optional[Mapping[str, str]] = None,
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
    body = _row_body_for_gate_probe(
        index,
        parent_row,
        module_body_cache=module_body_cache,
    )
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
    miss_side: str = "a",
    other_gate: Optional[HierarchyGrepEndpointGate] = None,
) -> ConnectResult:
    """Build a failed ConnectResult with the miss on the correct a/b side."""
    miss_ep = ConnectEndpoint(
        spec=spec,
        inst_path=(gate.hierarchy or gate.hierarchy_input or "").strip(),
        port_name=(gate.port_tail or "").strip(),
        module=(gate.rows[-1].module if gate.rows else ""),
        port_found=False,
    )
    err = gate.error or f"hierarchy grep miss: {gate.hierarchy_input}"
    if other_gate is not None:
        other_spec = (
            str(chk.endpoint_b) if miss_side == "a" else str(chk.endpoint_a)
        )
        other_ep = _endpoint_from_hgrep_gate(other_spec, other_gate)
    else:
        other_spec = (
            str(chk.endpoint_b) if miss_side == "a" else str(chk.endpoint_a)
        )
        other_ep = ConnectEndpoint(
            spec=other_spec,
            inst_path="",
            port_name="",
            module="",
            port_found=False,
        )
    if miss_side == "b":
        ep_a, ep_b = other_ep, miss_ep
    else:
        ep_a, ep_b = miss_ep, other_ep
    return ConnectResult(
        ep_a,
        ep_b,
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
    module_body_cache: Optional[Mapping[str, str]] = None,
) -> HierarchyGrepCheckGate:
    """
    Tier0 gate for one connect check.

    ``pass`` when every endpoint spec resolves cleanly (non-ambiguous).
    ``fallback`` triggers path-walk when grep is inconclusive.
    """
    def _finish(gate: HierarchyGrepCheckGate) -> HierarchyGrepCheckGate:
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
        _peel_grep_wire_tail(
            g,
            index=index,
            scoped_files=peel_scoped,
            module_body_cache=module_body_cache,
        )
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
        n_a = len(specs_a)
        gates_a = gates[:n_a]
        gates_b = gates[n_a:]
        # Prefer reporting the first failing side (a before b) but never
        # attach a b-side miss onto endpoint_a.
        if any(not g.ok for g in gates_a):
            miss = next(g for g in gates_a if not g.ok)
            miss_side = "a"
            other = next((g for g in gates_b if g.ok), gates_b[0] if gates_b else None)
        else:
            miss = next(g for g in gates_b if not g.ok)
            miss_side = "b"
            other = next((g for g in gates_a if g.ok), gates_a[0] if gates_a else None)
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
                    miss_side=miss_side,
                    other_gate=other,
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
        module_body_cache=module_body_cache,
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
    for_fast_path_only: bool = True,
) -> Sequence[str]:
    """
    Return grep-implicated RTL paths for text COI (tier0 gate survivor files).

    Only absolute paths recorded by hierarchy_grep resolve are opened — not the
    whole design or an entire filelist subtree.

    When *for_fast_path_only* is True (default) and the gate is not on the hgrep
    fast path (``fallback`` / incomplete resolve), return *all_sources* so full
    path-walk is not trapped on a partial hop scope.
    """
    del index  # kept for call-site compatibility
    if for_fast_path_only and not gate.use_grep_fast_path:
        return tuple(str(s) for s in all_sources if s)
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


def _endpoint_from_hgrep_gate(
    spec: str,
    eg: Optional[HierarchyGrepEndpointGate],
) -> ConnectEndpoint:
    """Build a ConnectEndpoint so hierarchy reports can show inst/port hit/miss."""
    raw = str(spec or "").strip()
    if eg is None:
        return ConnectEndpoint(
            spec=raw,
            inst_path="",
            port_name="",
            module="",
            port_found=False,
        )
    hier = (eg.hierarchy or eg.hierarchy_input or "").strip()
    port = (eg.port_tail or "").strip()
    module = ""
    if eg.rows:
        # Prefer the deepest inst row (leaf of hierarchy chain).
        module = str(eg.rows[-1].module or "").strip()
    return ConnectEndpoint(
        spec=raw,
        inst_path=hier,
        port_name=port,
        module=module,
        port_found=bool(eg.ok and port),
    )


def _split_endpoint_gates(
    chk: ConnectivityCheck,
    gates: Sequence[HierarchyGrepEndpointGate],
) -> Tuple[Tuple[HierarchyGrepEndpointGate, ...], Tuple[HierarchyGrepEndpointGate, ...]]:
    """Split flattened endpoint gates back into a-side / b-side groups."""
    specs_a = _specs_for_gate(chk.endpoint_a)
    n_a = len(specs_a)
    if n_a <= 0:
        return (), tuple(gates)
    return tuple(gates[:n_a]), tuple(gates[n_a:])


def _primary_gate(
    gates: Sequence[HierarchyGrepEndpointGate],
    *,
    prefer_ok: bool = True,
) -> Optional[HierarchyGrepEndpointGate]:
    if not gates:
        return None
    if prefer_ok:
        for g in gates:
            if g.ok:
                return g
    for g in gates:
        if not g.ok:
            return g
    return gates[0]


def _hgrep_endpoint_status_lines(
    side: str,
    gates: Sequence[HierarchyGrepEndpointGate],
) -> Tuple[List[str], List[str], List[ConnectResult]]:
    """
    Per-list-element status for human reports and aggregate errors.

    walk_notes use a stable prefix ``hgrep-ep`` so the report formatter can
    render multi-hierarchy lists without packing them into one ``[a, b, c]`` line.
    """
    notes: List[str] = []
    errors: List[str] = []
    subs: List[ConnectResult] = []
    multi = len(gates) > 1
    for i, g in enumerate(gates):
        label = f"{side}[{i}]" if multi else side
        ok = bool(g.ok)
        status = "PASS" if ok else "FAIL"
        why = (g.error or "").strip() or ("hierarchy miss" if not ok else "")
        if ok:
            detail = ""
            if g.port_tail:
                detail = f" hier={g.hierarchy} port={g.port_tail}"
            elif g.hierarchy:
                detail = f" hier={g.hierarchy}"
            note = f"hgrep-ep {label} {status} {g.spec}{detail}"
        else:
            note = f"hgrep-ep {label} {status} {g.spec} — {why}"
            errors.append(f"{label} {g.spec}: {why}")
        notes.append(note)
        ep = _endpoint_from_hgrep_gate(g.spec, g)
        subs.append(
            ConnectResult(
                ep,
                ep,
                ok,
                "hgrep",
                errors=() if ok else (why,),
                note=f"hgrep endpoint {label}",
                check_id=label,
                walk_notes=[note],
            )
        )
    return notes, errors, subs


def connect_result_from_hgrep_gate(
    chk: ConnectivityCheck,
    gate: HierarchyGrepCheckGate,
) -> ConnectResult:
    """Map a tier0 gate outcome to a connect TSV row (hgrep-only phase)."""
    gates_a, gates_b = _split_endpoint_gates(chk, gate.endpoint_gates or ())
    notes_a, errs_a, subs_a = _hgrep_endpoint_status_lines("a", gates_a)
    notes_b, errs_b, subs_b = _hgrep_endpoint_status_lines("b", gates_b)
    walk_notes = notes_a + notes_b
    all_errs = errs_a + errs_b

    # Parent endpoints keep display form (list brackets); provenance from
    # first OK gate when available, else first miss.
    eg_a = _primary_gate(gates_a, prefer_ok=True) or _primary_gate(
        gates_a, prefer_ok=False
    )
    eg_b = _primary_gate(gates_b, prefer_ok=True) or _primary_gate(
        gates_b, prefer_ok=False
    )
    ep_a = _endpoint_from_hgrep_gate(str(chk.endpoint_a), eg_a)
    ep_b = _endpoint_from_hgrep_gate(str(chk.endpoint_b), eg_b)
    # Keep original display specs (including ``[…]`` list form).
    if str(chk.endpoint_a).strip():
        ep_a = ConnectEndpoint(
            spec=str(chk.endpoint_a),
            inst_path=ep_a.inst_path,
            port_name=ep_a.port_name,
            module=ep_a.module,
            port_found=ep_a.port_found,
        )
    if str(chk.endpoint_b).strip():
        ep_b = ConnectEndpoint(
            spec=str(chk.endpoint_b),
            inst_path=ep_b.inst_path,
            port_name=ep_b.port_name,
            module=ep_b.module,
            port_found=ep_b.port_found,
        )

    ok = gate.status == "pass" and not all_errs
    if gate.fast_fail_result is not None and gate.fast_fail_result.errors:
        # Prefer concrete miss messages collected above; fall back to ff.
        if not all_errs:
            all_errs = list(gate.fast_fail_result.errors)

    n_fail = sum(1 for g in (*gates_a, *gates_b) if not g.ok)
    n_total = len(gates_a) + len(gates_b)
    if ok:
        note = (
            f"hgrep-gate pass; endpoints={n_total}; "
            f"scoped_files={len(gate.scoped_files)}; "
            f"fast_path={gate.use_grep_fast_path}"
        )
    else:
        note = (
            f"hgrep-gate fail; {n_fail}/{n_total} endpoint(s) miss; "
            f"scoped_files={len(gate.scoped_files)}"
        )

    # Per-endpoint detail lives in walk_notes / errors (human report). Avoid
    # sub_results self-pairs that would pollute flattened TSV as fake a→a rows.
    _ = (subs_a, subs_b)

    return ConnectResult(
        ep_a,
        ep_b,
        ok,
        "hgrep",
        errors=tuple(all_errs) if all_errs else (),
        note=note,
        check_id=chk.check_id,
        walk_notes=walk_notes,
        connected_text=ok,
    )


def run_hgrep_connect_batch(
    request: ConnectivityRequest,
    sources: Sequence[str],
    *,
    top: str,
    connect_output_dir: Optional[Path] = None,
    connect_output_name: str = "conn.tsv",
    refresh_cache: bool = False,
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

    _emit_hgrep_trace(
        "connect-hgrep begin "
        f"checks={len(request.checks)} sources={len(paths)} "
        "(no path-walk index; grep_hie gate only)",
        on_emit=on_emit,
    )
    index = DesignIndex({})
    module_body_cache: Dict[str, str] = {}
    session = prepare_hierarchy_grep_session(
        paths,
        top=top_name,
        work_dir=connect_output_dir,
        refresh_cache=refresh_cache,
        on_emit=on_emit,
    )
    report_path = resolve_hgrep_gate_report_path(
        connect_output_dir=connect_output_dir,
        connect_output_name=connect_output_name,
    )
    announce_hgrep_gate_report_path(report_path, on_emit=on_emit)

    results: List[ConnectResult] = []
    total_checks = len(request.checks)
    milestone_state: Dict[str, int] = {}
    rows_by_path: Dict[str, FlatRow] = {}
    _emit_hgrep_check_milestones(
        0,
        total_checks,
        on_emit=on_emit,
        state=milestone_state,
    )
    gates: List[HierarchyGrepCheckGate] = []
    for idx, chk in enumerate(request.checks, start=1):
        gate = gate_connect_check(
            chk,
            session,
            top=top_name,
            index=index,
            report_path=report_path,
            module_body_cache=module_body_cache,
        )
        gates.append(gate)
        for row in gate.rows or ():
            if row.full_path and row.full_path not in rows_by_path:
                rows_by_path[row.full_path] = row
        for eg in gate.endpoint_gates or ():
            for row in eg.rows or ():
                if row.full_path and row.full_path not in rows_by_path:
                    rows_by_path[row.full_path] = row
        _emit_hgrep_trace(gate.log_line, on_emit=on_emit)
        results.append(connect_result_from_hgrep_gate(chk, gate))
        _emit_hgrep_check_milestones(
            idx,
            total_checks,
            on_emit=on_emit,
            state=milestone_state,
        )

    try:
        from hierwalk.connect.hgrep_pathwalk_handoff import (
            resolve_pathwalk_handoff_path,
            write_handoff_from_gates,
        )

        handoff_path = resolve_pathwalk_handoff_path(
            report_path or connect_output_dir
        )
        if handoff_path is not None:
            write_handoff_from_gates(
                gates,
                request.checks,
                top=top_name,
                path=handoff_path,
                extra={
                    "grep_hie": str(
                        getattr(session, "cache_key", None)
                        or (connect_output_dir / "grep_hie.json" if connect_output_dir else "")
                    )
                },
            )
            _emit_hgrep_trace(
                f"hgrep-pathwalk-handoff path={handoff_path}",
                on_emit=on_emit,
            )
    except Exception as exc:
        _emit_hgrep_trace(
            f"hgrep-pathwalk-handoff write-skip err={exc!r}",
            on_emit=on_emit,
        )

    _emit_hgrep_trace(
        f"connect-hgrep done checks={len(results)} "
        f"pass={sum(1 for r in results if r.connected)} "
        f"report={report_path} hierarchy_rows={len(rows_by_path)}",
        on_emit=on_emit,
    )
    return (
        ConnectivityBatchResult(results=tuple(results), modules_cached=0),
        index,
        rows_by_path,
    )