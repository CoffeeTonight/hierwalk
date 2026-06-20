#!/usr/bin/env python3
"""Rough index-build benchmark (synthetic filelists)."""

from __future__ import annotations

import argparse
import tempfile
import textwrap
import time
from pathlib import Path

from hierwalk.cache import build_design_index
from hierwalk.filelist import parse_filelist
from hierwalk.preprocess import clear_include_unit_cache
from hierwalk.generate_fold import _fold_body_cached


def _write_filelist(
    root: Path,
    *,
    n_files: int,
    generate_ratio: float,
    shared_include: bool,
) -> Path:
    if shared_include:
        inc = root / "common" / "defs.vh"
        inc.parent.mkdir(parents=True, exist_ok=True)
        inc.write_text(
            "\n".join(f"`define MACRO_{i} {i}" for i in range(100)) + "\n",
            encoding="utf-8",
        )
        inc_line = "+incdir+common\n"
    else:
        inc_line = ""

    paths: list[str] = []
    gen_every = max(1, int(1.0 / generate_ratio)) if generate_ratio > 0 else 0
    for i in range(n_files):
        p = root / f"blk_{i // 100}" / f"mod_{i}.v"
        p.parent.mkdir(parents=True, exist_ok=True)
        if gen_every and i % gen_every == 0:
            body = textwrap.dedent(
                f"""
                `include "defs.vh"
                module mod_{i} #(parameter N=2) (input clk);
                  generate
                    for (genvar g=0; g<N; g=g+1) begin : gen
                      child u ( .clk(clk) );
                    end
                  endgenerate
                endmodule
                """
            )
        else:
            body = textwrap.dedent(
                f"""
                module mod_{i} (input clk);
                  leaf u ( .clk(clk) );
                endmodule
                """
            )
        p.write_text(body, encoding="utf-8")
        paths.append(str(p.resolve()))

    fl = root / "design.f"
    fl.write_text(inc_line + "\n".join(paths) + "\n", encoding="utf-8")
    return fl


def _run_once(
    n_files: int,
    *,
    jobs: int,
    generate_ratio: float,
    shared_include: bool,
    low_memory: bool,
) -> float:
    _fold_body_cached.cache_clear()
    clear_include_unit_cache()
    with tempfile.TemporaryDirectory(prefix="hierwalk_bench_") as td:
        root = Path(td)
        fl_path = _write_filelist(
            root,
            n_files=n_files,
            generate_ratio=generate_ratio,
            shared_include=shared_include,
        )
        fl = parse_filelist(fl_path)
        t0 = time.perf_counter()
        build_design_index(
            fl,
            ignore_paths=[],
            ignore_path_files=[],
            ignore_modules=[],
            ignore_filelists=[],
            jobs=jobs,
            low_memory=low_memory,
        )
        return time.perf_counter() - t0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark hier-walk index build")
    parser.add_argument("-n", "--files", type=int, default=12000)
    parser.add_argument("-j", "--jobs", type=int, default=0)
    parser.add_argument(
        "--generate-ratio",
        type=float,
        default=0.05,
        help="fraction of modules with generate blocks (0..1)",
    )
    parser.add_argument("--shared-include", action="store_true", default=True)
    parser.add_argument("--no-shared-include", action="store_false", dest="shared_include")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--low-memory",
        action="store_true",
        help="use fused per-file build instead of default 2-pass",
    )
    args = parser.parse_args()

    times: list[float] = []
    for i in range(args.repeat):
        elapsed = _run_once(
            args.files,
            jobs=args.jobs,
            generate_ratio=args.generate_ratio,
            shared_include=args.shared_include,
            low_memory=args.low_memory,
        )
        times.append(elapsed)
        print(f"run {i + 1}: {elapsed:.2f}s")

    best = min(times)
    print(
        f"files={args.files} jobs={args.jobs} "
        f"generate_ratio={args.generate_ratio} "
        f"shared_include={args.shared_include} "
        f"low_memory={args.low_memory} "
        f"best={best:.2f}s"
    )


if __name__ == "__main__":
    main()