"""hgconn CLI — bloom text connectivity from hgpath DBs."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from hg_core.log import emit_hg_log, hg_log_path
from hg_core.report import ReportBuilder, format_elapsed_sec
from hg_core.summary import append_hgconn_summary
from hg_core.run_config import load_hg_run_config, require_hg_run_config
from hgpath.batch import run_batch
from hgpath.flat_db import load_or_build_flat_db
from hgpath.tree_db import TreeDb

from hgconn.walk import run_bloom_batch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hgconn: bloom text-conn from hgpath DBs.")
    ap.add_argument("--work-dir", required=True, help="hgpath work directory")
    ap.add_argument("--checks", required=True, help="checks or RUN JSON")
    ap.add_argument("--top", default="", help="top module (or JSON top)")
    ap.add_argument("--filelist", default="", help="optional filelist if flat DB missing")
    ap.add_argument("--index-cwd", default="", help="override JSON index-cwd")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    work = Path(args.work_dir).expanduser().resolve()
    log_fh = hg_log_path(work, "hgconn").open("a", encoding="utf-8")

    def on_log(msg: str) -> None:
        emit_hg_log(msg, tool="hgconn", log_file=log_fh)

    checks_path = Path(args.checks).expanduser().resolve()
    cfg = load_hg_run_config(
        checks_path,
        filelist_cli=args.filelist,
        top_cli=args.top,
        index_cwd_cli=args.index_cwd,
    )
    tree = TreeDb.load(work)
    need_filelist = not tree.entries

    try:
        cfg = require_hg_run_config(cfg, need_filelist=need_filelist)
    except SystemExit as exc:
        emit_hg_log(f"error: {exc}", tool="hgconn", log_file=log_fh)
        return 2

    if cfg.env_applied:
        on_log(f"config-env applied keys={','.join(cfg.env_applied)}")

    emit_hg_log("begin", tool="hgconn", log_file=log_fh)
    checks = cfg.checks
    if not checks:
        emit_hg_log("error: no checks in JSON", tool="hgconn", log_file=log_fh)
        return 2

    if need_filelist and not cfg.filelist:
        emit_hg_log("error: empty tree DB; run hgpath first", tool="hgconn", log_file=log_fh)
        return 2

    session = None
    filelist = cfg.filelist or str(args.filelist or "").strip()
    if filelist:
        from hierwalk.filelist import parse_filelist

        fl = parse_filelist(
            filelist,
            index_cwd=cfg.index_cwd,
            extra_defines=cfg.defines or None,
        )
        sources = [str(p) for p in fl.source_files]
        _flat, session = load_or_build_flat_db(
            sources, top=cfg.top, work_dir=work, on_log=on_log
        )
        if cfg.index_cwd:
            on_log(f"index-cwd={cfg.index_cwd}")
    else:
        from hierwalk.hierarchy_grep import HierarchyGrepSession, load_grep_hie

        from hgpath.flat_db import resolve_flat_db_path

        flat_path = resolve_flat_db_path(work)
        cached = load_grep_hie(flat_path)
        session = HierarchyGrepSession.from_grep_hie_cache(cached, cache_path=flat_path)

    batch = run_batch(checks, top=cfg.top, session=session, tree=tree, on_log=on_log)
    results = run_bloom_batch(batch.check_results, on_log=on_log)

    connected = sum(1 for r in results if r.connected)
    report = ReportBuilder(title="hgconn report", tool="hgconn", started_at=t0)
    report.add(f"top: {cfg.top}")
    report.add(f"checks: {len(checks)}")
    append_hgconn_summary(
        report,
        entries=batch.entries,
        check_results=batch.check_results,
        conn_results=results,
    )
    report.add("")
    report.add("--- timing ---")
    report.add(f"total_elapsed: {format_elapsed_sec(t0)}")
    report_path = work / "hgconn.report"
    report.finish(report_path)

    emit_hg_log(
        f"done connected={connected}/{len(checks)} elapsed={format_elapsed_sec(t0)} "
        f"report={report_path}",
        tool="hgconn",
        log_file=log_fh,
    )
    log_fh.close()
    return 0 if connected == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())