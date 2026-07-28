"""CLI: filelist expand + essential DB build + run-config JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _parse_defines(items: list[str]) -> dict[str, str]:
    extra: dict[str, str] = {}
    for d in items:
        if "=" in d:
            k, v = d.split("=", 1)
        else:
            k, v = d, "1"
        extra[k.strip()] = v.strip()
    return extra


def _add_common_compile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Run JSON/JSONC (filelist, top, cwd, defines, build_db, …)",
    )
    p.add_argument("--cwd", type=Path, default=None, help="EDA run dir for -F (overrides config)")
    p.add_argument("--top", default="", help="Top module (overrides config)")
    p.add_argument(
        "--define",
        action="append",
        default=[],
        metavar="NAME[=VAL]",
        help="Extra +define+ (merged on top of config defines)",
    )


def _load_merged_config(
    *,
    config: Optional[Path],
    filelist: Optional[Path],
    cwd: Optional[Path],
    top: str,
    define: list[str],
    db: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    require_filelist: bool = True,
):
    from pyhirewalk.run_config import RunConfig, load_run_config, merge_run_config

    if config is not None:
        cfg = load_run_config(config)
        cfg = merge_run_config(
            cfg,
            filelist=filelist,
            top=top or None,
            index_cwd=cwd,
            defines=_parse_defines(define) or None,
            db_path=db,
            work_dir=work_dir,
        )
    else:
        if require_filelist and filelist is None:
            raise SystemExit("need filelist path or --config")
        if filelist is None:
            raise SystemExit("need filelist path or --config")
        cfg = RunConfig(
            filelist=filelist.resolve(),
            top=top or "",
            index_cwd=cwd.resolve() if cwd else None,
            defines=_parse_defines(define),
            db_path=db.resolve() if db else None,
            work_dir=work_dir.resolve() if work_dir else None,
        )
    return cfg


def _run_build_db(cfg, *, quiet: bool, as_json: bool) -> int:
    from pyhirewalk.index.build_db import build_essential_db

    if cfg.db_path is None:
        print("build-db needs -o/--db or config build_db.output", file=sys.stderr)
        return 2

    def progress(msg: str) -> None:
        if not quiet:
            print(f"[pyhirewalk] {msg}", file=sys.stderr)

    if not quiet and cfg.config_path:
        print(f"[pyhirewalk] config: {cfg.config_path}", file=sys.stderr)
        print(
            f"[pyhirewalk] defines: {len(cfg.defines)} macros",
            file=sys.stderr,
        )

    result = build_essential_db(
        cfg.filelist,
        cfg.db_path,
        index_cwd=cfg.index_cwd,
        top=cfg.top or None,
        extra_defines=cfg.defines or None,
        work_dir=cfg.work_dir,
        on_progress=progress,
    )

    if as_json:
        payload = result.summary()
        payload["defines"] = dict(cfg.defines)
        payload["config"] = str(cfg.config_path) if cfg.config_path else None
        print(json.dumps(payload, indent=2))
    else:
        print(f"db:          {result.db_path}")
        print(f"context_id:  {result.context_id}")
        print(f"files:       {result.n_files}")
        print(
            f"modules:     {result.n_modules}  "
            f"(unique names: {result.n_unique_module_names})"
        )
        print(f"defines:     {len(cfg.defines)}")
        print(f"pyslang:     {result.pyslang_version or '(unknown)'}")
        print("timings (sec):")
        for k, v in result.timings.items():
            print(f"  {k:22s} {v:10.3f}")
        if result.warnings:
            print(f"warnings:    {len(result.warnings)}")
            for w in result.warnings[:5]:
                print(f"  - {w}")
        if result.errors:
            print(f"errors:      {len(result.errors)}")
            for e in result.errors[:10]:
                print(f"  - {e}")

    if result.n_modules == 0 and result.errors:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyhirewalk",
        description="RTL hierarchy COI toolkit (essential index / filelist)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    fl = sub.add_parser("filelist", help="Expand an EDA filelist and print summary")
    fl.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help="Top-level .f (optional if --config)",
    )
    _add_common_compile_args(fl)
    fl.add_argument(
        "--write-slang-f",
        type=Path,
        default=None,
        help="Write flattened slang-safe filelist",
    )
    fl.add_argument("--json", action="store_true", help="Print summary JSON")

    bd = sub.add_parser(
        "build-db",
        help="Build essential SQLite index (files + module→file) and print timings",
    )
    bd.add_argument(
        "filelist",
        type=Path,
        nargs="?",
        default=None,
        help="Top-level .f (optional if --config)",
    )
    bd.add_argument(
        "-o",
        "--db",
        type=Path,
        default=None,
        help="Output .sqlite (or set build_db.output in --config)",
    )
    _add_common_compile_args(bd)
    bd.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Directory for intermediate flat .f",
    )
    bd.add_argument("--quiet", action="store_true", help="Less progress on stderr")
    bd.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable timing summary JSON",
    )

    run = sub.add_parser(
        "run",
        help="Load run JSON and execute (currently: build_db when db/output set)",
    )
    run.add_argument("config", type=Path, help="Run JSON/JSONC path")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--json", action="store_true")
    run.add_argument(
        "--define",
        action="append",
        default=[],
        metavar="NAME[=VAL]",
        help="Extra +define+ merged on top of config",
    )
    run.add_argument("--top", default="", help="Override top")
    run.add_argument("--cwd", type=Path, default=None, help="Override cwd")
    run.add_argument("-o", "--db", type=Path, default=None, help="Override db path")

    args = parser.parse_args(argv)

    if args.cmd == "filelist":
        from pyhirewalk.context import build_context

        try:
            cfg = _load_merged_config(
                config=args.config,
                filelist=args.path,
                cwd=args.cwd,
                top=args.top,
                define=args.define,
            )
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

        ctx = build_context(
            cfg.filelist,
            index_cwd=cfg.index_cwd,
            extra_defines=cfg.defines or None,
            top=cfg.top or None,
        )
        if args.write_slang_f:
            out = ctx.write_slang_filelist(args.write_slang_f)
            print(f"wrote {out}", file=sys.stderr)

        if args.json:
            payload = ctx.summary()
            payload["defines"] = {**ctx.defines}
            payload["sources"] = [str(p) for p in ctx.source_files]
            payload["errors"] = list(ctx.errors)
            payload["config"] = str(cfg.config_path) if cfg.config_path else None
            print(json.dumps(payload, indent=2))
        else:
            s = ctx.summary()
            print(f"context_id: {s['context_id']}")
            print(f"sources:    {s['n_sources']}")
            print(f"incdirs:    {s['n_incdirs']}")
            print(f"defines:    {s['n_defines']}")
            if cfg.defines:
                for k, v in sorted(cfg.defines.items())[:20]:
                    print(f"  +define+{k}={v}")
                if len(cfg.defines) > 20:
                    print(f"  … {len(cfg.defines) - 20} more")
            print(f"tops:       {', '.join(s['top_modules']) or cfg.top or '(none)'}")
            print(f"filelists:  {s['n_filelist_edges']} nested edges")
            if ctx.errors:
                print(f"errors:     {len(ctx.errors)}")
                for e in ctx.errors[:10]:
                    print(f"  - {e}")
        return 1 if ctx.errors else 0

    if args.cmd == "build-db":
        try:
            cfg = _load_merged_config(
                config=args.config,
                filelist=args.filelist,
                cwd=args.cwd,
                top=args.top,
                define=args.define,
                db=args.db,
                work_dir=args.work_dir,
            )
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return _run_build_db(cfg, quiet=args.quiet, as_json=args.json)

    if args.cmd == "run":
        try:
            cfg = _load_merged_config(
                config=args.config,
                filelist=None,
                cwd=args.cwd,
                top=args.top,
                define=args.define,
                db=args.db,
                require_filelist=True,
            )
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if cfg.db_path is None:
            print(
                "run: no build_db.output / db in config — nothing to do yet",
                file=sys.stderr,
            )
            return 2
        return _run_build_db(cfg, quiet=args.quiet, as_json=args.json)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
