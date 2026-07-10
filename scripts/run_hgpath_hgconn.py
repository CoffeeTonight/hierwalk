#!/usr/bin/env python3
"""Run hgpath then hgconn in sequence."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hgpath → hgconn orchestrator")
    ap.add_argument("filelist", nargs="?", default="")
    ap.add_argument("--top", default="")
    ap.add_argument("--checks", required=True)
    ap.add_argument("--index-cwd", default="")
    ap.add_argument("--work-dir", default=".db_hgpath")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    src = Path(__file__).resolve().parents[1] / "src"
    env = {**dict(**__import__("os").environ), "PYTHONPATH": str(src)}
    py = sys.executable
    work = args.work_dir

    hgpath_cmd = [py, "-m", "hgpath.cli", "--checks", args.checks, "--work-dir", work]
    if args.filelist:
        hgpath_cmd.append(args.filelist)
    if args.top:
        hgpath_cmd.extend(["--top", args.top])
    if args.index_cwd:
        hgpath_cmd.extend(["--index-cwd", args.index_cwd])
    if args.refresh:
        hgpath_cmd.append("--refresh")
    rc1 = subprocess.call(hgpath_cmd, env=env)
    if rc1 != 0:
        return rc1
    hgconn_cmd = [
        py, "-m", "hgconn.cli", "--work-dir", work, "--checks", args.checks,
    ]
    if args.top:
        hgconn_cmd.extend(["--top", args.top])
    if args.filelist:
        hgconn_cmd.extend(["--filelist", args.filelist])
    if args.index_cwd:
        hgconn_cmd.extend(["--index-cwd", args.index_cwd])
    return subprocess.call(hgconn_cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())