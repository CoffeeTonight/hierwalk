"""hgpath CLI — phase 1 hierarchy existence (flat + tree DB)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from hg_core.log import emit_hg_log, hg_log_path
from hg_core.report import ReportBuilder, format_elapsed_sec
from hg_core.summary import append_hgpath_summary
from hg_core.run_config import load_hg_run_config, require_hg_run_config
from hgpath.batch import run_batch
from hgpath.flat_db import load_or_build_flat_db, try_load_flat_db_cache
from hgpath.tree_db import TreeDb, resolve_tree_db_path


def _parse_filelist(cfg, on_log):
    from hierwalk.filelist import parse_filelist

    fl = parse_filelist(
        cfg.filelist,
        index_cwd=cfg.index_cwd,
        extra_defines=cfg.defines or None,
    )
    if cfg.index_cwd and on_log:
        on_log(f"index-cwd={cfg.index_cwd}")
    if cfg.defines and on_log:
        on_log(f"defines={len(cfg.defines)}")
    return fl


def _load_flat_and_filelist(cfg, work, refresh, on_log):
    from hierwalk.filelist import filelist_result_from_grep_hie

    if not refresh:
        hit = try_load_flat_db_cache(
            work_dir=work,
            filelist=cfg.filelist,
            index_cwd=cfg.index_cwd,
            on_log=on_log,
        )
        if hit is not None:
            flat_db, session = hit
            fl = filelist_result_from_grep_hie(
                {"rtl_paths": session.rtl_paths, "top": cfg.top},
                top=cfg.top,
                defines=cfg.defines or None,
            )
            if cfg.index_cwd and on_log:
                on_log(f"index-cwd={cfg.index_cwd}")
            return flat_db, session, fl

    fl = _parse_filelist(cfg, on_log)
    sources = [str(p) for p in fl.source_files]
    flat_db, session = load_or_build_flat_db(
        sources,
        top=cfg.top,
        work_dir=work,
        refresh=refresh,
        filelist=cfg.filelist,
        index_cwd=cfg.index_cwd,
        on_log=on_log,
    )
    return flat_db, session, fl


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="hgpath: hierarchy existence (flat + tree DB)."
    )
    ap.add_argument("filelist", nargs="?", default="", help="RTL filelist (.f)")
    ap.add_argument("--top", default="", help="top module name")
    ap.add_argument(
        "--checks",
        default="",
        help="checks or RUN JSON (env, index-cwd, filelist, top, checks)",
    )
    ap.add_argument("--index-cwd", default="", help="override JSON index-cwd")
    ap.add_argument("--work-dir", default=".db_hgpath", help="cache directory")
    ap.add_argument("--refresh", action="store_true", help="rebuild flat DB")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    work = Path(args.work_dir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    log_path = hg_log_path(work, "hgpath")
    log_fh = log_path.open("a", encoding="utf-8")

    def on_log(msg: str) -> None:
        emit_hg_log(msg, tool="hgpath", log_file=log_fh)

    cfg = None
    checks_path = Path(args.checks).expanduser().resolve() if args.checks else None
    if checks_path is not None:
        cfg = load_hg_run_config(
            checks_path,
            filelist_cli=args.filelist,
            top_cli=args.top,
            index_cwd_cli=args.index_cwd,
        )
    else:
        from hg_core.run_config import HgRunConfig

        cfg = HgRunConfig(
            filelist=str(args.filelist or "").strip(),
            top=str(args.top or "").strip(),
            index_cwd=str(args.index_cwd or "").strip() or None,
        )

    try:
        cfg = require_hg_run_config(cfg)
    except SystemExit as exc:
        emit_hg_log(f"error: {exc}", tool="hgpath", log_file=log_fh)
        return 2

    if cfg.env_applied:
        on_log(f"config-env applied keys={','.join(cfg.env_applied)}")

    emit_hg_log(f"begin filelist={cfg.filelist} top={cfg.top}", tool="hgpath", log_file=log_fh)

    flat_db, session, fl = _load_flat_and_filelist(cfg, work, args.refresh, on_log)
    if not fl.source_files:
        emit_hg_log("error: no sources", tool="hgpath", log_file=log_fh)
        return 2

    if cfg.defines:
        session.defines = dict(cfg.defines)
        on_log(
            f"instance-scan defines={len(cfg.defines)} "
            f"(compile-accurate ifdef from JSON; default scans all conditional branches)"
        )
    elif fl.defines:
        on_log(
            f"filelist defines={len(fl.defines)} (not applied to instance scan; "
            f"add JSON \"defines\" only for compile-accurate ifdef filtering)"
        )
    on_log(f"flat-ready modules={flat_db.module_count} rtl_files={flat_db.rtl_file_count}")
    if flat_db.rtl_file_count == 0:
        emit_hg_log("error: filelist expanded to 0 RTL files", tool="hgpath", log_file=log_fh)
        return 2

    tree = TreeDb.load(work)
    if args.refresh and tree.path.is_file():
        tree.path.unlink()
        tree = TreeDb(work_dir=work, path=resolve_tree_db_path(work))

    checks = cfg.checks
    batch = None
    if checks:
        batch = run_batch(checks, top=cfg.top, session=session, tree=tree, on_log=on_log)
        if tree.save_if_changed():
            on_log(f"tree-saved path={tree.path} nodes={tree.node_count}")
        else:
            on_log(f"tree-unchanged path={tree.path} nodes={tree.node_count}")

    report = ReportBuilder(title="hgpath report", tool="hgpath", started_at=t0)
    report.add(f"top: {cfg.top}")
    report.add(f"checks: {len(checks)}")
    if batch is not None:
        append_hgpath_summary(
            report,
            entries=batch.entries,
            check_results=batch.check_results,
        )
    else:
        report.add("(no checks in input JSON — hierarchy summary skipped)")
    report.add("")
    report.add("--- db / cache ---")
    report.add(f"flat_db: {flat_db.path}")
    report.add(f"tree_db: {tree.path}")
    report.add(f"modules: {flat_db.module_count}")
    report.add(f"rtl_files: {flat_db.rtl_file_count}")
    report.add(f"tree_nodes: {tree.node_count}")
    report.add(f"total_elapsed: {format_elapsed_sec(t0)}")
    report_path = work / "hgpath.report"
    report.finish(report_path)

    emit_hg_log(
        f"done modules={flat_db.module_count} tree_nodes={tree.node_count} "
        f"elapsed={format_elapsed_sec(t0)} report={report_path}",
        tool="hgpath",
        log_file=log_fh,
    )
    log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())