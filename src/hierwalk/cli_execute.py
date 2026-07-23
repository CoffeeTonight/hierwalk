"""Execute one hier-walk run from a resolved RunConfig."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import hierwalk
from hierwalk.coverage_audit import compute_coverage_audit
from hierwalk.cache import (
    get_cached_elab,
    load_or_build_index,
    resolve_run_work_dir,
    resolve_top_label,
    set_active_work_dir,
    store_cached_elab,
    work_base_dir,
)
from hierwalk.elab import elaborate_tops_parallel
from hierwalk.lazy_scope import (
    elab_scope_paths,
    endpoint_specs_from_request,
    lazy_filelist_defer_exists,
    lazy_index_ifdef,
    lazy_processing_enabled,
    lazy_scoped_connect_elab,
)
from hierwalk.perf import effective_low_memory
from hierwalk.filelist import parse_filelist
from hierwalk.progress import ProgressHeartbeat, ProgressReporter, progress_callback
from hierwalk.hierarchy_log import emit_hierarchy_rows_log, emit_path_provenance_log, rows_lookup
from hierwalk.report import RunReport, default_log_path, emit_run_report, phase_log_path
from hierwalk.path_chain import attach_path_chains, format_path_chain_compact
from hierwalk.search_spec import effective_search_spec, execute_search_spec
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import (
    check_connectivity,
    emit_connect_trace_log,
    format_connect_results_tsv,
    print_connect_trace_reports,
    run_connectivity_request,
)
from hierwalk.path_walk import run_path_walk_connect, run_path_walk_index
from hierwalk.connect.pipeline.artifacts import (
    connect_output_paths,
    missing_verification_artifacts,
)
from hierwalk.run_request import (
    normalize_run_mode,
    resolve_connectivity_request,
    resolve_effective_index_strategy,
    resolve_effective_run_mode,
)
from hierwalk.cone import (
    fanin_cone,
    fanout_cone,
    format_cone_tsv,
    print_cone_report,
    write_cone_dot,
)
from hierwalk.inst_trace import (
    format_inst_trace_tsv,
    print_inst_trace_report,
    run_inst_trace,
)

from hierwalk.top_find import find_top_modules, resolve_top_modules
from hierwalk.run_request import RunConfig
from hierwalk.verification_timing import (
    get_active_recorder,
    record_connect_check,
    record_verification_item,
)


def _verification_phase(cfg: RunConfig) -> str:
    from hierwalk.run_request import parse_connect_phase_value

    if cfg.check_hgrep:
        return "hgrep"
    if cfg.check_pyslangwalk:
        # Allow cascade when JSON set verification_phase to hgrep+pyslangwalk.
        try:
            return parse_connect_phase_value(
                cfg.verification_phase or "pyslangwalk"
            )
        except ValueError:
            return "pyslangwalk"
    try:
        return parse_connect_phase_value(cfg.verification_phase or "both")
    except ValueError:
        return "both"


def _fail_if_missing_verification_artifacts(
    cfg: RunConfig,
    work_dir: Path,
    *,
    label: str,
) -> int:
    missing = missing_verification_artifacts(cfg, work_dir)
    if not missing:
        return 0
    for path in missing:
        print(
            f"run: ERROR {label} artifact missing: {path.resolve()}",
            file=sys.stderr,
            flush=True,
        )
    return 2


def execute_run(cfg: RunConfig, ap) -> int:
    connect_request: Optional[ConnectivityRequest] = None
    if (
        cfg.check_connect_batch
        or cfg.connect_inline
        or cfg.check_hgrep
        or cfg.check_pyslangwalk
    ):
        connect_request = resolve_connectivity_request(cfg)

    effective_mode = resolve_effective_run_mode(cfg, connect_request)
    index_strategy = resolve_effective_index_strategy(cfg, effective_mode)
    hgrep_mode = index_strategy == "hgrep"
    pyslangwalk_mode = index_strategy == "pyslangwalk"
    path_walk_mode = index_strategy == "path-walk"
    cone_mode = effective_mode == "cone"
    inst_trace_mode = effective_mode == "inst-trace"
    connect_run_mode = effective_mode in (
        "check-connect",
        "check-connect-batch",
        "check-hgrep",
        "check-pyslangwalk",
        "path-walk",
    )
    if cfg.check_connect and connect_request is not None:
        ap.error("use either check_connect or check_connect_batch/connect, not both")
    if cfg.fanin_cone and cfg.fanout_cone:
        ap.error("use either fanin_cone or fanout_cone, not both")
    if (
        path_walk_mode
        and not hgrep_mode
        and connect_run_mode
        and connect_request is None
        and not cfg.check_connect
    ):
        ap.error("path-walk connect requires checks in batch JSON or --check-connect")
    if effective_mode == "check-connect-batch" and connect_request is None:
        ap.error(
            "check-connect-batch mode requires checks/pairs in batch JSON "
            "or a pairs text file"
        )
    if inst_trace_mode and cfg.inst_trace is None:
        ap.error("inst-trace mode requires inst_trace in JSON")
    if cone_mode and not (cfg.fanin_cone or cfg.fanout_cone):
        ap.error("cone mode requires fanin_cone or fanout_cone in JSON")

    _full_index_modes = ("hierarchy", "search", "find-top")
    if effective_mode in _full_index_modes:
        explicit_mode = normalize_run_mode(cfg.mode or "")
        hierarchy_allowed = (
            cfg.full_index_step
            or explicit_mode in _full_index_modes
            or cfg.direct_filelist_cli
        )
        if not hierarchy_allowed:
            ap.error(
                "hierarchy/search/find-top blocked: flat-suite JSON did not schedule "
                "an enabled run_on_full_index step (enable: 1 required). "
                "Inferred legacy hierarchy from shared cfg.mode=None is not allowed. "
                "Pass RUN.json as the positional argument (not a legacy -c flag)."
            )
        if not cfg.quiet:
            print(
                f"run: enable-gate: hierarchy allowed={int(hierarchy_allowed)} "
                f"flat_suite_step={int(cfg.flat_suite_step)} "
                f"full_index_step={int(cfg.full_index_step)} "
                f"cfg.mode={explicit_mode or '(none)'}",
                file=sys.stderr,
            )

    t0 = time.perf_counter()
    extra_defines = dict(cfg.defines_map)
    if connect_request is not None:
        extra_defines.update(connect_request.defines)
    use_cache = not cfg.no_cache
    reporter = ProgressReporter(enabled=not cfg.quiet)
    reporter.set_filelist(cfg.filelist)
    on_progress = progress_callback(reporter)

    lazy = lazy_processing_enabled()
    if lazy and on_progress:
        ifdef_note = "ifdef at index" if lazy_index_ifdef() else "ifdef/macro deferred"
        on_progress(
            f"index: lazy ({ifdef_note}; connect/elab on-demand; "
            f"HIERWALK_LAZY=0 to disable)"
        )

    # Always expand the *current* filelist. Reusing grep_hie.json RTL paths
    # without comparing sources reuses a previous design when only the top
    # name matches (same ``.db_{TOP}/``), which surfaces as bogus hierarchy
    # misses (e.g. list ``a`` fails while ``b`` paths look related).
    # Cache reuse of the module index still happens inside
    # ``prepare_hierarchy_grep_session`` via ``grep_hie_sources_match``.
    fl = parse_filelist(
        cfg.filelist,
        index_cwd=cfg.index_cwd,
        extra_defines=extra_defines,
        on_progress=on_progress,
        ignore_filelists=list(cfg.ignore_filelist),
        defer_source_exists=lazy_filelist_defer_exists(),
    )
    if not fl.source_files:
        print("No sources in filelist", file=sys.stderr)
        return 1

    top_label = resolve_top_label(
        cfg_top=cfg.top or "",
        connect_top=connect_request.top if connect_request else "",
        inst_trace_top=cfg.inst_trace.top if cfg.inst_trace else "",
        filelist_tops=list(fl.top_modules),
        filelist_path=cfg.filelist,
    )
    work_dir = resolve_run_work_dir(
        top_label,
        base=work_base_dir(cfg.index_cwd),
        explicit_cache_dir=cfg.cache_dir,
    )
    cache_dir = work_dir
    set_active_work_dir(work_dir)
    from hierwalk.connect.pipeline.artifacts import archive_run_config_sources

    archived_configs = archive_run_config_sources(work_dir, cfg)
    if not cfg.quiet:
        print(
            f"run: work-dir: {work_dir} (top={top_label})",
            file=sys.stderr,
        )
        if archived_configs:
            print(
                "run: archived run config: "
                + ", ".join(str(p.name) for p in archived_configs),
                file=sys.stderr,
            )

    log_path: Path | None = None
    if not cfg.no_log_file:
        phase_for_log = _verification_phase(cfg)
        log_phase = phase_for_log if phase_for_log in ("text", "logical") else ""
        log_path = (
            phase_log_path(Path(cfg.log_file), phase=log_phase)
            if cfg.log_file
            else default_log_path(
                cfg.filelist,
                cfg.output,
                work_dir=work_dir,
                phase=log_phase,
            )
        )
    timing_rec = get_active_recorder()
    if timing_rec is not None:
        timing_rec.register_log_path(log_path)

    if not cfg.quiet:
        from hierwalk.config_env_audit import emit_verilog_defines_audit

        audit_extra = dict(cfg.defines_map or {})
        if connect_request is not None:
            audit_extra.update(connect_request.defines)
        if path_walk_mode or hgrep_mode:
            audit_defines = dict(fl.defines)
            audit_defines.update(audit_extra)
        else:
            from hierwalk.connect.logical.scan import collect_design_defines
            from hierwalk.index import DesignIndex

            audit_sources = [str(p) for p in fl.source_files]
            audit_index = DesignIndex._assemble(
                {},
                path_patterns=[],
                module_patterns=[],
                preprocess_include_dirs=[str(p) for p in fl.include_dirs],
                preprocess_defines=dict(fl.defines),
                parse_sources=audit_sources,
            )
            audit_defines = collect_design_defines(
                audit_index,
                sources=audit_sources,
                extra_defines=audit_extra,
            )
        emit_verilog_defines_audit(
            effective_defines=audit_defines,
            json_defines=cfg.defines_map,
            connect_defines=(
                connect_request.defines if connect_request is not None else None
            ),
            stream=sys.stderr,
        )
        if hgrep_mode:
            loader = "hgrep(grep_hie.json)"
        elif path_walk_mode:
            loader = "path-walk"
        else:
            loader = "load_or_build_index"
        print(
            f"run: enable-trace: stage=execute_run effective_mode={effective_mode} "
            f"index_strategy={index_strategy} index_loader={loader}",
            file=sys.stderr,
        )

    if hgrep_mode or path_walk_mode or pyslangwalk_mode:
        if hgrep_mode and on_progress:
            on_progress(
                "hgrep: hierarchy_grep gate only (no path-walk index; grep_hie.json)"
            )
        elif pyslangwalk_mode and on_progress:
            on_progress(
                "pyslangwalk: module-index + open only path RTL with pyslang"
            )
        elif path_walk_mode and on_progress:
            on_progress("path-walk: on-demand index (endpoint paths only)")
        top_for_walk = (
            cfg.top
            or (connect_request.top if connect_request else "")
            or (cfg.inst_trace.top if cfg.inst_trace else "")
            or (fl.top_modules[0] if fl.top_modules else "")
        )
        if not top_for_walk:
            print("path-walk requires --top or JSON top", file=sys.stderr)
            return 2
        pw_ignore = dict(
            ignore_paths=list(cfg.ignore_path),
            ignore_path_files=list(cfg.ignore_path_file),
            ignore_modules=list(cfg.ignore_module),
            ignore_filelists=list(cfg.ignore_filelist),
            cache_dir=cache_dir,
            no_cache=not use_cache,
            refresh_cache=cfg.refresh_cache,
            on_progress=on_progress,
            trace_stream=sys.stderr if not cfg.quiet else None,
            trace_log_path=log_path,
        )
        compile_defines = dict(fl.defines)
        compile_defines.update(extra_defines)
        elapsed = time.perf_counter() - t0

        if inst_trace_mode and path_walk_mode:
            assert cfg.inst_trace is not None
            try:
                index, pw_state, top_name = run_path_walk_index(
                    fl,
                    [cfg.inst_trace.instance],
                    top=top_for_walk,
                    extra_defines=extra_defines,
                    reuse_suite_session=cfg.flat_suite_step,
                    jobs=cfg.jobs,
                    **pw_ignore,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            _item_t0 = time.perf_counter()
            trace_result = run_inst_trace(
                cfg.inst_trace,
                rows=pw_state.rows(),
                index=index,
                top=top_name,
                defines=extra_defines or None,
            )
            record_verification_item(
                cfg.inst_trace.instance,
                time.perf_counter() - _item_t0,
            )
            if not cfg.quiet:
                emit_path_provenance_log(
                    trace_result.instance,
                    rows_lookup(pw_state.rows()),
                    stream=sys.stderr,
                    label="instance",
                    prefix="[hier-walk inst-trace]",
                )
            trace_rows = rows_lookup(pw_state.rows())
            term_stream = sys.stderr if cfg.output == "-" else sys.stdout
            print_inst_trace_report(
                trace_result,
                stream=term_stream,
                rows_by_path=trace_rows,
            )
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as fh:
                    print_inst_trace_report(
                        trace_result,
                        stream=fh,
                        rows_by_path=trace_rows,
                    )
            body = format_inst_trace_tsv(
                trace_result,
                rows_by_path=trace_rows,
            )
            report_mode = "inst-trace"
            search_pattern = cfg.inst_trace.instance
        elif cone_mode and path_walk_mode:
            cone_label = cfg.fanout_cone or cfg.fanin_cone or ""
            try:
                index, pw_state, top_name = run_path_walk_index(
                    fl,
                    [cone_label],
                    top=top_for_walk,
                    extra_defines=extra_defines,
                    reuse_suite_session=cfg.flat_suite_step,
                    jobs=cfg.jobs,
                    **pw_ignore,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            over_approx = (
                cfg.over_approximate_if
                if cfg.over_approximate_if is not None
                else True
            )
            _item_t0 = time.perf_counter()
            if cfg.fanout_cone:
                cone_result = fanout_cone(
                    cfg.fanout_cone,
                    rows=pw_state.rows(),
                    index=index,
                    top=top_name,
                    defines=compile_defines,
                    over_approximate_if=over_approx,
                )
                report_mode = "fanout-cone"
            else:
                assert cfg.fanin_cone is not None
                cone_result = fanin_cone(
                    cfg.fanin_cone,
                    rows=pw_state.rows(),
                    index=index,
                    top=top_name,
                    defines=compile_defines,
                    over_approximate_if=over_approx,
                )
                report_mode = "fanin-cone"
            record_verification_item(cone_label, time.perf_counter() - _item_t0)
            if not cfg.quiet:
                emit_path_provenance_log(
                    cone_result.origin_scope,
                    rows_lookup(pw_state.rows()),
                    stream=sys.stderr,
                    label="origin",
                    prefix="[hier-walk cone]",
                )
            cone_rows = rows_lookup(pw_state.rows())
            term_stream = sys.stderr if cfg.output == "-" else sys.stdout
            print_cone_report(
                cone_result,
                stream=term_stream,
                rows_by_path=cone_rows,
            )
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as fh:
                    print_cone_report(
                        cone_result,
                        stream=fh,
                        rows_by_path=cone_rows,
                    )
            if cfg.cone_graph:
                write_cone_dot(cone_result, cfg.cone_graph)
            body = format_cone_tsv(
                cone_result,
                rows_by_path=cone_rows,
            )
            search_pattern = cone_label
        else:
            if cfg.check_connect and connect_request is None:
                connect_request = ConnectivityRequest(
                    checks=(
                        ConnectivityCheck(
                            cfg.check_connect[0],
                            cfg.check_connect[1],
                        ),
                    ),
                    top=cfg.top or "",
                )
                extra_defines.update(connect_request.defines)
            else:
                connect_request = resolve_connectivity_request(cfg)
                if connect_request is None:
                    print("missing connectivity request", file=sys.stderr)
                    return 1
                extra_defines.update(connect_request.defines)
            use_trace = cfg.connect_trace or cfg.connect_log
            if connect_request is not None:
                trace_on = connect_request.trace or use_trace
                log_on = connect_request.connect_log or cfg.connect_log
                include_ff = connect_request.include_ff or cfg.include_ff
                if (
                    trace_on != connect_request.trace
                    or log_on != connect_request.connect_log
                    or include_ff != connect_request.include_ff
                ):
                    connect_request = ConnectivityRequest(
                        checks=connect_request.checks,
                        top=connect_request.top,
                        defines=connect_request.defines,
                        trace=trace_on,
                        connect_log=log_on,
                        include_ff=include_ff,
                        strict_generate=connect_request.strict_generate,
                        over_approximate_if=connect_request.over_approximate_if,
                    )
            phase = _verification_phase(cfg)
            do_hgrep = phase == "hgrep"
            do_pyslangwalk = phase == "pyslangwalk"
            do_cascade = phase == "hgrep+pyslangwalk"
            do_text = phase in ("text", "both")
            do_logical = phase in ("logical", "both")
            try:
                batch, index, pw_state = run_path_walk_connect(
                    connect_request,
                    fl,
                    top=top_for_walk,
                    extra_defines=extra_defines,
                    reuse_suite_session=cfg.flat_suite_step,
                    jobs=cfg.jobs,
                    connect_jobs=cfg.connect_jobs,
                    connect_output_dir=work_dir,
                    connect_output_name=cfg.output,
                    connect_phase=phase,
                    **pw_ignore,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            connect_results = batch.results
            endpoint_rows = pw_state.rows_by_path
            conn_paths = connect_output_paths(work_dir, cfg.output)
            if do_hgrep or do_cascade:
                if not conn_paths.hgrep_gate_report.is_file():
                    print(
                        f"connect: missing hgrep gate report "
                        f"{conn_paths.hgrep_gate_report}",
                        file=sys.stderr,
                    )
                    return 2
            elif do_pyslangwalk:
                # Hierarchy-only; no text/logical TSV pair artifacts required.
                pass
            else:
                artifact_rc = _fail_if_missing_verification_artifacts(
                    cfg,
                    work_dir,
                    label="connect",
                )
                if artifact_rc:
                    return artifact_rc
            if do_hgrep:
                stdout_phase = "hgrep"
            elif do_cascade:
                stdout_phase = "hgrep+pyslangwalk"
            elif do_pyslangwalk:
                stdout_phase = "pyslangwalk"
            elif do_logical and not do_text:
                stdout_phase = "logical"
            else:
                stdout_phase = "text"
            body = format_connect_results_tsv(
                connect_results,
                modules_cached=batch.modules_cached,
                rows_by_path=endpoint_rows,
                phase=stdout_phase,
            )
            if not cfg.quiet:
                emit_hierarchy_rows_log(
                    pw_state.rows(),
                    stream=sys.stderr,
                    title="path-walk instance rows (rtl + filelist)",
                )
                for result in connect_results:
                    emit_connect_trace_log(
                        result,
                        stream=sys.stderr,
                        check_prefix=result.check_id or "",
                        rows_by_path=endpoint_rows,
                    )
            use_trace = cfg.connect_trace or cfg.connect_log
            trace_on = connect_request.trace or use_trace
            if do_logical and not do_text:
                trace_title = "connectivity path evidence (logical)"
            elif do_text and not do_logical:
                trace_title = "connectivity path evidence (text)"
            else:
                trace_title = "connectivity path evidence"
            if cfg.output == "-":
                sys.stdout.write(body)
            elif do_hgrep and cfg.output:
                # path-walk text/logical write under work_dir via require_*;
                # hgrep also mirrors the configured output path when set.
                out_file = Path(cfg.output)
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(body, encoding="utf-8")
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n# connect execution ({stdout_phase})\n")
                    emit_hierarchy_rows_log(
                        pw_state.rows(),
                        stream=fh,
                        title="path-walk instance rows (rtl + filelist)",
                    )
                    if not cfg.quiet:
                        for result in connect_results:
                            emit_connect_trace_log(
                                result,
                                stream=fh,
                                check_prefix=result.check_id or "",
                                rows_by_path=endpoint_rows,
                            )
                    fh.write(f"\n# connect results ({stdout_phase})\n")
                    fh.write(body)
                    if not body.endswith("\n"):
                        fh.write("\n")
                    if trace_on:
                        print_connect_trace_reports(
                            connect_results,
                            stream=fh,
                            title=f"{trace_title} (log)",
                            rows_by_path=endpoint_rows,
                        )
                    fh.flush()
            if trace_on:
                term_stream = sys.stderr if cfg.output == "-" else sys.stdout
                print_connect_trace_reports(
                    connect_results,
                    stream=term_stream,
                    title=trace_title,
                    rows_by_path=endpoint_rows,
                )
            if on_progress and not cfg.quiet:
                on_progress(
                    f"path-walk: done {pw_state.stats.checks_run} check(s), "
                    f"{len(pw_state.rows_by_path)} row(s), "
                    f"{pw_state.stats.modules_loaded} module(s), "
                    f"{time.perf_counter() - t0:.1f}s"
                )
            emit_run_report(
                RunReport(
                    filelist_path=cfg.filelist,
                    elapsed_sec=time.perf_counter() - t0,
                    fl=fl,
                    index=index,
                    cache_enabled=False,
                    elab_tops=[top_for_walk],
                    instance_rows=len(pw_state.rows_by_path),
                    mode="path-walk",
                    output_path=(
                        str(cfg.output)
                        if do_hgrep and cfg.output != "-"
                        else str(
                            conn_paths.logical_tsv
                            if do_logical
                            else conn_paths.text_tsv
                        )
                    ),
                    filelist_warnings=len(fl.errors),
                    connect_results=connect_results,
                    connect_phase=stdout_phase,
                    connect_rows_by_path=endpoint_rows,
                    connect_signal_tails=pw_state.signal_tail_records,
                    connect_top=top_for_walk,
                ),
                log_path=log_path,
            )
            from hierwalk.path_walk import build_path_walk_db_full

            if not cfg.flat_suite_step:
                db_queued = build_path_walk_db_full(pw_state.mod_db)
                if db_queued and on_progress and not cfg.quiet:
                    on_progress(
                        f"path-walk: post-verify DB build warmed {db_queued} file(s)"
                    )
            return 0

        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        if on_progress and not cfg.quiet:
            on_progress(
                f"path-walk: done 1 trace step, "
                f"{len(pw_state.rows_by_path)} row(s), "
                f"{pw_state.stats.modules_loaded} module(s), "
                f"{time.perf_counter() - t0:.1f}s"
            )
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=time.perf_counter() - t0,
                fl=fl,
                index=index,
                cache_enabled=False,
                elab_tops=[top_name],
                instance_rows=len(pw_state.rows_by_path),
                mode=report_mode,
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                search_pattern=search_pattern,
            ),
            log_path=log_path,
        )
        return 0

    heartbeat = ProgressHeartbeat(
        reporter.phase,
        "index",
        enabled=not cfg.quiet and len(fl.source_files) >= 500,
        get_detail=reporter.get_detail,
    )
    low_memory = effective_low_memory(
        explicit=cfg.low_memory,
        num_sources=len(fl.source_files),
    )
    if low_memory and not cfg.low_memory and on_progress:
        on_progress(
            f"index: auto low-memory fused build ({len(fl.source_files)} sources)"
        )
    with heartbeat:
        index, bundle, index_cache_hit, index_rebuilt, index_incremental, cache_path = (
            load_or_build_index(
            cfg.filelist,
            fl,
            cache_dir=cache_dir,
            extra_defines=extra_defines,
            ignore_paths=list(cfg.ignore_path),
            ignore_path_files=list(cfg.ignore_path_file),
            ignore_modules=list(cfg.ignore_module),
            ignore_filelists=list(cfg.ignore_filelist),
            jobs=cfg.jobs,
            use_cache=use_cache,
            refresh_cache=cfg.refresh_cache,
            low_memory=low_memory,
            on_progress=on_progress,
            )
        )
    if index_cache_hit and not cfg.quiet:
        reporter.phase(f"cache hit: index ({len(index.modules)} modules)")

    if cfg.find_top:
        tops = find_top_modules(index)
        elapsed = time.perf_counter() - t0
        lines = ["module\tfile\tstop_reason"]
        for name in tops:
            rec = index.get_module(name)
            file_p = rec.file_path if rec else ""
            stop = index.module_stop_reason(name)
            lines.append(f"{name}\t{file_p}\t{stop}")
        body = "\n".join(lines) + "\n"
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                top_candidates=len(tops),
                mode="find-top",
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
            ),
            log_path=log_path,
        )
        return 0

    try:
        tops = resolve_top_modules(
            index,
            top=cfg.top or (connect_request.top if connect_request else ""),
            filelist_tops=fl.top_modules,
            all_tops=cfg.all_tops,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        print("Hint: hier-walk ... --find-top", file=sys.stderr)
        return 2

    elab_scope = None
    use_scoped_elab = (
        lazy_scoped_connect_elab()
        and connect_run_mode
        and not cone_mode
        and not inst_trace_mode
    )
    if use_scoped_elab:
        pair = tuple(cfg.check_connect) if cfg.check_connect else None
        specs = endpoint_specs_from_request(connect_request, pair=pair)
        if specs:
            top_for_scope = tops[0] if tops else ""
            elab_scope = elab_scope_paths(specs, top=top_for_scope)
            if on_progress:
                on_progress(f"elab: scoped {len(elab_scope)} path(s) for connect")

    def _get_cached_elab(top_name: str):
        if not use_cache or elab_scope is not None:
            return None
        return get_cached_elab(bundle, top_name, cfg.max_depth)

    def _store_cached_elab(
        top_name: str,
        root,
        part,
    ) -> None:
        if elab_scope is not None:
            return
        store_cached_elab(
            bundle,
            top_name,
            cfg.max_depth,
            root,
            part,
            cache_dir=cache_dir,
            use_cache=use_cache,
        )

    roots, rows, elab_cache_hits = elaborate_tops_parallel(
        index,
        tops,
        max_depth=cfg.max_depth,
        scope_paths=elab_scope,
        jobs=cfg.jobs,
        get_cached=_get_cached_elab,
        store_cached=_store_cached_elab,
        on_progress=on_progress,
    )
    if elab_cache_hits and on_progress:
        on_progress(f"cache hit: elab ({elab_cache_hits}/{len(tops)} tops)")
    rows.sort(key=lambda r: (r.full_path.count("."), r.full_path))
    elapsed = time.perf_counter() - t0
    coverage = (
        compute_coverage_audit(index, fl, rows, tops=tops) if rows and tops else None
    )
    if not cfg.quiet and effective_mode == "hierarchy" and rows:
        emit_hierarchy_rows_log(rows, stream=sys.stderr)

    if inst_trace_mode:
        assert cfg.inst_trace is not None
        top_name = (
            cfg.inst_trace.top
            or cfg.top
            or (tops[0] if tops else "")
        )
        _item_t0 = time.perf_counter()
        trace_result = run_inst_trace(
            cfg.inst_trace,
            rows=rows,
            index=index,
            top=top_name,
            defines=extra_defines or None,
        )
        record_verification_item(
            cfg.inst_trace.instance,
            time.perf_counter() - _item_t0,
        )
        if not cfg.quiet:
            emit_path_provenance_log(
                trace_result.instance,
                rows_lookup(rows),
                stream=sys.stderr,
                label="instance",
                prefix="[hier-walk inst-trace]",
            )
        trace_rows = rows_lookup(rows)
        term_stream = sys.stderr if cfg.output == "-" else sys.stdout
        print_inst_trace_report(
            trace_result,
            stream=term_stream,
            rows_by_path=trace_rows,
        )
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as fh:
                print_inst_trace_report(
                    trace_result,
                    stream=fh,
                    rows_by_path=trace_rows,
                )
        body = format_inst_trace_tsv(
            trace_result,
            rows_by_path=trace_rows,
        )
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                elab_tops=tops,
                elab_cache_hits=elab_cache_hits,
                instance_rows=len(rows),
                mode="inst-trace",
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                search_pattern=cfg.inst_trace.instance,
                coverage=coverage,
            ),
            log_path=log_path,
        )
    elif cone_mode:
        top_name = tops[0] if tops else ""
        compile_defines = dict(fl.defines)
        compile_defines.update(extra_defines)
        over_approx = (
            cfg.over_approximate_if
            if cfg.over_approximate_if is not None
            else True
        )
        cone_label = cfg.fanout_cone or cfg.fanin_cone or ""
        _item_t0 = time.perf_counter()
        if cfg.fanout_cone:
            cone_result = fanout_cone(
                cfg.fanout_cone,
                rows=rows,
                index=index,
                top=top_name,
                defines=compile_defines,
                over_approximate_if=over_approx,
            )
            mode_name = "fanout-cone"
        else:
            assert cfg.fanin_cone is not None
            cone_result = fanin_cone(
                cfg.fanin_cone,
                rows=rows,
                index=index,
                top=top_name,
                defines=compile_defines,
                over_approximate_if=over_approx,
            )
            mode_name = "fanin-cone"
        record_verification_item(cone_label, time.perf_counter() - _item_t0)
        if not cfg.quiet:
            emit_path_provenance_log(
                cone_result.origin_scope,
                rows_lookup(rows),
                stream=sys.stderr,
                label="origin",
                prefix="[hier-walk cone]",
            )
        cone_rows = rows_lookup(rows)
        term_stream = sys.stderr if cfg.output == "-" else sys.stdout
        print_cone_report(
            cone_result,
            stream=term_stream,
            rows_by_path=cone_rows,
        )
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as fh:
                print_cone_report(
                    cone_result,
                    stream=fh,
                    rows_by_path=cone_rows,
                )
        if cfg.cone_graph:
            write_cone_dot(cone_result, cfg.cone_graph)
        body = format_cone_tsv(
            cone_result,
            rows_by_path=cone_rows,
        )
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                elab_tops=tops,
                elab_cache_hits=elab_cache_hits,
                instance_rows=len(rows),
                mode=mode_name,
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                search_pattern=cone_label,
                coverage=coverage,
            ),
            log_path=log_path,
        )
    elif connect_run_mode:
        top_name = tops[0] if tops else ""
        compile_defines = dict(fl.defines)
        compile_defines.update(extra_defines)
        use_trace = cfg.connect_trace or cfg.connect_log
        if effective_mode in ("check-connect-batch", "path-walk"):
            request = connect_request
            assert request is not None
            trace_on = request.trace or use_trace
            log_on = request.connect_log or cfg.connect_log
            include_ff = request.include_ff or cfg.include_ff
            if (
                trace_on != request.trace
                or log_on != request.connect_log
                or include_ff != request.include_ff
            ):
                request = ConnectivityRequest(
                    checks=request.checks,
                    top=request.top,
                    defines=request.defines,
                    trace=trace_on,
                    connect_log=log_on,
                    include_ff=include_ff,
                    strict_generate=request.strict_generate,
                    over_approximate_if=request.over_approximate_if,
                )
            batch = run_connectivity_request(
                request,
                rows=rows,
                index=index,
                top=top_name,
                extra_defines=compile_defines,
                jobs=cfg.jobs,
            )
            connect_results = batch.results
            endpoint_rows = rows_lookup(rows)
            body = format_connect_results_tsv(
                connect_results,
                modules_cached=batch.modules_cached,
                rows_by_path=endpoint_rows,
            )
        else:
            assert cfg.check_connect is not None
            _item_t0 = time.perf_counter()
            result = check_connectivity(
                cfg.check_connect[0],
                cfg.check_connect[1],
                rows=rows,
                index=index,
                top=top_name,
                defines=compile_defines,
                trace=use_trace,
                ff_barrier=not cfg.include_ff,
                strict_generate=cfg.strict_generate,
                over_approximate_if=cfg.over_approximate_if,
            )
            record_connect_check(
                check_id="",
                endpoint_a=cfg.check_connect[0],
                endpoint_b=cfg.check_connect[1],
                elapsed_sec=time.perf_counter() - _item_t0,
            )
            connect_results = [result]
            endpoint_rows = rows_lookup(rows)
            body = format_connect_results_tsv(
                connect_results,
                rows_by_path=endpoint_rows,
            )
        if not cfg.quiet:
            for result in connect_results:
                emit_connect_trace_log(
                    result,
                    stream=sys.stderr,
                    check_prefix=result.check_id or "",
                    rows_by_path=endpoint_rows,
                )
        if use_trace:
            term_stream = sys.stderr if cfg.output == "-" else sys.stdout
            print_connect_trace_reports(
                connect_results,
                stream=term_stream,
                rows_by_path=endpoint_rows,
            )
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as fh:
                    print_connect_trace_reports(
                        connect_results,
                        stream=fh,
                        title="connectivity path evidence (log)",
                        rows_by_path=endpoint_rows,
                    )
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                elab_tops=tops,
                elab_cache_hits=elab_cache_hits,
                instance_rows=len(rows),
                mode=effective_mode,
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                coverage=coverage,
                connect_results=connect_results,
                connect_phase=(
                    _verification_phase(cfg)
                    if _verification_phase(cfg) in ("text", "logical", "both")
                    else "logical"
                ),
            ),
            log_path=log_path,
        )
    elif (search_spec := effective_search_spec(cfg)) is not None:
        hits = execute_search_spec(rows, index, search_spec)
        if search_spec.instance or search_spec.path:
            need_chain = [h for h in hits if not h.path_chain]
        else:
            need_chain = []
        if need_chain:
            top_name = tops[0] if tops else ""
            attach_path_chains(
                need_chain, index, rows, top=top_name, refine_paths=False
            )
        hits.sort(key=lambda h: h.full_path)
        lines = [
            "full_path\tmatched\tmodule\tdepth\tfile\t"
            "via_filelist\tfilelist_chain\tstop_reason\tkind\t"
            "port\tport_found\tport_line\tport_decl\tport_param_note\t"
            "path_chain"
        ]
        for h in hits:
            lines.append(
                f"{h.full_path}\t{h.matched_name}\t{h.module}\t"
                f"{h.depth}\t{h.file}\t{h.via_filelist}\t{h.filelist_chain}\t"
                f"{h.stop_reason}\t{h.match_kind}\t"
                f"{h.port_name}\t{h.port_found}\t{h.port_line}\t"
                f"{h.port_decl}\t{h.port_param_note}\t"
                f"{format_path_chain_compact(h.path_chain)}"
            )
        body = "\n".join(lines) + "\n"
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                elab_tops=tops,
                elab_cache_hits=elab_cache_hits,
                instance_rows=len(rows),
                search_hits=len(hits),
                search_pattern=search_spec.summary_pattern(),
                search_hit_details=hits,
                mode="search",
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                coverage=coverage,
            ),
            log_path=log_path,
        )
    else:
        lines = [
            "full_path\tinst_leaf\tmodule\tdepth\tfile\t"
            "stop_reason\tvia_filelist\tfilelist_chain"
        ]
        for r in rows:
            lines.append(
                f"{r.full_path}\t{r.inst_leaf}\t{r.module}\t"
                f"{r.depth}\t{r.file}\t{r.stop_reason}\t"
                f"{r.via_filelist}\t{r.filelist_chain}"
            )
        body = "\n".join(lines) + "\n"
        if cfg.output == "-":
            sys.stdout.write(body)
        else:
            with open(cfg.output, "w", encoding="utf-8") as f:
                f.write(body)
        emit_run_report(
            RunReport(
                filelist_path=cfg.filelist,
                elapsed_sec=elapsed,
                fl=fl,
                index=index,
                cache_path=cache_path if use_cache else None,
                cache_enabled=use_cache,
                index_cache_hit=index_cache_hit,
                index_rebuilt=index_rebuilt,
                index_incremental=index_incremental,
                elab_tops=tops,
                elab_cache_hits=elab_cache_hits,
                instance_rows=len(rows),
                mode="hierarchy",
                output_path=cfg.output,
                filelist_warnings=len(fl.errors),
                coverage=coverage,
                hierarchy_rows=rows,
            ),
            log_path=log_path,
        )
    return 0


