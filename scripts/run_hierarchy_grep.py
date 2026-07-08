#!/usr/bin/env python3
"""Standalone hierarchy_grep gate runner (reuses ``.db_{TOP}/grep_hie.json``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hierwalk.connect.session import format_connect_results_tsv
from hierwalk.connect.shared.request import ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import (
    load_connect_request,
    try_parse_connect_request_json,
)


def _load_request(path: Path, *, top: str) -> ConnectivityRequest:
    text = path.read_text(encoding="utf-8-sig").lstrip()
    if path.suffix.lower() == ".json" or text.startswith(("{", "[")):
        data = json.loads(text)
        req = try_parse_connect_request_json(data)
        if req is None:
            raise SystemExit(f"not a connect checks JSON: {path}")
        if top and not req.top:
            req = ConnectivityRequest(
                checks=req.checks,
                top=top,
                defines=req.defines,
                trace=req.trace,
                connect_log=req.connect_log,
                include_ff=req.include_ff,
                strict_generate=req.strict_generate,
                over_approximate_if=req.over_approximate_if,
            )
        return req
    req = load_connect_request(str(path))
    if top and not req.top:
        req = ConnectivityRequest(
            checks=req.checks,
            top=top,
            defines=req.defines,
            trace=req.trace,
            connect_log=req.connect_log,
            include_ff=req.include_ff,
            strict_generate=req.strict_generate,
            over_approximate_if=req.over_approximate_if,
        )
    return req


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run hierarchy_grep gate checks only (no connect-coi). "
            "Caches grep index under .db_{TOP}/grep_hie.json."
        )
    )
    ap.add_argument(
        "batch",
        help="checks JSON or text pairs file (same format as --check-connect-batch)",
    )
    ap.add_argument("-f", "--filelist", required=True, help="Verilog filelist (.f)")
    ap.add_argument("--top", default="", help="top module (overrides batch JSON)")
    ap.add_argument("-o", "--output", default="-", help="TSV output (default: stdout)")
    ap.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help="work/cache root (default: .db_{TOP} under filelist cwd)",
    )
    ap.add_argument(
        "--refresh-cache",
        action="store_true",
        help="delete grep_hie.json and rebuild hierarchy grep index",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="disable index/elab disk cache (grep_hie.json still written unless --refresh-cache clears it)",
    )
    args = ap.parse_args(argv)

    batch_path = Path(args.batch)
    fl_path = Path(args.filelist)
    if not batch_path.is_file():
        print(f"missing batch file: {batch_path}", file=sys.stderr)
        return 2
    if not fl_path.is_file():
        print(f"missing filelist: {fl_path}", file=sys.stderr)
        return 2

    request = _load_request(batch_path, top=args.top)
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    top_name = (request.top or args.top or "").strip()
    if not top_name and fl.top_modules:
        top_name = str(fl.top_modules[0]).strip()
    if not top_name:
        print("top module required (--top or batch JSON)", file=sys.stderr)
        return 2

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    batch, _index, _state = run_path_walk_connect(
        request,
        fl,
        top=top_name,
        no_cache=args.no_cache,
        refresh_cache=args.refresh_cache,
        connect_phase="hgrep",
        cache_dir=cache_dir,
    )
    body = format_connect_results_tsv(
        batch.results,
        modules_cached=batch.modules_cached,
        phase="hgrep",
    )
    if args.output == "-":
        sys.stdout.write(body)
    else:
        Path(args.output).write_text(body, encoding="utf-8")
    return 0 if all(r.connected for r in batch.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())