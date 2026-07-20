"""Batch connectivity: pyslangwalk hierarchy gate, then path-walk text-COI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.hierarchy_grep_gate import prepare_hierarchy_grep_session
from hierwalk.connect.shared.expand import (
    build_expand_meta,
    expand_check_to_pairs,
    needs_expansion,
)
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import ConnectivityBatchResult, ConnectivitySession
from hierwalk.filelist import FilelistResult
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.pyslang_walk import (
    PyslangWalkResult,
    PyslangWalkSession,
    flat_rows_from_pyslang_result,
)

LogFn = Optional[Callable[[str], None]]


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


def _hierarchy_fail(
    *,
    endpoint_a: str,
    endpoint_b: str,
    check_id: str,
    err_a: str,
    err_b: str,
    ok_a: bool,
    ok_b: bool,
    sub_id: str = "",
) -> ConnectResult:
    errors: List[str] = []
    if not ok_a:
        errors.append(f"a: {err_a}")
    if not ok_b:
        errors.append(f"b: {err_b}")
    cid = f"{check_id}:{sub_id}" if sub_id and check_id else (sub_id or check_id)
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
    text_coi: bool,
    state: Any = None,
) -> ConnectResult:
    ok_a, err_a, res_a = _endpoint_resolve(pw, endpoint_a, top)
    ok_b, err_b, res_b = _endpoint_resolve(pw, endpoint_b, top)
    if not (ok_a and ok_b):
        return _hierarchy_fail(
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            check_id=check_id,
            err_a=err_a,
            err_b=err_b,
            ok_a=ok_a,
            ok_b=ok_b,
            sub_id=sub_id,
        )

    cid = f"{check_id}:{sub_id}" if sub_id and check_id else (sub_id or check_id)
    rows = _merge_rows(res_a, res_b)
    scoped = _scoped_files(res_a, res_b)

    if not text_coi:
        return ConnectResult(
            ConnectEndpoint(endpoint_a, res_a.hierarchy, "", ""),
            ConnectEndpoint(endpoint_b, res_b.hierarchy, "", ""),
            True,
            "pyslangwalk",
            errors=[],
            check_id=cid,
            note=f"pyslangwalk hierarchy-ok files={len(scoped)}",
        )

    # Seed path-walk rows from pyslangwalk, then run text-COI.
    if state is not None:
        for row in rows:
            state.rows_by_path[row.full_path] = row
            if row.parent_path:
                state._children_by_parent.setdefault(row.parent_path, set()).add(
                    row.full_path
                )
        # Ensure full hierarchy for endpoint specs (fills any gaps)
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
    # Prefer scoped RTL; fall back to full sources on miss.
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
) -> Tuple[ConnectivityBatchResult, DesignIndex]:
    """
    1) pyslangwalk hierarchy (module-index + per-path pyslang)
    2) if both OK and text_coi: path-walk text-COI with DesignIndex when available
    """
    del connect_output_name
    top_name = (request.top or top or "").strip() or "top"
    work = Path(connect_output_dir).expanduser().resolve() if connect_output_dir else None
    defs = dict(defines or request.defines or {})

    def _log(msg: str) -> None:
        if on_emit is not None:
            on_emit(msg)

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

    for chk in request.checks:
        cid = chk.check_id or ""
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
                    text_coi=text_coi,
                    state=state,
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
                    "pyslangwalk+text" if text_coi else "pyslangwalk",
                    errors=errors[:8],
                    check_id=cid,
                    note=(
                        f"pyslangwalk expand pairs={len(sub_results)} "
                        f"pass={sum(1 for s in sub_results if s.connected)}"
                    ),
                    sub_results=tuple(sub_results),
                    connected_text=all_ok if text_coi else None,
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
            endpoint_a=str(chk.endpoint_a),
            endpoint_b=str(chk.endpoint_b),
            top=top_name,
            check_id=cid,
            dedup_cache=dedup_cache,
            dedup_stats=dedup_stats,
            text_coi=text_coi,
            state=state,
        )
        results.append(result)
        _log(
            f"pyslangwalk check={cid or '-'} "
            f"status={'pass' if result.connected else 'fail'} mode={result.mode}"
        )

    _log(
        f"connect-pyslangwalk done checks={len(results)} "
        f"pass={sum(1 for r in results if r.connected)} text_coi={text_coi}"
    )
    return ConnectivityBatchResult(results=tuple(results), modules_cached=0), index
