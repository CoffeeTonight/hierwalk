#!/usr/bin/env python3
"""200-check RTL read count benchmark for hgpath tree cache."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import hierwalk.hierarchy_grep as hg
from hierwalk.connect.shared.request import ConnectivityCheck

from hgpath.batch import run_batch
from hgpath.flat_db import load_or_build_flat_db
from hgpath.tree_db import TreeDb, resolve_tree_db_path


def main() -> int:
    demo = Path(__file__).resolve().parents[2] / "hgrep_demo"
    top_v = demo / "top_many.v"
    if not top_v.is_file():
        print("missing top_many.v", file=sys.stderr)
        return 2

    read_count = 0
    orig = hg._read_text

    def counting_read(path):
        nonlocal read_count
        read_count += 1
        return orig(path)

    hg._read_text = counting_read
    work = demo / ".bench_hgpath"
    _db, session = load_or_build_flat_db([str(top_v)], top="top", work_dir=work)
    tree = TreeDb(work_dir=work, path=resolve_tree_db_path(work))
    checks = tuple(
        ConnectivityCheck(
            f"top.u_{i % 5}.out",
            f"top.u_{(i + 2) % 5}.out",
            check_id=f"hg{i:03d}",
        )
        for i in range(200)
    )
    t0 = time.perf_counter()
    run_batch(checks, top="top", session=session, tree=tree)
    elapsed = time.perf_counter() - t0
    report = {
        "checks": 200,
        "rtl_read_calls": read_count,
        "unique_files": 1,
        "elapsed_sec": round(elapsed, 2),
        "cache_ok": read_count <= 1,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["cache_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())