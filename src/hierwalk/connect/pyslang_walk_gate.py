"""Batch connectivity: pyslangwalk hierarchy gate, then path-walk text-COI.

Hierarchy list endpoints use **AND** semantics (like hgrep): every expanded
path on side a and side b must resolve. Text-COI (when enabled) still uses
pair expansion for connectivity after hierarchy passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.hierarchy_grep_gate import prepare_hierarchy_grep_session
from hierwalk.connect.shared.expand import (
    build_expand_meta,
    expand_check_to_pairs,
    hierarchy_endpoint_specs,
    needs_expansion,
    parse_list_display_spec,
)
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import ConnectivityBatchResult, ConnectivitySession
from hierwalk.filelist import FilelistResult
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.connect.pyslang_electrical import (
    ElectricalP2PRow,
    build_electrical_graph,
    query_a_to_b,
    write_electrical_report,
)
from hierwalk.pyslang_walk import (
    PyslangWalkResult,
    PyslangWalkSession,
    flat_rows_from_pyslang_result,
)

LogFn = Optional[Callable[[str], None]]


def _modules_from_results(*results: PyslangWalkResult) -> List[str]:
    mods: List[str] = []
    seen = set()
    for res in results:
        if not res:
            continue
        for n in res.nodes or ():
            for m in (n.module, n.child_module):
                if m and m not in seen:
                    seen.add(m)
                    mods.append(m)
    return mods


def _a_fail_map(
    specs_a: Sequence[str],
    res_a: Sequence[PyslangWalkResult],
) -> Dict[str, Tuple[str, str]]:
    """Map a-spec → (fail_node, fail_rtl) for hierarchy misses."""
    out: Dict[str, Tuple[str, str]] = {}
    for spec, res in zip(specs_a, res_a):
        if res.ok:
            continue
        node = res.fail_segment or spec
        rtl = ""
        if res.nodes:
            rtl = res.nodes[-1].file or ""
        elif res.scoped_files:
            rtl = res.scoped_files[-1]
        out[spec] = (node, rtl)
    return out


def _endpoint_resolve(
    session: PyslangWalkSession, spec: str, top: str
) -> Tuple[bool, str, PyslangWalkResult]:
    res = session.resolve(spec, top=top)
    return res.ok, res.error, res


def _merge_rows(*results: PyslangWalkResult) -> List[FlatRow]:
    by_path: Dict[str, FlatRow] = {}
    for res in results:
        if not res or not res.ok:
            continue
        for row in flat_rows_from_pyslang_result(res):
            by_path.setdefault(row.full_path, row)
    return list(by_path.values())


def _scoped_files(*results: PyslangWalkResult) -> List[str]:
    files: set = set()
    for res in results:
        if res:
            files.update(res.scoped_files)
    return sorted(files)


def _side_specs(raw: object) -> Tuple[str, ...]:
    """Expand list/concat/scalar display into individual hierarchy paths."""
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return hierarchy_endpoint_specs(str(raw or ""))


def _is_list_display(raw: object) -> bool:
    """True when the endpoint was authored as a JSON list (or multi-path)."""
    if isinstance(raw, (list, tuple)):
        return True
    return parse_list_display_spec(str(raw or "")) is not None


def _resolve_side(
    pw: PyslangWalkSession,
    *,
    side: str,
    specs: Sequence[str],
    top: str,
    list_form: bool = False,
) -> Tuple[List[PyslangWalkResult], List[str], List[str]]:
    """
    Resolve every path on one side (AND).

    Returns (results, walk_notes, errors).
    List-form endpoints always use ``a[i]`` / ``b[i]`` labels so notes match
    the ``[path, …]`` display (even for a single-element list).
    """
    results: List[PyslangWalkResult] = []
    notes: List[str] = []
    errors: List[str] = []
    use_index = list_form or len(specs) > 1
    for i, spec in enumerate(specs):
        label = f"{side}[{i}]" if use_index else side
        ok, err, res = _endpoint_resolve(pw, spec, top)
        results.append(res)
        if ok:
            detail = ""
            if res.nodes:
                last = res.nodes[-1]
                if last.kind:
                    detail = f" hier={res.hierarchy} leaf={last.kind}:{last.segment}"
                else:
                    detail = f" hier={res.hierarchy}"
            notes.append(f"pyslang-ep {label} PASS {spec}{detail}")
        else:
            why = (err or res.error or "hierarchy miss").strip()
            notes.append(f"pyslang-ep {label} FAIL {spec} — {why}")
            errors.append(f"{label} {spec}: {why}")
    return results, notes, errors


def _hierarchy_and_gate(
    pw: PyslangWalkSession,
    *,
    endpoint_a: object,
    endpoint_b: object,
    top: str,
    check_id: str,
) -> Tuple[bool, List[str], List[str], List[PyslangWalkResult], List[PyslangWalkResult]]:
    """AND-resolve all list elements on a and b (hgrep-compatible)."""
    del check_id
    specs_a = _side_specs(endpoint_a)
    specs_b = _side_specs(endpoint_b)
    if not specs_a or not specs_b:
        return (
            False,
            [],
            ["empty endpoint a or b"],
            [],
            [],
        )
    res_a, notes_a, err_a = _resolve_side(
        pw,
        side="a",
        specs=specs_a,
        top=top,
        list_form=_is_list_display(endpoint_a),
    )
    res_b, notes_b, err_b = _resolve_side(
        pw,
        side="b",
        specs=specs_b,
        top=top,
        list_form=_is_list_display(endpoint_b),
    )
    notes = notes_a + notes_b
    errors = err_a + err_b
    ok = not errors and all(r.ok for r in res_a) and all(r.ok for r in res_b)
    return ok, notes, errors, res_a, res_b


def _pair_text(
    pw: PyslangWalkSession,
    conn: ConnectivitySession,
    *,
    endpoint_a: str,
    endpoint_b: str,
    top: str,
    check_id: str,
    sub_id: str = "",
    dedup_cache: Dict,
    dedup_stats: List[int],
    state: Any = None,
    seed_rows: Optional[Sequence[FlatRow]] = None,
    seed_scoped: Optional[Sequence[str]] = None,
) -> ConnectResult:
    """Text-COI for one endpoint pair after hierarchy already passed."""
    cid = f"{check_id}:{sub_id}" if sub_id and check_id else (sub_id or check_id)
    rows = list(seed_rows or [])
    scoped = list(seed_scoped or [])

    if not rows:
        ok_a, err_a, res_a = _endpoint_resolve(pw, endpoint_a, top)
        ok_b, err_b, res_b = _endpoint_resolve(pw, endpoint_b, top)
        if not (ok_a and ok_b):
            errors: List[str] = []
            if not ok_a:
                errors.append(f"a: {err_a}")
            if not ok_b:
                errors.append(f"b: {err_b}")
            return ConnectResult(
                ConnectEndpoint(endpoint_a, "", "", ""),
                ConnectEndpoint(endpoint_b, "", "", ""),
                False,
                "pyslangwalk",
                errors=errors,
                check_id=cid,
                note=f"pyslangwalk hierarchy-miss a_ok={ok_a} b_ok={ok_b}",
                connected_text=False,
            )
        rows = _merge_rows(res_a, res_b)
        scoped = _scoped_files(res_a, res_b)

    if state is not None:
        for row in rows:
            state.rows_by_path[row.full_path] = row
            if row.parent_path:
                state._children_by_parent.setdefault(row.parent_path, set()).add(
                    row.full_path
                )
        try:
            from hierwalk.path_walk import _walk_hierarchy_for_check
            from hierwalk.connect.shared.request import ConnectivityCheck as CC

            _walk_hierarchy_for_check(
                state,
                CC(endpoint_a, endpoint_b, check_id=cid),
                jobs=0,
                seen_lca=set(),
            )
            rows = list(state.rows_by_path.values())
        except Exception:
            pass

    chk = ConnectivityCheck(endpoint_a, endpoint_b, check_id=cid)
    text_res = conn.text_check_entry(
        chk,
        trace=False,
        dedup_cache=dedup_cache,
        dedup_stats=dedup_stats,
        rows=rows,
        hgrep_gate_rows=rows,
        hgrep_scoped_sources=scoped or list(conn.sources or ()),
    )
    if not text_res.connected and conn.sources:
        text_res = conn.text_check_entry(
            chk,
            trace=False,
            dedup_cache=dedup_cache,
            dedup_stats=dedup_stats,
            rows=rows if not state else list(state.rows_by_path.values()),
            hgrep_gate_rows=rows,
            hgrep_scoped_sources=list(conn.sources),
        )

    note = (
        f"pyslangwalk+text hier=ok conn={text_res.connected} "
        f"scoped={len(scoped)} {text_res.note or text_res.mode}"
    )
    return ConnectResult(
        text_res.endpoint_a,
        text_res.endpoint_b,
        text_res.connected,
        "pyslangwalk+text",
        hops=list(text_res.hops or []),
        errors=list(text_res.errors or []),
        note=note,
        check_id=cid,
        sub_results=text_res.sub_results,
        connected_text=bool(text_res.connected),
        walk_notes=list(text_res.walk_notes or []),
    )


def run_pyslangwalk_connect_batch(
    request: ConnectivityRequest,
    sources: Sequence[str],
    *,
    top: str = "",
    connect_output_dir: Optional[Path] = None,
    connect_output_name: str = "conn.tsv",
    refresh_cache: bool = False,
    on_emit: LogFn = None,
    text_coi: bool = True,
    filelist: Optional[FilelistResult] = None,
    defines: Optional[Dict[str, str]] = None,
    no_cache: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[ConnectivityBatchResult, DesignIndex, Dict[str, FlatRow]]:
    """
    1) pyslangwalk hierarchy — **AND** all list endpoints (like hgrep)
    2) if hierarchy OK and text_coi: pair-expand + text-COI

    Returns ``(batch, index, rows_by_path)`` where *rows_by_path* are hierarchy
    FlatRows useful for report/TSV provenance.
    """
    del connect_output_name
    top_name = (request.top or top or "").strip() or "top"
    work = Path(connect_output_dir).expanduser().resolve() if connect_output_dir else None
    defs = dict(defines or request.defines or {})

    def _log(msg: str) -> None:
        # Match hgrep: timestamped stderr + optional on_emit (trace file / progress).
        from hierwalk.connect.hierarchy_grep_gate import emit_hgrep_trace

        emit_hgrep_trace(msg, on_emit=on_emit)

    _log(
        f"connect-pyslangwalk begin checks={len(request.checks)} "
        f"sources={len(sources)} text_coi={text_coi}"
    )
    grep_session = prepare_hierarchy_grep_session(
        [str(p) for p in sources],
        top=top_name,
        work_dir=work,
        refresh_cache=refresh_cache,
        on_emit=on_emit,
    )
    if defs:
        grep_session.defines = dict(defs)

    pw = PyslangWalkSession.from_grep_session(grep_session, on_log=on_emit)

    state = None
    index: DesignIndex
    if text_coi and filelist is not None:
        from hierwalk.path_walk import PathWalkState, create_path_walk_index

        _log("pyslangwalk: building path-walk DesignIndex for text-COI")
        index, mod_db = create_path_walk_index(
            filelist,
            top_name,
            defines=defs,
            cache_dir=cache_dir or work,
            no_cache=no_cache,
            on_progress=on_emit,
        )
        state = PathWalkState(
            index=index,
            top=top_name,
            mod_db=mod_db,
        )
        try:
            state.ensure_root()
        except Exception:
            pass
    else:
        index = DesignIndex({})

    src_list = [str(p) for p in sources]
    conn = ConnectivitySession(
        rows=list(state.rows_by_path.values()) if state else [],
        index=index,
        top=top_name,
        defines=defs,
        sources=src_list,
        ff_barrier=not bool(request.include_ff),
    )

    dedup_cache: Dict = {}
    dedup_stats = [0, 0]
    results: List[ConnectResult] = []
    # Collect hierarchy FlatRows for report/TSV provenance (path_walk state seed).
    gate_rows: Dict[str, FlatRow] = {}
    electrical_rows: List[ElectricalP2PRow] = []
    # Cache electrical graphs by frozenset(files) within this batch.
    elec_cache: Dict[Tuple[str, ...], Any] = {}

    def _run_electrical(
        *,
        cid: str,
        specs_a: Sequence[str],
        specs_b: Sequence[str],
        res_a: Sequence[PyslangWalkResult],
        res_b: Sequence[PyslangWalkResult],
    ) -> List[ElectricalP2PRow]:
        mods = _modules_from_results(*res_a, *res_b)
        files: List[str] = []
        seen_f: set = set()
        for m in mods:
            for f in pw.lookup_module_files(m)[:1]:
                af = str(Path(f).resolve()) if f else ""
                if af and af not in seen_f and Path(af).is_file():
                    seen_f.add(af)
                    files.append(af)
        # Always include path-scoped files from resolve.
        for f in _scoped_files(*res_a, *res_b):
            af = str(Path(f).resolve()) if f else ""
            if af and af not in seen_f and Path(af).is_file():
                seen_f.add(af)
                files.append(af)
        key = tuple(sorted(files))
        if key not in elec_cache:
            uf, _diags = build_electrical_graph(
                files,
                top=top_name,
                defines=defs,
                on_log=on_emit,
            )
            elec_cache[key] = uf
        uf = elec_cache[key]
        return query_a_to_b(
            uf,
            check_id=cid,
            a_specs=specs_a,
            b_specs=specs_b,
            a_fail=_a_fail_map(specs_a, res_a),
        )

    for chk in request.checks:
        cid = chk.check_id or ""
        specs_a = _side_specs(chk.endpoint_a)
        specs_b = _side_specs(chk.endpoint_b)
        hier_ok, walk_notes, hier_errors, res_a, res_b = _hierarchy_and_gate(
            pw,
            endpoint_a=chk.endpoint_a,
            endpoint_b=chk.endpoint_b,
            top=top_name,
            check_id=cid,
        )
        n_ep = len(specs_a) + len(specs_b)
        seed_rows = _merge_rows(*res_a, *res_b)
        seed_scoped = _scoped_files(*res_a, *res_b)
        for row in seed_rows:
            gate_rows.setdefault(row.full_path, row)

        # Structural electrical p2p (always; hierarchy-fail a rows → FAIL).
        try:
            electrical_rows.extend(
                _run_electrical(
                    cid=cid,
                    specs_a=specs_a,
                    specs_b=specs_b,
                    res_a=res_a,
                    res_b=res_b,
                )
            )
        except Exception as exc:
            _log(f"pyslangwalk electrical error check={cid or '-'}: {exc}")
            for a in specs_a:
                electrical_rows.append(
                    ElectricalP2PRow(
                        check_id=cid,
                        a=a,
                        b_slice="",
                        status="FAIL",
                        fail_node=a,
                        fail_rtl="",
                        note=f"electrical error: {exc}",
                    )
                )

        if not hier_ok:
            n_fail = len(hier_errors)
            results.append(
                ConnectResult(
                    ConnectEndpoint(str(chk.endpoint_a), "", "", ""),
                    ConnectEndpoint(str(chk.endpoint_b), "", "", ""),
                    False,
                    "pyslangwalk",
                    errors=tuple(hier_errors[:12]),
                    check_id=cid,
                    note=(
                        f"pyslangwalk hierarchy-miss; "
                        f"{n_fail}/{n_ep} endpoint(s) fail"
                    ),
                    connected_text=False,
                    walk_notes=walk_notes,
                )
            )
            _log(
                f"pyslangwalk check={cid or '-'} status=fail mode=pyslangwalk "
                f"hier_fail={n_fail}/{n_ep}"
            )
            continue

        if not text_coi:
            results.append(
                ConnectResult(
                    ConnectEndpoint(str(chk.endpoint_a), "", "", ""),
                    ConnectEndpoint(str(chk.endpoint_b), "", "", ""),
                    True,
                    "pyslangwalk",
                    errors=[],
                    check_id=cid,
                    note=(
                        f"pyslangwalk hierarchy-ok endpoints={n_ep} "
                        f"files={len(seed_scoped)}"
                    ),
                    walk_notes=walk_notes,
                )
            )
            _log(
                f"pyslangwalk check={cid or '-'} status=pass mode=pyslangwalk "
                f"endpoints={n_ep}"
            )
            continue

        # Hierarchy OK → text-COI (pair expand for multi-list connectivity)
        expand = chk.expand
        if expand is None:
            auto = build_expand_meta(chk.endpoint_a, chk.endpoint_b)
            if needs_expansion(auto):
                expand = auto

        if expand is not None and needs_expansion(expand):
            try:
                pairs = expand_check_to_pairs(
                    str(chk.endpoint_a),
                    str(chk.endpoint_b),
                    check_id=cid,
                    expand=expand,
                )
            except ValueError as exc:
                results.append(
                    ConnectResult(
                        ConnectEndpoint(str(chk.endpoint_a), "", "", ""),
                        ConnectEndpoint(str(chk.endpoint_b), "", "", ""),
                        False,
                        "pyslangwalk",
                        errors=[str(exc)],
                        check_id=cid,
                        note="pyslangwalk expand unsupported",
                        connected_text=False,
                        walk_notes=walk_notes,
                    )
                )
                continue
            sub_results = [
                _pair_text(
                    pw,
                    conn,
                    endpoint_a=p.endpoint_a,
                    endpoint_b=p.endpoint_b,
                    top=top_name,
                    check_id=cid,
                    sub_id=p.sub_id or f"e{i}",
                    dedup_cache=dedup_cache,
                    dedup_stats=dedup_stats,
                    state=state,
                    seed_rows=seed_rows,
                    seed_scoped=seed_scoped,
                )
                for i, p in enumerate(pairs)
            ]
            all_ok = all(sr.connected for sr in sub_results)
            errors: List[str] = []
            for sr in sub_results:
                if not sr.connected:
                    errors.extend(sr.errors or [sr.note])
            results.append(
                ConnectResult(
                    ConnectEndpoint(str(chk.endpoint_a), "", "", ""),
                    ConnectEndpoint(str(chk.endpoint_b), "", "", ""),
                    all_ok,
                    "pyslangwalk+text",
                    errors=errors[:8],
                    check_id=cid,
                    note=(
                        f"pyslangwalk+text expand pairs={len(sub_results)} "
                        f"pass={sum(1 for s in sub_results if s.connected)}"
                    ),
                    sub_results=tuple(sub_results),
                    connected_text=all_ok,
                    walk_notes=walk_notes,
                )
            )
            _log(
                f"pyslangwalk check={cid or '-'} status="
                f"{'pass' if all_ok else 'fail'} expand_pairs={len(sub_results)}"
            )
            continue

        result = _pair_text(
            pw,
            conn,
            endpoint_a=specs_a[0],
            endpoint_b=specs_b[0],
            top=top_name,
            check_id=cid,
            dedup_cache=dedup_cache,
            dedup_stats=dedup_stats,
            state=state,
            seed_rows=seed_rows,
            seed_scoped=seed_scoped,
        )
        # Keep original display specs on the parent row.
        result = ConnectResult(
            ConnectEndpoint(
                str(chk.endpoint_a),
                result.endpoint_a.inst_path,
                result.endpoint_a.port_name,
                result.endpoint_a.module,
                result.endpoint_a.port_found,
            ),
            ConnectEndpoint(
                str(chk.endpoint_b),
                result.endpoint_b.inst_path,
                result.endpoint_b.port_name,
                result.endpoint_b.module,
                result.endpoint_b.port_found,
            ),
            result.connected,
            result.mode,
            hops=list(result.hops or []),
            errors=list(result.errors or []),
            note=result.note,
            check_id=cid,
            sub_results=result.sub_results,
            connected_text=result.connected_text,
            walk_notes=list(walk_notes) + list(result.walk_notes or []),
        )
        results.append(result)
        _log(
            f"pyslangwalk check={cid or '-'} "
            f"status={'pass' if result.connected else 'fail'} mode={result.mode}"
        )

    if state is not None:
        for path, row in gate_rows.items():
            state.rows_by_path.setdefault(path, row)
            if row.parent_path:
                state._children_by_parent.setdefault(row.parent_path, set()).add(
                    path
                )
        index = state.index  # type: ignore[assignment]
        for path, row in state.rows_by_path.items():
            gate_rows.setdefault(path, row)

    batch = ConnectivityBatchResult(results=tuple(results), modules_cached=0)

    # Always write structural electrical report beside the run db.
    if work is not None:
        report_path = work / "pyslangwalk.report"
        try:
            write_electrical_report(report_path, electrical_rows, top=top_name)
            n_pass = sum(1 for r in electrical_rows if r.status == "PASS")
            n_fail = sum(1 for r in electrical_rows if r.status == "FAIL")
            _log(
                f"pyslangwalk.report written path={report_path} "
                f"rows={len(electrical_rows)} pass={n_pass} fail={n_fail}"
            )
        except Exception as exc:
            _log(f"pyslangwalk.report write failed: {exc}")

    _log(
        f"connect-pyslangwalk done checks={len(results)} "
        f"pass={sum(1 for r in results if r.connected)} text_coi={text_coi}"
    )
    return batch, index, dict(gate_rows)
