"""Structural COI connectivity (public API and batch session)."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    import io

    from hierwalk.connect_walk_log import emit_connect_walk_report, format_connect_log_line
    from hierwalk.hierarchy_log import format_endpoint_provenance_line

    buf = io.StringIO()
    prefix = "[hier-walk connect]"
    if result.check_id:
        prefix = f"{prefix} [{result.check_id}]"
    buf.write(
        format_connect_log_line(
            f"{result.endpoint_a.spec} -> {result.endpoint_b.spec}",
            prefix=prefix,
        )
        + "\n"
    )
    if rows_by_path is not None:
        buf.write(
            format_connect_log_line(
                format_endpoint_provenance_line("A", result.endpoint_a, rows_by_path),
                prefix=f"{prefix}  ",
            )
            + "\n"
        )
        buf.write(
            format_connect_log_line(
                format_endpoint_provenance_line("B", result.endpoint_b, rows_by_path),
                prefix=f"{prefix}  ",
            )
            + "\n"
        )
    if result.errors:
        for err in result.errors:
            buf.write(format_connect_log_line(f"error: {err}", prefix=f"{prefix}  ") + "\n")
    if result.connected:
        buf.write(
            format_connect_log_line(
                f"connected: {result.connected}  mode: {result.mode}  note: {result.note}",
                prefix=f"{prefix}  ",
            )
            + "\n"
        )
    else:
        buf.write(
            format_connect_log_line(f"not connected ({result.note})", prefix=f"{prefix}  ")
            + "\n"
        )
    if rows_by_path is not None:
        emit_connect_walk_report(
            result,
            stream=buf,
            prefix=prefix,
            rows_by_path=rows_by_path,
            diagnostic=result.coi_walk,
        )
    return buf.getvalue()


def emit_connect_trace_log(
    result: ConnectResult,
    *,
    stream: IO[str] = sys.stderr,
    check_prefix: str = "",
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> None:
    """Emit endpoint provenance and COI walk evidence for one connect result."""
    if result.sub_results:
        for sub in result.sub_results:
            emit_connect_trace_log(
                sub,
                stream=stream,
                check_prefix=sub.check_id or check_prefix,
                rows_by_path=rows_by_path,
            )
        return
    from hierwalk.connect_walk_log import (
        _emit_line,
        emit_connect_walk_report,
    )
    from hierwalk.hierarchy_log import format_endpoint_provenance_line

    prefix = "[hier-walk connect]"
    if check_prefix:
        prefix = f"{prefix} [{check_prefix}]"
    _emit_line(
        stream,
        f"{result.endpoint_a.spec} -> {result.endpoint_b.spec}",
        prefix=prefix,
    )
    if rows_by_path is not None:
        _emit_line(
            stream,
            format_endpoint_provenance_line("A", result.endpoint_a, rows_by_path),
            prefix=f"{prefix}  ",
        )
        _emit_line(
            stream,
            format_endpoint_provenance_line("B", result.endpoint_b, rows_by_path),
            prefix=f"{prefix}  ",
        )
    if result.errors:
        for err in result.errors:
            _emit_line(stream, f"error: {err}", prefix=f"{prefix}  ")
    if not result.connected:
        _emit_line(stream, f"not connected ({result.note})", prefix=f"{prefix}  ")
    else:
        _emit_line(
            stream,
            f"connected: {result.connected}  mode: {result.mode}  note: {result.note}",
            prefix=f"{prefix}  ",
        )
    if rows_by_path is not None:
        emit_connect_walk_report(
            result,
            stream=stream,
            prefix=prefix,
            rows_by_path=rows_by_path,
            diagnostic=result.coi_walk,
        )


def format_connect_results_report(
    results: Sequence[ConnectResult],
    *,
    phase: str = "logical",
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    signal_tails: Optional[Sequence[object]] = None,
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> List[str]:
    """Hierarchy-first connect report (inst/port/wire/reg), then COI verdict."""
    from hierwalk.connect_artifacts import (
        SignalTailRecord,
        format_connect_results_report as _format_analysis_report,
    )

    tails: Sequence[SignalTailRecord] = ()
    if signal_tails:
        tails = tuple(
            rec
            for rec in signal_tails
            if isinstance(rec, SignalTailRecord)
        )
    return _format_analysis_report(
        results,
        phase=phase,
        rows_by_path=rows_by_path,
        signal_tails=tails,
        index=index,
        top=top,
    )


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


def flatten_connect_results_for_output(
    results: Sequence[ConnectResult],
) -> List[ConnectResult]:
    """Flatten aggregates; keep top-level rows when flatten would drop leaf specs."""
    leaf = flatten_connect_results(results)
    if leaf:
        return leaf
    if not results:
        return []
    fallback = [
        r
        for r in results
        if r.endpoint_a.spec or r.endpoint_b.spec or r.check_id
    ]
    return fallback or list(results)


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
    *,
    sources: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    from hierwalk.connect_scan import design_parse_sources

    if sources is None:
        sources = design_parse_sources(index)
    return collect_design_defines(
        index,
        sources=sources if sources else None,
        extra_defines=dict(defines or {}),
    )


def _resolve_connect_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


class _ConnectCoiHeartbeat:
    """Periodic connect-coi progress while checks run (``HIERWALK_PW_HEARTBEAT``)."""

    def __init__(
        self,
        *,
        total_checks: int,
        get_checks_done: Any,
        get_modules_cached: Any,
        get_detail: Any = lambda: "",
        on_emit: Optional[Any] = None,
        interval_sec: Optional[float] = None,
    ) -> None:
        from hierwalk.perf import pw_heartbeat_interval_sec

        self._total = total_checks
        self._get_checks_done = get_checks_done
        self._get_modules_cached = get_modules_cached
        self._get_detail = get_detail
        self._on_emit = on_emit
        self._interval = (
            interval_sec if interval_sec is not None else pw_heartbeat_interval_sec()
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = time.monotonic()
        self._count = 0

    def __enter__(self) -> "_ConnectCoiHeartbeat":
        if self._interval is None or self._on_emit is None or self._total <= 0:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=self._interval + 2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._emit_once()

    def _emit_once(self) -> None:
        if self._on_emit is None:
            return
        self._count += 1
        done = self._get_checks_done()
        modules = self._get_modules_cached()
        elapsed = time.monotonic() - self._started
        detail = self._get_detail()
        msg = (
            f"connect-coi heartbeat count={self._count} "
            f"checks_done={done}/{self._total} "
            f"modules_cached={modules} elapsed_sec={elapsed:.1f}"
        )
        if detail:
            msg += f" {detail}"
        self._on_emit(msg)


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
    mod_cache: Dict[Tuple[str, str, str, str, str, bool, bool, bool], ModuleConnectIndex],
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str = "",
    elab_index: Optional[ElabIndex] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    resolve_param_dims: bool = True,
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
        ok, hops, mod_n, diag = _bidirectional_coi(
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
            resolve_param_dims=resolve_param_dims,
        )
        walk_notes: List[str] = []
        if not ok and diag is not None:
            from hierwalk.connect_walk_log import build_walk_notes

            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup or {},
                start=start,
                goal=goal,
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
            walk_notes=walk_notes,
            coi_walk=diag,
        )

    if mode == "port-hierarchy":
        port_ep = ep_a if _has_port(ep_a) else ep_b
        hier_ep = ep_b if _has_port(ep_a) else ep_a
        start = (port_ep.inst_path, port_ep.port_name or "")
        ok, hops, mod_n, diag = _forward_coi_to_scope(
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
            resolve_param_dims=resolve_param_dims,
        )
        walk_notes: List[str] = []
        if not ok and diag is not None:
            from hierwalk.connect_walk_log import build_walk_notes

            walk_notes = build_walk_notes(
                diag,
                rows_by_path=lookup or {},
                start=start,
                goal=(hier_ep.inst_path, ""),
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
            walk_notes=walk_notes,
            coi_walk=diag,
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


def _coarse_text_inst_path(inst_path: str) -> str:
    if not inst_path:
        return ""
    return ".".join(part.split("[", 1)[0] for part in inst_path.split("."))


def _coarse_text_port_base(port_name: str) -> str:
    if not port_name:
        return ""
    return port_name.split("[", 1)[0].split(".", 1)[0]


def _text_coi_dedup_key(
    ep_a: ConnectEndpoint,
    ep_b: ConnectEndpoint,
    errors: Sequence[str],
) -> Tuple[Any, ...]:
    """Coarse text-conn COI key: strip slice/index from inst paths and port names."""
    mode = _mode(ep_a, ep_b) if ep_a.module and ep_b.module else "unknown"
    if errors:
        return (
            "err",
            mode,
            _coarse_text_inst_path(ep_a.inst_path),
            _coarse_text_port_base(ep_a.port_name or ""),
            _coarse_text_inst_path(ep_b.inst_path),
            _coarse_text_port_base(ep_b.port_name or ""),
            tuple(errors),
        )
    if (_has_port(ep_a) and not ep_a.port_found) or (
        _has_port(ep_b) and not ep_b.port_found
    ):
        return (
            "port-miss",
            mode,
            _coarse_text_inst_path(ep_a.inst_path),
            _coarse_text_port_base(ep_a.port_name or ""),
            bool(ep_a.port_found),
            _coarse_text_inst_path(ep_b.inst_path),
            _coarse_text_port_base(ep_b.port_name or ""),
            bool(ep_b.port_found),
        )
    return (
        "coi",
        mode,
        _coarse_text_inst_path(ep_a.inst_path),
        _coarse_text_port_base(ep_a.port_name or ""),
        _coarse_text_inst_path(ep_b.inst_path),
        _coarse_text_port_base(ep_b.port_name or ""),
    )


def _fanout_text_coi_result(
    template: ConnectResult,
    *,
    spec_a: str,
    spec_b: str,
    ep_a: ConnectEndpoint,
    ep_b: ConnectEndpoint,
    check_id: str,
) -> ConnectResult:
    """Copy a deduped text COI verdict onto a leaf check (original specs/endpoints)."""
    return ConnectResult(
        ConnectEndpoint(
            spec_a,
            ep_a.inst_path,
            ep_a.port_name,
            ep_a.module,
            ep_a.port_found,
        ),
        ConnectEndpoint(
            spec_b,
            ep_b.inst_path,
            ep_b.port_name,
            ep_b.module,
            ep_b.port_found,
        ),
        template.connected,
        template.mode,
        hops=list(template.hops),
        errors=list(template.errors),
        note=template.note,
        check_id=check_id,
        walk_notes=list(template.walk_notes),
        coi_walk=template.coi_walk,
    )


def _connect_pair_text_deduped(
    endpoint_a: str,
    endpoint_b: str,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    effective_defines: Mapping[str, str],
    trace: bool,
    strict_generate: bool,
    ff_barrier: bool,
    over_approximate_if: Optional[bool],
    mod_cache: Dict[Tuple[str, str, str, str, str, bool, bool, bool], ModuleConnectIndex],
    param_ctx_cache: Dict[str, Mapping[str, str]],
    check_id: str,
    elab_index: Optional[ElabIndex],
    rows_by_path: Mapping[str, FlatRow],
    dedup_cache: Dict[Tuple[Any, ...], ConnectResult],
    dedup_stats: List[int],
    dedup_lock: Optional[threading.Lock] = None,
) -> ConnectResult:
    lookup = rows_by_path
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
    key = _text_coi_dedup_key(ep_a, ep_b, errors)

    def _run() -> ConnectResult:
        dedup_stats[0] += 1
        hit = dedup_cache.get(key)
        if hit is not None:
            return _fanout_text_coi_result(
                hit,
                spec_a=endpoint_a,
                spec_b=endpoint_b,
                ep_a=ep_a,
                ep_b=ep_b,
                check_id=check_id,
            )
        dedup_stats[1] += 1
        result = _connect_pair(
            endpoint_a,
            endpoint_b,
            rows=rows,
            index=index,
            top=top,
            effective_defines=effective_defines,
            trace=trace,
            strict_generate=strict_generate,
            ff_barrier=ff_barrier,
            over_approximate_if=over_approximate_if,
            mod_cache=mod_cache,
            param_ctx_cache=param_ctx_cache,
            check_id=check_id,
            elab_index=elab_index,
            rows_by_path=rows_by_path,
            resolve_param_dims=False,
        )
        dedup_cache[key] = result
        return result

    if dedup_lock is not None:
        with dedup_lock:
            return _run()
    return _run()


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
    sources: Optional[Sequence[str]] = None
    strict_generate: bool = False
    ff_barrier: bool = True
    over_approximate_if: Optional[bool] = None
    mod_cache: Dict[
        Tuple[str, str, str, str, str, bool, bool, bool],
        ModuleConnectIndex,
    ] = field(default_factory=dict)
    param_ctx_cache: Dict[str, Mapping[str, str]] = field(default_factory=dict)
    elab_index: Optional[ElabIndex] = None
    resolve_param_dims: bool = True
    _effective_defines_cache: Dict[str, str] = field(default_factory=dict, repr=False)
    _effective_defines_stamp: Tuple[
        Tuple[str, ...],
        Tuple[Tuple[str, str], ...],
        Tuple[str, ...],
        Tuple[Tuple[str, str], ...],
    ] = field(default=((), (), (), ()), repr=False)

    def __post_init__(self) -> None:
        if self.elab_index is None and self.rows:
            self.elab_index = ElabIndex.from_rows(self.rows)
        if not self.top and self.rows:
            self.top = self.rows[0].full_path.split(".", 1)[0]
        self._refresh_effective_defines()

    def _refresh_effective_defines(self) -> Dict[str, str]:
        from hierwalk.connect_scan import design_parse_sources

        srcs = (
            list(self.sources)
            if self.sources is not None
            else design_parse_sources(self.index)
        )
        defines_stamp = tuple(sorted(self.defines.items()))
        parse_stamp = tuple(getattr(self.index, "_parse_sources", ()) or ())
        modules_stamp = tuple(
            sorted(
                (name, rec.file_path)
                for name, rec in self.index.modules.items()
            )
        )
        stamp = (parse_stamp, modules_stamp, tuple(srcs), defines_stamp)
        if stamp != self._effective_defines_stamp or not self._effective_defines_cache:
            self._effective_defines_cache = _effective_defines(
                self.index,
                self.defines,
                sources=srcs or None,
            )
            self._effective_defines_stamp = stamp
        return dict(self._effective_defines_cache)

    def effective_defines(self) -> Dict[str, str]:
        """Filelist + RTL defines; recomputed when the index or source list changes."""
        return self._refresh_effective_defines()

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
        self._effective_defines_stamp = ((), (), (), ())
        self._effective_defines_cache.clear()

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
                defines=self.effective_defines(),
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
                effective_defines=self.effective_defines(),
                trace=trace,
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                check_id=check_id,
                elab_index=self.elab_index,
                rows_by_path=self.rows_by_path,
                resolve_param_dims=self.resolve_param_dims,
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
                        effective_defines=self.effective_defines(),
                        trace=trace,
                        strict_generate=self.strict_generate,
                        ff_barrier=self.ff_barrier,
                        over_approximate_if=self.over_approximate_if,
                        mod_cache=self.mod_cache,
                        param_ctx_cache=self.param_ctx_cache,
                        check_id=sub_id,
                        elab_index=self.elab_index,
                        rows_by_path=self.rows_by_path,
                        resolve_param_dims=self.resolve_param_dims,
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
                defines=dict(self.effective_defines()),
                sources=self.sources,
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                elab_index=self.elab_index,
                resolve_param_dims=self.resolve_param_dims,
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
                defines=dict(self.effective_defines()),
                sources=self.sources,
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                elab_index=self.elab_index,
                resolve_param_dims=self.resolve_param_dims,
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

    def text_check_entry(
        self,
        chk: ConnectivityCheck,
        *,
        trace: bool,
        dedup_cache: Dict[Tuple[Any, ...], ConnectResult],
        dedup_stats: List[int],
        dedup_lock: Optional[threading.Lock] = None,
    ) -> ConnectResult:
        """Single text-phase check with shared coarse COI dedup cache."""
        lookup = self.rows_by_path
        if chk.expand is not None and chk.expand.map_kind == "waypoint-fanout":
            return self.check_entry(chk, trace=trace)

        pairs = expand_check_to_pairs(
            chk.endpoint_a,
            chk.endpoint_b,
            check_id=chk.check_id,
            expand=chk.expand,
        )
        if len(pairs) == 1 and not pairs[0].sub_id:
            pair = pairs[0]
            sub_id = chk.check_id or pair.sub_id.strip("[]->")
            return _connect_pair_text_deduped(
                pair.endpoint_a,
                pair.endpoint_b,
                rows=self.rows,
                index=self.index,
                top=self.top,
                effective_defines=self.effective_defines(),
                trace=trace,
                strict_generate=self.strict_generate,
                ff_barrier=self.ff_barrier,
                over_approximate_if=self.over_approximate_if,
                mod_cache=self.mod_cache,
                param_ctx_cache=self.param_ctx_cache,
                check_id=sub_id,
                elab_index=self.elab_index,
                rows_by_path=lookup,
                dedup_cache=dedup_cache,
                dedup_stats=dedup_stats,
                dedup_lock=dedup_lock,
            )

        fanout_mode = chk.expand.fanout_mode if chk.expand is not None else "all"
        sub_results: List[ConnectResult] = []
        for pair in pairs:
            sub_id = (
                f"{chk.check_id}{pair.sub_id}"
                if chk.check_id
                else pair.sub_id.strip("[]->")
            )
            sub_results.append(
                _connect_pair_text_deduped(
                    pair.endpoint_a,
                    pair.endpoint_b,
                    rows=self.rows,
                    index=self.index,
                    top=self.top,
                    effective_defines=self.effective_defines(),
                    trace=trace,
                    strict_generate=self.strict_generate,
                    ff_barrier=self.ff_barrier,
                    over_approximate_if=self.over_approximate_if,
                    mod_cache=self.mod_cache,
                    param_ctx_cache=self.param_ctx_cache,
                    check_id=sub_id,
                    elab_index=self.elab_index,
                    rows_by_path=lookup,
                    dedup_cache=dedup_cache,
                    dedup_stats=dedup_stats,
                    dedup_lock=dedup_lock,
                )
            )
        return aggregate_connect_results(
            chk.endpoint_a,
            chk.endpoint_b,
            sub_results,
            check_id=chk.check_id,
            fanout_mode=fanout_mode,
        )

    def run_text_request(
        self,
        request: ConnectivityRequest,
        *,
        trace: Optional[bool] = None,
        on_progress: Optional[Any] = None,
        on_heartbeat: Optional[Any] = None,
        jobs: int = 0,
        record_timing: bool = False,
    ) -> ConnectivityBatchResult:
        """
        Text-conn batch: coarse COI dedup across slice-expanded leaf checks.

        Use only for the text phase (``resolve_param_dims=False``). Logical conn
        must call :meth:`run_request` so each leaf keeps its own COI search.
        """
        from hierwalk.validate_connect import waypoint_perf_warnings

        use_trace = request.trace if trace is None else trace
        perf_notes = tuple(waypoint_perf_warnings(request))
        dedup_cache: Dict[Tuple[Any, ...], ConnectResult] = {}
        dedup_stats = [0, 0]
        checks = list(request.checks)
        workers = _resolve_connect_jobs(jobs, len(checks))
        dedup_lock = threading.Lock() if workers > 1 else None
        checks_done = 0
        checks_done_lock = threading.Lock() if workers > 1 else None

        def _bump_checks_done() -> None:
            nonlocal checks_done
            if checks_done_lock is None:
                checks_done += 1
                return
            with checks_done_lock:
                checks_done += 1

        def _one(chk: ConnectivityCheck) -> ConnectResult:
            t0 = time.perf_counter()
            result = self.text_check_entry(
                chk,
                trace=use_trace,
                dedup_cache=dedup_cache,
                dedup_stats=dedup_stats,
                dedup_lock=dedup_lock,
            )
            if record_timing:
                from hierwalk.verification_timing import record_connect_check

                record_connect_check(
                    check_id=chk.check_id,
                    endpoint_a=str(chk.endpoint_a),
                    endpoint_b=str(chk.endpoint_b),
                    elapsed_sec=time.perf_counter() - t0,
                )
            _bump_checks_done()
            return result

        with _ConnectCoiHeartbeat(
            total_checks=len(checks),
            get_checks_done=lambda: checks_done,
            get_modules_cached=lambda: self.modules_cached,
            on_emit=on_heartbeat,
        ):
            if workers <= 1 or len(checks) < 4:
                results = [_one(chk) for chk in checks]
            else:
                ordered: List[Optional[ConnectResult]] = [None] * len(checks)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(_one, chk): idx for idx, chk in enumerate(checks)
                    }
                    for fut in as_completed(futures):
                        ordered[futures[fut]] = fut.result()
                results = [r for r in ordered if r is not None]

        leaves, unique = dedup_stats[0], dedup_stats[1]
        if on_progress is not None and leaves > unique:
            on_progress(
                f"connect: text-coi dedup leaves={leaves} unique={unique} "
                f"saved={leaves - unique}"
            )
        if on_progress is not None and workers > 1:
            on_progress(f"connect: text-coi parallel workers={workers}")
        return ConnectivityBatchResult(
            results=tuple(results),
            modules_cached=self.modules_cached,
            perf_warnings=perf_notes,
            text_coi_leaves=leaves,
            text_coi_unique=unique,
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
                    hit = _port_param_ctx(
                        self.index,
                        row,
                        self.top,
                        resolve_param_dims=self.resolve_param_dims,
                    )
                    self.param_ctx_cache[path] = hit
        pmap = hit
        _module_index(
            self.mod_cache,
            self.index,
            row.module,
            pmap,
            defines=self.effective_defines(),
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
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
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
        rows_by_path=rows_by_path,
    )


@dataclass(frozen=True)
class ConnectivityBatchResult:
    """Outcome of ``check_connectivity_batch`` (shared module-index cache)."""

    results: Tuple[ConnectResult, ...]
    modules_cached: int
    perf_warnings: Tuple[str, ...] = ()
    text_coi_leaves: int = 0
    text_coi_unique: int = 0


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
    from hierwalk.connect_scan import design_parse_sources

    session_defines = dict(extra_defines or {})
    session_defines.update(request.defines)
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top=top_name,
        defines=session_defines,
        sources=design_parse_sources(index),
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

    leaf_results = flatten_connect_results_for_output(results)
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