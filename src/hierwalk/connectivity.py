"""Structural COI connectivity (public API and batch session)."""

from __future__ import annotations

import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, IO, List, Mapping, Optional, Sequence, Tuple, Union

from hierwalk.connect_endpoints import (
    _module_index,
    _port_param_ctx,
    _prune_rows_lca,
    parse_connect_endpoint,
    resolve_endpoint,
)
from hierwalk.connect_expand import (
    aggregate_connect_results,
    expand_check_to_pairs,
)
from hierwalk.connect_request import (
    ConnectivityCheck,
    ConnectivityRequest,
    load_connect_request,
    parse_connect_request_json,
)
from hierwalk.connect_scan import (
    ModuleConnectIndex,
    build_module_connect_index,
    collect_design_defines,
)
from hierwalk.connect_search import (
    _bidirectional_coi,
    _connect_note,
    _forward_coi_to_scope,
    _resolve_over_approximate_if,
)
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectHop, ConnectResult, ElabIndex, FlatRow

__all__ = [
    "ConnectivityBatchResult",
    "ConnectivitySession",
    "build_module_connect_index",
    "check_connectivity",
    "check_connectivity_batch",
    "emit_connect_trace_log",
    "flatten_connect_results",
    "format_connect_hop",
    "format_connect_result_row",
    "format_connect_results_report",
    "format_connect_results_tsv",
    "format_connect_trace_report",
    "load_connect_pairs",
    "parse_connect_endpoint",
    "parse_connect_pairs_json",
    "print_connect_trace_reports",
    "resolve_endpoint",
    "run_connectivity_request",
]

def format_connect_hop(hop: ConnectHop) -> str:
    return f"[{hop.kind}] {hop.detail}"


def format_connect_trace_report(
    result: ConnectResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> str:
    """Multi-line evidence report for a connectivity result."""
    from hierwalk.hierarchy_log import (
        format_endpoint_provenance_line,
        format_scopes_provenance_lines,
        hierarchy_spine_between,
        scopes_from_hop_detail,
    )

    lines = [
        f"check: {result.endpoint_a.spec} -> {result.endpoint_b.spec}",
        f"connected: {result.connected}  mode: {result.mode}  note: {result.note}",
    ]
    if rows_by_path is not None:
        lines.append(format_endpoint_provenance_line("A", result.endpoint_a, rows_by_path))
        lines.append(format_endpoint_provenance_line("B", result.endpoint_b, rows_by_path))
    if result.errors:
        lines.append("errors:")
        lines.extend(f"  - {e}" for e in result.errors)
    if result.connected and rows_by_path is not None:
        spine = hierarchy_spine_between(
            result.endpoint_a.inst_path,
            result.endpoint_b.inst_path,
        )
        if spine:
            lines.append("path hierarchy (rtl + filelist):")
            lines.extend(
                format_scopes_provenance_lines(spine, rows_by_path, indent="  ")
            )
    if result.connected and result.hops:
        lines.append("path evidence:")
        for i, hop in enumerate(result.hops, 1):
            lines.append(f"  {i}. {format_connect_hop(hop)}")
            if rows_by_path is not None:
                hop_scopes = scopes_from_hop_detail(hop.detail)
                if hop_scopes:
                    lines.extend(
                        format_scopes_provenance_lines(
                            hop_scopes,
                            rows_by_path,
                            indent="    ",
                        )
                    )
    elif result.connected:
        lines.append("path evidence: (no hop detail; enable connect_trace / connect_log)")
    return "\n".join(lines) + "\n"


def emit_connect_trace_log(
    result: ConnectResult,
    *,
    stream: IO[str] = sys.stderr,
    check_prefix: str = "",
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> None:
    """Emit endpoint provenance and path evidence for one connect result."""
    if result.sub_results:
        for sub in result.sub_results:
            emit_connect_trace_log(
                sub,
                stream=stream,
                check_prefix=sub.check_id or check_prefix,
                rows_by_path=rows_by_path,
            )
        return
    from hierwalk.hierarchy_log import (
        emit_scopes_provenance_log,
        format_endpoint_provenance_line,
        hierarchy_spine_between,
        scopes_from_hop_detail,
    )

    prefix = "[hier-walk connect]"
    if check_prefix:
        prefix = f"{prefix} [{check_prefix}]"
    header = f"{prefix} {result.endpoint_a.spec} -> {result.endpoint_b.spec}"
    print(header, file=stream, flush=True)
    if rows_by_path is not None:
        print(
            f"{prefix}   {format_endpoint_provenance_line('A', result.endpoint_a, rows_by_path)}",
            file=stream,
            flush=True,
        )
        print(
            f"{prefix}   {format_endpoint_provenance_line('B', result.endpoint_b, rows_by_path)}",
            file=stream,
            flush=True,
        )
    if result.errors:
        for err in result.errors:
            print(f"{prefix}   error: {err}", file=stream, flush=True)
    if not result.connected:
        print(f"{prefix}   not connected ({result.note})", file=stream, flush=True)
        return
    print(
        f"{prefix}   connected: {result.connected}  mode: {result.mode}  note: {result.note}",
        file=stream,
        flush=True,
    )
    if rows_by_path is not None:
        spine = hierarchy_spine_between(
            result.endpoint_a.inst_path,
            result.endpoint_b.inst_path,
        )
        emit_scopes_provenance_log(
            spine,
            rows_by_path,
            stream=stream,
            prefix=prefix,
            title="path hierarchy (rtl + filelist):",
            indent="    ",
        )
    if not result.hops:
        print(f"{prefix}   path evidence: (no hop detail; use connect_trace)", file=stream, flush=True)
        return
    for i, hop in enumerate(result.hops, 1):
        print(f"{prefix}   {i}. {format_connect_hop(hop)}", file=stream, flush=True)
        if rows_by_path is not None:
            hop_scopes = scopes_from_hop_detail(hop.detail)
            emit_scopes_provenance_log(
                hop_scopes,
                rows_by_path,
                stream=stream,
                prefix=prefix,
                title="",
                indent="      ",
            )


def format_connect_results_report(
    results: Sequence[ConnectResult],
    *,
    phase: str = "logical",
) -> List[str]:
    """Compact per-check lines for the end-of-run report (always lists endpoints)."""
    leaf_results = flatten_connect_results(results)
    if not leaf_results and results:
        leaf_results = [
            r
            for r in results
            if r.endpoint_a.spec or r.endpoint_b.spec or r.check_id
        ]
    if not leaf_results and results:
        leaf_results = list(results)
    if not leaf_results:
        return ["  (no checks)"]
    phase_label = str(phase).strip().lower() or "logical"
    lines: List[str] = []
    for result in leaf_results:
        cid = f" [{result.check_id}]" if result.check_id else ""
        pair = f"{result.endpoint_a.spec} -> {result.endpoint_b.spec}"
        text_ok = _connected_text_value(result)
        logical_ok = _connected_logical_value(result)
        if phase_label == "text":
            status = "PASS" if text_ok else "FAIL"
            lines.append(f"  {status}{cid} {pair}")
        elif phase_label == "logical":
            status = "PASS" if logical_ok else "FAIL"
            lines.append(f"  {status}{cid} {pair}")
        else:
            text_s = "PASS" if text_ok else "FAIL"
            logical_s = "PASS" if logical_ok else "FAIL"
            lines.append(f"  text={text_s} logical={logical_s}{cid} {pair}")
        if result.errors:
            err = " | ".join(result.errors)
            lines.append(f"         errors: {err}")
        elif result.note and not (text_ok if phase_label == "text" else logical_ok):
            lines.append(f"         note: {result.note}")
    return lines


def flatten_connect_results(
    results: Sequence[ConnectResult],
) -> List[ConnectResult]:
    """Expand aggregate checks into per-bit / per-index leaf rows for output."""
    out: List[ConnectResult] = []
    for result in results:
        if result.waypoint_events:
            out.append(result)
        elif result.sub_results:
            out.extend(flatten_connect_results(result.sub_results))
        else:
            out.append(result)
    return out


def print_connect_trace_reports(
    results: Sequence[ConnectResult],
    *,
    stream: IO[str],
    title: str = "connectivity path evidence",
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> None:
    """Print human-readable path-evidence blocks for terminal or log file."""
    leaf_results = flatten_connect_results(results)
    if not leaf_results:
        return
    print(f"\n--- {title} ---", file=stream, flush=True)
    for result in leaf_results:
        if result.check_id:
            print(f"# check_id: {result.check_id}", file=stream, flush=True)
        print(
            format_connect_trace_report(result, rows_by_path=rows_by_path),
            end="",
            file=stream,
            flush=True,
        )


def _effective_defines(
    index: DesignIndex,
    defines: Mapping[str, str] | None,
) -> Dict[str, str]:
    return {**collect_design_defines(index), **dict(defines or {})}


def _resolve_connect_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def _connect_pair(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    effective_defines: Mapping[str, str],
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = True,
    over_approximate_if: Optional[bool] = None,
    mod_cache: Dict[Tuple[str, str, str, str, str, bool, bool], ModuleConnectIndex],
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str = "",
    elab_index: Optional[ElabIndex] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> ConnectResult:
    lookup = (
        rows_by_path
        if rows_by_path is not None
        else (elab_index.rows_by_path if elab_index is not None else None)
    )
    ep_a, err_a = resolve_endpoint(
        endpoint_a,
        rows,
        index,
        top=top,
        require_port=False,
        rows_by_path=lookup,
    )
    ep_b, err_b = resolve_endpoint(
        endpoint_b,
        rows,
        index,
        top=top,
        require_port=False,
        rows_by_path=lookup,
    )
    errors = list(err_a) + list(err_b)

    if errors:
        mode = _mode(ep_a, ep_b) if ep_a.module and ep_b.module else "unknown"
        return ConnectResult(
            ep_a,
            ep_b,
            False,
            mode,
            errors=errors,
            check_id=check_id,
        )

    if _has_port(ep_a) and not ep_a.port_found:
        return ConnectResult(
            ep_a, ep_b, False, _mode(ep_a, ep_b), errors=errors, check_id=check_id
        )
    if _has_port(ep_b) and not ep_b.port_found:
        return ConnectResult(
            ep_a, ep_b, False, _mode(ep_a, ep_b), errors=errors, check_id=check_id
        )

    pruned = _prune_rows_lca(rows, ep_a.inst_path, ep_b.inst_path)
    mode = _mode(ep_a, ep_b)

    if mode == "port-port":
        start = (ep_a.inst_path, ep_a.port_name or "")
        goal = (ep_b.inst_path, ep_b.port_name or "")
        ok, hops, mod_n = _bidirectional_coi(
            start,
            goal,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            ff_barrier=ff_barrier,
            over_approximate_if=over_approximate_if,
            mod_cache=mod_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
        )
        return ConnectResult(
            ep_a,
            ep_b,
            ok,
            mode,
            hops=hops,
            errors=errors,
            note=_connect_note(ok, mod_n),
            check_id=check_id,
        )

    if mode == "port-hierarchy":
        port_ep = ep_a if _has_port(ep_a) else ep_b
        hier_ep = ep_b if _has_port(ep_a) else ep_a
        start = (port_ep.inst_path, port_ep.port_name or "")
        ok, hops, mod_n = _forward_coi_to_scope(
            start,
            hier_ep.inst_path,
            rows=pruned,
            index=index,
            top=top,
            defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            ff_barrier=ff_barrier,
            over_approximate_if=over_approximate_if,
            mod_cache=mod_cache,
            param_ctx_cache=param_ctx_cache,
            elab_index=elab_index,
        )
        return ConnectResult(
            ep_a,
            ep_b,
            ok,
            mode,
            hops=hops,
            errors=errors,
            note=_connect_note(ok, mod_n, hier=True),
            check_id=check_id,
        )

    return ConnectResult(
        ep_a,
        ep_b,
        ep_a.inst_path == ep_b.inst_path
        or _is_ancestor(ep_a.inst_path, ep_b.inst_path)
        or _is_ancestor(ep_b.inst_path, ep_a.inst_path),
        "hierarchy-hierarchy",
        errors=errors,
        note="same or ancestor/descendant (no port trace)",
        check_id=check_id,
    )


@dataclass
class ConnectivitySession:
    """
    Reusable connectivity checker for many endpoint pairs.

    ``mod_cache`` and ``param_ctx_cache`` persist across ``check`` / ``check_many``
    so repeated queries through the same RTL modules (e.g. array fan-out) avoid
    rebuilding ``ModuleConnectIndex`` graphs.
    """

    rows: Sequence[FlatRow]
    index: DesignIndex
    top: str = ""
    defines: Mapping[str, str] = field(default_factory=dict)
    strict_generate: bool = False
    ff_barrier: bool = True
    over_approximate_if: Optional[bool] = None
    mod_cache: Dict[Tuple[str, str, str, str, str, bool, bool], ModuleConnectIndex] = field(
        default_factory=dict
    )
    param_ctx_cache: Dict[str, Mapping[str, str]] = field(default_factory=dict)
    elab_index: Optional[ElabIndex] = None

    def __post_init__(self) -> None:
        if self.elab_index is None and self.rows:
            self.elab_index = ElabIndex.from_rows(self.rows)
        if not self.top and self.rows:
            self.top = self.rows[0].full_path.split(".", 1)[0]
        self._effective_defines = _effective_defines(self.index, self.defines)

    @property
    def rows_by_path(self) -> Dict[str, FlatRow]:
        if self.elab_index is not None:
            return self.elab_index.rows_by_path
        return {r.full_path: r for r in self.rows}

    @property
    def modules_cached(self) -> int:
        return len(self.mod_cache)

    def clear_cache(self) -> None:
        self.mod_cache.clear()
        self.param_ctx_cache.clear()

    def check(
        self,
        endpoint_a: str,
        endpoint_b: str,
        *,
        trace: bool = False,
        check_id: str = "",
        expand: Optional[Any] = None,
    ) -> ConnectResult:
        t0 = time.perf_counter()
        if expand is not None and expand.map_kind == "waypoint-fanout":
            from hierwalk.waypoint_fanout import run_waypoint_fanout_check

            result, _events = run_waypoint_fanout_check(
                list(expand.elements_a),
                list(expand.elements_b),
                rows=self.rows,
                index=self.index,
                top=self.top,
                path_kind=expand.path_kinds,
                direction=getattr(expand, "direction", "fanout"),
                defines=self._effective_defines,
                over_approximate_if=self.over_approximate_if
                if self.over_approximate_if is not None
                else True,
                check_id=check_id,
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                connect_trace=trace,
                trace_interior=getattr(expand, "trace_interior", False),
                full_path_kinds=getattr(expand, "full_path_kinds", False),
                elab_index=self.elab_index,
                comb_cache=self.mod_cache if self.ff_barrier else None,
            )
            from hierwalk.verification_timing import record_connect_check

            record_connect_check(
                check_id=check_id,
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                elapsed_sec=time.perf_counter() - t0,
            )
            return result
        pairs = expand_check_to_pairs(
            endpoint_a,
            endpoint_b,
            check_id=check_id,
            expand=expand,
        )
        if len(pairs) == 1 and not pairs[0].sub_id:
            result = _connect_pair(
                pairs[0].endpoint_a,
                pairs[0].endpoint_b,
                rows=self.rows,
                index=self.index,
                top=self.top,
                effective_defines=self._effective_defines,
                trace=trace,
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                check_id=check_id,
                elab_index=self.elab_index,
                rows_by_path=self.rows_by_path,
            )
        else:
            fanout_mode = expand.fanout_mode if expand is not None else "all"
            sub_results: List[ConnectResult] = []
            for pair in pairs:
                sub_id = f"{check_id}{pair.sub_id}" if check_id else pair.sub_id.strip("[]->")
                sub_results.append(
                    _connect_pair(
                        pair.endpoint_a,
                        pair.endpoint_b,
                        rows=self.rows,
                        index=self.index,
                        top=self.top,
                        effective_defines=self._effective_defines,
                        trace=trace,
                        strict_generate=self.strict_generate,
                        ff_barrier=self.ff_barrier,
                        over_approximate_if=self.over_approximate_if,
                        mod_cache=self.mod_cache,
                        param_ctx_cache=self.param_ctx_cache,
                        check_id=sub_id,
                        elab_index=self.elab_index,
                        rows_by_path=self.rows_by_path,
                    )
                )
            result = aggregate_connect_results(
                endpoint_a,
                endpoint_b,
                sub_results,
                check_id=check_id,
                fanout_mode=fanout_mode,
            )
        from hierwalk.verification_timing import record_connect_check

        record_connect_check(
            check_id=check_id,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            elapsed_sec=time.perf_counter() - t0,
        )
        return result

    def check_entry(
        self,
        chk: ConnectivityCheck,
        *,
        trace: bool = False,
    ) -> ConnectResult:
        return self.check(
            chk.endpoint_a,
            chk.endpoint_b,
            trace=trace,
            check_id=chk.check_id,
            expand=chk.expand,
        )

    def check_many(
        self,
        pairs: Sequence[Tuple[str, str]],
        *,
        trace: bool = False,
        jobs: int = 0,
    ) -> List[ConnectResult]:
        pair_list = list(pairs)
        if not pair_list:
            return []
        workers = _resolve_connect_jobs(jobs, len(pair_list))
        if workers == 1 or len(pair_list) < 4:
            return [self.check(a, b, trace=trace) for a, b in pair_list]

        chunk_count = min(workers, len(pair_list))
        chunk_size = max(1, math.ceil(len(pair_list) / chunk_count))
        chunks = [
            pair_list[i : i + chunk_size]
            for i in range(0, len(pair_list), chunk_size)
        ]

        def _run_chunk(chunk: Sequence[Tuple[str, str]]) -> List[ConnectResult]:
            local = ConnectivitySession(
                rows=self.rows,
                index=self.index,
                top=self.top,
                defines=dict(self._effective_defines),
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                elab_index=self.elab_index,
            )
            return [local.check(a, b, trace=trace) for a, b in chunk]

        out: List[ConnectResult] = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            for part in pool.map(_run_chunk, chunks):
                out.extend(part)
        return out

    def run_request(
        self,
        request: ConnectivityRequest,
        *,
        trace: Optional[bool] = None,
        jobs: int = 0,
        on_progress: Optional[Any] = None,
    ) -> ConnectivityBatchResult:
        from hierwalk.validate_connect import waypoint_perf_warnings

        use_trace = request.trace if trace is None else trace
        perf_notes = tuple(waypoint_perf_warnings(request))
        if perf_notes and on_progress is not None:
            for note in perf_notes:
                on_progress(f"connect: perf note: {note}")
        checks = list(request.checks)
        workers = _resolve_connect_jobs(jobs, len(checks))
        if workers == 1 or len(checks) < 4:
            results = tuple(
                self.check_entry(chk, trace=use_trace) for chk in checks
            )
            return ConnectivityBatchResult(
                results=results,
                modules_cached=self.modules_cached,
                perf_warnings=perf_notes,
            )

        chunk_count = min(workers, len(checks))
        chunk_size = max(1, math.ceil(len(checks) / chunk_count))
        chunks = [
            checks[i : i + chunk_size] for i in range(0, len(checks), chunk_size)
        ]

        def _run_chunk(chunk: Sequence[Any]) -> List[ConnectResult]:
            local = ConnectivitySession(
                rows=self.rows,
                index=self.index,
                top=self.top,
                defines=dict(self._effective_defines),
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                elab_index=self.elab_index,
            )
            return [local.check_entry(chk, trace=use_trace) for chk in chunk]

        merged: List[ConnectResult] = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            for part in pool.map(_run_chunk, chunks):
                merged.extend(part)
        return ConnectivityBatchResult(
            results=tuple(merged),
            modules_cached=self.modules_cached,
            perf_warnings=perf_notes,
        )

    def prewarm_inst(self, inst_path: str) -> bool:
        """Build ``ModuleConnectIndex`` for the module at *inst_path* (if known)."""
        row = self.rows_by_path.get(inst_path)
        if row is None:
            return False
        over_approx = _resolve_over_approximate_if(
            self.strict_generate,
            self.over_approximate_if,
        )
        from hierwalk.connect_endpoints import _shared_cache_lock

        path = row.full_path
        hit = self.param_ctx_cache.get(path)
        if hit is None:
            with _shared_cache_lock(self.param_ctx_cache, path):
                hit = self.param_ctx_cache.get(path)
                if hit is None:
                    hit = _port_param_ctx(self.index, row, self.top)
                    self.param_ctx_cache[path] = hit
        pmap = hit
        _module_index(
            self.mod_cache,
            self.index,
            row.module,
            pmap,
            defines=self._effective_defines,
            over_approximate_if=over_approx,
            ff_barrier=self.ff_barrier,
        )
        return True


def check_connectivity(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str = "",
    defines: Mapping[str, str] | None = None,
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = True,
    over_approximate_if: Optional[bool] = None,
) -> ConnectResult:
    if not top and rows:
        top = rows[0].full_path.split(".", 1)[0]
    return _connect_pair(
        endpoint_a,
        endpoint_b,
        rows=rows,
        index=index,
        top=top,
        effective_defines=_effective_defines(index, defines),
        trace=trace,
        strict_generate=strict_generate,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
        mod_cache={},
        param_ctx_cache={},
    )


@dataclass(frozen=True)
class ConnectivityBatchResult:
    """Outcome of ``check_connectivity_batch`` (shared module-index cache)."""

    results: Tuple[ConnectResult, ...]
    modules_cached: int
    perf_warnings: Tuple[str, ...] = ()


def _session_from_options(
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Mapping[str, str] | None,
    trace: bool,
    strict_generate: bool,
    ff_barrier: bool,
    over_approximate_if: Optional[bool],
) -> ConnectivitySession:
    return ConnectivitySession(
        rows=rows,
        index=index,
        top=top,
        defines=dict(defines or {}),
        strict_generate=strict_generate,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
    )


def check_connectivity_batch(
    pairs: Sequence[Tuple[str, str]],
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str = "",
    defines: Mapping[str, str] | None = None,
    trace: bool = False,
    strict_generate: bool = False,
    ff_barrier: bool = True,
    over_approximate_if: Optional[bool] = None,
) -> ConnectivityBatchResult:
    """
    Batch connectivity with the same options as ``check_connectivity``.

    Reuses ``ModuleConnectIndex`` across *pairs* (array fan-out, bus checks, …).
    """
    session = _session_from_options(
        rows=rows,
        index=index,
        top=top,
        defines=defines,
        trace=trace,
        strict_generate=strict_generate,
        ff_barrier=ff_barrier,
        over_approximate_if=over_approximate_if,
    )
    results = tuple(session.check(a, b, trace=trace) for a, b in pairs)
    return ConnectivityBatchResult(
        results=results,
        modules_cached=session.modules_cached,
    )


def run_connectivity_request(
    request: ConnectivityRequest,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str = "",
    extra_defines: Mapping[str, str] | None = None,
    jobs: int = 0,
    on_progress: Optional[Any] = None,
) -> ConnectivityBatchResult:
    """Run a full JSON connectivity request (checks + options)."""
    top_name = request.top or top
    if not top_name and rows:
        top_name = rows[0].full_path.split(".", 1)[0]
    merged_defines = _effective_defines(index, extra_defines)
    merged_defines.update(request.defines)
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top=top_name,
        defines=merged_defines,
        strict_generate=request.strict_generate,
        ff_barrier=not request.include_ff,
        over_approximate_if=request.over_approximate_if,
    )
    return session.run_request(request, jobs=jobs, on_progress=on_progress)


def _connected_text_value(result: ConnectResult) -> bool:
    if result.connected_text is not None:
        return result.connected_text
    return result.connected


def _connected_logical_value(result: ConnectResult) -> bool:
    if result.connected_logical is not None:
        return result.connected_logical
    return result.connected


def format_connect_result_row(
    result: ConnectResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    phase: str = "logical",
) -> str:
    from hierwalk.hierarchy_log import endpoint_provenance_fields

    err_text = " | ".join(result.errors)
    hop_text = " | ".join(format_connect_hop(h) for h in result.hops)
    logical_notes = " | ".join(result.logical_notes)
    a_prov = (
        endpoint_provenance_fields(result.endpoint_a, rows_by_path)
        if rows_by_path is not None
        else {}
    )
    b_prov = (
        endpoint_provenance_fields(result.endpoint_b, rows_by_path)
        if rows_by_path is not None
        else {}
    )
    text_connected = _connected_text_value(result)
    logical_connected = _connected_logical_value(result)
    if str(phase).strip().lower() == "text":
        return (
            f"{result.check_id}\t{result.endpoint_a.spec}\t{result.endpoint_b.spec}\t"
            f"{text_connected}\t{result.mode}\t{result.note}\t"
            f"{err_text}\t"
            f"{hop_text}\t"
            f"{a_prov.get('rtl', '')}\t{a_prov.get('via_filelist', '')}\t"
            f"{a_prov.get('filelist_chain', '')}\t"
            f"{b_prov.get('rtl', '')}\t{b_prov.get('via_filelist', '')}\t"
            f"{b_prov.get('filelist_chain', '')}\t"
            f"text"
        )
    return (
        f"{result.check_id}\t{result.endpoint_a.spec}\t{result.endpoint_b.spec}\t"
        f"{text_connected}\t{logical_connected}\t{logical_connected}\t"
        f"{result.mode}\t{result.note}\t{logical_notes}\t"
        f"{err_text}\t"
        f"{hop_text}\t"
        f"{a_prov.get('rtl', '')}\t{a_prov.get('via_filelist', '')}\t"
        f"{a_prov.get('filelist_chain', '')}\t"
        f"{b_prov.get('rtl', '')}\t{b_prov.get('via_filelist', '')}\t"
        f"{b_prov.get('filelist_chain', '')}\t"
        f"logical"
    )


def format_connect_results_tsv(
    results: Sequence[ConnectResult],
    *,
    modules_cached: Optional[int] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    phase: str = "logical",
) -> str:
    from hierwalk.waypoint_fanout import format_waypoint_fanout_tsv

    leaf_results = flatten_connect_results(results)
    phase_label = str(phase).strip().lower() or "logical"
    if phase_label == "text":
        header = (
            "check_id\tendpoint_a\tendpoint_b\tconnected_text\tmode\tnote\terrors\thops\t"
            "a_rtl\ta_via_filelist\ta_filelist_chain\t"
            "b_rtl\tb_via_filelist\tb_filelist_chain\tphase"
        )
    else:
        header = (
            "check_id\tendpoint_a\tendpoint_b\tconnected_text\tconnected_logical\t"
            "connected\tmode\tnote\tlogical_notes\terrors\thops\t"
            "a_rtl\ta_via_filelist\ta_filelist_chain\t"
            "b_rtl\tb_via_filelist\tb_filelist_chain\tphase"
        )
    lines = [
        "# connect results",
        header,
        *(
            format_connect_result_row(r, rows_by_path=rows_by_path, phase=phase_label)
            for r in leaf_results
        ),
    ]
    waypoint_blocks: List[str] = []
    for result in leaf_results:
        if not result.waypoint_events:
            continue
        if result.check_id:
            waypoint_blocks.append(f"# waypoint_fanout\t{result.check_id}")
        waypoint_blocks.append(
            format_waypoint_fanout_tsv(result.waypoint_events).rstrip("\n")
        )
    if waypoint_blocks:
        lines.append("# --- waypoint-fanout trace ---")
        lines.extend(waypoint_blocks)
    if modules_cached is not None:
        lines.append(f"# modules_cached\t{modules_cached}")
    return "\n".join(lines) + "\n"


def parse_connect_pairs_json(data: Any) -> List[Tuple[str, str]]:
    """Backward-compatible pairs-only JSON parse."""
    req = parse_connect_request_json(data)
    return [(c.endpoint_a, c.endpoint_b) for c in req.checks]


def load_connect_pairs(path: Union[str, Path]) -> List[Tuple[str, str]]:
    """Backward-compatible pairs loader (text or minimal JSON)."""
    return [(c.endpoint_a, c.endpoint_b) for c in load_connect_request(path).checks]


def _has_port(ep: ConnectEndpoint) -> bool:
    return bool(ep.port_name)


def _mode(a: ConnectEndpoint, b: ConnectEndpoint) -> str:
    if _has_port(a) and _has_port(b):
        return "port-port"
    if _has_port(a) or _has_port(b):
        return "port-hierarchy"
    return "hierarchy-hierarchy"


def _is_ancestor(ancestor: str, path: str) -> bool:
    return path.startswith(ancestor + ".")