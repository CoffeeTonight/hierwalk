#!/usr/bin/env python3
"""Benchmark how fast path-walk connect gives up on missing hierarchy endpoints."""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import clear_path_walk_suite_session, run_path_walk_connect


def _median_ms(samples: list[float]) -> float:
    return statistics.median(samples) if samples else 0.0


def _build_request(
    top: str,
    *,
    count: int,
    valid_b: str,
    prefix: str,
) -> ConnectivityRequest:
    checks = tuple(
        ConnectivityCheck(
            f"{top}.{prefix}_{i}.probe_in",
            valid_b,
            check_id=f"missing_{i:03d}",
        )
        for i in range(count)
    )
    return ConnectivityRequest(checks=checks, top=top, include_ff=True)


def _run_once(
    request: ConnectivityRequest,
    flr,
    *,
    top: str,
    phase: str,
    no_cache: bool,
) -> tuple[float, list[bool], list[bool | None], int]:
    clear_path_walk_suite_session()
    t0 = time.perf_counter()
    batch, _index, state = run_path_walk_connect(
        request,
        flr,
        top=top,
        no_cache=no_cache,
        connect_phase=phase,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    connected = [r.connected for r in batch.results]
    text_flags = [r.connected_text for r in batch.results]
    return elapsed_ms, connected, text_flags, len(state.rows_by_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--filelist",
        default="examples/stress_seed42/filelist.f",
        help="Design filelist (default: stress_seed42)",
    )
    ap.add_argument("--top", default="stress_top")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--runs", type=int, default=3, help="Repeat full batch N times")
    ap.add_argument(
        "--valid-b",
        default="stress_top.u_spine.probe_out",
        help="Reachable B for missing-negative checks (shallow anchor near top)",
    )
    ap.add_argument("--prefix", default="u_missing_bench")
    ap.add_argument("--no-cache", action="store_true", default=True)
    ap.add_argument(
        "--phases",
        default="text,logical,both",
        help="Comma-separated connect_phase values to benchmark",
    )
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    fl_path = Path(args.filelist)
    if not fl_path.is_absolute():
        fl_path = root / fl_path
    index_cwd = fl_path.parent
    flr = parse_filelist(str(fl_path), index_cwd=str(index_cwd))
    request = _build_request(
        args.top,
        count=args.count,
        valid_b=args.valid_b,
        prefix=args.prefix,
    )
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    print(f"bench-missing: filelist={fl_path}")
    print(f"bench-missing: checks={args.count} top={args.top} runs={args.runs}")
    print(f"bench-missing: sample A={args.top}.{args.prefix}_0.probe_in")
    print(f"bench-missing: endpoint B={args.valid_b}")
    print()

    for phase in phases:
        totals: list[float] = []
        per_check: list[float] = []
        rows_seen = 0
        last_connected: list[bool] = []
        last_text: list[bool | None] = []
        for _ in range(args.runs):
            ms, connected, text_flags, rows = _run_once(
                request,
                flr,
                top=args.top,
                phase=phase,
                no_cache=args.no_cache,
            )
            totals.append(ms)
            per_check.append(ms / max(args.count, 1))
            rows_seen = rows
            last_connected = connected
            last_text = text_flags

        false_n = sum(1 for c in last_connected if not c)
        text_false_n = sum(1 for t in last_text if t is False or t is None and false_n)
        print(f"=== phase={phase} ===")
        print(f"  total median: { _median_ms(totals):.1f} ms  (per-check ~{_median_ms(per_check):.2f} ms)")
        print(f"  connected=False: {false_n}/{args.count}")
        if phase in ("text", "both"):
            print(f"  connected_text=False/None: {sum(1 for t in last_text if not t)}/{args.count}")
        print(f"  walk rows at end: {rows_seen}")
        if false_n != args.count:
            hits = [request.checks[i].check_id for i, c in enumerate(last_connected) if c]
            print(f"  WARNING unexpected connected=True: {hits[:5]}", file=sys.stderr)
            return 1
        print()

    print("bench-missing: OK all missing checks returned connected=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())