#!/usr/bin/env python3
"""Compare path-walk cache backends: pickle vs sqlite (cold + warm runs)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import textwrap
import time
from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.preprocess import clear_include_unit_cache


def _write_chain_design(root: Path, *, n_blocks: int, children_per: int) -> Path:
    """Linear hierarchy: top -> block_i -> leaf_{i,j}."""
    paths: list[str] = []
    for i in range(n_blocks):
        leaves = []
        for j in range(children_per):
            leaf = root / f"leaf_{i}_{j}.v"
            leaf.write_text(
                textwrap.dedent(
                    f"""
                    module leaf_{i}_{j} (input a, output z);
                      assign z = a;
                    endmodule
                    """
                ),
                encoding="utf-8",
            )
            paths.append(str(leaf.resolve()))
            leaves.append(
                f"leaf_{i}_{j} u_{j} (.a(1'b0), .z());"
            )
        blk = root / f"block_{i}.v"
        blk.write_text(
            textwrap.dedent(
                f"""
                module block_{i} ();
                  {chr(10).join('  ' + ln for ln in leaves)}
                endmodule
                """
            ),
            encoding="utf-8",
        )
        paths.append(str(blk.resolve()))

    top = root / "top.v"
    insts = [f"block_{i} u_{i} ();" for i in range(n_blocks)]
    top.write_text(
        textwrap.dedent(
            f"""
            module top ();
              {chr(10).join('  ' + ln for ln in insts)}
            endmodule
            """
        ),
        encoding="utf-8",
    )
    paths.append(str(top.resolve()))

    checks = []
    for i in range(min(3, n_blocks)):
        for j in range(min(2, children_per)):
            checks.append(
                {
                    "id": f"c{i}_{j}",
                    "a": f"top.u_{i}.u_{j}.a",
                    "b": f"top.u_{i}.u_{j}.z",
                }
            )

    fl = root / "design.f"
    fl.write_text("\n".join(paths) + "\n", encoding="utf-8")
    (root / "checks.json").write_text(
        json.dumps({"top": "top", "checks": checks}),
        encoding="utf-8",
    )
    return fl


def _cache_artifacts(cache_root: Path) -> dict:
    if not cache_root.is_dir():
        return {"files": 0, "bytes": 0, "sqlite": False}
    sqlite = cache_root / "pw_cache.sqlite"
    if sqlite.is_file():
        return {
            "files": 1,
            "bytes": sqlite.stat().st_size,
            "sqlite": True,
        }
    files = list(cache_root.rglob("*.pkl"))
    total = sum(p.stat().st_size for p in files)
    return {"files": len(files), "bytes": total, "sqlite": False}


def _run_pw_pass(
    fl_path: Path,
    *,
    cache_dir: Path,
    backend: str,
    jobs: int,
) -> dict:
    clear_include_unit_cache()
    os.environ["HIERWALK_PW_CACHE"] = backend
    fl = parse_filelist(fl_path)
    checks_data = json.loads((fl_path.parent / "checks.json").read_text(encoding="utf-8"))
    request = ConnectivityRequest(
        checks=tuple(
            ConnectivityCheck(
                endpoint_a=c["a"],
                endpoint_b=c["b"],
                check_id=c["id"],
            )
            for c in checks_data["checks"]
        ),
        top="top",
    )
    t0 = time.perf_counter()
    batch, _, state = run_path_walk_connect(
        request,
        fl,
        top="top",
        cache_dir=cache_dir,
        jobs=jobs,
    )
    total_ms = (time.perf_counter() - t0) * 1000.0
    mod_db = state.mod_db

    cache_root = mod_db.cache_root
    artifacts = _cache_artifacts(cache_root) if cache_root else {"files": 0, "bytes": 0}
    return {
        "backend": backend,
        "total_ms": round(total_ms, 1),
        "checks": len(batch.results),
        "tier0_files": mod_db.files_regex_scanned,
        "tier1_files": mod_db.files_validated,
        "cache_regex_hits": mod_db.cache_regex_hits,
        "cache_validated_hits": mod_db.cache_validated_hits,
        "cache_artifacts": artifacts,
    }


def _bench_backend(
    fl_path: Path,
    *,
    backend: str,
    jobs: int,
    repeats: int,
) -> list[dict]:
    runs: list[dict] = []
    with tempfile.TemporaryDirectory(prefix=f"hw_pw_{backend}_") as td:
        cache_dir = Path(td) / "cache"
        cache_dir.mkdir()
        for i in range(repeats):
            label = "cold" if i == 0 else f"warm{i}"
            row = _run_pw_pass(fl_path, cache_dir=cache_dir, backend=backend, jobs=jobs)
            row["pass"] = label
            runs.append(row)
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=40, help="block_* modules")
    parser.add_argument("--children", type=int, default=5, help="leaves per block")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=2, help="runs per backend (1=cold only)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hw_pw_bench_design_") as td:
        root = Path(td)
        fl_path = _write_chain_design(
            root,
            n_blocks=args.blocks,
            children_per=args.children,
        )
        n_sources = len(fl_path.read_text(encoding="utf-8").strip().splitlines())
        print(f"design: {n_sources} RTL files, blocks={args.blocks}, children={args.children}")
        print(f"jobs={args.jobs}, repeats={args.repeats}")
        print()

        results: dict[str, list[dict]] = {}
        for backend in ("pickle", "sqlite"):
            print(f"=== backend={backend} ===")
            runs = _bench_backend(
                fl_path,
                backend=backend,
                jobs=args.jobs,
                repeats=args.repeats,
            )
            results[backend] = runs
            for row in runs:
                art = row["cache_artifacts"]
                print(
                    f"  {row['pass']:5s} total={row['total_ms']:8.1f}ms "
                    f"tier0={row['tier0_files']} tier1={row['tier1_files']} "
                    f"hits={row['cache_regex_hits']}+{row['cache_validated_hits']} "
                    f"cache_files={art['files']} cache_kb={art['bytes'] // 1024}"
                )
            print()

        if args.repeats >= 2:
            p_cold = results["pickle"][0]["total_ms"]
            p_warm = results["pickle"][1]["total_ms"]
            s_cold = results["sqlite"][0]["total_ms"]
            s_warm = results["sqlite"][1]["total_ms"]
            print("=== summary (warm pass, 2nd run) ===")
            print(f"  pickle cold: {p_cold:.1f}ms  warm: {p_warm:.1f}ms  speedup: {p_cold / max(p_warm, 0.1):.2f}x")
            print(f"  sqlite cold: {s_cold:.1f}ms  warm: {s_warm:.1f}ms  speedup: {s_cold / max(s_warm, 0.1):.2f}x")
            if p_warm > 0:
                print(f"  warm sqlite vs pickle: {p_warm / s_warm:.2f}x ({'sqlite faster' if s_warm < p_warm else 'pickle faster'})")
            p_art = results["pickle"][-1]["cache_artifacts"]
            s_art = results["sqlite"][-1]["cache_artifacts"]
            print(f"  cache artifacts: pickle {p_art['files']} files / sqlite {s_art['files']} files")


if __name__ == "__main__":
    main()