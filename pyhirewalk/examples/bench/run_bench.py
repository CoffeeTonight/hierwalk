#!/usr/bin/env python3
"""
Run hier_pyslang against golden connectivity cases.

  python3 examples/bench/run_bench.py
  python3 examples/bench/run_bench.py --only hard_patterns,serv
  python3 examples/bench/run_bench.py --list

Each config under configs/*.json may embed checks with:
  expect: "connected" | "disconnected"
  why: human rationale (ground truth note)

Exit 0 if all expectations match; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parents[1]  # pyhirewalk/
_CONFIGS = _BENCH / "configs"
_WORK = _BENCH / "work"
_LOGS = _BENCH / "logs"


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_configs() -> List[Path]:
    return sorted(_CONFIGS.glob("*.json"))


def expect_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    rcc = cfg.get("run_conn_check") or {}
    for ch in rcc.get("checks") or []:
        eid = ch.get("id")
        if not eid:
            continue
        exp = ch.get("expect")
        if exp is None:
            # ibex-style: no_conn_noise → disconnected; else connected preferred
            if "noise" in str(eid) or "no_conn" in str(eid):
                exp = "disconnected"
            else:
                exp = "connected"
        out[str(eid)] = str(exp)
    return out


def run_one(cfg_path: Path, *, no_cone_walk: bool = False) -> Dict[str, Any]:
    _WORK.mkdir(parents=True, exist_ok=True)
    _LOGS.mkdir(parents=True, exist_ok=True)
    name = cfg_path.stem
    out_json = _WORK / f"{name}.hier_pyslang.json"
    log_path = _LOGS / f"{name}.log"
    cmd = [
        sys.executable,
        str(_ROOT / "hier_pyslang.py"),
        "--config",
        str(cfg_path),
        "-o",
        str(out_json),
    ]
    if no_cone_walk:
        cmd.append("--no-cone-walk")
    # Prefer cone-files when modules_json present
    cfg = _load(cfg_path)
    if cfg.get("modules_json"):
        cmd.extend(["--cone-files", "--map", str(cfg["modules_json"])])

    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n--- stdout ---\n{proc.stdout}\n\n--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )

    result: Dict[str, Any] = {
        "name": name,
        "config": str(cfg_path),
        "rc": proc.returncode,
        "elapsed_sec": round(elapsed, 3),
        "out": str(out_json),
        "log": str(log_path),
        "checks": [],
        "pass": False,
        "error": None,
    }

    if proc.returncode not in (0, 1) or not out_json.is_file():
        # hier_pyslang uses 0/1 for ok with pairs; 2+ hard fail
        tail = (proc.stderr or "")[-2000:]
        result["error"] = f"tool rc={proc.returncode} out_exists={out_json.is_file()}\n{tail}"
        return result

    doc = _load(out_json)
    exp = expect_map(cfg)
    cfg_checks = {
        str(c.get("id")): c
        for c in (cfg.get("run_conn_check") or {}).get("checks") or []
        if c.get("id")
    }
    ok_all = True
    for ch in doc.get("checks") or []:
        cid = ch.get("id")
        n_pairs = len(ch.get("pairs") or [])
        want = exp.get(str(cid), "connected")
        meta = cfg_checks.get(str(cid), {})
        min_proc = int(meta.get("min_proc") or 0)
        min_hops = int(meta.get("min_evidence") or 0)
        min_pairs = int(meta.get("min_pairs") or 0)
        if want == "connected":
            good = n_pairs >= 1
        else:
            good = n_pairs == 0
        # Optional multi-FF / multi-hop requirements on best pair
        max_proc = 0
        max_ev = 0
        for pr in ch.get("pairs") or []:
            vias = [e.get("via") for e in (pr.get("evidence") or [])]
            # count posedge/always bodies even when walked reverse (proc_rev)
            max_proc = max(
                max_proc,
                sum(
                    1
                    for v in vias
                    if v == "proc" or v == "proc_rev" or (isinstance(v, str) and v.startswith("proc"))
                ),
            )
            max_ev = max(max_ev, len(pr.get("evidence") or []))
        if good and min_proc and max_proc < min_proc:
            good = False
        if good and min_hops and max_ev < min_hops:
            good = False
        # Fan-out / multi-sink: require enough distinct (src,dst) pairs found
        if good and min_pairs and n_pairs < min_pairs:
            good = False
        if not good:
            ok_all = False
        result["checks"].append(
            {
                "id": cid,
                "expect": want,
                "n_pairs": n_pairs,
                "pass": good,
                "unconnected": len(ch.get("unconnected") or []),
                "max_proc_in_pair": max_proc,
                "max_evidence": max_ev,
                "min_proc": min_proc or None,
                "min_evidence": min_hops or None,
                "min_pairs": min_pairs or None,
                "why": meta.get("why"),
            }
        )
    # also flag missing expected checks
    seen = {c["id"] for c in result["checks"]}
    for cid, want in exp.items():
        if cid not in seen:
            ok_all = False
            result["checks"].append(
                {
                    "id": cid,
                    "expect": want,
                    "n_pairs": None,
                    "pass": False,
                    "unconnected": None,
                    "why": "missing from tool output",
                }
            )
    result["pass"] = ok_all and proc.returncode in (0, 1)
    meta = doc.get("meta") or {}
    result["tool_stats"] = meta.get("stats")
    result["timings"] = meta.get("timings_sec")
    result["graph"] = meta.get("graph")
    result["n_diags"] = meta.get("n_diags")
    result["fatal"] = meta.get("fatal")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="pyhirewalk connectivity golden bench")
    ap.add_argument("--only", type=str, default="", help="comma list of config stems")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-cone-walk", action="store_true")
    ap.add_argument(
        "-o",
        type=Path,
        default=_WORK / "bench_summary.json",
        help="summary JSON path",
    )
    args = ap.parse_args(argv)

    configs = discover_configs()
    if args.list:
        for c in configs:
            print(c.stem, c)
        return 0

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    if only:
        configs = [c for c in configs if c.stem in only]

    if not configs:
        print("No configs found", file=sys.stderr)
        return 2

    summary: Dict[str, Any] = {
        "n_designs": len(configs),
        "results": [],
        "n_pass": 0,
        "n_fail": 0,
    }
    print(f"[bench] designs={len(configs)} root={_ROOT}", file=sys.stderr)
    for cfg_path in configs:
        print(f"[bench] RUN {cfg_path.stem} …", file=sys.stderr)
        r = run_one(cfg_path, no_cone_walk=args.no_cone_walk)
        summary["results"].append(r)
        status = "PASS" if r["pass"] else "FAIL"
        if r["pass"]:
            summary["n_pass"] += 1
        else:
            summary["n_fail"] += 1
        print(
            f"[bench] {status} {r['name']}  {r['elapsed_sec']}s  "
            f"checks={len(r['checks'])} diags={r.get('n_diags')} "
            f"err={r.get('error') is not None}",
            file=sys.stderr,
        )
        for ch in r["checks"]:
            mark = "✓" if ch["pass"] else "✗"
            extra = ""
            if ch.get("min_proc"):
                extra += f" proc={ch.get('max_proc_in_pair')}/{ch.get('min_proc')}"
            if ch.get("min_evidence"):
                extra += f" ev={ch.get('max_evidence')}/{ch.get('min_evidence')}"
            if ch.get("min_pairs"):
                extra += f" min_pairs={ch.get('min_pairs')}"
            print(
                f"         {mark} {ch['id']}: expect={ch['expect']} "
                f"pairs={ch['n_pairs']}{extra}",
                file=sys.stderr,
            )

    args.o.parent.mkdir(parents=True, exist_ok=True)
    args.o.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[bench] wrote {args.o}", file=sys.stderr)
    print(
        f"[bench] TOTAL pass={summary['n_pass']} fail={summary['n_fail']}",
        file=sys.stderr,
    )
    return 0 if summary["n_fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
