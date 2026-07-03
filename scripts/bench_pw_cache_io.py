#!/usr/bin/env python3
"""Micro-benchmark: tier0/tier1 disk cache read/write (pickle vs sqlite)."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk_db import PathWalkModuleDb, path_walk_db_cache_key
from hierwalk.preprocess import clear_include_unit_cache


def _write_files(root: Path, n: int) -> Path:
    paths: list[str] = []
    for i in range(n):
        p = root / f"mod_{i}.v"
        p.write_text(
            f"module mod_{i} (input a, output z);\n  assign z = a;\nendmodule\n",
            encoding="utf-8",
        )
        paths.append(str(p.resolve()))
    fl = root / "design.f"
    fl.write_text("\n".join(paths) + "\n", encoding="utf-8")
    return fl


def _build_db(fl_path: Path, cache_dir: Path, backend: str) -> PathWalkModuleDb:
    os.environ["HIERWALK_PW_CACHE"] = backend
    clear_include_unit_cache()
    fl = parse_filelist(fl_path)
    sources = [str(Path(p).resolve()) for p in fl.source_files]
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines=dict(fl.defines),
    )
    cache_key = path_walk_db_cache_key(
        sources,
        defines={},
        include_dirs=[str(p) for p in fl.include_dirs],
        skip_path_patterns=[],
    )
    return PathWalkModuleDb(
        sources,
        index,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines={},
        cache_dir=cache_dir,
        cache_key=cache_key,
        jobs=4,
    )


def _populate_tier0(db: PathWalkModuleDb) -> float:
    t0 = time.perf_counter()
    for src in db._sources:
        db._tier0_scan_file(src)
    return (time.perf_counter() - t0) * 1000.0


def _populate_tier1(db: PathWalkModuleDb, limit: int) -> float:
    t0 = time.perf_counter()
    for src in db._sources[:limit]:
        db.tier1_scan_file(src)
    return (time.perf_counter() - t0) * 1000.0


def _reread_tier0(db: PathWalkModuleDb) -> float:
    db._regex_scanned.clear()
    db.cache_regex_hits = 0
    t0 = time.perf_counter()
    for src in db._sources:
        db._tier0_scan_file(src)
    return (time.perf_counter() - t0) * 1000.0


def _reread_tier1(db: PathWalkModuleDb, limit: int) -> float:
    db._validated_memory.clear()
    db.cache_validated_hits = 0
    t0 = time.perf_counter()
    for src in db._sources[:limit]:
        db.tier1_scan_file(src)
    return (time.perf_counter() - t0) * 1000.0


def _artifact_stats(db: PathWalkModuleDb) -> dict:
    root = db.cache_root
    if root is None:
        return {"files": 0, "kb": 0}
    sqlite = root / "pw_cache.sqlite"
    if sqlite.is_file():
        return {"files": 1, "kb": sqlite.stat().st_size // 1024}
    pkls = list(root.rglob("*.pkl"))
    total = sum(p.stat().st_size for p in pkls)
    return {"files": len(pkls), "kb": total // 1024}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=400)
    parser.add_argument("--tier1", type=int, default=80, help="files to tier1 scan")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hw_cache_io_") as td:
        root = Path(td)
        fl_path = _write_files(root, args.files)
        print(f"files={args.files} tier1_sample={args.tier1}")
        print()

        for backend in ("pickle", "sqlite"):
            cache_dir = root / f"cache_{backend}"
            cache_dir.mkdir()
            db = _build_db(fl_path, cache_dir, backend)

            cold_t0 = _populate_tier0(db)
            cold_t1 = _populate_tier1(db, args.tier1)
            warm_t0 = _reread_tier0(db)
            warm_t1 = _reread_tier1(db, args.tier1)
            art = _artifact_stats(db)

            print(f"=== {backend} ===")
            print(f"  cold tier0: {cold_t0:8.1f}ms  tier1: {cold_t1:8.1f}ms")
            print(
                f"  warm tier0: {warm_t0:8.1f}ms  tier1: {warm_t1:8.1f}ms  "
                f"hits={db.cache_regex_hits}+{db.cache_validated_hits}"
            )
            print(f"  cache artifacts: {art['files']} files, {art['kb']} KiB")
            if cold_t0 > 0:
                print(f"  tier0 warm speedup: {cold_t0 / max(warm_t0, 0.1):.2f}x")
            if cold_t1 > 0:
                print(f"  tier1 warm speedup: {cold_t1 / max(warm_t1, 0.1):.2f}x")
            print()


if __name__ == "__main__":
    main()