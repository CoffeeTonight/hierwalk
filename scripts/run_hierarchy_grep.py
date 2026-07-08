#!/usr/bin/env python3
"""Standalone hierarchy_grep gate runner (RUN.json input; ``grep_hie.json`` cache)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Tuple

from hierwalk.cache import (
    resolve_run_work_dir,
    resolve_top_label,
    set_active_work_dir,
    work_base_dir,
)
from hierwalk.connect.session import format_connect_results_tsv
from hierwalk.connect.shared.request import ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.lazy_scope import lazy_filelist_defer_exists
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import (
    RunConfig,
    apply_config_env_from_document,
    read_json_document,
    resolve_connectivity_request,
)
from hierwalk.run_tests import (
    RUN_CONN_CHECK,
    build_test_run_configs,
    expand_suite_verification_plan,
    try_parse_run_test_suite,
)


def _load_hgrep_run(
    config_path: Path,
) -> Tuple[RunConfig, ConnectivityRequest]:
    """Load RUN.json (flat suite or legacy) and pick hgrep ``run_conn_check``."""
    if not config_path.is_file():
        raise SystemExit(f"missing config: {config_path}")

    data = read_json_document(config_path)
    if not isinstance(data, dict):
        raise SystemExit(f"config must be a JSON object: {config_path}")

    env_applied = apply_config_env_from_document(data)
    if env_applied:
        print(
            f"run: config-env: applied JSON env ({len(env_applied)} keys)",
            file=sys.stderr,
        )

    base_dir = config_path.parent
    suite = try_parse_run_test_suite(data, base_dir=base_dir)
    if suite is not None:
        plan = expand_suite_verification_plan(
            build_test_run_configs(suite, data, base_dir=base_dir)
        )
        hits: list[Tuple[RunConfig, ConnectivityRequest]] = []
        for entry, cfg in plan:
            if entry is None or entry.kind != RUN_CONN_CHECK:
                continue
            phase = (cfg.verification_phase or "").strip().lower()
            if phase != "hgrep":
                continue
            req = resolve_connectivity_request(cfg)
            if req is None:
                raise SystemExit(
                    f"{entry.name or RUN_CONN_CHECK}: connect_phase=hgrep but no checks"
                )
            hits.append((cfg, req))
        if not hits:
            raise SystemExit(
                "no enabled run_conn_check with connect_phase=hgrep in RUN.json"
            )
        if len(hits) > 1:
            print(
                f"run: note: {len(hits)} hgrep steps; running first only",
                file=sys.stderr,
            )
        return hits[0]

    from hierwalk.run_request import parse_shared_run_request_json

    cfg = parse_shared_run_request_json(data, base_dir=base_dir)
    if not cfg.filelist:
        raise SystemExit("RUN.json needs filelist (flat suite or top-level)")
    inline = data.get("connect") if isinstance(data.get("connect"), dict) else data
    cfg = replace(
        cfg,
        verification_phase="hgrep",
        connect_inline=inline,
    )
    req = resolve_connectivity_request(cfg)
    if req is None:
        raise SystemExit("RUN.json has no checks (run_conn_check.checks or connect)")
    return cfg, req


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run hierarchy_grep gate from RUN.json (flat suite or legacy). "
            "Applies JSON env before filelist parse; caches under .db_{TOP}/grep_hie.json."
        )
    )
    ap.add_argument(
        "config",
        help="RUN.json with filelist, env, run_conn_check.connect_phase=hgrep",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="",
        help="override output TSV path (default: run_conn_check output or stdout)",
    )
    ap.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help="override work/cache root",
    )
    ap.add_argument(
        "--refresh-cache",
        action="store_true",
        help="delete grep_hie.json and rebuild (also honors JSON refresh-cache)",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="disable index/elab disk cache",
    )
    args = ap.parse_args(argv)

    config_path = Path(args.config).expanduser().resolve()
    cfg, request = _load_hgrep_run(config_path)

    if not cfg.filelist:
        print("RUN.json missing filelist", file=sys.stderr)
        return 2

    fl = parse_filelist(
        cfg.filelist,
        index_cwd=cfg.index_cwd,
        extra_defines=cfg.defines_map,
        ignore_filelists=list(cfg.ignore_filelist),
        defer_source_exists=lazy_filelist_defer_exists(),
    )
    if not fl.source_files:
        print("No sources in filelist", file=sys.stderr)
        return 2

    top_name = resolve_top_label(
        cfg_top=cfg.top or "",
        connect_top=request.top,
        filelist_tops=list(fl.top_modules),
        filelist_path=cfg.filelist,
    )
    if not top_name:
        print("top module required in RUN.json", file=sys.stderr)
        return 2

    work_dir = resolve_run_work_dir(
        top_name,
        base=work_base_dir(cfg.index_cwd),
        explicit_cache_dir=args.cache_dir or cfg.cache_dir,
    )
    set_active_work_dir(work_dir)
    print(f"run: work-dir: {work_dir} (top={top_name})", file=sys.stderr)
    print(
        f"run: filelist: {len(fl.source_files)} RTL sources from {cfg.filelist}",
        file=sys.stderr,
    )

    output = args.output or cfg.output or "-"
    refresh_cache = args.refresh_cache or cfg.refresh_cache
    no_cache = args.no_cache or cfg.no_cache

    batch, _index, _state = run_path_walk_connect(
        request,
        fl,
        top=top_name,
        no_cache=no_cache,
        refresh_cache=refresh_cache,
        connect_phase="hgrep",
        connect_output_dir=work_dir,
        connect_output_name=output if output != "-" else "conn.tsv",
        cache_dir=work_dir,
    )
    body = format_connect_results_tsv(
        batch.results,
        modules_cached=batch.modules_cached,
        phase="hgrep",
    )
    if output == "-":
        sys.stdout.write(body)
    else:
        Path(output).write_text(body, encoding="utf-8")
    return 0 if all(r.connected for r in batch.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())